"""Layer-2 stop rule (PR 22; PREREGISTRATION.md section 10, "Layer 2", and section 18, amendment 1).

Written and committed BEFORE the layer-2 dev baseline (``configs/dev_sec_baseline.toml``)
was scored or run. It mirrors the layer-1 rule of section 10 with the thresholds
of section 10 (**PROPOSED**), on the deterministic ``sec_answer`` labels of the
``no_gate`` records of the dev split, per model:

* **R2E** (wrong entity) = ``wrong_entity`` records / all records of the two
  conditions in which a peer company's filing is shown (``present/peer``,
  ``absent/peer``) - the analogue of layer 1's 12 non-``null_control`` conditions;
* **R2P** (wrong period or scale) = ``wrong_period_or_scale`` records / all
  records of all three conditions (another year of the same filing, or a scale
  error, is possible without a peer document);
* a model's rate for the rule is ``max(R2E, R2P)``.

Decision, per model and overall, exactly as ``binding_gate.baseline_report`` (section 10):
CONTINUE if a model not under a measurement check has a rate >= 10%; otherwise
MEASUREMENT-CHECK if a model is under it; STOP if every model is below 5% on both
rates; IN-BETWEEN otherwise (default stop; the owner may continue as an
exploratory study with a committed note and an amendment row). Measurement check:
more than 20% of a model's dev records are ``other_wrong`` or ``unparseable`` (the
labels under which the deterministic scorer cannot tell what went wrong).
Records with an ``error`` stay in every denominator.

What the decision governs: STOP ends layer 2 after its baseline - the gated dev
runs (``dev_sec_gated``, ``dev_sec_gated_scope``) of the ``layer2-dev`` stage are
not started and ``configs/test_sec.toml`` is not run; the baseline is published as
the layer-2 finding. CONTINUE (or a committed note ``results/dev_sec_baseline_continue.json``
after IN-BETWEEN or MEASUREMENT-CHECK) lets the stage go on.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from binding_gate.baseline_report import (
    CARRY_PERCENT,
    CONTINUE,
    CONTINUE_PERCENT,
    IN_BETWEEN,
    MEASUREMENT_CHECK,
    MEASUREMENT_CHECK_PERCENT,
    STOP,
    ReportError,
)
from binding_gate.contracts import Split, TraceRecord
from binding_gate.trace import TraceError, read_manifest, read_traces

SCORER = "sec_answer"
PEER_SHOWN = ("present/peer", "absent/peer")
UNMEASURED_LABELS = ("other_wrong", "unparseable")
LABELS = ("correct", "value_unsupported", "wrong_entity", "wrong_period_or_scale", "other_wrong", "abstained",
          "unparseable", "error")
CONTINUE_NOTE = "results/dev_sec_baseline_continue.json"
DECISION_ARTIFACT = "layer2-dev--decision"
RULE = "PREREGISTRATION.md section 10, layer 2 (binding_gate.layer2_stop); added by amendment 1 before the layer-2 baseline"


def record_label(record: TraceRecord) -> str:
    if record.error is not None:
        return "error"
    for score in record.scores:
        if score.scorer == SCORER:
            return score.label if score.label in LABELS else "error"
    raise ReportError(f"record {record.item_id!r} ({record.model}, repeat {record.repeat}) has no {SCORER} score")


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def model_rows(records: Iterable[TraceRecord], *, arm: str = "no_gate") -> list[dict[str, Any]]:
    """Per model: counts, R2E, R2P, measurement check and the model's decision."""
    per_model: dict[str, list[TraceRecord]] = {}
    seen: set[tuple[str, str, int]] = set()
    for record in records:
        if record.split is not Split.DEV:
            raise ReportError("the layer-2 stop rule is for dev runs only; a non-dev record was found")
        if record.arm != arm:
            continue
        key = (record.model, record.item_id, record.repeat)
        if key in seen:
            raise ReportError(f"duplicate record {key}")
        seen.add(key)
        per_model.setdefault(record.model, []).append(record)
    rows = []
    for model, group in sorted(per_model.items()):
        labels_all: Counter[str] = Counter()
        labels_peer: Counter[str] = Counter()
        by_stratum: dict[str, Counter[str]] = {}
        for record in group:
            label = record_label(record)
            labels_all[label] += 1
            by_stratum.setdefault(record.stratum or "(none)", Counter())[label] += 1
            if record.stratum in PEER_SHOWN:
                labels_peer[label] += 1
        n_all, n_peer = len(group), sum(labels_peer.values())
        complete = n_all == len({r.item_id for r in group}) * len({r.repeat for r in group})
        if n_peer == 0:
            raise ReportError(f"model {model!r} has no record of a condition with a peer filing ({', '.join(PEER_SHOWN)})")
        wrong_entity, period = labels_peer["wrong_entity"], labels_all["wrong_period_or_scale"]
        unmeasured = sum(labels_all[label] for label in UNMEASURED_LABELS)
        row = {
            "model": model,
            "n_records": n_all,
            "n_peer_shown": n_peer,
            "labels_all": {label: labels_all.get(label, 0) for label in LABELS},
            "labels_peer_shown": {label: labels_peer.get(label, 0) for label in LABELS},
            "by_stratum": {s: {label: c.get(label, 0) for label in LABELS} for s, c in sorted(by_stratum.items())},
            "wrong_entity_peer_shown": wrong_entity,
            "wrong_period_or_scale_all": period,
            "R2E": _rate(wrong_entity, n_peer),
            "R2P": _rate(period, n_all),
            "unmeasured": unmeasured,
            "unmeasured_share": _rate(unmeasured, n_all),
            "measurement_check": unmeasured * 100 > MEASUREMENT_CHECK_PERCENT * n_all,
            "complete": complete,
        }
        row["at_least_continue"] = (wrong_entity * 100 >= CONTINUE_PERCENT * n_peer) or (period * 100 >= CONTINUE_PERCENT * n_all)
        row["at_least_carry"] = (wrong_entity * 100 >= CARRY_PERCENT * n_peer) or (period * 100 >= CARRY_PERCENT * n_all)
        if row["measurement_check"]:
            row["decision"] = MEASUREMENT_CHECK
        elif row["at_least_continue"]:
            row["decision"] = CONTINUE
        elif not row["at_least_carry"]:
            row["decision"] = STOP
        else:
            row["decision"] = IN_BETWEEN
        rows.append(row)
    if not rows:
        raise ReportError(f"no record of arm {arm!r} was found")
    return rows


def overall(rows: Sequence[Mapping[str, Any]]) -> str:
    """The same combination rule as ``baseline_report.overall_decision`` (section 10)."""
    mechanical = [r for r in rows if not r["measurement_check"]]
    if any(r["at_least_continue"] for r in mechanical):
        return CONTINUE
    if len(mechanical) < len(rows):
        return MEASUREMENT_CHECK
    if all(not r["at_least_carry"] for r in mechanical):
        return STOP
    return IN_BETWEEN


def decision_from_run_dirs(run_dirs: Sequence[str | Path], *, workflow_run_id: int | str) -> dict[str, Any]:
    """The layer-2 decision file (``decision.json`` of the ``layer2-dev--decision`` artifact)."""
    if not run_dirs:
        raise ReportError("at least one run directory is required")
    records: list[TraceRecord] = []
    unfinished: list[str] = []
    for run_dir in run_dirs:
        try:
            manifest = read_manifest(run_dir)
            records.extend(read_traces(run_dir))
        except (TraceError, OSError) as exc:
            raise ReportError(f"cannot read run directory {run_dir}: {exc}") from exc
        if manifest.config.get("dataset", {}).get("name") != "sec_financials":
            raise ReportError(f"{run_dir}: not a layer-2 (sec_financials) run")
        if manifest.ended_at is None:
            unfinished.append(Path(run_dir).as_posix())
    rows = model_rows(records)
    complete = all(row["complete"] for row in rows)
    return {
        "decision_version": 1,
        "layer": 2,
        "workflow_run_id": int(workflow_run_id),
        "decision": overall(rows),
        "provisional": bool(unfinished) or not complete,
        "unfinished_runs": unfinished,
        "thresholds_percent": {"continue": CONTINUE_PERCENT, "carry": CARRY_PERCENT,
                               "measurement_check": MEASUREMENT_CHECK_PERCENT},
        "models": {r["model"]: r for r in rows},
        "rule": RULE,
    }


def decision_markdown(decision: Mapping[str, Any]) -> str:
    def pct(value: Any) -> str:
        return "-" if value is None else f"{100 * value:.1f}%"

    lines = [f"## Layer-2 stop rule (section 10, layer 2): **{decision['decision']}**"
             + (" (PROVISIONAL)" if decision["provisional"] else ""), "",
             f"workflow run {decision['workflow_run_id']}; rate of a model = max(R2E, R2P); continue at >= "
             f"{CONTINUE_PERCENT}%, stop below {CARRY_PERCENT}% (PROPOSED, as section 10)", "",
             "| model | decision | R2E (wrong entity, peer shown) | R2P (wrong period or scale, all) | records | other_wrong + unparseable |",
             "|---|---|---:|---:|---:|---:|"]
    for model, row in decision["models"].items():
        lines.append(f"| {model} | {row['decision']} | {pct(row['R2E'])} | {pct(row['R2P'])} | {row['n_records']} | "
                     f"{pct(row['unmeasured_share'])}{' (MEASUREMENT CHECK)' if row['measurement_check'] else ''} |")
    return "\n".join(lines) + "\n"


def gate(decision: Mapping[str, Any], *, notes_dir: str | Path = "results") -> tuple[bool, str]:
    """``(allowed, why)`` for the rest of layer 2 (``pipeline.dev_rest_gate`` with the layer-2 note)."""
    from binding_gate.pipeline import dev_rest_gate

    if decision.get("layer") != 2:
        return False, "not a layer-2 decision file"
    return dev_rest_gate(decision, notes_dir=notes_dir, note=CONTINUE_NOTE, what="the rest of layer 2")


def write(decision: Mapping[str, Any], json_path: str | Path, markdown_path: str | Path | None = None) -> None:
    Path(json_path).write_text(json.dumps(decision, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if markdown_path:
        Path(markdown_path).write_text(decision_markdown(decision), encoding="utf-8")
