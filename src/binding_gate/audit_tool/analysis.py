"""Analysis of the human audit (PREREGISTRATION.md section 13).

Definitions
-----------
* The gate *says the evidence is bound* when its verdict is ``accept``; it says
  it is not bound when it blocked (``abstain`` or ``research``).
* The auditor's ``yes`` means bound, ``no`` not bound. ``cannot_tell`` is not a
  decision: it is left out of the agreement denominators, counted, and in a
  sensitivity variant counted as a disagreement.
* **Agreement** in a half = labels that agree with the gate / decided labels of
  that half. In the blocked half this is the share of blocks the auditor
  confirms; in the accepted half the share of accepts the auditor confirms.
* A **gate error** is a decided label that disagrees with the gate: a false
  accept (accepted, auditor ``no``) or a false block (blocked, auditor ``yes``).
* **Decomposer share** = gate errors whose decomposition category (entered by
  the auditor after the reveal, comparing the constraints recorded in the trace
  with the question) is a decomposer fault / gate errors with a category other
  than ``not_assessed``.

Intervals: Clopper-Pearson, and why
-----------------------------------
Every proportion gets a two-sided **Clopper-Pearson exact binomial** interval at
``AUDIT_CI_LEVEL``. Reasons: the counts are small (about 50 per half) and the
proportions may be at or near 0 or 1, where a bootstrap percentile interval
collapses to a single point (for example ``[1, 1]`` when all 50 agree) and the
normal approximation leaves ``[0, 1]``; Clopper-Pearson keeps at least the
nominal coverage for every true proportion, needs no resampling (so no seed and
no Monte Carlo error), and is a pure function of two integers. Within one half
the cases of one stratum are a simple random sample (a fixed-seed hash order is
used as the random order), which is what the binomial model assumes; cases are
treated as independent although several can share a question.

The **population-weighted agreement** combines the strata with their population
shares ``W_s = N_s / N`` (``N_s`` decisions in stratum ``s``, ``N`` all
decisions): estimate ``sum W_s p_s``. Its interval is conservative: each stratum
gets a Clopper-Pearson interval at level ``1 - alpha / K`` (``K`` strata with
``N_s > 0``; Bonferroni) and the bounds are combined as ``sum W_s L_s`` and
``sum W_s U_s``, which covers the weighted proportion with probability at least
``1 - alpha``. A stratum without decided labels contributes ``[0, 1]`` and makes
the point estimate undefined.

Single auditor
--------------
With one label file there is no inter-rater check, and the analysis says so.
With two or more, the first file is the primary auditor (all figures above) and
Cohen's kappa over the three labels is reported for each further auditor
against the first, on the cases both labelled.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from binding_gate.audit_tool import prereg
from binding_gate.audit_tool.labels import load_labels
from binding_gate.audit_tool.sampling import ACCEPTED, BLOCKED, AuditError, load_sample
from binding_gate.audit_tool.views import constraints_summary

ANALYSIS_FORMAT = "binding-gate-audit-analysis"
ANALYSIS_VERSION = 1
SINGLE_AUDITOR_LIMITATION = (
    "One auditor labelled the sample, so there is no inter-rater check: the auditor's own errors and "
    "biases are not measured and the agreement figures inherit them."
)


# --------------------------------------------------------------------------- #
# Clopper-Pearson
# --------------------------------------------------------------------------- #


def _binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p)."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return min(1.0, sum(math.comb(n, i) * p**i * (1.0 - p) ** (n - i) for i in range(k + 1)))


def _bisect(f, target: float, increasing: bool) -> float:
    low, high = 0.0, 1.0
    for _ in range(200):
        mid = (low + high) / 2
        value = f(mid)
        if (value < target) == increasing:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def clopper_pearson(k: int, n: int, level: Fraction | float = prereg.AUDIT_CI_LEVEL) -> tuple[float, float] | None:
    """Two-sided exact binomial interval for ``k`` successes in ``n`` trials (``None`` if ``n == 0``)."""
    if n == 0:
        return None
    if not 0 <= k <= n:
        raise ValueError(f"need 0 <= k <= n, got k={k}, n={n}")
    alpha = 1.0 - float(level)
    lower = 0.0 if k == 0 else _bisect(lambda p: 1.0 - _binom_cdf(k - 1, n, p), alpha / 2, increasing=True)
    upper = 1.0 if k == n else _bisect(lambda p: _binom_cdf(k, n, p), alpha / 2, increasing=False)
    return lower, upper


def _proportion(k: int, n: int, level: Fraction | float = prereg.AUDIT_CI_LEVEL) -> dict[str, Any]:
    ci = clopper_pearson(k, n, level)
    return {
        "k": k,
        "n": n,
        "value": None if n == 0 else k / n,
        "ci": None if ci is None else [round(ci[0], 6), round(ci[1], 6)],
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _agrees(half: str, label: str) -> bool | None:
    if label == "cannot_tell":
        return None
    return (label == "yes") == (half == ACCEPTED)


def _describe(case: Mapping[str, Any], entry: Mapping[str, Any]) -> str:
    decision = case["verdict"]["decision"].upper()
    reasons = "; ".join(case["verdict"].get("reasons", [])) or "no reasons recorded"
    if case["half"] == ACCEPTED:
        text = (
            f"False accept: the gate returned {decision}, but the auditor judged that the quoted evidence is "
            "not about the question's entity and qualifiers."
        )
    else:
        text = (
            f"False block: the gate returned {decision} ({reasons}), but the auditor judged that the quoted "
            "evidence is about the question's entity and qualifiers."
        )
    if entry.get("note"):
        text += f" Auditor's note: {entry['note']}"
    return text


def _kappa(pairs: Sequence[tuple[str, str]]) -> float | None:
    if not pairs:
        return None
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    left, right = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(left[c] * right[c] for c in prereg.LABELS) / (n * n)
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)


def _weighted(sample: Mapping[str, Any], by_stratum: Mapping[tuple[str, str], tuple[int, int]], level: Fraction) -> dict[str, Any]:
    population = sample["population"]
    total = sum(n for half in population.values() for n in half.values())
    strata = [(h, s, n) for h, half in population.items() for s, n in half.items() if n > 0]
    k_strata = len(strata)
    alpha = 1 - Fraction(level)
    per_level = 1 - alpha / k_strata if k_strata else level
    estimate: Fraction | None = Fraction(0)
    low = high = 0.0
    rows = []
    for half, stratum, size in strata:
        agree, decided = by_stratum.get((half, stratum), (0, 0))
        share = Fraction(size, total)
        ci = clopper_pearson(agree, decided, per_level) or (0.0, 1.0)
        low += float(share) * ci[0]
        high += float(share) * ci[1]
        if decided == 0:
            estimate = None
        elif estimate is not None:
            estimate += share * Fraction(agree, decided)
        rows.append(
            {
                "half": half,
                "stratum": stratum,
                "population": size,
                "population_share": float(share),
                "agree": agree,
                "decided": decided,
                "ci_at_bonferroni_level": [round(ci[0], 6), round(ci[1], 6)],
            }
        )
    return {
        "value": None if estimate is None else float(estimate),
        "ci": [round(low, 6), round(high, 6)] if k_strata else None,
        "method": (
            "population-share weighted mean of stratum agreements; conservative interval from per-stratum "
            f"Clopper-Pearson intervals at level 1 - alpha/{k_strata} (Bonferroni), combined with the same weights"
        ),
        "strata": rows,
    }


# --------------------------------------------------------------------------- #
# The analysis
# --------------------------------------------------------------------------- #


def analyse_audit(
    sample: Mapping[str, Any],
    sample_sha256: str,
    label_sets: Sequence[Mapping[str, Any]],
    *,
    label_hashes: Sequence[str] | None = None,
    level: Fraction = prereg.AUDIT_CI_LEVEL,
) -> dict[str, Any]:
    """Agreement, disagreements and decomposer share (see the module docstring)."""
    if not label_sets:
        raise AuditError("at least one label file is required")
    cases = {case["case_id"]: case for case in sample["cases"]}
    primary = {entry["case_id"]: entry for entry in label_sets[0]["labels"]}

    halves: dict[str, Counter] = {BLOCKED: Counter(), ACCEPTED: Counter()}
    by_stratum: dict[tuple[str, str], list[int]] = {}
    by_model: dict[str, Counter] = {}
    disagreements: list[dict[str, Any]] = []
    decomposition = Counter()
    for case in sorted(cases.values(), key=lambda c: c["display_number"]):
        half = case["half"]
        counts = halves[half]
        counts["cases"] += 1
        model_counts = by_model.setdefault(case["model"], Counter())
        model_counts["cases"] += 1
        entry = primary.get(case["case_id"])
        if entry is None:
            continue
        counts["labelled"] += 1
        counts[entry["label"]] += 1
        model_counts["labelled"] += 1
        agrees = _agrees(half, entry["label"])
        stratum_counts = by_stratum.setdefault((half, case["stratum"]), [0, 0])
        if agrees is None:
            continue
        counts["decided"] += 1
        model_counts["decided"] += 1
        stratum_counts[1] += 1
        if agrees:
            counts["agree"] += 1
            model_counts["agree"] += 1
            stratum_counts[0] += 1
            continue
        category = entry.get("decomposition", "not_assessed")
        decomposition[category] += 1
        disagreements.append(
            {
                "display_number": case["display_number"],
                "case_id": case["case_id"],
                "kind": "false accept" if half == ACCEPTED else "false block",
                "model": case["model"],
                "dataset": case["dataset"],
                "item_id": case["item_id"],
                "condition": case.get("condition"),
                "decision": case["verdict"]["decision"],
                "reasons": list(case["verdict"].get("reasons", [])),
                "auditor_label": entry["label"],
                "note": entry.get("note", ""),
                "decomposition": category,
                "constraints": constraints_summary(case.get("constraints")),
                "description": _describe(case, entry),
            }
        )

    per_half = {}
    sensitivity = {}
    for half, counts in halves.items():
        per_half[half] = {
            "cases": counts["cases"],
            "labelled": counts["labelled"],
            "yes": counts["yes"],
            "no": counts["no"],
            "cannot_tell": counts["cannot_tell"],
            "agreement": _proportion(counts["agree"], counts["decided"], level),
        }
        sensitivity[half] = _proportion(counts["agree"], counts["labelled"], level)
    agree_all = sum(c["agree"] for c in halves.values())
    decided_all = sum(c["decided"] for c in halves.values())
    labelled_all = sum(c["labelled"] for c in halves.values())

    errors = len(disagreements)
    assessed = errors - decomposition["not_assessed"]
    faults = sum(n for c, n in decomposition.items() if c in prereg.DECOMPOSER_FAULT_CATEGORIES)

    inter_rater = []
    for index, other in enumerate(label_sets[1:], start=1):
        theirs = {e["case_id"]: e["label"] for e in other["labels"]}
        pairs = [(primary[c]["label"], theirs[c]) for c in primary if c in theirs]
        inter_rater.append(
            {
                "auditor": other.get("auditor"),
                "against": label_sets[0].get("auditor"),
                "n_common": len(pairs),
                "raw_agreement": (sum(a == b for a, b in pairs) / len(pairs)) if pairs else None,
                "cohen_kappa": _kappa(pairs),
            }
        )

    hashes = list(label_hashes or [None] * len(label_sets))
    return {
        "format": ANALYSIS_FORMAT,
        "version": ANALYSIS_VERSION,
        "sample_sha256": sample_sha256,
        "split": sample["split"],
        "datasets": sample.get("datasets", []),
        "runs": sample["runs"],
        "parameters": {**prereg.as_dict(), "used_ci_level": str(Fraction(level))},
        "sample_as_preregistered": bool(sample.get("as_preregistered")),
        "sample_composition": {
            "population": sample["population"],
            "composition": sample["composition"],
            "shortfall": sample.get("shortfall", []),
            "judge_run": sample.get("judge_run", False),
        },
        "auditors": [
            {"auditor": labels.get("auditor"), "labels_sha256": digest, "n_labelled": len(labels["labels"])}
            for labels, digest in zip(label_sets, hashes)
        ],
        "n_cases": len(cases),
        "n_labelled": labelled_all,
        "complete": labelled_all == len(cases),
        "unlabelled": [c for c in sorted(cases, key=lambda c: cases[c]["display_number"]) if c not in primary],
        "per_half": per_half,
        "sample_agreement_unweighted": {
            **_proportion(agree_all, decided_all, level),
            "note": "share of decided labels that agree with the gate in the 50/50 sample; not a population figure",
        },
        "population_weighted_agreement": _weighted(
            sample, {k: (v[0], v[1]) for k, v in by_stratum.items()}, level
        ),
        "sensitivity_cannot_tell_as_disagreement": {
            "per_half": sensitivity,
            "overall": _proportion(agree_all, labelled_all, level),
        },
        "disagreements": disagreements,
        "decomposer": {
            "gate_errors": errors,
            "assessed": assessed,
            "decomposer_fault": faults,
            "share": _proportion(faults, assessed, level),
            "by_category": dict(sorted(decomposition.items())),
        },
        "by_model": {m: dict(c) for m, c in sorted(by_model.items())},
        "single_auditor": len(label_sets) == 1,
        "limitations": [SINGLE_AUDITOR_LIMITATION] if len(label_sets) == 1 else [],
        "inter_rater": inter_rater,
        "ci_method": (
            "Clopper-Pearson exact binomial: small counts and proportions near 0 or 1, where bootstrap "
            "percentile intervals collapse to a point; guaranteed at least nominal coverage; no resampling."
        ),
    }


def analyse_files(sample_path: str | Path, label_paths: Sequence[str | Path]) -> dict[str, Any]:
    sample, sample_sha = load_sample(sample_path)
    label_sets = [load_labels(path, sample, sample_sha) for path in label_paths]
    hashes = [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in label_paths]
    return analyse_audit(sample, sample_sha, label_sets, label_hashes=hashes)
