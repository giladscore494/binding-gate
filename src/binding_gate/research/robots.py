"""robots.txt parsing and matching following RFC 9309 (Robots Exclusion Protocol).

Why not ``urllib.robotparser``: it applies the first matching rule in file
order, whereas RFC 9309 (section 2.2.2) requires the *most specific* (longest)
match, with ``allow`` winning a tie, and defines the ``*`` and ``$`` special
characters. This module implements exactly that, in about a hundred lines that
a reviewer can check against the RFC, and is tested.

Rules implemented
-----------------
* Lines are ``<field>:<value>``; ``#`` starts a comment; field names are
  case-insensitive; unknown fields (``sitemap``, ``crawl-delay``, ...) are
  ignored.
* A *group* is one or more consecutive ``user-agent`` lines followed by
  ``allow`` / ``disallow`` rules. A ``user-agent`` line after a rule starts a
  new group.
* Group selection (2.2.1): the groups whose user-agent value equals our product
  token (case-insensitive) are combined; if there is none, the ``*`` groups are
  combined; if there is none either, everything is allowed.
* Matching (2.2.2): a rule's path pattern is compared with the URL's path and
  query (``/`` when the path is empty). ``*`` matches any sequence of
  characters; a trailing ``$`` anchors the end. The longest matching pattern
  (in characters) wins; on equal length ``allow`` wins. An empty ``disallow``
  value is no rule. No match means allowed. ``/robots.txt`` is always allowed.
* Percent-encoding: both pattern and path are compared after decoding
  percent-escapes of unreserved characters only (letters, digits, ``-._~``),
  so ``/%7Euser`` and ``/~user`` are the same (RFC 9309 2.2.2 and RFC 3986 2.3).
* Size (2.5): only the first :data:`MAX_ROBOTS_BYTES` bytes are parsed.

How fetch outcomes map to a decision (2.3.1) is decided by the fetcher
(``binding_gate.research.fetch``): 2xx -> these rules; 4xx -> "unavailable",
everything allowed; 5xx or network failure -> "unreachable", everything
disallowed.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

MAX_ROBOTS_BYTES = 500 * 1024
"""RFC 9309 section 2.5: crawlers must parse at least 500 KiB; we parse exactly that much."""

_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")


def _decode_unreserved(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else "%" + match.group(1).upper()

    return _ESCAPE.sub(replace, text)


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str

    def matches(self, path: str) -> bool:
        anchored = self.pattern.endswith("$")
        body = self.pattern[:-1] if anchored else self.pattern
        regex = "".join(".*" if part == "*" else re.escape(part) for part in re.split(r"(\*)", body))
        return re.match(regex + ("$" if anchored else ""), path, flags=re.DOTALL) is not None


@dataclass(frozen=True)
class Group:
    agents: tuple[str, ...]
    rules: tuple[Rule, ...]


@dataclass(frozen=True)
class RobotsRules:
    """Parsed robots.txt. :meth:`allowed` answers for one URL and one product token."""

    groups: tuple[Group, ...]

    def rules_for(self, product_token: str) -> tuple[Rule, ...]:
        token = product_token.casefold()
        own = [g for g in self.groups if token in g.agents]
        chosen = own or [g for g in self.groups if "*" in g.agents]
        return tuple(rule for group in chosen for rule in group.rules)

    def allowed(self, url: str, product_token: str) -> bool:
        path = request_path(url)
        if path == "/robots.txt":
            return True
        best: Rule | None = None
        for rule in self.rules_for(product_token):
            if not rule.matches(path):
                continue
            if best is None or len(rule.pattern) > len(best.pattern) or (
                len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow
            ):
                best = rule
        return best is None or best.allow


ALLOW_ALL = RobotsRules(groups=())
DISALLOW_ALL = RobotsRules(groups=(Group(agents=("*",), rules=(Rule(False, "/"),)),))


def request_path(url: str) -> str:
    """Path and query of ``url`` as compared with robots.txt patterns."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return _decode_unreserved(path)


def parse_robots(content: bytes | str) -> RobotsRules:
    """Parse robots.txt content (bytes are decoded as UTF-8, invalid bytes replaced)."""
    if isinstance(content, bytes):
        content = content[:MAX_ROBOTS_BYTES].decode("utf-8", errors="replace")
    groups: list[Group] = []
    agents: list[str] = []
    rules: list[Rule] = []
    in_rules = False

    def close() -> None:
        if agents:
            groups.append(Group(tuple(agents), tuple(rules)))

    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        field = field.casefold()
        if field == "user-agent":
            if in_rules:
                close()
                agents, rules, in_rules = [], [], False
            if value:
                agents.append(value.casefold())
        elif field in ("allow", "disallow"):
            if not agents:
                continue  # a rule outside any group is ignored (RFC 9309 2.2)
            in_rules = True
            if value:  # an empty value ("Disallow:") is no rule
                rules.append(Rule(field == "allow", _decode_unreserved(value)))
    close()
    return RobotsRules(groups=tuple(groups))


def robots_url(url: str) -> str:
    """``scheme://host[:port]/robots.txt`` for ``url``."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


def origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"
