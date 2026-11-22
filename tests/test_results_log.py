"""PR 17: the results-log branch (binding_gate.results_log): allow-list, scans, folder names, git on a local bare
repository, and the budget ledger reading the branch's snapshots. No network, no key."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import builders as b
import run_helpers as h
import sec_helpers
from binding_gate import cli, ledger, pipeline, results_log, runner
from binding_gate.ghapi import GitHubAPI
from test_ledger import BUDGET, FakeGitHub, _artifact, _zip

REAL_REPO = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((REAL_REPO / ".github" / "workflows" / "results_log.yml").read_text(encoding="utf-8"))
PAGE_TEXT = "Acme Corp reported record revenue. " * 120  # 4,200 characters of "document text"


# --------------------------------------------------------------------------- #
# Folder names
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("workflow,title,stage", [
    ("dev pipeline", "dev pipeline: dev-baseline", "dev-baseline"),
    ("dev pipeline", "dev pipeline: sec-dataset-prepare", "sec-dataset-prepare"),
    ("dev pipeline", "dev pipeline: ../../main", "dev-pipeline-..-..-main"),
    ("baseline", "baseline: configs/dev_smoke.toml (all, dry run)", "baseline-dev_smoke-all-dry-run"),
    ("baseline", "baseline: configs/dev_sec_baseline.toml (openai-low, paid)", "baseline-dev_sec_baseline-openai-low-paid"),
    ("frozen test run", "frozen test run: layer2_sec at prereg-freeze-1", "frozen-layer2_sec"),
    ("something else", "x", "something-else"),
])
def test_stage_labels_come_from_the_run_titles(workflow, title, stage):
    assert results_log.stage_label(workflow, title) == stage
    folder = results_log.run_folder(123, results_log.stage_label(workflow, title))
    assert results_log.check_folder(folder) == folder and folder.startswith("runs/123-")


def test_run_folders_and_their_checks():
    assert results_log.run_folder(7, "smoke") == "runs/7-smoke"
    assert results_log.run_folder("7", "smoke", attempt=2) == "runs/7-smoke-attempt2"
    for bad in ("main", "runs", "/runs/1", "runs/../x", "results/1", "runs/a b", "frozen/../../x"):
        with pytest.raises(results_log.ResultsLogError):
            results_log.check_folder(bad)
    with pytest.raises(results_log.ResultsLogError):
        results_log.run_folder("12; rm -rf /", "smoke")
    assert all(s in pipeline.STAGES for s in ("check-secrets", "layer2-dev", "sec-dataset-prepare"))


# --------------------------------------------------------------------------- #
# The allow-list and the scan
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", [
    "check-secrets--report/report.json", "smoke--dev_smoke--usage-check/smoke_usage_check.json",
    "dev-baseline--decision/decision.json", "pipeline--all-dev--ledger/ledger.json", "dev-rest--analysis/index.json",
    "dev-rest--analysis/dev-gated/analysis.json", "dev-rest--analysis/dev-gated/report.md", "dev-rest--analysis/dev-gated.log",
    "dev-rest--analysis/calibration_dev_gated.json", "dev-baseline--dev_baseline--baseline-report/baseline_report.json",
    "dev-baseline--dev_baseline--runs-openai-low/r1/baseline_report_openai-low.json",
    "dev-baseline--dev_baseline--runs-openai-low/r1/manifest.json", "x--runs-a/r1/invocations.jsonl",
    "dev-rest--decomposer_eval_dev--decomposer-eval/d1/decomposer_report.json", "dev-rest--dev_llm_judge--rescore/judge_agreement.json",
    "layer3-dev--dev_live--layer3-summary-all-models/layer3_summary.json", "sec-dataset-prepare--dataset/prepare.json",
    "sec-dataset-prepare--dataset/manifest.json", "sec-dataset-prepare--dataset/selection.json",
    "layer3-dev--dev_live--snapshot-manifest-all-models/runs/r1/snapshots/publish_manifest.json",
])
def test_report_files_are_on_the_allow_list(path):
    assert results_log.name_refusal(path) is None


@pytest.mark.parametrize("path,why", [
    ("x--runs-a/r1/traces.jsonl", "traces"),
    ("x--rescore/r1/scores.llm_judge.jsonl", "quote"),
    ("sec/items.jsonl", "document text"),
    ("x--decomposer-eval/d1/decompositions.jsonl", "questions"),
    ("x--runs-encrypted-all-models/runs.tar.gz.gpg", "encrypted"),
    ("x--snapshot-manifest-all/runs/r1/snapshots/raw/abc.html", "third-party"),
    ("x--snapshot-manifest-all/runs/r1/snapshots/manifest.json", "third-party"),
    ("data/cache/abc.json", "caches"),
    ("seeds/r1/manifest.json", "seed runs"),
    ("x--analysis/dev-gated/figures/a.png", "binary"),
    ("x/notes.txt", "allow-list"),
    ("x/test_access_log.jsonl", "pull request"),
])
def test_everything_else_is_refused_by_name(path, why):
    assert why in results_log.name_refusal(path)


@pytest.mark.parametrize("content,why", [
    ('{"key": "sk-proj-' + "a" * 40 + '"}', "API key"),
    ('{"k": "AIza' + "B" * 35 + '"}', "Google API key"),
    ('{"t": "ghp_' + "c" * 36 + '"}', "GitHub token"),
    ('{"contact": "owner@example.org"}', "e-mail"),
    ('{"page": "' + PAGE_TEXT + '"}', "possible page or document text"),
    ('{"records": [{"text": "' + "word " * 80 + '"}]}', "'text' field"),
    ('{"messages": ["' + "x" * 400 + '"]}', "'messages' field"),
    ("not json", "not valid JSON"),
])
def test_content_that_looks_like_a_secret_or_page_text_is_refused(content, why):
    assert why in results_log.content_refusal("a/report.json", content.encode())


def test_text_files_jsonl_and_secret_values_from_the_environment():
    assert "characters" in results_log.content_refusal("a/x.log", ("ok\n" + "y" * 2500).encode())
    assert "line 2" in results_log.content_refusal("a/invocations.jsonl", ('{"a": 1}\n{"text": "' + "z" * 400 + '"}\n').encode())
    secrets = results_log.secret_values({"SEC_CONTACT_EMAIL": "x", "OPENAI_API_KEY": "plain-value-1234", "PATH": "/usr/bin"})
    assert secrets == ["plain-value-1234"]  # values shorter than 8 characters and undocumented variables are ignored
    assert results_log.content_refusal("a/report.md", b"the key plain-value-1234 leaked", secrets=secrets) == \
        "contains the value of a secret"
    assert results_log.content_refusal("a/report.md", b"\xff\xfe") == "not UTF-8 text"
    assert results_log.content_refusal("a/ledger.json", json.dumps({"problems": ["p" * 5000]}).encode()) is None  # ledger limit
    assert results_log.content_refusal("a/report.md", b"| model | 12.0% |\n") is None


# --------------------------------------------------------------------------- #
# Collecting real outputs
# --------------------------------------------------------------------------- #


def artifact_tree(tmp_path: Path, monkeypatch) -> Path:
    """Downloaded artifacts as actions/download-artifact lays them out: one directory per artifact."""
    src = tmp_path / "artifacts"
    repo, config, _raw = h.setup(tmp_path / "w", repeats=1)
    (repo / "configs" / "budget.toml").write_text(BUDGET, encoding="utf-8")
    summary = runner.execute_run(config, repo_root=repo, runs_dir=src / "dev-rest--dev_gated--runs-good", confirm_spend=True,
                                 log=lambda m: None)
    assert cli.main(["report", str(summary.run_dir), "--json", str(summary.run_dir / "baseline_report_good.json")]) == 0
    assert pipeline.main(["analyze-dev", str(src / "dev-rest--dev_gated--runs-good"), "--out", str(src / "dev-rest--analysis")]) == 0
    result = ledger.compute(repo_root=repo, directories=[src / "dev-rest--dev_gated--runs-good"], snapshot=None)
    (src / "dev-rest--dev_gated--ledger").mkdir()
    (src / "dev-rest--dev_gated--ledger" / "ledger.json").write_text(json.dumps(result), encoding="utf-8")
    (src / "dev-rest--dev_gated--ledger" / "ledger.md").write_text(ledger.markdown(result), encoding="utf-8")
    for name in __import__("binding_gate.secrets", fromlist=["KNOWN_SECRETS"]).KNOWN_SECRETS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RUNS_ENCRYPTION_PASSPHRASE", "VALUE-THAT-MUST-NOT-APPEAR")
    (src / "check-secrets--report").mkdir()
    assert pipeline.main(["check-secrets", "--json", str(src / "check-secrets--report" / "report.json"),
                          "--markdown", str(src / "check-secrets--report" / "report.md"), "--repo-root", str(REAL_REPO)]) == 0
    sec_root = tmp_path / "sec"
    sec_helpers.write_dataset(sec_root)
    (src / "sec-dataset-prepare--dataset").mkdir()
    for name in ("manifest.json", "selection.json", "items.jsonl"):
        shutil.copy(sec_root / name, src / "sec-dataset-prepare--dataset" / name)
    # a traces file renamed to an allowed name must still be refused by the content scan
    traces = (summary.run_dir / "traces.jsonl").read_text(encoding="utf-8")
    (src / "renamed--report").mkdir()
    (src / "renamed--report" / "report.json").write_text(json.dumps({"traces": traces.splitlines(), "page": PAGE_TEXT}), encoding="utf-8")
    (src / "x--runs-encrypted-all-models").mkdir()
    (src / "x--runs-encrypted-all-models" / "runs.tar.gz.gpg").write_bytes(b"\x8c\x0d")
    return src


def test_collect_keeps_the_reports_and_never_traces_items_secrets_or_page_text(tmp_path, monkeypatch):
    src = artifact_tree(tmp_path, monkeypatch)
    meta = {"run_id": 42, "workflow": "dev pipeline", "title": "dev pipeline: dev-rest", "stage": "dev-rest", "attempt": 1}
    record = results_log.collect(src, tmp_path / "out", meta=meta)
    kept = {f["path"] for f in record["kept"]}
    left = {f["path"]: f["reason"] for f in record["left_out"]}
    run_dir = next(p for p in (src / "dev-rest--dev_gated--runs-good").iterdir() if p.is_dir()).name
    expected_kept = {
        "check-secrets--report/report.json", "check-secrets--report/report.md",
        "dev-rest--dev_gated--ledger/ledger.json", "dev-rest--dev_gated--ledger/ledger.md",
        "dev-rest--analysis/index.json", "dev-rest--analysis/runner-test/analysis.json",
        "dev-rest--analysis/runner-test/report.md", "dev-rest--analysis/runner-test.log",
        f"dev-rest--dev_gated--runs-good/{run_dir}/manifest.json", f"dev-rest--dev_gated--runs-good/{run_dir}/invocations.jsonl",
        f"dev-rest--dev_gated--runs-good/{run_dir}/baseline_report_good.json",
        "sec-dataset-prepare--dataset/manifest.json", "sec-dataset-prepare--dataset/selection.json",
    }
    assert expected_kept <= kept, sorted(expected_kept - kept)
    assert f"dev-rest--dev_gated--runs-good/{run_dir}/traces.jsonl" in left
    assert "sec-dataset-prepare--dataset/items.jsonl" in left
    assert "possible page or document text" in left["renamed--report/report.json"]
    assert "x--runs-encrypted-all-models/runs.tar.gz.gpg" in left
    out_text = "\n".join(p.read_text(encoding="utf-8") for p in (tmp_path / "out").rglob("*") if p.is_file())
    assert "VALUE-THAT-MUST-NOT-APPEAR" not in out_text and sec_helpers.FAKE_EMAIL not in out_text
    assert PAGE_TEXT[:200] not in out_text
    assert not list((tmp_path / "out").rglob("traces.jsonl")) and not list((tmp_path / "out").rglob("items.jsonl"))
    saved = json.loads((tmp_path / "out" / "run.json").read_text(encoding="utf-8"))
    assert saved["run_id"] == 42 and saved["stage"] == "dev-rest" and saved["left_out"] == record["left_out"]
    assert results_log.staging_refusals(tmp_path / "out") == []
    with pytest.raises(results_log.ResultsLogError, match="must be empty"):
        results_log.collect(src, tmp_path / "out", meta=meta)


def test_collect_cli_prints_the_folder(tmp_path, capsys):
    (tmp_path / "src" / "a--decision").mkdir(parents=True)
    (tmp_path / "src" / "a--decision" / "decision.json").write_text('{"decision": "CONTINUE"}', encoding="utf-8")
    assert results_log.main(["collect", str(tmp_path / "src"), "--out", str(tmp_path / "o"), "--run-id", "9",
                             "--workflow", "dev pipeline", "--title", "dev pipeline: dev-baseline", "--attempt", "2"]) == 0
    assert "folder=runs/9-dev-baseline-attempt2" in capsys.readouterr().out
    assert (tmp_path / "o" / "a--decision" / "decision.json").is_file()
    assert results_log.main(["collect", str(tmp_path / "missing"), "--out", str(tmp_path / "o2"), "--run-id", "9",
                             "--workflow", "baseline"]) == 0  # a run without artifacts still gets its run.json
    assert json.loads((tmp_path / "o2" / "run.json").read_text(encoding="utf-8"))["kept"] == []


# --------------------------------------------------------------------------- #
# Git: a local bare repository stands in for GitHub
# --------------------------------------------------------------------------- #


def remote_with_clones(tmp_path: Path, n: int = 2) -> tuple[Path, list[Path]]:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    seed = b.init_repo(tmp_path / "seed")
    b.git(seed, "branch", "-M", "main")
    b.git(seed, "remote", "add", "origin", str(bare))
    b.git(seed, "push", "-q", "origin", "main")
    clones = []
    for i in range(n):
        clone = tmp_path / f"clone{i}"
        subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
        clones.append(clone)
    return bare, clones


def staging(tmp_path: Path, name: str, content: dict | None = None) -> Path:
    path = tmp_path / f"staging-{name}"
    (path / "a--decision").mkdir(parents=True, exist_ok=True)
    (path / "a--decision" / "decision.json").write_text(json.dumps(content or {"decision": name}), encoding="utf-8")
    return path


def branch_files(bare: Path, branch: str = results_log.BRANCH) -> set[str]:
    out = subprocess.run(["git", "-C", str(bare), "ls-tree", "-r", "--name-only", branch], capture_output=True, text=True, check=True)
    return set(out.stdout.split())


def test_the_first_commit_creates_an_orphan_branch_and_main_is_untouched(tmp_path):
    bare, (clone, _other) = remote_with_clones(tmp_path)
    main_before = b.git(bare, "rev-parse", "main")
    result = results_log.commit_to_branch(clone, staging(tmp_path, "one"), "runs/1-smoke", message="results-log: runs/1-smoke")
    assert result["result"] == "pushed" and result["attempts"] == 1
    assert branch_files(bare) == {"README.md", "runs/1-smoke/a--decision/decision.json"}
    assert b.git(bare, "rev-parse", "main") == main_before
    assert b.git(bare, "rev-list", "--count", results_log.BRANCH) == "1"  # orphan: no shared history with main
    merge_base = subprocess.run(["git", "-C", str(bare), "merge-base", "main", results_log.BRANCH], capture_output=True)
    assert merge_base.returncode != 0
    author = b.git(bare, "log", "-1", "--format=%an <%ae>", results_log.BRANCH)
    assert author == f"{results_log.BOT_NAME} <{results_log.BOT_EMAIL}>"
    assert not (clone / "runs").exists() and b.git(clone, "status", "--porcelain") == ""  # the checkout is unchanged
    assert b.git(clone, "worktree", "list").count("\n") == 0  # the temporary worktree is gone
    again = results_log.commit_to_branch(clone, staging(tmp_path, "one"), "runs/1-smoke", message="same")
    assert again["result"] == "unchanged"
    results_log.commit_to_branch(clone, staging(tmp_path, "two"), "runs/2-dev-baseline", message="second")
    assert branch_files(bare) >= {"runs/1-smoke/a--decision/decision.json", "runs/2-dev-baseline/a--decision/decision.json"}


def test_a_concurrent_push_is_retried_with_pull_rebase(tmp_path):
    bare, (first, second) = remote_with_clones(tmp_path)
    results_log.commit_to_branch(first, staging(tmp_path, "zero"), "runs/1-check-secrets", message="m0")

    def other_run_pushes(attempt):
        if attempt == 1:
            results_log.commit_to_branch(second, staging(tmp_path, "other"), "runs/3-smoke", message="other run")

    result = results_log.commit_to_branch(first, staging(tmp_path, "mine"), "runs/2-dev-baseline", message="mine",
                                          before_push=other_run_pushes, sleep=lambda s: None)
    assert result["result"] == "pushed" and result.get("rebased") is True
    assert {"runs/1-check-secrets/a--decision/decision.json", "runs/2-dev-baseline/a--decision/decision.json",
            "runs/3-smoke/a--decision/decision.json"} <= branch_files(bare)
    assert b.git(bare, "rev-list", "--count", results_log.BRANCH) == "3"  # linear history, nothing overwritten


def test_two_runs_creating_the_branch_at_once_both_land(tmp_path):
    bare, (first, second) = remote_with_clones(tmp_path)
    readme_other = {"value": None}

    def other_creates_the_branch(attempt):
        if attempt == 1:
            results_log.commit_to_branch(second, staging(tmp_path, "other"), "runs/5-smoke", message="other")
            readme_other["value"] = True

    result = results_log.commit_to_branch(first, staging(tmp_path, "mine"), "runs/4-smoke", message="mine",
                                          before_push=other_creates_the_branch, sleep=lambda s: None)
    assert result["result"] == "pushed" and readme_other["value"]
    assert {"README.md", "runs/4-smoke/a--decision/decision.json", "runs/5-smoke/a--decision/decision.json"} <= branch_files(bare)


def test_nothing_is_committed_when_a_staged_file_is_not_allowed(tmp_path):
    bare, (clone, _other) = remote_with_clones(tmp_path)
    bad = staging(tmp_path, "bad")
    (bad / "a--decision" / "traces.jsonl").write_text('{"x": 1}\n', encoding="utf-8")
    with pytest.raises(results_log.ResultsLogError, match="traces.jsonl"):
        results_log.commit_to_branch(clone, bad, "runs/1-smoke", message="m")
    leaky = staging(tmp_path, "leaky", {"contact": "owner@example.org"})
    with pytest.raises(results_log.ResultsLogError, match="e-mail"):
        results_log.commit_to_branch(clone, leaky, "runs/1-smoke", message="m")
    with pytest.raises(results_log.ResultsLogError, match="runs/<...> or frozen/<...>"):
        results_log.commit_to_branch(clone, staging(tmp_path, "ok"), "main", message="m")
    assert results_log.remote_branch_exists(clone, "origin", results_log.BRANCH) is False


# --------------------------------------------------------------------------- #
# The budget ledger reads the branch's snapshots (spend past the 90-day artifact retention)
# --------------------------------------------------------------------------- #


def test_the_ledger_reads_results_log_snapshots_so_expired_artifacts_stay_covered(tmp_path):
    bare, (clone, _other) = remote_with_clones(tmp_path)
    earlier = {"ledger_version": 1, "covered_artifact_ids": [4],
               "entries": {"run:old-run": {"usd": 3.5, "kind": "trace", "source": "artifact 4"}}}
    snap = tmp_path / "snap"
    (snap / "ledger-after-run").mkdir(parents=True)
    (snap / "ledger-after-run" / "ledger.json").write_text(json.dumps(earlier), encoding="utf-8")
    results_log.commit_to_branch(clone, snap, "runs/10-dev-baseline", message="m")
    b.git(clone, "fetch", "-q", "origin", f"+refs/heads/{results_log.BRANCH}:refs/remotes/origin/{results_log.BRANCH}")
    out = tmp_path / "exported"
    assert results_log.export_ledgers(clone, f"refs/remotes/origin/{results_log.BRANCH}", out) == \
        ["runs/10-dev-baseline/ledger-after-run/ledger.json"]
    assert results_log.export_ledgers(clone, "refs/remotes/origin/no-such-branch", tmp_path / "none") == []
    assert ledger.extra_ledger_coverage(out) == {"covered": [4], "read": 1, "problems": []}

    trusted = {"event": "workflow_dispatch", "head_repository": {"full_name": "o/r"}}
    artifacts = [_artifact(4, "dev-baseline--dev_baseline--runs-openai-low", 100, expired=True)]
    api = GitHubAPI("o/r", "t", "https://api.github.test", FakeGitHub(artifacts, {}, {100: trusted}))
    without = ledger.fetch(api, tmp_path / "in1")
    assert without["artifacts"][0]["status"] == "expired-uncovered" and without["problems"]
    api = GitHubAPI("o/r", "t", "https://api.github.test", FakeGitHub(artifacts, {}, {100: trusted}))
    index = ledger.fetch(api, tmp_path / "in2", snapshot_covered=ledger.extra_ledger_coverage(out)["covered"])
    assert index["artifacts"][0]["status"] == "expired-covered" and not index["problems"]
    repo = h.make_repo(tmp_path / "w")
    (repo / "configs" / "budget.toml").write_text(BUDGET, encoding="utf-8")
    result = ledger.compute(repo_root=repo, inputs=tmp_path / "in2", directories=[out], snapshot=None)
    assert result["complete"] and result["total_usd"] == pytest.approx(3.5) and 4 in result["covered_artifact_ids"]
    (out / "broken").mkdir()
    (out / "broken" / "ledger.json").write_text("{}", encoding="utf-8")
    assert ledger.extra_ledger_coverage(out)["problems"]


def test_ledger_fetch_cli_accepts_the_exported_snapshots(tmp_path, monkeypatch):
    out = tmp_path / "exported" / "runs" / "1-x" / "a--ledger"
    out.mkdir(parents=True)
    (out / "ledger.json").write_text(json.dumps({"ledger_version": 1, "covered_artifact_ids": [1], "entries": {}}), encoding="utf-8")
    trusted = {"event": "workflow_dispatch", "head_repository": {"full_name": "o/r"}}
    fake = FakeGitHub([_artifact(1, "runs-openai-low", 100, expired=True)], {1: _zip({})}, {100: trusted})
    monkeypatch.setattr(GitHubAPI, "from_environment", classmethod(lambda cls, **kw: GitHubAPI("o/r", "t", "https://api.github.test", fake)))
    monkeypatch.chdir(tmp_path)
    assert ledger.main(["fetch", "--out", "inputs", "--extra-ledgers", str(tmp_path / "exported")]) == 0
    index = json.loads((tmp_path / "inputs" / "artifacts.json").read_text(encoding="utf-8"))
    assert index["artifacts"][0]["status"] == "expired-covered"


# --------------------------------------------------------------------------- #
# The workflow
# --------------------------------------------------------------------------- #


def test_the_results_log_workflow_runs_after_every_dispatch_run_and_holds_no_secret():
    on = WORKFLOW.get("on", WORKFLOW.get(True))
    assert on == {"workflow_run": {"workflows": ["dev pipeline", "baseline", "frozen test run"], "types": ["completed"]}}
    names = {yaml.safe_load((REAL_REPO / ".github" / "workflows" / f).read_text(encoding="utf-8"))["name"]
             for f in ("dev_pipeline.yml", "baseline.yml", "frozen_test.yml")}
    assert names == set(on["workflow_run"]["workflows"])  # the names it listens to exist
    assert WORKFLOW["permissions"] == {"contents": "write", "actions": "read"} and "concurrency" not in WORKFLOW
    text = (REAL_REPO / ".github" / "workflows" / "results_log.yml").read_text(encoding="utf-8")
    assert "secrets." not in text
    (job,) = WORKFLOW["jobs"].values()
    assert "github.event.workflow_run.event == 'workflow_dispatch'" in job["if"]
    assert "head_repository.full_name == github.repository" in job["if"]
    steps = [s.get("name") or s.get("uses") for s in job["steps"]]
    collect = "Keep only the allowed report files (allow-list, scanned for secrets and page text)"
    ledger_step = "Budget ledger right after the run (report only; collected like an artifact named ledger-after-run)"
    commit = "Commit to the results-log branch (bot identity; retried on a concurrent push)"
    assert steps.index(ledger_step) < steps.index(collect) < steps.index(commit)
    by_name = {s.get("name"): s for s in job["steps"]}
    assert "binding_gate.results_log collect" in by_name[collect]["run"]
    assert "binding_gate.results_log commit" in by_name[commit]["run"] and by_name[commit]["env"]["FOLDER"]
    download = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact"))
    assert download["with"]["run-id"] == "${{ github.event.workflow_run.id }}" and "pattern" not in download["with"]
    for workflow in ("dev_pipeline.yml", "baseline.yml", "frozen_test.yml"):
        doc = yaml.safe_load((REAL_REPO / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))
        assert doc.get("run-name"), workflow  # the folder name comes from the run's title


def test_pushes_to_the_results_log_branch_do_not_run_the_test_suite():
    tests = yaml.safe_load((REAL_REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))
    on = tests.get("on", tests.get(True))
    assert on["push"] == {"branches-ignore": ["results-log"]}


def test_when_the_rebase_conflicts_the_commit_is_rebuilt_on_the_fetched_branch(tmp_path):
    """The same folder written twice at once (a re-delivered event): the later collection replaces the earlier."""
    bare, (first, second) = remote_with_clones(tmp_path)
    results_log.commit_to_branch(first, staging(tmp_path, "zero"), "runs/1-check-secrets", message="m0")
    sleeps = []

    def same_folder_other_content(attempt):
        if attempt == 1:
            results_log.commit_to_branch(second, staging(tmp_path, "theirs", {"decision": "STOP"}), "runs/2-dev-baseline",
                                         message="theirs")

    result = results_log.commit_to_branch(first, staging(tmp_path, "mine", {"decision": "CONTINUE"}), "runs/2-dev-baseline",
                                          message="mine", before_push=same_folder_other_content, sleep=sleeps.append)
    assert result["result"] == "pushed" and result["attempts"] == 2 and sleeps == [2.0]
    shown = b.git(bare, "show", f"{results_log.BRANCH}:runs/2-dev-baseline/a--decision/decision.json")
    assert json.loads(shown) == {"decision": "CONTINUE"}
    assert "runs/1-check-secrets/a--decision/decision.json" in branch_files(bare)
