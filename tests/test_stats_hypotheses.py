"""H1-H4 decision logic at the boundary values of PREREGISTRATION.md section 11."""

from __future__ import annotations

from fractions import Fraction as F

import stats_helpers as sh
from binding_gate.contracts import Split
from binding_gate.stats import hypotheses as hyp
from binding_gate.stats import prereg
from binding_gate.stats.analysis import analyse, evaluate_model_hypotheses
from binding_gate.stats.bootstrap import Interval

BELOW = Interval(-0.2, -0.01, 100, 0)
TOUCHING = Interval(-0.2, 0.0, 100, 0)
ABOVE = Interval(0.01, 0.2, 100, 0)


def h1(m1_arm, m1_ref, wrong_arm=F(1, 10), wrong_ref=F(2, 10), interval=BELOW):
    return hyp.decide_h1(
        m1_arm=m1_arm, m1_ref=m1_ref, wrong_all_arm=wrong_arm, wrong_all_ref=wrong_ref, diff_m1_interval=interval
    )[0]


def test_h1_relative_reduction_boundary_is_inclusive_and_exact():
    assert prereg.H1_MIN_RELATIVE_REDUCTION == F(1, 2)
    assert h1(F(1, 10), F(2, 10)) == hyp.SUPPORTED  # exactly 50%
    assert h1(F(3, 30), F(6, 30)) == hyp.SUPPORTED  # exactly 50%, other fractions
    assert h1(F(100001, 1000000), F(2, 10)) == hyp.NOT_SUPPORTED  # 49.9995%
    assert h1(F(1, 3), F(2, 3)) == hyp.SUPPORTED  # 0.333.../0.666...: exact, no float rounding


def test_h1_interval_must_lie_strictly_below_zero():
    assert h1(F(1, 10), F(3, 10), interval=TOUCHING) == hyp.NOT_SUPPORTED
    assert h1(F(1, 10), F(3, 10), interval=ABOVE) == hyp.NOT_SUPPORTED
    assert h1(F(1, 10), F(3, 10), interval=Interval(None, None, 0, 100)) == hyp.NOT_SUPPORTED
    assert h1(F(1, 10), F(3, 10), interval=None) == hyp.NOT_SUPPORTED


def test_h1_wrong_per_all_records_may_be_equal_but_not_higher():
    assert h1(F(1, 10), F(3, 10), wrong_arm=F(2, 10), wrong_ref=F(2, 10)) == hyp.SUPPORTED
    assert h1(F(1, 10), F(3, 10), wrong_arm=F(21, 100), wrong_ref=F(2, 10)) == hyp.NOT_SUPPORTED


def test_h1_undefined_quantities_fail():
    assert h1(None, F(3, 10)) == hyp.NOT_SUPPORTED  # the gate answered nothing
    assert h1(F(0), F(0)) == hyp.NOT_SUPPORTED  # no baseline errors: no reduction possible
    assert h1(F(0), None) == hyp.NOT_SUPPORTED


def test_h2_boundary():
    assert hyp.decide_h2(m2_arm=F(10, 100))[0] == hyp.SUPPORTED
    assert hyp.decide_h2(m2_arm=F(1001, 10000))[0] == hyp.NOT_SUPPORTED
    assert hyp.decide_h2(m2_arm=F(0))[0] == hyp.SUPPORTED
    assert hyp.decide_h2(m2_arm=None)[0] == hyp.NOT_SUPPORTED


def h3(wrong_g, wrong_o, m2_g, m2_o, interval=BELOW):
    return hyp.decide_h3(
        wrong_all_gated=wrong_g, wrong_all_other=wrong_o, diff_wrong_all_interval=interval, m2_gated=m2_g, m2_other=m2_o
    )[0]


def test_h3_support_trade_off_and_failure():
    assert h3(F(1, 10), F(2, 10), F(5, 100), F(5, 100)) == hyp.SUPPORTED  # M2 equal is "not higher"
    assert h3(F(1, 10), F(2, 10), F(6, 100), F(5, 100)) == hyp.TRADE_OFF  # better errors, higher M2
    assert h3(F(3, 10), F(2, 10), F(4, 100), F(5, 100), interval=ABOVE) == hyp.TRADE_OFF  # worse errors, lower M2
    assert h3(F(1, 10), F(2, 10), F(5, 100), F(5, 100), interval=TOUCHING) == hyp.NOT_SUPPORTED
    assert h3(F(2, 10), F(2, 10), F(4, 100), F(5, 100), interval=TOUCHING) == hyp.NOT_SUPPORTED  # equal errors
    assert h3(F(3, 10), F(2, 10), F(6, 100), F(5, 100), interval=ABOVE) == hyp.NOT_SUPPORTED  # worse on both
    assert h3(F(1, 10), F(2, 10), None, F(5, 100)) == hyp.NOT_SUPPORTED


def test_h4_boundary():
    assert hyp.decide_h4(m3_arm=F(3, 10), m3_ref=F(3, 10))[0] == hyp.SUPPORTED
    assert hyp.decide_h4(m3_arm=F(299, 1000), m3_ref=F(3, 10))[0] == hyp.NOT_SUPPORTED
    assert hyp.decide_h4(m3_arm=None, m3_ref=F(3, 10))[0] == hyp.NOT_SUPPORTED


def _estimates(failed_gated: F) -> dict:
    est = {
        "arm/no_gate/m1": F(4, 10), "arm/gated/m1": F(1, 10),
        "arm/no_gate/wrong_all": F(3, 10), "arm/gated/wrong_all": F(1, 10),
        "arm/gated/m2": F(5, 100),
        "arm/no_gate/failed_all": F(0), "arm/gated/failed_all": failed_gated,
        "cmp/gated/no_gate/m1_worst": F(1, 10), "cmp/gated/no_gate/ref_m1_best": F(4, 10),
        "cmp/gated/no_gate/wrong_all_worst": F(1, 10),
    }
    return est


def test_validity_guard_boundary_and_not_evaluable_cases():
    ci = {"cmp/gated/no_gate/diff_m1": BELOW, "cmp/gated/no_gate/diff_m1_worst": BELOW}
    common = dict(layer=1, arms=("no_gate", "gated"), ci=ci, base_reasons=[], correct_rule_placeholder=False)
    at_limit = evaluate_model_hypotheses(est=_estimates(F(5, 100)), **common)
    assert at_limit["H1"].status == hyp.SUPPORTED and at_limit["H1"].confirmatory
    assert at_limit["H2"].status == hyp.SUPPORTED and at_limit["H2"].confirmatory
    over = evaluate_model_hypotheses(est=_estimates(F(501, 10000)), **common)
    assert over["H1"].status == hyp.SUPPORTED and not over["H1"].confirmatory
    assert "validity guard" in over["H1"].non_confirmatory_reasons[0]
    assert at_limit["H3"].status == hyp.NOT_EVALUABLE and not at_limit["H3"].confirmatory  # no faithfulness arm
    assert at_limit["H4"].status == hyp.NOT_EVALUABLE  # layer 1 data
    layer2 = evaluate_model_hypotheses(est=_estimates(F(0)), **{**common, "layer": 2})
    assert {r.status for r in layer2.values()} == {hyp.NOT_EVALUABLE}
    blocked = evaluate_model_hypotheses(est=_estimates(F(0)), **{**common, "base_reasons": ["split is 'dev'"]})
    assert blocked["H1"].status == hyp.SUPPORTED and not blocked["H1"].confirmatory


def test_analysis_end_to_end_decisions_and_labels():
    # 20 groups x 1 item; no_gate wrong on 10, gated blocks 8 of them and loses 1 correct answer.
    no_gate = ["target_grounded"] * 10 + ["wrong_entity"] * 10
    gated = ["target_grounded"] * 9 + ["abstention"] + ["abstention"] * 8 + ["wrong_entity"] * 2
    faith = ["target_grounded"] * 9 + ["abstention"] * 3 + ["wrong_entity"] * 8
    records = sh.grid({"no_gate": no_gate, "gated": gated, "faithfulness_checker": faith})
    result = analyse(records, resamples=500, seed=3)
    model = result["models"]["m"]
    h = model["hypotheses"]
    assert h["H1"]["status"] == hyp.SUPPORTED  # M1 1/2 -> 2/11
    assert h["H2"]["status"] == hyp.SUPPORTED  # M2 = 1/10 exactly
    assert h["H3"]["status"] == hyp.SUPPORTED
    assert h["H4"]["status"] == hyp.NOT_EVALUABLE
    # fewer resamples than pre-registered: everything is labelled not confirmatory (PR 13: one repeat
    # is the pre-registered number, so the repeats are not a reason)
    assert not h["H1"]["confirmatory"]
    reasons = " ".join(h["H1"]["non_confirmatory_reasons"])
    assert "bootstrap parameters" in reasons and "repeats" not in reasons
    assert model["controlled_layer_claim"].startswith("the gate works")
    assert result["summary"]["H1"]["supported"] == ["m"] and result["summary"]["H1"]["supported_confirmatory"] == []
    assert h["H1"]["sensitivity"]["status_under_worst_case"] == hyp.SUPPORTED  # no failed records here


def test_dev_split_is_never_confirmatory():
    records = sh.grid({"no_gate": ["wrong_entity"] * 4, "gated": ["abstention"] * 4}, split=Split.DEV)
    result = analyse(records, resamples=50)
    assert any("not 'test'" in r for r in result["confirmatory_blockers"])
    assert not any(r["confirmatory"] for r in result["models"]["m"]["hypotheses"].values())
