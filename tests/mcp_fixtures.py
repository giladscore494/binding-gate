"""Synthetic inputs and test doubles for the MCP server tests (PR 9). Nothing here calls a model or the network.

Two invented drug pages, written so that each gate outcome can be produced on purpose:

* ``lisinopril_page`` is about lisinopril; ``enalapril_page`` is about enalapril.
  Both state a maximum dose for hypertension, so quoting the enalapril page for a
  lisinopril question is the wrong-entity failure the gate exists for.
"""

from __future__ import annotations

import copy
import threading
from typing import Any

from binding_gate.contracts import (
    Decomposition,
    Message,
    ModelCall,
    ModelRequest,
    ModelResponse,
    QuestionConstraints,
    TokenUsage,
)
from binding_gate.registry import register

QUESTION = "What is the maximum daily dose of lisinopril for hypertension in adults?"

LISINOPRIL_PAGE = (
    "Lisinopril is an angiotensin-converting enzyme inhibitor. Lisinopril is used to treat hypertension "
    "and heart failure. In adults with hypertension, the usual starting dose is 10 mg once daily, and the "
    "maximum daily dose of lisinopril is 80 mg. Lisinopril tablets can be taken with or without food."
)
ENALAPRIL_PAGE = (
    "Enalapril is an angiotensin-converting enzyme inhibitor. Enalapril is used to treat hypertension. "
    "In adults with hypertension, the usual starting dose is 5 mg once daily, and the maximum daily dose "
    "is 40 mg, given in one or two doses. Enalapril can cause a dry cough."
)

DOCUMENTS = [
    {"id": "lisinopril_page", "text": LISINOPRIL_PAGE, "url": "https://example.org/lisinopril"},
    {"id": "enalapril_page", "text": ENALAPRIL_PAGE},
]

CONSTRAINTS = {
    "entity": {"name": "lisinopril", "aliases": ["Zestril", "Prinivil"], "kind": "drug"},
    "attribute": "maximum daily dose",
    "qualifiers": [{"kind": "other", "text": "hypertension", "aliases": []}],
    "presuppositions": [{"text": "Lisinopril is used for hypertension.", "key_terms": ["hypertension"]}],
}

TARGET_QUOTE = "the maximum daily dose of lisinopril is 80 mg"
COMPETITOR_QUOTE = "the maximum daily dose is 40 mg, given in one or two doses"


def args(**overrides: Any) -> dict[str, Any]:
    """verify_answer arguments for the correct (ACCEPT) case, with overrides."""
    base = {
        "question": QUESTION,
        "answer": "80 mg",
        "quote": TARGET_QUOTE,
        "source_id": "lisinopril_page",
        "documents": copy.deepcopy(DOCUMENTS),
        "constraints": copy.deepcopy(CONSTRAINTS),
    }
    base.update(overrides)
    return base


def wrong_entity_args(**overrides: Any) -> dict[str, Any]:
    """The answer of the enalapril page given to the lisinopril question."""
    return args(**{"answer": "40 mg", "quote": COMPETITOR_QUOTE, "source_id": "enalapril_page", **overrides})


def constraints_contract(constraints: dict[str, Any] = CONSTRAINTS, question: str = QUESTION) -> QuestionConstraints:
    return QuestionConstraints.from_dict({"question": question, **copy.deepcopy(constraints)})


# --------------------------------------------------------------------------- #
# Decomposer doubles
# --------------------------------------------------------------------------- #

T0 = "2026-11-22T10:00:00+00:00"
T1 = "2026-11-22T10:00:01+00:00"


def decompose_call(question: str) -> ModelCall:
    return ModelCall(
        purpose="decompose",
        client="mcp-fixed-decomposer",
        request=ModelRequest((Message("user", f"Question: {question}"),)),
        started_at=T0,
        ended_at=T1,
        response=ModelResponse("{}", "fixed-model", TokenUsage(40, 30), {}),
    )


class FakeClient:
    """Stands in for a model client; only ``secret_name`` is read by the MCP layer."""

    def __init__(self, secret_name: str = ""):
        self.secret_name = secret_name
        self.name = "fake-client"


FIXED_CALLS: list[str] = []
_LOCK = threading.Lock()


@register("decomposer", "fake_mcp_fixed_decomposer")
class FixedDecomposer:
    """Returns the constraints given as its option, for any question; logs each question in FIXED_CALLS.

    Registered so that the ``gated`` arm and the MCP service can build the very
    same decomposer through the registry.
    """

    name = "fake_mcp_fixed_decomposer"

    def __init__(self, *, constraints: dict[str, Any] = CONSTRAINTS, secret_name: str = "", fail: bool = False):
        self.constraints = copy.deepcopy(constraints)
        self.client = FakeClient(secret_name)
        self.fail = fail

    def describe(self) -> dict[str, Any]:
        return {"name": self.name}

    def decompose(self, question: str) -> Decomposition:
        with _LOCK:
            FIXED_CALLS.append(question)
        if self.fail:
            error = RuntimeError("scripted decomposer failure")
            error.model_calls = (decompose_call(question),)  # type: ignore[attr-defined]
            raise error
        return Decomposition(
            constraints=constraints_contract(self.constraints, question),
            model_calls=(decompose_call(question),),
        )


class FixedAnswerModel:
    """A model client for the ``gated`` arm that always replies with one structured answer (no network)."""

    def __init__(self, reply: str):
        self.name = "fixed-answer-model"
        self.reply = reply

    def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(self.reply, "fixed-answer-model", TokenUsage(10, 10), {})
