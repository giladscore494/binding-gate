"""PR 15: the global budget ledger (binding_gate.ledger) on synthetic run directories and a fake GitHub API."""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

import run_helpers as h
from binding_gate import ledger, runner
from binding_gate.contracts import Message, ModelCall, ModelRequest, ModelResponse, TokenUsage
from binding_gate.ghapi import GitHubAPI, extract_zip, is_trusted_run

REAL_REPO = Path(__file__).resolve().parents[1]
BUDGET = 'budget_version = 1\nhard_limit_usd = 50.0\nplanned_high_max_usd = 45.0\nreserve_usd = 5.0\n'


def make_run(tmp_path: Path, **overrides):
    """A real (fake-model) run through the runner; returns (repo, run_dir, summary)."""
    repo, config, _raw = h.setup(tmp_path, **overrides)
    (repo / "configs" / "budget.toml").write_text(BUDGET, encoding="utf-8")
    summary = runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None)
    return repo, summary.run_dir, summary


# --------------------------------------------------------------------------- #
# Spend of run directories, priced as the runner prices it
# --------------------------------------------------------------------------- #


def test_the_spend_of_a_run_directory_equals_what_the_runner_charged(tmp_path):
    repo, run_dir, summary = make_run(tmp_path)
    entry = ledger.trace_run_spend(run_dir, repo)
    assert entry.key == f"run:{summary.run_id}"
    assert entry.usd == pytest.approx(sum(summary.spend_usd.values()))
    assert entry.detail["computed_usd"] == pytest.approx(entry.detail["recorded_usd"])
    assert entry.unpriced_calls == 0 and entry.calls > 0
    assert set(entry.detail["by_model_usd"]) == {"good", "bad"}


def test_copies_of_the_same_run_count_once_and_the_larger_copy_wins(tmp_path):
    repo, run_dir, summary = make_run(tmp_path)
    inputs = tmp_path / "copies"
    shutil.copytree(run_dir, inputs / "a1" / run_dir.name)
    shutil.copytree(run_dir, inputs / "a2" / run_dir.name)
    result = ledger.compute(repo_root=repo, directories=[inputs], snapshot=None)
    assert list(result["entries"]) == [f"run:{summary.run_id}"]
    assert result["total_usd"] == pytest.approx(sum(summary.spend_usd.values()), abs=1e-6)
    # a truncated (earlier) copy is smaller: the full copy is kept
    lines = (inputs / "a2" / run_dir.name / "traces.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
    (inputs / "a2" / run_dir.name / "traces.jsonl").write_text("".join(lines[:3]), encoding="utf-8")
    again = ledger.compute(repo_root=repo, directories=[inputs], snapshot=None)
    assert again["total_usd"] == pytest.approx(result["total_usd"], abs=1e-6)


def test_the_spend_the_runner_recorded_is_used_when_it_is_larger(tmp_path):
    repo, run_dir, summary = make_run(tmp_path)
    with (run_dir / runner.INVOCATIONS_NAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "ended", "spend_usd": {"good": 7.0, "bad": 0.0}}) + "\n")
    entry = ledger.trace_run_spend(run_dir, repo)
    assert entry.usd == pytest.approx(7.0 + summary.spend_usd["bad"])
    assert ledger.recorded_run_spend([{"event": "ended", "spend_usd": {"x": 1.0}}, {"event": "ended", "spend_usd": {"x": 2.0, "y": 1.0}},
                                      {"event": "started", "spend_usd": {"x": 9.0}}]) == 3.0


def test_an_unreadable_run_directory_makes_the_ledger_incomplete(tmp_path):
    repo, run_dir, _summary = make_run(tmp_path)
    broken = tmp_path / "broken" / run_dir.name
    shutil.copytree(run_dir, broken)
    (broken / "traces.jsonl").write_text("{not json\n", encoding="utf-8")
    result = ledger.compute(repo_root=repo, directories=[tmp_path / "broken"], snapshot=None)
    assert not result["complete"] and "cannot be read" in result["problems"][0]
    check = ledger.check(result, 0.1, what="x")
    assert not check["allowed"] and "INCOMPLETE" in check["reasons"][0]


def _call(model_id: str, usage: TokenUsage | None, *, client: str = "judge", max_output: int | None = None) -> dict:
    params = {} if max_output is None else {"max_output_tokens": max_output}
    return ModelCall(
        purpose="judge", client=client, request=ModelRequest((Message("user", "x" * 400),), params),
        started_at="2026-11-22T10:00:00+00:00", ended_at="2026-11-22T10:00:01+00:00",
        response=ModelResponse("{}", model_id, usage, {"requested_model": model_id}),
    ).to_dict()


def test_judge_scores_and_decomposer_measurements_are_priced(tmp_path):
    repo, run_dir, summary = make_run(tmp_path)
    scores = [
        {"item_id": "i1", "judge_hash": "h" * 64, "score": {"details": {"model_calls": [_call("fake-dear", TokenUsage(1000, 100))]}}},
        {"item_id": "i2", "judge_hash": "h" * 64, "score": {"details": {"model_calls": [_call("fake-dear", None, max_output=50)]}}},
        {"item_id": "i3", "judge_hash": "h" * 64, "score": {"details": {}}},
    ]
    (run_dir / "scores.llm_judge.jsonl").write_text("".join(json.dumps(s) + "\n" for s in scores), encoding="utf-8")
    (entry,) = ledger.judge_scores_spend(run_dir, repo)
    expected = (1000 * 100 + 100 * 200) / 1e6 + (100 * 100 + 50 * 200) / 1e6  # 400 chars / 4 = 100 input tokens
    assert entry.key == f"judge:{summary.run_id}:{'h' * 16}" and entry.usd == pytest.approx(expected)

    from binding_gate.canonical import sha256_json
    from binding_gate.contracts import RunManifest

    cfg = {"eval_version": 1, "name": "decomposer-eval-test", "runner": {"prices": "configs/prices.toml", "max_usd": 1.0},
           "decomposer": {"name": "model_decomposer", "options": {"model": {"name": "x", "label": "decomposer",
                                                                            "options": {"model_id": "fake-cheap", "max_output_tokens": 300}}}}}
    eval_dir = tmp_path / "evals" / "decomposer-eval-1"
    eval_dir.mkdir(parents=True)
    manifest = RunManifest(run_id="decomposer-eval-1", config=cfg, config_hash=sha256_json(cfg), git_commit="abc", git_dirty=False,
                           package_version="0", python_version="3", started_at="2026-11-22T10:00:00+00:00")
    (eval_dir / "manifest.json").write_text(manifest.to_json(), encoding="utf-8")
    records = [{"question_id": "q", "repeat": 0, "cost_usd": 0.0,
                "model_calls": [_call("fake-cheap", TokenUsage(2000, 1000), client="decomposer"),
                                _call("unknown-id", None, client="decomposer")]}]
    (eval_dir / "decompositions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    dec = ledger.decomposer_eval_spend(eval_dir, repo)
    # the second call has no reported model id with a price, but its client label maps to fake-cheap (usage: 100 in, 300 out)
    assert dec.usd == pytest.approx((2000 * 1 + 1000 * 2) / 1e6 + (100 * 1 + 300 * 2) / 1e6)
    assert dec.unpriced_calls == 0

    result = ledger.compute(repo_root=repo, directories=[tmp_path / "runs", tmp_path / "evals"], snapshot=None)
    assert set(result["entries"]) == {f"run:{summary.run_id}", entry.key, dec.key}
    assert result["total_usd"] == pytest.approx(sum(summary.spend_usd.values()) + entry.usd + dec.usd, abs=1e-6)


def test_unpriced_calls_make_the_ledger_incomplete(tmp_path):
    repo, run_dir, _summary = make_run(tmp_path)
    (run_dir / "scores.llm_judge.jsonl").write_text(json.dumps(
        {"judge_hash": "z" * 64, "score": {"details": {"model_calls": [_call("no-such-model", TokenUsage(1, 1))]}}}) + "\n", encoding="utf-8")
    result = ledger.compute(repo_root=repo, directories=[tmp_path / "runs"], snapshot=None)
    assert not result["complete"] and "cannot be priced" in result["problems"][0]


# --------------------------------------------------------------------------- #
# Snapshot, merging, the check, the command line
# --------------------------------------------------------------------------- #


def test_the_committed_snapshot_is_read_and_manual_entries_count(tmp_path):
    repo, run_dir, summary = make_run(tmp_path)
    (repo / "results").mkdir()
    snapshot = {"ledger_version": 1, "covered_artifact_ids": [11],
                "entries": {"manual:frozen-test-run-1": {"usd": 12.5, "kind": "manual", "source": "provider dashboards"},
                            f"run:{summary.run_id}": {"usd": 0.000001, "kind": "carried", "source": "old"}}}
    (repo / "results" / "ledger.json").write_text(json.dumps(snapshot), encoding="utf-8")
    result = ledger.compute(repo_root=repo, directories=[tmp_path / "runs"])
    assert result["complete"]
    assert result["total_usd"] == pytest.approx(12.5 + sum(summary.spend_usd.values()), abs=1e-6)
    assert result["entries"][f"run:{summary.run_id}"]["kind"] == "trace"  # the larger, recomputed value wins
    assert 11 in result["covered_artifact_ids"]


def test_the_repository_snapshot_is_a_valid_empty_ledger():
    data = ledger.read_ledger_file(REAL_REPO / ledger.SNAPSHOT_PATH)
    assert data["entries"] == {} and data["covered_artifact_ids"] == []


def test_check_allows_up_to_the_hard_limit_and_refuses_above_it():
    base = {"total_usd": 40.0, "hard_limit_usd": 50.0, "complete": True, "problems": [], "active_runs": []}
    assert ledger.check(base, 10.0, what="x")["allowed"]
    refused = ledger.check(base, 10.01, what="configs/dev_baseline.toml")
    assert not refused["allowed"] and "exceeds the hard limit" in refused["reasons"][0]
    busy = ledger.check({**base, "active_runs": [{"id": 7, "name": "baseline", "status": "in_progress"}]}, 0.1, what="x")
    assert not busy["allowed"] and "in progress" in busy["reasons"][0]


def test_cli_compute_and_check_exit_5_when_refused(tmp_path, capsys):
    repo, run_dir, summary = make_run(tmp_path)
    out = tmp_path / "ledger.json"
    assert ledger.main(["compute", str(tmp_path / "runs"), "--repo-root", str(repo), "--json", str(out), "--no-snapshot",
                        "--markdown", str(tmp_path / "l.md")]) == 0
    assert ledger.main(["check", "--ledger", str(out), "--cap", "1.0", "--repo-root", str(repo)]) == 0
    assert ledger.main(["check", "--ledger", str(out), "--cap", "50.0", "--repo-root", str(repo),
                        "--markdown", str(tmp_path / "l.md")]) == ledger.EXIT_REFUSED
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["check"]["allowed"] is False
    assert "REFUSED" in (tmp_path / "l.md").read_text(encoding="utf-8")
    assert "REFUSED by the budget ledger" in capsys.readouterr().out


def test_caps_of_configs_and_stages_are_the_caps_the_plan_lists():
    import tomllib

    plan = tomllib.loads((REAL_REPO / "configs" / "budget.toml").read_text(encoding="utf-8"))
    for entry in plan["run"]:
        if entry["config"]:
            assert ledger.config_cap(entry["config"], repo_root=REAL_REPO) * entry["invocations"] == pytest.approx(entry["cap_usd"], abs=0.006)
    assert ledger.config_cap("configs/dev_smoke.toml", model="zai-mid", repo_root=REAL_REPO) == pytest.approx(0.2)
    from binding_gate.pipeline import STAGES

    for stage, configs in STAGES.items():
        assert ledger.stage_cap(stage, repo_root=REAL_REPO) == pytest.approx(
            sum(ledger.config_cap(c, repo_root=REAL_REPO) for c in configs))
    # PR 17: GRAB-RAG (cap 0.40) left all-dev; the three SEC dev runs (0.30 + 1.70 + 0.30) joined it as layer2-dev
    assert ledger.stage_cap("all-dev", repo_root=REAL_REPO) == pytest.approx(19.20)
    assert ledger.stage_cap("layer2-dev", repo_root=REAL_REPO) == pytest.approx(2.30)
    for free in ("check-secrets", "sec-dataset-prepare"):
        assert ledger.stage_cap(free, repo_root=REAL_REPO) == 0


@pytest.mark.parametrize("name,role", [
    ("runs-openai-low", "runs"),
    ("dev-rest--dev_gated--runs-openai-low", "runs"),
    ("dev-rest--dev_gated--runs-openai-low-attempt2", "runs"),
    ("layer3-dev--dev_live--runs-encrypted-all-models", "runs-encrypted"),
    ("runs-encrypted-all-models", "runs-encrypted"),
    ("layer3-dev--dev_live--layer3-summary-all-models", "layer3-summary"),
    ("dev-rest--decomposer_eval_dev--decomposer-eval", "decomposer-eval"),
    ("dev-rest--dev_llm_judge--rescore-attempt3", "rescore"),
    ("ledger", "ledger"),
    ("pipeline--all-dev--ledger", "ledger"),
    ("dev-rest--dev_gated--ledger-attempt2", "ledger"),
    ("baseline-report", None),
    ("dev-rest--dev_gated--baseline-report", None),
    ("layer3-dev--dev_live--snapshot-manifest-all-models", None),
    ("dev-baseline--decision", None),
    ("dev-rest--analysis", None),
    ("smoke--dev_smoke--usage-check", None),
    ("check-secrets--report", None),
])
def test_artifact_roles_follow_the_naming_scheme(name, role):
    assert ledger.artifact_role(name) == role


def test_the_summary_name_of_an_encrypted_artifact():
    assert ledger.summary_name_for("layer3-dev--dev_live--runs-encrypted-all-models") == "layer3-dev--dev_live--layer3-summary-all-models"
    assert ledger.summary_name_for("runs-encrypted-all-models-attempt2") == "layer3-summary-all-models-attempt2"


# --------------------------------------------------------------------------- #
# Fetching through a fake GitHub API
# --------------------------------------------------------------------------- #


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class FakeGitHub:
    """Serves /actions/artifacts (two pages), /actions/runs/<id>, /actions/runs?status=, and zip downloads via redirects."""

    def __init__(self, artifacts: list[dict], zips: dict[int, bytes], runs: dict[int, dict], active: list[dict] | None = None):
        self.artifacts, self.zips, self.runs, self.active = artifacts, zips, runs, active or []
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, method, url, headers):
        self.requests.append((url, dict(headers)))
        from urllib.parse import parse_qs, urlsplit

        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        path = parts.path
        if parts.netloc == "storage.test":
            return 200, {}, self.zips[int(path.strip("/").split("/")[-1])]
        if path.endswith("/actions/artifacts"):
            page, per = int(query["page"]), int(query["per_page"])
            items = self.artifacts[(page - 1) * per: page * per]
            return 200, {}, json.dumps({"total_count": len(self.artifacts), "artifacts": items}).encode()
        if "/actions/artifacts/" in path and path.endswith("/zip"):
            artifact_id = int(path.split("/")[-2])
            return 302, {"location": f"https://storage.test/blob/{artifact_id}"}, b""
        if path.endswith("/actions/runs"):
            runs = [r for r in self.active if r["status"] == query["status"]]
            return 200, {}, json.dumps({"total_count": len(runs), "workflow_runs": runs}).encode()
        if "/actions/runs/" in path:
            run_id = int(path.rsplit("/", 1)[-1])
            return (200, {}, json.dumps(self.runs[run_id]).encode()) if run_id in self.runs else (404, {}, b"{}")
        return 404, {}, b"{}"


def _artifact(artifact_id: int, name: str, run_id: int, *, expired: bool = False) -> dict:
    return {"id": artifact_id, "name": name, "expired": expired, "size_in_bytes": 10, "created_at": f"2026-11-{artifact_id % 28 + 1:02d}T00:00:00Z",
            "archive_download_url": f"https://api.github.test/repos/o/r/actions/artifacts/{artifact_id}/zip",
            "workflow_run": {"id": run_id}}


def _run_zip(run_dir: Path) -> bytes:
    return _zip({f"{run_dir.name}/{p.name}": p.read_bytes() for p in run_dir.iterdir() if p.is_file()})


def test_fetch_reads_trusted_artifacts_skips_covered_ones_and_reports_unknown_spend(tmp_path):
    repo, run_dir, summary = make_run(tmp_path)
    trusted = {"event": "workflow_dispatch", "head_repository": {"full_name": "o/r"}, "status": "completed"}
    runs = {100: trusted, 200: trusted, 300: {"event": "pull_request", "head_repository": {"full_name": "fork/r"}}, 400: trusted}
    earlier = {"ledger_version": 1, "covered_artifact_ids": [2, 3],
               "entries": {"run:old-run": {"usd": 1.25, "kind": "trace", "source": "artifact 2"}}}
    artifacts = [
        _artifact(1, "dev-baseline--dev_baseline--runs-openai-low", 100),
        _artifact(2, "runs-google-low", 100),                       # covered by the earlier ledger: not downloaded
        _artifact(3, "runs-zai-low", 100, expired=True),             # expired but covered
        _artifact(4, "runs-openai-mid", 200, expired=True),          # expired, NOT covered: unknown spend
        _artifact(5, "ledger", 200),
        _artifact(6, "runs-openai-low", 300),                        # a fork's pull request: ignored
        _artifact(7, "layer3-dev--dev_live--runs-encrypted-all-models", 400),  # no summary: unknown spend
        _artifact(8, "baseline-report", 100),                        # no spend
    ] + [_artifact(1000 + i, f"x{i}--analysis", 100) for i in range(100)]  # forces a second page
    zips = {1: _run_zip(run_dir), 5: _zip({"ledger.json": json.dumps(earlier).encode()}), 6: b"never downloaded"}
    api = GitHubAPI("o/r", "SECRET-TOKEN", "https://api.github.test",
                    FakeGitHub(artifacts, zips, runs, active=[{"id": 999, "event": "workflow_dispatch", "status": "in_progress", "name": "baseline"},
                                                              {"id": 555, "event": "workflow_dispatch", "status": "in_progress", "name": "me"},
                                                              {"id": 777, "event": "push", "status": "queued", "name": "tests"}]))
    index = ledger.fetch(api, tmp_path / "inputs", current_run_id=555)
    status = {a["id"]: a["status"] for a in index["artifacts"]}
    assert status[1] == "read" and status[2] == "covered" and status[3] == "expired-covered"
    assert status[4] == "expired-uncovered" and status[5] == "read" and status[6] == "ignored-untrusted-run"
    assert status[7] == "encrypted-without-summary" and status[8] == "no-spend"
    assert [r["id"] for r in index["active_runs"]] == [999]
    # the redirect to storage is followed WITHOUT the token
    storage = [h for u, h in api.transport.requests if "storage.test" in u]
    assert storage and all("Authorization" not in h for h in storage)
    assert all(h.get("Authorization") == "Bearer SECRET-TOKEN" for u, h in api.transport.requests if "api.github.test" in u)

    result = ledger.compute(repo_root=repo, inputs=tmp_path / "inputs", snapshot=None)
    assert not result["complete"] and len(result["problems"]) == 2
    assert result["total_usd"] == pytest.approx(1.25 + sum(summary.spend_usd.values()), abs=1e-6)
    assert {1, 2, 3} <= set(result["covered_artifact_ids"]) and 4 not in result["covered_artifact_ids"]
    assert "SECRET-TOKEN" not in json.dumps(result)
    assert not ledger.check(result, 0.1, what="x")["allowed"]


def test_an_encrypted_run_with_its_summary_is_accounted(tmp_path):
    repo, run_dir, summary = make_run(tmp_path)
    trusted = {"event": "workflow_dispatch", "head_repository": {"full_name": "o/r"}}
    entry = ledger.trace_run_spend(run_dir, repo)
    layer3 = {"summary_version": 1, "runs": [{"run_id": summary.run_id, "spend": {"key": entry.key, **entry.to_dict()}}]}
    artifacts = [_artifact(1, "layer3-dev--dev_live--runs-encrypted-all-models", 10),
                 _artifact(2, "layer3-dev--dev_live--layer3-summary-all-models", 10)]
    api = GitHubAPI("o/r", "t", "https://api.github.test",
                    FakeGitHub(artifacts, {2: _zip({"layer3_summary.json": json.dumps(layer3).encode()})}, {10: trusted}))
    index = ledger.fetch(api, tmp_path / "inputs")
    assert [a["status"] for a in index["artifacts"]] == ["encrypted-spend-in-summary", "read"]
    result = ledger.compute(repo_root=repo, inputs=tmp_path / "inputs", snapshot=None)
    assert result["complete"] and result["total_usd"] == pytest.approx(entry.usd, abs=1e-6)
    assert result["entries"][entry.key]["kind"] == "layer3_summary"


def test_trusted_runs_and_safe_extraction(tmp_path):
    assert is_trusted_run({"event": "workflow_dispatch", "head_repository": {"full_name": "o/r"}}, "o/r")
    assert not is_trusted_run({"event": "pull_request", "head_repository": {"full_name": "o/r"}}, "o/r")
    assert not is_trusted_run({"event": "workflow_dispatch", "head_repository": {"full_name": "x/r"}}, "o/r")
    from binding_gate.ghapi import GitHubAPIError

    with pytest.raises(GitHubAPIError):
        extract_zip(_zip({"../escape.txt": b"x"}), tmp_path / "x")
    assert extract_zip(_zip({"a/b.txt": b"x"}), tmp_path / "ok") == ["a/b.txt"]


def test_fetch_from_the_environment_needs_the_token(monkeypatch):
    from binding_gate.ghapi import GitHubAPIError

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    with pytest.raises(GitHubAPIError, match="GITHUB_TOKEN"):
        GitHubAPI.from_environment()
