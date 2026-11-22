"""Shared, deterministic text machinery of the binding checks (gate functions C and D).

This module registers nothing; the check modules of this package use it so that
every check locates the quote, measures distances and matches names in exactly
the same way. Everything here is a pure function of its arguments: no model, no
network, no randomness, no clock, no gold metadata. The definitions below are
the ones stated in PREREGISTRATION.md section 5.1.

Normalisation and tokens
------------------------
* Every text (document, quote, entity name, alias, qualifier, key term) is first
  normalised with :func:`binding_gate.textnorm.normalize_text`, the
  pre-registered normalisation of section 6.1 (NFKC, typographic punctuation to
  ASCII, case folding, whitespace collapsed). Diacritics are NOT removed.
* A **token** is a maximal run of Unicode word characters (regular expression
  ``\\w+``) in the normalised text. Positions are reported both as token indices
  and as character offsets into the normalised text.

Matching a name (a "mention")
-----------------------------
A form (entity name or alias, qualifier text or alias, key term, competitor
term) is *mentioned* in a document where its token sequence occurs as a
contiguous token sequence of the document. So matching is by whole words,
punctuation between words is ignored ("5-fluorouracil" matches
"5 fluorouracil"; "TSC" does not match inside "TSCx"), and a possessive
"drug's" still contains the token "drug". A form without any word character
cannot be matched and is reported as ``unmatchable``.

Locating the quote
------------------
The quote is looked up only in the document whose id equals the candidate's
``source_id``. An **occurrence** is every character offset at which the
normalised quote is a substring of the normalised document (overlapping
occurrences included). Its token span is the set of tokens that overlap the
occurrence's characters.

Distance and window
-------------------
The **distance** between two token spans is the number of tokens strictly
between them; it is 0 when they touch or overlap. A mention is **within the
window** of a quote occurrence when its distance to that occurrence is at most
``window_tokens``.

The anchor occurrence
---------------------
When the quote occurs more than once in the cited document, all window-based
checks use the same **anchor** occurrence: the one closest to a mention of the
question's entity (smallest distance; an occurrence with no entity mention in
the document counts as infinitely far), ties broken by the earliest start.

Competing-entity candidates
---------------------------
See :func:`competitor_candidates` and :func:`lead_term`. In short: the subject
term at the start of each provided document (its *lead term*), if it recurs in
that document, is a candidate competing entity unless it is the question's
entity, appears in the question, or is a qualifier.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from binding_gate.contracts import AnswerCandidate, AnswerStatus, Document, QuestionConstraints
from binding_gate.textnorm import normalize_text

WORD = re.compile(r"\w+")

MAX_LISTED = 20
"""At most this many positions are listed in check details (counts are always complete)."""

LEAD_CUT = re.compile(r"[,;:()\[\]{}!?\"]|\.(?=\s|$)")
"""Where the lead segment of a document ends: the first comma, semicolon, colon,
bracket, exclamation or question mark, double quote, or full stop followed by
whitespace or the end of the text (so "5.3 mg" does not end it)."""

STOP_WORDS: frozenset[str] = frozenset(
    """
    a an the this that these those it its they their them he his she her we our you your i my
    and or but nor so yet for of in on at by to from with without within into onto upon over
    under about against between among through during before after above below across along
    around beyond toward towards via per as than then also both either neither not no
    is are was were be been being am has have had do does did can could may might must shall
    should will would which who whom whose what when where why how while although though
    because since if unless whereas whether there here such each every all any some many much
    more most other another same few several only very
    """.split()
)
"""Fixed list of English function words. The lead term of a document stops at the
first of these (PREREGISTRATION.md section 5.1). It is part of the frozen gate."""


# --------------------------------------------------------------------------- #
# Tokens and spans
# --------------------------------------------------------------------------- #


def tokens_of(text: str) -> tuple[str, ...]:
    """The tokens of ``normalize_text(text)``."""
    return tuple(match.group(0) for match in WORD.finditer(normalize_text(text)))


@dataclass(frozen=True)
class Span:
    """A token span ``[start, end)`` with its characters ``[char_start, char_end)`` in the normalised text."""

    start: int
    end: int
    char_start: int
    char_end: int

    def to_dict(self) -> dict[str, int]:
        return {
            "token_start": self.start,
            "token_end": self.end,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }


def distance(a: Span, b: Span) -> int:
    """Number of tokens strictly between two spans; 0 when they touch or overlap."""
    if a.end <= b.start:
        return b.start - a.end
    if b.end <= a.start:
        return a.start - b.end
    return 0


def overlaps(a: Span, b: Span) -> bool:
    """True when the two spans share at least one token."""
    return a.start < b.end and b.start < a.end


@dataclass(frozen=True)
class DocText:
    """A document's normalised text and tokens."""

    id: str
    normalized: str
    tokens: tuple[str, ...]
    offsets: tuple[tuple[int, int], ...]

    @classmethod
    def of(cls, document: Document) -> DocText:
        normalized = normalize_text(document.text)
        matches = list(WORD.finditer(normalized))
        return cls(
            id=document.id,
            normalized=normalized,
            tokens=tuple(m.group(0) for m in matches),
            offsets=tuple((m.start(), m.end()) for m in matches),
        )

    def span(self, start: int, end: int) -> Span:
        """The span of tokens ``[start, end)`` (``end > start``)."""
        return Span(start, end, self.offsets[start][0], self.offsets[end - 1][1])

    def find(self, phrase: Sequence[str]) -> list[Span]:
        """Every occurrence of the token sequence ``phrase`` (empty phrase: none)."""
        k = len(phrase)
        if k == 0:
            return []
        phrase = tuple(phrase)
        first = phrase[0]
        return [
            self.span(i, i + k)
            for i in range(len(self.tokens) - k + 1)
            if self.tokens[i] == first and self.tokens[i : i + k] == phrase
        ]

    def count(self, phrase: Sequence[str]) -> int:
        return len(self.find(phrase))

    def char_span_to_tokens(self, char_start: int, char_end: int) -> Span:
        """Tokens overlapping ``[char_start, char_end)``; an empty span at the next token if none do."""
        inside = [i for i, (s, e) in enumerate(self.offsets) if s < char_end and e > char_start]
        if inside:
            return Span(inside[0], inside[-1] + 1, char_start, char_end)
        following = next((i for i, (s, _e) in enumerate(self.offsets) if s >= char_end), len(self.offsets))
        return Span(following, following, char_start, char_end)


# --------------------------------------------------------------------------- #
# Forms and mentions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Form:
    """One surface form to look for: the text as given and its tokens."""

    text: str
    tokens: tuple[str, ...]


def forms_of(texts: Iterable[str]) -> tuple[list[Form], list[str]]:
    """``(matchable forms, unmatchable texts)``; duplicates (same tokens) keep the first spelling."""
    forms: list[Form] = []
    unmatchable: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for text in texts:
        tokens = tokens_of(text)
        if not tokens:
            unmatchable.append(text)
        elif tokens not in seen:
            seen.add(tokens)
            forms.append(Form(text, tokens))
    return forms, unmatchable


def entity_texts(constraints: QuestionConstraints) -> list[str]:
    return [constraints.entity.name, *constraints.entity.aliases]


@dataclass(frozen=True)
class Mention:
    form: str
    span: Span


def mentions(doc: DocText, forms: Sequence[Form]) -> list[Mention]:
    """All mentions of any form, sorted by position, then longer first, then form order."""
    found = [
        (span.start, -(span.end - span.start), index, Mention(form.text, span))
        for index, form in enumerate(forms)
        for span in doc.find(form.tokens)
    ]
    return [entry[3] for entry in sorted(found, key=lambda entry: entry[:3])]


def nearest(mention_list: Sequence[Mention], target: Span) -> tuple[Mention | None, float]:
    """The mention closest to ``target`` (ties: earliest position) and its distance (inf if none)."""
    best: Mention | None = None
    best_distance: float = math.inf
    for mention in mention_list:  # already sorted by position, so the first minimum wins ties
        d = distance(mention.span, target)
        if d < best_distance:
            best, best_distance = mention, d
    return best, best_distance


def describe_mention(mention: Mention | None, target: Span, window_tokens: int | None = None) -> dict[str, Any] | None:
    if mention is None:
        return None
    d = distance(mention.span, target)
    out: dict[str, Any] = {
        "form": mention.form,
        "distance_tokens": d,
        "inside_quote": overlaps(mention.span, target),
        **mention.span.to_dict(),
    }
    if window_tokens is not None:
        out["in_window"] = d <= window_tokens
    return out


# --------------------------------------------------------------------------- #
# Locating the quote
# --------------------------------------------------------------------------- #


def require_answered(answer: AnswerCandidate) -> None:
    if answer.status is not AnswerStatus.ANSWERED:
        raise ValueError(
            f"binding checks evaluate only answered candidates, got status {answer.status.value!r}"
        )


def quote_occurrences(doc: DocText, quote: str) -> list[Span]:
    """Every (possibly overlapping) occurrence of the normalised quote in the document."""
    needle = normalize_text(quote)
    if not needle:
        return []
    found: list[Span] = []
    position = doc.normalized.find(needle)
    while position != -1:
        found.append(doc.char_span_to_tokens(position, position + len(needle)))
        position = doc.normalized.find(needle, position + 1)
    return found


def cited_document(answer: AnswerCandidate, documents: Sequence[Document]) -> Document | None:
    return next((d for d in documents if d.id == answer.source_id), None)


@dataclass(frozen=True)
class Located:
    """The quote found in its cited document, with the anchor occurrence and the entity mentions."""

    doc: DocText
    occurrences: tuple[Span, ...]
    anchor: Span
    entity_forms: tuple[Form, ...]
    entity_unmatchable: tuple[str, ...]
    entity_mentions: tuple[Mention, ...]
    nearest_entity: Mention | None
    entity_distance: float

    def base_details(self) -> dict[str, Any]:
        return {
            "source_id": self.doc.id,
            "occurrences": len(self.occurrences),
            "anchor": self.anchor.to_dict(),
        }


@dataclass(frozen=True)
class NotLocated:
    """Why the quote could not be placed in its cited document."""

    reason: str  # "source_not_provided" or "quote_not_found"
    source_id: str | None

    def details(self) -> dict[str, Any]:
        return {"reason": self.reason, "source_id": self.source_id}


def locate(
    constraints: QuestionConstraints, answer: AnswerCandidate, documents: Sequence[Document]
) -> Located | NotLocated:
    """Find the quote in the cited document and choose the anchor occurrence (see module docstring)."""
    require_answered(answer)
    document = cited_document(answer, documents)
    if document is None:
        return NotLocated("source_not_provided", answer.source_id)
    doc = DocText.of(document)
    occurrences = quote_occurrences(doc, answer.quote or "")
    if not occurrences:
        return NotLocated("quote_not_found", answer.source_id)
    forms, unmatchable = forms_of(entity_texts(constraints))
    entity_mentions = mentions(doc, forms)
    scored = []
    for occurrence in occurrences:
        _mention, d = nearest(entity_mentions, occurrence)
        scored.append((d, occurrence.char_start, occurrence))
    _d, _start, anchor = min(scored, key=lambda entry: entry[:2])
    mention, d = nearest(entity_mentions, anchor)
    return Located(
        doc=doc,
        occurrences=tuple(occurrences),
        anchor=anchor,
        entity_forms=tuple(forms),
        entity_unmatchable=tuple(unmatchable),
        entity_mentions=tuple(entity_mentions),
        nearest_entity=mention,
        entity_distance=d,
    )


def json_distance(d: float) -> int | None:
    """Distances for JSON details: ``None`` stands for "no mention" (infinite)."""
    return None if math.isinf(d) else int(d)


# --------------------------------------------------------------------------- #
# Competing-entity candidates
# --------------------------------------------------------------------------- #


def lead_run(doc: DocText) -> tuple[str, ...]:
    """The tokens at the very start of the document, up to the first stop word.

    Only the *lead segment* is considered: the text before the first
    :data:`LEAD_CUT` character. Tokens are taken from its start until the first
    token in :data:`STOP_WORDS`.
    """
    cut = LEAD_CUT.search(doc.normalized)
    end = cut.start() if cut else len(doc.normalized)
    run: list[str] = []
    for token, (start, _stop) in zip(doc.tokens, doc.offsets):
        if start >= end or token in STOP_WORDS:
            break
        run.append(token)
    return tuple(run)


def lead_term(doc: DocText, *, max_tokens: int, min_repeats: int) -> tuple[str, ...] | None:
    """The document's lead term: the subject named at its start, if it recurs.

    Rule: take :func:`lead_run`; if it is empty or longer than ``max_tokens``
    there is no lead term. Otherwise the lead term is the LONGEST prefix of the
    run that occurs at least ``min_repeats`` times in the whole document and
    contains at least one letter (pure numbers are not names). Example:
    "Everoovate represents a novel ..." gives the run ("everoovate",
    "represents"); "everoovate represents" occurs once, "everoovate" three times,
    so the lead term is "everoovate".
    """
    run = lead_run(doc)
    if not run or len(run) > max_tokens:
        return None
    for k in range(len(run), 0, -1):
        prefix = run[:k]
        if any(ch.isalpha() for token in prefix for ch in token) and doc.count(prefix) >= min_repeats:
            return prefix
    return None


def _contains(outer: tuple[str, ...], inner: tuple[str, ...]) -> bool:
    k = len(inner)
    return any(outer[i : i + k] == inner for i in range(len(outer) - k + 1))


def _related(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Equal, or one contains the other as a contiguous token sequence."""
    return _contains(a, b) or _contains(b, a)


@dataclass(frozen=True)
class Candidate:
    term: str
    tokens: tuple[str, ...]
    from_document: str
    excluded: str | None  # None, "question_entity", "in_question" or "qualifier"

    def to_dict(self) -> dict[str, Any]:
        return {"term": self.term, "from_document": self.from_document, "excluded": self.excluded}


def competitor_candidates(
    constraints: QuestionConstraints,
    documents: Sequence[Document],
    *,
    max_tokens: int,
    min_repeats: int,
) -> list[Candidate]:
    """Competing-entity candidates from the provided documents, in document order.

    For every provided document its :func:`lead_term` (if any) is a candidate.
    A candidate is EXCLUDED (kept in the list with the reason, for auditing) when
    its tokens are equal to, contain, or are contained in the tokens of the
    question's entity name or an alias (``question_entity``); when it occurs as a
    contiguous token sequence in the question (``in_question``); or when it is
    related in the same way to a qualifier text or alias (``qualifier``).
    Duplicates (same tokens) are listed once, at their first document.
    Uses only the question, the constraints and the document text.
    """
    entity_tokens = [form.tokens for form in forms_of(entity_texts(constraints))[0]]
    question_tokens = tokens_of(constraints.question)
    qualifier_tokens = [
        form.tokens
        for qualifier in constraints.qualifiers
        for form in forms_of([qualifier.text, *qualifier.aliases])[0]
    ]
    out: list[Candidate] = []
    seen: set[tuple[str, ...]] = set()
    for document in documents:
        term = lead_term(DocText.of(document), max_tokens=max_tokens, min_repeats=min_repeats)
        if term is None or term in seen:
            continue
        seen.add(term)
        if any(_related(term, tokens) for tokens in entity_tokens):
            excluded: str | None = "question_entity"
        elif _contains(question_tokens, term):
            excluded = "in_question"
        elif any(_related(term, tokens) for tokens in qualifier_tokens):
            excluded = "qualifier"
        else:
            excluded = None
        out.append(Candidate(" ".join(term), term, document.id, excluded))
    return out
