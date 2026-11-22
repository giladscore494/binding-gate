"""Search clients: Serper (the study's, PR 27) and SerpApi (PR 8, kept): requests, responses, errors, preflight, keys."""

from __future__ import annotations

import json
import urllib.parse

import pytest

import live_fakes as lf
from binding_gate.answering import ModelCallFailed, call_model
from binding_gate.contracts import Message, ModelRequest
from binding_gate.models._http import HttpResponse, ModelError
from binding_gate.research.search import (
    SEARCH_CLIENTS,
    SERPAPI_MODEL_ID,
    SERPER_MODEL_ID,
    SERPER_PREFLIGHT_QUERY,
    DryRunSearchClient,
    SerpApiSearchClient,
    SerperSearchClient,
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


SERPAPI_SPEC = {**lf.SEARCH_SPEC, "name": "serpapi_google"}


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
    search = build_search_client(SERPAPI_SPEC)
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
    real = build_search_client(SERPAPI_SPEC)
    dry = DryRunSearchClient(real)
    out = dry.complete(request("anything"))
    results = results_from_text(out.text)
    assert len(results) == 10 and out.model == SERPAPI_MODEL_ID and out.raw["dry_run"] is True
    assert results == results_from_text(DryRunSearchClient(real).complete(request("anything")).text)  # deterministic


# --------------------------------------------------------------------------- #
# Serper (PR 27): the study's search client
# --------------------------------------------------------------------------- #

SERPER_KEY = "serper-secret-456"
# Shape of a Serper /search response (serper.dev and third-party documentation, read 2026-11-22).
SERPER_DOCUMENTED = {
    "searchParameters": {"q": "Coffee", "gl": "us", "hl": "en", "num": 5, "type": "search", "engine": "google"},
    "organic": [
        {"title": "Coffee", "link": "https://en.wikipedia.org/wiki/Coffee",
         "snippet": "Coffee is a beverage prepared from roasted coffee beans.", "position": 1, "sitelinks": []},
        {"title": "No link", "position": 2},
        {"title": "Ftp", "link": "ftp://x.test/a", "snippet": "", "position": 3},
        {"title": "Second", "link": "http://b.test/", "snippet": "s", "position": 4, "date": "Nov 1, 2026"},
        "not a dict",
    ],
    "credits": 1,
}


def serper(transport, **options):
    return SerperSearchClient(transport=transport, min_interval_seconds=0.0, backoff_seconds=0.0, **options)


def test_serper_request_is_a_json_post_with_the_key_only_in_the_header(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", SERPER_KEY)
    seen = []

    def transport(req):
        seen.append(req)
        return HttpResponse(200, json.dumps(SERPER_DOCUMENTED).encode())

    response = serper(transport, max_results=5).complete(request())
    (req,) = seen
    assert (req.method, req.url) == ("POST", "https://google.serper.dev/search")
    assert req.headers["X-API-KEY"] == SERPER_KEY and req.headers["Content-Type"] == "application/json"
    assert json.loads(req.body) == {"q": "Coffee", "gl": "us", "hl": "en", "num": 5}
    assert SERPER_KEY not in req.url and SERPER_KEY.encode() not in req.body
    results = results_from_text(response.text)
    assert [(r.rank, r.title, r.url) for r in results] == [
        (1, "Coffee", "https://en.wikipedia.org/wiki/Coffee"), (4, "Second", "http://b.test/")]
    assert response.model == SERPER_MODEL_ID == "serper/google" and response.usage is None
    assert response.raw["provider"] == "serper" and response.raw["requested_model"] == SERPER_MODEL_ID
    assert response.raw["results"] == 2 and response.raw["credits"] == 1 and response.raw["no_results"] is False
    assert response.raw["request_params"] == {"q": "Coffee", "gl": "us", "hl": "en", "num": 5}
    assert SERPER_KEY not in json.dumps(response.to_dict())  # the key is never in the logged response


def test_serper_options_describe_and_blob_sink(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", SERPER_KEY)
    blobs = []
    body = json.dumps(SERPER_DOCUMENTED).encode()
    c = serper(lambda r: HttpResponse(200, body), max_results=3, hl="de", gl="at",
               blob_sink=lambda b: blobs.append(b) or "sha-of-body")
    assert c.describe() == {"provider": "serper", "model_id": "serper/google", "engine": "google",
                            "hl": "de", "gl": "at", "max_results": 3}
    assert c.secret_name == "SERPER_API_KEY" and c.min_interval_seconds == 0.0
    out = c.complete(request())
    assert blobs == [body] and out.raw["response_sha256"] == "sha-of-body"
    assert SerperSearchClient().min_interval_seconds == 1.0  # default spacing: Serper has no hourly limit
    with pytest.raises(ValueError):
        SerperSearchClient(max_results=0)
    with pytest.raises(ValueError):
        SerperSearchClient(hl="en-US!")
    with pytest.raises(ValueError):
        SerperSearchClient(min_interval_seconds=-1)


def test_serper_empty_or_missing_organic_is_a_successful_empty_search(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", SERPER_KEY)
    for body in ({"searchParameters": {"q": "x"}, "organic": []}, {"searchParameters": {"q": "x"}}):
        out = serper(lambda r, b=body: HttpResponse(200, json.dumps(b).encode())).complete(request())
        assert results_from_text(out.text) == [] and out.raw["no_results"] is True
    with pytest.raises(ModelError):
        serper(lambda r: HttpResponse(200, json.dumps({"organic": "nope"}).encode())).complete(request())


def test_serper_keeps_at_most_max_results_and_falls_back_to_list_order(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", SERPER_KEY)
    organic = [{"title": f"t{i}", "link": f"https://r{i}.test/"} for i in range(15)]
    out = serper(lambda r: HttpResponse(200, json.dumps({"organic": organic}).encode()), max_results=10).complete(request())
    results = results_from_text(out.text)
    assert [r.rank for r in results] == list(range(1, 11)) and results[-1].url == "https://r9.test/"


@pytest.mark.parametrize(
    "status,body,retryable,balance",
    [
        (401, {"message": "Unauthorized.", "statusCode": 401}, False, False),
        (403, {"message": "Unauthorized.", "statusCode": 403}, False, False),
        (400, {"message": "Not enough credits", "statusCode": 400}, False, True),
        (402, {"message": "Payment required", "statusCode": 402}, False, True),
        (400, {"message": "Query is required", "statusCode": 400}, False, False),
        (429, {"message": "Too many requests", "statusCode": 429}, True, False),
        (500, {"message": "Internal error", "statusCode": 500}, True, False),
        (503, None, True, False),
    ],
)
def test_serper_errors_say_whether_to_retry_and_never_contain_the_key(monkeypatch, status, body, retryable, balance):
    monkeypatch.setenv("SERPER_API_KEY", SERPER_KEY)
    raw = b"<html>busy</html>" if body is None else json.dumps({**body, "echo": f"X-API-KEY: {SERPER_KEY}"}).encode()
    with pytest.raises(ModelError) as info:
        serper(lambda r: HttpResponse(status, raw)).complete(request())
    assert info.value.retryable is retryable and info.value.status == status
    assert SERPER_KEY not in str(info.value)
    assert str(info.value).startswith("BALANCE EXHAUSTED") is balance


def test_serper_transport_errors_are_retryable_and_scrubbed(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", SERPER_KEY)

    def broken(req):
        raise ModelError(f"transport error: reset while sending {req.headers['X-API-KEY']}", retryable=True)

    with pytest.raises(ModelError) as info:
        serper(broken).complete(request())
    assert info.value.retryable and SERPER_KEY not in str(info.value)


def test_serper_rate_limit_spaces_searches(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", SERPER_KEY)
    waits, now = [], [10.0]
    body = json.dumps(SERPER_DOCUMENTED).encode()
    c = SerperSearchClient(transport=lambda r: HttpResponse(200, body), min_interval_seconds=1.5,
                           sleep=waits.append, monotonic=lambda: now[0])
    for _ in range(3):
        c.complete(request())
    assert waits == [1.5, 3.0]


def test_serper_retries_through_call_model_on_429_but_not_on_401(monkeypatch):
    web, serp = lf.install(monkeypatch, key=SERPER_KEY)
    search = build_search_client(lf.SEARCH_SPEC)
    assert isinstance(search, SerperSearchClient)
    serp.status_sequence = [429]
    response, calls = call_model(search, request(lf.Q_EXAMPLE), "search", sleep=lambda s: None)
    assert [c.error is None for c in calls] == [False, True] and len(results_from_text(response.text)) == 3
    serp.status_sequence = [401]
    with pytest.raises(ModelCallFailed) as info:
        call_model(search, request(lf.Q_EXAMPLE), "search", sleep=lambda s: None)
    assert len(info.value.model_calls) == 1  # not retryable
    assert all(SERPER_KEY not in json.dumps(c.to_dict()) for c in info.value.model_calls)


def test_serper_preflight_is_one_real_search_and_reports_no_quota(monkeypatch):
    web, serp = lf.install(monkeypatch, key=SERPER_KEY)
    search = build_search_client(lf.SEARCH_SPEC)
    result = search.preflight()
    assert result.ok and "one credit used" in result.detail and SERPER_KEY not in result.detail
    assert search.searches_left is None  # unknown: the runner keeps [runner] max_searches as the cap
    assert serp.preflights == [SERPER_PREFLIGHT_QUERY] and serp.queries == []
    (req,) = serp.requests
    assert req.method == "POST" and json.loads(req.body)["q"] == "binding-gate preflight"
    serp.preflight_status = 403
    failed = search.preflight()
    assert not failed.ok and "403" in failed.detail and SERPER_KEY not in failed.detail
    serp.preflight_status = 400  # e.g. out of credits
    assert not search.preflight().ok


def test_serper_preflight_needs_an_organic_list(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", SERPER_KEY)
    c = serper(lambda r: HttpResponse(200, json.dumps({"searchParameters": {}}).encode()))
    result = c.preflight()
    assert not result.ok and "organic" in result.detail and c.searches_left is None
    ok = serper(lambda r: HttpResponse(200, json.dumps({"organic": []}).encode())).preflight()
    assert ok.ok


def test_serper_missing_key_is_a_secret_error_and_the_secret_is_documented(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    assert "SERPER_API_KEY" in KNOWN_SECRETS
    with pytest.raises(SecretError):
        serper(lambda r: HttpResponse(200, b"{}")).complete(request())
    with pytest.raises(SecretError):
        serper(lambda r: HttpResponse(200, b"{}")).preflight()


def test_serper_is_registered_and_the_dry_run_stand_in_works_with_it():
    assert SEARCH_CLIENTS["serper_google"] is SerperSearchClient
    assert SEARCH_CLIENTS["serpapi_google"] is SerpApiSearchClient  # kept, unused by the study configs
    assert normalise_search_spec({"name": "serper_google"}) == {"name": "serper_google", "label": "search", "options": {}}
    real = build_search_client({"name": "serper_google", "options": {"max_results": 7}})
    dry = DryRunSearchClient(real)
    assert dry.model_id == SERPER_MODEL_ID and dry.secret_name == "SERPER_API_KEY" and dry.describe() == real.describe()
    out = dry.complete(request("anything"))
    assert len(results_from_text(out.text)) == 7 and out.model == SERPER_MODEL_ID and out.raw["dry_run"] is True
