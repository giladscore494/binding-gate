"""Unit tests of the five deterministic checks (gate functions C and D)."""

from __future__ import annotations

import pytest

import gate_fakes as g
from binding_gate import registry
from binding_gate.contracts import AnswerCandidate, CheckStatus, Document

PASS, FAIL, NA = CheckStatus.PASS, CheckStatus.FAIL, CheckStatus.NOT_APPLICABLE


def make(name, **options):
    return registry.create("check", name, **options)


def test_every_reserved_check_name_is_registered_and_reports_its_name():
    for name in ("quote_verbatim", "entity_in_window", "qualifiers_in_window", "no_closer_competitor", "premise_present"):
        assert make(name).name == name


@pytest.mark.parametrize(
    "name,options",
    [
        ("quote_verbatim", {"min_quote_tokens": 0}),
        ("quote_verbatim", {"min_quote_tokens": True}),
        ("entity_in_window", {"window_tokens": -1}),
        ("entity_in_window", {"window_tokens": 2.5}),
        ("no_closer_competitor", {"lead_min_repeats": 0}),
        ("no_closer_competitor", {"competitor_tie": "maybe"}),
        ("premise_present", {"premise_scope": "page"}),
    ],
)
def test_invalid_options_are_rejected(name, options):
    with pytest.raises(ValueError):
        make(name, **options)


@pytest.mark.parametrize(
    "name", ["quote_verbatim", "entity_in_window", "qualifiers_in_window", "no_closer_competitor", "premise_present"]
)
def test_checks_never_judge_abstentions_or_unparseable_output(name):
    for candidate in (AnswerCandidate.abstained(raw="x"), AnswerCandidate.unparseable(raw="x", reason="r")):
        with pytest.raises(ValueError, match="answered"):
            make(name).check(g.constraints(presuppositions=(("p", ("x",)),)), candidate, [g.doc("d1", "x")])


# -- quote_verbatim ------------------------------------------------------------ #

DOC = "Abatacept showed a modest benefit in small lupus nephritis series over twelve weeks."


def test_quote_verbatim_passes_after_normalisation_and_lists_positions():
    result = make("quote_verbatim").check(
        g.constraints(), g.answer("ABATACEPT  showed a modest benefit"), [g.doc("d1", DOC)]
    )
    assert result.status is PASS
    assert result.details["occurrences"] == 1 and result.details["quote_tokens"] == 5
    assert result.details["occurrence_spans"][0] == {"token_start": 0, "token_end": 5, "char_start": 0, "char_end": 33}


def test_quote_verbatim_typographic_and_unicode_variants_match():
    document = g.doc("d1", "It is the patient’s choice — and the doctor’s ﬁnal call.")
    result = make("quote_verbatim").check(g.constraints(), g.answer("the patient's choice - and the doctor's final call"), [document])
    assert result.status is PASS


def test_quote_verbatim_diacritics_are_not_folded():
    document = g.doc("d1", "Les résultats du café étaient très positifs pour tous.")
    ok = make("quote_verbatim").check(g.constraints(), g.answer("résultats du café étaient très"), [document])
    stripped = make("quote_verbatim").check(g.constraints(), g.answer("resultats du cafe etaient tres"), [document])
    assert ok.status is PASS and stripped.status is FAIL and stripped.details["reason"] == "quote_not_found"


@pytest.mark.parametrize(
    "quote,source,reason",
    [
        ("Abatacept showed a modest benefit", "d9", "source_not_provided"),
        ("modest benefit in", "d1", "quote_too_short"),
        ("Abatacept showed a large benefit", "d1", "quote_not_found"),
        ("...", "d1", "quote_too_short"),
    ],
)
def test_quote_verbatim_failures_carry_the_first_failing_condition(quote, source, reason):
    result = make("quote_verbatim").check(g.constraints(), g.answer(quote, source), [g.doc("d1", DOC)])
    assert result.status is FAIL and result.details["reason"] == reason


def test_quote_verbatim_reports_the_quote_in_another_document_but_still_fails():
    docs = [g.doc("d1", "Nothing relevant is written in this first document at all."), g.doc("d2", DOC)]
    result = make("quote_verbatim").check(g.constraints(), g.answer("showed a modest benefit in small", "d1"), docs)
    assert result.status is FAIL and result.details["found_in_other_documents"] == ["d2"]


def test_quote_verbatim_at_document_boundaries_and_whole_document():
    for quote in ("Abatacept showed a modest benefit", "nephritis series over twelve weeks.", DOC):
        assert make("quote_verbatim").check(g.constraints(), g.answer(quote), [g.doc("d1", DOC)]).status is PASS


def test_quote_verbatim_minimum_length_is_configurable():
    check = make("quote_verbatim", min_quote_tokens=3)
    assert check.check(g.constraints(), g.answer("modest benefit in"), [g.doc("d1", DOC)]).status is PASS


def test_literal_ellipsis_document_from_the_benchmark():
    # Seven benchmark documents are the literal text "..." (PREREGISTRATION.md section 3, fact 10).
    docs = [g.doc("cy_doc", "...")]
    quote = make("quote_verbatim").check(g.constraints(), g.answer("...", "cy_doc"), docs)
    assert quote.status is FAIL and quote.details["reason"] == "quote_too_short"
    # without quote_verbatim (an ablation) the binding checks still behave: "..." has no tokens
    entity = make("entity_in_window").check(g.constraints(), g.answer("...", "cy_doc"), docs)
    assert entity.status is FAIL and entity.details["reason"] == "entity_not_in_document"
    competitor = make("no_closer_competitor").check(g.constraints(), g.answer("...", "cy_doc"), docs)
    assert competitor.status is NA and competitor.details["reason"] == "no_competitor_candidates"


# -- entity_in_window ------------------------------------------------------------ #


def entity_doc(gap: int) -> str:
    return f"Abatacept {g.filler(gap)} the quoted claim about outcomes {g.filler(3, 'tail')}"


@pytest.mark.parametrize("gap,window,status", [(0, 0, PASS), (5, 5, PASS), (6, 5, FAIL), (40, 40, PASS), (41, 40, FAIL)])
def test_entity_window_boundary_is_inclusive(gap, window, status):
    result = make("entity_in_window", window_tokens=window).check(
        g.constraints(), g.answer("the quoted claim about outcomes"), [g.doc("d1", entity_doc(gap))]
    )
    assert result.status is status
    assert result.details["nearest"]["distance_tokens"] == gap
    if status is FAIL:
        assert result.details["reason"] == "entity_outside_window"


def test_entity_inside_the_quote_has_distance_zero():
    result = make("entity_in_window").check(g.constraints(), g.answer("Abatacept showed a modest benefit"), [g.doc("d1", DOC)])
    nearest = result.details["nearest"]
    assert result.status is PASS and nearest["inside_quote"] and nearest["distance_tokens"] == 0
    assert nearest["form"] == "abatacept" and nearest["char_start"] == 0


def test_entity_after_the_quote_counts_too():
    text = f"the quoted claim about outcomes {g.filler(4)} Abatacept"
    result = make("entity_in_window", window_tokens=4).check(g.constraints(), g.answer("the quoted claim about outcomes"), [g.doc("d1", text)])
    assert result.status is PASS and result.details["nearest"]["distance_tokens"] == 4


def test_entity_alias_matches_and_is_reported():
    c = g.constraints(entity="abatacept", aliases=("Orencia", "CTLA4-Ig"))
    text = f"CTLA4 Ig {g.filler(3)} the quoted claim about outcomes"
    result = make("entity_in_window").check(c, g.answer("the quoted claim about outcomes"), [g.doc("d1", text)])
    assert result.status is PASS and result.details["nearest"]["form"] == "CTLA4-Ig"
    assert result.details["entity_forms"] == ["abatacept", "Orencia", "CTLA4-Ig"]


def test_entity_matching_is_case_insensitive_but_whole_word():
    c = g.constraints(entity="TSC")
    whole = make("entity_in_window").check(c, g.answer("the quoted claim about outcomes"), [g.doc("d1", "In tsc, the quoted claim about outcomes")])
    partial = make("entity_in_window").check(c, g.answer("the quoted claim about outcomes"), [g.doc("d1", "In tscx, the quoted claim about outcomes")])
    assert whole.status is PASS and partial.status is FAIL and partial.details["reason"] == "entity_not_in_document"


def test_entity_check_uses_the_occurrence_nearest_the_entity():
    text = f"the quoted claim about outcomes {g.filler(60)} abatacept: the quoted claim about outcomes"
    result = make("entity_in_window", window_tokens=5).check(g.constraints(), g.answer("the quoted claim about outcomes"), [g.doc("d1", text)])
    assert result.status is PASS and result.details["occurrences"] == 2
    assert result.details["anchor"]["token_start"] == 66


def test_entity_check_is_not_applicable_when_the_quote_cannot_be_placed():
    for quote, source, reason in (("the claim", "d9", "source_not_provided"), ("absent words entirely here", "d1", "quote_not_found")):
        result = make("entity_in_window").check(g.constraints(), g.answer(quote, source), [g.doc("d1", DOC)])
        assert result.status is NA and result.details["reason"] == reason


def test_unmatchable_entity_forms_are_listed():
    c = g.constraints(entity="abatacept", aliases=("+++",))
    result = make("entity_in_window").check(c, g.answer("Abatacept showed a modest benefit"), [g.doc("d1", DOC)])
    assert result.details["unmatchable_forms"] == ["+++"]


# -- qualifiers_in_window --------------------------------------------------------- #


def test_qualifiers_all_present_within_the_window():
    c = g.constraints(qualifiers=(("lupus nephritis", ()), ("twelve weeks", ())))
    result = make("qualifiers_in_window").check(c, g.answer("Abatacept showed a modest benefit"), [g.doc("d1", DOC)])
    assert result.status is PASS
    assert [q["nearest"]["distance_tokens"] for q in result.details["qualifiers"]] == [2, 6]


def test_a_missing_or_distant_qualifier_fails_and_is_named():
    c = g.constraints(qualifiers=(("lupus nephritis", ()), ("2024", ())))
    result = make("qualifiers_in_window").check(c, g.answer("Abatacept showed a modest benefit"), [g.doc("d1", DOC)])
    assert result.status is FAIL and result.details["missing"] == ["2024"]
    assert result.details["reason"] == "qualifier_outside_window"
    narrow = make("qualifiers_in_window", window_tokens=1).check(
        g.constraints(), g.answer("Abatacept showed a modest benefit"), [g.doc("d1", DOC)]
    )
    assert narrow.status is FAIL and narrow.details["qualifiers"][0]["nearest"]["distance_tokens"] == 2


def test_qualifier_alias_and_kind_are_reported():
    c = g.constraints(qualifiers=(("systemic lupus nephritis", ("lupus nephritis",)),))
    result = make("qualifiers_in_window").check(c, g.answer("Abatacept showed a modest benefit"), [g.doc("d1", DOC)])
    entry = result.details["qualifiers"][0]
    assert result.status is PASS and entry["kind"] == "other" and entry["nearest"]["form"] == "lupus nephritis"


def test_no_qualifiers_is_not_applicable():
    result = make("qualifiers_in_window").check(g.constraints(qualifiers=()), g.answer("Abatacept showed a modest benefit"), [g.doc("d1", DOC)])
    assert result.status is NA and result.details == {"reason": "no_qualifiers"}


def test_unmatchable_qualifier_counts_as_missing():
    c = g.constraints(qualifiers=(("%", ()),))
    result = make("qualifiers_in_window").check(c, g.answer("Abatacept showed a modest benefit"), [g.doc("d1", DOC + " 50 %")])
    assert result.status is FAIL and result.details["qualifiers"][0]["unmatchable_forms"] == ["%"]


# -- no_closer_competitor -------------------------------------------------------- #

CX = (
    "Abatacept is a fusion protein. Abatacept showed modest benefit in lupus nephritis. "
    "Unlike rituximab, abatacept was well tolerated in a small series."
)
CY = "Rituximab has emerged for lupus nephritis. Rituximab improved proteinuria in case series of adults."


def competitor(quote: str, source: str, **options):
    docs = [g.doc("cx_doc", CX), g.doc("cy_doc", CY)]
    return make("no_closer_competitor", **options).check(g.constraints(), g.answer(quote, source), docs)


def test_competitor_lead_terms_come_from_every_provided_document():
    result = competitor("Abatacept showed modest benefit in lupus nephritis", "cx_doc")
    assert result.status is PASS
    assert result.details["candidates"] == [
        {"term": "abatacept", "from_document": "cx_doc", "excluded": "question_entity"},
        {"term": "rituximab", "from_document": "cy_doc", "excluded": None},
    ]
    assert result.details["entity"]["distance_tokens"] == 0
    assert result.details["nearest_competitor"]["distance_tokens"] > 0


def test_quote_from_the_competing_document_fails():
    result = competitor("Rituximab improved proteinuria in case series", "cy_doc")
    assert result.status is FAIL and result.details["reason"] == "competitor_closer"
    assert result.details["entity"] is None
    assert result.details["nearest_competitor"]["form"] == "rituximab"


def test_competitor_strictly_closer_than_the_entity_fails():
    text = f"Rituximab is old. Abatacept {g.filler(10)} rituximab improved proteinuria in many adults {g.filler(3)}. Rituximab again."
    docs = [g.doc("d1", text)]
    result = make("no_closer_competitor").check(g.constraints(), g.answer("improved proteinuria in many adults"), docs)
    assert result.status is FAIL
    assert result.details["entity"]["distance_tokens"] == 11
    assert result.details["nearest_competitor"]["distance_tokens"] == 0


def test_tie_policy_decides_equal_distances():
    quote = "Unlike rituximab, abatacept was well tolerated"
    assert competitor(quote, "cx_doc").status is FAIL
    assert competitor(quote, "cx_doc", competitor_tie="pass").status is PASS


def test_competitor_outside_the_window_is_ignored():
    text = f"Rituximab is old. Rituximab again. {g.filler(30)} abatacept improved proteinuria in many adults"
    result = make("no_closer_competitor", window_tokens=10).check(
        g.constraints(), g.answer("improved proteinuria in many adults"), [g.doc("d1", text)]
    )
    assert result.status is PASS and result.details["competitor_mentions_in_window"] == []
    assert result.details["competitor_mentions_in_document"] == 2


def test_competitor_check_without_candidates_is_not_applicable():
    result = make("no_closer_competitor").check(g.constraints(), g.answer("Abatacept showed a modest benefit"), [g.doc("d1", DOC)])
    assert result.status is NA and result.details["reason"] == "no_competitor_candidates"


def test_overlapping_entity_and_competitor_mentions():
    # The competitor candidate "mofetil" overlaps the entity's alias "mycophenolate mofetil": the
    # mention inside the entity mention is the entity, not a competitor.
    c = g.constraints(entity="MMF", aliases=("mycophenolate mofetil",))
    text = "Mofetil esters are prodrugs. Mofetil esters again. Mycophenolate mofetil improved proteinuria in adults."
    result = make("no_closer_competitor").check(c, g.answer("improved proteinuria in adults"), [g.doc("d1", text)])
    assert [x["term"] for x in result.details["candidates"]] == ["mofetil esters"]
    assert result.status is PASS
    sub = "Mofetil is an ester. Mofetil again. Mycophenolate mofetil improved proteinuria in adults."
    excluded = make("no_closer_competitor").check(c, g.answer("improved proteinuria in adults"), [g.doc("d1", sub)])
    assert excluded.details["candidates"][0]["excluded"] == "question_entity" and excluded.status is NA


def test_absent_target_document_condition():
    # Deceptive Grounding "absent": only the competing document is shown.
    docs = [g.doc("cy_doc", CY)]
    quote = g.answer("Rituximab improved proteinuria in case series", "cy_doc")
    assert make("quote_verbatim").check(g.constraints(), quote, docs).status is PASS
    assert make("entity_in_window").check(g.constraints(), quote, docs).status is FAIL
    assert make("no_closer_competitor").check(g.constraints(), quote, docs).status is FAIL
    assert make("qualifiers_in_window").check(g.constraints(), quote, docs).status is PASS


def test_competitor_check_never_uses_document_ids_or_metadata():
    a = [g.doc("cx_doc", CX), g.doc("cy_doc", CY)]
    renamed = [
        Document.create("zzz", CX, metadata={"role": "competitor"}),
        Document.create("aaa", CY, metadata={"role": "target"}),
    ]
    first = make("no_closer_competitor").check(g.constraints(), g.answer("Rituximab improved proteinuria in case series", "cy_doc"), a)
    second = make("no_closer_competitor").check(g.constraints(), g.answer("Rituximab improved proteinuria in case series", "aaa"), renamed)
    assert first.status is second.status is FAIL
    strip = lambda d: {k: v for k, v in d.items() if k not in {"source_id", "candidates"}}  # noqa: E731
    assert strip(first.details) == strip(second.details)


# -- premise_present ---------------------------------------------------------------- #


def premise(terms, scope="document", window_tokens=40):
    c = g.constraints(presuppositions=(("abatacept is used in lupus nephritis", terms),))
    return make("premise_present", premise_scope=scope, window_tokens=window_tokens).check(
        c, g.answer("Abatacept showed a modest benefit"), [g.doc("d1", DOC)]
    )


def test_premise_key_terms_present_in_the_cited_document():
    result = premise(("abatacept", "Lupus Nephritis"))
    assert result.status is PASS
    terms = result.details["presuppositions"][0]["key_terms"]
    assert [t["found"] for t in terms] == [True, True] and terms[1]["first"]["token_start"] == 7


def test_missing_premise_key_term_fails_and_is_named():
    result = premise(("abatacept", "children"))
    assert result.status is FAIL and result.details["reason"] == "key_term_missing"
    assert result.details["missing"] == [{"presupposition": "abatacept is used in lupus nephritis", "key_term": "children"}]


def test_premise_window_scope_restricts_to_the_window():
    assert premise(("twelve weeks",), scope="window", window_tokens=10).status is PASS
    assert premise(("twelve weeks",), scope="window", window_tokens=1).status is FAIL


def test_premise_without_key_terms_is_not_applicable_and_listed_unchecked():
    c = g.constraints(presuppositions=(("something is assumed", ()),))
    result = make("premise_present").check(c, g.answer("Abatacept showed a modest benefit"), [g.doc("d1", DOC)])
    assert result.status is NA and result.details["reason"] == "no_key_terms"
    assert result.details["unchecked"] == ["something is assumed"]
    none = make("premise_present").check(g.constraints(), g.answer("Abatacept showed a modest benefit"), [g.doc("d1", DOC)])
    assert none.status is NA


def test_premise_with_unknown_source_is_not_applicable():
    c = g.constraints(presuppositions=(("p", ("abatacept",)),))
    result = make("premise_present").check(c, g.answer("Abatacept showed a modest benefit", "d9"), [g.doc("d1", DOC)])
    assert result.status is NA and result.details["reason"] == "source_not_provided"
    window = make("premise_present", premise_scope="window").check(c, g.answer("not in the document at all", "d1"), [g.doc("d1", DOC)])
    assert window.status is NA and window.details["reason"] == "quote_not_found"
