"""Dev-only calibration of the gate parameters from existing traces. No model is called.

Usage (from the repository root)::

    python -m binding_gate.gates.calibration RUN_DIR [RUN_DIR ...]
        [--arm gated] [--window-tokens 10,20,40,80] [--min-quote-tokens 5]
        [--lead-max-tokens 4] [--lead-min-repeats 2]
        [--premise-scope document] [--competitor-tie fail,pass]
        [--entity-scope window,window_or_document_subject] [--qualifier-scope ...]
        [--table-binding false,true] [--json FILE]

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

Binding-scope selection (PR 18; ``--select-scope``)
---------------------------------------------------
::

    python -m binding_gate.gates.calibration --select-scope RUN_DIR [RUN_DIR ...] [--json FILE]

applies the pre-registered selection rule of PREREGISTRATION.md section 5.1
("Binding scope") to the dev run(s) of ONE scope config
(``configs/dev_gated_scope.toml`` for layer 1, ``configs/dev_sec_gated_scope.toml``
for layer 2; several run directories = its per-model jobs). Those configs hold
one ``gated`` arm per candidate setting of :data:`gates.binding.BINDING_SCOPES`
(labels ``gated`` for ``window`` and ``gated_scope_<name>`` for the others),
identical except for ``binding_scope``, all judging the same seeded answers. The
command refuses anything but dev data, analyses the runs with the layer's own
rule (``stats.layers.analyse_layer``: M1 and M2 per model, exactly as the
frozen analysis computes them) and chooses (:func:`choose_scope`):

1. per candidate, ``M2max`` = the largest dev M2 over the models and ``M1mean``
   = the unweighted mean of the dev M1 over the models (exact fractions);
2. a candidate is *feasible* when ``M2max <= 0.08`` (:data:`SCOPE_M2_MAX`,
   PROPOSED: a margin below the H2 threshold of 0.10);
3. if some candidate is feasible, the chosen setting is the feasible one with
   the smallest ``M1mean``; ties by fewer enabled options
   (``gates.binding.enabled_scope_options``), then by the canonical order
   window, subject, table, subject_table;
4. if none is feasible, the chosen setting is the one with the smallest
   ``M2max`` (ties: smaller ``M1mean``, then as in 3), and the report states
   that H2 is at risk on this layer;
5. a candidate with an undefined M1 or M2 for some model is not feasible and is
   ranked last; if every candidate has an undefined ``M2max``, or a candidate
   is missing, nothing is chosen (exit 1).

It prints the chosen ``binding_scope`` and the one line to set in the gated arm
of the layer's frozen test config.
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

from fractions import Fraction

from binding_gate import registry
from binding_gate.config import ConfigError, RunConfig, parse_config
from binding_gate.contracts import AnswerStatus, Decision, Item, Split, TraceRecord
from binding_gate.gates.binding import (
    BINDING_SCOPES,
    CUSTOM_SCOPE,
    DEFAULT_PARAMETERS,
    SCOPE_PARAMETERS,
    enabled_scope_options,
    resolve_scope,
)
from binding_gate.gates.binding import NAME as GATE_NAME
from binding_gate.split import select_dev
from binding_gate.trace import read_manifest, read_traces

GRID_PARAMETERS = (
    "window_tokens",
    "min_quote_tokens",
    "lead_max_tokens",
    "lead_min_repeats",
    "premise_scope",
    "competitor_tie",
    "entity_scope",  # PR 18
    "qualifier_scope",  # PR 18
    "table_binding",  # PR 18
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
    explicit = dict(spec.options.get("parameters", {}))
    configured = {**DEFAULT_PARAMETERS, **explicit, **resolve_scope(explicit)}
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


# --------------------------------------------------------------------------- #
# Binding-scope selection (PR 18; PREREGISTRATION.md section 5.1, "Binding scope")
# --------------------------------------------------------------------------- #

SCOPE_M2_MAX = Fraction(8, 100)
"""PROPOSED: a candidate is feasible when its dev M2 is at most this for every model (margin below 0.10)."""
SCOPE_ARM_PREFIX = "gated_scope_"
SCOPE_RESAMPLES = 200
"""Bootstrap resamples of the analysis behind the selection; the rule uses point estimates only, which do not
depend on it (exact fractions of column totals)."""
FROZEN_TEST_CONFIGS: dict[int, tuple[str, ...]] = {
    1: ("configs/test_layer1.toml", "configs/test_layer1_midtier.toml"),
    2: ("configs/test_sec.toml",),
}
"""Where the chosen ``binding_scope`` is frozen (gated arm). Layer 1's mid-tier run is exploratory but must run
the same gate (tests/test_pr12_test_config.py checks that they agree)."""


def scope_arms(config: RunConfig) -> dict[str, str]:
    """``setting -> arm label`` of the candidate arms of a scope config (refuses anything ambiguous)."""
    arms = [a for a in config.arms if a.name == "gated" and (a.label == "gated" or a.label.startswith(SCOPE_ARM_PREFIX))]
    found: dict[str, str] = {}
    reference: dict[str, Any] | None = None
    for arm in arms:
        parameters = dict(arm.options.get("parameters", {}))
        setting = resolve_scope({k: v for k, v in parameters.items() if k in SCOPE_PARAMETERS})["binding_scope"]
        if setting == CUSTOM_SCOPE:
            raise CalibrationError(f"arm {arm.label!r}: its scope options match no candidate setting")
        if setting in found:
            raise CalibrationError(f"arms {found[setting]!r} and {arm.label!r} both have binding_scope {setting!r}")
        expected = "gated" if setting == "window" else f"{SCOPE_ARM_PREFIX}{setting}"
        if arm.label != expected:
            raise CalibrationError(f"arm {arm.label!r} has binding_scope {setting!r}; it must be labelled {expected!r}")
        rest = {**arm.options, "parameters": {k: v for k, v in parameters.items() if k not in SCOPE_PARAMETERS}}
        if reference is None:
            reference = rest
        elif rest != reference:
            raise CalibrationError(f"arm {arm.label!r} differs from the other candidates in more than binding_scope")
        found[setting] = arm.label
    missing = [s for s in BINDING_SCOPES if s not in found]
    if missing:
        raise CalibrationError(f"the run lacks the candidate setting(s) {missing}; the rule needs all of {list(BINDING_SCOPES)}")
    return {s: found[s] for s in BINDING_SCOPES}


@dataclass
class ScopeCandidate:
    setting: str
    arm: str
    m1: dict[str, Fraction | None]
    m2: dict[str, Fraction | None]

    @property
    def enabled(self) -> int:
        return enabled_scope_options(self.setting)

    @property
    def m2_max(self) -> Fraction | None:
        values = list(self.m2.values())
        return None if not values or any(v is None for v in values) else max(values)  # type: ignore[type-var]

    @property
    def m1_mean(self) -> Fraction | None:
        values = list(self.m1.values())
        return None if not values or any(v is None for v in values) else sum(values, Fraction(0)) / len(values)  # type: ignore[arg-type]

    @property
    def feasible(self) -> bool:
        return self.m2_max is not None and self.m1_mean is not None and self.m2_max <= SCOPE_M2_MAX

    def to_dict(self) -> dict[str, Any]:
        def fr(value: Fraction | None) -> dict[str, Any]:
            return {"value": None, "exact": None} if value is None else {
                "value": float(value), "exact": f"{value.numerator}/{value.denominator}"}

        return {
            "binding_scope": self.setting,
            "arm": self.arm,
            "enabled_options": self.enabled,
            "m1_by_model": {m: fr(v) for m, v in sorted(self.m1.items())},
            "m2_by_model": {m: fr(v) for m, v in sorted(self.m2.items())},
            "m1_mean": fr(self.m1_mean),
            "m2_max": fr(self.m2_max),
            "feasible": self.feasible,
        }


@dataclass
class ScopeSelection:
    layer: int | None
    run_dirs: list[str]
    candidates: list[ScopeCandidate]
    chosen: str
    case: str  # "feasible" or "none_feasible"

    @property
    def h2_at_risk(self) -> bool:
        return self.case == "none_feasible"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "binding_scope_selection",
            "rule": "PREREGISTRATION.md section 5.1, Binding scope (PR 18)",
            "split": "dev",
            "layer": self.layer,
            "run_dirs": self.run_dirs,
            "m2_max_threshold": f"{SCOPE_M2_MAX.numerator}/{SCOPE_M2_MAX.denominator}",
            "candidates": [c.to_dict() for c in self.candidates],
            "chosen": self.chosen,
            "case": self.case,
            "h2_at_risk": self.h2_at_risk,
            "freeze_line": f'binding_scope = "{self.chosen}"',
            "freeze_in": list(FROZEN_TEST_CONFIGS.get(self.layer or 0, ())),
        }

    def format(self) -> str:
        def pct(value: Fraction | None) -> str:
            return "undefined" if value is None else f"{100 * float(value):.2f}%"

        lines = [
            "BINDING-SCOPE SELECTION (dev split only; PREREGISTRATION.md section 5.1; no model was called)",
            f"layer {self.layer}; feasible = M2 <= {pct(SCOPE_M2_MAX)} for every model",
            "",
            f"{'binding_scope':<16}{'options':>8}{'M1 mean':>12}{'M2 max':>12}{'feasible':>10}   per model (M1 / M2)",
        ]
        for c in self.candidates:
            per = "; ".join(f"{m}: {pct(c.m1[m])} / {pct(c.m2.get(m))}" for m in sorted(c.m1))
            mark = " <= CHOSEN" if c.setting == self.chosen else ""
            lines.append(f"{c.setting:<16}{c.enabled:>8}{pct(c.m1_mean):>12}{pct(c.m2_max):>12}"
                         f"{('yes' if c.feasible else 'no'):>10}   {per}{mark}")
        lines.append("")
        if self.h2_at_risk:
            lines.append(f"No candidate is feasible: chosen by the smallest M2 max. H2 IS AT RISK on layer {self.layer}.")
        else:
            lines.append("Chosen among the feasible candidates by the smallest M1 mean.")
        targets = ", ".join(FROZEN_TEST_CONFIGS.get(self.layer or 0, ())) or "(no frozen test config for this layer)"
        lines.append(f'CHOSEN: binding_scope = "{self.chosen}"  -> set this line in the gated arm of {targets}')
        return "\n".join(lines)


def choose_scope(candidates: Sequence[ScopeCandidate]) -> tuple[str, str]:
    """``(chosen setting, case)`` by rules 1-5 of the module docstring."""
    order = {name: i for i, name in enumerate(BINDING_SCOPES)}
    big = Fraction(10**9)
    feasible = [c for c in candidates if c.feasible]
    if feasible:
        best = min(feasible, key=lambda c: (c.m1_mean, c.enabled, order[c.setting]))
        return best.setting, "feasible"
    defined = [c for c in candidates if c.m2_max is not None]
    if not defined:
        raise CalibrationError("no candidate has a defined M2 for every model; nothing can be chosen")
    best = min(defined, key=lambda c: (c.m2_max, c.m1_mean if c.m1_mean is not None else big, c.enabled, order[c.setting]))
    return best.setting, "none_feasible"


def candidates_from_analysis(analysis: dict[str, Any], arms: dict[str, str]) -> list[ScopeCandidate]:
    """M1 and M2 of each candidate arm, per model, from an analysis of ``stats.layers.analyse_layer``."""
    def exact(model: dict[str, Any], key: str) -> Fraction | None:
        entry = model["estimates"].get(key)
        return None if entry is None or entry.get("exact") is None else Fraction(entry["exact"])

    out = []
    for setting, arm in arms.items():
        m1, m2 = {}, {}
        for name, model in sorted(analysis["models"].items()):
            if arm not in model["arms"]:
                raise CalibrationError(f"model {name!r} has no records of arm {arm!r}")
            m1[name] = exact(model, f"arm/{arm}/m1")
            m2[name] = exact(model, f"arm/{arm}/m2")
        out.append(ScopeCandidate(setting, arm, m1, m2))
    return out


def select_scope(run_dirs: Sequence[str | Path]) -> ScopeSelection:
    """Apply the selection rule to the dev run(s) of one scope config (see the module docstring)."""
    from binding_gate.stats.layers import analyse_layer

    if not run_dirs:
        raise CalibrationError("give at least one run directory")
    configs = []
    for run_dir in run_dirs:
        manifest = read_manifest(run_dir)
        if manifest.config.get("split") != Split.DEV.value or manifest.test_unlock_tag is not None:
            raise CalibrationError(f"{run_dir}: refused - the scope selection reads dev runs only")
        try:
            configs.append(parse_config(manifest.config))
        except ConfigError as exc:
            raise CalibrationError(f"{run_dir}: manifest config is invalid: {exc}") from exc
        for record in read_traces(run_dir):
            if record.split is not Split.DEV:
                raise CalibrationError(f"{run_dir}: refused - record {record.item_id!r} is not from the dev split")
    first = configs[0]
    if any((c.name, c.dataset, c.arms) != (first.name, first.dataset, first.arms) for c in configs[1:]):
        raise CalibrationError("all run directories must come from the same scope config")
    arms = scope_arms(first)
    analysis, rule = analyse_layer(run_dirs, resamples=SCOPE_RESAMPLES)
    if analysis.get("split") != Split.DEV.value:  # pragma: no cover - checked above, per record
        raise CalibrationError("the analysis is not of the dev split")
    candidates = candidates_from_analysis(analysis, arms)
    chosen, case = choose_scope(candidates)
    return ScopeSelection(rule.layer if rule else None, [Path(d).as_posix() for d in run_dirs], candidates, chosen, case)


def _ints(text: str) -> list[int]:
    return [int(part) for part in text.split(",") if part.strip()]


def _words(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _bools(text: str) -> list[bool]:
    values = {"true": True, "false": False}
    words = _words(text.lower())
    if not words or any(w not in values for w in words):
        raise argparse.ArgumentTypeError("expected a comma-separated list of true / false")
    return [values[w] for w in words]


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
    parser.add_argument("--entity-scope", type=_words, default=None)  # PR 18
    parser.add_argument("--qualifier-scope", type=_words, default=None)  # PR 18
    parser.add_argument("--table-binding", type=_bools, default=None)  # PR 18
    parser.add_argument("--json", default=None, help="also write the report to this JSON file")
    parser.add_argument("--select-scope", action="store_true",
                        help="PR 18: apply the binding-scope selection rule (section 5.1) to the runs of a scope config")
    args = parser.parse_args(argv)
    if args.select_scope:
        try:
            selection = select_scope(args.run_dirs)
        except (CalibrationError, RuntimeError, ValueError, OSError) as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(selection.format())
        if args.json:
            Path(args.json).write_text(json.dumps(selection.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
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
