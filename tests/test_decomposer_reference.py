"""The REFERENCE-ONLY Deceptive Grounding decomposition (uses gold; never part of the gate)."""

from __future__ import annotations

import pytest

import dg_fixture
from binding_gate import registry
from binding_gate.contracts import Item, QualifierKind
from binding_gate.decomposers import deceptive_grounding_reference as ref


@pytest.fixture
def items(tmp_path):
    options = dg_fixture.write_checkout(tmp_path / "dg", n_dev=2, n_test=1)
    return list(registry.create("dataset", "deceptive_grounding", **options).iter_items())


def test_reference_follows_the_pre_registered_procedure(items):
    reference = ref.DeceptiveGroundingReference()
    item = items[0]
    x, c = item.gold["X"], item.gold["C"]
    constraints = reference.constraints(item)
    assert constraints.question == item.question
    assert (constraints.entity.name, constraints.entity.aliases, constraints.entity.kind) == (x, (), "drug")
    assert constraints.attribute == ref.REFERENCE_ATTRIBUTE
    assert [(q.kind, q.text, q.aliases) for q in constraints.qualifiers] == [(QualifierKind.OTHER, c, ())]
    (premise,) = constraints.presuppositions
    assert premise.key_terms == (x, c) and x in premise.text and c in premise.text
    assert reference.forbidden_entity_names(item) == (item.gold["Y"], item.gold["Y_synthetic"])
    assert x not in reference.forbidden_entity_names(item)


def test_all_conditions_of_a_question_share_one_reference(items):
    reference = ref.DeceptiveGroundingReference()
    by_question: dict[str, set] = {}
    for item in items:
        by_question.setdefault(item.question, set()).add(reference.constraints(item))
    assert all(len(values) == 1 for values in by_question.values())
    assert len(by_question) == 3


def test_reference_refuses_anything_but_a_template_item_with_gold(items):
    reference = ref.DeceptiveGroundingReference()
    item = items[0]
    with pytest.raises(ref.ReferenceError, match="never be given an arm input"):
        reference.constraints(item.arm_input())  # type: ignore[arg-type]
    other = Item.from_dict({**item.to_dict(), "dataset": "grab_rag"})
    with pytest.raises(ref.ReferenceError, match="not 'deceptive_grounding'"):
        reference.constraints(other)
    edited = Item.from_dict({**item.to_dict(), "question": item.question.replace("treat", "prevent")})
    with pytest.raises(ref.ReferenceError, match="not the benchmark template"):
        reference.constraints(edited)
    no_gold = Item.from_dict({**item.to_dict(), "gold": {}})
    with pytest.raises(ref.ReferenceError, match="gold"):
        reference.forbidden_entity_names(no_gold)


def test_reference_cannot_be_configured_into_an_arm():
    assert ref.DeceptiveGroundingReference.reference_only is True
    assert not hasattr(ref.DeceptiveGroundingReference, "decompose")
    assert "deceptive_grounding_reference" not in registry.available("decomposer")
    assert "deceptive_grounding" not in registry.available("decomposer")
    assert ref.REFERENCES == {"deceptive_grounding": ref.DeceptiveGroundingReference}
