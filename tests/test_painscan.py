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


def test_dossier_answers_the_question_or_lists_interview_questions(tmp_path):
    plan = painscan.load_plan()
    qs = painscan.build_dossier_queries(plan, ["voice_receptionist", "recruiting"])
    assert len(qs) == 16 and all(q.kind.startswith("dossier:") for q in qs)

    def fake(q, plan, key):
        if q.domain == "recruiting":
            return {"organic": []}
        texts = {"dossier:who": "As a plumber my AI receptionist booked wrong slots",
                 "dossier:money": "AI receptionist mistake lost customers, cost us $1,200",
                 "dossier:pay": "AI receptionist pricing $199/month, worth paying for",
                 "dossier:existing_fail": "Switched from Smith.ai, still have to manually check every booking"}
        return {"organic": [{"title": texts[q.kind], "link": f"https://r.com/{q.qid}/{i}", "snippet": "plumber"} for i in range(3)]}

    painscan.run(plan, qs, tmp_path, "k", 100, search=fake)
    rows = painscan.read_rows(tmp_path)
    good = painscan.dossier(rows, plan, "voice_receptionist")
    assert good["verdict"] == "candidate" and good["who_top"][0][0] == "plumber" and not good["interview_questions"]
    bad = painscan.dossier(rows, plan, "recruiting")
    assert bad["verdict"] == "not yet" and bad["status"]["simple_fix"] == "no" and len(bad["interview_questions"]) == 4
    painscan.write_dossiers([good, bad], tmp_path)
    assert painscan.QUESTION in (tmp_path / "dossiers.md").read_text()


def test_custom_round_file_and_redirect_keys(tmp_path):
    plan = painscan.load_plan()
    f = tmp_path / "round_01.toml"
    f.write_text('round = 1\npurpose = "t"\n[[query]]\ncandidate = "c1"\nquestion = "money"\nsource = "reddit"\ntbs = "qdr:y"\ntext = "agent said done"\n'
                 '[[query]]\ncandidate = "c1"\nquestion = "pay"\nsource = "site:community.n8n.io"\ntbs = ""\ntext = "verify agent"\n')
    qs = painscan.load_round(f, plan)
    assert [q.q for q in qs] == ["agent said done site:reddit.com", "verify agent site:community.n8n.io"]
    assert qs[0].tbs == "qdr:y" and qs[1].tbs == "" and qs[0].kind == "v2:money"
    bad = tmp_path / "bad.toml"
    bad.write_text('round = 2\n[[query]]\ncandidate = "c"\ntext = "x"\ntbs = "qdr:zz"\n')
    with pytest.raises(ValueError):
        painscan.load_round(bad, plan)
    goto = "https://www.google.com/goto?url=CAESnQEBxCJF1yG&gsessionid=x"
    assert painscan.canonical_key(goto, "My n8n Agent LIED!") == "title:my n8n agent lied"
    assert painscan.canonical_key("https://www.reddit.com/r/n8n/x", "t") == "https://www.reddit.com/r/n8n/x"


def test_balance_check_never_fails_the_run():
    def boom(req, timeout):
        raise OSError("no network")
    assert painscan.serper_balance("k", opener=boom) is None

    class R:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False
    assert painscan.serper_balance("k", opener=lambda req, timeout: R(b'{"balance": 1376}')) == 1376
