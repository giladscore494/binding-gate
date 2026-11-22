"""Deterministic detection of table-like lines for table-cell binding (PR 18, option ``table_binding``).

This module registers nothing. It is used only when a run sets
``table_binding = true`` (default ``false``: no check calls it, so the default
gate is unchanged). Pure functions of the document text and the candidate; no
model, no network, no ids, no metadata, no gold.

Why: in a table the binding of a value to its conditions is the row/column
structure, not distance. In a multi-year table every year sits within a few
tokens of every value of a row, so window proximity cannot tell a current-year
figure from a prior-year one; and a figure 40 rows below the statement title is
far from it although the title governs it.

Lines and positions
-------------------
The raw document text is split into lines at ``\\n`` (``\\r\\n`` and ``\\r`` first
become ``\\n``). Every line is normalised with ``textnorm.normalize_text``. The
non-empty normalised lines joined by single spaces must equal the normalised
document (the text the binding checks work on); if they do not (possible only
for exotic Unicode across a line break), the document is treated as having no
table at all. Character positions below are positions in that normalised text.

Three table formats
-------------------
1. **Flattened rows** (the statement format of ``datasets/sec_build.py``,
   ``row_line``): a normalised line that splits at ``" | "`` into at least two
   fields, whose first field (the *row label*) is non-empty and each of whose
   other fields has the form ``"<column header>: <cell>"`` (split at the first
   ``": "``; header and cell non-empty). Every such field is one value cell with
   its own column header. Flattened rows that share the same caption (below)
   form one table.
2. **Pipe tables**: a block of consecutive lines whose normalised text starts
   and ends with ``|`` and contains at least three ``|``; cells are the texts
   between the bars, stripped. Separator lines (every cell of the form
   ``---``, ``:---``, ``---:`` or ``:---:``, at least three dashes) belong to the
   block and are ignored. The first non-separator line is the header row; every
   other line is a data row. Every row must have the header's number of cells
   (otherwise the block is not a table). Cell 0 of a data row is its row label;
   cell ``j >= 1`` is a value cell whose column header is header cell ``j``.
3. **Tab tables**: a block of at least two consecutive raw lines that contain a
   tab character and split at tabs into the same number (at least two) of
   fields; the first line is the header row, as for pipe tables. Each field is
   located in the normalised line from left to right; if one cannot be, the
   block is not a table.

A blank line ends a pipe or tab block. A line that matches no format is a
*text line*.

Caption
-------
The caption of a table is the nearest text line above its first row that does
not end with ``":"`` (lines ending with a colon, such as "Operating
expenses:", are sub-headings inside a statement). Blank lines are skipped, and
for a flattened row so are the flattened rows above it; a line of any other
table stops the search (no caption). A caption line with more than
:data:`CAPTION_MAX_TOKENS` tokens is running prose: no caption. For flattened
rows the caption is looked up for every row; rows with the same caption line
are one table.

Which cells a quote is bound to
-------------------------------
The *quoted cells* are the value cells whose cell text (for flattened rows: the
part after ``"<header>: "``) overlaps the anchor occurrence of the quote. If
there are none, the quote is not in a table cell and table binding does not
apply. If there are several, they are narrowed by the answer's value: a number
is a run of digits with ``,`` or ``.`` between digit groups, compared by its
digits only (``"5,459"`` = ``"5459"``); the cells whose text contains a number
of the value are kept, unless none does (then all quoted cells are kept). The
remaining cells are the *bound cells*.

Limits (stated honestly)
------------------------
* Only these three line formats are recognised; HTML tables, fixed-width
  (space-aligned) tables, merged or multi-line header rows, and tables split
  over pages are not. A header that spans several header rows is not combined.
* A table without a header row (pipe or tab) takes its first row as header.
* Captions are a heuristic: the nearest short text line above, not a parsed
  table title.
* Narrowing by the value's digits can keep several cells that hold the same
  number; all of them must then satisfy the qualifier rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from binding_gate.textnorm import normalize_text

WORD = re.compile(r"\w+")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
CAPTION_MAX_TOKENS = 40
"""A text line with more tokens than this is prose, never a caption (fixed, part of the gate)."""

FORMATS = ("flattened", "pipe", "tab")


@dataclass(frozen=True)
class Cell:
    """One value cell: its texts (normalised) and the characters of its cell text in the normalised document."""

    table: int
    row: int
    row_label: str
    header: str
    text: str
    char_start: int
    char_end: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "row": self.row,
            "row_label": self.row_label,
            "column_header": self.header,
            "cell": self.text,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }


@dataclass(frozen=True)
class Table:
    index: int
    format: str
    caption: str | None
    headers: tuple[str, ...]
    row_labels: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "format": self.format,
            "caption": self.caption,
            "column_headers": list(self.headers),
            "rows": len(self.row_labels),
        }


@dataclass(frozen=True)
class Tables:
    tables: tuple[Table, ...]
    cells: tuple[Cell, ...]
    aligned: bool


@dataclass
class _Line:
    raw: str
    norm: str
    offset: int  # start of ``norm`` in the normalised document (-1 for an empty line)


def _lines(text: str, normalized: str) -> list[_Line] | None:
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[_Line] = []
    pieces: list[str] = []
    position = 0
    for raw in raw_lines:
        norm = normalize_text(raw)
        if norm:
            if pieces:
                position += 1
            out.append(_Line(raw, norm, position))
            pieces.append(norm)
            position += len(norm)
        else:
            out.append(_Line(raw, "", -1))
    if " ".join(pieces) != normalized:
        return None
    return out


def _flattened(norm: str) -> list[tuple[str, int, int, int, int]] | None:
    """``(header, header_start, cell_start, cell_end, -)`` per value field, plus the row label as field 0."""
    fields = norm.split(" | ")
    if len(fields) < 2 or not fields[0].strip():
        return None
    out = []
    position = 0
    for index, field in enumerate(fields):
        start = position
        position += len(field) + 3
        if index == 0:
            out.append((field.strip(), start, start, start + len(field), 0))
            continue
        cut = field.find(": ")
        if cut <= 0 or not field[:cut].strip() or not field[cut + 2 :].strip():
            return None
        out.append((field[:cut].strip(), start, start + cut + 2, start + len(field), 1))
    return out


def _bar_cells(norm: str) -> list[tuple[str, int, int]] | None:
    if not (norm.startswith("|") and norm.endswith("|") and norm.count("|") >= 3):
        return None
    cells = []
    position = 1
    for part in norm[1:-1].split("|"):
        stripped = part.strip()
        lead = len(part) - len(part.lstrip())
        cells.append((stripped, position + lead, position + lead + len(stripped)))
        position += len(part) + 1
    return cells


def _tab_cells(line: _Line) -> list[tuple[str, int, int]] | None:
    if "\t" not in line.raw or not line.norm:
        return None
    cells = []
    cursor = 0
    for field in line.raw.split("\t"):
        norm = normalize_text(field)
        if not norm:
            cells.append(("", cursor, cursor))
            continue
        found = line.norm.find(norm, cursor)
        if found < 0:
            return None
        cells.append((norm, found, found + len(norm)))
        cursor = found + len(norm)
    return cells if len(cells) >= 2 else None


def _caption_line(lines: Sequence[_Line], kinds: Sequence[str | None], first: int, skip: str | None) -> int:
    """Index of the caption line of a table starting at ``first`` (-1: none; module docstring)."""
    for i in range(first - 1, -1, -1):
        if not lines[i].norm or (skip is not None and kinds[i] == skip):
            continue
        if kinds[i] is not None:
            return -1  # another table stops the search
        if lines[i].norm.endswith(":"):
            continue
        return i
    return -1


def _caption(lines: Sequence[_Line], index: int) -> str | None:
    if index < 0:
        return None
    norm = lines[index].norm
    return norm if len(WORD.findall(norm)) <= CAPTION_MAX_TOKENS else None


def parse_tables(text: str, normalized: str) -> Tables:
    """Every table of the document (see the module docstring)."""
    lines = _lines(text, normalized)
    if lines is None:
        return Tables((), (), False)
    kinds: list[str | None] = [None] * len(lines)
    blocks: list[tuple[str, list[int], list[list[tuple[str, int, int]]]]] = []
    i = 0
    while i < len(lines):  # pipe and tab blocks
        bars = _bar_cells(lines[i].norm) if lines[i].norm else None
        tabs = None if bars is not None else _tab_cells(lines[i])
        if bars is None and tabs is None:
            i += 1
            continue
        fmt = "pipe" if bars is not None else "tab"
        members: list[int] = []
        cells: list[list[tuple[str, int, int]]] = []
        j = i
        while j < len(lines) and lines[j].norm:
            row = _bar_cells(lines[j].norm) if fmt == "pipe" else _tab_cells(lines[j])
            if row is None:
                break
            members.append(j)
            cells.append(row)
            j += 1
        content = [(m, c) for m, c in zip(members, cells)
                   if not (fmt == "pipe" and all(SEPARATOR_CELL.match(text_) for text_, _s, _e in c))]
        widths = {len(c) for _m, c in content}
        if len(content) >= 2 and len(widths) == 1 and widths.pop() >= 2:
            for m in members:
                kinds[m] = fmt
            blocks.append((fmt, [m for m, _c in content], [c for _m, c in content]))
        i = max(j, i + 1)
    flattened: dict[int, list[tuple[str, int, int, int, int]]] = {}
    for index, line in enumerate(lines):
        if kinds[index] is None and line.norm:
            row = _flattened(line.norm)
            if row is not None:
                kinds[index] = "flattened"
                flattened[index] = row

    tables: list[Table] = []
    cells_out: list[Cell] = []
    groups: dict[int, list[int]] = {}  # caption line -> flattened rows, in document order
    for index in sorted(flattened):
        groups.setdefault(_caption_line(lines, kinds, index, "flattened"), []).append(index)
    for caption_line, members in sorted(groups.items(), key=lambda entry: entry[1][0]):
        table_index = len(tables)
        headers: list[str] = []
        labels: list[str] = []
        for index in members:
            row, offset, norm = flattened[index], lines[index].offset, lines[index].norm
            label = row[0][0]
            labels.append(label)
            for header, _hs, cell_start, cell_end, _flag in row[1:]:
                if header not in headers:
                    headers.append(header)
                cell_text = norm[cell_start:cell_end].strip()
                cells_out.append(Cell(table_index, index, label, header, cell_text, offset + cell_start, offset + cell_end))
        tables.append(Table(table_index, "flattened", _caption(lines, caption_line), tuple(headers), tuple(labels)))
    for fmt, members, rows in blocks:
        table_index = len(tables)
        header_row = rows[0]
        labels = []
        for m, row in zip(members[1:], rows[1:]):
            offset = lines[m].offset
            label = row[0][0]
            labels.append(label)
            for j, (text_, start, end) in enumerate(row[1:], start=1):
                if text_:
                    cells_out.append(Cell(table_index, m, label, header_row[j][0], text_, offset + start, offset + end))
        caption_line = _caption_line(lines, kinds, members[0], None)
        tables.append(Table(table_index, fmt, _caption(lines, caption_line),
                            tuple(text_ for text_, _s, _e in header_row[1:]), tuple(labels)))
    return Tables(tuple(tables), tuple(cells_out), True)


def numbers(text: str) -> frozenset[str]:
    """The numbers of a text, by their digits only (``"$5,459 million"`` -> ``{"5459"}``)."""
    return frozenset(re.sub(r"\D", "", m.group(0)) for m in NUMBER.finditer(normalize_text(text)))


def bound_cells(tables: Tables, char_start: int, char_end: int, value: str | None) -> tuple[list[Cell], list[Cell]]:
    """``(quoted cells, bound cells)`` for the anchor occurrence ``[char_start, char_end)`` (module docstring)."""
    quoted = [c for c in tables.cells if c.char_start < char_end and c.char_end > char_start]
    if len(quoted) <= 1:
        return quoted, quoted
    wanted = numbers(value or "")
    matching = [c for c in quoted if wanted & numbers(c.text)]
    return quoted, matching or quoted


def mentions_form(text: str, form_tokens: Sequence[str]) -> bool:
    """True when ``form_tokens`` occurs as a contiguous token sequence of the normalised ``text``."""
    tokens = tuple(WORD.findall(normalize_text(text)))
    k = len(form_tokens)
    return k > 0 and any(tokens[i : i + k] == tuple(form_tokens) for i in range(len(tokens) - k + 1))
