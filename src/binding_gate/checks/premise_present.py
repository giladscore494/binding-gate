"""Check ``premise_present`` (gate function D).

What the question presupposes must be present in the evidence: every key term
of every presupposition (``Presupposition.key_terms`` from the decomposer) must
be mentioned, by whole-token matching after the pre-registered normalisation,
in the evidence.

The evidence is selected by ``premise_scope`` (PROPOSED ``"document"``):

``"document"``  the whole cited document;
``"window"``    the window of ``window_tokens`` tokens around the anchor
                occurrence of the quote (the same window as the binding checks).

PR 18 adds, for symmetry with ``entity_scope`` / ``qualifier_scope``:

``"document_subject"``            the whole cited document, but only if it is bound
                                  to the question's entity by its subject
                                  (``_evidence.subject_binding``); otherwise every
                                  key term counts as missing;
``"window_or_document_subject"``  a key term passes if it is within the window, or
                                  if it is in the document and the document is
                                  bound to the entity by its subject.

They are NOT candidates of the section 5.1 selection: the default
``"document"`` already accepts a key term anywhere in the cited document, so
both new values can only block more answers (each is ``"document"`` or
``"window"`` with an added condition); they exist so that an ablation can
measure that condition. Both need the quote to be located (else not_applicable,
as for ``"window"``).

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
from binding_gate.checks.entity_in_window import DEFAULT_LEAD_MAX_TOKENS, DEFAULT_LEAD_MIN_REPEATS, DEFAULT_WINDOW_TOKENS
from binding_gate.checks.quote_verbatim import int_option
from binding_gate.contracts import AnswerCandidate, CheckResult, CheckStatus, Document, QuestionConstraints
from binding_gate.registry import register

NAME = "premise_present"
SCOPES = ("document", "window", "document_subject", "window_or_document_subject")
DEFAULT_SCOPE = "document"


@register("check", NAME)
class PremisePresent:
    """Options: ``premise_scope`` (PROPOSED ``"document"``), ``window_tokens`` (used only by the window scopes);
    PR 18: ``lead_max_tokens`` and ``lead_min_repeats`` (used only by the subject scopes)."""

    name = NAME

    def __init__(
        self,
        *,
        premise_scope: str = DEFAULT_SCOPE,
        window_tokens: int = DEFAULT_WINDOW_TOKENS,
        lead_max_tokens: int = DEFAULT_LEAD_MAX_TOKENS,
        lead_min_repeats: int = DEFAULT_LEAD_MIN_REPEATS,
    ):
        if premise_scope not in SCOPES:
            raise ValueError(f"premise_scope must be one of {list(SCOPES)}, got {premise_scope!r}")
        self.premise_scope = premise_scope
        self.window_tokens = int_option("window_tokens", window_tokens, minimum=0)
        self.lead_max_tokens = int_option("lead_max_tokens", lead_max_tokens)
        self.lead_min_repeats = int_option("lead_min_repeats", lead_min_repeats)

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
        subject_bound = False
        if self.premise_scope != "document":
            located = ev.locate(constraints, answer, documents)
            if isinstance(located, ev.NotLocated):
                return CheckResult(self.name, CheckStatus.NOT_APPLICABLE, {**base, **located.details()})
            doc = located.doc
            anchor = located.anchor
            base.update(located.base_details())
            base["window_tokens"] = self.window_tokens
            if self.premise_scope != "window":  # PR 18 subject scopes
                binding = ev.subject_binding(
                    constraints, located, documents, window_tokens=self.window_tokens,
                    max_tokens=self.lead_max_tokens, min_repeats=self.lead_min_repeats,
                )
                base["document_subject"] = binding.to_dict()
                subject_bound = binding.bound
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
                if self.premise_scope == "document_subject":
                    spans = spans if subject_bound else []
                elif self.premise_scope == "window_or_document_subject":
                    spans = spans if subject_bound else [s for s in spans if ev.distance(s, anchor) <= self.window_tokens]  # type: ignore[arg-type]
                elif anchor is not None:
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
