"""Arm ``no_gate``: the model answers in the required structure; nothing checks it.

This is the baseline of PREREGISTRATION.md section 5 and the only arm run in
PR 2. One model call per item; the parsed candidate is the final answer,
whatever its status (answered, abstained or unparseable).
"""

from __future__ import annotations

from binding_gate.answering import DEFAULT_PRESENTATION, PRESENTATIONS, answer_once
from binding_gate.contracts import ArmInput, ArmOutput, Attempt
from binding_gate.interfaces import ModelClient
from binding_gate.registry import register


@register("arm", "no_gate")
class NoGateArm:
    """Options: ``presentation`` (see ``binding_gate.answering``). ``model`` is passed by the runner."""

    name = "no_gate"

    def __init__(self, *, model: ModelClient, presentation: str = DEFAULT_PRESENTATION):
        if presentation not in PRESENTATIONS:
            raise ValueError(f"unknown presentation {presentation!r}; available: {sorted(PRESENTATIONS)}")
        self.model = model
        self.presentation = presentation

    def run(self, arm_input: ArmInput) -> ArmOutput:
        answer, calls = answer_once(self.model, arm_input, self.presentation)
        attempt = Attempt(answer=answer, evidence=tuple(d.ref() for d in arm_input.documents))
        return ArmOutput(attempts=(attempt,), final_answer=answer, model_calls=calls)
