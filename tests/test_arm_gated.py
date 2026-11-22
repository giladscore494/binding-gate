"""Arm ``gated``: same candidate as no_gate (zero extra model calls), verdicts in the trace, ablations by config."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import fakes
import gate_fakes as g
import run_helpers as h
from binding_gate import registry, runner
from binding_gate.arms.gated import MEMO, GatedArm, clear_decomposition_memo
from binding_gate.config import load_config
from binding_gate.contracts import AnswerStatus, ArmInput, Decision
from binding_gate.gates.binding import ALL_CHECKS, DEFAULT_PARAMETERS
from binding_gate.trace import read_traces

REPO_ROOT = Path(__file__).resolve().parents[1]
N_DEV_ITEMS = 3 * 15
DECOMPOSER = {"name": "fake_decomposer", "options": {}}


@pytest.fixture(autouse=True)
def _fresh_memo():
    clear_decomposition_memo()
    g.DECOMPOSE_CALLS.clear()
    yield
    clear_decomposition_memo()
    g.DECOMPOSE_CALLS.clear()


def gated(label="gated", checks=ALL_CHECKS, decomposer=DECOMPOSER, **options):
    return {"name": "gated", "label": label, "options": {"checks": list(checks), "decomposer": decomposer, **options}}


def run(tmp_path, arms, **overrides):
    repo, config, _raw = h.setup(tmp_path, arms=arms, **overrides)
    summary = runner.execute_run(
        config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None
    )
    return summary, list(read_traces(summary.run_dir))


def by_key(records, arm):
    return {(r.item_id, r.model, r.repeat): r for r in records if r.arm == arm}


def provenance(record):
    return record.scores[0].label


# -- same candidate, different gate ---------------------------------------------- #


def test_gated_reuses_the_cached_no_gate_candidate_and_records_verdicts(tmp_path):
    summary, records = run(tmp_path, [{"name": "no_gate"}, gated()])
    assert summary.closed and summary.error_records == 0
    assert len(records) == N_DEV_ITEMS * 2 * 2 * 2  # items x arms x models x repeats
    # ZERO extra model calls: the provider saw one request per item, model and repeat.
    assert len(fakes.CALLS) == N_DEV_ITEMS * 2 * 2
    # The decomposer ran once per distinct question (3 dev (X, C) pairs), for the whole run.
    assert sorted(Counter(g.DECOMPOSE_CALLS).values()) == [1, 1, 1]

    baseline, gate_records = by_key(records, "no_gate"), by_key(records, "gated")
    assert baseline.keys() == gate_records.keys()
    decisions: Counter = Counter()
    for key, record in gate_records.items():
        base = baseline[key]
        (attempt,) = record.attempts
        assert attempt.answer == base.final_answer  # the same candidate
        answer_call = record.model_calls[0]
        assert answer_call.purpose == "answer" and answer_call.request == base.model_calls[0].request
        assert runner.is_cache_hit(answer_call) and not runner.is_cache_hit(base.model_calls[0])
        if attempt.answer.status is AnswerStatus.ANSWERED:
            assert attempt.verdict is not None and record.constraints is not None
            assert [r.check for r in attempt.verdict.check_results] == list(ALL_CHECKS)
            assert [c.purpose for c in record.model_calls] == ["answer", "decompose"]
            decisions[(record.model, attempt.verdict.decision)] += 1
            if attempt.verdict.decision is Decision.ACCEPT:
                assert record.final_answer == attempt.answer
            else:
                assert record.final_answer.status is AnswerStatus.ABSTAINED
                assert record.final_answer.raw == ""
                assert record.final_answer.reason.startswith(f"gate {attempt.verdict.decision.value}: ")
        else:
            # never a verdict on a non-answered candidate; it passes through unchanged
            assert attempt.verdict is None and record.constraints is None
            assert record.final_answer == attempt.answer and len(record.model_calls) == 1
    # "good" quotes the target document: accepted. "bad" quotes the competitor: always blocked.
    assert {d for (m, d) in decisions if m == "good"} == {Decision.ACCEPT}
    assert {d for (m, d) in decisions if m == "bad"} <= {Decision.RESEARCH, Decision.ABSTAIN}
    assert decisions[("bad", Decision.RESEARCH)] > 0
    bad_base = [provenance(r) for k, r in baseline.items() if k[1] == "bad"]
    bad_gated = [provenance(r) for k, r in gate_records.items() if k[1] == "bad"]
    assert set(bad_base) == {"competitor_only"} and set(bad_gated) == {"abstained"}
    good_gated = Counter(provenance(r) for k, r in gate_records.items() if k[1] == "good")
    good_base = Counter(provenance(r) for k, r in baseline.items() if k[1] == "good")
    assert good_gated == good_base  # no correct answer lost on these clean stimuli


def test_decomposition_is_shared_and_later_uses_are_marked_as_cache_hits(tmp_path):
    _summary, records = run(tmp_path, [gated()], repeats=2)
    decompose_calls = [c for r in records for c in r.model_calls if c.purpose == "decompose"]
    fresh = [c for c in decompose_calls if not runner.is_cache_hit(c)]
    assert len(fresh) == len(set(g.DECOMPOSE_CALLS)) == 3
    assert len(decompose_calls) > len(fresh)
    # every record of one question carries the same constraints
    by_question: dict = {}
    for record in records:
        if record.constraints is not None:
            by_question.setdefault(record.constraints.question, set()).add(record.constraints.to_json())
    assert all(len(v) == 1 for v in by_question.values()) and len(by_question) == 3


def test_ablation_is_pure_config_and_shares_candidate_and_constraints(tmp_path):
    minus = [c for c in ALL_CHECKS if c != "entity_in_window"]
    _summary, records = run(tmp_path, [gated("gated-full"), gated("gated-minus-entity", minus)], repeats=1)
    assert len(fakes.CALLS) == N_DEV_ITEMS * 2  # still one answer call per item and model
    assert len(g.DECOMPOSE_CALLS) == 3
    full, ablated = by_key(records, "gated-full"), by_key(records, "gated-minus-entity")
    changed = 0
    for key, record in ablated.items():
        assert record.attempts[0].answer == full[key].attempts[0].answer
        assert record.constraints == full[key].constraints
        if record.attempts[0].verdict is not None:
            assert [r.check for r in record.attempts[0].verdict.check_results] == minus
            changed += record.attempts[0].verdict.decision is not full[key].attempts[0].verdict.decision
    assert changed > 0
    # Without entity_in_window some wrong-entity answers get through; with it, none do.
    assert "competitor_only" in {provenance(r) for r in ablated.values()}
    assert "competitor_only" not in {provenance(r) for r in full.values()}


def test_unparseable_and_abstained_candidates_pass_through_without_decomposition(tmp_path):
    models = [
        {"name": "scripted_fake", "label": "garbage", "options": {"model_id": "fake-cheap", "behaviour": "garbage"}},
        {"name": "scripted_fake", "label": "abstain", "options": {"model_id": "fake-cheap", "behaviour": "abstain"}},
    ]
    _summary, records = run(tmp_path, [gated()], models=models, repeats=1)
    assert g.DECOMPOSE_CALLS == []
    for record in records:
        (attempt,) = record.attempts
        assert attempt.verdict is None and record.final_answer == attempt.answer
        expected = "unparseable" if record.model == "garbage" else "abstained"
        assert record.final_answer.status.value == expected and provenance(record) == expected


@pytest.mark.parametrize("mode", ["fail", "garbage"])
def test_a_failing_decomposer_becomes_an_error_record_that_keeps_the_answer_call(tmp_path, mode):
    arm = gated(decomposer={"name": "fake_decomposer", "options": {"mode": mode}})
    summary, records = run(tmp_path, [arm], repeats=1)
    answered = [r for r in records if r.error is not None]
    assert answered and summary.error_records == len(answered)
    for record in answered:
        assert "decomposition failed" in record.error
        assert record.model_calls[0].purpose == "answer"
        purposes = [c.purpose for c in record.model_calls]
        assert purposes == (["answer", "decompose"] if mode == "fail" else ["answer"])


def test_a_dry_run_never_creates_or_calls_the_decomposer(tmp_path):
    arm = gated(decomposer={"name": "not_registered_yet", "options": {}})
    repo, config, _raw = h.setup(tmp_path, arms=[{"name": "no_gate"}, arm])
    report = runner.dry_run(config, repo_root=repo)
    assert fakes.CALLS == [] and g.DECOMPOSE_CALLS == []
    assert report.requests_per_repeat == N_DEV_ITEMS  # the two arms share one request per item


# -- construction ---------------------------------------------------------------- #


@pytest.mark.parametrize(
    "options,message",
    [
        ({"checks": ["quote_verbatim"]}, "decomposer"),
        ({"checks": ["quote_verbatim"], "decomposer": "fake_decomposer"}, "table"),
        ({"checks": ["quote_verbatim"], "decomposer": {"options": {}}}, "decomposer.name"),
        ({"checks": ["quote_verbatim"], "decomposer": {"name": "x", "model": "y"}}, "unknown key"),
        ({"checks": ["quote_verbatim"], "decomposer": DECOMPOSER, "presentation": "plain"}, "presentation"),
        ({"checks": [], "decomposer": DECOMPOSER}, "non-empty"),
        ({"checks": ["quote_verbatim"], "decomposer": DECOMPOSER, "parameters": {"windows": 3}}, "unknown gate parameter"),
    ],
)
def test_arm_options_are_validated(options, message):
    with pytest.raises((ValueError, TypeError), match=message):
        registry.create("arm", "gated", model=fakes.ScriptedFake(), **options)


def test_arm_used_directly_memoises_across_instances():
    arm_input = ArmInput(
        item_id="i1",
        question="What does the literature say about using abatacept to treat lupus nephritis?",
        documents=(g.doc("cx_doc", "Abatacept showed modest benefit in lupus nephritis in a small series of adults."),),
    )
    first = GatedArm(model=fakes.ScriptedFake(label="m"), decomposer=DECOMPOSER, checks=list(ALL_CHECKS))
    second = GatedArm(model=fakes.ScriptedFake(label="m"), decomposer=DECOMPOSER, checks=["quote_verbatim"])
    out1, out2 = first.run(arm_input), second.run(arm_input)
    assert len(g.DECOMPOSE_CALLS) == 1 and len(MEMO) == 1
    assert out1.constraints == out2.constraints
    assert not runner.is_cache_hit(out1.model_calls[-1]) and runner.is_cache_hit(out2.model_calls[-1])
    assert out1.attempts[0].verdict.decision is Decision.ACCEPT


# -- the committed config ------------------------------------------------------------ #


def test_dev_gated_config_reuses_the_baseline_and_lists_all_checks_with_proposed_parameters():
    config = load_config(REPO_ROOT / "configs" / "dev_gated.toml")
    baseline = load_config(REPO_ROOT / "configs" / "dev_baseline.toml")
    assert config.split.value == "dev" and config.repeats == baseline.repeats and config.limit is None
    assert config.models == baseline.models and config.dataset == baseline.dataset
    assert config.scorers == baseline.scorers and config.runner == baseline.runner
    assert [a.name for a in config.arms] == ["no_gate", "gated"]
    options = config.arms[1].options
    assert options["checks"] == list(ALL_CHECKS)
    assert options["parameters"] == DEFAULT_PARAMETERS
    assert options["presentation"] == config.arms[0].options["presentation"]
    assert set(options["decomposer"]) == {"name", "options"}


def test_dev_gated_arms_dry_run_on_the_synthetic_checkout(tmp_path):
    arms = [spec.to_dict() for spec in load_config(REPO_ROOT / "configs" / "dev_gated.toml").arms]
    repo, config, _raw = h.setup(tmp_path, arms=arms)
    report = runner.dry_run(config, repo_root=repo)
    assert report.arms == ("no_gate", "gated") and report.requests_per_repeat == N_DEV_ITEMS
    assert fakes.CALLS == [] and g.DECOMPOSE_CALLS == []
