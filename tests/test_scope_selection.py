"""PR 18: the pre-registered binding-scope selection rule (PREREGISTRATION.md section 5.1) and its plumbing.

The rule itself (``calibration.choose_scope``) on hand-made candidates; the whole path (dev run of a scope config
-> analysis -> chosen setting) on the synthetic benchmark with the scripted fake models; the dev pipeline's
analysis, the MCP server's acceptance of the new options, and the fake-model dev tool on the fixtures.
"""

from __future__ import annotations

import json
from fractions import Fraction as F
from pathlib import Path

import pytest

import audit_helpers as ah
import dg_fixture
import scope_effect
from binding_gate import pipeline
from binding_gate.canonical import sha256_json
from binding_gate.config import load_config
from binding_gate.gates import calibration
from binding_gate.gates.binding import BINDING_SCOPES
from binding_gate.gates.calibration import CalibrationError, ScopeCandidate, choose_scope

REPO_ROOT = Path(__file__).resolve().parents[1]


def cand(setting, m1, m2):
    models = [f"m{i}" for i in range(len(m1))]
    return ScopeCandidate(setting, setting, dict(zip(models, m1)), dict(zip(models, m2)))


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #


def test_the_feasible_candidate_with_the_lowest_mean_m1_is_chosen():
    candidates = [
        cand("window", [F(1, 100), F(1, 100)], [F(30, 100), F(20, 100)]),  # lowest M1 but infeasible
        cand("subject", [F(4, 100), F(2, 100)], [F(5, 100), F(8, 100)]),   # feasible, M1 mean 0.03
        cand("table", [F(2, 100), F(2, 100)], [F(7, 100), F(9, 100)]),     # one model above 0.08: infeasible
        cand("subject_table", [F(3, 100), F(3, 100)], [F(1, 100), F(2, 100)]),  # feasible, M1 mean 0.03 (tie)
    ]
    assert choose_scope(candidates) == ("subject", "feasible")  # tie on M1 mean: fewer enabled options


def test_exactly_0_08_is_feasible_and_ties_fall_back_to_the_canonical_order():
    candidates = [cand(s, [F(1, 10)], [F(8, 100)]) for s in ("table", "subject")]  # 1 and 2 enabled options
    assert choose_scope(candidates) == ("table", "feasible")
    same = [cand("subject", [F(1, 10)], [F(8, 100)]), cand("window", [F(1, 10)], [F(8, 100)])]
    assert choose_scope(same) == ("window", "feasible")


def test_without_a_feasible_candidate_the_lowest_m2_max_is_chosen_and_h2_is_at_risk():
    candidates = [cand("window", [F(1, 100)], [F(30, 100)]), cand("subject", [F(5, 100)], [F(12, 100)]),
                  cand("table", [F(1, 100)], [None]), cand("subject_table", [F(9, 100)], [F(12, 100)])]
    assert choose_scope(candidates) == ("subject", "none_feasible")
    with pytest.raises(CalibrationError, match="no candidate"):
        choose_scope([cand("window", [F(0)], [None])])


def test_undefined_m1_makes_a_candidate_infeasible():
    candidates = [cand("window", [None], [F(1, 100)]), cand("subject", [F(5, 100)], [F(5, 100)])]
    assert choose_scope(candidates) == ("subject", "feasible")


def test_scope_arms_of_the_committed_configs_and_refusals():
    for name in ("dev_gated_scope.toml", "dev_sec_gated_scope.toml"):
        arms = calibration.scope_arms(load_config(REPO_ROOT / "configs" / name))
        assert list(arms) == list(BINDING_SCOPES)
        assert arms == {"window": "gated", "subject": "gated_scope_subject", "table": "gated_scope_table",
                        "subject_table": "gated_scope_subject_table"}
    with pytest.raises(CalibrationError, match="lacks the candidate"):
        calibration.scope_arms(load_config(REPO_ROOT / "configs" / "dev_gated.toml"))


# --------------------------------------------------------------------------- #
# The whole path on synthetic runs
# --------------------------------------------------------------------------- #


def scope_arm(setting: str, label: str | None = None, **extra) -> dict:
    arm = ah.gated_arm(label or ("gated" if setting == "window" else f"gated_scope_{setting}"))
    arm["options"]["parameters"] = {"binding_scope": setting, **extra}
    return arm


MODELS = [  # two models that both answer from the target document, so that M2 is defined for each
    {"name": "scripted_fake", "label": "good", "options": {"model_id": "fake-cheap", "behaviour": "target"}},
    {"name": "scripted_fake", "label": "good2", "options": {"model_id": "fake-cheap", "behaviour": "target"}},
]


def scope_run(tmp_path: Path, arms=None, models=MODELS):
    arms = arms or [{"name": "no_gate"}, *(scope_arm(s) for s in BINDING_SCOPES)]
    _repo, summary = ah.gated_run(tmp_path, arms=arms, repeats=1, name="dev-gated-scope", models=models)
    return summary.run_dir


def test_selection_from_a_dev_run_of_a_scope_config(tmp_path, capsys):
    run_dir = scope_run(tmp_path)
    selection = calibration.select_scope([run_dir])
    assert selection.layer == 1 and [c.setting for c in selection.candidates] == list(BINDING_SCOPES)
    # both scripted models quote the target document: no wrong-entity answer, and on the synthetic checkout the
    # gate blocks no correct one in any setting
    assert all(c.m1_mean == 0 and c.m2_max == 0 for c in selection.candidates)
    assert set(selection.candidates[0].m2) == {"good", "good2"}
    assert selection.chosen == "window" and selection.case == "feasible"  # tie: fewest enabled options
    out = tmp_path / "sel.json"
    assert calibration.main(["--select-scope", str(run_dir), "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert 'CHOSEN: binding_scope = "window"' in printed and "configs/test_layer1.toml" in printed
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["freeze_line"] == 'binding_scope = "window"' and data["m2_max_threshold"] == "2/25"


def test_a_model_without_correct_baseline_answers_leaves_m2_undefined_and_nothing_is_chosen(tmp_path):
    run_dir = scope_run(tmp_path, models=[{**MODELS[0]}, {"name": "scripted_fake", "label": "bad",
                                                          "options": {"model_id": "fake-cheap", "behaviour": "competitor"}}])
    with pytest.raises(CalibrationError, match="no candidate has a defined M2"):
        calibration.select_scope([run_dir])


def test_selection_refuses_a_run_without_every_candidate_or_with_other_differences(tmp_path, capsys):
    incomplete = scope_run(tmp_path / "a", [{"name": "no_gate"}, scope_arm("window"), scope_arm("subject")])
    assert calibration.main(["--select-scope", str(incomplete)]) == 1
    assert "lacks the candidate" in capsys.readouterr().err
    different = scope_run(tmp_path / "b", [{"name": "no_gate"}, scope_arm("window"), scope_arm("subject", window_tokens=10),
                                           scope_arm("table"), scope_arm("subject_table")])
    with pytest.raises(CalibrationError, match="more than binding_scope"):
        calibration.select_scope([different])


def test_selection_refuses_test_runs(tmp_path):
    run_dir = scope_run(tmp_path)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["config"]["split"] = "test"
    manifest["config_hash"] = sha256_json(manifest["config"])
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CalibrationError, match="dev runs only"):
        calibration.select_scope([run_dir])


def test_analyze_dev_runs_the_selection_for_scope_configs(tmp_path):
    run_dir = scope_run(tmp_path)
    index = pipeline.analyze_dev(Path(run_dir).parent, tmp_path / "analysis")
    entry = index["scope_selection"]["dev-gated-scope"]
    assert entry["exit_code"] == 0 and index["failed"] == []
    assert json.loads(Path(entry["json"]).read_text(encoding="utf-8"))["chosen"] == "window"
    assert "CHOSEN" in (tmp_path / "analysis" / "scope_selection_dev-gated-scope.txt").read_text(encoding="utf-8")


def test_calibration_grid_varies_the_new_options(tmp_path):
    _repo, summary = ah.gated_run(tmp_path, repeats=1)
    report = calibration.calibrate([summary.run_dir], values={"entity_scope": ["window", "window_or_document_subject"],
                                                              "table_binding": [False, True]})
    assert len(report.rows) == 4 and sum(r.is_configured for r in report.rows) == 1
    assert next(r for r in report.rows if r.is_configured).changed_vs_recorded == 0


# --------------------------------------------------------------------------- #
# MCP server
# --------------------------------------------------------------------------- #


def test_mcp_accepts_the_new_options_with_the_same_defaults():
    from binding_gate.mcp import core, schemas

    props = schemas.parameter_schema()["properties"]
    assert props["table_binding"] == {**props["table_binding"], "type": "boolean", "default": False}
    assert props["binding_scope"]["enum"] == list(BINDING_SCOPES) and props["binding_scope"]["default"] == "window"
    assert props["entity_scope"]["default"] == props["qualifier_scope"]["default"] == "window"
    assert core.parse_parameters({"binding_scope": "subject_table"}) == {"binding_scope": "subject_table"}
    for bad, fragment in (({"table_binding": 1}, "true or false"), ({"entity_scope": "page"}, "must be one of"),
                          ({"binding_scope": "table", "table_binding": False}, "contradicts")):
        with pytest.raises(core.ToolError) as info:
            core.parse_parameters(bad)
        assert fragment in str(info.value)


# --------------------------------------------------------------------------- #
# The fake-model dev tool
# --------------------------------------------------------------------------- #


def test_scope_effect_tool_on_the_fixtures(tmp_path):
    options = dg_fixture.write_checkout(tmp_path / "dg")
    result = scope_effect.measure(options, tmp_path / "sec")
    layer1 = result["layer1_deceptive_grounding_dev"]
    assert {g for g in layer1} == {"target-quoted (correct)", "competitor-quoted (wrong entity)"}
    layer2 = result["layer2_sec_fixture"]

    def share(group, setting):
        return layer2[group]["settings"][setting]["share"]

    correct, prior, peer = ("target-quoted, current year (correct)", "target-quoted, prior year (wrong period)",
                            "competitor-quoted (wrong entity)")
    assert share(correct, "subject") < share(correct, "window") and share(correct, "subject_table") == 0
    assert share(prior, "table") == share(prior, "subject_table") == 1.0
    assert all(share(peer, s) == 1.0 for s in BINDING_SCOPES)
    assert "indicative only" in scope_effect.markdown(result)
