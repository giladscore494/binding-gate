"""Pre-registered stratified subsamples of a split (PR 13; PREREGISTRATION.md section 9).

The USD 50 budget of the study (``configs/budget.toml``) runs some arms and
models on a fixed subsample of items instead of on every item: the mid-tier
capability check, the LLM judges and the nondeterminism check. The subsample is
chosen by a rule fixed before any model output exists, from item ids alone:

```
SUBSAMPLE_SEED = 20261122
key(item)      = SHA-256("<seed>\\n<item id>") as a hex string
subsample      = in every stratum, the per_stratum items with the smallest key
```

The stratum is the item's ``stratum`` (layer 1: the condition
``"<cx_level>/<cy_level>"``, so every one of the 15 conditions is equally
represented); items without a stratum form one stratum. A stratum with fewer
items is taken whole. The items keep their input order. The rule reads no gold
label and no model output, so the same definition gives the same items on any
machine, and subsamples drawn with the same seed are nested: the items kept with
``per_stratum = 5`` are among those kept with ``per_stratum = 20``.

Applied by the runner after the split has been selected (dev:
``split.select_dev``; test: ``testlock.load_test_split``, so the test lock and
its log apply unchanged). Selected in a run config with::

    [sample]
    per_stratum = 20
    seed = 20261122

PR 22 (PREREGISTRATION.md section 18, amendment 1): an optional
``exclude_strata`` list removes whole strata BEFORE the per-stratum selection,
for example the three ``<cx_level>/null_control`` conditions of layer 1::

    [sample]
    per_stratum = 12
    seed = 20261122
    exclude_strata = ["absent/null_control", "partial/null_control", "complete/null_control"]

Ranking is per stratum, so the items kept in the remaining strata are exactly
those kept without the exclusion (the nesting above still holds). A table
without the key is unchanged, and so is the config hash of every config that
does not use it.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from binding_gate.contracts import Item

SUBSAMPLE_SEED = 20261122
"""The pre-registered seed of every subsample of the study (fixed 2026-11-22, before any model output)."""

NO_STRATUM = "(none)"


def sample_key(seed: int, item_id: str) -> str:
    """Rank key of one item: SHA-256 of ``"<seed>\\n<item id>"`` (hex)."""
    return hashlib.sha256(f"{seed}\n{item_id}".encode("utf-8")).hexdigest()


def stratified_sample(
    items: Sequence[Item], *, per_stratum: int, seed: int, exclude_strata: Sequence[str] = ()
) -> list[Item]:
    """The ``per_stratum`` items with the smallest :func:`sample_key` in every stratum not in
    ``exclude_strata``, in input order."""
    if isinstance(per_stratum, bool) or not isinstance(per_stratum, int) or per_stratum < 1:
        raise ValueError("per_stratum must be an integer >= 1")
    excluded = set(exclude_strata)
    by_stratum: dict[str, list[Item]] = {}
    for item in items:
        stratum = item.stratum or NO_STRATUM
        if stratum in excluded:
            continue
        by_stratum.setdefault(stratum, []).append(item)
    kept: set[str] = set()
    for members in by_stratum.values():
        ranked = sorted(members, key=lambda item: (sample_key(seed, item.id), item.id))
        kept.update(item.id for item in ranked[:per_stratum])
    return [item for item in items if item.id in kept]


def sample_items(items: Sequence[Item], sample: Mapping[str, Any]) -> list[Item]:
    """Apply a parsed ``[sample]`` table (``per_stratum``, ``seed``, optional ``exclude_strata``)."""
    return stratified_sample(
        items,
        per_stratum=sample["per_stratum"],
        seed=sample["seed"],
        exclude_strata=tuple(sample.get("exclude_strata", ())),
    )
