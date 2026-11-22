"""Decision rules for H1-H4 (PREREGISTRATION.md section 11), per model.

Each ``decide_*`` function is a pure function of point estimates (exact
``Fraction`` values, ``None`` when undefined) and bootstrap intervals, so that
the rules can be tested at their boundary values. Thresholds come from
``binding_gate.stats.prereg``.

Status values
-------------
``SUPPORTED``      every criterion of the hypothesis holds.
``NOT SUPPORTED``  at least one criterion fails. A criterion whose quantity is
                   undefined (for example M1 of an arm that answered nothing, or
                   a relative reduction from a reference M1 of zero) fails.
``TRADE-OFF``      H3 only: the gate is better on one of its two quantities and
                   worse on the other (section 11). Not support.
``NOT EVALUABLE``  the arms or the layer the hypothesis needs are not in the data.

Whether a result is **confirmatory** is decided separately (see
``binding_gate.stats.analysis``): test split, complete data, the pre-registered
number of repeats and bootstrap parameters, finished runs, and the validity guard
of section 6.4 item 6 for every arm involved. A non-confirmatory result keeps its
status but is labelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from binding_gate.stats import prereg
from binding_gate.stats.bootstrap import Interval

SUPPORTED = "SUPPORTED"
NOT_SUPPORTED = "NOT SUPPORTED"
TRADE_OFF = "TRADE-OFF"
NOT_EVALUABLE = "NOT EVALUABLE"


def _show(value: Fraction | None) -> str:
    return "undefined" if value is None else f"{float(value):.4f} ({value})"


@dataclass(frozen=True)
class Criterion:
    name: str
    rule: str
    observed: str
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class HypothesisResult:
    hypothesis: str
    status: str
    criteria: list[Criterion] = field(default_factory=list)
    arms: tuple[str, ...] = ()
    confirmatory: bool = False
    non_confirmatory_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sensitivity: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis,
            "status": self.status,
            "confirmatory": self.confirmatory,
            "non_confirmatory_reasons": list(self.non_confirmatory_reasons),
            "arms": list(self.arms),
            "criteria": [c.to_dict() for c in self.criteria],
            "notes": list(self.notes),
            "sensitivity": self.sensitivity,
        }


def not_evaluable(hypothesis: str, reason: str) -> HypothesisResult:
    return HypothesisResult(hypothesis, NOT_EVALUABLE, notes=[reason])


def _interval_text(interval: Interval | None) -> str:
    if interval is None or interval.high is None:
        return "no interval"
    text = f"[{interval.low:.4f}, {interval.high:.4f}]"
    if interval.undefined:
        text += f" ({interval.undefined} undefined resamples left out)"
    return text


def decide_h1(
    *,
    m1_arm: Fraction | None,
    m1_ref: Fraction | None,
    wrong_all_arm: Fraction | None,
    wrong_all_ref: Fraction | None,
    diff_m1_interval: Interval | None,
) -> tuple[str, list[Criterion]]:
    """H1: relative reduction of M1 >= threshold; CI of M1(a) - M1(ref) entirely below 0;
    wrong per all records of the arm not above the reference."""
    relred = None if m1_arm is None or m1_ref is None or m1_ref == 0 else 1 - m1_arm / m1_ref
    threshold = prereg.H1_MIN_RELATIVE_REDUCTION
    criteria = [
        Criterion(
            "relative_reduction",
            f"1 - M1(arm)/M1(reference) >= {float(threshold):.0%} (point estimate)",
            f"M1(arm) = {_show(m1_arm)}, M1(reference) = {_show(m1_ref)}, reduction = {_show(relred)}",
            relred is not None and relred >= threshold,
        ),
        Criterion(
            "interval_excludes_zero",
            f"{float(prereg.CI_LEVEL):.0%} interval of M1(arm) - M1(reference) lies entirely below 0",
            _interval_text(diff_m1_interval),
            diff_m1_interval is not None and diff_m1_interval.excludes_zero_below(),
        ),
        Criterion(
            "wrong_per_all_not_increased",
            "wrong-entity / all records: arm <= reference (point estimate)",
            f"arm = {_show(wrong_all_arm)}, reference = {_show(wrong_all_ref)}",
            wrong_all_arm is not None and wrong_all_ref is not None and wrong_all_arm <= wrong_all_ref,
        ),
    ]
    return (SUPPORTED if all(c.passed for c in criteria) else NOT_SUPPORTED), criteria


def decide_h2(*, m2_arm: Fraction | None) -> tuple[str, list[Criterion]]:
    """H2: M2 of the gated arm <= threshold (point estimate)."""
    threshold = prereg.H2_MAX_CORRECT_BLOCKED
    criterion = Criterion(
        "correct_answers_blocked",
        f"M2(arm) <= {float(threshold):.0%} (point estimate)",
        f"M2(arm) = {_show(m2_arm)}",
        m2_arm is not None and m2_arm <= threshold,
    )
    return (SUPPORTED if criterion.passed else NOT_SUPPORTED), [criterion]


def decide_h3(
    *,
    wrong_all_gated: Fraction | None,
    wrong_all_other: Fraction | None,
    diff_wrong_all_interval: Interval | None,
    m2_gated: Fraction | None,
    m2_other: Fraction | None,
) -> tuple[str, list[Criterion]]:
    """H3: gated vs faithfulness checker.

    * better on errors: wrong per all records lower for gated, with the interval of
      the paired difference (gated - other) entirely below 0;
    * not worse on cost: M2(gated) <= M2(other).

    Both: SUPPORTED. Better on errors but M2 higher, or worse on errors (point
    estimate higher) but M2 lower: TRADE-OFF. Anything else: NOT SUPPORTED.
    """
    defined = None not in (wrong_all_gated, wrong_all_other)
    better_errors = (
        defined
        and wrong_all_gated < wrong_all_other  # type: ignore[operator]
        and diff_wrong_all_interval is not None
        and diff_wrong_all_interval.excludes_zero_below()
    )
    worse_errors = defined and wrong_all_gated > wrong_all_other  # type: ignore[operator]
    m2_defined = None not in (m2_gated, m2_other)
    m2_not_higher = m2_defined and m2_gated <= m2_other  # type: ignore[operator]
    m2_lower = m2_defined and m2_gated < m2_other  # type: ignore[operator]
    criteria = [
        Criterion(
            "fewer_wrong_per_all",
            "wrong-entity / all records lower for gated, interval of (gated - other) entirely below 0",
            f"gated = {_show(wrong_all_gated)}, other = {_show(wrong_all_other)}, "
            f"interval {_interval_text(diff_wrong_all_interval)}",
            bool(better_errors),
        ),
        Criterion(
            "m2_not_higher",
            "M2(gated) <= M2(other) (point estimate)",
            f"gated = {_show(m2_gated)}, other = {_show(m2_other)}",
            bool(m2_not_higher),
        ),
    ]
    if better_errors and m2_not_higher:
        status = SUPPORTED
    elif (better_errors and m2_defined and not m2_not_higher) or (worse_errors and m2_lower):
        status = TRADE_OFF
    else:
        status = NOT_SUPPORTED
    return status, criteria


def decide_h4(*, m3_arm: Fraction | None, m3_ref: Fraction | None) -> tuple[str, list[Criterion]]:
    """H4: M3(gated_research) - M3(no_gate) >= threshold (point estimate)."""
    diff = None if m3_arm is None or m3_ref is None else m3_arm - m3_ref
    threshold = prereg.H4_MIN_NET_DIFFERENCE
    criterion = Criterion(
        "net_correct_not_lower",
        f"M3(arm) - M3(reference) >= {threshold} (point estimate)",
        f"M3(arm) = {_show(m3_arm)}, M3(reference) = {_show(m3_ref)}, difference = {_show(diff)}",
        diff is not None and diff >= threshold,
    )
    return (SUPPORTED if criterion.passed else NOT_SUPPORTED), [criterion]
