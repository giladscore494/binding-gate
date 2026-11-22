"""Check ``qualifiers_in_window`` (gate function C, third check).

Every qualifier of the constraints (time, place, version, unit, other) must be
mentioned, by its text or one of its aliases, within ``window_tokens`` tokens of
the anchor occurrence of the quote in the cited document.

* NOT_APPLICABLE when the constraints have no qualifiers
  (``reason = "no_qualifiers"``) or when the quote cannot be placed in its cited
  document (``source_not_provided`` / ``quote_not_found``).
* PASS when every qualifier has a mention within the window.
* FAIL otherwise (``reason = "qualifier_outside_window"``); ``details["missing"]``
  lists the qualifier texts without a mention in the window. A qualifier none
  of whose forms contains a word character can never be matched and counts as
  missing (``unmatchable``).

Each qualifier's entry in ``details["qualifiers"]`` gives its kind, text, the
nearest mention (form, distance, positions) or ``null``.
"""

from __future__ import annotations

from typing import Any, Sequence

from binding_gate.checks import _evidence as ev
from binding_gate.checks.entity_in_window import DEFAULT_WINDOW_TOKENS
from binding_gate.checks.quote_verbatim import int_option
from binding_gate.contracts import AnswerCandidate, CheckResult, CheckStatus, Document, QuestionConstraints
from binding_gate.registry import register

NAME = "qualifiers_in_window"


@register("check", NAME)
class QualifiersInWindow:
    """Options: ``window_tokens`` (PROPOSED 40, the same window as ``entity_in_window``)."""

    name = NAME

    def __init__(self, *, window_tokens: int = DEFAULT_WINDOW_TOKENS):
        self.window_tokens = int_option("window_tokens", window_tokens, minimum=0)

    def check(
        self, constraints: QuestionConstraints, answer: AnswerCandidate, documents: Sequence[Document]
    ) -> CheckResult:
        ev.require_answered(answer)
        if not constraints.qualifiers:
            return CheckResult(self.name, CheckStatus.NOT_APPLICABLE, {"reason": "no_qualifiers"})
        located = ev.locate(constraints, answer, documents)
        if isinstance(located, ev.NotLocated):
            return CheckResult(self.name, CheckStatus.NOT_APPLICABLE, located.details())
        entries: list[dict[str, Any]] = []
        missing: list[str] = []
        for qualifier in constraints.qualifiers:
            forms, unmatchable = ev.forms_of([qualifier.text, *qualifier.aliases])
            mention, d = ev.nearest(ev.mentions(located.doc, forms), located.anchor)
            in_window = mention is not None and d <= self.window_tokens
            if not in_window:
                missing.append(qualifier.text)
            entries.append(
                {
                    "kind": qualifier.kind.value,
                    "text": qualifier.text,
                    "unmatchable_forms": unmatchable,
                    "nearest": ev.describe_mention(mention, located.anchor, self.window_tokens),
                    "in_window": in_window,
                }
            )
        details = {
            **located.base_details(),
            "window_tokens": self.window_tokens,
            "qualifiers": entries,
            "missing": missing,
        }
        if missing:
            return CheckResult(self.name, CheckStatus.FAIL, {**details, "reason": "qualifier_outside_window"})
        return CheckResult(self.name, CheckStatus.PASS, details)
