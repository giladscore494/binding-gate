"""The page fetcher: one GET per URL, robots.txt respected, size and time limits, failures recorded.

Every fetch ends in exactly one page record in the snapshot store, whatever
happened; nothing is retried (a failed page is simply not shown, and the reason
is in the store and in the trace). The fetcher never runs code from a page and
never follows anything but HTTP redirects.

Request
-------
``GET`` with the fixed headers ``User-Agent:`` :data:`USER_AGENT` (it names the
project and its repository, so a site owner can find out who fetched a page),
``Accept: text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1`` and
``Accept-Encoding: gzip, identity``. No cookies, no JavaScript.

Order of steps and the outcome codes they produce
-------------------------------------------------
1. ``invalid_url``             not an ``http``/``https`` URL with a host
2. robots.txt of the origin (fetched once per origin per run, stored as a
   ``robots`` record; RFC 9309 section 2.3.1): 2xx -> parsed rules; 4xx ->
   "unavailable", everything allowed; 5xx, timeout or network failure ->
   "unreachable", everything disallowed. Up to five redirects are followed for
   robots.txt itself.
   ``robots_disallowed``       our product token :data:`PRODUCT_TOKEN` may not fetch the URL
3. politeness: at least ``per_host_interval_seconds`` between two requests to
   the same host (waiting is not a failure)
4. the request; redirects (301, 302, 303, 307, 308 with a ``Location``) are
   followed up to ``max_redirects`` times, each target checked against its own
   robots.txt first
   ``too_many_redirects``      more redirects than allowed
   ``timeout`` / ``network_error``  no response
   ``http_error``              final status not 2xx (the status is recorded)
   ``unsupported_content_type`` media type is not HTML, XHTML or plain text
5. the body: at most ``max_bytes`` bytes are kept (``truncated`` is recorded
   when the server sent more); gzip/deflate content encoding is undone, again
   with the size limit
   ``decode_error``            the content encoding could not be undone
6. text extraction (``binding_gate.research.extract``)
   ``empty_text``              nothing but markup
7. ``ok``                      raw bytes and text stored as blobs

Limits (``FetchSettings``; PROPOSED values, see PREREGISTRATION.md section 3,
layer 3): ``max_bytes = 2,000,000``, ``timeout_seconds = 20``,
``max_redirects = 5``, ``per_host_interval_seconds = 1.0``.
"""

from __future__ import annotations

import gzip
import io
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from binding_gate import __version__
from binding_gate.canonical import utc_now_iso
from binding_gate.research import robots as robots_mod
from binding_gate.research.extract import EXTRACTOR_VERSION, SUPPORTED_TYPES, content_type_of, extract
from binding_gate.research.snapshots import PageSnapshot, SnapshotStore, sha256_bytes, snapshot_id_for

PRODUCT_TOKEN = "binding-gate-research"
USER_AGENT = (
    f"{PRODUCT_TOKEN}/{__version__} (+https://github.com/giladscore494/binding-gate; "
    "research crawler for a pre-registered study; fetches single pages; respects robots.txt)"
)
ACCEPT = "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1"
FETCH_MODEL_ID = "binding-gate/fetch@1"
"""The id under which fetch calls are logged and priced (at zero) in ``configs/prices.toml``."""
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
OUTCOMES = (
    "ok", "invalid_url", "robots_disallowed", "too_many_redirects", "timeout", "network_error",
    "http_error", "unsupported_content_type", "decode_error", "empty_text",
)


class FetchTransportError(RuntimeError):
    """No HTTP response was obtained. ``kind`` is ``"timeout"`` or ``"network_error"``."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class FetchResponse:
    """One HTTP response, redirects NOT followed. ``body`` holds at most ``max_bytes + 1`` bytes."""

    status: int
    headers: Mapping[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        name = name.lower()
        for key, value in self.headers.items():
            if key.lower() == name:
                return value
        return None


FetchTransport = Callable[[str, Mapping[str, str], float, int], FetchResponse]
"""``(url, headers, timeout_seconds, max_bytes) -> FetchResponse``; raises :class:`FetchTransportError`."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - stdlib signature
        return None  # the fetcher follows redirects itself, checking robots.txt for each target


def urllib_fetch_transport(url: str, headers: Mapping[str, str], timeout: float, max_bytes: int) -> FetchResponse:
    """The standard-library transport (proxy settings from the environment are honoured)."""
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            return FetchResponse(response.status, dict(response.headers.items()), response.read(max_bytes + 1))
    except urllib.error.HTTPError as exc:
        body = exc.read(max_bytes + 1) if exc.fp is not None else b""
        return FetchResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, body)
    except (socket.timeout, TimeoutError) as exc:
        raise FetchTransportError("timeout", f"timed out after {timeout} s: {exc}") from None
    except (urllib.error.URLError, ConnectionError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise FetchTransportError("timeout", f"timed out after {timeout} s") from None
        raise FetchTransportError("network_error", f"{type(reason).__name__}: {reason}") from None


@dataclass(frozen=True)
class FetchSettings:
    """Limits of the fetcher. All values are PROPOSED (PREREGISTRATION.md section 3, layer 3)."""

    max_bytes: int = 2_000_000
    timeout_seconds: float = 20.0
    max_redirects: int = 5
    per_host_interval_seconds: float = 1.0

    @classmethod
    def from_options(cls, options: Mapping[str, Any] | None) -> FetchSettings:
        options = dict(options or {})
        unknown = sorted(set(options) - set(cls.__dataclass_fields__))
        if unknown:
            raise ValueError(f"fetch: unknown option(s) {unknown}; allowed: {sorted(cls.__dataclass_fields__)}")
        settings = cls(**options)
        for name in ("max_bytes", "max_redirects"):
            value = getattr(settings, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < (1 if name == "max_bytes" else 0):
                raise ValueError(f"fetch.{name} must be a non-negative integer")
        for name in ("timeout_seconds", "per_host_interval_seconds"):
            value = getattr(settings, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"fetch.{name} must be a number >= 0")
        return settings

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _decode_content(body: bytes, encoding: str | None, max_bytes: int) -> bytes:
    encoding = (encoding or "identity").strip().lower()
    if encoding in ("", "identity"):
        return body
    if encoding in ("gzip", "x-gzip"):
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as handle:
            return handle.read(max_bytes + 1)
    if encoding == "deflate":
        decompressor = zlib.decompressobj()
        try:
            return decompressor.decompress(body, max_bytes + 1)
        except zlib.error:
            return zlib.decompressobj(-zlib.MAX_WBITS).decompress(body, max_bytes + 1)
    raise ValueError(f"unsupported content encoding {encoding!r}")


@dataclass
class _HostClock:
    interval: float
    sleep: Callable[[float], None]
    monotonic: Callable[[], float]
    last: dict[str, float] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def wait(self, host: str) -> None:
        if self.interval <= 0:
            return
        with self.lock:
            now = self.monotonic()
            ready = self.last.get(host, now - self.interval) + self.interval
            self.last[host] = max(now, ready)
        if ready > now:
            self.sleep(ready - now)


class PageFetcher:
    """Fetches pages into a :class:`SnapshotStore`. Thread-safe; one instance per run.

    ``transport``, ``clock``, ``sleep`` and ``monotonic`` exist for tests.
    """

    name = "fetch"
    model_id = FETCH_MODEL_ID
    secret_name = ""

    def __init__(
        self,
        store: SnapshotStore,
        settings: FetchSettings | None = None,
        *,
        transport: FetchTransport | None = None,
        clock: Callable[[], str] = utc_now_iso,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.settings = settings or FetchSettings()
        self._transport = transport or urllib_fetch_transport
        self._clock = clock
        self._hosts = _HostClock(self.settings.per_host_interval_seconds, sleep, monotonic)
        self._robots: dict[str, tuple[robots_mod.RobotsRules, dict[str, Any]]] = {}
        self._robots_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @property
    def headers(self) -> dict[str, str]:
        return {"User-Agent": USER_AGENT, "Accept": ACCEPT, "Accept-Encoding": "gzip, identity"}

    def preload_robots(self, record: Mapping[str, Any]) -> None:
        """Reuse a robots decision recorded earlier in the same run (resume)."""
        outcome = record["outcome"]
        if outcome == "parsed":
            rules = robots_mod.parse_robots(self.store.get_blob(record["raw_sha256"]))
        elif outcome == "unavailable_allow_all":
            rules = robots_mod.ALLOW_ALL
        else:
            rules = robots_mod.DISALLOW_ALL
        self._robots.setdefault(record["origin"], (rules, dict(record)))

    # -- robots.txt -------------------------------------------------------- #

    def robots_for(self, url: str) -> tuple[robots_mod.RobotsRules, dict[str, Any]]:
        origin = robots_mod.origin(url)
        with self._guard:
            lock = self._robots_locks.setdefault(origin, threading.Lock())
        with lock:
            if origin not in self._robots:
                self._robots[origin] = self._fetch_robots(origin, url)
            return self._robots[origin]

    def _fetch_robots(self, origin: str, url: str) -> tuple[robots_mod.RobotsRules, dict[str, Any]]:
        target = robots_mod.robots_url(url)
        record: dict[str, Any] = {
            "kind": "robots", "origin": origin, "robots_url": target, "fetched_at": self._clock(),
            "http_status": None, "outcome": None, "raw_sha256": None, "error": None,
        }
        rules = robots_mod.DISALLOW_ALL
        try:
            response = None
            for _hop in range(6):
                self._hosts.wait(urllib.parse.urlsplit(target).netloc.lower())
                response = self._transport(target, self.headers, self.settings.timeout_seconds, robots_mod.MAX_ROBOTS_BYTES)
                location = response.header("location")
                if response.status in REDIRECT_STATUSES and location:
                    target = urllib.parse.urljoin(target, location)
                    continue
                break
            assert response is not None
            record["http_status"] = response.status
            if 200 <= response.status < 300:
                record["raw_sha256"] = self.store.put_blob(response.body[: robots_mod.MAX_ROBOTS_BYTES])
                record["outcome"] = "parsed"
                rules = robots_mod.parse_robots(response.body)
            elif 400 <= response.status < 500:
                record["outcome"] = "unavailable_allow_all"
                rules = robots_mod.ALLOW_ALL
            else:
                record["outcome"] = "unreachable_disallow_all"
                record["error"] = f"HTTP {response.status}"
        except FetchTransportError as exc:
            record["outcome"] = "unreachable_disallow_all"
            record["error"] = f"{exc.kind}: {exc}"
        self.store.append(record)
        return rules, record

    # -- pages --------------------------------------------------------------- #

    def fetch(self, url: str) -> PageSnapshot:
        """Fetch ``url`` once and store the outcome; never raises for a failed fetch."""
        fetched_at = self._clock()
        base: dict[str, Any] = {
            "kind": "page", "url": url, "final_url": url, "fetched_at": fetched_at, "http_status": None,
            "content_type": None, "charset": None, "raw_sha256": None, "raw_bytes": None, "truncated": False,
            "text_sha256": None, "text_chars": None, "title": None, "redirects": [], "robots": None,
            "error": None, "extractor_version": EXTRACTOR_VERSION, "user_agent": USER_AGENT,
        }

        def finish(outcome: str, **fields: Any) -> PageSnapshot:
            record = {**base, **fields, "outcome": outcome}
            record["snapshot_id"] = snapshot_id_for(url, fetched_at, outcome, record["raw_sha256"])
            self.store.append(record)
            return PageSnapshot(record)

        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return finish("invalid_url", error="not an http(s) URL with a host")
        current = url
        redirects: list[str] = []
        while True:
            rules, robots_record = self.robots_for(current)
            base["robots"] = {"origin": robots_record["origin"], "robots_outcome": robots_record["outcome"]}
            if not rules.allowed(current, PRODUCT_TOKEN):
                return finish("robots_disallowed", final_url=current, redirects=redirects,
                              error=f"robots.txt of {robots_record['origin']} disallows {PRODUCT_TOKEN}")
            self._hosts.wait(urllib.parse.urlsplit(current).netloc.lower())
            try:
                response = self._transport(current, self.headers, self.settings.timeout_seconds, self.settings.max_bytes)
            except FetchTransportError as exc:
                return finish(exc.kind, final_url=current, redirects=redirects, error=str(exc)[:500])
            location = response.header("location")
            if response.status in REDIRECT_STATUSES and location:
                if len(redirects) >= self.settings.max_redirects:
                    return finish("too_many_redirects", final_url=current, redirects=redirects, http_status=response.status,
                                  error=f"more than {self.settings.max_redirects} redirects")
                current = urllib.parse.urljoin(current, location)
                redirects.append(current)
                if urllib.parse.urlsplit(current).scheme not in ("http", "https"):
                    return finish("invalid_url", final_url=current, redirects=redirects, error="redirect to a non-http(s) URL")
                continue
            break
        media, _charset = content_type_of(response.header("content-type"))
        common = {"final_url": current, "redirects": redirects, "http_status": response.status,
                  "content_type": response.header("content-type")}
        if not 200 <= response.status < 300:
            return finish("http_error", **common, error=f"HTTP {response.status}")
        if media not in SUPPORTED_TYPES:
            return finish("unsupported_content_type", **common, error=f"media type {media or '(none)'} is not extracted")
        try:
            body = _decode_content(response.body, response.header("content-encoding"), self.settings.max_bytes)
        except (OSError, EOFError, zlib.error, ValueError) as exc:
            return finish("decode_error", **common, error=f"{type(exc).__name__}: {exc}"[:500])
        truncated = len(body) > self.settings.max_bytes or len(response.body) > self.settings.max_bytes
        body = body[: self.settings.max_bytes]
        raw_sha = self.store.put_blob(body)
        extracted = extract(body, response.header("content-type"))
        stored = {**common, "raw_sha256": raw_sha, "raw_bytes": len(body), "truncated": truncated,
                  "charset": extracted.charset, "title": extracted.title}
        if not extracted.text.strip():
            return finish("empty_text", **stored, error="no text after extraction")
        text_sha = self.store.put_text(extracted.text)
        assert text_sha == sha256_bytes(extracted.text.encode("utf-8"))
        return finish("ok", **stored, text_sha256=text_sha, text_chars=len(extracted.text))
