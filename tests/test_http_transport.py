"""The real standard-library transport against a local HTTP server (loopback only)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from binding_gate import registry
from binding_gate.contracts import Message, ModelRequest
from binding_gate.models._http import ModelError

REQUEST = ModelRequest((Message("system", "S"), Message("user", "U")))
BODY = {
    "id": "resp_1",
    "status": "completed",
    "model": "gpt-local-1",
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}],
    "usage": {"input_tokens": 3, "output_tokens": 2, "output_tokens_details": {"reasoning_tokens": 0}},
}


class Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []
    script: list[int] = []

    def log_message(self, *args):  # keep test output quiet
        pass

    def _reply(self, status: int, body: dict, headers: dict | None = None) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        Handler.seen.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": json.loads(self.rfile.read(length))}
        )
        status = Handler.script.pop(0) if Handler.script else 200
        if status == 200:
            self._reply(200, BODY)
        else:
            self._reply(status, {"error": {"message": "scripted"}}, {"Retry-After": "3"})

    def do_GET(self):
        if self.path == "/v1/models/gpt-local":
            self._reply(200, {"id": "gpt-local", "object": "model"})
        else:
            self._reply(404, {"error": {"message": "no such model"}})


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-local-test")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    Handler.seen, Handler.script = [], []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()
    httpd.server_close()


def client(base_url: str, model_id: str = "gpt-local"):
    return registry.create(
        "model", "openai_responses", label="local", model_id=model_id, max_output_tokens=64, base_url=base_url, timeout_seconds=10
    )


def test_a_real_http_round_trip(server):
    response = client(server).complete(REQUEST)
    assert (response.text, response.model) == ("hello", "gpt-local-1")
    assert (response.usage.input_tokens, response.usage.output_tokens) == (3, 2)
    (seen,) = Handler.seen
    assert seen["path"] == "/v1/responses" and seen["auth"] == "Bearer sk-local-test"
    assert seen["body"]["instructions"] == "S" and seen["body"]["input"] == [{"role": "user", "content": "U"}]


def test_http_error_statuses_surface_with_retry_information(server):
    Handler.script[:] = [429, 400]
    with pytest.raises(ModelError) as rate_limited:
        client(server).complete(REQUEST)
    assert (rate_limited.value.status, rate_limited.value.retryable, rate_limited.value.retry_after) == (429, True, 3.0)
    with pytest.raises(ModelError) as bad_request:
        client(server).complete(REQUEST)
    assert (bad_request.value.status, bad_request.value.retryable) == (400, False)
    assert "sk-local-test" not in str(bad_request.value)


def test_preflight_over_http(server):
    assert client(server).preflight().ok
    missing = client(server, "gpt-missing").preflight()
    assert not missing.ok and "does not exist" in missing.detail


def test_connection_failure_is_a_retryable_transport_error(server):
    dead = client("http://127.0.0.1:9/v1")
    with pytest.raises(ModelError) as caught:
        dead.complete(REQUEST)
    assert caught.value.retryable and caught.value.status is None
