"""The decomposer's fixed prompt and its strict, non-repairing parser."""

from __future__ import annotations

import json

import pytest

from binding_gate.contracts import QualifierKind
from binding_gate.decomposers import decomposition_format as fmt

QUESTION = "What was the boiling point of Fluxol at sea level in 1990, in kelvin?"

PROMPT_V1_SHA256 = "9e655050a2eaac39615ba4b2e9f974730757ba84d9e396305a98adf79084af90"
"""SHA-256 of prompt version 1, pinned so that any edit of the prompt wording is a visible change."""


def valid() -> dict:
    return {
        "entity": {"name": "Fluxol", "aliases": ["FX-1"], "kind": "chemical"},
        "attribute": "boiling point",
        "qualifiers": [
            {"kind": "place", "text": "at sea level", "aliases": []},
            {"kind": "time", "text": "1990", "aliases": []},
            {"kind": "unit", "text": "kelvin", "aliases": ["K"]},
        ],
        "presuppositions": [{"text": "Fluxol has a boiling point.", "key_terms": ["Fluxol"]}],
    }


def parse(data) -> fmt.DecompositionParse:
    return fmt.parse_decomposition(data if isinstance(data, str) else json.dumps(data), QUESTION)


# --- prompt and request ---------------------------------------------------------------------------- #


def test_request_is_the_fixed_prompt_plus_the_question():
    request = fmt.build_decompose_request(QUESTION)
    assert [m.role for m in request.messages] == ["system", "user"]
    assert request.messages[0].content == fmt.DECOMPOSER_SYSTEM_PROMPT
    assert request.messages[1].content == "Question: " + QUESTION
    assert request.params == {}
    assert fmt.build_decompose_request(QUESTION) == request  # deterministic
    with pytest.raises(ValueError):
        fmt.build_decompose_request("   ")
    with pytest.raises(ValueError, match="unknown decomposer prompt version"):
        fmt.build_decompose_request(QUESTION, version=99)


def test_prompt_is_pinned_and_generic():
    assert fmt.PROMPT_VERSION == 1 and fmt.PROMPTS == {1: fmt.DECOMPOSER_SYSTEM_PROMPT}
    assert fmt.prompt_sha256() == PROMPT_V1_SHA256, "the v1 prompt changed: add a new version instead"
    lowered = fmt.DECOMPOSER_SYSTEM_PROMPT.casefold()
    for word in ("deceptive", "cx_doc", "cy_doc", "competitor document", "benchmark"):
        assert word not in lowered
    for kind in QualifierKind:
        assert f'"{kind.value}"' in fmt.DECOMPOSER_SYSTEM_PROMPT
    # The worked example in the prompt is itself valid output.
    example_question = "How much memory did the Zentrix Z5 laptop ship with in its 2023 edition, in gigabytes?"
    example = fmt.DECOMPOSER_SYSTEM_PROMPT.rsplit("\n", 1)[1]
    assert fmt.parse_decomposition(example, example_question).parsed


# --- parser: accepted ------------------------------------------------------------------------------ #


def test_valid_reply_becomes_constraints_with_the_asked_question():
    result = parse(valid())
    assert result.parsed and result.reason is None
    c = result.constraints
    assert c.question == QUESTION
    assert (c.entity.name, c.entity.aliases, c.entity.kind) == ("Fluxol", ("FX-1",), "chemical")
    assert [q.kind for q in c.qualifiers] == [QualifierKind.PLACE, QualifierKind.TIME, QualifierKind.UNIT]
    assert c.presuppositions[0].key_terms == ("Fluxol",)


def test_code_fence_outer_whitespace_null_kind_empty_lists_and_kind_case_are_accepted():
    data = valid()
    data["entity"]["kind"] = None
    data["qualifiers"] = [{"kind": " Time ", "text": "  1990 ", "aliases": []}]
    data["presuppositions"] = []
    for text in (json.dumps(data), f"```json\n{json.dumps(data)}\n```", f"\n```\n{json.dumps(data)}\n```  \n"):
        result = parse(text)
        assert result.parsed, result.reason
        assert result.constraints.entity.kind is None
        assert result.constraints.qualifiers[0].kind is QualifierKind.TIME
        assert result.constraints.qualifiers[0].text == "1990"  # outer whitespace trimmed only
        assert result.constraints.presuppositions == ()


# --- parser: rejected, never repaired ---------------------------------------------------------------- #


def _without(path: list, key: str) -> dict:
    data = valid()
    target = data
    for step in path:
        target = target[step]
    del target[key]
    return data


def _with(path: list, key: str, value) -> dict:
    data = valid()
    target = data
    for step in path:
        target = target[step]
    target[key] = value
    return data


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "empty output"),
        ("   \n", "empty output"),
        ("The entity is Fluxol.", "not exactly one JSON value"),
        ("Here it is: " + json.dumps(valid()), "not exactly one JSON value"),
        (json.dumps(valid()) + " Hope this helps.", "not exactly one JSON value"),
        (json.dumps(valid()) + json.dumps(valid()), "not exactly one JSON value"),
        ("[" + json.dumps(valid()) + "]", "must be a JSON object"),
        ('{"entity": {"name": "Fluxol"', "not exactly one JSON value"),
        ("```json\n" + json.dumps(valid()), "not closed"),
        ("```python\n" + json.dumps(valid()) + "\n```", "must open with"),
        (_without([], "attribute"), "missing key(s) ['attribute']"),
        (_with([], "confidence", 0.9), "unexpected key(s) ['confidence']"),
        (_without(["entity"], "aliases"), "entity is missing key(s) ['aliases']"),
        (_with(["entity"], "name", "  "), "entity.name must not be empty"),
        (_with(["entity"], "name", 7), "entity.name must be a string"),
        (_with(["entity"], "aliases", "FX-1"), "entity.aliases must be a JSON array"),
        (_with(["entity"], "aliases", ["FX-1", ""]), "entity.aliases[1] must not be empty"),
        (_with(["entity"], "kind", ""), "entity.kind must not be empty"),
        (_with([], "qualifiers", {"kind": "time"}), "qualifiers must be a JSON array"),
        (_with(["qualifiers", 0], "kind", "location"), "qualifiers[0].kind must be one of"),
        (_with(["qualifiers", 1], "extra", 1), "qualifiers[1] has unexpected key(s)"),
        (_without(["presuppositions", 0], "key_terms"), "presuppositions[0] is missing key(s)"),
        (_with(["presuppositions", 0], "key_terms", [None]), "key_terms[0] must be a string"),
        (_with([], "attribute", ["boiling point"]), "attribute must be a string"),
    ],
)
def test_invalid_replies_are_unparseable_with_a_reason(raw, reason):
    result = parse(raw)
    assert result.status == fmt.STATUS_UNPARSEABLE and result.constraints is None
    assert reason in result.reason


def test_duplicate_keys_are_rejected_instead_of_silently_keeping_one():
    text = json.dumps(valid())[:-1] + ', "attribute": "melting point"}'
    result = parse(text)
    assert not result.parsed and "duplicate key(s) ['attribute']" in result.reason


def test_parser_is_pure_and_checks_its_arguments():
    raw = json.dumps(valid())
    assert parse(raw) == parse(raw)
    with pytest.raises(TypeError):
        fmt.parse_decomposition(None, QUESTION)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        fmt.parse_decomposition(raw, " ")
