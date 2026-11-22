"""Layer 3 inside the real runner and CLI: zero network, every paid call priced and capped."""

from __future__ import annotations

import json

import pytest

import live_fakes as lf
import run_helpers as h
import sealqa_fixture as fx
from binding_gate import cli, runner
from binding_gate.arms.gated import clear_decomposition_memo
from binding_gate.config import parse_config
from binding_gate.research import search as search_mod
from binding_gate.research.session import clear_sessions
from binding_gate.research.snapshots import SnapshotStore, verify_run
from binding_gate.scorers.sealqa_answer import analyse_layer3, layer3_outcome
from binding_gate.secrets import SecretError
from binding_gate.trace import read_traces

Q_UNKNOWN = "Which city hosted the 2040 Unknown Games?"  # dev split; the search finds nothing
LIVE_PRICES = """
[[price]]
model_id = "serper/google"
input_per_mtok = 0.0
output_per_mtok = 0.0
per_call_usd = 0.025
source = "test"
accessed = "2026-11-22"
[[price]]
model_id = "binding-gate/fetch@1"
input_per_mtok = 0.0
output_per_mtok = 0.0
source = "test"
accessed = "2026-11-22"
"""
ANSWER_COST = (1000 * 1.0 + 100 * 2.0) / 1_000_000  # LiveFakeModel usage at fake-cheap prices
DECOMPOSE_COST = (200 * 1.0 + 50 * 2.0) / 1_000_000


def make(tmp_path, monkeypatch, *, max_usd=5.0, prices=h.PRICES + LIVE_PRICES, key="serp-test-key", repeats=2):
    clear_sessions()
    clear_decomposition_memo()
    lf.MODEL_CALLS.clear()
    lf.DECOMPOSE_CALLS.clear()
    repo = h.make_repo(tmp_path)
    (repo / "configs" / "prices.toml").write_text(prices, encoding="utf-8")
    hard = [
        fx.row(0, question=lf.Q_EXAMPLE, answer="Northbridge"),
        fx.row(1, question=lf.Q_SAMPLE, answer="Southport"),  # test split: never run
        fx.row(2, question=Q_UNKNOWN, answer="Nowhere"),
    ]
    options = fx.write(tmp_path / "sealqa", monkeypatch, hard, [dict(hard[0])])
    raw = {
        "config_version": 1,
        "name": "live-test",
        "split": "dev",
        "repeats": repeats,
        "dataset": {"name": "sealqa", "options": options},
        "models": [{"name": "fake_live_model", "label": "good", "options": {"model_id": "fake-cheap"}}],
        "arms": lf.arm_specs(),
        "scorers": [{"name": "sealqa_answer"}],
        "runner": {"concurrency": 2, "max_usd": max_usd},
    }
    web, serp = lf.install(monkeypatch, key=key)
    return repo, parse_config(raw), raw, web, serp


@pytest.fixture(autouse=True)
def _clean():
    yield
    clear_sessions()
    clear_decomposition_memo()


def test_a_live_run_end_to_end_with_snapshots_scores_and_priced_auxiliary_calls(tmp_path, monkeypatch):
    repo, config, _raw, web, serp = make(tmp_path, monkeypatch)
    summary = runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None)
    records = list(read_traces(summary.run_dir))
    assert summary.closed and summary.error_records == 0 and len(records) == 2 * 2 * 2
    # Every query searched once per run (shared by arms and repeats); the test-split question never.
    assert sorted(serp.queries) == sorted([lf.Q_EXAMPLE, lf.RESEARCH_QUERY_EXAMPLE, Q_UNKNOWN])
    example = [r for r in records if r.item_id == fx_gid(lf.Q_EXAMPLE)]
    unknown = [r for r in records if r.item_id == fx_gid(Q_UNKNOWN)]
    assert {(r.arm, r.scores[0].label) for r in example} == {("no_gate", "incorrect"), ("gated_research", "correct")}
    assert {(r.arm, r.scores[0].label) for r in unknown} == {("no_gate", "abstained"), ("gated_research", "abstained")}
    assert {layer3_outcome(r) for r in example} == {"wrong_entity", "target_grounded"}

    # Spend = answer calls (first answer per repeat for both questions + re-search answer per repeat)
    # + one decomposition + three searches; fetches are free; nothing is unpriced.
    assert summary.unpriced_clients == []
    expected = 6 * ANSWER_COST + DECOMPOSE_COST + 3 * 0.025
    assert summary.spend_usd["good"] == pytest.approx(expected)

    # Snapshots: stored, verifiable against every trace reference, publish manifest written.
    counts = verify_run(summary.run_dir)
    assert counts["evidence_refs_checked"] > 0 and counts["search"] == 3
    manifest = json.loads((summary.run_dir / "snapshots" / "publish_manifest.json").read_text())
    assert {p["url"] for p in manifest["pages"]} == {lf.PAGE_A, lf.PAGE_BLOCKED, lf.PAGE_GONE, lf.PAGE_D}
    assert "Northbridge" not in json.dumps(manifest)

    # Statistics with the layer-3 rule: M3 and M4 come from the deterministic scorer.
    analysis = analyse_layer3([summary.run_dir], resamples=50)
    assert analysis["parameters"]["correct_rule_is_placeholder"] is False


def to_toml(raw: dict) -> str:
    """Any JSON-shaped config as TOML (inline tables and arrays; every key quoted)."""

    def value(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float, str)):
            return json.dumps(v)
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        return "{" + ", ".join(f"{json.dumps(k)} = {value(x)}" for k, x in v.items()) + "}"

    return "".join(f"{json.dumps(k)} = {value(v)}\n" for k, v in raw.items())


def fx_gid(question):
    from binding_gate.datasets.sealqa import group_id_of

    return group_id_of(question)


def test_dry_run_estimates_model_search_fetch_and_upper_bounds_without_any_network(tmp_path, monkeypatch):
    repo, config, _raw, web, serp = make(tmp_path, monkeypatch, key=None)
    report = runner.dry_run(config, repo_root=repo)
    assert serp.urls == [] and web.requests == [] and lf.MODEL_CALLS == []
    assert report.n_items == 2 and report.requests_per_repeat == 2  # both arms share one first answer request
    rows = {a.role: a for a in report.auxiliary["good"]}
    assert set(rows) == {"search", "fetch", "re-answer", "decomposer"}
    extra = 2 * 2 * 1  # items x repeats x (max_attempts - 1)
    assert (rows["search"].calls_low, rows["search"].calls_high) == (2, 2 + extra)
    assert (rows["fetch"].calls_low, rows["fetch"].calls_high) == (2 * 5, 2 * 10 + extra * 10)
    assert (rows["re-answer"].calls_low, rows["re-answer"].calls_high) == (0, extra)
    assert (rows["decomposer"].calls_low, rows["decomposer"].calls_high) == (0, 2)
    assert rows["search"].cost_low == pytest.approx(2 * 0.025) and rows["search"].cost_high == pytest.approx(6 * 0.025)
    assert rows["fetch"].cost_high == 0.0 and rows["re-answer"].input_tokens_high > 0
    # Worst-case pages: 5 documents of 8,000 characters each in the first answer request.
    assert report.input_chars_per_repeat > 2 * 5 * 8000
    total = report.model_total("good", "cost_low")
    assert total == pytest.approx(report.models[0].cost_low + sum(a.cost_low for a in report.auxiliary["good"]))
    text = report.format()
    assert "AUXILIARY CALLS" in text and "serper/google" in text and "TOTAL per model job" in text
    assert json.loads(json.dumps(report.to_dict()))["auxiliary"]["good"]


def test_the_low_estimate_including_searches_must_fit_under_the_cap(tmp_path, monkeypatch):
    repo, config, _raw, web, serp = make(tmp_path, monkeypatch, max_usd=0.05)
    with pytest.raises(runner.RunnerError, match="auxiliary calls included"):
        runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None)
    assert serp.queries == [] and web.requests == []


def test_a_search_client_without_a_price_is_refused_before_any_call(tmp_path, monkeypatch):
    repo, config, _raw, web, serp = make(tmp_path, monkeypatch, prices=h.PRICES)
    with pytest.raises(runner.RunnerError, match="no price for search"):
        runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None)
    assert serp.urls == []


def test_missing_search_key_is_reported_and_stops_a_run_before_any_call(tmp_path, monkeypatch):
    repo, config, _raw, web, serp = make(tmp_path, monkeypatch, key=None)
    assert runner.missing_keys(config, "all") == [("search search (arm no_gate)", "SERPER_API_KEY")]
    with pytest.raises(SecretError):
        runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None)
    assert serp.urls == []


def test_preflight_covers_the_search_account_and_the_decomposer_client(tmp_path, monkeypatch):
    repo, config, _raw, web, serp = make(tmp_path, monkeypatch)
    targets = [where for where, _client in runner.preflight_targets(config)]
    assert targets == ["good", "search search (arm no_gate)", "fetch fetch (arm no_gate)", "decomposer decomposer (arm gated_research)"]
    results = runner.preflight(config)
    # PR 27: Serper has no account API; its preflight is ONE real search with a fixed query (one credit)
    assert [r.ok for r in results] == [True, True, True] and serp.queries == []
    assert serp.preflights == [search_mod.SERPER_PREFLIGHT_QUERY]


def test_comma_separated_model_selection(tmp_path, monkeypatch):
    repo, config, raw, web, serp = make(tmp_path, monkeypatch)
    raw["models"].append({"name": "fake_live_model", "label": "other", "options": {"model_id": "fake-dear"}})
    config = parse_config(raw)
    assert [s.label for s in runner.select_models(config, "good,other")] == ["good", "other"]
    assert [s.label for s in runner.select_models(config, "other")] == ["other"]
    with pytest.raises(runner.RunnerError, match="nope"):
        runner.select_models(config, "good,nope")


def test_requirements_dataset_pin_and_prepare_commands(tmp_path, monkeypatch, capsys):
    repo, config, raw, web, serp = make(tmp_path, monkeypatch)
    assert runner.run_requirements(config) == {
        "extras": ["sealqa"],
        "secrets": ["SERPER_API_KEY"],
        "layer": 3,
        "private_outputs": True,
        "single_job": True,
        "baseline_report": False,
        "faithfulness_checker": False,
        "faithfulness_weights": "",
        "seed_runs": [],
    }
    path = tmp_path / "live.toml"
    path.write_text(to_toml(raw), encoding="utf-8")
    assert cli.main(["requirements", "--config", str(path)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "extras=sealqa\n" in out and "private_outputs=true\n" in out and "single_job=true\n" in out
    assert cli.main(["dataset-pin", "--config", str(path)]) == cli.EXIT_OK
    assert "method=prepare" in capsys.readouterr().out
    assert cli.main(["models", "--config", str(path), "--available"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out) == ["good"]


def test_cli_preflight_exits_4_when_only_the_search_key_is_missing(tmp_path, monkeypatch, capsys):
    repo, config, raw, web, serp = make(tmp_path, monkeypatch, key=None)
    path = tmp_path / "live.toml"
    path.write_text(to_toml(raw), encoding="utf-8")
    assert cli.main(["preflight", "--config", str(path)]) == cli.EXIT_NO_KEY
    out = capsys.readouterr().out
    assert "preflight good: OK" in out and "SKIPPED - repository secret / environment variable SERPER_API_KEY" in out
    assert cli.main(["run", "--config", str(path), "--confirm-spend", "--repo-root", str(repo)]) == cli.EXIT_NO_KEY


def test_snapshot_cli_verifies_a_run(tmp_path, monkeypatch, capsys):
    repo, config, _raw, web, serp = make(tmp_path, monkeypatch, repeats=1)
    summary = runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None)
    from binding_gate.research import snapshots

    assert snapshots.main(["verify", str(summary.run_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["evidence_refs_checked"] > 0
    store = SnapshotStore.for_run(summary.run_dir)
    page = next(p for p in store.pages() if p.ok)
    blob = store.blobs / page.text_sha256
    blob.chmod(0o644)
    blob.write_text("tampered")
    assert snapshots.main(["verify", str(summary.run_dir)]) == 1


def test_cli_analyze_uses_the_layer3_rule_for_sealqa_runs_and_refuses_a_mix(tmp_path, monkeypatch, capsys):
    repo, config, _raw, web, serp = make(tmp_path, monkeypatch, repeats=1)
    summary = runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None)
    capsys.readouterr()
    out = tmp_path / "analysis"
    assert cli.main(["analyze", str(summary.run_dir), "--out", str(out), "--no-figures"]) == cli.EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed["outcome_rule"] == "sealqa_answer (layer 3)"
    written = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    assert written["parameters"]["correct_rule_is_placeholder"] is False  # not the provenance placeholder
    direct = analyse_layer3([summary.run_dir])
    assert written["models"] == json.loads(json.dumps(direct["models"]))  # same as the scorer's own command

    # A layer-1 run directory in the same call is refused with a pointer to the layer-3 command.
    repo1, config1, _raw1 = h.setup(tmp_path / "layer1")
    layer1 = runner.execute_run(config1, repo_root=repo1, runs_dir=tmp_path / "runs1", confirm_spend=True, log=lambda m: None)
    capsys.readouterr()
    assert cli.main(["analyze", str(summary.run_dir), str(layer1.run_dir), "--no-figures"]) == cli.EXIT_FAILURE
    err = capsys.readouterr().err
    assert "analyse each dataset separately" in err and "binding_gate.scorers.sealqa_answer analyze" in err
    assert not (summary.run_dir / "analysis").exists()

    # A layer-1 run alone still uses the provenance rule.
    assert cli.main(["analyze", str(layer1.run_dir), "--no-figures"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["outcome_rule"] == "provenance"
