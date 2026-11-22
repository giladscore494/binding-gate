"""Reading run directories, output files, the Markdown report and the optional figures."""

from __future__ import annotations

import json

import pytest

import run_helpers as h
import stats_helpers as sh
from binding_gate import runner
from binding_gate.stats.analysis import analyse, analyse_run_dirs, load_trace_set, write_outputs
from binding_gate.stats.bootstrap import StatsError
from binding_gate.stats.report import render_markdown
from binding_gate.trace import TRACES_NAME


def dev_run(tmp_path, **overrides):
    repo, config, _raw = h.setup(tmp_path, **overrides)
    summary = runner.execute_run(
        config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None
    )
    return summary.run_dir


def test_dev_run_directory_is_analysed_but_never_confirmatory(tmp_path):
    run_dir = dev_run(tmp_path, arms=[{"name": "no_gate"}, {"name": "no_gate", "label": "gated"}], repeats=3)
    result = analyse_run_dirs([run_dir], resamples=200)
    assert result["split"] == "dev" and result["inputs"]["runs"][0]["finished"]
    assert result["models"]["bad"]["n_groups"] == 3
    # identical arms: no reduction, the paired difference is exactly zero
    assert result["models"]["bad"]["estimates"]["cmp/gated/no_gate/diff_m1"]["value"] == 0.0
    assert result["models"]["bad"]["hypotheses"]["H1"]["status"] == "NOT SUPPORTED"
    assert not result["models"]["bad"]["hypotheses"]["H1"]["confirmatory"]


def test_a_changed_trace_file_is_detected(tmp_path):
    run_dir = dev_run(tmp_path, repeats=1)
    path = run_dir / TRACES_NAME
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(lines[:-1]), encoding="utf-8")
    with pytest.raises(StatsError, match="SHA-256 differs"):
        load_trace_set([run_dir])
    with pytest.raises(StatsError):
        load_trace_set([])


def test_outputs_and_report(tmp_path):
    records = sh.grid(
        {"no_gate": ["wrong_entity", "target_grounded"] * 6, "gated": ["abstention", "target_grounded"] * 6},
        items_per_group=2,
    )
    result = analyse(records, resamples=100)
    written = write_outputs(result, tmp_path / "out", figures=False)
    assert json.loads((tmp_path / "out" / "analysis.json").read_text(encoding="utf-8"))["models"]["m"]["n_groups"] == 6
    report = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert written["figures"] == [] and "figures not requested" in report
    for expected in ("M1 wrong among answered", "M2 correct answers blocked", "| H1 |", "Outcome table",
                     "By condition", "Spread across repeats", "not confirmatory", "Pooled over models"):
        assert expected in report
    assert render_markdown(result) == render_markdown(json.loads(json.dumps(result)))  # JSON round trip


def test_figures_are_written_when_matplotlib_is_installed(tmp_path):
    pytest.importorskip("matplotlib")
    records = []
    for k in range(6):
        stratum = ["absent/null_control", "complete/synthetic_y", "partial/class_proximate"][k % 3]
        records.append(sh.record("wrong_entity", arm="no_gate", item=f"i{k}", group=f"g{k}", stratum=stratum))
        records.append(sh.record("abstention", arm="gated", item=f"i{k}", group=f"g{k}", stratum=stratum))
    written = write_outputs(analyse(records, resamples=50), tmp_path / "out")
    names = sorted(p.rsplit("/", 1)[1] for p in written["figures"])
    assert names == ["conditions_m.png", "primary_pair.png"]
    for path in written["figures"]:
        with open(path, "rb") as handle:
            assert handle.read(8) == b"\x89PNG\r\n\x1a\n"


def test_without_matplotlib_the_report_says_figures_were_skipped(tmp_path, monkeypatch):
    from binding_gate.plots import figures

    monkeypatch.setattr(figures, "available", lambda: False)
    records = sh.grid({"no_gate": ["wrong_entity", "target_grounded"]})
    written = write_outputs(analyse(records, resamples=20), tmp_path / "out")
    assert written["figures"] == [] and "matplotlib is not installed" in written["figure_note"]
    assert "matplotlib is not installed" in (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
