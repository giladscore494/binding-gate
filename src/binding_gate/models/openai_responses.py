"""OpenAI client: Responses API (``POST /v1/responses``).

Written from OpenAI's published API reference; NOT exercised against the live
API in PR 2 (tests use recorded-shape fixtures). The smoke run validates it.

Request: ``model``, ``instructions`` (system text), ``input`` (role/content
turns), ``max_output_tokens``, ``store = false``, plus ``temperature`` and
``reasoning`` only when configured. Reasoning effort is left at the provider
default unless ``reasoning`` is set in the config ("as deployed").

Response: text is the concatenation of ``output_text`` parts of ``message``
items in ``output``. ``usage.output_tokens`` includes reasoning tokens;
``usage.output_tokens_details.reasoning_tokens`` is recorded separately.
``status = "incomplete"`` with reason ``max_output_tokens`` is a truncation.
The free preflight is ``GET /v1/models/{id}``.
"""

from __future__ import annotations

from typing import Any

from binding_gate.contracts import TokenUsage
from binding_gate.models._http import (
    HttpModelClient,
    ModelError,
    ParsedCompletion,
    as_int,
    as_str,
    passthrough,
)
from binding_gate.registry import register


@register("model", "openai_responses")
class OpenAIResponsesClient(HttpModelClient):
    provider = "openai"
    secret_name = "OPENAI_API_KEY"
    default_base_url = "https://api.openai.com/v1"
    _prompt_keys = frozenset({"input", "instructions"})

    def __init__(self, *, reasoning: dict[str, Any] | None = None, **options: Any):
        super().__init__(**options)
        self.reasoning = passthrough("reasoning", reasoning)

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"}

    def _completion_url(self) -> str:
        return f"{self.base_url}/responses"

    def _payload(self, system: str | None, turns: list[dict[str, str]], params: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model_id,
            "input": turns,
            "max_output_tokens": params["max_output_tokens"],
            "store": False,
        }
        if system is not None:
            payload["instructions"] = system
        if "temperature" in params:
            payload["temperature"] = params["temperature"]
        if self.reasoning is not None:
            payload["reasoning"] = self.reasoning
        return payload

    def _parse(self, data: dict[str, Any]) -> ParsedCompletion:
        status = as_str(data.get("status"))
        if status == "failed" or data.get("error"):
            raise ModelError(f"OpenAI response failed: {str(data.get('error'))[:300]}")
        texts: list[str] = []
        for item in data.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        texts.append(str(part.get("text") or ""))
        incomplete = data.get("incomplete_details") or {}
        reason = as_str(incomplete.get("reason")) if isinstance(incomplete, dict) else None
        usage_raw = data.get("usage") if isinstance(data.get("usage"), dict) else None
        usage = None
        reasoning_tokens = None
        if usage_raw is not None:
            tokens_in, tokens_out = as_int(usage_raw.get("input_tokens")), as_int(usage_raw.get("output_tokens"))
            if tokens_in is not None and tokens_out is not None:
                usage = TokenUsage(tokens_in, tokens_out)
            details = usage_raw.get("output_tokens_details")
            if isinstance(details, dict):
                reasoning_tokens = as_int(details.get("reasoning_tokens"))
        return ParsedCompletion(
            text="".join(texts),
            model=as_str(data.get("model")),
            usage=usage,
            usage_raw=usage_raw,
            reasoning_tokens=reasoning_tokens,
            finish_reason=reason or status,
            truncated=status == "incomplete" and reason == "max_output_tokens",
            response_id=as_str(data.get("id")),
        )
