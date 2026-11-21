"""Append-only run traces.

A run directory ``<runs_dir>/<run_id>/`` contains

* ``manifest.json`` - a :class:`~binding_gate.contracts.RunManifest`, written when
  the run starts and completed (end time, record count, SHA-256 of the trace
  file) when it is closed;
* ``traces.jsonl`` - one :class:`~binding_gate.contracts.TraceRecord` per line.

Guarantees of :class:`TraceWriter`
----------------------------------
* It never truncates, rewrites or deletes ``traces.jsonl``: the file is only
  ever opened in append mode and every record is flushed to disk when written.
* It refuses to start in a directory that already exists, so a run can never
  overwrite another run.
* It refuses records that belong to another run (different run id, config hash
  or git commit) and refuses a second record for the same
  ``(item, arm, model, repeat)``.
* A crashed run can be continued with :meth:`TraceWriter.resume` only under the
  same config hash and git commit; records already written are kept.

Re-scoring a finished run must write new files next to the trace
(``scores.<scorer>.jsonl``); it must not modify ``traces.jsonl``.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Iterator

import binding_gate
from binding_gate import gitinfo
from binding_gate.canonical import utc_now_iso
from binding_gate.config import RunConfig
from binding_gate.contracts import ContractError, RunManifest, TraceRecord
from binding_gate.testlock import ACCESS_LOG_RELPATH

MANIFEST_NAME = "manifest.json"
TRACES_NAME = "traces.jsonl"

TraceKey = tuple[str, str, str, int]


class TraceError(RuntimeError):
    """Raised on any attempt to use a run directory in an unsupported way."""


def new_run_id(config_hash: str) -> str:
    """``<UTC timestamp>-<first 8 hex of config hash>-<4 random hex>``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{config_hash[:8]}-{secrets.token_hex(2)}"


def _key(record: TraceRecord) -> TraceKey:
    return (record.item_id, record.arm, record.model, record.repeat)


def _write_manifest(run_dir: Path, manifest: RunManifest) -> None:
    """Write the manifest atomically (temp file + rename)."""
    temporary = run_dir / (MANIFEST_NAME + ".tmp")
    temporary.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, run_dir / MANIFEST_NAME)


def read_manifest(run_dir: str | Path) -> RunManifest:
    path = Path(run_dir) / MANIFEST_NAME
    try:
        return RunManifest.from_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TraceError(f"cannot read {path}: {exc}") from exc


def read_traces(run_dir: str | Path) -> Iterator[TraceRecord]:
    """Yield the trace records of a run directory in the order they were written."""
    path = Path(run_dir) / TRACES_NAME
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield TraceRecord.from_json(line)
            except ContractError as exc:
                raise TraceError(f"{path}:{number}: {exc}") from exc


class TraceWriter:
    """Append-only writer for one run. Use :meth:`start` or :meth:`resume`."""

    def __init__(self, run_dir: Path, manifest: RunManifest, written: set[TraceKey]):
        self.run_dir = run_dir
        self.manifest = manifest
        self._written = written
        self._handle = (run_dir / TRACES_NAME).open("a", encoding="utf-8", newline="\n")

    # -- construction ------------------------------------------------------ #

    @classmethod
    def start(
        cls,
        runs_dir: str | Path,
        config: RunConfig,
        *,
        repo_root: str | Path,
        config_path: str | Path | None = None,
        test_unlock_tag: str | None = None,
        run_id: str | None = None,
    ) -> TraceWriter:
        """Create a new run directory and write the initial manifest.

        ``repo_root`` must be the top level of a git repository: the commit hash
        and whether the working tree had uncommitted changes (the test access
        log aside) are recorded in the manifest.
        """
        config_hash = config.config_hash
        run_id = run_id or new_run_id(config_hash)
        run_dir = Path(runs_dir) / run_id
        repo_root = Path(repo_root)
        manifest = RunManifest(
            run_id=run_id,
            config=config.to_dict(),
            config_hash=config_hash,
            git_commit=gitinfo.head_commit(repo_root),
            git_dirty=bool(gitinfo.dirty_paths(repo_root, ignore=[ACCESS_LOG_RELPATH])),
            package_version=binding_gate.__version__,
            python_version=platform.python_version(),
            started_at=utc_now_iso(),
            config_path=None if config_path is None else Path(config_path).as_posix(),
            test_unlock_tag=test_unlock_tag,
        )
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise TraceError(f"run directory {run_dir} already exists; runs are never overwritten") from None
        _write_manifest(run_dir, manifest)
        return cls(run_dir, manifest, set())

    @classmethod
    def resume(cls, run_dir: str | Path, config: RunConfig, *, repo_root: str | Path) -> TraceWriter:
        """Continue an unfinished run; allowed only with the same config and commit."""
        run_dir = Path(run_dir)
        manifest = read_manifest(run_dir)
        if manifest.ended_at is not None:
            raise TraceError(f"run {manifest.run_id} is already closed and cannot be resumed")
        if manifest.config_hash != config.config_hash:
            raise TraceError("cannot resume: the config hash differs from the run's manifest")
        commit = gitinfo.head_commit(Path(repo_root))
        if manifest.git_commit != commit:
            raise TraceError(
                f"cannot resume: run was started at commit {manifest.git_commit[:12]}, "
                f"HEAD is now {commit[:12]}"
            )
        traces = run_dir / TRACES_NAME
        written = {_key(record) for record in read_traces(run_dir)} if traces.exists() else set()
        return cls(run_dir, manifest, written)

    # -- writing ----------------------------------------------------------- #

    @property
    def closed(self) -> bool:
        return self._handle.closed

    def completed_keys(self) -> frozenset[TraceKey]:
        """``(item_id, arm, model, repeat)`` of every record already in the trace."""
        return frozenset(self._written)

    def append(self, record: TraceRecord) -> None:
        """Validate and append one record, flushing it to disk."""
        if self.closed:
            raise TraceError("the trace writer is closed")
        if not isinstance(record, TraceRecord):
            raise TraceError("only TraceRecord objects can be appended")
        for name in ("run_id", "config_hash", "git_commit"):
            if getattr(record, name) != getattr(self.manifest, name):
                raise TraceError(f"record {name} does not match the run manifest")
        key = _key(record)
        if key in self._written:
            raise TraceError(f"a record for (item, arm, model, repeat) = {key} was already written")
        self._handle.write(record.to_json() + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._written.add(key)

    def close(self) -> RunManifest:
        """Close the trace and complete the manifest. Idempotent."""
        if self.closed:
            return self.manifest
        self._handle.close()
        digest = hashlib.sha256((self.run_dir / TRACES_NAME).read_bytes()).hexdigest()
        self.manifest = RunManifest.from_dict(
            {
                **self.manifest.to_dict(),
                "ended_at": utc_now_iso(),
                "trace_count": len(self._written),
                "traces_sha256": digest,
            }
        )
        _write_manifest(self.run_dir, self.manifest)
        return self.manifest

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # A run that died with an exception stays "unfinished" (no ended_at) so
        # that it is visibly incomplete and can be resumed.
        if exc_type is None:
            self.close()
        else:
            self._handle.close()
