"""Reading the parts of PREREGISTRATION.md that the public report reproduces.

The report quotes the document instead of restating it, so the two cannot
drift: the status line and version, the success rules of section 11, the
reporting commitments of section 15 (identified as ``R1``, ``R2``, ...), the
threats to validity of section 16 and the amendment log of section 18.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class PreregParseError(ValueError):
    """Raised when PREREGISTRATION.md lacks a part the report must reproduce."""


@dataclass(frozen=True)
class Commitment:
    ident: str
    text: str


def _strip_md(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def status(text: str) -> str:
    match = re.search(r"^\*\*Status:\*\*\s*(\S+)", text, re.MULTILINE)
    if not match:
        raise PreregParseError("no '**Status:**' line")
    return match.group(1)


def version(text: str) -> str:
    match = re.search(r"^\*\*Document version:\*\*\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else "unknown"


def section(text: str, number: int) -> str:
    match = re.search(rf"^## {number}\. .*?$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        raise PreregParseError(f"no section {number}")
    return match.group(1)


def _bullets(body: str) -> list[str]:
    items: list[str] = []
    for line in body.splitlines():
        if line.startswith("- "):
            items.append(line[2:])
        elif line.startswith("  ") and items and line.strip():
            items[-1] += " " + line.strip()
        elif not line.strip() and items:
            items.append("")
    return [_strip_md(i) for i in items if i.strip()]


def threats(text: str) -> list[tuple[str, str]]:
    """Section 16 as ``(title, text)`` pairs; the title is the bold lead-in."""
    out = []
    for item in _bullets(section(text, 16)):
        match = re.match(r"\*\*(.+?)\*\*\s*(.*)", item)
        out.append((match.group(1).rstrip("."), match.group(2)) if match else ("", item))
    if not out:
        raise PreregParseError("section 16 lists no threats")
    return out


def _table_rows(body: str) -> list[list[str]]:
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|") or re.match(r"^\|[\s\-|]+\|$", line):
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def amendments(text: str) -> list[dict[str, str]]:
    """Rows of the section 18 amendment table (an empty list if there are none yet)."""
    rows = _table_rows(section(text, 18))
    if not rows:
        raise PreregParseError("section 18 has no amendment table")
    header = [h.lower() for h in rows[0]]
    out = []
    for row in rows[1:]:
        if not row or row[0].startswith("(none"):
            continue
        out.append(dict(zip(header, row)))
    return out


def hypothesis_rules(text: str) -> dict[str, str]:
    """The 'Supported if' column of the section 11 table, per hypothesis."""
    rules = {}
    for row in _table_rows(section(text, 11)):
        if len(row) >= 2 and re.fullmatch(r"H\d", row[0]):
            rules[row[0]] = row[1]
    if not rules:
        raise PreregParseError("section 11 has no hypothesis table")
    return rules


def commitments(text: str) -> list[Commitment]:
    """Section 15 commitments written as ``- **R<n>** ...``."""
    out = []
    for item in _bullets(section(text, 15)):
        match = re.match(r"\*\*(R\d+)\.?\*\*\s*(.*)", item)
        if match:
            out.append(Commitment(match.group(1), match.group(2)))
    if not out:
        raise PreregParseError("section 15 lists no commitments of the form '- **R1** ...'")
    return out
