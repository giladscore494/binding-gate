"""One research session per run: cached searches and fetches, and the documents of an attempt.

How a query becomes documents (:meth:`ResearchSession.gather`)
-------------------------------------------------------------
1. **Search** the query once per run (cache key: SHA-256 of the search client's
   description and the query). The call is made through
   ``binding_gate.answering.call_model`` (bounded, logged retries) with
   ``purpose = "search"``; the results and the call are stored in the snapshot
   store.
2. **Fetch** the result URLs in rank order, skipping URLs already used by an
   earlier attempt of the same item and duplicates, until
   ``documents_per_attempt`` pages were fetched successfully or the results
   are exhausted. Each URL is fetched at most once per run (cache key: the URL
   string as returned by the search engine); every fetch, failed or not, is
   logged as a call with ``purpose = "fetch"`` whose response text summarises
   the outcome and the snapshot id.
3. **Documents.** For each successful page, the stored extracted text is read
   back from the snapshot store (hash-checked) and its first
   ``max_document_chars`` characters become a ``Document`` with id ``web<n>``
   (n = 1, 2, ... in the order shown), ``url`` = the final URL after
   redirects, and metadata ``snapshot_id``, ``url`` (as returned by the
   search), ``final_url``, ``fetched_at``, ``title``, ``rank`` and
   ``retrieved_on`` (the date of the search). The shown text is stored as a
   blob too, so that its hash can be checked later.
4. **Evidence references.** ``EvidenceRef(id=<snapshot id>, sha256=<hash of the
   shown text>, url=<final URL>)``, in the same order as the documents: within
   one attempt, the document shown as ``web<n>`` is ``attempt.evidence[n-1]``.

Sharing. Searches and fetches are shared by every arm, model and repeat of the
run (all of them see the same evidence for the same query), and a resumed run
reloads them from the store, so nothing is searched or fetched twice. A reused
search or fetch is logged again in the record that reuses it, as a copy with
``response.raw["cache_hit"] = true`` (charged nothing; the runner's convention).

Dry run. :func:`session_for` with a dry-run context returns a session whose
search client and fetcher are stand-ins: no network, ``max_results`` results per
search and pages exactly ``max_document_chars`` long (the worst case for the
prompt size), kept in memory.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from binding_gate.answering import call_model
from binding_gate.canonical import canonical_json, sha256_json, utc_now_iso
from binding_gate.contracts import Document, EvidenceRef, Message, ModelCall, ModelRequest, ModelResponse
from binding_gate.decomposers.cache import as_cache_hit
from binding_gate.research.context import RunContext
from binding_gate.research.fetch import FETCH_MODEL_ID, FetchSettings, PageFetcher
from binding_gate.research.search import (
    DryRunSearchClient,
    SearchResult,
    build_search_client,
    normalise_search_spec,
    results_from_text,
)
from binding_gate.research.snapshots import PageSnapshot, SnapshotStore, sha256_bytes, snapshot_id_for

SEARCH_PURPOSE = "search"
FETCH_PURPOSE = "fetch"
DEFAULT_DOCUMENTS_PER_ATTEMPT = 5
DEFAULT_MAX_DOCUMENT_CHARS = 8000


@dataclass(frozen=True)
class Evidence:
    """What :meth:`ResearchSession.gather` returns for one query."""

    query: str
    results: tuple[SearchResult, ...]
    documents: tuple[Document, ...]
    refs: tuple[EvidenceRef, ...]
    calls: tuple[ModelCall, ...]
    urls_used: frozenset[str]


@dataclass
class _SearchEntry:
    record: dict[str, Any]
    calls: tuple[ModelCall, ...]


class MemorySnapshotStore(SnapshotStore):
    """A snapshot store kept in memory (dry runs only; nothing is written to disk)."""

    def __init__(self) -> None:
        super().__init__(Path("<memory>"))
        self._blobs: dict[str, bytes] = {}
        self._records: list[dict[str, Any]] = []

    def put_blob(self, data: bytes) -> str:
        digest = sha256_bytes(data)
        with self._lock:
            self._blobs.setdefault(digest, bytes(data))
        return digest

    def get_blob(self, digest: str) -> bytes:
        return self._blobs[digest]

    def append(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._records.append(dict(record))

    def records(self, kind: str | None = None):  # type: ignore[override]
        with self._lock:
            snapshot = list(self._records)
        return iter([r for r in snapshot if kind is None or r.get("kind") == kind])


class DryRunFetcher:
    """Stands in for the page fetcher during a dry run: every page is fetched "successfully"
    and its text is exactly ``max_document_chars`` characters long."""

    name = "fetch"
    model_id = FETCH_MODEL_ID
    secret_name = ""

    def __init__(self, store: SnapshotStore, settings: FetchSettings, max_document_chars: int):
        self.store = store
        self.settings = settings
        self.max_document_chars = max_document_chars

    def preload_robots(self, record: Mapping[str, Any]) -> None:  # pragma: no cover - nothing to preload
        return None

    def fetch(self, url: str) -> PageSnapshot:
        text = ("dry-run page text " * (self.max_document_chars // 18 + 1))[: self.max_document_chars]
        digest = self.store.put_text(text)
        fetched_at = utc_now_iso()
        record = {
            "kind": "page", "url": url, "final_url": url, "fetched_at": fetched_at, "outcome": "ok",
            "http_status": 200, "content_type": "text/plain", "charset": "utf-8", "raw_sha256": digest,
            "raw_bytes": len(text), "truncated": False, "text_sha256": digest, "text_chars": len(text),
            "title": None, "redirects": [], "robots": None, "error": None, "extractor_version": 1,
            "user_agent": "dry-run",
        }
        record["snapshot_id"] = snapshot_id_for(url, fetched_at, "ok", digest)
        self.store.append(record)
        return PageSnapshot(record)


def fetch_call(snapshot: PageSnapshot, settings: FetchSettings, client: str = "fetch") -> ModelCall:
    """The logged call for one fetch (a response even for a failed fetch: the failure is an
    observation stored in the snapshot store, never retried)."""
    r = snapshot.record
    summary = {
        "snapshot_id": r["snapshot_id"],
        "outcome": r["outcome"],
        "http_status": r.get("http_status"),
        "content_type": r.get("content_type"),
        "final_url": r.get("final_url"),
        "raw_sha256": r.get("raw_sha256"),
        "text_sha256": r.get("text_sha256"),
        "error": r.get("error"),
    }
    return ModelCall(
        purpose=FETCH_PURPOSE,
        client=client,
        request=ModelRequest(messages=(Message("user", r["url"]),), params=settings.to_dict()),
        started_at=r["fetched_at"],
        ended_at=r["fetched_at"],
        response=ModelResponse(
            text=canonical_json(summary),
            model=FETCH_MODEL_ID,
            raw={"ok": r["outcome"] == "ok", "outcome": r["outcome"], "snapshot_id": r["snapshot_id"],
                 "dry_run": r.get("user_agent") == "dry-run"},
        ),
    )


class ResearchSession:
    """Searches and fetches for one run, shared by all arms, models and repeats. Thread-safe."""

    def __init__(self, *, store: SnapshotStore, search_client: Any, fetcher: Any, dry_run: bool = False):
        self.store = store
        self.search_client = search_client
        self.fetcher = fetcher
        self.dry_run = dry_run
        self._searches: dict[str, _SearchEntry] = {}
        self._pages: dict[str, tuple[PageSnapshot, ModelCall]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._preload()

    # -- persistence -------------------------------------------------------- #

    def _preload(self) -> None:
        """Reload searches, pages and robots decisions recorded earlier in this run (resume)."""
        for record in self.store.records():
            kind = record.get("kind")
            if kind == "search":
                call = ModelCall.from_dict(record["call"])
                self._searches.setdefault(record["key"], _SearchEntry(record, (call,)))
            elif kind == "page":
                snapshot = PageSnapshot(record)
                self._pages[record["url"]] = (snapshot, fetch_call(snapshot, self.fetcher.settings, self.fetcher.name))
            elif kind == "robots" and hasattr(self.fetcher, "preload_robots"):
                self.fetcher.preload_robots(record)

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    # -- search and fetch --------------------------------------------------- #

    def search_key(self, query: str) -> str:
        return sha256_json({"search": self.search_client.describe(), "query": query})

    def search(self, query: str) -> tuple[dict[str, Any], tuple[ModelCall, ...]]:
        """The stored search record for ``query`` and the calls to log for this use.

        Raises ``binding_gate.answering.ModelCallFailed`` (carrying every logged
        attempt) when the search failed after the client's retries; a failure
        is not cached.
        """
        key = self.search_key(query)
        with self._lock_for("search:" + key):
            entry = self._searches.get(key)
            if entry is not None:
                return entry.record, as_cache_hit(entry.calls)
            request = ModelRequest(messages=(Message("user", query),))
            response, calls = call_model(self.search_client, request, SEARCH_PURPOSE)
            results = results_from_text(response.text)
            record = {
                "kind": "search",
                "search_id": f"search-{key[:16]}",
                "key": key,
                "provider": getattr(self.search_client, "provider", self.search_client.describe().get("provider")),
                "model_id": response.model,
                "query": query,
                "params": self.search_client.describe(),
                "requested_at": calls[-1].started_at,
                "response_sha256": response.raw.get("response_sha256"),
                "results": [r.to_dict() for r in results],
                "call": calls[-1].to_dict(),
            }
            self.store.append(record)
            self._searches[key] = _SearchEntry(record, (calls[-1],))
            return record, calls

    def fetch(self, url: str) -> tuple[PageSnapshot, ModelCall, bool]:
        """``(snapshot, call to log, reused)`` for ``url``; fetched at most once per run."""
        with self._lock_for("page:" + url):
            cached = self._pages.get(url)
            if cached is not None:
                snapshot, call = cached
                return snapshot, as_cache_hit((call,))[0], True
            snapshot = self.fetcher.fetch(url)
            call = fetch_call(snapshot, self.fetcher.settings, self.fetcher.name)
            self._pages[url] = (snapshot, call)
            return snapshot, call, False

    # -- documents for one attempt ------------------------------------------ #

    def gather(
        self,
        query: str,
        *,
        documents_per_attempt: int = DEFAULT_DOCUMENTS_PER_ATTEMPT,
        max_document_chars: int = DEFAULT_MAX_DOCUMENT_CHARS,
        exclude_urls: frozenset[str] = frozenset(),
    ) -> Evidence:
        """Search ``query`` and build the documents of one attempt (see the module docstring)."""
        record, search_calls = self.search(query)
        results = tuple(SearchResult(int(r["rank"]), r["title"], r["url"], r["snippet"]) for r in record["results"])
        retrieved_on = record["requested_at"][:10]
        calls: list[ModelCall] = list(search_calls)
        documents: list[Document] = []
        refs: list[EvidenceRef] = []
        used = set(exclude_urls)
        for result in results:
            if len(documents) >= documents_per_attempt:
                break
            if result.url in used:
                continue
            used.add(result.url)
            snapshot, call, _reused = self.fetch(result.url)
            calls.append(call)
            if not snapshot.ok or snapshot.text_sha256 is None:
                continue
            shown = self.store.get_text(snapshot.text_sha256)[:max_document_chars]
            if not shown.strip():
                continue
            self.store.put_text(shown)
            document = Document.create(
                f"web{len(documents) + 1}",
                shown,
                url=snapshot.final_url,
                metadata={
                    "snapshot_id": snapshot.snapshot_id,
                    "url": result.url,
                    "final_url": snapshot.final_url,
                    "fetched_at": snapshot.fetched_at,
                    "title": snapshot.title,
                    "rank": result.rank,
                    "retrieved_on": retrieved_on,
                },
            )
            documents.append(document)
            refs.append(EvidenceRef(id=snapshot.snapshot_id, sha256=document.sha256, url=snapshot.final_url))
        return Evidence(query, results, tuple(documents), tuple(refs), tuple(calls), frozenset(used))


# --------------------------------------------------------------------------- #
# One session per run and configuration
# --------------------------------------------------------------------------- #

_SESSIONS: dict[tuple[str, str], ResearchSession] = {}
_STORES: dict[str, SnapshotStore] = {}
_SESSIONS_LOCK = threading.Lock()


def session_for(
    context: RunContext,
    search_spec: Mapping[str, Any],
    fetch_options: Mapping[str, Any] | None,
    *,
    max_document_chars: int = DEFAULT_MAX_DOCUMENT_CHARS,
) -> ResearchSession:
    """The shared session of this run for this search/fetch configuration.

    Real run: one :class:`SnapshotStore` per run directory, one session per
    (run, search spec, fetch settings). Dry run: in-memory stand-ins, one per
    dry-run invocation (``context.token``).
    """
    spec = normalise_search_spec(search_spec)
    settings = FetchSettings.from_options(fetch_options)
    identity: dict[str, Any] = {"search": spec, "fetch": settings.to_dict(), "dry_run": context.dry_run}
    if context.dry_run:
        identity["max_document_chars"] = max_document_chars  # the stand-in pages are this long
    key = (context.token, canonical_json(identity))
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(key)
        if session is not None:
            return session
        real_client = build_search_client(spec)
        if context.dry_run:
            store: SnapshotStore = MemorySnapshotStore()
            session = ResearchSession(
                store=store,
                search_client=DryRunSearchClient(real_client),
                fetcher=DryRunFetcher(store, settings, max_document_chars),
                dry_run=True,
            )
        else:
            assert context.run_dir is not None
            store_key = str(Path(context.run_dir).resolve())
            store = _STORES.setdefault(store_key, SnapshotStore.for_run(context.run_dir))
            real_client.blob_sink = store.put_blob
            session = ResearchSession(store=store, search_client=real_client, fetcher=PageFetcher(store, settings))
        _SESSIONS[key] = session
        return session


def clear_sessions() -> None:
    """Forget every session (tests, or a process that starts a second run)."""
    with _SESSIONS_LOCK:
        _SESSIONS.clear()
        _STORES.clear()


__all__ = [
    "DEFAULT_DOCUMENTS_PER_ATTEMPT",
    "DEFAULT_MAX_DOCUMENT_CHARS",
    "Evidence",
    "ResearchSession",
    "MemorySnapshotStore",
    "DryRunFetcher",
    "session_for",
    "clear_sessions",
    "fetch_call",
]
