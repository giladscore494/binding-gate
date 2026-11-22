"""PR 13 mechanics behind the USD 50 plan: subsamples, seed wildcard, seeded dry runs, search quota."""

from __future__ import annotations

import pytest

import run_helpers as h
from binding_gate import runner
from binding_gate.config import ConfigError, parse_config
from binding_gate.contracts import Split
from binding_gate.sampling import SUBSAMPLE_SEED, sample_key, stratified_sample
from binding_gate.stats.analysis import analyse_run_dirs
from binding_gate.trace import read_traces
from test_live_runner import make as make_live
from test_live_runner import _clean  # noqa: F401 - autouse fixture: fresh research sessions per test
from test_pr12_resume_and_seeding import run, seed_run

# --------------------------------------------------------------------------- #
# Stratified subsample
# --------------------------------------------------------------------------- #


class _Item:
    def __init__(self, item_id: str, stratum: str | None):
        self.id, self.stratum = item_id, stratum


ITEMS = [_Item(f"t{k}/{s}", s) for k in range(40) for s in ("absent/null_control", "complete/synthetic_y", "partial/class_proximate")]


def test_subsample_takes_the_smallest_keys_per_stratum_in_input_order():
    chosen = stratified_sample(ITEMS, per_stratum=5, seed=SUBSAMPLE_SEED)
    assert len(chosen) == 15
    for stratum in {i.stratum for i in ITEMS}:
        members = [i for i in ITEMS if i.stratum == stratum]
        expected = sorted(members, key=lambda i: sample_key(SUBSAMPLE_SEED, i.id))[:5]
        assert {i.id for i in chosen if i.stratum == stratum} == {i.id for i in expected}
    positions = [ITEMS.index(i) for i in chosen]
    assert positions == sorted(positions)


def test_subsamples_with_one_seed_are_nested_and_a_small_stratum_is_taken_whole():
    small = {i.id for i in stratified_sample(ITEMS, per_stratum=4, seed=SUBSAMPLE_SEED)}
    large = {i.id for i in stratified_sample(ITEMS, per_stratum=20, seed=SUBSAMPLE_SEED)}
    assert small < large
    assert len(stratified_sample(ITEMS, per_stratum=1000, seed=1)) == len(ITEMS)
    assert stratified_sample(ITEMS, per_stratum=5, seed=1) != stratified_sample(ITEMS, per_stratum=5, seed=2)
    assert len(stratified_sample([_Item("a", None), _Item("b", None)], per_stratum=1, seed=0)) == 1
    with pytest.raises(ValueError):
        stratified_sample(ITEMS, per_stratum=0, seed=1)


def test_sample_table_is_validated_hashed_only_when_present_and_allowed_on_test(tmp_path):
    raw = h.raw_config({"root": "x"})
    plain = parse_config(raw)
    assert plain.sample is None and "sample" not in plain.to_dict()
    sampled = parse_config({**raw, "sample": {"per_stratum": 3, "seed": 7}})
    assert sampled.to_dict()["sample"] == {"per_stratum": 3, "seed": 7} and sampled.config_hash != plain.config_hash
    test = parse_config({**raw, "split": "test", "sample": {"per_stratum": 3, "seed": 7}})
    assert test.split is Split.TEST
    for bad in ({"per_stratum": 0, "seed": 1}, {"per_stratum": 2}, {"per_stratum": 2, "seed": -1, "x": 1}, {"per_stratum": 2, "seed": True}):
        with pytest.raises(ConfigError):
            parse_config({**raw, "sample": bad})
    with pytest.raises(ConfigError, match="cannot be combined"):
        parse_config({**raw, "limit": 2, "sample": {"per_stratum": 3, "seed": 7}})


def test_the_runner_applies_the_subsample_after_the_split(tmp_path):
    repo, _config, raw = h.setup(tmp_path)
    full = runner.load_plan(parse_config(raw), repo_root=repo)
    sampled = runner.load_plan(parse_config({**raw, "sample": {"per_stratum": 2, "seed": SUBSAMPLE_SEED}}), repo_root=repo)
    strata = {i.stratum for i in full.items}
    assert len(sampled.items) == 2 * len(strata)
    assert [i.id for i in sampled.items] == [i.id for i in stratified_sample(full.items, per_stratum=2, seed=SUBSAMPLE_SEED)]


# --------------------------------------------------------------------------- #
# Seeds: the "seeds/*" wildcard and the seeded dry run
# --------------------------------------------------------------------------- #


def test_seed_wildcard_is_the_only_pattern_and_stands_alone():
    raw = h.raw_config({"root": "x"})
    for table in ({"seed_responses_from": ["seeds/run-*"]}, {"seed_responses_from": ["seeds/*", "seeds/a"]}):
        with pytest.raises(runner.RunnerError):
            runner.RunnerSettings.from_config(parse_config({**raw, "runner": table}))
    settings = runner.RunnerSettings.from_config(parse_config({**raw, "runner": {"seed_responses_from": ["seeds/*"]}}))
    assert settings.seed_responses_from == ("seeds/*",)


def test_seed_wildcard_expands_to_downloaded_run_directories(tmp_path):
    (tmp_path / "seeds" / "run-b").mkdir(parents=True)
    (tmp_path / "seeds" / "run-b" / "manifest.json").write_text("{}")
    (tmp_path / "seeds" / "run-a").mkdir()
    (tmp_path / "seeds" / "run-a" / "manifest.json").write_text("{}")
    (tmp_path / "seeds" / "not-a-run").mkdir()
    (tmp_path / "seeds" / "baseline_report_x.txt").write_text("report")
    assert runner.expand_seed_paths(("seeds/*",), tmp_path) == ("seeds/run-a", "seeds/run-b")
    assert runner.expand_seed_paths(("seeds/*",), tmp_path / "elsewhere") == ()
    assert runner.expand_seed_paths(("seeds/x",), tmp_path) == ("seeds/x",)


def test_a_seeded_run_with_the_wildcard_reuses_answers_and_its_dry_run_subtracts_them(tmp_path):
    repo, raw, seed_dir = seed_run(tmp_path, repeats=1)
    target = repo / "seeds" / seed_dir.name
    target.parent.mkdir()
    seed_dir.rename(target)
    config = parse_config({**raw, "repeats": 1, "runner": {"concurrency": 2, "max_usd": 5.0, "seed_responses_from": ["seeds/*"]}})
    report = runner.dry_run(config, repo_root=repo)
    assert [s.path for s in report.seeds] == [f"seeds/{seed_dir.name}"] and not report.seed_note
    assert all(m.calls == 0 and m.cost_high == 0 for m in report.models)  # every answer is seeded
    summary = run(repo, config, tmp_path / "runs")
    answers = [c for r in read_traces(summary.run_dir) for c in r.model_calls if c.purpose == "answer"]
    assert answers and all(runner.SEEDED_FROM_KEY in c.response.raw for c in answers)
    assert all(v == 0 for v in summary.spend_usd.values())


def test_without_any_downloaded_seed_the_dry_run_says_so_and_a_paid_run_refuses(tmp_path):
    repo, _config, raw = h.setup(tmp_path)
    config = parse_config({**raw, "runner": {"concurrency": 2, "max_usd": 5.0, "seed_responses_from": ["seeds/*"]}})
    report = runner.dry_run(config, repo_root=repo)
    assert "none was found" in report.seed_note and "NOTE:" in report.format()
    with pytest.raises(runner.RunnerError, match="no seed run directory"):
        run(repo, config, tmp_path / "runs")


# --------------------------------------------------------------------------- #
# Search quota ([runner] max_searches)
# --------------------------------------------------------------------------- #


def test_the_search_quota_stops_starting_units_and_a_resume_finishes_the_run(tmp_path, monkeypatch):
    repo, config, raw, _web, serp = make_live(tmp_path, monkeypatch, repeats=1)
    limited = parse_config({**raw, "runner": {**raw["runner"], "concurrency": 1, "max_searches": 3}})
    messages: list[str] = []
    first = runner.execute_run(limited, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=messages.append)
    # one unit may take 1 (no_gate) + 2 (gated_research) searches; after the first item 2 were made
    assert first.search_limit_reached and first.budget_exceeded and not first.closed
    assert first.searches == len(serp.queries) == 2 and first.search_limit == 3
    assert any("SEARCH QUOTA REACHED" in m for m in messages)
    assert len({r.item_id for r in read_traces(first.run_dir)}) == 1
    # the next month: the same config (a resume needs the same config hash); the count starts again
    second = runner.execute_run(limited, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True,
                                resume_dir=first.run_dir, log=lambda m: None)
    assert second.closed and not second.search_limit_reached
    assert len({r.item_id for r in read_traces(second.run_dir)}) == 2


def test_the_quota_is_lowered_to_the_searches_the_account_reports_as_left(tmp_path, monkeypatch):
    repo, config, raw, _web, serp = make_live(tmp_path, monkeypatch, repeats=1)
    serp.account["total_searches_left"] = 2
    limited = parse_config({**raw, "runner": {**raw["runner"], "concurrency": 1, "max_searches": 240}})
    messages: list[str] = []
    summary = runner.execute_run(limited, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=messages.append)
    assert summary.search_limit == 2 and summary.search_limit_reached and summary.searches == 0
    assert any("2 searches left" in m for m in messages)


def test_max_searches_is_validated():
    raw = h.raw_config({"root": "x"})
    for bad in (0, -1, 2.5, True):
        with pytest.raises(runner.RunnerError):
            runner.RunnerSettings.from_config(parse_config({**raw, "runner": {"max_searches": bad}}))


# --------------------------------------------------------------------------- #
# Analysis: a subsample run is never confirmatory
# --------------------------------------------------------------------------- #


def test_an_analysis_of_a_subsample_run_says_it_is_exploratory(tmp_path):
    repo, _config, raw = h.setup(tmp_path, repeats=1)
    summary = run(repo, parse_config({**raw, "sample": {"per_stratum": 2, "seed": SUBSAMPLE_SEED}}), tmp_path / "runs")
    analysis = analyse_run_dirs([summary.run_dir], resamples=20)
    model = next(iter(analysis["models"].values()))
    reasons = " ".join(model["hypotheses"]["H1"]["non_confirmatory_reasons"])
    assert "pre-registered item subsample" in reasons
