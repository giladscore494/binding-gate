"""Check ``qualifiers_in_window`` (gate function C, third check).

Every qualifier of the constraints (time, place, version, unit, other) must be
mentioned, by its text or one of its aliases, within ``window_tokens`` tokens of
the anchor occurrence of the quote in the cited document.

* NOT_APPLICABLE when the constraints have no qualifiers
  (``reason = "no_qualifiers"``) or when the quote cannot be placed in its cited
  document (``source_not_provided`` / ``quote_not_found``).
* PASS when every qualifier has a mention within the window.
* FAIL otherwise (``reason = "qualifier_outside_window"``); ``details["missing"]``
  lists the qualifier texts without a mention in the window. A qualifier none
  of whose forms contains a word character can never be matched and counts as
  missing (``unmatchable``).

Each qualifier's entry in ``details["qualifiers"]`` gives its kind, text, the
nearest mention (form, distance, positions) or ``null``.

Binding scope (PR 18; PROPOSED defaults ``qualifier_scope = "window"``,
``table_binding = false``, which give exactly the behaviour and details above)
-----------------------------------------------------------------------------
``qualifier_scope`` decides, per qualifier, where a mention binds it:

``"window"``                      within the window of the anchor (the rule above);
``"document_subject"``            anywhere in the cited document, provided the
                                  document is bound to the question's entity by its
                                  subject (``_evidence.subject_binding``; the same
                                  condition as ``entity_scope = "document_subject"``);
``"window_or_document_subject"``  either of the two.

Why: in a document whose whole subject is the entity (a drug-for-disease
monograph), a condition the document is about (the disease) is stated once,
often in its first sentences, and governs every statement of the document; a
quote further down is still about it. The subject guard keeps the window rule
for documents about another or several entities.

``table_binding = true`` (``_tables``): when the anchor occurrence of the quote
lies in one or more value cells of a detected table, each qualifier is decided
by the table structure first, using its bound cells:

1. if a form of the qualifier is mentioned in some column header of the bound
   cells' table(s) (the table distinguishes its columns by it), it PASSES iff it
   is mentioned in the column header of EVERY bound cell (``bound_by =
   "table_column"``); a value from another column of the same row therefore
   FAILS, whatever the window or the document subject say;
2. else, if a form is mentioned in some row label of those tables, it passes iff
   it is mentioned in the row label of every bound cell (``"table_row"``);
3. else the table does not distinguish by it: it passes if a form is mentioned
   in the caption of every bound cell's table (``"table_caption"``), or else by
   the ``qualifier_scope`` rule above.

A quote that lies in no value cell is decided by ``qualifier_scope`` alone. With
any non-default option, ``details`` add ``qualifier_scope``, ``table_binding``,
per-qualifier ``bound_by``, and the bound cells.
"""

from __future__ import annotations

from typing import Any, Sequence

from binding_gate.checks import _evidence as ev
from binding_gate.checks import _tables
from binding_gate.checks.entity_in_window import (
    DEFAULT_LEAD_MAX_TOKENS,
    DEFAULT_LEAD_MIN_REPEATS,
    DEFAULT_SCOPE,
    DEFAULT_WINDOW_TOKENS,
    scope_option,
)
from binding_gate.checks.quote_verbatim import int_option
from binding_gate.contracts import AnswerCandidate, CheckResult, CheckStatus, Document, QuestionConstraints
from binding_gate.registry import register

NAME = "qualifiers_in_window"


def bool_option(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false, got {value!r}")
    return value


@register("check", NAME)
class QualifiersInWindow:
    """Options: ``window_tokens`` (PROPOSED 40, the same window as ``entity_in_window``); PR 18:
    ``qualifier_scope`` (PROPOSED ``"window"``), ``table_binding`` (PROPOSED ``false``), ``lead_max_tokens`` and
    ``lead_min_repeats`` (used only by a subject scope)."""

    name = NAME

    def __init__(
        self,
        *,
        window_tokens: int = DEFAULT_WINDOW_TOKENS,
        qualifier_scope: str = DEFAULT_SCOPE,
        table_binding: bool = False,
        lead_max_tokens: int = DEFAULT_LEAD_MAX_TOKENS,
        lead_min_repeats: int = DEFAULT_LEAD_MIN_REPEATS,
    ):
        self.window_tokens = int_option("window_tokens", window_tokens, minimum=0)
        self.qualifier_scope = scope_option("qualifier_scope", qualifier_scope)
        self.table_binding = bool_option("table_binding", table_binding)
        self.lead_max_tokens = int_option("lead_max_tokens", lead_max_tokens)
        self.lead_min_repeats = int_option("lead_min_repeats", lead_min_repeats)

    @property
    def is_default(self) -> bool:
        return self.qualifier_scope == "window" and not self.table_binding

    def check(
        self, constraints: QuestionConstraints, answer: AnswerCandidate, documents: Sequence[Document]
    ) -> CheckResult:
        ev.require_answered(answer)
        if not constraints.qualifiers:
            return CheckResult(self.name, CheckStatus.NOT_APPLICABLE, {"reason": "no_qualifiers"})
        located = ev.locate(constraints, answer, documents)
        if isinstance(located, ev.NotLocated):
            return CheckResult(self.name, CheckStatus.NOT_APPLICABLE, located.details())
        if not self.is_default:
            return self._check_scoped(constraints, answer, documents, located)
        entries: list[dict[str, Any]] = []
        missing: list[str] = []
        for qualifier in constraints.qualifiers:
            forms, unmatchable = ev.forms_of([qualifier.text, *qualifier.aliases])
            mention, d = ev.nearest(ev.mentions(located.doc, forms), located.anchor)
            in_window = mention is not None and d <= self.window_tokens
            if not in_window:
                missing.append(qualifier.text)
            entries.append(
                {
                    "kind": qualifier.kind.value,
                    "text": qualifier.text,
                    "unmatchable_forms": unmatchable,
                    "nearest": ev.describe_mention(mention, located.anchor, self.window_tokens),
                    "in_window": in_window,
                }
            )
        details = {
            **located.base_details(),
            "window_tokens": self.window_tokens,
            "qualifiers": entries,
            "missing": missing,
        }
        if missing:
            return CheckResult(self.name, CheckStatus.FAIL, {**details, "reason": "qualifier_outside_window"})
        return CheckResult(self.name, CheckStatus.PASS, details)

    # ----------------------------------------------------------------------- #
    # PR 18: subject scope and table binding (never reached with the defaults)
    # ----------------------------------------------------------------------- #

    def _check_scoped(
        self,
        constraints: QuestionConstraints,
        answer: AnswerCandidate,
        documents: Sequence[Document],
        located: ev.Located,
    ) -> CheckResult:
        binding: ev.SubjectBinding | None = None
        if self.qualifier_scope != "window":
            binding = ev.subject_binding(
                constraints, located, documents, window_tokens=self.window_tokens,
                max_tokens=self.lead_max_tokens, min_repeats=self.lead_min_repeats,
            )
        cells: list[_tables.Cell] = []
        quoted: list[_tables.Cell] = []
        tables: _tables.Tables | None = None
        if self.table_binding:
            document = ev.cited_document(answer, documents)
            if document is not None:  # always, since the quote was located in it
                tables = _tables.parse_tables(document.text, located.doc.normalized)
                quoted, cells = _tables.bound_cells(
                    tables, located.anchor.char_start, located.anchor.char_end, answer.value
                )
        in_tables = [t for t in (tables.tables if tables else ()) if any(c.table == t.index for c in cells)]
        entries: list[dict[str, Any]] = []
        missing: list[str] = []
        for qualifier in constraints.qualifiers:
            forms, unmatchable = ev.forms_of([qualifier.text, *qualifier.aliases])
            doc_mentions = ev.mentions(located.doc, forms)
            mention, d = ev.nearest(doc_mentions, located.anchor)
            in_window = mention is not None and d <= self.window_tokens
            bound_by = self._table_rule(forms, cells, in_tables) if cells else None
            if bound_by is None:
                if self.qualifier_scope in ("window", "window_or_document_subject") and in_window:
                    bound_by = "window"
                elif self.qualifier_scope != "window" and binding is not None and binding.bound and doc_mentions:
                    bound_by = "document_subject"
            passed = bound_by is not None and not bound_by.startswith("not:")
            if not passed:
                missing.append(qualifier.text)
            entries.append(
                {
                    "kind": qualifier.kind.value,
                    "text": qualifier.text,
                    "unmatchable_forms": unmatchable,
                    "nearest": ev.describe_mention(mention, located.anchor, self.window_tokens),
                    "in_window": in_window,
                    "bound_by": bound_by if passed else None,
                    "table_rule": bound_by.removeprefix("not:") if bound_by and bound_by.startswith("not:") else None,
                }
            )
        details: dict[str, Any] = {
            **located.base_details(),
            "window_tokens": self.window_tokens,
            "qualifier_scope": self.qualifier_scope,
            "table_binding": self.table_binding,
            "qualifiers": entries,
            "missing": missing,
        }
        if binding is not None:
            details["document_subject"] = binding.to_dict()
        if self.table_binding:
            details["tables_detected"] = len(tables.tables) if tables else 0
            details["quoted_cells"] = len(quoted)
            details["bound_cells"] = [c.to_dict() for c in cells[: ev.MAX_LISTED]]
            details["bound_tables"] = [t.to_dict() for t in in_tables]
        if missing:
            return CheckResult(self.name, CheckStatus.FAIL, {**details, "reason": "qualifier_outside_window"})
        return CheckResult(self.name, CheckStatus.PASS, details)

    @staticmethod
    def _table_rule(
        forms: Sequence[ev.Form], cells: Sequence[_tables.Cell], tables: Sequence[_tables.Table]
    ) -> str | None:
        """Rules 1-3 of the module docstring: ``"table_column"`` / ``"table_row"`` / ``"table_caption"`` when the
        qualifier is bound by the table, ``"not:table_column"`` / ``"not:table_row"`` when the table decides that it
        is NOT, and ``None`` when the table does not decide (fall back to ``qualifier_scope``)."""

        def named(text: str | None) -> bool:
            return text is not None and any(_tables.mentions_form(text, f.tokens) for f in forms)

        if any(named(h) for t in tables for h in t.headers):
            return "table_column" if all(named(c.header) for c in cells) else "not:table_column"
        if any(named(r) for t in tables for r in t.row_labels):
            return "table_row" if all(named(c.row_label) for c in cells) else "not:table_row"
        captions = {t.index: t.caption for t in tables}
        if all(named(captions.get(c.table)) for c in cells):
            return "table_caption"
        return None
