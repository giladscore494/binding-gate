"""The required answer structure and its deterministic parser (PREREGISTRATION.md 6.2, 6.4).

Every arm asks the model for the same structure and parses the reply with
:func:`parse_answer`, a pure function of the raw text: no model call, no
network, no clock, no knowledge of the item.

The structure the model is asked for (:data:`ANSWER_FORMAT_INSTRUCTIONS`) is one
JSON object, either::

    {"status": "answered", "value": "...", "quote": "...", "source_id": "..."}
    {"status": "abstained", "reason": "..."}

Parsing rule (frozen with the pre-registration; any change is an amendment)
--------------------------------------------------------------------------
1. Scan the raw text from left to right. At every ``{`` that is not inside an
   object already decoded, try to decode one JSON value. Keep every decoded JSON
   *object* that has a ``"status"`` key (an "answer object"). Text around the
   objects (prose, Markdown code fences, reasoning) is ignored.
2. No answer object -> ``unparseable``. Several answer objects that are not all
   equal -> ``unparseable`` (the parser never chooses between different answers).
3. ``status`` must be the string ``answered`` or ``abstained`` (surrounding
   whitespace and letter case ignored).
4. ``answered`` needs ``value``, ``quote`` and ``source_id``, each a JSON string
   with at least one non-whitespace character. Anything missing, empty or of
   another type -> ``unparseable``.
5. ``abstained`` must not carry an answer: ``value``, ``quote`` and
   ``source_id`` must each be absent, ``null`` or blank; otherwise
   ``unparseable``. ``reason`` is optional.
6. Keys other than the five named above are ignored.

The parser never repairs output: no fixing of broken JSON, no guessing a missing
field, no second request to the model. Outer whitespace of the three answer
fields is trimmed; nothing else is altered. The minimum quote length of
PREREGISTRATION.md 6.3 is applied by the provenance scorer, not here, because it
is a scoring rule that uses the pre-registered text normalisation.
"""

from __future__ import annotations

import json
from typing import Any

from binding_gate.contracts import AnswerCandidate

ANSWER_FORMAT_VERSION = 1

ANSWER_FORMAT_INSTRUCTIONS = """\
Output format (required; it replaces any other formatting or citation instruction above).
Reply with exactly one JSON object and nothing else.

If the documents contain evidence that answers the question:
{"status": "answered", "value": "<your answer in one or two sentences>", "quote": "<one passage copied from one document>", "source_id": "<source label of that document>"}

If they do not:
{"status": "abstained", "reason": "<one short sentence>"}

Rules:
- "quote" must be copied word for word from a single document: one continuous passage of at least five words that supports your answer. Do not paraphrase, shorten, merge or correct it.
- "source_id" must be the source label of the document the quote was copied from, exactly as written after "source:" in that document's header.
- All fields are JSON strings. Do not add other keys."""

_ANSWER_FIELDS = ("value", "quote", "source_id")


def _answer_objects(text: str) -> list[dict[str, Any]]:
    """Every top-level JSON object with a ``status`` key found in ``text``, in order."""
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    position = 0
    while True:
        start = text.find("{", position)
        if start < 0:
            return found
        try:
            value, end = decoder.raw_decode(text, start)
        except (json.JSONDecodeError, RecursionError):
            position = start + 1
            continue
        if isinstance(value, dict) and "status" in value:
            found.append(value)
        position = end


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_answer(raw: str) -> AnswerCandidate:
    """Parse a model reply into an :class:`AnswerCandidate` (see the module docstring)."""
    if not isinstance(raw, str):
        raise TypeError("parse_answer expects the raw model output as a string")
    if not raw.strip():
        return AnswerCandidate.unparseable(raw=raw, reason="empty output")

    objects = _answer_objects(raw)
    if not objects:
        return AnswerCandidate.unparseable(raw=raw, reason="no JSON object with a status field")
    if any(obj != objects[0] for obj in objects[1:]):
        return AnswerCandidate.unparseable(raw=raw, reason="several different answer objects")
    obj = objects[0]

    status = obj["status"]
    if not isinstance(status, str) or status.strip().casefold() not in {"answered", "abstained"}:
        return AnswerCandidate.unparseable(
            raw=raw, reason="status is neither 'answered' nor 'abstained'"
        )
    status = status.strip().casefold()

    if status == "abstained":
        carried = [name for name in _ANSWER_FIELDS if not _blank(obj.get(name))]
        if carried:
            return AnswerCandidate.unparseable(
                raw=raw, reason=f"abstained but carries {', '.join(carried)}"
            )
        reason = obj.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reason = None
        return AnswerCandidate.abstained(raw=raw, reason=None if reason is None else reason.strip())

    for name in _ANSWER_FIELDS:
        value = obj.get(name)
        if not isinstance(value, str):
            kind = "missing" if value is None else "not a string"
            return AnswerCandidate.unparseable(raw=raw, reason=f"answered but {name} is {kind}")
        if not value.strip():
            return AnswerCandidate.unparseable(raw=raw, reason=f"answered but {name} is empty")
    return AnswerCandidate.answered(
        value=obj["value"].strip(),
        quote=obj["quote"].strip(),
        source_id=obj["source_id"].strip(),
        raw=raw,
    )
