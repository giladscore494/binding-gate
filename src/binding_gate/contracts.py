"""Frozen data contracts shared by every component of binding-gate.

These dataclasses are the only formats in which components exchange data and in
which runs are persisted. They are frozen in PR 1 so that later pull requests,
developed in parallel, cannot drift apart.

Rules
-----
* Every contract is an immutable dataclass that validates itself on construction
  and raises :class:`ContractError` on bad data.
* ``to_dict()`` / ``from_dict()`` (and ``to_json()`` / ``from_json()``) round-trip
  exactly. ``from_dict`` rejects unknown and missing keys.
* Persisted top-level records (:class:`Item`, :class:`TraceRecord`,
  :class:`RunManifest`) carry ``schema_version`` and refuse to load any other
  version. Other contracts can be stored standalone with :func:`to_envelope`.
* Any change to a contract (field added, removed, renamed, retyped, enum member
  changed) requires bumping :data:`SCHEMA_VERSION` and recording the new
  fingerprint in ``schema_lock.py``. ``tests/test_schema_version.py`` fails
  otherwise.

Constructors accept plain JSON-style values for nested fields (a ``dict`` for a
nested contract, a ``list`` for a tuple, a ``str`` for an enum) and coerce them,
which is what makes ``from_dict`` uniform.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Self, TypeVar

from binding_gate.canonical import canonical_json, sha256_json, sha256_text

SCHEMA_VERSION = 1
"""Version of the contracts in this module. Bump on ANY contract change."""


class ContractError(ValueError):
    """Raised when data does not satisfy a contract."""


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #

_E = TypeVar("_E", bound=Enum)
_C = TypeVar("_C", bound="Contract")


def _text(name: str, value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{name} must be a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise ContractError(f"{name} must not be empty")
    return value


def _opt_text(name: str, value: Any) -> str | None:
    return None if value is None else _text(name, value)


def _texts(name: str, value: Any) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ContractError(f"{name} must be a list of strings")
    return tuple(_text(f"{name}[{i}]", v) for i, v in enumerate(value))


def _boolean(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{name} must be a boolean")
    return value


def _integer(name: str, value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{name} must be an integer")
    if value < minimum:
        raise ContractError(f"{name} must be >= {minimum}, got {value}")
    return value


def _json_object(name: str, value: Any) -> dict[str, Any]:
    """Validate an opaque JSON object and return a detached copy of it."""
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be a JSON object")
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name} is not JSON-serialisable: {exc}") from exc


def _enum(name: str, enum_cls: type[_E], value: Any) -> _E:
    try:
        return enum_cls(value)
    except ValueError:
        allowed = ", ".join(repr(m.value) for m in enum_cls)
        raise ContractError(f"{name} must be one of {allowed}; got {value!r}") from None


def _one(name: str, cls: type[_C], value: Any) -> _C:
    if isinstance(value, cls):
        return value
    if isinstance(value, Mapping):
        return cls.from_dict(value)
    raise ContractError(f"{name} must be a {cls.__name__} or a JSON object")


def _opt_one(name: str, cls: type[_C], value: Any) -> _C | None:
    return None if value is None else _one(name, cls, value)


def _many(name: str, cls: type[_C], value: Any) -> tuple[_C, ...]:
    if not isinstance(value, (list, tuple)):
        raise ContractError(f"{name} must be a list of {cls.__name__}")
    return tuple(_one(f"{name}[{i}]", cls, v) for i, v in enumerate(value))


def _timestamp(name: str, value: Any) -> str:
    """Validate an ISO-8601 timestamp that carries a UTC offset."""
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ContractError(f"{name} is not an ISO-8601 timestamp: {text!r}") from None
    if parsed.tzinfo is None:
        raise ContractError(f"{name} must include a UTC offset: {text!r}")
    return text


def _sha256(name: str, value: Any) -> str:
    text = _text(name, value)
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise ContractError(f"{name} must be a lowercase hex SHA-256 digest")
    return text


def _schema_version(value: Any) -> int:
    if value != SCHEMA_VERSION or isinstance(value, bool):
        raise ContractError(
            f"unsupported schema_version {value!r}; this code reads and writes only "
            f"version {SCHEMA_VERSION}. Migrate the data explicitly."
        )
    return SCHEMA_VERSION


def _encode(value: Any) -> Any:
    if isinstance(value, Contract):
        return {f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, Mapping):
        return {k: _encode(v) for k, v in value.items()}
    return value


class Contract:
    """Base class giving every contract uniform JSON (de)serialisation."""

    def _set(self, **values: Any) -> None:
        """Assign validated/coerced values on a frozen dataclass (``__post_init__`` only)."""
        for name, value in values.items():
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable ``dict`` (enums as strings, tuples as lists)."""
        return _encode(self)

    def to_json(self) -> str:
        """Return canonical JSON (sorted keys, one line)."""
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Build an instance from ``to_dict()`` output; unknown/missing keys are errors."""
        if not isinstance(data, Mapping):
            raise ContractError(f"{cls.__name__} must be built from a JSON object")
        fields = {f.name: f for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
        unknown = sorted(set(data) - set(fields))
        if unknown:
            raise ContractError(f"{cls.__name__}: unknown field(s) {unknown}")
        required = {
            name
            for name, f in fields.items()
            if name == "schema_version"
            or (f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING)
        }
        missing = sorted(required - set(data))
        if missing:
            raise ContractError(f"{cls.__name__}: missing field(s) {missing}")
        return cls(**data)

    @classmethod
    def from_json(cls, text: str) -> Self:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ContractError(f"{cls.__name__}: invalid JSON: {exc}") from exc
        return cls.from_dict(data)


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class QualifierKind(str, Enum):
    """Kind of a condition that narrows the question."""

    TIME = "time"
    PLACE = "place"
    VERSION = "version"
    UNIT = "unit"
    OTHER = "other"


class AnswerStatus(str, Enum):
    """State of a structured answer."""

    ANSWERED = "answered"
    ABSTAINED = "abstained"
    UNPARSEABLE = "unparseable"


class CheckStatus(str, Enum):
    """Outcome of one deterministic check."""

    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


class Decision(str, Enum):
    """Gate verdict."""

    ACCEPT = "accept"
    RESEARCH = "research"
    ABSTAIN = "abstain"


class Split(str, Enum):
    """Data split. Test items are only reachable through ``binding_gate.testlock``."""

    DEV = "dev"
    TEST = "test"


# --------------------------------------------------------------------------- #
# (A) Question decomposition
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Entity(Contract):
    """The entity the question is about.

    ``aliases`` are other surface forms that count as a mention of the same
    entity (brand/generic names, abbreviations). ``kind`` is an optional free-text
    type such as ``"drug"``.
    """

    name: str
    aliases: tuple[str, ...] = ()
    kind: str | None = None

    def __post_init__(self) -> None:
        self._set(
            name=_text("Entity.name", self.name),
            aliases=_texts("Entity.aliases", self.aliases),
            kind=_opt_text("Entity.kind", self.kind),
        )


@dataclass(frozen=True)
class Qualifier(Contract):
    """A condition the answer must satisfy (time, place, version, unit, other)."""

    kind: QualifierKind
    text: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self._set(
            kind=_enum("Qualifier.kind", QualifierKind, self.kind),
            text=_text("Qualifier.text", self.text),
            aliases=_texts("Qualifier.aliases", self.aliases),
        )


@dataclass(frozen=True)
class Presupposition(Contract):
    """Something the question takes for granted.

    ``text`` states the premise in words. ``key_terms`` are the surface strings
    whose presence in the evidence the deterministic premise check (D) looks for.
    """

    text: str
    key_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self._set(
            text=_text("Presupposition.text", self.text),
            key_terms=_texts("Presupposition.key_terms", self.key_terms),
        )


@dataclass(frozen=True)
class QuestionConstraints(Contract):
    """Output of the decomposer (A): what any acceptable evidence must be about."""

    question: str
    entity: Entity
    attribute: str
    qualifiers: tuple[Qualifier, ...] = ()
    presuppositions: tuple[Presupposition, ...] = ()

    def __post_init__(self) -> None:
        self._set(
            question=_text("QuestionConstraints.question", self.question),
            entity=_one("QuestionConstraints.entity", Entity, self.entity),
            attribute=_text("QuestionConstraints.attribute", self.attribute),
            qualifiers=_many("QuestionConstraints.qualifiers", Qualifier, self.qualifiers),
            presuppositions=_many(
                "QuestionConstraints.presuppositions", Presupposition, self.presuppositions
            ),
        )


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvidenceRef(Contract):
    """A pointer to a document by id and content hash (used in traces)."""

    id: str
    sha256: str
    url: str | None = None

    def __post_init__(self) -> None:
        self._set(
            id=_text("EvidenceRef.id", self.id),
            sha256=_sha256("EvidenceRef.sha256", self.sha256),
            url=_opt_text("EvidenceRef.url", self.url),
        )


@dataclass(frozen=True)
class Document(Contract):
    """One piece of evidence text shown to the model and inspected by the gate.

    ``sha256`` is the SHA-256 of ``text`` encoded as UTF-8 and is verified on
    construction; use :meth:`create` to compute it. ``metadata`` is opaque and
    is visible to arms, so it MUST NOT contain gold information (for example
    whether the document is about the queried entity or a competitor); that
    belongs in :attr:`Item.gold`.
    """

    id: str
    text: str
    sha256: str
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._set(
            id=_text("Document.id", self.id),
            text=_text("Document.text", self.text),
            sha256=_sha256("Document.sha256", self.sha256),
            url=_opt_text("Document.url", self.url),
            metadata=_json_object("Document.metadata", self.metadata),
        )
        if sha256_text(self.text) != self.sha256:
            raise ContractError(f"Document {self.id!r}: sha256 does not match text")

    @classmethod
    def create(
        cls,
        id: str,
        text: str,
        *,
        url: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Document:
        """Build a document, computing the content hash from ``text``."""
        if not isinstance(text, str):
            raise ContractError("Document.text must be a string")
        return cls(id=id, text=text, sha256=sha256_text(text), url=url, metadata=dict(metadata or {}))

    def ref(self) -> EvidenceRef:
        return EvidenceRef(id=self.id, sha256=self.sha256, url=self.url)


# --------------------------------------------------------------------------- #
# (B) Structured answer
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AnswerCandidate(Contract):
    """The structured answer every arm must produce (gated or not).

    * ``answered``: ``value``, ``quote`` and ``source_id`` are all present.
      ``quote`` is the text the model claims to copy verbatim from the document
      whose id is ``source_id``.
    * ``abstained``: the model explicitly declined; the three fields are ``None``.
    * ``unparseable``: the raw output could not be parsed into either of the above
      (including a value without a quote or source); ``reason`` says why.

    ``raw`` always holds the unmodified model output the candidate was parsed
    from (it may be empty when the candidate was produced by the gate itself).
    """

    status: AnswerStatus
    raw: str
    value: str | None = None
    quote: str | None = None
    source_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        self._set(
            status=_enum("AnswerCandidate.status", AnswerStatus, self.status),
            raw=_text("AnswerCandidate.raw", self.raw, allow_empty=True),
            value=_opt_text("AnswerCandidate.value", self.value),
            quote=_opt_text("AnswerCandidate.quote", self.quote),
            source_id=_opt_text("AnswerCandidate.source_id", self.source_id),
            reason=_opt_text("AnswerCandidate.reason", self.reason),
        )
        parts = (self.value, self.quote, self.source_id)
        if self.status is AnswerStatus.ANSWERED:
            if any(p is None for p in parts):
                raise ContractError("an answered candidate needs value, quote and source_id")
        elif any(p is not None for p in parts):
            raise ContractError(
                f"a candidate with status {self.status.value!r} must not carry "
                "value, quote or source_id"
            )
        if self.status is AnswerStatus.UNPARSEABLE and self.reason is None:
            raise ContractError("an unparseable candidate needs a reason")

    @classmethod
    def answered(cls, *, value: str, quote: str, source_id: str, raw: str) -> AnswerCandidate:
        return cls(AnswerStatus.ANSWERED, raw, value=value, quote=quote, source_id=source_id)

    @classmethod
    def abstained(cls, *, raw: str, reason: str | None = None) -> AnswerCandidate:
        return cls(AnswerStatus.ABSTAINED, raw, reason=reason)

    @classmethod
    def unparseable(cls, *, raw: str, reason: str) -> AnswerCandidate:
        return cls(AnswerStatus.UNPARSEABLE, raw, reason=reason)


# --------------------------------------------------------------------------- #
# (C, D, E) Checks and verdict
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckResult(Contract):
    """Result of one deterministic check. ``details`` is opaque JSON for auditing."""

    check: str
    status: CheckStatus
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._set(
            check=_text("CheckResult.check", self.check),
            status=_enum("CheckResult.status", CheckStatus, self.status),
            details=_json_object("CheckResult.details", self.details),
        )

    @property
    def passed(self) -> bool:
        return self.status is CheckStatus.PASS

    @property
    def failed(self) -> bool:
        return self.status is CheckStatus.FAIL


@dataclass(frozen=True)
class Verdict(Contract):
    """Gate decision (E) with the check results that led to it.

    Invariants: ``accept`` is impossible while any recorded check failed;
    ``research`` and ``abstain`` need at least one reason; ``research`` needs a
    ``research_request`` (what to look for next) and only ``research`` may have one.
    """

    decision: Decision
    reasons: tuple[str, ...] = ()
    check_results: tuple[CheckResult, ...] = ()
    research_request: str | None = None

    def __post_init__(self) -> None:
        self._set(
            decision=_enum("Verdict.decision", Decision, self.decision),
            reasons=_texts("Verdict.reasons", self.reasons),
            check_results=_many("Verdict.check_results", CheckResult, self.check_results),
            research_request=_opt_text("Verdict.research_request", self.research_request),
        )
        if self.decision is Decision.ACCEPT:
            failed = [r.check for r in self.check_results if r.failed]
            if failed:
                raise ContractError(f"verdict cannot be accept while checks failed: {failed}")
        elif not self.reasons:
            raise ContractError(f"a {self.decision.value!r} verdict needs at least one reason")
        if (self.decision is Decision.RESEARCH) != (self.research_request is not None):
            raise ContractError("research_request is required for, and only for, research")


# --------------------------------------------------------------------------- #
# Benchmark items
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ArmInput(Contract):
    """What an arm is allowed to see of an item: no gold, no group, no stratum."""

    item_id: str
    question: str
    documents: tuple[Document, ...] = ()

    def __post_init__(self) -> None:
        self._set(
            item_id=_text("ArmInput.item_id", self.item_id),
            question=_text("ArmInput.question", self.question),
            documents=_many("ArmInput.documents", Document, self.documents),
        )
        _unique_ids("ArmInput.documents", self.documents)


@dataclass(frozen=True)
class Item(Contract):
    """One benchmark stimulus in the common format produced by a dataset adapter.

    ``group_id`` is the unit of the dev/test split: every item that shares
    underlying content (e.g. all conditions built from the same source triple)
    must share it. ``stratum`` is an optional condition label used only for
    reporting. ``gold`` is opaque, dataset-specific metadata for scorers and is
    never shown to an arm (see :meth:`arm_input`). ``documents`` may be empty for
    live-web items whose evidence is fetched at run time.
    """

    id: str
    group_id: str
    dataset: str
    question: str
    documents: tuple[Document, ...] = ()
    gold: dict[str, Any] = field(default_factory=dict)
    stratum: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self._set(
            id=_text("Item.id", self.id),
            group_id=_text("Item.group_id", self.group_id),
            dataset=_text("Item.dataset", self.dataset),
            question=_text("Item.question", self.question),
            documents=_many("Item.documents", Document, self.documents),
            gold=_json_object("Item.gold", self.gold),
            stratum=_opt_text("Item.stratum", self.stratum),
            schema_version=_schema_version(self.schema_version),
        )
        _unique_ids("Item.documents", self.documents)

    def arm_input(self) -> ArmInput:
        """The gold-free view of this item that is handed to arms."""
        return ArmInput(item_id=self.id, question=self.question, documents=self.documents)


def _unique_ids(name: str, documents: tuple[Document, ...]) -> None:
    ids = [d.id for d in documents]
    if len(ids) != len(set(ids)):
        raise ContractError(f"{name}: document ids must be unique, got {ids}")


# --------------------------------------------------------------------------- #
# Model calls
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Message(Contract):
    """One chat message of a prompt (``role`` is e.g. system/user/assistant)."""

    role: str
    content: str

    def __post_init__(self) -> None:
        self._set(
            role=_text("Message.role", self.role),
            content=_text("Message.content", self.content, allow_empty=True),
        )


@dataclass(frozen=True)
class TokenUsage(Contract):
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        self._set(
            input_tokens=_integer("TokenUsage.input_tokens", self.input_tokens),
            output_tokens=_integer("TokenUsage.output_tokens", self.output_tokens),
        )


@dataclass(frozen=True)
class ModelRequest(Contract):
    """A prompt plus opaque sampling parameters (temperature, max tokens, ...)."""

    messages: tuple[Message, ...]
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._set(
            messages=_many("ModelRequest.messages", Message, self.messages),
            params=_json_object("ModelRequest.params", self.params),
        )
        if not self.messages:
            raise ContractError("ModelRequest.messages must not be empty")


@dataclass(frozen=True)
class ModelResponse(Contract):
    """Raw text returned by a model, the exact model id that served it, and usage.

    ``usage`` is ``None`` when the provider does not report token counts.
    ``raw`` is opaque provider metadata (never secrets).
    """

    text: str
    model: str
    usage: TokenUsage | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._set(
            text=_text("ModelResponse.text", self.text, allow_empty=True),
            model=_text("ModelResponse.model", self.model),
            usage=_opt_one("ModelResponse.usage", TokenUsage, self.usage),
            raw=_json_object("ModelResponse.raw", self.raw),
        )


@dataclass(frozen=True)
class ModelCall(Contract):
    """One logged model invocation: who was asked what, for which purpose, and the reply.

    ``purpose`` says why the call was made (e.g. ``"answer"``, ``"decompose"``,
    ``"judge"``). Exactly one of ``response`` and ``error`` is set.
    """

    purpose: str
    client: str
    request: ModelRequest
    started_at: str
    ended_at: str
    response: ModelResponse | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        self._set(
            purpose=_text("ModelCall.purpose", self.purpose),
            client=_text("ModelCall.client", self.client),
            request=_one("ModelCall.request", ModelRequest, self.request),
            started_at=_timestamp("ModelCall.started_at", self.started_at),
            ended_at=_timestamp("ModelCall.ended_at", self.ended_at),
            response=_opt_one("ModelCall.response", ModelResponse, self.response),
            error=_opt_text("ModelCall.error", self.error),
        )
        if (self.response is None) == (self.error is None):
            raise ContractError("ModelCall needs exactly one of response and error")


# --------------------------------------------------------------------------- #
# Component outputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Decomposition(Contract):
    """What a decomposer returns: the constraints and the model calls it made."""

    constraints: QuestionConstraints
    model_calls: tuple[ModelCall, ...] = ()

    def __post_init__(self) -> None:
        self._set(
            constraints=_one("Decomposition.constraints", QuestionConstraints, self.constraints),
            model_calls=_many("Decomposition.model_calls", ModelCall, self.model_calls),
        )


@dataclass(frozen=True)
class Attempt(Contract):
    """One answer attempt: the candidate, the evidence it was given, the gate verdict.

    ``verdict`` is ``None`` when no gate ran (ungated arms) or when the candidate
    was not an answer: a gate only ever evaluates ``answered`` candidates, so
    abstentions and unparseable outputs pass through untouched and stay visible.
    """

    answer: AnswerCandidate
    evidence: tuple[EvidenceRef, ...] = ()
    verdict: Verdict | None = None

    def __post_init__(self) -> None:
        self._set(
            answer=_one("Attempt.answer", AnswerCandidate, self.answer),
            evidence=_many("Attempt.evidence", EvidenceRef, self.evidence),
            verdict=_opt_one("Attempt.verdict", Verdict, self.verdict),
        )
        if self.verdict is not None and self.answer.status is not AnswerStatus.ANSWERED:
            raise ContractError("a verdict may only be attached to an answered candidate")


def _validate_outcome(
    owner: str, attempts: tuple[Attempt, ...], final_answer: AnswerCandidate
) -> None:
    """Shared invariants tying attempts to the final answer.

    * There is at least one attempt; every attempt but the last ended in a
      ``research`` verdict (that is the only reason to try again).
    * If the last attempt was accepted or not gated, the final answer IS that
      attempt's candidate. Otherwise (``abstain``, or ``research`` with no further
      attempt) the final answer is an abstention.
    """
    if not attempts:
        raise ContractError(f"{owner} needs at least one attempt")
    for i, attempt in enumerate(attempts[:-1]):
        if attempt.verdict is None or attempt.verdict.decision is not Decision.RESEARCH:
            raise ContractError(f"{owner}.attempts[{i}] is not last, so its verdict must be research")
    last = attempts[-1]
    if last.verdict is None or last.verdict.decision is Decision.ACCEPT:
        if final_answer != last.answer:
            raise ContractError(f"{owner}.final_answer must equal the last attempt's candidate")
    elif final_answer.status is not AnswerStatus.ABSTAINED:
        raise ContractError(
            f"{owner}.final_answer must be an abstention when the last verdict is "
            f"{last.verdict.decision.value!r}"
        )


@dataclass(frozen=True)
class ArmOutput(Contract):
    """Everything an arm returns for one item.

    ``final_answer`` is what the system would actually tell the user and is the
    only thing primary scoring looks at.
    """

    attempts: tuple[Attempt, ...]
    final_answer: AnswerCandidate
    constraints: QuestionConstraints | None = None
    model_calls: tuple[ModelCall, ...] = ()

    def __post_init__(self) -> None:
        self._set(
            attempts=_many("ArmOutput.attempts", Attempt, self.attempts),
            final_answer=_one("ArmOutput.final_answer", AnswerCandidate, self.final_answer),
            constraints=_opt_one("ArmOutput.constraints", QuestionConstraints, self.constraints),
            model_calls=_many("ArmOutput.model_calls", ModelCall, self.model_calls),
        )
        _validate_outcome("ArmOutput", self.attempts, self.final_answer)


@dataclass(frozen=True)
class ScoreResult(Contract):
    """Output of one scorer for one final answer: a label plus opaque details."""

    scorer: str
    label: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._set(
            scorer=_text("ScoreResult.scorer", self.scorer),
            label=_text("ScoreResult.label", self.label),
            details=_json_object("ScoreResult.details", self.details),
        )


# --------------------------------------------------------------------------- #
# Persisted run records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TraceRecord(Contract):
    """One line of ``traces.jsonl``: one item x arm x model x repeat.

    Either the arm produced an outcome (``attempts`` and ``final_answer`` set,
    ``error`` is ``None``) or it crashed (``error`` set, no outcome). Crashed
    records are written, never dropped, so they stay in every denominator.
    ``model`` is the label of the model under test for this record.
    """

    run_id: str
    config_hash: str
    git_commit: str
    item_id: str
    group_id: str
    dataset: str
    split: Split
    arm: str
    model: str
    repeat: int
    started_at: str
    ended_at: str
    stratum: str | None = None
    constraints: QuestionConstraints | None = None
    attempts: tuple[Attempt, ...] = ()
    final_answer: AnswerCandidate | None = None
    model_calls: tuple[ModelCall, ...] = ()
    scores: tuple[ScoreResult, ...] = ()
    error: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self._set(
            run_id=_text("TraceRecord.run_id", self.run_id),
            config_hash=_sha256("TraceRecord.config_hash", self.config_hash),
            git_commit=_text("TraceRecord.git_commit", self.git_commit),
            item_id=_text("TraceRecord.item_id", self.item_id),
            group_id=_text("TraceRecord.group_id", self.group_id),
            dataset=_text("TraceRecord.dataset", self.dataset),
            split=_enum("TraceRecord.split", Split, self.split),
            arm=_text("TraceRecord.arm", self.arm),
            model=_text("TraceRecord.model", self.model),
            repeat=_integer("TraceRecord.repeat", self.repeat),
            started_at=_timestamp("TraceRecord.started_at", self.started_at),
            ended_at=_timestamp("TraceRecord.ended_at", self.ended_at),
            stratum=_opt_text("TraceRecord.stratum", self.stratum),
            constraints=_opt_one("TraceRecord.constraints", QuestionConstraints, self.constraints),
            attempts=_many("TraceRecord.attempts", Attempt, self.attempts),
            final_answer=_opt_one("TraceRecord.final_answer", AnswerCandidate, self.final_answer),
            model_calls=_many("TraceRecord.model_calls", ModelCall, self.model_calls),
            scores=_many("TraceRecord.scores", ScoreResult, self.scores),
            error=_opt_text("TraceRecord.error", self.error),
            schema_version=_schema_version(self.schema_version),
        )
        if self.error is None:
            if self.final_answer is None:
                raise ContractError("TraceRecord needs a final_answer unless error is set")
            _validate_outcome("TraceRecord", self.attempts, self.final_answer)
        elif self.final_answer is not None or self.attempts:
            raise ContractError("a TraceRecord with an error must not carry an outcome")

    def total_usage(self) -> TokenUsage:
        """Sum of reported token usage over all model calls of this record."""
        usages = [c.response.usage for c in self.model_calls if c.response and c.response.usage]
        return TokenUsage(
            input_tokens=sum(u.input_tokens for u in usages),
            output_tokens=sum(u.output_tokens for u in usages),
        )


@dataclass(frozen=True)
class RunManifest(Contract):
    """``manifest.json`` of a run directory: what was run, from which code, and when.

    ``ended_at``, ``trace_count`` and ``traces_sha256`` are filled when the run
    is closed; a manifest without them describes an unfinished or crashed run.
    ``test_unlock_tag`` is the freeze tag used to unlock the test split, if any.
    """

    run_id: str
    config: dict[str, Any]
    config_hash: str
    git_commit: str
    git_dirty: bool
    package_version: str
    python_version: str
    started_at: str
    config_path: str | None = None
    test_unlock_tag: str | None = None
    ended_at: str | None = None
    trace_count: int | None = None
    traces_sha256: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self._set(
            run_id=_text("RunManifest.run_id", self.run_id),
            config=_json_object("RunManifest.config", self.config),
            config_hash=_sha256("RunManifest.config_hash", self.config_hash),
            git_commit=_text("RunManifest.git_commit", self.git_commit),
            git_dirty=_boolean("RunManifest.git_dirty", self.git_dirty),
            package_version=_text("RunManifest.package_version", self.package_version),
            python_version=_text("RunManifest.python_version", self.python_version),
            started_at=_timestamp("RunManifest.started_at", self.started_at),
            config_path=_opt_text("RunManifest.config_path", self.config_path),
            test_unlock_tag=_opt_text("RunManifest.test_unlock_tag", self.test_unlock_tag),
            ended_at=None if self.ended_at is None else _timestamp("RunManifest.ended_at", self.ended_at),
            trace_count=None if self.trace_count is None else _integer("RunManifest.trace_count", self.trace_count),
            traces_sha256=None if self.traces_sha256 is None else _sha256("RunManifest.traces_sha256", self.traces_sha256),
            schema_version=_schema_version(self.schema_version),
        )
        if sha256_json(self.config) != self.config_hash:
            raise ContractError("RunManifest.config_hash does not match RunManifest.config")


# --------------------------------------------------------------------------- #
# Standalone storage and the schema fingerprint
# --------------------------------------------------------------------------- #

CONTRACT_TYPES: tuple[type[Contract], ...] = (
    Entity,
    Qualifier,
    Presupposition,
    QuestionConstraints,
    EvidenceRef,
    Document,
    AnswerCandidate,
    CheckResult,
    Verdict,
    ArmInput,
    Item,
    Message,
    TokenUsage,
    ModelRequest,
    ModelResponse,
    ModelCall,
    Decomposition,
    Attempt,
    ArmOutput,
    ScoreResult,
    TraceRecord,
    RunManifest,
)
ENUM_TYPES: tuple[type[Enum], ...] = (QualifierKind, AnswerStatus, CheckStatus, Decision, Split)

_BY_NAME: dict[str, type[Contract]] = {cls.__name__: cls for cls in CONTRACT_TYPES}


def to_envelope(obj: Contract) -> dict[str, Any]:
    """Wrap any contract for standalone storage (caches, fixtures, gold files).

    The envelope is ``{"schema_version": N, "kind": "<class name>", "data": {...}}``.
    """
    if type(obj) not in CONTRACT_TYPES:
        raise ContractError(f"{type(obj).__name__} is not a registered contract type")
    return {"schema_version": SCHEMA_VERSION, "kind": type(obj).__name__, "data": obj.to_dict()}


def from_envelope(envelope: Mapping[str, Any]) -> Contract:
    """Inverse of :func:`to_envelope`; rejects other schema versions and unknown kinds."""
    if not isinstance(envelope, Mapping) or set(envelope) != {"schema_version", "kind", "data"}:
        raise ContractError("an envelope has exactly the keys schema_version, kind and data")
    _schema_version(envelope["schema_version"])
    cls = _BY_NAME.get(envelope["kind"])
    if cls is None:
        raise ContractError(f"unknown contract kind {envelope['kind']!r}")
    return cls.from_dict(envelope["data"])


def schema_fingerprint() -> str:
    """SHA-256 over the shape of every contract (fields, declared types, defaults, enums).

    The value recorded for the current :data:`SCHEMA_VERSION` in ``schema_lock.py``
    must equal this, which is what forces a version bump on contract changes.
    """
    shape = {
        "contracts": {
            cls.__name__: [
                [f.name, str(f.type), f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING]
                for f in dataclasses.fields(cls)  # type: ignore[arg-type]
            ]
            for cls in CONTRACT_TYPES
        },
        "enums": {cls.__name__: [m.value for m in cls] for cls in ENUM_TYPES},
    }
    return sha256_json(shape)


__all__ = [
    "SCHEMA_VERSION",
    "ContractError",
    "Contract",
    "CONTRACT_TYPES",
    "ENUM_TYPES",
    *[cls.__name__ for cls in CONTRACT_TYPES],
    *[cls.__name__ for cls in ENUM_TYPES],
    "to_envelope",
    "from_envelope",
    "schema_fingerprint",
]
