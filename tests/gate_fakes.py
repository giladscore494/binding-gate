"""Test doubles for the gate tests: a fake decomposer (registered as ``fake_decomposer``) and builders.

PR 3 builds the real decomposers in parallel; the gated arm reaches any
decomposer only through the registry and the ``Decomposer`` protocol, so these
tests register their own. Nothing here calls a model or the network.
"""

from __future__ import annotations

import re
import threading

from binding_gate.answering import ModelCallFailed
from binding_gate.contracts import (
    AnswerCandidate,
    Decomposition,
    Document,
    Entity,
    Message,
    ModelCall,
    ModelRequest,
    ModelResponse,
    Presupposition,
    Qualifier,
    QualifierKind,
    QuestionConstraints,
    TokenUsage,
)
from binding_gate.registry import register

T0 = "2026-11-22T10:00:00+00:00"
T1 = "2026-11-22T10:00:01+00:00"

DECOMPOSE_CALLS: list[str] = []
"""Every question any fake decomposer was asked to decompose. Tests clear it."""
_LOCK = threading.Lock()
_DG_QUESTION = re.compile(r"^What does the literature say about using (.+) to treat (.+)\?$")


def decompose_call(question: str, text: str = "{}") -> ModelCall:
    return ModelCall(
        purpose="decompose",
        client="fake-decomposer",
        request=ModelRequest((Message("user", f"Decompose: {question}"),)),
        started_at=T0,
        ended_at=T1,
        response=ModelResponse(text, "fake-decomposer-model", TokenUsage(50, 20), {}),
    )


@register("decomposer", "fake_decomposer")
class FakeDecomposer:
    """Parses the Deceptive Grounding question template deterministically.

    entity = X, one qualifier (kind ``other``) = C, one presupposition whose key
    terms are chosen by ``premise_terms``: ``"C"`` (default), ``"X"``, ``"XC"``
    or ``"none"``. ``mode = "fail"`` raises like a decomposer whose model call
    failed; ``mode = "garbage"`` returns something that is not a Decomposition.
    """

    name = "fake_decomposer"

    def __init__(self, *, premise_terms: str = "C", mode: str = "ok"):
        self.premise_terms = premise_terms
        self.mode = mode

    def decompose(self, question: str) -> Decomposition:
        with _LOCK:
            DECOMPOSE_CALLS.append(question)
        call = decompose_call(question)
        if self.mode == "fail":
            raise ModelCallFailed("scripted decomposer failure", [call])
        if self.mode == "garbage":
            return {"not": "a decomposition"}  # type: ignore[return-value]
        match = _DG_QUESTION.match(question)
        if match is None:
            raise ValueError(f"fake decomposer cannot parse {question!r}")
        x, c = match.groups()
        terms = {"C": (c,), "X": (x,), "XC": (x, c), "none": ()}[self.premise_terms]
        return Decomposition(
            constraints=QuestionConstraints(
                question=question,
                entity=Entity(name=x, kind="drug"),
                attribute="what the literature says about its use",
                qualifiers=(Qualifier(QualifierKind.OTHER, c),),
                presuppositions=(Presupposition(f"{x} has been considered for {c}", terms),),
            ),
            model_calls=(call,),
        )


# --------------------------------------------------------------------------- #
# Builders for check unit tests
# --------------------------------------------------------------------------- #


def constraints(
    entity: str = "abatacept",
    aliases: tuple[str, ...] = (),
    qualifiers: tuple[tuple[str, tuple[str, ...]], ...] = (("lupus nephritis", ()),),
    presuppositions: tuple[tuple[str, tuple[str, ...]], ...] = (),
    question: str | None = None,
) -> QuestionConstraints:
    if question is None:
        q_text = " and ".join(q for q, _a in qualifiers) or "this condition"
        question = f"What does the literature say about using {entity} to treat {q_text}?"
    return QuestionConstraints(
        question=question,
        entity=Entity(name=entity, aliases=aliases, kind="drug"),
        attribute="evidence for use",
        qualifiers=tuple(Qualifier(QualifierKind.OTHER, text, aliases=a) for text, a in qualifiers),
        presuppositions=tuple(Presupposition(text, terms) for text, terms in presuppositions),
    )


def doc(doc_id: str, text: str) -> Document:
    return Document.create(doc_id, text)


def answer(quote: str, source_id: str = "d1", value: str = "some value") -> AnswerCandidate:
    return AnswerCandidate.answered(value=value, quote=quote, source_id=source_id, raw="{}")


def filler(n: int, word: str = "word") -> str:
    """``n`` neutral tokens (not stop words, not names), to control distances exactly."""
    return " ".join(f"{word}{i}" for i in range(n))
