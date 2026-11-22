"""A tiny document model rendered to Markdown and to a single self-contained HTML file.

The report is built once as a list of blocks and rendered twice, so the Markdown
and the HTML version always say the same thing. Inline markup is limited to
```code``` spans and ``**bold**``; everything else is escaped.

The HTML renderer emits no script, no external stylesheet, font or image: CSS is
inline and figures are embedded as ``data:`` URIs. Void elements are written in
XHTML style (``<img ... />``) so that the output is also well-formed XML apart
from the doctype.
"""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


@dataclass
class Heading:
    level: int
    text: str
    anchor: str


@dataclass
class Paragraph:
    text: str


@dataclass
class BulletList:
    items: list[str]


@dataclass
class Table:
    headers: list[str]
    rows: list[list[str]]
    caption: str = ""


@dataclass
class Callout:
    kind: str  # "negative", "warning", "info", "positive"
    title: str
    text: str = ""
    items: list[str] = field(default_factory=list)


@dataclass
class Figure:
    path: Path
    markdown_ref: str
    caption: str


Block = Heading | Paragraph | BulletList | Table | Callout | Figure


def slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return value or "section"


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def _md_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(blocks: Sequence[Block]) -> str:
    out: list[str] = []
    for block in blocks:
        if isinstance(block, Heading):
            out += [f'<a id="{block.anchor}"></a>', "", f"{'#' * block.level} {block.text}", ""]
        elif isinstance(block, Paragraph):
            out += [block.text, ""]
        elif isinstance(block, BulletList):
            out += [f"- {item}" for item in block.items] + [""]
        elif isinstance(block, Table):
            if block.caption:
                out += [f"*{block.caption}*", ""]
            out.append("| " + " | ".join(_md_cell(h) for h in block.headers) + " |")
            out.append("|" + "---|" * len(block.headers))
            out += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in block.rows]
            out.append("")
        elif isinstance(block, Callout):
            label = {"negative": "NEGATIVE", "warning": "WARNING", "info": "NOTE", "positive": "RESULT"}[block.kind]
            out.append(f"> **{label}: {block.title}**")
            if block.text:
                out += [">", f"> {block.text}"]
            if block.items:
                out.append(">")
                out += [f"> - {item}" for item in block.items]
            out.append("")
        elif isinstance(block, Figure):
            out += [f"![{block.caption}]({block.markdown_ref})", "", f"*{block.caption}*", ""]
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

_CSS = """
:root { --ink:#1d1d1b; --muted:#5f5e5a; --line:#d9d8d2; --bg:#fbfbf9; --neg:#9b2c1f; --warn:#8a4b00; --info:#2a5fa8; --pos:#2f6b3a; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:1100px; margin:0 auto; padding:24px 24px 80px; }
h1 { font-size:26px; } h2 { font-size:21px; margin-top:38px; border-bottom:1px solid var(--line); padding-bottom:4px; }
h3 { font-size:17px; margin-top:26px; } h4 { font-size:15px; margin-top:20px; }
code { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:.92em; background:#efeee8; padding:0 3px; border-radius:3px; }
table { border-collapse:collapse; margin:10px 0 18px; font-size:13.5px; display:block; overflow-x:auto; max-width:100%; }
th, td { border:1px solid var(--line); padding:4px 8px; text-align:left; vertical-align:top; }
th { background:#f1f0ea; }
caption { text-align:left; color:var(--muted); font-style:italic; padding-bottom:4px; }
.callout { border-left:5px solid var(--info); background:#fff; padding:10px 14px; margin:14px 0; border-radius:4px; }
.callout.negative { border-color:var(--neg); } .callout.warning { border-color:var(--warn); }
.callout.positive { border-color:var(--pos); }
.callout .title { font-weight:700; }
figure { margin:16px 0; } figure img { max-width:100%; border:1px solid var(--line); }
figcaption { color:var(--muted); font-size:13px; }
"""


def _inline(text: str) -> str:
    """Escape, then turn `code` and **bold** into markup."""
    parts = re.split(r"(`[^`]+`)", str(text))
    out = []
    for part in parts:
        if len(part) >= 2 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
        else:
            escaped = html.escape(part)
            out.append(re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped))
    return "".join(out)


def _data_uri(path: Path) -> str:
    kind = {".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(
        path.suffix.lower(), "application/octet-stream"
    )
    return f"data:{kind};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def render_html(blocks: Sequence[Block], title: str) -> str:
    out = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8" />',
        '<meta name="viewport" content="width=device-width, initial-scale=1" />',
        f"<title>{html.escape(title)}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        "<main>",
    ]
    for block in blocks:
        if isinstance(block, Heading):
            out.append(f'<h{block.level} id="{html.escape(block.anchor)}">{_inline(block.text)}</h{block.level}>')
        elif isinstance(block, Paragraph):
            out.append(f"<p>{_inline(block.text)}</p>")
        elif isinstance(block, BulletList):
            out.append("<ul>" + "".join(f"<li>{_inline(item)}</li>" for item in block.items) + "</ul>")
        elif isinstance(block, Table):
            rows = ["<table>"]
            if block.caption:
                rows.append(f"<caption>{_inline(block.caption)}</caption>")
            rows.append("<thead><tr>" + "".join(f"<th>{_inline(h)}</th>" for h in block.headers) + "</tr></thead>")
            rows.append("<tbody>")
            rows += ["<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>" for row in block.rows]
            rows.append("</tbody></table>")
            out.append("".join(rows))
        elif isinstance(block, Callout):
            label = {"negative": "Negative", "warning": "Warning", "info": "Note", "positive": "Result"}[block.kind]
            inner = f'<div class="title">{label}: {_inline(block.title)}</div>'
            if block.text:
                inner += f"<p>{_inline(block.text)}</p>"
            if block.items:
                inner += "<ul>" + "".join(f"<li>{_inline(item)}</li>" for item in block.items) + "</ul>"
            out.append(f'<div class="callout {block.kind}">{inner}</div>')
        elif isinstance(block, Figure):
            out.append(
                f'<figure><img src="{_data_uri(block.path)}" alt="{html.escape(block.caption)}" />'
                f"<figcaption>{_inline(block.caption)}</figcaption></figure>"
            )
    out += ["</main>", "</body>", "</html>", ""]
    return "\n".join(out)
