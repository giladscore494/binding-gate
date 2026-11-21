"""Moonshot (Kimi) client: OpenAI-compatible chat completions (``POST /v1/chat/completions``).

Written from Moonshot's published API reference; NOT exercised against the live
API in PR 2 (tests use recorded-shape fixtures). Reserved for the secondary
judge (PREREGISTRATION.md section 9); no PR 2 config runs it.

Request: ``model``, ``messages`` (system / user / assistant), ``max_tokens``,
plus ``temperature`` only when configured.

Response: text is ``choices[0].message.content`` (a separate
``reasoning_content`` field, if present, is not part of the answer).
``usage.completion_tokens`` is the billed output;
``usage.completion_tokens_details.reasoning_tokens`` is recorded when reported.
``finish_reason = "length"`` is a truncation. The free preflight is
``GET /v1/models`` (the list must contain the configured id).
"""

from __future__ import annotations

from typing import Any

from binding_gate.contracts import TokenUsage
from binding_gate.models._http import HttpModelClient, ParsedCompletion, as_int, as_str
from binding_gate.registry import register


@register("model", "moonshot_chat")
class MoonshotChatClient(HttpModelClient):
    provider = "moonshot"
    secret_name = "MOONSHOT_API_KEY"
    default_base_url = "https://api.moonshot.ai/v1"
    _prompt_keys = frozenset({"messages"})

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"}

    def _completion_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _preflight_url(self) -> str:
        return f"{self.base_url}/models"

    def _preflight_found(self, data: dict[str, Any]) -> bool:
        return any(
            isinstance(entry, dict) and entry.get("id") == self.model_id
            for entry in data.get("data") or []
        )

    def _payload(self, system: str | None, turns: list[dict[str, str]], params: dict[str, Any]) -> dict[str, Any]:
        messages = ([{"role": "system", "content": system}] if system is not None else []) + turns
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": params["max_output_tokens"],
        }
        if "temperature" in params:
            payload["temperature"] = params["temperature"]
        return payload

    def _parse(self, data: dict[str, Any]) -> ParsedCompletion:
        choices = data.get("choices") or []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        usage_raw = data.get("usage") if isinstance(data.get("usage"), dict) else None
        usage = None
        reasoning_tokens = None
        if usage_raw is not None:
            tokens_in = as_int(usage_raw.get("prompt_tokens"))
            tokens_out = as_int(usage_raw.get("completion_tokens"))
            if tokens_in is not None and tokens_out is not None:
                usage = TokenUsage(tokens_in, tokens_out)
            details = usage_raw.get("completion_tokens_details")
            if isinstance(details, dict):
                reasoning_tokens = as_int(details.get("reasoning_tokens"))
        finish = as_str(first.get("finish_reason"))
        return ParsedCompletion(
            text=str(message.get("content") or ""),
            model=as_str(data.get("model")),
            usage=usage,
            usage_raw=usage_raw,
            reasoning_tokens=reasoning_tokens,
            finish_reason=finish,
            truncated=finish == "length",
            response_id=as_str(data.get("id")),
        )
