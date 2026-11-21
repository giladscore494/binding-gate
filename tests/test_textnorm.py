"""The pre-registered normalisation used for verbatim quote matching."""

from __future__ import annotations

from binding_gate.textnorm import contains_verbatim, normalize_text


def test_whitespace_case_and_typography_are_folded():
    assert normalize_text("  The Dose\n was “10–mg” ") == 'the dose was "10-mg"'
    assert normalize_text("it’s zero​width") == "it's zerowidth"
    assert normalize_text("ﬁne Ｆｕｌｌ") == "fine full"


def test_normalisation_is_idempotent():
    text = "  Mixed\tCASE — and «quotes»  "
    assert normalize_text(normalize_text(text)) == normalize_text(text)


def test_content_differences_are_not_forgiven():
    document = "The starting dose of Alphadrug is 10 mg once daily."
    assert contains_verbatim(document, "starting dose of  ALPHADRUG is 10 mg")
    assert not contains_verbatim(document, "starting dose of Alphadrug is 100 mg")
    assert not contains_verbatim(document, "starting dose of Betadrug is 10 mg")
    assert not contains_verbatim(document, "dose of Alphadrug, 10 mg")


def test_empty_quote_never_matches():
    assert not contains_verbatim("anything", "")
    assert not contains_verbatim("anything", " \n ")
