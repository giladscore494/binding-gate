"""The pre-registration document and the code must agree, and the repo must be honest about its state."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from binding_gate import split
from binding_gate.testlock import (
    ACCESS_LOG_RELPATH,
    EVENT_LOADED,
    preregistration_status,
    read_access_log,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PREREG = (REPO_ROOT / "PREREGISTRATION.md").read_text(encoding="utf-8")

APACHE_2_0_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"


def _declared(name: str) -> str:
    match = re.search(rf"^{name}\s*=\s*(.+)$", PREREG, re.MULTILINE)
    assert match, f"PREREGISTRATION.md must declare {name}"
    return match.group(1).strip()


def test_split_parameters_in_document_match_the_code():
    assert _declared("SPLIT_SALT") == f'"{split.SPLIT_SALT}"'
    assert int(_declared("BUCKETS")) == split.BUCKETS
    assert int(_declared("TEST_BUCKETS")) == split.TEST_BUCKETS


def test_status_is_draft_or_frozen():
    assert preregistration_status(REPO_ROOT) in {"DRAFT", "FROZEN"}


def test_committed_access_log_is_intact_and_shows_no_test_use_before_the_freeze():
    entries = read_access_log(REPO_ROOT / ACCESS_LOG_RELPATH)  # raises if the chain is broken
    if preregistration_status(REPO_ROOT) != "FROZEN":
        assert not [e for e in entries if e.get("event") == EVENT_LOADED]


def test_every_proposed_number_is_marked_as_needing_approval():
    for line_number, line in enumerate(PREREG.splitlines(), start=1):
        if "**PROPOSED" in line and "requires owner approval" not in " ".join(
            PREREG.splitlines()[line_number - 1 : line_number + 2]
        ):
            raise AssertionError(f"PREREGISTRATION.md:{line_number}: PROPOSED without the approval note")


def test_license_is_the_canonical_apache_2_0_text():
    assert hashlib.sha256((REPO_ROOT / "LICENSE").read_bytes()).hexdigest() == APACHE_2_0_SHA256
