"""SerpApi search client: request building, documented response shapes, errors, preflight, key hygiene."""

from __future__ import annotations

import json
import urllib.parse

import pytest

import live_fakes as lf
from binding_gate.answering import ModelCallFailed, call_model
from binding_gate.contracts import Message, ModelRequest
from binding_gate.models._http import HttpResponse, ModelError
from binding_gate.research.search import (
    SERPAPI_MODEL_ID,
    DryRunSearchClient,
    SerpApiSearchClient,
    build_search_client,
    normalise_search_spec,
    parse_organic_results,
    results_from_text,
)
from binding_gate.secrets import KNOWN_SECRETS, SecretError

KEY = "serp-secret-123"
# Shape of a SerpApi Google response, from serpapi.com/search-api and serpapi.com/organic-results.
DOCUMENTED = {
    "search_metadata": {"id": "644b696a15afff2c2fdb8474", "status": "Success", "created_at": "2026-11-22 10:00:00 UTC"},
    "search_parameters": {"engine": "google", "q": "Coffee"},
    "search_information": {"organic_results_state": "Results for exact spelling", "total_results": 1},
    "organic_results": [
        {"position": 1, "title": "Coffee", "link": "https://en.wikipedia.org/wiki/Coffee",
         "redirect_link": "https://www.google.com/url?...", "displayed_link": "https://en.wikipedia.org › wiki › Coffee",
         "snippet": "Coffee is a beverage prepared from roasted coffee beans.", "source": "Wikipedia"},
        {"position": 2, "title": "No link"},
        {"position": 3, "title": "Ftp", "link": "ftp://x.test/a", "snippet": ""},
        {"position": 4, "title": "Second", "link": "http://b.test/", "snippet": "s"},
    ],
}


def client(transport, **options):
    return SerpApiSearchClient(transport=transport, min_interval_seconds=0.0, backoff_seconds=0.0, **options)


def request(query="Coffee"):
    return ModelRequest((Message("user", query),))


def test_request_parameters_and_documented_response_parsing(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", KEY)
    seen = []

    def transport(req):
        seen.append(req)
        return HttpResponse(200, json.dumps(DOCUMENTED).encode())

    response = client(transport, max_results=5).complete(request())
    (req,) = seen
    parts = urllib.parse.urlsplit(req.url)
    params = dict(urllib.parse.parse_qsl(parts.query))
    assert (parts.scheme, parts.netloc, parts.path, req.method) == ("https", "serpapi.com", "/search", "GET")
    assert params == {"engine": "google", "q": "Coffee", "hl": "en", "gl": "us", "output": "json", "api_key": KEY}
    results = results_from_text(response.text)
    assert [(r.rank, r.url) for r in results] == [(1, "https://en.wikipedia.org/wiki/Coffee"), (4, "http://b.test/")]
    assert response.model == SERPAPI_MODEL_ID and response.usage is None
    assert response.raw["search_id"] == "644b696a15afff2c2fdb8474" and response.raw["requested_model"] == SERPAPI_MODEL_ID
    assert KEY not in json.dumps(response.to_dict())  # the key is never in the logged response


def test_max_results_limits_and_ranks_fall_back_to_order():
    raw = [{"title": "a", "link": "https://a.test"}, {"position": True, "title": "b", "link": "https://b.test"}]
    assert [(r.rank, r.url) for r in parse_organic_results(raw, 10)] == [(1, "https://a.test"), (2, "https://b.test")]
    assert len(parse_organic_results(raw, 1)) == 1 and parse_organic_results(None, 3) == []


def test_no_results_is_a_successful_empty_search(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", KEY)
    body = {"search_metadata": {"status": "Success"}, "error": "Google hasn't returned any results for this query."}
    response = client(lambda r: HttpResponse(200, json.dumps(body).encode())).complete(request())
    assert results_from_text(response.text) == [] and response.raw["no_results"] is True


@pytest.mark.parametrize(
    "status,body,retryable",
    [
        (429, {"error": "Your account has run out of searches."}, True),
        (503, {"error": "busy"}, True),
        (401, {"error": "Invalid API key."}, False),
        (400, {"error": "Missing query `q` parameter."}, False),
        (200, {"search_metadata": {"status": "Error"}, "error": "something broke"}, False),
    ],
)
def test_errors_say_whether_to_retry_and_never_contain_the_key(monkeypatch, status, body, retryable):
    monkeypatch.setenv("SERPAPI_API_KEY", KEY)
    body = {**body, "echo": f"https://serpapi.com/search?api_key={KEY}"}
    with pytest.raises(ModelError) as info:
        client(lambda r: HttpResponse(status, json.dumps(body).encode())).complete(request())
    assert info.value.retryable is retryable and KEY not in str(info.value)


def test_retries_are_logged_by_call_model_as_search_calls(monkeypatch):
    web, serp = lf.install(monkeypatch)
    serp.status_sequence = [429, 429]
    search = build_search_client(lf.SEARCH_SPEC)
    response, calls = call_model(search, request(lf.Q_EXAMPLE), "search", sleep=lambda s: None)
    assert [c.purpose for c in calls] == ["search"] * 3 and [c.error is None for c in calls] == [False, False, True]
    assert len(results_from_text(response.text)) == 3
    serp.status_sequence = [429, 429, 429]
    with pytest.raises(ModelCallFailed) as info:
        call_model(search, request(lf.Q_EXAMPLE), "search", sleep=lambda s: None)
    assert len(info.value.model_calls) == 3  # max_retries = 2 in the test spec


def test_rate_limit_spaces_searches(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", KEY)
    waits, now = [], [10.0]
    body = json.dumps(DOCUMENTED).encode()
    c = SerpApiSearchClient(transport=lambda r: HttpResponse(200, body), min_interval_seconds=18.0,
                            sleep=waits.append, monotonic=lambda: now[0])
    c.complete(request())
    c.complete(request())
    c.complete(request())
    assert waits == [18.0, 36.0]


def test_preflight_uses_the_free_account_api_and_hides_the_key(monkeypatch):
    web, serp = lf.install(monkeypatch, key=KEY)
    search = build_search_client(lf.SEARCH_SPEC)
    result = search.preflight()
    assert result.ok and "250 searches left" in result.detail and KEY not in result.detail
    assert "should-not-leak" not in result.detail
    assert urllib.parse.urlsplit(serp.urls[-1]).path == "/account.json" and serp.queries == []
    serp.account["total_searches_left"] = 0
    assert not search.preflight().ok


def test_a_missing_key_is_a_secret_error_and_the_secret_is_documented(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    assert "SERPAPI_API_KEY" in KNOWN_SECRETS
    with pytest.raises(SecretError):
        client(lambda r: HttpResponse(200, b"{}")).complete(request())


def test_spec_validation_and_dry_run_stand_in():
    with pytest.raises(ValueError, match="search.name"):
        normalise_search_spec({"name": "bing"})
    with pytest.raises(ValueError, match="unknown key"):
        normalise_search_spec({"name": "serpapi_google", "key": "x"})
    with pytest.raises(ValueError):
        build_search_client({"name": "serpapi_google", "options": {"max_results": 0}})
    real = build_search_client(lf.SEARCH_SPEC)
    dry = DryRunSearchClient(real)
    out = dry.complete(request("anything"))
    results = results_from_text(out.text)
    assert len(results) == 10 and out.model == SERPAPI_MODEL_ID and out.raw["dry_run"] is True
    assert results == results_from_text(DryRunSearchClient(real).complete(request("anything")).text)  # deterministic
