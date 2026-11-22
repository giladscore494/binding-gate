"""From trace records to per-item column totals, and the metric definitions.

Definitions (PREREGISTRATION.md sections 6.3, 6.4, 7 and 8)
-----------------------------------------------------------
Every record (item x arm x model x repeat) has exactly one outcome category of
section 6.3: ``target_grounded``, ``wrong_entity``, ``ambiguous``,
``ungrounded``, ``abstention``, ``unparseable`` or ``error``.

* *answered*  = target_grounded + wrong_entity + ambiguous + ungrounded
* *failed*    = unparseable + error (never an answer, never dropped)
* *correct*   = target_grounded (layers 1 and 2; layer 3: PR 8's rule, a
  placeholder until then; see ``prereg.CORRECT_OUTCOMES_DEFAULT``)

**Analysis units.** For one model, the units are the ``(item, repeat)`` pairs
for which EVERY arm of the analysis has a record, so that all arms are compared
on exactly the same units (paired design). Records outside this set are counted
as ``unpaired_records``; any such record makes the analysis incomplete and
therefore not confirmatory. With a complete run (the runner writes a record,
possibly an error record, for every item x arm x model x repeat) nothing is
unpaired.

**Averaging over repeats (section 8).** For item ``i`` with paired repeats
``R_i``, the per-item value of an indicator ``X`` of arm ``a`` is
``x_i = (1/|R_i|) * sum over r in R_i of X(a, i, r)``. Column totals are sums of
``x_i`` over items, stored as integers ``S * x_i`` with
``S = lcm(|R_i| over all items)`` (``S = 3`` for complete data with 3 repeats).
With complete data every per-item average has the same weight, so these totals
equal the record-level pooled counts divided by the number of repeats.

Per arm ``a`` (sums over items; ``N`` = number of items):

* ``wrong``     sum of x_i for X = wrong-entity answer
* ``answered``  X = answered
* ``correct``   X = correct
* ``abstained`` X = abstention
* ``failed``    X = unparseable or error
* ``lost``      X = (reference arm correct AND arm ``a`` not correct) on the same
  (item, repeat). An unparseable or error record of ``a`` is therefore a lost
  correct answer whenever the reference was correct (section 6.4 item 3).
* ``research_first``, ``research_first_correct``: first attempt ended in a
  RESEARCH verdict; and, in addition, the final answer is correct (M4).

Metrics (each a ratio of column totals; the scale ``S`` cancels):

* **M1** wrong among answered       = wrong / answered
* **M2** correct answers blocked     = lost(a) / correct(reference)
* wrong per all records              = wrong / N
* correct (target-grounded) per all  = correct / N   (this is **M3** in layer 3)
* failed (unparseable + error) rate  = failed / N    (validity guard, 6.4 item 6)
* **M4** recovery after re-search    = research_first_correct / research_first
  (PLACEHOLDER until PR 8 fixes layer-3 correctness)
* paired difference of M1            = M1(a) - M1(ref); relative reduction
  = 1 - M1(a) / M1(ref)
* worst-case sensitivity bound (6.4 item 7) for an arm ``a`` against the
  reference: every failed record of ``a`` counted as a wrong-entity answer
  (``M1_worst(a) = (wrong + failed) / (answered + failed)``, wrong per all
  ``(wrong + failed) / N``), and every failed record of the reference counted
  as an answered, not-wrong record (``M1_best(ref) = wrong / (answered + failed)``).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

from binding_gate.baseline_report import OUTCOMES, record_outcome
from binding_gate.contracts import Decision, TraceRecord
from binding_gate.stats.bootstrap import Diff, Expr, Ratio, RelativeReduction, StatsError

ANSWERED = frozenset({"target_grounded", "wrong_entity", "ambiguous", "ungrounded"})
FAILED = frozenset({"unparseable", "error"})
ARM_COLUMNS = (
    "wrong",
    "answered",
    "correct",
    "abstained",
    "failed",
    "lost",
    "research_first",
    "research_first_correct",
)
ITEMS = "items"

OutcomeFn = Callable[[TraceRecord], str]


def first_attempt_research(record: TraceRecord) -> bool:
    if not record.attempts:
        return False
    verdict = record.attempts[0].verdict
    return verdict is not None and verdict.decision is Decision.RESEARCH


@dataclass(frozen=True)
class Columns:
    """Column index: ``(arm, column name)`` -> position, plus the item-count column."""

    arms: tuple[str, ...]
    index: dict[tuple[str, str], int]
    items: int

    @classmethod
    def build(cls, arms: Sequence[str]) -> Columns:
        index: dict[tuple[str, str], int] = {}
        for arm in arms:
            for name in ARM_COLUMNS:
                index[(arm, name)] = len(index)
        return cls(tuple(arms), index, len(index))

    @property
    def width(self) -> int:
        return self.items + 1

    def __call__(self, arm: str, name: str) -> int:
        return self.index[(arm, name)]


@dataclass
class ItemRow:
    item_id: str
    group_id: str
    stratum: str | None
    repeats: tuple[int, ...]
    values: list[int]  # scaled per-item averages; values[columns.items] == scale


@dataclass
class ModelTable:
    """Everything the statistics need about one model."""

    model: str
    dataset: str
    arms: tuple[str, ...]
    reference: str | None
    columns: Columns
    scale: int
    items: list[ItemRow]
    unit_values: dict[tuple[str, int], list[int]]  # (item, repeat) -> unscaled indicators
    n_records: int
    unpaired_records: int
    repeats: tuple[int, ...]
    complete_repeats: bool
    outcome_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    outcome_counts_by_condition: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    cost: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def groups(self) -> list[str]:
        return sorted({row.group_id for row in self.items})

    def cluster_rows(self) -> list[list[int]]:
        """One row of column totals per group, groups sorted by ``group_id``."""
        by_group: dict[str, list[int]] = {}
        for row in self.items:
            totals = by_group.setdefault(row.group_id, [0] * self.columns.width)
            for k, value in enumerate(row.values):
                totals[k] += value
        return [by_group[group] for group in sorted(by_group)]

    def totals(self, rows: Iterable[ItemRow] | None = None) -> list[int]:
        totals = [0] * self.columns.width
        for row in self.items if rows is None else rows:
            for k, value in enumerate(row.values):
                totals[k] += value
        return totals

    def repeat_totals(self, repeat: int) -> list[int]:
        """Unscaled totals over the units of one repeat index (for the spread across repeats)."""
        totals = [0] * self.columns.width
        for (item_id, r), values in self.unit_values.items():
            if r != repeat:
                continue
            for k, value in enumerate(values):
                totals[k] += value
        return totals


def build_model_table(
    records: Sequence[TraceRecord],
    *,
    reference: str,
    correct_outcomes: Iterable[str],
    outcome_of: OutcomeFn = record_outcome,
    expected_repeats: int | None = None,
) -> ModelTable:
    """Reduce the records of ONE model to per-item column totals (see the module docstring)."""
    if not records:
        raise StatsError("no records")
    models = {r.model for r in records}
    datasets = {r.dataset for r in records}
    if len(models) != 1 or len(datasets) != 1:
        raise StatsError("build_model_table needs the records of exactly one model and one dataset")
    correct = frozenset(correct_outcomes)
    unknown = correct - set(OUTCOMES)
    if unknown:
        raise StatsError(f"unknown outcome categories in correct_outcomes: {sorted(unknown)}")
    arms_present = sorted({r.arm for r in records})
    ref = reference if reference in arms_present else None
    arms = tuple(([ref] if ref else []) + [a for a in arms_present if a != ref])
    columns = Columns.build(arms)

    outcome: dict[tuple[str, str, int], str] = {}
    research: dict[tuple[str, str, int], bool] = {}
    meta: dict[str, tuple[str, str | None]] = {}
    outcome_counts: dict[str, Counter[str]] = {arm: Counter() for arm in arms}
    by_condition: dict[str, dict[str, Counter[str]]] = {arm: {} for arm in arms}
    cost: dict[str, Counter[str]] = {arm: Counter() for arm in arms}
    for record in records:
        key = (record.arm, record.item_id, record.repeat)
        if key in outcome:
            raise StatsError(f"duplicate record for (arm, item, repeat) = {key} of model {record.model!r}")
        known = meta.setdefault(record.item_id, (record.group_id, record.stratum))
        if known != (record.group_id, record.stratum):
            raise StatsError(f"item {record.item_id!r} appears with different group ids or strata")
        result = outcome_of(record)
        if result not in OUTCOMES:
            raise StatsError(f"unknown outcome {result!r} for item {record.item_id!r}")
        outcome[key] = result
        research[key] = first_attempt_research(record)
        outcome_counts[record.arm][result] += 1
        by_condition[record.arm].setdefault(record.stratum or "(none)", Counter())[result] += 1
        _add_cost(cost[record.arm], record)

    units_by_item: dict[str, list[int]] = {}
    all_units = {(item, r) for (_arm, item, r) in outcome}
    for item, r in sorted(all_units):
        if all((arm, item, r) in outcome for arm in arms):
            units_by_item.setdefault(item, []).append(r)
    n_paired_units = sum(len(rs) for rs in units_by_item.values())
    unpaired = len(records) - n_paired_units * len(arms)
    if not units_by_item:
        raise StatsError(f"model {records[0].model!r}: no (item, repeat) has a record for every arm")
    scale = math.lcm(*(len(rs) for rs in units_by_item.values()))

    unit_values: dict[tuple[str, int], list[int]] = {}
    rows: list[ItemRow] = []
    for item in sorted(units_by_item):
        repeats = tuple(units_by_item[item])
        sums = [0] * columns.width
        for r in repeats:
            values = [0] * columns.width
            values[columns.items] = 1
            ref_correct = ref is not None and outcome[(ref, item, r)] in correct
            for arm in arms:
                o = outcome[(arm, item, r)]
                is_correct = o in correct
                values[columns(arm, "wrong")] = int(o == "wrong_entity")
                values[columns(arm, "answered")] = int(o in ANSWERED)
                values[columns(arm, "correct")] = int(is_correct)
                values[columns(arm, "abstained")] = int(o == "abstention")
                values[columns(arm, "failed")] = int(o in FAILED)
                values[columns(arm, "lost")] = int(ref_correct and not is_correct and arm != ref)
                values[columns(arm, "research_first")] = int(research[(arm, item, r)])
                values[columns(arm, "research_first_correct")] = int(research[(arm, item, r)] and is_correct)
            unit_values[(item, r)] = values
            for k, value in enumerate(values):
                sums[k] += value
        factor = scale // len(repeats)
        group_id, stratum = meta[item]
        rows.append(ItemRow(item, group_id, stratum, repeats, [v * factor for v in sums]))

    all_repeats = tuple(sorted({r for (_a, _i, r) in outcome}))
    complete_repeats = all(len(row.repeats) == len(all_repeats) for row in rows) and (
        expected_repeats is None or len(all_repeats) == expected_repeats
    )
    return ModelTable(
        model=records[0].model,
        dataset=records[0].dataset,
        arms=arms,
        reference=ref,
        columns=columns,
        scale=scale,
        items=rows,
        unit_values=unit_values,
        n_records=len(records),
        unpaired_records=unpaired,
        repeats=all_repeats,
        complete_repeats=complete_repeats,
        outcome_counts={arm: {o: outcome_counts[arm].get(o, 0) for o in OUTCOMES} for arm in arms},
        outcome_counts_by_condition={
            arm: {cond: {o: c.get(o, 0) for o in OUTCOMES} for cond, c in sorted(by_condition[arm].items())}
            for arm in arms
        },
        cost={arm: dict(sorted(cost[arm].items())) for arm in arms},
    )


def _add_cost(counter: Counter[str], record: TraceRecord) -> None:
    for call in record.model_calls:
        if call.response is None:
            counter["failed_calls"] += 1
            continue
        if call.response.raw.get("cache_hit") is True:
            counter["cached_calls"] += 1
            continue
        counter["paid_calls"] += 1
        if call.response.usage is not None:
            counter["input_tokens"] += call.response.usage.input_tokens
            counter["output_tokens"] += call.response.usage.output_tokens


# --------------------------------------------------------------------------- #
# Statistic definitions
# --------------------------------------------------------------------------- #

ARM_STATS = ("m1", "m2", "wrong_all", "correct_all", "answered_all", "abstained_all", "failed_all", "m4")
COMPARISON_STATS = (
    "diff_m1",
    "relred_m1",
    "diff_wrong_all",
    "diff_correct_all",
    "m1_worst",
    "ref_m1_best",
    "diff_m1_worst",
    "relred_m1_worst",
    "wrong_all_worst",
    "diff_wrong_all_worst",
)
PAIR_STATS = ("diff_wrong_all", "diff_m2", "diff_m1")


def arm_key(arm: str, stat: str) -> str:
    return f"arm/{arm}/{stat}"


def comparison_key(arm: str, reference: str, stat: str) -> str:
    return f"cmp/{arm}/{reference}/{stat}"


def pair_key(a: str, b: str, stat: str) -> str:
    return f"pair/{a}/{b}/{stat}"


def arm_statistics(columns: Columns, arm: str, reference: str | None) -> dict[str, Expr]:
    c = columns
    stats: dict[str, Expr] = {
        "m1": Ratio((c(arm, "wrong"),), (c(arm, "answered"),)),
        "wrong_all": Ratio((c(arm, "wrong"),), (c.items,)),
        "correct_all": Ratio((c(arm, "correct"),), (c.items,)),
        "answered_all": Ratio((c(arm, "answered"),), (c.items,)),
        "abstained_all": Ratio((c(arm, "abstained"),), (c.items,)),
        "failed_all": Ratio((c(arm, "failed"),), (c.items,)),
        "m4": Ratio((c(arm, "research_first_correct"),), (c(arm, "research_first"),)),
    }
    if reference is not None and arm != reference:
        stats["m2"] = Ratio((c(arm, "lost"),), (c(reference, "correct"),))
    return stats


def comparison_statistics(columns: Columns, arm: str, reference: str) -> dict[str, Expr]:
    c = columns
    a = arm_statistics(columns, arm, reference)
    r = arm_statistics(columns, reference, None)
    m1_worst = Ratio((c(arm, "wrong"), c(arm, "failed")), (c(arm, "answered"), c(arm, "failed")))
    ref_m1_best = Ratio((c(reference, "wrong"),), (c(reference, "answered"), c(reference, "failed")))
    wrong_all_worst = Ratio((c(arm, "wrong"), c(arm, "failed")), (c.items,))
    return {
        "diff_m1": Diff(a["m1"], r["m1"]),
        "relred_m1": RelativeReduction(a["m1"], r["m1"]),
        "diff_wrong_all": Diff(a["wrong_all"], r["wrong_all"]),
        "diff_correct_all": Diff(a["correct_all"], r["correct_all"]),
        "m1_worst": m1_worst,
        "ref_m1_best": ref_m1_best,
        "diff_m1_worst": Diff(m1_worst, ref_m1_best),
        "relred_m1_worst": RelativeReduction(m1_worst, ref_m1_best),
        "wrong_all_worst": wrong_all_worst,
        "diff_wrong_all_worst": Diff(wrong_all_worst, r["wrong_all"]),
    }


def pair_statistics(columns: Columns, a: str, b: str, reference: str) -> dict[str, Expr]:
    sa = arm_statistics(columns, a, reference)
    sb = arm_statistics(columns, b, reference)
    return {
        "diff_wrong_all": Diff(sa["wrong_all"], sb["wrong_all"]),
        "diff_m2": Diff(sa["m2"], sb["m2"]),
        "diff_m1": Diff(sa["m1"], sb["m1"]),
    }


def all_statistics(table: ModelTable, pairs: Sequence[tuple[str, str]] = ()) -> dict[str, Expr]:
    """Every named statistic of a model: per arm, each arm vs the reference, and named arm pairs."""
    stats: dict[str, Expr] = {}
    for arm in table.arms:
        for name, expr in arm_statistics(table.columns, arm, table.reference).items():
            stats[arm_key(arm, name)] = expr
    if table.reference is not None:
        for arm in table.arms:
            if arm == table.reference:
                continue
            for name, expr in comparison_statistics(table.columns, arm, table.reference).items():
                stats[comparison_key(arm, table.reference, name)] = expr
        for a, b in pairs:
            if a in table.arms and b in table.arms and table.reference not in (a, b):
                for name, expr in pair_statistics(table.columns, a, b, table.reference).items():
                    stats[pair_key(a, b, name)] = expr
    return stats


def point_estimates(stats: Mapping[str, Expr], totals: Sequence[int]):
    """Exact point estimates (``Fraction`` or ``None`` when undefined)."""
    return {name: expr.evaluate(totals, exact=True) for name, expr in stats.items()}
