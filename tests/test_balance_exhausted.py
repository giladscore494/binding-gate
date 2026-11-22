"""PR 19: a provider's prepaid balance running out stops the run cleanly and resumably."""

from __future__ import annotations

import json

import pytest

import fakes
import run_helpers as h
from binding_gate import runner
from binding_gate.models import _http
from binding_gate.trace import read_manifest, read_traces


class _Transport:
    def __init__(self, status: int, body: str):
        self.status, self.body = status, body

    def __call__(self, request):
        return _http.HttpResponse(self.status, self.body.encode("utf-8"), {})


@pytest.mark.parametrize(
    "status,body",
    [
        (402, "{}"),
        (429, '{"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}'),
        (400, '{"error": {"message": "Insufficient balance"}}'),
        (403, '{"error": {"message": "\u4f59\u989d\u4e0d\u8db3"}}'),
        (400, '{"type": "error", "error": {"message": "Your credit balance is too low"}}'),
    ],
)
def test_exhausted_balance_is_detected_and_not_retryable(status, body):
    with pytest.raises(_http.ModelError) as info:
        _http.request_json(_Transport(status, body), "POST", "https://x/y", headers={}, payload={}, timeout=1)
    assert info.value.retryable is False
    assert str(info.value).startswith(_http.BALANCE_EXHAUSTED_PREFIX)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_ordinary_rate_limits_and_server_errors_stay_retryable(status):
    with pytest.raises(_http.ModelError) as info:
        _http.request_json(_Transport(status, '{"error": {"message": "Rate limit reached, slow down"}}'), "POST", "https://x/y", headers={}, payload={}, timeout=1)
    assert info.value.retryable is True
    assert not str(info.value).startswith(_http.BALANCE_EXHAUSTED_PREFIX)


def run(repo, config, tmp_path, **kw):
    return runner.execute_run(config, runs_dir=tmp_path / "runs", repo_root=repo, confirm_spend=True, **kw)


def test_run_stops_cleanly_writes_no_failed_record_and_resumes_after_top_up(tmp_path):
    broke = {"name": "scripted_fake", "label": "broke", "options": {"model_id": "fake-cheap", "balance_after": 3}}
    repo, config, _raw = h.setup(tmp_path, models=[broke], repeats=1, runner={"concurrency": 1, "max_usd": 5.0})
    messages = []
    fakes.CALLS.clear()
    summary = run(repo, config, tmp_path, log=messages.append)
    assert summary.balance_exhausted and summary.budget_exceeded and not summary.closed
    records = list(read_traces(summary.run_dir))
    assert len(records) == 3 and all(r.error is None for r in records)
    assert any("BALANCE EXHAUSTED" in m for m in messages)
    events = [json.loads(line) for line in (summary.run_dir / "invocations.jsonl").read_text().splitlines()]
    assert any(e.get("event") == "balance_exhausted" for e in events)
    assert read_manifest(summary.run_dir).ended_at is None
    # one failed call, never retried
    assert len(fakes.CALLS) == 4
