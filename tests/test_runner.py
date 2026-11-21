"""The runner: complete traces, cache, resume, dry run, budget cap, test-split discipline."""

from __future__ import annotations

from pathlib import Path

import pytest

import fakes
import run_helpers as h
from binding_gate import registry, runner
from binding_gate.config import parse_config
from binding_gate.contracts import AnswerStatus, Message, ModelRequest, ModelResponse, Split
from binding_gate.registry import register
from binding_gate.split import item_split
from binding_gate.testlock import ACCESS_LOG_RELPATH, EVENT_LOADED, EVENT_REFUSED, read_access_log
from binding_gate.trace import TraceError, read_manifest, read_traces

REAL_REPO = Path(__file__).resolve().parents[1]
N_DEV_ITEMS = 3 * 15


def run(repo, config, tmp_path, **kwargs):
    kwargs.setdefault("confirm_spend", True)
    kwargs.setdefault("log", lambda message: None)
    return runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "runs", **kwargs)


def label_of(record):
    return record.scores[0].label


# -- the complete loop ----------------------------------------------------------- #


def test_one_record_per_item_arm_model_repeat_on_dev_only(tmp_path):
    repo, config, _raw = h.setup(tmp_path)
    summary = run(repo, config, tmp_path)
    records = list(read_traces(summary.run_dir))
    assert summary.closed and summary.records_written == len(records) == N_DEV_ITEMS * 2 * 2
    keys = {(r.item_id, r.arm, r.model, r.repeat) for r in records}
    assert len(keys) == len(records)
    assert {r.model for r in records} == {"good", "bad"} and {r.repeat for r in records} == {0, 1}
    assert all(r.split is Split.DEV and r.arm == "no_gate" and r.error is None for r in records)
    assert all(item_split_of(r) is Split.DEV for r in records)
    manifest = read_manifest(summary.run_dir)
    assert manifest.ended_at is not None and manifest.trace_count == len(records)
    assert manifest.config_hash == config.config_hash and manifest.test_unlock_tag is None
    assert len({r.group_id for r in records}) == 3


def item_split_of(record):
    from binding_gate.split import assign_split

    return assign_split(record.dataset, record.group_id)


def test_records_hold_prompt_raw_response_usage_and_scores(tmp_path):
    repo, config, _raw = h.setup(tmp_path)
    records = list(read_traces(run(repo, config, tmp_path).run_dir))
    by_model = {"good": [], "bad": []}
    for record in records:
        by_model[record.model].append(record)
        (call,) = record.model_calls
        assert call.purpose == "answer" and call.client == record.model
        assert call.request.messages[0].role == "system"
        assert call.response.model == "fake-cheap-2026-01-01"
        assert call.response.usage.input_tokens == 1000
        assert record.final_answer.raw == call.response.text
        assert [s.scorer for s in record.scores] == ["provenance"]
    # "good" quotes the X document, and abstains when it is absent.
    for record in by_model["good"]:
        expected = "abstained" if record.stratum.startswith("absent/") else "target_only"
        assert label_of(record) == expected
    # "bad" always quotes the competing document: the wrong-entity answer.
    assert {label_of(r) for r in by_model["bad"]} == {"competitor_only"}


def test_arms_never_see_gold_group_or_stratum(tmp_path):
    seen = []

    @register("arm", "spy_arm")
    class SpyArm:
        name = "spy_arm"

        def __init__(self, *, model):
            self.inner = registry.create("arm", "no_gate", model=model)

        def run(self, arm_input):
            seen.append(arm_input)
            return self.inner.run(arm_input)

    try:
        repo, config, _raw = h.setup(tmp_path, arms=[{"name": "spy_arm"}], repeats=1)
        run(repo, config, tmp_path, selection="good")
    finally:
        registry._factories["arm"].pop("spy_arm")
    # (the cost estimate inside execute_run also drives the arms once, with a recording client)
    assert len({arm_input.item_id for arm_input in seen}) == N_DEV_ITEMS
    for arm_input in seen:
        assert set(arm_input.to_dict()) == {"item_id", "question", "documents"}


# -- cache: same candidate, different gate ---------------------------------------- #


def test_identical_requests_are_answered_once_per_model_and_repeat(tmp_path):
    repo, config, _raw = h.setup(
        tmp_path, arms=[{"name": "no_gate", "label": "no_gate"}, {"name": "no_gate", "label": "same_prompt_again"}]
    )
    summary = run(repo, config, tmp_path, selection="good")
    records = list(read_traces(summary.run_dir))
    assert len(records) == N_DEV_ITEMS * 2 * 2
    # Two arms share the prompt: the provider is asked once per item and repeat.
    assert len(fakes.CALLS) == N_DEV_ITEMS * 2
    first = {(r.item_id, r.repeat): r for r in records if r.arm == "no_gate"}
    second = {(r.item_id, r.repeat): r for r in records if r.arm == "same_prompt_again"}
    for key, record in first.items():
        other = second[key]
        assert other.final_answer == record.final_answer
        assert not runner.is_cache_hit(record.model_calls[0])
        assert runner.is_cache_hit(other.model_calls[0])
    # A cached response costs nothing: spend counts each provider call once.
    per_call = (1000 * 1.0 + 500 * 2.0) / 1_000_000
    assert summary.spend_usd["good"] == pytest.approx(N_DEV_ITEMS * 2 * per_call)


def test_cache_key_separates_clients_repeats_and_requests():
    request = ModelRequest((Message("user", "a"),))
    other = ModelRequest((Message("user", "b"),))
    key = runner.cache_key("m", 0, request)
    assert key == runner.cache_key("m", 0, ModelRequest((Message("user", "a"),)))
    assert len({key, runner.cache_key("m", 1, request), runner.cache_key("n", 0, request), runner.cache_key("m", 0, other)}) == 4


# -- errors are records, not gaps -------------------------------------------------- #


def test_failed_model_calls_become_error_records_with_the_logged_attempts(tmp_path):
    crash = {"name": "scripted_fake", "label": "crash", "options": {"model_id": "fake-cheap", "behaviour": "crash"}}
    repo, config, _raw = h.setup(tmp_path, models=[crash], repeats=1)
    summary = run(repo, config, tmp_path)
    records = list(read_traces(summary.run_dir))
    assert len(records) == N_DEV_ITEMS and summary.error_records == N_DEV_ITEMS
    for record in records:
        assert record.error and "ModelCallFailed" in record.error
        assert record.final_answer is None and record.attempts == () and record.scores == ()
        assert len(record.model_calls) == 1 and record.model_calls[0].error


def test_retried_calls_are_all_in_the_trace(tmp_path):
    flaky = {"name": "scripted_fake", "label": "flaky", "options": {"model_id": "fake-cheap", "fail_first": 2}}
    repo, config, _raw = h.setup(tmp_path, models=[flaky], repeats=1)
    records = list(read_traces(run(repo, config, tmp_path).run_dir))
    for record in records:
        assert record.error is None
        assert [c.error is None for c in record.model_calls] == [False, False, True]


def test_unparseable_output_is_kept_and_scored_never_dropped(tmp_path):
    garbage = {"name": "scripted_fake", "label": "garbage", "options": {"model_id": "fake-cheap", "behaviour": "garbage"}}
    repo, config, _raw = h.setup(tmp_path, models=[garbage], repeats=1)
    records = list(read_traces(run(repo, config, tmp_path).run_dir))
    assert len(records) == N_DEV_ITEMS
    assert all(r.final_answer.status is AnswerStatus.UNPARSEABLE and label_of(r) == "unparseable" for r in records)
    assert all(r.final_answer.raw == "The literature is mixed [1]." for r in records)


def test_a_crashing_scorer_does_not_discard_the_arm_output(tmp_path):
    @register("scorer", "broken_scorer")
    class Broken:
        name = "broken_scorer"

        def score(self, item, final_answer):
            raise RuntimeError("scorer bug")

    try:
        repo, config, _raw = h.setup(tmp_path, scorers=[{"name": "provenance"}, {"name": "broken_scorer"}], repeats=1)
        records = list(read_traces(run(repo, config, tmp_path, selection="bad").run_dir))
    finally:
        registry._factories["scorer"].pop("broken_scorer")
    assert all(r.final_answer is not None for r in records)
    assert {(s.scorer, s.label) for r in records for s in r.scores} == {
        ("provenance", "competitor_only"),
        ("broken_scorer", runner.SCORER_ERROR_LABEL),
    }


# -- selection, resume ------------------------------------------------------------- #


def test_per_model_selection_runs_only_that_model(tmp_path):
    repo, config, _raw = h.setup(tmp_path)
    summary = run(repo, config, tmp_path, selection="bad")
    assert {r.model for r in read_traces(summary.run_dir)} == {"bad"}
    assert {label for label, _request in fakes.CALLS} == {"bad"}
    assert runner.read_invocations(summary.run_dir)[0]["selected_models"] == ["bad"]
    with pytest.raises(runner.RunnerError, match="no model with label"):
        run(repo, config, tmp_path, selection="nobody")


def test_resume_continues_an_interrupted_run_without_repeating_calls(tmp_path, monkeypatch):
    repo, config, _raw = h.setup(tmp_path, runner={"concurrency": 1, "max_usd": 5.0})
    real_append = runner.TraceWriter.append
    written = {"n": 0}

    def dying_append(self, record):
        if written["n"] == 50:
            raise KeyboardInterrupt
        written["n"] += 1
        real_append(self, record)

    monkeypatch.setattr(runner.TraceWriter, "append", dying_append)
    with pytest.raises(KeyboardInterrupt):
        run(repo, config, tmp_path)
    (run_dir,) = (tmp_path / "runs").iterdir()
    assert read_manifest(run_dir).ended_at is None
    assert len(list(read_traces(run_dir))) == 50
    monkeypatch.setattr(runner.TraceWriter, "append", real_append)

    fakes.CALLS.clear()
    summary = run(repo, config, tmp_path, resume_dir=run_dir)
    records = list(read_traces(run_dir))
    total = N_DEV_ITEMS * 2 * 2
    assert summary.closed and len(records) == total
    assert summary.records_already_present == 50 and summary.records_written == total - 50
    assert len({(r.item_id, r.arm, r.model, r.repeat) for r in records}) == total
    # Only the missing records were requested again (plus at most the one lost in flight).
    assert total - 50 <= len(fakes.CALLS) <= total - 50 + 1
    # Spend after resume covers the whole run, not just the second part.
    per_call = (1000 * 1.0 + 500 * 2.0) / 1_000_000
    assert sum(summary.spend_usd.values()) == pytest.approx(total * per_call)
    events = [entry["event"] for entry in runner.read_invocations(run_dir)]
    assert events == ["started", "ended", "resumed", "ended"]
    with pytest.raises(TraceError, match="already closed"):
        run(repo, config, tmp_path, resume_dir=run_dir)


def test_resume_refuses_a_different_config(tmp_path, monkeypatch):
    repo, config, raw = h.setup(tmp_path)
    summary = run(repo, config, tmp_path)
    with pytest.raises(TraceError):
        run(repo, parse_config({**raw, "repeats": 3}), tmp_path, resume_dir=summary.run_dir)


# -- dry run ------------------------------------------------------------------------ #


def test_dry_run_counts_calls_and_costs_without_calling_any_model_or_writing_traces(tmp_path):
    repo, config, _raw = h.setup(tmp_path)
    report = runner.dry_run(config, repo_root=repo)
    assert fakes.CALLS == []
    assert not (tmp_path / "runs").exists()
    assert (report.n_groups, report.n_items, report.requests_per_repeat) == (3, N_DEV_ITEMS, N_DEV_ITEMS)
    assert report.items_by_stratum["absent/null_control"] == 3 and len(report.items_by_stratum) == 15
    good = report.models[0]
    assert (good.label, good.model_id, good.calls) == ("good", "fake-cheap", N_DEV_ITEMS * 2)
    tokens = good.input_tokens
    assert tokens == pytest.approx(report.input_chars_per_repeat * 2 / 4, abs=N_DEV_ITEMS * 2)
    assert good.cost_low == pytest.approx((tokens * 1.0 + good.calls * 100 * 2.0) / 1e6)
    assert good.cost_high == pytest.approx((tokens * 1.0 + good.calls * 1100 * 2.0) / 1e6)
    assert good.cost_ceiling == pytest.approx((tokens * 1.0 + good.calls * 4096 * 2.0) / 1e6)
    assert good.cap == 5.0
    text = report.format()
    assert "DRY RUN" in text and "no model API was called" in text and "TOTAL" in text


def test_dry_run_honours_limit_as_first_n_groups(tmp_path):
    repo, config, _raw = h.setup(tmp_path, limit=2)
    report = runner.dry_run(config, repo_root=repo, selection="good")
    assert (report.n_groups, report.n_items, len(report.models)) == (2, 30, 1)


def test_dry_run_reports_an_unpriced_model_instead_of_guessing(tmp_path):
    unknown = {"name": "scripted_fake", "label": "new", "options": {"model_id": "no-price-yet"}}
    repo, config, _raw = h.setup(tmp_path, models=[unknown])
    report = runner.dry_run(config, repo_root=repo)
    assert report.models[0].priced is False and report.models[0].cost_low is None
    assert "no price" in report.format()
    with pytest.raises(runner.RunnerError, match="no price"):
        run(repo, config, tmp_path)


# -- spend safeguards ----------------------------------------------------------------- #


def test_paid_run_refuses_without_confirmation_and_calls_nothing(tmp_path):
    repo, config, _raw = h.setup(tmp_path)
    with pytest.raises(runner.RunnerError, match="confirm"):
        run(repo, config, tmp_path, confirm_spend=False)
    assert fakes.CALLS == [] and not (tmp_path / "runs").exists()


def test_paid_run_needs_a_budget_cap(tmp_path):
    repo, config, _raw = h.setup(tmp_path, runner={"concurrency": 2})
    with pytest.raises(runner.RunnerError, match="budget cap"):
        run(repo, config, tmp_path)
    assert fakes.CALLS == []


def test_failed_preflight_stops_the_run_before_any_call(tmp_path):
    ghost = {"name": "scripted_fake", "label": "ghost", "options": {"model_id": "fake-cheap", "preflight_ok": False}}
    repo, config, _raw = h.setup(tmp_path, models=[ghost])
    with pytest.raises(runner.RunnerError, match="preflight failed"):
        run(repo, config, tmp_path)
    assert fakes.CALLS == [] and not (tmp_path / "runs").exists()
    assert [r.ok for r in runner.preflight(config)] == [False]


def test_run_refuses_when_even_the_low_estimate_exceeds_the_cap(tmp_path):
    dear = {"name": "scripted_fake", "label": "dear", "options": {"model_id": "fake-dear"}}
    repo, config, _raw = h.setup(tmp_path, models=[dear], runner={"max_usd": 0.01})
    with pytest.raises(runner.RunnerError, match="low cost estimate"):
        run(repo, config, tmp_path)
    assert fakes.CALLS == []


def test_budget_cap_aborts_cleanly_and_leaves_the_run_unfinished(tmp_path):
    # Reported usage (50,000 output tokens per call) is far above the estimate: $10 per call.
    dear = {"name": "scripted_fake", "label": "dear", "options": {"model_id": "fake-dear", "output_tokens": 50_000, "input_tokens": 0}}
    cheap = {"name": "scripted_fake", "label": "cheap", "options": {"model_id": "fake-cheap"}}
    repo, config, _raw = h.setup(
        tmp_path, models=[dear, cheap], repeats=1, runner={"concurrency": 2, "max_usd": 45.0, "max_usd_by_model": {"cheap": 1.0}}
    )
    messages = []
    summary = run(repo, config, tmp_path, log=messages.append)
    assert summary.budget_exceeded and summary.aborted_models == ["dear"] and not summary.closed
    records = list(read_traces(summary.run_dir))
    dear_records = [r for r in records if r.model == "dear"]
    # Cap 45 at $10 per call: stops at the 5th call; at most `concurrency` more were in flight.
    assert 5 <= len(dear_records) <= 5 + 2
    assert summary.spend_usd["dear"] >= 45.0
    assert any("BUDGET CAP REACHED for dear" in m for m in messages)
    # The other model is unaffected and complete.
    assert len([r for r in records if r.model == "cheap"]) == N_DEV_ITEMS
    assert read_manifest(summary.run_dir).ended_at is None
    # Resuming under the same config cannot spend more on the capped model.
    fakes.CALLS.clear()
    again = run(repo, config, tmp_path, resume_dir=summary.run_dir)
    assert again.aborted_models == ["dear"] and fakes.CALLS == []


def test_a_response_without_usage_is_charged_conservatively(tmp_path):
    silent = {"name": "scripted_fake", "label": "silent", "options": {"model_id": "fake-cheap", "report_usage": False}}
    repo, config, _raw = h.setup(tmp_path, models=[silent], repeats=1)
    summary = run(repo, config, tmp_path)
    # Charged as max_output_tokens (4096) of output per call plus the estimated input.
    assert summary.spend_usd["silent"] > N_DEV_ITEMS * 4096 * 2.0 / 1e6


# -- settings -------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "table",
    [{"concurrency": 0}, {"concurrency": 1000}, {"max_usd": 0}, {"max_usd": "10"}, {"surprise": 1}, {"max_usd_by_model": {"nobody": 1.0}}, {"prices": ""}],
)
def test_runner_settings_are_validated(tmp_path, table):
    _repo, config, _raw = h.setup(tmp_path, runner=table)
    with pytest.raises(runner.RunnerError):
        runner.RunnerSettings.from_config(config)


def test_runner_settings_defaults_and_overrides(tmp_path):
    _repo, config, _raw = h.setup(tmp_path, runner={"max_usd": 3, "max_usd_by_model": {"bad": 9.5}})
    settings = runner.RunnerSettings.from_config(config)
    assert (settings.concurrency, settings.prices) == (4, "configs/prices.toml")
    assert (settings.cap_for("good"), settings.cap_for("bad")) == (3.0, 9.5)


# -- test-split discipline --------------------------------------------------------------- #


def test_a_test_config_is_refused_while_the_preregistration_is_a_draft_and_the_refusal_is_logged(tmp_path):
    repo, config, _raw = h.setup(tmp_path, split="test")
    with pytest.raises(runner.RunnerError, match="freeze tag"):
        run(repo, config, tmp_path)
    from binding_gate.testlock import TestSplitLocked

    with pytest.raises(TestSplitLocked):
        run(repo, config, tmp_path, test_unlock_tag="prereg-freeze-1", purpose="unit test")
    entries = read_access_log(repo / ACCESS_LOG_RELPATH)
    assert [e["event"] for e in entries] == [EVENT_REFUSED]
    assert fakes.CALLS == [] and not (tmp_path / "runs").exists()


def test_dev_runs_never_touch_the_test_lock(tmp_path):
    repo, config, _raw = h.setup(tmp_path)
    summary = run(repo, config, tmp_path)
    assert not (repo / ACCESS_LOG_RELPATH).exists()
    test_groups = {i.group_id for i in registry.create("dataset", "deceptive_grounding", **config.dataset.options).iter_items() if item_split(i) is Split.TEST}
    assert len(test_groups) == 2
    assert not test_groups & {r.group_id for r in read_traces(summary.run_dir)}
    prompts = " ".join(request.messages[-1].content for _label, request in fakes.CALLS)
    assert not any(f"using {group.split(' || ')[0]} to treat" in prompts for group in test_groups)
    with pytest.raises(runner.RunnerError, match="split is 'dev'"):
        run(repo, config, tmp_path, test_unlock_tag="prereg-freeze-1")


def test_the_real_repository_access_log_shows_no_test_split_load():
    entries = read_access_log(REAL_REPO / ACCESS_LOG_RELPATH)
    assert not [e for e in entries if e.get("event") == EVENT_LOADED]
