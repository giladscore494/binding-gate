"""Gate ``binding``: runs the configured binding checks and maps their results to a verdict (E).

The gate is a pure function of (constraints, answered candidate, documents):
no model call, no network, no randomness, no clock, no gold metadata. Same
inputs give byte-identical verdicts (``Verdict.to_json()``) on any machine.

Which checks run
----------------
Exactly the check names listed in ``checks``, in that order, each built through
the registry (``registry.create("check", name, ...)``). Removing a name from the
list disables that check; this is how ablations are expressed in the run config
(ARCHITECTURE.md section 3, "Ablations are config, not code").

Shared parameters (one value per run config, passed to every check that uses it):

==================  =========  ==================================================
parameter           PROPOSED   used by
==================  =========  ==================================================
window_tokens       40         entity_in_window, qualifiers_in_window,
                               no_closer_competitor, premise_present (window scope)
min_quote_tokens    5          quote_verbatim
lead_max_tokens     4          no_closer_competitor
lead_min_repeats    2          no_closer_competitor
premise_scope       document   premise_present
competitor_tie      fail       no_closer_competitor
==================  =========  ==================================================

All PROPOSED values require owner approval before the pre-registration is
frozen (PREREGISTRATION.md section 5.1).

Decision table (function :func:`decide`)
----------------------------------------
Let F be the set of checks whose status is ``fail``. ``not_applicable`` never
blocks.

====  ==========================================================  ===========
rule  condition                                                   verdict
====  ==========================================================  ===========
1     F contains ``quote_verbatim`` or ``premise_present``, or a   ABSTAIN
      check name not listed in :data:`RESEARCH_CHECKS`
2     otherwise, F is not empty (so every failed check is one of   RESEARCH
      ``entity_in_window``, ``qualifiers_in_window``,
      ``no_closer_competitor``)
3     F is empty                                                  ACCEPT
====  ==========================================================  ===========

Why: a quote that is not in its cited source, or evidence that does not contain
what the question presupposes, cannot be repaired by looking for another
source for the same answer, so the gate abstains. Evidence that exists but is
about another entity or other conditions is the failure a targeted re-search
can fix (layer 3, PR 8), so the gate asks for one. In layers 1 and 2 there is no
search and a RESEARCH verdict counts as an abstention (PREREGISTRATION.md
section 5): the arm then reports an abstention as its final answer.

Every failed check contributes one reason ``"<check>: <reason code>"``, in the
order of ``checks``. A RESEARCH verdict carries a ``research_request``: the
canonical JSON (sorted keys) of ``{"entity", "aliases", "attribute",
"qualifiers", "failed_checks", "query"}``, where ``query`` is the entity name,
the attribute and the qualifier texts joined by single spaces.
"""

from __future__ import annotations

from typing import Any, Sequence

from binding_gate import registry
from binding_gate.canonical import canonical_json
from binding_gate.contracts import (
    AnswerCandidate,
    CheckResult,
    Decision,
    Document,
    QuestionConstraints,
    Verdict,
)

NAME = "binding"

ALL_CHECKS: tuple[str, ...] = (
    "quote_verbatim",
    "entity_in_window",
    "qualifiers_in_window",
    "no_closer_competitor",
    "premise_present",
)
"""The reserved check names (ARCHITECTURE.md section 3), in their canonical order."""

ABSTAIN_CHECKS: tuple[str, ...] = ("quote_verbatim", "premise_present")
RESEARCH_CHECKS: tuple[str, ...] = ("entity_in_window", "qualifiers_in_window", "no_closer_competitor")

DEFAULT_PARAMETERS: dict[str, Any] = {
    "window_tokens": 40,
    "min_quote_tokens": 5,
    "lead_max_tokens": 4,
    "lead_min_repeats": 2,
    "premise_scope": "document",
    "competitor_tie": "fail",
}
"""PROPOSED values (PREREGISTRATION.md section 5.1); the run config states them explicitly."""

CHECK_PARAMETERS: dict[str, tuple[str, ...]] = {
    "quote_verbatim": ("min_quote_tokens",),
    "entity_in_window": ("window_tokens",),
    "qualifiers_in_window": ("window_tokens",),
    "no_closer_competitor": ("window_tokens", "lead_max_tokens", "lead_min_repeats", "competitor_tie"),
    "premise_present": ("premise_scope", "window_tokens"),
}
"""Which shared parameters each check receives. A check not listed here gets none."""


def research_request(constraints: QuestionConstraints, failed: Sequence[str]) -> str:
    """The deterministic research request of a RESEARCH verdict (see the module docstring)."""
    qualifiers = [q.text for q in constraints.qualifiers]
    return canonical_json(
        {
            "entity": constraints.entity.name,
            "aliases": list(constraints.entity.aliases),
            "attribute": constraints.attribute,
            "qualifiers": qualifiers,
            "failed_checks": list(failed),
            "query": " ".join([constraints.entity.name, constraints.attribute, *qualifiers]),
        }
    )


def decide(results: Sequence[CheckResult], constraints: QuestionConstraints) -> Verdict:
    """The decision table of the module docstring: check results -> verdict."""
    results = tuple(results)
    failed = [r for r in results if r.failed]
    reasons = tuple(f"{r.check}: {r.details.get('reason', 'failed')}" for r in failed)
    if not failed:
        passed = sum(r.passed for r in results)
        return Verdict(
            Decision.ACCEPT,
            reasons=(f"no check failed ({passed} passed, {len(results) - passed} not applicable)",),
            check_results=results,
        )
    if any(r.check not in RESEARCH_CHECKS for r in failed):
        return Verdict(Decision.ABSTAIN, reasons=reasons, check_results=results)
    return Verdict(
        Decision.RESEARCH,
        reasons=reasons,
        check_results=results,
        research_request=research_request(constraints, [r.check for r in failed]),
    )


def validate_check_names(checks: Any) -> tuple[str, ...]:
    if isinstance(checks, str) or not isinstance(checks, (list, tuple)) or not checks:
        raise ValueError("checks must be a non-empty list of check names")
    if not all(isinstance(c, str) and c for c in checks):
        raise ValueError("checks must contain only non-empty strings")
    duplicates = sorted({c for c in checks if list(checks).count(c) > 1})
    if duplicates:
        raise ValueError(f"checks lists a name more than once: {duplicates}")
    unknown = sorted(set(checks) - set(registry.available("check")))
    if unknown:
        raise ValueError(f"unknown check name(s) {unknown}; available: {registry.available('check')}")
    return tuple(checks)


@registry.register("gate", NAME)
class BindingGate:
    """Options: ``checks`` (list of check names, required) and the shared parameters above."""

    name = NAME

    def __init__(self, *, checks: Sequence[str], **parameters: Any):
        self.checks = validate_check_names(checks)
        unknown = sorted(set(parameters) - set(DEFAULT_PARAMETERS))
        if unknown:
            raise ValueError(
                f"unknown gate parameter(s) {unknown}; allowed: {sorted(DEFAULT_PARAMETERS)}"
            )
        self.parameters = {**DEFAULT_PARAMETERS, **parameters}
        self._checks = [
            registry.create(
                "check",
                name,
                **{key: self.parameters[key] for key in CHECK_PARAMETERS.get(name, ())},
            )
            for name in self.checks
        ]
        for name, check in zip(self.checks, self._checks):
            if getattr(check, "name", None) != name:
                raise ValueError(f"check registered as {name!r} reports the name {getattr(check, 'name', None)!r}")

    def run_checks(
        self, constraints: QuestionConstraints, answer: AnswerCandidate, documents: Sequence[Document]
    ) -> tuple[CheckResult, ...]:
        results = []
        for name, check in zip(self.checks, self._checks):
            result = check.check(constraints, answer, documents)
            if not isinstance(result, CheckResult) or result.check != name:
                raise ValueError(f"check {name!r} returned a result for {getattr(result, 'check', None)!r}")
            results.append(result)
        return tuple(results)

    def evaluate(
        self, constraints: QuestionConstraints, answer: AnswerCandidate, documents: Sequence[Document]
    ) -> Verdict:
        return decide(self.run_checks(constraints, answer, documents), constraints)
