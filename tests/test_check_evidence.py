"""Shared text machinery of the checks: tokens, mentions, distances, anchor, lead terms, candidates."""

from __future__ import annotations

import math

import pytest

import gate_fakes as g
from binding_gate.checks import _evidence as ev
from binding_gate.contracts import AnswerCandidate


def doc_text(text: str) -> ev.DocText:
    return ev.DocText.of(g.doc("d1", text))


def test_tokens_are_word_runs_of_the_preregistered_normal_form():
    assert ev.tokens_of("Bruton’s  TYROSINE-kinase (BTK)") == ("bruton", "s", "tyrosine", "kinase", "btk")
    # NFKC folds full-width letters; diacritics are kept, not stripped
    assert ev.tokens_of("ＡＢＣ café") == ("abc", "café")
    assert ev.tokens_of("café") == ("café",)  # decomposed -> composed by NFKC
    assert ev.tokens_of("...") == ()


def test_phrases_match_whole_tokens_only_and_ignore_punctuation():
    d = doc_text("TSCx is not TSC. 5-fluorouracil and 5 fluorouracil; drug's effect")
    assert [s.start for s in d.find(ev.tokens_of("tsc"))] == [3]
    assert len(d.find(ev.tokens_of("5 fluorouracil"))) == 2
    assert len(d.find(ev.tokens_of("drug"))) == 1
    assert d.find(()) == []


def test_distance_counts_tokens_strictly_between_spans():
    a = ev.Span(0, 2, 0, 0)
    assert ev.distance(a, ev.Span(2, 3, 0, 0)) == 0  # touching
    assert ev.distance(a, ev.Span(5, 6, 0, 0)) == 3
    assert ev.distance(ev.Span(5, 6, 0, 0), a) == 3  # symmetric
    assert ev.distance(a, ev.Span(1, 4, 0, 0)) == 0 and ev.overlaps(a, ev.Span(1, 4, 0, 0))
    assert not ev.overlaps(a, ev.Span(2, 3, 0, 0))


def test_quote_occurrences_include_overlapping_ones_and_mid_token_starts():
    d = doc_text("ab ab ab")
    spans = ev.quote_occurrences(d, "ab ab")
    assert [(s.start, s.end) for s in spans] == [(0, 2), (1, 3)]
    mid = ev.quote_occurrences(doc_text("mycophenolate mofetil"), "phenolate mof")
    assert [(s.start, s.end) for s in mid] == [(0, 2)]
    assert ev.quote_occurrences(d, "​") == []  # normalises to nothing


def test_anchor_is_the_occurrence_closest_to_the_entity_then_the_earliest():
    text = f"same quote here {g.filler(30)} abatacept same quote here {g.filler(5)} same quote here"
    located = ev.locate(g.constraints(), g.answer("same quote here"), [g.doc("d1", text)])
    assert isinstance(located, ev.Located)
    assert len(located.occurrences) == 3
    assert located.anchor == located.occurrences[1] and located.entity_distance == 0
    no_entity = ev.locate(g.constraints(), g.answer("same quote here"), [g.doc("d1", "same quote here, same quote here")])
    assert no_entity.anchor == no_entity.occurrences[0] and math.isinf(no_entity.entity_distance)


def test_locate_reports_why_the_quote_cannot_be_placed():
    docs = [g.doc("d1", "abatacept helps in lupus nephritis a lot")]
    missing_source = ev.locate(g.constraints(), g.answer("helps in lupus", source_id="d9"), docs)
    assert missing_source == ev.NotLocated("source_not_provided", "d9")
    not_found = ev.locate(g.constraints(), g.answer("helps in gout"), docs)
    assert not_found == ev.NotLocated("quote_not_found", "d1")


def test_checks_refuse_non_answered_candidates():
    with pytest.raises(ValueError, match="answered"):
        ev.locate(g.constraints(), AnswerCandidate.abstained(raw="x"), [])


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Mycophenolate mofetil has emerged. Mycophenolate mofetil helps.", "mycophenolate mofetil"),
        # longest recurring prefix of the run: "everoovate represents" occurs once
        ("Everoovate represents a novel agent. Later everoovate was tested.", "everoovate"),
        ("Rituivimab, a monoclonal antibody, is used. Rituivimab works.", "rituivimab"),
        ("5-Fluorouracil is old. 5-fluorouracil is cheap.", "5 fluorouracil"),
        # no repetition: no lead term
        ("Current understanding of the drug is limited. Nothing else.", None),
        # starts with a stop word: empty run
        ("The agent zorbex is new. Zorbex again.", None),
        # pure numbers are not names
        ("2024 was a year. In 2024 more happened.", None),
        # run longer than lead_max_tokens
        ("Alpha beta gamma delta epsilon is long. Alpha beta gamma delta epsilon.", None),
        ("...", None),
        # a full stop inside a number does not end the lead segment
        ("Drug 5.3 is given. Drug 5.3 again.", "drug 5 3"),
    ],
)
def test_lead_term(text, expected):
    term = ev.lead_term(doc_text(text), max_tokens=4, min_repeats=2)
    assert (" ".join(term) if term else None) == expected


def test_competitor_candidates_exclude_the_entity_the_question_and_qualifiers():
    c = g.constraints(entity="abatacept", aliases=("CTLA4-Ig",), qualifiers=(("lupus nephritis", ("LN",)),))
    docs = [
        g.doc("cx", "Abatacept is used. Abatacept again."),
        g.doc("a", "CTLA4-Ig fusion is a protein. CTLA4-Ig fusion again."),  # contains an alias
        g.doc("b", "Lupus nephritis is a disease. Lupus nephritis again."),  # in the question
        g.doc("c", "LN biopsy is routine. LN biopsy again."),  # contains a qualifier alias
        g.doc("cy", "Rituximab has emerged. Rituximab again."),
        g.doc("cy2", "Rituximab is repeated here. Rituximab twice."),  # duplicate term
        g.doc("z", "..."),
    ]
    candidates = ev.competitor_candidates(c, docs, max_tokens=4, min_repeats=2)
    assert [(x.term, x.from_document, x.excluded) for x in candidates] == [
        ("abatacept", "cx", "question_entity"),
        ("ctla4 ig fusion", "a", "question_entity"),
        ("lupus nephritis", "b", "in_question"),
        ("ln biopsy", "c", "qualifier"),
        ("rituximab", "cy", None),
    ]
