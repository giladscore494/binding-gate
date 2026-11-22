"""Tool definitions of the MCP server: names, descriptions (written for an agent) and JSON input schemas.

Plain dictionaries, no SDK. ``server.py`` turns them into the SDK's ``Tool``
objects; ``tests/test_mcp_core.py`` compares them with a committed snapshot, so
any change to what an agent sees is a visible diff.

Where the values come from (nothing is duplicated):

* check names and their order: ``binding_gate.gates.binding.ALL_CHECKS``;
* default gate parameters: ``binding_gate.gates.binding.DEFAULT_PARAMETERS``
  (the PROPOSED values of PREREGISTRATION.md section 5.1);
* allowed values of ``premise_scope`` and ``competitor_tie``: the check modules;
* qualifier kinds: ``binding_gate.contracts.QualifierKind``;
* size limits: the server configuration (``binding_gate.mcp.config.Limits``).

The JSON schemas describe the input for the client. The server does not rely on
the client having validated anything: ``core.py`` validates every argument
itself and returns a clear error (the low-level MCP server does not validate
tool arguments against the schema).
"""

from __future__ import annotations

from typing import Any

from binding_gate.checks.no_closer_competitor import TIE_POLICIES
from binding_gate.checks.premise_present import SCOPES
from binding_gate.contracts import QualifierKind
from binding_gate.gates.binding import ALL_CHECKS, DEFAULT_PARAMETERS
from binding_gate.mcp.config import Limits

VERIFY_ANSWER = "verify_answer"
DECOMPOSE_QUESTION = "decompose_question"
TOOL_NAMES: tuple[str, ...] = (VERIFY_ANSWER, DECOMPOSE_QUESTION)

PARAMETER_MINIMUMS: dict[str, int] = {
    "window_tokens": 0,
    "min_quote_tokens": 1,
    "lead_max_tokens": 1,
    "lead_min_repeats": 1,
}
"""Smallest value of each integer gate parameter, as enforced by the check constructors."""

PARAMETER_ENUMS: dict[str, tuple[str, ...]] = {
    "premise_scope": tuple(SCOPES),
    "competitor_tie": tuple(TIE_POLICIES),
}
"""Allowed values of each string gate parameter, taken from the check modules."""

PARAMETER_DESCRIPTIONS: dict[str, str] = {
    "window_tokens": "Largest number of tokens allowed between the quote and a mention of the entity, "
    "a qualifier or a competing entity.",
    "min_quote_tokens": "Smallest number of whitespace-separated tokens a quote must have.",
    "lead_max_tokens": "Longest lead term (in tokens) that counts as the subject of a document "
    "when looking for competing entities.",
    "lead_min_repeats": "How often a lead term must occur in its document to count as a competing entity.",
    "premise_scope": "Where presupposition key terms are looked for: the whole cited document, "
    "or only the window around the quote.",
    "competitor_tie": "Whether a competing entity exactly as close to the quote as the question's "
    "entity blocks the answer ('fail') or not ('pass').",
}

_typed = set(PARAMETER_MINIMUMS) | set(PARAMETER_ENUMS)
if set(PARAMETER_DESCRIPTIONS) != set(DEFAULT_PARAMETERS) or _typed != set(DEFAULT_PARAMETERS):  # pragma: no cover
    # A gate parameter was added or removed without updating this module.
    raise RuntimeError("binding_gate.mcp.schemas is out of date with gates.binding.DEFAULT_PARAMETERS")


def _string(max_length: int, description: str) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": max_length, "description": description}


def _string_list(limits: Limits, description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "maxItems": limits.max_list_items,
        "items": {"type": "string", "minLength": 1, "maxLength": limits.max_term_chars},
        "description": description,
    }


def parameter_schema() -> dict[str, Any]:
    """JSON schema of the optional ``parameters`` object; defaults are the PROPOSED values."""
    properties: dict[str, Any] = {}
    for name, default in DEFAULT_PARAMETERS.items():
        if name in PARAMETER_ENUMS:
            prop: dict[str, Any] = {"type": "string", "enum": list(PARAMETER_ENUMS[name])}
        else:
            prop = {"type": "integer", "minimum": PARAMETER_MINIMUMS[name]}
        prop["default"] = default
        prop["description"] = PARAMETER_DESCRIPTIONS[name]
        properties[name] = prop
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "description": "Gate parameters. Omit to use the defaults, which are the PROPOSED values of the "
        "study's pre-registration (section 5.1; not yet frozen). Any value you set is echoed in the output.",
    }


def constraints_schema(limits: Limits) -> dict[str, Any]:
    """JSON schema of explicit question constraints (the decomposer's output format, without the question)."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["entity", "attribute"],
        "description": "What acceptable evidence must be about. Pass this to run the gate fully "
        "deterministically, without calling any model. Same format as the output of decompose_question.",
        "properties": {
            "entity": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "description": "The entity the question asks about.",
                "properties": {
                    "name": _string(limits.max_term_chars, "Its name as it would appear in a source, e.g. 'Lisinopril'."),
                    "aliases": _string_list(
                        limits, "Other names that count as a mention of the same entity (brand names, abbreviations)."
                    ),
                    "kind": {
                        "type": ["string", "null"],
                        "maxLength": limits.max_term_chars,
                        "description": "Optional free-text type, e.g. 'drug'. Not used by the checks.",
                    },
                },
            },
            "attribute": _string(
                limits.max_term_chars,
                "What is asked about the entity, e.g. 'maximum daily dose'. Used only in the research request.",
            ),
            "qualifiers": {
                "type": "array",
                "maxItems": limits.max_list_items,
                "description": "Conditions the evidence must also mention near the quote (time, place, version, unit, other).",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "text"],
                    "properties": {
                        "kind": {"type": "string", "enum": [k.value for k in QualifierKind]},
                        "text": _string(limits.max_term_chars, "The condition as it would appear in a source, e.g. '2023'."),
                        "aliases": _string_list(limits, "Other surface forms of the same condition."),
                    },
                },
            },
            "presuppositions": {
                "type": "array",
                "maxItems": limits.max_list_items,
                "description": "What the question takes for granted. Every key term must appear in the evidence.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text"],
                    "properties": {
                        "text": _string(limits.max_question_chars, "The premise in words."),
                        "key_terms": _string_list(limits, "Words whose presence in the evidence the premise check looks for."),
                    },
                },
            },
        },
    }


VERIFY_DESCRIPTION = (
    "Check whether an answer you found in a source is bound to the question: is the quote copied verbatim "
    "from the document you cite, and is that passage about the entity and the conditions (time, place, "
    "version, unit) the question asks about, rather than about a neighbouring entity? Call it before you "
    "state an answer taken from a web page or document. Pass the documents you actually read, unchanged. "
    "The verdict is computed by deterministic string rules (no model, no network) when you pass "
    "'constraints'.\n"
    "Verdicts: ACCEPT - you may give the answer and cite the quote. RESEARCH - the quote exists, but the "
    "passage is not clearly about the asked entity or conditions (or another entity is closer to it); look "
    "for a better source, using research_request.query, and verify again. ABSTAIN - the quote is not in the "
    "cited document, is too short, or the evidence lacks what the question presupposes; do not give this "
    "answer: copy the quote exactly, or say you could not verify it.\n"
    "Limits: the gate checks binding, not truth. An ACCEPT means the evidence is about the right thing, not "
    "that the source is correct or that the value was read correctly."
)

DECOMPOSE_DESCRIPTION = (
    "Turn a question into the constraints that verify_answer checks: the entity, the asked attribute, "
    "qualifiers and presuppositions. Uses a language model configured by the server operator, so it is "
    "not deterministic and its API cost is paid by the operator's key. Review the result and pass it to "
    "verify_answer as 'constraints'. If no decomposer is configured, this tool returns an error: write the "
    "constraints yourself and pass them to verify_answer."
)


def verify_answer_tool(limits: Limits, *, decomposer_configured: bool) -> dict[str, Any]:
    constraints_note = (
        "Optional: if omitted, the server's decomposer model derives them (not deterministic, costs money)."
        if decomposer_configured
        else "Required on this server (no decomposer model is configured)."
    )
    constraints = constraints_schema(limits)
    constraints["description"] = constraints["description"] + " " + constraints_note
    return {
        "name": VERIFY_ANSWER,
        "title": "Verify that an answer's evidence is about the question",
        "description": VERIFY_DESCRIPTION,
        "annotations": {
            "title": "Verify answer binding",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": decomposer_configured,
        },
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["question", "answer", "quote", "source_id", "documents"],
            "properties": {
                "question": _string(limits.max_question_chars, "The question exactly as it was asked."),
                "answer": _string(limits.max_question_chars, "The answer value you intend to give."),
                "quote": _string(
                    limits.max_question_chars,
                    "One continuous passage copied verbatim from the cited document that supports the answer "
                    "(at least min_quote_tokens words).",
                ),
                "source_id": _string(limits.max_id_chars, "The id of the document the quote is copied from."),
                "documents": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": limits.max_documents,
                    "description": "Every document you read for this answer, with its full text as you saw it. "
                    "Other documents matter: their subjects are the competing entities. Ids must be unique. "
                    f"At most {limits.max_document_chars} characters per text and "
                    f"{limits.max_total_document_chars} in total.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["id", "text"],
                        "properties": {
                            "id": _string(limits.max_id_chars, "A short id you choose, e.g. 'doc1'."),
                            "text": _string(limits.max_document_chars, "The document text."),
                            "url": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": limits.max_url_chars,
                                "description": "Optional; recorded only, never fetched.",
                            },
                        },
                    },
                },
                "constraints": constraints,
                "checks": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "enum": list(ALL_CHECKS)},
                    "default": list(ALL_CHECKS),
                    "description": "Which checks to run (default: all five). Leaving one out is an ablation; "
                    "they always run in the canonical order shown in the enum.",
                },
                "parameters": parameter_schema(),
            },
        },
    }


def decompose_question_tool(limits: Limits, *, decomposer_configured: bool) -> dict[str, Any]:
    status = (
        "A decomposer is configured on this server."
        if decomposer_configured
        else "No decomposer is configured on this server: calling this tool returns an explanation only."
    )
    return {
        "name": DECOMPOSE_QUESTION,
        "title": "Decompose a question into constraints (uses a model)",
        "description": DECOMPOSE_DESCRIPTION + " " + status,
        "annotations": {
            "title": "Decompose question",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": decomposer_configured,
        },
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["question"],
            "properties": {
                "question": _string(limits.max_question_chars, "The question exactly as it was asked."),
            },
        },
    }


def tool_definitions(limits: Limits, *, decomposer_configured: bool) -> list[dict[str, Any]]:
    """All tool definitions, in the order of :data:`TOOL_NAMES`."""
    return [
        verify_answer_tool(limits, decomposer_configured=decomposer_configured),
        decompose_question_tool(limits, decomposer_configured=decomposer_configured),
    ]
