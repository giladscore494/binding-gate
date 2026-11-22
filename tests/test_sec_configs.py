"""PR 17: the layer-2 run configs (SEC financial filings) agree with each other and with the layer-1 design."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from binding_gate import runner
from binding_gate.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "configs"
SEC = ["dev_sec_smoke.toml", "dev_sec_baseline.toml", "dev_sec_gated.toml", "test_sec.toml"]


@pytest.mark.parametrize("name", SEC)
def test_sec_configs_use_the_primary_models_presentation_scorer_and_one_dataset_pin(name):
    config = load_config(CONFIGS / name)
    baseline = load_config(CONFIGS / "dev_baseline.toml")
    assert [m.to_dict() for m in config.models] == [m.to_dict() for m in baseline.models]  # A, E, H as on layer 1
    assert config.dataset.name == "sec_financials" and config.repeats == 1
    assert config.dataset.options == load_config(CONFIGS / "dev_sec_baseline.toml").dataset.options
    assert config.dataset.options["build_config"] == "configs/datasets/sec_financials_v1.toml"
    assert all(a.options["presentation"] == "sec_filings" for a in config.arms)
    assert [s.name for s in config.scorers] == ["sec_answer"] and config.arms[0].label == "no_gate"
    assert runner.RunnerSettings.from_config(config).prices == "configs/prices.toml"


def test_gate_options_are_those_of_dev_gated_except_the_presentation():
    reference = next(a for a in load_config(CONFIGS / "dev_gated.toml").arms if a.label == "gated").options
    for name in ("dev_sec_gated.toml", "test_sec.toml"):
        for arm in load_config(CONFIGS / name).arms:
            if arm.name != "gated":
                continue
            options = {k: v for k, v in arm.options.items() if k not in ("presentation", "checks")}
            assert options == {k: v for k, v in reference.items() if k not in ("presentation", "checks")}, (name, arm.label)


def test_arm_sets_and_seeding():
    gated = load_config(CONFIGS / "dev_sec_gated.toml")
    labels = [a.label for a in gated.arms]
    assert labels == ["no_gate", "faithfulness_checker", "gated", "gated_minus_quote_verbatim", "gated_minus_entity_in_window",
                      "gated_minus_qualifiers_in_window", "gated_minus_no_closer_competitor", "gated_minus_premise_present"]
    assert runner.RunnerSettings.from_config(gated).seed_responses_from == ("seeds/*",)
    assert [a.label for a in load_config(CONFIGS / "test_sec.toml").arms] == labels
    assert not load_config(CONFIGS / "test_sec.toml").runner.get("seed_responses_from")
    smoke = load_config(CONFIGS / "dev_sec_smoke.toml")
    assert smoke.limit == 2 and [a.label for a in smoke.arms] == ["no_gate"]


def test_the_dataset_is_unpinned_until_the_first_generation_and_configs_hold_no_address():
    for name in SEC:
        text = (CONFIGS / name).read_text(encoding="utf-8")
        assert 'data_sha256 = "UNPINNED"' in text and "@" not in text.replace("@0", ""), name
    assert "@" not in (CONFIGS / "datasets" / "sec_financials_v1.toml").read_text(encoding="utf-8")


def test_grab_rag_configs_moved_to_the_extended_design():
    assert not list(CONFIGS.glob("*grab_rag*.toml"))
    for name in ("dev_grab_rag_supportive.toml", "test_grab_rag_supportive.toml"):
        raw = tomllib.loads((CONFIGS / "extended" / name).read_text(encoding="utf-8"))
        assert raw["name"].startswith("extended-") and raw["dataset"]["name"] == "grab_rag"


def test_requirements_of_a_sec_config(capsys):
    req = runner.run_requirements(load_config(CONFIGS / "dev_sec_gated.toml"))
    assert req["layer"] == 2 and req["private_outputs"] is False and req["baseline_report"] is False
    assert req["faithfulness_checker"] is True and req["extras"] == ["faithfulness"]
    assert set(req["secrets"]) == {"OPENAI_API_KEY", "GEMINI_API_KEY", "ZAI_API_KEY"}  # the run itself needs no SEC secret


def test_preregistration_version_0_8_names_layer_2_and_the_code_agrees():
    from binding_gate.stats import prereg

    text = (REPO_ROOT / "PREREGISTRATION.md").read_text(encoding="utf-8")
    assert "**Document version:** 0.8 (2026-11-22, PR 17)" in text and "**Status:** DRAFT" in text
    assert prereg.LAYER_BY_DATASET == {"deceptive_grounding": 1, "sec_financials": 2, "sealqa": 3}
    assert prereg.HYPOTHESES_BY_LAYER[2] == ("H1", "H2") and "grab_rag" in prereg.EXTENDED_DATASETS
    section3 = text.split("## 3. Datasets and layers", 1)[1].split("## 4.", 1)[0]
    for fact in ("CY2024Q4I", "2025-07-01 and 2026-06-30", "10,000 characters", "`OperatingIncomeLoss`", "6189, 6221"):
        assert fact in section3, fact
    section12 = text.split("## 12. Test-split discipline", 1)[1].split("## 13.", 1)[0]
    assert "configs/test_sec.toml" in section12


def test_the_contact_secret_is_documented_in_both_languages_and_in_secrets_py():
    from binding_gate.secrets import KNOWN_SECRETS

    assert "dataset-prepare" in KNOWN_SECRETS["SEC_CONTACT_EMAIL"]
    running = (REPO_ROOT / "docs" / "RUNNING.md").read_text(encoding="utf-8")
    english, hebrew = running.split("## הוראות הפעלה בעברית", 1)
    assert "| `SEC_CONTACT_EMAIL` |" in english and "| `SEC_CONTACT_EMAIL` |" in hebrew
    assert "`SEC_CONTACT_EMAIL`" in (REPO_ROOT / "README.md").read_text(encoding="utf-8")
