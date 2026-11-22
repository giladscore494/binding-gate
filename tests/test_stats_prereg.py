"""PREREGISTRATION.md sections 6.4, 8 and 11 and binding_gate.stats.prereg must agree."""

from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path

from binding_gate.stats import prereg

REPO_ROOT = Path(__file__).resolve().parents[1]
PREREG = (REPO_ROOT / "PREREGISTRATION.md").read_text(encoding="utf-8")


def _section(number: int) -> str:
    start = re.search(rf"^## {number}\. ", PREREG, re.MULTILINE)
    end = re.search(rf"^## {number + 1}\. ", PREREG, re.MULTILINE)
    assert start and end
    return PREREG[start.start() : end.start()]


def _declared(name: str, text: str) -> Fraction:
    matches = re.findall(rf"^{name}\s*=\s*(\S+)\s*$", text, re.MULTILINE)
    assert len(matches) == 1, f"{name} must be declared exactly once in the section"
    return Fraction(matches[0])


def test_statistics_parameters_in_section_8_match_the_code():
    section = _section(8)
    assert _declared("CI_LEVEL", section) == prereg.CI_LEVEL
    assert _declared("BOOTSTRAP_RESAMPLES", section) == prereg.BOOTSTRAP_RESAMPLES
    assert _declared("BOOTSTRAP_SEED", section) == prereg.BOOTSTRAP_SEED
    assert _declared("REPEATS", section) == prereg.REPEATS
    assert _declared("VALIDITY_MAX_FAILED_RATE", section) == prereg.VALIDITY_MAX_FAILED_RATE


def test_thresholds_in_section_11_match_the_code():
    section = _section(11)
    assert _declared("H1_MIN_RELATIVE_REDUCTION", section) == prereg.H1_MIN_RELATIVE_REDUCTION
    assert _declared("H2_MAX_CORRECT_BLOCKED", section) == prereg.H2_MAX_CORRECT_BLOCKED
    assert _declared("H4_MIN_NET_DIFFERENCE", section) == prereg.H4_MIN_NET_DIFFERENCE


def test_prose_percentages_agree_with_the_constants():
    section_11 = _section(11)
    rows = {line.split("|")[1].strip(): line for line in section_11.splitlines() if line.startswith("| H")}
    level = f"{float(prereg.CI_LEVEL):.0%}"
    assert f"at least {float(prereg.H1_MIN_RELATIVE_REDUCTION):.0%} lower" in rows["H1"]
    assert f"{level} interval" in rows["H1"] and f"{level} interval" in rows["H3"]
    assert f"at most {float(prereg.H2_MAX_CORRECT_BLOCKED):.0%}" in rows["H2"]
    assert "point estimate ≥ 0" in rows["H4"]
    section_6 = _section(6)
    assert f"exceeds {float(prereg.VALIDITY_MAX_FAILED_RATE):.0%}" in section_6
    assert f"{prereg.BOOTSTRAP_RESAMPLES:,}".replace(",", "") in _section(8).replace(",", "")


def test_arm_names_are_the_reserved_names():
    architecture = (REPO_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    reserved = re.search(r"^\| Arms \| (.+) \|$", architecture, re.MULTILINE).group(1)
    for arm in (prereg.REFERENCE_ARM, prereg.GATED_ARM, prereg.FAITHFULNESS_ARM, prereg.RESEARCH_ARM):
        assert f"`{arm}`" in reserved


def test_values_are_still_marked_as_proposed():
    for number in (8, 11):
        assert "PROPOSED — requires owner approval before freeze" in _section(number)
    assert "PROPOSED" in prereg.as_dict()["status"]
