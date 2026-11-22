"""PR 15: the unencrypted layer-3 summary carries costs and outcomes but never page text or quotes."""

from __future__ import annotations

import json

import pytest

import live_fakes as lf
import run_helpers as h
import sealqa_fixture as fx
from binding_gate import ledger, runner
from binding_gate.arms.gated import clear_decomposition_memo
from binding_gate.config import parse_config
from binding_gate.layer3_summary import ALLOWED_FIELDS, MAX_VALUE_CHARS, SUMMARY_NAME, build_summary, main, sanitise
from binding_gate.research.session import clear_sessions
from binding_gate.trace import read_traces
from test_ledger import BUDGET
from test_live_runner import LIVE_PRICES, Q_UNKNOWN

PAGE_TEXT_SENTINELS = (
    "drew large crowds", "after a long bid", "held every four years", "Home | News", "var x = 1",
    "snippet 1", "result 1", "should-not-leak", "serp-test-key", "secret",
)
"""Text that exists only in fetched pages, search results or secrets of the fake world."""


@pytest.fixture(autouse=True)
def _clean():
    clear_sessions()
    clear_decomposition_memo()
    yield
    clear_sessions()
    clear_decomposition_memo()


@pytest.fixture
def live_run(tmp_path, monkeypatch):
    lf.MODEL_CALLS.clear()
    lf.DECOMPOSE_CALLS.clear()
    repo = h.make_repo(tmp_path)
    (repo / "configs" / "prices.toml").write_text(h.PRICES + LIVE_PRICES, encoding="utf-8")
    (repo / "configs" / "budget.toml").write_text(BUDGET, encoding="utf-8")
    hard = [fx.row(0, question=lf.Q_EXAMPLE, answer="Northbridge"), fx.row(1, question=lf.Q_SAMPLE, answer="Southport"),
            fx.row(2, question=Q_UNKNOWN, answer="Nowhere")]
    options = fx.write(tmp_path / "sealqa", monkeypatch, hard, [dict(hard[0])])
    raw = {
        "config_version": 1, "name": "live-test", "split": "dev", "repeats": 1,
        "dataset": {"name": "sealqa", "options": options},
        "models": [{"name": "fake_live_model", "label": "good", "options": {"model_id": "fake-cheap"}}],
        "arms": lf.arm_specs(), "scorers": [{"name": "sealqa_answer"}], "runner": {"concurrency": 2, "max_usd": 5.0},
    }
    lf.install(monkeypatch)
    summary = runner.execute_run(parse_config(raw), repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None)
    return repo, summary


def _walk(level: str, value, path: str = "") -> None:
    """Every key at every level of the summary is in ALLOWED_FIELDS (the records, attempts, evidence, verdicts, pages)."""
    assert isinstance(value, dict), path
    extra = set(value) - ALLOWED_FIELDS[level]
    assert not extra, f"{path}: keys {extra} are not allowed at level {level!r}"
    children = {"summary": {"runs": "run"}, "run": {"records": "record", "pages": "page"}, "record": {"attempts": "attempt"},
                "attempt": {"evidence": "evidence"}, "verdict": {"checks": "check"}}.get(level, {})
    for key, child in children.items():
        for i, entry in enumerate(value.get(key) or []):
            _walk(child, entry, f"{path}.{key}[{i}]")
    if level == "attempt" and value.get("verdict") is not None:
        _walk("verdict", value["verdict"], f"{path}.verdict")


def test_the_summary_has_costs_outcomes_and_hashes_but_no_page_text_and_no_quotes(live_run, tmp_path):
    repo, run = live_run
    records = list(read_traces(run.run_dir))
    quotes = {a.answer.quote for r in records for a in r.attempts if a.answer.quote}
    assert quotes  # the fake world produced answered attempts with quotes
    summary = build_summary([run.run_dir], repo_root=repo)
    text = json.dumps(summary, ensure_ascii=False)
    for sentinel in PAGE_TEXT_SENTINELS:
        assert sentinel not in text, sentinel
    for quote in quotes:
        assert quote not in text
    for record in records:
        for call in record.model_calls:  # no prompt, query or raw model output is copied (a fetch "request" is its URL)
            if call.purpose != "fetch":
                assert call.request.messages[-1].content not in text
            if call.response is not None and len(call.response.text) > 20:
                assert call.response.text not in text
    _walk("summary", summary)
    (run_summary,) = summary["runs"]
    assert run_summary["trace_count"] == len(records) and run_summary["searches"] > 0
    assert run_summary["pages"] and all(p["url"].startswith("https://") and len(p["text_sha256"] or "x" * 64) == 64 for p in run_summary["pages"])
    answered = [r for r in run_summary["records"] if r["final_status"] == "answered"]
    assert answered and all(r["final_quote_sha256"] and r["final_quote_chars"] > 0 for r in answered)
    assert all(len(r["final_value"]) <= MAX_VALUE_CHARS for r in answered)
    assert {r["score"]["label"] for r in run_summary["records"]} <= {"correct", "incorrect", "abstained", "unparseable"}
    gated = [a for r in run_summary["records"] for a in r["attempts"] if a["verdict"]]
    assert gated and all(c["reason"] is None or c["reason"].islower() for a in gated for c in a["verdict"]["checks"])
    assert "metrics" in summary and "metrics_error" not in summary


def test_the_ledger_reads_the_summary_spend_exactly_as_from_the_traces(live_run, tmp_path):
    repo, run = live_run
    out = tmp_path / "public"
    assert main([str(run.run_dir), "--out", str(out), "--repo-root", str(repo)]) == 0
    from_traces = ledger.trace_run_spend(run.run_dir, repo)
    (from_summary,) = ledger.summary_entries(out / SUMMARY_NAME)
    assert from_summary.key == from_traces.key == f"run:{run.run_id}"
    assert from_summary.usd == pytest.approx(from_traces.usd) == pytest.approx(sum(run.spend_usd.values()))
    assert from_summary.detail["by_purpose_usd"].get("search", 0) > 0  # searches priced at 0.025 in the test price file
    result = ledger.compute(repo_root=repo, directories=[out], snapshot=None)
    assert result["complete"] and result["total_usd"] == pytest.approx(from_traces.usd, abs=1e-6)


def test_long_values_are_cut_and_metrics_are_sanitised():
    assert sanitise({"a": "x" * 500, "b": [1, "ok", {"c": "y" * 121}], "x" * 200: 1}) == {"a": None, "b": [1, "ok", {"c": None}]}
