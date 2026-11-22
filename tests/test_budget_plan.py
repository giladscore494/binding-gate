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
ACTIVE = [r for r in RUNS if r.get("status") != "stopped"]
"""PR 22: runs a pre-registered stop rule stopped (status = "stopped") are checked against their configs but are
not part of the planned totals (the ledger refuses to run them)."""
STOPPED_BY_LAYER1_STOP = {
    "dev_repeats", "dev_gated", "dev_gated_scope", "dev_ablation_quote_verbatim", "dev_ablation_entity_in_window",
    "dev_ablation_qualifiers_in_window", "dev_ablation_no_closer_competitor", "dev_ablation_premise_present",
    "dev_baselines", "dev_llm_judge_checks", "rescore_dev_llm_judge", "test_layer1", "test_layer1_midtier",
}
VALIDATION_RUNS = ("validate_layer1_judge_structured", "validate_layer1_freeform", "validate_layer1_judge_freeform")
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
    total_high = sum(r["high_usd"] for r in ACTIVE)
    total_caps = sum(r["cap_usd"] for r in ACTIVE)
    assert total_high <= PLAN["planned_high_max_usd"], total_high
    assert total_caps <= PLAN["hard_limit_usd"], total_caps
    assert PLAN["hard_limit_usd"] - total_high >= PLAN["reserve_usd"]


def test_every_entry_is_complete_and_its_cap_covers_its_high_estimate():
    ids = [r["id"] for r in RUNS]
    assert len(ids) == len(set(ids))
    base = {"id", "config", "kind", "invocations", "models", "items", "arms", "low_usd", "high_usd", "cap_usd", "note"}
    for r in RUNS:
        assert set(r) in (base, base | {"status", "stopped_by"}), r["id"]
        if "status" in r:
            assert r["status"] == "stopped" and r["stopped_by"].strip(), r["id"]
        assert r["invocations"] >= 1 and 0 <= r["low_usd"] <= r["high_usd"] <= r["cap_usd"], r["id"]


@pytest.mark.parametrize("entry", [r for r in RUNS if r["kind"] != "planned"], ids=lambda r: r["id"])
def test_each_cap_in_the_plan_is_what_its_config_enforces(entry):
    assert not entry["config"].startswith("configs/extended/")
    assert entry["cap_usd"] == pytest.approx(entry["invocations"] * config_cap(entry), abs=0.006)


def test_the_caps_of_all_configs_referenced_by_the_plan_sum_to_at_most_50_usd():
    enforced = sum(r["invocations"] * config_cap(r) for r in ACTIVE if r["kind"] != "planned")
    planned = sum(r["cap_usd"] for r in ACTIVE if r["kind"] == "planned")
    assert enforced + planned <= 50.0


def test_no_planned_run_is_left_without_a_config():
    """PR 16 (section 17, item 39) and PR 17: the test configs of layers 2 and 3 exist and enforce their planned caps."""
    assert [r for r in RUNS if r["kind"] == "planned"] == []
    by_id = {r["id"]: r for r in RUNS}
    assert by_id["test_live"]["config"] == "configs/test_live.toml"
    assert by_id["test_sec"]["config"] == "configs/test_sec.toml"
    assert not any("grab_rag" in r["config"] for r in RUNS)  # PR 17: GRAB-RAG is in the extended design only


def test_pr17_totals_and_the_layer_2_entries():
    """PR 17: layer 2 is the SEC financial-filings layer; GRAB-RAG left the plan; nothing else changed."""
    by_id = {r["id"]: r for r in RUNS}
    sec = [by_id[i] for i in ("dev_sec_smoke", "dev_sec_baseline", "dev_sec_gated", "test_sec")]
    assert round(sum(r["high_usd"] for r in sec), 2) == 4.28 and round(sum(r["cap_usd"] for r in sec), 2) == 5.05
    # PR 18 added dev_gated_scope and dev_sec_gated_scope (0.21 HIGH, 0.60 caps); see test_pr18_scope_runs.
    # PR 22 added the validation runs and stopped 13 layer-1 runs (test_pr22_...); every earlier entry is unchanged.
    # PR 27 re-derived the three layer-3 entries (searches priced at $0.001, Serper): HIGH +1.04, caps +1.05
    # (before PR 27: 42.78 / 48.35); see test_pr27_layer3_entries_with_priced_searches.
    before = [r for r in RUNS if r["id"] not in VALIDATION_RUNS]
    assert round(sum(r["high_usd"] for r in before), 2) == 43.82 and round(sum(r["cap_usd"] for r in before), 2) == 49.40
    assert by_id["test_layer1"]["cap_usd"] == 16.75 and by_id["test_layer1_midtier"]["cap_usd"] == 6.95
    assert by_id["rescore_dev_llm_judge"]["cap_usd"] == 4.75 and by_id["dev_baselines"]["cap_usd"] == 1.10


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
        if model_id == "serper/google":  # PR 27: the same conservative price in both files
            assert price.per_call_usd == main.get(model_id).per_call_usd == 0.001
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


def test_all_layer3_searches_fit_the_serper_free_tier():
    """PR 27: Serper's free tier gives 2,500 queries on signup (no monthly renewal); the caps of PR 13/16 stay."""
    limits = {n: runner.RunnerSettings.from_config(load_config(CONFIGS / n)).max_searches
              for n in ("dev_live_smoke.toml", "dev_live.toml", "test_live.toml")}
    assert limits == {"dev_live_smoke.toml": 10, "dev_live.toml": 240, "test_live.toml": 250}
    # every invocation also spends one preflight search; even two resumes of the test run fit
    assert sum(limits.values()) + 2 * limits["test_live.toml"] + 10 <= 2500


def test_pr27_layer3_entries_with_priced_searches():
    """PR 27 (amendment 3): searches cost $0.001 (Serper); the layer-3 entries were re-derived by the same rule."""
    by_id = {r["id"]: r for r in RUNS}
    got = {i: (by_id[i]["low_usd"], by_id[i]["high_usd"], by_id[i]["cap_usd"]) for i in ("dev_live_smoke", "dev_live", "test_live")}
    assert got == {"dev_live_smoke": (0.01, 0.03, 0.10), "dev_live": (0.66, 1.64, 1.75), "test_live": (0.90, 2.22, 2.40)}
    assert load_prices(CONFIGS / "prices.toml").get("serper/google").per_call_usd == 0.001
    # test_live = dev_live x 146 / 108, rounded up
    assert abs(by_id["dev_live"]["high_usd"] * 146 / 108 - by_id["test_live"]["high_usd"]) < 0.01


@pytest.mark.skipif(
    not (REPO_ROOT / "data" / "raw" / "deceptive-grounding-benchmark" / "data").is_dir(),
    reason="benchmark not cloned into data/raw/ (it is never committed)",
)
def test_the_plan_reproduces_the_dry_run_of_the_dev_baseline():
    report = runner.dry_run(load_config(CONFIGS / "dev_baseline.toml"), repo_root=REPO_ROOT)
    (entry,) = [r for r in RUNS if r["id"] == "dev_baseline"]
    assert entry["low_usd"] == pytest.approx(sum(report.model_total(m.label, "cost_low") for m in report.models), abs=0.01)
    assert entry["high_usd"] == pytest.approx(sum(report.model_total(m.label, "cost_high") for m in report.models), abs=0.01)


def test_pr18_scope_runs_cost_what_their_gated_counterparts_cost_and_fit_the_room_left():
    """PR 18: the binding-scope candidate runs pay only the decomposer (answers seeded), like dev_gated / dev_sec_gated."""
    by_id = {r["id"]: r for r in RUNS}
    for scope, base in (("dev_gated_scope", "dev_gated"), ("dev_sec_gated_scope", "dev_sec_gated")):
        entry = by_id[scope]
        assert entry["config"] == f"configs/{scope}.toml" and entry["invocations"] == 1
        assert (entry["low_usd"], entry["high_usd"], entry["cap_usd"]) == (
            by_id[base]["low_usd"], by_id[base]["high_usd"], by_id[base]["cap_usd"])
        assert entry["cap_usd"] <= 50.0 - 47.75 - 0.30  # within the room PR 17 left, with the other scope run


def test_pr22_layer1_runs_stopped_by_section_10_and_the_validation_runs():
    """PR 22 (amendment 1): exactly the layer-1 runs the STOP ends are stopped; the validation fits the active plan."""
    by_id = {r["id"]: r for r in RUNS}
    assert {r["id"] for r in RUNS if r.get("status") == "stopped"} == STOPPED_BY_LAYER1_STOP
    assert all("45053502161" in by_id[i]["stopped_by"] and "amendment 1" in by_id[i]["stopped_by"]
               for i in STOPPED_BY_LAYER1_STOP)
    for still_active in ("smoke", "dev_baseline", "decomposer_eval", "dev_sec_baseline", "test_sec", "dev_live", "test_live"):
        assert "status" not in by_id[still_active]
    validation = [by_id[i] for i in VALIDATION_RUNS]
    assert [r["config"] for r in validation] == [
        "configs/rescore/validation_judge_structured.toml", "configs/dev_validation_freeform.toml",
        "configs/rescore/validation_judge_freeform.toml"]
    assert round(sum(r["low_usd"] for r in validation), 2) == 4.87
    assert round(sum(r["high_usd"] for r in validation), 2) == 22.43
    assert round(sum(r["cap_usd"] for r in validation), 2) == 23.65
    # PR 27: the layer-3 entries rose by HIGH 1.04 and caps 1.05 (before: 34.95 / 38.20)
    assert round(sum(r["high_usd"] for r in ACTIVE), 2) == 35.99 and round(sum(r["cap_usd"] for r in ACTIVE), 2) == 39.25
    for r in validation:  # caps just above HIGH: HIGH x 1.05 rounded up to 0.05 USD (at least 0.10 per model)
        assert r["high_usd"] <= r["cap_usd"] <= r["high_usd"] * 1.05 + 0.10, r["id"]


def test_pr22_one_subsample_for_every_validation_step():
    from binding_gate.scorers.llm_judge import RescoreConfig

    run = load_config(CONFIGS / "dev_validation_freeform.toml")
    judged = [RescoreConfig.load(CONFIGS / "rescore" / n) for n in ("validation_judge_structured.toml", "validation_judge_freeform.toml")]
    expected = {"per_stratum": 12, "seed": 20261122,
                "exclude_strata": ["absent/null_control", "partial/null_control", "complete/null_control"]}
    assert run.sample == expected and all(c.sample == expected for c in judged)
    assert judged[0].judge_hash == judged[1].judge_hash == RescoreConfig.load(CONFIGS / "rescore" / "dev_llm_judge.toml").judge_hash
    assert [a.name for a in run.arms] == ["free_form"] and [m.label for m in run.models] == ["openai-low", "google-low", "zai-low"]
    # the models are configured exactly as in the dev baseline
    baseline = load_config(CONFIGS / "dev_baseline.toml")
    assert [m.to_dict() for m in run.models] == [m.to_dict() for m in baseline.models]


@pytest.mark.skipif(
    not (REPO_ROOT / "data" / "raw" / "deceptive-grounding-benchmark" / "data").is_dir(),
    reason="benchmark not cloned into data/raw/ (it is never committed)",
)
def test_pr22_the_plan_reproduces_the_dry_run_of_the_freeform_run():
    report = runner.dry_run(load_config(CONFIGS / "dev_validation_freeform.toml"), repo_root=REPO_ROOT)
    (entry,) = [r for r in RUNS if r["id"] == "validate_layer1_freeform"]
    assert entry["low_usd"] == pytest.approx(sum(report.model_total(m.label, "cost_low") for m in report.models), abs=0.01)
    assert entry["high_usd"] == pytest.approx(sum(report.model_total(m.label, "cost_high") for m in report.models), abs=0.01)


@pytest.mark.skipif(
    not (REPO_ROOT / "data" / "raw" / "deceptive-grounding-benchmark" / "data").is_dir(),
    reason="benchmark not cloned into data/raw/ (it is never committed)",
)
@pytest.mark.parametrize("run_id, answer_chars", [("validate_layer1_judge_structured", 800),
                                                  ("validate_layer1_judge_freeform", 2000)])
def test_pr22_judge_estimates_from_the_real_judge_prompts(run_id, answer_chars):
    """The judge estimates of the validation: the real benchmark-judge prompts of the 144 subsample items, an answer
    of ``answer_chars`` characters, three models, the judge's own token assumptions (configs/prices.toml)."""
    from binding_gate.scorers import llm_judge as lj

    plan = runner.load_plan(load_config(CONFIGS / "dev_validation_freeform.toml"), repo_root=REPO_ROOT)
    assert len(plan.items) == 144
    book = load_prices(CONFIGS / "prices.toml")
    model_id = "kimi-k3.1-pro"
    tokens = sum(book.assumptions.input_tokens(sum(len(m.content) for m in lj.build_judge_request(item, "x" * answer_chars).messages))
                 for item in plan.items)
    calls = 3 * len(plan.items)
    visible = book.assumptions.visible_output_tokens
    low = book.get(model_id).cost(3 * tokens, calls * (visible + book.reasoning_low(model_id)))
    high = book.get(model_id).cost(3 * tokens, calls * (visible + book.reasoning_high(model_id)))
    (entry,) = [r for r in RUNS if r["id"] == run_id]
    assert entry["low_usd"] == pytest.approx(low, abs=0.01) and entry["high_usd"] == pytest.approx(high, abs=0.01)
