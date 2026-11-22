"""Check ``entity_in_window`` (gate function C, second check).

The question's entity (its name or any alias from the constraints) must be
mentioned within ``window_tokens`` tokens of the quote in the cited document.

* NOT_APPLICABLE when the quote cannot be placed in its cited document
  (``details["reason"]``: ``source_not_provided`` or ``quote_not_found``); that
  failure belongs to ``quote_verbatim``, so an ablation without
  ``quote_verbatim`` measures what binding alone contributes.
* PASS when the nearest entity mention to the anchor occurrence of the quote
  is at a distance of at most ``window_tokens`` tokens (a mention inside the
  quote has distance 0).
* FAIL otherwise; ``details["reason"]`` is ``entity_not_in_document`` (no
  mention anywhere in the cited document) or ``entity_outside_window``.

Distances, mentions, the window and the anchor are defined in
``binding_gate.checks._evidence`` and PREREGISTRATION.md section 5.1.
"""

from __future__ import annotations

from typing import Sequence

from binding_gate.checks import _evidence as ev
from binding_gate.checks.quote_verbatim import int_option
from binding_gate.contracts import AnswerCandidate, CheckResult, CheckStatus, Document, QuestionConstraints
from binding_gate.registry import register

NAME = "entity_in_window"
DEFAULT_WINDOW_TOKENS = 40


@register("check", NAME)
class EntityInWindow:
    """Options: ``window_tokens`` (PROPOSED 40, PREREGISTRATION.md section 5.1)."""

    name = NAME

    def __init__(self, *, window_tokens: int = DEFAULT_WINDOW_TOKENS):
        self.window_tokens = int_option("window_tokens", window_tokens, minimum=0)

    def check(
        self, constraints: QuestionConstraints, answer: AnswerCandidate, documents: Sequence[Document]
    ) -> CheckResult:
        located = ev.locate(constraints, answer, documents)
        if isinstance(located, ev.NotLocated):
            return CheckResult(self.name, CheckStatus.NOT_APPLICABLE, located.details())
        details = {
            **located.base_details(),
            "window_tokens": self.window_tokens,
            "entity_forms": [form.text for form in located.entity_forms],
            "unmatchable_forms": list(located.entity_unmatchable),
            "entity_mentions_in_document": len(located.entity_mentions),
            "nearest": ev.describe_mention(located.nearest_entity, located.anchor, self.window_tokens),
        }
        if located.nearest_entity is None:
            return CheckResult(self.name, CheckStatus.FAIL, {**details, "reason": "entity_not_in_document"})
        if located.entity_distance > self.window_tokens:
            return CheckResult(self.name, CheckStatus.FAIL, {**details, "reason": "entity_outside_window"})
        return CheckResult(self.name, CheckStatus.PASS, details)
