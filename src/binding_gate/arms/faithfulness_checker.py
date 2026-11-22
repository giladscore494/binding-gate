"""Arm ``faithfulness_checker``: the ``no_gate`` candidate, accepted or blocked by MiniCheck.

PREREGISTRATION.md section 5: "``no_gate`` answer, then an existing open-source
claim-versus-source faithfulness checker decides accept/block." This is the
comparison arm of hypothesis H3.

Same candidate, different gate
------------------------------
The candidate is obtained exactly as in ``no_gate``: the request is built with
``binding_gate.answering.build_answer_request`` and sent with ``call_model``
(purpose ``answer``). Within a run the runner answers an identical request for
the same model label and repeat from its cache, so this arm judges the very
candidate ``no_gate`` produced and makes no extra call to the model under test.
Abstentions and unparseable candidates pass through unchanged (no verdict).

The checker
-----------
Selected under the rule of PREREGISTRATION.md section 5 (reasons there):
MiniCheck (Tang, Laban and Durrett, EMNLP 2024), the model
``MiniCheck-Flan-T5-Large``.

* code: https://github.com/Liyan06/MiniCheck at commit :data:`MINICHECK_COMMIT`,
  Apache-2.0 (LICENSE file of the repository). Not on PyPI: the PyPI project
  named ``minicheck`` is an unrelated model checker, so the package is
  installed from the pinned git commit through this project's
  ``faithfulness`` extra.
* weights: Hugging Face ``lytang/MiniCheck-Flan-T5-Large`` at revision
  :data:`MINICHECK_WEIGHTS_REVISION`, MIT (model card).
* decision: MiniCheck's own label, ``1`` (supported) when the support
  probability is greater than 0.5 (``MiniCheck._score_inferencer``); this
  threshold is the package default and is not tuned.
* chunking: the package default for this model (documents split at sentence
  boundaries into chunks of about 500 words; the maximum over chunks counts).

How the arm applies it (fixed; changing it is an amendment)
-----------------------------------------------------------
* claim = the candidate's ``value`` (the answer). MiniCheck's README says a
  multi-sentence claim should be split into sentences first; the value is split
  with NLTK's ``sent_tokenize`` (the tokenizer MiniCheck itself uses) and every
  sentence is checked.
* evidence (option ``evidence``): ``all_documents`` (default) = the texts of all
  documents shown to the model, in the order shown, separated by a blank line,
  without headers. This is the usual way such checkers are used in
  retrieval-augmented generation: is the answer supported by the retrieved
  context? ``cited_document`` = only the document named in ``source_id``
  (a cited id that is not a shown document blocks the answer).
* accept if and only if every sentence of the claim is labelled supported;
  otherwise the arm abstains with the reason. The quote is not given to the
  checker: it is evidence, not the claim, and judging the quote is the gate's
  job.

The checker is deliberately given the question's answer only, not the
question: that is what a claim-versus-source checker evaluates, and it is
exactly why such a checker can pass a supported claim about the wrong entity
(PREREGISTRATION.md section 1).

Backends and tests
------------------
The heavy dependencies (PyTorch, Transformers, the model weights) are needed
only when a candidate is actually checked and are imported lazily: building
the arm, the dry run and the test suite never import them. When the arm is
built for a real run (with a provider client, not the dry run's recording
client) it checks cheaply, without network or PyTorch, that the pinned package
and the NLTK data are installed, so that a missing extra stops the run before
the first paid call instead of producing error records. Backends are looked
up in :data:`BACKENDS`; tests register a fake one. One backend instance per
process is shared by all arm instances (the runner builds one arm per model
and repeat) and its calls are serialised, so the model is loaded once.

Preparation (once per machine): ``pip install -e ".[faithfulness]"``, then
``python -m binding_gate.arms.faithfulness_checker prepare`` (downloads the
NLTK sentence tokenizer and the pinned weights, about 3 GB), then
``python -m binding_gate.arms.faithfulness_checker verify`` (two invented
examples, prints the probabilities and the time per check).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

from binding_gate.answering import DEFAULT_PRESENTATION, PRESENTATIONS, build_answer_request, call_model
from binding_gate.canonical import canonical_json
from binding_gate.contracts import (
    AnswerCandidate,
    AnswerStatus,
    ArmInput,
    ArmOutput,
    Attempt,
    CheckResult,
    CheckStatus,
    Decision,
    Document,
    Verdict,
)
from binding_gate.interfaces import ModelClient
from binding_gate.judging import unwrap_client
from binding_gate.parsing import parse_answer
from binding_gate.registry import register

CHECK_NAME = "faithfulness_checker"
EVIDENCE_MODES = ("all_documents", "cited_document")
DEFAULT_EVIDENCE = "all_documents"
DEFAULT_BACKEND = "minicheck"

MINICHECK_REPOSITORY = "https://github.com/Liyan06/MiniCheck"
MINICHECK_COMMIT = "b58b9fa69acbd1015ec970fa65dd752413a053d2"
MINICHECK_MODEL_NAME = "flan-t5-large"
MINICHECK_CHECKPOINT = "lytang/MiniCheck-Flan-T5-Large"
MINICHECK_WEIGHTS_REVISION = "96eafd01cee2d16cf81aaa2fb226b14f422a37b3"
MINICHECK_DECISION_RULE = "MiniCheck pred_label: supported iff support probability > 0.5 (package default)"
NLTK_RESOURCE = "tokenizers/punkt_tab"
INSTALL_HINT = 'pip install -e ".[faithfulness]" && python -m binding_gate.arms.faithfulness_checker prepare'


class FaithfulnessCheckerUnavailable(RuntimeError):
    """The checker cannot run here (package missing, wrong version, missing data or weights)."""


class FaithfulnessCheckFailed(RuntimeError):
    """Checking one candidate failed. ``model_calls`` keeps the answer call, so the
    runner's error record shows what was answered (ARCHITECTURE.md section 8)."""

    def __init__(self, message: str, model_calls: Sequence[Any] = ()):
        super().__init__(message)
        self.model_calls = tuple(model_calls)


@dataclass(frozen=True)
class SentenceVerdict:
    sentence: str
    supported: bool
    support_probability: float


class FaithfulnessBackend(Protocol):
    """What the arm needs from a checker implementation."""

    name: str

    def describe(self) -> dict[str, Any]:
        """Identity of the checker (package, version pin, weights, decision rule) for the trace."""
        ...

    def split_claim(self, claim: str) -> list[str]:
        """The sentences of ``claim`` as the checker's documentation prescribes."""
        ...

    def check(self, document: str, claims: Sequence[str]) -> list[tuple[bool, float]]:
        """``(supported, support probability)`` for each claim against ``document``."""
        ...


class MiniCheckBackend:
    """MiniCheck-Flan-T5-Large at the pinned commit and weights revision (see the module docstring).

    Option ``cache_dir``: where Hugging Face weights are cached (default: the
    standard Hugging Face cache). It changes no result.
    """

    name = "minicheck"

    def __init__(self, *, cache_dir: str | None = None):
        if cache_dir is not None and (not isinstance(cache_dir, str) or not cache_dir):
            raise ValueError("cache_dir must be a non-empty string or omitted")
        self.cache_dir = cache_dir
        self._scorer: Any = None
        self._sent_tokenize: Callable[[str], list[str]] | None = None
        self._lock = threading.Lock()

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "package": "minicheck",
            "repository": MINICHECK_REPOSITORY,
            "commit": MINICHECK_COMMIT,
            "model_name": MINICHECK_MODEL_NAME,
            "checkpoint": MINICHECK_CHECKPOINT,
            "weights_revision": MINICHECK_WEIGHTS_REVISION,
            "decision_rule": MINICHECK_DECISION_RULE,
        }

    # -- environment checks (each raises FaithfulnessCheckerUnavailable) ------ #

    @staticmethod
    def installed_commit() -> str | None:
        """The git commit the installed ``minicheck`` distribution was built from, if recorded."""
        import importlib.metadata

        try:
            distribution = importlib.metadata.distribution("minicheck")
        except importlib.metadata.PackageNotFoundError:
            return None
        text = distribution.read_text("direct_url.json")
        if not text:
            return None
        try:
            return json.loads(text).get("vcs_info", {}).get("commit_id")
        except (ValueError, AttributeError):
            return None

    def ensure_ready(self) -> None:
        """Cheap check, without network or PyTorch: pinned package installed, NLTK data present."""
        commit = self.installed_commit()
        if commit != MINICHECK_COMMIT:
            raise FaithfulnessCheckerUnavailable(
                f"MiniCheck must be installed from {MINICHECK_REPOSITORY} at commit {MINICHECK_COMMIT}; "
                f"installed: {commit or 'not installed, or not from git'}. Run: {INSTALL_HINT}"
            )
        try:
            import nltk

            nltk.data.find(NLTK_RESOURCE)
        except (ImportError, LookupError) as exc:
            raise FaithfulnessCheckerUnavailable(
                f"the NLTK resource {NLTK_RESOURCE} is missing ({type(exc).__name__}). Run: {INSTALL_HINT}"
            ) from None

    def _verify_environment(self) -> None:
        self.ensure_ready()
        # MiniCheck loads the checkpoint at revision "main"; it is used only while
        # "main" is the pinned revision, so that a silent upstream change cannot alter results.
        try:
            from huggingface_hub import HfApi

            current = HfApi().model_info(MINICHECK_CHECKPOINT, revision="main").sha
        except Exception as exc:  # noqa: BLE001 - any failure means the pin cannot be confirmed
            raise FaithfulnessCheckerUnavailable(
                f"cannot confirm that {MINICHECK_CHECKPOINT} 'main' is the pinned revision "
                f"{MINICHECK_WEIGHTS_REVISION}: {type(exc).__name__}: {exc}"
            ) from None
        if current != MINICHECK_WEIGHTS_REVISION:
            raise FaithfulnessCheckerUnavailable(
                f"{MINICHECK_CHECKPOINT} 'main' is now revision {current}, not the pinned "
                f"{MINICHECK_WEIGHTS_REVISION}; the checker is not run (record an amendment to re-pin)"
            )

    def _load(self) -> None:
        if self._scorer is not None:
            return
        self._verify_environment()
        try:
            from minicheck.minicheck import MiniCheck
            from nltk.tokenize import sent_tokenize
        except ImportError as exc:
            raise FaithfulnessCheckerUnavailable(f"cannot import MiniCheck: {exc}. Run: {INSTALL_HINT}") from None
        self._scorer = MiniCheck(model_name=MINICHECK_MODEL_NAME, cache_dir=self.cache_dir)
        self._sent_tokenize = sent_tokenize

    def split_claim(self, claim: str) -> list[str]:
        with self._lock:
            self._load()
            assert self._sent_tokenize is not None
            return [s for s in (part.strip() for part in self._sent_tokenize(claim)) if s]

    def check(self, document: str, claims: Sequence[str]) -> list[tuple[bool, float]]:
        with self._lock:
            self._load()
            labels, probabilities, _chunks, _per_chunk = self._scorer.score(
                docs=[document] * len(claims), claims=list(claims)
            )
        return [(int(label) == 1, float(probability)) for label, probability in zip(labels, probabilities)]


BACKENDS: dict[str, Callable[..., FaithfulnessBackend]] = {"minicheck": MiniCheckBackend}
"""Backend name -> factory. Only ``minicheck`` is part of the study; tests add fakes."""

_INSTANCES: dict[str, FaithfulnessBackend] = {}
_INSTANCES_LOCK = threading.Lock()


def get_backend(name: str, options: dict[str, Any]) -> FaithfulnessBackend:
    """The process-wide backend instance for ``(name, options)`` (built on first use, not loaded)."""
    try:
        factory = BACKENDS[name]
    except KeyError:
        raise ValueError(f"unknown faithfulness backend {name!r}; available: {sorted(BACKENDS)}") from None
    key = f"{name}:{canonical_json(options)}"
    with _INSTANCES_LOCK:
        instance = _INSTANCES.get(key)
        if instance is None:
            instance = factory(**options)
            _INSTANCES[key] = instance
        return instance


def reset_backends() -> None:
    """Forget the shared backend instances (for tests)."""
    with _INSTANCES_LOCK:
        _INSTANCES.clear()


def evidence_text(documents: Sequence[Document], answer: AnswerCandidate, mode: str) -> str | None:
    """The text the claim is checked against, or ``None`` if the cited document was not shown."""
    if mode == "all_documents":
        return "\n\n".join(document.text for document in documents)
    cited = [document.text for document in documents if document.id == answer.source_id]
    return cited[0] if cited else None


# Registered only when imported as a module, not when run with ``python -m`` (the command
# below then runs the imported module, so the component is registered exactly once).
@register("arm", "faithfulness_checker") if __name__ != "__main__" else (lambda component: component)
class FaithfulnessCheckerArm:
    """Options: ``presentation``; ``backend`` (default ``minicheck``); ``backend_options``
    (table, default empty); ``evidence`` (``all_documents`` default, or ``cited_document``)."""

    name = "faithfulness_checker"

    def __init__(
        self,
        *,
        model: ModelClient,
        presentation: str = DEFAULT_PRESENTATION,
        backend: str = DEFAULT_BACKEND,
        backend_options: dict[str, Any] | None = None,
        evidence: str = DEFAULT_EVIDENCE,
    ):
        if presentation not in PRESENTATIONS:
            raise ValueError(f"unknown presentation {presentation!r}; available: {sorted(PRESENTATIONS)}")
        if evidence not in EVIDENCE_MODES:
            raise ValueError(f"evidence must be one of {list(EVIDENCE_MODES)}, got {evidence!r}")
        if backend_options is not None and not isinstance(backend_options, dict):
            raise ValueError("backend_options must be a table")
        self.model = model
        self.presentation = presentation
        self.evidence = evidence
        # Built now (validates the options), loaded only when the first candidate is checked.
        self.backend = get_backend(backend, dict(backend_options or {}))
        # With a real provider client (not the dry run's recording client), check now that the
        # checker can run at all, so a missing extra does not turn every answer into an error record.
        ensure_ready = getattr(self.backend, "ensure_ready", None)
        if ensure_ready is not None and getattr(unwrap_client(model), "provider", None) is not None:
            ensure_ready()

    def run(self, arm_input: ArmInput) -> ArmOutput:
        request = build_answer_request(arm_input, self.presentation)  # identical to no_gate -> cache hit
        response, calls = call_model(self.model, request, "answer")
        answer = parse_answer(response.text)
        evidence_refs = tuple(d.ref() for d in arm_input.documents)
        if answer.status is not AnswerStatus.ANSWERED:
            attempt = Attempt(answer=answer, evidence=evidence_refs)
            return ArmOutput(attempts=(attempt,), final_answer=answer, model_calls=calls)

        try:
            verdict = self.decide(arm_input.documents, answer)
        except Exception as exc:  # noqa: BLE001 - becomes an error record that keeps the answer call
            raise FaithfulnessCheckFailed(f"faithfulness check failed: {type(exc).__name__}: {exc}", calls) from exc
        if verdict.decision is Decision.ACCEPT:
            final = answer
        else:
            final = AnswerCandidate.abstained(raw="", reason=f"blocked by {CHECK_NAME}: {verdict.reasons[0]}")
        attempt = Attempt(answer=answer, evidence=evidence_refs, verdict=verdict)
        return ArmOutput(attempts=(attempt,), final_answer=final, model_calls=calls)

    def decide(self, documents: Sequence[Document], answer: AnswerCandidate) -> Verdict:
        """Map the checker's output for one answered candidate to a verdict (accept or abstain)."""
        details: dict[str, Any] = {"checker": self.backend.describe(), "evidence": self.evidence}
        document = evidence_text(documents, answer, self.evidence)
        if document is None:
            reason = f"the cited source {answer.source_id!r} is not one of the shown documents"
            result = CheckResult(CHECK_NAME, CheckStatus.FAIL, {**details, "reason": reason})
            return Verdict(Decision.ABSTAIN, reasons=(reason,), check_results=(result,))
        claim = answer.value or ""
        sentences = self.backend.split_claim(claim) or [claim]
        scored = self.backend.check(document, sentences)
        if len(scored) != len(sentences):
            raise FaithfulnessCheckerUnavailable(
                f"the checker returned {len(scored)} results for {len(sentences)} claim sentences"
            )
        verdicts = [SentenceVerdict(s, supported, p) for s, (supported, p) in zip(sentences, scored)]
        details["sentences"] = [
            {"sentence": v.sentence, "supported": v.supported, "support_probability": v.support_probability}
            for v in verdicts
        ]
        if all(v.supported for v in verdicts):
            result = CheckResult(CHECK_NAME, CheckStatus.PASS, details)
            return Verdict(Decision.ACCEPT, check_results=(result,))
        unsupported = sum(not v.supported for v in verdicts)
        reason = (
            f"{unsupported} of {len(verdicts)} sentence(s) of the answer not supported by the "
            f"{'shown documents' if self.evidence == 'all_documents' else 'cited document'} "
            f"according to the faithfulness checker"
        )
        result = CheckResult(CHECK_NAME, CheckStatus.FAIL, details)
        return Verdict(Decision.ABSTAIN, reasons=(reason,), check_results=(result,))


# --------------------------------------------------------------------------- #
# Preparation and verification on a machine that will run the checker
# --------------------------------------------------------------------------- #

_VERIFY_DOCUMENT = (
    "Velorin is an invented compound. In a made-up trial of 40 adults with an invented skin "
    "condition, velorin cleared the rash in 12 participants within four weeks."
)
_VERIFY_CLAIMS = (
    "Velorin cleared the rash in 12 participants.",  # supported by the invented document
    "Velorin cleared the rash in every participant.",  # contradicted by it
)


def _prepare(cache_dir: str | None) -> int:
    try:
        import nltk
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        print(f"ERROR: {exc}. Install the extra first: pip install -e \".[faithfulness]\"", file=sys.stderr)
        return 1
    if not nltk.download("punkt_tab", quiet=True):
        print("ERROR: could not download the NLTK resource punkt_tab", file=sys.stderr)
        return 1
    path = snapshot_download(MINICHECK_CHECKPOINT, revision=MINICHECK_WEIGHTS_REVISION, cache_dir=cache_dir)
    print(f"NLTK punkt_tab ready; {MINICHECK_CHECKPOINT}@{MINICHECK_WEIGHTS_REVISION} in {path}")
    return 0


def _verify(cache_dir: str | None) -> int:
    backend = MiniCheckBackend(cache_dir=cache_dir)
    try:
        started = time.perf_counter()
        results = backend.check(_VERIFY_DOCUMENT, list(_VERIFY_CLAIMS))
        elapsed = time.perf_counter() - started
    except FaithfulnessCheckerUnavailable as exc:
        print(f"NOT READY: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(backend.describe(), indent=2))
    for claim, (supported, probability) in zip(_VERIFY_CLAIMS, results):
        print(f"{'supported' if supported else 'NOT supported':>13}  p={probability:.4f}  {claim}")
    print(f"{elapsed:.1f} s for model loading plus {len(results)} checks")
    expected = [True, False]
    if [supported for supported, _ in results] != expected:
        print("WARNING: the invented examples were not labelled as expected", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m binding_gate.arms.faithfulness_checker",
        description="Prepare and verify the MiniCheck faithfulness checker (no model API is called).",
    )
    parser.add_argument("command", choices=["prepare", "verify"])
    parser.add_argument("--cache-dir", default=None, help="Hugging Face cache directory (default: standard cache)")
    args = parser.parse_args(argv)
    return _prepare(args.cache_dir) if args.command == "prepare" else _verify(args.cache_dir)


if __name__ == "__main__":
    from binding_gate.arms.faithfulness_checker import main as _main

    sys.exit(_main())
