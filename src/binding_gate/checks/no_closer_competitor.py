"""Check ``no_closer_competitor`` (gate function C, fourth check).

No competing entity may be closer to the quote than the question's entity.

Competing entity (deterministic, gold-free; PREREGISTRATION.md section 5.1)
---------------------------------------------------------------------------
The gate cannot know which other entities are "of the same kind" without a
model or a lexicon, so it uses the structure of the evidence itself: a
competing entity is the **subject of a provided document** that is not the
question's entity. Concretely (``binding_gate.checks._evidence``):

1. For every document provided to the arm (all of them, not only the cited
   one), its *lead term*: the tokens at the start of the document up to the
   first function word (fixed list ``STOP_WORDS``) within the lead segment
   (before the first comma, bracket, colon, semicolon, quote mark or sentence
   end), at most ``lead_max_tokens`` tokens; the longest prefix of that run
   which recurs at least ``lead_min_repeats`` times in the document and
   contains a letter.
2. A lead term is excluded when it equals, contains or is contained in the
   question's entity name or an alias, occurs in the question text, or is
   related in the same way to a qualifier.
3. The remaining lead terms are the candidates. Their mentions in the cited
   document are found by whole-token matching; a mention that overlaps a
   mention of the question's entity is ignored.

Only the question, the constraints and the document texts are used; document
ids, metadata and gold labels are not.

Decision
--------
* NOT_APPLICABLE when the quote cannot be placed in its cited document, or when
  no candidate remains after exclusion (``reason = "no_competitor_candidates"``).
* FAIL (``reason = "competitor_closer"``) when some candidate mention lies
  within ``window_tokens`` of the anchor occurrence and its distance is less
  than the distance of the nearest entity mention (an entity that is not
  mentioned in the document counts as infinitely far), or EQUAL to it when
  ``competitor_tie = "fail"`` (PROPOSED). Rationale for failing ties: when both
  are equally close (for example both inside the quote), the gate cannot tell
  which entity the quoted statement is about. ``competitor_tie = "pass"`` gives
  the question's entity the benefit of the doubt instead.
* PASS otherwise (no candidate mention in the window, or the entity strictly
  closer than every candidate mention in the window).
"""

from __future__ import annotations

from typing import Sequence

from binding_gate.checks import _evidence as ev
from binding_gate.checks.entity_in_window import DEFAULT_WINDOW_TOKENS
from binding_gate.checks.quote_verbatim import int_option
from binding_gate.contracts import AnswerCandidate, CheckResult, CheckStatus, Document, QuestionConstraints
from binding_gate.registry import register

NAME = "no_closer_competitor"
DEFAULT_LEAD_MAX_TOKENS = 4
DEFAULT_LEAD_MIN_REPEATS = 2
TIE_POLICIES = ("fail", "pass")
DEFAULT_TIE = "fail"


@register("check", NAME)
class NoCloserCompetitor:
    """Options: ``window_tokens`` (PROPOSED 40), ``lead_max_tokens`` (PROPOSED 4),
    ``lead_min_repeats`` (PROPOSED 2), ``competitor_tie`` (PROPOSED ``"fail"``)."""

    name = NAME

    def __init__(
        self,
        *,
        window_tokens: int = DEFAULT_WINDOW_TOKENS,
        lead_max_tokens: int = DEFAULT_LEAD_MAX_TOKENS,
        lead_min_repeats: int = DEFAULT_LEAD_MIN_REPEATS,
        competitor_tie: str = DEFAULT_TIE,
    ):
        if competitor_tie not in TIE_POLICIES:
            raise ValueError(f"competitor_tie must be one of {list(TIE_POLICIES)}, got {competitor_tie!r}")
        self.competitor_tie = competitor_tie
        self.window_tokens = int_option("window_tokens", window_tokens, minimum=0)
        self.lead_max_tokens = int_option("lead_max_tokens", lead_max_tokens)
        self.lead_min_repeats = int_option("lead_min_repeats", lead_min_repeats)

    def check(
        self, constraints: QuestionConstraints, answer: AnswerCandidate, documents: Sequence[Document]
    ) -> CheckResult:
        located = ev.locate(constraints, answer, documents)
        if isinstance(located, ev.NotLocated):
            return CheckResult(self.name, CheckStatus.NOT_APPLICABLE, located.details())
        candidates = ev.competitor_candidates(
            constraints, documents, max_tokens=self.lead_max_tokens, min_repeats=self.lead_min_repeats
        )
        active = [c for c in candidates if c.excluded is None]
        details = {
            **located.base_details(),
            "window_tokens": self.window_tokens,
            "lead_max_tokens": self.lead_max_tokens,
            "lead_min_repeats": self.lead_min_repeats,
            "competitor_tie": self.competitor_tie,
            "candidates": [c.to_dict() for c in candidates],
            "entity": ev.describe_mention(located.nearest_entity, located.anchor, self.window_tokens),
        }
        if not active:
            return CheckResult(
                self.name, CheckStatus.NOT_APPLICABLE, {**details, "reason": "no_competitor_candidates"}
            )
        competitor_mentions = [
            mention
            for mention in ev.mentions(located.doc, [ev.Form(c.term, c.tokens) for c in active])
            if not any(ev.overlaps(mention.span, e.span) for e in located.entity_mentions)
        ]
        in_window = [
            m for m in competitor_mentions if ev.distance(m.span, located.anchor) <= self.window_tokens
        ]
        closest, closest_distance = ev.nearest(in_window, located.anchor)
        details["competitor_mentions_in_document"] = len(competitor_mentions)
        details["competitor_mentions_in_window"] = [
            ev.describe_mention(m, located.anchor) for m in in_window[: ev.MAX_LISTED]
        ]
        details["nearest_competitor"] = ev.describe_mention(closest, located.anchor, self.window_tokens)
        closer = closest is not None and (
            closest_distance < located.entity_distance
            or (closest_distance == located.entity_distance and self.competitor_tie == "fail")
        )
        if closer:
            return CheckResult(self.name, CheckStatus.FAIL, {**details, "reason": "competitor_closer"})
        return CheckResult(self.name, CheckStatus.PASS, details)
