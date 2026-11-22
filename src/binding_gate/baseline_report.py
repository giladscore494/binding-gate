"""PR 2 baseline report: the stop rule of PREREGISTRATION.md section 10, from traces only.

Reads ``traces.jsonl`` of one or more run directories (for example the per-model
artifacts of the GitHub Actions workflow) and computes, per model, for the
``no_gate`` arm on the dev split:

* **R** = wrong-entity answers / all records, over the 12 conditions whose
  competing-document condition is not ``null_control`` (all repeats pooled; with
  complete data this equals the mean over items of the per-item average);
* the realised counts (groups, items, repeats, records) and the full outcome
  table of section 6.3;
* the **measurement check**: the share of ALL dev records that are ``ungrounded``
  or ``unparseable``; above 20% the stop rule is not applied mechanically;
* the stop-rule outcome per model and overall.

No model is called and nothing is re-scored: outcomes are taken from the
``provenance`` score stored in each record. Records with an ``error`` count as
``error`` and stay in every denominator. This module is deliberately not part of
``stats/`` (PR 6): it implements only the section 10 decision.

Thresholds are the PROPOSED values of section 10 and still need owner approval.
"""

from __future__ import annotations

import re

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from binding_gate.contracts import Split, TraceRecord
from binding_gate.trace import TraceError, read_manifest, read_traces

CONTINUE_PERCENT = 10
"""Section 10: continue if at least one model has R >= 10%."""
CARRY_PERCENT = 5
"""Section 10: models at or above 5% are carried forward; stop if every model is below 5%."""
MEASUREMENT_CHECK_PERCENT = 20
"""Section 10: more than 20% ungrounded-or-unparseable records triggers the measurement check."""

NULL_CONTROL = "null_control"
PROVENANCE = "provenance"
OUTCOMES = (
    "target_grounded",
    "wrong_entity",
    "ambiguous",
    "ungrounded",
    "abstention",
    "unparseable",
    "error",
)

CONTINUE = "CONTINUE"
STOP = "STOP"
IN_BETWEEN = "IN-BETWEEN"
MEASUREMENT_CHECK = "MEASUREMENT-CHECK"


class ReportError(RuntimeError):
    """Raised when the traces cannot support the baseline report."""


_ERROR_MESSAGE_CHARS = 300
_SECRET_LIKE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,}|Bearer\s+\S+|key=[^&\s]+)")


def summarise_errors(errors: Iterable[str], limit: int = 3) -> list[dict[str, Any]]:
    """The ``limit`` most frequent distinct error messages, truncated and scrubbed of key-like strings."""
    counts: Counter[str] = Counter()
    for error in errors:
        message = _SECRET_LIKE.sub("[redacted]", " ".join(error.split()))[:_ERROR_MESSAGE_CHARS]
        counts[message] += 1
    return [{"count": n, "message": msg} for msg, n in counts.most_common(limit)]


NOT_APPLICABLE = "not_applicable"
"""PR 22: the ``provenance`` outcome of a free-form answer (arm ``free_form``); not a section 6.3 category."""


def record_outcome(record: TraceRecord) -> str:
    """The section 6.3 outcome category of one record.

    PR 22: a free-form answer (``provenance`` outcome ``not_applicable``) is REFUSED with
    :class:`ReportError`: it is exploratory (PREREGISTRATION.md section 18, amendment 1), scored by
    the LLM judge only, and must never enter the stop rule or the statistics of H1-H4 (which all
    read outcomes through this function).
    """
    if record.error is not None:
        return "error"
    for score in record.scores:
        if score.scorer == PROVENANCE:
            outcome = score.details.get("outcome")
            if outcome in OUTCOMES:
                return outcome
            if outcome == NOT_APPLICABLE:
                raise ReportError(
                    f"record {record.item_id!r} (arm {record.arm!r}, model {record.model!r}) is a free-form answer "
                    "that the deterministic scorer does not score (provenance 'not_applicable'). Free-form runs are "
                    "exploratory (PREREGISTRATION.md section 18, amendment 1) and are analysed only by "
                    "`python -m binding_gate.validation`, never by the stop rule or the statistics of H1-H4."
                )
            return "error"  # the scorer itself failed (label "scorer_error")
    raise ReportError(
        f"record {record.item_id!r} ({record.model}, repeat {record.repeat}) has no provenance score"
    )


def _condition(record: TraceRecord) -> tuple[str, str]:
    """``(cx_level, cy_level)`` from the stratum ``"<cx_level>/<cy_level>"``."""
    parts = (record.stratum or "").split("/")
    if len(parts) != 2 or not all(parts):
        raise ReportError(f"record {record.item_id!r} has no '<cx_level>/<cy_level>' stratum")
    return parts[0], parts[1]


def _percent(numerator: int, denominator: int) -> str:
    return "n/a" if denominator == 0 else f"{100 * numerator / denominator:.1f}%"


@dataclass
class ModelBaseline:
    """Everything the report states about one model."""

    model: str
    served_models: list[str] = field(default_factory=list)
    n_groups: int = 0
    n_items: int = 0
    repeats: list[int] = field(default_factory=list)
    n_records: int = 0
    complete: bool = False
    outcomes_all: dict[str, int] = field(default_factory=dict)
    n_relevant: int = 0
    outcomes_relevant: dict[str, int] = field(default_factory=dict)
    by_condition: dict[str, dict[str, int]] = field(default_factory=dict)
    source_id_mismatches: int = 0
    truncated_calls: int = 0
    failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    calls_reporting_reasoning: int = 0
    paid_calls: int = 0
    decision: str = ""
    # PR 20: the most frequent distinct error messages (scrubbed, truncated), so an
    # operator can diagnose a failing configuration from the report alone.
    top_errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def wrong_entity(self) -> int:
        return self.outcomes_relevant.get("wrong_entity", 0)

    @property
    def unmeasured(self) -> int:
        """Ungrounded plus unparseable records over all dev conditions."""
        return self.outcomes_all.get("ungrounded", 0) + self.outcomes_all.get("unparseable", 0)

    @property
    def measurement_check(self) -> bool:
        return self.unmeasured * 100 > MEASUREMENT_CHECK_PERCENT * self.n_records

    def rate_at_least(self, percent: int) -> bool:
        return self.wrong_entity * 100 >= percent * self.n_relevant

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["R"] = None if not self.n_relevant else self.wrong_entity / self.n_relevant
        data["wrong_entity_relevant"] = self.wrong_entity
        data["ungrounded_or_unparseable_share"] = (
            None if not self.n_records else self.unmeasured / self.n_records
        )
        data["measurement_check"] = self.measurement_check
        return data


def _model_decision(model: ModelBaseline) -> str:
    if model.measurement_check:
        return MEASUREMENT_CHECK
    if model.rate_at_least(CONTINUE_PERCENT):
        return CONTINUE
    if not model.rate_at_least(CARRY_PERCENT):
        return STOP
    return IN_BETWEEN


def overall_decision(models: Sequence[ModelBaseline]) -> str:
    """The section 10 outcome for the study.

    Models under a measurement check are not decided mechanically. If a model
    without that flag reaches 10%, the study continues regardless. Otherwise any
    flagged model makes the overall outcome a measurement check (the owner
    decides, with the decision logged). With no flagged model: STOP if every
    model is below 5%, IN-BETWEEN if the best is between 5% and 10%.
    """
    if not models:
        raise ReportError("no model has baseline records")
    mechanical = [m for m in models if not m.measurement_check]
    if any(m.rate_at_least(CONTINUE_PERCENT) for m in mechanical):
        return CONTINUE
    if len(mechanical) < len(models):
        return MEASUREMENT_CHECK
    if all(not m.rate_at_least(CARRY_PERCENT) for m in mechanical):
        return STOP
    return IN_BETWEEN


@dataclass
class BaselineReport:
    arm: str
    run_dirs: list[str]
    unfinished_runs: list[str]
    models: list[ModelBaseline]
    overall: str
    provisional: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "run_dirs": self.run_dirs,
            "unfinished_runs": self.unfinished_runs,
            "thresholds_percent": {
                "continue": CONTINUE_PERCENT,
                "carry": CARRY_PERCENT,
                "measurement_check": MEASUREMENT_CHECK_PERCENT,
            },
            "models": [m.to_dict() for m in self.models],
            "overall": self.overall,
            "provisional": self.provisional,
        }

    def format(self) -> str:
        lines = [
            "BASELINE REPORT (PREREGISTRATION.md section 10) - dev split, arm "
            f"'{self.arm}', deterministic provenance scoring",
            f"run directories: {', '.join(self.run_dirs)}",
        ]
        if self.unfinished_runs:
            lines.append(f"WARNING: unfinished run(s): {', '.join(self.unfinished_runs)}")
        lines.append(
            f"Thresholds (PROPOSED, need owner approval): continue at R >= {CONTINUE_PERCENT}%, "
            f"carry models at R >= {CARRY_PERCENT}%, measurement check above "
            f"{MEASUREMENT_CHECK_PERCENT}% ungrounded-or-unparseable."
        )
        for m in self.models:
            lines += [
                "",
                f"== model {m.model}  (served as: {', '.join(m.served_models) or 'unknown'})",
                f"   realised counts : {m.n_groups} groups, {m.n_items} items, repeats {m.repeats}, "
                f"{m.n_records} records{'' if m.complete else '  ** INCOMPLETE: not every item has every repeat **'}",
                f"   R = wrong-entity / all records over the 12 non-null_control conditions = "
                f"{m.wrong_entity} / {m.n_relevant} = {_percent(m.wrong_entity, m.n_relevant)}",
                "   outcomes, 12 non-null_control conditions: "
                + ", ".join(f"{o} {m.outcomes_relevant.get(o, 0)}" for o in OUTCOMES),
                "   outcomes, all 15 conditions            : "
                + ", ".join(f"{o} {m.outcomes_all.get(o, 0)}" for o in OUTCOMES),
                f"   unparseable share (all records)        : "
                f"{_percent(m.outcomes_all.get('unparseable', 0), m.n_records)}",
                f"   ungrounded share (all records)         : "
                f"{_percent(m.outcomes_all.get('ungrounded', 0), m.n_records)}",
                f"   error share (all records)              : "
                f"{_percent(m.outcomes_all.get('error', 0), m.n_records)}",
                f"   measurement check: ungrounded + unparseable = {m.unmeasured} / {m.n_records} = "
                f"{_percent(m.unmeasured, m.n_records)} -> "
                f"{'TRIGGERED (> ' if m.measurement_check else 'not triggered (<= '}{MEASUREMENT_CHECK_PERCENT}%)",
                f"   quote found, but not in the cited source_id: {m.source_id_mismatches} record(s)",
                f"   model calls: {m.paid_calls} answered by the provider, {m.failed_calls} failed attempt(s), "
                f"{m.truncated_calls} truncated at the output limit",
                f"   tokens reported: input {m.input_tokens:,}, output {m.output_tokens:,} "
                f"(of which reasoning {m.reasoning_tokens:,}, itemised by {m.calls_reporting_reasoning} call(s))",
                *[f"   error x{e['count']}: {e['message']}" for e in m.top_errors],
                "   wrong-entity / records by condition (rows: competing document; columns: X document)",
            ]
            cx_levels = sorted({key.split("/")[0] for key in m.by_condition})
            cy_levels = sorted({key.split("/")[1] for key in m.by_condition})
            lines.append("      " + f"{'':<20}" + "".join(f"{cx:>14}" for cx in cx_levels))
            for cy in cy_levels:
                cells = []
                for cx in cx_levels:
                    cell = m.by_condition.get(f"{cx}/{cy}")
                    cells.append(f"{'-':>14}" if cell is None else f"{cell['wrong_entity']:>8}/{cell['n']:<5}")
                lines.append("      " + f"{cy:<20}" + "".join(cells))
            lines.append(f"   STOP-RULE OUTCOME for {m.model}: {m.decision}")
        lines += ["", f"OVERALL STOP-RULE OUTCOME: {self.overall}" + ("  (PROVISIONAL)" if self.provisional else "")]
        if self.provisional:
            lines.append(
                "PROVISIONAL because a run is unfinished or a model's records are incomplete; "
                "the stop rule is applied to complete baselines only."
            )
        explanation = {
            CONTINUE: "at least one model has R >= 10%. The study continues with the models at or above 5%.",
            STOP: "every model has R < 5%. The baseline is the project's finding; PRs 3-10 are not started.",
            IN_BETWEEN: "the best model is between 5% and 10%. Default: stop; the owner may continue "
            "as an exploratory study by logging the reason.",
            MEASUREMENT_CHECK: "no model reaches 10% mechanically and at least one model exceeds the "
            "measurement-check threshold. The owner decides, with the secondary judge's rate next to R.",
        }[self.overall]
        lines.append(f"Meaning: {explanation}")
        return "\n".join(lines)


def build_report(records: Iterable[TraceRecord], *, arm: str = "no_gate") -> list[ModelBaseline]:
    """Aggregate dev records of ``arm`` per model (pure function of the records)."""
    per_model: dict[str, list[TraceRecord]] = {}
    seen: set[tuple[str, str, str, int]] = set()
    for record in records:
        if record.split is not Split.DEV:
            raise ReportError("the baseline report is for dev runs only; a non-dev record was found")
        if record.arm != arm:
            continue
        key = (record.item_id, record.arm, record.model, record.repeat)
        if key in seen:
            raise ReportError(f"duplicate record for (item, arm, model, repeat) = {key}")
        seen.add(key)
        per_model.setdefault(record.model, []).append(record)

    results: list[ModelBaseline] = []
    for model_label in sorted(per_model):
        rows = per_model[model_label]
        result = ModelBaseline(model=model_label)
        outcomes_all: Counter[str] = Counter()
        outcomes_relevant: Counter[str] = Counter()
        served: set[str] = set()
        items = {r.item_id for r in rows}
        repeats = sorted({r.repeat for r in rows})
        for record in rows:
            outcome = record_outcome(record)
            cx_level, cy_level = _condition(record)
            outcomes_all[outcome] += 1
            cell = result.by_condition.setdefault(f"{cx_level}/{cy_level}", {"n": 0, "wrong_entity": 0})
            cell["n"] += 1
            cell["wrong_entity"] += outcome == "wrong_entity"
            if cy_level != NULL_CONTROL:
                outcomes_relevant[outcome] += 1
            for score in record.scores:
                if score.scorer == PROVENANCE and score.details.get("matched_document_ids"):
                    result.source_id_mismatches += score.details.get("source_id_matches") is False
            for call in record.model_calls:
                if call.response is None:
                    result.failed_calls += 1
                    continue
                raw = call.response.raw
                if raw.get("cache_hit") is True:
                    continue
                result.paid_calls += 1
                served.add(call.response.model)
                result.truncated_calls += raw.get("truncated") is True
                if call.response.usage is not None:
                    result.input_tokens += call.response.usage.input_tokens
                    result.output_tokens += call.response.usage.output_tokens
                reasoning = raw.get("reasoning_tokens")
                if isinstance(reasoning, int) and not isinstance(reasoning, bool):
                    result.reasoning_tokens += reasoning
                    result.calls_reporting_reasoning += 1
        result.top_errors = summarise_errors(r.error for r in rows if r.error is not None)
        result.served_models = sorted(served)
        result.n_groups = len({r.group_id for r in rows})
        result.n_items = len(items)
        result.repeats = repeats
        result.n_records = len(rows)
        result.complete = len(rows) == len(items) * len(repeats)
        result.outcomes_all = {o: outcomes_all.get(o, 0) for o in OUTCOMES}
        result.outcomes_relevant = {o: outcomes_relevant.get(o, 0) for o in OUTCOMES}
        result.n_relevant = sum(outcomes_relevant.values())
        if result.n_relevant == 0:
            raise ReportError(f"model {model_label!r} has no record outside null_control")
        result.decision = _model_decision(result)
        results.append(result)
    if not results:
        raise ReportError(f"no record of arm {arm!r} was found")
    return results


def report_from_run_dirs(run_dirs: Sequence[str | Path], *, arm: str = "no_gate") -> BaselineReport:
    """Read the traces of the given run directories and build the report."""
    if not run_dirs:
        raise ReportError("at least one run directory is required")
    records: list[TraceRecord] = []
    unfinished: list[str] = []
    for run_dir in run_dirs:
        run_dir = Path(run_dir)
        try:
            manifest = read_manifest(run_dir)
            records.extend(read_traces(run_dir))
        except (TraceError, OSError) as exc:
            raise ReportError(f"cannot read run directory {run_dir}: {exc}") from exc
        if manifest.ended_at is None:
            unfinished.append(run_dir.as_posix())
    models = build_report(records, arm=arm)
    return BaselineReport(
        arm=arm,
        run_dirs=[Path(d).as_posix() for d in run_dirs],
        unfinished_runs=unfinished,
        models=models,
        overall=overall_decision(models),
        provisional=bool(unfinished) or any(not m.complete for m in models),
    )


def write_json(report: BaselineReport, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
