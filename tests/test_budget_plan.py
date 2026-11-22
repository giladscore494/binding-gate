"""PR 13: the USD 50 budget plan (configs/budget.toml; PREREGISTRATION.md section 9, "Budget plan").

The plan must cover every paid config of the study, its caps must be the caps the configs
enforce, and the caps together must stay under the hard limit of 50 USD. The full-scale design
of PRs 1-12 is kept under configs/extended/, marked as not part of the study.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from binding_gate import runner
from binding_gate.config import load_config
from binding_gate.pricing import load_prices
from binding_gate.scorers.llm_judge import RescoreConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "configs"
PLAN = tomllib.loads((CONFIGS / "budget.toml").read_text(encoding="utf-8"))
RUNS = PLAN["run"]
NOT_PAID = {"prices.toml", "budget.toml", "example.toml"}
"""Files in configs/ that make no paid call: the price table, this plan, and the format illustration
(whose model client is a stand-in that does not exist)."""


def config_cap(entry: dict) -> float:
    """What the config of a plan entry enforces at most, per invocation, in US dollars."""
    path = REPO_ROOT / entry["config"]
    if entry["kind"] == "run":
        config = load_config(path)
        settings = runner.RunnerSettings.from_config(config)
        return sum(settings.cap_for(m.label) for m in config.models)
    if entry["kind"] == "decomposer_eval":
        return tomllib.loads(path.read_text(encoding="utf-8"))["runner"]["max_usd"]
    if entry["kind"] == "rescore":
        rescore = RescoreConfig.load(path)
        return rescore.max_usd * len(rescore.judges)  # the cap is per judge model and invocation
    raise AssertionError(f"unknown kind {entry['kind']!r}")


def test_the_plan_stays_under_the_hard_limit_with_a_reserve():
    assert PLAN["hard_limit_usd"] == 50.0
    assert PLAN["planned_high_max_usd"] + PLAN["reserve_usd"] <= PLAN["hard_limit_usd"]
    total_high = sum(r["high_usd"] for r in RUNS)
    total_caps = sum(r["cap_usd"] for r in RUNS)
    assert total_high <= PLAN["planned_high_max_usd"], total_high
    assert total_caps <= PLAN["hard_limit_usd"], total_caps
    assert PLAN["hard_limit_usd"] - total_high >= PLAN["reserve_usd"]


def test_every_entry_is_complete_and_its_cap_covers_its_high_estimate():
    ids = [r["id"] for r in RUNS]
    assert len(ids) == len(set(ids))
    for r in RUNS:
        assert set(r) == {"id", "config", "kind", "invocations", "models", "items", "arms", "low_usd", "high_usd", "cap_usd", "note"}
        assert r["invocations"] >= 1 and 0 <= r["low_usd"] <= r["high_usd"] <= r["cap_usd"], r["id"]


@pytest.mark.parametrize("entry", [r for r in RUNS if r["kind"] != "planned"], ids=lambda r: r["id"])
def test_each_cap_in_the_plan_is_what_its_config_enforces(entry):
    assert not entry["config"].startswith("configs/extended/")
    assert entry["cap_usd"] == pytest.approx(entry["invocations"] * config_cap(entry), abs=0.006)


def test_the_caps_of_all_configs_referenced_by_the_plan_sum_to_at_most_50_usd():
    enforced = sum(r["invocations"] * config_cap(r) for r in RUNS if r["kind"] != "planned")
    planned = sum(r["cap_usd"] for r in RUNS if r["kind"] == "planned")
    assert enforced + planned <= 50.0


def test_no_planned_run_is_left_without_a_config():
    """PR 16 (section 17, item 39): the test configs of layers 2 and 3 exist and enforce their planned caps."""
    assert [r for r in RUNS if r["kind"] == "planned"] == []
    by_id = {r["id"]: r for r in RUNS}
    assert by_id["test_live"]["config"] == "configs/test_live.toml"
    assert by_id["test_grab_rag_supportive"]["config"] == "configs/test_grab_rag_supportive.toml"


def test_every_paid_config_is_in_the_plan_or_marked_extended():
    referenced = {r["config"] for r in RUNS if r["config"]}
    paid = {
        p.relative_to(REPO_ROOT).as_posix()
        for p in [*CONFIGS.glob("*.toml"), *(CONFIGS / "rescore").glob("*.toml")]
        if p.name not in NOT_PAID
    }
    assert paid - referenced == set(), "paid configs missing from configs/budget.toml"
    assert referenced - paid == set(), "the plan names configs that do not exist"


@pytest.mark.parametrize(
    "path", sorted(p for p in (CONFIGS / "extended").rglob("*.toml") if p.name != "prices.toml"), ids=lambda p: p.name
)
def test_extended_configs_are_marked_and_priced_separately(path):
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# EXTENDED DESIGN - NOT PART OF THE USD 50 STUDY; NOT RUN (PR 13).")
    raw = tomllib.loads(text)
    assert raw["name"].startswith("extended-")
    assert '"configs/prices.toml"' not in text and '"configs/extended/prices.toml"' in text


def test_extended_price_file_has_the_same_token_prices_and_the_old_assumptions():
    main = load_prices(CONFIGS / "prices.toml")
    extended = load_prices(CONFIGS / "extended" / "prices.toml")
    assert extended.assumptions.reasoning_tokens_high == 2000
    dropped = {"claude-haiku-5-5", "claude-sonnet-5-5", "deepseek-flash"}  # PR 14: not in the USD 50 study
    assert not dropped & set(main.prices) and {"claude-haiku-5-5", "claude-sonnet-5-5"} <= set(extended.prices)
    for model_id, price in extended.prices.items():
        if model_id in dropped:
            continue
        if model_id == "serpapi/google":
            assert price.per_call_usd == 0.025 and main.get(model_id).per_call_usd == 0.0
            continue
        assert (price.input_per_mtok, price.output_per_mtok) == (
            main.get(model_id).input_per_mtok, main.get(model_id).output_per_mtok
        ), model_id


def test_judge_only_limits_fit_inside_the_runner_caps():
    for r in RUNS:
        if r["kind"] != "run":
            continue
        config = load_config(REPO_ROOT / r["config"])
        settings = runner.RunnerSettings.from_config(config)
        for arm in config.arms:
            if "judge_max_usd" in arm.options:
                assert all(arm.options["judge_max_usd"] <= settings.cap_for(m.label) for m in config.models)


def test_month_one_searches_fit_the_serpapi_free_plan():
    limits = [runner.RunnerSettings.from_config(load_config(CONFIGS / n)).max_searches for n in ("dev_live_smoke.toml", "dev_live.toml")]
    assert sum(limits) <= 250


def test_month_two_test_searches_fit_the_serpapi_free_plan():
    """PR 16: the layer-3 test run is month 2 of the allocation (resumed in month 3 if needed)."""
    assert runner.RunnerSettings.from_config(load_config(CONFIGS / "test_live.toml")).max_searches <= 250


@pytest.mark.skipif(
    not (REPO_ROOT / "data" / "raw" / "deceptive-grounding-benchmark" / "data").is_dir(),
    reason="benchmark not cloned into data/raw/ (it is never committed)",
)
def test_the_plan_reproduces_the_dry_run_of_the_dev_baseline():
    report = runner.dry_run(load_config(CONFIGS / "dev_baseline.toml"), repo_root=REPO_ROOT)
    (entry,) = [r for r in RUNS if r["id"] == "dev_baseline"]
    assert entry["low_usd"] == pytest.approx(sum(report.model_total(m.label, "cost_low") for m in report.models), abs=0.01)
    assert entry["high_usd"] == pytest.approx(sum(report.model_total(m.label, "cost_high") for m in report.models), abs=0.01)
