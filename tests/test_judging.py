"""Shared judge plumbing: families, judge choice, budget arithmetic, text helpers."""

from __future__ import annotations

import pytest

import builders as b
import pr5_fakes as f
from binding_gate import judging
from binding_gate.contracts import ModelCall, ModelResponse, TokenUsage
from binding_gate.models.anthropic_messages import AnthropicMessagesClient
from binding_gate.models.moonshot_chat import MoonshotChatClient
from binding_gate.runner import CachedClient, ResponseCache


def test_family_comes_from_the_provider_even_behind_the_runner_cache():
    moonshot = MoonshotChatClient(model_id="kimi-k3.1-pro", max_output_tokens=10, label="j")
    anthropic = AnthropicMessagesClient(model_id="claude-haiku-5-5", max_output_tokens=10, label="m")
    assert judging.model_family(moonshot) == "moonshot"
    assert judging.model_family(CachedClient(anthropic, 0, ResponseCache())) == "anthropic"
    assert judging.model_family(object()) is None
    assert judging.model_family(f.FamilyFake(provider="somebody-else")) is None


def test_choose_judge_takes_the_first_of_another_family():
    a = judging.JudgeSpec("x", "a")
    c = judging.JudgeSpec("x", "c")
    judges = [(a, f.FakeJudge(provider="openai")), (c, f.FakeJudge(provider="google"))]
    assert judging.choose_judge(judges, "openai")[0] is c
    assert judging.choose_judge(judges, "anthropic")[0] is a
    with pytest.raises(judging.JudgeFamilyError, match="no configured judge"):
        judging.choose_judge(judges[:1], "openai")
    with pytest.raises(judging.JudgeFamilyError, match="cannot be determined"):
        judging.choose_judge(judges, None)


@pytest.mark.parametrize(
    "raw",
    [None, [], "x", [{"name": "m"}], [{"name": "m", "label": "l", "extra": 1}], [{"name": "m", "label": "l", "options": []}]],
)
def test_judge_lists_are_validated(raw):
    with pytest.raises(ValueError):
        judging.parse_judges("judges", raw)


def test_budget_charges_reported_usage_and_estimates_missing_usage_expensively():
    budget = judging.JudgeBudget(max_usd=1.0, input_per_mtok=2.0, output_per_mtok=10.0, max_output_tokens=1000)
    call = b.model_call()  # usage 120 in / 30 out
    assert budget.call_cost(call) == pytest.approx((120 * 2 + 30 * 10) / 1e6)
    no_usage = ModelCall(
        purpose="judge", client="j", request=call.request, started_at=b.T0, ended_at=b.T1,
        response=ModelResponse("x", "m", None, {}),
    )
    chars = sum(len(m.content) for m in call.request.messages)
    assert budget.call_cost(no_usage) == pytest.approx((-(-chars // 4) * 2 + 1000 * 10) / 1e6)
    failed = ModelCall(purpose="judge", client="j", request=call.request, started_at=b.T0, ended_at=b.T1, error="boom")
    assert budget.call_cost(failed) == 0.0
    budget.charge([call])
    budget.ensure_available()
    budget.charge([ModelCall(purpose="judge", client="j", request=call.request, started_at=b.T0, ended_at=b.T1,
                             response=ModelResponse("x", "m", TokenUsage(0, 100_000), {}))])
    with pytest.raises(judging.JudgeBudgetExceeded):
        budget.ensure_available()


def test_text_helpers():
    assert judging.strip_think("<think>a {\"k\": 1}</think> answer ") == "answer"
    assert judging.strip_think("<THINK>never closed") == ""
    assert judging.json_objects_with_key('x {"k": 1} y {"j": 2} {"k": 3', "k") == [{"k": 1}]
