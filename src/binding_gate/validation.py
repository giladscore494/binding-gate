"""Exploratory validation of the layer-1 STOP decision (PR 22; PREREGISTRATION.md section 18, amendment 1).

POST HOC and EXPLORATORY: decided by the owner on 2026-11-22 AFTER the layer-1 dev
baseline (workflow run 45053502161) had produced a STOP under section 10. Nothing
here re-thresholds the stop rule or re-opens layer-1 confirmatory work; the
report decides nothing. It answers two questions about the measurement behind the
STOP, on the dev split only:

V1  Does the deterministic metric undercount? The secondary LLM judge (the
    benchmark's own claim-level attribution judge, ``binding_gate.scorers.llm_judge``,
    slot J) labels the STRUCTURED answers of the dev baseline (arm ``no_gate``) on a
    fixed subsample; the report gives the judge's rates and its agreement with the
    deterministic ``provenance`` label.
V2  Does forcing the structured answer suppress the failure? The same judge labels
    the FREE-FORM answers of arm ``free_form`` (``binding_gate.arms.free_form``) on
    the same items and models; the report compares the two rates per model, paired
    by item, with a cluster bootstrap by ``group_id``.

The subsample (both steps; one definition, ``configs/rescore/validation_judge_structured.toml``,
``[sample]``): ``binding_gate.sampling`` with seed 20261122, 12 items per condition,
the three ``null_control`` conditions excluded: 12 x 12 = 144 dev items per model.

Definitions (fixed here before any validation output exists)
------------------------------------------------------------
Unit: one record of the subsample (one item, one model, one arm; one repeat).

* judge label: the ``llm_judge`` label of the record's score line
  (``scores.llm_judge.jsonl`` beside the trace), or ``unscored`` without one.
* POSITIVE (the failure): label ``eaf_or_substitution`` - at least one affirmative
  claim about the queried drug attributed to the competing document, or entity
  substitution (the benchmark's claim-level entity-attribution failure).
* rates over ALL subsample records of the arm and model (abstentions, ``no_claims``,
  judge failures, unscored records and arm errors stay in the denominator and count
  as not positive): ``eaf_or_substitution``, ``eaf`` (``failure_detected``),
  ``substitution`` (``entity_substitution_detected``) and ``confabulation`` (at
  least one claim attributed to neither document). The same rates over the
  DETERMINATE records only (label not ``judge_unparseable``, ``judge_error``,
  ``arm_error`` or ``unscored``) are a sensitivity analysis.
* V1 agreement (structured answers only): ours = ``provenance`` outcome
  ``wrong_entity``; judge = ``eaf_or_substitution``; raw agreement and Cohen's
  kappa over the judged population, judge failures counted as disagreement, and
  kappa over parsed judgments only (``llm_judge.agreement_rows``).
* V2 paired comparison, per model: the items with a record in both arms. Statistic
  ``free_form rate - structured rate`` of the positive label (all paired items;
  sensitivity: items whose two judgments are both determinate), with the
  discordant counts. Interval: percentile cluster bootstrap by ``group_id`` with
  the pre-registered parameters of section 8 (seed 20261122, 10,000 resamples, 95%;
  ``binding_gate.stats.bootstrap``), both arms resampled jointly.

The report holds labels, counts, rates and intervals only: no prompt, no answer,
no judge claim and no document text, so it may go to the ``results-log`` branch.

Command
-------
``python -m binding_gate.validation report --sample-config C --structured DIR ... [--free-form DIR ...]
[--repo-root .] [--baseline-workflow-run-id N] --json FILE [--markdown FILE]``
    Reads traces and score files only (and the dev items of the pinned dataset,
    to draw the subsample); calls nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from binding_gate.stats import prereg
from binding_gate.stats.bootstrap import Diff, Ratio, cluster_bootstrap

VALIDATION_VERSION = 1
REPORT_JSON = "validation_report.json"
REPORT_MD = "validation_report.md"
STRUCTURED_ARM = "no_gate"
FREE_FORM_ARM = "free_form"
AMENDMENT = "PREREGISTRATION.md section 18, amendment 1 (2026-11-22)"
LAYER1_BASELINE_WORKFLOW_RUN = 45053502161
"""The dev-baseline workflow run whose STOP this validation follows (recorded in the amendment)."""

UNSCORED = "unscored"
NOT_DETERMINATE = frozenset({"judge_unparseable", "judge_error", "arm_error", UNSCORED})
RATES = ("eaf_or_substitution", "eaf", "substitution", "confabulation")


class ValidationError(RuntimeError):
    """Raised when the validation report cannot be built."""


# --------------------------------------------------------------------------- #
# One record's judge flags
# --------------------------------------------------------------------------- #


def judge_flags(entry: Mapping[str, Any] | None) -> dict[str, Any]:
    """Label and the four flags of one score line (``None``: no score line)."""
    from binding_gate.scorers.llm_judge import LABEL_EAF_OR_SUBSTITUTION

    if entry is None:
        return {"label": UNSCORED, "eaf_or_substitution": False, "eaf": False, "substitution": False,
                "confabulation": False, "determinate": False}
    score = entry.get("score") or {}
    label = str(score.get("label", UNSCORED))
    judgment = (score.get("details") or {}).get("judgment") or {}
    return {
        "label": label,
        "eaf_or_substitution": label == LABEL_EAF_OR_SUBSTITUTION,
        "eaf": judgment.get("failure_detected") is True,
        "substitution": judgment.get("entity_substitution_detected") is True,
        "confabulation": isinstance(judgment.get("confabulation_count"), int) and judgment["confabulation_count"] > 0,
        "determinate": label not in NOT_DETERMINATE,
    }


# --------------------------------------------------------------------------- #
# Loading the subsample
# --------------------------------------------------------------------------- #


def _sample_ids(run: Any, sample: Mapping[str, Any] | None) -> set[str]:
    from binding_gate.sampling import sample_items

    items = list(run.items.values())
    return {item.id for item in (sample_items(items, sample) if sample is not None else items)}


def load_units(
    run_dirs: Sequence[str | Path], *, arm: str, sample: Mapping[str, Any] | None, repo_root: str | Path
) -> dict[str, list[dict[str, Any]]]:
    """Per model label: one unit per subsample record of ``arm`` (item, group, stratum, judge flags, our outcome)."""
    from binding_gate.baseline_report import record_outcome
    from binding_gate.scorers.llm_judge import load_run, read_scores, record_key

    units: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str, int]] = set()
    for run_dir in run_dirs:
        run = load_run(run_dir, repo_root=repo_root)
        kept = _sample_ids(run, sample)
        scores = read_scores(run_dir)
        for record in run.records:
            if record.arm != arm or record.item_id not in kept:
                continue
            key = (record.model, record.item_id, record.repeat)
            if key in seen:
                raise ValidationError(f"duplicate record {key} of arm {arm!r} across the run directories")
            seen.add(key)
            unit = {
                "item_id": record.item_id,
                "group_id": record.group_id,
                "stratum": record.stratum,
                "repeat": record.repeat,
                "record": record,
                **judge_flags(scores.get(record_key(record))),
            }
            if arm == STRUCTURED_ARM:
                unit["ours_wrong_entity"] = record_outcome(record) == "wrong_entity"
            units.setdefault(record.model, []).append(unit)
    return units


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def _interval(rows: list[list[int]], statistics: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if not rows:
        return {name: {"low": None, "high": None, "defined": 0, "undefined": 0} for name in statistics}
    intervals = cluster_bootstrap(
        rows, statistics, resamples=prereg.BOOTSTRAP_RESAMPLES, seed=prereg.BOOTSTRAP_SEED, level=prereg.CI_LEVEL
    )
    return {name: interval.to_dict() for name, interval in intervals.items()}


def _clusters(units: Iterable[Mapping[str, Any]], columns: Sequence[Any]) -> list[list[int]]:
    """Integer column totals per ``group_id`` (sorted), from per-unit column functions."""
    by_group: dict[str, list[int]] = {}
    for unit in units:
        row = by_group.setdefault(unit["group_id"], [0] * len(columns))
        for k, column in enumerate(columns):
            row[k] += int(column(unit))
    return [by_group[g] for g in sorted(by_group)]


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def arm_summary(units: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Judge rates of one arm and model over the subsample (see the module docstring)."""
    n = len(units)
    determinate = [u for u in units if u["determinate"]]
    labels = Counter(u["label"] for u in units)
    out: dict[str, Any] = {
        "records": n,
        "groups": len({u["group_id"] for u in units}),
        "labels": dict(sorted(labels.items())),
        "judge_unparseable": labels.get("judge_unparseable", 0),
        "judge_error": labels.get("judge_error", 0),
        "unscored": labels.get(UNSCORED, 0),
        "arm_error": labels.get("arm_error", 0),
        "determinate": len(determinate),
        "counts": {name: sum(bool(u[name]) for u in units) for name in RATES},
        "rates": {name: _rate(sum(bool(u[name]) for u in units), n) for name in RATES},
        "rates_determinate_only": {
            name: _rate(sum(bool(u[name]) for u in determinate), len(determinate)) for name in RATES
        },
    }
    columns = [lambda u: 1, *[(lambda u, name=name: u[name]) for name in RATES]]
    statistics = {name: Ratio((k + 1,), (0,)) for k, name in enumerate(RATES)}
    out["intervals"] = _interval(_clusters(units, columns), statistics)
    return out


def agreement(units: Sequence[Mapping[str, Any]], scores_by_record: Mapping[Any, Any]) -> dict[str, Any]:
    """V1: ``llm_judge.agreement_rows`` over the subsample records of one model (structured answers)."""
    from binding_gate.scorers.llm_judge import agreement_rows

    rows = agreement_rows([u["record"] for u in units], scores_by_record)
    if not rows:
        return {}
    row = rows[0]
    keep = ("records", "unscored", "judged_population", "judge_failures", "raw_agreement", "kappa",
            "kappa_parsed_only", "both_positive", "only_ours_positive", "only_judge_positive", "crosstab")
    out = {k: row[k] for k in keep}
    ours = sum(bool(u.get("ours_wrong_entity")) for u in units)
    non_null = [u for u in units if (u["stratum"] or "").split("/")[-1] != "null_control"]
    out["deterministic_wrong_entity"] = ours
    out["deterministic_rate"] = _rate(ours, len(units))
    out["deterministic_rate_non_null_control"] = _rate(sum(bool(u.get("ours_wrong_entity")) for u in non_null), len(non_null))
    return out


def paired_comparison(structured: Sequence[Mapping[str, Any]], free: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """V2: free-form minus structured rate of the positive label, paired by item (one model)."""
    s_by_item = {(u["item_id"], u["repeat"]): u for u in structured}
    f_by_item = {(u["item_id"], u["repeat"]): u for u in free}
    keys = sorted(set(s_by_item) & set(f_by_item))
    pairs = [{"group_id": s_by_item[k]["group_id"], "s": s_by_item[k], "f": f_by_item[k]} for k in keys]
    columns = [
        lambda p: 1,
        lambda p: p["s"]["eaf_or_substitution"],
        lambda p: p["f"]["eaf_or_substitution"],
        lambda p: p["s"]["determinate"] and p["f"]["determinate"],
        lambda p: p["s"]["determinate"] and p["f"]["determinate"] and p["s"]["eaf_or_substitution"],
        lambda p: p["s"]["determinate"] and p["f"]["determinate"] and p["f"]["eaf_or_substitution"],
    ]
    rows = _clusters(pairs, columns)
    totals = [sum(col) for col in zip(*rows)] if rows else [0] * len(columns)
    structured_rate, free_rate = Ratio((1,), (0,)), Ratio((2,), (0,))
    s_det, f_det = Ratio((4,), (3,)), Ratio((5,), (3,))
    statistics = {
        "structured_rate": structured_rate,
        "free_form_rate": free_rate,
        "difference": Diff(free_rate, structured_rate),
        "structured_rate_determinate": s_det,
        "free_form_rate_determinate": f_det,
        "difference_determinate": Diff(f_det, s_det),
    }

    def exact(expr: Any) -> float | None:
        value = expr.evaluate(totals, exact=True)
        return None if value is None else float(Fraction(value))

    return {
        "paired_items": len(pairs),
        "groups": len(rows),
        "unpaired_structured": len(set(s_by_item) - set(f_by_item)),
        "unpaired_free_form": len(set(f_by_item) - set(s_by_item)),
        "both_determinate": totals[3],
        "both_positive": sum(p["s"]["eaf_or_substitution"] and p["f"]["eaf_or_substitution"] for p in pairs),
        "only_structured_positive": sum(p["s"]["eaf_or_substitution"] and not p["f"]["eaf_or_substitution"] for p in pairs),
        "only_free_form_positive": sum(p["f"]["eaf_or_substitution"] and not p["s"]["eaf_or_substitution"] for p in pairs),
        "estimates": {name: exact(expr) for name, expr in statistics.items()},
        "intervals": _interval(rows, statistics),
    }


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def build_report(
    *,
    sample_config: str | Path,
    structured_dirs: Sequence[str | Path],
    free_form_dirs: Sequence[str | Path] = (),
    repo_root: str | Path = ".",
    baseline_workflow_run_id: int | None = None,
) -> dict[str, Any]:
    from binding_gate.scorers.llm_judge import RescoreConfig, read_scores, record_key

    if not structured_dirs:
        raise ValidationError("at least one dev-baseline run directory (--structured) is required")
    config = RescoreConfig.load(Path(repo_root) / sample_config if not Path(sample_config).is_absolute() else sample_config)
    if config.sample is None:
        raise ValidationError(f"{sample_config}: the validation needs the fixed subsample ([sample])")
    structured = load_units(structured_dirs, arm=STRUCTURED_ARM, sample=config.sample, repo_root=repo_root)
    free = load_units(free_form_dirs, arm=FREE_FORM_ARM, sample=config.sample, repo_root=repo_root) if free_form_dirs else {}
    if not structured:
        raise ValidationError(f"no record of arm {STRUCTURED_ARM!r} in the subsample of the given run directories")
    scores: dict[Any, Any] = {}
    for run_dir in structured_dirs:
        scores.update(read_scores(run_dir))

    models = sorted(set(structured) | set(free))
    report: dict[str, Any] = {
        "validation_version": VALIDATION_VERSION,
        "status": "EXPLORATORY, post hoc - decides nothing; layer-1 confirmatory work stays stopped (section 10)",
        "amendment": AMENDMENT,
        "decided_after": (
            "the layer-1 dev baseline (no_gate, deterministic provenance scoring) gave STOP under section 10 "
            f"(workflow run {LAYER1_BASELINE_WORKFLOW_RUN})"
        ),
        "baseline_workflow_run_id": baseline_workflow_run_id,
        "split": "dev",
        "sample": {"config": Path(sample_config).as_posix(), **config.sample},
        "judges": [{"label": j.label, "name": j.name, "model_id": j.options.get("model_id")} for j in config.judges],
        "judge_hash": config.judge_hash,
        "bootstrap": {"unit": "group_id", "seed": prereg.BOOTSTRAP_SEED, "resamples": prereg.BOOTSTRAP_RESAMPLES,
                      "level": str(prereg.CI_LEVEL)},
        "definitions": {
            "positive": "judge label eaf_or_substitution (claim attributed to the competing document, or entity substitution)",
            "rates": "over all subsample records of the arm and model; judge failures, unscored records and arm errors "
                     "count as not positive; rates_determinate_only leave them out",
            "agreement": "ours = provenance wrong_entity, judge = eaf_or_substitution; judge failures count as disagreement",
            "difference": "free_form rate minus structured (no_gate) rate on the same items, paired; cluster bootstrap",
        },
        "models": {},
    }
    for model in models:
        s_units, f_units = structured.get(model, []), free.get(model, [])
        row: dict[str, Any] = {}
        if s_units:
            row["structured"] = arm_summary(s_units)
            row["structured"]["agreement"] = agreement(
                s_units, {record_key(u["record"]): scores[record_key(u["record"])]
                          for u in s_units if record_key(u["record"]) in scores}
            )
        if f_units:
            row["free_form"] = arm_summary(f_units)
        if s_units and f_units:
            row["paired"] = paired_comparison(s_units, f_units)
        report["models"][model] = row
    report["notes"] = _notes(report, free_form_given=bool(free_form_dirs))
    return report


def _notes(report: Mapping[str, Any], *, free_form_given: bool) -> list[str]:
    notes = []
    if not free_form_given:
        notes.append("no free_form run directory was given: V2 (the paired comparison) is missing")
    for model, row in report["models"].items():
        for arm in ("structured", "free_form"):
            summary = row.get(arm)
            if summary and summary["unscored"]:
                notes.append(f"{model} / {arm}: {summary['unscored']} subsample record(s) have no judge score "
                             "(budget cap reached or the judge step failed); they count as not positive")
            if summary and summary["judge_unparseable"] + summary["judge_error"]:
                notes.append(f"{model} / {arm}: {summary['judge_unparseable']} unparseable judge output(s) and "
                             f"{summary['judge_error']} failed judge call(s)")
    return notes


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _ci(interval: Mapping[str, Any] | None) -> str:
    if not interval or interval.get("low") is None:
        return "n/a"
    return f"[{100 * interval['low']:.1f}, {100 * interval['high']:.1f}]"


def _num(value: Any) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def markdown(report: Mapping[str, Any]) -> str:
    sample = report["sample"]
    lines = [
        "## Layer-1 STOP validation (EXPLORATORY, post hoc; decides nothing)",
        "",
        f"- {report['amendment']}; decided after {report['decided_after']}.",
        f"- Subsample: {sample['per_stratum']} items per condition, seed {sample['seed']}, excluded "
        f"{', '.join(sample.get('exclude_strata', [])) or 'none'} ({sample['config']}).",
        f"- Judge: {', '.join(str(j['model_id']) for j in report['judges'])} (benchmark judge prompt); intervals: "
        f"cluster bootstrap by group_id, seed {report['bootstrap']['seed']}, {report['bootstrap']['resamples']} "
        f"resamples, level {report['bootstrap']['level']}.",
        "",
        "### Judge rates (all subsample records; 95% interval)",
        "",
        "| model | arm | records | EAF or substitution | EAF | substitution | confabulation | judge unparseable / error | unscored |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for model, row in report["models"].items():
        for arm in ("structured", "free_form"):
            s = row.get(arm)
            if not s:
                continue
            r, ci = s["rates"], s["intervals"]
            lines.append(
                f"| {model} | {arm} | {s['records']} | {_pct(r['eaf_or_substitution'])} {_ci(ci['eaf_or_substitution'])} | "
                f"{_pct(r['eaf'])} | {_pct(r['substitution'])} | {_pct(r['confabulation'])} | "
                f"{s['judge_unparseable']} / {s['judge_error']} | {s['unscored']} |"
            )
    lines += ["", "### V1: agreement with the deterministic label (structured answers)", "",
              "| model | deterministic wrong-entity | judge EAF or substitution | raw agreement | kappa | kappa (parsed only) | only ours | only judge |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for model, row in report["models"].items():
        s = row.get("structured")
        if not s or not s.get("agreement"):
            continue
        a = s["agreement"]
        lines.append(
            f"| {model} | {_pct(a['deterministic_rate'])} | {_pct(s['rates']['eaf_or_substitution'])} | "
            f"{_pct(a['raw_agreement'])} | {_num(a['kappa'])} | {_num(a['kappa_parsed_only'])} | "
            f"{a['only_ours_positive']} | {a['only_judge_positive']} |"
        )
    lines += ["", "### V2: free-form minus structured, paired by item (95% interval)", "",
              "| model | paired items | structured | free form | difference | difference (both determinate) | only structured | only free form |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for model, row in report["models"].items():
        p = row.get("paired")
        if not p:
            continue
        e, ci = p["estimates"], p["intervals"]
        lines.append(
            f"| {model} | {p['paired_items']} | {_pct(e['structured_rate'])} | {_pct(e['free_form_rate'])} | "
            f"{_pct(e['difference'])} {_ci(ci['difference'])} | {_pct(e['difference_determinate'])} "
            f"{_ci(ci['difference_determinate'])} | {p['only_structured_positive']} | {p['only_free_form_positive']} |"
        )
    lines += [""] + [f"- NOTE: {n}" for n in report.get("notes", [])]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m binding_gate.validation", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("report", help="the V1/V2 validation report from traces and judge scores; calls nothing")
    sub.add_argument("--sample-config", required=True, help="re-scoring config whose [sample] defines the subsample")
    sub.add_argument("--structured", nargs="+", required=True, help="dev-baseline run directories (judge-scored)")
    sub.add_argument("--free-form", nargs="*", default=[], help="free_form run directories (judge-scored)")
    sub.add_argument("--repo-root", default=".")
    sub.add_argument("--baseline-workflow-run-id", type=int, default=None)
    sub.add_argument("--json", required=True)
    sub.add_argument("--markdown", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_report(
            sample_config=args.sample_config,
            structured_dirs=args.structured,
            free_form_dirs=args.free_form,
            repo_root=args.repo_root,
            baseline_workflow_run_id=args.baseline_workflow_run_id,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    Path(args.json).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text = markdown(report)
    if args.markdown:
        Path(args.markdown).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
