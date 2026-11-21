"""The command line, end to end on the synthetic checkout. No network, no API key."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import fakes
import run_helpers as h
from binding_gate import cli
from binding_gate.testlock import ACCESS_LOG_RELPATH

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_MODEL = {
    "name": "openai_responses",
    "label": "openai-low",
    "options": {"model_id": "fake-cheap", "max_output_tokens": 4096},
}


@pytest.fixture
def project(tmp_path):
    repo, _config, raw = h.setup(tmp_path)
    config_path = repo / "configs" / "run.toml"
    config_path.write_text(h.toml_text(raw), encoding="utf-8")
    return repo, config_path, raw


def call(*argv) -> int:
    return cli.main([str(a) for a in argv])


def test_dry_run_end_to_end_in_a_fresh_process_without_any_key(project, tmp_path):
    """Integration: `python -m binding_gate.cli dry-run` on the fixture, real provider client configured."""
    repo, _config_path, raw = project
    config_path = repo / "configs" / "real_client.toml"
    config_path.write_text(h.toml_text({**raw, "models": [REAL_MODEL]}), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    out = tmp_path / "estimate.json"
    result = subprocess.run(
        [sys.executable, "-m", "binding_gate.cli", "dry-run", "--config", str(config_path), "--repo-root", str(repo), "--json", str(out)],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "DRY RUN - no model API was called" in result.stdout
    assert "3 groups, 45 items" in result.stdout
    estimate = json.loads(out.read_text(encoding="utf-8"))
    (model,) = estimate["models"]
    assert (model["label"], model["calls"], model["secret"]) == ("openai-low", 90, "OPENAI_API_KEY")
    assert model["cost_low"] > 0 and model["cost_high"] > model["cost_low"]
    assert not (tmp_path / "runs").exists() and not (repo / "runs").exists()
    assert not (repo / ACCESS_LOG_RELPATH).exists()


def test_run_then_report(project, tmp_path, capsys):
    repo, config_path, _raw = project
    runs = tmp_path / "runs"
    assert call("run", "--config", config_path, "--repo-root", repo, "--runs-dir", runs) == cli.EXIT_FAILURE
    assert "REFUSED" in capsys.readouterr().err and not runs.exists() and fakes.CALLS == []

    assert call("run", "--config", config_path, "--repo-root", repo, "--runs-dir", runs, "--confirm-spend") == cli.EXIT_OK
    summary = json.loads(capsys.readouterr().out.split("\n", 2)[2])  # after the two preflight lines
    assert summary["closed"] and summary["records_written"] == 45 * 2 * 2
    (run_dir,) = runs.iterdir()
    assert call("report", run_dir, "--json", tmp_path / "report.json") == cli.EXIT_OK
    output = capsys.readouterr().out
    assert "OVERALL STOP-RULE OUTCOME: CONTINUE" in output
    assert json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))["overall"] == "CONTINUE"


def test_models_and_dataset_pin_feed_the_workflow(project, capsys):
    repo, config_path, raw = project
    assert call("models", "--config", config_path) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out) == ["good", "bad"]
    assert call("dataset-pin", "--config", config_path) == cli.EXIT_OK
    lines = dict(line.split("=", 1) for line in capsys.readouterr().out.strip().splitlines())
    assert lines == {
        "repository": "https://github.com/CedricCaruzzo/deceptive-grounding-benchmark",
        "commit": raw["dataset"]["options"]["commit"],
        "root": raw["dataset"]["options"]["root"],
    }


def test_missing_api_key_is_a_distinct_exit_code_and_nothing_runs(project, tmp_path, capsys, monkeypatch):
    repo, _config_path, raw = project
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_path = repo / "configs" / "real_client.toml"
    config_path.write_text(h.toml_text({**raw, "models": [REAL_MODEL]}), encoding="utf-8")
    assert call("preflight", "--config", config_path) == cli.EXIT_NO_KEY
    assert "SKIPPED" in capsys.readouterr().out
    assert call("run", "--config", config_path, "--repo-root", repo, "--runs-dir", tmp_path / "runs", "--confirm-spend") == cli.EXIT_NO_KEY
    assert "OPENAI_API_KEY is not set" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()


def test_preflight_command_reports_ok_and_failure(project, capsys):
    repo, config_path, raw = project
    assert call("preflight", "--config", config_path, "--model", "good") == cli.EXIT_OK
    assert "preflight good: OK" in capsys.readouterr().out
    ghost = {"name": "scripted_fake", "label": "ghost", "options": {"model_id": "fake-cheap", "preflight_ok": False}}
    bad_path = repo / "configs" / "ghost.toml"
    bad_path.write_text(h.toml_text({**raw, "models": [ghost]}), encoding="utf-8")
    assert call("preflight", "--config", bad_path) == cli.EXIT_FAILURE
    assert "preflight ghost: FAILED" in capsys.readouterr().out


def test_errors_are_reported_without_a_traceback(project, tmp_path, capsys):
    repo, config_path, _raw = project
    assert call("dry-run", "--config", tmp_path / "missing.toml") == cli.EXIT_FAILURE
    assert call("dry-run", "--config", config_path, "--repo-root", repo, "--model", "nobody") == cli.EXIT_FAILURE
    assert call("report", tmp_path / "no-such-run") == cli.EXIT_FAILURE
    assert capsys.readouterr().err.count("ERROR") == 3
    with pytest.raises(SystemExit):
        call("no-such-command")


def test_budget_abort_has_its_own_exit_code(project, tmp_path, capsys):
    repo, _config_path, raw = project
    dear = {"name": "scripted_fake", "label": "dear", "options": {"model_id": "fake-dear", "output_tokens": 50000, "input_tokens": 0}}
    config_path = repo / "configs" / "dear.toml"
    config_path.write_text(h.toml_text({**raw, "models": [dear], "runner": {"concurrency": 1, "max_usd": 25.0}}), encoding="utf-8")
    assert call("run", "--config", config_path, "--repo-root", repo, "--runs-dir", tmp_path / "runs", "--confirm-spend") == cli.EXIT_BUDGET
    assert "ABORTED: budget cap reached" in capsys.readouterr().err
