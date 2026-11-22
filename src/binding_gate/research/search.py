"""The web search client of the study: Serper's Google Search API, over plain HTTPS.

PR 27 (PREREGISTRATION.md section 18, amendment 3): the study uses **Serper**
(serper.dev) because the owner holds a Serper key, not a SerpApi key. SerpApi was
the original choice (PR 8); its client :class:`SerpApiSearchClient` is kept below,
unused by the study configs. Both return Google's organic results; the arms, the
queries and the parsed result shape (rank, title, URL, snippet) are the same.

Serper facts (serper.dev and third-party documentation, read 2026-11-22):

* **Request.** ``POST https://google.serper.dev/search`` with headers
  ``X-API-KEY: <key>`` and ``Content-Type: application/json`` and the JSON body
  ``{"q": ..., "gl": "us", "hl": "en", "num": <max_results>}``; ``num``,
  ``hl``, ``gl`` and ``page`` are documented optional parameters (``page`` is
  not sent: the first results page is used).
* **Response.** A JSON object with ``organic``, a list of ``{title, link,
  snippet, position, ...}``, plus ``searchParameters`` and possibly
  ``credits``. At most ``max_results`` organic results with an http(s) link
  are kept, in rank order (rank = ``position``, else list order).
* **Errors.** HTTP 401/403 = missing or wrong key (not retryable). Running out
  of credits is a billing 4xx error: HTTP 402 or a body saying the credits or
  balance are used up is reported as ``BALANCE EXHAUSTED`` (the runner stops);
  every other 4xx except 408/429 is not retryable; 408, 429 and 5xx are
  (``models/_http.RETRYABLE_STATUS``). Credits are deducted only for successful
  queries.
* **Price.** Free tier: 2,500 queries on signup, no card. Paid plans about $1
  per 1,000 queries (third-party summaries; UNVERIFIED). ``configs/prices.toml``
  charges every successful search $0.001 (conservative).
* **Terms.** serper.dev/terms has no clause restricting storage of results
  (already noted when Serper was the runner-up in PR 8).
* **No free account endpoint.** Serper documents no balance or account API, so
  :meth:`SerperSearchClient.preflight` makes ONE real search with the fixed
  harmless query :data:`SERPER_PREFLIGHT_QUERY`: it costs one (free-tier)
  credit, is not logged as a study call and is not charged to a budget cap. It
  reports OK if the answer is HTTP 200 with an ``organic`` list, and leaves
  ``searches_left = None``: the runner's search quota is then ``[runner]
  max_searches`` alone.

The API key is read from ``SERPER_API_KEY`` through ``binding_gate.secrets`` at
call time, sent only in the ``X-API-KEY`` header and removed from every error
message.

Original SerpApi selection (PR 8; pages read on 2026-11-22), kept for the record:

* **Terms permit this use.** The terms of SerpApi (serpapi.com/legal) contain no
  clause against storing results, research use or evaluating AI systems; they
  forbid reselling the service. By contrast, Brave Search API's terms
  (api-dashboard.search.brave.com/terms-of-service, section 3(b)) forbid storing
  results beyond transient use and using them to "create, evaluate, train ...
  benchmark" AI models; Tavily's API agreement (tavily.com/policy) forbids
  storing the output and using it in "machine learning or algorithmic
  activities". Exa's general terms could not be located (only its Exa Connect
  marketplace terms); a third-party summary says it forbids persistent caching.
  Google's Custom Search JSON API is reported (several third-party sources) to
  be closed to new customers and to shut down on 2027-01-01.
* **Free preflight.** SerpApi's Account API is "free of charge, and using it will
  not be counted toward your monthly quota" (serpapi.com/account-api).
* **Documented response and errors** (serpapi.com/search-api,
  serpapi.com/organic-results, serpapi.com/api-status-and-error-codes).
* **Quality signal.** Results are Google's organic results, the engine most
  agents' users would see.
* **Price** (serpapi.com/pricing): Free 250 searches/month (50/hour); Starter
  $25 for 1,000/month (200/hour), i.e. $0.025 per search; "Only successful
  searches are counted"; cached searches (identical parameters within one
  hour) are free.
* Runner-up at the time: Serper (serper.dev/terms has no storage restriction
  either; about $1 per 1,000 queries per a third-party pricing summary) -
  cheaper, but with less formal documentation and no documented free account
  endpoint. Serper became the study's provider in PR 27 (see above).

SerpApi request (``GET https://serpapi.com/search``): ``engine=google``, ``q``,
``hl``, ``gl``, ``output=json``, ``api_key``. ``num`` is not sent (it is not
listed in SerpApi's current Google parameters). HTTP 429 (hourly limit or out
of searches) and 5xx are retryable; 401/403 and other 4xx are not. An HTTP 200
body whose ``error`` says Google returned no results is a successful search
with zero results; ``search_metadata.status == "Error"`` is a failure. The key
is sent as the ``api_key`` query parameter (SerpApi's only documented method),
read from ``SERPAPI_API_KEY``, and removed from every error message.

Interface. Both clients implement the ``ModelClient`` protocol
(``complete(ModelRequest) -> ModelResponse``) so that searches are retried and
logged by ``binding_gate.answering.call_model`` exactly like model calls, with
``ModelCall.purpose = "search"``. The request's last message is the query. The
response text is the canonical JSON ``{"results": [{"rank", "title", "url",
"snippet"}, ...]}``; ``ModelResponse.model`` is the client's model id
(:data:`SERPER_MODEL_ID` or :data:`SERPAPI_MODEL_ID`), which is what the runner
prices; ``raw`` holds the request parameters (never the key), the result count
and the SHA-256 of the provider's raw JSON (stored in the snapshot store, not in
the trace).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from binding_gate import secrets
from binding_gate.canonical import canonical_json
from binding_gate.contracts import ModelRequest, ModelResponse
from binding_gate.models._http import (
    BALANCE_EXHAUSTED_PREFIX,
    RETRYABLE_STATUS,
    HttpRequest,
    ModelError,
    PreflightResult,
    RetryPolicy,
    Transport,
    is_balance_exhausted,
    urllib_transport,
)

SERPAPI_MODEL_ID = "serpapi/google"
SERPAPI_SECRET = "SERPAPI_API_KEY"
SERPER_MODEL_ID = "serper/google"
SERPER_SECRET = "SERPER_API_KEY"
SERPER_PREFLIGHT_QUERY = "binding-gate preflight"
"""The fixed, harmless query of Serper's preflight (one real search: one credit)."""
_SERPER_CREDIT_PHRASES = ("not enough credits", "out of credits", "no credits", "credits exhausted")
DRY_RUN_HOST = "dry-run.invalid"
_NO_RESULTS_MARKERS = ("hasn't returned any results", "has not returned any results")


@dataclass(frozen=True)
class SearchResult:
    rank: int
    title: str
    url: str
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        return {"rank": self.rank, "title": self.title, "url": self.url, "snippet": self.snippet}


def results_from_text(text: str) -> list[SearchResult]:
    """Parse the canonical response text of a search call back into results."""
    data = json.loads(text)
    return [SearchResult(int(r["rank"]), str(r["title"]), str(r["url"]), str(r["snippet"])) for r in data["results"]]


def results_text(results: list[SearchResult]) -> str:
    return canonical_json({"results": [r.to_dict() for r in results]})


def query_of(request: ModelRequest) -> str:
    query = request.messages[-1].content.strip()
    if not query:
        raise ModelError("empty search query")
    return query


class SerpApiSearchClient:
    """See the module docstring. Options come from the arm's ``search.options`` table.

    ``max_results``           organic results kept per search (default 10)
    ``hl``, ``gl``            interface language and country (default ``en``, ``us``)
    ``min_interval_seconds``  minimum time between two searches of this process,
                              to stay under the plan's hourly limit (default 18 s,
                              i.e. 200 per hour = the Starter plan; PROPOSED)
    ``timeout_seconds``       per HTTP request (default 60)
    ``max_retries``, ``backoff_seconds``, ``backoff_multiplier``, ``max_backoff_seconds``
                              retry policy used by ``call_model``
    """

    provider = "serpapi"
    secret_name = SERPAPI_SECRET
    model_id = SERPAPI_MODEL_ID
    endpoint = "https://serpapi.com/search"
    account_endpoint = "https://serpapi.com/account.json"
    max_output_tokens = None

    def __init__(
        self,
        *,
        label: str = "search",
        max_results: int = 10,
        hl: str = "en",
        gl: str = "us",
        min_interval_seconds: float = 18.0,
        timeout_seconds: float = 60.0,
        max_retries: int = 4,
        backoff_seconds: float = 5.0,
        backoff_multiplier: float = 2.0,
        max_backoff_seconds: float = 120.0,
        transport: Transport | None = None,
        blob_sink: Callable[[bytes], str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 100:
            raise ValueError("search.max_results must be an integer between 1 and 100")
        for name, value in (("hl", hl), ("gl", gl)):
            if not isinstance(value, str) or not value.isalpha() or len(value) > 5:
                raise ValueError(f"search.{name} must be a short language/country code")
        for name, value in (("min_interval_seconds", min_interval_seconds), ("timeout_seconds", timeout_seconds)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"search.{name} must be a number >= 0")
        self.name = label
        self.max_results = max_results
        self.hl = hl
        self.gl = gl
        self.min_interval_seconds = float(min_interval_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self.retry = RetryPolicy(max_retries, backoff_seconds, backoff_multiplier, max_backoff_seconds)
        self.blob_sink = blob_sink
        self._transport = transport or urllib_transport
        self._sleep = sleep
        self._monotonic = monotonic
        self._next_allowed = 0.0
        self._lock = threading.Lock()
        self.searches_left: int | None = None  # set by preflight() (PR 13: runner search quota)

    def describe(self) -> dict[str, Any]:
        """Everything that changes what a search returns (part of the search cache key). No secret."""
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "engine": "google",
            "hl": self.hl,
            "gl": self.gl,
            "max_results": self.max_results,
        }

    # -- helpers ------------------------------------------------------------ #

    def _scrub(self, text: str, api_key: str) -> str:
        return text.replace(api_key, "***") if api_key else text

    def _wait_turn(self) -> None:
        with self._lock:
            now = self._monotonic()
            start = max(now, self._next_allowed)
            self._next_allowed = start + self.min_interval_seconds
        if start > now:
            self._sleep(start - now)

    def _get(self, url: str, api_key: str, timeout: float) -> tuple[int, dict[str, Any], bytes]:
        try:
            response = self._transport(HttpRequest("GET", url, {"Accept": "application/json"}, None, timeout))
        except ModelError as exc:
            raise ModelError(self._scrub(str(exc), api_key), status=exc.status, retryable=exc.retryable) from None
        text = response.body.decode("utf-8", errors="replace")
        try:
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            data = None
        if not 200 <= response.status < 300:
            message = (data or {}).get("error") if isinstance(data, dict) else None
            raise ModelError(
                self._scrub(f"SerpApi HTTP {response.status}: {message or text[:300]}", api_key),
                status=response.status,
                retryable=response.status in RETRYABLE_STATUS,
            )
        if not isinstance(data, dict):
            raise ModelError(f"SerpApi HTTP {response.status} with a body that is not a JSON object", status=response.status, retryable=True)
        return response.status, data, response.body

    # -- ModelClient protocol --------------------------------------------- #

    def complete(self, request: ModelRequest) -> ModelResponse:
        """One search. Raises ``ModelError`` (with ``retryable``) on failure; never retries itself."""
        query = query_of(request)
        api_key = secrets.get_secret(self.secret_name)
        params = {"engine": "google", "q": query, "hl": self.hl, "gl": self.gl, "output": "json", "api_key": api_key}
        url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
        self._wait_turn()
        status, data, body = self._get(url, api_key, self.timeout_seconds)
        metadata = data.get("search_metadata") if isinstance(data.get("search_metadata"), dict) else {}
        error = data.get("error")
        no_results = isinstance(error, str) and any(m in error for m in _NO_RESULTS_MARKERS)
        if metadata.get("status") == "Error" or (error and not no_results):
            raise ModelError(self._scrub(f"SerpApi search failed: {error or metadata}", api_key)[:500], status=status)
        results = parse_organic_results(data.get("organic_results"), self.max_results)
        digest = self.blob_sink(body) if self.blob_sink is not None else None
        information = data.get("search_information") if isinstance(data.get("search_information"), dict) else {}
        return ModelResponse(
            text=results_text(results),
            model=self.model_id,
            usage=None,
            raw={
                "provider": self.provider,
                "requested_model": self.model_id,
                "search_id": metadata.get("id"),
                "search_status": metadata.get("status"),
                "organic_results_state": information.get("organic_results_state"),
                "no_results": no_results,
                "results": len(results),
                "response_sha256": digest,
                "request_params": {k: v for k, v in params.items() if k != "api_key"},
            },
        )

    def preflight(self) -> PreflightResult:
        """Free check of the key and the remaining searches (SerpApi Account API; not billed)."""
        api_key = secrets.get_secret(self.secret_name)
        url = f"{self.account_endpoint}?{urllib.parse.urlencode({'api_key': api_key})}"
        try:
            _status, data, _body = self._get(url, api_key, min(self.timeout_seconds, 60.0))
        except ModelError as exc:
            return PreflightResult(self.name, self.model_id, False, f"SerpApi account check failed: {exc}")
        left = data.get("total_searches_left")
        # PR 13: read by the runner's search quota ([runner] max_searches) after the preflight
        self.searches_left = left if isinstance(left, int) and not isinstance(left, bool) else None
        detail = (
            f"SerpApi account {data.get('account_status')!r}, plan {data.get('plan_name')!r}, "
            f"{left} searches left, hourly limit {data.get('account_rate_limit_per_hour')}"
        )
        ok = isinstance(left, int) and left > 0 and data.get("account_status", "Active") == "Active"
        return PreflightResult(self.name, self.model_id, ok, detail if ok else f"not usable: {detail}")


class SerperSearchClient(SerpApiSearchClient):
    """Serper's Google Search API (see the module docstring); the study's search client since PR 27.

    Options as :class:`SerpApiSearchClient` (``max_results``, ``hl``, ``gl``,
    ``min_interval_seconds``, ``timeout_seconds`` and the retry policy). Serper
    documents no hourly limit like SerpApi's Free plan, so the default spacing is
    1 s (PROPOSED). ``max_results`` is sent as ``num``.
    """

    provider = "serper"
    secret_name = SERPER_SECRET
    model_id = SERPER_MODEL_ID
    endpoint = "https://google.serper.dev/search"
    account_endpoint = None  # Serper documents no account or balance endpoint

    def __init__(self, *, min_interval_seconds: float = 1.0, **options: Any):
        super().__init__(min_interval_seconds=min_interval_seconds, **options)

    def describe(self) -> dict[str, Any]:
        """Everything that changes what a search returns (part of the search cache key). No secret."""
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "engine": "google",
            "hl": self.hl,
            "gl": self.gl,
            "max_results": self.max_results,
        }

    def _post(self, payload: Mapping[str, Any], api_key: str, timeout: float) -> tuple[int, dict[str, Any], bytes]:
        headers = {"X-API-KEY": api_key, "Content-Type": "application/json", "Accept": "application/json"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            response = self._transport(HttpRequest("POST", self.endpoint, headers, body, timeout))
        except ModelError as exc:
            raise ModelError(self._scrub(str(exc), api_key), status=exc.status, retryable=exc.retryable) from None
        text = response.body.decode("utf-8", errors="replace")
        try:
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            data = None
        if not 200 <= response.status < 300:
            message = None
            if isinstance(data, dict):
                message = data.get("message") or data.get("error")
            detail = self._scrub(f"Serper HTTP {response.status}: {message or text[:300]}", api_key)[:500]
            lowered = text.lower()
            if is_balance_exhausted(response.status, text) or any(p in lowered for p in _SERPER_CREDIT_PHRASES):
                raise ModelError(f"{BALANCE_EXHAUSTED_PREFIX}: {detail}", status=response.status, retryable=False)
            raise ModelError(detail, status=response.status, retryable=response.status in RETRYABLE_STATUS)
        if not isinstance(data, dict):
            raise ModelError(f"Serper HTTP {response.status} with a body that is not a JSON object", status=response.status, retryable=True)
        return response.status, data, response.body

    def _payload(self, query: str) -> dict[str, Any]:
        return {"q": query, "gl": self.gl, "hl": self.hl, "num": self.max_results}

    def complete(self, request: ModelRequest) -> ModelResponse:
        """One search. Raises ``ModelError`` (with ``retryable``) on failure; never retries itself."""
        query = query_of(request)
        api_key = secrets.get_secret(self.secret_name)
        payload = self._payload(query)
        self._wait_turn()
        status, data, body = self._post(payload, api_key, self.timeout_seconds)
        organic = data.get("organic")
        if organic is not None and not isinstance(organic, list):
            raise ModelError(f"Serper HTTP {status}: 'organic' is not a list", status=status)
        results = parse_organic_results(organic, self.max_results)
        digest = self.blob_sink(body) if self.blob_sink is not None else None
        credits = data.get("credits")
        return ModelResponse(
            text=results_text(results),
            model=self.model_id,
            usage=None,
            raw={
                "provider": self.provider,
                "requested_model": self.model_id,
                "no_results": not results,
                "results": len(results),
                "credits": credits if isinstance(credits, int) and not isinstance(credits, bool) else None,
                "response_sha256": digest,
                "request_params": dict(payload),
            },
        )

    def preflight(self) -> PreflightResult:
        """ONE real search with :data:`SERPER_PREFLIGHT_QUERY` (costs one credit; Serper has no free account check).

        ``searches_left`` stays ``None`` (unknown): the runner's quota is then ``[runner] max_searches``.
        """
        api_key = secrets.get_secret(self.secret_name)
        self.searches_left = None
        try:
            self._wait_turn()
            _status, data, _body = self._post(self._payload(SERPER_PREFLIGHT_QUERY), api_key, min(self.timeout_seconds, 60.0))
        except ModelError as exc:
            return PreflightResult(self.name, self.model_id, False, f"Serper test search failed: {exc}")
        organic = data.get("organic")
        if not isinstance(organic, list):
            return PreflightResult(
                self.name, self.model_id, False, "not usable: Serper answered HTTP 200 without an 'organic' list"
            )
        return PreflightResult(
            self.name,
            self.model_id,
            True,
            f"Serper test search OK ({len(organic)} organic results; one credit used; searches left unknown)",
        )


def parse_organic_results(raw: Any, limit: int) -> list[SearchResult]:
    """SerpApi ``organic_results`` or Serper ``organic`` -> results with an http(s) link, in rank order, at most ``limit``."""
    results: list[SearchResult] = []
    if not isinstance(raw, list):
        return results
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        link = item.get("link")
        if not isinstance(link, str) or urllib.parse.urlsplit(link).scheme not in ("http", "https"):
            continue
        position = item.get("position")
        rank = position if isinstance(position, int) and not isinstance(position, bool) else index + 1
        results.append(
            SearchResult(
                rank=rank,
                title=str(item.get("title") or ""),
                url=link,
                snippet=str(item.get("snippet") or ""),
            )
        )
        if len(results) == limit:
            break
    return results


class DryRunSearchClient:
    """Stands in for a search client during a dry run: no network, ``max_results`` synthetic results.

    It reports the real client's label, model id and secret name, so that the
    dry run prices its calls exactly like real searches.
    """

    def __init__(self, real: Any):
        self.name = real.name
        self.model_id = real.model_id
        self.secret_name = getattr(real, "secret_name", "")
        self.max_results = getattr(real, "max_results", 10)
        self.retry = None
        self._describe = real.describe()

    def describe(self) -> dict[str, Any]:
        return dict(self._describe)

    def complete(self, request: ModelRequest) -> ModelResponse:
        query = query_of(request)
        tag = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
        results = [
            SearchResult(rank, f"dry-run result {rank}", f"https://{DRY_RUN_HOST}/{tag}/{rank}", "")
            for rank in range(1, self.max_results + 1)
        ]
        return ModelResponse(text=results_text(results), model=self.model_id, raw={"dry_run": True})


SEARCH_CLIENTS: dict[str, Callable[..., Any]] = {
    "serper_google": SerperSearchClient,  # the study's client since PR 27
    "serpapi_google": SerpApiSearchClient,  # the original choice (PR 8); kept, unused by the study configs
}
"""Search clients that a run config may name in ``search = {name = ..., label = ..., options = {...}}``."""

_SPEC_KEYS = {"name", "label", "options"}


def normalise_search_spec(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError('search must be a table {name = "serper_google", label = "...", options = {...}}')
    unknown = sorted(set(raw) - _SPEC_KEYS)
    if unknown:
        raise ValueError(f"search: unknown key(s) {unknown}; allowed: {sorted(_SPEC_KEYS)}")
    name = raw.get("name")
    if name not in SEARCH_CLIENTS:
        raise ValueError(f"search.name must be one of {sorted(SEARCH_CLIENTS)}, got {name!r}")
    label = raw.get("label", "search")
    if not isinstance(label, str) or not label:
        raise ValueError("search.label must be a non-empty string")
    options = raw.get("options", {})
    if not isinstance(options, Mapping):
        raise ValueError("search.options must be a table")
    canonical_json(options)
    return {"name": name, "label": label, "options": dict(options)}


def build_search_client(spec: Mapping[str, Any]) -> Any:
    """Create the search client named in a (normalised) spec. Needs no API key."""
    spec = normalise_search_spec(spec)
    return SEARCH_CLIENTS[spec["name"]](label=spec["label"], **spec["options"])
