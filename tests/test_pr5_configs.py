"""The PR 5 configs: baselines, per-check ablations, the re-scoring config."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import dg_fixture
import run_helpers as h
from binding_gate import judging, registry, runner
from binding_gate.arms.llm_judge_checks import CHECK_NAMES
from binding_gate.config import load_config, parse_config
from binding_gate.pricing import load_prices
from binding_gate.scorers.llm_judge import RescoreConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "configs"
ABLATIONS = sorted(CONFIGS.glob("dev_ablation_*.toml"))
FAMILIES_UNDER_TEST = {"openai", "anthropic", "google"}


def baseline() -> dict:
    return load_config(CONFIGS / "dev_baseline.toml").to_dict()


def with_fixture_dataset(config, tmp_path: Path):
    """The same config, but on the synthetic checkout (the real benchmark is not in the repository)."""
    raw = config.to_dict()
    raw["dataset"]["options"] = dg_fixture.write_checkout(tmp_path / "dg")
    return parse_config(raw)


# -- dev_baselines.toml --------------------------------------------------------------- #


def test_baselines_config_runs_the_three_comparison_arms_next_to_no_gate():
    config = load_config(CONFIGS / "dev_baselines.toml")
    assert [(a.name, a.label) for a in config.arms] == [
        ("no_gate", "no_gate"),
        ("prompt_entity_check", "prompt_entity_check"),
        ("faithfulness_checker", "faithfulness_checker"),
        ("llm_judge_checks", "llm_judge_checks"),
    ]
    reference = baseline()
    data = config.to_dict()
    for key in ("dataset", "models", "scorers", "split", "repeats"):
        assert data[key] == reference[key]
    options = {a.name: a.options for a in config.arms}
    assert options["faithfulness_checker"] == {
        "presentation": "deceptive_grounding_doc_injection", "backend": "minicheck", "evidence": "all_documents"
    }
    assert options["llm_judge_checks"]["checks"] == list(CHECK_NAMES)
    settings = runner.RunnerSettings.from_config(config)
    assert all(settings.cap_for(m.label) is not None for m in config.models)


def test_baselines_judge_is_slot_j_priced_and_of_another_family_than_every_model(monkeypatch):
    config = load_config(CONFIGS / "dev_baselines.toml")
    options = {a.name: a.options for a in config.arms}["llm_judge_checks"]
    (spec,) = judging.parse_judges("judges", options["judges"])
    client = spec.build()
    assert (spec.name, client.model_id, judging.model_family(client)) == ("moonshot_chat", "kimi-k3.1-pro", "moonshot")
    assert load_prices(REPO_ROOT / options["prices"]).has("kimi-k3.1-pro")
    assert options["judge_max_usd"] > 0
    families = {judging.model_family(runner.build_model(m)) for m in config.models}
    assert families == FAMILIES_UNDER_TEST
    monkeypatch.setenv("MOONSHOT_API_KEY", "placeholder-for-construction-only")
    for model in config.models:  # the arm accepts every model under test (no family clash)
        registry.create("arm", "llm_judge_checks", model=runner.build_model(model), **options)
    monkeypatch.delenv("MOONSHOT_API_KEY")
    with pytest.raises(judging.JudgeUnavailable, match="MOONSHOT_API_KEY"):  # fails before any paid call
        registry.create("arm", "llm_judge_checks", model=runner.build_model(config.models[0]), **options)


def test_baselines_dry_run_works_without_any_key_or_heavy_dependency(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "MOONSHOT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    repo = h.make_repo(tmp_path)
    (repo / "configs" / "prices.toml").write_text((CONFIGS / "prices.toml").read_text(), encoding="utf-8")
    config = with_fixture_dataset(load_config(CONFIGS / "dev_baselines.toml"), tmp_path)
    report = runner.dry_run(config, repo_root=repo)
    assert report.n_items == 45 and report.requests_per_repeat == 45 * 2
    assert len(report.models) == 6 and all(m.calls == 45 * 2 * 3 for m in report.models)


# -- dev_ablation_*.toml -------------------------------------------------------------- #


def test_one_ablation_config_per_reserved_check():
    assert [p.name for p in ABLATIONS] == sorted(f"dev_ablation_{name}.toml" for name in CHECK_NAMES)


@pytest.mark.parametrize("path", ABLATIONS, ids=lambda p: p.stem)
def test_each_ablation_is_the_full_gate_plus_the_gate_minus_one_check(path):
    removed = path.stem.removeprefix("dev_ablation_")
    config = load_config(path)
    no_gate, full, ablated = config.arms
    assert (no_gate.name, full.name, ablated.name) == ("no_gate", "gated", "gated")
    assert (full.label, ablated.label) == ("gated", f"gated_minus_{removed}")
    assert full.options["checks"] == list(CHECK_NAMES)
    assert ablated.options["checks"] == [c for c in CHECK_NAMES if c != removed]
    assert {k: v for k, v in full.options.items() if k != "checks"} == {
        k: v for k, v in ablated.options.items() if k != "checks"
    }
    reference = baseline()
    data = config.to_dict()
    for key in ("dataset", "models", "scorers", "split", "repeats", "runner"):
        assert data[key] == reference[key], key


def test_ablation_configs_differ_only_in_the_removed_check():
    def strip(path):
        data = load_config(path).to_dict()
        data.pop("name"), data.pop("description")
        data["arms"][2]["label"] = data["arms"][2]["options"]["checks"] = None
        return data

    first = strip(ABLATIONS[0])
    assert all(strip(p) == first for p in ABLATIONS[1:])


def test_ablations_match_the_full_gated_config_when_pr4_has_provided_one():
    """After the PR 4 merge: every option of the full gated arm (other than checks) must be copied."""
    full_configs = []
    for path in sorted(CONFIGS.glob("*.toml")):
        if path.name == "prices.toml" or path in ABLATIONS:
            continue
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        for arm in raw.get("arms", []):
            if arm.get("name") == "gated" and arm.get("options", {}).get("checks", list(CHECK_NAMES)) == list(CHECK_NAMES):
                full_configs.append((path, arm.get("options", {})))
    if not full_configs:
        pytest.skip("no full gated config in configs/ yet (PR 4 is developed in parallel)")
    path, options = full_configs[0]
    reference = {k: v for k, v in options.items() if k != "checks"}
    for ablation in ABLATIONS:
        for arm in load_config(ablation).arms[1:]:
            assert {k: v for k, v in arm.options.items() if k != "checks"} == reference, (ablation.name, path.name)


@pytest.mark.parametrize("path", ABLATIONS, ids=lambda p: p.stem)
def test_ablation_dry_run(path, tmp_path):
    if "gated" not in registry.available("arm"):
        pytest.skip("the gated arm (PR 4, developed in parallel) is not registered in this tree yet")
    repo = h.make_repo(tmp_path)
    (repo / "configs" / "prices.toml").write_text((CONFIGS / "prices.toml").read_text(), encoding="utf-8")
    report = runner.dry_run(with_fixture_dataset(load_config(path), tmp_path), repo_root=repo)
    assert report.arms == ("no_gate", "gated", f"gated_minus_{path.stem.removeprefix('dev_ablation_')}")


# -- configs/rescore/dev_llm_judge.toml ----------------------------------------------- #


def test_rescore_config_uses_slot_j_with_the_benchmarks_settings():
    config = RescoreConfig.load(CONFIGS / "rescore" / "dev_llm_judge.toml")
    (spec,) = config.judges
    client = spec.build()
    assert (client.model_id, judging.model_family(client)) == ("kimi-k3.1-pro", "moonshot")
    assert client.max_output_tokens == 12288 and client.temperature == 0.0  # as benchmark_judge.py
    assert load_prices(REPO_ROOT / config.prices).has(client.model_id) and config.max_usd > 0


def test_no_key_or_machine_path_in_the_rescore_config():
    text = (CONFIGS / "rescore" / "dev_llm_judge.toml").read_text(encoding="utf-8")
    assert "/home/" not in text and "sk-" not in text and "api_key" not in text.lower()
