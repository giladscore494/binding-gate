"""Pure-Python implementation of the MCP tools. No MCP SDK is needed to use or test this module.

``server.py`` only translates between the MCP protocol and :meth:`GateService.call_tool`.

Why the verdict is the research gate's verdict
----------------------------------------------
:meth:`GateService.verify_answer` builds the gate exactly as the ``gated`` arm
does - ``registry.create("gate", "binding", checks=[...], **parameters)`` - and
calls the same ``evaluate(constraints, answer, documents)``. There is no second
implementation of any check, of the decision table, or of a default value: the
check names come from ``gates.binding.ALL_CHECKS`` and the defaults from
``gates.binding.DEFAULT_PARAMETERS``. The output carries ``verdict_sha256``, the
SHA-256 of ``Verdict.to_json()``, which is byte-identical to the verdict a
research run would record for the same constraints, candidate and documents
(``tests/test_mcp_core.py`` checks this against the registry gate and against
the ``gated`` arm).

What a call can and cannot do
-----------------------------
* With explicit ``constraints`` the call is a pure function of its arguments:
  no model, no network, no clock, no randomness, no file system.
* Without ``constraints`` the configured decomposer model is called (network,
  API key, cost); without a configured decomposer that is an error.
* No argument is ever used as a path, URL to fetch, command or code. A
  document ``url`` is only copied to the output.
* Every argument is validated here (types, unknown keys, sizes); the JSON
  schema given to the client is a description, not the enforcement.
* Outputs are JSON objects; ``canonical_json`` (sorted keys) is used for the
  text form, and every list has a fixed order (checks in canonical order,
  documents in input order).

Input conventions
-----------------
* The quote is matched after the pre-registered normalisation
  (PREREGISTRATION.md section 6.1); the candidate's ``raw`` field, which the
  checks do not read, is set to the canonical JSON of the structured answer the
  research protocol asks a model for (``{"status": "answered", "value", "quote",
  "source_id"}``).
* The requested checks are run in the canonical order of ``ALL_CHECKS``
  whatever order the caller lists them in, so the output depends only on the
  set. Every committed run config lists them in that order too.
* An answer without a quote or a source id is not something the gate judges:
  under the research protocol it is ``unparseable`` (section 6.2), so here it
  is an input error that tells the agent to quote or abstain.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from binding_gate import __version__, registry, secrets
from binding_gate.canonical import canonical_json, sha256_text
from binding_gate.contracts import (
    SCHEMA_VERSION,
    AnswerCandidate,
    ContractError,
    Decomposition,
    Document,
    QualifierKind,
    QuestionConstraints,
    Verdict,
)
from binding_gate.gates.binding import ALL_CHECKS, DEFAULT_PARAMETERS, resolve_scope
from binding_gate.gates.binding import NAME as GATE_NAME
from binding_gate.mcp import schemas
from binding_gate.mcp.config import Limits, ServerConfig

PARAMETERS_STATUS = (
    "PROPOSED (PREREGISTRATION.md section 5.1): requires owner approval before the pre-registration is "
    "frozen; may still change on dev data"
)
"""How the default gate parameters are to be read. Update when the pre-registration is frozen."""

NOTICE = (
    "The binding gate checks binding, not truth: ACCEPT means the quote is verbatim in the cited document "
    "and the passage is about the question's entity and conditions under fixed string rules; it does not "
    "mean the source is correct or the value was read correctly. Its parameters are PROPOSED values of a "
    "pre-registration that is not yet frozen, and the study measuring its effect has no results yet."
)

DECOMPOSER_NOT_CONFIGURED = (
    "No decomposer model is configured on this server, so questions cannot be decomposed here. "
    "Write the constraints yourself and pass them to verify_answer as 'constraints': "
    '{"entity": {"name": "...", "aliases": [...]}, "attribute": "...", '
    '"qualifiers": [{"kind": "time|place|version|unit|other", "text": "..."}], '
    '"presuppositions": [{"text": "...", "key_terms": [...]}]}. '
    "The server operator can enable a decomposer with a [decomposer] table in the server configuration file."
)


# --------------------------------------------------------------------------- #
# Errors and results
# --------------------------------------------------------------------------- #


class ToolError(Exception):
    """A refused tool call. ``code`` is machine-readable; ``message`` says what to do."""

    def __init__(self, code: str, message: str, *, extra: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = dict(extra or {})

    def payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


ERROR_CODES: tuple[str, ...] = (
    "invalid_input",
    "too_large",
    "constraints_required",
    "decomposer_not_configured",
    "api_key_missing",
    "decomposition_failed",
    "unknown_tool",
    "internal_error",
)


@dataclass(frozen=True)
class ToolResult:
    """What a tool call returns: a JSON object, and whether it is an error."""

    payload: dict[str, Any]
    is_error: bool = False

    def text(self) -> str:
        """The canonical JSON text of the payload (sorted keys, one line)."""
        return canonical_json(self.payload)


def _invalid(path: str, message: str) -> ToolError:
    return ToolError("invalid_input", f"{path}: {message}")


def _too_large(path: str, size: int, limit: int, unit: str) -> ToolError:
    return ToolError("too_large", f"{path}: {size} {unit}, more than the limit of {limit} {unit}")


# --------------------------------------------------------------------------- #
# Argument validation (no third-party schema validator: the core has no dependencies)
# --------------------------------------------------------------------------- #


def _object(path: str, value: Any, *, required: Sequence[str], optional: Sequence[str] = ()) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid(path, f"must be a JSON object, got {_json_type(value)}")
    allowed = set(required) | set(optional)
    unknown = sorted(str(k) for k in value if k not in allowed)
    if unknown:
        raise _invalid(path, f"unknown field(s) {unknown}; allowed: {sorted(allowed)}")
    missing = [k for k in required if k not in value]
    if missing:
        raise _invalid(path, f"missing required field(s) {missing}")
    return value


def _string(path: str, value: Any, limit: int) -> str:
    if not isinstance(value, str):
        raise _invalid(path, f"must be a string, got {_json_type(value)}")
    if not value.strip():
        raise _invalid(path, "must not be empty")
    if len(value) > limit:
        raise _too_large(path, len(value), limit, "characters")
    return value


def _string_list(path: str, value: Any, limits: Limits) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _invalid(path, f"must be a list of strings, got {_json_type(value)}")
    if len(value) > limits.max_list_items:
        raise _too_large(path, len(value), limits.max_list_items, "items")
    return tuple(_string(f"{path}[{i}]", v, limits.max_term_chars) for i, v in enumerate(value))


def _list(path: str, value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list):
        raise _invalid(path, f"must be a list, got {_json_type(value)}")
    if len(value) > limit:
        raise _too_large(path, len(value), limit, "items")
    return value


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def parse_documents(value: Any, limits: Limits) -> tuple[Document, ...]:
    """Validate ``documents`` and build the contracts' :class:`Document` (hash computed from the text)."""
    items = _list("documents", value, limits.max_documents)
    if not items:
        raise _invalid("documents", "must contain at least one document (the one the quote is copied from)")
    documents: list[Document] = []
    seen: set[str] = set()
    total = 0
    for i, raw in enumerate(items):
        path = f"documents[{i}]"
        obj = _object(path, raw, required=("id", "text"), optional=("url",))
        doc_id = _string(f"{path}.id", obj["id"], limits.max_id_chars)
        if doc_id in seen:
            raise _invalid(f"{path}.id", f"duplicate document id {doc_id!r}; ids must be unique")
        seen.add(doc_id)
        text = _string(f"{path}.text", obj["text"], limits.max_document_chars)
        total += len(text)
        if total > limits.max_total_document_chars:
            raise _too_large("documents (all texts together)", total, limits.max_total_document_chars, "characters")
        url = _string(f"{path}.url", obj["url"], limits.max_url_chars) if "url" in obj else None
        documents.append(Document.create(doc_id, text, url=url))
    return tuple(documents)


def parse_constraints(value: Any, question: str, limits: Limits) -> QuestionConstraints:
    """Validate explicit constraints (decomposer output format without the question)."""
    obj = _object("constraints", value, required=("entity", "attribute"), optional=("qualifiers", "presuppositions"))
    ent = _object("constraints.entity", obj["entity"], required=("name",), optional=("aliases", "kind"))
    kind = ent.get("kind")
    entity = {
        "name": _string("constraints.entity.name", ent["name"], limits.max_term_chars),
        "aliases": _string_list("constraints.entity.aliases", ent.get("aliases", []), limits),
        "kind": None if kind is None else _string("constraints.entity.kind", kind, limits.max_term_chars),
    }
    attribute = _string("constraints.attribute", obj["attribute"], limits.max_term_chars)
    kinds = [k.value for k in QualifierKind]
    qualifiers = []
    for i, raw in enumerate(_list("constraints.qualifiers", obj.get("qualifiers", []), limits.max_list_items)):
        path = f"constraints.qualifiers[{i}]"
        q = _object(path, raw, required=("kind", "text"), optional=("aliases",))
        if q["kind"] not in kinds:
            raise _invalid(f"{path}.kind", f"must be one of {kinds}, got {q['kind']!r}")
        qualifiers.append(
            {
                "kind": q["kind"],
                "text": _string(f"{path}.text", q["text"], limits.max_term_chars),
                "aliases": _string_list(f"{path}.aliases", q.get("aliases", []), limits),
            }
        )
    presuppositions = []
    for i, raw in enumerate(_list("constraints.presuppositions", obj.get("presuppositions", []), limits.max_list_items)):
        path = f"constraints.presuppositions[{i}]"
        p = _object(path, raw, required=("text",), optional=("key_terms",))
        presuppositions.append(
            {
                "text": _string(f"{path}.text", p["text"], limits.max_question_chars),
                "key_terms": _string_list(f"{path}.key_terms", p.get("key_terms", []), limits),
            }
        )
    try:
        return QuestionConstraints(
            question=question,
            entity=entity,
            attribute=attribute,
            qualifiers=qualifiers,
            presuppositions=presuppositions,
        )
    except ContractError as exc:  # pragma: no cover - everything above is checked first
        raise _invalid("constraints", str(exc)) from exc


def parse_checks(value: Any) -> tuple[str, ...]:
    """Validate ``checks``: a non-empty set of reserved check names, returned in canonical order."""
    if value is None:
        return ALL_CHECKS
    items = _list("checks", value, len(ALL_CHECKS))
    if not items:
        raise _invalid("checks", "must list at least one check; omit the field to run all of them")
    for i, name in enumerate(items):
        if not isinstance(name, str) or name not in ALL_CHECKS:
            raise _invalid(f"checks[{i}]", f"must be one of {list(ALL_CHECKS)}, got {name!r}")
    duplicates = sorted({n for n in items if items.count(n) > 1})
    if duplicates:
        raise _invalid("checks", f"lists a check more than once: {duplicates}")
    return tuple(name for name in ALL_CHECKS if name in items)


def parse_parameters(value: Any) -> dict[str, Any]:
    """Validate ``parameters`` overrides (unset ones keep the PROPOSED defaults of the gate)."""
    if value is None:
        return {}
    obj = _object("parameters", value, required=(), optional=tuple(DEFAULT_PARAMETERS))
    overrides: dict[str, Any] = {}
    for name in DEFAULT_PARAMETERS:
        if name not in obj:
            continue
        item = obj[name]
        path = f"parameters.{name}"
        if name in schemas.PARAMETER_ENUMS:
            allowed = list(schemas.PARAMETER_ENUMS[name])
            if item not in allowed:
                raise _invalid(path, f"must be one of {allowed}, got {item!r}")
        elif name in schemas.PARAMETER_BOOLEANS:
            if not isinstance(item, bool):
                raise _invalid(path, f"must be true or false, got {item!r}")
        else:
            minimum = schemas.PARAMETER_MINIMUMS[name]
            if isinstance(item, bool) or not isinstance(item, int) or item < minimum:
                raise _invalid(path, f"must be an integer >= {minimum}, got {item!r}")
        overrides[name] = item
    try:  # PR 18: binding_scope and a contradicting scope option
        resolve_scope(overrides)
    except ValueError as exc:
        raise _invalid("parameters", str(exc)) from None
    return overrides


def constraints_output(constraints: QuestionConstraints) -> dict[str, Any]:
    """Constraints in the input format of verify_answer (the question itself is left out)."""
    data = constraints.to_dict()
    data.pop("question")
    return data


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


class GateService:
    """The tools of the MCP server as plain methods. Holds the server configuration and the decomposer.

    ``decomposer_factory`` exists for tests; by default the decomposer named in
    the configuration is built through the registry (which makes no network
    call; the model is first called by ``decompose``).
    """

    def __init__(self, config: ServerConfig | None = None, *, decomposer_factory: Callable[[], Any] | None = None):
        self.config = config or ServerConfig()
        self.limits = self.config.limits
        self._decomposer: Any = None
        if self.config.decomposer is not None:
            spec = self.config.decomposer
            factory = decomposer_factory or (lambda: registry.create("decomposer", spec["name"], **spec["options"]))
            self._decomposer = factory()

    # -- metadata ------------------------------------------------------------ #

    @property
    def decomposer_configured(self) -> bool:
        return self._decomposer is not None

    def tool_definitions(self) -> list[dict[str, Any]]:
        return schemas.tool_definitions(self.limits, decomposer_configured=self.decomposer_configured)

    def decomposer_api_key_name(self) -> str | None:
        """Name (never value) of the environment variable the decomposer's model client reads, if any."""
        client = getattr(self._decomposer, "client", None)
        name = getattr(client, "secret_name", "") or ""
        return name or None

    # -- dispatch ------------------------------------------------------------ #

    def call_tool(self, name: str, arguments: Any) -> ToolResult:
        """Run tool ``name``. Never raises: every failure becomes an error result with a code and a message."""
        handlers = {schemas.VERIFY_ANSWER: self.verify_answer, schemas.DECOMPOSE_QUESTION: self.decompose_question}
        try:
            handler = handlers.get(name)
            if handler is None:
                raise ToolError("unknown_tool", f"unknown tool {name!r}; available: {list(schemas.TOOL_NAMES)}")
            return ToolResult(handler({} if arguments is None else arguments))
        except ToolError as exc:
            return ToolResult(exc.payload(), is_error=True)
        except Exception as exc:  # noqa: BLE001 - a tool call must not take the server down
            return ToolResult(
                ToolError("internal_error", f"{type(exc).__name__}: {exc}").payload(),
                is_error=True,
            )

    # -- verify_answer -------------------------------------------------------- #

    def verify_answer(self, arguments: Any) -> dict[str, Any]:
        limits = self.limits
        args = _object(
            "arguments",
            arguments,
            required=("question", "answer", "quote", "source_id", "documents"),
            optional=("constraints", "checks", "parameters"),
        )
        question = _string("question", args["question"], limits.max_question_chars)
        value = _string("answer", args["answer"], limits.max_question_chars)
        if isinstance(args["quote"], str) and not args["quote"].strip():
            raise _invalid(
                "quote",
                "a non-empty verbatim quote is required. Under the study's protocol an answer without a "
                "quote cannot be accepted (it is 'unparseable', section 6.2): copy one continuous passage "
                "from the cited document, or abstain.",
            )
        quote = _string("quote", args["quote"], limits.max_question_chars)
        source_id = _string("source_id", args["source_id"], limits.max_id_chars)
        documents = parse_documents(args["documents"], limits)
        checks = parse_checks(args.get("checks"))
        overrides = parse_parameters(args.get("parameters"))

        decomposition: Decomposition | None = None
        if "constraints" in args and args["constraints"] is not None:
            constraints = parse_constraints(args["constraints"], question, limits)
        elif self._decomposer is None:
            raise ToolError("constraints_required", DECOMPOSER_NOT_CONFIGURED)
        else:
            decomposition = self._decompose(question)
            constraints = decomposition.constraints

        answer = AnswerCandidate.answered(
            value=value,
            quote=quote,
            source_id=source_id,
            raw=canonical_json({"quote": quote, "source_id": source_id, "status": "answered", "value": value}),
        )
        gate = registry.create("gate", GATE_NAME, checks=list(checks), **overrides)
        verdict: Verdict = gate.evaluate(constraints, answer, documents)

        output: dict[str, Any] = {
            "verdict": verdict.decision.value.upper(),
            "reasons": list(verdict.reasons),
            "checks": [result.to_dict() for result in verdict.check_results],
            "research_request": (
                json.loads(verdict.research_request) if verdict.research_request is not None else None
            ),
            "verdict_sha256": sha256_text(verdict.to_json()),
            "constraints": constraints_output(constraints),
            "constraints_source": "explicit" if decomposition is None else "decomposer",
            "documents": [d.ref().to_dict() for d in documents],
            "gate": {
                "name": GATE_NAME,
                "checks": list(gate.checks),
                "checks_are_default": tuple(gate.checks) == ALL_CHECKS,
                "parameters": dict(gate.parameters),
                "parameters_are_default": dict(gate.parameters) == DEFAULT_PARAMETERS,
                "parameters_status": PARAMETERS_STATUS,
            },
            "notice": NOTICE,
            "binding_gate_version": __version__,
            "schema_version": SCHEMA_VERSION,
        }
        if decomposition is not None:
            output["decomposition"] = self._decomposition_output(decomposition)
        return output

    # -- decompose_question ---------------------------------------------------- #

    def decompose_question(self, arguments: Any) -> dict[str, Any]:
        args = _object("arguments", arguments, required=("question",))
        question = _string("question", args["question"], self.limits.max_question_chars)
        if self._decomposer is None:
            raise ToolError("decomposer_not_configured", DECOMPOSER_NOT_CONFIGURED)
        decomposition = self._decompose(question)
        return {
            "constraints": constraints_output(decomposition.constraints),
            "decomposition": self._decomposition_output(decomposition),
            "notice": "Produced by a language model: not deterministic. Review the constraints before passing "
            "them to verify_answer. " + NOTICE,
            "binding_gate_version": __version__,
            "schema_version": SCHEMA_VERSION,
        }

    # -- helpers --------------------------------------------------------------- #

    def _decompose(self, question: str) -> Decomposition:
        key = self.decomposer_api_key_name()
        if key is not None:
            try:
                present = secrets.has_secret(key)
            except secrets.SecretError as exc:
                raise ToolError("api_key_missing", str(exc)) from exc
            if not present:
                raise ToolError(
                    "api_key_missing",
                    f"the configured decomposer needs the environment variable {key} (an API key), which is not "
                    "set in the server's environment. Pass explicit 'constraints' to verify_answer instead.",
                )
        try:
            decomposition = self._decomposer.decompose(question)
        except Exception as exc:  # noqa: BLE001 - reported to the caller with the logged calls
            calls = [call.to_dict() for call in getattr(exc, "model_calls", ())]
            raise ToolError(
                "decomposition_failed",
                f"the decomposer failed: {type(exc).__name__}: {exc}. Pass explicit 'constraints' instead.",
                extra={"model_calls": calls},
            ) from exc
        if not isinstance(decomposition, Decomposition):
            raise ToolError(
                "decomposition_failed", f"the decomposer returned {type(decomposition).__name__}, not a Decomposition"
            )
        return decomposition

    def _decomposition_output(self, decomposition: Decomposition) -> dict[str, Any]:
        describe = getattr(self._decomposer, "describe", None)
        return {
            "decomposer": describe() if callable(describe) else {"name": getattr(self._decomposer, "name", None)},
            "model_calls": [call.to_dict() for call in decomposition.model_calls],
        }
