"""Z.ai (GLM) client: OpenAI-format chat completions (``POST https://api.z.ai/api/paas/v4/chat/completions``).

PR 14, slots H (``glm-5.3-flash``) and I (``glm-5.3``) of PREREGISTRATION.md
section 9. Written from Z.ai's published documentation (docs.z.ai, read
2026-11-22); NOT exercised against the live API (tests use recorded-shape
fixtures). The smoke run validates it.

Verified on docs.z.ai (2026-11-22):

* Endpoint: server ``https://api.z.ai/api`` + ``POST /paas/v4/chat/completions``
  (api-reference/llm/chat-completion), bearer authentication
  (``Authorization: Bearer <key>``). The OpenAI SDK examples of the API
  introduction use ``base_url="https://api.z.ai/api/paas/v4/"``, so the wire
  format is the one of :class:`MoonshotChatClient` (OpenAI-compatible chat
  completions); this client reuses its payload, parsing and truncation rules.
  (``https://api.z.ai/api/coding/paas/v4`` is the GLM Coding Plan endpoint and is
  not used.)
* Model ids ``glm-5.3`` and ``glm-5.3-flash`` are listed values of ``model``.
* Thinking CANNOT be disabled for GLM-5.3 and GLM-5.3-Flash: "GLM-5.3 and
  GLM-5.3-FLASH no longer support disabling thinking"; ``thinking.type`` only
  accepts ``"enabled"`` for them (a request with ``"disabled"`` fails). Their
  reasoning depth is set by ``reasoning_effort``, of which they support only
  ``low`` / ``high`` / ``max`` (default ``max``). The lowest setting is therefore
  ``thinking = {type = "enabled"}`` with ``reasoning_effort = "low"``.
* ``temperature`` is documented in [0.0, 1.0] (default 1.0).
* Response: ``choices[0].message.content`` is the answer; a separate
  ``reasoning_content`` (the thinking) is not part of it; ``usage.prompt_tokens``
  and ``usage.completion_tokens``; ``finish_reason`` is one of ``stop``,
  ``tool_calls``, ``length`` (a truncation), ``sensitive`` (content filter),
  ``model_context_window_exceeded``, ``network_error``. Every finish reason is
  recorded in the trace.

UNVERIFIED (not found on docs.z.ai, 2026-11-22):

* Whether ``usage.completion_tokens`` includes the thinking tokens (the price
  file and the cap assume it does: thinking is billed as output) and whether
  ``usage.completion_tokens_details.reasoning_tokens`` is reported (recorded
  when present).
* A list-models endpoint. The free preflight calls ``GET {base_url}/models``
  (the OpenAI convention; ``{"data": [{"id": ...}, ...]}``) and requires the
  configured id in the list. If Z.ai has no such endpoint the preflight FAILS
  (it never passes silently) and nothing is spent; the config can then set
  ``preflight_list_models = false``, which makes the preflight report the id as
  unverified (ok, nothing called) so that the smoke run's first call - which
  costs nothing if the id is unknown, because the provider refuses it - checks
  the id instead.

Options, all from ``[models.options]``, in addition to those of
:class:`HttpModelClient`:

``thinking``               optional table sent as the request's ``thinking`` field
                           (the study's configs send ``{type = "enabled"}``).
``reasoning_effort``       optional string sent as ``reasoning_effort`` (the
                           study's configs send ``"low"``, the lowest setting).
``preflight_list_models``  bool, default true: see above.
"""

from __future__ import annotations

from typing import Any

from binding_gate import secrets
from binding_gate.models._http import PreflightResult, passthrough
from binding_gate.models.moonshot_chat import MoonshotChatClient
from binding_gate.registry import register


@register("model", "zai_chat")
class ZaiChatClient(MoonshotChatClient):
    provider = "zai"
    secret_name = "ZAI_API_KEY"
    default_base_url = "https://api.z.ai/api/paas/v4"

    def __init__(
        self,
        *,
        thinking: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        preflight_list_models: bool = True,
        **options: Any,
    ):
        super().__init__(**options)
        self.thinking = passthrough("thinking", thinking)
        if reasoning_effort is not None and (not isinstance(reasoning_effort, str) or not reasoning_effort.strip()):
            raise ValueError("reasoning_effort must be a non-empty string")
        self.reasoning_effort = reasoning_effort
        if not isinstance(preflight_list_models, bool):
            raise ValueError("preflight_list_models must be true or false")
        self.preflight_list_models = preflight_list_models

    def _payload(self, system: str | None, turns: list[dict[str, str]], params: dict[str, Any]) -> dict[str, Any]:
        payload = super()._payload(system, turns, params)
        if self.thinking is not None:
            payload["thinking"] = self.thinking
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        return payload

    def preflight(self) -> PreflightResult:
        if self.preflight_list_models:
            return super().preflight()
        secrets.get_secret(self.secret_name)  # still refuses (SecretError) when the key is not set
        return PreflightResult(
            self.name,
            self.model_id,
            True,
            f"UNVERIFIED: {self.provider} list-models check disabled in the config "
            "(preflight_list_models = false); the first call of the smoke run checks model id "
            f"{self.model_id!r}",
        )
