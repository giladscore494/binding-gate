"""Check ``premise_present`` (gate function D).

What the question presupposes must be present in the evidence: every key term
of every presupposition (``Presupposition.key_terms`` from the decomposer) must
be mentioned, by whole-token matching after the pre-registered normalisation,
in the evidence.

The evidence is selected by ``premise_scope`` (PROPOSED ``"document"``):

``"document"``  the whole cited document;
``"window"``    the window of ``window_tokens`` tokens around the anchor
                occurrence of the quote (the same window as the binding checks).

* NOT_APPLICABLE when no presupposition has a key term
  (``reason = "no_key_terms"``); when the cited document is not among the
  provided documents (``source_not_provided``); or, for scope ``"window"``,
  when the quote is not found in it (``quote_not_found``).
* PASS when every key term is present.
* FAIL otherwise (``reason = "key_term_missing"``); ``details["missing"]`` lists
  ``{"presupposition", "key_term"}`` pairs. A key term without any word
  character can never be matched and counts as missing.

Presuppositions without key terms are listed as ``unchecked`` (the decomposer
gave the check nothing to look for); they do not fail the check.
"""

from __future__ import annotations

from typing import Any, Sequence

from binding_gate.checks import _evidence as ev
from binding_gate.checks.entity_in_window import DEFAULT_WINDOW_TOKENS
from binding_gate.checks.quote_verbatim import int_option
from binding_gate.contracts import AnswerCandidate, CheckResult, CheckStatus, Document, QuestionConstraints
from binding_gate.registry import register

NAME = "premise_present"
SCOPES = ("document", "window")
DEFAULT_SCOPE = "document"


@register("check", NAME)
class PremisePresent:
    """Options: ``premise_scope`` (PROPOSED ``"document"``), ``window_tokens`` (used only by scope ``"window"``)."""

    name = NAME

    def __init__(self, *, premise_scope: str = DEFAULT_SCOPE, window_tokens: int = DEFAULT_WINDOW_TOKENS):
        if premise_scope not in SCOPES:
            raise ValueError(f"premise_scope must be one of {list(SCOPES)}, got {premise_scope!r}")
        self.premise_scope = premise_scope
        self.window_tokens = int_option("window_tokens", window_tokens, minimum=0)

    def check(
        self, constraints: QuestionConstraints, answer: AnswerCandidate, documents: Sequence[Document]
    ) -> CheckResult:
        ev.require_answered(answer)
        checked = [p for p in constraints.presuppositions if p.key_terms]
        unchecked = [p.text for p in constraints.presuppositions if not p.key_terms]
        base: dict[str, Any] = {"premise_scope": self.premise_scope, "unchecked": unchecked}
        if not checked:
            return CheckResult(self.name, CheckStatus.NOT_APPLICABLE, {**base, "reason": "no_key_terms"})
        cited = ev.cited_document(answer, documents)
        if cited is None:
            return CheckResult(
                self.name,
                CheckStatus.NOT_APPLICABLE,
                {**base, "reason": "source_not_provided", "source_id": answer.source_id},
            )
        anchor: ev.Span | None = None
        if self.premise_scope == "window":
            located = ev.locate(constraints, answer, documents)
            if isinstance(located, ev.NotLocated):
                return CheckResult(self.name, CheckStatus.NOT_APPLICABLE, {**base, **located.details()})
            doc = located.doc
            anchor = located.anchor
            base.update(located.base_details())
            base["window_tokens"] = self.window_tokens
        else:
            doc = ev.DocText.of(cited)
            base["source_id"] = cited.id
        entries: list[dict[str, Any]] = []
        missing: list[dict[str, str]] = []
        for presupposition in checked:
            terms = []
            for term in presupposition.key_terms:
                tokens = ev.tokens_of(term)
                spans = doc.find(tokens)
                if anchor is not None:
                    spans = [s for s in spans if ev.distance(s, anchor) <= self.window_tokens]
                found = spans[0] if spans else None
                if found is None:
                    missing.append({"presupposition": presupposition.text, "key_term": term})
                terms.append(
                    {
                        "key_term": term,
                        "unmatchable": not tokens,
                        "found": found is not None,
                        "first": found.to_dict() if found is not None else None,
                        "mentions": len(spans),
                    }
                )
            entries.append({"presupposition": presupposition.text, "key_terms": terms})
        details = {**base, "presuppositions": entries, "missing": missing}
        if missing:
            return CheckResult(self.name, CheckStatus.FAIL, {**details, "reason": "key_term_missing"})
        return CheckResult(self.name, CheckStatus.PASS, details)
