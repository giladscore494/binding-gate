"""robots.txt (RFC 9309), HTML/text extraction, and the first and re-search queries."""

from __future__ import annotations

import json

import pytest

from binding_gate.gates.binding import research_request
from binding_gate.research import extract as ex
from binding_gate.research import robots as rb
from binding_gate.research.query import first_query, research_query
from binding_gate.research.fetch import PRODUCT_TOKEN, USER_AGENT
from gate_fakes import constraints

# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #

ROBOTS = """
# comment
User-agent: *
Disallow: /private
Allow: /private/public-page
Disallow: /*.pdf$

User-agent: binding-gate-research
User-agent: other-bot
Disallow: /no-research/
Allow: /no-research/except
Disallow:
Sitemap: https://site.test/sitemap.xml
"""


def test_our_own_group_replaces_the_star_group():
    rules = rb.parse_robots(ROBOTS)
    assert rules.allowed("https://site.test/private/x", PRODUCT_TOKEN)  # star rules do not apply to us
    assert not rules.allowed("https://site.test/no-research/page", PRODUCT_TOKEN)
    assert rules.allowed("https://site.test/no-research/except-this", PRODUCT_TOKEN)  # longer allow wins
    assert rules.allowed("https://site.test/anything", "Binding-Gate-Research")  # case-insensitive token


def test_star_group_longest_match_wildcards_and_end_anchor():
    rules = rb.parse_robots(ROBOTS)
    assert not rules.allowed("https://site.test/private/secret", "someone")
    assert rules.allowed("https://site.test/private/public-page", "someone")
    assert not rules.allowed("https://site.test/docs/file.pdf", "someone")
    assert rules.allowed("https://site.test/docs/file.pdf?x=1", "someone")  # $ anchors the end
    assert rules.allowed("https://site.test/robots.txt", "someone")


def test_tie_goes_to_allow_and_percent_encoding_of_unreserved_characters_is_ignored():
    rules = rb.parse_robots("User-agent: *\nDisallow: /a\nAllow: /a\nDisallow: /~user\n")
    assert rules.allowed("https://s.test/a", "x")
    assert not rules.allowed("https://s.test/%7Euser/page", "x")


def test_no_matching_group_allows_everything_and_rules_outside_groups_are_ignored():
    assert rb.parse_robots("Disallow: /\nUser-agent: other\nDisallow: /\n").allowed("https://s.test/", PRODUCT_TOKEN)
    assert rb.ALLOW_ALL.allowed("https://s.test/x", PRODUCT_TOKEN)
    assert not rb.DISALLOW_ALL.allowed("https://s.test/x", PRODUCT_TOKEN)


def test_only_the_first_500_kib_are_parsed():
    content = b"User-agent: *\n" + b"#" * rb.MAX_ROBOTS_BYTES + b"\nDisallow: /\n"
    assert rb.parse_robots(content).allowed("https://s.test/x", "any")


def test_user_agent_names_the_project_and_starts_with_the_product_token():
    assert USER_AGENT.startswith(PRODUCT_TOKEN + "/") and "github.com/giladscore494/binding-gate" in USER_AGENT
    assert rb.robots_url("https://Site.test:8443/a/b?c") == "https://Site.test:8443/robots.txt"


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #


def test_html_extraction_keeps_text_drops_scripts_and_navigation_and_puts_the_title_first():
    html = (
        "<html><head><title> The  Title </title><style>p{}</style></head><body><nav>Menu</nav>"
        "<h1>Head</h1><p>First &amp; second<br>line two</p><script>alert(1)</script>"
        "<table><tr><td>Born</td><td>1950</td></tr><tr><td>Died</td><td>2020</td></tr></table>"
        "<footer>Footer</footer></body></html>"
    )
    out = ex.extract(html.encode(), "text/html; charset=utf-8")
    assert out.title == "The Title"
    assert out.text == "The Title\nHead\nFirst & second\nline two\nBorn | 1950\nDied | 2020"
    assert out.charset == "utf-8" and out.replacement_chars == 0


def test_charset_from_header_then_meta_then_utf8_and_replacements_are_counted():
    latin = "<p>caf\xe9</p>".encode("latin-1")
    assert ex.extract(latin, "text/html; charset=ISO-8859-1").text == "café"
    meta = b'<meta charset="iso-8859-1"><p>caf\xe9</p>'
    assert ex.extract(meta, "text/html").text == "café"
    bad = ex.extract(b"<p>caf\xe9</p>", "text/html")
    assert bad.charset == "utf-8" and bad.replacement_chars == 1


def test_plain_text_and_unsupported_types():
    assert ex.extract(b"a  b\r\n\r\n c ", "text/plain").text == "a b\nc"
    with pytest.raises(ValueError, match="unsupported"):
        ex.extract(b"%PDF", "application/pdf")
    assert ex.content_type_of('text/HTML; Charset="UTF-8"') == ("text/html", "UTF-8")


def test_extraction_is_deterministic():
    html = "<div><p>Same</p><p>input</p></div>".encode()
    assert ex.extract(html, "text/html") == ex.extract(html, "text/html")


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #


def test_first_query_is_the_question_with_whitespace_collapsed():
    assert first_query("  Who   won\nthe cup? ") == "Who won the cup?"
    with pytest.raises(ValueError):
        first_query("   ")


def test_research_query_template_from_a_real_research_request():
    c = constraints(entity="Example Games", aliases=("EG",), qualifiers=(("2031", ()), ("Europe", ())))
    request = research_request(c, ["entity_in_window"])
    assert research_query(request) == '"Example Games" 2031 Europe evidence for use'
    assert research_query(request) == research_query(request)  # deterministic


def test_research_query_removes_quotes_duplicates_and_empty_parts():
    request = json.dumps({"entity": 'The "Big" Fair', "aliases": [], "attribute": "2031",
                          "qualifiers": ["2031", "  ", "Paris", "paris"], "failed_checks": [], "query": "x"})
    assert research_query(request) == '"The Big Fair" 2031 Paris'
    with pytest.raises(ValueError):
        research_query(json.dumps({"entity": " "}))
    with pytest.raises(ValueError):
        research_query("not json")
