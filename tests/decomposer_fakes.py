"""A scripted decomposer model for tests, registered as model component ``decomposer_fake``.

It never touches the network. It reads the question from the request and
replies according to ``behaviour``:

``reference``         the decomposition the reference procedure would give
                      (entity X, qualifier C, presupposition with key terms X and C)
``fenced``            the same, inside a Markdown ```json code fence
``prose``             the same JSON preceded by a sentence (must be unparseable)
``garbage``           text that is not JSON
``wrong_entity``      the disease as the entity
``competitor_alias``  the right entity with the competitor as an alias (fixture naming:
                      ``xanadrugN`` -> competitor ``yoladrugN``)
``extra_qualifier``   the reference plus an invented qualifier
``crash``             a non-retryable error on every call
``flip``              ``garbage`` on the first call per question, ``reference`` afterwards

``fail_first`` makes the first N calls per question fail with a retryable error.
"""

from __future__ import annotations

import json
import re
import threading

from binding_gate.contracts import ModelRequest, ModelResponse, TokenUsage
from binding_gate.models._http import ModelError, PreflightResult, RetryPolicy
from binding_gate.registry import register

CALLS: list[tuple[str, str]] = []
"""``(client label, question)`` of every call any instance received. Tests clear it."""
_LOCK = threading.Lock()
_TEMPLATE = re.compile(r"^Question: What does the literature say about using (.+) to treat (.+)\?$", re.DOTALL)


def reference_json(x: str, c: str, *, entity: str | None = None, aliases: list[str] | None = None,
                   extra_qualifiers: list[dict] | None = None) -> dict:
    return {
        "entity": {"name": entity or x, "aliases": aliases or [], "kind": "drug"},
        "attribute": "what the literature says about this use",
        "qualifiers": [{"kind": "other", "text": c, "aliases": []}, *(extra_qualifiers or [])],
        "presuppositions": [{"text": f"{x} is used to treat {c}.", "key_terms": [x, c]}],
    }


@register("model", "decomposer_fake")
class DecomposerFake:
    def __init__(
        self,
        *,
        label: str | None = None,
        model_id: str = "fake-cheap",
        behaviour: str = "reference",
        fail_first: int = 0,
        input_tokens: int = 800,
        output_tokens: int = 200,
        report_usage: bool = True,
        preflight_ok: bool = True,
        max_output_tokens: int = 4096,
        max_retries: int = 3,
    ):
        self.name = label or model_id
        self.model_id = model_id
        self.behaviour = behaviour
        self.max_output_tokens = max_output_tokens
        self.retry = RetryPolicy(max_retries=max_retries, backoff_seconds=0.0)
        self._fail_first = fail_first
        self._usage = TokenUsage(input_tokens, output_tokens) if report_usage else None
        self._preflight_ok = preflight_ok
        self._seen: dict[str, int] = {}

    def preflight(self) -> PreflightResult:
        return PreflightResult(self.name, self.model_id, self._preflight_ok,
                               "listed" if self._preflight_ok else "does not exist")

    def complete(self, request: ModelRequest) -> ModelResponse:
        question = request.messages[-1].content
        with _LOCK:
            CALLS.append((self.name, question))
            seen = self._seen.get(question, 0)
            self._seen[question] = seen + 1
        if self.behaviour == "crash":
            raise ModelError("HTTP 400: scripted permanent failure", status=400)
        if seen < self._fail_first:
            raise ModelError("HTTP 503: scripted outage", status=503, retryable=True, retry_after=0)
        match = _TEMPLATE.match(question)
        x, c = (match.group(1), match.group(2)) if match else ("something", "somewhere")
        behaviour = self.behaviour
        if behaviour == "flip":
            behaviour = "garbage" if seen - self._fail_first == 0 else "reference"
        if behaviour == "garbage":
            text = "The entity is probably the drug."
        elif behaviour == "wrong_entity":
            text = json.dumps(reference_json(x, c, entity=c))
        elif behaviour == "competitor_alias":
            text = json.dumps(reference_json(x, c, aliases=[x.replace("xanadrug", "yoladrug")]))
        elif behaviour == "extra_qualifier":
            text = json.dumps(reference_json(x, c, extra_qualifiers=[{"kind": "time", "text": "recent", "aliases": []}]))
        else:
            text = json.dumps(reference_json(x, c))
            if behaviour == "fenced":
                text = f"```json\n{text}\n```"
            elif behaviour == "prose":
                text = f"Here is the decomposition: {text}"
        return ModelResponse(text=text, model=f"{self.model_id}-2026-01-01", usage=self._usage,
                             raw={"truncated": False})
