"""Model clients: request construction and response parsing against recorded-shape fixtures.

The fixtures below are written from the providers' documented response formats.
Nothing here talks to a network, so the LIVE behaviour of these clients is
unverified until the smoke run (docs/RUNNING_BASELINE.md).
"""

from __future__ import annotations

import json

import pytest

from binding_gate import registry
from binding_gate.contracts import Message, ModelRequest
from binding_gate.models._http import HttpRequest, HttpResponse, ModelError, RetryPolicy
from binding_gate.secrets import KNOWN_SECRETS, SecretError

REQUEST = ModelRequest((Message("system", "SYSTEM TEXT"), Message("user", "USER TEXT")))
KEY = "sk-test-SECRET-VALUE"

OPENAI_RESPONSE = {
    "id": "resp_123",
    "object": "response",
    "status": "completed",
    "model": "gpt-6-luna-2026-10-01",
    "output": [
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "content": [{"type": "output_text", "text": '{"status": "abstained"}', "annotations": []}],
        },
    ],
    "usage": {
        "input_tokens": 900,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 340,
        "output_tokens_details": {"reasoning_tokens": 300},
        "total_tokens": 1240,
    },
}
ANTHROPIC_RESPONSE = {
    "id": "msg_abc",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-5-5-20261001",
    "content": [
        {"type": "thinking", "thinking": "hidden", "signature": "sig"},
        {"type": "text", "text": '{"status": "abstained"}'},
    ],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 910, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 120},
}
GEMINI_RESPONSE = {
    "candidates": [
        {
            "content": {
                "role": "model",
                "parts": [{"text": "thinking summary", "thought": True}, {"text": '{"status": "abstained"}'}],
            },
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {"promptTokenCount": 905, "candidatesTokenCount": 40, "thoughtsTokenCount": 260, "totalTokenCount": 1205},
    "modelVersion": "gemini-3.5-flash-001",
    "responseId": "gem-1",
}
MOONSHOT_RESPONSE = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "kimi-k3.1-pro",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": '{"status": "abstained"}', "reasoning_content": "hidden"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 880, "completion_tokens": 210, "total_tokens": 1090, "completion_tokens_details": {"reasoning_tokens": 170}},
}

PROVIDERS = {
    "openai_responses": ("OPENAI_API_KEY", "gpt-6-luna", OPENAI_RESPONSE),
    "anthropic_messages": ("ANTHROPIC_API_KEY", "claude-haiku-5-5", ANTHROPIC_RESPONSE),
    "google_gemini": ("GEMINI_API_KEY", "gemini-3.5-flash", GEMINI_RESPONSE),
    "moonshot_chat": ("MOONSHOT_API_KEY", "kimi-k3.1-pro", MOONSHOT_RESPONSE),
}


class FakeTransport:
    """Returns queued responses and records the requests it was given."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status, body = response
        headers = {"Retry-After": "7"} if status == 429 else {}
        return HttpResponse(status, json.dumps(body).encode("utf-8"), headers)


def make(name, transport, **options):
    _secret, model_id, _body = PROVIDERS[name]
    options.setdefault("model_id", model_id)
    options.setdefault("max_output_tokens", 4096)
    return registry.create("model", name, label="label-x", transport=transport, **options)


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    for secret in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "MOONSHOT_API_KEY"):
        monkeypatch.setenv(secret, KEY)


def sent(transport):
    request = transport.requests[-1]
    return request, json.loads(request.body.decode("utf-8"))


# -- request construction ---------------------------------------------------- #


def test_openai_request():
    transport = FakeTransport((200, OPENAI_RESPONSE))
    make("openai_responses", transport).complete(REQUEST)
    request, body = sent(transport)
    assert (request.method, request.url) == ("POST", "https://api.openai.com/v1/responses")
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert request.headers["Content-Type"] == "application/json"
    assert body == {
        "model": "gpt-6-luna",
        "instructions": "SYSTEM TEXT",
        "input": [{"role": "user", "content": "USER TEXT"}],
        "max_output_tokens": 4096,
        "store": False,
    }


def test_openai_optional_temperature_and_reasoning_are_sent_only_when_configured():
    transport = FakeTransport((200, OPENAI_RESPONSE))
    make("openai_responses", transport, temperature=0.0, reasoning={"effort": "low"}).complete(REQUEST)
    _request, body = sent(transport)
    assert body["temperature"] == 0.0 and body["reasoning"] == {"effort": "low"}


def test_anthropic_request():
    transport = FakeTransport((200, ANTHROPIC_RESPONSE))
    make("anthropic_messages", transport, temperature=0.0).complete(REQUEST)
    request, body = sent(transport)
    assert request.url == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert body == {
        "model": "claude-haiku-5-5",
        "max_tokens": 4096,
        "system": "SYSTEM TEXT",
        "messages": [{"role": "user", "content": "USER TEXT"}],
        "temperature": 0.0,
    }


def test_gemini_request_keeps_the_key_out_of_the_url():
    transport = FakeTransport((200, GEMINI_RESPONSE))
    make("google_gemini", transport, temperature=0.0).complete(REQUEST)
    request, body = sent(transport)
    assert request.url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
    assert KEY not in request.url and request.headers["x-goog-api-key"] == KEY
    assert body == {
        "systemInstruction": {"parts": [{"text": "SYSTEM TEXT"}]},
        "contents": [{"role": "user", "parts": [{"text": "USER TEXT"}]}],
        "generationConfig": {"maxOutputTokens": 4096, "temperature": 0.0},
    }


def test_moonshot_request():
    transport = FakeTransport((200, MOONSHOT_RESPONSE))
    make("moonshot_chat", transport).complete(REQUEST)
    request, body = sent(transport)
    assert request.url == "https://api.moonshot.ai/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert body == {
        "model": "kimi-k3.1-pro",
        "messages": [{"role": "system", "content": "SYSTEM TEXT"}, {"role": "user", "content": "USER TEXT"}],
        "max_tokens": 4096,
    }


def test_request_params_override_the_client_defaults_and_unknown_params_fail_loudly():
    transport = FakeTransport((200, MOONSHOT_RESPONSE))
    client = make("moonshot_chat", transport, temperature=0.0)
    client.complete(ModelRequest(REQUEST.messages, {"max_output_tokens": 64, "temperature": 0.5}))
    _request, body = sent(transport)
    assert (body["max_tokens"], body["temperature"]) == (64, 0.5)
    with pytest.raises(ModelError, match="unsupported request parameter"):
        client.complete(ModelRequest(REQUEST.messages, {"top_p": 0.1}))


# -- response parsing ---------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,served,tokens_in,tokens_out,reasoning",
    [
        ("openai_responses", "gpt-6-luna-2026-10-01", 900, 340, 300),
        ("anthropic_messages", "claude-haiku-5-5-20261001", 910, 120, None),
        ("google_gemini", "gemini-3.5-flash-001", 905, 300, 260),  # 40 visible + 260 thinking
        ("moonshot_chat", "kimi-k3.1-pro", 880, 210, 170),
    ],
)
def test_response_parsing_records_text_served_model_and_usage(name, served, tokens_in, tokens_out, reasoning):
    _secret, model_id, body = PROVIDERS[name]
    response = make(name, FakeTransport((200, body))).complete(REQUEST)
    assert response.text == '{"status": "abstained"}'
    assert response.model == served
    assert (response.usage.input_tokens, response.usage.output_tokens) == (tokens_in, tokens_out)
    raw = response.raw
    assert raw["requested_model"] == model_id and raw["provider_reported_model"] == served
    assert raw["reasoning_tokens"] == reasoning
    assert raw["truncated"] is False
    assert raw["request_params"]["model" if name != "google_gemini" else "generationConfig"]
    assert "USER TEXT" not in json.dumps(raw) and KEY not in json.dumps(raw)


@pytest.mark.parametrize(
    "name,patch",
    [
        ("openai_responses", {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}),
        ("anthropic_messages", {"stop_reason": "max_tokens"}),
        ("google_gemini", {"candidates": [{"content": {"parts": [{"text": "{"}]}, "finishReason": "MAX_TOKENS"}]}),
        ("moonshot_chat", {"choices": [{"message": {"content": "{"}, "finish_reason": "length"}]}),
    ],
)
def test_truncation_is_flagged(name, patch):
    body = {**PROVIDERS[name][2], **patch}
    assert make(name, FakeTransport((200, body))).complete(REQUEST).raw["truncated"] is True


def test_missing_usage_and_model_are_reported_as_missing_not_invented():
    body = {k: v for k, v in MOONSHOT_RESPONSE.items() if k not in {"usage", "model"}}
    response = make("moonshot_chat", FakeTransport((200, body))).complete(REQUEST)
    assert response.usage is None
    assert response.model == "kimi-k3.1-pro" and response.raw["provider_reported_model"] is None


def test_gemini_blocked_prompt_gives_empty_text_with_the_reason():
    body = {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 10}}
    response = make("google_gemini", FakeTransport((200, body))).complete(REQUEST)
    assert response.text == "" and response.raw["finish_reason"] == "blocked:SAFETY"


def test_openai_failed_response_raises():
    body = {**OPENAI_RESPONSE, "status": "failed", "error": {"code": "server_error", "message": "boom"}}
    with pytest.raises(ModelError, match="failed"):
        make("openai_responses", FakeTransport((200, body))).complete(REQUEST)


# -- errors -------------------------------------------------------------------- #


@pytest.mark.parametrize("status,retryable", [(429, True), (500, True), (503, True), (529, True), (400, False), (401, False), (404, False)])
def test_http_errors_say_whether_to_retry_and_never_retry_themselves(status, retryable):
    transport = FakeTransport((status, {"error": {"message": "nope"}}), (200, OPENAI_RESPONSE))
    with pytest.raises(ModelError) as caught:
        make("openai_responses", transport).complete(REQUEST)
    assert caught.value.status == status and caught.value.retryable is retryable
    assert caught.value.retry_after == (7.0 if status == 429 else None)
    assert len(transport.requests) == 1
    assert KEY not in str(caught.value)


def test_transport_errors_are_retryable():
    transport = FakeTransport(ModelError("transport error: timeout", retryable=True))
    with pytest.raises(ModelError) as caught:
        make("anthropic_messages", transport).complete(REQUEST)
    assert caught.value.retryable and caught.value.status is None


def test_missing_key_is_a_secret_error_and_no_request_is_sent(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    transport = FakeTransport((200, GEMINI_RESPONSE))
    with pytest.raises(SecretError, match="GEMINI_API_KEY"):
        make("google_gemini", transport).complete(REQUEST)
    assert transport.requests == []


def test_every_client_secret_is_documented_and_construction_needs_no_key(monkeypatch):
    for name, (secret, _model_id, _body) in PROVIDERS.items():
        monkeypatch.delenv(secret)
        client = make(name, FakeTransport())
        assert client.secret_name == secret and secret in KNOWN_SECRETS
        assert client.name == "label-x"


def test_options_are_validated():
    with pytest.raises(ValueError):
        make("openai_responses", FakeTransport(), model_id="")
    with pytest.raises(ValueError):
        make("openai_responses", FakeTransport(), max_output_tokens=0)
    with pytest.raises(ValueError):
        make("openai_responses", FakeTransport(), temperature=-1)
    with pytest.raises(TypeError):
        make("openai_responses", FakeTransport(), no_such_option=1)
    with pytest.raises(ValueError):
        make("anthropic_messages", FakeTransport(), thinking="on")


def test_retry_policy_backoff_is_exponential_bounded_and_honours_retry_after():
    policy = RetryPolicy(max_retries=5, backoff_seconds=2.0, backoff_multiplier=2.0, max_backoff_seconds=10.0)
    assert [policy.delay(n) for n in (1, 2, 3, 4)] == [2.0, 4.0, 8.0, 10.0]
    assert policy.delay(1, retry_after=7.0) == 7.0
    assert policy.delay(1, retry_after=500.0) == 10.0
    with pytest.raises(ValueError):
        RetryPolicy(max_retries=-1)


# -- preflight ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "name,url,found",
    [
        ("openai_responses", "https://api.openai.com/v1/models/gpt-6-luna", {"id": "gpt-6-luna", "object": "model"}),
        ("anthropic_messages", "https://api.anthropic.com/v1/models/claude-haiku-5-5", {"id": "claude-haiku-5-5", "type": "model"}),
        ("google_gemini", "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash", {"name": "models/gemini-3.5-flash"}),
        ("moonshot_chat", "https://api.moonshot.ai/v1/models", {"object": "list", "data": [{"id": "other"}, {"id": "kimi-k3.1-pro"}]}),
    ],
)
def test_preflight_uses_the_free_models_endpoint(name, url, found):
    transport = FakeTransport((200, found))
    result = make(name, transport).preflight()
    assert result.ok and result.label == "label-x"
    request = transport.requests[0]
    assert (request.method, request.url, request.body) == ("GET", url, None)


@pytest.mark.parametrize(
    "name,status,body",
    [
        ("openai_responses", 404, {"error": {"message": "The model does not exist"}}),
        ("anthropic_messages", 404, {"type": "error", "error": {"type": "not_found_error"}}),
        ("google_gemini", 404, {"error": {"status": "NOT_FOUND"}}),
        ("moonshot_chat", 200, {"object": "list", "data": [{"id": "kimi-k2.5"}]}),
        ("openai_responses", 200, {"id": "some-other-model"}),
        ("openai_responses", 401, {"error": {"message": "bad key"}}),
    ],
)
def test_preflight_fails_loudly_when_the_model_id_is_not_listed(name, status, body):
    result = make(name, FakeTransport((status, body))).preflight()
    assert not result.ok
    assert PROVIDERS[name][1] in result.detail and KEY not in result.detail
