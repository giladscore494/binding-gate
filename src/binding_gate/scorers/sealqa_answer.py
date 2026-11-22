"""Scorer ``sealqa_answer``: deterministic correctness against SealQA's gold answer (layer 3).

The primary layer-3 rule (PREREGISTRATION.md section 7, layer 3). No model, no
network: a pure function of the final answer and ``item.gold``.

Rule (version 1, **PROPOSED - requires owner approval before freeze**)
--------------------------------------------------------------------
An ``answered`` final answer is ``correct`` when its VALUE carries the gold
answer: after the pre-registered normalisation
(``binding_gate.textnorm.normalize_text``) and then GRAB-RAG's answer
normalisation (``binding_gate.datasets.grab_rag.answer_normalize``: lower case,
punctuation and the articles a/an/the removed, ``1,000`` -> ``1000``, ``1st`` ->
``first``), the normalised gold answer occurs in the normalised value as a
whole-word sequence. This is the containment rule used for layer 2. SealQA
gives one gold answer per question and no aliases (``gold["answers"]`` is a
list so that aliases could be added by an amendment).

Otherwise the answered final answer is ``incorrect``. ``abstained`` and
``unparseable`` final answers are labelled as such; an answered final answer
whose quote has fewer than ``min_quote_tokens`` (5) whitespace tokens after
normalisation is ``unparseable``, as in ``provenance`` and ``grab_rag_answer``.

Known limits of a deterministic rule (why a secondary judge is planned):
SealQA's own grader is a GPT-4o-mini auto-rater that accepts semantically
equivalent and partial answers ("San Francisco" for "San Francisco, California");
containment misses reorderings, other date formats, units and spelled-out
numbers, and can accept a value that names the gold answer while asserting
something else. The rule decides H4; an LLM judge may only cross-check it
(judge-family rule).

Details: ``outcome`` (the label), ``value_has_gold``, ``value_exact`` (normalised
value equals the normalised gold answer), ``freshness``, ``search_results``,
``subsets``, ``quote_tokens``, ``source_id``.

Statistics. The statistics package (``binding_gate.stats``) names its outcome
categories after layer 1. :func:`layer3_outcome` maps this scorer's labels onto
them - ``correct`` -> ``target_grounded`` (counted as correct),
``incorrect`` -> ``wrong_entity`` (counted as a wrong answer among answered),
``abstained`` -> ``abstention``, ``unparseable`` -> ``unparseable``, an arm error
-> ``error`` - and :func:`analyse_layer3` runs the analysis with that mapping
and ``correct_outcomes = ("target_grounded",)``, so M3 = correct / all items
and M4 = correct final answers among records whose first attempt ended in
RESEARCH. ``python -m binding_gate.scorers.sealqa_answer analyze RUN_DIR ...``
writes the analysis.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from binding_gate.contracts import AnswerCandidate, AnswerStatus, Item, ScoreResult, TraceRecord
from binding_gate.datasets.grab_rag import answer_normalize, contains_answer
from binding_gate.registry import register
from binding_gate.textnorm import normalize_text

SCORER_NAME = "sealqa_answer"
RULE_VERSION = 1
LABEL_CORRECT = "correct"
LABEL_INCORRECT = "incorrect"
LABEL_ABSTAINED = "abstained"
LABEL_UNPARSEABLE = "unparseable"
LABELS = (LABEL_CORRECT, LABEL_INCORRECT, LABEL_ABSTAINED, LABEL_UNPARSEABLE)
DEFAULT_MIN_QUOTE_TOKENS = 5

LAYER3_OUTCOME_BY_LABEL = {
    LABEL_CORRECT: "target_grounded",
    LABEL_INCORRECT: "wrong_entity",
    LABEL_ABSTAINED: "abstention",
    LABEL_UNPARSEABLE: "unparseable",
}
LAYER3_CORRECT_OUTCOMES = ("target_grounded",)


class SealQAScoringError(ValueError):
    """Raised when an item lacks the gold answer."""


def value_carries_gold(value: str, answers: Sequence[str]) -> bool:
    return contains_answer(normalize_text(value), [normalize_text(a) for a in answers])


# Registered only when imported as a module, not when run with ``python -m`` (as in llm_judge_checks).
@register("scorer", SCORER_NAME) if __name__ != "__main__" else (lambda component: component)
class SealQAAnswerScorer:
    """Options: ``min_quote_tokens`` (default 5, as ``provenance``)."""

    name = SCORER_NAME

    def __init__(self, *, min_quote_tokens: int = DEFAULT_MIN_QUOTE_TOKENS):
        if isinstance(min_quote_tokens, bool) or not isinstance(min_quote_tokens, int) or min_quote_tokens < 1:
            raise ValueError("min_quote_tokens must be an integer >= 1")
        self.min_quote_tokens = min_quote_tokens

    def score(self, item: Item, final_answer: AnswerCandidate) -> ScoreResult:
        answers = item.gold.get("answers")
        if not isinstance(answers, list) or not answers or not all(isinstance(a, str) and a.strip() for a in answers):
            raise SealQAScoringError(f"item {item.id!r}: gold['answers'] must be a non-empty list of strings")
        base: dict[str, Any] = {
            "rule_version": RULE_VERSION,
            "freshness": item.gold.get("freshness"),
            "search_results": item.gold.get("search_results"),
            "subsets": item.gold.get("subsets"),
        }
        if final_answer.status is AnswerStatus.ABSTAINED:
            return ScoreResult(self.name, LABEL_ABSTAINED, {"outcome": LABEL_ABSTAINED, **base})
        if final_answer.status is AnswerStatus.UNPARSEABLE:
            return ScoreResult(self.name, LABEL_UNPARSEABLE, {"outcome": LABEL_UNPARSEABLE, "reason": "unparseable_output", **base})
        quote_tokens = len(normalize_text(final_answer.quote or "").split())
        if quote_tokens < self.min_quote_tokens:
            return ScoreResult(
                self.name,
                LABEL_UNPARSEABLE,
                {"outcome": LABEL_UNPARSEABLE, "reason": "quote_too_short", "quote_tokens": quote_tokens,
                 "min_quote_tokens": self.min_quote_tokens, **base},
            )
        value = final_answer.value or ""
        has_gold = value_carries_gold(value, answers)
        exact = any(answer_normalize(normalize_text(value)) == answer_normalize(normalize_text(a)) for a in answers)
        label = LABEL_CORRECT if has_gold else LABEL_INCORRECT
        return ScoreResult(
            self.name,
            label,
            {"outcome": label, "value_has_gold": has_gold, "value_exact": exact, "quote_tokens": quote_tokens,
             "source_id": final_answer.source_id, **base},
        )


def layer3_outcome(record: TraceRecord) -> str:
    """The statistics' outcome category of a layer-3 record (see the module docstring)."""
    if record.error is not None:
        return "error"
    for score in record.scores:
        if score.scorer == SCORER_NAME:
            return LAYER3_OUTCOME_BY_LABEL.get(score.label, "error")  # "scorer_error" -> error
    raise SealQAScoringError(f"record {record.item_id!r} ({record.arm}, {record.model}) has no {SCORER_NAME} score")


def analyse_layer3(run_dirs: Sequence[str | Path], **kwargs: Any) -> dict[str, Any]:
    """``binding_gate.stats.analysis.analyse_run_dirs`` with the layer-3 outcome rule."""
    from binding_gate.stats.analysis import analyse_run_dirs

    analysis = analyse_run_dirs(run_dirs, outcome_of=layer3_outcome, correct_outcomes=LAYER3_CORRECT_OUTCOMES, **kwargs)
    # PR 16: name the rule in the output (H4 and its statuses are in each model's "hypotheses").
    analysis["outcome_rule"] = {
        "scorer": SCORER_NAME,
        "rule_version": RULE_VERSION,
        "mapping": dict(LAYER3_OUTCOME_BY_LABEL),
        "correct": LABEL_CORRECT,
        "m3": "correct final answers / all records (estimate arm/<arm>/correct_all)",
        "m4": "among gated_research records whose first attempt ended in RESEARCH, the share with a correct "
        "final answer (estimate arm/<arm>/m4)",
        "decision": "H4 (PREREGISTRATION.md section 11)",
    }
    return analysis


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m binding_gate.scorers.sealqa_answer")
    parser.add_argument("command", choices=["analyze"])
    parser.add_argument("run_dirs", nargs="+")
    parser.add_argument("--out", default=None, help="output directory (default: <first run dir>/analysis)")
    parser.add_argument("--no-figures", action="store_true")
    args = parser.parse_args(argv)
    from binding_gate.stats.analysis import write_outputs

    analysis = analyse_layer3(args.run_dirs)
    out = Path(args.out) if args.out else Path(args.run_dirs[0]) / "analysis"
    written = write_outputs(analysis, out, figures=not args.no_figures)
    print(json.dumps({"written": written, "summary": analysis["summary"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
