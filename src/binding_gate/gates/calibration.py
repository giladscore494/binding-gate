"""Dev-only calibration of the gate parameters from existing traces. No model is called.

Usage (from the repository root)::

    python -m binding_gate.gates.calibration RUN_DIR [RUN_DIR ...]
        [--arm gated] [--window-tokens 10,20,40,80] [--min-quote-tokens 5]
        [--lead-max-tokens 4] [--lead-min-repeats 2]
        [--premise-scope document] [--competitor-tie fail,pass] [--json FILE]

What it does
------------
1. **Refuses anything but dev.** A run directory whose manifest config has a
   split other than ``dev``, or that was started with a test unlock tag, is
   refused before its traces are opened; every trace record is checked again
   and the first record whose split is not ``dev`` aborts the whole command
   before anything is computed or printed. Items are loaded through
   ``split.select_dev`` only; the test lock is never touched.
2. Reads the records of the chosen gated arm (``--arm``, default ``gated``) that
   have an answered first candidate and recorded constraints. Records that are
   errors, abstentions or unparseable are counted and skipped (the gate never
   judges them). The candidate, the recorded constraints and the dev item's
   documents (checked against the trace's evidence hashes) are the inputs; the
   recorded decomposition is reused, so nothing is decomposed again.
3. For every point of the parameter grid (the cartesian product of the given
   values; unspecified parameters keep the arm's configured value) it re-runs
   the ``binding`` gate with the arm's check list and tallies the decisions,
   the failures per check, how many decisions differ from the recorded verdict,
   and - using the deterministic provenance scorer on the CANDIDATE (dev gold;
   this is analysis, not gating) - how many wrong-entity candidates
   (``competitor_only``) would be accepted and how many target-grounded
   candidates (``target_only``) would be blocked.

The row whose parameters equal the arm's configuration must reproduce the
recorded verdicts exactly (``changed_vs_recorded = 0``); this doubles as a
determinism check of the gate.

Calibration results are exploratory dev results. A parameter changed because of
them must be changed in the run config and in PREREGISTRATION.md section 5.1
before the freeze.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from binding_gate import registry
from binding_gate.config import ConfigError, RunConfig, parse_config
from binding_gate.contracts import AnswerStatus, Decision, Item, Split, TraceRecord
from binding_gate.gates.binding import DEFAULT_PARAMETERS, NAME as GATE_NAME
from binding_gate.split import select_dev
from binding_gate.trace import read_manifest, read_traces

GRID_PARAMETERS = (
    "window_tokens",
    "min_quote_tokens",
    "lead_max_tokens",
    "lead_min_repeats",
    "premise_scope",
    "competitor_tie",
)
PROVENANCE_LABELS = ("target_only", "competitor_only", "both", "neither", "unparseable")


class CalibrationError(RuntimeError):
    """Raised when the input is not usable dev data (or not dev data at all)."""


@dataclass(frozen=True)
class Case:
    record: TraceRecord
    item: Item


@dataclass
class GridRow:
    parameters: dict[str, Any]
    is_configured: bool
    n: int = 0
    decisions: Counter = field(default_factory=Counter)
    check_failures: Counter = field(default_factory=Counter)
    changed_vs_recorded: int = 0
    by_provenance: dict[str, Counter] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        accepted = {label: c["accept"] for label, c in sorted(self.by_provenance.items())}
        totals = {label: sum(c.values()) for label, c in sorted(self.by_provenance.items())}
        return {
            "parameters": self.parameters,
            "is_configured": self.is_configured,
            "n": self.n,
            "decisions": {d.value: self.decisions[d.value] for d in Decision},
            "check_failures": dict(sorted(self.check_failures.items())),
            "changed_vs_recorded": self.changed_vs_recorded,
            "candidates_by_provenance": totals,
            "accepted_by_provenance": accepted,
            "wrong_entity_accepted": accepted.get("competitor_only", 0),
            "target_grounded_blocked": totals.get("target_only", 0) - accepted.get("target_only", 0),
        }


@dataclass
class CalibrationReport:
    run_dirs: list[str]
    arm: str
    checks: list[str]
    configured: dict[str, Any]
    skipped: dict[str, int]
    rows: list[GridRow]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "gate_calibration",
            "split": "dev",
            "run_dirs": self.run_dirs,
            "arm": self.arm,
            "checks": self.checks,
            "configured_parameters": self.configured,
            "skipped_records": self.skipped,
            "rows": [row.to_dict() for row in self.rows],
        }

    def format(self) -> str:
        lines = [
            "GATE CALIBRATION (dev split only; exploratory; no model was called)",
            f"arm {self.arm!r}, checks {self.checks}",
            f"configured parameters: {json.dumps(self.configured, sort_keys=True)}",
            f"skipped records: {json.dumps(self.skipped, sort_keys=True)}",
            "",
            f"{'parameters':<70}{'n':>6}{'accept':>8}{'research':>10}{'abstain':>9}"
            f"{'changed':>9}{'wrong acc.':>11}{'target blk.':>12}",
        ]
        for row in self.rows:
            data = row.to_dict()
            varied = ", ".join(f"{k}={v}" for k, v in row.parameters.items())
            marker = " *" if row.is_configured else ""
            lines.append(
                f"{(varied + marker):<70}{data['n']:>6}{data['decisions']['accept']:>8}"
                f"{data['decisions']['research']:>10}{data['decisions']['abstain']:>9}"
                f"{data['changed_vs_recorded']:>9}{data['wrong_entity_accepted']:>11}"
                f"{data['target_grounded_blocked']:>12}"
            )
        lines += [
            "",
            "* = the arm's configured parameters (must show changed = 0).",
            "wrong acc. = candidates whose quote is only in a competitor document that the gate accepts;",
            "target blk. = candidates whose quote is only in a target document that the gate blocks.",
        ]
        return "\n".join(lines)


def _arm_spec(config: RunConfig, label: str):
    for spec in config.arms:
        if spec.label == label:
            return spec
    raise CalibrationError(f"the run has no arm labelled {label!r}; arms: {[a.label for a in config.arms]}")


def load_cases(run_dirs: Sequence[str | Path], arm: str) -> tuple[RunConfig, list[Case], Counter]:
    """Dev-only loading of the gated records and their items (see the module docstring)."""
    if not run_dirs:
        raise CalibrationError("give at least one run directory")
    configs: list[RunConfig] = []
    records: list[TraceRecord] = []
    for run_dir in run_dirs:
        manifest = read_manifest(run_dir)
        if manifest.config.get("split") != Split.DEV.value or manifest.test_unlock_tag is not None:
            raise CalibrationError(
                f"{run_dir}: refused - calibration reads dev runs only (manifest split "
                f"{manifest.config.get('split')!r}, test unlock tag {manifest.test_unlock_tag!r})"
            )
        try:
            config = parse_config(manifest.config)
        except ConfigError as exc:
            raise CalibrationError(f"{run_dir}: manifest config is invalid: {exc}") from exc
        configs.append(config)
        for record in read_traces(run_dir):
            if record.split is not Split.DEV:
                raise CalibrationError(
                    f"{run_dir}: refused - record {record.item_id!r} has split {record.split.value!r}; "
                    "calibration never reads test traces"
                )
            records.append(record)
    first = configs[0]
    for other in configs[1:]:
        if other.dataset != first.dataset or _arm_spec(other, arm) != _arm_spec(first, arm):
            raise CalibrationError("all run directories must share the dataset and the arm's configuration")
    _arm_spec(first, arm)

    adapter = registry.create("dataset", first.dataset.name, **first.dataset.options)
    items = {item.id: item for item in select_dev(adapter.iter_items())}
    skipped: Counter = Counter()
    cases: list[Case] = []
    for record in records:
        if record.arm != arm:
            continue
        if record.error is not None:
            skipped["error"] += 1
            continue
        answer = record.attempts[0].answer
        if answer.status is not AnswerStatus.ANSWERED:
            skipped[answer.status.value] += 1
            continue
        if record.constraints is None:
            skipped["no_constraints"] += 1
            continue
        item = items.get(record.item_id)
        if item is None:
            raise CalibrationError(f"item {record.item_id!r} of the trace is not a dev item of the dataset")
        shown = {(d.id, d.sha256) for d in item.documents}
        recorded = {(e.id, e.sha256) for e in record.attempts[0].evidence}
        if shown != recorded:
            raise CalibrationError(f"item {record.item_id!r}: documents differ from the trace's evidence hashes")
        cases.append(Case(record, item))
    return first, cases, skipped


def parameter_grid(configured: dict[str, Any], values: dict[str, list[Any]]) -> list[dict[str, Any]]:
    names = list(GRID_PARAMETERS)
    axes = [values.get(name) or [configured[name]] for name in names]
    return [dict(zip(names, combo)) for combo in itertools.product(*axes)]


def calibrate(
    run_dirs: Sequence[str | Path], *, arm: str = "gated", values: dict[str, list[Any]] | None = None
) -> CalibrationReport:
    config, cases, skipped = load_cases(run_dirs, arm)
    spec = _arm_spec(config, arm)
    checks = list(spec.options.get("checks", []))
    configured = {**DEFAULT_PARAMETERS, **dict(spec.options.get("parameters", {}))}
    scorer_spec = next((s for s in config.scorers if s.name == "provenance"), None)
    scorer = registry.create("scorer", "provenance", **(scorer_spec.options if scorer_spec else {}))
    labels = {
        id(case): scorer.score(case.item, case.record.attempts[0].answer).label for case in cases
    }
    rows: list[GridRow] = []
    for parameters in parameter_grid(configured, values or {}):
        gate = registry.create("gate", GATE_NAME, checks=checks, **parameters)
        row = GridRow(parameters=parameters, is_configured=parameters == {k: configured[k] for k in GRID_PARAMETERS})
        for case in cases:
            attempt = case.record.attempts[0]
            verdict = gate.evaluate(case.record.constraints, attempt.answer, case.item.documents)
            row.n += 1
            row.decisions[verdict.decision.value] += 1
            for result in verdict.check_results:
                if result.failed:
                    row.check_failures[result.check] += 1
            if attempt.verdict is None or attempt.verdict.decision is not verdict.decision:
                row.changed_vs_recorded += 1
            row.by_provenance.setdefault(labels[id(case)], Counter())[verdict.decision.value] += 1
        rows.append(row)
    return CalibrationReport(
        run_dirs=[Path(d).as_posix() for d in run_dirs],
        arm=arm,
        checks=checks,
        configured={k: configured[k] for k in GRID_PARAMETERS},
        skipped=dict(sorted(skipped.items())),
        rows=rows,
    )


def _ints(text: str) -> list[int]:
    return [int(part) for part in text.split(",") if part.strip()]


def _words(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m binding_gate.gates.calibration",
        description="Dev-only gate calibration from existing traces (no model calls).",
    )
    parser.add_argument("run_dirs", nargs="+", help="dev run directories")
    parser.add_argument("--arm", default="gated", help="label of the gated arm in the run config")
    parser.add_argument("--window-tokens", type=_ints, default=None)
    parser.add_argument("--min-quote-tokens", type=_ints, default=None)
    parser.add_argument("--lead-max-tokens", type=_ints, default=None)
    parser.add_argument("--lead-min-repeats", type=_ints, default=None)
    parser.add_argument("--premise-scope", type=_words, default=None)
    parser.add_argument("--competitor-tie", type=_words, default=None)
    parser.add_argument("--json", default=None, help="also write the report to this JSON file")
    args = parser.parse_args(argv)
    values = {name: getattr(args, name) for name in GRID_PARAMETERS if getattr(args, name) is not None}
    try:
        report = calibrate(args.run_dirs, arm=args.arm, values=values)
    except (CalibrationError, RuntimeError, ValueError, OSError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(report.format())
    if args.json:
        Path(args.json).write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
