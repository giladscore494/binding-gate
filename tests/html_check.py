"""A strict well-formedness check for the generated HTML files (standard library only)."""

from __future__ import annotations

import re
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _Checker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, tuple[int, int]]] = []
        self.errors: list[str] = []
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.ids: list[str] = []
        self.doctype = False

    def handle_decl(self, decl: str) -> None:
        self.doctype = self.doctype or decl.lower() == "doctype html"

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if attributes.get("id"):
            self.ids.append(attributes["id"])
        if tag not in VOID:
            self.stack.append((tag, self.getpos()))

    def handle_startendtag(self, tag, attrs):
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if attributes.get("id"):
            self.ids.append(attributes["id"])
        if tag not in VOID:
            self.errors.append(f"self-closing non-void element <{tag}/> at {self.getpos()}")

    def handle_endtag(self, tag):
        if tag in VOID:
            self.errors.append(f"end tag for void element </{tag}> at {self.getpos()}")
            return
        if not self.stack:
            self.errors.append(f"unexpected </{tag}> at {self.getpos()}")
            return
        open_tag, where = self.stack.pop()
        if open_tag != tag:
            self.errors.append(f"</{tag}> at {self.getpos()} closes <{open_tag}> opened at {where}")


def check(document: str) -> _Checker:
    """Parse ``document``; raise AssertionError with every problem found."""
    checker = _Checker()
    checker.feed(document)
    checker.close()
    problems = list(checker.errors)
    if checker.stack:
        problems.append(f"unclosed elements: {[t for t, _ in checker.stack]}")
    if not checker.doctype:
        problems.append("no <!DOCTYPE html>")
    duplicates = {i for i in checker.ids if checker.ids.count(i) > 1}
    if duplicates:
        problems.append(f"duplicate ids: {sorted(duplicates)[:5]}")
    if problems:
        raise AssertionError("HTML is not well-formed:\n" + "\n".join(problems[:20]))
    return checker


def external_references(document: str) -> list[str]:
    """Every src/href that points outside the file (anything that is not a data: URI or a #fragment)."""
    refs = re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", document)
    return [r for r in refs if not (r.startswith("data:") or r.startswith("#"))]
