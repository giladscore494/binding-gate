"""Deterministic, leakage-free dev/test split.

The split of an item depends only on ``(SPLIT_SALT, dataset, group_id)``, so

* it is identical on every machine and in every run, with no stored split file;
* all items sharing a ``group_id`` (for example every condition built from the
  same source triple) land in the same split;
* adding or removing other items never moves an item.

``SPLIT_SALT`` and ``TEST_BUCKETS`` are pre-registered (PREREGISTRATION.md,
section 4) and ``tests/test_preregistration.py`` checks that the document and
this module agree. Do not change them after any data has been looked at.

This module deliberately offers no function that returns test items; the only
way to obtain them is ``binding_gate.testlock.load_test_split``.
"""

from __future__ import annotations

import hashlib
from typing import Iterable

from binding_gate.contracts import Item, Split

SPLIT_SALT = "binding-gate/split/v1"
BUCKETS = 10_000
TEST_BUCKETS = 6_000
"""Groups whose bucket is below this go to test: 60% test / 40% dev in expectation."""


def split_bucket(dataset: str, group_id: str, *, salt: str = SPLIT_SALT) -> int:
    """Bucket in ``[0, BUCKETS)`` for a group: SHA-256 of ``salt\\ndataset\\ngroup_id``."""
    if not dataset or not group_id:
        raise ValueError("dataset and group_id must be non-empty")
    digest = hashlib.sha256(f"{salt}\n{dataset}\n{group_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % BUCKETS


def assign_split(dataset: str, group_id: str) -> Split:
    """The pre-registered split of a group."""
    return Split.TEST if split_bucket(dataset, group_id) < TEST_BUCKETS else Split.DEV


def item_split(item: Item) -> Split:
    return assign_split(item.dataset, item.group_id)


def select_dev(items: Iterable[Item]) -> list[Item]:
    """The dev items, in input order. Test items are dropped without being returned."""
    return [item for item in items if item_split(item) is Split.DEV]
