"""Unencrypted summary of a layer-3 (live web) run, written inside the job (PR 15).

A layer-3 run directory holds third-party page text, so the workflow uploads it
only encrypted (PREREGISTRATION.md section 14). The orchestrator of the study
cannot decrypt it (it has no access to secret values), and the budget ledger must
still know what the run cost. So, in the same job and before the run directory is
encrypted, this command writes ``layer3_summary.json``: the run's cost, metrics
and per-question outcomes, and NOTHING that carries page text.

    python -m binding_gate.layer3_summary RUN_DIR [RUN_DIR ...] --out DIR [--no-analysis]

Exactly what the summary may contain (:data:`ALLOWED_FIELDS`; tested)
----------------------------------------------------------------------
Per run: run id, config name and hash, git commit, dataset, split, model labels
and arm labels, start/end times, whether the run closed, record count, trace
SHA-256, the ledger's spend entry (``spend``: dollars by model and by call
purpose, call counts; ``binding_gate.ledger.trace_run_spend``), the number of web
searches, and the snapshot store's counts.

Per record (one item x arm x model x repeat): item id, group id, stratum, arm,
model, repeat; ``error`` (true/false) and the exception TYPE only (never its
message); the final answer's status; the final answer's VALUE cut to
:data:`MAX_VALUE_CHARS` characters (the model's short answer, e.g. a name or a
year, needed to audit correctness); the final answer's source id; the quote only
as its SHA-256, its length in characters and its whitespace-token count (never its
text); the ``sealqa_answer`` score's label and the whitelisted details
:data:`SCORE_DETAIL_KEYS` (outcome, value_has_gold, value_exact, quote_tokens,
rule_version, freshness, search_results, subsets, min_quote_tokens and a reason
CODE); the number of attempts; per attempt: answer status, quote SHA-256 and
length, source id, the evidence references (snapshot id, SHA-256 of the text
shown, URL) and the gate verdict as decision plus, per check, its name, status
and reason CODE (a lower-case identifier; any other text is dropped); per call
purpose the number of paid calls and their cost.

Per page used as evidence: snapshot id, URL, final URL, fetch time, outcome,
HTTP status, content type, raw and text SHA-256, byte and character counts (from
the snapshot index; the same fields as the published manifest).

Metrics: the layer-3 analysis (``scorers.sealqa_answer.analyse_layer3``),
sanitised: numbers, booleans, nulls and strings of at most
:data:`MAX_METRIC_STRING` characters only.

Never included: page text, page titles, search-result titles and snippets, search
queries and research requests, quotes, raw model outputs, prompts, check
details, verdict reasons in free text, error messages, gold answers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from binding_gate.canonical import utc_now_iso

SUMMARY_VERSION = 1
SUMMARY_NAME = "layer3_summary.json"
MAX_VALUE_CHARS = 100
MAX_METRIC_STRING = 120
SCORE_DETAIL_KEYS = frozenset({
    "outcome", "value_has_gold", "value_exact", "quote_tokens", "rule_version", "freshness", "search_results",
    "subsets", "min_quote_tokens", "reason",
})
PAGE_KEYS = ("snapshot_id", "url", "final_url", "fetched_at", "outcome", "http_status", "content_type",
             "raw_sha256", "raw_bytes", "text_sha256", "text_chars")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ALLOWED_FIELDS = {
    "summary": {"summary_version", "kind", "generated_at", "allowed_fields_doc", "runs", "metrics", "metrics_error"},
    "run": {"run_id", "config_name", "config_hash", "git_commit", "dataset", "split", "models", "arms", "started_at",
            "ended_at", "closed", "trace_count", "traces_sha256", "spend", "searches", "snapshot_counts", "records",
            "pages", "spend_error"},
    "record": {"item_id", "group_id", "stratum", "arm", "model", "repeat", "error", "error_type", "final_status",
               "final_value", "final_value_truncated", "final_source_id", "final_quote_sha256", "final_quote_chars",
               "final_quote_tokens", "score", "n_attempts", "attempts", "calls"},
    "attempt": {"answer_status", "quote_sha256", "quote_chars", "source_id", "evidence", "verdict"},
    "evidence": {"id", "sha256", "url"},
    "verdict": {"decision", "checks"},
    "check": {"check", "status", "reason"},
    "page": set(PAGE_KEYS),
}
"""Every key that may appear at each level of the summary (tests walk a summary against it)."""


def _sha(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def _code(value: Any) -> str | None:
    return value if isinstance(value, str) and _CODE.match(value) else None


def _quote_fields(prefix: str, quote: str | None) -> dict[str, Any]:
    from binding_gate.textnorm import normalize_text

    if quote is None:
        return {f"{prefix}_sha256": None, f"{prefix}_chars": None}
    out = {f"{prefix}_sha256": _sha(quote), f"{prefix}_chars": len(quote)}
    if prefix == "final_quote":
        out["final_quote_tokens"] = len(normalize_text(quote).split())
    return out


def _score(record: Any) -> dict[str, Any] | None:
    from binding_gate.scorers.sealqa_answer import SCORER_NAME

    for score in record.scores:
        if score.scorer != SCORER_NAME:
            continue
        details = {}
        for key in SCORE_DETAIL_KEYS:
            if key not in score.details:
                continue
            value = score.details[key]
            if key == "reason":
                value = _code(value)
            elif isinstance(value, str):
                value = value[:MAX_METRIC_STRING]
            elif isinstance(value, list):
                value = [v[:MAX_METRIC_STRING] for v in value if isinstance(v, str)]
            elif not (value is None or isinstance(value, (bool, int, float))):
                continue
            details[key] = value
        return {"label": _code(score.label) or "other", **details}
    return None


def _verdict(verdict: Any) -> dict[str, Any] | None:
    if verdict is None:
        return None
    return {
        "decision": verdict.decision.value,
        "checks": [{"check": r.check, "status": r.status.value, "reason": _code(r.details.get("reason"))}
                   for r in verdict.check_results],
    }


def record_summary(record: Any, cost: Any) -> dict[str, Any]:
    """The allowed fields of one trace record (see the module docstring)."""
    from binding_gate.runner import is_cache_hit

    final = record.final_answer
    value = None if final is None else final.value
    calls: dict[str, dict[str, Any]] = {}
    for call in record.model_calls:
        if call.response is None or is_cache_hit(call):
            continue
        slot = calls.setdefault(call.purpose, {"paid_calls": 0, "usd": 0.0})
        slot["paid_calls"] += 1
        slot["usd"] = round(slot["usd"] + cost.call_cost(call), 8)
    out: dict[str, Any] = {
        "item_id": record.item_id, "group_id": record.group_id, "stratum": record.stratum, "arm": record.arm,
        "model": record.model, "repeat": record.repeat, "error": record.error is not None,
        "error_type": None if record.error is None else (record.error.split(":", 1)[0][:60] or None),
        "final_status": None if final is None else final.status.value,
        "final_value": None if value is None else value[:MAX_VALUE_CHARS],
        "final_value_truncated": value is not None and len(value) > MAX_VALUE_CHARS,
        "final_source_id": None if final is None else final.source_id,
        **_quote_fields("final_quote", None if final is None else final.quote),
        "score": _score(record),
        "n_attempts": len(record.attempts),
        "attempts": [
            {
                "answer_status": a.answer.status.value,
                **_quote_fields("quote", a.answer.quote),
                "source_id": a.answer.source_id,
                "evidence": [{"id": e.id, "sha256": e.sha256, "url": e.url} for e in a.evidence],
                "verdict": _verdict(a.verdict),
            }
            for a in record.attempts
        ],
        "calls": calls,
    }
    if out["error_type"] is not None and not re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", out["error_type"]):
        out["error_type"] = "error"
    return out


def run_summary(run_dir: str | Path, repo_root: str | Path) -> dict[str, Any]:
    """The allowed fields of one layer-3 run directory."""
    from binding_gate.config import parse_config
    from binding_gate.ledger import run_cost_model, trace_run_spend
    from binding_gate.research.snapshots import SnapshotStore
    from binding_gate.trace import read_manifest, read_traces

    run_dir = Path(run_dir)
    manifest = read_manifest(run_dir)
    config = parse_config(manifest.config)
    cost = run_cost_model(config, Path(repo_root))
    records = list(read_traces(run_dir))
    out: dict[str, Any] = {
        "run_id": manifest.run_id, "config_name": config.name, "config_hash": manifest.config_hash,
        "git_commit": manifest.git_commit, "dataset": config.dataset.name, "split": config.split.value,
        "models": [m.label for m in config.models], "arms": [a.label for a in config.arms],
        "started_at": manifest.started_at, "ended_at": manifest.ended_at, "closed": manifest.ended_at is not None,
        "trace_count": len(records), "traces_sha256": manifest.traces_sha256,
    }
    try:
        out["spend"] = {"key": None, **trace_run_spend(run_dir, repo_root, source=f"layer-3 summary of {manifest.run_id}").to_dict()}
        out["spend"]["key"] = f"run:{manifest.run_id}"
    except Exception as exc:  # noqa: BLE001 - the ledger then reports the run as unknown spend
        out["spend_error"] = type(exc).__name__
    summaries = [record_summary(r, cost) for r in records]
    out["records"] = summaries
    out["searches"] = sum(r["calls"].get("search", {}).get("paid_calls", 0) for r in summaries)
    store = SnapshotStore.for_run(run_dir)
    counts: dict[str, int] = {}
    pages: dict[str, dict[str, Any]] = {}
    if store.root.is_dir():
        used = {e["id"] for r in summaries for a in r["attempts"] for e in a["evidence"]}
        for entry in _index(store.root):
            kind = entry.get("kind")
            counts[str(kind)] = counts.get(str(kind), 0) + 1
            if kind == "page" and entry.get("snapshot_id") in used:
                pages[entry["snapshot_id"]] = {k: entry.get(k) for k in PAGE_KEYS}
    out["snapshot_counts"] = counts
    out["pages"] = [pages[k] for k in sorted(pages)]
    return out


def _index(root: Path) -> list[dict[str, Any]]:
    from binding_gate.research.snapshots import INDEX_NAME

    path = root / INDEX_NAME
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sanitise(value: Any) -> Any:
    """Numbers, booleans, nulls and short strings only (dict keys kept if short); anything longer is dropped."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_METRIC_STRING else None
    if isinstance(value, Mapping):
        return {str(k): sanitise(v) for k, v in value.items() if isinstance(k, str) and len(k) <= MAX_METRIC_STRING}
    if isinstance(value, (list, tuple)):
        return [sanitise(v) for v in value]
    return None


def build_summary(run_dirs: Sequence[str | Path], *, repo_root: str | Path = ".", analysis: bool = True) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "summary_version": SUMMARY_VERSION,
        "kind": "layer3-summary",
        "generated_at": utc_now_iso(),
        "allowed_fields_doc": "binding_gate.layer3_summary (module docstring): no page text, no quotes, no prompts",
        "runs": [run_summary(d, repo_root) for d in run_dirs],
    }
    if analysis:
        try:
            from binding_gate.scorers.sealqa_answer import analyse_layer3

            summary["metrics"] = sanitise(analyse_layer3([str(d) for d in run_dirs]))
        except Exception as exc:  # noqa: BLE001 - the costs and outcomes are still written
            summary["metrics_error"] = f"{type(exc).__name__}"
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m binding_gate.layer3_summary", description=__doc__.split("\n\n")[0])
    parser.add_argument("run_dirs", nargs="+")
    parser.add_argument("--out", required=True, help="output directory (gets layer3_summary.json)")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--no-analysis", action="store_true")
    args = parser.parse_args(argv)
    summary = build_summary(args.run_dirs, repo_root=args.repo_root, analysis=not args.no_analysis)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / SUMMARY_NAME).write_text(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    total = sum((r.get("spend") or {}).get("usd", 0.0) for r in summary["runs"])
    print(f"layer-3 summary: {len(summary['runs'])} run(s), {sum(len(r['records']) for r in summary['runs'])} records, "
          f"spend ${total:.4f}; written to {out / SUMMARY_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
