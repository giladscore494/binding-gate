"""Scorer ``grab_rag_answer``: deterministic answer scoring for GRAB-RAG (layer 2).

GRAB-RAG, unlike Deceptive Grounding, has gold answers, and its misleading
condition plants a known wrong answer. This scorer decides, by string matching
only (no model, no network), whether the VALUE of an answered final answer
carries a gold answer, the planted wrong answer, both or neither, and records
where its QUOTE was found. It complements ``provenance`` (run both; see
``configs/dev_grab_rag_baseline.toml``) and does not change it.

Value matching
--------------
``value`` *carries* an answer string when, after the pre-registered
normalisation (``binding_gate.textnorm.normalize_text``) and then GRAB-RAG's own
answer normalisation (``binding_gate.datasets.grab_rag.answer_normalize``: lower
case, punctuation and articles removed, ``1,000`` -> ``1000``, ``1st`` ->
``first``), the answer occurs in the value as a whole-word sequence. This is
GRAB-RAG's ``contains_answer`` rule, applied to the model's value instead of a
passage. Containment rather than exact match, because our answer format asks for
a value of one or two sentences, not a bare span.

* gold answers: ``item.gold["answers"]`` (all aliases);
* planted answer: ``item.gold["planted_answer"]`` (misleading items; ``None``
  for supportive items and for misleading items whose planted answer could not
  be derived, see ``details["planted_known"]``).

Labels
------
``correct``      the value carries a gold answer and not the planted answer
``misled``       the value carries the planted answer and no gold answer
``ambiguous``    it carries both
``other_wrong``  it carries neither
``abstained``    the final answer is an abstention
``unparseable``  the final answer is unparseable, OR answered with a quote
                 shorter than ``min_quote_tokens`` (same rule and default as
                 ``provenance``, so both scorers agree on what is scoreable)

``details`` records ``outcome`` (= the label), ``value_has_gold``,
``value_has_planted``, ``planted_known``, ``condition``, and for answered
candidates the quote's ``matched_document_ids`` and ``quote_source``:

``misleading``   the quote occurs in the edited passage (role ``competitor``)
``supporting``   it occurs in a gold passage (role ``target``; supportive items)
``other``        it occurs only in passages of role ``other`` (hard negatives)
``not_found``    it occurs in no shown passage

A ``misled`` answer whose quote_source is ``misleading`` is the layer-2
analogue of a wrong-entity answer: the model repeated the planted value and
quoted the passage that planted it.
"""

from __future__ import annotations

from typing import Any, Sequence

from binding_gate.contracts import AnswerCandidate, AnswerStatus, Item, ScoreResult
from binding_gate.datasets.grab_rag import contains_answer
from binding_gate.registry import register
from binding_gate.textnorm import normalize_text

SCORER_NAME = "grab_rag_answer"

LABEL_CORRECT = "correct"
LABEL_MISLED = "misled"
LABEL_AMBIGUOUS = "ambiguous"
LABEL_OTHER_WRONG = "other_wrong"
LABEL_ABSTAINED = "abstained"
LABEL_UNPARSEABLE = "unparseable"
LABELS = (LABEL_CORRECT, LABEL_MISLED, LABEL_AMBIGUOUS, LABEL_OTHER_WRONG, LABEL_ABSTAINED, LABEL_UNPARSEABLE)

QUOTE_SOURCE_BY_ROLE = {"competitor": "misleading", "target": "supporting", "other": "other"}
DEFAULT_MIN_QUOTE_TOKENS = 5


class GrabRagScoringError(ValueError):
    """Raised when an item lacks the gold metadata this scorer needs."""


def value_carries(value: str, answers: Sequence[str]) -> bool:
    """Whether ``value`` carries one of ``answers`` (see the module docstring)."""
    return contains_answer(normalize_text(value), [normalize_text(a) for a in answers])


@register("scorer", SCORER_NAME)
class GrabRagAnswerScorer:
    """Options: ``min_quote_tokens`` (default 5, as ``provenance``; PREREGISTRATION.md 6.3)."""

    name = SCORER_NAME

    def __init__(self, *, min_quote_tokens: int = DEFAULT_MIN_QUOTE_TOKENS):
        if isinstance(min_quote_tokens, bool) or not isinstance(min_quote_tokens, int) or min_quote_tokens < 1:
            raise ValueError("min_quote_tokens must be an integer >= 1")
        self.min_quote_tokens = min_quote_tokens

    def _gold(self, item: Item) -> tuple[list[str], str | None, dict[str, str]]:
        answers = item.gold.get("answers")
        if not isinstance(answers, list) or not answers or not all(isinstance(a, str) for a in answers):
            raise GrabRagScoringError(f"item {item.id!r}: gold['answers'] must be a non-empty list of strings")
        planted = item.gold.get("planted_answer")
        if planted is not None and not isinstance(planted, str):
            raise GrabRagScoringError(f"item {item.id!r}: gold['planted_answer'] must be a string or null")
        roles = item.gold.get("document_roles")
        if not isinstance(roles, dict) or any(roles.get(d.id) not in QUOTE_SOURCE_BY_ROLE for d in item.documents):
            raise GrabRagScoringError(f"item {item.id!r}: every document needs a role in gold['document_roles']")
        return answers, planted, roles

    def score(self, item: Item, final_answer: AnswerCandidate) -> ScoreResult:
        answers, planted, roles = self._gold(item)
        base: dict[str, Any] = {"condition": item.gold.get("condition"), "planted_known": planted is not None}
        if final_answer.status is AnswerStatus.ABSTAINED:
            return ScoreResult(self.name, LABEL_ABSTAINED, {"outcome": LABEL_ABSTAINED, **base})
        if final_answer.status is AnswerStatus.UNPARSEABLE:
            return ScoreResult(
                self.name, LABEL_UNPARSEABLE, {"outcome": LABEL_UNPARSEABLE, "reason": "unparseable_output", **base}
            )
        quote = normalize_text(final_answer.quote or "")
        quote_tokens = len(quote.split())
        if quote_tokens < self.min_quote_tokens:
            return ScoreResult(
                self.name,
                LABEL_UNPARSEABLE,
                {
                    "outcome": LABEL_UNPARSEABLE,
                    "reason": "quote_too_short",
                    "quote_tokens": quote_tokens,
                    "min_quote_tokens": self.min_quote_tokens,
                    **base,
                },
            )
        value = final_answer.value or ""
        has_gold = value_carries(value, answers)
        has_planted = planted is not None and value_carries(value, [planted])
        if has_gold and has_planted:
            label = LABEL_AMBIGUOUS
        elif has_gold:
            label = LABEL_CORRECT
        elif has_planted:
            label = LABEL_MISLED
        else:
            label = LABEL_OTHER_WRONG
        matched = [d.id for d in item.documents if quote in normalize_text(d.text)]
        sources = {QUOTE_SOURCE_BY_ROLE[roles[d]] for d in matched}
        quote_source = next((s for s in ("misleading", "supporting", "other") if s in sources), "not_found")
        return ScoreResult(
            self.name,
            label,
            {
                "outcome": label,
                **base,
                "value_has_gold": has_gold,
                "value_has_planted": has_planted,
                "matched_document_ids": matched,
                "quote_source": quote_source,
                "quote_tokens": quote_tokens,
                "source_id": final_answer.source_id,
                "source_id_matches": final_answer.source_id in matched,
            },
        )
