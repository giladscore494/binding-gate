"""PR 12 (pre-registration section 17, item 38): configs/test_layer1.toml runs the pre-registered arms.

Every arm of the frozen layer-1 test config must have exactly the options of its
dev counterpart, so that what is frozen is what was run (and calibrated) on dev.
The only allowed difference is a split-dependent field (the judge's budget scales
with the number of items). Labels must be the ones the analysis reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import dg_fixture
import run_helpers as h
from binding_gate import runner
from binding_gate.arms.llm_judge_checks import CHECK_NAMES
from binding_gate.config import load_config, parse_config
from binding_gate.contracts import Split
from binding_gate.stats import prereg

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "configs"
TEST_CONFIG = load_config(CONFIGS / "test_layer1.toml")

SPLIT_DEPENDENT = {"llm_judge_checks": {"judge_max_usd"}}
"""Options that may differ between the dev counterpart and the test config, per arm name."""


def dev_counterpart(label: str):
    """The dev config and arm whose options the test arm must reproduce."""
    if label == "no_gate":
        path = "dev_baseline.toml"
    elif label in ("prompt_entity_check", "faithfulness_checker"):
        path = "dev_baselines.toml"
    elif label == "gated":
        path = "dev_gated.toml"
    elif label.startswith("gated_minus_"):
        path = f"dev_ablation_{label.removeprefix('gated_minus_')}.toml"
    else:  # pragma: no cover - a new arm needs a counterpart here
        raise AssertionError(f"no dev counterpart for test arm {label!r}")
    config = load_config(CONFIGS / path)
    (arm,) = [a for a in config.arms if a.label == label]
    return config, arm


@pytest.mark.parametrize("arm", TEST_CONFIG.arms, ids=lambda a: a.label)
def test_every_test_arm_has_the_options_of_its_dev_counterpart(arm):
    _config, dev_arm = dev_counterpart(arm.label)
    assert arm.name == dev_arm.name
    allowed = SPLIT_DEPENDENT.get(arm.name, set())

    def strip(options):
        return {k: v for k, v in options.items() if k not in allowed}

    assert strip(arm.options) == strip(dev_arm.options)
    assert set(arm.options) == set(dev_arm.options), "a split-dependent option may change, not disappear"


def test_labels_are_the_ones_the_analysis_and_the_dev_configs_use():
    labels = [a.label for a in TEST_CONFIG.arms]
    for required in (prereg.REFERENCE_ARM, prereg.GATED_ARM, prereg.FAITHFULNESS_ARM):
        assert required in labels
    # PR 13: llm_judge_checks runs on a dev subsample only (configs/dev_llm_judge_checks.toml)
    assert labels == [
        "no_gate", "prompt_entity_check", "faithfulness_checker", "gated",
        *(f"gated_minus_{check}" for check in CHECK_NAMES),
    ]
    assert not any("-" in label for label in labels), "one naming scheme: underscores, as in the dev configs"
    dev_ablation_labels = sorted(
        load_config(path).arms[2].label for path in CONFIGS.glob("dev_ablation_*.toml")
    )
    assert sorted(label for label in labels if label.startswith("gated_minus_")) == dev_ablation_labels


def test_models_dataset_scorers_and_repeats_are_those_of_the_dev_configs():
    for path in ("dev_baseline.toml", "dev_baselines.toml", "dev_gated.toml"):
        dev = load_config(CONFIGS / path)
        assert TEST_CONFIG.models == dev.models, path
        assert TEST_CONFIG.dataset == dev.dataset and TEST_CONFIG.scorers == dev.scorers
        assert TEST_CONFIG.repeats == dev.repeats == prereg.REPEATS
    assert TEST_CONFIG.split is Split.TEST and TEST_CONFIG.limit is None
    assert "TODO" not in (CONFIGS / "test_layer1.toml").read_text(encoding="utf-8")


def test_the_test_run_has_no_judge_and_shares_one_answer_among_all_gate_arms():
    """PR 13: every arm except prompt_entity_check sends the no_gate answer request (one paid answer)."""
    assert "llm_judge_checks" not in {a.name for a in TEST_CONFIG.arms}
    sharing = [a.label for a in TEST_CONFIG.arms if a.name in ("no_gate", "gated", "faithfulness_checker")]
    assert len(sharing) == len(TEST_CONFIG.arms) - 1


def test_caps_cover_every_model_and_exceed_the_dev_caps_of_the_same_arms():
    settings = runner.RunnerSettings.from_config(TEST_CONFIG)
    dev = runner.RunnerSettings.from_config(load_config(CONFIGS / "dev_baselines.toml"))
    for model in TEST_CONFIG.models:
        assert settings.cap_for(model.label) > dev.cap_for(model.label)


def test_the_same_arms_dry_run_on_dev_without_any_key(tmp_path, monkeypatch):
    """Section 17 item 38: the test config's arms run on dev (synthetic checkout, recording client)."""
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "MOONSHOT_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    repo = h.make_repo(tmp_path)
    (repo / "configs" / "prices.toml").write_text((CONFIGS / "prices.toml").read_text(), encoding="utf-8")
    raw = TEST_CONFIG.to_dict()
    raw["split"] = "dev"
    raw["dataset"]["options"] = dg_fixture.write_checkout(tmp_path / "dg")
    report = runner.dry_run(parse_config(raw), repo_root=repo)
    assert report.split == "dev" and report.arms == tuple(a.label for a in TEST_CONFIG.arms)
    assert report.n_items == 45 and report.requests_per_repeat == 45 * 2  # no_gate (shared) + prompt_entity_check
    roles = {a.role for rows in report.auxiliary.values() for a in rows}
    assert roles == {"decomposer"} and not report.auxiliary_problems


# --------------------------------------------------------------------------- #
# PR 13: the exploratory mid-tier test run
# --------------------------------------------------------------------------- #

MIDTIER = load_config(CONFIGS / "test_layer1_midtier.toml")


@pytest.mark.parametrize("arm", MIDTIER.arms, ids=lambda a: a.label)
def test_every_midtier_arm_has_the_options_of_its_dev_counterpart(arm):
    _config, dev_arm = dev_counterpart(arm.label)
    assert (arm.name, arm.options) == (dev_arm.name, dev_arm.options)


def test_midtier_run_is_a_subsample_of_the_test_split_with_the_smoke_runs_mid_tier_models():
    assert MIDTIER.split is Split.TEST and MIDTIER.repeats == prereg.REPEATS and MIDTIER.limit is None
    assert MIDTIER.sample == {"per_stratum": 15, "seed": 20261122}
    smoke = {m.label: m for m in load_config(CONFIGS / "dev_smoke.toml").models}
    assert [m.label for m in MIDTIER.models] == ["openai-mid", "anthropic-mid", "google-mid"]
    assert all(m == smoke[m.label] for m in MIDTIER.models)
    assert MIDTIER.dataset == TEST_CONFIG.dataset and MIDTIER.scorers == TEST_CONFIG.scorers
    assert [a.label for a in MIDTIER.arms] == ["no_gate", "faithfulness_checker", "gated"]
