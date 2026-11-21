"""Trace writer: append-only JSONL plus a manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import builders as b
import binding_gate
from binding_gate.contracts import SCHEMA_VERSION
from binding_gate.trace import (
    MANIFEST_NAME,
    TRACES_NAME,
    TraceError,
    TraceWriter,
    new_run_id,
    read_manifest,
    read_traces,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return b.init_repo(tmp_path / "repo")


def _start(tmp_path: Path, repo: Path, **kwargs) -> TraceWriter:
    return TraceWriter.start(tmp_path / "runs", b.run_config(), repo_root=repo, **kwargs)


def _record(writer: TraceWriter, item_id: str = "item-1", repeat: int = 0):
    m = writer.manifest
    return b.trace_record(run_id=m.run_id, config_hash=m.config_hash, git_commit=m.git_commit, item_id=item_id, repeat=repeat)


def test_manifest_is_written_at_start(tmp_path, repo):
    writer = _start(tmp_path, repo, config_path="configs/x.toml")
    manifest = read_manifest(writer.run_dir)
    config = b.run_config()
    assert manifest.config == config.to_dict() and manifest.config_hash == config.config_hash
    assert manifest.git_commit == b.git(repo, "rev-parse", "HEAD") and manifest.git_dirty is False
    assert manifest.package_version == binding_gate.__version__
    assert manifest.schema_version == SCHEMA_VERSION
    assert manifest.config_path == "configs/x.toml"
    assert manifest.started_at and manifest.ended_at is None and manifest.trace_count is None
    assert writer.run_dir.name == manifest.run_id and manifest.run_id.split("-")[1] == config.config_hash[:8]
    writer.close()


def test_dirty_tree_is_recorded(tmp_path, repo):
    (repo / "scratch.py").write_text("x = 1\n")
    with _start(tmp_path, repo) as writer:
        assert writer.manifest.git_dirty is True


def test_records_round_trip_in_order_and_manifest_is_completed(tmp_path, repo):
    with _start(tmp_path, repo) as writer:
        records = [_record(writer, f"item-{i}") for i in range(5)]
        for record in records:
            writer.append(record)
    assert list(read_traces(writer.run_dir)) == records
    manifest = read_manifest(writer.run_dir)
    raw = (writer.run_dir / TRACES_NAME).read_bytes()
    assert manifest.trace_count == 5 and manifest.ended_at is not None
    assert manifest.traces_sha256 == hashlib.sha256(raw).hexdigest()
    lines = raw.decode().splitlines()
    assert len(lines) == 5 and all(json.loads(line)["schema_version"] == SCHEMA_VERSION for line in lines)


def test_each_append_only_adds_bytes_and_is_on_disk_immediately(tmp_path, repo):
    writer = _start(tmp_path, repo)
    path = writer.run_dir / TRACES_NAME
    previous = b""
    for i in range(4):
        writer.append(_record(writer, f"item-{i}"))
        current = path.read_bytes()  # read while the writer is still open
        assert current.startswith(previous) and len(current) > len(previous)
        assert current.endswith(b"\n")
        previous = current
    writer.close()
    assert path.read_bytes() == previous


def test_existing_run_directory_is_never_overwritten(tmp_path, repo):
    with _start(tmp_path, repo, run_id="fixed") as writer:
        writer.append(_record(writer))
    before = (writer.run_dir / TRACES_NAME).read_bytes()
    with pytest.raises(TraceError, match="already exists"):
        _start(tmp_path, repo, run_id="fixed")
    assert (writer.run_dir / TRACES_NAME).read_bytes() == before


def test_foreign_and_duplicate_records_are_refused(tmp_path, repo):
    with _start(tmp_path, repo) as writer:
        good = _record(writer)
        writer.append(good)
        with pytest.raises(TraceError, match="already written"):
            writer.append(good)
        m = writer.manifest
        for kwargs, field in [
            ({"run_id": "other", "config_hash": m.config_hash, "git_commit": m.git_commit}, "run_id"),
            ({"run_id": m.run_id, "config_hash": "f" * 64, "git_commit": m.git_commit}, "config_hash"),
            ({"run_id": m.run_id, "config_hash": m.config_hash, "git_commit": "b" * 40}, "git_commit"),
        ]:
            with pytest.raises(TraceError, match=field):
                writer.append(b.trace_record(item_id="item-2", **kwargs))
        with pytest.raises(TraceError, match="TraceRecord"):
            writer.append(good.to_dict())  # type: ignore[arg-type]
        writer.append(_record(writer, repeat=1))  # same item, another repeat: allowed
    assert read_manifest(writer.run_dir).trace_count == 2


def test_closed_writer_refuses_appends_and_close_is_idempotent(tmp_path, repo):
    writer = _start(tmp_path, repo)
    writer.append(_record(writer))
    first = writer.close()
    assert writer.close() == first
    with pytest.raises(TraceError, match="closed"):
        writer.append(_record(writer, "item-2"))


def test_crashed_run_stays_open_and_can_be_resumed(tmp_path, repo):
    with pytest.raises(RuntimeError, match="boom"):
        with _start(tmp_path, repo) as writer:
            writer.append(_record(writer, "item-1"))
            raise RuntimeError("boom")
    assert read_manifest(writer.run_dir).ended_at is None
    before = (writer.run_dir / TRACES_NAME).read_bytes()

    resumed = TraceWriter.resume(writer.run_dir, b.run_config(), repo_root=repo)
    assert resumed.completed_keys() == {("item-1", "gated", "model-a", 0)}
    with pytest.raises(TraceError, match="already written"):
        resumed.append(_record(resumed, "item-1"))
    resumed.append(_record(resumed, "item-2"))
    manifest = resumed.close()
    assert (writer.run_dir / TRACES_NAME).read_bytes().startswith(before)
    assert manifest.trace_count == 2 and manifest.run_id == writer.manifest.run_id


def test_resume_is_refused_for_closed_runs_other_configs_and_other_commits(tmp_path, repo):
    with _start(tmp_path, repo) as closed:
        pass
    with pytest.raises(TraceError, match="already closed"):
        TraceWriter.resume(closed.run_dir, b.run_config(), repo_root=repo)

    open_run = _start(tmp_path, repo)
    open_run._handle.close()  # simulate a crash without close()
    with pytest.raises(TraceError, match="config hash differs"):
        TraceWriter.resume(open_run.run_dir, b.run_config(repeats=5), repo_root=repo)
    (repo / "new.txt").write_text("x\n")
    b.git(repo, "add", "-A")
    b.git(repo, "commit", "-q", "-m", "code changed")
    with pytest.raises(TraceError, match="HEAD is now"):
        TraceWriter.resume(open_run.run_dir, b.run_config(), repo_root=repo)


def test_corrupt_trace_line_is_reported_with_its_line_number(tmp_path, repo):
    with _start(tmp_path, repo) as writer:
        writer.append(_record(writer))
    with (writer.run_dir / TRACES_NAME).open("a") as handle:
        handle.write('{"schema_version": 999}\n')
    with pytest.raises(TraceError, match=r"traces\.jsonl:2"):
        list(read_traces(writer.run_dir))


def test_run_ids_are_unique_and_carry_the_config_hash():
    config_hash = b.run_config().config_hash
    ids = {new_run_id(config_hash) for _ in range(50)}
    assert len(ids) > 1 and all(config_hash[:8] in run_id for run_id in ids)


def test_manifest_file_is_readable_json(tmp_path, repo):
    with _start(tmp_path, repo) as writer:
        pass
    data = json.loads((writer.run_dir / MANIFEST_NAME).read_text())
    assert data["run_id"] == writer.manifest.run_id and data["trace_count"] == 0
