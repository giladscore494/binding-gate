"""Shared "ask the model for a structured answer" step.

Every arm that starts from the model's own answer (``no_gate`` in PR 2; the
gated arm and the checker baselines later) must build its answer request with
:func:`build_answer_request` and send it with :func:`call_model`. Because the
request is then identical across those arms, the runner's response cache hands
them the same candidate answer for the same item, model and repeat
(PREREGISTRATION.md section 5, "same candidate, different gate").

Presentations
-------------
A *presentation* turns a question and its documents into chat messages in the
way a benchmark shows them. It is selected by name in the arm options, so the
prompt of a run is fixed by its config file.

``deceptive_grounding_doc_injection``
    The Deceptive Grounding benchmark's own ``doc_injection`` prompt followed by
    :data:`binding_gate.parsing.ANSWER_FORMAT_INSTRUCTIONS`.

``grab_rag_context``
    GRAB-RAG's system message and context/question layout (PR 7,
    ``binding_gate.datasets.grab_rag.context_messages``) followed by the same
    instruction.

``sealqa_live_web``
    Layer 3 (PR 8, ``binding_gate.datasets.sealqa.live_web_messages``): the web
    pages fetched for the question, each with its URL, then the question and
    the same instruction.

``sec_filings``
    Layer 2 (PR 17, ``binding_gate.datasets.sec_financials.filing_messages``):
    the rendered financial statements of one or two 10-K filings, the question
    and the same instruction.

Free-form answers (PR 22; PREREGISTRATION.md section 18, amendment 1)
---------------------------------------------------------------------
The exploratory arm ``free_form`` (``binding_gate.arms.free_form``) asks for a
normal answer instead of the structure: the benchmark's own ``doc_injection``
messages with NO answer-format instruction (:func:`build_free_form_request`).
Its reply is not parsed. Because the frozen contracts (``contracts.py``) know
only the three statuses of a structured answer, a non-empty free-form reply is
represented as an ``answered`` candidate whose ``value`` is the whole reply
(``<think>`` blocks removed, outer whitespace trimmed), whose ``raw`` is the
unmodified reply, and whose ``quote`` and ``source_id`` are the fixed sentinels
:data:`FREE_FORM_QUOTE` and :data:`FREE_FORM_SOURCE_ID` (:func:`free_form_candidate`).
An empty reply is ``unparseable`` (reason ``empty output``), exactly as
:func:`binding_gate.parsing.parse_answer` treats it. :func:`is_free_form`
recognises such a candidate; the deterministic scorers label it
``not_applicable`` instead of scoring the sentinel as a quote, and the
statistics of the confirmatory hypotheses refuse it. Only the secondary LLM
judge (``binding_gate.scorers.llm_judge``) scores free-form answers.

Retries
-------
:func:`call_model` is the only place where a model call is retried. Each attempt
becomes one :class:`~binding_gate.contracts.ModelCall`: failed attempts carry
``error``, the successful one carries the response. If every attempt fails,
:class:`ModelCallFailed` is raised and carries the logged calls, so the runner
can still write them to the trace (ARCHITECTURE.md section 8, points 1 and 5).
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Sequence

from binding_gate.canonical import utc_now_iso
from binding_gate.contracts import (
    AnswerCandidate,
    ArmInput,
    Document,
    Message,
    ModelCall,
    ModelRequest,
    ModelResponse,
)
from binding_gate.interfaces import ModelClient
from binding_gate.parsing import ANSWER_FORMAT_INSTRUCTIONS, parse_answer

DEFAULT_PRESENTATION = "deceptive_grounding_doc_injection"

FREE_FORM_QUOTE = "[free_form: no verbatim quote was requested]"
"""Sentinel ``quote`` of a free-form answer (PR 22). It is never text of a document."""
FREE_FORM_SOURCE_ID = "[free_form: no source id was requested]"
"""Sentinel ``source_id`` of a free-form answer (PR 22). It is never the id of a shown document."""


def _deceptive_grounding(question: str, documents: Sequence[Document]) -> tuple[Message, ...]:
    from binding_gate.datasets.deceptive_grounding import doc_injection_messages

    return doc_injection_messages(question, documents, ANSWER_FORMAT_INSTRUCTIONS)


def _grab_rag(question: str, documents: Sequence[Document]) -> tuple[Message, ...]:
    from binding_gate.datasets.grab_rag import context_messages

    return context_messages(question, documents, ANSWER_FORMAT_INSTRUCTIONS)


def _sealqa_live_web(question: str, documents: Sequence[Document]) -> tuple[Message, ...]:
    from binding_gate.datasets.sealqa import live_web_messages

    return live_web_messages(question, documents, ANSWER_FORMAT_INSTRUCTIONS)


def _sec_filings(question: str, documents: Sequence[Document]) -> tuple[Message, ...]:
    from binding_gate.datasets.sec_financials import filing_messages

    return filing_messages(question, documents, ANSWER_FORMAT_INSTRUCTIONS)


PRESENTATIONS: dict[str, Callable[[str, Sequence[Document]], tuple[Message, ...]]] = {
    "deceptive_grounding_doc_injection": _deceptive_grounding,
    "grab_rag_context": _grab_rag,
    "sealqa_live_web": _sealqa_live_web,
    "sec_filings": _sec_filings,
}


def build_answer_request(arm_input: ArmInput, presentation: str = DEFAULT_PRESENTATION) -> ModelRequest:
    """The answer request for one item. Sampling parameters come from the model client's config."""
    try:
        build = PRESENTATIONS[presentation]
    except KeyError:
        raise ValueError(
            f"unknown presentation {presentation!r}; available: {sorted(PRESENTATIONS)}"
        ) from None
    return ModelRequest(messages=build(arm_input.question, arm_input.documents))


class ModelCallFailed(RuntimeError):
    """A model call failed after the configured retries. ``model_calls`` holds every attempt."""

    def __init__(self, message: str, model_calls: Sequence[ModelCall]):
        super().__init__(message)
        self.model_calls = tuple(model_calls)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000] or type(exc).__name__


def call_model(
    model: ModelClient,
    request: ModelRequest,
    purpose: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[ModelResponse, tuple[ModelCall, ...]]:
    """Send ``request``, retrying retryable failures under the client's retry policy.

    The policy is the client's ``retry`` attribute (a
    ``binding_gate.models._http.RetryPolicy``); a client without one is called
    once. An exception is retried only if it has a true ``retryable`` attribute.
    """
    policy: Any = getattr(model, "retry", None)
    max_retries = getattr(policy, "max_retries", 0)
    calls: list[ModelCall] = []
    failures = 0
    while True:
        started = utc_now_iso()
        try:
            response = model.complete(request)
        except Exception as exc:  # noqa: BLE001 - every failure is logged, then re-raised below
            failures += 1
            calls.append(
                ModelCall(
                    purpose=purpose,
                    client=model.name,
                    request=request,
                    started_at=started,
                    ended_at=utc_now_iso(),
                    error=_describe(exc),
                )
            )
            if not getattr(exc, "retryable", False) or failures > max_retries:
                raise ModelCallFailed(
                    f"model call failed after {failures} attempt(s): {_describe(exc)}", calls
                ) from exc
            delay = policy.delay(failures, getattr(exc, "retry_after", None))
            sleep(delay * (1.0 + 0.25 * random.random()))  # jitter: spread concurrent retries
            continue
        calls.append(
            ModelCall(
                purpose=purpose,
                client=model.name,
                request=request,
                started_at=started,
                ended_at=utc_now_iso(),
                response=response,
            )
        )
        return response, tuple(calls)


def answer_once(
    model: ModelClient,
    arm_input: ArmInput,
    presentation: str = DEFAULT_PRESENTATION,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[AnswerCandidate, tuple[ModelCall, ...]]:
    """Ask ``model`` once for the structured answer and parse it. No repair, no second request."""
    request = build_answer_request(arm_input, presentation)
    response, calls = call_model(model, request, "answer", sleep=sleep)
    return parse_answer(response.text), calls


# --------------------------------------------------------------------------- #
# Free-form answers (PR 22, exploratory)
# --------------------------------------------------------------------------- #


def _deceptive_grounding_free_form(question: str, documents: Sequence[Document]) -> tuple[Message, ...]:
    from binding_gate.datasets.deceptive_grounding import doc_injection_messages

    return doc_injection_messages(question, documents, "")


FREE_FORM_PRESENTATIONS: dict[str, Callable[[str, Sequence[Document]], tuple[Message, ...]]] = {
    "deceptive_grounding_doc_injection": _deceptive_grounding_free_form,
}
"""Presentations without the structured-answer instruction. Only layer 1: the benchmark's own
``doc_injection`` messages, character for character (its system prompt asks for ``[n]`` citations)."""


def build_free_form_request(arm_input: ArmInput, presentation: str = DEFAULT_PRESENTATION) -> ModelRequest:
    """The benchmark's own request for one item, with no answer-format instruction."""
    try:
        build = FREE_FORM_PRESENTATIONS[presentation]
    except KeyError:
        raise ValueError(
            f"presentation {presentation!r} has no free-form variant; available: {sorted(FREE_FORM_PRESENTATIONS)}"
        ) from None
    return ModelRequest(messages=build(arm_input.question, arm_input.documents))


def free_form_candidate(raw: str) -> AnswerCandidate:
    """A free-form reply as a candidate that fits the frozen contracts (see the module docstring)."""
    from binding_gate.judging import strip_think

    if not isinstance(raw, str):
        raise TypeError("free_form_candidate expects the raw model output as a string")
    text = strip_think(raw).strip()
    if not text:
        return AnswerCandidate.unparseable(raw=raw, reason="empty output")
    return AnswerCandidate.answered(value=text, quote=FREE_FORM_QUOTE, source_id=FREE_FORM_SOURCE_ID, raw=raw)


def is_free_form(candidate: AnswerCandidate | None) -> bool:
    """True for an answered candidate that carries both free-form sentinels."""
    return (
        candidate is not None
        and candidate.status.value == "answered"
        and candidate.quote == FREE_FORM_QUOTE
        and candidate.source_id == FREE_FORM_SOURCE_ID
    )
