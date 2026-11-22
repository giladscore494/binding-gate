"""Separate accuracy measurement of the decomposer (A) on a dev sample.

PREREGISTRATION.md section 7, "Decomposer (A), measured on its own", defines
what is measured; this module implements it. Entry point::

    python -m binding_gate.evaluation.decomposer dry-run --config configs/decomposer_eval_dev.toml
    python -m binding_gate.evaluation.decomposer run     --config configs/decomposer_eval_dev.toml --confirm-spend
    python -m binding_gate.evaluation.decomposer report  runs/<run directory>

``dry-run`` builds every request and estimates calls and cost; it calls no API,
needs no key and writes nothing. ``run`` is the paid measurement, with the same
safeguards as the main runner (``--confirm-spend``, a hard budget cap from the
config, a price for the model id, the low estimate under the cap, the free
preflight). ``report`` recomputes the metrics from a run directory's records.

What one run does
-----------------
1. Load the dataset named in the config and keep the dev split only
   (``split.select_dev``; the test split is never loaded, and a config with any
   other split is refused).
2. Group the dev items by question. The unit of the measurement is the
   distinct question: in Deceptive Grounding one question is shared by all 15
   conditions of every triple of an (X, C) pair, and the gate decomposes each
   question once per run.
3. Derive the reference decomposition of each question with the dataset's
   reference procedure (REFERENCE ONLY, it reads gold; see
   ``binding_gate.decomposers.deceptive_grounding_reference``).
4. Select the sample: rank the questions by
   ``SHA-256("<seed>\\n<question>")`` and keep the first ``size``
   (independent of file order and of Python's random module).
5. Ask the decomposer ``repeats`` times per question, without its cache (each
   repeat is an independent call), and compare each result with the reference
   using the matching rules below.

Matching rules (pre-registered)
-------------------------------
Every string is compared after ``textnorm.normalize_text`` (PREREGISTRATION.md
6.1). The *forms* of an entity or qualifier are its text plus its aliases.

* **Entity correct**: some form of the decomposer's entity equals some form of
  the reference entity, AND no form of the decomposer's entity equals a
  forbidden name (a competitor). ``name exact`` additionally requires the
  decomposer's ``name`` itself to equal the reference name.
* **Competitor collision**: a form of the decomposer's entity equals a forbidden
  name. Counted separately and always an entity error.
* **Qualifier match (exact)**: a predicted and a reference qualifier share a
  form. Kind is not required to agree; kind agreement is reported separately.
  Recall = matched reference qualifiers / reference qualifiers (all records; a
  failed decomposition matches nothing). Precision = matched predicted
  qualifiers / predicted qualifiers (parsed records).
* **Qualifier match (lenient, secondary)**: additionally, the word tokens
  (``\\w+``) of one form occur as a contiguous run in the other.
* **Presupposition agreement (exact)**: for every reference presupposition, one
  decomposer presupposition has, for each reference key term, a key term equal
  to it. A reference key term that names the entity is also matched by any form
  of a correct decomposer entity; one that names a reference qualifier is also
  matched by any form of a decomposer qualifier that matched it. Lenient
  (secondary): key terms may match by token containment as above.
* **Attribute**: recorded, not scored (open-ended question; no check reads it).
* **Failures** (unparseable reply, or a call that failed after retries) stay in
  every all-records denominator and count as wrong on every all-records metric.

How errors would propagate
--------------------------
Each record is assigned the consequence categories of :data:`CONSEQUENCES`. Each
category states the effect it would have on the gated arm *by the design of the
checks named in PREREGISTRATION.md section 1*; PR 4's checks determine the actual
effect. "Expected items affected per run" weights each record by the number of
dev items that share its question, divided by the number of repeats.

Output
------
``runs/decomposer-eval-<UTC stamp>-<config hash>-<random>/`` with

* ``manifest.json``   a ``RunManifest`` (config, config hash, git commit, ...);
  ``trace_count`` and ``traces_sha256`` refer to ``decompositions.jsonl``;
* ``decompositions.jsonl``  one record per question x repeat, appended and
  flushed as soon as it exists: question, status, failure reason, raw reply,
  parsed constraints, reference, forbidden names, comparison, consequences,
  every ``ModelCall`` and its cost;
* ``report.json``     the metrics, the threshold comparison, the spend.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import binding_gate
from binding_gate import gitinfo, registry, secrets
from binding_gate.answering import ModelCallFailed
from binding_gate.canonical import canonical_json, sha256_json, sha256_text, utc_now_iso
from binding_gate.config import ComponentSpec
from binding_gate.contracts import (
    Item,
    ModelCall,
    ModelRequest,
    QuestionConstraints,
    RunManifest,
    TokenUsage,
)
from binding_gate.decomposers.deceptive_grounding_reference import REFERENCES
from binding_gate.pricing import PriceBook, PricingError, load_prices
from binding_gate.secrets import SecretError
from binding_gate.split import select_dev
from binding_gate.testlock import ACCESS_LOG_RELPATH
from binding_gate.textnorm import normalize_text
from binding_gate.trace import new_run_id

EVAL_VERSION = 1
RECORD_VERSION = 1
MANIFEST_NAME = "manifest.json"
RECORDS_NAME = "decompositions.jsonl"
REPORT_NAME = "report.json"
RUN_PREFIX = "decomposer-eval"

STATUS_PARSED = "parsed"
STATUS_UNPARSEABLE = "unparseable"
STATUS_CALL_FAILED = "call_failed"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_BUDGET = 3
EXIT_NO_KEY = 4

Log = Callable[[str], None]


class EvalError(RuntimeError):
    """Raised when an evaluation must not start or its inputs are malformed."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

_TOP_KEYS = {
    "eval_version", "name", "description", "split", "repeats",
    "sample", "dataset", "reference", "decomposer", "runner", "thresholds",
}  # fmt: skip
_SAMPLE_KEYS = {"size", "seed"}
_RUNNER_KEYS = {"max_usd", "prices"}
_COMPONENT_KEYS = {"name", "options"}

THRESHOLD_KEYS = (
    "entity_accuracy_min",
    "failure_rate_max",
    "competitor_collisions_max",
    "qualifier_recall_min",
    "qualifier_precision_min",
    "presupposition_agreement_min",
)


@dataclass(frozen=True)
class Thresholds:
    """Proposed acceptance thresholds (PREREGISTRATION.md section 7). Rates are fractions."""

    entity_accuracy_min: float
    failure_rate_max: float
    competitor_collisions_max: int
    qualifier_recall_min: float
    qualifier_precision_min: float
    presupposition_agreement_min: float

    def to_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in THRESHOLD_KEYS}


@dataclass(frozen=True)
class EvalConfig:
    """A validated decomposer-evaluation config. Build with :func:`load_eval_config`."""

    name: str
    description: str
    repeats: int
    sample_size: int
    sample_seed: int
    dataset: ComponentSpec
    reference: str
    decomposer: ComponentSpec
    max_usd: float | None
    prices: str
    thresholds: Thresholds
    split: str = "dev"

    def to_dict(self) -> dict[str, Any]:
        """The normalised config; this exact structure is hashed and stored in the manifest."""
        runner: dict[str, Any] = {"prices": self.prices}
        if self.max_usd is not None:
            runner["max_usd"] = self.max_usd
        return {
            "eval_version": EVAL_VERSION,
            "name": self.name,
            "description": self.description,
            "split": self.split,
            "repeats": self.repeats,
            "sample": {"size": self.sample_size, "seed": self.sample_seed},
            "dataset": {"name": self.dataset.name, "options": self.dataset.options},
            "reference": {"name": self.reference},
            "decomposer": {"name": self.decomposer.name, "options": self.decomposer.options},
            "runner": runner,
            "thresholds": self.thresholds.to_dict(),
        }

    @property
    def config_hash(self) -> str:
        return sha256_json(self.to_dict())


def _table(where: str, value: Any, keys: set[str], *, required: set[str] | None = None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvalError(f"[{where}] must be a table")
    unknown = sorted(set(value) - keys)
    if unknown:
        raise EvalError(f"[{where}]: unknown key(s) {unknown}")
    missing = sorted((required if required is not None else keys) - set(value))
    if missing:
        raise EvalError(f"[{where}]: missing key(s) {missing}")
    return value


def _int(where: str, value: Any, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvalError(f"{where} must be an integer >= {minimum}")
    return value


def _fraction(where: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise EvalError(f"{where} must be a number between 0 and 1")
    return float(value)


def _component(where: str, value: Any) -> ComponentSpec:
    table = _table(where, value, _COMPONENT_KEYS, required={"name"})
    name = table["name"]
    if not isinstance(name, str) or not name:
        raise EvalError(f"[{where}].name must be a non-empty string")
    options = table.get("options", {})
    if not isinstance(options, Mapping):
        raise EvalError(f"[{where}].options must be a table")
    try:
        options = json.loads(canonical_json(options))
    except (TypeError, ValueError) as exc:
        raise EvalError(f"[{where}].options must be JSON-compatible: {exc}") from exc
    return ComponentSpec(name=name, label=name, options=options)


def parse_eval_config(raw: Mapping[str, Any]) -> EvalConfig:
    """Validate an already-parsed evaluation config mapping."""
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise EvalError(f"unknown top-level key(s) {unknown}")
    if raw.get("eval_version") != EVAL_VERSION:
        raise EvalError(f"eval_version must be {EVAL_VERSION}, got {raw.get('eval_version')!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise EvalError("name must be a non-empty string")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise EvalError("description must be a string")
    if raw.get("split") != "dev":
        raise EvalError(
            f"split must be 'dev' (got {raw.get('split')!r}): the decomposer is measured on the dev "
            "split only and this tool never loads the test split"
        )
    sample = _table("sample", raw.get("sample"), _SAMPLE_KEYS)
    reference = _table("reference", raw.get("reference"), {"name"})
    if reference["name"] not in REFERENCES:
        raise EvalError(f"[reference].name must be one of {sorted(REFERENCES)}, got {reference['name']!r}")
    runner = _table("runner", raw.get("runner", {}), _RUNNER_KEYS, required=set())
    max_usd = runner.get("max_usd")
    if max_usd is not None and (isinstance(max_usd, bool) or not isinstance(max_usd, (int, float)) or max_usd <= 0):
        raise EvalError("[runner].max_usd must be a number > 0 (US dollars)")
    prices = runner.get("prices", "configs/prices.toml")
    if not isinstance(prices, str) or not prices:
        raise EvalError("[runner].prices must be a path")
    th = _table("thresholds", raw.get("thresholds"), set(THRESHOLD_KEYS))
    thresholds = Thresholds(
        entity_accuracy_min=_fraction("[thresholds].entity_accuracy_min", th["entity_accuracy_min"]),
        failure_rate_max=_fraction("[thresholds].failure_rate_max", th["failure_rate_max"]),
        competitor_collisions_max=_int(
            "[thresholds].competitor_collisions_max", th["competitor_collisions_max"], minimum=0
        ),
        qualifier_recall_min=_fraction("[thresholds].qualifier_recall_min", th["qualifier_recall_min"]),
        qualifier_precision_min=_fraction("[thresholds].qualifier_precision_min", th["qualifier_precision_min"]),
        presupposition_agreement_min=_fraction(
            "[thresholds].presupposition_agreement_min", th["presupposition_agreement_min"]
        ),
    )
    dataset = _component("dataset", raw.get("dataset"))
    if dataset.name != reference["name"]:
        raise EvalError("[reference].name must be the dataset the reference procedure is defined for")
    return EvalConfig(
        name=name,
        description=description,
        repeats=_int("repeats", raw.get("repeats"), minimum=1),
        sample_size=_int("[sample].size", sample["size"], minimum=1),
        sample_seed=_int("[sample].seed", sample["seed"], minimum=0),
        dataset=dataset,
        reference=reference["name"],
        decomposer=_component("decomposer", raw.get("decomposer")),
        max_usd=None if max_usd is None else float(max_usd),
        prices=prices,
        thresholds=thresholds,
    )


def load_eval_config(path: str | Path) -> EvalConfig:
    path = Path(path)
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise EvalError(f"{path}: invalid TOML: {exc}") from exc
    try:
        return parse_eval_config(raw)
    except EvalError as exc:
        raise EvalError(f"{path}: {exc}") from None


# --------------------------------------------------------------------------- #
# Questions, references and the sample
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class QuestionUnit:
    """One distinct dev question with its reference decomposition."""

    question: str
    question_id: str
    group_ids: tuple[str, ...]
    n_items: int
    reference: QuestionConstraints
    forbidden: tuple[str, ...]


def question_id(question: str) -> str:
    return sha256_text(question)[:16]


def build_units(items: Iterable[Item], reference_name: str) -> list[QuestionUnit]:
    """Group items by question and attach the reference; every item of a question must agree."""
    reference = REFERENCES[reference_name]()
    grouped: dict[str, list[Item]] = {}
    for item in items:
        grouped.setdefault(item.question, []).append(item)
    units = []
    for question, members in grouped.items():
        constraints = {canonical_json(reference.constraints(m).to_dict()) for m in members}
        if len(constraints) != 1:
            raise EvalError(f"items of the question {question!r} disagree on the reference decomposition")
        forbidden = sorted({name for m in members for name in reference.forbidden_entity_names(m)})
        units.append(
            QuestionUnit(
                question=question,
                question_id=question_id(question),
                group_ids=tuple(sorted({m.group_id for m in members})),
                n_items=len(members),
                reference=reference.constraints(members[0]),
                forbidden=tuple(forbidden),
            )
        )
    return sorted(units, key=lambda u: u.question)


def sample_rank(seed: int, question: str) -> str:
    return hashlib.sha256(f"{seed}\n{question}".encode("utf-8")).hexdigest()


def select_sample(units: Sequence[QuestionUnit], size: int, seed: int) -> list[QuestionUnit]:
    """The ``size`` questions with the smallest ``SHA-256("<seed>\\n<question>")``, sorted by question."""
    if size > len(units):
        raise EvalError(f"sample size {size} is larger than the {len(units)} distinct dev questions available")
    chosen = sorted(units, key=lambda u: sample_rank(seed, u.question))[:size]
    return sorted(chosen, key=lambda u: u.question)


def load_units(config: EvalConfig) -> tuple[list[QuestionUnit], str, int]:
    """``(sample, dataset fingerprint, number of distinct dev questions)``. Dev only."""
    adapter = registry.create("dataset", config.dataset.name, **config.dataset.options)
    fingerprint = adapter.fingerprint()
    dev_items = select_dev(adapter.iter_items())
    if not dev_items:
        raise EvalError("the dataset yielded no dev items")
    units = build_units(dev_items, config.reference)
    return select_sample(units, config.sample_size, config.sample_seed), fingerprint, len(units)


# --------------------------------------------------------------------------- #
# Matching rules
# --------------------------------------------------------------------------- #

_WORD = re.compile(r"\w+")


def forms(text: str, aliases: Iterable[str] = ()) -> list[str]:
    """Normalised, non-empty, de-duplicated surface forms (text first, then aliases)."""
    return _normals((text, *aliases))


def _normals(values: Iterable[str]) -> list[str]:
    """Normalised, non-empty, de-duplicated strings, in order."""
    seen: list[str] = []
    for value in values:
        normal = normalize_text(value)
        if normal and normal not in seen:
            seen.append(normal)
    return seen


def _tokens(normal: str) -> tuple[str, ...]:
    return tuple(_WORD.findall(normal))


def _contiguous(inner: tuple[str, ...], outer: tuple[str, ...]) -> bool:
    if not inner or len(inner) > len(outer):
        return False
    return any(outer[i : i + len(inner)] == inner for i in range(len(outer) - len(inner) + 1))


def lenient_equal(a: str, b: str) -> bool:
    """Exact equality of normalised forms, or one form's word tokens contiguous in the other's."""
    if a == b:
        return True
    ta, tb = _tokens(a), _tokens(b)
    return _contiguous(ta, tb) or _contiguous(tb, ta)


def _any(left: Iterable[str], right: Iterable[str], lenient: bool) -> bool:
    right = list(right)
    return any((lenient_equal(a, b) if lenient else a == b) for a in left for b in right)


def compare(
    predicted: QuestionConstraints | None,
    reference: QuestionConstraints,
    forbidden: Iterable[str],
) -> dict[str, Any]:
    """Compare one decomposition with the reference (see "Matching rules" in the module docstring)."""
    ref_entity = forms(reference.entity.name, reference.entity.aliases)
    ref_qualifiers = [forms(q.text, q.aliases) for q in reference.qualifiers]
    n_ref_q = len(reference.qualifiers)
    if predicted is None:
        return {
            "parsed": False,
            "entity": {"correct": False, "name_exact": False, "matched_via": None,
                       "competitor_collisions": [], "n_aliases": 0},
            "qualifiers": {"reference": n_ref_q, "predicted": 0,
                           "reference_matched_exact": 0, "reference_matched_lenient": 0,
                           "predicted_matched_exact": 0, "predicted_matched_lenient": 0,
                           "kind_agree": 0, "matched_kinds": []},
            "presuppositions": {"reference": len(reference.presuppositions), "predicted": 0,
                                "agree_exact": False, "agree_lenient": False},
            "attribute": None,
        }  # fmt: skip

    forbidden_forms = set(_normals(forbidden))
    pred_entity = forms(predicted.entity.name, predicted.entity.aliases)
    name_exact = normalize_text(predicted.entity.name) == normalize_text(reference.entity.name)
    shared = [f for f in pred_entity if f in ref_entity]
    matched_via = "name" if normalize_text(predicted.entity.name) in ref_entity else ("alias" if shared else None)
    collisions = sorted(f for f in pred_entity if f in forbidden_forms)
    entity_correct = matched_via is not None and not collisions

    pred_qualifiers = [forms(q.text, q.aliases) for q in predicted.qualifiers]
    ref_matched_exact = [any(_any(p, r, False) for p in pred_qualifiers) for r in ref_qualifiers]
    ref_matched_lenient = [any(_any(p, r, True) for p in pred_qualifiers) for r in ref_qualifiers]
    pred_matched_exact = [any(_any(p, r, False) for r in ref_qualifiers) for p in pred_qualifiers]
    pred_matched_lenient = [any(_any(p, r, True) for r in ref_qualifiers) for p in pred_qualifiers]
    kind_agree = 0
    matched_kinds: list[str] = []
    for r_index, r in enumerate(ref_qualifiers):
        partners = [i for i, p in enumerate(pred_qualifiers) if _any(p, r, False)]
        if partners:
            kinds = [predicted.qualifiers[i].kind for i in partners]
            matched_kinds.append(kinds[0].value)
            kind_agree += reference.qualifiers[r_index].kind in kinds

    # Accepted surface forms of each reference key term (see "Presupposition agreement").
    def accepted(term: str) -> list[str]:
        normal = normalize_text(term)
        result = [normal]
        if normal in ref_entity and entity_correct:
            result += pred_entity
        for r_index, r in enumerate(ref_qualifiers):
            if normal in r:
                for p in pred_qualifiers:
                    if _any(p, r, False):
                        result += p
        return result

    def presupposition_agrees(lenient: bool) -> bool:
        for ref_p in reference.presuppositions:
            needed = [accepted(term) for term in ref_p.key_terms]
            if not any(
                all(_any(_normals(p.key_terms), options, lenient) for options in needed)
                for p in predicted.presuppositions
            ):
                return False
        return True

    return {
        "parsed": True,
        "entity": {
            "correct": entity_correct,
            "name_exact": name_exact,
            "matched_via": matched_via,
            "competitor_collisions": collisions,
            "n_aliases": len(predicted.entity.aliases),
        },
        "qualifiers": {
            "reference": n_ref_q,
            "predicted": len(pred_qualifiers),
            "reference_matched_exact": sum(ref_matched_exact),
            "reference_matched_lenient": sum(ref_matched_lenient),
            "predicted_matched_exact": sum(pred_matched_exact),
            "predicted_matched_lenient": sum(pred_matched_lenient),
            "kind_agree": kind_agree,
            "matched_kinds": matched_kinds,
        },
        "presuppositions": {
            "reference": len(reference.presuppositions),
            "predicted": len(predicted.presuppositions),
            "agree_exact": presupposition_agrees(False),
            "agree_lenient": presupposition_agrees(True),
        },
        "attribute": predicted.attribute,
    }


# --------------------------------------------------------------------------- #
# Consequences for the gate
# --------------------------------------------------------------------------- #

CONSEQUENCES: dict[str, str] = {
    "decomposition_unparseable": (
        "no constraints: the gated arm cannot run its checks and writes an error record for every "
        "item with this question; an error counts as a lost correct answer wherever no_gate was "
        "target-grounded (PREREGISTRATION.md 6.4). The failure is cached, so it affects the whole run."
    ),
    "decomposition_call_failed": (
        "no constraints because the call failed after retries: error records as above, but not "
        "cached, so later items with this question ask again."
    ),
    "entity_wrong": (
        "entity_in_window and no_closer_competitor test the wrong name: correct target-grounded "
        "answers tend to be blocked (raises M2)."
    ),
    "entity_form_is_competitor": (
        "a competitor's name counts as the queried entity: wrong-entity answers can pass "
        "entity_in_window (the gate fails to lower M1)."
    ),
    "qualifier_missed": (
        "a condition of the question (the disease, in Deceptive Grounding) is not checked: "
        "qualifiers_in_window cannot block evidence about the drug in another disease."
    ),
    "qualifier_extra": (
        "a condition that is not in the reference must appear near the quote: risk of blocking "
        "correct answers (raises M2)."
    ),
    "presupposition_missed": (
        "premise_present has no premise covering the reference key terms: the premise check may "
        "pass or be not_applicable where the evidence does not support the premise (e.g. no "
        "document about X in the 'absent' conditions)."
    ),
}


def consequences(status: str, comparison: Mapping[str, Any]) -> list[str]:
    """Consequence categories of one record (empty list = consistent with the reference)."""
    if status == STATUS_UNPARSEABLE:
        return ["decomposition_unparseable"]
    if status == STATUS_CALL_FAILED:
        return ["decomposition_call_failed"]
    found = []
    entity = comparison["entity"]
    if entity["matched_via"] is None:
        found.append("entity_wrong")
    if entity["competitor_collisions"]:
        found.append("entity_form_is_competitor")
    q = comparison["qualifiers"]
    if q["reference_matched_exact"] < q["reference"]:
        found.append("qualifier_missed")
    if q["predicted_matched_exact"] < q["predicted"]:
        found.append("qualifier_extra")
    if not comparison["presuppositions"]["agree_exact"]:
        found.append("presupposition_missed")
    return found


# --------------------------------------------------------------------------- #
# Records and metrics
# --------------------------------------------------------------------------- #


def make_record(
    unit: QuestionUnit,
    repeat: int,
    *,
    status: str,
    constraints: QuestionConstraints | None,
    reason: str | None,
    raw: str | None,
    model_calls: Sequence[ModelCall],
    cost_usd: float,
) -> dict[str, Any]:
    comparison = compare(constraints, unit.reference, unit.forbidden)
    return {
        "record_version": RECORD_VERSION,
        "question_id": unit.question_id,
        "question": unit.question,
        "group_ids": list(unit.group_ids),
        "n_items": unit.n_items,
        "repeat": repeat,
        "status": status,
        "reason": reason,
        "raw": raw,
        "constraints": None if constraints is None else constraints.to_dict(),
        "reference": unit.reference.to_dict(),
        "forbidden_entity_names": list(unit.forbidden),
        "comparison": comparison,
        "consequences": consequences(status, comparison),
        "model_calls": [call.to_dict() for call in model_calls],
        "cost_usd": round(cost_usd, 8),
    }


def _rate(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


def compute_metrics(records: Sequence[Mapping[str, Any]], repeats: int) -> dict[str, Any]:
    """All metrics from records alone; comparisons are recomputed from the stored constraints."""
    rows = []
    for record in records:
        predicted = None if record["constraints"] is None else QuestionConstraints.from_dict(record["constraints"])
        reference = QuestionConstraints.from_dict(record["reference"])
        comparison = compare(predicted, reference, record["forbidden_entity_names"])
        rows.append((record, comparison, consequences(record["status"], comparison)))

    n = len(rows)
    parsed = [c for _r, c, _k in rows if c["parsed"]]
    unparseable = sum(r["status"] == STATUS_UNPARSEABLE for r, _c, _k in rows)
    call_failed = sum(r["status"] == STATUS_CALL_FAILED for r, _c, _k in rows)
    entity_correct = sum(c["entity"]["correct"] for _r, c, _k in rows)
    collisions = sum(bool(c["entity"]["competitor_collisions"]) for _r, c, _k in rows)
    q = [c["qualifiers"] for _r, c, _k in rows]
    kinds: dict[str, int] = {}
    for item in q:
        for kind in item["matched_kinds"]:
            kinds[kind] = kinds.get(kind, 0) + 1
    ref_q = sum(x["reference"] for x in q)
    pred_q = sum(x["predicted"] for x in q)
    matched_pairs = sum(len(x["matched_kinds"]) for x in q)

    by_question: dict[str, list[tuple[Mapping[str, Any], dict[str, Any]]]] = {}
    for record, comparison, _k in rows:
        by_question.setdefault(record["question_id"], []).append((record, comparison))
    identical = consistent = 0
    for members in by_question.values():
        shapes = {canonical_json(r["constraints"]) if r["constraints"] is not None else None for r, _c in members}
        identical += len(shapes) == 1 and None not in shapes
        consistent += len({c["entity"]["correct"] for _r, c in members}) == 1

    categories: dict[str, dict[str, Any]] = {}
    for name, effect in CONSEQUENCES.items():
        hits = [r for r, _c, k in rows if name in k]
        categories[name] = {
            "records": len(hits),
            "rate": _rate(len(hits), n),
            "expected_items_affected_per_run": sum(r["n_items"] for r in hits) / repeats if repeats else None,
            "effect": effect,
        }
    clean = [r for r, _c, k in rows if not k]

    return {
        "n_records": n,
        "n_questions": len(by_question),
        "repeats": repeats,
        "parse_failure_rate": _rate(unparseable, n),
        "call_failure_rate": _rate(call_failed, n),
        "failure_rate": _rate(unparseable + call_failed, n),
        "entity_accuracy": _rate(entity_correct, n),
        "entity_accuracy_among_parsed": _rate(entity_correct, len(parsed)),
        "entity_name_exact_rate": _rate(sum(c["entity"]["name_exact"] for _r, c, _k in rows), n),
        "entity_matched_via_alias": sum(c["entity"]["matched_via"] == "alias" for _r, c, _k in rows),
        "competitor_collisions": collisions,
        "mean_entity_aliases": _rate(sum(c["entity"]["n_aliases"] for c in parsed), len(parsed)),
        "qualifier_recall_exact": _rate(sum(x["reference_matched_exact"] for x in q), ref_q),
        "qualifier_recall_lenient": _rate(sum(x["reference_matched_lenient"] for x in q), ref_q),
        "qualifier_precision_exact": _rate(sum(x["predicted_matched_exact"] for x in q), pred_q),
        "qualifier_precision_lenient": _rate(sum(x["predicted_matched_lenient"] for x in q), pred_q),
        "qualifier_kind_agreement": _rate(sum(x["kind_agree"] for x in q), matched_pairs),
        "matched_qualifier_kinds": dict(sorted(kinds.items())),
        "mean_predicted_qualifiers": _rate(pred_q, len(parsed)),
        "presupposition_agreement_exact": _rate(
            sum(c["presuppositions"]["agree_exact"] for _r, c, _k in rows), n
        ),
        "presupposition_agreement_lenient": _rate(
            sum(c["presuppositions"]["agree_lenient"] for _r, c, _k in rows), n
        ),
        "records_consistent_with_reference": len(clean),
        "stability": {
            "questions_with_identical_constraints_across_repeats": _rate(identical, len(by_question)),
            "questions_with_consistent_entity_correctness": _rate(consistent, len(by_question)),
        },
        "consequences": categories,
    }


def evaluate_thresholds(metrics: Mapping[str, Any], thresholds: Thresholds) -> dict[str, Any]:
    """Compare metrics with the thresholds. A metric that cannot be computed does not pass."""
    checks = [
        ("entity_accuracy", ">=", thresholds.entity_accuracy_min),
        ("failure_rate", "<=", thresholds.failure_rate_max),
        ("competitor_collisions", "<=", thresholds.competitor_collisions_max),
        ("qualifier_recall_exact", ">=", thresholds.qualifier_recall_min),
        ("qualifier_precision_exact", ">=", thresholds.qualifier_precision_min),
        ("presupposition_agreement_exact", ">=", thresholds.presupposition_agreement_min),
    ]
    results = []
    for metric, op, limit in checks:
        value = metrics.get(metric)
        passed = value is not None and (value >= limit if op == ">=" else value <= limit)
        results.append({"metric": metric, "value": value, "op": op, "threshold": limit, "passed": passed})
    return {"all_passed": all(r["passed"] for r in results), "checks": results}


# --------------------------------------------------------------------------- #
# Decomposer construction, cost and the dry run
# --------------------------------------------------------------------------- #


def build_decomposer(config: EvalConfig) -> Any:
    """The configured decomposer WITHOUT its cache: every repeat is an independent call."""
    if "cache" in config.decomposer.options:
        raise EvalError("[decomposer.options] must not set 'cache'; the evaluation always disables it")
    decomposer = registry.create("decomposer", config.decomposer.name, **config.decomposer.options, cache="none")
    for attribute in ("client", "request_for", "describe"):
        if not hasattr(decomposer, attribute):
            raise EvalError(f"decomposer {config.decomposer.name!r} has no {attribute!r}; it cannot be measured here")
    return decomposer


def _model_id(client: Any) -> str:
    return getattr(client, "model_id", None) or client.name


def _chars(request: ModelRequest) -> int:
    return sum(len(m.content) for m in request.messages)


def call_cost(call: ModelCall, prices: PriceBook, model_id: str, max_output_tokens: int) -> float:
    """Dollars for one logged call from REPORTED usage; estimated (expensively) when none was reported."""
    if call.response is None or not prices.has(model_id):
        return 0.0
    usage = call.response.usage
    if usage is None:
        usage = TokenUsage(prices.assumptions.input_tokens(_chars(call.request)), max_output_tokens)
    return prices.get(model_id).cost(usage.input_tokens, usage.output_tokens)


@dataclass(frozen=True)
class Estimate:
    config_name: str
    config_hash: str
    dataset_fingerprint: str
    dev_questions_available: int
    sample_size: int
    sample_seed: int
    repeats: int
    calls: int
    input_chars: int
    input_tokens: int
    model_label: str
    model_id: str
    secret: str | None
    max_output_tokens: int | None
    priced: bool
    cost_low: float | None
    cost_high: float | None
    cost_ceiling: float | None
    cap: float | None
    prices_path: str
    prices_sha256: str
    assumptions: dict[str, Any] = field(default_factory=dict)
    sample_question_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["sample_question_ids"] = list(self.sample_question_ids)
        return data

    def format(self) -> str:
        def money(value: float | None) -> str:
            return "no price" if value is None else f"${value:,.4f}"

        a = self.assumptions
        return "\n".join(
            [
                "DRY RUN - no model API was called, nothing was spent, nothing was written.",
                f"config            : {self.config_name}  (hash {self.config_hash[:12]})",
                f"dataset           : data hash {self.dataset_fingerprint[:12]}; dev split only",
                f"sample            : {self.sample_size} of {self.dev_questions_available} distinct dev "
                f"questions (seed {self.sample_seed}) x {self.repeats} repeats",
                f"decomposer model  : {self.model_label} = {self.model_id}  (API key: {self.secret or 'none'})",
                f"calls             : {self.calls}  ({self.input_tokens:,} input tokens estimated "
                f"from {self.input_chars:,} prompt characters)",
                f"prices            : {self.prices_path}  (sha256 {self.prices_sha256[:12]})",
                "token assumptions : input = characters / "
                f"{a.get('chars_per_token')}; visible output = {a.get('visible_output_tokens')} tokens per "
                f"call; hidden reasoning = {a.get('reasoning_tokens_low')} (low) to "
                f"{a.get('reasoning_tokens_high')} (high) per call; ceiling = every call uses max_output_tokens",
                f"cost              : low {money(self.cost_low)}, high {money(self.cost_high)}, "
                f"ceiling {money(self.cost_ceiling)}",
                f"budget cap        : {'none' if self.cap is None else f'${self.cap:,.2f}'} "
                "([runner] max_usd; a paid run stops when reported usage reaches it)",
                "Estimates are not measurements.",
            ]
        )


def estimate(
    config: EvalConfig,
    *,
    repo_root: str | Path,
    decomposer: Any | None = None,
    loaded: tuple[list[QuestionUnit], str, int] | None = None,
) -> Estimate:
    """Count calls and estimate cost without calling any API."""
    try:
        prices = load_prices(Path(repo_root) / config.prices)
    except PricingError as exc:
        raise EvalError(str(exc)) from exc
    decomposer = decomposer or build_decomposer(config)
    units, fingerprint, available = loaded or load_units(config)
    client = decomposer.client
    requests = [decomposer.request_for(unit.question) for unit in units]
    chars_per_repeat = sum(_chars(r) for r in requests)
    tokens_per_repeat = sum(prices.assumptions.input_tokens(_chars(r)) for r in requests)
    calls = len(units) * config.repeats
    input_tokens = tokens_per_repeat * config.repeats
    model_id = _model_id(client)
    max_output = getattr(client, "max_output_tokens", None)
    low = high = ceiling = None
    if prices.has(model_id):
        price = prices.get(model_id)
        visible = prices.assumptions.visible_output_tokens
        low = price.cost(input_tokens, calls * (visible + prices.reasoning_low(model_id)))
        high = price.cost(input_tokens, calls * (visible + prices.reasoning_high(model_id)))
        if max_output is not None:
            ceiling = price.cost(input_tokens, calls * max_output)
    return Estimate(
        config_name=config.name,
        config_hash=config.config_hash,
        dataset_fingerprint=fingerprint,
        dev_questions_available=available,
        sample_size=len(units),
        sample_seed=config.sample_seed,
        repeats=config.repeats,
        calls=calls,
        input_chars=chars_per_repeat * config.repeats,
        input_tokens=input_tokens,
        model_label=client.name,
        model_id=model_id,
        secret=getattr(client, "secret_name", None) or None,
        max_output_tokens=max_output,
        priced=prices.has(model_id),
        cost_low=low,
        cost_high=high,
        cost_ceiling=ceiling,
        cap=config.max_usd,
        prices_path=config.prices,
        prices_sha256=prices.sha256,
        assumptions=dict(prices.assumptions.__dict__),
        sample_question_ids=tuple(u.question_id for u in units),
    )


# --------------------------------------------------------------------------- #
# The paid run
# --------------------------------------------------------------------------- #


def _write_json_atomic(path: Path, data: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@dataclass
class EvalSummary:
    run_dir: Path
    records: int = 0
    spend_usd: float = 0.0
    aborted: bool = False
    report: dict[str, Any] = field(default_factory=dict)


def read_records(run_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(run_dir) / RECORDS_NAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_report(
    records: Sequence[Mapping[str, Any]],
    config: EvalConfig,
    *,
    complete: bool,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metrics = compute_metrics(records, config.repeats)
    thresholds = evaluate_thresholds(metrics, config.thresholds)
    if not complete:
        verdict = "INCOMPLETE"
    else:
        verdict = "MEETS PROPOSED THRESHOLDS" if thresholds["all_passed"] else "BELOW PROPOSED THRESHOLDS"
    return {
        "report_version": 1,
        "config_name": config.name,
        "config_hash": config.config_hash,
        "complete": complete,
        "verdict": verdict,
        "metrics": metrics,
        "thresholds": thresholds,
        "spend_usd": round(sum(float(r.get("cost_usd", 0.0)) for r in records), 6),
        "note": (
            "Thresholds are PROPOSED and require owner approval before freeze. Consequences are the "
            "expected effect by the design of the checks, not measured gate behaviour."
        ),
        **(extra or {}),
    }


def run_evaluation(
    config: EvalConfig,
    *,
    repo_root: str | Path,
    runs_dir: str | Path,
    confirm_spend: bool,
    config_path: str | Path | None = None,
    log: Log = print,
) -> EvalSummary:
    """The paid measurement (see the module docstring). The only function here that calls a model."""
    if not confirm_spend:
        raise EvalError(
            "refusing to make paid model calls without explicit confirmation "
            "(pass --confirm-spend; use dry-run to see the estimate first)"
        )
    repo_root = Path(repo_root)
    if config.max_usd is None:
        raise EvalError("no budget cap: set [runner] max_usd in the evaluation config")
    try:
        prices = load_prices(repo_root / config.prices)
    except PricingError as exc:
        raise EvalError(str(exc)) from exc
    decomposer = build_decomposer(config)
    client = decomposer.client
    model_id = _model_id(client)
    if not prices.has(model_id):
        raise EvalError(f"{config.prices} has no price for model id {model_id!r}; the budget cap cannot be enforced")
    loaded = load_units(config)
    plan = estimate(config, repo_root=repo_root, decomposer=decomposer, loaded=loaded)
    if plan.cost_low is not None and plan.cost_low > config.max_usd:
        raise EvalError(
            f"even the low cost estimate (${plan.cost_low:.4f}) exceeds the budget cap "
            f"(${config.max_usd:.2f}); raise the cap or reduce the sample"
        )
    check = getattr(client, "preflight", None)
    if check is not None:
        result = check()
        log(f"preflight {client.name}: {'OK' if result.ok else 'FAILED'} - {result.detail}")
        if not result.ok:
            raise EvalError(f"preflight failed for the decomposer model: {result.detail}")

    units, fingerprint, available = loaded
    config_dict = config.to_dict()
    run_id = f"{RUN_PREFIX}-{new_run_id(config.config_hash)}"
    run_dir = Path(runs_dir) / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise EvalError(f"run directory {run_dir} already exists; runs are never overwritten") from None
    manifest = RunManifest(
        run_id=run_id,
        config=config_dict,
        config_hash=sha256_json(config_dict),
        git_commit=gitinfo.head_commit(repo_root),
        git_dirty=bool(gitinfo.dirty_paths(repo_root, ignore=[ACCESS_LOG_RELPATH])),
        package_version=binding_gate.__version__,
        python_version=platform.python_version(),
        started_at=utc_now_iso(),
        config_path=None if config_path is None else Path(config_path).as_posix(),
    )
    _write_json_atomic(run_dir / MANIFEST_NAME, manifest.to_dict())

    summary = EvalSummary(run_dir=run_dir)
    max_output = getattr(client, "max_output_tokens", 0) or 0
    records: list[dict[str, Any]] = []
    with (run_dir / RECORDS_NAME).open("a", encoding="utf-8", newline="\n") as handle:
        for repeat in range(config.repeats):
            for unit in units:
                if summary.spend_usd >= config.max_usd:
                    summary.aborted = True
                    break
                constraints = None
                reason = raw = None
                try:
                    decomposition = decomposer.decompose(unit.question)
                    status, constraints, calls = STATUS_PARSED, decomposition.constraints, decomposition.model_calls
                    raw = next((c.response.text for c in reversed(calls) if c.response is not None), None)
                except ModelCallFailed as exc:
                    status, calls, reason = STATUS_CALL_FAILED, exc.model_calls, str(exc)
                except Exception as exc:  # noqa: BLE001 - a parse failure of any decomposer is recorded
                    if not hasattr(exc, "model_calls"):
                        raise
                    status, calls = STATUS_UNPARSEABLE, tuple(exc.model_calls)
                    reason, raw = getattr(exc, "reason", str(exc)), getattr(exc, "raw", None)
                cost = sum(call_cost(c, prices, model_id, max_output) for c in calls)
                record = make_record(
                    unit, repeat, status=status, constraints=constraints, reason=reason, raw=raw,
                    model_calls=calls, cost_usd=cost,
                )  # fmt: skip
                handle.write(canonical_json(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                records.append(record)
                summary.records += 1
                summary.spend_usd += cost
                if summary.spend_usd >= config.max_usd:
                    log(
                        f"BUDGET CAP REACHED: reported usage costs ${summary.spend_usd:.4f} >= cap "
                        f"${config.max_usd:.2f}; no further calls are made"
                    )
            if summary.aborted:
                break

    complete = summary.records == len(units) * config.repeats
    report = build_report(
        records,
        config,
        complete=complete,
        extra={
            "run_id": run_id,
            "decomposer": decomposer.describe(),
            "model_id": model_id,
            "dataset_fingerprint": fingerprint,
            "dev_questions_available": available,
            "sample_question_ids": [u.question_id for u in units],
            "prices_path": config.prices,
            "prices_sha256": prices.sha256,
            "aborted_by_budget_cap": summary.aborted,
        },
    )
    _write_json_atomic(run_dir / REPORT_NAME, report)
    digest = hashlib.sha256((run_dir / RECORDS_NAME).read_bytes()).hexdigest()
    closed = RunManifest.from_dict(
        {**manifest.to_dict(), "ended_at": utc_now_iso(), "trace_count": summary.records, "traces_sha256": digest}
    )
    _write_json_atomic(run_dir / MANIFEST_NAME, closed.to_dict())
    summary.report = report
    return summary


def report_from_run_dir(run_dir: str | Path) -> dict[str, Any]:
    """Recompute the report of a finished or aborted run from its manifest and records."""
    run_dir = Path(run_dir)
    manifest = RunManifest.from_json((run_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    config = parse_eval_config(manifest.config)
    records = read_records(run_dir)
    expected = config.sample_size * config.repeats
    return build_report(records, config, complete=len(records) == expected, extra={"run_id": manifest.run_id})


def format_report(report: Mapping[str, Any]) -> str:
    m = report["metrics"]

    def pct(value: Any) -> str:
        return "n/a" if value is None else f"{100 * value:.1f}%"

    lines = [
        f"Decomposer evaluation {report.get('run_id', '')}  config {report['config_name']} "
        f"(hash {report['config_hash'][:12]})",
        f"verdict: {report['verdict']}   records: {m['n_records']} ({m['n_questions']} questions x "
        f"{m['repeats']} repeats)   spend: ${report['spend_usd']:.4f}",
        "",
        f"entity accuracy               {pct(m['entity_accuracy'])}  (among parsed {pct(m['entity_accuracy_among_parsed'])}; "
        f"name exact {pct(m['entity_name_exact_rate'])}; competitor collisions {m['competitor_collisions']})",
        f"qualifier recall / precision  {pct(m['qualifier_recall_exact'])} / {pct(m['qualifier_precision_exact'])}  "
        f"(lenient {pct(m['qualifier_recall_lenient'])} / {pct(m['qualifier_precision_lenient'])}; "
        f"kind agreement {pct(m['qualifier_kind_agreement'])})",
        f"presupposition agreement      {pct(m['presupposition_agreement_exact'])}  "
        f"(lenient {pct(m['presupposition_agreement_lenient'])})",
        f"parse failures / call failures {pct(m['parse_failure_rate'])} / {pct(m['call_failure_rate'])}",
        f"stability across repeats      identical constraints {pct(m['stability']['questions_with_identical_constraints_across_repeats'])}; "
        f"consistent entity correctness {pct(m['stability']['questions_with_consistent_entity_correctness'])}",
        "",
        "thresholds (PROPOSED - require owner approval before freeze):",
    ]
    for check in report["thresholds"]["checks"]:
        value = check["value"]
        shown = "n/a" if value is None else (f"{value}" if isinstance(value, int) else f"{value:.3f}")
        lines.append(
            f"  {'PASS' if check['passed'] else 'FAIL'}  {check['metric']:<32} {shown:>8} {check['op']} {check['threshold']}"
        )
    lines += ["", "how errors would propagate (records; expected dev items affected per run):"]
    for name, info in m["consequences"].items():
        lines.append(f"  {name:<28} {info['records']:>4}  {info['expected_items_affected_per_run'] or 0:>8.1f}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m binding_gate.evaluation.decomposer",
        description="Separate accuracy measurement of the decomposer (A) on a dev sample.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("dry-run", "count calls and estimate cost; calls no API"),
                            ("run", "the paid measurement (requires --confirm-spend)")):  # fmt: skip
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--config", required=True, help="path of the evaluation config (TOML)")
        sub.add_argument("--repo-root", default=".", help="top level of the binding-gate checkout (default: .)")
        if name == "dry-run":
            sub.add_argument("--json", default=None, help="also write the estimate to this JSON file")
        else:
            sub.add_argument("--confirm-spend", action="store_true", help="confirm that paid API calls may be made")
            sub.add_argument("--runs-dir", default="runs", help="where run directories are created (default: runs)")
    report = commands.add_parser("report", help="recompute the report of a run directory")
    report.add_argument("run_dir")
    report.add_argument("--json", default=None, help="also write the report to this JSON file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "dry-run":
            plan = estimate(load_eval_config(args.config), repo_root=args.repo_root)
            print(plan.format())
            if args.json:
                Path(args.json).write_text(json.dumps(plan.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return EXIT_OK
        if args.command == "run":
            config = load_eval_config(args.config)
            if not args.confirm_spend:
                print("REFUSED: a paid run needs --confirm-spend. Use dry-run to see the estimate first.", file=sys.stderr)
                return EXIT_FAILURE
            secret = getattr(build_decomposer(config).client, "secret_name", None)
            if secret and not secrets.has_secret(secret):
                print(f"API key {secret} is not set; nothing was run", file=sys.stderr)
                return EXIT_NO_KEY
            summary = run_evaluation(
                config, repo_root=args.repo_root, runs_dir=args.runs_dir, confirm_spend=True,
                config_path=args.config,
            )  # fmt: skip
            print(format_report({**summary.report}))
            print(f"\nrun directory: {summary.run_dir}")
            return EXIT_BUDGET if summary.aborted else EXIT_OK
        report = report_from_run_dir(args.run_dir)
        print(format_report(report))
        if args.json:
            _write_json_atomic(Path(args.json), report)
        return EXIT_OK
    except SecretError as exc:
        print(f"ERROR (API key): {exc}", file=sys.stderr)
        return EXIT_NO_KEY
    except (PricingError, RuntimeError, ValueError, TypeError, OSError, LookupError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
