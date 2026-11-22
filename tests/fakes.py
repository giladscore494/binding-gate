"""A scripted model client for runner tests. It is registered as ``scripted_fake``.

It never touches the network. Its behaviour is chosen by the ``behaviour``
option, so it can be configured from a run config like a real client:

``target``      quote the first shown document whose source label is ``cx_doc``,
                abstain when there is none
``competitor``  quote the ``cy_doc`` document
``paraphrase``  answer with a quote that is in no document
``garbage``     return text that is not the required structure
``abstain``     always abstain
``crash``       raise a non-retryable error on every call
"""

from __future__ import annotations

import json
import re
import threading

from binding_gate.contracts import ModelRequest, ModelResponse, TokenUsage
from binding_gate.models._http import ModelError, PreflightResult, RetryPolicy
from binding_gate.registry import register

CALLS: list[tuple[str, ModelRequest]] = []
"""Every call any scripted client received: ``(label, request)``. Tests clear it."""
_LOCK = threading.Lock()
_DOC = re.compile(r"\[Document \d+ — source: (\w+)\]\n(.*?)(?=\n\n\[Document |\n\nBased on the above documents)", re.DOTALL)


def shown_documents(request: ModelRequest) -> dict[str, str]:
    return dict(_DOC.findall(request.messages[-1].content))


def _quote(text: str) -> str:
    return " ".join(text.split()[:8])


@register("model", "scripted_fake")
class ScriptedFake:
    def __init__(
        self,
        *,
        label: str | None = None,
        model_id: str = "fake-model",
        behaviour: str = "target",
        fail_first: int = 0,
        input_tokens: int = 1000,
        output_tokens: int = 500,
        report_usage: bool = True,
        preflight_ok: bool = True,
        max_output_tokens: int = 4096,
        max_retries: int = 2,
        balance_after: int | None = None,
    ):
        self.name = label or model_id
        self._balance_after = balance_after  # PR 19: calls served before the "wallet" is empty
        self._served = 0
        self.model_id = model_id
        self.behaviour = behaviour
        self.max_output_tokens = max_output_tokens
        self.retry = RetryPolicy(max_retries=max_retries, backoff_seconds=0.0)
        self._fail_first = fail_first
        self._usage = TokenUsage(input_tokens, output_tokens) if report_usage else None
        self._preflight_ok = preflight_ok
        self._failures: dict[str, int] = {}

    def preflight(self) -> PreflightResult:
        detail = "listed" if self._preflight_ok else f"model id {self.model_id!r} does not exist"
        return PreflightResult(self.name, self.model_id, self._preflight_ok, detail)

    def complete(self, request: ModelRequest) -> ModelResponse:
        prompt = request.messages[-1].content
        with _LOCK:
            CALLS.append((self.name, request))
            seen = self._failures.get(prompt, 0)
            self._failures[prompt] = seen + 1
        if self.behaviour == "crash":
            raise ModelError("HTTP 400: scripted permanent failure", status=400)
        if self._balance_after is not None:
            with _LOCK:
                self._served += 1
                empty = self._served > self._balance_after
            if empty:
                raise ModelError(
                    "BALANCE EXHAUSTED: HTTP 429: {\"error\": {\"code\": \"insufficient_quota\"}}",
                    status=429,
                    retryable=False,
                )
        if seen < self._fail_first:
            raise ModelError("HTTP 429: scripted rate limit", status=429, retryable=True, retry_after=0)
        documents = shown_documents(request)
        if self.behaviour == "garbage":
            text = "The literature is mixed [1]."
        elif self.behaviour == "abstain" or (self.behaviour == "target" and "cx_doc" not in documents):
            text = json.dumps({"status": "abstained", "reason": "no evidence about the queried drug"})
        elif self.behaviour == "paraphrase":
            text = json.dumps(
                {"status": "answered", "value": "v", "quote": "these seven words appear in no document", "source_id": "cy_doc"}
            )
        else:
            source = "cx_doc" if self.behaviour == "target" else "cy_doc"
            text = json.dumps(
                {"status": "answered", "value": "a value", "quote": _quote(documents[source]), "source_id": source}
            )
        return ModelResponse(
            text=text,
            model=f"{self.model_id}-2026-01-01",
            usage=self._usage,
            raw={"truncated": False, "reasoning_tokens": 7},
        )
