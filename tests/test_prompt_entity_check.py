"""Arm prompt_entity_check: the no_gate prompt plus one fixed instruction, its own model call."""

from __future__ import annotations

import pr5_fakes as f
from binding_gate import registry
from binding_gate.answering import build_answer_request
from binding_gate.arms.prompt_entity_check import (
    ENTITY_CHECK_INSTRUCTION,
    PromptEntityCheckArm,
    build_entity_check_request,
)
from binding_gate.contracts import AnswerStatus
from binding_gate.parsing import ANSWER_FORMAT_INSTRUCTIONS


def test_request_is_the_no_gate_request_plus_the_instruction_at_the_end():
    arm_input = f.dg_arm_input()
    base = build_answer_request(arm_input)
    request = build_entity_check_request(arm_input)
    assert request.messages[:-1] == base.messages[:-1]  # same system prompt
    assert request.messages[-1].content == base.messages[-1].content + "\n\n" + ENTITY_CHECK_INSTRUCTION
    assert request.params == base.params
    # The answer-format instruction (section 6.2) is still there, before the new instruction.
    user = request.messages[-1].content
    assert user.index(ANSWER_FORMAT_INSTRUCTIONS) < user.index(ENTITY_CHECK_INSTRUCTION)


def test_instruction_is_generic_and_asks_for_the_required_abstention_structure():
    text = ENTITY_CHECK_INSTRUCTION
    assert '"status": "abstained"' in text
    for leak in ("cx_doc", "cy_doc", "xanadrug", "competitor", "target"):
        assert leak not in text.lower()


def test_arm_makes_its_own_call_and_parses_it_with_the_shared_parser():
    model = f.FamilyFake(label="m", behaviour="target")
    arm = registry.create("arm", "prompt_entity_check", model=model)
    assert isinstance(arm, PromptEntityCheckArm)
    output = arm.run(f.dg_arm_input())
    (call,) = output.model_calls
    assert call.purpose == "answer" and ENTITY_CHECK_INSTRUCTION in call.request.messages[-1].content
    assert output.final_answer.status is AnswerStatus.ANSWERED and output.final_answer.source_id == "cx_doc"
    assert output.attempts[0].verdict is None  # no external check


def test_unparseable_output_is_kept():
    output = PromptEntityCheckArm(model=f.FamilyFake(label="m", behaviour="garbage")).run(f.dg_arm_input())
    assert output.final_answer.status is AnswerStatus.UNPARSEABLE


def test_unknown_presentation_is_refused():
    import pytest

    with pytest.raises(ValueError, match="presentation"):
        PromptEntityCheckArm(model=f.FamilyFake(label="m"), presentation="nope")
