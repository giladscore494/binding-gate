"""The analysis: trace files in, per-model metrics, intervals and hypothesis decisions out.

Entry points
------------
* :func:`load_trace_set` reads one or more run directories (manifest + traces),
  verifies each finished trace file against the SHA-256 in its manifest, and
  refuses mixed splits, mixed datasets and duplicate records.
* :func:`analyse` is a pure function of the records (plus run metadata) that
  returns one JSON-compatible dictionary: per model the paired metrics with
  bootstrap intervals, the comparisons with the reference arm, the worst-case
  sensitivity bound, the full outcome tables, per-condition and per-factor
  breakdowns, the spread across repeats, costs, and the H1-H4 decisions.
* :func:`write_outputs` writes ``analysis.json`` and ``report.md`` (and the
  figures when the optional ``plots`` extra is installed).

No model is called; nothing is re-scored. Outcomes come from the ``provenance``
score stored in each record (``binding_gate.baseline_report.record_outcome``);
error records count as ``error`` and stay in every denominator.

Confirmatory status
-------------------
A hypothesis result is confirmatory only if ALL of these hold; otherwise it is
reported with the reasons it is not:

1. every record is from the test split;
2. every run directory is finished (manifest has an end time);
3. the model's data are complete: no unpaired record, every item has the same
   repeats, and the number of repeats is the pre-registered one;
4. the bootstrap used the pre-registered seed, number of resamples and level;
5. validity guard (section 6.4 item 6): no arm involved in the comparison has an
   unparseable-plus-error rate above the pre-registered limit;
6. for H4: the layer-3 correctness rule is not the placeholder.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import binding_gate
from binding_gate.baseline_report import record_outcome
from binding_gate.contracts import RunManifest, Split, TraceRecord
from binding_gate.stats import hypotheses as hyp
from binding_gate.stats import prereg
from binding_gate.stats.bootstrap import Interval, StatsError, cluster_bootstrap
from binding_gate.stats.metrics import (
    ModelTable,
    all_statistics,
    arm_key,
    arm_statistics,
    build_model_table,
    comparison_key,
    pair_key,
    point_estimates,
)
from binding_gate.trace import TRACES_NAME, TraceError, read_manifest, read_traces

ANALYSIS_VERSION = 1
ANALYSIS_JSON = "analysis.json"
REPORT_MD = "report.md"
FIGURES_DIR = "figures"
CONDITION_STATS = ("wrong_all", "m1", "m2", "correct_all", "failed_all")


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


@dataclass
class TraceSet:
    records: list[TraceRecord]
    run_dirs: list[str]
    manifests: list[RunManifest]
    unfinished: list[str] = field(default_factory=list)


def load_trace_set(run_dirs: Sequence[str | Path]) -> TraceSet:
    """Read and verify the given run directories."""
    if not run_dirs:
        raise StatsError("at least one run directory is required")
    records: list[TraceRecord] = []
    manifests: list[RunManifest] = []
    unfinished: list[str] = []
    for run_dir in run_dirs:
        run_dir = Path(run_dir)
        try:
            manifest = read_manifest(run_dir)
            traces = run_dir / TRACES_NAME
            if manifest.ended_at is None:
                unfinished.append(run_dir.as_posix())
            elif manifest.traces_sha256 != hashlib.sha256(traces.read_bytes()).hexdigest():
                raise StatsError(
                    f"{traces}: SHA-256 differs from the manifest; the trace file was changed after the run"
                )
            records.extend(read_traces(run_dir))
        except (TraceError, OSError) as exc:
            raise StatsError(f"cannot read run directory {run_dir}: {exc}") from exc
        manifests.append(manifest)
    return TraceSet(records, [Path(d).as_posix() for d in run_dirs], manifests, unfinished)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _num(value: Fraction | None) -> dict[str, Any]:
    if value is None:
        return {"value": None, "exact": None}
    return {"value": float(value), "exact": f"{value.numerator}/{value.denominator}"}


def _condition_parts(stratum: str | None) -> tuple[str, str] | None:
    parts = (stratum or "").split("/")
    return (parts[0], parts[1]) if len(parts) == 2 and all(parts) else None


def _subset_estimates(table: ModelTable, rows) -> dict[str, dict[str, Any]]:
    totals = table.totals(rows)
    result: dict[str, dict[str, Any]] = {}
    for arm in table.arms:
        stats = arm_statistics(table.columns, arm, table.reference)
        values = point_estimates({k: v for k, v in stats.items() if k in CONDITION_STATS}, totals)
        result[arm] = {name: _num(value)["value"] for name, value in values.items()}
        result[arm]["n_items"] = len(rows)
    return result


def _repeat_spread(table: ModelTable) -> dict[str, dict[str, Any]]:
    spread: dict[str, dict[str, Any]] = {}
    for arm in table.arms:
        stats = arm_statistics(table.columns, arm, table.reference)
        chosen = {k: v for k, v in stats.items() if k in ("m1", "m2", "wrong_all", "correct_all")}
        per_stat: dict[str, Any] = {}
        per_repeat = {r: point_estimates(chosen, table.repeat_totals(r)) for r in table.repeats}
        for name in chosen:
            values = {str(r): None if per_repeat[r][name] is None else float(per_repeat[r][name]) for r in table.repeats}
            defined = [v for v in values.values() if v is not None]
            per_stat[name] = {
                "per_repeat": values,
                "min": min(defined) if defined else None,
                "max": max(defined) if defined else None,
            }
        spread[arm] = per_stat
    return spread


# --------------------------------------------------------------------------- #
# Hypotheses for one model
# --------------------------------------------------------------------------- #


def _confirmatory(
    result: hyp.HypothesisResult,
    *,
    base_reasons: Sequence[str],
    failed_rates: Mapping[str, Fraction | None],
) -> hyp.HypothesisResult:
    reasons = list(base_reasons)
    for arm in result.arms:
        rate = failed_rates.get(arm)
        if rate is not None and rate > prereg.VALIDITY_MAX_FAILED_RATE:
            reasons.append(
                f"validity guard: arm {arm!r} has unparseable + error rate {float(rate):.2%} "
                f"> {float(prereg.VALIDITY_MAX_FAILED_RATE):.0%}"
            )
    result.non_confirmatory_reasons = reasons
    result.confirmatory = result.status != hyp.NOT_EVALUABLE and not reasons
    return result


def evaluate_model_hypotheses(
    *,
    layer: int | None,
    arms: Sequence[str],
    est: Mapping[str, Fraction | None],
    ci: Mapping[str, Interval],
    base_reasons: Sequence[str],
    correct_rule_placeholder: bool,
) -> dict[str, hyp.HypothesisResult]:
    """H1-H4 for one model from its point estimates ``est`` and intervals ``ci``."""
    ref, gated, faith, research = (
        prereg.REFERENCE_ARM,
        prereg.GATED_ARM,
        prereg.FAITHFULNESS_ARM,
        prereg.RESEARCH_ARM,
    )
    wanted = prereg.HYPOTHESES_BY_LAYER.get(layer, ()) if layer is not None else ()
    failed_rates = {arm: est.get(arm_key(arm, "failed_all")) for arm in arms}
    results: dict[str, hyp.HypothesisResult] = {}

    def missing(hypothesis: str, needed: Sequence[str]) -> hyp.HypothesisResult | None:
        if hypothesis not in wanted:
            layers = [str(n) for n, names in sorted(prereg.HYPOTHESES_BY_LAYER.items()) if hypothesis in names]
            where = ("layer " if len(layers) == 1 else "layers ") + " and ".join(layers)
            return hyp.not_evaluable(hypothesis, f"{hypothesis} is evaluated on {where}; these traces are layer {layer}")
        absent = [arm for arm in needed if arm not in arms]
        if absent:
            return hyp.not_evaluable(hypothesis, f"arm(s) {absent} not in the traces")
        return None

    # H1
    results["H1"] = missing("H1", (gated, ref)) or _h1(gated, ref, est, ci)
    # H2
    if (r := missing("H2", (gated, ref))) is None:
        status, criteria = hyp.decide_h2(m2_arm=est[arm_key(gated, "m2")])
        r = hyp.HypothesisResult("H2", status, criteria, arms=(gated, ref))
        r.notes.append(f"M2 interval: {_iv(ci.get(arm_key(gated, 'm2')))} (reported, not part of the rule)")
    results["H2"] = r
    # H3
    if (r := missing("H3", (gated, faith, ref))) is None:
        status, criteria = hyp.decide_h3(
            wrong_all_gated=est[arm_key(gated, "wrong_all")],
            wrong_all_other=est[arm_key(faith, "wrong_all")],
            diff_wrong_all_interval=ci.get(pair_key(gated, faith, "diff_wrong_all")),
            m2_gated=est[arm_key(gated, "m2")],
            m2_other=est[arm_key(faith, "m2")],
        )
        r = hyp.HypothesisResult("H3", status, criteria, arms=(gated, faith, ref))
        r.notes.append(f"interval of M2(gated) - M2(other): {_iv(ci.get(pair_key(gated, faith, 'diff_m2')))}")
    results["H3"] = r
    # H4
    if (r := missing("H4", (research, ref))) is None:
        status, criteria = hyp.decide_h4(
            m3_arm=est[arm_key(research, "correct_all")], m3_ref=est[arm_key(ref, "correct_all")]
        )
        r = hyp.HypothesisResult("H4", status, criteria, arms=(research, ref))
        r.notes.append(
            "interval of M3(arm) - M3(reference): "
            f"{_iv(ci.get(comparison_key(research, ref, 'diff_correct_all')))} (reported, not part of the rule)"
        )
    results["H4"] = r

    for name, result in results.items():
        extra = list(base_reasons)
        if name == "H4" and correct_rule_placeholder:
            extra.append("layer-3 correctness rule is the placeholder (fixed by PR 8)")
        _confirmatory(result, base_reasons=extra, failed_rates=failed_rates)
    return results


def _iv(interval: Interval | None) -> str:
    return hyp._interval_text(interval)


def _h1(gated: str, ref: str, est, ci) -> hyp.HypothesisResult:
    status, criteria = hyp.decide_h1(
        m1_arm=est[arm_key(gated, "m1")],
        m1_ref=est[arm_key(ref, "m1")],
        wrong_all_arm=est[arm_key(gated, "wrong_all")],
        wrong_all_ref=est[arm_key(ref, "wrong_all")],
        diff_m1_interval=ci.get(comparison_key(gated, ref, "diff_m1")),
    )
    worst_status, worst_criteria = hyp.decide_h1(
        m1_arm=est[comparison_key(gated, ref, "m1_worst")],
        m1_ref=est[comparison_key(gated, ref, "ref_m1_best")],
        wrong_all_arm=est[comparison_key(gated, ref, "wrong_all_worst")],
        wrong_all_ref=est[arm_key(ref, "wrong_all")],
        diff_m1_interval=ci.get(comparison_key(gated, ref, "diff_m1_worst")),
    )
    result = hyp.HypothesisResult("H1", status, criteria, arms=(gated, ref))
    result.sensitivity = {
        "description": "worst case for the gate (section 6.4 item 7): every unparseable/error record of "
        f"{gated!r} counted as a wrong-entity answer, every such record of {ref!r} as an answered, "
        "not-wrong record. Reported, never decisive.",
        "status_under_worst_case": worst_status,
        "criteria": [c.to_dict() for c in worst_criteria],
    }
    return result


# --------------------------------------------------------------------------- #
# The analysis
# --------------------------------------------------------------------------- #


def analyse(
    records: Sequence[TraceRecord],
    *,
    run_dirs: Sequence[str] = (),
    manifests: Sequence[RunManifest] = (),
    unfinished: Sequence[str] = (),
    resamples: int = prereg.BOOTSTRAP_RESAMPLES,
    seed: int = prereg.BOOTSTRAP_SEED,
    level: Fraction = prereg.CI_LEVEL,
    correct_outcomes: Iterable[str] | None = None,
    outcome_of: Callable[[TraceRecord], str] = record_outcome,
) -> dict[str, Any]:
    """Analyse trace records; see the module docstring. Returns a JSON-compatible dict."""
    if not records:
        raise StatsError("no trace records to analyse")
    splits = {r.split for r in records}
    datasets = {r.dataset for r in records}
    if len(splits) != 1:
        raise StatsError("records from both the dev and the test split were given; analyse them separately")
    if len(datasets) != 1:
        raise StatsError(f"records from several datasets were given ({sorted(datasets)}); analyse them separately")
    split = next(iter(splits))
    dataset = next(iter(datasets))
    layer = prereg.LAYER_BY_DATASET.get(dataset)
    correct_placeholder = correct_outcomes is None
    correct = tuple(prereg.CORRECT_OUTCOMES_DEFAULT if correct_outcomes is None else correct_outcomes)
    as_preregistered = (resamples, seed, Fraction(level)) == (
        prereg.BOOTSTRAP_RESAMPLES,
        prereg.BOOTSTRAP_SEED,
        prereg.CI_LEVEL,
    )

    global_reasons: list[str] = []
    if split is not Split.TEST:
        global_reasons.append(f"split is {split.value!r}, not 'test' (exploratory only)")
    if unfinished:
        global_reasons.append(f"unfinished run(s): {list(unfinished)}")
    if not as_preregistered:
        global_reasons.append("bootstrap parameters differ from the pre-registration")
    subsampled = sorted(
        {m.run_id for m in manifests if isinstance(m.config, dict) and m.config.get("sample") is not None}
    )
    if subsampled:
        # PR 13: a run on a pre-registered subsample (mid-tier check, judges, repeats) is exploratory
        global_reasons.append(f"run(s) on a pre-registered item subsample (exploratory only): {subsampled}")

    by_model: dict[str, list[TraceRecord]] = {}
    for record in records:
        by_model.setdefault(record.model, []).append(record)

    models_out: dict[str, Any] = OrderedDict()
    tables: dict[str, ModelTable] = {}
    for model in sorted(by_model):
        table = build_model_table(
            by_model[model],
            reference=prereg.REFERENCE_ARM,
            correct_outcomes=correct,
            outcome_of=outcome_of,
            expected_repeats=prereg.REPEATS,
        )
        tables[model] = table
        pairs = [(prereg.GATED_ARM, prereg.FAITHFULNESS_ARM)]
        stats = all_statistics(table, pairs)
        est = point_estimates(stats, table.totals())
        ci = cluster_bootstrap(table.cluster_rows(), stats, resamples=resamples, seed=seed, level=level)

        model_reasons = list(global_reasons)
        if table.unpaired_records:
            model_reasons.append(f"{table.unpaired_records} record(s) have no counterpart in every arm")
        if not table.complete_repeats:
            model_reasons.append(
                f"repeats {list(table.repeats)} are incomplete or differ from the pre-registered {prereg.REPEATS}"
            )
        hypotheses = evaluate_model_hypotheses(
            layer=layer,
            arms=table.arms,
            est=est,
            ci=ci,
            base_reasons=model_reasons,
            correct_rule_placeholder=correct_placeholder,
        )
        h1, h2 = hypotheses["H1"], hypotheses["H2"]
        if h1.status == hyp.NOT_EVALUABLE or h2.status == hyp.NOT_EVALUABLE:
            claim = "not evaluable"
        elif h1.status == hyp.SUPPORTED and h2.status == hyp.SUPPORTED:
            claim = "the gate works (H1 and H2 supported)"
        elif h1.status == hyp.SUPPORTED:
            claim = "reduces errors at an unacceptable cost (H1 without H2)"
        else:
            claim = "H1 not supported"

        conditions: dict[str, Any] = {}
        factors: dict[str, dict[str, Any]] = {"x_document": {}, "competing_document": {}}
        strata = sorted({row.stratum or "(none)" for row in table.items})
        for stratum in strata:
            rows = [row for row in table.items if (row.stratum or "(none)") == stratum]
            conditions[stratum] = _subset_estimates(table, rows)
        if all(_condition_parts(row.stratum) for row in table.items):
            for index, factor in enumerate(("x_document", "competing_document")):
                levels = sorted({_condition_parts(row.stratum)[index] for row in table.items})  # type: ignore[index]
                for level_name in levels:
                    rows = [r for r in table.items if _condition_parts(r.stratum)[index] == level_name]  # type: ignore[index]
                    factors[factor][level_name] = _subset_estimates(table, rows)
        else:
            factors = {}

        models_out[model] = {
            "dataset": table.dataset,
            "arms": list(table.arms),
            "reference": table.reference,
            "n_records": table.n_records,
            "n_items": len(table.items),
            "n_groups": len(table.groups),
            "repeats": list(table.repeats),
            "scale": table.scale,
            "unpaired_records": table.unpaired_records,
            "complete": not table.unpaired_records and table.complete_repeats,
            "estimates": {
                name: {**_num(est[name]), "ci": ci[name].to_dict()} for name in stats
            },
            "outcome_counts": table.outcome_counts,
            "outcome_counts_by_condition": table.outcome_counts_by_condition,
            "conditions": conditions,
            "factors": factors,
            "repeat_spread": _repeat_spread(table),
            "validity_guard": {
                arm: {
                    "failed_rate": _num(est[arm_key(arm, "failed_all")])["value"],
                    "limit": float(prereg.VALIDITY_MAX_FAILED_RATE),
                    "exceeded": est[arm_key(arm, "failed_all")] is not None
                    and est[arm_key(arm, "failed_all")] > prereg.VALIDITY_MAX_FAILED_RATE,
                }
                for arm in table.arms
            },
            "cost": table.cost,
            "hypotheses": {name: result.to_dict() for name, result in hypotheses.items()},
            "controlled_layer_claim": claim,
        }

    summary: dict[str, Any] = {}
    for name in ("H1", "H2", "H3", "H4"):
        statuses = {m: models_out[m]["hypotheses"][name] for m in models_out}
        summary[name] = {
            "evaluated": [m for m, r in statuses.items() if r["status"] != hyp.NOT_EVALUABLE],
            "supported": [m for m, r in statuses.items() if r["status"] == hyp.SUPPORTED],
            "supported_confirmatory": [
                m for m, r in statuses.items() if r["status"] == hyp.SUPPORTED and r["confirmatory"]
            ],
            "trade_off": [m for m, r in statuses.items() if r["status"] == hyp.TRADE_OFF],
        }

    return {
        "analysis_version": ANALYSIS_VERSION,
        "package_version": binding_gate.__version__,
        "dataset": dataset,
        "layer": layer,
        "split": split.value,
        "parameters": {
            **prereg.as_dict(),
            "used": {"ci_level": str(Fraction(level)), "bootstrap_resamples": resamples, "bootstrap_seed": seed},
            "as_preregistered": as_preregistered,
            "correct_outcomes": list(correct),
            "correct_rule_is_placeholder": correct_placeholder and layer == 3,
            "cluster": "group_id",
        },
        "inputs": {
            "run_dirs": list(run_dirs),
            "unfinished_runs": list(unfinished),
            "runs": [
                {
                    "run_id": m.run_id,
                    "config_hash": m.config_hash,
                    "git_commit": m.git_commit,
                    "git_dirty": m.git_dirty,
                    "test_unlock_tag": m.test_unlock_tag,
                    "traces_sha256": m.traces_sha256,
                    "trace_count": m.trace_count,
                    "finished": m.ended_at is not None,
                }
                for m in manifests
            ],
            "n_records": len(records),
        },
        "confirmatory_blockers": global_reasons,
        "models": models_out,
        "pooled_descriptive": _pooled(tables),
        "summary": summary,
    }


def _pooled(tables: Mapping[str, ModelTable]) -> dict[str, Any]:
    """Point estimates over all models together (descriptive only, section 8)."""
    if not tables:
        return {}
    common = set.intersection(*(set(t.arms) for t in tables.values()))
    reference = prereg.REFERENCE_ARM if all(t.reference == prereg.REFERENCE_ARM for t in tables.values()) else None
    out: dict[str, Any] = {}
    for arm in sorted(common):
        sums: dict[str, Fraction] = {}
        for table in tables.values():
            totals = table.totals()
            for name in ("wrong", "answered", "correct", "failed", "lost"):
                sums[name] = sums.get(name, Fraction(0)) + Fraction(totals[table.columns(arm, name)], table.scale)
            sums["items"] = sums.get("items", Fraction(0)) + Fraction(totals[table.columns.items], table.scale)
            if reference:
                sums["ref_correct"] = sums.get("ref_correct", Fraction(0)) + Fraction(
                    totals[table.columns(reference, "correct")], table.scale
                )

        def ratio(a: Fraction, b: Fraction) -> float | None:
            return None if b == 0 else float(a / b)

        out[arm] = {
            "m1": ratio(sums["wrong"], sums["answered"]),
            "m2": ratio(sums["lost"], sums["ref_correct"]) if reference and arm != reference else None,
            "wrong_all": ratio(sums["wrong"], sums["items"]),
            "correct_all": ratio(sums["correct"], sums["items"]),
            "failed_all": ratio(sums["failed"], sums["items"]),
        }
    return {"models": sorted(tables), "arms": out, "note": "descriptive only; no hypothesis uses pooled numbers"}


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def write_outputs(analysis: Mapping[str, Any], out_dir: str | Path, *, figures: bool = True) -> dict[str, Any]:
    """Write ``analysis.json``, ``report.md`` and, if possible, figures. Returns what was written."""
    from binding_gate.stats.report import render_markdown

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ANALYSIS_JSON).write_text(
        json.dumps(analysis, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    written: dict[str, Any] = {"analysis": (out_dir / ANALYSIS_JSON).as_posix(), "figures": []}
    figure_note = "figures not requested"
    if figures:
        from binding_gate.plots import figures as plot_figures

        if plot_figures.available():
            paths = plot_figures.write_figures(analysis, out_dir / FIGURES_DIR)
            written["figures"] = [p.as_posix() for p in paths]
            figure_note = f"{len(paths)} figure(s) in {FIGURES_DIR}/"
        else:
            figure_note = "figures skipped: matplotlib is not installed (pip install -e '.[plots]')"
    (out_dir / REPORT_MD).write_text(render_markdown(analysis, figure_note=figure_note), encoding="utf-8")
    written["report"] = (out_dir / REPORT_MD).as_posix()
    written["figure_note"] = figure_note
    return written


def analyse_run_dirs(run_dirs: Sequence[str | Path], **kwargs: Any) -> dict[str, Any]:
    trace_set = load_trace_set(run_dirs)
    return analyse(
        trace_set.records,
        run_dirs=trace_set.run_dirs,
        manifests=trace_set.manifests,
        unfinished=trace_set.unfinished,
        **kwargs,
    )
