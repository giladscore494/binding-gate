"""PR 15: the dev pipeline helpers (binding_gate.pipeline): stages, smoke usage check, stop-rule gate, check-secrets."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

import run_helpers as h
from binding_gate import pipeline, runner
from binding_gate.ghapi import GitHubAPI
from test_ledger import BUDGET, FakeGitHub, _artifact, _zip

REAL_REPO = Path(__file__).resolve().parents[1]
PLAN = tomllib.loads((REAL_REPO / "configs" / "budget.toml").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #


def test_all_dev_covers_every_dev_run_of_the_budget_plan_once_and_nothing_of_the_test_split():
    dev = [r["config"] for r in PLAN["run"] if r["config"] and not Path(r["config"]).name.startswith("test_")]
    assert sorted(pipeline.STAGES["all-dev"]) == sorted(dev)
    assert len(set(pipeline.STAGES["all-dev"])) == len(pipeline.STAGES["all-dev"])
    assert all(not Path(c).name.startswith("test_") for configs in pipeline.STAGES.values() for c in configs)
    assert pipeline.STAGES["all-dev"] == (pipeline.STAGES["smoke"] + pipeline.STAGES["dev-baseline"] + pipeline.STAGES["dev-rest"]
                                          + pipeline.STAGES["layer2-dev"] + pipeline.STAGES["layer3-dev"])
    for free in pipeline.FREE_STAGES:
        assert pipeline.STAGES[free] == ()
    assert set(pipeline.FREE_STAGES) == {"check-secrets", "sec-dataset-prepare"}
    for configs in pipeline.STAGES.values():
        assert all((REAL_REPO / c).is_file() for c in configs)


def test_dev_rest_runs_in_the_documented_order():
    order = [Path(c).stem for c in pipeline.STAGES["dev-rest"]]
    assert order[:4] == ["decomposer_eval_dev", "dev_repeats", "dev_gated", "dev_gated_scope"]  # PR 18: scope candidates
    assert order[4:9] == [Path(a).stem for a in pipeline.ABLATIONS]
    assert order[9:] == ["dev_baselines", "dev_llm_judge_checks", "dev_llm_judge"]


def test_layer2_dev_runs_smoke_then_baseline_then_the_gated_run_seeded_from_that_baseline():
    """PR 17: the SEC layer replaced the GRAB-RAG run; dev_sec_gated reuses the answers of dev_sec_baseline."""
    from binding_gate.config import load_config

    assert [Path(c).stem for c in pipeline.STAGES["layer2-dev"]] == [
        "dev_sec_smoke", "dev_sec_baseline", "dev_sec_gated", "dev_sec_gated_scope"]  # PR 18: scope candidates
    assert not any("grab_rag" in c for configs in pipeline.STAGES.values() for c in configs)
    seeds = {Path(c).stem: runner.run_requirements(load_config(REAL_REPO / c))["seed_runs"] for c in pipeline.STAGES["layer2-dev"]}
    assert not seeds["dev_sec_smoke"] and not seeds["dev_sec_baseline"] and seeds["dev_sec_gated"]
    assert pipeline.LAYER2_SEED_PATTERN == "layer2-dev--dev_sec_baseline--runs-*"
    for config in pipeline.STAGES["layer2-dev"]:
        assert load_config(REAL_REPO / config).dataset.name == "sec_financials"


def test_sec_prepare_report_prints_the_pin_and_checks_the_manifest(tmp_path, capsys):
    from binding_gate.canonical import sha256_json

    content = {"dataset": "sec_financials", "n_items": 3}
    digest = sha256_json(content)
    prepared = {"content_sha256": digest, "pinned_in_config": "UNPINNED", "matches_pin": False, "n_items": 3,
                "n_companies": 1, "items_by_split": {"dev": 3, "test": 0}, "items_by_stratum": {}, "excluded": 0,
                "retrieval": {"network_requests": 10, "cache_hits": 2, "user_agent_project": "x"}, "next_step": "pin it"}
    (tmp_path / "prepare.json").write_text(json.dumps(prepared), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({"content": content, "content_sha256": digest}), encoding="utf-8")
    argv = ["sec-prepare-report", "--prepare", str(tmp_path / "prepare.json"), "--manifest", str(tmp_path / "manifest.json"),
            "--markdown", str(tmp_path / "p.md"), "--json", str(tmp_path / "p.json")]
    assert pipeline.main(argv) == 0
    out = capsys.readouterr().out
    assert f"content_sha256={digest}" in out and "matches_pin=false" in out
    assert "NOT the pin" in (tmp_path / "p.md").read_text(encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({"content": {"other": 1}, "content_sha256": digest}), encoding="utf-8")
    assert pipeline.main(argv) == 1
    assert "::error::SEC dataset" in capsys.readouterr().out


def test_stage_configs_command(capsys):
    assert pipeline.main(["stage-configs", "--stage", "layer3-dev"]) == 0
    assert capsys.readouterr().out.split() == ["configs/dev_live_smoke.toml", "configs/dev_live.toml"]
    assert pipeline.main(["stage-configs", "--stage", "nonsense"]) == 1


# --------------------------------------------------------------------------- #
# Stop-rule decision and the dev-rest gate
# --------------------------------------------------------------------------- #


def report(overall: str, provisional: bool = False) -> dict:
    return {"overall": overall, "provisional": provisional, "unfinished_runs": [],
            "models": [{"model": "openai-low", "decision": overall, "R": 0.12, "n_records": 1455, "measurement_check": False}]}


def test_decision_file_and_markdown(tmp_path, capsys):
    path = tmp_path / "baseline_report.json"
    path.write_text(json.dumps(report("CONTINUE")), encoding="utf-8")
    assert pipeline.main(["decision", "--report", str(path), "--workflow-run-id", "42", "--json", str(tmp_path / "d.json"),
                          "--markdown", str(tmp_path / "d.md")]) == 0
    assert "decision=CONTINUE" in capsys.readouterr().out
    decision = json.loads((tmp_path / "d.json").read_text(encoding="utf-8"))
    assert decision["workflow_run_id"] == 42 and decision["seed_artifact_pattern"] == "dev-baseline--dev_baseline--runs-*"
    assert "12.0%" in (tmp_path / "d.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("overall,provisional,allowed", [
    ("CONTINUE", False, True), ("STOP", False, False), ("IN-BETWEEN", False, False),
    ("MEASUREMENT-CHECK", False, False), ("CONTINUE", True, False),
])
def test_dev_rest_needs_a_final_continue_without_a_note(tmp_path, overall, provisional, allowed):
    decision = pipeline.decision_from_report(report(overall, provisional), 42)
    ok, why = pipeline.dev_rest_gate(decision, notes_dir=tmp_path)
    assert ok is allowed, why


def test_a_committed_note_continues_after_in_between_only_for_its_workflow_run(tmp_path):
    decision = pipeline.decision_from_report(report("IN-BETWEEN"), 42)
    note = {"workflow_run_id": 42, "decision": "IN-BETWEEN", "continue": True, "reason": "exploratory continuation",
            "amendment": "PREREGISTRATION.md section 18, entry 2026-12-01"}
    (tmp_path / "dev_baseline_continue.json").write_text(json.dumps(note), encoding="utf-8")
    assert pipeline.dev_rest_gate(decision, notes_dir=tmp_path)[0]
    for change in ({"workflow_run_id": 41}, {"decision": "MEASUREMENT-CHECK"}, {"continue": False}, {"amendment": ""}):
        (tmp_path / "dev_baseline_continue.json").write_text(json.dumps({**note, **change}), encoding="utf-8")
        assert not pipeline.dev_rest_gate(decision, notes_dir=tmp_path)[0], change
    stop = pipeline.decision_from_report(report("STOP"), 42)
    (tmp_path / "dev_baseline_continue.json").write_text(json.dumps({**note, "decision": "STOP"}), encoding="utf-8")
    assert not pipeline.dev_rest_gate(stop, notes_dir=tmp_path)[0]  # STOP cannot be overridden by a note


def test_the_gate_command_prints_the_seed_run_and_exits_5_when_refused(tmp_path, capsys):
    path = tmp_path / "decision.json"
    path.write_text(json.dumps(pipeline.decision_from_report(report("CONTINUE"), 7)), encoding="utf-8")
    assert pipeline.main(["dev-rest-gate", "--decision", str(path), "--notes-dir", str(tmp_path)]) == 0
    assert "seed_run_id=7" in capsys.readouterr().out
    path.write_text(json.dumps(pipeline.decision_from_report(report("STOP"), 7)), encoding="utf-8")
    assert pipeline.main(["dev-rest-gate", "--decision", str(path), "--notes-dir", str(tmp_path)]) == pipeline.EXIT_REFUSED
    assert "seed_run_id" not in capsys.readouterr().out


def test_find_decision_takes_the_newest_trusted_artifact(tmp_path):
    trusted = {"event": "workflow_dispatch", "head_repository": {"full_name": "o/r"}}
    old = pipeline.decision_from_report(report("STOP"), 10)
    new = pipeline.decision_from_report(report("CONTINUE"), 20)
    artifacts = [_artifact(3, "dev-baseline--decision", 30), _artifact(2, "dev-baseline--decision-attempt2", 20),
                 _artifact(1, "dev-baseline--decision", 10), _artifact(4, "dev-baseline--decision", 40, expired=True)]
    artifacts[0]["created_at"] = "2026-12-03T00:00:00Z"  # newest, but from a fork's pull request
    artifacts[1]["created_at"] = "2026-12-02T00:00:00Z"
    artifacts[2]["created_at"] = "2026-12-01T00:00:00Z"
    zips = {2: _zip({"decision.json": json.dumps(new).encode()}), 1: _zip({"decision.json": json.dumps(old).encode()})}
    runs = {10: trusted, 20: trusted, 30: {"event": "pull_request", "head_repository": {"full_name": "fork/r"}}, 40: trusted}
    api = GitHubAPI("o/r", "t", "https://api.github.test", FakeGitHub(artifacts, zips, runs))
    assert pipeline.find_decision(api, tmp_path / "d")["workflow_run_id"] == 20


# --------------------------------------------------------------------------- #
# Smoke usage check
# --------------------------------------------------------------------------- #


def smoke_repo(tmp_path: Path, *, output_tokens: int, cap: float, high: float = 0.5, error: bool = False):
    """A fake-model smoke run plus a budget plan with one run of the same model id."""
    overrides = {"repeats": 1, "models": [
        {"name": "scripted_fake", "label": "good", "options": {"model_id": "fake-cheap", "output_tokens": output_tokens,
                                                              "input_tokens": 400, "behaviour": "crash" if error else "target"}}]}
    repo, config, raw = h.setup(tmp_path, **overrides)
    (repo / "configs" / "later.toml").write_text(h.toml_text(raw), encoding="utf-8")
    budget = BUDGET + (f'\n[[run]]\nid = "later"\nconfig = "configs/later.toml"\nkind = "run"\ninvocations = 1\nmodels = "fake"\n'
                       f'items = "x"\narms = "no_gate"\nlow_usd = 0.0\nhigh_usd = {high}\ncap_usd = {cap}\nnote = ""\n'
                       '\n[[run]]\nid = "planned"\nconfig = ""\nkind = "planned"\ninvocations = 1\nmodels = "fake"\nitems = "x"\n'
                       f'arms = "x"\nlow_usd = 0.0\nhigh_usd = {high}\ncap_usd = {cap}\nnote = ""\n')
    (repo / "configs" / "budget.toml").write_text(budget, encoding="utf-8")
    summary = runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None)
    return repo, summary.run_dir


def test_smoke_check_passes_when_the_measured_usage_is_within_the_assumption(tmp_path):
    # run_helpers' price file assumes 100 visible + 1000 reasoning tokens per call (high); 500 measured
    repo, run_dir = smoke_repo(tmp_path, output_tokens=500, cap=0.55)
    result = pipeline.smoke_check([run_dir], repo_root=repo)
    assert result["ok"], result["failures"]
    row = result["measured"]["fake-cheap"]
    assert row["output_ratio"] == pytest.approx(500 / 1100, abs=1e-3) and row["calls_with_usage"] > 0
    later = next(r for r in result["runs"] if r["id"] == "later")
    assert later["measured_ids"] == ["fake-cheap"] and later["projected_high_usd"] < 0.5


def test_smoke_check_fails_when_the_measured_usage_would_exceed_a_cap(tmp_path, capsys):
    repo, run_dir = smoke_repo(tmp_path, output_tokens=3000, cap=0.55)
    result = pipeline.smoke_check([run_dir], repo_root=repo)
    assert not result["ok"]
    assert {r["id"] for r in result["runs"] if r["exceeds_cap"]} == {"later", "planned"}
    assert any("exceeds its cap" in f for f in result["failures"])
    out = tmp_path / "check.json"
    assert pipeline.main(["smoke-check", str(run_dir), "--json", str(out), "--repo-root", str(repo),
                          "--markdown", str(tmp_path / "c.md")]) == 1
    assert "::error::smoke usage check" in capsys.readouterr().out
    assert "EXCEEDS CAP" in (tmp_path / "c.md").read_text(encoding="utf-8")


def test_smoke_check_fails_on_error_records(tmp_path):
    repo, run_dir = smoke_repo(tmp_path, output_tokens=10, cap=5.0, error=True)
    result = pipeline.smoke_check([run_dir], repo_root=repo)
    assert not result["ok"] and result["error_records"] == {"good": 45} and "ended in an error" in result["failures"][0]


def test_smoke_check_projects_the_real_plan_from_the_ids_of_each_config():
    ids = {r["id"]: pipeline.run_model_ids(r, repo_root=REAL_REPO) for r in PLAN["run"]}
    assert ids["dev_baseline"] == ["gemini-3.1-flash-lite", "glm-5.3-flash", "gpt-6-luna"]
    assert ids["rescore_dev_llm_judge"] == ["kimi-k3.1-pro"] and ids["decomposer_eval"] == ["gpt-6-luna"]
    assert "serpapi/google" in ids["dev_live"]
    assert all(ids[r["id"]] is None for r in PLAN["run"] if not r["config"])  # planned runs without a config yet


# --------------------------------------------------------------------------- #
# check-secrets
# --------------------------------------------------------------------------- #


def test_check_secrets_reports_names_never_values_and_skips_missing_keys(tmp_path, monkeypatch, capsys):
    from binding_gate.secrets import KNOWN_SECRETS

    for name in KNOWN_SECRETS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RUNS_ENCRYPTION_PASSPHRASE", "VALUE-THAT-MUST-NOT-APPEAR")
    out = tmp_path / "report.json"
    assert pipeline.main(["check-secrets", "--json", str(out), "--markdown", str(tmp_path / "r.md"),
                          "--repo-root", str(REAL_REPO)]) == 0
    text = out.read_text(encoding="utf-8") + (tmp_path / "r.md").read_text(encoding="utf-8") + capsys.readouterr().out
    assert "VALUE-THAT-MUST-NOT-APPEAR" not in text
    report = json.loads(out.read_text(encoding="utf-8"))
    status = {r["name"]: r["set"] for r in report["secrets"]}
    assert status["RUNS_ENCRYPTION_PASSPHRASE"] is True and status["OPENAI_API_KEY"] is False
    assert {r["name"] for r in report["secrets"] if r["needed_by_study"]} == set(pipeline.STUDY_SECRETS)
    assert {r["status"] for r in report["preflights"]} == {"SKIPPED"}
    ids = {r["model_id"] for r in report["preflights"]}
    assert {"gpt-6-luna", "gpt-6.1-sol", "gemini-3.1-flash-lite", "gemini-3.5-flash", "glm-5.3-flash", "glm-5.3",
            "kimi-k3.1-pro", "serpapi/google"} <= ids


def test_a_zai_list_models_404_is_diagnosed_with_the_fallback(monkeypatch):
    from binding_gate.models._http import PreflightResult
    from binding_gate.models.zai_chat import ZaiChatClient

    monkeypatch.setenv("ZAI_API_KEY", "k")
    monkeypatch.setattr(ZaiChatClient, "preflight", lambda self: PreflightResult(self.name, self.model_id, False,
                                                                                 f"model id {self.model_id!r} does not exist at zai (HTTP 404)"))
    rows = [r for r in pipeline.run_preflights(("configs/dev_smoke.toml",), repo_root=REAL_REPO) if r["provider"] == "zai"]
    assert rows and all(r["status"] == "FAILED" and "preflight_list_models = false" in r["diagnosis"] for r in rows)


# --------------------------------------------------------------------------- #
# analyze-dev
# --------------------------------------------------------------------------- #


def test_analyze_dev_groups_run_directories_by_config(tmp_path):
    repo, config, raw = h.setup(tmp_path, repeats=1)
    runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "artifacts" / "a1", confirm_spend=True, log=lambda m: None)
    groups = pipeline.group_run_dirs(tmp_path / "artifacts")
    assert list(groups) == ["runner-test"] and len(groups["runner-test"]) == 1
    index = pipeline.analyze_dev(tmp_path / "artifacts", tmp_path / "analysis")
    assert index["analyses"]["runner-test"]["exit_code"] == 0 and index["calibration"] is None
    assert (tmp_path / "analysis" / "runner-test" / "analysis.json").is_file()
    assert json.loads((tmp_path / "analysis" / "index.json").read_text(encoding="utf-8"))["failed"] == []
