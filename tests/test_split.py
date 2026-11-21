"""Dev/test split: deterministic, group-preserving, independent of other items."""

from __future__ import annotations

import random

import pytest

import builders as b
from binding_gate import split
from binding_gate.contracts import Split
from binding_gate.split import assign_split, item_split, select_dev, split_bucket

# Frozen expectations computed once from the pre-registered salt (the first one
# was also reproduced with `printf ... | sha256sum`). If one of these changes,
# the split of every dataset has changed.
KNOWN_BUCKETS = {
    ("deceptive_grounding", "triple-0001"): 283,
    ("deceptive_grounding", "triple-0002"): 1385,
    ("sealqa", "row-17"): 5166,
    ("toy", "héllo/ünicode"): 7971,
}


def _conditions(n_groups: int, per_group: int = 15):
    return [
        b.item(item_id=f"g{g}-c{c}", group_id=f"g{g}", dataset="toy")
        for g in range(n_groups)
        for c in range(per_group)
    ]


def test_preregistered_constants():
    assert split.SPLIT_SALT == "binding-gate/split/v1"
    assert (split.TEST_BUCKETS, split.BUCKETS) == (6_000, 10_000)


@pytest.mark.parametrize("key", KNOWN_BUCKETS, ids=lambda k: "/".join(k))
def test_assignment_is_pinned_to_known_values(key):
    assert split_bucket(*key) == KNOWN_BUCKETS[key]


def test_assignment_is_stable_across_calls_and_order():
    items = _conditions(50)
    first = {i.id: item_split(i) for i in items}
    shuffled = items[:]
    random.Random(0).shuffle(shuffled)
    assert {i.id: item_split(i) for i in shuffled} == first
    assert [i.id for i in select_dev(items)] == [i.id for i in items if first[i.id] is Split.DEV]


def test_all_items_of_a_group_share_a_split():
    items = _conditions(200)
    by_group: dict[str, set[Split]] = {}
    for i in items:
        by_group.setdefault(i.group_id, set()).add(item_split(i))
    assert all(len(splits) == 1 for splits in by_group.values())
    dev_groups = {i.group_id for i in select_dev(items)}
    test_groups = {i.group_id for i in items} - dev_groups
    assert dev_groups and test_groups and not dev_groups & test_groups
    assert len(select_dev(items)) == 15 * len(dev_groups)


def test_assignment_ignores_item_id_and_other_items():
    alone = b.item(item_id="x", group_id="g7", dataset="toy")
    renamed = b.item(item_id="completely-different", group_id="g7", dataset="toy")
    assert item_split(alone) is item_split(renamed) is assign_split("toy", "g7")


def test_dataset_namespaces_the_group_id():
    differing = [g for g in range(200) if split_bucket("a", f"g{g}") != split_bucket("b", f"g{g}")]
    assert len(differing) > 150


def test_salt_changes_the_split():
    moved = [g for g in range(200) if split_bucket("toy", f"g{g}") != split_bucket("toy", f"g{g}", salt="other")]
    assert len(moved) > 150


def test_ratio_is_close_to_the_preregistered_one():
    n = 20_000
    test_share = sum(assign_split("toy", f"group-{g}") is Split.TEST for g in range(n)) / n
    assert abs(test_share - split.TEST_BUCKETS / split.BUCKETS) < 0.015


def test_empty_keys_are_rejected():
    with pytest.raises(ValueError):
        split_bucket("", "g")
    with pytest.raises(ValueError):
        split_bucket("toy", "")


def test_split_module_exposes_no_test_selector():
    assert not [name for name in dir(split) if "test" in name.lower() and callable(getattr(split, name))]
