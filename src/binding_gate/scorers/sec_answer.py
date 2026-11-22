"""Scorer ``sec_answer``: deterministic correctness of answers about 10-K figures (layer 2, PR 17).

The dataset ``sec_financials`` has independent gold values (XBRL facts of the
filing, ``item.gold["value"]`` in US dollars). This scorer decides, with no model
and no network, whether the amount stated in an answered final answer's VALUE is
the gold amount, the peer company's amount (wrong entity), the same company's
amount for another year or at another scale (wrong period or scale), or none of
them; and records, with the existing ``provenance`` logic, where its QUOTE was
found (target or competitor document). Rule version :data:`RULE_VERSION`
(PREREGISTRATION.md sections 6.3 and 7, layer 2; **PROPOSED**).

Reading the amount (:func:`parse_amount`)
-----------------------------------------
After the normalisation of section 6.1 (which also folds case), numbers are
scanned left to right. A number is an *amount* when it carries a currency marker
(``$``, ``us$``, ``usd``) or a scale word, or is written with thousands
separators (``1,234``). Not amounts: percentages; numbers glued to letters
(``fy2024``, ``q4``, ``10-k``); ordinals (``1st``); a day after a month name
(``september 28``); a four-digit year 1900-2100 without currency or scale. The
answer's amount is the FIRST amount of the value.

* Scale: a scale word right after the number (``thousand``, ``million``,
  ``billion``, ``trillion`` and their plurals; ``k``, ``m``, ``mm``, ``mn``,
  ``b``, ``bn``, ``t``, ``tn`` attached or after one space - ``k/m/b/t`` only
  attached); otherwise a phrase ``in thousands`` / ``in millions`` / ``in
  billions`` anywhere in the value; otherwise none (whole dollars). A bare
  ``391,035`` therefore means 391,035 dollars: if the statement is in millions
  that is a scale error, as it would be in an analyst's note.
* Sign: negative when a minus sign or parentheses enclose the number (``-512``,
  ``$(512)``, ``(512) million``) or the word ``loss``/``losses``/``deficit``
  occurs in the 60 characters before it, other than in the phrases ``income
  (loss)``, ``income (or loss)``, ``income or loss``, ``income/(loss)`` (and the
  same with ``earnings`` or ``profit``), which merely name the line.
* Precision: the place value 10^e of the last stated digit, scale included
  (decimals: the last written decimal, even a zero; integers: the last non-zero
  digit). ``391,035 million`` -> e = 6; ``$391.0 billion`` -> e = 8;
  ``$1.2 billion`` -> e = 8. Significant digits: from the first to the last
  stated non-zero (decimals: written) digit.

Matching an amount ``a`` to a value ``v`` (:func:`matches`)
------------------------------------------------------------
``a`` matches ``v`` when it has at least :data:`MIN_SIGNIFICANT_DIGITS`
significant digits and ``| |a| - |v| | < 10^e`` (one unit of the last stated
digit: correct rounding and truncation both pass), and the sign does not
contradict: an explicitly negative amount never matches a positive value, while
an amount without explicit sign matches either sign (``a net loss of $512
million`` and ``$512 million`` both match -512,000,000; sign words are too varied
to require them).

Labels (first that applies)
---------------------------
``correct``                the amount matches the gold value and the quote is found in a target document
``value_unsupported``      the amount matches the gold value but the quote is not in a target document
                           (e.g. a figure recalled from memory while quoting the peer's statement)
``wrong_entity``           it matches a value of the same metric of the PEER company (any year of its filing)
``wrong_period_or_scale``  it matches the target's value of the same metric for another year of the same
                           filing, or the gold value or another-year value after rescaling by 10^(3k), k != 0
``other_wrong``            answered, and none of the above (also: no amount, or fewer than 2 significant digits)
``abstained``, ``unparseable``  as in ``provenance`` (the same minimum quote length of 5 tokens)

``details``: ``outcome`` (the statistics' category), the parsed amount, every
match, ``scale_error``, ``provenance`` (the ``provenance`` scorer's label for the
same answer), ``strict_wrong_entity`` (wrong entity AND quote only in the
competitor document) and ``source_id_matches``.

Statistics (layer 2)
--------------------
:func:`sec_outcome` maps the labels onto the layer-1 categories used by
``binding_gate.stats``: ``correct`` -> ``target_grounded`` (the correct answer
of M2 and H2), ``wrong_entity`` -> ``wrong_entity`` (M1 and H1), the other
answered labels -> ``ungrounded`` (answered, neither correct nor wrong-entity),
``abstained`` -> ``abstention``, ``unparseable`` -> ``unparseable``, an error ->
``error``. :func:`analyse_sec` runs the pre-registered analysis with H1 and H2
per model (section 11) and adds the label counts, the strict wrong-entity count
and the binding-error count (wrong entity + wrong period or scale; exploratory).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from binding_gate.contracts import AnswerCandidate, AnswerStatus, Item, ScoreResult, TraceRecord
from binding_gate.registry import register
from binding_gate.scorers.provenance import ProvenanceScorer
from binding_gate.textnorm import normalize_text

SCORER_NAME = "sec_answer"
RULE_VERSION = 1
MIN_SIGNIFICANT_DIGITS = 2
DEFAULT_MIN_QUOTE_TOKENS = 5

LABEL_CORRECT = "correct"
LABEL_VALUE_UNSUPPORTED = "value_unsupported"
LABEL_WRONG_ENTITY = "wrong_entity"
LABEL_WRONG_PERIOD_OR_SCALE = "wrong_period_or_scale"
LABEL_OTHER_WRONG = "other_wrong"
LABEL_ABSTAINED = "abstained"
LABEL_UNPARSEABLE = "unparseable"
LABELS = (LABEL_CORRECT, LABEL_VALUE_UNSUPPORTED, LABEL_WRONG_ENTITY, LABEL_WRONG_PERIOD_OR_SCALE,
          LABEL_OTHER_WRONG, LABEL_ABSTAINED, LABEL_UNPARSEABLE)  # fmt: skip

OUTCOME_BY_LABEL = {
    LABEL_CORRECT: "target_grounded",
    LABEL_VALUE_UNSUPPORTED: "ungrounded",
    LABEL_WRONG_ENTITY: "wrong_entity",
    LABEL_WRONG_PERIOD_OR_SCALE: "ungrounded",
    LABEL_OTHER_WRONG: "ungrounded",
    LABEL_ABSTAINED: "abstention",
    LABEL_UNPARSEABLE: "unparseable",
}
SEC_CORRECT_OUTCOMES = ("target_grounded",)

SCALE_WORDS = {
    "thousand": 3, "thousands": 3, "million": 6, "millions": 6, "billion": 9, "billions": 9,
    "trillion": 12, "trillions": 12, "mm": 6, "mn": 6, "bn": 9, "tn": 12,
}  # fmt: skip
ATTACHED_ONLY = {"k": 3, "m": 6, "b": 9, "t": 12}
MONTH_WORDS = {
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
    "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}  # fmt: skip

_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_SCALE_AFTER = re.compile(r"\s?(thousands?|millions?|billions?|trillions?|mm|mn|bn|tn)(?![a-z])|(k|m|b|t)(?![a-z])")
_SCALE_PHRASE = re.compile(r"\bin (thousands|millions|billions)\b")
_LOSS = re.compile(r"\b(loss|losses|deficit)\b")
_LINE_NAME = re.compile(r"(income|earnings|profit)\s*(\(\s*(or\s+)?loss\s*\)|or\s+loss|/\s*\(?\s*loss\s*\)?)")


class SecScoringError(ValueError):
    """Raised when an item lacks the gold metadata this scorer needs."""


@dataclass(frozen=True)
class Amount:
    magnitude: Decimal  # absolute value in US dollars
    negative: bool  # explicit negative sign
    exponent: int  # place value 10^e of the last stated digit, scale included
    significant_digits: int
    text: str
    scale_exponent: int

    def signed(self) -> Decimal:
        return -self.magnitude if self.negative else self.magnitude

    def to_dict(self) -> dict[str, Any]:
        return {"value": str(self.signed()), "explicit_negative": self.negative, "precision_exponent": self.exponent,
                "significant_digits": self.significant_digits, "text": self.text, "scale_exponent": self.scale_exponent}


def _precision(digits: str) -> tuple[int, int]:
    """(exponent of the last stated digit, significant digits) of a number written without separators."""
    if "." in digits:
        whole, frac = digits.split(".")
        stripped = (whole + frac).lstrip("0")
        return -len(frac), max(1, len(stripped))
    stripped = digits.lstrip("0")
    if not stripped:
        return 0, 1
    trailing = len(stripped) - len(stripped.rstrip("0"))
    return trailing, len(stripped) - trailing


def parse_amount(value: str) -> Amount | None:
    """The first amount stated in ``value`` (see the module docstring), or ``None``."""
    text = normalize_text(value)
    phrase = _SCALE_PHRASE.search(text)
    for match in _NUMBER.finditer(text):
        start, end = match.span()
        before, after = text[:start], text[end:]
        if before and (before[-1].isalnum() or before[-1] in "._") or (after and (after[0] == "%" or after.startswith(" %"))):
            continue
        if re.match(r"(st|nd|rd|th)\b", after) or re.match(r"-[a-z]", after):
            continue
        prev_word = re.search(r"([a-z]+)\.?\s*$", before)
        if prev_word and prev_word.group(1) in MONTH_WORDS and "," not in match.group() and len(match.group()) <= 2:
            continue
        prefix = before[-6:]
        currency = bool(re.search(r"(\$|us\$|usd)\s*[-(]?\s*$", prefix))
        scale_match = _SCALE_AFTER.match(after)
        closing = re.match(r"\s?\)", after)
        if scale_match is None and closing:  # "$(512) million": the scale word follows the parenthesis
            shifted = _SCALE_AFTER.match(after[closing.end():])
            if shifted and shifted.group(1):
                scale_match = shifted
        scale_exp = 0
        if scale_match:
            if scale_match.group(1):
                scale_exp = SCALE_WORDS[scale_match.group(1)]
            else:
                scale_exp = ATTACHED_ONLY[scale_match.group(2)]
        digits = match.group().replace(",", "")
        lead = before[-8:]
        parenthesised = bool(re.search(r"\(\s?(\$|us\$|usd)?\s?$", lead)) and bool(closing)
        if not (currency or scale_exp or "," in match.group() or parenthesised):
            continue
        if not currency and not scale_exp and "." not in digits and len(digits) == 4 and 1900 <= int(digits) <= 2100 and "," not in match.group():
            continue
        if not scale_exp and phrase:
            scale_exp = {"thousands": 3, "millions": 6, "billions": 9}[phrase.group(1)]
        exponent, significant = _precision(digits)
        minus = bool(re.search(r"(^|[^\w)])-(\$|us\$|usd)?\s?$", lead) or re.search(r"(\$|us\$|usd)\s?-$", lead))
        negative = minus or parenthesised
        if not negative:
            window = _LINE_NAME.sub(" ", before[-60:])
            negative = bool(_LOSS.search(window))
        magnitude = Decimal(digits).scaleb(scale_exp)
        return Amount(magnitude, negative, exponent + scale_exp, significant, text[max(0, start - 3): end + 12].strip(), scale_exp)
    return None


def matches(amount: Amount, value: int, *, rescale: int = 0) -> bool:
    """Whether ``amount`` x 10^rescale states ``value`` (module docstring)."""
    if amount.significant_digits < MIN_SIGNIFICANT_DIGITS:
        return False
    if amount.negative and value > 0:
        return False
    magnitude = amount.magnitude.scaleb(rescale)
    tolerance = Decimal(1).scaleb(amount.exponent + rescale)
    return abs(magnitude - abs(Decimal(value))) < tolerance


@register("scorer", SCORER_NAME)
class SecAnswerScorer:
    """Options: ``min_quote_tokens`` (default 5, as ``provenance``; PREREGISTRATION.md 6.3)."""

    name = SCORER_NAME

    def __init__(self, *, min_quote_tokens: int = DEFAULT_MIN_QUOTE_TOKENS):
        if isinstance(min_quote_tokens, bool) or not isinstance(min_quote_tokens, int) or min_quote_tokens < 1:
            raise ValueError("min_quote_tokens must be an integer >= 1")
        self.min_quote_tokens = min_quote_tokens
        self._provenance = ProvenanceScorer(min_quote_tokens=min_quote_tokens)

    @staticmethod
    def _gold(item: Item) -> tuple[int, list[int], list[int]]:
        gold = item.gold
        value = gold.get("value")
        if not isinstance(value, int) or isinstance(value, bool):
            raise SecScoringError(f"item {item.id!r}: gold['value'] must be an integer number of US dollars")
        try:
            alternatives = [int(a["value"]) for a in gold.get("target_alternatives", [])]
            competitor = [int(c["value"]) for c in gold.get("competitor", {}).get("values", [])]
        except (KeyError, TypeError, ValueError) as exc:
            raise SecScoringError(f"item {item.id!r}: malformed alternatives or competitor values") from exc
        return value, alternatives, competitor

    def score(self, item: Item, final_answer: AnswerCandidate) -> ScoreResult:
        value, alternatives, competitor = self._gold(item)
        provenance = self._provenance.score(item, final_answer)  # also validates the document roles
        base: dict[str, Any] = {"condition": item.gold.get("condition"), "metric": item.gold.get("metric"),
                                "rule_version": RULE_VERSION, "provenance": provenance.label}
        if final_answer.status is AnswerStatus.ABSTAINED:
            return ScoreResult(self.name, LABEL_ABSTAINED, {"outcome": OUTCOME_BY_LABEL[LABEL_ABSTAINED], **base})
        if final_answer.status is AnswerStatus.UNPARSEABLE or provenance.label == "unparseable":
            reason = provenance.details.get("reason", "unparseable_output")
            return ScoreResult(self.name, LABEL_UNPARSEABLE, {"outcome": "unparseable", "reason": reason, **base})
        amount = parse_amount(final_answer.value or "")
        found = {
            "gold": False,
            "competitor": False,
            "target_other_period": False,
            "scaled": None,
        }
        if amount is not None:
            found["gold"] = matches(amount, value)
            found["competitor"] = any(matches(amount, v) for v in competitor)
            found["target_other_period"] = any(matches(amount, v) for v in alternatives)
            for k in (-4, -3, -2, -1, 1, 2, 3, 4):
                if any(matches(amount, v, rescale=3 * k) for v in [value, *alternatives]):
                    found["scaled"] = 3 * k
                    break
        in_target = provenance.label in ("target_only", "both")
        if found["gold"]:
            label = LABEL_CORRECT if in_target else LABEL_VALUE_UNSUPPORTED
        elif found["competitor"]:
            label = LABEL_WRONG_ENTITY
        elif found["target_other_period"] or found["scaled"] is not None:
            label = LABEL_WRONG_PERIOD_OR_SCALE
        else:
            label = LABEL_OTHER_WRONG
        reason = None
        if amount is None:
            reason = "no_amount"
        elif amount.significant_digits < MIN_SIGNIFICANT_DIGITS:
            reason = "too_imprecise"
        details = {
            "outcome": OUTCOME_BY_LABEL[label],
            **base,
            "amount": amount.to_dict() if amount else None,
            "reason": reason,
            "matches": found,
            "scale_error": label == LABEL_WRONG_PERIOD_OR_SCALE and not found["target_other_period"],
            "strict_wrong_entity": label == LABEL_WRONG_ENTITY and provenance.label == "competitor_only",
            "matched_document_ids": provenance.details.get("matched_document_ids", []),
            "source_id_matches": provenance.details.get("source_id_matches"),
        }
        return ScoreResult(self.name, label, details)


# --------------------------------------------------------------------------- #
# Statistics: the layer-2 outcome rule, H1 and H2 per model
# --------------------------------------------------------------------------- #

SEC_DECISION = "H1 and H2 per primary model (PREREGISTRATION.md section 11), as on layer 1"


def sec_outcome(record: TraceRecord) -> str:
    """The statistics' outcome category of a layer-2 record (see the module docstring)."""
    if record.error is not None:
        return "error"
    for score in record.scores:
        if score.scorer == SCORER_NAME:
            return OUTCOME_BY_LABEL.get(score.label, "error")
    raise SecScoringError(f"record {record.item_id!r} ({record.arm}, {record.model}) has no {SCORER_NAME} score")


def label_counts(records: Sequence[TraceRecord]) -> dict[str, dict[str, dict[str, int]]]:
    """Per model and arm: every ``sec_answer`` label, strict wrong-entity answers and binding errors (counts)."""
    out: dict[str, dict[str, Counter]] = {}
    for record in records:
        counts = out.setdefault(record.model, {}).setdefault(record.arm, Counter())
        score = next((s for s in record.scores if s.scorer == SCORER_NAME), None)
        if record.error is not None or score is None:
            counts["error"] += 1
            continue
        counts[score.label] += 1
        if score.details.get("strict_wrong_entity"):
            counts["strict_wrong_entity"] += 1
        if score.label in (LABEL_WRONG_ENTITY, LABEL_WRONG_PERIOD_OR_SCALE):
            counts["binding_errors"] += 1
        if score.details.get("scale_error"):
            counts["scale_errors"] += 1
    keys = (*LABELS, "error", "strict_wrong_entity", "binding_errors", "scale_errors")
    return {m: {a: {k: c.get(k, 0) for k in keys} for a, c in sorted(arms.items())} for m, arms in sorted(out.items())}


def analyse_sec(run_dirs: Sequence[str | Path], **kwargs: Any) -> dict[str, Any]:
    """``binding_gate.stats.analysis.analyse`` with the layer-2 outcome rule (H1 and H2 per model)."""
    from binding_gate.stats.analysis import analyse, load_trace_set

    trace_set = load_trace_set(run_dirs)
    analysis = analyse(
        trace_set.records,
        run_dirs=trace_set.run_dirs,
        manifests=trace_set.manifests,
        unfinished=trace_set.unfinished,
        outcome_of=sec_outcome,
        correct_outcomes=SEC_CORRECT_OUTCOMES,
        **kwargs,
    )
    analysis["outcome_rule"] = {
        "scorer": SCORER_NAME,
        "rule_version": RULE_VERSION,
        "mapping": dict(OUTCOME_BY_LABEL),
        "correct": LABEL_CORRECT,
        "m1": "wrong_entity (the peer company's figure) / answered",
        "m2": "among records where no_gate was correct, the share where the compared arm was not",
        "decision": SEC_DECISION,
    }
    analysis["layer2"] = {
        "label_counts": label_counts(trace_set.records),
        "note": "exploratory: strict wrong entity = wrong_entity with the quote only in the competitor document; "
        "binding errors = wrong_entity + wrong_period_or_scale",
    }
    return analysis
