"""The pre-registered parameters of the human audit (PREREGISTRATION.md section 13).

``tests/test_audit_prereg.py`` fails if section 13 and this module disagree.
Every value is **PROPOSED and requires owner approval before freeze**; after the
freeze it changes only through the amendment log (section 18).
"""

from __future__ import annotations

from fractions import Fraction

AUDIT_SEED = 20261122
"""Seed of the deterministic sample (fixed on 2026-11-22, before any gate decision existed)."""

AUDIT_BLOCKED = 50
"""Gate decisions sampled from the blocked half (verdict ABSTAIN or RESEARCH)."""

AUDIT_ACCEPTED = 50
"""Gate decisions sampled from the accepted half (verdict ACCEPT)."""

AUDIT_DISAGREEMENT_SHARE = Fraction(1, 2)
"""When the secondary judge was run: at most this share of each half is drawn from
the units where the two scorers disagree (oversampling); the rest from the others."""

AUDIT_CI_LEVEL = Fraction(95, 100)
"""Two-sided level of every audit interval (Clopper-Pearson exact binomial)."""

AUDIT_ARMS = ("gated", "gated_research")
"""Arms whose first-attempt verdicts form the population (ablation arms are excluded)."""

AUDIT_ATTEMPT = 0
"""Which attempt's verdict is a "gate decision": the first one (index 0)."""

LABELS = ("yes", "no", "cannot_tell")
"""Auditor answers to: is the quoted evidence about the question's entity and qualifiers?"""

DECOMPOSITION_CATEGORIES = (
    "not_assessed",
    "constraints_correct",
    "wrong_entity",
    "wrong_or_missing_qualifier",
    "wrong_or_missing_presupposition",
    "other_decomposition_problem",
)
"""After the reveal the auditor compares the decomposer's constraints (from the trace)
with the question. Every category except ``not_assessed`` and ``constraints_correct``
traces a gate error to the decomposer."""

DECOMPOSER_FAULT_CATEGORIES = frozenset(DECOMPOSITION_CATEGORIES[2:])


def as_dict() -> dict[str, object]:
    """The parameters as JSON-friendly values (recorded in every sample and analysis)."""
    return {
        "seed": AUDIT_SEED,
        "n_blocked": AUDIT_BLOCKED,
        "n_accepted": AUDIT_ACCEPTED,
        "disagreement_share": str(AUDIT_DISAGREEMENT_SHARE),
        "ci_level": str(AUDIT_CI_LEVEL),
        "ci_method": "Clopper-Pearson exact binomial",
        "arms": list(AUDIT_ARMS),
        "attempt": AUDIT_ATTEMPT,
        "status": "PROPOSED - requires owner approval before freeze",
    }
