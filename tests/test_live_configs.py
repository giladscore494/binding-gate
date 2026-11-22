"""The committed layer-3 configs: pinned data, same first attempt for both arms, priced searches, dry run offline."""

from __future__ import annotations

from pathlib import Path

import pytest

import live_fakes as lf
import sealqa_fixture as fx
from binding_gate import runner
from binding_gate.config import load_config, parse_config
from binding_gate.datasets import sealqa
from binding_gate.gates.binding import ALL_CHECKS
from binding_gate.pricing import load_prices
from binding_gate.research.session import clear_sessions

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "configs"
FIRST_ATTEMPT_KEYS = ("presentation", "documents_per_attempt", "max_document_chars", "search", "fetch")


@pytest.mark.parametrize("name", ["dev_live_smoke.toml", "dev_live.toml"])
def test_live_configs_pin_the_data_and_share_the_first_attempt(name):
    config = load_config(CONFIGS / name)
    assert config.split.value == "dev" and config.dataset.name == "sealqa"
    options = config.dataset.options
    assert options["revision"] == sealqa.PINNED_REVISION and options["root"].startswith("data/raw/")
    assert options["subsets"] == ["seal_hard", "seal_0"]
    (baseline, gated) = config.arms
    assert (baseline.name, baseline.label, gated.name, gated.label) == ("no_gate_live", "no_gate", "gated_research", "gated_research")
    for key in FIRST_ATTEMPT_KEYS:
        assert baseline.options[key] == gated.options[key], key  # same searches, pages and answer request
    assert gated.options["max_attempts"] == 2 and gated.options["checks"] == list(ALL_CHECKS)
    dev_gated = load_config(CONFIGS / "dev_gated.toml").arms[1].options
    assert gated.options["parameters"] == dev_gated["parameters"] and gated.options["decomposer"] == dev_gated["decomposer"]
    assert [s.name for s in config.scorers] == ["sealqa_answer"]
    assert gated.options["search"]["name"] == "serpapi_google"


def test_dev_live_uses_primary_models_of_dev_gated_with_one_repeat_and_the_smoke_one():
    """PR 13: layer 3 runs slots A and E (configs/budget.toml), one repeat, the same model specs as dev_gated."""
    full = load_config(CONFIGS / "dev_live.toml")
    smoke = load_config(CONFIGS / "dev_live_smoke.toml")
    gated = {m.label: m.to_dict() for m in load_config(CONFIGS / "dev_gated.toml").models}
    assert [m.label for m in full.models] == ["openai-low", "google-low"]
    assert [m.to_dict() for m in full.models] == [gated[m.label] for m in full.models] and full.repeats == 1 and full.limit is None
    assert [m.to_dict() for m in smoke.models] == [gated["openai-low"]] and (smoke.repeats, smoke.limit) == (1, 5)
    assert full.to_dict()["arms"] == smoke.to_dict()["arms"]


def test_live_configs_keep_to_the_serpapi_free_plan():
    """PR 13: 250 searches per month at 50 per hour (serpapi.com/pricing, read 2026-11-22)."""
    full = load_config(CONFIGS / "dev_live.toml")
    smoke = load_config(CONFIGS / "dev_live_smoke.toml")
    limits = [runner.RunnerSettings.from_config(c).max_searches for c in (smoke, full)]
    assert limits == [10, 240] and sum(limits) <= 250  # month 1: smoke + dev
    for config in (full, smoke):
        for arm in config.arms:
            assert arm.options["search"]["options"]["min_interval_seconds"] >= 3600 / 50


@pytest.mark.parametrize("name", ["dev_live_smoke.toml", "dev_live.toml"])
def test_every_model_and_auxiliary_client_has_a_price_and_a_cap(name):
    config = load_config(CONFIGS / name)
    settings = runner.RunnerSettings.from_config(config)
    book = load_prices(REPO_ROOT / settings.prices)
    for spec in config.models:
        assert book.has(runner.model_id_of(runner.build_model(spec))) and settings.cap_for(spec.label)
    aux, problems = runner.auxiliary_clients(config)
    assert problems == [] and {a.role for a in aux} == {"search", "fetch", "decomposer"}
    for entry in aux:
        assert book.has(entry.model_id), entry.model_id
    # PR 13: SerpApi's Free plan costs nothing; its quota is enforced by [runner] max_searches
    assert book.get("serpapi/google").per_call_usd == 0.0 and book.get("binding-gate/fetch@1").call_cost(10**6, 10**6) == 0.0


def test_the_dev_live_dry_run_works_offline_without_any_key_and_caps_cover_the_high_estimate(tmp_path, monkeypatch):
    clear_sessions()
    for var in ("SERPAPI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    web, serp = lf.install(monkeypatch, key=None)
    hard = [fx.row(i) for i in range(60)]
    options = fx.write(tmp_path / "sealqa", monkeypatch, hard, [dict(r) for r in hard[:20]])
    raw = load_config(CONFIGS / "dev_live.toml").to_dict()
    raw["dataset"]["options"] = options
    report = runner.dry_run(parse_config(raw), repo_root=REPO_ROOT)
    assert serp.urls == [] and web.requests == []
    for model in report.models:
        assert report.auxiliary[model.label]  # searches, fetches, re-answers, decomposer
        high = report.model_total(model.label, "cost_high")
        assert high is not None and high < model.cap  # 60 synthetic questions, well below the real size
    clear_sessions()
