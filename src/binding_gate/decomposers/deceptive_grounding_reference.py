"""REFERENCE ONLY: the gold decomposition of a Deceptive Grounding question.

This is NOT a decomposer and is NOT registered. It reads ``Item.gold`` (the
queried drug X, the disease C and the competitors Y and ``Y_synthetic``), which
an arm must never see, so it must never be used inside the gate or any arm. It
exists for one purpose: the separate accuracy measurement of the model-based
decomposer (``binding_gate.evaluation.decomposer``; PREREGISTRATION.md section 7,
"Decomposer (A), measured on its own"). It does not implement the
``Decomposer`` protocol (it has no ``decompose(question)``) and refuses anything
that is not a full ``Item`` with gold, so it cannot be plugged into an arm by
configuration.

Reference procedure (version :data:`REFERENCE_VERSION`)
-------------------------------------------------------
Every Deceptive Grounding question is the benchmark template
``"What does the literature say about using {X} to treat {C}?"``. From the
item's gold metadata:

* **entity** = ``Entity(name=X, aliases=(), kind="drug")``. The benchmark has
  no alias lists, so the reference has none; the decomposer's own aliases are
  not scored for correctness, only checked against the competitors (below).
* **attribute** = :data:`REFERENCE_ATTRIBUTE`. The question is open-ended and
  asks for no single attribute, and none of the gate's checks reads the
  attribute, so it is recorded but not scored.
* **qualifiers** = one qualifier, ``Qualifier(kind="other", text=C)``. The
  disease restricts which facts about X count (evidence about X in another
  disease does not answer the question), so it is a qualifier and not part of
  the entity. Its kind is ``other``: of the five kinds, ``time``, ``version``
  and ``unit`` plainly do not apply, and ``place`` means a location or setting,
  not a medical condition. Matching does not require the kind to agree (the
  agreement is reported separately), because the gate's qualifier check looks
  for the qualifier's text, not its kind.
* **presuppositions** = one premise, "X is used, or has been proposed, to treat
  C", with ``key_terms = (X, C)``. The question takes for granted that there is
  something to say about using X for C; the premise check (D) needs both terms.
* **forbidden entity names** = Y and ``Y_synthetic`` of the item. The competitor
  must never be the entity or one of its aliases: if it were, evidence about
  the competitor would satisfy the gate's entity check. The drug of the
  ``class_proximate`` documents is not recorded by the benchmark and cannot be
  listed.

The reference is derived mechanically; nothing in it was written by hand for an
individual question.
"""

from __future__ import annotations

from typing import Any

from binding_gate.contracts import (
    Entity,
    Item,
    Presupposition,
    Qualifier,
    QualifierKind,
    QuestionConstraints,
)
from binding_gate.datasets.deceptive_grounding import DATASET_NAME, QUESTION_TEMPLATE

REFERENCE_VERSION = 1
REFERENCE_ATTRIBUTE = "what the published literature reports about this treatment use"
ENTITY_KIND = "drug"


class ReferenceError(ValueError):
    """Raised when an item cannot be given a reference decomposition."""


def _gold_text(item: Item, key: str) -> str:
    value: Any = item.gold.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReferenceError(f"item {item.id}: gold[{key!r}] must be a non-empty string")
    return value


class DeceptiveGroundingReference:
    """REFERENCE ONLY (uses gold). Never use inside the gate; see the module docstring."""

    dataset = DATASET_NAME
    version = REFERENCE_VERSION
    reference_only = True

    def _check(self, item: Any) -> Item:
        if not isinstance(item, Item):
            raise ReferenceError(
                "the reference decomposition needs a full Item with gold metadata; it must never "
                f"be given an arm input (got {type(item).__name__})"
            )
        if item.dataset != DATASET_NAME:
            raise ReferenceError(f"item {item.id} is from dataset {item.dataset!r}, not {DATASET_NAME!r}")
        expected = QUESTION_TEMPLATE.format(X=_gold_text(item, "X"), C=_gold_text(item, "C"))
        if item.question != expected:
            raise ReferenceError(f"item {item.id}: the question is not the benchmark template for its X and C")
        return item

    def constraints(self, item: Item) -> QuestionConstraints:
        """The reference constraints of the item's question (identical for all items of an (X, C) pair)."""
        item = self._check(item)
        x, c = _gold_text(item, "X"), _gold_text(item, "C")
        return QuestionConstraints(
            question=item.question,
            entity=Entity(name=x, aliases=(), kind=ENTITY_KIND),
            attribute=REFERENCE_ATTRIBUTE,
            qualifiers=(Qualifier(QualifierKind.OTHER, c),),
            presuppositions=(Presupposition(f"{x} is used, or has been proposed, to treat {c}.", (x, c)),),
        )

    def forbidden_entity_names(self, item: Item) -> tuple[str, ...]:
        """Names that must never be the entity or an alias of it: the competitors Y and Y_synthetic."""
        item = self._check(item)
        return (_gold_text(item, "Y"), _gold_text(item, "Y_synthetic"))


REFERENCES: dict[str, type[DeceptiveGroundingReference]] = {DATASET_NAME: DeceptiveGroundingReference}
"""Reference procedures by dataset name. Other datasets have none yet (PR 7 and PR 8)."""
