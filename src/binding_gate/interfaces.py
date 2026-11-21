"""Abstract interfaces every component implements.

These are structural types (``typing.Protocol``): a component does not need to
inherit from anything, it only needs the attributes and methods listed here.
Components are created by factories registered in ``binding_gate.registry``.

Determinism requirements
------------------------
* :class:`BindingCheck`, :class:`Gate` and any :class:`Scorer` used for primary
  metrics must be pure functions of their arguments: no model calls, no network,
  no randomness, no clock. The same inputs must give the same output on any
  machine.
* Only :class:`ModelClient` talks to a model. :class:`Decomposer` and
  :class:`Arm` may use model clients and must return every call they made so it
  can be written to the trace.
* Nothing except a :class:`Scorer` (and the dataset adapter that built the item)
  may read ``Item.gold``. Arms receive an :class:`~binding_gate.contracts.ArmInput`,
  which does not contain it.
"""

from __future__ import annotations

from typing import Iterator, Protocol, Sequence

from binding_gate.contracts import (
    AnswerCandidate,
    ArmInput,
    ArmOutput,
    CheckResult,
    Decomposition,
    Document,
    Item,
    ModelRequest,
    ModelResponse,
    QuestionConstraints,
    ScoreResult,
    Verdict,
)


class DatasetAdapter(Protocol):
    """Converts one external benchmark into :class:`Item` objects.

    The adapter yields ALL items in a stable order and never splits them: the
    dev/test split is applied by ``binding_gate.split`` / ``binding_gate.testlock``
    on ``Item.group_id``. ``Item.dataset`` must equal :attr:`name`.
    """

    name: str

    def iter_items(self) -> Iterator[Item]:
        """Yield every item of the dataset in a deterministic order."""
        ...

    def fingerprint(self) -> str:
        """A hash identifying the exact source data (e.g. SHA-256 over the input files)."""
        ...


class ModelClient(Protocol):
    """One configured model endpoint. ``name`` is the label written to traces."""

    name: str

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Send the request and return the raw reply. Raise on failure; do not retry silently."""
        ...


class Decomposer(Protocol):
    """Gate function (A): question -> constraints. The only model-based gate step."""

    name: str

    def decompose(self, question: str) -> Decomposition:
        ...


class BindingCheck(Protocol):
    """One deterministic check of gate functions (C) and (D).

    Called only for ``answered`` candidates. Must return a
    :class:`~binding_gate.contracts.CheckResult` whose ``check`` equals
    :attr:`name`; use ``not_applicable`` when the check has nothing to test
    (for example a question without qualifiers).
    """

    name: str

    def check(
        self,
        constraints: QuestionConstraints,
        answer: AnswerCandidate,
        documents: Sequence[Document],
    ) -> CheckResult:
        ...


class Gate(Protocol):
    """Gate function (E): runs its checks and maps the results to a verdict, deterministically."""

    name: str

    def evaluate(
        self,
        constraints: QuestionConstraints,
        answer: AnswerCandidate,
        documents: Sequence[Document],
    ) -> Verdict:
        ...


class Arm(Protocol):
    """One experimental condition: how a question is turned into a final answer.

    Every arm, including ungated baselines, returns the same structured output.
    """

    name: str

    def run(self, arm_input: ArmInput) -> ArmOutput:
        ...


class Scorer(Protocol):
    """Labels a final answer using the item's gold metadata."""

    name: str

    def score(self, item: Item, final_answer: AnswerCandidate) -> ScoreResult:
        ...
