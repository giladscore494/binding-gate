"""painscan: find recent, money-costing pain with AI agents in public discussions, through Serper.

Standalone (stdlib only). It does not import or change binding_gate. The study it serves is
pre-registered in tools/painscan/PREREGISTRATION.md; the plan (domains, templates, feasibility
attributes, scoring) is fixed in tools/painscan/plan.toml before any search.

Commands
    python tools/painscan/painscan.py plan                       # print query count and plan hash
    python tools/painscan/painscan.py run --mode smoke --out out  # 6 queries: checks the date filter
    python tools/painscan/painscan.py run --mode full --max-queries 900 --out out
    python tools/painscan/painscan.py report --in out --out out   # aggregates -> report.md / report.json

The API key is read from the SERPER_API_KEY environment variable only; it is never printed.
Raw results (titles, links, snippets) stay in the workflow artifact; report.md holds aggregates,
titles and links only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
PLAN_PATH = HERE / "plan.toml"
SERPER_URL = "https://google.serper.dev/search"

# ----------------------------------------------------------------------------------------------
# Plan


@dataclass(frozen=True)
class Query:
    qid: str
    domain: str
    kind: str      # pain | demand | competition
    source: str    # reddit | web | forums | hn
    window: str    # recent | baseline | none
    text: str      # the template filled with the domain phrase
    q: str         # the full query sent to Serper (with site: filters)
    tbs: str       # Serper/Google date filter ("" = none)


def load_plan(path: Path = PLAN_PATH) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def plan_hash(path: Path = PLAN_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _site_filter(source: str, plan: dict) -> str:
    if source == "reddit":
        return "site:reddit.com"
    if source == "hn":
        return "site:news.ycombinator.com"
    if source == "forums":
        return "(" + " OR ".join(f"site:{s}" for s in plan["forum_sites"]) + ")"
    return ""


def build_queries(plan: dict, recent_tbs: str | None = None) -> list[Query]:
    """All queries of the plan, in a fixed order (domain, template, text, window)."""
    s = plan["serper"]
    recent = recent_tbs or s["recent_tbs"]
    windows = {"both": [("recent", recent), ("baseline", s["baseline_tbs"])],
               "recent": [("recent", recent)], "baseline": [("baseline", s["baseline_tbs"])],
               "none": [("none", "")]}
    out: list[Query] = []
    for d in plan["domain"]:
        for t in plan["template"]:
            for text in t["texts"]:
                filled = text.format(p=d["phrase"])
                site = _site_filter(t["source"], plan)
                q = f"{filled} {site}".strip()
                for wname, tbs in windows[t["window"]]:
                    key = f"{d['id']}|{t['kind']}|{t['source']}|{wname}|{filled}"
                    qid = hashlib.sha1(key.encode()).hexdigest()[:12]
                    out.append(Query(qid, d["id"], t["kind"], t["source"], wname, filled, q, tbs))
    return out


def smoke_queries(plan: dict) -> list[Query]:
    """Six queries: one pain query under four date filters (does Serper honour qdr:m3?) + 2 others."""
    s = plan["serper"]
    phrase = "AI receptionist wrong site:reddit.com"
    out = []
    for name, tbs in [("m3", s["recent_tbs"]), ("m1", s["recent_tbs_fallback"]), ("y", s["baseline_tbs"]), ("none", "")]:
        out.append(Query(f"smoke-{name}", "voice_receptionist", "pain", "reddit", name, "AI receptionist wrong", phrase, tbs))
    out.append(Query("smoke-comp", "voice_receptionist", "competition", "web", "none", "AI receptionist software", "AI receptionist software", ""))
    out.append(Query("smoke-forums", "workflow_automation", "pain", "forums", "recent", "n8n AI agent wrong",
                     "n8n AI agent wrong " + _site_filter("forums", plan), s["recent_tbs"]))
    return out


# ----------------------------------------------------------------------------------------------
# Serper client


class QuotaExhausted(RuntimeError):
    pass


def serper_search(query: Query, plan: dict, api_key: str, *, opener=urllib.request.urlopen, retries: int = 3) -> dict:
    s = plan["serper"]
    body = {"q": query.q, "gl": s["gl"], "hl": s["hl"], "num": s["num"]}
    if query.tbs:
        body["tbs"] = query.tbs
    data = json.dumps(body).encode()
    last_err = ""
    for attempt in range(retries):
        req = urllib.request.Request(SERPER_URL, data=data, method="POST",
                                     headers={"X-API-KEY": api_key, "Content-Type": "application/json"})
        try:
            with opener(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")[:300]
            low = text.lower()
            if e.code in (400, 402, 403) and ("credit" in low or "quota" in low):
                raise QuotaExhausted(f"HTTP {e.code}: {text}") from None
            if e.code in (401,):
                raise RuntimeError(f"HTTP 401 (key rejected): {text}") from None
            last_err = f"HTTP {e.code}: {text}"
            time.sleep(2 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = f"network: {e}"
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"query {query.qid} failed after {retries} attempts: {last_err}")


def flatten(query: Query, resp: dict) -> list[dict]:
    rows = []
    for item in resp.get("organic", []) or []:
        link = item.get("link", "")
        rows.append({**asdict(query), "position": item.get("position"), "title": item.get("title", ""),
                     "link": link, "host": urlparse(link).netloc.lower().removeprefix("www."),
                     "snippet": item.get("snippet", ""), "date": item.get("date", "")})
    return rows


def run(plan: dict, queries: list[Query], out: Path, api_key: str, max_queries: int, *, search=serper_search) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "results.jsonl"
    done_path = out / "done.jsonl"
    done = set()
    if done_path.exists():
        done = {json.loads(l)["qid"] for l in done_path.read_text().splitlines() if l.strip()}
    cap = min(max_queries, plan["serper"]["max_queries_hard"])
    todo = [q for q in queries if q.qid not in done][: max(0, cap)]
    stats = {"planned": len(queries), "already_done": len(done), "attempted": 0, "ok": 0, "failed": 0,
             "quota_exhausted": False, "credits_reported": None, "errors": []}

    def one(q: Query):
        try:
            return q, search(q, plan, api_key), None
        except QuotaExhausted as e:
            return q, None, e
        except Exception as e:  # noqa: BLE001 - recorded, visible in the summary
            return q, None, e

    workers = plan["serper"]["workers"]
    with results_path.open("a", encoding="utf-8") as rf, done_path.open("a", encoding="utf-8") as df, \
            ThreadPoolExecutor(max_workers=workers) as pool:
        for i in range(0, len(todo), workers * 4):
            if stats["quota_exhausted"]:
                break
            for q, resp, err in pool.map(one, todo[i:i + workers * 4]):
                stats["attempted"] += 1
                if isinstance(err, QuotaExhausted):
                    stats["quota_exhausted"] = True
                    stats["errors"].append(f"{q.qid}: {err}")
                    continue
                if err is not None:
                    stats["failed"] += 1
                    stats["errors"].append(f"{q.qid}: {err}")
                    continue
                stats["ok"] += 1
                if resp.get("credits") is not None:
                    stats["credits_reported"] = resp.get("credits")
                for row in flatten(q, resp):
                    rf.write(json.dumps(row, ensure_ascii=False) + "\n")
                df.write(json.dumps({"qid": q.qid, "n": len(resp.get("organic", []) or [])}) + "\n")
    stats["errors"] = stats["errors"][:20]
    (out / f"run_stats_{int(time.time())}.json").write_text(json.dumps(stats, indent=1))
    return stats


# ----------------------------------------------------------------------------------------------
# Scoring (fixed before data; PREREGISTRATION.md section 4)

AGENT_RE = re.compile(r"\b(ai|a\.i\.|agent|agents|agentic|gpt|chatgpt|claude|gemini|copilot|llm|bot|chatbot|automation|n8n|zapier)\b", re.I)
PAIN_RE = re.compile(r"(wrong|mistake|hallucinat|made up|makes up|invent|fabricat|incorrect|inaccurate|broke|broken|"
                     r"deleted|wiped|lied|lying|error|errors|frustrat|nightmare|useless|unreliable|can'?t trust|"
                     r"double[- ]check|manually (check|fix|review)|wasted|disaster|screwed|messed up|garbage|fail)", re.I)
COST_RE = re.compile(r"(\$\s?\d|\d+\s?(k|grand)\b|\bhours?\b|\bdays? of\b|refund|chargeback|lost (a |the )?(customer|client|sale|deal|money|order)|"
                     r"\bfine[ds]?\b|penalt|lawsuit|sued|overcharg|cost (us|me)|revenue)", re.I)
DEMAND_RE = re.compile(r"(is there (a|any) (tool|way|app|service)|looking for (a )?(tool|way|solution)|any (tool|app)s? (that|to)|"
                       r"would pay|recommend (a|any) (tool|app)|how do (you|i) (check|verify|validate)|need a way)", re.I)
NON_VENDOR_HOSTS = {"reddit.com", "youtube.com", "medium.com", "linkedin.com", "en.wikipedia.org", "wikipedia.org", "quora.com",
                    "x.com", "twitter.com", "facebook.com", "news.ycombinator.com", "github.com", "forbes.com", "techcrunch.com",
                    "g2.com", "capterra.com", "getapp.com", "softwareadvice.com", "producthunt.com", "trustradius.com"}
LISTING_HOSTS = {"g2.com", "capterra.com", "getapp.com", "softwareadvice.com", "trustradius.com", "producthunt.com"}


def classify(row: dict) -> dict:
    text = f"{row.get('title', '')} {row.get('snippet', '')}"
    agent = bool(AGENT_RE.search(text))
    pain = len(PAIN_RE.findall(text))
    return {"agent": agent, "pain_hits": pain, "relevant": agent and pain >= 1,
            "cost": bool(COST_RE.search(text)), "demand": bool(DEMAND_RE.search(text))}


def _pct_ranks(values: dict[str, float]) -> dict[str, float]:
    """Percentile rank in [0, 1] (ties share the mean rank)."""
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        r = ((i + j) / 2) / (n - 1) if n > 1 else 0.5
        for k in range(i, j + 1):
            out[items[k][0]] = r
        i = j + 1
    return out


def aggregate(rows: list[dict], plan: dict) -> dict:
    domains = {d["id"]: d for d in plan["domain"]}
    agg = {did: {"recent_links": set(), "baseline_links": set(), "recent_cost": set(), "demand_links": set(),
                 "vendor_hosts": set(), "listing_hosts": set(), "examples": {}, "sources": {}} for did in domains}
    for r in rows:
        a = agg.get(r["domain"])
        if a is None:
            continue
        c = classify(r)
        link = r["link"]
        if r["kind"] == "pain" and c["relevant"]:
            if r["window"] == "recent":
                a["recent_links"].add(link)
                a["sources"][r["source"]] = a["sources"].get(r["source"], 0) + 1
                if c["cost"]:
                    a["recent_cost"].add(link)
                score = c["pain_hits"] + (2 if c["cost"] else 0)
                if link not in a["examples"] or a["examples"][link][0] < score:
                    a["examples"][link] = (score, r["title"], r.get("date", ""), r["source"])
            elif r["window"] == "baseline":
                a["baseline_links"].add(link)
        elif r["kind"] == "demand" and (c["demand"] or c["relevant"]):
            a["demand_links"].add(link)
        elif r["kind"] == "competition":
            host = r["host"]
            if host in LISTING_HOSTS:
                a["listing_hosts"].add(host)
            if host and host not in NON_VENDOR_HOSTS and not host.endswith(".reddit.com"):
                a["vendor_hosts"].add(host)

    metrics = {}
    for did, a in agg.items():
        recent = len(a["recent_links"])
        base = len(a["baseline_links"] | a["recent_links"])
        metrics[did] = {"recent_relevant": recent, "year_relevant": base,
                        "recency_share": round(recent / base, 3) if base else 0.0,
                        "cost_share": round(len(a["recent_cost"]) / recent, 3) if recent else 0.0,
                        "demand": len(a["demand_links"]), "competitors": len(a["vendor_hosts"]),
                        "listing_sites": len(a["listing_hosts"]), "recent_by_source": a["sources"]}
    # Composite (fixed rule): pct ranks of pain volume, recency, cost share, demand minus competition,
    # plus feasibility points. Domains under the evidence floor are listed but not ranked.
    floor = 10
    pr = {k: _pct_ranks({d: metrics[d][k] for d in metrics}) for k in ("recent_relevant", "recency_share", "cost_share", "demand", "competitors")}
    for did, m in metrics.items():
        d = domains[did]
        feas = (1.0 if d["source_of_truth"] else 0) + (1.0 if d["smb_reachable"] else 0) + (0.5 if d["israel_edge"] else 0) \
            - (2.0 if d["regulated"] else 0) - (1.0 if d["bigtech_building"] else 0)
        market = pr["recent_relevant"][did] + pr["recency_share"][did] + pr["cost_share"][did] + pr["demand"][did] - pr["competitors"][did]
        m["market_score"] = round(market, 3)
        m["feasibility"] = feas
        m["composite"] = round(market + feas, 3)
        m["ranked"] = m["recent_relevant"] >= floor
        m["examples"] = [{"title": t, "link": l, "date": dt, "source": s}
                         for l, (sc, t, dt, s) in sorted(a_ex(agg[did]).items(), key=lambda kv: -kv[1][0])[:8]]
    return metrics


def a_ex(a: dict) -> dict:
    return a["examples"]


def write_report(metrics: dict, plan: dict, out: Path, meta: dict) -> None:
    ranked = sorted((k for k in metrics if metrics[k]["ranked"]), key=lambda k: -metrics[k]["composite"])
    unranked = sorted((k for k in metrics if not metrics[k]["ranked"]), key=lambda k: -metrics[k]["recent_relevant"])
    (out / "report.json").write_text(json.dumps({"meta": meta, "metrics": metrics}, indent=1, ensure_ascii=False))
    phr = {d["id"]: d["phrase"] for d in plan["domain"]}
    lines = ["# painscan report", "", f"plan hash `{meta.get('plan_hash', '')[:16]}`; rows {meta.get('rows')}; "
             f"queries done {meta.get('queries_done')}", "",
             "Composite = pct-rank(recent pain) + pct-rank(recency share) + pct-rank(cost share) + pct-rank(demand) "
             "- pct-rank(competitors) + feasibility. Ranked only with >= 10 recent relevant results.", "",
             "| # | domain | recent | 12m | recency | cost share | demand | competitors | feas | composite |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for i, k in enumerate(ranked, 1):
        m = metrics[k]
        lines.append(f"| {i} | {k} ({phr[k]}) | {m['recent_relevant']} | {m['year_relevant']} | {m['recency_share']} | "
                     f"{m['cost_share']} | {m['demand']} | {m['competitors']} | {m['feasibility']} | {m['composite']} |")
    if unranked:
        lines += ["", "Below the evidence floor (not ranked): " + ", ".join(f"{k} ({metrics[k]['recent_relevant']})" for k in unranked)]
    lines += ["", "## Examples (recent, highest pain score)", ""]
    for k in ranked[:10]:
        lines.append(f"### {k}")
        for e in metrics[k]["examples"]:
            title = e["title"].replace("|", "/")[:140]
            lines.append(f"- [{title}]({e['link']}) {e['date']} ({e['source']})")
        lines.append("")
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")


def read_rows(path: Path) -> list[dict]:
    rows = []
    for f in sorted(path.rglob("results.jsonl")):
        rows += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    # de-duplicate identical (qid, link) pairs from resumed runs
    seen, out = set(), []
    for r in rows:
        key = (r["qid"], r["link"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ----------------------------------------------------------------------------------------------
# Stage 2: the question engine (PREREGISTRATION.md section 7)
#
# THE QUESTION, asked of every shortlisted problem:
#   "Which problem costs a specific group of people enough money that they would pay a solo
#    developer for a simple fix, and why do the existing solutions not satisfy them?"
# It is split into five sub-questions. Four are answered from search evidence by fixed rules;
# "simple_fix" comes from the plan's feasibility attributes. A sub-question without enough
# evidence is UNKNOWN, and every UNKNOWN becomes a question for a customer conversation.

QUESTION = ("Which problem costs a specific group of people enough money that they would pay a solo developer "
            "for a simple fix, and why do the existing solutions not satisfy them?")
SEGMENTS = ["plumber", "electrician", "hvac", "contractor", "roofer", "landscap", "dentist", "dental", "clinic", "chiropract",
            "med spa", "salon", "restaurant", "cafe", "bakery", "realtor", "real estate agent", "landlord", "property manager",
            "law firm", "lawyer", "attorney", "accountant", "bookkeeper", "cpa", "agency", "freelancer", "consultant",
            "shopify store", "ecommerce store", "amazon seller", "etsy seller", "dropship", "startup", "saas founder",
            "dispatcher", "freight broker", "trucking", "carrier", "warehouse", "manufacturer", "distributor", "wholesaler",
            "recruiter", "staffing", "insurance agent", "auto shop", "dealership", "gym", "school", "nonprofit", "developer"]
AMOUNT_RE = re.compile(r"\$\s?\d[\d,]*(\.\d+)?\s?(k|m)?\b(\s?(/|per|a)\s?(mo|month|year|yr|hour|hr|week|day|call|seat|user))?", re.I)
PRICE_RE = re.compile(r"\$\s?\d[\d,]*(\.\d+)?\s?(/|per|a)\s?(mo|month|year|yr|seat|user)\b|pricing|per month|/month|subscription", re.I)
WILLING_RE = re.compile(r"(would (gladly |happily )?pay|worth (paying|it|the money)|happy to pay|shut up and take my money|paying for)", re.I)
FAIL_RE = re.compile(r"(switched (from|to)|alternative to|alternatives|cancel+ed|not worth|doesn'?t (work|support|handle)|"
                     r"still (have|had|need) to|manually|spreadsheet|went back to|gave up|too expensive|overpriced|clunky|limited)", re.I)
WORKAROUND_RE = re.compile(r"(manually|spreadsheet|excel|double[- ]check|hire[ds]? (a|an)|virtual assistant|\bva\b|went back to|by hand|copy[- ]paste)", re.I)


def build_dossier_queries(plan: dict, domain_ids: list[str]) -> list[Query]:
    s = plan["serper"]
    tbs = {"baseline": s["baseline_tbs"], "recent": s["recent_tbs"], "none": ""}
    domains = {d["id"]: d for d in plan["domain"]}
    out = []
    for did in domain_ids:
        d = domains[did]
        for t in plan["dossier_template"]:
            for text in t["texts"]:
                filled = text.format(p=d["phrase"])
                q = f"{filled} {_site_filter(t['source'], plan)}".strip()
                qid = hashlib.sha1(f"dossier|{did}|{t['question']}|{filled}".encode()).hexdigest()[:12]
                out.append(Query(qid, did, f"dossier:{t['question']}", t["source"], t["window"], filled, q, tbs[t["window"]]))
    return out


def dossier(rows: list[dict], plan: dict, domain_id: str, stage1_rows: list[dict] | None = None) -> dict:
    """Evidence for the five sub-questions of one domain, with a fixed answer status each."""
    d = next(x for x in plan["domain"] if x["id"] == domain_id)
    mine = [r for r in rows if r["domain"] == domain_id and r["kind"].startswith("dossier:")]
    pain_rows = [r for r in (stage1_rows or []) if r["domain"] == domain_id and r["kind"] == "pain"]
    ev = {"who": {}, "money": [], "pay": [], "existing_fail": [], "workarounds": []}
    seen = set()
    for r in mine + pain_rows:
        if r["link"] in seen:
            continue
        seen.add(r["link"])
        text = f"{r['title']} {r['snippet']}"
        low = text.lower()
        for seg in SEGMENTS:
            if seg in low:
                ev["who"].setdefault(seg, []).append(r["link"])
        item = {"title": r["title"][:140], "link": r["link"], "date": r.get("date", "")}
        if AMOUNT_RE.search(text) and (classify(r)["relevant"] or r["kind"] == "dossier:money"):
            ev["money"].append({**item, "amount": AMOUNT_RE.search(text).group(0)})
        if PRICE_RE.search(text) or WILLING_RE.search(text):
            ev["pay"].append(item)
        if FAIL_RE.search(text) and r["kind"] in ("dossier:existing_fail", "dossier:pay", "dossier:who", "pain"):
            ev["existing_fail"].append(item)
        if WORKAROUND_RE.search(text):
            ev["workarounds"].append(item)
    who_sorted = sorted(ev["who"].items(), key=lambda kv: -len(kv[1]))
    status = {
        "who": "answered" if who_sorted and len(who_sorted[0][1]) >= 3 else ("partial" if who_sorted else "unknown"),
        "money": "answered" if len(ev["money"]) >= 3 else ("partial" if ev["money"] else "unknown"),
        "pay": "answered" if len(ev["pay"]) >= 3 else ("partial" if ev["pay"] else "unknown"),
        "existing_fail": "answered" if len(ev["existing_fail"]) >= 3 else ("partial" if ev["existing_fail"] else "unknown"),
        "simple_fix": "answered" if (d["source_of_truth"] and d["smb_reachable"] and not d["regulated"]) else "no",
    }
    verdict = "candidate" if all(v == "answered" for v in status.values()) else "not yet"
    interview = []
    if status["who"] != "answered":
        interview.append("Who exactly hits this, and how often?")
    if status["money"] != "answered":
        interview.append("What did the last occurrence cost you (money, hours, a customer)?")
    if status["pay"] != "answered":
        interview.append("What do you pay today to prevent or fix it, and would you pay for a check that catches it?")
    if status["existing_fail"] != "answered":
        interview.append("What have you tried, and why didn't it solve it?")
    return {"domain": domain_id, "phrase": d["phrase"], "question": QUESTION, "status": status, "verdict": verdict,
            "who_top": [(k, len(v)) for k, v in who_sorted[:5]],
            "evidence": {k: (v[:6] if isinstance(v, list) else None) for k, v in ev.items() if k != "who"},
            "interview_questions": interview}


def write_dossiers(dossiers: list[dict], out: Path) -> None:
    (out / "dossiers.json").write_text(json.dumps(dossiers, indent=1, ensure_ascii=False))
    L = ["# painscan dossiers", "", f"> {QUESTION}", ""]
    for x in dossiers:
        L += [f"## {x['domain']} ({x['phrase']}) — verdict: **{x['verdict']}**", "",
              "| sub-question | status |", "|---|---|"] + [f"| {k} | {v} |" for k, v in x["status"].items()]
        L += ["", "Top segments: " + (", ".join(f"{k} ({n})" for k, n in x["who_top"]) or "none"), ""]
        for k in ("money", "pay", "existing_fail", "workarounds"):
            items = x["evidence"].get(k) or []
            if items:
                L.append(f"**{k}**")
                for e in items:
                    extra = f" — {e['amount']}" if "amount" in e else ""
                    L.append(f"- [{e['title'].replace('|', '/')}]({e['link']}){extra}")
                L.append("")
        if x["interview_questions"]:
            L += ["Open questions for customer conversations:"] + [f"- {q}" for q in x["interview_questions"]] + [""]
    (out / "dossiers.md").write_text("\n".join(L), encoding="utf-8")


# ----------------------------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    r = sub.add_parser("run")
    r.add_argument("--mode", choices=["smoke", "full", "dossier"], required=True)
    r.add_argument("--domains", default="", help="dossier mode: comma-separated domain ids (shortlist)")
    r.add_argument("--max-queries", type=int, default=0)
    r.add_argument("--recent-tbs", default="", help="override the recent window (after the smoke check)")
    r.add_argument("--out", type=Path, required=True)
    rep = sub.add_parser("report")
    rep.add_argument("--in", dest="inp", type=Path, required=True)
    rep.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    plan = load_plan()

    if args.cmd == "plan":
        qs = build_queries(plan)
        kinds = {}
        for q in qs:
            kinds[q.kind] = kinds.get(q.kind, 0) + 1
        print(json.dumps({"queries": len(qs), "by_kind": kinds, "domains": len(plan["domain"]), "plan_hash": plan_hash()}))
        return 0

    if args.cmd == "run":
        key = os.environ.get("SERPER_API_KEY", "").strip()
        if not key:
            print("SERPER_API_KEY is not set", file=sys.stderr)
            return 2
        if args.mode == "smoke":
            queries, cap = smoke_queries(plan), 6
        elif args.mode == "dossier":
            ids = [x.strip() for x in args.domains.split(",") if x.strip()]
            known = {d["id"] for d in plan["domain"]}
            if not ids or any(i not in known for i in ids):
                print(f"--domains must list known domain ids, got {ids}", file=sys.stderr)
                return 2
            queries = build_dossier_queries(plan, ids)
            cap = args.max_queries if args.max_queries > 0 else len(queries)
        else:
            queries = build_queries(plan, args.recent_tbs or None)
            cap = args.max_queries if args.max_queries > 0 else len(queries)
        stats = run(plan, queries, args.out, key, cap)
        stats["plan_hash"] = plan_hash()
        print(json.dumps(stats, indent=1))
        if args.mode == "smoke":
            rows = read_rows(args.out)
            by = {}
            for row in rows:
                by.setdefault(row["qid"], []).append(row.get("date", ""))
            print(json.dumps({"dates_by_window": by}, indent=1))
        return 0 if stats["ok"] > 0 else 1

    if args.cmd == "report":
        rows = read_rows(args.inp)
        done = sum(1 for f in args.inp.rglob("done.jsonl") for l in f.read_text().splitlines() if l.strip())
        stage1 = [r for r in rows if not r["kind"].startswith("dossier:")]
        metrics = aggregate(stage1, plan)
        args.out.mkdir(parents=True, exist_ok=True)
        write_report(metrics, plan, args.out, {"plan_hash": plan_hash(), "rows": len(stage1), "queries_done": done})
        dossier_domains = sorted({r["domain"] for r in rows if r["kind"].startswith("dossier:")})
        if dossier_domains:
            write_dossiers([dossier(rows, plan, d, stage1) for d in dossier_domains], args.out)
        print((args.out / "report.md").read_text()[:3000])
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
