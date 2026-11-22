"""PR 14: the Z.ai (GLM) client (slots H and I), against recorded-shape fixtures only (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from binding_gate import judging, registry, runner
from binding_gate.config import load_config
from binding_gate.secrets import KNOWN_SECRETS, SecretError
from test_model_clients import KEY, REQUEST, FakeTransport, sent

REPO_ROOT = Path(__file__).resolve().parents[1]

# The documented OpenAI-format chat completion shape (docs.z.ai/api-reference/llm/chat-completion,
# read 2026-11-22): choices[].message.content (+ reasoning_content for the thinking),
# usage.prompt_tokens / completion_tokens / prompt_tokens_details.cached_tokens / total_tokens.
ZAI_RESPONSE = {
    "id": "20261122-zai-1",
    "request_id": "req-1",
    "created": 1795000000,
    "model": "glm-5.3-flash",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": '{"status": "abstained"}', "reasoning_content": "hidden thinking"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 880, "completion_tokens": 240, "prompt_tokens_details": {"cached_tokens": 0}, "total_tokens": 1120},
}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", KEY)


def make(transport, **options):
    options.setdefault("model_id", "glm-5.3-flash")
    options.setdefault("max_output_tokens", 4096)
    return registry.create("model", "zai_chat", label="zai-low", transport=transport, **options)


def test_request_goes_to_the_general_api_with_the_lowest_reasoning_setting():
    transport = FakeTransport((200, ZAI_RESPONSE))
    make(transport, temperature=0.0, thinking={"type": "enabled"}, reasoning_effort="low").complete(REQUEST)
    request, body = sent(transport)
    assert (request.method, request.url) == ("POST", "https://api.z.ai/api/paas/v4/chat/completions")
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert body == {
        "model": "glm-5.3-flash",
        "messages": [{"role": "system", "content": "SYSTEM TEXT"}, {"role": "user", "content": "USER TEXT"}],
        "max_tokens": 4096,
        "temperature": 0.0,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
    }


def test_without_the_options_neither_field_is_sent():
    transport = FakeTransport((200, ZAI_RESPONSE))
    make(transport).complete(REQUEST)
    _request, body = sent(transport)
    assert "thinking" not in body and "reasoning_effort" not in body and "temperature" not in body


def test_response_text_excludes_the_thinking_and_records_the_returned_model():
    response = make(FakeTransport((200, ZAI_RESPONSE))).complete(REQUEST)
    assert response.text == '{"status": "abstained"}'
    assert response.raw["provider"] == "zai"
    assert response.raw["requested_model"] == "glm-5.3-flash"
    assert response.raw["provider_reported_model"] == "glm-5.3-flash"  # a version change would be visible here
    assert (response.usage.input_tokens, response.usage.output_tokens) == (880, 240)
    assert response.raw["reasoning_tokens"] is None  # not documented by Z.ai; recorded when reported
    assert response.raw["request_params"] == {"model": "glm-5.3-flash", "max_tokens": 4096}  # the prompt is not repeated
    assert KEY not in json.dumps(response.raw)


def test_reasoning_tokens_are_recorded_when_reported():
    usage = {**ZAI_RESPONSE["usage"], "completion_tokens_details": {"reasoning_tokens": 190}}
    response = make(FakeTransport((200, {**ZAI_RESPONSE, "usage": usage}))).complete(REQUEST)
    assert response.raw["reasoning_tokens"] == 190


@pytest.mark.parametrize("finish,truncated", [("length", True), ("sensitive", False), ("stop", False)])
def test_finish_reason_is_recorded_and_length_is_a_truncation(finish, truncated):
    body = {**ZAI_RESPONSE, "choices": [{"message": {"content": ""}, "finish_reason": finish}]}
    raw = make(FakeTransport((200, body))).complete(REQUEST).raw
    assert raw["finish_reason"] == finish and raw["truncated"] is truncated


def test_preflight_lists_the_models_for_free():
    transport = FakeTransport((200, {"object": "list", "data": [{"id": "glm-5.3"}, {"id": "glm-5.3-flash"}]}))
    result = make(transport).preflight()
    assert result.ok
    request = transport.requests[0]
    assert (request.method, request.url, request.body) == ("GET", "https://api.z.ai/api/paas/v4/models", None)
    missing = make(FakeTransport((200, {"object": "list", "data": [{"id": "glm-5.3"}]}))).preflight()
    assert not missing.ok and "glm-5.3-flash" in missing.detail


def test_preflight_fails_and_never_passes_silently_if_the_endpoint_is_missing():
    """The list-models endpoint is not documented by Z.ai (UNVERIFIED); a 404 is a failed preflight."""
    result = make(FakeTransport((404, {"error": {"code": "404", "message": "Not Found"}}))).preflight()
    assert not result.ok and "HTTP 404" in result.detail


def test_the_list_check_can_be_switched_off_in_the_config_and_then_says_unverified():
    transport = FakeTransport()
    result = make(transport, preflight_list_models=False).preflight()
    assert result.ok and "UNVERIFIED" in result.detail and transport.requests == []


def test_preflight_without_the_key_refuses(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY")
    for options in ({}, {"preflight_list_models": False}):
        with pytest.raises(SecretError, match="ZAI_API_KEY"):
            make(FakeTransport(), **options).preflight()


@pytest.mark.parametrize(
    "options",
    [{"thinking": "enabled"}, {"reasoning_effort": ""}, {"reasoning_effort": 1}, {"preflight_list_models": "no"}],
)
def test_malformed_options_are_refused(options):
    with pytest.raises(ValueError):
        make(FakeTransport(), **options)


def test_secret_family_and_the_committed_slots_h_and_i():
    assert "ZAI_API_KEY" in KNOWN_SECRETS
    client = make(FakeTransport())
    assert client.secret_name == "ZAI_API_KEY" and judging.model_family(client) == "zai"
    smoke = {m.label: m for m in load_config(REPO_ROOT / "configs" / "dev_smoke.toml").models}
    for label, model_id in (("zai-low", "glm-5.3-flash"), ("zai-mid", "glm-5.3")):
        built = runner.build_model(smoke[label])
        assert built.model_id == model_id and built.base_url == "https://api.z.ai/api/paas/v4"
        assert built.thinking == {"type": "enabled"} and built.reasoning_effort == "low"
        assert built.preflight_list_models is True and built.temperature == 0.0
