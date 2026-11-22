"""Deterministic sampling of gate decisions for the human audit (PREREGISTRATION.md section 13).

Population
----------
A *gate decision* is the verdict on attempt ``AUDIT_ATTEMPT`` (the first) of a
trace record of one of the arms ``AUDIT_ARMS`` (``gated``; ``gated_research`` in
layer 3). Ablation arms are not in the population. Records without a verdict
(abstained or unparseable candidates pass through the gate unjudged) and error
records are not gate decisions. Every given run directory contributes; all must
be from ONE split (mixing dev and test is refused). The split, every run's
hashes and the parameters are written into the sample file.

Halves and strata
-----------------
*Blocked* = verdict ``abstain`` or ``research``; *accepted* = verdict ``accept``.
``AUDIT_BLOCKED`` and ``AUDIT_ACCEPTED`` decisions are drawn from the two
halves. If the secondary judge (``scores.llm_judge.jsonl`` beside a trace) was
run, each half is split into two strata:

``scorer_disagreement``
    the primary deterministic scorer and the judge disagree about the candidate
    answer: "wrong-entity answer" (provenance outcome ``wrong_entity``) versus
    "EAF or substitution" (judge label ``eaf_or_substitution``); a judge failure
    (``judge_unparseable``, ``judge_error``) never agrees (PREREGISTRATION.md 6.5).
``other``
    everything else, including candidates without a judge label.

At most ``AUDIT_DISAGREEMENT_SHARE`` of a half comes from ``scorer_disagreement``
(oversampling); the rest from ``other``; if one stratum is too small, the other
fills the half. Without any judge scores each half is one stratum ``all``. A half
with fewer decisions than its target is taken whole and the shortfall recorded.

Every sampled case carries its sampling weight ``N_s / n_s`` (decisions in its
stratum over decisions drawn from it), so that population-level figures can be
re-weighted.

Determinism
-----------
Each decision has a key ``[run_id, item_id, arm, model, repeat, attempt]``.
Within a stratum the decisions with the smallest
``SHA-256("<seed>\\n<canonical JSON of the key>")`` are drawn. The display order
of the sample is the order of ``SHA-256("<seed>\\ndisplay\\n<case_id>")``, so
blocked and accepted cases are interleaved and the half cannot be guessed from
the position. ``case_id`` is an opaque hash of the key. The same traces and seed
give the same sample on any machine, independent of file order.

What the candidate's provenance and judge label are
---------------------------------------------------
The candidate of a ``gated`` record is the ``no_gate`` candidate of the same
item, model and repeat (same request, runner cache). Its provenance outcome and
judge label are taken from the ``no_gate`` record of the same run when that
record carries the identical candidate, otherwise from the gated record itself
when the gate accepted (then the gated record's final answer is the candidate).
Otherwise they are unknown (``null``).

Documents
---------
Traces do not store document texts separately; they store the exact prompt
(``ModelCall.request``) and, per attempt, the evidence references (id and
SHA-256 of each shown document). The documents are recovered from the answer
prompt by their ``[Document n — source: <id>]`` headers and each text is accepted
only if its SHA-256 equals the evidence reference. A document that cannot be
recovered is marked, and the full prompt is attached as a fallback.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from binding_gate.canonical import canonical_json, sha256_text
from binding_gate.contracts import AnswerStatus, Decision, Split, TraceRecord
from binding_gate.audit_tool import prereg
from binding_gate.trace import TRACES_NAME, TraceError, read_manifest, read_traces

SAMPLE_FORMAT = "binding-gate-audit-sample"
SAMPLE_VERSION = 1
BLOCKED = "blocked"
ACCEPTED = "accepted"
STRATUM_DISAGREE = "scorer_disagreement"
STRATUM_OTHER = "other"
STRATUM_ALL = "all"
REFERENCE_ARM = "no_gate"
WRONG_ENTITY = "wrong_entity"
JUDGE_POSITIVE = "eaf_or_substitution"
JUDGE_SCORES_NAME = "scores.llm_judge.jsonl"
JUDGED_LABELS = frozenset({"eaf_or_substitution", "confabulation_only", "correct_attribution", "no_claims"})
JUDGE_FAILURE_LABELS = frozenset({"judge_unparseable", "judge_error"})


class AuditError(RuntimeError):
    """Raised when an audit input is unusable."""


# --------------------------------------------------------------------------- #
# Reading run directories
# --------------------------------------------------------------------------- #


@dataclass
class LoadedRun:
    run_dir: Path
    manifest: Any
    records: list[TraceRecord]
    judge_labels: dict[tuple[str, str, str, int], str] = field(default_factory=dict)


def _read_judge_labels(run_dir: Path) -> dict[tuple[str, str, str, int], str]:
    path = run_dir / JUDGE_SCORES_NAME
    if not path.exists():
        return {}
    labels: dict[tuple[str, str, str, int], str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            key = (entry["item_id"], entry["arm"], entry["model"], int(entry["repeat"]))
            label = entry["score"]["label"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AuditError(f"{path}:{number}: malformed judge score line: {exc}") from None
        if key in labels:
            raise AuditError(f"{path}:{number}: duplicate judge score for {key}")
        labels[key] = str(label)
    return labels


def load_runs(run_dirs: Sequence[str | Path]) -> list[LoadedRun]:
    """Read every run directory; verify finished trace files against their manifest hash."""
    if not run_dirs:
        raise AuditError("at least one run directory is required")
    runs: list[LoadedRun] = []
    for run_dir in run_dirs:
        run_dir = Path(run_dir)
        try:
            manifest = read_manifest(run_dir)
            traces = run_dir / TRACES_NAME
            if manifest.ended_at is not None:
                digest = hashlib.sha256(traces.read_bytes()).hexdigest()
                if digest != manifest.traces_sha256:
                    raise AuditError(f"{traces}: SHA-256 differs from the manifest; the trace file was changed")
            records = list(read_traces(run_dir))
        except (TraceError, OSError) as exc:
            raise AuditError(f"cannot read run directory {run_dir}: {exc}") from exc
        runs.append(LoadedRun(run_dir, manifest, records, _read_judge_labels(run_dir)))
    return runs


# --------------------------------------------------------------------------- #
# Documents from the prompt
# --------------------------------------------------------------------------- #


def _answer_prompt(record: TraceRecord) -> str | None:
    for call in record.model_calls:
        if call.purpose == "answer":
            users = [m.content for m in call.request.messages if m.role == "user"]
            return users[-1] if users else None
    return None


def recover_documents(record: TraceRecord, attempt: int = 0) -> tuple[list[dict[str, Any]], str | None]:
    """Documents shown for one attempt, recovered from the answer prompt and verified by hash.

    Returns ``(documents, prompt_fallback)``: each document is
    ``{id, sha256, text, recovered}`` (``text`` is ``None`` when not recovered);
    ``prompt_fallback`` is the full prompt when any document could not be recovered.
    """
    refs = record.attempts[attempt].evidence
    prompt = _answer_prompt(record)
    documents: list[dict[str, Any]] = []
    position = 0
    for number, ref in enumerate(refs, start=1):
        text = None
        if prompt is not None:
            header = f"[Document {number} — source: {ref.id}]\n"
            found = prompt.find(header, position)
            if found >= 0:
                start = found + len(header)
                ends = [i for i in _find_all(prompt, "\n\n", start)] + [len(prompt)]
                for end in ends:
                    if sha256_text(prompt[start:end]) == ref.sha256:
                        text = prompt[start:end]
                        position = end
                        break
        documents.append({"id": ref.id, "sha256": ref.sha256, "url": ref.url, "text": text, "recovered": text is not None})
    fallback = None if all(d["recovered"] for d in documents) else prompt
    return documents, fallback


def _find_all(text: str, needle: str, start: int) -> Iterable[int]:
    index = text.find(needle, start)
    while index >= 0:
        yield index
        index = text.find(needle, index + 1)


# --------------------------------------------------------------------------- #
# Gate decisions
# --------------------------------------------------------------------------- #


@dataclass
class GateDecision:
    key: list[Any]
    record: TraceRecord
    run: LoadedRun
    half: str
    provenance: str | None
    judge_label: str | None
    disagreement: bool | None
    stratum: str = STRATUM_ALL

    @property
    def rank(self) -> str:
        return hashlib.sha256(f"{prereg.AUDIT_SEED}\n{canonical_json(self.key)}".encode("utf-8")).hexdigest()

    @property
    def case_id(self) -> str:
        return sha256_text("case\n" + canonical_json(self.key))[:16]


def _outcome(record: TraceRecord) -> str | None:
    for score in record.scores:
        if score.scorer == "provenance":
            outcome = score.details.get("outcome")
            return outcome if isinstance(outcome, str) else None
    return None


def _candidate_sources(run: LoadedRun, index: Mapping, record: TraceRecord, attempt: int):
    """Records that carry this decision's candidate as their final answer (reference first)."""
    candidate = record.attempts[attempt].answer
    reference = index.get((record.item_id, REFERENCE_ARM, record.model, record.repeat))
    sources = []
    if reference is not None and reference.final_answer == candidate:
        sources.append(reference)
    if record.final_answer == candidate:
        sources.append(record)
    return sources


def _scorer_disagreement(provenance: str | None, judge_label: str | None) -> bool | None:
    if provenance is None or judge_label is None:
        return None
    if judge_label in JUDGE_FAILURE_LABELS:
        return True
    if judge_label not in JUDGED_LABELS:
        return None
    return (provenance == WRONG_ENTITY) != (judge_label == JUDGE_POSITIVE)


def collect_decisions(
    runs: Sequence[LoadedRun], *, arms: Sequence[str] = prereg.AUDIT_ARMS, attempt: int = prereg.AUDIT_ATTEMPT
) -> list[GateDecision]:
    decisions: list[GateDecision] = []
    for run in runs:
        index = {(r.item_id, r.arm, r.model, r.repeat): r for r in run.records}
        for record in run.records:
            if record.arm not in arms or record.error is not None or len(record.attempts) <= attempt:
                continue
            chosen = record.attempts[attempt]
            if chosen.verdict is None or chosen.answer.status is not AnswerStatus.ANSWERED:
                continue
            half = ACCEPTED if chosen.verdict.decision is Decision.ACCEPT else BLOCKED
            provenance = judge_label = None
            for source in _candidate_sources(run, index, record, attempt):
                provenance = provenance or _outcome(source)
                judge_label = judge_label or run.judge_labels.get(
                    (source.item_id, source.arm, source.model, source.repeat)
                )
            key = [run.manifest.run_id, record.item_id, record.arm, record.model, record.repeat, attempt]
            decisions.append(
                GateDecision(key, record, run, half, provenance, judge_label, _scorer_disagreement(provenance, judge_label))
            )
    return decisions


# --------------------------------------------------------------------------- #
# The sample
# --------------------------------------------------------------------------- #


def _allocate(target: int, sizes: Mapping[str, int], judge_run: bool) -> dict[str, int]:
    if not judge_run:
        return {STRATUM_ALL: min(target, sizes.get(STRATUM_ALL, 0))}
    n_d_cap = int(Fraction(target) * prereg.AUDIT_DISAGREEMENT_SHARE)
    n_d = min(n_d_cap, sizes.get(STRATUM_DISAGREE, 0))
    n_o = min(target - n_d, sizes.get(STRATUM_OTHER, 0))
    if n_d + n_o < target:
        n_d = min(sizes.get(STRATUM_DISAGREE, 0), target - n_o)
    return {STRATUM_DISAGREE: n_d, STRATUM_OTHER: n_o}


def _case(decision: GateDecision, population: int, drawn: int) -> dict[str, Any]:
    record = decision.record
    attempt = decision.key[-1]
    chosen = record.attempts[attempt]
    documents, fallback = recover_documents(record, attempt)
    constraints = record.constraints
    return {
        "case_id": decision.case_id,
        "half": decision.half,
        "stratum": decision.stratum,
        "weight": {"population": population, "drawn": drawn, "exact": f"{population}/{drawn}", "value": population / drawn},
        "run_id": decision.key[0],
        "dataset": record.dataset,
        "item_id": record.item_id,
        "group_id": record.group_id,
        "condition": record.stratum,
        "arm": record.arm,
        "model": record.model,
        "repeat": record.repeat,
        "attempt": attempt,
        "question": constraints.question if constraints is not None else None,
        "documents": documents,
        "prompt_fallback": fallback,
        "candidate": {
            "value": chosen.answer.value,
            "quote": chosen.answer.quote,
            "source_id": chosen.answer.source_id,
        },
        "verdict": chosen.verdict.to_dict(),  # type: ignore[union-attr]
        "constraints": constraints.to_dict() if constraints is not None else None,
        "candidate_provenance": decision.provenance,
        "judge_label": decision.judge_label,
        "scorer_disagreement": decision.disagreement,
    }


def draw_sample(
    runs: Sequence[LoadedRun],
    *,
    arms: Sequence[str] = prereg.AUDIT_ARMS,
    n_blocked: int = prereg.AUDIT_BLOCKED,
    n_accepted: int = prereg.AUDIT_ACCEPTED,
) -> dict[str, Any]:
    """The audit sample as a JSON-compatible dictionary (see the module docstring)."""
    splits = {r.split for run in runs for r in run.records}
    if len(splits) > 1:
        raise AuditError("the run directories mix the dev and the test split; audit them separately")
    decisions = collect_decisions(runs, arms=arms)
    if not decisions:
        raise AuditError(f"no gate decisions (verdicts of arms {list(arms)}) in the given runs")
    split = next(iter(splits))
    judge_run = any(run.judge_labels for run in runs)
    for d in decisions:
        d.stratum = (STRATUM_DISAGREE if d.disagreement else STRATUM_OTHER) if judge_run else STRATUM_ALL

    targets = {BLOCKED: n_blocked, ACCEPTED: n_accepted}
    population: dict[str, dict[str, int]] = {}
    composition: dict[str, dict[str, int]] = {}
    shortfall: list[str] = []
    cases: list[dict[str, Any]] = []
    for half in (BLOCKED, ACCEPTED):
        in_half = [d for d in decisions if d.half == half]
        sizes = Counter(d.stratum for d in in_half)
        strata = (STRATUM_DISAGREE, STRATUM_OTHER) if judge_run else (STRATUM_ALL,)
        population[half] = {s: sizes.get(s, 0) for s in strata}
        allocation = _allocate(targets[half], sizes, judge_run)
        composition[half] = allocation
        drawn_total = sum(allocation.values())
        if drawn_total < targets[half]:
            shortfall.append(
                f"{half}: only {drawn_total} gate decision(s) exist; target {targets[half]} (all were taken)"
            )
        for stratum, n in allocation.items():
            if n == 0:
                continue
            members = sorted((d for d in in_half if d.stratum == stratum), key=lambda d: d.rank)
            cases.extend(_case(d, sizes[stratum], n) for d in members[:n])

    def display_key(case: Mapping[str, Any]) -> str:
        return sha256_text(f"{prereg.AUDIT_SEED}\ndisplay\n{case['case_id']}")

    cases.sort(key=display_key)
    for number, case in enumerate(cases, start=1):
        case["display_number"] = number

    by_model = Counter((c["model"], c["half"]) for c in cases)
    return {
        "format": SAMPLE_FORMAT,
        "version": SAMPLE_VERSION,
        "parameters": {**prereg.as_dict(), "arms": list(arms), "n_blocked": n_blocked, "n_accepted": n_accepted},
        "as_preregistered": (tuple(arms), n_blocked, n_accepted)
        == (prereg.AUDIT_ARMS, prereg.AUDIT_BLOCKED, prereg.AUDIT_ACCEPTED),
        "split": split.value if isinstance(split, Split) else str(split),
        "datasets": sorted({d.record.dataset for d in decisions}),
        "runs": [
            {
                "run_dir": run.run_dir.as_posix(),
                "run_id": run.manifest.run_id,
                "config_hash": run.manifest.config_hash,
                "git_commit": run.manifest.git_commit,
                "git_dirty": run.manifest.git_dirty,
                "test_unlock_tag": run.manifest.test_unlock_tag,
                "traces_sha256": run.manifest.traces_sha256,
                "trace_count": run.manifest.trace_count,
                "finished": run.manifest.ended_at is not None,
                "judge_scores": bool(run.judge_labels),
            }
            for run in runs
        ],
        "judge_run": judge_run,
        "population": population,
        "composition": composition,
        "population_total": len(decisions),
        "shortfall": shortfall,
        "cases_by_model": {f"{m} / {h}": n for (m, h), n in sorted(by_model.items())},
        "cases": cases,
    }


def write_sample(sample: Mapping[str, Any], path: str | Path) -> str:
    """Write the sample file; return its SHA-256 (the id that label files refer to)."""
    data = (json.dumps(sample, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def load_sample(path: str | Path) -> tuple[dict[str, Any], str]:
    """Read a sample file; return ``(sample, sha256 of the file bytes)``."""
    data = Path(path).read_bytes()
    try:
        sample = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise AuditError(f"{path}: not a JSON file: {exc}") from None
    if sample.get("format") != SAMPLE_FORMAT or sample.get("version") != SAMPLE_VERSION:
        raise AuditError(f"{path}: not an audit sample of version {SAMPLE_VERSION}")
    return sample, hashlib.sha256(data).hexdigest()


def sample_from_run_dirs(run_dirs: Sequence[str | Path], **kwargs: Any) -> dict[str, Any]:
    return draw_sample(load_runs(run_dirs), **kwargs)
