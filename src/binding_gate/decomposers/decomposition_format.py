"""The decomposer's fixed prompt and its deterministic output parser (gate function A).

Nothing in this module calls a model. :func:`build_decompose_request` turns a
question into the :class:`~binding_gate.contracts.ModelRequest` the decomposer
sends, and :func:`parse_decomposition` turns the model's raw reply into
:class:`~binding_gate.contracts.QuestionConstraints` or an explicit failure.
Both are pure functions of their arguments.

The prompt
----------
:data:`DECOMPOSER_SYSTEM_PROMPT` (prompt version :data:`PROMPT_VERSION`) is the
complete, fixed instruction. The user message is ``"Question: " + question``.
The prompt is generic: it does not mention any benchmark, drug, disease or
document, and its one worked example is invented. Its SHA-256 is part of the
decomposer's configuration hash, so any edit to the wording changes the hash of
every run that uses it. Changing it after the freeze is an amendment.

Output format and parsing rule (frozen with the pre-registration)
----------------------------------------------------------------
The model must reply with exactly one JSON object::

    {"entity": {"name": str, "aliases": [str, ...], "kind": str | null},
     "attribute": str,
     "qualifiers": [{"kind": "time"|"place"|"version"|"unit"|"other",
                     "text": str, "aliases": [str, ...]}, ...],
     "presuppositions": [{"text": str, "key_terms": [str, ...]}, ...]}

1. Outer whitespace is ignored. If the reply starts with a Markdown code fence
   (a first line of exactly three backticks, optionally followed by ``json``),
   it must end with a closing fence line; the lines in between are the payload.
   Any other text before or after the object makes the reply ``unparseable``.
2. The payload must be exactly one JSON value (``json.loads``; trailing data is
   an error) and must be an object. A key that occurs twice in any object is an
   error.
3. Every object must have exactly the keys shown above: a missing key or an
   extra key is an error. (The prompt asks for all keys; ``aliases`` and lists
   may be empty, ``kind`` of the entity may be ``null``.)
4. Every string must contain at least one non-whitespace character; its outer
   whitespace is trimmed and nothing else is changed. Lists must be JSON arrays
   of such strings or objects. ``qualifiers[].kind`` must be one of the five
   kinds (letter case and outer whitespace ignored).
5. ``QuestionConstraints.question`` is the question that was asked, never text
   from the model.

The parser never repairs output: no fixing of broken JSON, no dropping of extra
keys, no guessing of a missing field, no second request. A reply that breaks any
rule yields ``status = "unparseable"`` with a ``reason``. The decomposer turns
that into :class:`~binding_gate.decomposers.model_decomposer.DecompositionFailed`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from binding_gate.canonical import sha256_text
from binding_gate.contracts import (
    ContractError,
    Entity,
    Message,
    ModelRequest,
    Presupposition,
    Qualifier,
    QualifierKind,
    QuestionConstraints,
)

PROMPT_VERSION = 1

DECOMPOSER_SYSTEM_PROMPT = """\
You decompose a question into the constraints that any evidence used to answer it must satisfy. You do not answer the question.

Reply with exactly one JSON object with exactly these four keys, and nothing else (no explanation, no Markdown):
{"entity": {"name": "...", "aliases": ["..."], "kind": "..."}, "attribute": "...", "qualifiers": [{"kind": "...", "text": "...", "aliases": ["..."]}], "presuppositions": [{"text": "...", "key_terms": ["..."]}]}

Field rules:
- "entity": the one thing the question asks about.
  - "name": copied exactly as it is written in the question (same spelling and wording; do not expand, translate or correct it).
  - "aliases": other names that refer to exactly the same entity, such as a brand name and a generic name, or the full form of an abbreviation. List only names you are confident denote the same entity. Never list a related, similar, competing or alternative entity, and never the class or family it belongs to. Use [] if there are none.
  - "kind": a short lower-case type such as "drug", "person", "company" or "product", or null if unclear.
- "attribute": what the question asks to know about the entity, in a few words.
- "qualifiers": every condition stated in the question that restricts which facts about the entity count.
  - "kind" is one of: "time" (dates, years, periods), "place" (countries, regions, sites, settings), "version" (editions, releases, models, generations), "unit" (units of measurement or currency), "other" (any other restricting condition, for example a disease, a population or a circumstance).
  - "text": copied exactly from the question as one continuous span. Keep a multi-word condition together as one qualifier.
  - "aliases": other wordings of the same condition that you are confident about; [] if none.
  - Do not add conditions that are not in the question. The kind of source the question refers to ("the literature", "studies", "reports", "sources") is not a qualifier. Use [] if there are no qualifiers.
- "presuppositions": what the question takes for granted as true.
  - "text": the premise stated as one sentence.
  - "key_terms": the words or phrases, each copied exactly from the question, that evidence for this premise must mention.
  - Use [] if the question presupposes nothing.
- Every string must be non-empty. Do not add other keys.

Example (invented):
Question: How much memory did the Zentrix Z5 laptop ship with in its 2023 edition, in gigabytes?
{"entity": {"name": "Zentrix Z5", "aliases": [], "kind": "product"}, "attribute": "amount of memory it shipped with", "qualifiers": [{"kind": "version", "text": "2023 edition", "aliases": []}, {"kind": "unit", "text": "gigabytes", "aliases": ["GB"]}], "presuppositions": [{"text": "The Zentrix Z5 laptop had a 2023 edition.", "key_terms": ["Zentrix Z5", "2023 edition"]}]}"""

PROMPTS: dict[int, str] = {PROMPT_VERSION: DECOMPOSER_SYSTEM_PROMPT}
"""Every prompt version that exists. A new wording is added as a new version; old ones stay."""

USER_PREFIX = "Question: "

STATUS_PARSED = "parsed"
STATUS_UNPARSEABLE = "unparseable"

_TOP_KEYS = frozenset({"entity", "attribute", "qualifiers", "presuppositions"})
_ENTITY_KEYS = frozenset({"name", "aliases", "kind"})
_QUALIFIER_KEYS = frozenset({"kind", "text", "aliases"})
_PRESUPPOSITION_KEYS = frozenset({"text", "key_terms"})
_KINDS = {kind.value for kind in QualifierKind}


def prompt_text(version: int = PROMPT_VERSION) -> str:
    """The system prompt of ``version``."""
    try:
        return PROMPTS[version]
    except KeyError:
        raise ValueError(f"unknown decomposer prompt version {version!r}; available: {sorted(PROMPTS)}") from None


def prompt_sha256(version: int = PROMPT_VERSION) -> str:
    """SHA-256 of the system prompt of ``version`` (UTF-8)."""
    return sha256_text(prompt_text(version))


def build_decompose_request(question: str, version: int = PROMPT_VERSION) -> ModelRequest:
    """The request for one question. Sampling parameters come from the model client's config."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("the question must be a non-empty string")
    return ModelRequest(
        messages=(Message("system", prompt_text(version)), Message("user", USER_PREFIX + question))
    )


@dataclass(frozen=True)
class DecompositionParse:
    """Result of :func:`parse_decomposition`: constraints, or a reason why there are none."""

    status: str
    constraints: QuestionConstraints | None = None
    reason: str | None = None

    @property
    def parsed(self) -> bool:
        return self.status == STATUS_PARSED


class _Unparseable(ValueError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _value in pairs]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise _Unparseable(f"duplicate key(s) {duplicates}")
    return dict(pairs)


def _payload(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    opening = lines[0].strip().casefold()
    if opening not in {"```", "```json"}:
        raise _Unparseable("the code fence must open with ``` or ```json on its own line")
    if len(lines) < 3 or lines[-1].strip() != "```":
        raise _Unparseable("the code fence is not closed by a line of three backticks")
    return "\n".join(lines[1:-1])


def _object(where: str, value: Any, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _Unparseable(f"{where} must be a JSON object")
    missing = sorted(keys - set(value))
    if missing:
        raise _Unparseable(f"{where} is missing key(s) {missing}")
    extra = sorted(set(value) - keys)
    if extra:
        raise _Unparseable(f"{where} has unexpected key(s) {extra}")
    return value


def _string(where: str, value: Any) -> str:
    if not isinstance(value, str):
        raise _Unparseable(f"{where} must be a string")
    if not value.strip():
        raise _Unparseable(f"{where} must not be empty")
    return value.strip()


def _strings(where: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _Unparseable(f"{where} must be a JSON array of strings")
    return tuple(_string(f"{where}[{i}]", v) for i, v in enumerate(value))


def _list(where: str, value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise _Unparseable(f"{where} must be a JSON array")
    return value


def _constraints(question: str, data: Any) -> QuestionConstraints:
    top = _object("the reply", data, _TOP_KEYS)
    entity_raw = _object("entity", top["entity"], _ENTITY_KEYS)
    kind = entity_raw["kind"]
    entity = Entity(
        name=_string("entity.name", entity_raw["name"]),
        aliases=_strings("entity.aliases", entity_raw["aliases"]),
        kind=None if kind is None else _string("entity.kind", kind),
    )
    qualifiers = []
    for i, raw in enumerate(_list("qualifiers", top["qualifiers"])):
        where = f"qualifiers[{i}]"
        q = _object(where, raw, _QUALIFIER_KEYS)
        q_kind = _string(f"{where}.kind", q["kind"]).casefold()
        if q_kind not in _KINDS:
            raise _Unparseable(f"{where}.kind must be one of {sorted(_KINDS)}")
        qualifiers.append(
            Qualifier(
                kind=QualifierKind(q_kind),
                text=_string(f"{where}.text", q["text"]),
                aliases=_strings(f"{where}.aliases", q["aliases"]),
            )
        )
    presuppositions = []
    for i, raw in enumerate(_list("presuppositions", top["presuppositions"])):
        where = f"presuppositions[{i}]"
        p = _object(where, raw, _PRESUPPOSITION_KEYS)
        presuppositions.append(
            Presupposition(
                text=_string(f"{where}.text", p["text"]),
                key_terms=_strings(f"{where}.key_terms", p["key_terms"]),
            )
        )
    return QuestionConstraints(
        question=question,
        entity=entity,
        attribute=_string("attribute", top["attribute"]),
        qualifiers=tuple(qualifiers),
        presuppositions=tuple(presuppositions),
    )


def parse_decomposition(raw: str, question: str) -> DecompositionParse:
    """Parse a decomposer reply (see the module docstring). Never raises for bad output."""
    if not isinstance(raw, str):
        raise TypeError("parse_decomposition expects the raw model output as a string")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("the question must be a non-empty string")
    if not raw.strip():
        return DecompositionParse(STATUS_UNPARSEABLE, reason="empty output")
    try:
        payload = _payload(raw)
        try:
            data = json.loads(payload, object_pairs_hook=_no_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise _Unparseable(f"not exactly one JSON value: {exc.msg} (char {exc.pos})") from None
        except RecursionError:
            raise _Unparseable("JSON nested too deeply") from None
        constraints = _constraints(question, data)
    except _Unparseable as exc:
        return DecompositionParse(STATUS_UNPARSEABLE, reason=str(exc))
    except ContractError as exc:  # defensive: the checks above should make this unreachable
        return DecompositionParse(STATUS_UNPARSEABLE, reason=f"violates the contract: {exc}")
    return DecompositionParse(STATUS_PARSED, constraints=constraints)
