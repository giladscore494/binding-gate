"""The pre-registered analysis parameters, in one place.

Every number here mirrors PREREGISTRATION.md (section 6.4 item 6, section 8 and
section 11). ``tests/test_stats_prereg.py`` fails if the document and this module
disagree, in the same way ``tests/test_preregistration.py`` guards the split
salt. All values are **PROPOSED and require owner approval before freeze**; once
the pre-registration is frozen they change only through its amendment log.

Thresholds are exact fractions so that a decision at a boundary value (for
example a relative reduction of exactly 50%) never depends on floating-point
rounding.
"""

from __future__ import annotations

from fractions import Fraction

# --------------------------------------------------------------------------- #
# Section 8: statistics
# --------------------------------------------------------------------------- #

CI_LEVEL = Fraction(95, 100)
"""Two-sided level of every bootstrap percentile interval (95%)."""

BOOTSTRAP_RESAMPLES = 10_000
"""Number of cluster-bootstrap resamples."""

BOOTSTRAP_SEED = 20261122
"""Seed of ``random.Random`` for the cluster bootstrap (fixed before any result)."""

REPEATS = 1
"""Pre-registered runs per (item, arm, model). A different number makes an analysis non-confirmatory.
PR 13 (USD 50 budget): one repeat; nondeterminism is measured separately on a dev subsample
(configs/dev_repeats.toml)."""

# --------------------------------------------------------------------------- #
# Section 6.4 item 6: validity guard
# --------------------------------------------------------------------------- #

VALIDITY_MAX_FAILED_RATE = Fraction(5, 100)
"""If (unparseable + error) / all records of an arm exceeds this, every comparison
involving that arm is reported but labelled not confirmatory."""

# --------------------------------------------------------------------------- #
# Section 11: success thresholds
# --------------------------------------------------------------------------- #

H1_MIN_RELATIVE_REDUCTION = Fraction(50, 100)
"""H1: 1 - M1(gated) / M1(no_gate) must be at least this (point estimate)."""

H2_MAX_CORRECT_BLOCKED = Fraction(10, 100)
"""H2: M2 of ``gated`` must be at most this (point estimate)."""

H4_MIN_NET_DIFFERENCE = Fraction(0)
"""H4: M3(gated_research) - M3(no_gate) must be at least this (point estimate)."""

# --------------------------------------------------------------------------- #
# Arms and layers named by the hypotheses (reserved names, ARCHITECTURE.md 3)
# --------------------------------------------------------------------------- #

REFERENCE_ARM = "no_gate"
GATED_ARM = "gated"
FAITHFULNESS_ARM = "faithfulness_checker"
RESEARCH_ARM = "gated_research"

LAYER_BY_DATASET = {"deceptive_grounding": 1, "sec_financials": 2, "sealqa": 3}
"""Layer of each dataset (PREREGISTRATION.md section 3). H1-H3 are evaluated on
layer 1, H1 and H2 also on layer 2 (SEC financial filings, PR 17; the same
thresholds, per model), H4 on layer 3. GRAB-RAG (layer 2 until PR 17) is in the
extended design only (``EXTENDED_DATASETS``) and belongs to no layer."""

HYPOTHESES_BY_LAYER = {1: ("H1", "H2", "H3"), 2: ("H1", "H2"), 3: ("H4",)}

EXTENDED_DATASETS = ("grab_rag",)
"""Datasets kept in the extended design (configs/extended/), not part of any layer since PR 17."""

CORRECT_OUTCOMES_DEFAULT = ("target_grounded",)
"""Outcome categories that count as a correct final answer. For layers 1 and 2
this is the pre-registered proxy "target-grounded" (section 6.3). For layer 3 the
correctness rule is fixed by PR 8; until then this default is a PLACEHOLDER and
any layer-3 number computed with it is labelled as such."""


def as_dict() -> dict[str, object]:
    """The parameters as JSON-friendly values (recorded in every analysis output)."""
    return {
        "ci_level": str(CI_LEVEL),
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "repeats": REPEATS,
        "validity_max_failed_rate": str(VALIDITY_MAX_FAILED_RATE),
        "h1_min_relative_reduction": str(H1_MIN_RELATIVE_REDUCTION),
        "h2_max_correct_blocked": str(H2_MAX_CORRECT_BLOCKED),
        "h4_min_net_difference": str(H4_MIN_NET_DIFFERENCE),
        "reference_arm": REFERENCE_ARM,
        "gated_arm": GATED_ARM,
        "faithfulness_arm": FAITHFULNESS_ARM,
        "research_arm": RESEARCH_ARM,
        "status": "PROPOSED - requires owner approval before freeze",
    }
