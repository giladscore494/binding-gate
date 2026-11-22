"""Arm ``gated``: the no_gate candidate, judged by the deterministic binding gate.

PREREGISTRATION.md sections 5 and 5.1; ARCHITECTURE.md sections 3, 8 and 8a.

What one call of :meth:`GatedArm.run` does
------------------------------------------
1. **Candidate.** Builds the answer request with
   ``answering.build_answer_request(arm_input, presentation)`` and sends it with
   ``answering.call_model`` (via ``answer_once``) - exactly what ``no_gate``
   does. The request is therefore identical, and the runner's response cache
   (key: model label, repeat, request) hands this arm the very candidate that
   ``no_gate`` produced for the same item, model and repeat ("same candidate,
   different gate"). When ``no_gate`` runs in the same run, this arm makes no
   additional model call; its answer call is logged with
   ``raw["cache_hit"] = true``.
2. **Pass-through.** If the candidate is ``abstained`` or ``unparseable`` it is
   the final answer unchanged; no decomposition is requested and no verdict is
   attached (a gate only judges answered candidates; the contracts enforce it).
3. **Constraints (A).** For an answered candidate, the question is decomposed by
   the decomposer named in ``options.decomposer``, built through the registry
   and used only through the ``Decomposer`` protocol (``decompose(question) ->
   Decomposition``). See "Decomposition sharing" below.
4. **Checks and verdict (C, D, E).** The ``binding`` gate runs the checks named
   in ``options.checks`` with the shared ``options.parameters`` and returns the
   verdict (``binding_gate.gates.binding``).
5. **Final answer.** ACCEPT: the candidate. RESEARCH or ABSTAIN: an abstention
   (``raw = ""``, ``reason = "gate <decision>: <reasons>"``). There is no search
   in layers 1 and 2, so a RESEARCH verdict is final and counts as an
   abstention (section 5).

The trace record then holds one ``Attempt`` (candidate, evidence references,
verdict or ``None``), the constraints, and every model call (answer first, then
decomposition).

Decomposition sharing
---------------------
The runner builds one arm instance per (arm label, model, repeat). So that all
gated arms (including ablations), all models and all repeats of a process are
judged against the SAME constraints for a question, decompositions are kept in
a process-wide memo keyed by ``SHA-256(canonical JSON of {decomposer spec,
question})``. The first record that needs a question's constraints makes the
decomposer's calls; later records reuse the result and list the same calls with
``response.raw["cache_hit"] = true`` (charged nothing, like the runner's own
cache). A failed decomposition is not memoised. The memo is not rebuilt on
resume: questions decomposed before an interruption are decomposed again
afterwards (recorded in the trace either way).

The decomposer is created lazily, when the first answered candidate needs it.
Hence a dry run - whose recording client always returns an abstention - never
creates or calls the decomposer, and no decomposer API can be reached by it.

Options
-------
``decomposer``    ``{name = "<registered decomposer>", options = {...}}`` (required)
``checks``        list of check names (required; see the reserved names)
``parameters``    shared gate parameters (``window_tokens``, ``min_quote_tokens``,
                  ``lead_max_tokens``, ``lead_min_repeats``, ``premise_scope``);
                  unset ones take the PROPOSED defaults of ``gates.binding``
``presentation``  answer prompt layout (``binding_gate.answering.PRESENTATIONS``)
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Mapping

from binding_gate import registry
from binding_gate.answering import DEFAULT_PRESENTATION, PRESENTATIONS, ModelCallFailed, answer_once
from binding_gate.canonical import canonical_json, sha256_json
from binding_gate.contracts import (
    AnswerCandidate,
    AnswerStatus,
    ArmInput,
    ArmOutput,
    Attempt,
    Decision,
    Decomposition,
    ModelCall,
    ModelResponse,
)
from binding_gate.gates.binding import NAME as GATE_NAME
from binding_gate.interfaces import ModelClient

NAME = "gated"
CACHE_HIT_KEY = "cache_hit"  # the same flag the runner sets on reused responses
_SPEC_KEYS = {"name", "options"}


def decomposer_spec(raw: Any) -> dict[str, Any]:
    """Validate ``options.decomposer`` and return it as ``{"name", "options"}``."""
    if not isinstance(raw, Mapping):
        raise ValueError('decomposer must be a table {name = "...", options = {...}}')
    unknown = sorted(set(raw) - _SPEC_KEYS)
    if unknown:
        raise ValueError(f"decomposer: unknown key(s) {unknown}; allowed: {sorted(_SPEC_KEYS)}")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("decomposer.name must be a non-empty string")
    options = raw.get("options", {})
    if not isinstance(options, Mapping):
        raise ValueError("decomposer.options must be a table")
    canonical_json(options)  # JSON-compatible, so that it can be part of the memo key
    return {"name": name, "options": dict(options)}


def _as_cache_hit(call: ModelCall) -> ModelCall:
    if call.response is None:
        return call
    response = call.response
    return ModelCall(
        purpose=call.purpose,
        client=call.client,
        request=call.request,
        started_at=call.started_at,
        ended_at=call.ended_at,
        response=ModelResponse(
            text=response.text,
            model=response.model,
            usage=response.usage,
            raw={**response.raw, CACHE_HIT_KEY: True},
        ),
    )


class DecompositionMemo:
    """Thread-safe, process-wide memo: key -> Decomposition. Concurrent identical requests decompose once."""

    def __init__(self) -> None:
        self._done: dict[str, Decomposition] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get_or_compute(self, key: str, compute: Callable[[], Decomposition]) -> tuple[Decomposition, bool]:
        """Return ``(decomposition, reused)``."""
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            with self._guard:
                done = self._done.get(key)
            if done is not None:
                return done, True
            result = compute()
            if not isinstance(result, Decomposition):
                raise TypeError(f"decomposer returned {type(result).__name__}, expected Decomposition")
            with self._guard:
                self._done[key] = result
            return result, False

    def clear(self) -> None:
        with self._guard:
            self._done.clear()
            self._locks.clear()

    def __len__(self) -> int:
        return len(self._done)


MEMO = DecompositionMemo()


def clear_decomposition_memo() -> None:
    """Forget every memoised decomposition (for tests and for long-lived processes)."""
    MEMO.clear()


@registry.register("arm", NAME)
class GatedArm:
    """See the module docstring. ``model`` is the client under test, passed by the runner."""

    name = NAME

    def __init__(
        self,
        *,
        model: ModelClient,
        decomposer: Mapping[str, Any],
        checks: list[str],
        parameters: Mapping[str, Any] | None = None,
        presentation: str = DEFAULT_PRESENTATION,
    ):
        if presentation not in PRESENTATIONS:
            raise ValueError(f"unknown presentation {presentation!r}; available: {sorted(PRESENTATIONS)}")
        if parameters is not None and not isinstance(parameters, Mapping):
            raise ValueError("parameters must be a table")
        self.model = model
        self.presentation = presentation
        self.decomposer_spec = decomposer_spec(decomposer)
        self.gate = registry.create("gate", GATE_NAME, checks=list(checks), **dict(parameters or {}))
        self._decomposer: Any = None
        self._lock = threading.Lock()

    def _get_decomposer(self) -> Any:
        with self._lock:
            if self._decomposer is None:
                spec = self.decomposer_spec
                self._decomposer = registry.create("decomposer", spec["name"], **spec["options"])
            return self._decomposer

    def decompose(self, question: str) -> tuple[Decomposition, tuple[ModelCall, ...]]:
        """Constraints for ``question`` and the model calls to record for this item."""
        key = sha256_json({"decomposer": self.decomposer_spec, "question": question})
        decomposition, reused = MEMO.get_or_compute(key, lambda: self._get_decomposer().decompose(question))
        calls = decomposition.model_calls
        if reused:
            calls = tuple(_as_cache_hit(call) for call in calls)
        return decomposition, tuple(calls)

    def run(self, arm_input: ArmInput) -> ArmOutput:
        answer, answer_calls = answer_once(self.model, arm_input, self.presentation)
        evidence = tuple(d.ref() for d in arm_input.documents)
        if answer.status is not AnswerStatus.ANSWERED:
            attempt = Attempt(answer=answer, evidence=evidence)
            return ArmOutput(attempts=(attempt,), final_answer=answer, model_calls=answer_calls)
        try:
            decomposition, decompose_calls = self.decompose(arm_input.question)
        except Exception as exc:  # noqa: BLE001 - keep the answer call in the error record
            raise ModelCallFailed(
                f"decomposition failed: {type(exc).__name__}: {exc}",
                answer_calls + tuple(getattr(exc, "model_calls", ())),
            ) from exc
        constraints = decomposition.constraints
        verdict = self.gate.evaluate(constraints, answer, arm_input.documents)
        if verdict.decision is Decision.ACCEPT:
            final = answer
        else:
            final = AnswerCandidate.abstained(
                raw="", reason=f"gate {verdict.decision.value}: " + "; ".join(verdict.reasons)
            )
        return ArmOutput(
            attempts=(Attempt(answer=answer, evidence=evidence, verdict=verdict),),
            final_answer=final,
            constraints=constraints,
            model_calls=answer_calls + decompose_calls,
        )
