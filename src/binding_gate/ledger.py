"""Global budget ledger (PR 15): the ACTUAL spend of the whole study so far, against the USD 50 limit.

The runner's caps bound each run; this ledger bounds the study. It computes what
has really been spent from the run records themselves and refuses a paid run
whose cap could take the total above ``hard_limit_usd`` of ``configs/budget.toml``
(USD 50).

What is counted (one ENTRY per key; several copies of the same records count once)
------------------------------------------------------------------------------------
``run:<run_id>``                 a run directory (``manifest.json`` + ``traces.jsonl``):
                                 every logged call of every record (model under test,
                                 decomposer, judge, search, fetch), priced EXACTLY as
                                 the runner prices it (``runner.CostModel`` with the
                                 run's ``[runner] prices`` file: reported usage, the
                                 id in the response, cache hits and failed calls
                                 free, a call without usage charged at prompt
                                 characters / chars_per_token input plus the client's
                                 max_output_tokens). The spend the runner itself
                                 recorded in ``invocations.jsonl`` ("ended" events)
                                 is read too; the entry is the LARGER of the two.
                                 A layer-3 summary (``layer3_summary.json``, written
                                 inside the job before the run directory is encrypted)
                                 carries the same entry for an encrypted run.
``decomposer-eval:<run_id>``     a decomposer measurement (``decompositions.jsonl``):
                                 its calls priced the same way, or its recorded
                                 ``cost_usd`` if larger.
``judge:<run_id>:<judge hash>``  the judge re-scoring (``scores.llm_judge.jsonl``
                                 beside a trace): the judge calls kept in each score.
``manual:<id>``                  spend entered by hand in the committed snapshot
                                 ``results/ledger.json`` (for example a frozen test run
                                 made on a computer, outside GitHub Actions).

Copies of one run directory (the same run in several artifacts, or an artifact and
a resumed copy) have the same key; the largest value is kept (traces are
append-only, so a later copy is a superset of an earlier one).

Where the records come from (``fetch``, inside GitHub Actions only)
------------------------------------------------------------------
``python -m binding_gate.ledger fetch`` lists EVERY artifact of the repository
through the REST API with the workflow's ``GITHUB_TOKEN`` (``binding_gate.ghapi``),
keeps only artifacts of runs started by hand in this repository
(``workflow_dispatch``; a fork's pull request cannot add one), and classifies them
by name (:func:`artifact_role`):

* ``runs-<model>`` (with any ``<stage>--<config>--`` prefix): run directories;
* ``decomposer-eval``: a decomposer measurement; ``rescore``: judge scores;
* ``layer3-summary-<...>``: the unencrypted summary of an encrypted layer-3 run;
* ``runs-encrypted-<...>``: an encrypted layer-3 run directory - NOT read (it is
  encrypted); its spend must come from the ``layer3-summary-<...>`` artifact of the
  same workflow run, otherwise its spend is unknown;
* ``ledger``: an earlier ledger result. Its entries are carried forward and the
  artifacts it read are not downloaded again (artifacts cannot be changed after
  upload), so every ledger carries the spend of everything before it;
* anything else (reports, decisions, analyses, publish manifests): no spend.

Completeness (when the ledger refuses instead of guessing)
----------------------------------------------------------
The result lists ``problems``; any problem makes ``complete`` false and every paid
run is then refused. Problems: a spend artifact that has EXPIRED and is not
covered by an earlier ledger or by the committed snapshot; an encrypted run
without its summary; an artifact that could not be downloaded or read; a call
that cannot be priced. A paid run is also refused while another hand-started
workflow run of the repository is in progress (its spend is not uploaded yet; a
run that is only queued has spent nothing, and its own check will see this one).

Residual risk (documented in docs/RUNNING.md, "Budget ledger"): artifacts are kept
for at most 90 days (``retention-days: 90`` on every upload, the maximum GitHub
allows for public repositories; a repository setting may lower it). The chain of
ledger artifacts carries older spend forward only as long as a ledger runs at
least once per retention period; otherwise the orchestrator commits the latest
``ledger.json`` as ``results/ledger.json`` (the same format), which the ledger
always reads. Spend that never reached an artifact (a job killed before its upload
step, a run on a computer) is invisible unless it is entered in the snapshot; the
provider spending limits are the outer safety net.

Commands
--------
``fetch --out DIR``                       download (Actions only; needs GITHUB_TOKEN)
``compute [DIR ...] [--inputs DIR] --json FILE [--markdown FILE]``
``cap (--config C [--model L] | --stage S)``
``check --ledger FILE (--config C [--model L] | --stage S | --cap USD)``
                                          exit 5 (refused) when the ledger is
                                          incomplete, another run is active, or
                                          total + cap > hard limit.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from binding_gate.canonical import utc_now_iso

LEDGER_VERSION = 1
LEDGER_NAME = "ledger.json"
INDEX_NAME = "artifacts.json"
SUMMARY_NAME = "layer3_summary.json"
SNAPSHOT_PATH = "results/ledger.json"
BUDGET_PATH = "configs/budget.toml"
DEFAULT_PRICES = "configs/prices.toml"
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REFUSED = 5
RETENTION_DAYS = 90
"""``retention-days`` of every artifact upload: the maximum GitHub allows for public repositories."""
UNKNOWN_MAX_OUTPUT_TOKENS = 32768
"""Output tokens charged for a judge call without reported usage whose request names no maximum."""
TOLERANCE_USD = 1e-9

ROLE_RUNS = "runs"
ROLE_ENCRYPTED = "runs-encrypted"
ROLE_SUMMARY = "layer3-summary"
ROLE_DECOMPOSER_EVAL = "decomposer-eval"
ROLE_RESCORE = "rescore"
ROLE_LEDGER = "ledger"
SPEND_ROLES = frozenset({ROLE_RUNS, ROLE_ENCRYPTED, ROLE_SUMMARY, ROLE_DECOMPOSER_EVAL, ROLE_RESCORE})
"""Artifact roles whose content records spend (an expired one must be covered)."""
READ_ROLES = frozenset({ROLE_RUNS, ROLE_SUMMARY, ROLE_DECOMPOSER_EVAL, ROLE_RESCORE, ROLE_LEDGER})
"""Artifact roles the ledger downloads and reads."""

STATUS_READ = "read"
STATUS_COVERED = "covered"
STATUS_EXPIRED_COVERED = "expired-covered"
STATUS_ENCRYPTED_SUMMARY = "encrypted-spend-in-summary"
STATUS_NO_SPEND = "no-spend"
STATUS_UNTRUSTED = "ignored-untrusted-run"
PROBLEM_STATUSES = frozenset({"expired-uncovered", "download-failed", "encrypted-without-summary", "run-unreadable"})
ACCOUNTED_STATUSES = frozenset({STATUS_READ, STATUS_COVERED, STATUS_EXPIRED_COVERED, STATUS_ENCRYPTED_SUMMARY})


class LedgerError(RuntimeError):
    """The ledger cannot be computed or a check cannot be made."""


# --------------------------------------------------------------------------- #
# Artifact names
# --------------------------------------------------------------------------- #


def strip_attempt(name: str) -> str:
    """``name`` without the ``-attempt<N>`` suffix that re-run attempts add (PR 15 naming)."""
    head, sep, tail = name.rpartition("-attempt")
    return head if sep and tail.isdigit() else name


def artifact_role(name: str) -> str | None:
    """The role of an artifact from its name (see the module docstring); ``None`` = no spend."""
    last = strip_attempt(name).rsplit("--", 1)[-1]
    if last.startswith("runs-encrypted-"):
        return ROLE_ENCRYPTED
    if last.startswith("layer3-summary"):
        return ROLE_SUMMARY
    if last.startswith("decomposer-eval"):
        return ROLE_DECOMPOSER_EVAL
    if last.startswith("rescore"):
        return ROLE_RESCORE
    if last == "ledger" or last.startswith("ledger-"):
        return ROLE_LEDGER
    if last.startswith("runs-"):
        return ROLE_RUNS
    return None


def summary_name_for(encrypted_name: str) -> str:
    """The name of the layer-3 summary artifact uploaded next to an encrypted one."""
    head, sep, last = encrypted_name.rpartition("--")
    return f"{head}{sep}{last.replace('runs-encrypted-', 'layer3-summary-', 1)}"


# --------------------------------------------------------------------------- #
# Entries
# --------------------------------------------------------------------------- #


@dataclass
class Entry:
    key: str
    usd: float
    kind: str
    source: str
    calls: int = 0
    unpriced_calls: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "usd": round(self.usd, 8), "kind": self.kind, "source": self.source, "calls": self.calls,
            "unpriced_calls": self.unpriced_calls, "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, key: str, data: Mapping[str, Any], *, source: str | None = None) -> Entry:
        usd = data.get("usd")
        if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd < 0 or not math.isfinite(usd):
            raise LedgerError(f"ledger entry {key!r}: usd must be a number >= 0")
        return cls(
            key=key, usd=float(usd), kind=str(data.get("kind", "carried")),
            source=source or str(data.get("source", "")), calls=int(data.get("calls", 0) or 0),
            unpriced_calls=int(data.get("unpriced_calls", 0) or 0), detail=dict(data.get("detail") or {}),
        )


def merge(entries: Iterable[Entry]) -> dict[str, Entry]:
    """One entry per key: the one with the largest spend (copies of the same records count once)."""
    out: dict[str, Entry] = {}
    for entry in entries:
        kept = out.get(entry.key)
        if kept is None or entry.usd > kept.usd + TOLERANCE_USD:
            out[entry.key] = entry
        elif kept is not None and entry.unpriced_calls > kept.unpriced_calls:
            kept.unpriced_calls = entry.unpriced_calls
    return out


# --------------------------------------------------------------------------- #
# Spend of one directory, priced as the runner prices it
# --------------------------------------------------------------------------- #


def _request_chars(call: Any) -> int:
    return sum(len(m.content) for m in call.request.messages)


def recorded_run_spend(invocations: Sequence[Mapping[str, Any]]) -> float:
    """Spend the runner recorded: per model label the largest value over "ended" events (spend is cumulative
    per run directory, a resumed invocation starts from the trace), summed over labels."""
    best: dict[str, float] = {}
    for event in invocations:
        if event.get("event") != "ended":
            continue
        for label, usd in (event.get("spend_usd") or {}).items():
            if isinstance(usd, (int, float)) and not isinstance(usd, bool):
                best[label] = max(best.get(label, 0.0), float(usd))
    return sum(best.values())


def run_cost_model(config: Any, repo_root: Path) -> Any:
    """The runner's ``CostModel`` for a run config, built exactly as ``runner.execute_run`` builds it."""
    from binding_gate import runner
    from binding_gate.pricing import load_prices

    settings = runner.RunnerSettings.from_config(config)
    prices = load_prices(repo_root / settings.prices)
    aux, _problems = runner.auxiliary_clients(config)
    clients = {spec.label: runner.build_model(spec) for spec in config.models}
    return runner.CostModel(
        prices=prices,
        model_ids={**{e.label: e.model_id for e in aux}, **{label: runner.model_id_of(c) for label, c in clients.items()}},
        max_output_tokens={
            **{e.label: getattr(e.client, "max_output_tokens", 0) or 0 for e in aux},
            **{label: getattr(c, "max_output_tokens", 0) or 0 for label, c in clients.items()},
        },
    )


def trace_run_spend(run_dir: str | Path, repo_root: str | Path, *, source: str = "") -> Entry:
    """The ``run:<run_id>`` entry of a run directory (see the module docstring)."""
    from binding_gate.config import parse_config
    from binding_gate.runner import is_cache_hit, priced_id, read_invocations
    from binding_gate.trace import read_manifest, read_traces

    run_dir, repo_root = Path(run_dir), Path(repo_root)
    manifest = read_manifest(run_dir)
    config = parse_config(manifest.config)
    cost = run_cost_model(config, repo_root)
    by_model: dict[str, float] = defaultdict(float)
    by_purpose: dict[str, float] = defaultdict(float)
    calls = unpriced = records = 0
    for record in read_traces(run_dir):
        records += 1
        for call in record.model_calls:
            if call.response is None or is_cache_hit(call):
                continue
            calls += 1
            if priced_id(call, cost.prices, cost.model_ids) is None:
                unpriced += 1
            usd = cost.call_cost(call)
            by_model[record.model] += usd
            by_purpose[call.purpose] += usd
    computed = sum(by_model.values())
    invocations = read_invocations(run_dir)
    recorded = recorded_run_spend(invocations)
    used_sha = sorted({e.get("prices_sha256") for e in invocations if e.get("prices_sha256")})
    detail = {
        "config_name": config.name, "split": config.split.value, "dataset": config.dataset.name,
        "records": records, "closed": manifest.ended_at is not None,
        "computed_usd": round(computed, 8), "recorded_usd": round(recorded, 8),
        "by_model_usd": {k: round(v, 8) for k, v in sorted(by_model.items())},
        "by_purpose_usd": {k: round(v, 8) for k, v in sorted(by_purpose.items())},
        "prices_sha256_now": cost.prices.sha256,
    }
    if used_sha and used_sha != [cost.prices.sha256]:
        detail["prices_changed_since_run"] = used_sha
    return Entry(f"run:{manifest.run_id}", max(computed, recorded), "trace", source, calls, unpriced, detail)


def _price_calls(calls: Iterable[Any], prices: Any, label_ids: Mapping[str, str], max_output: Mapping[str, int]) -> tuple[float, int, int]:
    """``(usd, priced-or-not paid calls, unpriced calls)`` with the runner's rules for usage and ids."""
    from binding_gate.contracts import TokenUsage
    from binding_gate.runner import is_cache_hit, priced_id

    total, paid, unpriced = 0.0, 0, 0
    for call in calls:
        if call.response is None or is_cache_hit(call):
            continue
        paid += 1
        model_id = priced_id(call, prices, label_ids)
        if model_id is None:
            unpriced += 1
            continue
        usage = call.response.usage
        if usage is None:
            limit = max_output.get(call.client)
            if limit is None:
                params_limit = call.request.params.get("max_output_tokens")
                limit = params_limit if isinstance(params_limit, int) else UNKNOWN_MAX_OUTPUT_TOKENS
            usage = TokenUsage(prices.assumptions.input_tokens(_request_chars(call)), limit)
        total += prices.get(model_id).call_cost(usage.input_tokens, usage.output_tokens)
    return total, paid, unpriced


def decomposer_eval_spend(run_dir: str | Path, repo_root: str | Path, *, source: str = "") -> Entry:
    """The ``decomposer-eval:<run_id>`` entry of a decomposer measurement."""
    from binding_gate.contracts import ModelCall
    from binding_gate.evaluation.decomposer import read_records
    from binding_gate.pricing import load_prices
    from binding_gate.trace import read_manifest

    run_dir, repo_root = Path(run_dir), Path(repo_root)
    manifest = read_manifest(run_dir)
    cfg = manifest.config
    prices = load_prices(repo_root / str((cfg.get("runner") or {}).get("prices", DEFAULT_PRICES)))
    model = ((cfg.get("decomposer") or {}).get("options") or {}).get("model") or {}
    options = model.get("options") or {}
    label = str(model.get("label") or options.get("model_id") or "decomposer")
    label_ids = {label: str(options.get("model_id", ""))}
    max_output = {label: int(options.get("max_output_tokens", 0) or 0)}
    records = read_records(run_dir)
    calls = [ModelCall.from_dict(c) for r in records for c in r.get("model_calls", [])]
    computed, paid, unpriced = _price_calls(calls, prices, label_ids, max_output)
    recorded = sum(float(r.get("cost_usd", 0.0) or 0.0) for r in records)
    detail = {"config_name": cfg.get("name"), "records": len(records), "closed": manifest.ended_at is not None,
              "computed_usd": round(computed, 8), "recorded_usd": round(recorded, 8)}
    return Entry(f"decomposer-eval:{manifest.run_id}", max(computed, recorded), "decomposer_eval", source, paid, unpriced, detail)


def judge_scores_spend(run_dir: str | Path, repo_root: str | Path, *, source: str = "") -> list[Entry]:
    """One ``judge:<run_id>:<judge hash>`` entry per judge configuration in ``scores.llm_judge.jsonl``."""
    from binding_gate.contracts import ModelCall
    from binding_gate.pricing import load_prices
    from binding_gate.scorers.llm_judge import SCORES_NAME
    from binding_gate.trace import read_manifest

    run_dir, repo_root = Path(run_dir), Path(repo_root)
    run_id = read_manifest(run_dir).run_id
    prices = load_prices(repo_root / DEFAULT_PRICES)
    by_hash: dict[str, list[Any]] = defaultdict(list)
    lines = 0
    for line in (run_dir / SCORES_NAME).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        lines += 1
        entry = json.loads(line)
        details = (entry.get("score") or {}).get("details") or {}
        by_hash[str(entry.get("judge_hash", "unknown"))].extend(ModelCall.from_dict(c) for c in details.get("model_calls", []))
    out = []
    for judge_hash, calls in sorted(by_hash.items()):
        usd, paid, unpriced = _price_calls(calls, prices, {}, {})
        out.append(Entry(f"judge:{run_id}:{judge_hash[:16]}", usd, "judge_scores", source, paid, unpriced,
                         {"score_lines": lines, "prices": DEFAULT_PRICES}))
    return out


def summary_entries(path: str | Path, *, source: str = "") -> list[Entry]:
    """The spend entries a layer-3 summary carries (``binding_gate.layer3_summary``)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for run in data.get("runs", []):
        spend = run.get("spend") or {}
        key = spend.get("key")
        if not isinstance(key, str) or not key.startswith("run:"):
            raise LedgerError(f"{path}: a run has no spend entry")
        entry = Entry.from_dict(key, spend, source=source or str(path))
        entry.kind = "layer3_summary"
        out.append(entry)
    return out


@dataclass
class Scan:
    entries: list[Entry] = field(default_factory=list)
    carried: list[dict[str, Any]] = field(default_factory=list)  # earlier ledgers found
    problems: list[str] = field(default_factory=list)


def scan_directory(root: str | Path, repo_root: str | Path, *, source: str = "") -> Scan:
    """Every run directory, decomposer measurement, judge score file, layer-3 summary and earlier ledger under ``root``."""
    from binding_gate.evaluation.decomposer import RECORDS_NAME
    from binding_gate.scorers.llm_judge import SCORES_NAME
    from binding_gate.trace import TRACES_NAME

    root = Path(root)
    scan = Scan()
    label = source or root.as_posix()
    for manifest in sorted(root.rglob("manifest.json")):
        run_dir = manifest.parent
        where = f"{label}: {run_dir.relative_to(root).as_posix() or '.'}"
        try:
            if (run_dir / TRACES_NAME).is_file():
                scan.entries.append(trace_run_spend(run_dir, repo_root, source=where))
            if (run_dir / RECORDS_NAME).is_file():
                scan.entries.append(decomposer_eval_spend(run_dir, repo_root, source=where))
            if (run_dir / SCORES_NAME).is_file():
                scan.entries.extend(judge_scores_spend(run_dir, repo_root, source=where))
        except Exception as exc:  # noqa: BLE001 - an unreadable record set is unknown spend
            scan.problems.append(f"{where}: cannot be read ({type(exc).__name__}: {str(exc)[:300]})")
    for path in sorted(root.rglob(SUMMARY_NAME)):
        try:
            scan.entries.extend(summary_entries(path, source=f"{label}: {path.relative_to(root).as_posix()}"))
        except Exception as exc:  # noqa: BLE001
            scan.problems.append(f"{label}: {path.name} cannot be read ({type(exc).__name__}: {str(exc)[:300]})")
    for path in sorted(root.rglob(LEDGER_NAME)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("ledger_version") != LEDGER_VERSION:
                raise LedgerError(f"ledger_version must be {LEDGER_VERSION}")
            scan.carried.append({"source": f"{label}: {path.relative_to(root).as_posix()}", "data": data})
        except Exception as exc:  # noqa: BLE001
            scan.problems.append(f"{label}: earlier ledger {path.name} cannot be read ({type(exc).__name__}: {exc})")
    return scan


# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #


def load_budget(repo_root: str | Path) -> dict[str, Any]:
    return tomllib.loads((Path(repo_root) / BUDGET_PATH).read_text(encoding="utf-8"))


def read_ledger_file(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("ledger_version") != LEDGER_VERSION:
        raise LedgerError(f"{path}: ledger_version must be {LEDGER_VERSION}")
    return data


def _carried(data: Mapping[str, Any], source: str) -> tuple[list[Entry], set[int]]:
    entries = [Entry.from_dict(k, v, source=f"{source} <- {v.get('source', '')}") for k, v in (data.get("entries") or {}).items()]
    for entry in entries:
        entry.kind = entry.kind or "carried"
    covered = {int(i) for i in data.get("covered_artifact_ids", []) if isinstance(i, int) or str(i).isdigit()}
    return entries, covered


def compute(
    *,
    repo_root: str | Path = ".",
    inputs: str | Path | None = None,
    directories: Sequence[str | Path] = (),
    snapshot: str | Path | None = SNAPSHOT_PATH,
) -> dict[str, Any]:
    """Build the ledger from the fetched artifacts (``inputs``), extra directories and the committed snapshot."""
    repo_root = Path(repo_root)
    budget = load_budget(repo_root)
    hard_limit = float(budget["hard_limit_usd"])
    entries: list[Entry] = []
    problems: list[str] = []
    covered: set[int] = set()
    artifacts: list[dict[str, Any]] = []
    active_runs: list[dict[str, Any]] = []
    sources: list[str] = []
    meta: dict[str, Any] = {}

    snapshot_path = None if snapshot is None else (repo_root / snapshot if not Path(snapshot).is_absolute() else Path(snapshot))
    if snapshot_path is not None and snapshot_path.is_file():
        data = read_ledger_file(snapshot_path)
        found, ids = _carried(data, f"snapshot {snapshot}")
        entries += found
        covered |= ids
        sources.append(f"snapshot {snapshot} ({len(found)} entries)")

    if inputs is not None:
        inputs = Path(inputs)
        index = json.loads((inputs / INDEX_NAME).read_text(encoding="utf-8"))
        meta = {k: index.get(k) for k in ("repository", "current_run_id", "fetched_at")}
        artifacts = list(index.get("artifacts", []))
        active_runs = list(index.get("active_runs", []))
        problems += list(index.get("problems", []))
        for artifact in artifacts:
            if artifact.get("status") != STATUS_READ:
                continue
            where = f"artifact {artifact['id']} {artifact['name']!r} (workflow run {artifact.get('workflow_run_id')})"
            scan = scan_directory(inputs / artifact["dir"], repo_root, source=where)
            entries += scan.entries
            problems += scan.problems
            for carried in scan.carried:
                found, ids = _carried(carried["data"], carried["source"])
                entries += found
                covered |= ids
        covered |= {int(a["id"]) for a in artifacts if a.get("status") in ACCOUNTED_STATUSES and a.get("role") in SPEND_ROLES}
        sources.append(f"{sum(1 for a in artifacts if a.get('status') == STATUS_READ)} artifacts read from {inputs}")

    for directory in directories:
        scan = scan_directory(directory, repo_root)
        entries += scan.entries
        problems += scan.problems
        for carried in scan.carried:
            found, ids = _carried(carried["data"], carried["source"])
            entries += found
            covered |= ids
        sources.append(f"directory {Path(directory).as_posix()}")

    merged = merge(entries)
    unpriced = sorted(k for k, e in merged.items() if e.unpriced_calls)
    if unpriced:
        problems.append(f"calls that cannot be priced with configs/prices.toml in: {', '.join(unpriced)}")
    total = sum(e.usd for e in merged.values())
    return {
        "ledger_version": LEDGER_VERSION,
        "generated_at": utc_now_iso(),
        **meta,
        "hard_limit_usd": hard_limit,
        "total_usd": round(total, 6),
        "remaining_usd": round(hard_limit - total, 6),
        "complete": not problems,
        "problems": problems,
        "active_runs": active_runs,
        "sources": sources,
        "entries": {k: merged[k].to_dict() for k in sorted(merged)},
        "covered_artifact_ids": sorted(covered),
        "artifacts": artifacts,
        "retention_days": RETENTION_DAYS,
        "note": (
            "Actual spend from run records priced with configs/prices.toml as the runner prices them. Commit this "
            f"file as {SNAPSHOT_PATH} to carry the spend beyond the {RETENTION_DAYS}-day artifact retention; add "
            "'manual:<id>' entries for spend made outside GitHub Actions."
        ),
    }


# --------------------------------------------------------------------------- #
# Caps and the check
# --------------------------------------------------------------------------- #


def config_cap(path: str | Path, *, model: str = "all", repo_root: str | Path = ".") -> float:
    """The most one invocation of ``path`` can spend (the sum of the caps it enforces), in US dollars.

    Run configs: the ``[runner]`` cap of every selected model label; the decomposer
    measurement: its ``max_usd``; a re-scoring config: ``max_usd`` per judge model.
    The same rules as ``tests/test_budget_plan.py``.
    """
    full = Path(repo_root) / path if not Path(path).is_absolute() else Path(path)
    raw = tomllib.loads(full.read_text(encoding="utf-8"))
    if "eval_version" in raw:
        cap = (raw.get("runner") or {}).get("max_usd")
        if not isinstance(cap, (int, float)) or isinstance(cap, bool):
            raise LedgerError(f"{path}: no [runner] max_usd")
        return float(cap)
    if "rescore_version" in raw:
        from binding_gate.scorers.llm_judge import RescoreConfig

        rescore = RescoreConfig.load(full)
        return rescore.max_usd * len(rescore.judges)
    from binding_gate import runner
    from binding_gate.config import load_config

    config = load_config(full)
    settings = runner.RunnerSettings.from_config(config)
    caps = [settings.cap_for(spec.label) for spec in runner.select_models(config, model)]
    if any(c is None for c in caps):
        raise LedgerError(f"{path}: a selected model has no budget cap")
    return float(sum(caps))  # type: ignore[arg-type]


def stage_cap(stage: str, *, repo_root: str | Path = ".") -> float:
    from binding_gate.pipeline import stage_configs

    return sum(config_cap(p, repo_root=repo_root) for p in stage_configs(stage))


def check(ledger: Mapping[str, Any], cap: float, *, what: str) -> dict[str, Any]:
    """Allowed or refused, with the reasons (see the module docstring)."""
    total = float(ledger["total_usd"])
    limit = float(ledger["hard_limit_usd"])
    reasons = []
    if not ledger.get("complete"):
        reasons.append("the ledger is INCOMPLETE (unknown spend): " + "; ".join(ledger.get("problems", [])))
    active = ledger.get("active_runs") or []
    if active:
        names = ", ".join(f"{r.get('name') or r.get('path')} #{r.get('id')} ({r.get('status')})" for r in active)
        reasons.append(f"another hand-started workflow run is in progress ({names}); its spend is not "
                       "uploaded yet - start this run after it has finished")
    if total + cap > limit + TOLERANCE_USD:
        reasons.append(f"spent so far ${total:.4f} + cap of {what} ${cap:.2f} = ${total + cap:.4f} exceeds the hard "
                       f"limit ${limit:.2f}")
    return {
        "what": what, "cap_usd": round(cap, 6), "total_usd": round(total, 6), "hard_limit_usd": limit,
        "after_cap_usd": round(total + cap, 6), "remaining_after_cap_usd": round(limit - total - cap, 6),
        "allowed": not reasons, "reasons": reasons,
    }


def markdown(ledger: Mapping[str, Any]) -> str:
    """The job-summary text of a ledger (and of its check, if any)."""
    lines = [
        "## Budget ledger (actual spend so far)",
        "",
        f"- spent so far: **${ledger['total_usd']:.4f}** of the hard limit ${ledger['hard_limit_usd']:.2f}; "
        f"remaining **${ledger['remaining_usd']:.4f}**",
        f"- complete: **{'yes' if ledger.get('complete') else 'NO - unknown spend, paid runs are refused'}**",
    ]
    for problem in ledger.get("problems", []):
        lines.append(f"- problem: {problem}")
    for run in ledger.get("active_runs") or []:
        lines.append(f"- active workflow run: {run.get('name') or run.get('path')} #{run.get('id')} ({run.get('status')})")
    result = ledger.get("check")
    if result:
        verdict = "ALLOWED" if result["allowed"] else "REFUSED"
        lines += ["", f"### Check: {verdict} - {result['what']}", "",
                  f"cap ${result['cap_usd']:.2f}; spent ${result['total_usd']:.4f}; after the cap ${result['after_cap_usd']:.4f} "
                  f"(limit ${result['hard_limit_usd']:.2f})"]
        lines += [f"- {reason}" for reason in result["reasons"]]
    entries = ledger.get("entries") or {}
    if entries:
        lines += ["", "| entry | USD | kind | source |", "|---|---:|---|---|"]
        for key, entry in entries.items():
            lines.append(f"| `{key}` | {entry['usd']:.4f} | {entry['kind']} | {entry['source']} |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Fetching the artifacts (GitHub Actions)
# --------------------------------------------------------------------------- #


def fetch(api: Any, out_dir: str | Path, *, current_run_id: int | None = None, snapshot_covered: Iterable[int] = ()) -> dict[str, Any]:
    """Download what the ledger needs into ``out_dir`` and write ``artifacts.json`` (see the module docstring)."""
    from binding_gate.ghapi import GitHubAPIError, extract_zip, is_trusted_run

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    listing = api.artifacts()
    problems: list[str] = []
    rows: list[dict[str, Any]] = []
    trusted: dict[int, bool] = {}

    def trust(run_id: int | None) -> bool | None:
        if run_id is None:
            return None
        if run_id not in trusted:
            try:
                trusted[run_id] = is_trusted_run(api.run(run_id), api.repository)
            except GitHubAPIError:
                return None
        return trusted[run_id]

    for art in listing:
        run_id = (art.get("workflow_run") or {}).get("id")
        rows.append({
            "id": int(art["id"]), "name": str(art.get("name", "")), "role": artifact_role(str(art.get("name", ""))),
            "workflow_run_id": run_id, "created_at": art.get("created_at"), "expired": bool(art.get("expired")),
            "size_in_bytes": art.get("size_in_bytes"), "status": None, "dir": None, "_raw": art,
        })

    def download(row: dict[str, Any]) -> bool:
        target = f"a{row['id']}"
        try:
            extract_zip(api.download_artifact(row["_raw"]), out_dir / target)
        except Exception as exc:  # noqa: BLE001 - reported as a problem
            row["status"] = "download-failed"
            problems.append(f"artifact {row['id']} {row['name']!r} (workflow run {row['workflow_run_id']}) could not be "
                            f"downloaded: {type(exc).__name__}: {str(exc)[:200]}")
            return False
        row["status"], row["dir"] = STATUS_READ, target
        return True

    # 1. Earlier ledgers (small): their coverage decides what need not be downloaded again.
    covered = set(int(i) for i in snapshot_covered)
    for row in rows:
        if row["role"] is None:
            row["status"] = STATUS_NO_SPEND
            continue
        verdict = trust(row["workflow_run_id"])
        if verdict is False:
            row["status"] = STATUS_UNTRUSTED
        elif verdict is None and row["role"] == ROLE_LEDGER:
            row["status"] = "ignored-run-unreadable"  # an unverifiable earlier ledger is simply not used
        elif verdict is None:
            row["status"] = "run-unreadable"
            problems.append(f"artifact {row['id']} {row['name']!r}: its workflow run {row['workflow_run_id']} cannot be read")
    for row in rows:
        if row["role"] == ROLE_LEDGER and row["status"] is None and not row["expired"]:
            if download(row):
                path = out_dir / row["dir"] / LEDGER_NAME
                try:
                    covered |= {int(i) for i in read_ledger_file(path).get("covered_artifact_ids", [])}
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"earlier ledger artifact {row['id']} cannot be read: {exc}")
        elif row["role"] == ROLE_LEDGER and row["status"] is None:
            row["status"] = "expired"

    # 2. Spend artifacts.
    names_by_run: dict[Any, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        names_by_run[row["workflow_run_id"]][row["name"]] = row
    for row in rows:
        if row["status"] is not None or row["role"] not in SPEND_ROLES:
            continue
        if row["id"] in covered:
            row["status"] = STATUS_EXPIRED_COVERED if row["expired"] else STATUS_COVERED
        elif row["role"] == ROLE_ENCRYPTED:
            summary = names_by_run[row["workflow_run_id"]].get(summary_name_for(row["name"]))
            if summary is not None:
                row["status"] = STATUS_ENCRYPTED_SUMMARY
            else:
                row["status"] = "encrypted-without-summary"
                problems.append(f"encrypted artifact {row['id']} {row['name']!r} (workflow run {row['workflow_run_id']}) has no "
                                f"{summary_name_for(row['name'])!r} artifact: its spend is unknown")
        elif row["expired"]:
            row["status"] = "expired-uncovered"
            problems.append(f"artifact {row['id']} {row['name']!r} (workflow run {row['workflow_run_id']}) has EXPIRED and no "
                            f"earlier ledger or {SNAPSHOT_PATH} covers it: its spend is unknown (commit a snapshot "
                            "that includes it)")
        else:
            download(row)

    active = []
    for run in api.active_runs():
        if current_run_id is not None and run.get("id") == current_run_id:
            continue
        if run.get("event") != "workflow_dispatch" or run.get("status", "in_progress") != "in_progress":
            continue
        active.append({k: run.get(k) for k in ("id", "name", "path", "status", "created_at")})

    index = {
        "repository": api.repository,
        "current_run_id": current_run_id,
        "fetched_at": utc_now_iso(),
        "artifacts": [{k: v for k, v in row.items() if k != "_raw"} for row in rows],
        "active_runs": active,
        "problems": problems,
    }
    (out_dir / INDEX_NAME).write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return index


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def _cap_from_args(args: argparse.Namespace) -> tuple[float, str]:
    if args.cap is not None:
        return float(args.cap), f"a run with cap ${float(args.cap):.2f}"
    if args.stage:
        return stage_cap(args.stage, repo_root=args.repo_root), f"stage {args.stage!r}"
    if args.config:
        return config_cap(args.config, model=args.model, repo_root=args.repo_root), f"{args.config} (model {args.model})"
    raise LedgerError("give --config, --stage or --cap")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m binding_gate.ledger", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    fetch_cmd = commands.add_parser("fetch", help="download the artifacts the ledger needs (GitHub Actions only)")
    fetch_cmd.add_argument("--out", required=True)
    fetch_cmd.add_argument("--repo-root", default=".")
    fetch_cmd.add_argument("--snapshot", default=SNAPSHOT_PATH)

    compute_cmd = commands.add_parser("compute", help="compute the ledger from fetched artifacts and directories")
    compute_cmd.add_argument("directories", nargs="*")
    compute_cmd.add_argument("--inputs", default=None, help="the --out directory of fetch")
    compute_cmd.add_argument("--snapshot", default=SNAPSHOT_PATH)
    compute_cmd.add_argument("--no-snapshot", action="store_true")
    compute_cmd.add_argument("--repo-root", default=".")
    compute_cmd.add_argument("--json", required=True)
    compute_cmd.add_argument("--markdown", default=None)

    for name, help_text in (("cap", "print the cap of a config or a pipeline stage"),
                            ("check", "refuse (exit 5) a paid run that could take the total above the hard limit")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--config", default=None)
        sub.add_argument("--model", default="all")
        sub.add_argument("--stage", default=None)
        sub.add_argument("--cap", type=float, default=None)
        sub.add_argument("--repo-root", default=".")
        if name == "check":
            sub.add_argument("--ledger", required=True)
            sub.add_argument("--markdown", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "fetch":
            import os

            from binding_gate.ghapi import GitHubAPI

            snapshot = Path(args.repo_root) / args.snapshot
            covered: list[int] = []
            if snapshot.is_file():
                covered = [int(i) for i in read_ledger_file(snapshot).get("covered_artifact_ids", [])]
            run_id = os.environ.get("GITHUB_RUN_ID", "")
            index = fetch(GitHubAPI.from_environment(), args.out, current_run_id=int(run_id) if run_id.isdigit() else None,
                          snapshot_covered=covered)
            read = sum(1 for a in index["artifacts"] if a["status"] == STATUS_READ)
            print(f"ledger fetch: {len(index['artifacts'])} artifacts listed, {read} downloaded, "
                  f"{len(index['problems'])} problem(s), {len(index['active_runs'])} other active run(s)")
            return EXIT_OK
        if args.command == "compute":
            ledger = compute(repo_root=args.repo_root, inputs=args.inputs, directories=args.directories,
                             snapshot=None if args.no_snapshot else args.snapshot)
            Path(args.json).write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if args.markdown:
                Path(args.markdown).write_text(markdown(ledger), encoding="utf-8")
            print(f"spent so far: ${ledger['total_usd']:.4f} of ${ledger['hard_limit_usd']:.2f}; remaining "
                  f"${ledger['remaining_usd']:.4f}; complete: {ledger['complete']}")
            for problem in ledger["problems"]:
                print(f"PROBLEM: {problem}")
            return EXIT_OK
        if args.command == "cap":
            cap, what = _cap_from_args(args)
            print(f"{cap:.2f}")
            print(f"cap of {what}: ${cap:.2f}", file=sys.stderr)
            return EXIT_OK
        if args.command == "check":
            ledger = read_ledger_file(args.ledger)
            cap, what = _cap_from_args(args)
            result = check(ledger, cap, what=what)
            ledger["check"] = result
            Path(args.ledger).write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if args.markdown:
                Path(args.markdown).write_text(markdown(ledger), encoding="utf-8")
            if result["allowed"]:
                print(f"ALLOWED: {what}: spent ${result['total_usd']:.4f} + cap ${cap:.2f} = ${result['after_cap_usd']:.4f} "
                      f"<= ${result['hard_limit_usd']:.2f}")
                return EXIT_OK
            for reason in result["reasons"]:
                print(f"::error::REFUSED by the budget ledger: {reason}")
            return EXIT_REFUSED
    except (LedgerError, OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
