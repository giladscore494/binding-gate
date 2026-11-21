"""The answer parser: pure, strict, never repairing (PREREGISTRATION.md 6.2, 6.4)."""

from __future__ import annotations

import json

import pytest

from binding_gate.contracts import AnswerStatus
from binding_gate.parsing import ANSWER_FORMAT_INSTRUCTIONS, parse_answer

ANSWERED = {"status": "answered", "value": "10 mg", "quote": "the starting dose was 10 mg daily", "source_id": "cx_doc"}


def test_plain_answer_object():
    raw = json.dumps(ANSWERED)
    candidate = parse_answer(raw)
    assert candidate.status is AnswerStatus.ANSWERED
    assert (candidate.value, candidate.quote, candidate.source_id) == (
        "10 mg",
        "the starting dose was 10 mg daily",
        "cx_doc",
    )
    assert candidate.raw == raw


def test_answer_inside_code_fence_and_prose_is_found_and_raw_is_kept_unmodified():
    raw = "Here is my answer:\n```json\n" + json.dumps(ANSWERED, indent=2) + "\n```\nThanks."
    candidate = parse_answer(raw)
    assert candidate.status is AnswerStatus.ANSWERED
    assert candidate.raw == raw


def test_outer_whitespace_of_fields_is_trimmed_and_nothing_else():
    candidate = parse_answer(json.dumps({**ANSWERED, "quote": "  The Dose   was 10 mg  "}))
    assert candidate.quote == "The Dose   was 10 mg"


def test_status_is_case_insensitive_and_extra_keys_are_ignored():
    candidate = parse_answer(json.dumps({**ANSWERED, "status": " Answered ", "confidence": 0.9}))
    assert candidate.status is AnswerStatus.ANSWERED


@pytest.mark.parametrize(
    "raw",
    [
        '{"status": "abstained", "reason": "the documents are about another drug"}',
        '{"status": "abstained"}',
        '{"status": "abstained", "value": null, "quote": "", "source_id": null, "reason": "  "}',
    ],
)
def test_abstentions(raw):
    candidate = parse_answer(raw)
    assert candidate.status is AnswerStatus.ABSTAINED
    assert candidate.value is None and candidate.quote is None and candidate.source_id is None


def test_abstention_reason_is_kept():
    assert parse_answer('{"status": "abstained", "reason": " no evidence "}').reason == "no evidence"


@pytest.mark.parametrize(
    "raw,reason",
    [
        ("", "empty output"),
        ("   \n", "empty output"),
        ("The literature says it works [1].", "no JSON object"),
        ('{"value": "10 mg", "quote": "q", "source_id": "cx_doc"}', "no JSON object"),
        ('{"status": "answered", "value": "10 mg", "quote": "a b c d e"}', "source_id is missing"),
        ('{"status": "answered", "value": "10 mg", "source_id": "cx_doc"}', "quote is missing"),
        ('{"status": "answered", "quote": "a b c d e", "source_id": "cx_doc"}', "value is missing"),
        ('{"status": "answered", "value": " ", "quote": "a b c d e", "source_id": "cx_doc"}', "value is empty"),
        ('{"status": "answered", "value": 10, "quote": "a b c d e", "source_id": "cx_doc"}', "value is not a string"),
        ('{"status": "answered", "value": "v", "quote": ["a"], "source_id": "cx_doc"}', "quote is not a string"),
        ('{"status": "maybe", "value": "v", "quote": "q", "source_id": "s"}', "neither"),
        ('{"status": true}', "neither"),
        ('{"status": "abstained", "value": "10 mg"}', "abstained but carries value"),
        ('{"status": "answered", "value": "v", "quote": "q", "source_id": "s",}', "no JSON object"),
        ("{'status': 'abstained'}", "no JSON object"),
        ('{"answer": {"status": "abstained"}}', "no JSON object"),
        ('[{"status": "abstained"}', "several different") ,
    ],
)
def test_unparseable_outputs(raw, reason):
    if raw.startswith("[{"):
        raw = '{"status": "abstained"} or {"status": "answered", "value": "v", "quote": "q", "source_id": "s"}'
    candidate = parse_answer(raw)
    assert candidate.status is AnswerStatus.UNPARSEABLE
    assert reason in candidate.reason
    assert candidate.raw == raw
    assert candidate.value is None and candidate.quote is None and candidate.source_id is None


def test_identical_repeated_objects_are_one_answer():
    raw = json.dumps(ANSWERED) + "\n\nFinal answer again:\n" + json.dumps(ANSWERED)
    assert parse_answer(raw).status is AnswerStatus.ANSWERED


def test_braces_in_prose_before_the_object_do_not_break_parsing():
    raw = "Using set notation {X, Y} loosely: " + json.dumps(ANSWERED)
    assert parse_answer(raw).status is AnswerStatus.ANSWERED


def test_parser_is_a_pure_function():
    raw = json.dumps(ANSWERED)
    assert parse_answer(raw) == parse_answer(raw)
    with pytest.raises(TypeError):
        parse_answer(None)  # type: ignore[arg-type]


def test_instructions_describe_exactly_the_parsed_structure():
    for token in ('"status": "answered"', '"status": "abstained"', '"value"', '"quote"', '"source_id"', "at least five words"):
        assert token in ANSWER_FORMAT_INSTRUCTIONS
    # The instruction must not hint at a particular document: no example source label.
    assert "cx_doc" not in ANSWER_FORMAT_INSTRUCTIONS and "cy_doc" not in ANSWER_FORMAT_INSTRUCTIONS
