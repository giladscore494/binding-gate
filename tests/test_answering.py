"""The shared answer step: one request shape for all arms, logged retries, no repair."""

from __future__ import annotations

import pytest

import builders as b
from binding_gate.answering import ModelCallFailed, answer_once, build_answer_request, call_model
from binding_gate.contracts import AnswerStatus, ArmOutput, ModelResponse
from binding_gate.models._http import ModelError, RetryPolicy
from binding_gate.parsing import ANSWER_FORMAT_INSTRUCTIONS
from binding_gate.registry import create


class Flaky:
    """Fails ``failures`` times with the given error, then answers."""

    name = "flaky"

    def __init__(self, failures, error, text='{"status": "abstained"}', retries=3):
        self.failures, self.error, self.text = failures, error, text
        self.retry = RetryPolicy(max_retries=retries, backoff_seconds=1.0, backoff_multiplier=2.0)
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return ModelResponse(self.text, "flaky-1")


def test_request_is_deterministic_and_carries_the_answer_instruction():
    arm_input = b.item().arm_input()
    request = build_answer_request(arm_input)
    assert request == build_answer_request(arm_input)
    assert request.params == {}
    assert [m.role for m in request.messages] == ["system", "user"]
    assert request.messages[1].content.endswith(ANSWER_FORMAT_INSTRUCTIONS)
    assert arm_input.question in request.messages[1].content
    with pytest.raises(ValueError, match="unknown presentation"):
        build_answer_request(arm_input, "no_such_presentation")


def test_retryable_failures_are_retried_with_backoff_and_every_attempt_is_logged():
    sleeps: list[float] = []
    model = Flaky(2, ModelError("HTTP 429: slow down", status=429, retryable=True))
    response, calls = call_model(model, build_answer_request(b.item().arm_input()), "answer", sleep=sleeps.append)
    assert response.text == '{"status": "abstained"}'
    assert [c.error is None for c in calls] == [False, False, True]
    assert all(c.purpose == "answer" and c.client == "flaky" for c in calls)
    assert "429" in calls[0].error
    assert len(sleeps) == 2 and 1.0 <= sleeps[0] <= 1.25 and 2.0 <= sleeps[1] <= 2.5


def test_non_retryable_failure_is_not_retried():
    model = Flaky(5, ModelError("HTTP 400: bad request", status=400))
    with pytest.raises(ModelCallFailed) as caught:
        call_model(model, build_answer_request(b.item().arm_input()), "answer", sleep=lambda s: None)
    assert model.calls == 1 and len(caught.value.model_calls) == 1


def test_retries_are_bounded_and_the_failed_calls_travel_with_the_exception():
    model = Flaky(99, ModelError("HTTP 503", status=503, retryable=True), retries=2)
    with pytest.raises(ModelCallFailed, match="3 attempt") as caught:
        call_model(model, build_answer_request(b.item().arm_input()), "answer", sleep=lambda s: None)
    assert model.calls == 3
    assert [c.error is not None for c in caught.value.model_calls] == [True, True, True]


def test_unexpected_exceptions_are_logged_and_not_retried():
    model = Flaky(1, KeyError("bug"))
    with pytest.raises(ModelCallFailed) as caught:
        call_model(model, build_answer_request(b.item().arm_input()), "answer", sleep=lambda s: None)
    assert model.calls == 1 and "KeyError" in caught.value.model_calls[0].error


def test_answer_once_parses_without_repair():
    candidate, calls = answer_once(Flaky(0, None, text="not the structure"), b.item().arm_input())
    assert candidate.status is AnswerStatus.UNPARSEABLE and candidate.raw == "not the structure"
    assert len(calls) == 1


def test_no_gate_arm_returns_the_parsed_candidate_as_final_answer_whatever_its_status():
    documents = b.documents()
    text = '{"status": "answered", "value": "10 mg", "quote": "the starting dose of Alphadrug for adults was 10 mg", "source_id": "doc-1"}'
    for raw, status in ((text, AnswerStatus.ANSWERED), ('{"status": "abstained"}', AnswerStatus.ABSTAINED), ("??", AnswerStatus.UNPARSEABLE)):
        arm = create("arm", "no_gate", model=Flaky(0, None, text=raw))
        output = arm.run(b.item().arm_input())
        assert isinstance(output, ArmOutput)
        assert output.final_answer.status is status
        assert len(output.attempts) == 1 and output.attempts[0].verdict is None
        assert output.attempts[0].answer == output.final_answer
        assert [e.id for e in output.attempts[0].evidence] == [d.id for d in documents]
        assert output.constraints is None and len(output.model_calls) == 1
    with pytest.raises(ValueError):
        create("arm", "no_gate", model=Flaky(0, None), presentation="nope")
