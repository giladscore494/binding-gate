"""Scorer ``grab_rag_answer``: deterministic value and quote scoring for GRAB-RAG items."""

from __future__ import annotations

import pytest

import grab_rag_fixture as fx
from binding_gate import registry
from binding_gate.contracts import AnswerCandidate
from binding_gate.datasets import grab_rag as gr
from binding_gate.scorers.grab_rag_answer import GrabRagScoringError, value_carries

RECORDS = fx.pick_records("natural_questions", 3, 3)
RECORD = RECORDS[0]
MISLEADING = gr.misleading_item("natural_questions", RECORD, fx.qc_passages(RECORD, 0, RECORDS))
SUPPORTIVE = gr.supportive_item("natural_questions", RECORD, 0)
GOLD = RECORD["answers"][0]  # "Odran Vellis<n>"
PLANTED = MISLEADING.gold["planted_answer"]  # "Zorbland<n>"


def scorer(**options):
    return registry.create("scorer", "grab_rag_answer", **options)


def doc(item, role):
    return next(d for d in item.documents if item.gold["document_roles"][d.id] == role)


def answered(value, quote, source_id="doc1"):
    return AnswerCandidate.answered(value=value, quote=quote, source_id=source_id, raw="{}")


def test_misled_answer_quoting_the_edited_passage():
    edited = doc(MISLEADING, "competitor")
    quote = edited.text.split(": ", 1)[1][:120]
    result = scorer().score(MISLEADING, answered(f"It was founded by {PLANTED}.", quote, edited.id))
    assert result.scorer == "grab_rag_answer" and result.label == "misled"
    d = result.details
    assert d["outcome"] == "misled" and d["quote_source"] == "misleading" and d["source_id_matches"] is True
    assert (d["value_has_gold"], d["value_has_planted"], d["planted_known"]) == (False, True, True)
    assert d["matched_document_ids"] == [edited.id] and d["condition"] == "misleading"


def test_correct_answer_from_memory_quoting_a_hard_negative():
    negative = doc(MISLEADING, "other")
    quote = " ".join(negative.text.split()[:8])
    result = scorer().score(MISLEADING, answered(f"{GOLD.upper()}!", quote, negative.id))
    assert result.label == "correct" and result.details["quote_source"] == "other"


def test_ambiguous_other_wrong_and_quote_not_found():
    quote = "these seven words appear in no passage"
    assert scorer().score(MISLEADING, answered(f"{GOLD} or {PLANTED}", quote)).label == "ambiguous"
    result = scorer().score(MISLEADING, answered("Someone else entirely", quote))
    assert result.label == "other_wrong" and result.details["quote_source"] == "not_found"
    assert result.details["source_id_matches"] is False


def test_supportive_item_has_no_planted_answer():
    target = doc(SUPPORTIVE, "target")
    quote = " ".join(target.text.split()[:9])
    result = scorer().score(SUPPORTIVE, answered(f"The founder was {GOLD}.", quote))
    assert result.label == "correct" and result.details["quote_source"] == "supporting"
    assert result.details["planted_known"] is False and result.details["value_has_planted"] is False
    # A value that would be "misled" on the misleading item is just wrong here.
    assert scorer().score(SUPPORTIVE, answered(PLANTED, quote)).label == "other_wrong"


def test_abstained_unparseable_and_short_quotes():
    s = scorer()
    assert s.score(MISLEADING, AnswerCandidate.abstained(raw="{}", reason="conflict")).label == "abstained"
    result = s.score(MISLEADING, AnswerCandidate.unparseable(raw="?", reason="no json"))
    assert (result.label, result.details["reason"]) == ("unparseable", "unparseable_output")
    result = s.score(MISLEADING, answered(PLANTED, "four words only here"))
    assert (result.label, result.details["reason"], result.details["quote_tokens"]) == ("unparseable", "quote_too_short", 4)
    assert scorer(min_quote_tokens=4).score(MISLEADING, answered(PLANTED, "four words only here")).label == "misled"
    for bad in (0, -1, True, 2.5):
        with pytest.raises(ValueError):
            scorer(min_quote_tokens=bad)


def test_an_unknown_plant_never_scores_misled():
    item = gr.misleading_item("natural_questions", RECORD, fx.qc_passages(RECORD, 0, RECORDS))
    data = item.to_dict()
    data["gold"]["planted_answer"] = None
    item = item.from_dict(data)
    result = scorer().score(item, answered(PLANTED, "these seven words appear in no passage"))
    assert result.label == "other_wrong" and result.details["planted_known"] is False


@pytest.mark.parametrize(
    "value, answers, expected",
    [
        ("Odran Vellis.", ["Odran Vellis"], True),
        ("“ODRAN  VELLIS”", ["Odran Vellis"], True),  # typographic quotes, case, spacing
        ("It was the Beatles", ["Beatles"], True),  # articles ignored
        ("about 1,000 people", ["1000"], True),
        ("the 3rd edition", ["third edition"], True),
        ("in 19770", ["1977"], False),  # whole words only
        ("Vellis", ["Odran Vellis"], False),  # the whole alias is needed
        ("Odran Velli", ["Odran Vellis"], False),
    ],
)
def test_value_matching_rule(value, answers, expected):
    assert value_carries(value, answers) is expected


def test_items_without_the_needed_gold_are_refused():
    data = MISLEADING.to_dict()
    for key, bad in (("answers", []), ("planted_answer", 3), ("document_roles", {})):
        broken = {**data, "gold": {**data["gold"], key: bad}}
        with pytest.raises(GrabRagScoringError):
            scorer().score(MISLEADING.from_dict(broken), answered("x", "these seven words appear in no passage"))
