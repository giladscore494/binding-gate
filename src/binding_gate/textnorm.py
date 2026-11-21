"""The single, pre-registered text normalisation used for "verbatim" matching.

Both the deterministic provenance scorer (PR 2) and the binding checks (PR 4)
must compare quotes against documents through :func:`normalize_text` and nothing
else. The rule is stated in PREREGISTRATION.md section 6.1; changing it
after the freeze requires an entry in the amendment log.

Steps, in order:

1. Unicode NFKC normalisation (folds compatibility forms such as full-width
   characters, ligatures and non-breaking spaces).
2. Typographic quotes, apostrophes, dashes and the minus sign are mapped to their
   ASCII equivalents; soft hyphens and zero-width characters are removed.
3. ``str.casefold()`` (case-insensitive comparison).
4. Every run of whitespace becomes one space; leading and trailing whitespace is
   stripped.

Nothing else is changed: punctuation, digits, units and word order are preserved,
so a quote whose numbers or wording differ from the document does not match.
"""

from __future__ import annotations

import re
import unicodedata

_CHAR_MAP = {
    "‘": "'",  # left single quotation mark
    "’": "'",  # right single quotation mark
    "‚": "'",  # single low-9 quotation mark
    "‛": "'",  # single high-reversed-9 quotation mark
    "“": '"',  # left double quotation mark
    "”": '"',  # right double quotation mark
    "„": '"',  # double low-9 quotation mark
    "«": '"',  # left-pointing double angle quotation mark
    "»": '"',  # right-pointing double angle quotation mark
    "‐": "-",  # hyphen
    "‑": "-",  # non-breaking hyphen
    "‒": "-",  # figure dash
    "–": "-",  # en dash
    "—": "-",  # em dash
    "―": "-",  # horizontal bar
    "−": "-",  # minus sign
    "­": "",  # soft hyphen
    "​": "",  # zero-width space
    "‌": "",  # zero-width non-joiner
    "‍": "",  # zero-width joiner
    "﻿": "",  # byte-order mark / zero-width no-break space
}
_TRANSLATION = str.maketrans(_CHAR_MAP)
_WHITESPACE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Return the pre-registered normal form of ``text`` (see module docstring)."""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_TRANSLATION)
    text = text.casefold()
    return _WHITESPACE.sub(" ", text).strip()


def contains_verbatim(document_text: str, quote: str) -> bool:
    """True if ``quote`` occurs in ``document_text`` after normalising both.

    An empty (or whitespace-only) quote never matches.
    """
    needle = normalize_text(quote)
    return bool(needle) and needle in normalize_text(document_text)
