"""Scorer ``sec_answer`` (PR 17, layer 2): amount parsing, matching, labels and the layer-2 analysis."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

import run_helpers as h
import sec_helpers
from binding_gate import registry, runner
from binding_gate.config import parse_config
from binding_gate.contracts import AnswerCandidate, Document, Item
from binding_gate.scorers.sec_answer import (
    OUTCOME_BY_LABEL,
    SecScoringError,
    matches,
    parse_amount,
    sec_outcome,
)
from binding_gate.stats.layers import analyse_layer


@pytest.mark.parametrize(
    ("text", "value", "exponent", "negative"),
    [
        ("$391,035 million", "391035000000", 6, False),
        ("Apple's revenue was $391.0 billion in fiscal 2024.", "391000000000", 8, False),
        ("$1.2 billion", "1200000000", 8, False),
        ("1,234 million", "1234000000", 6, False),
        ("(512)", "512", 0, True),
        ("$(512) million", "512000000", 6, True),
        ("-$3.4 billion", "3400000000", 8, True),
        ("a net loss of $512 million", "512000000", 6, True),
        ("an operating loss of 45 million dollars", "45000000", 6, True),
        ("net income (loss) of $45 million", "45000000", 6, False),
        ("income/(loss) was $45 million", "45000000", 6, False),
        ("391,035", "391035", 0, False),
        ("For the fiscal year ended September 28, 2024, total revenue was $391,035 million.", "391035000000", 6, False),
        ("$1.2B", "1200000000", 8, False),
        ("$5.459bn", "5459000000", 6, False),
        ("5,459 mm", "5459000000", 6, False),
        ("USD 12,000 thousand", "12000000", 6, False),
        ("Revenue of 5,459 (in millions)", "5459000000", 6, False),
        ("$391,000,000,000", "391000000000", 9, False),
        ("In FY2024 (Form 10-K, Q4) revenue rose 12% to $7.1 billion", "7100000000", 8, False),
    ],
)
def test_parse_amount(text, value, exponent, negative):
    amount = parse_amount(text)
    assert amount is not None, text
    assert amount.magnitude == Decimal(value) and amount.exponent == exponent and amount.negative is negative


@pytest.mark.parametrize("text", ["2024", "about 12%", "fiscal year 2025", "on September 28", "the 10-K", "", "n/a"])
def test_no_amount(text):
    assert parse_amount(text) is None


def test_matching_rounding_truncation_precision_and_sign():
    assert matches(parse_amount("$1.2 billion"), 1_234_000_000)  # correct rounding
    assert matches(parse_amount("$1.2 billion"), 1_299_000_000)  # truncation: less than one unit of 0.1 billion
    assert not matches(parse_amount("$1.2 billion"), 1_300_000_000)
    assert matches(parse_amount("$391,035 million"), 391_035_000_000) and not matches(parse_amount("$391,036 million"), 391_035_000_000)
    assert not matches(parse_amount("$400 billion"), 391_035_000_000)  # one significant digit: too imprecise
    assert matches(parse_amount("a net loss of $512 million"), -512_000_000)
    assert matches(parse_amount("$512 million"), -512_000_000)  # no explicit sign: either sign
    assert not matches(parse_amount("$(512) million"), 512_000_000)  # explicitly negative never matches a positive value
    assert matches(parse_amount("391,035"), 391_035_000_000, rescale=6)


# -- labels on a hand-made item ---------------------------------------------------------------- #

TARGET = "Acme Corp.: annual report on Form 10-K\nTotal revenues | Dec. 31, 2025: $ 5,459 | Dec. 31, 2024: $ 5,022"
PEER = "Bolt Inc.: annual report on Form 10-K\nTotal revenues | Dec. 31, 2025: $ 7,777 | Dec. 31, 2024: $ 6,100"


def item(condition="target_peer"):
    docs = {"target_only": [("doc1", TARGET, "target")], "target_peer": [("doc1", PEER, "competitor"), ("doc2", TARGET, "target")],
            "peer_only": [("doc1", PEER, "competitor")]}[condition]
    return Item(
        id=f"sec/0000000001/revenue/{condition}", group_id="sec/0000000001", dataset="sec_financials",
        question="What was Acme Corp.'s total revenue, in US dollars, for the fiscal year ended December 31, 2025, ...?",
        documents=tuple(Document.create(i, t) for i, t, _r in docs),
        gold={"document_roles": {i: r for i, _t, r in docs}, "condition": condition, "metric": "revenue", "value": 5_459_000_000,
              "target_alternatives": [{"start": "2024-01-01", "end": "2024-12-31", "value": 5_022_000_000}],
              "competitor": {"values": [{"end": "2025-12-31", "value": 7_777_000_000}, {"end": "2024-12-31", "value": 6_100_000_000}]}},
        stratum="present/peer",
    )


def answer(value, quote, source="doc2"):
    return AnswerCandidate.answered(raw="{}", value=value, quote=quote, source_id=source)


def score(candidate, condition="target_peer"):
    return registry.create("scorer", "sec_answer").score(item(condition), candidate)


def test_labels():
    q_target = "Total revenues | Dec. 31, 2025: $ 5,459"
    correct = score(answer("Acme's revenue was $5,459 million.", q_target))
    assert correct.label == "correct" and correct.details["outcome"] == "target_grounded" and correct.details["provenance"] == "target_only"
    peer = score(answer("$7,777 million", "Total revenues | Dec. 31, 2025: $ 7,777", "doc1"))
    assert peer.label == "wrong_entity" and peer.details["strict_wrong_entity"] and peer.details["outcome"] == "wrong_entity"
    loose = score(answer("$7.78 billion", q_target))  # the peer's figure with a quote from the target document
    assert loose.label == "wrong_entity" and not loose.details["strict_wrong_entity"]
    prior = score(answer("$5,022 million", "Dec. 31, 2024: $ 5,022"))
    assert prior.label == "wrong_period_or_scale" and not prior.details["scale_error"]
    scale = score(answer("$5,459", q_target))
    assert scale.label == "wrong_period_or_scale" and scale.details["scale_error"] and scale.details["matches"]["scaled"] == 6
    other = score(answer("$9,999 million", q_target))
    assert other.label == "other_wrong" and other.details["reason"] is None
    none = score(answer("It grew strongly.", q_target))
    assert none.label == "other_wrong" and none.details["reason"] == "no_amount"
    vague = score(answer("about $5 billion", q_target))
    assert vague.label == "other_wrong" and vague.details["reason"] == "too_imprecise"
    unsupported = score(answer("$5,459 million", "Total revenues | Dec. 31, 2025: $ 7,777", "doc1"), "peer_only")
    assert unsupported.label == "value_unsupported" and unsupported.details["outcome"] == "ungrounded"
    short = score(answer("$5,459 million", "$ 5,459"))
    assert short.label == "unparseable" and short.details["reason"] == "quote_too_short"
    assert score(AnswerCandidate.abstained(raw="{}", reason="no")).label == "abstained"
    assert score(AnswerCandidate.unparseable(raw="?", reason="no json")).label == "unparseable"


def test_every_label_maps_to_a_statistics_outcome_and_bad_gold_is_refused():
    assert set(OUTCOME_BY_LABEL.values()) <= {"target_grounded", "wrong_entity", "ungrounded", "abstention", "unparseable"}
    bad = Item.from_dict({**item().to_dict(), "gold": {**item().gold, "value": "5459"}})
    with pytest.raises(SecScoringError):
        registry.create("scorer", "sec_answer").score(bad, answer("$1 million", "Total revenues | Dec. 31, 2025: $ 5,459"))


# -- the layer-2 analysis (dispatch through stats.layers) ------------------------------------- #


def test_dev_run_is_analysed_with_the_sec_rule_and_h1_h2_are_evaluated_not_confirmatory(tmp_path):
    options = sec_helpers.write_dataset(tmp_path / "sec", n_extra=24)
    repo, _config, _ = h.setup(tmp_path / "repo")
    raw = {
        "config_version": 1, "name": "sec-dev-test", "split": "dev", "repeats": 1,
        "dataset": {"name": "sec_financials", "options": options},
        "models": [{"name": "sec_scripted_fake", "label": "fake", "options": {"model_id": "fake-cheap"}}],
        "arms": [{"name": "no_gate", "label": "no_gate", "options": {"presentation": "sec_filings"}}],
        "scorers": [{"name": "sec_answer"}],
        "runner": {"concurrency": 2, "max_usd": 5.0},
    }
    summary = runner.execute_run(parse_config(raw), repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None)
    analysis, rule = analyse_layer([summary.run_dir], resamples=200)
    assert rule.layer == 2 and rule.scorer == "sec_answer" and analysis["layer"] == 2
    fake = analysis["models"]["fake"]
    assert fake["hypotheses"]["H1"]["status"] == "NOT EVALUABLE"  # no gated arm in this run
    assert "arm(s) ['gated'] not in the traces" in json.dumps(fake["hypotheses"]["H1"])
    counts = analysis["layer2"]["label_counts"]["fake"]["no_gate"]
    assert counts["correct"] > 0 and counts["wrong_entity"] > 0  # target_only correct; peer shown first -> peer figure
    assert analysis["outcome_rule"]["m1"].startswith("wrong_entity")
    assert set(fake["conditions"]) == {"present/none", "present/peer", "absent/peer"}


def test_sec_outcome_of_error_records_and_missing_scores():
    record = type("R", (), {"error": "boom", "scores": (), "item_id": "i", "arm": "a", "model": "m"})()
    assert sec_outcome(record) == "error"
    record.error = None
    with pytest.raises(SecScoringError):
        sec_outcome(record)
