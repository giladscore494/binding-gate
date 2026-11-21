"""Anthropic client: Messages API (``POST /v1/messages``).

Written from Anthropic's published API reference; NOT exercised against the live
API in PR 2 (tests use recorded-shape fixtures). The smoke run validates it.

Request: ``model``, ``max_tokens``, ``system``, ``messages``, plus
``temperature`` and ``thinking`` only when configured (thinking is left at the
provider default unless set in the config). Headers: ``x-api-key`` and
``anthropic-version``.

Response: text is the concatenation of ``text`` content blocks (thinking blocks
are not part of the answer). ``usage.output_tokens`` is the billed output and
includes thinking tokens; the Messages API does not itemise them, so
``reasoning_tokens`` is recorded as ``null`` unless a field for it is present.
``stop_reason = "max_tokens"`` is a truncation. The free preflight is
``GET /v1/models/{id}``.
"""

from __future__ import annotations

from typing import Any

from binding_gate.contracts import TokenUsage
from binding_gate.models._http import (
    HttpModelClient,
    ParsedCompletion,
    as_int,
    as_str,
    passthrough,
)
from binding_gate.registry import register


@register("model", "anthropic_messages")
class AnthropicMessagesClient(HttpModelClient):
    provider = "anthropic"
    secret_name = "ANTHROPIC_API_KEY"
    default_base_url = "https://api.anthropic.com/v1"
    _prompt_keys = frozenset({"messages", "system"})

    def __init__(
        self,
        *,
        thinking: dict[str, Any] | None = None,
        anthropic_version: str = "2023-06-01",
        **options: Any,
    ):
        super().__init__(**options)
        self.thinking = passthrough("thinking", thinking)
        if not isinstance(anthropic_version, str) or not anthropic_version:
            raise ValueError("anthropic_version must be a non-empty string")
        self.anthropic_version = anthropic_version

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"x-api-key": api_key, "anthropic-version": self.anthropic_version}

    def _completion_url(self) -> str:
        return f"{self.base_url}/messages"

    def _payload(self, system: str | None, turns: list[dict[str, str]], params: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": params["max_output_tokens"],
            "messages": turns,
        }
        if system is not None:
            payload["system"] = system
        if "temperature" in params:
            payload["temperature"] = params["temperature"]
        if self.thinking is not None:
            payload["thinking"] = self.thinking
        return payload

    def _parse(self, data: dict[str, Any]) -> ParsedCompletion:
        texts = [
            str(block.get("text") or "")
            for block in data.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        usage_raw = data.get("usage") if isinstance(data.get("usage"), dict) else None
        usage = None
        reasoning_tokens = None
        if usage_raw is not None:
            tokens_out = as_int(usage_raw.get("output_tokens"))
            parts = [
                as_int(usage_raw.get(key))
                for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            ]
            if parts[0] is not None and tokens_out is not None:
                usage = TokenUsage(sum(p or 0 for p in parts), tokens_out)
            details = usage_raw.get("output_tokens_details")
            if isinstance(details, dict):
                reasoning_tokens = as_int(details.get("thinking_tokens"))
        stop_reason = as_str(data.get("stop_reason"))
        return ParsedCompletion(
            text="".join(texts),
            model=as_str(data.get("model")),
            usage=usage,
            usage_raw=usage_raw,
            reasoning_tokens=reasoning_tokens,
            finish_reason=stop_reason,
            truncated=stop_reason == "max_tokens",
            response_id=as_str(data.get("id")),
        )
