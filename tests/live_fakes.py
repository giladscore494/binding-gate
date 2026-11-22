"""Test doubles for layer 3: a fake Serper (and SerpApi), a fake web, a fake answering model and decomposer.

Nothing here touches the network. :func:`install` replaces the two standard-library
transports that the search client and the page fetcher use by default, so the
real code paths (request building, robots.txt, redirects, extraction, snapshot
store, runner) run unchanged against an invented world:

* "Which city hosted the 2031 Example Games?" (gold: Northbridge). Searching the
  question returns a page about the *Sample Games 2027* (hosted by Southport), a
  page whose robots.txt forbids us, and a 404. The targeted re-search
  ``"Example Games" 2031 host city`` returns the right page.
* "Which city hosted the 2027 Sample Games?" (gold: Southport) is answered and
  accepted on the first attempt.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from typing import Any

from binding_gate.contracts import (
    Decomposition,
    Entity,
    Message,
    ModelCall,
    ModelRequest,
    ModelResponse,
    Qualifier,
    QualifierKind,
    QuestionConstraints,
    TokenUsage,
)
from binding_gate.models._http import HttpRequest, HttpResponse, ModelError, PreflightResult, RetryPolicy
from binding_gate.registry import register
from binding_gate.research import fetch as fetch_mod
from binding_gate.research import search as search_mod
from binding_gate.research.fetch import FetchResponse, FetchTransportError

Q_EXAMPLE = "Which city hosted the 2031 Example Games?"
Q_SAMPLE = "Which city hosted the 2027 Sample Games?"
RESEARCH_QUERY_EXAMPLE = '"Example Games" 2031 host city'
RESEARCH_QUERY_SAMPLE = '"Sample Games" 2027 host city'

PAGE_A = "https://sample.test/games"
PAGE_BLOCKED = "https://blocked.test/page"
PAGE_GONE = "https://gone.test/x"
PAGE_D = "https://example.test/2031"

HTML_A = (
    "<html><head><title>Sample Games 2027</title><script>var x = 1;</script></head><body>"
    "<nav>Home | News</nav><p>The Sample Games 2027 were hosted by Southport and drew large crowds.</p>"
    "<p>The Sample Games are held every four years.</p></body></html>"
)
HTML_D = (
    "<html><head><title>Example Games 2031</title></head><body>"
    "<p>The Example Games 2031 were hosted by Northbridge after a long bid.</p></body></html>"
)


class FakeWeb:
    """Pages and robots.txt files by URL; records every request."""

    def __init__(self) -> None:
        self.pages: dict[str, tuple[int, dict[str, str], bytes]] = {
            PAGE_A: (200, {"Content-Type": "text/html; charset=utf-8"}, HTML_A.encode("utf-8")),
            PAGE_BLOCKED: (200, {"Content-Type": "text/html"}, b"<p>secret</p>"),
            PAGE_GONE: (404, {"Content-Type": "text/html"}, b"<p>not found</p>"),
            PAGE_D: (200, {"Content-Type": "text/html"}, HTML_D.encode("utf-8")),
            "https://sample.test/robots.txt": (404, {}, b""),
            "https://blocked.test/robots.txt": (200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /\n"),
            "https://gone.test/robots.txt": (404, {}, b""),
            "https://example.test/robots.txt": (200, {}, b"User-agent: binding-gate-research\nAllow: /\n"),
        }
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.fail: dict[str, str] = {}  # url -> "timeout" | "network_error"
        self._lock = threading.Lock()

    def transport(self, url: str, headers: Any, timeout: float, max_bytes: int) -> FetchResponse:
        with self._lock:
            self.requests.append((url, dict(headers)))
        if url in self.fail:
            raise FetchTransportError(self.fail[url], f"scripted {self.fail[url]}")
        if url not in self.pages:
            parts = urllib.parse.urlsplit(url)
            if parts.path == "/robots.txt":
                return FetchResponse(404, {}, b"")
            return FetchResponse(404, {"Content-Type": "text/html"}, b"<p>unknown</p>")
        status, page_headers, body = self.pages[url]
        return FetchResponse(status, page_headers, body[: max_bytes + 1])

    def page_urls_requested(self) -> list[str]:
        return [u for u, _h in self.requests if not u.endswith("/robots.txt")]


def organic(*urls: str) -> list[dict[str, Any]]:
    return [{"position": i + 1, "title": f"result {i + 1}", "link": u, "snippet": f"snippet {i + 1}"} for i, u in enumerate(urls)]


class FakeSerp:
    """Search stand-in for both providers.

    Serper (the study's provider since PR 27): ``POST https://google.serper.dev/search``
    with a JSON body, key in ``X-API-KEY``; the answer is ``{"organic": [...]}``.
    Serper's preflight query (``SERPER_PREFLIGHT_QUERY``) is recorded in ``preflights``,
    not in ``queries``, which therefore holds the study's searches only.
    SerpApi (PR 8, kept): ``GET /search`` answers by query, ``/account.json`` by ``account``.
    """

    def __init__(self) -> None:
        self.results: dict[str, list[dict[str, Any]]] = {
            Q_EXAMPLE: organic(PAGE_A, PAGE_BLOCKED, PAGE_GONE),
            RESEARCH_QUERY_EXAMPLE: organic(PAGE_D, PAGE_A),
            Q_SAMPLE: organic(PAGE_A),
        }
        self.account: dict[str, Any] = {"account_status": "Active", "plan_name": "Free", "total_searches_left": 250,
                                        "account_rate_limit_per_hour": 50, "api_key": "should-not-leak"}
        self.queries: list[str] = []
        self.preflights: list[str] = []
        self.urls: list[str] = []
        self.requests: list[HttpRequest] = []
        self.status_sequence: list[int] = []  # statuses returned before success (e.g. [429])
        self.fail_queries: set[str] = set()  # queries that always get HTTP 503
        self.preflight_status = 200  # HTTP status of Serper's preflight search
        self._lock = threading.Lock()

    def transport(self, request: HttpRequest) -> HttpResponse:
        parts = urllib.parse.urlsplit(request.url)
        with self._lock:
            self.urls.append(request.url)
            self.requests.append(request)
        if request.method == "POST":
            return self._serper(request)
        params = dict(urllib.parse.parse_qsl(parts.query))
        if parts.path == "/account.json":
            return HttpResponse(200, json.dumps(self.account).encode())
        with self._lock:
            self.queries.append(params["q"])
            status = self.status_sequence.pop(0) if self.status_sequence else 200
            if params["q"] in self.fail_queries:
                status = 503
        if status != 200:
            return HttpResponse(status, json.dumps({"error": f"scripted {status}"}).encode())
        query = params["q"]
        if query not in self.results:
            body = {"search_metadata": {"id": "s0", "status": "Success"},
                    "search_information": {"organic_results_state": "Fully empty"},
                    "error": "Google hasn't returned any results for this query."}
        else:
            body = {"search_metadata": {"id": f"s-{len(self.queries)}", "status": "Success"},
                    "search_information": {"organic_results_state": "Results for exact spelling"},
                    "organic_results": self.results[query]}
        return HttpResponse(200, json.dumps(body).encode())

    def _serper(self, request: HttpRequest) -> HttpResponse:
        assert urllib.parse.urlsplit(request.url).netloc == "google.serper.dev"
        payload = json.loads(request.body or b"{}")
        query = payload["q"]
        with self._lock:
            if query == search_mod.SERPER_PREFLIGHT_QUERY:
                self.preflights.append(query)
                status = self.preflight_status
            else:
                self.queries.append(query)
                status = self.status_sequence.pop(0) if self.status_sequence else 200
                if query in self.fail_queries:
                    status = 503
        if status != 200:
            return HttpResponse(status, json.dumps({"message": f"scripted {status}", "statusCode": status}).encode())
        body = {"searchParameters": {"q": query, "gl": payload.get("gl"), "hl": payload.get("hl"),
                                     "num": payload.get("num"), "type": "search", "engine": "google"},
                "organic": list(self.results.get(query, [])), "credits": 1}
        return HttpResponse(200, json.dumps(body).encode())

def install(monkeypatch, web: FakeWeb | None = None, serp: FakeSerp | None = None, *, key: str | None = "serp-test-key"):
    """Route the search clients and the fetcher to the fakes; set (or unset) the search keys (Serper and SerpApi)."""
    web = web or FakeWeb()
    serp = serp or FakeSerp()
    monkeypatch.setattr(search_mod, "urllib_transport", serp.transport)
    monkeypatch.setattr(fetch_mod, "urllib_fetch_transport", web.transport)
    for name in (search_mod.SERPER_SECRET, search_mod.SERPAPI_SECRET):
        if key is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, key)
    return web, serp


# --------------------------------------------------------------------------- #
# Fake answering model and decomposer
# --------------------------------------------------------------------------- #

MODEL_CALLS: list[tuple[str, ModelRequest]] = []
_DOC = re.compile(r"\[Document \d+ — source: (web\d+)\]\nURL: [^\n]*\n(.*?)(?=\n\n\[Document |\n\nQuestion: )", re.DOTALL)
_MLOCK = threading.Lock()


def shown(request: ModelRequest) -> list[tuple[str, str]]:
    return _DOC.findall(request.messages[-1].content)


@register("model", "fake_live_model")
class LiveFakeModel:
    """Answers "hosted by X" questions from the first shown document that says so."""

    provider = "openai"

    def __init__(self, *, label: str | None = None, model_id: str = "fake-cheap", behaviour: str = "hosted",
                 max_output_tokens: int = 4096, input_tokens: int = 1000, output_tokens: int = 100):
        self.name = label or model_id
        self.model_id = model_id
        self.behaviour = behaviour
        self.max_output_tokens = max_output_tokens
        self.retry = RetryPolicy(max_retries=0, backoff_seconds=0.0)
        self.usage = TokenUsage(input_tokens, output_tokens)

    def preflight(self) -> PreflightResult:
        return PreflightResult(self.name, self.model_id, True, "listed")

    def complete(self, request: ModelRequest) -> ModelResponse:
        with _MLOCK:
            MODEL_CALLS.append((self.name, request))
        if self.behaviour == "crash":
            raise ModelError("HTTP 400: scripted", status=400)
        text = json.dumps({"status": "abstained", "reason": "nothing found"})
        if self.behaviour == "garbage":
            text = "I think it was somewhere."
        elif self.behaviour == "hosted":
            for source, body in shown(request):
                for sentence in re.split(r"(?<=\.)\s+", body):
                    match = re.search(r"hosted by (\w+)", sentence)
                    if match:
                        text = json.dumps({"status": "answered", "value": match.group(1), "quote": sentence.strip(), "source_id": source})
                        break
                else:
                    continue
                break
        return ModelResponse(text=text, model=self.model_id, usage=self.usage, raw={"requested_model": self.model_id})


DECOMPOSE_CALLS: list[str] = []


class _DecomposerClient:
    name = "decomposer"
    model_id = "fake-cheap"
    secret_name = ""
    max_output_tokens = 512

    def preflight(self) -> PreflightResult:
        return PreflightResult(self.name, self.model_id, True, "listed")


@register("decomposer", "fake_live_decomposer")
class LiveFakeDecomposer:
    """"Which city hosted the <year> <event>?" -> entity <event>, time qualifier <year>, attribute "host city".

    Has a ``client`` (priced as ``fake-cheap``) and ``request_for`` like ``model_decomposer``,
    so the runner prices and estimates it as an auxiliary client.
    """

    name = "fake_live_decomposer"
    _Q = re.compile(r"^Which city hosted the (\d{4}) (.+)\?$")

    def __init__(self, *, mode: str = "ok"):
        self.mode = mode
        self.client = _DecomposerClient()

    def request_for(self, question: str) -> ModelRequest:
        return ModelRequest((Message("user", f"Decompose: {question}"),))

    def decompose(self, question: str) -> Decomposition:
        with _MLOCK:
            DECOMPOSE_CALLS.append(question)
        call = ModelCall(
            purpose="decompose", client="decomposer", request=self.request_for(question),
            started_at="2026-11-22T10:00:00+00:00", ended_at="2026-11-22T10:00:01+00:00",
            response=ModelResponse("{}", "fake-cheap", TokenUsage(200, 50), {"requested_model": "fake-cheap"}),
        )
        if self.mode == "fail":
            from binding_gate.answering import ModelCallFailed

            raise ModelCallFailed("scripted decomposer failure", [call])
        year, event = self._Q.match(question).groups()
        return Decomposition(
            constraints=QuestionConstraints(
                question=question,
                entity=Entity(name=event),
                attribute="host city",
                qualifiers=(Qualifier(QualifierKind.TIME, year),),
            ),
            model_calls=(call,),
        )


SEARCH_SPEC = {
    "name": "serper_google",
    "label": "search",
    "options": {"max_results": 10, "min_interval_seconds": 0.0, "max_retries": 2, "backoff_seconds": 0.0},
}
FETCH_OPTIONS = {"per_host_interval_seconds": 0.0}
CHECKS = ["quote_verbatim", "entity_in_window", "qualifiers_in_window", "no_closer_competitor", "premise_present"]


def arm_specs(max_attempts: int = 2, decomposer_mode: str = "ok") -> list[dict[str, Any]]:
    common = {"search": SEARCH_SPEC, "fetch": FETCH_OPTIONS, "documents_per_attempt": 5, "max_document_chars": 8000}
    return [
        {"name": "no_gate_live", "label": "no_gate", "options": dict(common)},
        {
            "name": "gated_research",
            "label": "gated_research",
            "options": {
                **common,
                "max_attempts": max_attempts,
                "checks": CHECKS,
                "decomposer": {"name": "fake_live_decomposer", "options": {"mode": decomposer_mode}},
            },
        },
    ]
