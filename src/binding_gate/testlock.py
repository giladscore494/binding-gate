"""Test-set lock: the only way to obtain test-split items.

:func:`load_test_split` returns test items only when ALL of these hold:

1. the caller names a git tag (``confirm_tag``) that starts with ``prereg-freeze-``;
2. ``repo_root`` is the top level of a git repository and that tag exists in it;
3. ``HEAD`` is exactly the commit the tag points to;
4. the working tree is clean (no modified, staged or untracked files), ignoring
   only the access log itself;
5. ``PREREGISTRATION.md`` in that tree has the status line ``**Status:** FROZEN``;
6. a non-empty ``purpose`` is given.

Every call, granted or refused, appends one JSON line to
``audit/test_access_log.jsonl``, a file that is committed to the repository.
Each line stores the SHA-256 of the previous line, so removing or editing an
earlier entry breaks the chain and is detected by :func:`verify_access_log`
(run by the test suite in CI).

Limitations (stated honestly)
-----------------------------
This is an audit mechanism, not access control. Anyone who can run Python can
call ``binding_gate.split.assign_split`` directly, read the dataset files by
hand, edit this module, or rewrite the whole log including its hash chain before
committing. What the lock does guarantee: the documented, supported path cannot
touch test data by accident, cannot touch it before the pre-registration is
frozen and tagged, and leaves a visible record in git history when it does.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from binding_gate import gitinfo
from binding_gate.canonical import canonical_json, sha256_text, utc_now_iso
from binding_gate.contracts import Item, Split
from binding_gate.split import item_split

ACCESS_LOG_RELPATH = "audit/test_access_log.jsonl"
FREEZE_TAG_PREFIX = "prereg-freeze-"
PREREGISTRATION_FILE = "PREREGISTRATION.md"
ACCESS_LOG_VERSION = 1

EVENT_REFUSED = "test_unlock_refused"
EVENT_LOADED = "test_split_loaded"

_GENESIS = "0" * 64
_STATUS_LINE = re.compile(r"^\*\*Status:\*\*\s*(\w+)", re.MULTILINE)


class TestSplitLocked(PermissionError):
    """Raised when the conditions for using the test split do not hold."""

    __test__ = False  # not a pytest test class, despite the name

    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__("test split is locked: " + "; ".join(self.reasons))


class AccessLogError(RuntimeError):
    """Raised when the access log is malformed or its hash chain is broken."""


def preregistration_status(repo_root: Path) -> str | None:
    """The word after ``**Status:**`` in PREREGISTRATION.md, or ``None`` if absent."""
    try:
        text = (repo_root / PREREGISTRATION_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    match = _STATUS_LINE.search(text)
    return match.group(1) if match else None


def _unlock_refusals(repo_root: Path, confirm_tag: str, purpose: str) -> tuple[list[str], str | None]:
    """Return (reasons the unlock must be refused, HEAD commit if known)."""
    reasons: list[str] = []
    if not isinstance(purpose, str) or not purpose.strip():
        reasons.append("a non-empty purpose is required")
    if not isinstance(confirm_tag, str) or not confirm_tag.startswith(FREEZE_TAG_PREFIX):
        reasons.append(f"confirm_tag must name a tag starting with {FREEZE_TAG_PREFIX!r}")
        confirm_tag = ""

    try:
        head = gitinfo.head_commit(repo_root)
        top_level = gitinfo.top_level(repo_root)
    except gitinfo.GitError as exc:
        reasons.append(f"not a usable git repository: {exc}")
        return reasons, None
    if top_level.resolve() != repo_root.resolve():
        reasons.append(f"repo_root must be the repository top level ({top_level})")

    if confirm_tag:
        try:
            tagged = gitinfo.tag_commit(repo_root, confirm_tag)
        except gitinfo.GitError:
            reasons.append(f"tag {confirm_tag!r} does not exist")
        else:
            if tagged != head:
                reasons.append(f"HEAD ({head[:12]}) is not the commit tagged {confirm_tag!r} ({tagged[:12]})")

    dirty = gitinfo.dirty_paths(repo_root, ignore=[ACCESS_LOG_RELPATH])
    if dirty:
        shown = ", ".join(dirty[:5]) + (", ..." if len(dirty) > 5 else "")
        reasons.append(f"working tree is not clean: {shown}")

    status = preregistration_status(repo_root)
    if status != "FROZEN":
        reasons.append(f"{PREREGISTRATION_FILE} status is {status!r}, not 'FROZEN'")
    return reasons, head


def _last_line_hash(log_path: Path) -> str:
    if not log_path.exists():
        return _GENESIS
    lines = [line for line in log_path.read_text(encoding="utf-8").split("\n") if line]
    return sha256_text(lines[-1]) if lines else _GENESIS


def _append_entry(repo_root: Path, entry: dict[str, Any]) -> None:
    log_path = repo_root / ACCESS_LOG_RELPATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"log_version": ACCESS_LOG_VERSION, **entry, "prev_sha256": _last_line_hash(log_path)}
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(entry) + "\n")


def load_test_split(
    items: Iterable[Item],
    *,
    repo_root: str | Path,
    confirm_tag: str,
    purpose: str,
) -> list[Item]:
    """Return the test-split items of ``items`` if the lock conditions hold.

    Raises :class:`TestSplitLocked` otherwise. In both cases one entry is
    appended to the access log before this function returns or raises; if the
    log cannot be written, no data is returned.
    """
    repo_root = Path(repo_root)
    reasons, head = _unlock_refusals(repo_root, confirm_tag, purpose)
    base = {
        "timestamp": utc_now_iso(),
        "confirm_tag": confirm_tag if isinstance(confirm_tag, str) else repr(confirm_tag),
        "purpose": purpose if isinstance(purpose, str) else repr(purpose),
        "head_commit": head,
    }
    if reasons:
        _append_entry(repo_root, {**base, "event": EVENT_REFUSED, "refusal_reasons": reasons})
        raise TestSplitLocked(reasons)

    selected = [item for item in items if item_split(item) is Split.TEST]
    _append_entry(
        repo_root,
        {
            **base,
            "event": EVENT_LOADED,
            "datasets": sorted({item.dataset for item in selected}),
            "n_items": len(selected),
            "n_groups": len({(item.dataset, item.group_id) for item in selected}),
        },
    )
    return selected


def read_access_log(log_path: str | Path) -> list[dict[str, Any]]:
    """Parse and verify the access log; return its entries (empty if no file)."""
    log_path = Path(log_path)
    if not log_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    previous = _GENESIS
    for number, line in enumerate(log_path.read_text(encoding="utf-8").split("\n"), start=1):
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AccessLogError(f"{log_path}:{number}: invalid JSON: {exc}") from exc
        if not isinstance(entry, dict) or entry.get("prev_sha256") != previous:
            raise AccessLogError(
                f"{log_path}:{number}: hash chain broken (an earlier entry was edited, "
                "removed or reordered)"
            )
        entries.append(entry)
        previous = sha256_text(line)
    return entries


def verify_access_log(log_path: str | Path) -> int:
    """Verify the hash chain of the access log and return the number of entries."""
    return len(read_access_log(log_path))
