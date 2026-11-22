"""Polite, cached HTTP access to SEC EDGAR for the layer-2 dataset builder (PR 17).

Used only by ``binding_gate.datasets.sec_build`` (``cli dataset-prepare``), never
by a run: runs read the generated dataset directory, verified against its pinned
manifest hash. No API key exists or is needed; EDGAR data are free.

Fair access (SEC's published rules; **unverified** in the PR 17 sandbox, whose
proxy blocked sec.gov: the pages "Accessing EDGAR Data" and "EDGAR Application
Programming Interfaces" could not be opened, so what follows is the rule as the
project understands it and must be checked against those pages):

* every request declares a ``User-Agent`` naming the requester and a contact
  e-mail (SEC's example has the form "Sample Company Name AdminContact@<sample
  company domain>.com"). The User-Agent is built at run time as
  ``"<project part> <e-mail>"``: the project part (product token, version and
  project URL) is in the committed builder config
  (``configs/datasets/sec_financials_v1.toml``, key ``user_agent_project``); the
  e-mail is the owner's and is read ONLY from the repository secret /
  environment variable ``SEC_CONTACT_EMAIL`` (``binding_gate.secrets``). Without
  it the build refuses (:func:`contact_email_from_secret`); no address is ever
  made up. The e-mail and the full User-Agent are never written to the cache,
  the retrieval log, the manifest, the items or any other output: outputs
  record only that a contact was declared;
* at most 10 requests per second. This client enforces
  ``max_requests_per_second`` (default 5, refused above :data:`MAX_RPS_ALLOWED`)
  with a monotonic-clock spacing between consecutive network requests;
* every response is cached on disk under ``cache_dir`` (one file per URL, keyed
  by the SHA-256 of the URL), so a repeated or resumed build re-reads the cache
  instead of the network;
* transient failures (HTTP 429, 5xx, timeouts, connection errors) are retried at
  most ``max_retries`` times with exponential backoff (2 s doubling, capped at
  60 s); every attempt, successful or not, is appended to the retrieval log
  (``retrieval_log.jsonl``), with the URL, time, status, bytes and SHA-256 of the
  body. HTTP 404 is not retried and is reported as :class:`NotFound`.

The transport is injectable (``transport``), so the whole builder is tested on
fixtures shaped like EDGAR responses without any network access.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from binding_gate import __version__
from binding_gate.canonical import utc_now_iso

PROJECT_URL = "https://github.com/giladscore494/binding-gate"
PRODUCT_TOKEN = "binding-gate-dataset-builder"
MAX_RPS_ALLOWED = 8.0
"""Upper bound accepted for ``max_requests_per_second``: SEC's limit is 10 per second
(unverified, see the module docstring); 8 keeps a margin."""
DEFAULT_RPS = 5.0
DEFAULT_MAX_RETRIES = 4
BACKOFF_START_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0
TIMEOUT_SECONDS = 60.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FRAMES_URL = "https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{filename}"


class EdgarError(RuntimeError):
    """Raised when EDGAR cannot be reached or answers in an unexpected way."""


class NotFound(EdgarError):
    """HTTP 404 for a URL (not retried)."""


DEFAULT_PROJECT_PART = f"{PRODUCT_TOKEN}/{__version__} (+{PROJECT_URL})"
CONTACT_SECRET = "SEC_CONTACT_EMAIL"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MissingContact(EdgarError):
    """The contact e-mail secret is not set; EDGAR is not contacted."""


def contact_email_from_secret() -> str:
    """The owner's contact e-mail from the secret ``SEC_CONTACT_EMAIL`` (never logged, never stored)."""
    from binding_gate import secrets

    if not secrets.has_secret(CONTACT_SECRET):
        raise MissingContact(
            f"REFUSED: the repository secret / environment variable {CONTACT_SECRET} is not set. SEC's fair-access "
            "rules ask every automated client to declare a contact e-mail in its User-Agent; the layer-2 dataset "
            f"(sec_financials) is therefore built only with {CONTACT_SECRET} set (docs/RUNNING.md). Nothing was "
            "downloaded. No address is substituted."
        )
    return secrets.get_secret(CONTACT_SECRET)


def user_agent(project_part: str, contact_email: str) -> str:
    """The declared User-Agent ``"<project part> <e-mail>"`` (built in memory only)."""
    project_part = " ".join(str(project_part).split())
    if not project_part or "@" in project_part:
        raise EdgarError(
            "the committed User-Agent project part must be non-empty and contain no e-mail address "
            "(configs/datasets/sec_financials_v1.toml, user_agent_project)"
        )
    contact_email = str(contact_email).strip()
    if not _EMAIL.match(contact_email):
        raise MissingContact(f"{CONTACT_SECRET} does not look like an e-mail address (its value is not shown)")
    return f"{project_part} {contact_email}"


def archive_url(cik: int, accession: str, filename: str) -> str:
    return ARCHIVE_URL.format(cik=int(cik), accession_nodash=accession.replace("-", ""), filename=filename)


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: dict[str, str]


Transport = Callable[[str, dict[str, str], float], Response]
"""``transport(url, headers, timeout) -> Response``; raises ``OSError`` on connection failure."""


def urllib_transport(url: str, headers: dict[str, str], timeout: float) -> Response:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return Response(int(response.status), body, {k.lower(): v for k, v in response.headers.items()})
    except urllib.error.HTTPError as exc:
        return Response(int(exc.code), exc.read() if exc.fp else b"", {k.lower(): v for k, v in (exc.headers or {}).items()})


def _decode(body: bytes, encoding: str | None) -> bytes:
    encoding = (encoding or "").lower()
    if encoding == "gzip" or body[:2] == b"\x1f\x8b":
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body)
    return body


class EdgarClient:
    """GET with the declared User-Agent, a request-rate limit, an on-disk cache and logged retries."""

    def __init__(
        self,
        *,
        contact_email: str,
        project_part: str = DEFAULT_PROJECT_PART,
        cache_dir: str | Path,
        log_path: str | Path | None = None,
        max_requests_per_second: float = DEFAULT_RPS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        offline: bool = False,
    ):
        if isinstance(max_requests_per_second, bool) or not isinstance(max_requests_per_second, (int, float)):
            raise EdgarError("max_requests_per_second must be a number")
        if not 0 < float(max_requests_per_second) <= MAX_RPS_ALLOWED:
            raise EdgarError(
                f"max_requests_per_second must be in (0, {MAX_RPS_ALLOWED}]: SEC's fair-access limit is 10 per second"
            )
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise EdgarError("max_retries must be an integer >= 0")
        self._user_agent = user_agent(project_part, contact_email)  # private: never logged or stored
        self.contact_declared = True
        self.min_interval = 1.0 / float(max_requests_per_second)
        self.max_retries = max_retries
        self.cache_dir = Path(cache_dir)
        self.log_path = Path(log_path) if log_path is not None else None
        self.transport = transport
        self.sleep = sleep
        self.clock = clock
        self.offline = offline
        self._last_request: float | None = None
        self.network_requests = 0
        self.cache_hits = 0

    # -- cache ------------------------------------------------------------------------- #

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.body", self.cache_dir / f"{key}.json"

    def cached(self, url: str) -> bytes | None:
        body_path, meta_path = self._cache_paths(url)
        if not (body_path.is_file() and meta_path.is_file()):
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        body = body_path.read_bytes()
        if meta.get("url") != url or meta.get("sha256") != hashlib.sha256(body).hexdigest():
            return None
        if meta.get("status") == 404:
            raise NotFound(url)
        return body

    def _store(self, url: str, status: int, body: bytes) -> None:
        body_path, meta_path = self._cache_paths(url)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(body)
        meta = {"url": url, "status": status, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "retrieved_at": utc_now_iso()}
        meta_path.write_text(json.dumps(meta, sort_keys=True) + "\n", encoding="utf-8")

    # -- log --------------------------------------------------------------------------- #

    def _log(self, entry: dict) -> None:
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    # -- requests ---------------------------------------------------------------------- #

    def _wait_for_slot(self) -> None:
        if self._last_request is not None:
            wait = self.min_interval - (self.clock() - self._last_request)
            if wait > 0:
                self.sleep(wait)
        self._last_request = self.clock()

    def get(self, url: str) -> bytes:
        """The body of ``url`` (from the cache when present). Raises :class:`NotFound` or :class:`EdgarError`."""
        body = self.cached(url)
        if body is not None:
            self.cache_hits += 1
            return body
        if self.offline:
            raise EdgarError(f"offline build: {url} is not in the cache {self.cache_dir}")
        headers = {"User-Agent": self._user_agent, "Accept-Encoding": "gzip, deflate"}
        backoff = BACKOFF_START_SECONDS
        last_error = ""
        for attempt in range(1, self.max_retries + 2):
            self._wait_for_slot()
            self.network_requests += 1
            started = utc_now_iso()
            try:
                response = self.transport(url, headers, TIMEOUT_SECONDS)
            except OSError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self._log({"url": url, "attempt": attempt, "started_at": started, "status": None, "error": last_error})
            else:
                body = _decode(response.body, response.headers.get("content-encoding")) if response.status == 200 else response.body
                self._log({
                    "url": url, "attempt": attempt, "started_at": started, "status": response.status,
                    "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                })
                if response.status == 200:
                    self._store(url, 200, body)
                    return body
                if response.status == 404:
                    self._store(url, 404, b"")
                    raise NotFound(url)
                last_error = f"HTTP {response.status}"
                if response.status not in RETRY_STATUSES:
                    raise EdgarError(f"{url}: {last_error} (not retried)")
            if attempt <= self.max_retries:
                self.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
        raise EdgarError(f"{url}: failed after {self.max_retries + 1} attempt(s): {last_error}")

    def get_json(self, url: str) -> object:
        body = self.get(url)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise EdgarError(f"{url}: not valid JSON: {exc}") from exc
