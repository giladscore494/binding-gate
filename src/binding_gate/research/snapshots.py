"""Snapshot store for live web evidence (PREREGISTRATION.md section 14).

Every web search response, every robots.txt and every fetched page is stored at
the moment it is obtained, under ``runs/<run_id>/snapshots/`` (git-ignored with
the rest of ``runs/``). The gate and the scorers never see a live page: the
documents shown to the model are built from the stored text, read back and
checked against its hash. Traces refer to pages by snapshot id and SHA-256
(``Attempt.evidence``), so a published trace can be matched to a snapshot
without publishing the page itself.

Layout
------
::

    snapshots/
      blobs/<sha256>        raw bytes (page body, robots.txt, search response JSON)
                            and extracted page text (UTF-8), each named by the
                            SHA-256 of its own bytes; written once, read-only
      index.jsonl           one JSON object per event, append-only, fsynced:
                            kind "page", "robots" or "search" (fields below)
      publish_manifest.json written by :meth:`SnapshotStore.write_publish_manifest`

Immutability: a blob is written to a temporary file, fsynced and renamed into
place only if no blob of that name exists; an existing blob is never
rewritten (its content is verified instead), and blobs are made read-only.
:meth:`SnapshotStore.verify` re-hashes every blob named in the index. The index
is append-only; records are never edited or removed.

Page record (``kind = "page"``)
-------------------------------
``snapshot_id`` (``snap-`` + 16 hex digits of the SHA-256 of the canonical JSON
of url, fetched_at, outcome and raw_sha256), ``url`` (as given by the search
result), ``final_url`` (after redirects), ``fetched_at`` (UTC, ISO-8601),
``outcome`` (``ok`` or a failure code, see ``binding_gate.research.fetch``),
``http_status``, ``content_type``, ``charset``, ``raw_sha256``, ``raw_bytes``,
``truncated`` (body longer than the size limit), ``text_sha256``,
``text_chars``, ``title``, ``redirects`` (list of URLs), ``robots`` (origin and
decision), ``error`` (text for failures), ``extractor_version``,
``user_agent``.

Search record (``kind = "search"``)
-----------------------------------
``search_id``, ``key`` (cache key: provider, parameters and query),
``provider``, ``model_id``, ``query``, ``params``, ``requested_at``,
``response_sha256`` (the provider's raw JSON response, stored as a blob),
``results`` (rank, title, url, snippet) and ``call`` (the logged
``ModelCall`` as JSON, so that a resumed run can show the same call as a cache
hit).

Robots record (``kind = "robots"``)
-----------------------------------
``origin``, ``robots_url``, ``fetched_at``, ``http_status``, ``outcome``
(``parsed``, ``unavailable_allow_all``, ``unreachable_disallow_all``),
``raw_sha256``, ``error``.

Publication
-----------
Page contents are third-party material and stay private unless their licence
allows publication. :meth:`SnapshotStore.write_publish_manifest` writes only
URLs, timestamps, HTTP status, content types and hashes, plus for each search
the query, time, provider and the ranked result URLs (no titles or snippets).

Command line: ``python -m binding_gate.research.snapshots verify RUN_DIR`` checks
every blob and every evidence reference of the run's traces against the store;
``publish-manifest RUN_DIR`` writes the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from binding_gate.canonical import canonical_json, sha256_json, utc_now_iso

SNAPSHOTS_DIRNAME = "snapshots"
BLOBS_DIRNAME = "blobs"
INDEX_NAME = "index.jsonl"
PUBLISH_MANIFEST_NAME = "publish_manifest.json"
KINDS = ("page", "robots", "search")


class SnapshotError(RuntimeError):
    """Raised when the store is inconsistent or a blob does not match its hash."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_id_for(url: str, fetched_at: str, outcome: str, raw_sha256: str | None) -> str:
    digest = sha256_json({"url": url, "fetched_at": fetched_at, "outcome": outcome, "raw_sha256": raw_sha256})
    return f"snap-{digest[:16]}"


@dataclass(frozen=True)
class PageSnapshot:
    """One page record of the index (see the module docstring)."""

    record: dict[str, Any]

    @property
    def snapshot_id(self) -> str:
        return self.record["snapshot_id"]

    @property
    def url(self) -> str:
        return self.record["url"]

    @property
    def final_url(self) -> str:
        return self.record.get("final_url") or self.record["url"]

    @property
    def ok(self) -> bool:
        return self.record["outcome"] == "ok"

    @property
    def text_sha256(self) -> str | None:
        return self.record.get("text_sha256")

    @property
    def fetched_at(self) -> str:
        return self.record["fetched_at"]

    @property
    def title(self) -> str | None:
        return self.record.get("title")


class SnapshotStore:
    """Write-once storage of live web evidence for one run. Thread-safe."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.blobs = self.root / BLOBS_DIRNAME
        self.index_path = self.root / INDEX_NAME
        self._lock = threading.Lock()

    @classmethod
    def for_run(cls, run_dir: str | Path) -> SnapshotStore:
        return cls(Path(run_dir) / SNAPSHOTS_DIRNAME)

    # -- blobs -------------------------------------------------------------- #

    def _ensure_dirs(self) -> None:
        self.blobs.mkdir(parents=True, exist_ok=True)

    def put_blob(self, data: bytes) -> str:
        """Store ``data`` under its SHA-256 (once) and return the digest."""
        digest = sha256_bytes(data)
        with self._lock:
            self._ensure_dirs()
            path = self.blobs / digest
            if path.exists():
                if sha256_bytes(path.read_bytes()) != digest:
                    raise SnapshotError(f"stored blob {digest} does not match its name (altered on disk)")
                return digest
            tmp = path.with_name(f".{digest}.{os.getpid()}.{threading.get_ident()}.tmp")
            with tmp.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        return digest

    def put_text(self, text: str) -> str:
        return self.put_blob(text.encode("utf-8"))

    def get_blob(self, digest: str) -> bytes:
        path = self.blobs / digest
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SnapshotError(f"blob {digest} is missing from {self.blobs}: {exc}") from exc
        if sha256_bytes(data) != digest:
            raise SnapshotError(f"blob {digest} does not match its hash (altered on disk)")
        return data

    def get_text(self, digest: str) -> str:
        return self.get_blob(digest).decode("utf-8")

    # -- index -------------------------------------------------------------- #

    def append(self, record: dict[str, Any]) -> None:
        if record.get("kind") not in KINDS:
            raise SnapshotError(f"unknown record kind {record.get('kind')!r}")
        line = canonical_json(record)
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.index_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def records(self, kind: str | None = None) -> Iterator[dict[str, Any]]:
        if not self.index_path.exists():
            return
        with self.index_path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SnapshotError(f"{self.index_path}:{number}: not JSON: {exc}") from exc
                if kind is None or record.get("kind") == kind:
                    yield record

    def pages(self) -> list[PageSnapshot]:
        return [PageSnapshot(r) for r in self.records("page")]

    def page(self, snapshot_id: str) -> PageSnapshot:
        for snapshot in self.pages():
            if snapshot.snapshot_id == snapshot_id:
                return snapshot
        raise SnapshotError(f"no page snapshot {snapshot_id!r} in {self.index_path}")

    # -- integrity and publication ------------------------------------------ #

    def verify(self) -> dict[str, int]:
        """Re-hash every blob named in the index; raise :class:`SnapshotError` on any mismatch."""
        counts = {"page": 0, "robots": 0, "search": 0, "blobs_checked": 0}
        seen: set[str] = set()
        for record in self.records():
            counts[record["kind"]] += 1
            for key in ("raw_sha256", "text_sha256", "response_sha256"):
                digest = record.get(key)
                if digest and digest not in seen:
                    self.get_blob(digest)
                    seen.add(digest)
                    counts["blobs_checked"] += 1
        return counts

    def publish_manifest(self) -> dict[str, Any]:
        """URLs, timestamps and hashes only (no page text, titles or snippets)."""
        pages = []
        for r in self.records("page"):
            pages.append(
                {
                    key: r.get(key)
                    for key in (
                        "snapshot_id", "url", "final_url", "fetched_at", "outcome", "http_status",
                        "content_type", "raw_sha256", "raw_bytes", "text_sha256", "extractor_version",
                    )
                }
            )
        searches = [
            {
                "search_id": r["search_id"],
                "provider": r["provider"],
                "model_id": r["model_id"],
                "query": r["query"],
                "params": r["params"],
                "requested_at": r["requested_at"],
                "response_sha256": r["response_sha256"],
                "result_urls": [{"rank": x["rank"], "url": x["url"]} for x in r["results"]],
            }
            for r in self.records("search")
        ]
        robots = [
            {key: r.get(key) for key in ("origin", "robots_url", "fetched_at", "http_status", "outcome", "raw_sha256")}
            for r in self.records("robots")
        ]
        return {
            "manifest_version": 1,
            "generated_at": utc_now_iso(),
            "note": (
                "Page contents are not included: they are third-party material kept privately "
                "(PREREGISTRATION.md section 14). Each page can be matched by its SHA-256."
            ),
            "pages": pages,
            "searches": searches,
            "robots": robots,
        }

    def write_publish_manifest(self) -> Path:
        path = self.root / PUBLISH_MANIFEST_NAME
        self.root.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.publish_manifest(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path


# --------------------------------------------------------------------------- #
# Checking traces against the store
# --------------------------------------------------------------------------- #


def verify_run(run_dir: str | Path) -> dict[str, Any]:
    """Check every blob, and every snapshot reference in the run's traces, against the store.

    For each attempt evidence reference whose id is a snapshot id: the snapshot
    exists, its final URL equals the reference's URL, the text shown to the
    model is stored as a blob under the referenced SHA-256 (the session stores
    it when it builds the document), and that shown text is a prefix of the
    page's stored extracted text. Returns counts; raises on the first mismatch.
    """
    from binding_gate.trace import read_traces

    store = SnapshotStore.for_run(run_dir)
    counts = store.verify()
    pages = {p.snapshot_id: p for p in store.pages()}
    checked = 0
    for record in read_traces(run_dir):
        for attempt in record.attempts:
            for ref in attempt.evidence:
                if not ref.id.startswith("snap-"):
                    continue
                where = f"trace {record.item_id}/{record.arm}/{record.model}/{record.repeat}: {ref.id}"
                page = pages.get(ref.id)
                if page is None or page.text_sha256 is None:
                    raise SnapshotError(f"{where} is not a stored page with text")
                if ref.url is not None and ref.url != page.final_url:
                    raise SnapshotError(f"{where}: URL differs from the store")
                shown = store.get_text(ref.sha256)
                if not store.get_text(page.text_sha256).startswith(shown):
                    raise SnapshotError(f"{where}: the shown text is not a prefix of the stored page text")
                checked += 1
    counts["evidence_refs_checked"] = checked
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m binding_gate.research.snapshots",
        description="Check or publish the live-web snapshots of a run. Makes no network request.",
    )
    parser.add_argument("command", choices=["verify", "publish-manifest"])
    parser.add_argument("run_dir")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            print(json.dumps(verify_run(args.run_dir), indent=2, sort_keys=True))
        else:
            print(SnapshotStore.for_run(args.run_dir).write_publish_manifest())
    except (SnapshotError, OSError, ValueError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
