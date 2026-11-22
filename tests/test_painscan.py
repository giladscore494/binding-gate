"""tools/painscan: plan generation, scoring and the offline run/report path (no network)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "painscan"))
import painscan  # noqa: E402


def test_plan_is_fixed_and_unique():
    plan = painscan.load_plan()
    qs = painscan.build_queries(plan)
    assert len(qs) == 810
    assert len({q.qid for q in qs}) == len(qs)
    assert len(qs) <= plan["serper"]["max_queries_hard"]
    assert all(q.tbs == "" for q in qs if q.kind == "competition")
    assert {q.tbs for q in qs if q.window == "recent"} == {plan["serper"]["recent_tbs"]}
    assert all("site:reddit.com" in q.q for q in qs if q.source == "reddit")


def test_every_domain_has_all_feasibility_attributes():
    for d in painscan.load_plan()["domain"]:
        for k in ("regulated", "smb_reachable", "source_of_truth", "bigtech_building", "israel_edge"):
            assert isinstance(d[k], bool), (d["id"], k)
        assert d["reason"]


@pytest.mark.parametrize("text,relevant,cost,demand", [
    ("Our AI receptionist booked the wrong day and we lost a customer", True, True, False),
    ("AI bookkeeping is great", False, False, False),
    ("Is there a tool to check what my AI agent wrote?", False, False, True),
    ("ChatGPT made up a $400 refund policy", True, True, False),
])
def test_classify(text, relevant, cost, demand):
    c = painscan.classify({"title": text, "snippet": ""})
    assert (c["relevant"], c["cost"], c["demand"]) == (relevant, cost, demand)


def test_offline_run_and_report(tmp_path):
    plan = painscan.load_plan()
    qs = [q for q in painscan.build_queries(plan) if q.domain in ("voice_receptionist", "coding")]

    def fake_search(q, plan, key):
        if q.kind == "competition":
            return {"organic": [{"title": "x", "link": f"https://vendor{i}.com/{q.qid}", "snippet": ""} for i in range(5)], "credits": 99}
        n = 3 if q.window == "recent" else 6
        return {"organic": [{"title": f"AI agent wrong answer cost us $500 {i}", "link": f"https://r.com/{q.domain}/{q.window}/{q.qid}/{i}",
                             "snippet": "frustrating", "date": "2 weeks ago"} for i in range(n)]}

    stats = painscan.run(plan, qs, tmp_path, "k", 10_000, search=fake_search)
    assert stats["ok"] == len(qs) and stats["failed"] == 0 and not stats["quota_exhausted"]
    # resume: nothing is searched twice
    again = painscan.run(plan, qs, tmp_path, "k", 10_000, search=fake_search)
    assert again["attempted"] == 0
    rows = painscan.read_rows(tmp_path)
    metrics = painscan.aggregate(rows, plan)
    m = metrics["voice_receptionist"]
    assert m["recent_relevant"] == 11 * 3 and m["cost_share"] == 1.0 and m["competitors"] == 5 and m["ranked"]
    assert metrics["bookkeeping"]["recent_relevant"] == 0 and not metrics["bookkeeping"]["ranked"]
    painscan.write_report(metrics, plan, tmp_path, {"plan_hash": "h", "rows": len(rows), "queries_done": len(qs)})
    report = (tmp_path / "report.md").read_text()
    assert "voice_receptionist" in report and "snippet" not in json.loads((tmp_path / "report.json").read_text())["metrics"]["coding"]


def test_quota_exhaustion_stops_the_run(tmp_path):
    plan = painscan.load_plan()
    qs = painscan.build_queries(plan)[:40]

    def broke(q, plan, key):
        raise painscan.QuotaExhausted("HTTP 400: Not enough credits")

    stats = painscan.run(plan, qs, tmp_path, "k", 40, search=broke)
    assert stats["quota_exhausted"] and stats["ok"] == 0 and stats["attempted"] <= plan["serper"]["workers"] * 4
