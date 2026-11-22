"""Dev-only gate calibration from traces: reproduces recorded verdicts, varies parameters, refuses test data."""

from __future__ import annotations

import json

import pytest

import fakes
import gate_fakes as g
import run_helpers as h
from binding_gate import runner, testlock
from binding_gate.arms.gated import clear_decomposition_memo
from binding_gate.canonical import sha256_json
from binding_gate.gates import calibration
from binding_gate.gates.binding import ALL_CHECKS
from binding_gate.trace import MANIFEST_NAME, TRACES_NAME


@pytest.fixture(autouse=True)
def _fresh_memo():
    clear_decomposition_memo()
    g.DECOMPOSE_CALLS.clear()
    yield
    clear_decomposition_memo()


@pytest.fixture
def dev_run(tmp_path):
    arms = [
        {"name": "no_gate"},
        {"name": "gated", "options": {"checks": list(ALL_CHECKS), "decomposer": {"name": "fake_decomposer"}}},
    ]
    repo, config, _raw = h.setup(tmp_path, arms=arms, repeats=1)
    summary = runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None)
    return summary.run_dir


def test_calibration_reproduces_recorded_verdicts_and_shows_what_a_parameter_changes(dev_run, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("calibration must never touch the test lock")

    monkeypatch.setattr(testlock, "load_test_split", forbidden)
    calls_before, decompositions_before = len(fakes.CALLS), len(g.DECOMPOSE_CALLS)
    report = calibration.calibrate([dev_run], values={"window_tokens": [3, 40], "competitor_tie": ["fail", "pass"]})
    assert len(fakes.CALLS) == calls_before  # nothing is called again
    assert len(g.DECOMPOSE_CALLS) == decompositions_before
    assert report.checks == list(ALL_CHECKS)
    rows = {(r.parameters["window_tokens"], r.parameters["competitor_tie"]): r.to_dict() for r in report.rows}
    assert len(rows) == 4
    configured = rows[(40, "fail")]
    assert configured["is_configured"] and configured["changed_vs_recorded"] == 0
    assert configured["n"] > 0 and configured["wrong_entity_accepted"] == 0
    assert configured["target_grounded_blocked"] == 0
    assert configured["candidates_by_provenance"]["competitor_only"] > 0
    narrow = rows[(3, "fail")]
    # the qualifier is 4+ tokens after the fake model's target quote: a 3-token window blocks it
    assert narrow["target_grounded_blocked"] > 0 and narrow["changed_vs_recorded"] > 0
    assert narrow["check_failures"]["qualifiers_in_window"] >= narrow["target_grounded_blocked"]
    assert report.skipped["abstained"] > 0  # absent-condition abstentions are never gated
    text = report.format()
    assert "dev split only" in text and "*" in text


def test_calibration_cli_writes_json(dev_run, tmp_path, capsys):
    out = tmp_path / "calibration.json"
    assert calibration.main([str(dev_run), "--window-tokens", "10,40", "--json", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["kind"] == "gate_calibration" and data["split"] == "dev"
    assert [row["parameters"]["window_tokens"] for row in data["rows"]] == [10, 40]
    assert "GATE CALIBRATION" in capsys.readouterr().out


def test_calibration_refuses_a_run_whose_manifest_is_not_dev(dev_run, capsys):
    path = dev_run / MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["config"]["split"] = "test"
    manifest["config"]["limit"] = None
    manifest["config_hash"] = sha256_json(manifest["config"])
    path.write_text(json.dumps(manifest), encoding="utf-8")
    (dev_run / TRACES_NAME).write_text("this would not even parse\n", encoding="utf-8")  # never opened
    with pytest.raises(calibration.CalibrationError, match="refused"):
        calibration.calibrate([dev_run])
    assert calibration.main([str(dev_run)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "refused" in captured.err


def test_calibration_refuses_any_test_record_before_computing_anything(dev_run, capsys):
    path = dev_run / TRACES_NAME
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[-1] = lines[-1].replace('"split":"dev"', '"split":"test"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(calibration.CalibrationError, match="never reads test traces"):
        calibration.calibrate([dev_run])
    assert calibration.main([str(dev_run)]) == 1
    assert capsys.readouterr().out == ""


def test_calibration_rejects_an_unknown_arm_label(dev_run):
    with pytest.raises(calibration.CalibrationError, match="no arm labelled"):
        calibration.calibrate([dev_run], arm="gated-minus-x")


def test_parameter_grid_is_the_cartesian_product_in_a_fixed_order():
    configured = {name: 0 for name in calibration.GRID_PARAMETERS}
    grid = calibration.parameter_grid(configured, {"window_tokens": [1, 2], "lead_min_repeats": [3, 4]})
    assert [(p["window_tokens"], p["lead_min_repeats"]) for p in grid] == [(1, 3), (1, 4), (2, 3), (2, 4)]
    assert all(p["min_quote_tokens"] == 0 for p in grid)
