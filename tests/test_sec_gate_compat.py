"""PR 17: how the five deterministic checks behave on financial-statement text (layer 2).

No gate code was changed for layer 2 (the gate, its checks and their parameters are those of layer 1;
src/binding_gate/checks/ and gates/ are untouched by PR 17), so layer-1 behaviour is unchanged by
construction. These tests record, on the invented EDGAR fixture, what the unchanged gate does with
10-K statements, with constraints of the shape a decomposer is expected to produce (entity = the
registrant, aliases = tickers; one time qualifier with the year as alias). They are documentation of
expected behaviour and of known limitations (PREREGISTRATION.md section 16), not tuning.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import sec_helpers
from binding_gate.checks._evidence import DocText, competitor_candidates, lead_term
from binding_gate.contracts import AnswerCandidate, Entity, QuestionConstraints, Qualifier, QualifierKind
from binding_gate.datasets.sec_build import long_date
from binding_gate.datasets.sec_financials import SecFinancialsAdapter
from binding_gate.registry import create

PARAMETERS = dict(window_tokens=40, min_quote_tokens=5, lead_max_tokens=4, lead_min_repeats=2, premise_scope="document",
                  competitor_tie="fail")  # the PROPOSED layer-1 values, unchanged
CHECKS = ["quote_verbatim", "entity_in_window", "qualifiers_in_window", "no_closer_competitor", "premise_present"]
TOP_ROW_METRICS = {"revenue", "cash"}  # first row of their statement


@pytest.fixture(scope="module")
def items(tmp_path_factory):
    options = sec_helpers.write_dataset(tmp_path_factory.mktemp("secgate") / "d")
    return list(SecFinancialsAdapter(**options).iter_items())


def constraints(item) -> QuestionConstraints:
    gold = item.gold
    date = gold["report_date"]
    return QuestionConstraints(
        question=item.question,
        entity=Entity(name=gold["company"]["name"], aliases=tuple(gold["company"]["tickers"])),
        attribute=gold["metric"],
        qualifiers=(Qualifier(QualifierKind.TIME, f"fiscal year ended {long_date(date)}", aliases=(date[:4],)),),
        presuppositions=(),
    )


def verdicts(items, column=0):
    gate = create("gate", "binding", checks=CHECKS, **PARAMETERS)
    out = []
    for item in items:
        content = "\n\n".join(f"[Document {n} — source: {d.id}]\n{d.text}" for n, d in enumerate(item.documents, 1))
        reply = sec_helpers.scripted_answer(content + "\n\nQuestion: " + item.question, column=column)
        if reply["status"] != "answered":
            continue
        candidate = AnswerCandidate.answered(raw="{}", value=reply["value"], quote=reply["quote"], source_id=reply["source_id"])
        role = item.gold["document_roles"][reply["source_id"]]
        out.append((item, role, gate.evaluate(constraints(item), candidate, item.documents)))
    return out


def test_every_quote_from_the_peer_filing_is_blocked_by_entity_in_window(items):
    peer_answers = [(i, v) for i, role, v in verdicts(items) if role == "competitor"]
    assert peer_answers
    for item, verdict in peer_answers:
        assert verdict.decision.value == "research", item.id  # final abstention in this layer
        assert any(r.startswith("entity_in_window: entity_not_in_document") for r in verdict.reasons)


def test_correct_quotes_near_the_statement_title_are_accepted(items):
    target = [(i, v) for i, role, v in verdicts(items) if role == "target"]
    top = [v for i, v in target if i.gold["metric"] in TOP_ROW_METRICS]
    assert top and all(v.decision.value == "accept" for v in top)


def test_known_limitation_correct_answers_deep_in_a_table_are_blocked(items):
    """The registrant's name heads each statement; rows more than 40 tokens below it fail entity_in_window.
    On this fixture every correct answer for net income, operating income and total assets is blocked:
    the main risk for H2 on layer 2 (PREREGISTRATION.md sections 16 and 17)."""
    target = [(i, v) for i, role, v in verdicts(items) if role == "target"]
    deep = Counter((i.gold["metric"], v.decision.value) for i, v in target if i.gold["metric"] not in TOP_ROW_METRICS)
    assert deep and all(decision == "research" for (_m, decision) in deep)
    blocked = sum(n for (_m, d), n in deep.items() if d != "accept")
    assert blocked / len(target) > 0.5


def test_known_limitation_a_prior_year_figure_in_the_same_row_passes_the_qualifier_check(items):
    """Every year of a row sits within the window, so the current year's token is always "near" a prior-year
    value: qualifiers_in_window cannot catch a wrong-period answer in a multi-year table."""
    prior = [(i, v) for i, role, v in verdicts(items, column=1) if role == "target" and i.gold["metric"] in TOP_ROW_METRICS]
    assert prior and all(v.decision.value == "accept" for _i, v in prior)


def test_lead_term_of_a_filing_is_the_registrant_name_unless_it_starts_with_a_function_word(items):
    seen = {}
    for item in items:
        for document in item.documents:
            name = document.text.split(":", 1)[0]
            seen[name] = lead_term(DocText.of(document), max_tokens=4, min_repeats=2)
    assert seen["Zentrix Systems Inc."] == ("zentrix", "systems", "inc")
    assert seen["Abernet Bank Holding Corp."] == ("abernet", "bank", "holding", "corp")
    assert seen["The Ellery Stores Company"] is None  # "the" is a stop word: no competitor candidate (limitation)


def test_the_peer_is_a_competitor_candidate_and_the_target_is_excluded(items):
    item = next(i for i in items if i.stratum == "present/peer" and not i.gold["competitor"]["name"].startswith("The "))
    candidates = competitor_candidates(constraints(item), item.documents, max_tokens=4, min_repeats=2)
    by_term = {c.term: c.excluded for c in candidates}
    peer = " ".join(lead_term(DocText.of(next(d for d in item.documents if item.gold["document_roles"][d.id] == "competitor")),
                              max_tokens=4, min_repeats=2))
    assert by_term[peer] is None
    assert "question_entity" in by_term.values()


def test_pr17_did_not_change_the_gate():
    root = Path(__file__).resolve().parents[1] / "src" / "binding_gate"
    for path in [*(root / "checks").glob("*.py"), *(root / "gates").glob("*.py")]:
        text = path.read_text(encoding="utf-8")
        assert "sec_financials" not in text and "PR 17" not in text, path.name
