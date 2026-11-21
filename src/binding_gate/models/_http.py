"""Shared plumbing for the HTTP model clients (not a component itself).

Why plain HTTP instead of the providers' SDKs
---------------------------------------------
* The core package keeps zero runtime dependencies; four small JSON endpoints do
  not justify four SDKs with their own transitive dependencies and release
  cadence.
* The SDKs retry on their own. ARCHITECTURE.md section 8 requires every failed
  call to be visible in the trace, so retries must happen where calls are logged
  (``binding_gate.answering.call_model``), not inside a library.
* The request we send and the response we parse are a few lines each, so a
  reviewer can check them against the providers' documentation, and the tests
  can exercise them with a fake transport and no network.

A client never retries by itself: :meth:`HttpModelClient.complete` makes exactly
one HTTP request and raises :class:`ModelError`, which says whether the failure
is worth retrying (HTTP 408, 429, 5xx, timeouts and connection errors).

API keys are read through ``binding_gate.secrets`` at call time, only sent in
request headers, and never stored on the client, logged or put into error
messages.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from binding_gate import secrets
from binding_gate.contracts import ModelRequest, ModelResponse, TokenUsage

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})
_ERROR_BODY_LIMIT = 500
_REQUEST_PARAM_KEYS = frozenset({"temperature", "max_output_tokens"})


class ModelError(RuntimeError):
    """A failed model call. ``retryable`` tells the caller whether to try again."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff, configured per model in the run config."""

    max_retries: int = 0
    backoff_seconds: float = 2.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 60.0

    def __post_init__(self) -> None:
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or self.max_retries < 0:
            raise ValueError("max_retries must be an integer >= 0")
        for name in ("backoff_seconds", "backoff_multiplier", "max_backoff_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{name} must be a number >= 0")

    def delay(self, failed_attempts: int, retry_after: float | None = None) -> float:
        """Seconds to wait after the ``failed_attempts``-th failure (1-based)."""
        delay = self.backoff_seconds * (self.backoff_multiplier ** max(failed_attempts - 1, 0))
        if retry_after is not None:
            delay = max(delay, retry_after)
        return min(delay, self.max_backoff_seconds)


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    timeout: float


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


Transport = Callable[[HttpRequest], HttpResponse]


def urllib_transport(request: HttpRequest) -> HttpResponse:
    """Send one HTTP request with the standard library. Never retries."""
    raw = urllib.request.Request(
        request.url, data=request.body, method=request.method, headers=dict(request.headers)
    )
    try:
        with urllib.request.urlopen(raw, timeout=request.timeout) as response:
            return HttpResponse(response.status, response.read(), dict(response.headers.items()))
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read(), dict(exc.headers.items()) if exc.headers else {})
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ModelError(
            f"transport error: {type(reason).__name__}: {reason}", retryable=True
        ) from None


def _retry_after(headers: Mapping[str, str]) -> float | None:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return max(float(value), 0.0)
            except (TypeError, ValueError):
                return None
    return None


def request_json(
    transport: Transport,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    payload: Mapping[str, Any] | None,
    timeout: float,
) -> dict[str, Any]:
    """One HTTP exchange returning the decoded JSON object, or raising :class:`ModelError`."""
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    all_headers = {"Accept": "application/json", **headers}
    if body is not None:
        all_headers["Content-Type"] = "application/json"
    response = transport(HttpRequest(method, url, all_headers, body, timeout))
    text = response.body.decode("utf-8", errors="replace")
    if not 200 <= response.status < 300:
        raise ModelError(
            f"HTTP {response.status}: {text[:_ERROR_BODY_LIMIT]}",
            status=response.status,
            retryable=response.status in RETRYABLE_STATUS,
            retry_after=_retry_after(response.headers),
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ModelError(
            f"HTTP {response.status} with a body that is not JSON: {text[:_ERROR_BODY_LIMIT]}",
            status=response.status,
            retryable=True,
        ) from None
    if not isinstance(data, dict):
        raise ModelError("the response body is not a JSON object", status=response.status)
    return data


@dataclass(frozen=True)
class PreflightResult:
    """Outcome of the free "does this model id exist?" check."""

    label: str
    model_id: str
    ok: bool
    detail: str


def _number(name: str, value: Any, *, minimum: float, allow_none: bool = False) -> Any:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ValueError(f"{name} must be a number >= {minimum}")
    return value


class HttpModelClient:
    """Base class of the provider clients.

    Options (all from ``[models.options]`` of the run config):

    ``model_id``           exact provider model id (required; no aliases such as "latest").
    ``max_output_tokens``  output cap sent with every request (required).
    ``temperature``        sent only if given; omit it to use the provider default.
    ``timeout_seconds``    per HTTP request (default 180).
    ``max_retries``, ``backoff_seconds``, ``backoff_multiplier``, ``max_backoff_seconds``
                           retry policy used by ``binding_gate.answering.call_model``.
    ``base_url``           endpoint root; defaults to the provider's public API.

    Subclasses add provider-specific pass-through options (reasoning/thinking).
    ``label`` is supplied by the runner and becomes :attr:`name`, the model
    label written to traces. ``transport`` exists for tests.
    """

    provider: str = ""
    secret_name: str = ""
    default_base_url: str = ""

    def __init__(
        self,
        *,
        model_id: str,
        max_output_tokens: int,
        label: str | None = None,
        temperature: float | None = None,
        timeout_seconds: float = 180.0,
        max_retries: int = 4,
        backoff_seconds: float = 2.0,
        backoff_multiplier: float = 2.0,
        max_backoff_seconds: float = 60.0,
        base_url: str | None = None,
        transport: Transport | None = None,
    ):
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must be a non-empty string")
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens < 1:
            raise ValueError("max_output_tokens must be an integer >= 1")
        self.model_id = model_id
        self.name = label or model_id
        self.max_output_tokens = max_output_tokens
        self.temperature = _number("temperature", temperature, minimum=0.0, allow_none=True)
        self.timeout_seconds = float(_number("timeout_seconds", timeout_seconds, minimum=0.001))
        self.retry = RetryPolicy(max_retries, backoff_seconds, backoff_multiplier, max_backoff_seconds)
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        if not self.base_url.startswith("https://") and not self.base_url.startswith("http://"):
            raise ValueError("base_url must start with https:// or http://")
        self._transport = transport or urllib_transport

    # -- to be provided by subclasses -------------------------------------- #

    def _headers(self, api_key: str) -> dict[str, str]:
        raise NotImplementedError

    def _completion_url(self) -> str:
        raise NotImplementedError

    def _payload(self, system: str | None, turns: list[dict[str, str]], params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _parse(self, data: dict[str, Any]) -> "ParsedCompletion":
        raise NotImplementedError

    def _preflight_url(self) -> str:
        return f"{self.base_url}/models/{urllib.parse.quote(self.model_id, safe='')}"

    def _preflight_found(self, data: dict[str, Any]) -> bool:
        return data.get("id") == self.model_id

    # -- public API ---------------------------------------------------------- #

    def describe(self) -> dict[str, Any]:
        """What this client would call; used by the dry run. Contains no secret."""
        return {
            "label": self.name,
            "provider": self.provider,
            "model_id": self.model_id,
            "secret": self.secret_name,
            "max_output_tokens": self.max_output_tokens,
        }

    def effective_params(self, request: ModelRequest) -> dict[str, Any]:
        """Sampling parameters for ``request``: client config, overridden by ``request.params``."""
        unknown = sorted(set(request.params) - _REQUEST_PARAM_KEYS)
        if unknown:
            raise ModelError(f"unsupported request parameter(s) {unknown}")
        params: dict[str, Any] = {"max_output_tokens": self.max_output_tokens}
        if self.temperature is not None:
            params["temperature"] = self.temperature
        params.update(request.params)
        return params

    def build_http_payload(self, request: ModelRequest) -> dict[str, Any]:
        """The exact JSON body that :meth:`complete` sends (exposed for tests and audits)."""
        system_parts = [m.content for m in request.messages if m.role == "system"]
        turns = [{"role": m.role, "content": m.content} for m in request.messages if m.role != "system"]
        for turn in turns:
            if turn["role"] not in {"user", "assistant"}:
                raise ModelError(f"unsupported message role {turn['role']!r}")
        if not turns:
            raise ModelError("a request needs at least one non-system message")
        system = "\n\n".join(system_parts) if system_parts else None
        return self._payload(system, turns, self.effective_params(request))

    def complete(self, request: ModelRequest) -> ModelResponse:
        """One HTTP request. Raises :class:`ModelError` on any failure; never retries."""
        payload = self.build_http_payload(request)
        data = request_json(
            self._transport,
            "POST",
            self._completion_url(),
            headers=self._headers(secrets.get_secret(self.secret_name)),
            payload=payload,
            timeout=self.timeout_seconds,
        )
        parsed = self._parse(data)
        sent = {k: v for k, v in payload.items() if k not in self._prompt_keys}
        return ModelResponse(
            text=parsed.text,
            model=parsed.model or self.model_id,
            usage=parsed.usage,
            raw={
                "provider": self.provider,
                "requested_model": self.model_id,
                "provider_reported_model": parsed.model,
                "response_id": parsed.response_id,
                "finish_reason": parsed.finish_reason,
                "truncated": parsed.truncated,
                "reasoning_tokens": parsed.reasoning_tokens,
                "usage": parsed.usage_raw,
                "request_params": sent,
            },
        )

    _prompt_keys: frozenset[str] = frozenset()

    def preflight(self) -> PreflightResult:
        """Free check that the configured model id exists for this API key.

        Calls the provider's models endpoint (no tokens are generated, nothing
        is billed). Raises ``SecretError`` if the key is not set.
        """
        api_key = secrets.get_secret(self.secret_name)
        try:
            data = request_json(
                self._transport,
                "GET",
                self._preflight_url(),
                headers=self._headers(api_key),
                payload=None,
                timeout=min(self.timeout_seconds, 60.0),
            )
        except ModelError as exc:
            if exc.status == 404:
                detail = f"model id {self.model_id!r} does not exist at {self.provider} (HTTP 404)"
            else:
                detail = f"could not verify model id {self.model_id!r} at {self.provider}: {exc}"
            return PreflightResult(self.name, self.model_id, False, detail)
        if self._preflight_found(data):
            return PreflightResult(self.name, self.model_id, True, f"{self.provider} lists {self.model_id!r}")
        return PreflightResult(
            self.name,
            self.model_id,
            False,
            f"{self.provider} answered but does not list model id {self.model_id!r}",
        )


@dataclass(frozen=True)
class ParsedCompletion:
    """Provider-independent view of one completion response."""

    text: str
    model: str | None
    usage: TokenUsage | None
    usage_raw: dict[str, Any] | None
    reasoning_tokens: int | None
    finish_reason: str | None
    truncated: bool
    response_id: str | None


def as_int(value: Any) -> int | None:
    """``value`` if it is a non-negative integer, else ``None`` (bools excluded)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def passthrough(name: str, value: Any) -> dict[str, Any] | None:
    """Validate an optional provider-specific table such as ``reasoning`` or ``thinking``."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a table")
    return json.loads(json.dumps(value))
