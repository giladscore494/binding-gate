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

Binding scope (PR 18, option ``entity_scope``; PROPOSED default ``"window"``)
---------------------------------------------------------------------------
``"window"``                      the rule above (the only behaviour before PR 18;
                                  details are byte-identical to it).
``"document_subject"``            PASS iff the cited document is bound to the
                                  entity by its subject (``_evidence.subject_binding``:
                                  the document's subject contains the entity name or
                                  an alias, is no competitor candidate, and no
                                  competitor candidate is mentioned within the
                                  window); otherwise FAIL with the subject reason
                                  (``no_document_subject``, ``subject_not_entity``,
                                  ``subject_is_competitor``, ``competitor_in_window``).
``"window_or_document_subject"``  PASS if the window rule passes, else if the
                                  document-subject rule passes; otherwise FAIL with
                                  the window rule's reason.

Why: proximity is the right test in running prose that discusses several
entities, but in a document whose whole subject is one entity (a monograph
about a drug, the financial statements of one company) a correct figure can lie
far from the nearest repetition of the name. The subject rule uses the same
lead-term machinery as ``no_closer_competitor`` (``lead_max_tokens``,
``lead_min_repeats``), so "subject" and "competing entity" are defined alike.
"""

from __future__ import annotations

from typing import Sequence

from binding_gate.checks import _evidence as ev
from binding_gate.checks.quote_verbatim import int_option
from binding_gate.contracts import AnswerCandidate, CheckResult, CheckStatus, Document, QuestionConstraints
from binding_gate.registry import register

NAME = "entity_in_window"
DEFAULT_WINDOW_TOKENS = 40
SCOPES = ("window", "document_subject", "window_or_document_subject")
"""Values of the binding-scope options ``entity_scope`` and ``qualifier_scope`` (PR 18)."""
DEFAULT_SCOPE = "window"
DEFAULT_LEAD_MAX_TOKENS = 4  # the defaults of no_closer_competitor, which imports this module
DEFAULT_LEAD_MIN_REPEATS = 2


def scope_option(name: str, value: object) -> str:
    if value not in SCOPES:
        raise ValueError(f"{name} must be one of {list(SCOPES)}, got {value!r}")
    return str(value)


@register("check", NAME)
class EntityInWindow:
    """Options: ``window_tokens`` (PROPOSED 40, PREREGISTRATION.md section 5.1); PR 18: ``entity_scope``
    (PROPOSED ``"window"``), ``lead_max_tokens`` and ``lead_min_repeats`` (used only by a subject scope)."""

    name = NAME

    def __init__(
        self,
        *,
        window_tokens: int = DEFAULT_WINDOW_TOKENS,
        entity_scope: str = DEFAULT_SCOPE,
        lead_max_tokens: int = DEFAULT_LEAD_MAX_TOKENS,
        lead_min_repeats: int = DEFAULT_LEAD_MIN_REPEATS,
    ):
        self.window_tokens = int_option("window_tokens", window_tokens, minimum=0)
        self.entity_scope = scope_option("entity_scope", entity_scope)
        self.lead_max_tokens = int_option("lead_max_tokens", lead_max_tokens)
        self.lead_min_repeats = int_option("lead_min_repeats", lead_min_repeats)

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
            window_reason: str | None = "entity_not_in_document"
        elif located.entity_distance > self.window_tokens:
            window_reason = "entity_outside_window"
        else:
            window_reason = None
        if self.entity_scope == "window":  # the pre-PR-18 behaviour, details unchanged
            if window_reason is not None:
                return CheckResult(self.name, CheckStatus.FAIL, {**details, "reason": window_reason})
            return CheckResult(self.name, CheckStatus.PASS, details)
        details["entity_scope"] = self.entity_scope
        if self.entity_scope == "window_or_document_subject" and window_reason is None:
            return CheckResult(self.name, CheckStatus.PASS, {**details, "bound_by": "window"})
        binding = ev.subject_binding(
            constraints, located, documents, window_tokens=self.window_tokens,
            max_tokens=self.lead_max_tokens, min_repeats=self.lead_min_repeats,
        )
        details["document_subject"] = binding.to_dict()
        if binding.bound:
            return CheckResult(self.name, CheckStatus.PASS, {**details, "bound_by": "document_subject"})
        reason = window_reason if self.entity_scope == "window_or_document_subject" else binding.reason
        return CheckResult(self.name, CheckStatus.FAIL, {**details, "reason": reason})
