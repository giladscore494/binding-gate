"""PR 18: binding-scope options of the gate (document subject, table-cell binding, binding_scope profiles).

Synthetic texts only; every distance is controlled with ``gate_fakes.filler``. The defaults are covered by
``test_gate_scope_identity.py`` (byte-identical to the gate before PR 18); here the NON-default behaviour.
"""

from __future__ import annotations

import pytest

import gate_fakes as g
from binding_gate.checks import _evidence as ev
from binding_gate.checks import _tables
from binding_gate.contracts import CheckStatus, Decision, Entity, Qualifier, QualifierKind, QuestionConstraints
from binding_gate.gates.binding import BINDING_SCOPES, enabled_scope_options, resolve_scope
from binding_gate.registry import create
from binding_gate.textnorm import normalize_text

FAR = g.filler(80)


def check(name: str, **options):
    return create("check", name, **options)


def gate(**parameters):
    checks = ["quote_verbatim", "entity_in_window", "qualifiers_in_window", "no_closer_competitor", "premise_present"]
    return create("gate", "binding", checks=checks, **parameters)


# --------------------------------------------------------------------------- #
# Document subject
# --------------------------------------------------------------------------- #

MONOGRAPH = g.doc(
    "cx_doc",
    f"Abatacept is a fusion protein used in rheumatology. {FAR} In lupus nephritis abatacept was studied. "
    f"{FAR} Patients receiving the drug showed a sustained renal response at week fifty two.",
)
COMPETING = g.doc(
    "cy_doc",
    f"Rituximab depletes B cells and is used widely. {FAR} Rituximab improved renal outcomes in lupus nephritis "
    "cohorts with a sustained renal response at week fifty two.",
)
QUOTE = "a sustained renal response at week fifty two"


def test_document_subject_skips_one_leading_article_and_needs_repetition():
    def subject(text):
        return ev.document_subject(ev.DocText.of(g.doc("d", text)), max_tokens=4, min_repeats=2)

    assert subject("The Ellery Stores Company: x. The Ellery Stores Company y") == ("ellery", "stores", "company")
    assert ev.lead_term(ev.DocText.of(g.doc("d", "The Ellery Stores Company: x. Ellery Stores Company")),
                        max_tokens=4, min_repeats=2) is None  # the competitor rule is unchanged
    assert subject("Abatacept is good. abatacept again") == ("abatacept",)
    assert subject("Abatacept is good, once only") is None
    assert subject("the the x") is None  # only ONE article is skipped; the second "the" is a stop word


def test_subject_binding_reasons():
    c = g.constraints()
    located = ev.locate(c, g.answer(QUOTE, "cx_doc"), [MONOGRAPH, COMPETING])
    binding = ev.subject_binding(c, located, [MONOGRAPH, COMPETING], window_tokens=40, max_tokens=4, min_repeats=2)
    assert binding.bound and binding.subject == ("abatacept",) and binding.entity_form == "abatacept"
    other = ev.locate(g.constraints(entity="belimumab"), g.answer(QUOTE, "cx_doc"), [MONOGRAPH, COMPETING])
    assert ev.subject_binding(g.constraints(entity="belimumab"), other, [MONOGRAPH, COMPETING], window_tokens=40,
                              max_tokens=4, min_repeats=2).reason == "subject_not_entity"
    near = g.doc("cx_doc", f"Abatacept is used. {FAR} abatacept. {FAR} Like rituximab, patients {QUOTE}.")
    located = ev.locate(c, g.answer(QUOTE, "cx_doc"), [near, COMPETING])
    assert ev.subject_binding(c, located, [near, COMPETING], window_tokens=40, max_tokens=4,
                              min_repeats=2).reason == "competitor_in_window"
    nosubject = g.doc("cx_doc", f"In this review, abatacept is used. {FAR} abatacept. {FAR} Patients {QUOTE}.")
    located = ev.locate(c, g.answer(QUOTE, "cx_doc"), [nosubject])
    assert ev.subject_binding(c, located, [nosubject], window_tokens=40, max_tokens=4,
                              min_repeats=2).reason == "no_document_subject"


def test_a_generic_subject_shorter_than_the_entity_does_not_bind_it():
    c = g.constraints(entity="Bank of America Corp")
    doc = g.doc("d1", f"Bank results improved. {FAR} The bank reported {QUOTE}. Bank")
    located = ev.locate(c, g.answer(QUOTE, "d1"), [doc])
    binding = ev.subject_binding(c, located, [doc], window_tokens=40, max_tokens=4, min_repeats=2)
    assert binding.subject == ("bank",) and binding.reason == "subject_not_entity"


@pytest.mark.parametrize(
    ("scope", "source", "status", "reason"),
    [
        ("window", "cx_doc", CheckStatus.FAIL, "entity_outside_window"),
        ("document_subject", "cx_doc", CheckStatus.PASS, None),
        ("window_or_document_subject", "cx_doc", CheckStatus.PASS, None),
        ("document_subject", "cy_doc", CheckStatus.FAIL, "subject_not_entity"),
        ("window_or_document_subject", "cy_doc", CheckStatus.FAIL, "entity_not_in_document"),
    ],
)
def test_entity_scope(scope, source, status, reason):
    result = check("entity_in_window", entity_scope=scope).check(g.constraints(), g.answer(QUOTE, source), [MONOGRAPH, COMPETING])
    assert result.status is status and result.details.get("reason") == reason
    if scope == "window":
        assert "entity_scope" not in result.details  # default details unchanged
    else:
        assert result.details["entity_scope"] == scope and "document_subject" in result.details


def test_window_or_document_subject_passes_by_window_without_computing_the_subject():
    near = g.answer("Abatacept is a fusion protein used", "cx_doc")
    result = check("entity_in_window", entity_scope="window_or_document_subject").check(g.constraints(), near, [MONOGRAPH])
    assert result.passed and result.details["bound_by"] == "window" and "document_subject" not in result.details


@pytest.mark.parametrize(
    ("scope", "source", "passed"),
    [("window", "cx_doc", False), ("document_subject", "cx_doc", True), ("window_or_document_subject", "cx_doc", True),
     ("document_subject", "cy_doc", False), ("window_or_document_subject", "cy_doc", True)],  # cy: C is in the window
)
def test_qualifier_scope(scope, source, passed):
    result = check("qualifiers_in_window", qualifier_scope=scope).check(g.constraints(), g.answer(QUOTE, source),
                                                                        [MONOGRAPH, COMPETING])
    assert result.passed is passed
    if passed:
        assert result.details["qualifiers"][0]["bound_by"] == ("window" if source == "cy_doc" else "document_subject")


def test_qualifier_document_subject_needs_the_qualifier_somewhere_in_the_document():
    c = g.constraints(qualifiers=(("membranous nephropathy", ()),))
    result = check("qualifiers_in_window", qualifier_scope="document_subject").check(c, g.answer(QUOTE, "cx_doc"), [MONOGRAPH])
    assert result.failed and result.details["missing"] == ["membranous nephropathy"]


def test_premise_subject_scopes_add_a_condition_to_document_and_window():
    c = g.constraints(presuppositions=(("abatacept was tried in lupus nephritis", ("lupus nephritis",)),))
    docs = [MONOGRAPH, COMPETING]
    for scope, source, passed in (("document", "cy_doc", True), ("document_subject", "cy_doc", False),
                                  ("document_subject", "cx_doc", True), ("window_or_document_subject", "cx_doc", True),
                                  ("window", "cx_doc", False)):
        result = check("premise_present", premise_scope=scope).check(c, g.answer(QUOTE, source), docs)
        assert result.passed is passed, (scope, source)


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #

STATEMENT = "\n".join([
    "Zentrix Systems Inc.: annual report on Form 10-K for the fiscal year ended December 31, 2025",
    "Zentrix Systems Inc.: CONSOLIDATED STATEMENTS OF OPERATIONS - USD ($) $ in Millions",
    "Total revenues | Dec. 31, 2025: $ 30,333 | Dec. 31, 2024: $ 28,209",
    "Operating expenses:",
    *[f"Expense line {i} | Dec. 31, 2025: {1000 + i} | Dec. 31, 2024: {900 + i}" for i in range(12)],
    "Operating income | Dec. 31, 2025: 5,459 | Dec. 31, 2024: 5,077",
])
FILING = g.doc("doc1", STATEMENT)


def sec_constraints(year_alias: bool = True, unit: bool = False) -> QuestionConstraints:
    qualifiers = [Qualifier(QualifierKind.TIME, "fiscal year ended December 31, 2025", aliases=("2025",) if year_alias else ())]
    if unit:
        qualifiers.append(Qualifier(QualifierKind.UNIT, "millions"))
    return QuestionConstraints(
        question="What was Zentrix Systems Inc.'s operating income for the fiscal year ended December 31, 2025?",
        entity=Entity(name="Zentrix Systems Inc.", aliases=("ZTX",)), attribute="operating income",
        qualifiers=tuple(qualifiers), presuppositions=())


def test_flattened_rows_are_parsed_with_headers_labels_and_the_caption():
    tables = _tables.parse_tables(STATEMENT, normalize_text(STATEMENT))
    assert tables.aligned and len(tables.tables) == 1
    (table,) = tables.tables
    assert table.format == "flattened" and table.headers == ("dec. 31, 2025", "dec. 31, 2024")
    assert table.caption.startswith("zentrix systems inc.: consolidated statements")  # "Operating expenses:" skipped
    assert table.row_labels[0] == "total revenues" and len(tables.cells) == 2 * 14


def test_pipe_and_tab_tables():
    text = "Revenue by segment\n| Segment | 2024 | 2025 |\n|---|---|---|\n| North | 10.5 | 12.5 |\n| South | 3 | 4 |\n\nA\tB\tC\nx\t1\t2\n"
    tables = _tables.parse_tables(text, normalize_text(text))
    assert [(t.format, t.headers, t.caption) for t in tables.tables] == [
        ("pipe", ("2024", "2025"), "revenue by segment"), ("tab", ("b", "c"), None)]
    ragged = "| a | b |\n| c |\n"
    assert _tables.parse_tables(ragged, normalize_text(ragged)).tables == ()


def test_a_long_prose_line_is_never_a_caption():
    text = g.filler(50) + "\n| k | 2025 |\n| v | 1 |"
    assert _tables.parse_tables(text, normalize_text(text)).tables[0].caption is None


def run_qualifiers(quote, value, constraints=None, **options):
    return check("qualifiers_in_window", **options).check(constraints or sec_constraints(), g.answer(quote, "doc1", value), [FILING])


def test_table_binding_passes_the_current_year_cell_and_fails_a_same_row_prior_year_cell():
    current = run_qualifiers("Operating income | Dec. 31, 2025: 5,459", "$5,459 million", table_binding=True)
    assert current.passed and current.details["qualifiers"][0]["bound_by"] == "table_column"
    prior = run_qualifiers("Dec. 31, 2025: 5,459 | Dec. 31, 2024: 5,077", "$5,077 million", table_binding=True)
    assert prior.failed and prior.details["qualifiers"][0]["table_rule"] == "table_column"
    # without table binding the year in the same row passes the window rule: the H1 leak table binding closes
    assert run_qualifiers("Dec. 31, 2025: 5,459 | Dec. 31, 2024: 5,077", "$5,077 million").passed
    # and neither the window nor the document subject can rescue a cell from another column
    for scope in ("window_or_document_subject", "document_subject"):
        assert run_qualifiers("Dec. 31, 2025: 5,459 | Dec. 31, 2024: 5,077", "$5,077 million",
                              table_binding=True, qualifier_scope=scope).failed


def test_several_quoted_cells_are_narrowed_by_the_value_and_kept_all_without_a_number():
    row = "Operating income | Dec. 31, 2025: 5,459 | Dec. 31, 2024: 5,077"
    assert run_qualifiers(row, "5459", table_binding=True).passed
    unnarrowed = run_qualifiers(row, "about five billion", table_binding=True)
    assert unnarrowed.failed and len(unnarrowed.details["bound_cells"]) == 2


def test_a_qualifier_not_named_by_the_table_falls_back_to_the_caption_or_the_scope():
    deep = "Operating income | Dec. 31, 2025: 5,459"
    with_unit = run_qualifiers(deep, "5,459", sec_constraints(unit=True), table_binding=True)
    assert with_unit.passed and [q["bound_by"] for q in with_unit.details["qualifiers"]] == ["table_column", "table_caption"]
    no_alias = run_qualifiers(deep, "5,459", sec_constraints(year_alias=False), table_binding=True)
    assert no_alias.failed  # "fiscal year ended december 31, 2025" is only in the first line, far away
    by_subject = run_qualifiers(deep, "5,459", sec_constraints(year_alias=False), table_binding=True,
                                qualifier_scope="window_or_document_subject")
    assert by_subject.passed and by_subject.details["qualifiers"][0]["bound_by"] == "document_subject"


def test_row_labels_decide_a_qualifier_named_by_rows():
    text = "Prevalence by region\n| Region | Rate |\n| Europe | 4.1 |\n| Asia | 2.2 |\n"
    doc = g.doc("t", text)
    c = QuestionConstraints(question="What is the rate in Europe?", entity=Entity("Rate"), attribute="rate",
                            qualifiers=(Qualifier(QualifierKind.PLACE, "Europe"),), presuppositions=())
    qualify = check("qualifiers_in_window", table_binding=True)
    assert qualify.check(c, g.answer("| Europe | 4.1 |", "t", "4.1"), [doc]).passed
    assert qualify.check(c, g.answer("| Asia | 2.2 |", "t", "2.2"), [doc]).failed  # "europe" is in the window


def test_entity_subject_binds_figures_deep_in_a_statement():
    deep = g.answer("Operating income | Dec. 31, 2025: 5,459", "doc1", "$5,459 million")
    assert gate().evaluate(sec_constraints(), deep, [FILING]).decision is Decision.RESEARCH
    assert gate(binding_scope="subject").evaluate(sec_constraints(), deep, [FILING]).decision is Decision.ACCEPT
    prior = g.answer("Dec. 31, 2025: 5,459 | Dec. 31, 2024: 5,077", "doc1", "$5,077 million")
    assert gate(binding_scope="subject").evaluate(sec_constraints(), prior, [FILING]).decision is Decision.ACCEPT
    verdict = gate(binding_scope="subject_table").evaluate(sec_constraints(), prior, [FILING])
    assert verdict.decision is Decision.RESEARCH and verdict.reasons == ("qualifiers_in_window: qualifier_outside_window",)


def test_tables_are_ignored_when_lines_do_not_align_with_the_normalised_text():
    assert _tables.parse_tables("a | b: c\nx", "something else").aligned is False


# --------------------------------------------------------------------------- #
# Profiles and the gate
# --------------------------------------------------------------------------- #


def test_profiles_resolve_conflict_and_report_custom():
    assert resolve_scope({}) == {**BINDING_SCOPES["window"], "binding_scope": "window"}
    assert resolve_scope({"binding_scope": "subject_table"})["table_binding"] is True
    assert resolve_scope({"table_binding": True})["binding_scope"] == "table"
    assert resolve_scope({"entity_scope": "document_subject"})["binding_scope"] == "custom"
    assert resolve_scope({"binding_scope": "table", "table_binding": True})["binding_scope"] == "table"
    with pytest.raises(ValueError, match="contradicts binding_scope"):
        resolve_scope({"binding_scope": "table", "table_binding": False})
    with pytest.raises(ValueError, match="binding_scope must be one of"):
        resolve_scope({"binding_scope": "everything"})
    assert [enabled_scope_options(p) for p in BINDING_SCOPES] == [0, 2, 1, 3]
    assert gate(binding_scope="subject").parameters["qualifier_scope"] == "window_or_document_subject"


@pytest.mark.parametrize(
    ("parameters", "message"),
    [({"entity_scope": "page"}, "entity_scope"), ({"qualifier_scope": 1}, "qualifier_scope"),
     ({"table_binding": "yes"}, "table_binding"), ({"premise_scope": "doc"}, "premise_scope")],
)
def test_invalid_scope_options_are_refused(parameters, message):
    with pytest.raises(ValueError, match=message):
        gate(**parameters)
