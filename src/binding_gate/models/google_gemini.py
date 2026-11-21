"""Google Gemini client: ``models/{id}:generateContent`` (Generative Language API, v1beta).

Written from Google's published API reference; NOT exercised against the live
API in PR 2 (tests use recorded-shape fixtures). The smoke run validates it.

Request: ``systemInstruction``, ``contents`` (roles ``user`` / ``model``),
``generationConfig.maxOutputTokens``, plus ``temperature`` and
``thinkingConfig`` only when configured (thinking is left at the provider
default unless set in the config). The key is sent in the ``x-goog-api-key``
header, never in the URL.

Response: text is the concatenation of the non-thought ``parts`` of the first
candidate. ``usageMetadata.candidatesTokenCount`` EXCLUDES thinking tokens, which
are reported as ``thoughtsTokenCount`` and billed as output, so
``TokenUsage.output_tokens`` = candidates + thoughts and ``reasoning_tokens`` =
thoughts. ``finishReason = "MAX_TOKENS"`` is a truncation. A prompt blocked by
the provider yields an empty text with the block reason as finish reason.
The free preflight is ``GET /v1beta/models/{id}``.
"""

from __future__ import annotations

import urllib.parse
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


@register("model", "google_gemini")
class GoogleGeminiClient(HttpModelClient):
    provider = "google"
    secret_name = "GEMINI_API_KEY"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"
    _prompt_keys = frozenset({"contents", "systemInstruction"})

    def __init__(self, *, thinking_config: dict[str, Any] | None = None, **options: Any):
        super().__init__(**options)
        self.thinking_config = passthrough("thinking_config", thinking_config)

    def _model_path(self) -> str:
        return f"{self.base_url}/models/{urllib.parse.quote(self.model_id, safe='')}"

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"x-goog-api-key": api_key}

    def _completion_url(self) -> str:
        return f"{self._model_path()}:generateContent"

    def _preflight_url(self) -> str:
        return self._model_path()

    def _preflight_found(self, data: dict[str, Any]) -> bool:
        return data.get("name") == f"models/{self.model_id}"

    def _payload(self, system: str | None, turns: list[dict[str, str]], params: dict[str, Any]) -> dict[str, Any]:
        generation: dict[str, Any] = {"maxOutputTokens": params["max_output_tokens"]}
        if "temperature" in params:
            generation["temperature"] = params["temperature"]
        if self.thinking_config is not None:
            generation["thinkingConfig"] = self.thinking_config
        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "model" if turn["role"] == "assistant" else "user",
                    "parts": [{"text": turn["content"]}],
                }
                for turn in turns
            ],
            "generationConfig": generation,
        }
        if system is not None:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        return payload

    def _parse(self, data: dict[str, Any]) -> ParsedCompletion:
        candidates = data.get("candidates") or []
        first = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
        content = first.get("content") if isinstance(first.get("content"), dict) else {}
        texts = [
            str(part.get("text") or "")
            for part in content.get("parts") or []
            if isinstance(part, dict) and not part.get("thought")
        ]
        finish = as_str(first.get("finishReason"))
        if not first:
            feedback = data.get("promptFeedback")
            block = as_str(feedback.get("blockReason")) if isinstance(feedback, dict) else None
            finish = f"blocked:{block}" if block else "no_candidates"
        usage_raw = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else None
        usage = None
        reasoning_tokens = None
        if usage_raw is not None:
            tokens_in = as_int(usage_raw.get("promptTokenCount"))
            visible = as_int(usage_raw.get("candidatesTokenCount"))
            reasoning_tokens = as_int(usage_raw.get("thoughtsTokenCount"))
            if tokens_in is not None:
                usage = TokenUsage(tokens_in, (visible or 0) + (reasoning_tokens or 0))
        return ParsedCompletion(
            text="".join(texts),
            model=as_str(data.get("modelVersion")),
            usage=usage,
            usage_raw=usage_raw,
            reasoning_tokens=reasoning_tokens,
            finish_reason=finish,
            truncated=finish == "MAX_TOKENS",
            response_id=as_str(data.get("responseId")),
        )
