"""Test-set lock: refuses unless every condition holds, and logs every attempt."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import builders as b
from binding_gate.contracts import Split
from binding_gate.split import item_split, select_dev
from binding_gate.testlock import (
    ACCESS_LOG_RELPATH,
    EVENT_LOADED,
    EVENT_REFUSED,
    AccessLogError,
    TestSplitLocked,
    check_unlock,
    load_test_split,
    preregistration_status,
    read_access_log,
    verify_access_log,
)

TAG = "prereg-freeze-v1"
ITEMS = [b.item(item_id=f"g{g}-c{c}", group_id=f"g{g}") for g in range(40) for c in range(3)]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clean repository whose HEAD carries the freeze tag and a FROZEN pre-registration."""
    root = b.init_repo(tmp_path / "repo")
    b.git(root, "tag", TAG)
    return root


def _log(repo: Path) -> list[dict]:
    return read_access_log(repo / ACCESS_LOG_RELPATH)


def _load(repo: Path, tag: str = TAG, purpose: str = "frozen test run"):
    return load_test_split(ITEMS, repo_root=repo, confirm_tag=tag, purpose=purpose)


def _assert_refused(repo: Path, match: str, **kwargs) -> dict:
    before = len(_log(repo))
    with pytest.raises(TestSplitLocked, match=match):
        _load(repo, **kwargs)
    entries = _log(repo)
    assert len(entries) == before + 1, "a refusal must be logged"
    assert entries[-1]["event"] == EVENT_REFUSED and entries[-1]["refusal_reasons"]
    assert "n_items" not in entries[-1]
    return entries[-1]


def test_unlock_succeeds_only_when_everything_holds(repo):
    selected = _load(repo)
    assert selected and all(item_split(i) is Split.TEST for i in selected)
    assert {i.id for i in selected} | {i.id for i in select_dev(ITEMS)} == {i.id for i in ITEMS}
    (entry,) = _log(repo)
    assert entry["event"] == EVENT_LOADED
    assert entry["confirm_tag"] == TAG and entry["purpose"] == "frozen test run"
    assert entry["head_commit"] == b.git(repo, "rev-parse", "HEAD")
    assert entry["n_items"] == len(selected)
    assert entry["n_groups"] == len({i.group_id for i in selected})
    assert entry["datasets"] == ["toy"]


def test_access_log_itself_does_not_block_the_next_access(repo):
    _load(repo)
    _load(repo)  # the log is now an uncommitted file, which must be tolerated
    assert [e["event"] for e in _log(repo)] == [EVENT_LOADED, EVENT_LOADED]


def test_refused_without_the_right_tag_name(repo):
    b.git(repo, "tag", "v1.0")
    entry = _assert_refused(repo, "prereg-freeze-", tag="v1.0")
    assert entry["confirm_tag"] == "v1.0"
    _assert_refused(repo, "prereg-freeze-", tag="")


def test_refused_when_tag_does_not_exist(repo):
    _assert_refused(repo, "does not exist", tag="prereg-freeze-v2")


def test_refused_when_head_is_not_the_tagged_commit(repo):
    (repo / "later.txt").write_text("more work\n")
    b.git(repo, "add", "-A")
    b.git(repo, "commit", "-q", "-m", "work after the freeze")
    _assert_refused(repo, "is not the commit tagged")


@pytest.mark.parametrize("kind", ["modified", "untracked", "staged"])
def test_refused_when_working_tree_is_dirty(repo, kind):
    if kind == "modified":
        (repo / "PREREGISTRATION.md").write_text("# Pre-registration\n\n**Status:** FROZEN\n\nedited\n")
    else:
        (repo / "scratch.py").write_text("print('hi')\n")
        if kind == "staged":
            b.git(repo, "add", "scratch.py")
    _assert_refused(repo, "working tree is not clean")


def test_refused_while_preregistration_is_a_draft(tmp_path):
    root = b.init_repo(tmp_path / "repo", prereg_status="DRAFT")
    b.git(root, "tag", TAG)
    assert preregistration_status(root) == "DRAFT"
    _assert_refused(root, "status is 'DRAFT'")


def test_refused_without_a_purpose(repo):
    _assert_refused(repo, "purpose", purpose="  ")


def test_refused_outside_a_git_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    _assert_refused(plain, "not a usable git repository")


def test_refused_from_a_subdirectory(repo):
    (repo / "sub").mkdir()
    with pytest.raises(TestSplitLocked, match="top level"):
        load_test_split(ITEMS, repo_root=repo / "sub", confirm_tag=TAG, purpose="p")


def test_all_failed_conditions_are_reported_together(tmp_path):
    root = b.init_repo(tmp_path / "repo", prereg_status="DRAFT")
    (root / "scratch.py").write_text("x = 1\n")
    with pytest.raises(TestSplitLocked) as excinfo:
        load_test_split(ITEMS, repo_root=root, confirm_tag="prereg-freeze-missing", purpose="")
    assert len(excinfo.value.reasons) == 4


def test_log_is_append_only_and_chained(repo):
    _assert_refused(repo, "does not exist", tag="prereg-freeze-nope")
    _load(repo)
    _assert_refused(repo, "purpose", purpose="")
    log_path = repo / ACCESS_LOG_RELPATH
    lines = log_path.read_text().splitlines()
    assert len(lines) == 3 == verify_access_log(log_path)
    first = json.loads(lines[0])
    assert first["prev_sha256"] == "0" * 64 and first["log_version"] == 1
    assert [e["event"] for e in _log(repo)] == [EVENT_REFUSED, EVENT_LOADED, EVENT_REFUSED]
    # earlier lines are byte-identical after later appends
    _load(repo)
    assert log_path.read_text().splitlines()[:3] == lines


@pytest.mark.parametrize("tamper", ["delete_first", "edit_first", "reorder"])
def test_tampering_with_the_log_is_detected(repo, tamper):
    _assert_refused(repo, "does not exist", tag="prereg-freeze-nope")
    _load(repo)
    _load(repo)
    log_path = repo / ACCESS_LOG_RELPATH
    lines = log_path.read_text().splitlines()
    if tamper == "delete_first":
        lines = lines[1:]
    elif tamper == "edit_first":
        lines[0] = lines[0].replace("prereg-freeze-nope", "prereg-freeze-okay")
    else:
        lines[1], lines[2] = lines[2], lines[1]
    log_path.write_text("\n".join(lines) + "\n")
    with pytest.raises(AccessLogError, match="hash chain broken"):
        verify_access_log(log_path)


def test_missing_log_counts_as_empty(tmp_path):
    assert verify_access_log(tmp_path / "nope.jsonl") == 0


# -- read-only check (PR 12, pre-registration section 17 item 33) ------------------ #


def test_check_unlock_is_read_only_and_agrees_with_load_test_split(repo):
    log_path = repo / ACCESS_LOG_RELPATH
    granted = check_unlock(repo, TAG, "frozen test run")
    assert granted.would_unlock and granted.reasons == ()
    assert granted.head_commit == b.git(repo, "rev-parse", "HEAD")
    refused = check_unlock(repo, "prereg-freeze-nope", "")
    assert not refused.would_unlock
    assert any("does not exist" in r for r in refused.reasons) and any("purpose" in r for r in refused.reasons)
    assert not log_path.exists(), "check_unlock must never write the access log"
    # The official function reports exactly the same reasons and logs them.
    with pytest.raises(TestSplitLocked) as excinfo:
        load_test_split(ITEMS, repo_root=repo, confirm_tag="prereg-freeze-nope", purpose="")
    assert tuple(excinfo.value.reasons) == refused.reasons
    assert [e["event"] for e in _log(repo)] == [EVENT_REFUSED]


def test_check_unlock_reports_every_failed_condition_without_logging(tmp_path):
    root = b.init_repo(tmp_path / "repo", prereg_status="DRAFT")
    (root / "scratch.py").write_text("x = 1\n")
    result = check_unlock(root, "prereg-freeze-missing", "")
    assert len(result.reasons) == 4 and not result.would_unlock
    assert not (root / ACCESS_LOG_RELPATH).exists()
