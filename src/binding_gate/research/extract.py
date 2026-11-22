"""Turn the raw bytes of a fetched page into the text that the model and the gate see.

Why the standard library's ``html.parser`` and not a boilerplate remover
(trafilatura, readability, BeautifulSoup): the core package has no runtime
dependencies, and the extraction must be a fixed, inspectable rule rather than
a heuristic that changes between library versions. The extracted text is
stored in the snapshot store at fetch time (``text_sha256``), and the gate and
the scorers only ever read the stored text, so later changes of the parser
cannot change a recorded run. :data:`EXTRACTOR_VERSION` is recorded with every
snapshot; changing any rule below means a new version.

Rules (version 1)
-----------------
Decoding: the ``charset`` parameter of the ``Content-Type`` header; else, for
HTML, a ``<meta charset>`` or ``<meta http-equiv="content-type">`` declaration
in the first 4,096 bytes; else UTF-8. An unknown charset name falls back to
UTF-8. Undecodable bytes are replaced (U+FFFD), never dropped silently: the
count of replacement characters is returned.

HTML:

* The content of ``script``, ``style``, ``noscript``, ``template``, ``svg``,
  ``math``, ``iframe``, ``object``, ``canvas``, ``nav``, ``footer``, ``form``,
  ``button``, ``select`` and ``head`` is dropped, except the ``<title>`` text,
  which is kept separately.
* Block elements (paragraphs, headings, list items, table rows, divs, ...) and
  ``<br>`` end a line; table cells on one row are separated by `` | ``.
* Character references are decoded by ``html.parser``.
* Within a line, every run of whitespace becomes one space; lines are trimmed;
  empty lines are dropped. Lines are joined with ``\\n``.

Plain text: decoded, line endings normalised to ``\\n``, each line's whitespace
collapsed as above, empty lines dropped.

The document text shown to the model is ``<title>\\n<body text>`` when a title
exists (the title then acts as the page's first line, which is where the gate
looks for a document's subject), otherwise the body text alone.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from html.parser import HTMLParser

EXTRACTOR_VERSION = 1

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
TEXT_TYPES = frozenset({"text/plain"})
SUPPORTED_TYPES = HTML_TYPES | TEXT_TYPES

_SKIP = frozenset(
    {
        "script", "style", "noscript", "template", "svg", "math", "iframe", "object",
        "canvas", "nav", "footer", "form", "button", "select", "head",
    }
)
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})
_BLOCK = frozenset(
    {
        "address", "article", "aside", "blockquote", "body", "caption", "dd", "details", "dialog", "div",
        "dl", "dt", "fieldset", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "header",
        "hr", "html", "li", "main", "ol", "p", "pre", "section", "summary", "table", "tbody", "tfoot",
        "thead", "tr", "ul",
    }
)
_CELL = frozenset({"td", "th"})
_WS = re.compile(r"\s+")
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9._:-]+)""", re.IGNORECASE)


@dataclass(frozen=True)
class Extracted:
    """Result of :func:`extract`. ``text`` may be empty (the fetcher records that)."""

    text: str
    title: str | None
    charset: str
    replacement_chars: int


def content_type_of(header: str | None) -> tuple[str, str | None]:
    """``(media type in lower case, charset or None)`` from a Content-Type header value."""
    if not header:
        return "", None
    parts = [p.strip() for p in header.split(";")]
    media = parts[0].lower()
    charset = None
    for part in parts[1:]:
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip().strip("\"'") or None
    return media, charset


def _known(charset: str | None) -> str | None:
    if not charset:
        return None
    try:
        return codecs.lookup(charset).name
    except LookupError:
        return None


def choose_charset(raw: bytes, media_type: str, header_charset: str | None) -> str:
    charset = _known(header_charset)
    if charset is None and media_type in HTML_TYPES:
        match = _META_CHARSET.search(raw[:4096])
        if match:
            charset = _known(match.group(1).decode("ascii", errors="ignore"))
    return charset or "utf-8"


def _clean_lines(lines: list[str]) -> str:
    out = []
    for line in lines:
        line = _WS.sub(" ", line).strip()
        if line:
            out.append(line)
    return "\n".join(out)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.current: list[str] = []
        self.skip_depth = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.cells_in_row = 0

    def _break(self) -> None:
        if self.current:
            self.lines.append("".join(self.current))
            self.current = []
        self.cells_in_row = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
            return
        if tag in _SKIP:
            if tag not in _VOID:
                self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "br" or tag in _BLOCK:
            self._break()
        elif tag in _CELL:
            if self.cells_in_row:
                self.current.append(" | ")
            self.cells_in_row += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self.skip_depth and (tag == "br" or tag in _BLOCK):
            self._break()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
            return
        if tag in _SKIP:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if not self.skip_depth and tag in _BLOCK:
            self._break()

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        elif not self.skip_depth:
            self.current.append(data)

    def result(self) -> tuple[str, str | None]:
        self._break()
        title = _WS.sub(" ", "".join(self.title_parts)).strip() or None
        return _clean_lines(self.lines), title


def html_to_text(html: str) -> tuple[str, str | None]:
    """``(body text, title)`` of an HTML document under the version-1 rules."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.result()


def extract(raw: bytes, content_type_header: str | None) -> Extracted:
    """Decode and extract one page. Raises ``ValueError`` for an unsupported media type."""
    media, header_charset = content_type_of(content_type_header)
    if media not in SUPPORTED_TYPES:
        raise ValueError(f"unsupported media type {media or '(none)'}")
    charset = choose_charset(raw, media, header_charset)
    decoded = raw.decode(charset, errors="replace")
    replacements = decoded.count("�") - raw.decode(charset, errors="ignore").count("�")
    if media in HTML_TYPES:
        body, title = html_to_text(decoded)
    else:
        body, title = _clean_lines(decoded.replace("\r\n", "\n").replace("\r", "\n").split("\n")), None
    text = f"{title}\n{body}" if title and body else (body or title or "")
    return Extracted(text=text, title=title, charset=charset, replacement_chars=max(replacements, 0))
