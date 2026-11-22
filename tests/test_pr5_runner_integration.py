"""The PR 5 arms inside the real PR 2 runner: same candidate, zero extra calls, complete traces."""

from __future__ import annotations

import pytest

import fakes
import pr5_fakes as f
import run_helpers as h
from binding_gate import judging, runner
from binding_gate.arms import faithfulness_checker as fc
from binding_gate.contracts import AnswerStatus, Decision
from binding_gate.trace import read_traces

N_DEV_ITEMS = 3 * 15
REPEATS = 2
TARGET_ANSWERED = 3 * 10  # the "target" fake abstains in the 5 absent conditions of each triple


@pytest.fixture
def setup(tmp_path, monkeypatch):
    judging.reset_shared_state()
    fc.reset_backends()
    f.JUDGE_CALLS.clear()
    monkeypatch.setitem(fc.BACKENDS, "fake", lambda **o: f.ScriptedBackend(**o))
    prices = tmp_path / "judge_prices.toml"
    prices.write_text(h.PRICES + f.FAKE_JUDGE_PRICE, encoding="utf-8")
    arms = [
        {"name": "no_gate", "label": "no_gate"},
        {"name": "faithfulness_checker", "label": "faithfulness_checker", "options": {"backend": "fake"}},
        {
            "name": "llm_judge_checks",
            "label": "llm_judge_checks",
            "options": {
                "judge_max_usd": 50.0,
                "prices": prices.as_posix(),
                "judges": [{"name": "pr5_fake_judge", "label": "judge-a", "options": {"behaviour": "fail:entity_in_window"}}],
            },
        },
        {"name": "prompt_entity_check", "label": "prompt_entity_check"},
    ]
    models = [{"name": "pr5_family_fake", "label": "good", "options": {"model_id": "fake-cheap", "behaviour": "target", "provider": "openai"}}]
    repo, config, _raw = h.setup(tmp_path, arms=arms, models=models, repeats=REPEATS)
    # PR 8: the runner prices auxiliary calls (here the judge) by the id in the response and
    # refuses to start without a price for them, so the run's price file lists the judge too.
    (repo / "configs" / "prices.toml").write_text(h.PRICES + f.FAKE_JUDGE_PRICE, encoding="utf-8")
    yield repo, config, tmp_path
    judging.reset_shared_state()
    fc.reset_backends()


def test_checker_arms_reuse_the_no_gate_candidate_with_zero_extra_calls(setup):
    repo, config, tmp_path = setup
    summary = runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None)
    records = list(read_traces(summary.run_dir))
    assert summary.closed and summary.error_records == 0 and len(records) == N_DEV_ITEMS * REPEATS * 4

    # The model under test is asked twice per item and repeat: once for the shared no_gate
    # request (no_gate, faithfulness_checker, llm_judge_checks) and once for prompt_entity_check.
    assert len(fakes.CALLS) == N_DEV_ITEMS * REPEATS * 2
    # The judge is asked once per answered candidate.
    assert len(f.JUDGE_CALLS) == TARGET_ANSWERED * REPEATS

    by_arm = {}
    for record in records:
        by_arm.setdefault(record.arm, {})[(record.item_id, record.repeat)] = record
    for key, reference in by_arm["no_gate"].items():
        assert not runner.is_cache_hit(reference.model_calls[0])
        for arm in ("faithfulness_checker", "llm_judge_checks"):
            record = by_arm[arm][key]
            assert record.attempts[0].answer == reference.final_answer  # same candidate
            assert runner.is_cache_hit(record.model_calls[0])  # no extra call to the model under test
        assert not runner.is_cache_hit(by_arm["prompt_entity_check"][key].model_calls[0])

    for key, record in by_arm["llm_judge_checks"].items():
        if record.attempts[0].answer.status is AnswerStatus.ANSWERED:
            assert record.attempts[0].verdict.decision is Decision.ABSTAIN  # the judge fails entity_in_window
            assert [c.purpose for c in record.model_calls] == ["answer", "check"]
            assert record.final_answer.status is AnswerStatus.ABSTAINED
    # PR 8: judge calls are priced by the model id in the response ("fake-judge") and charged
    # to the budget of the record's model (llm_judge_checks keeps its own cap as well).
    assert summary.unpriced_clients == []
    per_call = (1000 * 1.0 + 500 * 2.0) / 1_000_000
    per_judge_call = (2000 * 2.0 + 300 * 10.0) / 1_000_000
    assert summary.spend_usd["good"] == pytest.approx(
        N_DEV_ITEMS * REPEATS * 2 * per_call + TARGET_ANSWERED * REPEATS * per_judge_call
    )


def test_dry_run_builds_every_request_without_loading_the_checker_or_calling_the_judge(setup):
    repo, config, _tmp = setup
    report = runner.dry_run(config, repo_root=repo)
    assert report.requests_per_repeat == N_DEV_ITEMS * 2  # shared request counted once, prompt_entity_check once
    assert f.JUDGE_CALLS == []


def test_judge_failures_become_error_records_with_all_calls(setup, monkeypatch):
    repo, config, tmp_path = setup
    from binding_gate.config import parse_config

    raw = config.to_dict()
    raw["arms"][2]["options"]["judges"][0]["options"] = {"behaviour": "garbage"}
    config = parse_config(raw)
    summary = runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "runs2", confirm_spend=True, log=lambda m: None)
    errors = [r for r in read_traces(summary.run_dir) if r.error is not None]
    assert len(errors) == summary.error_records == TARGET_ANSWERED * REPEATS
    for record in errors:
        assert record.arm == "llm_judge_checks" and "unparseable" in record.error
        assert [c.purpose for c in record.model_calls] == ["answer", "check"]
