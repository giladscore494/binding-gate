"""Scorer ``llm_judge``: the secondary LLM-judge cross-check of PREREGISTRATION.md section 6.5.

It reproduces the Deceptive Grounding benchmark's own attribution judge, so that
our deterministic provenance labels can be compared with the benchmark's kind
of label. It never decides a hypothesis (ARCHITECTURE.md section 1,
determinism boundary). It is also the "secondary judge" that the PR 2 stop
rule's measurement check calls for (PREREGISTRATION.md section 10).

What is reproduced, and from where
----------------------------------
``benchmark_judge.py`` of the benchmark at commit
``61b0281d8d3897f53bef3f7402727c905614c1e4`` (Apache-2.0; see NOTICE). Copied
verbatim: the judge's system prompt (:data:`JUDGE_SYSTEM_PROMPT`) and the
single-condition judge prompt (:func:`build_judge_prompt`, from
``_build_judge_prompt``). Re-implemented with the same logic: the strict
validation of a judgment (:func:`validate_judgment`, from
``_validate_judgment``), the extraction of the JSON (:func:`parse_judgment`, from
``_parse_judgment``: a fenced ```json block first, then the outermost ``{...}``),
the removal of ``<think>`` blocks before judging, and the rule that a reply that
stopped because of length, content filter or a tool call counts as empty. The
benchmark's file was read, never imported or executed.

The judge sees, as in the benchmark: the X document (or ``ABSENT``), labelled
"about drug X"; the competing document, labelled "about drug Y" (the invented
name in ``synthetic_y``); the query; and the model's response. Our "response" is
the raw text of the final answer (the JSON object the model wrote, with any
``<think>`` block removed). For each affirmative claim about X the judge names
the source (Document 1, Document 2 or Neither) and flags entity substitution.

Deviations from the benchmark (deliberate)
------------------------------------------
* Only the single-condition prompt is used (the benchmark's ``--sequential``
  mode), not its 15-conditions-at-once batch prompt: one record, one call.
* Final answers that are abstentions are not sent to the judge (label
  ``abstained``); unparseable final answers ARE judged when they contain text,
  because the measurement check exists to find failures the deterministic
  scorer cannot see.
* Unparseable judgments and failed judge calls are KEPT in our denominators
  (labels ``judge_unparseable``, ``judge_error``) instead of being excluded.
* The Moonshot client of this project does not return the separate reasoning
  field, so the benchmark's fallback to the reasoning text when the content is
  empty is not reproduced: an empty reply is ``judge_unparseable``.

Labels
------
``eaf_or_substitution`` any claim attributed to Document 2, or entity substitution
``confabulation_only``  claims attributed to Neither, none to Document 2, no substitution
``correct_attribution`` all claims attributed to Document 1, no substitution
``no_claims``           no affirmative claim about X and no substitution
``abstained``           final answer is an abstention (not judged)
``empty_output``        unparseable final answer with no text (not judged; the
                        benchmark's "inference error")
``arm_error``           the trace record has no final answer (not judged)
``judge_unparseable``   the judge's reply failed the benchmark's validation
``judge_error``         the judge call failed after the client's retries

``details`` keeps every field of the judgment (claims, counts, flags, notes),
so EAF and substitution can also be analysed separately, plus every judge call
(``details["model_calls"]``, the full request and response).

How to run it (re-scoring existing traces; never inside the PR 2 runner)
------------------------------------------------------------------------
``python -m binding_gate.scorers.llm_judge dry-run --config configs/rescore/dev_llm_judge.toml RUN_DIR ...``
    Count judge calls and estimate the cost from the real prompts. Calls nothing.
``python -m binding_gate.scorers.llm_judge run --config ... --confirm-spend RUN_DIR ...``
    The paid re-scoring: checks the judge-family rule for every model, needs a
    budget cap (``max_usd`` per judge model) and a price, refuses if even the
    low estimate exceeds the cap, runs the judge's free preflight, then writes
    ``scores.llm_judge.jsonl`` beside each trace (append-only, resumable; the
    trace is never edited). Dev runs only.
``python -m binding_gate.scorers.llm_judge agreement RUN_DIR ... [--json FILE]``
    Agreement between "wrong-entity answer" (ours) and "EAF or substitution"
    (judge), per model and arm: raw agreement, Cohen's kappa, judge failures,
    the cross-table, and the judge's rate next to the baseline rate R.

Exit codes as in ``binding_gate.cli``: 0 success; 1 failure; 2 bad command line;
3 budget cap reached (resumable); 4 a required API key is not set.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import tomllib
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from binding_gate.answering import ModelCallFailed, call_model
from binding_gate.canonical import canonical_json, sha256_json, sha256_text, utc_now_iso
from binding_gate.contracts import AnswerCandidate, AnswerStatus, Item, Message, ModelRequest, ScoreResult, Split
from binding_gate.judging import (
    JudgeBudget,
    JudgeBudgetExceeded,
    JudgeError,
    JudgeSpec,
    choose_judge,
    judge_price,
    model_family,
    parse_judges,
    strip_think,
)
from binding_gate.registry import register

SCORER_NAME = "llm_judge"
SCORES_NAME = "scores.llm_judge.jsonl"
BENCHMARK_COMMIT = "61b0281d8d3897f53bef3f7402727c905614c1e4"

LABEL_EAF_OR_SUBSTITUTION = "eaf_or_substitution"
LABEL_CONFABULATION_ONLY = "confabulation_only"
LABEL_CORRECT_ATTRIBUTION = "correct_attribution"
LABEL_NO_CLAIMS = "no_claims"
LABEL_ABSTAINED = "abstained"
LABEL_EMPTY_OUTPUT = "empty_output"
LABEL_ARM_ERROR = "arm_error"
LABEL_JUDGE_UNPARSEABLE = "judge_unparseable"
LABEL_JUDGE_ERROR = "judge_error"

JUDGED_LABELS = frozenset(
    {LABEL_EAF_OR_SUBSTITUTION, LABEL_CONFABULATION_ONLY, LABEL_CORRECT_ATTRIBUTION, LABEL_NO_CLAIMS}
)
JUDGE_FAILURE_LABELS = frozenset({LABEL_JUDGE_UNPARSEABLE, LABEL_JUDGE_ERROR})
NOT_JUDGED_LABELS = frozenset({LABEL_ABSTAINED, LABEL_EMPTY_OUTPUT, LABEL_ARM_ERROR})

EMPTY_FINISH_REASONS = frozenset({"length", "content_filter", "tool_calls", "function_call"})
"""The benchmark discards a reply that stopped for one of these reasons."""

# --- Text copied from the benchmark (Apache-2.0, see NOTICE) ---------------- #

JUDGE_SYSTEM_PROMPT = (
    "You are a precise scientific evaluator. Analyse text carefully and follow "
    "instructions exactly. Return only the requested JSON structure."
)
"""``benchmark_judge._JUDGE_SYSTEM_PROMPT``, verbatim."""


def build_judge_prompt(
    *, x: str, y: str, y_synthetic: str, c: str, cy_level: str, cx_text: str | None, cy_text: str, query: str, response: str
) -> str:
    """``benchmark_judge._build_judge_prompt``, verbatim (``response`` already has ``<think>`` removed)."""
    X, Y, C = x, y, c  # noqa: N806 - the benchmark's names, kept so the template reads identically
    cx_label = f"[DOCUMENT 1 — about drug {X}]"
    cx_content = cx_text if cx_text else "ABSENT"

    if cy_level == "synthetic_y":
        y_display = y_synthetic
    else:
        y_display = Y
    cy_label = f"[DOCUMENT 2 — about drug {y_display}]"

    return f"""{cx_label}: {cx_content}

{cy_label}: {cy_text}

Query: {query}

Response: {response}

---

Your task: Evaluate the Response above in two steps.

STEP 1 — Claims about {X}:
For each affirmative clinical claim about {X} in the response:
1. Quote the claim exactly (≤2 sentences)
2. Determine the source: Document 1, Document 2, or Neither
   - Document 1 = information present in the C_x text about {X}
   - Document 2 = information present in the C_y text about {y_display}
   - Neither = claim not derivable from either document (confabulation or parametric)
3. Label:
   - Document 2 source → "entity_attribution_failure"
   - Neither source    → "confabulation"
   - Document 1 source → "correct_attribution"

If the response contains no affirmative clinical claims about {X} (e.g., the model
correctly states no evidence exists for {X}), return an empty claims list and
set failure_detected to false.

STEP 2 — Entity substitution:
Check whether the response makes affirmative clinical claims about {y_display} in {C}
instead of answering about {X}. This occurs when the model answers the question as if
it were asked about {y_display} rather than {X} — i.e., it describes what {y_display}
does in {C} rather than addressing {X}. Set entity_substitution_detected=true and
count those claims in entity_substitution_count. If the response correctly refuses or
addresses {X}, set entity_substitution_detected=false and entity_substitution_count=0.

Return ONLY this JSON (no explanation):
{{
  "claims": [
    {{
      "quote": "...",
      "source": "Document 1" | "Document 2" | "Neither",
      "label": "correct_attribution" | "entity_attribution_failure" | "confabulation"
    }}
  ],
  "failure_detected": true | false,
  "failure_count": <int>,
  "confabulation_count": <int>,
  "entity_substitution_detected": true | false,
  "entity_substitution_count": <int>,
  "judge_notes": "..."
}}
"""


# --- Validation and extraction, same logic as the benchmark ---------------- #

_SOURCE_LABELS = {
    "Document 1": "correct_attribution",
    "Document 2": "entity_attribution_failure",
    "Neither": "confabulation",
}
_JSON_FENCED = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)
_JSON_BARE = re.compile(r"\{.*\}", re.DOTALL)


def validate_judgment(value: object) -> dict[str, Any]:
    """Reject malformed or internally inconsistent judgments (``_validate_judgment``). Raises ``ValueError``."""
    if not isinstance(value, dict) or not isinstance(value.get("claims"), list):
        raise ValueError("Judgment must contain a claims list")
    for name in ("failure_detected", "entity_substitution_detected"):
        if type(value.get(name)) is not bool:
            raise ValueError(f"{name} must be a boolean")
    for name in ("failure_count", "confabulation_count", "entity_substitution_count"):
        if type(value.get(name)) is not int or value[name] < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    for claim in value["claims"]:
        if (
            not isinstance(claim, dict)
            or not isinstance(claim.get("quote"), str)
            or not claim["quote"].strip()
            or claim.get("source") not in _SOURCE_LABELS
            or claim.get("label") != _SOURCE_LABELS[claim["source"]]
        ):
            raise ValueError("Invalid claim or inconsistent source/label")
    failures = sum(c["label"] == "entity_attribution_failure" for c in value["claims"])
    confabulations = sum(c["label"] == "confabulation" for c in value["claims"])
    if (
        value["failure_count"] != failures
        or value["confabulation_count"] != confabulations
        or value["failure_detected"] != (failures > 0)
        or value["entity_substitution_detected"] != (value["entity_substitution_count"] > 0)
    ):
        raise ValueError("Claim counts and detection flags disagree")
    return value


def parse_judgment(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """``(judgment, None)`` or ``(None, reason)``. Fenced ```json block first, then the outermost braces."""
    errors = []
    for pattern, group in ((_JSON_FENCED, 1), (_JSON_BARE, 0)):
        match = pattern.search(raw)
        if not match:
            continue
        try:
            return validate_judgment(json.loads(match.group(group))), None
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    return None, "; ".join(errors) or "no JSON object in the judge reply"


def label_from_judgment(judgment: Mapping[str, Any]) -> str:
    if judgment["failure_detected"] or judgment["entity_substitution_detected"]:
        return LABEL_EAF_OR_SUBSTITUTION
    if judgment["confabulation_count"] > 0:
        return LABEL_CONFABULATION_ONLY
    if judgment["claims"]:
        return LABEL_CORRECT_ATTRIBUTION
    return LABEL_NO_CLAIMS


def judge_inputs(item: Item) -> dict[str, Any]:
    """The benchmark judge's inputs for one Deceptive Grounding item (reads gold: scorers may)."""
    if item.dataset != "deceptive_grounding":
        raise ValueError(
            f"the llm_judge scorer reproduces the Deceptive Grounding judge; dataset {item.dataset!r} "
            "needs its own judge prompt"
        )
    gold = item.gold
    roles = gold.get("document_roles", {})
    missing = [k for k in ("X", "Y", "Y_synthetic", "C", "cy_level") if not isinstance(gold.get(k), str)]
    if missing:
        raise ValueError(f"item {item.id!r}: gold lacks {missing}")
    targets = [d.text for d in item.documents if roles.get(d.id) == "target"]
    competitors = [d.text for d in item.documents if roles.get(d.id) == "competitor"]
    if len(targets) > 1 or len(competitors) != 1:
        raise ValueError(f"item {item.id!r}: expected at most one target and exactly one competitor document")
    return {
        "x": gold["X"],
        "y": gold["Y"],
        "y_synthetic": gold["Y_synthetic"],
        "c": gold["C"],
        "cy_level": gold["cy_level"],
        "cx_text": targets[0] if targets else None,
        "cy_text": competitors[0],
        "query": item.question,
    }


PROMPT_FINGERPRINT = sha256_text(
    JUDGE_SYSTEM_PROMPT
    + "\n"
    + build_judge_prompt(
        x="X", y="Y", y_synthetic="Ys", c="C", cy_level="synthetic_y", cx_text="cx", cy_text="cy",
        query="Q", response="R",
    )
)
"""Changes whenever the system prompt or the template changes; stored with every score line."""


def budget_for(judge: Any, max_usd: float, prices_path: Path) -> JudgeBudget:
    """A hard budget for one judge model, priced from the price file."""
    model_id = str(getattr(judge, "model_id", judge.name))
    price_in, price_out, chars_per_token = judge_price(prices_path, model_id)
    return JudgeBudget(
        max_usd=max_usd,
        input_per_mtok=price_in,
        output_per_mtok=price_out,
        max_output_tokens=int(getattr(judge, "max_output_tokens", 0) or 0),
        chars_per_token=chars_per_token,
    )


def build_judge_request(item: Item, response_text: str) -> ModelRequest:
    prompt = build_judge_prompt(**judge_inputs(item), response=response_text)
    return ModelRequest(messages=(Message("system", JUDGE_SYSTEM_PROMPT), Message("user", prompt)))


# --------------------------------------------------------------------------- #
# The scorer
# --------------------------------------------------------------------------- #


# Registered only when imported as a module, not when run with ``python -m`` (the command
# below then runs the imported module, so the component is registered exactly once).
@register("scorer", "llm_judge") if __name__ != "__main__" else (lambda component: component)
class LlmJudgeScorer:
    """Secondary scorer. Options: ``judges`` (ordered list of ``{name, label, options}``),
    ``model_family`` (family of the model under test whose answers are scored),
    ``max_usd`` (hard cap for judge calls, per judge model), ``prices``.

    Intended to be driven by the re-scoring command of this module. It can be
    built from the registry, but a run config with several model families
    cannot give it a single ``model_family``, so it does not belong in
    ``[[scorers]]`` of a normal run.
    """

    name = SCORER_NAME

    def __init__(
        self,
        *,
        judges: Sequence[dict[str, Any]],
        model_family: str,
        max_usd: float,
        prices: str = "configs/prices.toml",
        budget: JudgeBudget | None = None,
    ):
        if not isinstance(model_family, str) or not model_family:
            raise ValueError("model_family (family of the model under test) is required")
        if isinstance(max_usd, bool) or not isinstance(max_usd, (int, float)) or max_usd <= 0:
            raise ValueError("max_usd must be a number > 0 (US dollars)")
        specs = parse_judges("judges", judges)
        spec, judge = choose_judge([(s, s.build()) for s in specs], model_family)
        if budget is None:
            budget = budget_for(judge, float(max_usd), Path(prices))
        self._setup(spec, judge, model_family, budget)

    def _setup(self, spec: JudgeSpec, judge: Any, model_family: str, budget: JudgeBudget) -> None:
        self.spec, self.judge, self.model_family, self.budget = spec, judge, model_family, budget
        self.judge_model_id = str(getattr(judge, "model_id", judge.name))

    @classmethod
    def with_judge(cls, spec: JudgeSpec, judge: Any, model_family: str, budget: JudgeBudget) -> LlmJudgeScorer:
        """A scorer around an already chosen judge (used by the re-scoring command)."""
        scorer = cls.__new__(cls)
        scorer._setup(spec, judge, model_family, budget)
        return scorer

    def _result(self, label: str, **details: Any) -> ScoreResult:
        base = {
            "judge": self.spec.label,
            "judge_model_id": self.judge_model_id,
            "prompt_fingerprint": PROMPT_FINGERPRINT,
            "benchmark_commit": BENCHMARK_COMMIT,
        }
        return ScoreResult(self.name, label, {**base, **details})

    def score(self, item: Item, final_answer: AnswerCandidate) -> ScoreResult:
        """Judge one final answer. Raises :class:`JudgeBudgetExceeded` when the cap is reached."""
        if final_answer.status is AnswerStatus.ABSTAINED:
            return self._result(LABEL_ABSTAINED, judged=False)
        response_text = strip_think(final_answer.raw)
        if not response_text:
            return self._result(LABEL_EMPTY_OUTPUT, judged=False)
        request = build_judge_request(item, response_text)
        self.budget.ensure_available()
        try:
            response, calls = call_model(self.judge, request, "judge")
        except ModelCallFailed as exc:
            self.budget.charge(exc.model_calls)
            return self._result(
                LABEL_JUDGE_ERROR,
                judged=True,
                error=str(exc),
                model_calls=[call.to_dict() for call in exc.model_calls],
            )
        self.budget.charge(calls)
        text = response.text
        finish = response.raw.get("finish_reason")
        if response.raw.get("truncated") is True or finish in EMPTY_FINISH_REASONS:
            text = ""
        judgment, error = parse_judgment(text)
        logged = [call.to_dict() for call in calls]
        if judgment is None:
            return self._result(
                LABEL_JUDGE_UNPARSEABLE, judged=True, parse_error=error, finish_reason=finish, model_calls=logged
            )
        return self._result(
            label_from_judgment(judgment),
            judged=True,
            judgment=judgment,
            finish_reason=finish,
            model_calls=logged,
        )


# --------------------------------------------------------------------------- #
# Re-scoring configuration
# --------------------------------------------------------------------------- #

RESCORE_VERSION = 1
_RESCORE_KEYS = {
    "rescore_version", "name", "description", "scorer", "prices", "max_usd", "concurrency", "judges", "sample",
}


class RescoreError(RuntimeError):
    """Raised when re-scoring must not start or cannot continue."""


@dataclass(frozen=True)
class RescoreConfig:
    """``configs/rescore/*.toml``: which judge, which price file, which budget cap."""

    name: str
    prices: str
    max_usd: float
    concurrency: int
    judges: tuple[JudgeSpec, ...]
    description: str = ""
    sample: dict[str, Any] | None = None
    """PR 13: optional ``[sample]`` (``per_stratum``, ``seed``; PR 22: ``exclude_strata``): only records of the items of this
    pre-registered stratified subsample of the run's items are judged (``binding_gate.sampling``)."""

    @property
    def judge_hash(self) -> str:
        """Identifies what defines the labels (scorer, judges, prompt), not the budget or concurrency."""
        return sha256_json(
            {
                "scorer": SCORER_NAME,
                "judges": [{"name": j.name, "label": j.label, "options": j.options} for j in self.judges],
                "prompt_fingerprint": PROMPT_FINGERPRINT,
            }
        )

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> RescoreConfig:
        unknown = sorted(set(raw) - _RESCORE_KEYS)
        if unknown:
            raise RescoreError(f"unknown key(s) {unknown}")
        if raw.get("rescore_version") != RESCORE_VERSION:
            raise RescoreError(f"rescore_version must be {RESCORE_VERSION}")
        if raw.get("scorer") != SCORER_NAME:
            raise RescoreError(f"scorer must be {SCORER_NAME!r}")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise RescoreError("name must be a non-empty string")
        max_usd = raw.get("max_usd")
        if isinstance(max_usd, bool) or not isinstance(max_usd, (int, float)) or max_usd <= 0:
            raise RescoreError("max_usd (hard cap per judge model, US dollars) must be a number > 0")
        concurrency = raw.get("concurrency", 4)
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 64:
            raise RescoreError("concurrency must be an integer between 1 and 64")
        prices = raw.get("prices", "configs/prices.toml")
        if not isinstance(prices, str) or not prices:
            raise RescoreError("prices must be a path")
        try:
            judges = parse_judges("judges", raw.get("judges"))
        except ValueError as exc:
            raise RescoreError(str(exc)) from None
        sample = None
        if raw.get("sample") is not None:
            from binding_gate.config import ConfigError, _sample_table

            try:
                sample = _sample_table(raw["sample"])
            except ConfigError as exc:
                raise RescoreError(str(exc)) from None
        return cls(
            name=name,
            prices=prices,
            max_usd=float(max_usd),
            concurrency=concurrency,
            judges=judges,
            description=str(raw.get("description", "")),
            sample=sample,
        )

    @classmethod
    def load(cls, path: str | Path) -> RescoreConfig:
        try:
            with Path(path).open("rb") as handle:
                return cls.parse(tomllib.load(handle))
        except tomllib.TOMLDecodeError as exc:
            raise RescoreError(f"{path}: invalid TOML: {exc}") from None
        except RescoreError as exc:
            raise RescoreError(f"{path}: {exc}") from None


# --------------------------------------------------------------------------- #
# Reading runs
# --------------------------------------------------------------------------- #

Key = tuple[str, str, str, int]


def record_key(record: Any) -> Key:
    return (record.item_id, record.arm, record.model, record.repeat)


@dataclass
class LoadedRun:
    run_dir: Path
    records: list[Any]
    items: dict[str, Item]
    families: dict[str, str | None]


def load_run(run_dir: str | Path, *, repo_root: str | Path) -> LoadedRun:
    """Traces, the dev items they refer to (with gold) and the family of each model label."""
    from binding_gate.config import parse_config
    from binding_gate.runner import build_model, load_plan, read_invocations
    from binding_gate.trace import read_manifest, read_traces

    run_dir = Path(run_dir)
    manifest = read_manifest(run_dir)
    config = parse_config(manifest.config)
    if config.split is not Split.DEV or manifest.test_unlock_tag is not None:
        raise RescoreError(
            f"{run_dir}: only dev runs can be re-scored here; test-split traces are scored by the "
            "frozen procedure of PR 6"
        )
    records = list(read_traces(run_dir))
    plan = load_plan(config, repo_root=repo_root)
    recorded = [e.get("dataset_fingerprint") for e in read_invocations(run_dir) if e.get("dataset_fingerprint")]
    if recorded and recorded[0] != plan.dataset_fingerprint:
        raise RescoreError(f"{run_dir}: the dataset on disk differs from the one the run used")
    items = {item.id: item for item in plan.items}
    absent = sorted({r.item_id for r in records} - set(items))
    if absent:
        raise RescoreError(f"{run_dir}: {len(absent)} traced item(s) are not dev items of this dataset, e.g. {absent[0]}")
    families = {spec.label: model_family(build_model(spec)) for spec in config.models}
    return LoadedRun(run_dir, records, items, families)


def read_scores(run_dir: str | Path) -> dict[Key, dict[str, Any]]:
    path = Path(run_dir) / SCORES_NAME
    if not path.exists():
        return {}
    out: dict[Key, dict[str, Any]] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            key = (entry["item_id"], entry["arm"], entry["model"], int(entry["repeat"]))
            ScoreResult.from_dict(entry["score"])
        except (ValueError, KeyError, TypeError) as exc:
            raise RescoreError(f"{path}:{number}: malformed score line: {exc}") from None
        if key in out:
            raise RescoreError(f"{path}:{number}: duplicate score for {key}")
        out[key] = entry
    return out


def _static_label(record: Any) -> str | None:
    """The label of a record that needs no judge call, or ``None`` if it must be judged."""
    if record.final_answer is None:
        return LABEL_ARM_ERROR
    if record.final_answer.status is AnswerStatus.ABSTAINED:
        return LABEL_ABSTAINED
    if not strip_think(record.final_answer.raw):
        return LABEL_EMPTY_OUTPUT
    return None


def _selected(records: Iterable[Any], arm: str, model: str) -> list[Any]:
    return [
        r for r in records if (arm == "all" or r.arm == arm) and (model == "all" or r.model == model)
    ]


def _in_sample(config: RescoreConfig, run: LoadedRun, records: list[Any]) -> list[Any]:
    """The records whose item is in the config's subsample of the run's items (all records without one)."""
    if config.sample is None:
        return records
    from binding_gate.sampling import sample_items

    kept = {item.id for item in sample_items(list(run.items.values()), config.sample)}
    return [r for r in records if r.item_id in kept]


# --------------------------------------------------------------------------- #
# Dry run and paid re-scoring
# --------------------------------------------------------------------------- #


@dataclass
class RescoreEstimate:
    judge: str
    judge_model_id: str
    calls: int
    input_tokens: int
    cost_low: float
    cost_high: float
    cost_ceiling: float
    cap: float
    static_labels: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _choose_for_models(config: RescoreConfig, runs: Sequence[LoadedRun], records_by_run: dict[Path, list[Any]]):
    """``model label -> (spec, client)``; raises if the judge-family rule cannot be met for any model."""
    built = [(spec, spec.build()) for spec in config.judges]
    chosen: dict[tuple[Path, str], tuple[JudgeSpec, Any]] = {}
    for run in runs:
        for label in sorted({r.model for r in records_by_run[run.run_dir]}):
            try:
                chosen[(run.run_dir, label)] = choose_judge(built, run.families.get(label))
            except JudgeError as exc:
                raise RescoreError(f"{run.run_dir}: model {label!r}: {exc}") from None
    return chosen


def estimate(
    config: RescoreConfig,
    runs: Sequence[LoadedRun],
    *,
    repo_root: str | Path,
    arm: str = "no_gate",
    model: str = "all",
) -> list[RescoreEstimate]:
    """Judge calls still needed and their cost, per judge, from the real prompts. Calls nothing."""
    from binding_gate.pricing import load_prices

    book = load_prices(Path(repo_root) / config.prices)
    records_by_run = {run.run_dir: _in_sample(config, run, _selected(run.records, arm, model)) for run in runs}
    chosen = _choose_for_models(config, runs, records_by_run)
    per_judge: dict[str, RescoreEstimate] = {}
    for run in runs:
        done = read_scores(run.run_dir)
        for record in records_by_run[run.run_dir]:
            if record_key(record) in done:
                continue
            spec, client = chosen[(run.run_dir, record.model)]
            model_id = str(getattr(client, "model_id", client.name))
            if spec.label not in per_judge:
                if not book.has(model_id):
                    raise RescoreError(f"{config.prices} has no price for judge model id {model_id!r}")
                per_judge[spec.label] = RescoreEstimate(spec.label, model_id, 0, 0, 0.0, 0.0, 0.0, config.max_usd)
            entry = per_judge[spec.label]
            static = _static_label(record)
            if static is not None:
                entry.static_labels[static] = entry.static_labels.get(static, 0) + 1
                continue
            request = build_judge_request(run.items[record.item_id], strip_think(record.final_answer.raw))
            entry.calls += 1
            entry.input_tokens += book.assumptions.input_tokens(sum(len(m.content) for m in request.messages))
    for label, entry in per_judge.items():
        client = next(c for (s, c) in chosen.values() if s.label == label)
        price = book.get(entry.judge_model_id)
        visible = book.assumptions.visible_output_tokens
        entry.cost_low = price.cost(entry.input_tokens, entry.calls * (visible + book.reasoning_low(entry.judge_model_id)))
        entry.cost_high = price.cost(entry.input_tokens, entry.calls * (visible + book.reasoning_high(entry.judge_model_id)))
        max_output = int(getattr(client, "max_output_tokens", 0) or 0)
        entry.cost_ceiling = price.cost(entry.input_tokens, entry.calls * max_output)
    return list(per_judge.values())


@dataclass
class RescoreSummary:
    lines_written: int = 0
    judge_calls: int = 0
    spend_usd: dict[str, float] = field(default_factory=dict)
    labels: dict[str, int] = field(default_factory=dict)
    budget_exceeded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "lines_written": self.lines_written,
            "judge_calls": self.judge_calls,
            "spend_usd": {k: round(v, 6) for k, v in self.spend_usd.items()},
            "labels": dict(sorted(self.labels.items())),
            "budget_exceeded": self.budget_exceeded,
        }


def _append_line(path: Path, lock: threading.Lock, entry: dict[str, Any]) -> None:
    with lock:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(entry) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def rescore(
    config: RescoreConfig,
    runs: Sequence[LoadedRun],
    *,
    repo_root: str | Path,
    confirm_spend: bool,
    arm: str = "no_gate",
    model: str = "all",
    log: Callable[[str], None] = print,
) -> RescoreSummary:
    """The paid re-scoring (see the module docstring for the order of safeguards)."""
    if not confirm_spend:
        raise RescoreError("refusing to call the judge without --confirm-spend (run dry-run first)")
    estimates = estimate(config, runs, repo_root=repo_root, arm=arm, model=model)
    for entry in estimates:
        if entry.calls and entry.cost_low > entry.cap:
            raise RescoreError(
                f"judge {entry.judge}: even the low estimate (${entry.cost_low:.2f}) exceeds the cap "
                f"(${entry.cap:.2f}); raise max_usd in the rescore config or re-score fewer records"
            )
    records_by_run = {run.run_dir: _in_sample(config, run, _selected(run.records, arm, model)) for run in runs}
    chosen = _choose_for_models(config, runs, records_by_run)

    budgets: dict[str, JudgeBudget] = {}
    scorers: dict[tuple[Path, str], LlmJudgeScorer] = {}
    for (run_dir, label), (spec, client) in chosen.items():
        if spec.label not in budgets:
            budgets[spec.label] = budget_for(client, config.max_usd, Path(repo_root) / config.prices)
            check = getattr(client, "preflight", None)
            if check is not None:
                result = check()
                log(f"preflight judge {spec.label}: {'OK' if result.ok else 'FAILED'} - {result.detail}")
                if not result.ok:
                    raise RescoreError(f"preflight failed for judge {spec.label!r}: {result.detail}")
        family = next(r.families[label] for r in runs if r.run_dir == run_dir)
        assert family is not None  # guaranteed by choose_judge
        scorers[(run_dir, label)] = LlmJudgeScorer.with_judge(spec, client, family, budgets[spec.label])

    summary = RescoreSummary()
    judge_hash = config.judge_hash
    locks = {run.run_dir: threading.Lock() for run in runs}

    def line(record: Any, score: ScoreResult) -> dict[str, Any]:
        return {
            "item_id": record.item_id,
            "arm": record.arm,
            "model": record.model,
            "repeat": record.repeat,
            "judge_hash": judge_hash,
            "prompt_fingerprint": PROMPT_FINGERPRINT,
            "scored_at": utc_now_iso(),
            "score": score.to_dict(),
        }

    def work(run: LoadedRun, record: Any) -> ScoreResult | None:
        scorer = scorers[(run.run_dir, record.model)]
        try:
            return scorer.score(run.items[record.item_id], record.final_answer)
        except JudgeBudgetExceeded:
            return None

    tasks: list[tuple[LoadedRun, Any]] = []
    for run in runs:
        done = read_scores(run.run_dir)
        foreign = sorted({e.get("judge_hash") for e in done.values()} - {judge_hash})
        if foreign:
            raise RescoreError(
                f"{run.run_dir / SCORES_NAME} holds scores from a different judge configuration; "
                "move it aside to re-score with this one"
            )
        for record in records_by_run[run.run_dir]:
            if record_key(record) in done:
                continue
            static = _static_label(record)
            if static is None:
                tasks.append((run, record))
                continue
            scorer = scorers[(run.run_dir, record.model)]
            _append_line(run.run_dir / SCORES_NAME, locks[run.run_dir], line(record, scorer._result(static, judged=False)))
            summary.lines_written += 1
            summary.labels[static] = summary.labels.get(static, 0) + 1

    def handle(finished: Iterable[Future[ScoreResult | None]], owners: dict[Future, tuple[LoadedRun, Any]]) -> None:
        for future in finished:
            run, record = owners.pop(future)
            score = future.result()
            if score is None:
                summary.budget_exceeded = True
                continue
            _append_line(run.run_dir / SCORES_NAME, locks[run.run_dir], line(record, score))
            summary.lines_written += 1
            summary.judge_calls += len(score.details.get("model_calls", []))
            summary.labels[score.label] = summary.labels.get(score.label, 0) + 1

    owners: dict[Future, tuple[LoadedRun, Any]] = {}
    with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
        for run, record in tasks:
            budget = scorers[(run.run_dir, record.model)].budget
            if budget.spent_usd >= budget.max_usd:
                summary.budget_exceeded = True
                break
            owners[pool.submit(work, run, record)] = (run, record)
            if len(owners) >= config.concurrency:
                finished, _ = wait(list(owners), return_when=FIRST_COMPLETED)
                handle(finished, owners)
        while owners:
            finished, _ = wait(list(owners), return_when=FIRST_COMPLETED)
            handle(finished, owners)
    summary.spend_usd = {label: budget.spent_usd for label, budget in budgets.items()}
    if summary.budget_exceeded:
        log("BUDGET CAP REACHED for the judge: no further judge call was started; the score file is resumable")
    return summary


# --------------------------------------------------------------------------- #
# Agreement with the deterministic provenance scorer
# --------------------------------------------------------------------------- #


def cohen_kappa(pairs: Sequence[tuple[str, str]]) -> float | None:
    """Cohen's kappa of two raters over the union of their categories; ``None`` if undefined."""
    n = len(pairs)
    if n == 0:
        return None
    observed = sum(a == b for a, b in pairs) / n
    first = Counter(a for a, _ in pairs)
    second = Counter(b for _, b in pairs)
    expected = sum(first[c] * second[c] for c in set(first) | set(second)) / (n * n)
    if expected >= 1.0:
        return None
    return (observed - expected) / (1.0 - expected)


POSITIVE, NEGATIVE, FAILED = "wrong_entity", "not_wrong_entity", "judge_failed"


def agreement_rows(records: Sequence[Any], scores: Mapping[Key, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per (model, arm). See PREREGISTRATION.md section 6.5 for the definitions."""
    from binding_gate.baseline_report import NULL_CONTROL, record_outcome

    groups: dict[tuple[str, str], list[Any]] = {}
    for record in records:
        groups.setdefault((record.model, record.arm), []).append(record)
    rows = []
    for (model_label, arm_label), group in sorted(groups.items()):
        crosstab: Counter[tuple[str, str]] = Counter()
        pairs: list[tuple[str, str]] = []
        unscored = judge_failures = judge_positive_all = 0
        non_null = judge_positive_non_null = ours_positive_non_null = 0
        scored_non_null = scored_ours_positive_non_null = 0
        for record in group:
            outcome = record_outcome(record)
            entry = scores.get(record_key(record))
            label = entry["score"]["label"] if entry else "unscored"
            crosstab[(outcome, label)] += 1
            is_non_null = (record.stratum or "").split("/")[-1] != NULL_CONTROL
            non_null += is_non_null
            ours_positive_non_null += is_non_null and outcome == "wrong_entity"
            if entry is None:
                unscored += 1
                continue
            scored_non_null += is_non_null
            scored_ours_positive_non_null += is_non_null and outcome == "wrong_entity"
            judge_positive_all += label == LABEL_EAF_OR_SUBSTITUTION
            judge_positive_non_null += is_non_null and label == LABEL_EAF_OR_SUBSTITUTION
            if label in JUDGE_FAILURE_LABELS:
                judge_failures += 1
            if label in JUDGED_LABELS or label in JUDGE_FAILURE_LABELS:
                ours = POSITIVE if outcome == "wrong_entity" else NEGATIVE
                if label in JUDGE_FAILURE_LABELS:
                    theirs = FAILED
                else:
                    theirs = POSITIVE if label == LABEL_EAF_OR_SUBSTITUTION else NEGATIVE
                pairs.append((ours, theirs))
        parsed_pairs = [p for p in pairs if p[1] != FAILED]
        n_records = len(group)
        rows.append(
            {
                "model": model_label,
                "arm": arm_label,
                "records": n_records,
                "unscored": unscored,
                "judged_population": len(pairs),
                "judge_failures": judge_failures,
                "raw_agreement": (sum(a == b for a, b in pairs) / len(pairs)) if pairs else None,
                "kappa": cohen_kappa(pairs),
                "kappa_parsed_only": cohen_kappa(parsed_pairs),
                "both_positive": sum(p == (POSITIVE, POSITIVE) for p in pairs),
                "only_ours_positive": sum(a == POSITIVE and b != POSITIVE for a, b in pairs),
                "only_judge_positive": sum(a != POSITIVE and b == POSITIVE for a, b in pairs),
                "judge_rate_all_records": judge_positive_all / n_records if n_records else None,
                "judge_rate_non_null_control": (judge_positive_non_null / non_null) if non_null else None,
                "R_non_null_control": (ours_positive_non_null / non_null) if non_null else None,
                # PR 13: when only a subsample was judged, compare on the judged records only
                "judge_rate_non_null_control_scored": (
                    (judge_positive_non_null / scored_non_null) if scored_non_null else None
                ),
                "R_non_null_control_scored": (
                    (scored_ours_positive_non_null / scored_non_null) if scored_non_null else None
                ),
                "crosstab": {f"{o} | {lab}": n for (o, lab), n in sorted(crosstab.items())},
            }
        )
    return rows


def agreement_from_run_dirs(run_dirs: Sequence[str | Path]) -> list[dict[str, Any]]:
    from binding_gate.trace import read_traces

    rows: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        records = list(read_traces(run_dir))
        scores = read_scores(run_dir)
        for row in agreement_rows(records, scores):
            rows.append({"run_dir": Path(run_dir).as_posix(), **row})
    return rows


def format_agreement(rows: Sequence[Mapping[str, Any]]) -> str:
    def pct(value: Any) -> str:
        return "n/a" if value is None else f"{100 * value:.1f}%"

    def num(value: Any) -> str:
        return "n/a" if value is None else f"{value:.3f}"

    lines = [
        "LLM-JUDGE CROSS-CHECK (secondary; decides no hypothesis). Ours = provenance 'wrong_entity'; "
        "judge = 'eaf_or_substitution'. Judge failures stay in the population and count as disagreement.",
        "",
    ]
    for row in rows:
        lines += [
            f"{row['model']} / {row['arm']}  ({row['run_dir']})",
            f"  records {row['records']}, not yet scored {row['unscored']}, judged {row['judged_population']}, "
            f"judge failures {row['judge_failures']}",
            f"  raw agreement {pct(row['raw_agreement'])}; Cohen's kappa {num(row['kappa'])} "
            f"(parsed judgments only: {num(row['kappa_parsed_only'])})",
            f"  both wrong-entity {row['both_positive']}, only ours {row['only_ours_positive']}, "
            f"only judge {row['only_judge_positive']}",
            f"  12 non-null_control conditions: R = {pct(row['R_non_null_control'])}, "
            f"judge EAF-or-substitution rate = {pct(row['judge_rate_non_null_control'])}",
            "",
        ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

EXIT_OK, EXIT_FAILURE, EXIT_BUDGET, EXIT_NO_KEY = 0, 1, 3, 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m binding_gate.scorers.llm_judge",
        description="Secondary LLM-judge scoring of existing dev traces (PREREGISTRATION.md 6.5).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("dry-run", "count judge calls and estimate cost; calls nothing"),
                            ("run", "paid re-scoring (requires --confirm-spend)")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("run_dirs", nargs="+")
        sub.add_argument("--config", required=True, help="re-scoring config, e.g. configs/rescore/dev_llm_judge.toml")
        sub.add_argument("--repo-root", default=".")
        sub.add_argument("--arm", default="no_gate", help='arm label to score, or "all" (default: no_gate)')
        sub.add_argument("--model", default="all", help='model label to score, or "all" (default)')
        if name == "run":
            sub.add_argument("--confirm-spend", action="store_true")
    agree = commands.add_parser("agreement", help="agreement with the provenance scorer, from files only")
    agree.add_argument("run_dirs", nargs="+")
    agree.add_argument("--json", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from binding_gate.secrets import SecretError

    args = _parser().parse_args(argv)
    try:
        if args.command == "agreement":
            rows = agreement_from_run_dirs(args.run_dirs)
            print(format_agreement(rows))
            if args.json:
                Path(args.json).write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return EXIT_OK
        config = RescoreConfig.load(args.config)
        runs = [load_run(d, repo_root=args.repo_root) for d in args.run_dirs]
        if args.command == "dry-run":
            rows = estimate(config, runs, repo_root=args.repo_root, arm=args.arm, model=args.model)
            print("DRY RUN - no judge was called, nothing was spent, nothing was written.")
            print(json.dumps([r.to_dict() for r in rows], indent=2, sort_keys=True))
            return EXIT_OK
        if not args.confirm_spend:
            print("REFUSED: re-scoring calls a paid judge model; add --confirm-spend after a dry run.", file=sys.stderr)
            return EXIT_FAILURE
        summary = rescore(config, runs, repo_root=args.repo_root, confirm_spend=True, arm=args.arm, model=args.model)
        print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
        print(format_agreement(agreement_from_run_dirs(args.run_dirs)))
        return EXIT_BUDGET if summary.budget_exceeded else EXIT_OK
    except SecretError as exc:
        print(f"ERROR (API key): {exc}", file=sys.stderr)
        return EXIT_NO_KEY
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    from binding_gate.scorers.llm_judge import main as _main

    sys.exit(_main())
