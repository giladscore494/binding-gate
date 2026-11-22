"""Arm ``free_form``: the benchmark's own question, documents and system prompt; a normal answer (PR 22).

EXPLORATORY, post hoc (PREREGISTRATION.md section 18, amendment 1, step V2). It
estimates how often a model makes the entity-attribution error when it answers
the way a free-form agent does, so that this rate can be compared, on the same
items, with the rate under the structured answer (value + verbatim quote +
source id) that every other arm requires. It decides no hypothesis and is never
part of the confirmatory analysis.

Request
    :func:`binding_gate.answering.build_free_form_request`: for layer 1 the
    Deceptive Grounding benchmark's ``doc_injection`` messages, character for
    character - its system prompt (which asks for ``[n]`` citations) and its
    user message with the document block and the question. NO answer-format
    instruction is appended: no JSON, no quote, no source id.

Answer
    One model call (retries as in every arm, ``answering.call_model``). The
    reply is not parsed. A non-empty reply becomes an ``answered`` candidate
    with ``value`` = the reply (``<think>`` blocks removed) and the fixed
    sentinels ``answering.FREE_FORM_QUOTE`` / ``answering.FREE_FORM_SOURCE_ID``
    in ``quote`` / ``source_id``; ``raw`` keeps the unmodified reply. An empty
    reply is ``unparseable`` (``empty output``). This is the only way to carry
    free text through the frozen contracts without a schema change, and it keeps
    their invariants (an answered candidate has value, quote and source id; no
    gate verdict; the final answer is the single attempt's candidate). The
    sentinel is never a document's text or id, and ``answering.is_free_form``
    recognises it: ``provenance`` labels such an answer ``not_applicable``
    instead of looking for the sentinel in the documents, and
    ``baseline_report.record_outcome`` (used by the stop-rule report and by every
    statistic of H1-H4) refuses it. Free-form answers are scored ONLY by the
    secondary LLM judge (``python -m binding_gate.scorers.llm_judge ... --arm
    free_form``), which reads ``raw`` as the benchmark judge reads a response.

    A refusal ("insufficient evidence") is NOT turned into an abstention: there
    is no deterministic rule for it in free text. The judge labels a reply
    without affirmative claims about the queried drug ``no_claims``.

Options: ``presentation`` (default ``deceptive_grounding_doc_injection``; only
presentations in ``answering.FREE_FORM_PRESENTATIONS``). ``model`` is passed by
the runner.
"""

from __future__ import annotations

from binding_gate.answering import (
    DEFAULT_PRESENTATION,
    FREE_FORM_PRESENTATIONS,
    build_free_form_request,
    call_model,
    free_form_candidate,
)
from binding_gate.contracts import ArmInput, ArmOutput, Attempt
from binding_gate.interfaces import ModelClient
from binding_gate.registry import register

ARM_NAME = "free_form"


@register("arm", ARM_NAME)
class FreeFormArm:
    """Exploratory free-form answer (see the module docstring)."""

    name = ARM_NAME

    def __init__(self, *, model: ModelClient, presentation: str = DEFAULT_PRESENTATION):
        if presentation not in FREE_FORM_PRESENTATIONS:
            raise ValueError(
                f"presentation {presentation!r} has no free-form variant; available: {sorted(FREE_FORM_PRESENTATIONS)}"
            )
        self.model = model
        self.presentation = presentation

    def run(self, arm_input: ArmInput) -> ArmOutput:
        request = build_free_form_request(arm_input, self.presentation)
        response, calls = call_model(self.model, request, "answer")
        answer = free_form_candidate(response.text)
        attempt = Attempt(answer=answer, evidence=tuple(d.ref() for d in arm_input.documents))
        return ArmOutput(attempts=(attempt,), final_answer=answer, model_calls=calls)
