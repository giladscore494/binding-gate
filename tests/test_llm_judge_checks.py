"""Arm llm_judge_checks: fixed prompt, strict parser, verdict mapping, family rule, failures, budget."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pr5_fakes as f
import run_helpers as h
from binding_gate import judging, registry
from binding_gate.answering import ModelCallFailed
from binding_gate.arms import llm_judge_checks as ljc
from binding_gate.contracts import AnswerCandidate, AnswerStatus, CheckStatus, Decision

ALL = list(ljc.CHECK_NAMES)


@pytest.fixture(autouse=True)
def _fresh():
    judging.reset_shared_state()
    f.JUDGE_CALLS.clear()
    yield
    judging.reset_shared_state()
    f.JUDGE_CALLS.clear()


@pytest.fixture
def prices(tmp_path) -> str:
    path = tmp_path / "prices.toml"
    path.write_text(h.PRICES + f.FAKE_JUDGE_PRICE, encoding="utf-8")
    return path.as_posix()


def judge(behaviour="pass", label="judge-a", provider="moonshot", **options):
    return {"name": "pr5_fake_judge", "label": label, "options": {"behaviour": behaviour, "provider": provider, **options}}


def make_arm(prices, *, behaviour="target", provider="openai", judges=None, cap=50.0, **options):
    model = f.FamilyFake(label="m", behaviour=behaviour, provider=provider)
    return registry.create(
        "arm", "llm_judge_checks", model=model, judges=judges or [judge()], judge_max_usd=cap, prices=prices, **options
    )


def candidate():
    return AnswerCandidate.answered(
        value="Twelve patients improved.", quote="a fictional series of twelve patients improved", source_id="cx_doc", raw="{}"
    )


# -- prompt ------------------------------------------------------------------------ #


def test_request_holds_question_documents_candidate_and_only_the_selected_checks():
    arm_input = f.dg_arm_input()
    request = ljc.build_check_request(arm_input.question, arm_input.documents, candidate(), ["entity_in_window", "quote_verbatim"])
    system, user = request.messages
    assert system.role == "system" and system.content == ljc.JUDGE_SYSTEM_PROMPT
    assert arm_input.question in user.content
    assert "[Document 1 — source: cx_doc]\n" + arm_input.documents[0].text in user.content
    assert "- quote: a fictional series of twelve patients improved" in user.content and "- source_id: cx_doc" in user.content
    assert f"- quote_verbatim: {ljc.CHECK_DEFINITIONS['quote_verbatim']}" in user.content
    assert "premise_present" not in user.content
    # canonical order, whatever the configured order
    assert user.content.index("- quote_verbatim:") < user.content.index("- entity_in_window:")
    assert ljc.build_check_request(arm_input.question, arm_input.documents, candidate(), ALL) == ljc.build_check_request(
        arm_input.question, arm_input.documents, candidate(), list(reversed(ALL))
    )


def test_only_answered_candidates_can_be_judged():
    with pytest.raises(ValueError):
        ljc.build_check_request("q", f.dg_arm_input().documents, AnswerCandidate.abstained(raw="{}"), ALL)


@pytest.mark.parametrize("checks", [[], ["quote_verbatim", "quote_verbatim"], ["nope"], "quote_verbatim"])
def test_check_names_are_validated(checks):
    with pytest.raises(ValueError):
        ljc.canonical_checks(checks)


# -- parser ------------------------------------------------------------------------ #


def reply(**overrides):
    results = {name: "pass" for name in ALL}
    results.update(overrides)
    return json.dumps({"checks": results, "explanation": "fine"})


def test_parser_accepts_the_required_object_with_surrounding_text_and_think_blocks():
    raw = "<think>{\"checks\": {\"draft\": 1}}</think>Here you go:\n```json\n" + reply(no_closer_competitor="FAIL ") + "\n```"
    parsed = ljc.parse_check_reply(raw, ALL)
    assert parsed.ok and parsed.results["no_closer_competitor"] == "fail" and parsed.explanation == "fine"


@pytest.mark.parametrize(
    "raw,error",
    [
        ("", "empty"),
        ("all checks pass", "no JSON object"),
        (reply() + reply(quote_verbatim="fail"), "several different"),
        (json.dumps({"checks": ["pass"]}), "not an object"),
        (json.dumps({"checks": {n: "pass" for n in ALL[:-1]}}), "missing ['premise_present']"),
        (json.dumps({"checks": {**{n: "pass" for n in ALL}, "extra": "pass"}}), "extra ['extra']"),
        (reply(entity_in_window="not_applicable"), "entity_in_window"),
        (reply(quote_verbatim=True), "quote_verbatim"),
        (reply(premise_present="maybe"), "premise_present"),
    ],
)
def test_parser_rejects_anything_else_without_repair(raw, error):
    parsed = ljc.parse_check_reply(raw, ALL)
    assert not parsed.ok and error in parsed.error


def test_identical_repeated_objects_are_one_answer():
    assert ljc.parse_check_reply(reply() + "\n" + reply(), ALL).ok


def test_not_applicable_is_allowed_where_defined():
    parsed = ljc.parse_check_reply(reply(qualifiers_in_window="not_applicable", premise_present="not_applicable"), ALL)
    assert parsed.ok


# -- verdict ----------------------------------------------------------------------- #


def test_verdict_accepts_without_failures_and_abstains_with_one_reason_per_failure():
    ok = ljc.verdict_from_checks(ljc.parse_check_reply(reply(qualifiers_in_window="not_applicable"), ALL), "j", "m")
    assert ok.decision is Decision.ACCEPT
    assert {r.check: r.status for r in ok.check_results}["qualifiers_in_window"] is CheckStatus.NOT_APPLICABLE
    bad = ljc.verdict_from_checks(ljc.parse_check_reply(reply(entity_in_window="fail", no_closer_competitor="fail"), ALL), "j", "m")
    assert bad.decision is Decision.ABSTAIN
    assert bad.reasons == ("entity_in_window failed (judged by j)", "no_closer_competitor failed (judged by j)")
    assert all(r.details["performed_by"] == "llm_judge" for r in bad.check_results)


# -- the arm ----------------------------------------------------------------------- #


def test_accepted_candidate_is_the_final_answer_and_both_calls_are_logged(prices):
    output = make_arm(prices).run(f.dg_arm_input())
    answer_call, judge_call = output.model_calls
    assert (answer_call.purpose, judge_call.purpose) == ("answer", "check")
    assert judge_call.client == "judge-a"
    assert output.attempts[0].verdict.decision is Decision.ACCEPT and output.final_answer == output.attempts[0].answer


def test_failed_check_blocks_the_answer(prices):
    output = make_arm(prices, judges=[judge("fail:entity_in_window")]).run(f.dg_arm_input())
    assert output.attempts[0].verdict.decision is Decision.ABSTAIN
    assert output.final_answer.status is AnswerStatus.ABSTAINED and "entity_in_window" in output.final_answer.reason


def test_ablated_check_list_reaches_the_judge(prices):
    output = make_arm(prices, checks=["quote_verbatim", "entity_in_window"]).run(f.dg_arm_input())
    assert [r.check for r in output.attempts[0].verdict.check_results] == ["quote_verbatim", "entity_in_window"]


@pytest.mark.parametrize("behaviour", ["abstain", "garbage"])
def test_non_answers_pass_through_without_a_judge_call(prices, behaviour):
    output = make_arm(prices, behaviour=behaviour).run(f.dg_arm_input())
    assert output.attempts[0].verdict is None and len(output.model_calls) == 1 and f.JUDGE_CALLS == []


@pytest.mark.parametrize("behaviour,match", [("garbage", "no JSON object"), ("truncated", "truncated")])
def test_unparseable_or_truncated_judge_output_is_an_error_with_every_call(prices, behaviour, match):
    with pytest.raises(judging.JudgeOutputUnparseable, match=match) as caught:
        make_arm(prices, judges=[judge(behaviour)]).run(f.dg_arm_input())
    assert [c.purpose for c in caught.value.model_calls] == ["answer", "check"]


def test_failed_judge_call_is_an_error_with_every_call(prices):
    with pytest.raises(ModelCallFailed) as caught:
        make_arm(prices, judges=[judge("crash")]).run(f.dg_arm_input())
    assert [c.purpose for c in caught.value.model_calls] == ["answer", "check"]
    assert caught.value.model_calls[1].error is not None


def test_failed_preflight_is_an_error_and_no_judge_call_is_made(prices):
    with pytest.raises(judging.JudgeUnavailable) as caught:
        make_arm(prices, judges=[judge(preflight_ok=False)]).run(f.dg_arm_input())
    assert [c.purpose for c in caught.value.model_calls] == ["answer"] and f.JUDGE_CALLS == []


# -- judge-family rule ------------------------------------------------------------- #


def test_a_judge_of_the_same_family_is_refused_before_any_call(prices):
    with pytest.raises(judging.JudgeFamilyError, match="family"):
        make_arm(prices, provider="moonshot")


def test_the_first_judge_of_another_family_is_used(prices):
    judges = [judge(label="same-family", provider="openai"), judge(label="other-family", provider="anthropic")]
    output = make_arm(prices, provider="openai", judges=judges).run(f.dg_arm_input())
    assert output.model_calls[1].client == "other-family"


def test_unknown_family_of_the_model_under_test_refuses_at_the_first_judge_call(prices):
    arm = make_arm(prices, provider="mystery-provider")  # construction is allowed (the dry run has no family)
    with pytest.raises(judging.JudgeFamilyError, match="cannot be determined") as caught:
        arm.run(f.dg_arm_input())
    assert [c.purpose for c in caught.value.model_calls] == ["answer"] and f.JUDGE_CALLS == []


def test_a_judge_of_unknown_family_never_qualifies(prices):
    with pytest.raises(judging.JudgeFamilyError):
        make_arm(prices, judges=[judge(provider="mystery-provider")])


# -- budget ------------------------------------------------------------------------ #


def test_judge_budget_is_shared_by_arm_instances_and_stops_further_calls(prices):
    per_call = (2000 * 2.0 + 300 * 10.0) / 1_000_000
    first = make_arm(prices, cap=per_call * 1.5)
    second = make_arm(prices, cap=per_call * 1.5)
    first.run(f.dg_arm_input())
    second.run(f.dg_arm_input())  # spent 1 call < cap: allowed, now 2 calls >= cap
    with pytest.raises(judging.JudgeBudgetExceeded) as caught:
        first.run(f.dg_arm_input())
    assert [c.purpose for c in caught.value.model_calls] == ["answer"]
    assert len(f.JUDGE_CALLS) == 2
    budget = first._budget(first._choice[1])
    assert budget is second._budget(second._choice[1]) and budget.spent_usd == pytest.approx(2 * per_call)


def test_judge_budget_is_per_model_under_test(prices):
    """PR 12: judge_max_usd applies per model label, like the runner's caps."""
    per_call = (2000 * 2.0 + 300 * 10.0) / 1_000_000
    first = make_arm(prices, cap=per_call * 0.5)
    other_model = registry.create(
        "arm", "llm_judge_checks", model=f.FamilyFake(label="m2", behaviour="target", provider="openai"),
        judges=[judge()], judge_max_usd=per_call * 0.5, prices=prices,
    )
    first.run(f.dg_arm_input())  # model "m" has now reached its cap
    with pytest.raises(judging.JudgeBudgetExceeded):
        first.run(f.dg_arm_input())
    other_model.run(f.dg_arm_input())  # model "m2" has its own budget
    assert len(f.JUDGE_CALLS) == 2
    assert first._budget(first._choice[1]) is not other_model._budget(other_model._choice[1])


def test_required_options(prices):
    model = f.FamilyFake(label="m")
    with pytest.raises(ValueError, match="judges"):
        registry.create("arm", "llm_judge_checks", model=model, judge_max_usd=1.0)
    with pytest.raises(ValueError, match="judge_max_usd"):
        registry.create("arm", "llm_judge_checks", model=model, judges=[judge()])
    with pytest.raises(ValueError, match="unique"):
        registry.create("arm", "llm_judge_checks", model=model, judges=[judge(), judge()], judge_max_usd=1.0)


def test_missing_judge_price_is_an_error_that_keeps_the_answer_call(tmp_path):
    path = tmp_path / "prices.toml"
    path.write_text(h.PRICES, encoding="utf-8")  # no price for fake-judge
    with pytest.raises(judging.JudgeUnavailable, match="fake-judge") as caught:
        make_arm(path.as_posix()).run(f.dg_arm_input())
    assert [c.purpose for c in caught.value.model_calls] == ["answer"]


def test_estimate_gives_an_upper_bound_from_real_prompts(tmp_path):
    repo, _config, raw = h.setup(tmp_path)
    (repo / "configs" / "prices.toml").write_text(h.PRICES + f.FAKE_JUDGE_PRICE, encoding="utf-8")
    raw["arms"] = [{"name": "llm_judge_checks", "options": {"judge_max_usd": 9.0, "judges": [judge()]}}]
    raw["arms"][0]["options"]["judges"][0]["options"] = {"behaviour": "pass"}
    config_path = repo / "configs" / "judge.toml"
    config_path.write_text(_toml_with_judges(raw), encoding="utf-8")
    (row,) = ljc.estimate_judge_cost(config_path, repo)
    assert row["max_judge_calls"] == 45 * 2 * 2  # items x repeats x models, every candidate assumed answered
    assert 0 < row["cost_low"] < row["cost_high"] and row["judge_max_usd"] == 9.0
    assert row["max_judge_calls_per_model"] == 45 * 2 and row["models"] == 2  # PR 12: the cap is per model
    assert row["cost_high_per_model"] == pytest.approx(row["cost_high"] / 2)
    assert ljc.main(["estimate", "--config", str(config_path), "--repo-root", str(repo)]) == 0


def _toml_with_judges(raw: dict) -> str:
    """run_helpers.toml_text plus the nested [[arms.options.judges]] table it cannot write."""
    judges = raw["arms"][0]["options"].pop("judges")
    text = h.toml_text(raw)
    for spec in judges:
        text += f'[[arms.options.judges]]\nname = "{spec["name"]}"\nlabel = "{spec["label"]}"\n[arms.options.judges.options]\n'
        text += "".join(f'{k} = "{v}"\n' for k, v in spec["options"].items())
    return text


def test_no_judge_text_mentions_the_benchmark_roles():
    text = (ljc.JUDGE_SYSTEM_PROMPT + " ".join(ljc.CHECK_DEFINITIONS.values())).lower()
    for leak in ("cx_doc", "cy_doc", "competitor document", "target document"):
        assert leak not in text


def test_module_has_no_dependency_on_pr4_code():
    source = Path(ljc.__file__).read_text(encoding="utf-8")
    assert "binding_gate.checks" not in source and "binding_gate.gates" not in source
