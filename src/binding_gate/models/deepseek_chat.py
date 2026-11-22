"""DeepSeek client: OpenAI-format chat completions (``POST https://api.deepseek.com/chat/completions``).

PR 13, slot G of PREREGISTRATION.md section 9. Written from DeepSeek's published
API documentation (api-docs.deepseek.com, read 2026-11-22); NOT exercised
against the live API (tests use recorded-shape fixtures). The smoke run
validates it.

The wire format is the one of :class:`MoonshotChatClient` (OpenAI-compatible
chat completions), so this client reuses its payload, parsing and truncation
rules and changes only the provider, the key, the base URL, the preflight and
one optional field:

``thinking``  optional table sent as the request's ``thinking`` field. DeepSeek
              documents (guides/thinking_mode, read 2026-11-22): "Thinking
              mode is enabled by default", switched in the OpenAI format with
              ``"thinking": {"type": "enabled" | "disabled"}``. The study's
              configs send ``{type = "disabled"}`` (non-thinking mode, the
              lowest reasoning setting; section 9).

Response: as Moonshot - text is ``choices[0].message.content``; a separate
``reasoning_content`` (thinking mode) is not part of the answer;
``usage.completion_tokens`` is the billed output. The provider's returned
``model`` is stored in every trace record, so a repointed alias
(``deepseek-flash``) is visible. The free preflight is ``GET /models``
(documented: ``{"object": "list", "data": [{"id": ...}, ...]}``); the list must
contain the configured id.
"""

from __future__ import annotations

from typing import Any

from binding_gate.models._http import passthrough
from binding_gate.models.moonshot_chat import MoonshotChatClient
from binding_gate.registry import register


@register("model", "deepseek_chat")
class DeepSeekChatClient(MoonshotChatClient):
    provider = "deepseek"
    secret_name = "DEEPSEEK_API_KEY"
    default_base_url = "https://api.deepseek.com"

    def __init__(self, *, thinking: dict[str, Any] | None = None, **options: Any):
        super().__init__(**options)
        self.thinking = passthrough("thinking", thinking)

    def _payload(self, system: str | None, turns: list[dict[str, str]], params: dict[str, Any]) -> dict[str, Any]:
        payload = super()._payload(system, turns, params)
        if self.thinking is not None:
            payload["thinking"] = self.thinking
        return payload
