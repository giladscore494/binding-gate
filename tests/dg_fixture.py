"""A tiny synthetic stand-in for the Deceptive Grounding benchmark checkout.

The files have exactly the keys and nesting of the real
``data/constructed_documents/<triple_id>.json`` files, but every drug, disease
and passage is invented here. No benchmark text is copied (the only benchmark
text in this repository is the prompt wording in
``binding_gate.datasets.deceptive_grounding``, attributed in NOTICE).
"""

from __future__ import annotations

import json
from pathlib import Path

from binding_gate.contracts import Split
from binding_gate.datasets.deceptive_grounding import (
    AGENT_SYSTEM_PROMPT,
    CX_LEVELS,
    CY_LEVELS,
    DATASET_NAME,
    data_manifest_sha256,
    pair_group_id,
)
from binding_gate.split import assign_split

FAKE_COMMIT = "c" * 40


def triple(index: int, *, degenerate: bool = False, rival: int = 0) -> dict:
    """Synthetic triple number ``index``. ``rival`` > 0 gives another competitor Y
    for the same (X, C) pair, as in the real corpus."""
    suffix = f"{index}" if rival == 0 else f"{index}r{rival}"
    x, y, c = f"xanadrug{index}", f"yoladrug{suffix}", f"zeta syndrome {index}"
    y_synthetic = f"yolad{suffix}ovate"
    triple_id = f"xanadrug{index}_zeta_syndrome_{index}_yoladrug{suffix}"
    cx = {
        "absent": None,
        "partial": (
            f"{x.capitalize()} is a made-up agent that blocks the invented receptor R{index} and is "
            f"discussed for {c} only on mechanistic grounds without outcome data in this brief."
        ),
        "complete": (
            f"{x.capitalize()} is a made-up agent that blocks the invented receptor R{index}. In {c} "
            f"a fictional series of {10 + index} patients reported partial response in {3 + index} cases."
        ),
    }
    cy = {
        "null_control": "..." if degenerate else (
            f"{y.capitalize()} was explored for an unrelated invented condition named omega fever, "
            f"where a fictional registry of {40 + index} people described slower progression."
        ),
        "class_proximate": (
            f"Quorodrug{index}, another invented blocker of the same family, was tried for an unrelated "
            f"invented condition named sigma rash with a fictional response in {20 + index} people."
        ),
        "context_adjacent": (
            f"{y.capitalize()} acts through the invented pathway P{index} and has its own fictional "
            f"rationale in {c}, drawn from laboratory models rather than patient outcomes."
        ),
        "prior_completing": (
            f"{y.capitalize()} was given in {c} in a fictional series of {10 + index} patients and "
            f"partial response occurred in {3 + index} cases according to this invented report."
        ),
        "synthetic_y": (
            f"{y_synthetic.capitalize()} was given in {c} in a fictional series of {10 + index} patients "
            f"and partial response occurred in {3 + index} cases according to this invented report."
        ),
    }
    return {
        "triple_id": triple_id,
        "X": x,
        "Y": y,
        "Y_synthetic": y_synthetic,
        "C": c,
        "subdomain": "rare_disease",
        "mechanism_class": "monoclonal_antibody",
        "prior_strength_class": "strong_positive_prior",
        "triple_risk_score": 0.5 + index / 100,
        "completing_information_targets": [f"fictional series of {10 + index} patients", "partial response"],
        "validation_anchor": False,
        "cx": cx,
        "cy": cy,
        "conditions": {
            f"{cx_level}__{cy_level}": {
                "cx": cx[cx_level],
                "cy": cy[cy_level],
                "cx_level": cx_level,
                "cy_level": cy_level,
            }
            for cx_level in CX_LEVELS
            for cy_level in CY_LEVELS
        },
        "generated": "2026-01-01T00:00:00+00:00",
    }


def pick_triples(n_dev: int, n_test: int) -> list[dict]:
    """Synthetic triples (one per (X, C) pair) of which exactly ``n_dev`` fall in dev and ``n_test`` in test."""
    chosen: list[dict] = []
    have = {Split.DEV: 0, Split.TEST: 0}
    want = {Split.DEV: n_dev, Split.TEST: n_test}
    index = 0
    while have != want:
        candidate = triple(index)
        split = assign_split(DATASET_NAME, pair_group_id(candidate["X"], candidate["C"]))
        if have[split] < want[split]:
            chosen.append(candidate)
            have[split] += 1
        index += 1
    return sorted(chosen, key=lambda t: t["triple_id"])


def write_checkout(
    root: Path,
    *,
    n_dev: int = 3,
    n_test: int = 2,
    with_git: bool = True,
    with_config_py: bool = True,
    triples: list[dict] | None = None,
) -> dict[str, str]:
    """Create the checkout under ``root``; return the dataset options that match it."""
    docs = root / "data" / "constructed_documents"
    docs.mkdir(parents=True)
    triples = pick_triples(n_dev, n_test) if triples is None else triples
    for entry in triples:
        (docs / f"{entry['triple_id']}.json").write_text(
            json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    index = {
        "generated": "2026-01-01T00:00:00+00:00",
        "total_triples": len(triples),
        "total_conditions": len(triples) * 15,
        "cx_levels": list(CX_LEVELS),
        "cy_levels": list(CY_LEVELS),
        "triples": [{"triple_id": t["triple_id"], "X": t["X"], "Y": t["Y"], "C": t["C"]} for t in triples],
    }
    (docs / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    if with_git:
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text(FAKE_COMMIT + "\n", encoding="utf-8")
    if with_config_py:
        (root / "config.py").write_text(
            f"import os\nAGENT_SYSTEM_PROMPT = {AGENT_SYSTEM_PROMPT!r}\nraise SystemExit('never executed')\n",
            encoding="utf-8",
        )
    return {
        "root": root.as_posix(),
        "commit": FAKE_COMMIT,
        "data_sha256": data_manifest_sha256(docs),
    }
