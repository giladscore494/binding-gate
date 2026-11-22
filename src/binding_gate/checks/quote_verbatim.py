"""Check ``quote_verbatim`` (gate function C, first check).

PASS when all three hold:

1. ``source_id`` names one of the documents provided to the arm;
2. the quote has at least ``min_quote_tokens`` whitespace-separated tokens after
   the pre-registered normalisation (the same length rule as the provenance
   scorer, PREREGISTRATION.md section 6.3);
3. the normalised quote is a substring of the normalised cited document
   (section 6.1).

Otherwise FAIL, with ``details["reason"]`` the first failing condition in that
order: ``source_not_provided``, ``quote_too_short``, ``quote_not_found``. This
check is never ``not_applicable`` for an answered candidate.

``details`` also lists every occurrence in the cited document (token and
character positions in the normalised text, at most
``_evidence.MAX_LISTED``) and the ids of other provided documents that contain
the quote (``found_in_other_documents``), which is useful when a model cites the
wrong source. No model, no randomness, no gold metadata.
"""

from __future__ import annotations

from typing import Sequence

from binding_gate.checks import _evidence as ev
from binding_gate.contracts import AnswerCandidate, CheckResult, CheckStatus, Document, QuestionConstraints
from binding_gate.registry import register
from binding_gate.textnorm import normalize_text

NAME = "quote_verbatim"
DEFAULT_MIN_QUOTE_TOKENS = 5


def int_option(name: str, value: object, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}, got {value!r}")
    return value


@register("check", NAME)
class QuoteVerbatim:
    """Options: ``min_quote_tokens`` (PROPOSED 5, PREREGISTRATION.md section 5.1)."""

    name = NAME

    def __init__(self, *, min_quote_tokens: int = DEFAULT_MIN_QUOTE_TOKENS):
        self.min_quote_tokens = int_option("min_quote_tokens", min_quote_tokens)

    def check(
        self, constraints: QuestionConstraints, answer: AnswerCandidate, documents: Sequence[Document]
    ) -> CheckResult:
        ev.require_answered(answer)
        quote = answer.quote or ""
        quote_tokens = len(normalize_text(quote).split())
        cited = ev.cited_document(answer, documents)
        occurrences = ev.quote_occurrences(ev.DocText.of(cited), quote) if cited is not None else []
        others = [
            d.id
            for d in documents
            if d.id != answer.source_id and ev.quote_occurrences(ev.DocText.of(d), quote)
        ]
        details = {
            "source_id": answer.source_id,
            "source_provided": cited is not None,
            "quote_tokens": quote_tokens,
            "min_quote_tokens": self.min_quote_tokens,
            "occurrences": len(occurrences),
            "occurrence_spans": [span.to_dict() for span in occurrences[: ev.MAX_LISTED]],
            "found_in_other_documents": others,
        }
        if cited is None:
            reason = "source_not_provided"
        elif quote_tokens < self.min_quote_tokens:
            reason = "quote_too_short"
        elif not occurrences:
            reason = "quote_not_found"
        else:
            return CheckResult(self.name, CheckStatus.PASS, details)
        return CheckResult(self.name, CheckStatus.FAIL, {**details, "reason": reason})
