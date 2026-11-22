"""Arm ``prompt_entity_check``: ``no_gate`` plus a fixed "check the entity yourself" instruction.

PREREGISTRATION.md section 5: "As ``no_gate``, plus an instruction to verify
that the evidence is about the question's entity and to abstain otherwise. No
external checks." It is the cheapest possible competitor of the gate: the same
model is simply told to do what the gate checks.

The request is exactly the ``no_gate`` request (``binding_gate.answering``) with
:data:`ENTITY_CHECK_INSTRUCTION` appended, after a blank line, to the last user
message, i.e. after the answer-format instruction of section 6.2. Everything
else, including the required answer structure and the deterministic parser, is
unchanged. Because the prompt differs, the runner's cache does not apply: this
arm makes its own model call and therefore draws its own sample (section 5,
"``prompt_entity_check`` uses a different prompt and therefore a different
sample").

The instruction names no entity and no document. It is generic on purpose:
the arm has no decomposer, so it cannot know the entity, and naming the
document roles would leak the design of the benchmark.

Changing :data:`ENTITY_CHECK_INSTRUCTION` changes the experiment: bump
:data:`PROMPT_VERSION` and record it (an amendment after the freeze).
"""

from __future__ import annotations

from binding_gate.answering import DEFAULT_PRESENTATION, PRESENTATIONS, build_answer_request, call_model
from binding_gate.contracts import ArmInput, ArmOutput, Attempt, Message, ModelRequest
from binding_gate.interfaces import ModelClient
from binding_gate.parsing import parse_answer
from binding_gate.registry import register

PROMPT_VERSION = 1

ENTITY_CHECK_INSTRUCTION = """\
Entity check (required, before you answer):
- First identify the entity the question asks about (for example the drug, product, person or organisation named in the question).
- Answer only if your quote comes from a passage that is explicitly about that same entity. A passage about a different entity is not evidence for the question, even if it is on the same topic, concerns a similar entity, or contains exactly the kind of information that was asked for.
- If no document contains such a passage, abstain: {"status": "abstained", "reason": "<one short sentence>"}."""


def build_entity_check_request(arm_input: ArmInput, presentation: str = DEFAULT_PRESENTATION) -> ModelRequest:
    """The ``no_gate`` request with :data:`ENTITY_CHECK_INSTRUCTION` appended to the last user message."""
    base = build_answer_request(arm_input, presentation)
    messages = list(base.messages)
    if not messages or messages[-1].role != "user":
        raise ValueError("the presentation must end with a user message")
    last = messages[-1]
    messages[-1] = Message("user", f"{last.content}\n\n{ENTITY_CHECK_INSTRUCTION}")
    return ModelRequest(messages=tuple(messages), params=base.params)


@register("arm", "prompt_entity_check")
class PromptEntityCheckArm:
    """Options: ``presentation`` (as for ``no_gate``). ``model`` is passed by the runner."""

    name = "prompt_entity_check"

    def __init__(self, *, model: ModelClient, presentation: str = DEFAULT_PRESENTATION):
        if presentation not in PRESENTATIONS:
            raise ValueError(f"unknown presentation {presentation!r}; available: {sorted(PRESENTATIONS)}")
        self.model = model
        self.presentation = presentation

    def run(self, arm_input: ArmInput) -> ArmOutput:
        request = build_entity_check_request(arm_input, self.presentation)
        response, calls = call_model(self.model, request, "answer")
        answer = parse_answer(response.text)
        attempt = Attempt(answer=answer, evidence=tuple(d.ref() for d in arm_input.documents))
        return ArmOutput(attempts=(attempt,), final_answer=answer, model_calls=calls)
