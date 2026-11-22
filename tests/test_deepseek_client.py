"""PR 13: the DeepSeek client (slot G), against recorded-shape fixtures only (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from binding_gate import judging, registry, runner
from binding_gate.config import load_config
from binding_gate.secrets import KNOWN_SECRETS
from test_model_clients import KEY, REQUEST, FakeTransport, sent

# The documented OpenAI-format chat completion shape (api-docs.deepseek.com, read 2026-11-22).
DEEPSEEK_RESPONSE = {
    "id": "ds-1",
    "object": "chat.completion",
    "model": "deepseek-v4.1-flash-1017",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": '{"status": "abstained"}'}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 870, "completion_tokens": 60, "total_tokens": 930},
}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)


def make(transport, **options):
    options.setdefault("model_id", "deepseek-flash")
    options.setdefault("max_output_tokens", 4096)
    return registry.create("model", "deepseek_chat", label="deepseek-low", transport=transport, **options)


def test_request_goes_to_deepseek_with_non_thinking_mode_and_temperature():
    transport = FakeTransport((200, DEEPSEEK_RESPONSE))
    make(transport, temperature=0.0, thinking={"type": "disabled"}).complete(REQUEST)
    request, body = sent(transport)
    assert request.url == "https://api.deepseek.com/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert body == {
        "model": "deepseek-flash",
        "messages": [{"role": "system", "content": "SYSTEM TEXT"}, {"role": "user", "content": "USER TEXT"}],
        "max_tokens": 4096,
        "temperature": 0.0,
        "thinking": {"type": "disabled"},
    }


def test_without_the_option_no_thinking_field_is_sent():
    transport = FakeTransport((200, DEEPSEEK_RESPONSE))
    make(transport).complete(REQUEST)
    _request, body = sent(transport)
    assert "thinking" not in body


def test_response_records_the_version_the_alias_was_served_by():
    response = make(FakeTransport((200, DEEPSEEK_RESPONSE))).complete(REQUEST)
    assert response.text == '{"status": "abstained"}'
    assert response.raw["requested_model"] == "deepseek-flash"
    assert response.raw["provider_reported_model"] == "deepseek-v4.1-flash-1017"  # alias drift is visible
    assert (response.usage.input_tokens, response.usage.output_tokens) == (870, 60)
    assert KEY not in json.dumps(response.raw)


def test_truncation_is_flagged():
    body = {**DEEPSEEK_RESPONSE, "choices": [{"message": {"content": "{"}, "finish_reason": "length"}]}
    assert make(FakeTransport((200, body))).complete(REQUEST).raw["truncated"] is True


def test_preflight_lists_the_models_for_free():
    transport = FakeTransport((200, {"object": "list", "data": [{"id": "deepseek-v4-pro"}, {"id": "deepseek-flash"}]}))
    assert make(transport).preflight().ok
    request = transport.requests[0]
    assert (request.method, request.url, request.body) == ("GET", "https://api.deepseek.com/models", None)
    missing = make(FakeTransport((200, {"object": "list", "data": [{"id": "deepseek-v4-pro"}]}))).preflight()
    assert not missing.ok and "deepseek-flash" in missing.detail


def test_thinking_must_be_a_table():
    with pytest.raises(ValueError):
        make(FakeTransport(), thinking="disabled")


def test_secret_family_and_the_committed_slot_g():
    assert "DEEPSEEK_API_KEY" in KNOWN_SECRETS
    client = make(FakeTransport())
    assert client.secret_name == "DEEPSEEK_API_KEY" and judging.model_family(client) == "deepseek"
    config = load_config(Path(__file__).resolve().parents[1] / "configs" / "dev_baseline.toml")
    spec = {m.label: m for m in config.models}["deepseek-low"]
    built = runner.build_model(spec)
    assert built.model_id == "deepseek-flash" and built.thinking == {"type": "disabled"}
