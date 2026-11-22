"""The search queries of layer 3: who chooses them and how (PREREGISTRATION.md section 5).

Both queries are chosen by the harness, deterministically; the model under test
never writes a query. This keeps the first attempt identical across arms and
models (so ``gated_research`` and the live ``no_gate`` baseline are compared on
the same evidence) and makes the re-search a pure function of the gate's
verdict.

First attempt
-------------
:func:`first_query`: the question text with runs of whitespace collapsed to one
space and the ends trimmed. Nothing is added or removed. (Google ignores words
beyond its query length limit; such questions are searched as they are.)

Re-search (template version 1, **PROPOSED - requires owner approval before freeze**)
------------------------------------------------------------------------------
:func:`research_query` reads the verdict's ``research_request`` (canonical JSON
written by ``binding_gate.gates.binding.research_request``: ``entity``,
``aliases``, ``attribute``, ``qualifiers``, ``failed_checks``, ``query``) and
returns::

    "<entity>" <qualifier 1> ... <qualifier n> <attribute>

* the entity name in double quotes, so that the search engine requires the
  exact name (the failures that lead to RESEARCH are evidence about another
  entity, or without the question's qualifiers);
* then every qualifier text, in the order of the constraints, each once
  (case-insensitive duplicates dropped);
* then the attribute;
* double quotes inside any part are removed, whitespace is collapsed, empty
  parts are skipped.

Aliases and the failed-check names are not used in the query (they are kept in
the trace through the verdict). The same research request always gives the
same query.
"""

from __future__ import annotations

import json
import re

RESEARCH_QUERY_TEMPLATE_VERSION = 1
_WS = re.compile(r"\s+")


def _clean(text: object) -> str:
    if not isinstance(text, str):
        return ""
    return _WS.sub(" ", text.replace('"', " ")).strip()


def first_query(question: str) -> str:
    """The first-attempt query: the question, whitespace collapsed."""
    query = _WS.sub(" ", question).strip()
    if not query:
        raise ValueError("the question is empty")
    return query


def research_query(research_request: str) -> str:
    """The re-search query built from a RESEARCH verdict's ``research_request`` (see the module docstring)."""
    try:
        data = json.loads(research_request)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"research_request is not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("research_request must be a JSON object")
    entity = _clean(data.get("entity"))
    if not entity:
        raise ValueError("research_request has no entity")
    parts = [f'"{entity}"']
    seen = {entity.casefold()}
    qualifiers = data.get("qualifiers") or []
    if not isinstance(qualifiers, list):
        raise ValueError("research_request.qualifiers must be a list")
    for qualifier in qualifiers:
        text = _clean(qualifier)
        if text and text.casefold() not in seen:
            parts.append(text)
            seen.add(text.casefold())
    attribute = _clean(data.get("attribute"))
    if attribute and attribute.casefold() not in seen:
        parts.append(attribute)
    return " ".join(parts)
