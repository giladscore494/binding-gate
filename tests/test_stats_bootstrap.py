"""The cluster bootstrap: determinism, cluster resampling, quantiles, undefined statistics."""

from __future__ import annotations

from fractions import Fraction

import pytest

import stats_helpers as sh
from binding_gate.stats import prereg
from binding_gate.stats.bootstrap import (
    Diff,
    Ratio,
    RelativeReduction,
    StatsError,
    cluster_bootstrap,
    percentile_interval,
    quantile_type7,
    resample_counts,
)
from binding_gate.stats.metrics import all_statistics, arm_key, build_model_table

LEVEL = Fraction(95, 100)


def test_resampling_sequence_is_a_fixed_golden_value():
    # Recorded on Python 3.13 and checked on every CI Python (3.11, 3.13): the
    # resampling must not depend on the interpreter version or the platform.
    assert list(resample_counts(5, 3, prereg.BOOTSTRAP_SEED)) == [
        [1, 0, 1, 2, 1],
        [1, 0, 2, 1, 1],
        [0, 1, 1, 2, 1],
    ]
    interval = cluster_bootstrap(
        [[1, 2], [0, 2], [2, 2], [1, 2]], {"r": Ratio((0,), (1,))}, resamples=2000, seed=7, level=LEVEL
    )["r"]
    assert (interval.low, interval.high, interval.defined, interval.undefined) == (0.125, 0.875, 2000, 0)


def test_same_seed_same_interval_different_seed_different_draws():
    rows = [[k % 3, 3] for k in range(40)]
    stats = {"r": Ratio((0,), (1,))}
    first = cluster_bootstrap(rows, stats, resamples=500, seed=11, level=LEVEL)
    again = cluster_bootstrap(rows, stats, resamples=500, seed=11, level=LEVEL)
    assert first == again
    assert list(resample_counts(40, 5, 11)) != list(resample_counts(40, 5, 12))


def test_each_resample_draws_exactly_one_full_set_of_clusters():
    for counts in resample_counts(7, 50, 3):
        assert len(counts) == 7 and sum(counts) == 7 and min(counts) >= 0


def test_clusters_not_items_are_resampled():
    # Two groups of 10 items: one group always wrong, the other never. Resampling
    # whole groups can only give wrong rates 0, 1/2 or 1; resampling items
    # independently would give a narrow interval around 1/2.
    spec = {"no_gate": ["wrong_entity"] * 10 + ["target_grounded"] * 10}
    table = build_model_table(
        sh.grid(spec, items_per_group=10), reference="no_gate", correct_outcomes=["target_grounded"]
    )
    rows = table.cluster_rows()
    assert len(rows) == 2  # items are summed into their group's row
    stats = {"wrong_all": all_statistics(table)[arm_key("no_gate", "wrong_all")]}
    interval = cluster_bootstrap(rows, stats, resamples=2000, seed=1, level=LEVEL)["wrong_all"]
    assert (interval.low, interval.high) == (0.0, 1.0)


def test_paired_differences_use_the_same_resample_for_both_arms():
    # The arm is identical to the reference: with joint resampling the paired
    # difference is exactly 0 in every resample.
    outcomes = ["wrong_entity", "target_grounded", "abstention", "ungrounded"] * 10
    table = build_model_table(
        sh.grid({"no_gate": outcomes, "gated": outcomes}), reference="no_gate", correct_outcomes=["target_grounded"]
    )
    stats = all_statistics(table)
    key = "cmp/gated/no_gate/diff_m1"
    interval = cluster_bootstrap(table.cluster_rows(), {key: stats[key]}, resamples=300, seed=5, level=LEVEL)[key]
    assert (interval.low, interval.high) == (0.0, 0.0)


def test_type7_quantile_matches_hand_computation():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert quantile_type7(values, Fraction(0)) == 1.0
    assert quantile_type7(values, Fraction(1)) == 5.0
    assert quantile_type7(values, Fraction(1, 4)) == 2.0
    assert quantile_type7(values, Fraction(1, 10)) == pytest.approx(1.4)
    # 10,000 values 0..9999: h = 9999 * 0.025 = 249.975 -> 249 + 0.975
    big = [float(v) for v in range(10_000)]
    interval = percentile_interval(big, 0, LEVEL)
    assert interval.low == pytest.approx(249.975) and interval.high == pytest.approx(9749.025)


def test_undefined_statistics_are_left_out_and_counted():
    # Cluster 0 has no answered record; whenever only it is drawn, the ratio is undefined.
    rows = [[0, 0], [1, 2]]
    interval = cluster_bootstrap(rows, {"m1": Ratio((0,), (1,))}, resamples=400, seed=2, level=LEVEL)["m1"]
    assert interval.undefined > 0 and interval.defined + interval.undefined == 400
    assert interval.low == interval.high == 0.5
    empty = percentile_interval([], 3, LEVEL)
    assert (empty.low, empty.high, empty.undefined) == (None, None, 3)


def test_expressions_exact_and_float_agree_and_handle_zero():
    totals = [1, 4, 3, 4]
    a, b = Ratio((0,), (1,)), Ratio((2,), (3,))
    assert a.evaluate(totals, exact=True) == Fraction(1, 4)
    assert Diff(a, b).evaluate(totals, exact=True) == Fraction(-1, 2)
    assert RelativeReduction(a, b).evaluate(totals, exact=True) == Fraction(2, 3)
    assert RelativeReduction(a, b).evaluate(totals, exact=False) == pytest.approx(2 / 3)
    assert Ratio((0,), (2,)).evaluate([1, 0, 0], exact=True) is None
    assert RelativeReduction(a, Ratio((2,), (1,))).evaluate([1, 4, 0], exact=True) is None  # reference 0
    assert Ratio((0, 2), (1, 3)).evaluate(totals, exact=True) == Fraction(4, 8)


def test_bad_inputs_are_refused():
    with pytest.raises(StatsError):
        cluster_bootstrap([], {}, resamples=10, seed=1, level=LEVEL)
    with pytest.raises(StatsError):
        cluster_bootstrap([[1, 2], [1]], {}, resamples=10, seed=1, level=LEVEL)
    with pytest.raises(StatsError):
        cluster_bootstrap([[1, 2]], {}, resamples=0, seed=1, level=LEVEL)


def test_full_preregistered_bootstrap_is_reasonably_fast():
    import time

    outcomes = ["wrong_entity", "target_grounded", "abstention", "unparseable", "ungrounded"] * 172
    spec = {arm: outcomes for arm in ("no_gate", "gated", "faithfulness_checker", "prompt_entity_check")}
    table = build_model_table(sh.grid(spec, items_per_group=10), reference="no_gate", correct_outcomes=["target_grounded"])
    assert len(table.cluster_rows()) == 86
    stats = all_statistics(table, [("gated", "faithfulness_checker")])
    start = time.perf_counter()
    cluster_bootstrap(
        table.cluster_rows(), stats, resamples=prereg.BOOTSTRAP_RESAMPLES, seed=prereg.BOOTSTRAP_SEED, level=LEVEL
    )
    assert time.perf_counter() - start < 60
