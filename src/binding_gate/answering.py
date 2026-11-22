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


def _deceptive_grounding(question: str, documents: Sequence[Document]) -> tuple[Message, ...]:
    from binding_gate.datasets.deceptive_grounding import doc_injection_messages

    return doc_injection_messages(question, documents, ANSWER_FORMAT_INSTRUCTIONS)


def _grab_rag(question: str, documents: Sequence[Document]) -> tuple[Message, ...]:
    from binding_gate.datasets.grab_rag import context_messages

    return context_messages(question, documents, ANSWER_FORMAT_INSTRUCTIONS)


PRESENTATIONS: dict[str, Callable[[str, Sequence[Document]], tuple[Message, ...]]] = {
    "deceptive_grounding_doc_injection": _deceptive_grounding,
    "grab_rag_context": _grab_rag,
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
