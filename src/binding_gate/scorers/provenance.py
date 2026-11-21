"""Scorer ``provenance``: deterministic primary scoring (PREREGISTRATION.md 6.3, 6.4).

Pure string matching; no model, no network. The quote of an ``answered`` final
answer is searched, after the pre-registered normalisation
(``binding_gate.textnorm``), in every document of the item. Document roles come
from ``item.gold["document_roles"]`` (document id -> ``target`` / ``competitor``
/ ``other``).

Labels
------
The four reserved provenance labels, for an answered candidate whose quote is
long enough:

``target_only``      found in at least one target document and in no competitor document
``competitor_only``  found in at least one competitor document and in no target document
``both``             found in a target document and in a competitor document
``neither``          found in no target and no competitor document

A quote found only in a document of role ``other`` is ``neither`` with respect
to the two roles; ``details["matched_document_ids"]`` still lists where it was
found. (Deceptive Grounding has no ``other`` documents.)

So that every record gets exactly one label from this scorer, two more labels
cover final answers that are not scoreable answers:

``abstained``        the final answer is an abstention
``unparseable``      the final answer is unparseable, OR it is answered with a
                     quote shorter than ``min_quote_tokens`` whitespace-separated
                     tokens after normalisation (section 6.3;
                     ``details["reason"] = "quote_too_short"``)

``details`` always records ``outcome`` (the section 6.3 outcome category), and
for answered candidates the matched document ids, the quote length and whether
the cited ``source_id`` is a document the quote was actually found in
(``source_id_matches``; a mismatch never changes the label, it is counted).
"""

from __future__ import annotations

from binding_gate.contracts import AnswerCandidate, AnswerStatus, Item, ScoreResult
from binding_gate.registry import register
from binding_gate.textnorm import normalize_text

LABEL_TARGET_ONLY = "target_only"
LABEL_COMPETITOR_ONLY = "competitor_only"
LABEL_BOTH = "both"
LABEL_NEITHER = "neither"
LABEL_ABSTAINED = "abstained"
LABEL_UNPARSEABLE = "unparseable"

OUTCOME_BY_LABEL = {
    LABEL_TARGET_ONLY: "target_grounded",
    LABEL_COMPETITOR_ONLY: "wrong_entity",
    LABEL_BOTH: "ambiguous",
    LABEL_NEITHER: "ungrounded",
    LABEL_ABSTAINED: "abstention",
    LABEL_UNPARSEABLE: "unparseable",
}
"""Scorer label -> outcome category of PREREGISTRATION.md section 6.3. The
seventh category, ``error``, belongs to records without a final answer; those
are never passed to a scorer."""

ROLES = ("target", "competitor", "other")
DEFAULT_MIN_QUOTE_TOKENS = 5


class ProvenanceError(ValueError):
    """Raised when an item lacks usable document roles."""


@register("scorer", "provenance")
class ProvenanceScorer:
    """Options: ``min_quote_tokens`` (default 5, PREREGISTRATION.md section 6.3, PROPOSED)."""

    name = "provenance"

    def __init__(self, *, min_quote_tokens: int = DEFAULT_MIN_QUOTE_TOKENS):
        if isinstance(min_quote_tokens, bool) or not isinstance(min_quote_tokens, int) or min_quote_tokens < 1:
            raise ValueError("min_quote_tokens must be an integer >= 1")
        self.min_quote_tokens = min_quote_tokens

    def _roles(self, item: Item) -> dict[str, str]:
        roles = item.gold.get("document_roles")
        if not isinstance(roles, dict):
            raise ProvenanceError(f"item {item.id!r}: gold['document_roles'] is missing")
        for document in item.documents:
            if roles.get(document.id) not in ROLES:
                raise ProvenanceError(
                    f"item {item.id!r}: document {document.id!r} has no role in gold['document_roles']"
                )
        return roles

    def _result(self, label: str, **details: object) -> ScoreResult:
        return ScoreResult(self.name, label, {"outcome": OUTCOME_BY_LABEL[label], **details})

    def score(self, item: Item, final_answer: AnswerCandidate) -> ScoreResult:
        roles = self._roles(item)
        if final_answer.status is AnswerStatus.ABSTAINED:
            return self._result(LABEL_ABSTAINED)
        if final_answer.status is AnswerStatus.UNPARSEABLE:
            return self._result(LABEL_UNPARSEABLE, reason="unparseable_output")

        quote = normalize_text(final_answer.quote or "")
        quote_tokens = len(quote.split())
        if quote_tokens < self.min_quote_tokens:
            return self._result(
                LABEL_UNPARSEABLE,
                reason="quote_too_short",
                quote_tokens=quote_tokens,
                min_quote_tokens=self.min_quote_tokens,
            )
        matched = [d.id for d in item.documents if quote in normalize_text(d.text)]
        matched_roles = {roles[document_id] for document_id in matched}
        if "target" in matched_roles and "competitor" in matched_roles:
            label = LABEL_BOTH
        elif "target" in matched_roles:
            label = LABEL_TARGET_ONLY
        elif "competitor" in matched_roles:
            label = LABEL_COMPETITOR_ONLY
        else:
            label = LABEL_NEITHER
        return self._result(
            label,
            matched_document_ids=matched,
            quote_tokens=quote_tokens,
            source_id=final_answer.source_id,
            source_id_is_shown_document=final_answer.source_id in roles,
            source_id_matches=final_answer.source_id in matched,
        )
