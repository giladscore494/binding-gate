"""Metrics on hand-built traces whose answers are known exactly (PREREGISTRATION.md 6.4, 7, 8)."""

from __future__ import annotations

from fractions import Fraction

import pytest

import stats_helpers as sh
from binding_gate.contracts import Split
from binding_gate.stats.analysis import analyse
from binding_gate.stats.bootstrap import StatsError
from binding_gate.stats.metrics import all_statistics, build_model_table, point_estimates

T, W, A, U, E, N, B = "target_grounded", "wrong_entity", "abstention", "unparseable", "error", "ungrounded", "ambiguous"


def estimates(records, pairs=()):
    table = build_model_table(records, reference="no_gate", correct_outcomes=[T])
    return table, point_estimates(all_statistics(table, pairs), table.totals())


def test_primary_pair_and_rates_on_a_known_example():
    spec = {
        "no_gate": [T, T, T, T, W, W, W, A, U, E],
        "gated": [T, T, T, A, A, A, W, A, U, U],
    }
    table, est = estimates(sh.grid(spec))
    assert table.arms == ("no_gate", "gated") and table.reference == "no_gate"
    assert est["arm/no_gate/m1"] == Fraction(3, 7)  # 3 wrong among 7 answered; U and E not answered
    assert est["arm/no_gate/wrong_all"] == Fraction(3, 10)  # all-items denominator keeps U and E
    assert est["arm/no_gate/correct_all"] == Fraction(4, 10)
    assert est["arm/no_gate/failed_all"] == Fraction(2, 10)
    assert est["arm/no_gate/abstained_all"] == Fraction(1, 10)
    assert est["arm/gated/m1"] == Fraction(1, 4)
    assert est["arm/gated/m2"] == Fraction(1, 4)  # item 3: no_gate correct, gated abstained
    assert est["cmp/gated/no_gate/diff_m1"] == Fraction(1, 4) - Fraction(3, 7)
    assert est["cmp/gated/no_gate/relred_m1"] == Fraction(5, 12)
    assert est["cmp/gated/no_gate/diff_wrong_all"] == Fraction(-2, 10)
    # worst case (6.4 item 7): gated U counted wrong; no_gate U/E counted answered, not wrong
    assert est["cmp/gated/no_gate/m1_worst"] == Fraction(3, 6)
    assert est["cmp/gated/no_gate/ref_m1_best"] == Fraction(3, 9)
    assert est["cmp/gated/no_gate/relred_m1_worst"] == Fraction(-1, 2)
    assert est["cmp/gated/no_gate/wrong_all_worst"] == Fraction(3, 10)
    assert "arm/no_gate/m2" not in est  # the reference has no M2


def test_unparseable_and_error_are_lost_correct_answers():
    _table, est = estimates(sh.grid({"no_gate": [T, T, W], "gated": [U, E, A]}))
    assert est["arm/gated/m2"] == Fraction(1)
    assert est["arm/gated/m1"] is None  # gated answered nothing: M1 undefined, never 0
    assert est["arm/gated/wrong_all"] == 0 and est["arm/gated/failed_all"] == Fraction(2, 3)
    assert est["cmp/gated/no_gate/relred_m1"] is None


def test_ambiguous_and_ungrounded_are_answered_but_not_wrong():
    _table, est = estimates(sh.grid({"no_gate": [B, N, W, T]}))
    assert est["arm/no_gate/m1"] == Fraction(1, 4)
    assert est["arm/no_gate/answered_all"] == 1


def test_per_item_average_over_repeats_before_aggregating():
    records = [
        sh.record(W, item="a", group="g1", repeat=0),
        sh.record(T, item="a", group="g1", repeat=1),
        sh.record(T, item="a", group="g1", repeat=2),
        sh.record(W, item="b", group="g2", repeat=0),
    ]
    table, est = estimates(records)
    assert table.scale == 3
    # item a: 1/3 wrong, item b: 1 wrong -> (1/3 + 1) / 2 = 2/3 (pooling records would give 1/2)
    assert est["arm/no_gate/wrong_all"] == Fraction(2, 3)
    assert not table.complete_repeats and table.unpaired_records == 0


def test_complete_repeats_equal_pooled_records():
    records = [
        sh.record(o, item=f"i{k}", group=f"g{k}", repeat=r)
        for k, outcomes in enumerate([[W, T, T], [W, W, A]])
        for r, o in enumerate(outcomes)
    ]
    table, est = estimates(records)
    assert table.complete_repeats and table.repeats == (0, 1, 2)
    assert est["arm/no_gate/wrong_all"] == Fraction(3, 6)
    assert est["arm/no_gate/m1"] == Fraction(3, 5)


def test_units_missing_in_one_arm_are_unpaired_and_excluded():
    records = sh.grid({"no_gate": [W, T, T], "gated": [A, T]})
    table, est = estimates(records)
    assert table.unpaired_records == 1 and len(table.items) == 2
    assert est["arm/no_gate/wrong_all"] == Fraction(1, 2)


def test_spread_across_repeats_and_conditions_in_the_analysis():
    records = []
    for k in range(4):
        stratum = "absent/null_control" if k < 2 else "complete/synthetic_y"
        for r in range(3):
            ng = W if (k + r) % 2 == 0 else T
            records.append(sh.record(ng, arm="no_gate", item=f"i{k}", group=f"g{k // 2}", repeat=r, stratum=stratum))
            records.append(sh.record(A if ng == W else T, arm="gated", item=f"i{k}", group=f"g{k // 2}", repeat=r, stratum=stratum))
    result = analyse(records, resamples=200)
    model = result["models"]["m"]
    spread = model["repeat_spread"]["no_gate"]["wrong_all"]
    assert spread["per_repeat"] == {"0": 0.5, "1": 0.5, "2": 0.5}
    assert set(model["conditions"]) == {"absent/null_control", "complete/synthetic_y"}
    assert model["conditions"]["absent/null_control"]["gated"]["wrong_all"] == 0.0
    assert set(model["factors"]["x_document"]) == {"absent", "complete"}
    assert model["outcome_counts"]["no_gate"][W] == 6 and model["outcome_counts"]["gated"]["abstention"] == 6
    assert model["n_groups"] == 2 and model["complete"]


def test_recovery_rate_m4_counts_first_attempt_research():
    records = [
        sh.record(T, arm="gated_research", item="a", group="g1", research_first=True),
        sh.record(A, arm="gated_research", item="b", group="g2", research_first=True),
        sh.record(T, arm="gated_research", item="c", group="g3"),
    ]
    _table, est = estimates(records)
    assert est["arm/gated_research/m4"] == Fraction(1, 2)


def test_invalid_inputs_are_refused():
    with pytest.raises(StatsError, match="duplicate"):
        estimates([sh.record(T), sh.record(W)])
    with pytest.raises(StatsError, match="different group"):
        estimates([sh.record(T, arm="no_gate"), sh.record(T, arm="gated", group="other")])
    with pytest.raises(StatsError, match="dev and the test"):
        analyse([sh.record(T), sh.record(T, item="i2", split=Split.DEV)])
    with pytest.raises(StatsError, match="several datasets"):
        analyse([sh.record(T), sh.record(T, item="i2", dataset="grab_rag")])
    with pytest.raises(StatsError, match="unknown outcome"):
        build_model_table([sh.record(T)], reference="no_gate", correct_outcomes=["right"])
    with pytest.raises(StatsError):
        analyse([])
