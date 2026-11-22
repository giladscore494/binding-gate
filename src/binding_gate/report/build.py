"""The public report: inputs, integrity checks, and the report as document blocks.

What the report consumes (it never reads traces itself, never calls a model and
never re-derives a decision):

* one or more ``analysis.json`` files written by ``binding_gate.stats`` (one per
  dataset and split), with the figures that ``binding_gate.plots`` wrote beside
  them in ``figures/``;
* optionally the run directories, for the run manifests and the runner's
  invocation log (``invocations.jsonl``: the USD spend per model, budget caps,
  unpriced clients); a run directory that is in no analysis is listed as a run
  that was not analysed (for example one that failed);
* optionally the baseline report JSON of section 10 (``python -m
  binding_gate.cli report RUN_DIR --json FILE``), for the stop-rule outcome;
* optionally the audit analysis (``python -m binding_gate.audit_tool analyze``);
* optionally a deviations file: a JSON list of ``{"description": ...,
  "amendment": <row number of section 18 or null>}``;
* PREREGISTRATION.md, for the status, the rules of section 11, the commitments
  of section 15, the threats of section 16 and the amendments of section 18.

Confirmatory status is taken from the analysis file as it is: a hypothesis
result is shown as confirmatory only if the analysis says ``confirmatory:
true``. An analysis that marks a result confirmatory while also listing reasons
against confirmatory status (or while its split is not ``test``, or while the
given PREREGISTRATION.md is not FROZEN) is refused, so that a hand-edited or
inconsistent file cannot be published as confirmatory.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from binding_gate.report import prereg_doc
from binding_gate.report.document import (
    Block,
    BulletList,
    Callout,
    Figure,
    Heading,
    Paragraph,
    Table,
    render_html,
    render_markdown,
)

REPORT_VERSION = 1
SUPPORTED, NOT_SUPPORTED, TRADE_OFF, NOT_EVALUABLE = "SUPPORTED", "NOT SUPPORTED", "TRADE-OFF", "NOT EVALUABLE"
HYPOTHESES = ("H1", "H2", "H3", "H4")
LAYERS = {1: "deceptive_grounding", 2: "grab_rag", 3: "sealqa"}
RESERVED_ARMS = ("no_gate", "prompt_entity_check", "faithfulness_checker", "llm_judge_checks", "gated", "gated_research")
KEY_ARMS = ("no_gate", "gated", "faithfulness_checker", "gated_research")
LAYER1_CX = ("absent", "partial", "complete")
LAYER1_CY = ("null_control", "class_proximate", "context_adjacent", "prior_completing", "synthetic_y")

SECTIONS = {
    "status": "Status of this report",
    "headline": "Headline: H1-H4 outcomes",
    "negative": "Negative results and failures",
    "confirmatory": "Confirmatory results",
    "exploratory": "Exploratory results",
    "coverage": "Coverage: layers, arms, models and conditions",
    "stop-rule": "Stop rule (pre-registration section 10)",
    "audit": "Human audit (pre-registration section 13)",
    "costs": "Costs actually spent",
    "deviations": "Deviations and amendments",
    "threats": "Threats to validity",
    "artifacts": "Run artifacts and input hashes",
    "commitments": "Reporting commitments (pre-registration section 15)",
}

COMMITMENT_SECTIONS = {
    "R1": ("headline", "confirmatory", "exploratory"),
    "R2": ("confirmatory", "exploratory"),
    "R3": ("coverage", "exploratory"),
    "R4": ("headline", "exploratory"),
    "R5": ("negative",),
    "R6": ("stop-rule",),
    "R7": ("deviations",),
    "R8": ("threats",),
    "R9": ("costs",),
    "R10": ("artifacts",),
    "R11": ("audit",),
    "R12": ("headline", "exploratory"),
}
"""Where the report meets each commitment of PREREGISTRATION.md section 15."""


class ReportError(RuntimeError):
    """Raised when the inputs cannot be published as they are."""


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


@dataclass
class InputFile:
    data: Any
    path: str
    sha256: str
    figures: list[Path] = field(default_factory=list)


@dataclass
class RunDir:
    path: str
    manifest: dict[str, Any]
    invocations: list[dict[str, Any]]


@dataclass
class ReportInputs:
    analyses: list[InputFile]
    prereg_text: str
    prereg_path: str = "PREREGISTRATION.md"
    run_dirs: list[RunDir] = field(default_factory=list)
    baselines: list[InputFile] = field(default_factory=list)
    audit: InputFile | None = None
    deviations: list[dict[str, Any]] = field(default_factory=list)
    deviations_file: InputFile | None = None
    title: str = "binding-gate: public report"


def _read_json(path: str | Path) -> InputFile:
    path = Path(path)
    try:
        raw = path.read_bytes()
        return InputFile(json.loads(raw.decode("utf-8")), path.as_posix(), hashlib.sha256(raw).hexdigest())
    except (OSError, ValueError) as exc:
        raise ReportError(f"cannot read {path}: {exc}") from exc


def _read_run_dir(path: str | Path) -> RunDir:
    path = Path(path)
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportError(f"{path}: no readable manifest.json: {exc}") from exc
    invocations_path = path / "invocations.jsonl"
    invocations = []
    if invocations_path.exists():
        invocations = [json.loads(line) for line in invocations_path.read_text(encoding="utf-8").splitlines() if line]
    return RunDir(path.as_posix(), manifest, invocations)


def load_inputs(
    analysis_paths: Sequence[str | Path],
    *,
    prereg_path: str | Path,
    run_dirs: Sequence[str | Path] = (),
    baseline_paths: Sequence[str | Path] = (),
    audit_path: str | Path | None = None,
    deviations_path: str | Path | None = None,
    title: str | None = None,
) -> ReportInputs:
    """Read every input. An analysis path may be ``analysis.json`` or the directory holding it."""
    if not analysis_paths:
        raise ReportError("at least one analysis (analysis.json or its directory) is required")
    analyses = []
    for given in analysis_paths:
        given = Path(given)
        file = given / "analysis.json" if given.is_dir() else given
        entry = _read_json(file)
        figures_dir = file.parent / "figures"
        entry.figures = sorted(figures_dir.glob("*.png")) if figures_dir.is_dir() else []
        analyses.append(entry)
    deviations_file = _read_json(deviations_path) if deviations_path else None
    deviations = list(deviations_file.data) if deviations_file else []
    for entry in deviations:
        if not isinstance(entry, dict) or not isinstance(entry.get("description"), str):
            raise ReportError("the deviations file must be a JSON list of {\"description\": ..., \"amendment\": ...}")
    inputs = ReportInputs(
        analyses=analyses,
        prereg_text=Path(prereg_path).read_text(encoding="utf-8"),
        prereg_path=Path(prereg_path).as_posix(),
        run_dirs=[_read_run_dir(d) for d in run_dirs],
        baselines=[_read_json(p) for p in baseline_paths],
        audit=_read_json(audit_path) if audit_path else None,
        deviations=deviations,
        deviations_file=deviations_file,
    )
    if title:
        inputs.title = title
    return inputs


# --------------------------------------------------------------------------- #
# Integrity checks
# --------------------------------------------------------------------------- #

_REQUIRED_ANALYSIS_KEYS = ("analysis_version", "dataset", "layer", "split", "parameters", "inputs", "models", "confirmatory_blockers", "summary")


def check_inputs(inputs: ReportInputs) -> None:
    """Refuse inputs that cannot be published as they are (see the module docstring)."""
    seen: set[tuple[str, str]] = set()
    for entry in inputs.analyses:
        analysis = entry.data
        missing = [k for k in _REQUIRED_ANALYSIS_KEYS if k not in analysis]
        if missing:
            raise ReportError(f"{entry.path}: not an analysis file of binding_gate.stats (missing {missing})")
        if analysis["analysis_version"] != 1:
            raise ReportError(f"{entry.path}: analysis_version {analysis['analysis_version']} is not supported")
        key = (analysis["dataset"], analysis["split"])
        if key in seen:
            raise ReportError(f"two analyses of dataset {key[0]!r}, split {key[1]!r}; give one per dataset and split")
        seen.add(key)
        for model, data in analysis["models"].items():
            for name, result in data["hypotheses"].items():
                if not result.get("confirmatory"):
                    continue
                problems = []
                if analysis["split"] != "test":
                    problems.append(f"split is {analysis['split']!r}")
                if analysis["confirmatory_blockers"]:
                    problems.append("the analysis lists confirmatory blockers")
                if result.get("non_confirmatory_reasons"):
                    problems.append("the result lists reasons against confirmatory status")
                if result.get("status") == NOT_EVALUABLE:
                    problems.append("the result is NOT EVALUABLE")
                if problems:
                    raise ReportError(
                        f"{entry.path}: {name} of model {model!r} is marked confirmatory, but "
                        + "; ".join(problems)
                        + ". The file is inconsistent; regenerate it with binding_gate.stats."
                    )
    status = prereg_doc.status(inputs.prereg_text)
    if status != "FROZEN" and _confirmatory_results(inputs):
        raise ReportError(
            f"the analyses mark results confirmatory, but {inputs.prereg_path} has status {status}; give the "
            "PREREGISTRATION.md of the frozen, tagged commit the test run was made from"
        )
    recorded = {
        run["run_id"]: run for entry in inputs.analyses for run in entry.data["inputs"]["runs"]
    }
    for run_dir in inputs.run_dirs:
        run_id = run_dir.manifest.get("run_id")
        if run_id in recorded:
            expected = recorded[run_id].get("traces_sha256")
            if expected is not None and run_dir.manifest.get("traces_sha256") != expected:
                raise ReportError(
                    f"{run_dir.path}: trace SHA-256 differs from the one recorded in the analysis for run {run_id}"
                )
    if inputs.audit is not None and inputs.audit.data.get("format") != "binding-gate-audit-analysis":
        raise ReportError(f"{inputs.audit.path}: not an audit analysis file")


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def pp(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:+.1f} pp"


def ci_text(ci: Mapping[str, Any] | Sequence[float] | None, fmt=pct) -> str:
    if ci is None:
        return "no interval"
    if isinstance(ci, Mapping):
        if ci.get("low") is None:
            return "no interval"
        text = f"[{fmt(ci['low'])}, {fmt(ci['high'])}]"
        return text + (f" ({ci['undefined']} undefined resamples)" if ci.get("undefined") else "")
    return f"[{fmt(ci[0])}, {fmt(ci[1])}]"


def est(model: Mapping[str, Any], key: str, fmt=pct, with_ci: bool = True) -> str:
    entry = model["estimates"].get(key)
    if entry is None:
        return "-"
    text = fmt(entry["value"])
    return f"{text} {ci_text(entry.get('ci'), fmt)}" if with_ci else text


def arm_key(arm: str, stat: str) -> str:
    return f"arm/{arm}/{stat}"


def comparison_key(arm: str, reference: str, stat: str) -> str:
    return f"cmp/{arm}/{reference}/{stat}"


def status_text(result: Mapping[str, Any]) -> str:
    if result["status"] == NOT_EVALUABLE:
        return NOT_EVALUABLE
    return f"{result['status']} ({'confirmatory' if result.get('confirmatory') else 'not confirmatory'})"


def analysis_title(analysis: Mapping[str, Any]) -> str:
    return f"Layer {analysis['layer']}: `{analysis['dataset']}`, split `{analysis['split']}`"


def _link(anchor: str) -> str:
    return f"[{SECTIONS[anchor]}](#{anchor})"


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


def _sorted_analyses(inputs: ReportInputs) -> list[InputFile]:
    return sorted(inputs.analyses, key=lambda e: (e.data["layer"] or 99, e.data["split"] != "test", e.data["dataset"]))


def _confirmatory_results(inputs: ReportInputs) -> list[tuple[Mapping, str, str, Mapping]]:
    out = []
    for entry in _sorted_analyses(inputs):
        for model, data in entry.data["models"].items():
            for name in HYPOTHESES:
                result = data["hypotheses"].get(name)
                if result and result.get("confirmatory"):
                    out.append((entry.data, model, name, result))
    return out


def _status_section(inputs: ReportInputs) -> list[Block]:
    status = prereg_doc.status(inputs.prereg_text)
    blocks: list[Block] = [Heading(2, SECTIONS["status"], "status")]
    confirmed = _confirmatory_results(inputs)
    blocks.append(
        Paragraph(
            f"Pre-registration: `{inputs.prereg_path}`, status **{status}**, version "
            f"{prereg_doc.version(inputs.prereg_text)}. Generated by `binding_gate.report` (report format "
            f"{REPORT_VERSION}) from the files listed in {_link('artifacts')}; no model was called and no "
            "decision was re-derived: statuses and confirmatory labels are taken from the analysis files."
        )
    )
    if status != "FROZEN":
        blocks.append(
            Callout(
                "warning",
                f"The pre-registration is {status}, not FROZEN",
                "Nothing in this report can be confirmatory evidence while the plan is not frozen. Thresholds "
                "and parameters marked PROPOSED in the pre-registration have not been approved.",
            )
        )
    if confirmed:
        blocks.append(
            Callout(
                "info",
                f"{len(confirmed)} hypothesis result(s) are confirmatory",
                f"They are listed in {_link('confirmatory')}. Everything else is exploratory.",
            )
        )
    else:
        reasons = sorted({r for e in inputs.analyses for r in e.data["confirmatory_blockers"]})
        blocks.append(
            Callout(
                "warning",
                "No result in this report is confirmatory",
                "Every number below is exploratory. Reasons given by the analysis files:",
                reasons or ["(the analyses list no global reason; see the per-hypothesis reasons)"],
            )
        )
    return blocks


def _headline_section(inputs: ReportInputs) -> list[Block]:
    rules = prereg_doc.hypothesis_rules(inputs.prereg_text)
    blocks: list[Block] = [
        Heading(2, SECTIONS["headline"], "headline"),
        Paragraph(
            "Every hypothesis for every model, each labelled confirmatory or not confirmatory exactly as the "
            "analysis file states. SUPPORTED and NOT SUPPORTED are shown in the same way."
        ),
    ]
    rows = []
    for entry in _sorted_analyses(inputs):
        for model, data in entry.data["models"].items():
            rows.append(
                [analysis_title(entry.data), f"`{model}`"]
                + [status_text(data["hypotheses"][h]) for h in HYPOTHESES]
                + [data.get("controlled_layer_claim", "")]
            )
    blocks.append(Table(["analysis", "model", *HYPOTHESES, "controlled-layer claim"], rows))
    blocks.append(Table(["hypothesis", "supported if (quoted from PREREGISTRATION.md section 11)"],
                        [[h, rules.get(h, "(not found in the document)")] for h in HYPOTHESES],
                        caption="The exact rules"))

    rows = []
    for entry in _sorted_analyses(inputs):
        for model, data in entry.data["models"].items():
            ref = data["reference"]
            for arm in data["arms"]:
                if arm not in KEY_ARMS:
                    continue
                rows.append([
                    analysis_title(entry.data), f"`{model}`", f"`{arm}`",
                    est(data, arm_key(arm, "m1")),
                    est(data, arm_key(arm, "m2")) if arm != ref else "(reference)",
                    est(data, arm_key(arm, "wrong_all")),
                    est(data, arm_key(arm, "failed_all"), with_ci=False),
                ])
    blocks.append(Table(
        ["analysis", "model", "arm", "M1 wrong among answered [95% CI]", "M2 correct answers blocked [95% CI]",
         "wrong / all [95% CI]", "unparseable + error / all"],
        rows,
        caption="Headline numbers: the primary pair, always together, with the failure rate next to it "
        "(all arms are in the exploratory section)",
    ))

    rows = []
    for entry in _sorted_analyses(inputs):
        for model, data in entry.data["models"].items():
            h1 = data["hypotheses"]["H1"]
            if h1["status"] == NOT_EVALUABLE:
                continue
            worst = (h1.get("sensitivity") or {}).get("status_under_worst_case", "n/a")
            ref, gated = data["reference"], "gated"
            rows.append([
                analysis_title(entry.data), f"`{model}`", h1["status"], worst,
                est(data, comparison_key(gated, ref, "diff_m1_worst"), pp) if ref else "-",
            ])
    if rows:
        blocks.append(Table(
            ["analysis", "model", "H1 status", "H1 status under the worst case", "worst-case M1 difference [95% CI]"],
            rows,
            caption="Sensitivity bound of section 6.4 item 7 (every unparseable/error record of the gate counted "
            "as wrong, of no_gate as not wrong); reported, never decisive",
        ))
    return blocks


def _failed_criteria(result: Mapping[str, Any]) -> str:
    failed = [c for c in result.get("criteria", []) if not c.get("passed")]
    if not failed:
        return "; ".join(result.get("notes", [])) or "no criteria recorded"
    return "; ".join(f"`{c['name']}` failed ({c['observed']})" for c in failed)


def _negative_section(inputs: ReportInputs) -> list[Block]:
    blocks: list[Block] = [
        Heading(2, SECTIONS["negative"], "negative"),
        Paragraph(
            "Placed directly after the headline and at the same level, so that what did not work is as visible "
            "as what did (pre-registration section 15, R5)."
        ),
    ]
    hypothesis_items, data_items = [], []
    for entry in _sorted_analyses(inputs):
        analysis = entry.data
        where = analysis_title(analysis)
        for model, data in analysis["models"].items():
            for name in HYPOTHESES:
                result = data["hypotheses"][name]
                if result["status"] == SUPPORTED:
                    sensitivity = result.get("sensitivity") or {}
                    worst = sensitivity.get("status_under_worst_case")
                    if name == "H1" and worst and worst != SUPPORTED:
                        hypothesis_items.append(
                            f"{where}, `{model}`: H1 is SUPPORTED but {worst} under the worst-case sensitivity bound."
                        )
                    continue
                hypothesis_items.append(
                    f"{where}, `{model}`: **{name} {result['status']}**"
                    + ("" if result["status"] == NOT_EVALUABLE else f" ({'confirmatory' if result.get('confirmatory') else 'not confirmatory'})")
                    + f" — {_failed_criteria(result)}"
                )
            claim = data.get("controlled_layer_claim", "")
            if "unacceptable cost" in claim:
                hypothesis_items.append(f"{where}, `{model}`: {claim}.")
            for arm, guard in data.get("validity_guard", {}).items():
                if guard.get("exceeded"):
                    data_items.append(
                        f"{where}, `{model}`, arm `{arm}`: unparseable + error rate {pct(guard['failed_rate'])} "
                        f"exceeds the validity limit {pct(guard['limit'])}."
                    )
            if not data.get("complete", True):
                data_items.append(
                    f"{where}, `{model}`: data incomplete or not with the pre-registered number of repeats "
                    f"({data.get('unpaired_records', 0)} unpaired record(s), repeat indices {data.get('repeats')})."
                )
        for run in analysis["inputs"].get("unfinished_runs", []):
            data_items.append(f"{where}: run directory `{run}` is unfinished.")
    data_items += _coverage_gaps(inputs)
    if not inputs.baselines:
        data_items.append("The stop-rule outcome (section 10) was not supplied to the report generator.")
    for baseline in inputs.baselines:
        if baseline.data.get("overall") != "CONTINUE":
            data_items.append(f"Stop-rule outcome in `{baseline.path}`: **{baseline.data.get('overall')}**.")
    audit = inputs.audit.data if inputs.audit else None
    if audit is None:
        data_items.append("The human audit (section 13) has not been carried out or was not supplied.")
    else:
        n = len(audit["disagreements"])
        if n:
            data_items.append(f"Human audit: the auditor disagreed with the gate on {n} case(s); all are listed in {_link('audit')}.")
        if not audit.get("complete"):
            data_items.append(f"Human audit incomplete: {len(audit.get('unlabelled', []))} case(s) unlabelled.")
    blocks.append(Callout(
        "negative", "Hypotheses not supported, not evaluable or traded off",
        "" if hypothesis_items else "None: every evaluated hypothesis was supported (see the confirmatory labels).",
        hypothesis_items,
    ))
    blocks.append(Callout(
        "negative", "Failures, gaps and incomplete data",
        "" if data_items else "None detected by the generator.",
        data_items,
    ))
    return blocks


def _coverage_gaps(inputs: ReportInputs) -> list[str]:
    items = []
    layers = {e.data["layer"] for e in inputs.analyses}
    for layer, dataset in LAYERS.items():
        if layer not in layers:
            items.append(f"Layer {layer} (`{dataset}`) is not in this report: no analysis was supplied.")
    arms = {arm for e in inputs.analyses for m in e.data["models"].values() for arm in m["arms"]}
    absent = [a for a in RESERVED_ARMS if a not in arms]
    if absent:
        items.append("Arms named in the pre-registration that are absent from every analysis: " + ", ".join(f"`{a}`" for a in absent) + ".")
    analysed_runs = {run["run_id"] for e in inputs.analyses for run in e.data["inputs"]["runs"]}
    for run_dir in inputs.run_dirs:
        manifest = run_dir.manifest
        if manifest.get("run_id") not in analysed_runs:
            items.append(
                f"Run `{manifest.get('run_id')}` (`{run_dir.path}`) is in no analysis"
                + (" and is unfinished" if manifest.get("ended_at") is None else "")
                + "."
            )
            continue
        configured = [m.get("label") or m.get("name") for m in manifest.get("config", {}).get("models", [])]
        analysed = {m for e in inputs.analyses for m in e.data["models"] if run_dir.manifest["run_id"] in {r["run_id"] for r in e.data["inputs"]["runs"]}}
        missing = [m for m in configured if m not in analysed]
        if missing:
            items.append(f"Run `{manifest['run_id']}`: model(s) {', '.join(f'`{m}`' for m in missing)} are in its config but have no analysed records.")
    return items


def _hypothesis_detail(name: str, result: Mapping[str, Any], rules: Mapping[str, str]) -> list[Block]:
    lines = [f"Rule: {rules.get(name, '(not found)')}"]
    for criterion in result.get("criteria", []):
        lines.append(
            f"{'PASS' if criterion['passed'] else 'FAIL'} `{criterion['name']}`: {criterion['rule']}; observed {criterion['observed']}"
        )
    lines += [f"note: {n}" for n in result.get("notes", [])]
    lines += [f"not confirmatory: {r}" for r in result.get("non_confirmatory_reasons", [])]
    if result.get("sensitivity"):
        lines.append(f"sensitivity bound: status under the worst case = {result['sensitivity']['status_under_worst_case']}")
    return [Paragraph(f"**{name}: {status_text(result)}**"), BulletList(lines)]


def _confirmatory_section(inputs: ReportInputs) -> list[Block]:
    rules = prereg_doc.hypothesis_rules(inputs.prereg_text)
    blocks: list[Block] = [
        Heading(2, SECTIONS["confirmatory"], "confirmatory"),
        Paragraph(
            "Only results that the analysis file marks as confirmatory (pre-registration section 8: test split, "
            "finished runs, complete data with the registered repeats and bootstrap, validity guard) appear here."
        ),
    ]
    results = _confirmatory_results(inputs)
    if not results:
        blocks.append(Callout("warning", "None", "No hypothesis result in the supplied analyses is confirmatory."))
        return blocks
    for analysis, model, name, result in results:
        blocks.append(Heading(3, f"{name}, `{model}`, {analysis_title(analysis)}", f"confirmatory-{name.lower()}-{_safe(model)}-{analysis['layer']}"))
        blocks += _hypothesis_detail(name, result, rules)
    return blocks


def _safe(text: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in text.lower())


def _model_tables(analysis: Mapping[str, Any], model: str, data: Mapping[str, Any], rules: Mapping[str, str]) -> list[Block]:
    ref = data["reference"]
    anchor = f"exploratory-{analysis['layer']}-{analysis['split']}-{_safe(model)}"
    blocks: list[Block] = [
        Heading(4, f"Model `{model}`", anchor),
        Paragraph(
            f"{data['n_groups']} clusters, {data['n_items']} items, repeats {data['repeats']}, {data['n_records']:,} "
            f"records; complete: {'yes' if data.get('complete') else '**no**'}."
        ),
    ]
    rows = []
    for arm in data["arms"]:
        rows.append([
            f"`{arm}`",
            est(data, arm_key(arm, "m1")),
            est(data, arm_key(arm, "m2")) if arm != ref else "(reference)",
            est(data, arm_key(arm, "wrong_all")),
            est(data, arm_key(arm, "correct_all")),
            est(data, arm_key(arm, "failed_all"), with_ci=False),
            est(data, arm_key(arm, "abstained_all"), with_ci=False),
        ])
    blocks.append(Table(
        ["arm", "M1 wrong among answered", "M2 correct answers blocked", "wrong / all", "correct / all",
         "unparseable + error / all", "abstained / all"],
        rows, caption="Primary pair, always together, with 95% cluster-bootstrap intervals",
    ))
    if ref:
        rows = []
        for arm in data["arms"]:
            if arm == ref:
                continue
            rows.append([
                f"`{arm}`",
                est(data, comparison_key(arm, ref, "diff_m1"), pp),
                est(data, comparison_key(arm, ref, "relred_m1")),
                est(data, comparison_key(arm, ref, "diff_wrong_all"), pp),
                est(data, comparison_key(arm, ref, "diff_m1_worst"), pp),
            ])
        if rows:
            blocks.append(Table(
                ["arm", "M1 difference", "relative reduction of M1", "wrong / all difference", "worst-case M1 difference"],
                rows, caption=f"Paired comparisons with `{ref}` (same items, model and repeats)",
            ))
    outcomes = list(next(iter(data["outcome_counts"].values())).keys()) if data["outcome_counts"] else []
    blocks.append(Table(
        ["arm", *outcomes],
        [[f"`{arm}`", *[str(counts[o]) for o in outcomes]] for arm, counts in data["outcome_counts"].items()],
        caption="Outcome table, all records (section 6.3)",
    ))
    rows = []
    for condition, per_arm in data.get("conditions", {}).items():
        cells = []
        for arm in data["arms"]:
            v = per_arm.get(arm, {})
            cell = f"wrong {pct(v.get('wrong_all'))}; M1 {pct(v.get('m1'))}"
            if arm != ref:
                cell += f"; M2 {pct(v.get('m2'))}"
            cells.append(cell)
        rows.append([f"`{condition}`", *cells])
    blocks.append(Table(["condition", *[f"`{a}`" for a in data["arms"]]], rows,
                        caption="By condition (exploratory; point estimates only)"))
    for factor, levels in (data.get("factors") or {}).items():
        rows = [[f"`{level}`", *[f"wrong {pct(v[a]['wrong_all'])}; M1 {pct(v[a]['m1'])}" for a in data["arms"]]]
                for level, v in levels.items()]
        blocks.append(Table(["level", *[f"`{a}`" for a in data["arms"]]], rows,
                            caption=f"By factor `{factor}` (exploratory)"))
    rows = []
    for arm, stats in data.get("repeat_spread", {}).items():
        entry = stats.get("wrong_all", {})
        per = ", ".join(f"r{r}: {pct(v)}" for r, v in entry.get("per_repeat", {}).items())
        rows.append([f"`{arm}`", per, pct(entry.get("min")), pct(entry.get("max"))])
    blocks.append(Table(["arm", "wrong / all per repeat", "min", "max"], rows, caption="Spread across repeats"))
    if analysis["layer"] == 3:
        placeholder = analysis["parameters"].get("correct_rule_is_placeholder")
        blocks.append(Table(
            ["arm", "M4 recovery after re-search"],
            [[f"`{a}`", est(data, arm_key(a, "m4"))] for a in data["arms"]],
            caption="M4" + (" (PLACEHOLDER correctness rule until PR 8 fixes it)" if placeholder else ""),
        ))
    blocks.append(Paragraph("Hypothesis decisions for this model:"))
    for name in HYPOTHESES:
        result = data["hypotheses"][name]
        if result.get("confirmatory"):
            blocks.append(Paragraph(f"**{name}: {status_text(result)}**, details in {_link('confirmatory')}."))
        else:
            blocks += _hypothesis_detail(name, result, rules)
    return blocks


def _exploratory_section(inputs: ReportInputs) -> list[Block]:
    rules = prereg_doc.hypothesis_rules(inputs.prereg_text)
    blocks: list[Block] = [
        Heading(2, SECTIONS["exploratory"], "exploratory"),
        Paragraph(
            "Every arm (including ablations), model and condition of every supplied analysis. Everything in this "
            "section is exploratory, except the hypothesis decisions that are explicitly labelled confirmatory "
            "(their details are in the confirmatory section)."
        ),
    ]
    for entry in _sorted_analyses(inputs):
        analysis = entry.data
        blocks.append(Heading(3, analysis_title(analysis), f"exploratory-{analysis['layer']}-{analysis['split']}"))
        params = analysis["parameters"]
        used = params.get("used", {})
        blocks.append(Paragraph(
            f"Bootstrap: {used.get('bootstrap_resamples')} cluster resamples of `{params.get('cluster', 'group_id')}`, "
            f"seed {used.get('bootstrap_seed')}, level {_level(used.get('ci_level'))}"
            + ("" if params.get("as_preregistered") else " (**differs from the pre-registration**)")
            + f"; correct outcome(s): {', '.join(params.get('correct_outcomes', []))}"
            + (" (**layer-3 placeholder**)" if params.get("correct_rule_is_placeholder") else "")
            + f". Source: `{entry.path}` (SHA-256 `{entry.sha256}`)."
        ))
        if analysis["confirmatory_blockers"]:
            blocks.append(Callout("warning", "Not confirmatory", "Reasons given by the analysis:", list(analysis["confirmatory_blockers"])))
        if analysis["layer"] == 2:
            blocks.append(Callout(
                "info", "Layer 2 is exploratory and has no threshold",
                "GRAB-RAG's misleading condition substitutes the answer, not the entity; it is a scope boundary for "
                "the gate (pre-registration section 3), not evidence for or against H1.",
            ))
        for figure in sorted(entry.figures, key=lambda p: (p.name != "primary_pair.png", p.name)):
            blocks.append(Figure(figure, f"figures/{_figure_name(analysis, figure)}", f"{analysis_title(analysis)}: `{figure.name}`"))
        for model, data in analysis["models"].items():
            blocks += _model_tables(analysis, model, data, rules)
        pooled = analysis.get("pooled_descriptive") or {}
        if pooled.get("arms"):
            blocks.append(Table(
                ["arm", "M1", "M2", "wrong / all", "correct / all", "unparseable + error / all"],
                [[f"`{a}`", pct(v.get("m1")), pct(v.get("m2")), pct(v.get("wrong_all")), pct(v.get("correct_all")), pct(v.get("failed_all"))]
                 for a, v in pooled["arms"].items()],
                caption="Pooled over models: descriptive only, no interval, no hypothesis uses it",
            ))
    return blocks


def _level(value: Any) -> str:
    try:
        return f"{float(Fraction(str(value))):.0%}"
    except (ValueError, ZeroDivisionError):
        return str(value)


def _figure_name(analysis: Mapping[str, Any], figure: Path) -> str:
    return f"layer{analysis['layer']}_{analysis['split']}_{figure.name}"


def _coverage_section(inputs: ReportInputs) -> list[Block]:
    blocks: list[Block] = [Heading(2, SECTIONS["coverage"], "coverage")]
    by_layer: dict[int, list[str]] = {}
    for entry in inputs.analyses:
        by_layer.setdefault(entry.data["layer"], []).append(entry.data["split"])
    blocks.append(Table(
        ["layer", "dataset", "in this report"],
        [[str(layer), f"`{dataset}`", ", ".join(f"split `{s}`" for s in sorted(by_layer[layer])) if layer in by_layer else "**ABSENT**"]
         for layer, dataset in LAYERS.items()],
        caption="Layers (pre-registration section 3)",
    ))
    rows = []
    for entry in _sorted_analyses(inputs):
        arms = sorted({a for m in entry.data["models"].values() for a in m["arms"]})
        rows.append([
            analysis_title(entry.data),
            ", ".join(f"`{m}`" for m in entry.data["models"]),
            ", ".join(f"`{a}`" for a in arms),
            ", ".join(f"`{a}`" for a in RESERVED_ARMS if a not in arms) or "none",
        ])
    blocks.append(Table(["analysis", "models", "arms present", "reserved arms absent"], rows, caption="Models and arms"))
    rows = []
    for entry in _sorted_analyses(inputs):
        for model, data in entry.data["models"].items():
            present = list(data.get("conditions", {}))
            missing = ""
            if entry.data["layer"] == 1:
                expected = [f"{x}/{y}" for x in LAYER1_CX for y in LAYER1_CY]
                missing = ", ".join(f"`{c}`" for c in expected if c not in present) or "none"
            rows.append([analysis_title(entry.data), f"`{model}`", str(len(present)), missing or "(no fixed list for this layer)"])
    blocks.append(Table(["analysis", "model", "conditions reported", "layer-1 conditions missing"], rows, caption="Conditions"))
    gaps = _coverage_gaps(inputs)
    if gaps:
        blocks.append(Callout("negative", "Gaps", items=gaps))
    return blocks


def _stop_rule_section(inputs: ReportInputs) -> list[Block]:
    blocks: list[Block] = [Heading(2, SECTIONS["stop-rule"], "stop-rule")]
    if not inputs.baselines:
        blocks.append(Callout("warning", "Not supplied", "No baseline report (section 10) was given to the generator, so the stop-rule outcome is not stated here."))
        return blocks
    for baseline in inputs.baselines:
        data = baseline.data
        blocks.append(Paragraph(
            f"`{baseline.path}` (SHA-256 `{baseline.sha256}`): overall outcome **{data.get('overall')}**"
            + (" (PROVISIONAL)" if data.get("provisional") else "") + f", arm `{data.get('arm')}`."
        ))
        rows = [[f"`{m.get('model')}`", pct(m.get("R")), pct(m.get("ungrounded_or_unparseable_share")), str(m.get("decision", ""))]
                for m in data.get("models", [])]
        blocks.append(Table(["model", "R (wrong-entity / all, non-null_control)", "ungrounded or unparseable share", "decision"], rows))
    return blocks


def _audit_section(inputs: ReportInputs) -> list[Block]:
    blocks: list[Block] = [Heading(2, SECTIONS["audit"], "audit")]
    if inputs.audit is None:
        blocks.append(Callout("warning", "Not carried out", "No audit analysis was supplied: the human audit of section 13 has not been carried out (or was not given to the generator)."))
        return blocks
    audit = inputs.audit.data
    blocks.append(Paragraph(
        f"`{inputs.audit.path}` (SHA-256 `{inputs.audit.sha256}`), sample `{audit['sample_sha256']}`, split "
        f"`{audit['split']}`, {audit['n_labelled']} of {audit['n_cases']} cases labelled. Auditor(s): "
        + ", ".join(f"{a.get('auditor') or '(name not recorded)'} ({a['n_labelled']} labels)" for a in audit["auditors"]) + "."
    ))
    if audit["split"] != "test":
        blocks.append(Callout("warning", "Not the pre-registered audit", f"This audit used `{audit['split']}` traces; section 13 audits frozen test runs."))
    if not audit.get("sample_as_preregistered", True):
        blocks.append(Callout("warning", "Sample parameters differ from the pre-registration", "See the deviations section."))
    for limitation in audit.get("limitations", []):
        blocks.append(Callout("warning", "Single auditor", limitation))
    comp = audit["sample_composition"]
    rows = []
    for half, strata in comp["population"].items():
        for stratum, size in strata.items():
            drawn = comp["composition"].get(half, {}).get(stratum, 0)
            rows.append([half, stratum, str(size), str(drawn), f"{size}/{drawn}" if drawn else "-"])
    blocks.append(Table(["half", "stratum", "decisions in population", "drawn", "weight N_s/n_s"], rows,
                        caption="Sample composition" + (" (judge was run: scorer disagreements over-sampled)" if comp.get("judge_run") else "")))
    for line in comp.get("shortfall", []):
        blocks.append(Callout("warning", "Shortfall", line))

    def prop(p: Mapping[str, Any]) -> str:
        return f"{pct(p['value'])} ({p['k']}/{p['n']}) {ci_text(p.get('ci'))}"

    rows = []
    for half, data in audit["per_half"].items():
        sens = audit["sensitivity_cannot_tell_as_disagreement"]["per_half"][half]
        rows.append([half, str(data["labelled"]), f"{data['yes']} / {data['no']} / {data['cannot_tell']}", prop(data["agreement"]), prop(sens)])
    unweighted = audit["sample_agreement_unweighted"]
    rows.append(["whole sample (unweighted)", str(audit["n_labelled"]), "", prop(unweighted),
                 prop(audit["sensitivity_cannot_tell_as_disagreement"]["overall"])])
    blocks.append(Table(
        ["half", "labelled", "yes / no / cannot tell", "agreement with the gate [95% CI]", "cannot tell counted as disagreement"],
        rows, caption=f"Agreement between gate and auditor. Interval method: {audit['ci_method']}",
    ))
    weighted = audit["population_weighted_agreement"]
    blocks.append(Paragraph(
        f"Population-weighted agreement: {pct(weighted['value'])} {ci_text(weighted.get('ci'))}; method: {weighted['method']}."
    ))
    dec = audit["decomposer"]
    blocks.append(Paragraph(
        f"Gate errors traced to the decomposer: {dec['decomposer_fault']} of {dec['assessed']} assessed gate errors "
        f"({pct(dec['share']['value'])} {ci_text(dec['share'].get('ci'))}); {dec['gate_errors']} gate errors in total; "
        f"categories: " + (", ".join(f"{k} {v}" for k, v in dec["by_category"].items()) or "none") + "."
    ))
    rows = [[str(d["display_number"]), d["kind"], f"`{d['model']}`", f"`{d['item_id']}`", d["decision"].upper(),
             d["auditor_label"], d["decomposition"], d["description"]] for d in audit["disagreements"]]
    blocks.append(Table(["case", "kind", "model", "item", "gate", "auditor", "decomposition", "description"], rows,
                        caption=f"Every disagreement ({len(rows)})") if rows else Paragraph("No disagreements between the gate and the auditor."))
    for rater in audit.get("inter_rater", []):
        kappa = "n/a" if rater["cohen_kappa"] is None else f"{rater['cohen_kappa']:.3f}"
        blocks.append(Paragraph(
            f"Inter-rater: {rater.get('auditor') or 'second auditor'} vs {rater.get('against') or 'primary auditor'}: "
            f"{rater['n_common']} common cases, raw agreement {pct(rater['raw_agreement'])}, Cohen's kappa {kappa}."
        ))
    return blocks


def _spend(run_dir: RunDir) -> tuple[dict[str, float], dict[str, Any], list[str], list[str], str | None]:
    ended = [e for e in run_dir.invocations if e.get("event") == "ended"]
    started = [e for e in run_dir.invocations if e.get("event") in ("started", "resumed")]
    spend = dict(ended[-1].get("spend_usd", {})) if ended else {}
    caps = dict(started[-1].get("caps_usd", {})) if started else {}
    unpriced = sorted({c for e in ended for c in e.get("unpriced_clients", [])})
    aborted = sorted({m for e in ended for m in e.get("aborted_models", [])})
    prices = started[-1].get("prices_sha256") if started else None
    return spend, caps, unpriced, aborted, prices


def _costs_section(inputs: ReportInputs) -> list[Block]:
    blocks: list[Block] = [Heading(2, SECTIONS["costs"], "costs")]
    if not inputs.run_dirs:
        blocks.append(Callout("warning", "USD spend not available", "No run directory was supplied, so the runner's recorded spend cannot be shown; token counts below come from the analyses."))
    else:
        rows, totals, unpriced_all = [], {}, set()
        for run_dir in inputs.run_dirs:
            spend, caps, unpriced, aborted, prices = _spend(run_dir)
            unpriced_all.update(unpriced)
            for model in sorted(set(spend) | set(caps)):
                totals[model] = totals.get(model, 0.0) + float(spend.get(model, 0.0))
                rows.append([f"`{run_dir.manifest.get('run_id')}`", f"`{model}`", f"${float(spend.get(model, 0.0)):.4f}",
                             "-" if caps.get(model) is None else f"${float(caps[model]):.2f}",
                             "yes" if model in aborted else "no", f"`{prices}`" if prices else "-"])
        blocks.append(Table(["run", "model", "spend (USD)", "budget cap (USD)", "cap reached", "price file SHA-256"], rows,
                            caption="Spend recorded by the runner: provider-reported token usage priced with the pinned price file; not an invoice"))
        blocks.append(Table(["model", "total spend over the supplied runs (USD)"],
                            [[f"`{m}`", f"${v:.4f}"] for m, v in sorted(totals.items())]))
        if unpriced_all:
            blocks.append(Callout("warning", "Calls that could not be priced",
                                  "These clients (for example the decomposer or a judge that is not a model under test) made calls that are not in the USD figures above; their tokens are counted below.",
                                  [f"`{c}`" for c in sorted(unpriced_all)]))
    rows = []
    keys = ["paid_calls", "cached_calls", "failed_calls", "input_tokens", "output_tokens"]
    for entry in _sorted_analyses(inputs):
        for model, data in entry.data["models"].items():
            for cost in data.get("cost", {}).values():
                keys += sorted(k for k in cost if k not in keys)
    for entry in _sorted_analyses(inputs):
        for model, data in entry.data["models"].items():
            for arm, cost in data.get("cost", {}).items():
                rows.append([analysis_title(entry.data), f"`{model}`", f"`{arm}`", *[f"{cost.get(k, 0):,}" for k in keys]])
    blocks.append(Table(["analysis", "model", "arm", *keys], rows,
                        caption="Calls and reported tokens per arm (all model calls in the trace records, including decomposer and judge calls)"))
    return blocks


def _deviations_section(inputs: ReportInputs) -> list[Block]:
    blocks: list[Block] = [Heading(2, SECTIONS["deviations"], "deviations")]
    rows = prereg_doc.amendments(inputs.prereg_text)
    if rows:
        header = list(rows[0].keys())
        blocks.append(Table(header, [[r.get(h, "") for h in header] for r in rows], caption="Amendment log (pre-registration section 18)"))
    else:
        blocks.append(Paragraph("Amendment log (section 18): no amendment recorded."))
    detected = []
    status = prereg_doc.status(inputs.prereg_text)
    if status != "FROZEN":
        detected.append(f"The pre-registration is {status}: no plan was frozen when these results were produced.")
    for entry in inputs.analyses:
        analysis = entry.data
        where = analysis_title(analysis)
        if not analysis["parameters"].get("as_preregistered", True):
            detected.append(f"{where}: bootstrap parameters differ from the pre-registration.")
        for run in analysis["inputs"]["runs"]:
            if run.get("git_dirty"):
                detected.append(f"{where}: run `{run['run_id']}` was made from a working tree with uncommitted changes.")
            if analysis["split"] == "test" and not run.get("test_unlock_tag"):
                detected.append(f"{where}: test run `{run['run_id']}` records no freeze tag.")
    if inputs.audit is not None:
        audit = inputs.audit.data
        if not audit.get("sample_as_preregistered", True):
            detected.append("The audit sample was drawn with parameters other than the pre-registered ones.")
        if audit["split"] != "test":
            detected.append(f"The audit sample was drawn from `{audit['split']}` traces, not from frozen test runs.")
    blocks.append(Callout("warning" if detected else "info", "Deviations detected in the inputs",
                          "" if detected else "None detected.", detected))
    if inputs.deviations:
        blocks.append(Table(
            ["declared deviation", "authorised by"],
            [[d["description"], f"amendment {d['amendment']}" if d.get("amendment") not in (None, "") else "**unplanned** (no amendment)"]
             for d in inputs.deviations],
            caption=f"Deviations declared in `{inputs.deviations_file.path}`" if inputs.deviations_file else "Declared deviations",
        ))
    else:
        blocks.append(Paragraph("Declared deviations: none were declared (no deviations file was given)."))
    blocks.append(Paragraph(
        "The generator detects only the deviations listed above; any other deviation must be declared in a "
        "deviations file (`--deviations`) to appear here."
    ))
    return blocks


def _threats_section(inputs: ReportInputs) -> list[Block]:
    items = [f"**{title}.** {text}" if title else text for title, text in prereg_doc.threats(inputs.prereg_text)]
    return [Heading(2, SECTIONS["threats"], "threats"), Paragraph("Reproduced from pre-registration section 16."), BulletList(items)]


def _artifacts_section(inputs: ReportInputs) -> list[Block]:
    blocks: list[Block] = [Heading(2, SECTIONS["artifacts"], "artifacts")]
    runs: dict[str, dict[str, Any]] = {}
    for entry in inputs.analyses:
        for run in entry.data["inputs"]["runs"]:
            runs.setdefault(run["run_id"], {**run, "analyses": []})["analyses"].append(analysis_title(entry.data))
    for run_dir in inputs.run_dirs:
        m = run_dir.manifest
        runs.setdefault(m.get("run_id"), {
            "run_id": m.get("run_id"), "config_hash": m.get("config_hash"), "git_commit": m.get("git_commit"),
            "git_dirty": m.get("git_dirty"), "test_unlock_tag": m.get("test_unlock_tag"),
            "traces_sha256": m.get("traces_sha256"), "trace_count": m.get("trace_count"),
            "finished": m.get("ended_at") is not None, "analyses": [],
        })
    rows = [[f"`{r['run_id']}`", f"`{r.get('config_hash')}`", f"`{r.get('git_commit')}`", "yes" if r.get("git_dirty") else "no",
             f"`{r.get('test_unlock_tag')}`" if r.get("test_unlock_tag") else "-", f"`{r.get('traces_sha256')}`",
             str(r.get("trace_count")), "yes" if r.get("finished") else "**no**", "; ".join(r["analyses"]) or "**none**"]
            for r in runs.values()]
    blocks.append(Table(["run id", "config SHA-256", "git commit", "dirty", "freeze tag", "traces.jsonl SHA-256", "records", "finished", "analysed in"], rows,
                        caption="Runs: each number traces to an append-only traces.jsonl with this SHA-256"))
    files = [["analysis", e.path, f"`{e.sha256}`"] for e in inputs.analyses]
    files += [["baseline report", e.path, f"`{e.sha256}`"] for e in inputs.baselines]
    if inputs.audit:
        files.append(["audit analysis", inputs.audit.path, f"`{inputs.audit.sha256}`"])
        files.append(["audit sample", "(referenced by the audit analysis)", f"`{inputs.audit.data['sample_sha256']}`"])
        for auditor in inputs.audit.data.get("auditors", []):
            if auditor.get("labels_sha256"):
                files.append(["audit labels", auditor.get("auditor") or "(auditor not recorded)", f"`{auditor['labels_sha256']}`"])
    if inputs.deviations_file:
        files.append(["deviations", inputs.deviations_file.path, f"`{inputs.deviations_file.sha256}`"])
    files.append(["pre-registration", inputs.prereg_path, f"`{hashlib.sha256(inputs.prereg_text.encode('utf-8')).hexdigest()}`"])
    blocks.append(Table(["input", "path", "SHA-256"], files, caption="Input files"))
    return blocks


def _commitments_section(inputs: ReportInputs) -> list[Block]:
    commitments = prereg_doc.commitments(inputs.prereg_text)
    unknown = [c.ident for c in commitments if c.ident not in COMMITMENT_SECTIONS]
    if unknown:
        raise ReportError(f"PREREGISTRATION.md section 15 lists {unknown}, which the report generator does not map to a section")
    rows = [[c.ident, c.text, ", ".join(_link(a) for a in COMMITMENT_SECTIONS[c.ident])] for c in commitments]
    return [Heading(2, SECTIONS["commitments"], "commitments"), Table(["id", "commitment", "met in"], rows)]


def build_blocks(inputs: ReportInputs) -> list[Block]:
    """The whole report as blocks (rendered by ``report.document``)."""
    check_inputs(inputs)
    blocks: list[Block] = [Heading(1, inputs.title, "top")]
    for part in (
        _status_section,
        _headline_section,
        _negative_section,
        _confirmatory_section,
        _exploratory_section,
        _coverage_section,
        _stop_rule_section,
        _audit_section,
        _costs_section,
        _deviations_section,
        _threats_section,
        _artifacts_section,
        _commitments_section,
    ):
        blocks += part(inputs)
    return blocks


def figures_of(blocks: Iterable[Block]) -> list[Figure]:
    return [b for b in blocks if isinstance(b, Figure)]


def write_report(inputs: ReportInputs, out_dir: str | Path) -> dict[str, Any]:
    """Render the report into ``out_dir``; return the paths written."""
    out_dir = Path(out_dir)
    blocks = build_blocks(inputs)
    out_dir.mkdir(parents=True, exist_ok=True)
    figures = figures_of(blocks)
    if figures:
        (out_dir / "figures").mkdir(exist_ok=True)
        for figure in figures:
            shutil.copyfile(figure.path, out_dir / figure.markdown_ref)
    (out_dir / "report.md").write_text(render_markdown(blocks), encoding="utf-8")
    (out_dir / "report.html").write_text(render_html(blocks, inputs.title), encoding="utf-8")
    return {
        "markdown": (out_dir / "report.md").as_posix(),
        "html": (out_dir / "report.html").as_posix(),
        "figures": [(out_dir / f.markdown_ref).as_posix() for f in figures],
    }
