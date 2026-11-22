"""Fakes for the PR 5 tests: a model under test with a family, a scripted judge, a checker backend.

Nothing here touches the network or loads a model. Registered model components:

``pr5_family_fake``  the PR 2 scripted fake (``tests/fakes.py``) plus a ``provider``
                     attribute, so that the judge-family rule can be applied
``pr5_fake_judge``   a judge client whose reply is chosen by ``behaviour``
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Sequence

from fakes import ScriptedFake
from binding_gate.arms.llm_judge_checks import CHECK_NAMES
from binding_gate.contracts import ModelRequest, ModelResponse, TokenUsage
from binding_gate.models._http import ModelError, PreflightResult, RetryPolicy
from binding_gate.registry import register

JUDGE_CALLS: list[tuple[str, ModelRequest]] = []
_LOCK = threading.Lock()

FAKE_JUDGE_PRICE = """
[[price]]
model_id = "fake-judge"
input_per_mtok = 2.0
output_per_mtok = 10.0
source = "test"
accessed = "2026-11-22"
"""


@register("model", "pr5_family_fake")
class FamilyFake(ScriptedFake):
    def __init__(self, *, provider: str = "openai", **options: Any):
        super().__init__(**options)
        self.provider = provider


def requested_checks(request: ModelRequest) -> list[str]:
    text = request.messages[-1].content
    section = text.split("Checks:\n", 1)[1].split("\n\nReply with", 1)[0]
    return [name for name in re.findall(r"^- (\w+): ", section, re.MULTILINE) if name in CHECK_NAMES]


def benchmark_judgment(source: str | None, *, substitution: bool = False) -> dict[str, Any]:
    labels = {"Document 1": "correct_attribution", "Document 2": "entity_attribution_failure", "Neither": "confabulation"}
    claims = [] if source is None else [{"quote": "a claim", "source": source, "label": labels[source]}]
    return {
        "claims": claims,
        "failure_detected": source == "Document 2",
        "failure_count": int(source == "Document 2"),
        "confabulation_count": int(source == "Neither"),
        "entity_substitution_detected": substitution,
        "entity_substitution_count": int(substitution),
        "judge_notes": "",
    }


@register("model", "pr5_fake_judge")
class FakeJudge:
    """``behaviour``:

    ``pass`` / ``fail:<check>``   llm_judge_checks reply, all pass / one check fails
    ``garbage``                   prose without JSON
    ``truncated``                 a valid pass reply flagged as truncated
    ``crash``                     non-retryable error
    ``bench_auto``                benchmark judgment: Document 2 if the response cites cy_doc,
                                  Document 1 if it cites cx_doc, no claims otherwise
    ``bench_bad``                 benchmark judgment with inconsistent counts
    """

    def __init__(
        self,
        *,
        label: str | None = None,
        model_id: str = "fake-judge",
        provider: str = "moonshot",
        behaviour: str = "pass",
        max_output_tokens: int = 4096,
        input_tokens: int = 2000,
        output_tokens: int = 300,
        preflight_ok: bool = True,
        finish_reason: str = "stop",
    ):
        self.name = label or model_id
        self.model_id = model_id
        self.provider = provider
        self.behaviour = behaviour
        self.max_output_tokens = max_output_tokens
        self.retry = RetryPolicy(max_retries=0, backoff_seconds=0.0)
        self.secret_name = ""
        self._usage = TokenUsage(input_tokens, output_tokens)
        self._preflight_ok = preflight_ok
        self._finish = finish_reason

    def preflight(self) -> PreflightResult:
        return PreflightResult(self.name, self.model_id, self._preflight_ok, "listed" if self._preflight_ok else "unknown id")

    def _checks_reply(self, request: ModelRequest, failing: str | None) -> str:
        results = {name: ("fail" if name == failing else "pass") for name in requested_checks(request)}
        return json.dumps({"checks": results, "explanation": "scripted"})

    def complete(self, request: ModelRequest) -> ModelResponse:
        with _LOCK:
            JUDGE_CALLS.append((self.name, request))
        truncated = False
        if self.behaviour == "crash":
            raise ModelError("HTTP 400: scripted judge failure", status=400)
        if self.behaviour == "pass":
            text = self._checks_reply(request, None)
        elif self.behaviour.startswith("fail:"):
            text = self._checks_reply(request, self.behaviour.split(":", 1)[1])
        elif self.behaviour == "truncated":
            text, truncated = self._checks_reply(request, None), True
        elif self.behaviour == "garbage":
            text = "I think the answer is fine."
        elif self.behaviour == "bench_auto":
            response = request.messages[-1].content.split("\n\nResponse: ", 1)[1].split("\n\n---\n", 1)[0]
            source = "Document 2" if "cy_doc" in response else "Document 1" if "cx_doc" in response else None
            text = "```json\n" + json.dumps(benchmark_judgment(source)) + "\n```"
        elif self.behaviour == "bench_bad":
            bad = benchmark_judgment("Document 2")
            bad["failure_count"] = 0
            text = json.dumps(bad)
        else:
            raise AssertionError(self.behaviour)
        return ModelResponse(
            text=text,
            model=self.model_id,
            usage=self._usage,
            raw={"truncated": truncated, "finish_reason": "length" if truncated else self._finish},
        )


class ScriptedBackend:
    """A faithfulness backend whose verdicts are scripted; records every call."""

    name = "fake"

    def __init__(self, *, verdicts: Sequence[bool] | None = None, fail: bool = False, probability: float = 0.9):
        self.verdicts = list(verdicts) if verdicts is not None else None
        self.fail = fail
        self.probability = probability
        self.calls: list[tuple[str, list[str]]] = []

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name}

    def split_claim(self, claim: str) -> list[str]:
        return [s.strip() + "." for s in claim.split(".") if s.strip()]

    def check(self, document: str, claims: Sequence[str]) -> list[tuple[bool, float]]:
        self.calls.append((document, list(claims)))
        if self.fail:
            raise RuntimeError("scripted backend failure")
        verdicts = self.verdicts if self.verdicts is not None else [True] * len(claims)
        return [(v, self.probability if v else 1 - self.probability) for v in verdicts[: len(claims)]]


def dg_arm_input(*, with_target: bool = True):
    """A Deceptive-Grounding-shaped arm input with invented text (``cx_doc`` then ``cy_doc``)."""
    from binding_gate.contracts import ArmInput, Document

    documents = []
    if with_target:
        documents.append(
            Document.create("cx_doc", "Xanadrug is an invented agent. In zeta syndrome a fictional series of twelve patients improved.")
        )
    documents.append(
        Document.create("cy_doc", "Yoladrug was given in zeta syndrome and a fictional series of twelve patients improved quickly.")
    )
    return ArmInput(
        item_id="t1/complete__prior_completing",
        question="What does the literature say about using xanadrug to treat zeta syndrome?",
        documents=tuple(documents),
    )
