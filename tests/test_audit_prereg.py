"""PREREGISTRATION.md section 13 and binding_gate.audit_tool.prereg must agree."""

from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path

from binding_gate.audit_tool import prereg

REPO_ROOT = Path(__file__).resolve().parents[1]
PREREG = (REPO_ROOT / "PREREGISTRATION.md").read_text(encoding="utf-8")
SECTION_13 = re.search(r"^## 13\. Human audit$(.*?)^## 14\. ", PREREG, re.MULTILINE | re.DOTALL).group(1)


def declared(name: str) -> str:
    match = re.search(rf"^{name}\s*=\s*(\S+)\s*$", SECTION_13, re.MULTILINE)
    assert match, f"section 13 must declare {name}"
    return match.group(1)


def test_section_13_parameters_match_the_code():
    assert int(declared("AUDIT_SEED")) == prereg.AUDIT_SEED
    assert int(declared("AUDIT_BLOCKED")) == prereg.AUDIT_BLOCKED
    assert int(declared("AUDIT_ACCEPTED")) == prereg.AUDIT_ACCEPTED
    assert Fraction(declared("AUDIT_DISAGREEMENT_SHARE")) == prereg.AUDIT_DISAGREEMENT_SHARE
    assert Fraction(declared("AUDIT_CI_LEVEL")) == prereg.AUDIT_CI_LEVEL


def test_section_13_names_the_rules_the_code_implements():
    for arm in prereg.AUDIT_ARMS:
        assert f"`{arm}`" in SECTION_13
    assert "*first* attempt" in SECTION_13 and prereg.AUDIT_ATTEMPT == 0
    assert "yes / no / cannot tell" in SECTION_13 and prereg.LABELS == ("yes", "no", "cannot_tell")
    assert "Clopper-Pearson" in SECTION_13
    flat = " ".join(SECTION_13.split())
    for words in ("wrong entity", "wrong or missing qualifier", "wrong or missing presupposition",
                  "other decomposition problem", "not assessed"):
        assert words in flat
    assert "PROPOSED — requires owner approval" in SECTION_13
    assert "owner decision" in SECTION_13  # who audits is not decided by code
