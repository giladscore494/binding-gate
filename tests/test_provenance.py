"""Deterministic provenance scoring (PREREGISTRATION.md 6.3, 6.4)."""

from __future__ import annotations

import pytest

import builders as b
from binding_gate.contracts import AnswerCandidate, Document, Item
from binding_gate.registry import create
from binding_gate.scorers.provenance import OUTCOME_BY_LABEL, ProvenanceError

TARGET = "In 2024 the starting dose of Alphadrug for adults was 10 mg once daily with food."
COMPETITOR = "In 2024 the starting dose of Betadrug for adults was 50 mg once daily with food."
SHARED = "Both agents are taken once daily with food and need regular monitoring."


def item(extra: tuple[Document, ...] = (), roles: dict | None = None) -> Item:
    documents = (
        Document.create("cx_doc", TARGET + " " + SHARED),
        Document.create("cy_doc", COMPETITOR + " " + SHARED),
        *extra,
    )
    gold_roles = {"cx_doc": "target", "cy_doc": "competitor", **(roles or {})}
    return Item(id="i", group_id="g", dataset="toy", question="Q?", documents=documents, gold={"document_roles": gold_roles})


def answered(quote: str, source_id: str = "cx_doc") -> AnswerCandidate:
    return AnswerCandidate.answered(value="v", quote=quote, source_id=source_id, raw="{}")


@pytest.fixture
def scorer():
    return create("scorer", "provenance")


@pytest.mark.parametrize(
    "quote,label,outcome,matched",
    [
        ("the starting dose of Alphadrug for adults was 10 mg", "target_only", "target_grounded", ["cx_doc"]),
        ("the starting dose of Betadrug for adults was 50 mg", "competitor_only", "wrong_entity", ["cy_doc"]),
        ("taken once daily with food and need regular monitoring", "both", "ambiguous", ["cx_doc", "cy_doc"]),
        ("the starting dose of Gammadrug for adults was 5 mg", "neither", "ungrounded", []),
    ],
)
def test_the_four_provenance_labels(scorer, quote, label, outcome, matched):
    result = scorer.score(item(), answered(quote))
    assert (result.scorer, result.label) == ("provenance", label)
    assert result.details["outcome"] == outcome == OUTCOME_BY_LABEL[label]
    assert result.details["matched_document_ids"] == matched


def test_matching_uses_the_preregistered_normalisation(scorer):
    quote = "THE  starting dose\nof Alphadrug for adults was 10 mg"
    assert scorer.score(item(), answered(quote)).label == "target_only"
    # A changed number is not a match.
    assert scorer.score(item(), answered("the starting dose of Alphadrug for adults was 20 mg")).label == "neither"


def test_label_follows_where_the_quote_is_not_the_cited_source_and_the_mismatch_is_recorded(scorer):
    result = scorer.score(item(), answered("the starting dose of Betadrug for adults was 50 mg", source_id="cx_doc"))
    assert result.label == "competitor_only"
    assert result.details["source_id_matches"] is False and result.details["source_id_is_shown_document"] is True
    unknown = scorer.score(item(), answered("the starting dose of Alphadrug for adults was 10 mg", source_id="Document 1"))
    assert unknown.label == "target_only" and unknown.details["source_id_is_shown_document"] is False


def test_short_quotes_are_unparseable_by_the_minimum_length_rule(scorer):
    result = scorer.score(item(), answered("was 10 mg once"))
    assert result.label == "unparseable"
    assert result.details == {"outcome": "unparseable", "reason": "quote_too_short", "quote_tokens": 4, "min_quote_tokens": 5}
    assert scorer.score(item(), answered("adults was 10 mg once")).label == "target_only"  # exactly 5 tokens
    assert create("scorer", "provenance", min_quote_tokens=3).score(item(), answered("was 10 mg")).label == "target_only"


def test_abstained_and_unparseable_candidates_get_their_own_labels(scorer):
    abstained = scorer.score(item(), AnswerCandidate.abstained(raw="{}"))
    assert (abstained.label, abstained.details["outcome"]) == ("abstained", "abstention")
    broken = scorer.score(item(), AnswerCandidate.unparseable(raw="??", reason="no JSON"))
    assert (broken.label, broken.details["outcome"]) == ("unparseable", "unparseable")


def test_a_quote_found_only_in_an_other_document_is_neither(scorer):
    extra = (Document.create("misc", "An unrelated notice about pharmacy opening hours on public holidays."),)
    result = scorer.score(item(extra, {"misc": "other"}), answered("notice about pharmacy opening hours on public holidays"))
    assert result.label == "neither" and result.details["matched_document_ids"] == ["misc"]


def test_missing_roles_are_an_error_not_a_guess(scorer):
    with pytest.raises(ProvenanceError):
        scorer.score(Item(id="i", group_id="g", dataset="toy", question="Q?", documents=b.documents()), answered("a b c d e"))
    with pytest.raises(ProvenanceError):
        scorer.score(item((Document.create("x", "text here"),)), answered("a b c d e"))


def test_scoring_is_deterministic(scorer):
    candidate = answered("the starting dose of Betadrug for adults was 50 mg")
    assert scorer.score(item(), candidate) == scorer.score(item(), candidate)
    with pytest.raises(ValueError):
        create("scorer", "provenance", min_quote_tokens=0)
