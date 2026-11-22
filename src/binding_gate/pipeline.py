"""Helpers of the dev pipeline workflow ``.github/workflows/dev_pipeline.yml`` (PR 15).

The pipeline runs every DEV step of docs/RUNNING.md from GitHub Actions, so that
the owner only adds repository secrets and the orchestrator (an AI that can start
workflows and read their artifacts through the GitHub API, but never sees a
secret value) drives everything else. The stages and their configs are defined
here once (:data:`STAGES`); the workflow and the budget ledger read them from here.

Commands (``python -m binding_gate.pipeline <command>``)
-------------------------------------------------------
``stage-configs --stage S``
    The configs of a stage, one per line (no key, no network).
``check-secrets --json FILE [--markdown FILE]``
    Which repository secrets are set (NAMES only, never values) and the free
    preflight of every model id, judge, decomposer and the Serper search key the
    dev configs use (Serper has no free account check: its preflight is one real search, one credit). A Z.ai list-models check that fails with HTTP 404 is reported as
    "endpoint missing" with the documented fallback (``preflight_list_models =
    false``). Exit 1 if any preflight FAILED (a missing key is a skip, not a failure).
``smoke-check RUN_DIR ... --json FILE [--markdown FILE]``
    Measured output tokens (visible + hidden reasoning) and input tokens per answer
    call of the smoke run, per model id, against the assumptions of
    ``configs/prices.toml``; projects every run of ``configs/budget.toml`` with the
    measured ratio and exits 1 if any projected HIGH spend exceeds that run's cap
    (the orchestrator re-derives the caps before continuing).
``decision --report FILE --workflow-run-id N --json FILE [--markdown FILE]``
    The section 10 stop-rule decision of a dev-baseline report as a small file.
``dev-rest-gate --decision FILE [--notes-dir results]``
    Exit 5 (refused) unless the decision is CONTINUE, or a continue note for that
    workflow run is committed (:data:`CONTINUE_NOTE`).
``validate-gate --decision FILE``
    (PR 22) Exit 5 (refused) unless the decision is a final STOP: ``validate-layer1`` is the exploratory
    follow-up of the layer-1 STOP (PREREGISTRATION.md section 18, amendment 1); prints ``seed_run_id=<N>``.
``layer2-decision RUN_DIR ... --workflow-run-id N --json FILE [--markdown FILE]``
    (PR 22) the layer-2 stop rule (``binding_gate.layer2_stop``) of the SEC dev baseline as a small file.
``layer2-gate --decision FILE [--notes-dir results]``
    (PR 22) prints ``allowed=true|false``: CONTINUE, or a committed note
    ``results/dev_sec_baseline_continue.json`` for that workflow run (exit 0 either way; a STOP is a result).
``find-decision --out DIR``
    (GitHub Actions) the newest ``dev-baseline--decision`` artifact of the
    repository, downloaded into DIR; prints ``workflow_run_id=<N>``.
``sec-prepare-report --prepare FILE [--manifest FILE] [--markdown FILE]``
    (PR 17) the outcome of ``cli dataset-prepare`` for the SEC dataset: prints
    ``content_sha256=<hash>`` and ``matches_pin=<true|false>`` (the orchestrator
    reads them from the job log and from the results-log branch) and checks that
    the generated ``manifest.json`` carries the same hash. Exit 1 if it does not.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tomllib
from pathlib import Path
from typing import Any, Mapping, Sequence

ABLATIONS = (
    "configs/dev_ablation_quote_verbatim.toml",
    "configs/dev_ablation_entity_in_window.toml",
    "configs/dev_ablation_qualifiers_in_window.toml",
    "configs/dev_ablation_no_closer_competitor.toml",
    "configs/dev_ablation_premise_present.toml",
)
STAGES: dict[str, tuple[str, ...]] = {
    "check-secrets": (),
    "smoke": ("configs/dev_smoke.toml",),
    "dev-baseline": ("configs/dev_baseline.toml",),
    "dev-rest": (
        "configs/decomposer_eval_dev.toml",
        "configs/dev_repeats.toml",
        "configs/dev_gated.toml",
        "configs/dev_gated_scope.toml",  # PR 18: binding-scope candidates (section 5.1 selection)
        *ABLATIONS,
        "configs/dev_baselines.toml",
        "configs/dev_llm_judge_checks.toml",
        "configs/rescore/dev_llm_judge.toml",
    ),
    "sec-dataset-prepare": (),
    "layer2-dev": ("configs/dev_sec_smoke.toml", "configs/dev_sec_baseline.toml", "configs/dev_sec_gated.toml",
                   "configs/dev_sec_gated_scope.toml"),  # PR 18: binding-scope candidates
    "layer3-dev": ("configs/dev_live_smoke.toml", "configs/dev_live.toml"),
    # PR 22 (PREREGISTRATION.md section 18, amendment 1): exploratory validation of the layer-1 STOP
    "validate-layer1": (
        "configs/rescore/validation_judge_structured.toml",  # V1: judge on the dev-baseline answers (subsample)
        "configs/dev_validation_freeform.toml",              # V2: free-form answers of the same items (A, E, H)
        "configs/rescore/validation_judge_freeform.toml",    # V2: judge on the free-form answers
    ),
}
STAGES["all-dev"] = (STAGES["smoke"] + STAGES["dev-baseline"] + STAGES["dev-rest"] + STAGES["layer2-dev"]
                     + STAGES["layer3-dev"])
"""Paid configs of each stage, in the order the stage runs them (one invocation each).

PR 17: the layer-2 dev runs (SEC financial filings) replace the GRAB-RAG run that ended ``dev-rest``; they are
their own stage ``layer2-dev`` (smoke, baseline, then the gated run seeded from that baseline in the same
workflow run), so that layer 1 never waits for the SEC dataset pin. ``sec-dataset-prepare`` is free: it generates
the SEC dataset from EDGAR and prints the content hash the orchestrator pins (every SEC run refuses while the
pin is ``UNPINNED``; the stage is never started automatically).

PR 18: ``dev_gated_scope`` (after ``dev_gated``) and ``dev_sec_gated_scope`` (after ``dev_sec_gated``, seeded from the
same SEC baseline) run the gated arm under each candidate ``binding_scope``; the analysis jobs apply the
pre-registered selection rule to them (:func:`analyze_dev`, ``calibration --select-scope``).

PR 22: ``validate-layer1`` (PREREGISTRATION.md section 18, amendment 1) is the exploratory follow-up of the layer-1
STOP: the LLM judge on a fixed subsample of the dev-baseline answers (V1), free-form answers of the same items
(``free_form`` arm, V2) and the judge on them, then ``python -m binding_gate.validation report``. It needs a final
STOP (:func:`validate_gate`), never a CONTINUE, and is not part of ``all-dev``. ``layer2-dev`` now applies the
layer-2 stop rule (``binding_gate.layer2_stop``) after its baseline: the gated runs need CONTINUE or a note."""
FREE_STAGES = ("check-secrets", "sec-dataset-prepare")
"""Stages that make no paid call: no confirmation, no budget-ledger check."""
LAYER2_SEED_PATTERN = "layer2-dev--dev_sec_baseline--runs-*"
VALIDATION_SEED_PATTERN = "dev-baseline--dev_baseline--runs-*"
"""PR 22: the dev-baseline run directories whose answers ``validate-layer1`` judges (step V1)."""
VALIDATION_DECISION = "STOP"
"""PR 22: ``validate-layer1`` is the exploratory follow-up of a (final) section 10 STOP, and runs only after one."""
"""Artifacts of the SEC dev baseline that seed ``configs/dev_sec_gated.toml`` (same workflow run)."""

PREFLIGHT_CONFIGS = (
    "configs/dev_smoke.toml",             # the six models under test
    "configs/decomposer_eval_dev.toml",   # the decomposer (slot Dec)
    "configs/dev_llm_judge_checks.toml",  # the judge (slot J)
    "configs/dev_live_smoke.toml",        # the Serper search key (one real search: one credit)
)
STUDY_SECRETS = ("OPENAI_API_KEY", "GEMINI_API_KEY", "ZAI_API_KEY", "MOONSHOT_API_KEY", "SERPER_API_KEY",
                 "RUNS_ENCRYPTION_PASSPHRASE", "SEC_CONTACT_EMAIL")
"""Secrets the USD 50 study needs (SEC_CONTACT_EMAIL: only the layer-2 SEC dataset, PR 17)."""
DECISION_ARTIFACT = "dev-baseline--decision"
CONTINUE_NOTE = "results/dev_baseline_continue.json"
"""Committed note that lets dev-rest run after an IN-BETWEEN or MEASUREMENT-CHECK decision:
``{"workflow_run_id": <N>, "decision": "IN-BETWEEN", "continue": true, "reason": "...",
"amendment": "PREREGISTRATION.md section 18, <entry>"}``."""
NOTE_DECISIONS = ("IN-BETWEEN", "MEASUREMENT-CHECK")
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REFUSED = 5


def stage_configs(stage: str) -> tuple[str, ...]:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; stages: {sorted(STAGES)}")
    return STAGES[stage]


# --------------------------------------------------------------------------- #
# check-secrets
# --------------------------------------------------------------------------- #


def secrets_status() -> list[dict[str, Any]]:
    """Every documented secret: set or not (never its value), and whether the USD 50 study needs it."""
    from binding_gate.secrets import KNOWN_SECRETS, has_secret

    return [{"name": name, "set": has_secret(name), "needed_by_study": name in STUDY_SECRETS} for name in KNOWN_SECRETS]


def run_preflights(configs: Sequence[str] = PREFLIGHT_CONFIGS, *, repo_root: str | Path = ".") -> list[dict[str, Any]]:
    """The free preflight of every distinct client of ``configs`` (model ids, decomposer, judge, Serper search)."""
    from binding_gate import runner, secrets
    from binding_gate.config import load_config
    from binding_gate.evaluation.decomposer import build_decomposer, load_eval_config

    targets: list[tuple[str, str, Any]] = []
    for path in configs:
        full = Path(repo_root) / path
        raw = tomllib.loads(full.read_text(encoding="utf-8"))
        if "eval_version" in raw:
            client = build_decomposer(load_eval_config(full)).client
            targets.append((path, "decomposer", client))
        else:
            targets += [(path, where, client) for where, client in runner.preflight_targets(load_config(full), "all")]
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for path, where, client in targets:
        model_id = str(getattr(client, "model_id", getattr(client, "name", "?")))
        key = (type(client).__name__, model_id, getattr(client, "secret_name", "") or "")
        if key in seen:
            continue
        seen.add(key)
        row: dict[str, Any] = {"config": path, "target": where, "model_id": model_id,
                               "provider": getattr(client, "provider", None), "secret": key[2] or None}
        check = getattr(client, "preflight", None)
        if check is None:
            continue  # the page fetcher: no key, nothing to check
        if key[2] and not secrets.has_secret(key[2]):
            row.update(status="SKIPPED", detail=f"secret {key[2]} is not set")
        else:
            try:
                result = check()
                row.update(status="OK" if result.ok else "FAILED", detail=result.detail)
            except Exception as exc:  # noqa: BLE001 - reported per target
                row.update(status="FAILED", detail=f"{type(exc).__name__}: {str(exc)[:300]}")
        if row["provider"] == "zai" and row["status"] == "FAILED" and "404" in row["detail"]:
            row["diagnosis"] = (
                "Z.ai list-models endpoint missing (HTTP 404); the id itself is not the problem. Fallback: a pull "
                "request sets preflight_list_models = false for the Z.ai models; the smoke run's first call (refused "
                "for free if the id is wrong) then checks the id."
            )
        elif row["provider"] == "zai" and row["status"] == "OK" and "UNVERIFIED" in str(row["detail"]):
            row["diagnosis"] = "list-models check disabled (preflight_list_models = false); the smoke run checks the id"
        results.append(row)
    return results


def check_secrets_markdown(report: Mapping[str, Any]) -> str:
    lines = ["## Secrets (names only) and free preflights", "", "| secret | set | needed by the USD 50 study |", "|---|---|---|"]
    for row in report["secrets"]:
        lines.append(f"| `{row['name']}` | {'yes' if row['set'] else 'NO'} | {'yes' if row['needed_by_study'] else 'no (optional)'} |")
    lines += ["", "| target | model / provider id | status | detail |", "|---|---|---|---|"]
    for row in report["preflights"]:
        detail = str(row["detail"]).replace("|", "/")
        if row.get("diagnosis"):
            detail += f" -- {row['diagnosis']}"
        lines.append(f"| {row['target']} ({row['config']}) | `{row['model_id']}` | **{row['status']}** | {detail} |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# smoke-check
# --------------------------------------------------------------------------- #


def measure_usage(run_dirs: Sequence[str | Path], prices: Any) -> dict[str, dict[str, Any]]:
    """Per model id: answer calls of the model under test with reported usage, against the price-file assumptions."""
    from binding_gate import runner
    from binding_gate.config import parse_config
    from binding_gate.runner import is_cache_hit, priced_id
    from binding_gate.trace import read_manifest, read_traces

    raw: dict[str, dict[str, list[int]]] = {}
    for run_dir in run_dirs:
        config = parse_config(read_manifest(run_dir).config)
        label_ids = {spec.label: runner.model_id_of(runner.build_model(spec)) for spec in config.models}
        for record in read_traces(run_dir):
            for call in record.model_calls:
                if call.purpose != "answer" or call.client != record.model or call.response is None or is_cache_hit(call):
                    continue
                # the id the runner prices the call by (requested id, reported id, configured id of the label)
                model_id = priced_id(call, prices, label_ids) or call.response.raw.get("requested_model") or call.response.model
                slot = raw.setdefault(str(model_id), {"out": [], "in": [], "chars": [], "reasoning": [], "no_usage": []})
                if call.response.usage is None:
                    slot["no_usage"].append(1)
                    continue
                slot["out"].append(call.response.usage.output_tokens)
                slot["in"].append(call.response.usage.input_tokens)
                slot["chars"].append(sum(len(m.content) for m in call.request.messages))
                reasoning = call.response.raw.get("reasoning_tokens")
                if isinstance(reasoning, int) and not isinstance(reasoning, bool):
                    slot["reasoning"].append(reasoning)
    out: dict[str, dict[str, Any]] = {}
    a = prices.assumptions
    for model_id, slot in sorted(raw.items()):
        n = len(slot["out"])
        assumed_out = a.visible_output_tokens + prices.reasoning_high(model_id)
        row: dict[str, Any] = {"calls_with_usage": n, "calls_without_usage": len(slot["no_usage"]),
                               "assumed_output_tokens_high": assumed_out, "priced": prices.has(model_id)}
        if n:
            assumed_in = sum(a.input_tokens(c) for c in slot["chars"])
            mean_out = statistics.fmean(slot["out"])
            row.update(
                mean_output_tokens=round(mean_out, 1), max_output_tokens=max(slot["out"]),
                mean_reasoning_tokens=None if not slot["reasoning"] else round(statistics.fmean(slot["reasoning"]), 1),
                output_ratio=round(mean_out / assumed_out, 4) if assumed_out else None,
                input_ratio=round(sum(slot["in"]) / assumed_in, 4) if assumed_in else None,
            )
            if prices.has(model_id) and assumed_out and assumed_in:
                price = prices.get(model_id)
                in_cost = price.input_per_mtok * assumed_in / n
                out_cost = price.output_per_mtok * assumed_out
                share_in = in_cost / (in_cost + out_cost) if in_cost + out_cost else 0.0
                row["cost_ratio"] = round(share_in * row["input_ratio"] + (1 - share_in) * row["output_ratio"], 4)
            else:
                row["cost_ratio"] = max(row["output_ratio"] or 0.0, row["input_ratio"] or 0.0)
        out[model_id] = row
    return out


def error_records(run_dirs: Sequence[str | Path]) -> dict[str, int]:
    """Records with an ``error`` per model label (the arm crashed, e.g. the provider refused a setting)."""
    from binding_gate.trace import read_traces

    counts: dict[str, int] = {}
    for run_dir in run_dirs:
        for record in read_traces(run_dir):
            if record.error is not None:
                counts[record.model] = counts.get(record.model, 0) + 1
    return counts


def run_model_ids(entry: Mapping[str, Any], *, repo_root: str | Path = ".") -> list[str] | None:
    """Every model / provider id a budget-plan run calls (``None`` for a planned run without a config)."""
    from binding_gate import runner
    from binding_gate.config import load_config

    if not entry.get("config"):
        return None
    full = Path(repo_root) / entry["config"]
    raw = tomllib.loads(full.read_text(encoding="utf-8"))
    if "eval_version" in raw:
        return [str(raw["decomposer"]["options"]["model"]["options"]["model_id"])]
    if "rescore_version" in raw:
        return [str(j["options"]["model_id"]) for j in raw.get("judges", [])]
    config = load_config(full)
    ids = [runner.model_id_of(runner.build_model(spec)) for spec in config.models]
    aux, _problems = runner.auxiliary_clients(config)
    return sorted(set(ids) | {e.model_id for e in aux})


def smoke_check(run_dirs: Sequence[str | Path], *, repo_root: str | Path = ".") -> dict[str, Any]:
    """Measured usage of the smoke run against the budget plan (see the module docstring)."""
    from binding_gate.pricing import load_prices

    repo_root = Path(repo_root)
    prices = load_prices(repo_root / "configs" / "prices.toml")
    plan = tomllib.loads((repo_root / "configs" / "budget.toml").read_text(encoding="utf-8"))
    measured = measure_usage(run_dirs, prices)
    failures: list[str] = []
    errors = error_records(run_dirs)
    for model, count in sorted(errors.items()):
        failures.append(f"model {model}: {count} smoke record(s) ended in an error (for example HTTP 400 for a reasoning or "
                        "temperature setting; docs/RUNNING.md step 3): fix the config before continuing")
    for model_id, row in measured.items():
        if row["calls_without_usage"] and not row["calls_with_usage"]:
            failures.append(f"{model_id}: no call reported token usage; the runner would charge every call at its full "
                            "max_output_tokens, which no cap allows")
    ratios = {m: r["cost_ratio"] for m, r in measured.items() if r.get("cost_ratio") is not None}
    runs = []
    for entry in plan["run"]:
        ids = run_model_ids(entry, repo_root=repo_root)
        used = ratios if ids is None else {m: ratios[m] for m in ids if m in ratios}
        unmeasured = [] if ids is None else [m for m in ids if m not in ratios and not _free(prices, m)]
        candidates = list(used.values()) + ([1.0] if unmeasured or not used else [])
        ratio = max(candidates)
        projected = entry["high_usd"] * ratio
        stopped = entry.get("status") == "stopped"  # PR 22: cannot run (ledger), so it cannot fail the check
        row = {"id": entry["id"], "config": entry["config"], "high_usd": entry["high_usd"], "cap_usd": entry["cap_usd"],
               "ratio": round(ratio, 4), "projected_high_usd": round(projected, 4), "measured_ids": sorted(used),
               "unmeasured_ids": unmeasured, "exceeds_cap": not stopped and projected > entry["cap_usd"] + 1e-9,
               "stopped": stopped}
        if row["exceeds_cap"]:
            failures.append(f"run {entry['id']}: projected HIGH ${projected:.2f} (plan ${entry['high_usd']:.2f} x measured "
                            f"ratio {ratio:.2f}) exceeds its cap ${entry['cap_usd']:.2f}")
        runs.append(row)
    return {
        "check_version": 1,
        "error_records": errors,
        "assumptions": dict(prices.assumptions.__dict__),
        "measured": measured,
        "runs": runs,
        "projected_high_total_usd": round(sum(r["projected_high_usd"] for r in runs if not r["stopped"]), 4),
        "hard_limit_usd": plan["hard_limit_usd"],
        "ok": not failures,
        "failures": failures,
        "rule": ("ratio of a model = its measured cost per answer call / the cost per call the plan assumed (input = "
                 "characters / chars_per_token, output = visible + reasoning_tokens_high), weighting input and output by "
                 "their assumed cost; a run's projected HIGH = its planned HIGH x the largest ratio among the ids it calls "
                 "(1 for an id the smoke run did not measure; every measured id for a planned run without a config)."),
    }


def _free(prices: Any, model_id: str) -> bool:
    if not prices.has(model_id):
        return False
    price = prices.get(model_id)
    return price.input_per_mtok == 0 and price.output_per_mtok == 0 and price.per_call_usd == 0


def smoke_markdown(result: Mapping[str, Any]) -> str:
    lines = ["## Smoke usage check (measured tokens vs the budget's assumptions)", "",
             f"**{'OK - every planned run stays under its cap' if result['ok'] else 'FAILED - re-derive the caps before continuing'}**", "",
             "| model id | calls | mean output tok | assumed high | mean reasoning | output ratio | input ratio | cost ratio |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for model_id, row in result["measured"].items():
        lines.append(f"| `{model_id}` | {row['calls_with_usage']} (+{row['calls_without_usage']} without usage) | "
                     f"{row.get('mean_output_tokens')} | {row['assumed_output_tokens_high']} | {row.get('mean_reasoning_tokens')} | "
                     f"{row.get('output_ratio')} | {row.get('input_ratio')} | {row.get('cost_ratio')} |")
    lines += ["", "| run | planned high | x ratio | projected high | cap | |", "|---|---:|---:|---:|---:|---|"]
    for row in result["runs"]:
        lines.append(f"| {row['id']} | {row['high_usd']:.2f} | {row['ratio']:.2f} | {row['projected_high_usd']:.2f} | "
                     f"{row['cap_usd']:.2f} | {'stopped' if row.get('stopped') else 'EXCEEDS CAP' if row['exceeds_cap'] else 'ok'} |")
    lines += [""] + [f"- FAILURE: {f}" for f in result["failures"]]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Stop-rule decision and the dev-rest gate
# --------------------------------------------------------------------------- #


def decision_from_report(report: Mapping[str, Any], workflow_run_id: int | str) -> dict[str, Any]:
    return {
        "decision_version": 1,
        "workflow_run_id": int(workflow_run_id),
        "decision": report["overall"],
        "provisional": bool(report.get("provisional")),
        "unfinished_runs": list(report.get("unfinished_runs", [])),
        "models": {m["model"]: {"decision": m.get("decision"), "R": m.get("R"), "n_records": m.get("n_records"),
                                "measurement_check": m.get("measurement_check")} for m in report.get("models", [])},
        "seed_artifact_pattern": "dev-baseline--dev_baseline--runs-*",
        "rule": "PREREGISTRATION.md section 10 (binding_gate.baseline_report)",
    }


def decision_markdown(decision: Mapping[str, Any]) -> str:
    lines = [f"## Stop rule (section 10): **{decision['decision']}**" + (" (PROVISIONAL)" if decision["provisional"] else ""),
             "", f"workflow run {decision['workflow_run_id']}", "", "| model | decision | R | records | measurement check |",
             "|---|---|---:|---:|---|"]
    for model, row in decision["models"].items():
        rate = "-" if row["R"] is None else f"{100 * row['R']:.1f}%"
        lines.append(f"| {model} | {row['decision']} | {rate} | {row['n_records']} | {row['measurement_check']} |")
    return "\n".join(lines) + "\n"


def dev_rest_gate(
    decision: Mapping[str, Any], *, notes_dir: str | Path = "results", note: str = CONTINUE_NOTE, what: str = "dev-rest"
) -> tuple[bool, str]:
    """``(allowed, why)``: CONTINUE (final), or a committed continue note for this workflow run's decision.

    PR 22: ``note`` and ``what`` let the layer-2 stop rule (``binding_gate.layer2_stop``) use the same gate
    with its own note (``results/dev_sec_baseline_continue.json``)."""
    run_id = decision.get("workflow_run_id")
    verdict = decision.get("decision")
    if verdict == "CONTINUE" and not decision.get("provisional"):
        return True, f"dev baseline of workflow run {run_id}: CONTINUE"
    note_path = Path(notes_dir) / Path(note).name
    if verdict in NOTE_DECISIONS or (verdict == "CONTINUE" and decision.get("provisional")):
        if not note_path.is_file():
            return False, (f"dev baseline of workflow run {run_id}: {verdict}{' (provisional)' if decision.get('provisional') else ''}. "
                           f"{what} runs only after CONTINUE, or with a committed note {note} for this workflow run "
                           "(PREREGISTRATION.md section 10: the owner's decision, recorded in the amendment log)")
        try:
            content = json.loads(note_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"{note} cannot be read: {exc}"
        problems = []
        if content.get("workflow_run_id") != run_id:
            problems.append(f"its workflow_run_id is {content.get('workflow_run_id')!r}, not {run_id}")
        if content.get("decision") != verdict:
            problems.append(f"its decision is {content.get('decision')!r}, not {verdict!r}")
        if content.get("continue") is not True:
            problems.append("it does not say continue: true")
        for key in ("reason", "amendment"):
            if not isinstance(content.get(key), str) or not content[key].strip():
                problems.append(f"it has no {key}")
        if problems:
            return False, f"{note} does not allow {what}: " + "; ".join(problems)
        return True, f"dev baseline of workflow run {run_id}: {verdict}, continued by {note} ({content['amendment']})"
    if what == "dev-rest":
        return False, f"dev baseline of workflow run {run_id}: {verdict}; the study does not continue (section 10)"
    return False, f"dev baseline of workflow run {run_id}: {verdict}; {what} does not run (section 10)"


def validate_gate(decision: Mapping[str, Any]) -> tuple[bool, str]:
    """PR 22: ``(allowed, why)`` for ``validate-layer1``: the newest dev-baseline decision must be a final STOP."""
    run_id = decision.get("workflow_run_id")
    verdict = decision.get("decision")
    if verdict == VALIDATION_DECISION and not decision.get("provisional"):
        return True, (f"dev baseline of workflow run {run_id}: STOP; validate-layer1 is its exploratory follow-up "
                      "(PREREGISTRATION.md section 18, amendment 1); layer-1 confirmatory work stays stopped")
    return False, (f"dev baseline of workflow run {run_id}: {verdict}{' (provisional)' if decision.get('provisional') else ''}; "
                   "validate-layer1 is the exploratory follow-up of a final STOP only (amendment 1)")


def find_decision(api: Any, out_dir: str | Path) -> dict[str, Any]:
    """Download the newest non-expired ``dev-baseline--decision`` artifact of a trusted run (GitHub Actions)."""
    from binding_gate.ghapi import extract_zip, is_trusted_run

    from binding_gate.ledger import strip_attempt

    candidates = [a for a in api.artifacts() if not a.get("expired") and strip_attempt(str(a.get("name", ""))) == DECISION_ARTIFACT]
    candidates.sort(key=lambda a: (str(a.get("created_at", "")), int(a.get("id", 0))), reverse=True)
    for artifact in candidates:
        run_id = (artifact.get("workflow_run") or {}).get("id")
        if run_id is None or not is_trusted_run(api.run(run_id), api.repository):
            continue
        extract_zip(api.download_artifact(artifact), out_dir)
        decision = json.loads((Path(out_dir) / "decision.json").read_text(encoding="utf-8"))
        if decision.get("workflow_run_id") != run_id:
            raise ValueError(f"artifact {artifact['id']}: decision.json names workflow run {decision.get('workflow_run_id')}, "
                             f"but the artifact belongs to {run_id}")
        return decision
    raise ValueError(f"no {DECISION_ARTIFACT!r} artifact found: run the dev-baseline stage first")


# --------------------------------------------------------------------------- #
# SEC dataset preparation (PR 17; free)
# --------------------------------------------------------------------------- #


def sec_prepare_report(prepared: Mapping[str, Any], manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The pin the orchestrator commits, from the printed ``cli dataset-prepare`` output (and the manifest)."""
    from binding_gate.canonical import sha256_json

    digest = prepared.get("content_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("the dataset-prepare output has no 64-digit content_sha256")
    problems: list[str] = []
    if manifest is not None:
        if manifest.get("content_sha256") != digest:
            problems.append(f"manifest.json names content_sha256 {manifest.get('content_sha256')!r}, the output {digest!r}")
        elif sha256_json(manifest.get("content")) != digest:
            problems.append("manifest.json: content_sha256 does not match its content")
    return {
        "report_version": 1,
        "content_sha256": digest,
        "pinned_in_config": prepared.get("pinned_in_config"),
        "matches_pin": bool(prepared.get("matches_pin")),
        "n_items": prepared.get("n_items"),
        "n_companies": prepared.get("n_companies"),
        "items_by_split": prepared.get("items_by_split"),
        "items_by_stratum": prepared.get("items_by_stratum"),
        "excluded": prepared.get("excluded"),
        "network_requests": (prepared.get("retrieval") or {}).get("network_requests"),
        "cache_hits": (prepared.get("retrieval") or {}).get("cache_hits"),
        "next_step": prepared.get("next_step"),
        "problems": problems,
    }


def sec_prepare_markdown(report: Mapping[str, Any]) -> str:
    verdict = ("matches the pin in the configs" if report["matches_pin"] else
               f"NOT the pin in the configs ({report['pinned_in_config']}); pin it in a pull request (docs/RUNNING.md step 8)")
    lines = ["## SEC dataset (layer 2) generated from EDGAR", "",
             f"- content_sha256: `{report['content_sha256']}` - {verdict}",
             f"- items: {report['n_items']} from {report['n_companies']} companies; by split {report['items_by_split']}; "
             f"by stratum {report['items_by_stratum']}; excluded companies or items: {report['excluded']}",
             f"- EDGAR requests: {report['network_requests']} over the network, {report['cache_hits']} from the cache",
             f"- next step: {report['next_step']}"]
    lines += [f"- PROBLEM: {p}" for p in report["problems"]]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Calibration and analysis of every dev run (no model call, no key)
# --------------------------------------------------------------------------- #

SCOPE_SELECTION_CONFIGS = ("dev-gated-scope", "dev-sec-gated-scope")
"""PR 18: config names whose runs get the binding-scope selection (section 5.1) in :func:`analyze_dev`."""
CALIBRATION_CONFIG = "dev-gated"
CALIBRATION_GRID = ("--window-tokens", "20,40,60,80", "--premise-scope", "document,window", "--competitor-tie", "fail,pass")
"""The grid of docs/RUNNING.md step 6a, applied to the ``dev-gated`` traces only."""


def group_run_dirs(root: str | Path) -> dict[str, list[str]]:
    """Run directories (manifest + traces) under ``root``, grouped by the config name in their manifest."""
    from binding_gate.trace import TRACES_NAME, read_manifest

    groups: dict[str, list[str]] = {}
    for manifest in sorted(Path(root).rglob("manifest.json")):
        run_dir = manifest.parent
        if not (run_dir / TRACES_NAME).is_file():
            continue
        name = str(read_manifest(run_dir).config.get("name", "unnamed"))
        groups.setdefault(name, []).append(run_dir.as_posix())
    return groups


def analyze_dev(root: str | Path, out: str | Path, *, figures: bool = False) -> dict[str, Any]:
    """``cli analyze`` once per config (all its models together), the gate calibration of ``dev-gated`` and (PR 18)
    the binding-scope selection of ``dev-gated-scope`` / ``dev-sec-gated-scope`` (``scope_selection_<name>.txt|json``)."""
    import contextlib
    import io

    from binding_gate import cli
    from binding_gate.gates import calibration

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    index: dict[str, Any] = {"analyses": {}, "calibration": None, "failed": []}
    for name, run_dirs in sorted(group_run_dirs(root).items()):
        target = out / name
        argv = ["analyze", *run_dirs, "--out", str(target)] + ([] if figures else ["--no-figures"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = cli.main(argv)
        (out / f"{name}.log").write_text(buffer.getvalue(), encoding="utf-8")
        index["analyses"][name] = {"run_dirs": run_dirs, "exit_code": code, "out": target.as_posix()}
        if code != 0:
            index["failed"].append(f"analyze {name}")
        if name == CALIBRATION_CONFIG:
            buffer = io.StringIO()
            json_out = out / "calibration_dev_gated.json"
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                try:
                    code = calibration.main([*run_dirs, *CALIBRATION_GRID, "--json", str(json_out)])
                except SystemExit as exc:  # argparse errors
                    code = int(exc.code or 1)
            (out / "calibration_dev_gated.txt").write_text(buffer.getvalue(), encoding="utf-8")
            index["calibration"] = {"run_dirs": run_dirs, "exit_code": code, "json": json_out.as_posix()}
            if code != 0:
                index["failed"].append("calibration dev-gated")
        if name in SCOPE_SELECTION_CONFIGS:
            buffer = io.StringIO()
            json_out = out / f"scope_selection_{name}.json"
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                try:
                    code = calibration.main(["--select-scope", *run_dirs, "--json", str(json_out)])
                except SystemExit as exc:  # argparse errors
                    code = int(exc.code or 1)
            (out / f"scope_selection_{name}.txt").write_text(buffer.getvalue(), encoding="utf-8")
            index.setdefault("scope_selection", {})[name] = {"run_dirs": run_dirs, "exit_code": code,
                                                              "json": json_out.as_posix()}
            if code != 0:
                index["failed"].append(f"scope selection {name}")
    (out / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return index


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def _write(path: str | None, text: str) -> None:
    if path:
        Path(path).write_text(text, encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m binding_gate.pipeline", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("stage-configs")
    sub.add_argument("--stage", required=True)
    sub = commands.add_parser("check-secrets")
    sub.add_argument("--json", required=True)
    sub.add_argument("--markdown", default=None)
    sub.add_argument("--repo-root", default=".")
    sub = commands.add_parser("smoke-check")
    sub.add_argument("run_dirs", nargs="+")
    sub.add_argument("--json", required=True)
    sub.add_argument("--markdown", default=None)
    sub.add_argument("--repo-root", default=".")
    sub = commands.add_parser("decision")
    sub.add_argument("--report", required=True)
    sub.add_argument("--workflow-run-id", required=True)
    sub.add_argument("--json", required=True)
    sub.add_argument("--markdown", default=None)
    sub = commands.add_parser("dev-rest-gate")
    sub.add_argument("--decision", required=True)
    sub.add_argument("--notes-dir", default="results")
    sub = commands.add_parser("find-decision")
    sub.add_argument("--out", required=True)
    sub = commands.add_parser("validate-gate")  # PR 22
    sub.add_argument("--decision", required=True)
    sub = commands.add_parser("layer2-decision")  # PR 22
    sub.add_argument("run_dirs", nargs="+")
    sub.add_argument("--workflow-run-id", required=True)
    sub.add_argument("--json", required=True)
    sub.add_argument("--markdown", default=None)
    sub = commands.add_parser("layer2-gate")  # PR 22
    sub.add_argument("--decision", required=True)
    sub.add_argument("--notes-dir", default="results")
    sub = commands.add_parser("sec-prepare-report")
    sub.add_argument("--prepare", required=True)
    sub.add_argument("--manifest", default=None)
    sub.add_argument("--json", default=None)
    sub.add_argument("--markdown", default=None)
    sub = commands.add_parser("analyze-dev")
    sub.add_argument("root")
    sub.add_argument("--out", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "stage-configs":
            print("\n".join(stage_configs(args.stage)))
            return EXIT_OK
        if args.command == "check-secrets":
            report = {"secrets": secrets_status(), "preflights": run_preflights(repo_root=args.repo_root)}
            report["failed"] = [r["target"] + " " + r["model_id"] for r in report["preflights"] if r["status"] == "FAILED"]
            Path(args.json).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            _write(args.markdown, check_secrets_markdown(report))
            print(check_secrets_markdown(report))
            return EXIT_FAILURE if report["failed"] else EXIT_OK
        if args.command == "smoke-check":
            result = smoke_check(args.run_dirs, repo_root=args.repo_root)
            Path(args.json).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            _write(args.markdown, smoke_markdown(result))
            print(smoke_markdown(result))
            for failure in result["failures"]:
                print(f"::error::smoke usage check: {failure}")
            return EXIT_OK if result["ok"] else EXIT_FAILURE
        if args.command == "decision":
            report = json.loads(Path(args.report).read_text(encoding="utf-8"))
            decision = decision_from_report(report, args.workflow_run_id)
            Path(args.json).write_text(json.dumps(decision, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            _write(args.markdown, decision_markdown(decision))
            print(f"decision={decision['decision']}")
            return EXIT_OK
        if args.command == "dev-rest-gate":
            decision = json.loads(Path(args.decision).read_text(encoding="utf-8"))
            allowed, why = dev_rest_gate(decision, notes_dir=args.notes_dir)
            print(("ALLOWED: " if allowed else "::error::REFUSED: ") + why)
            if allowed:
                print(f"seed_run_id={decision['workflow_run_id']}")
            return EXIT_OK if allowed else EXIT_REFUSED
        if args.command == "validate-gate":
            decision = json.loads(Path(args.decision).read_text(encoding="utf-8"))
            allowed, why = validate_gate(decision)
            print(("ALLOWED: " if allowed else "::error::REFUSED: ") + why)
            if allowed:
                print(f"seed_run_id={decision['workflow_run_id']}")
            return EXIT_OK if allowed else EXIT_REFUSED
        if args.command == "layer2-decision":
            from binding_gate import layer2_stop

            decision = layer2_stop.decision_from_run_dirs(args.run_dirs, workflow_run_id=args.workflow_run_id)
            layer2_stop.write(decision, args.json, args.markdown)
            print(layer2_stop.decision_markdown(decision))
            print(f"decision={decision['decision']}")
            return EXIT_OK
        if args.command == "layer2-gate":
            from binding_gate import layer2_stop

            decision = json.loads(Path(args.decision).read_text(encoding="utf-8"))
            allowed, why = layer2_stop.gate(decision, notes_dir=args.notes_dir)
            print(("ALLOWED: " if allowed else "REFUSED: ") + why)
            print(f"allowed={'true' if allowed else 'false'}")
            return EXIT_OK
        if args.command == "find-decision":
            from binding_gate.ghapi import GitHubAPI

            decision = find_decision(GitHubAPI.from_environment(), args.out)
            print(f"workflow_run_id={decision['workflow_run_id']}")
            print(f"decision={decision['decision']}")
            return EXIT_OK
        if args.command == "sec-prepare-report":
            prepared = json.loads(Path(args.prepare).read_text(encoding="utf-8"))
            manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8")) if args.manifest else None
            report = sec_prepare_report(prepared, manifest)
            if args.json:
                Path(args.json).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            _write(args.markdown, sec_prepare_markdown(report))
            print(f"content_sha256={report['content_sha256']}")
            print(f"matches_pin={'true' if report['matches_pin'] else 'false'}")
            for problem in report["problems"]:
                print(f"::error::SEC dataset: {problem}")
            return EXIT_FAILURE if report["problems"] else EXIT_OK
        if args.command == "analyze-dev":
            index = analyze_dev(args.root, args.out)
            print(json.dumps({k: index[k] for k in ("failed",)} | {"analysed": sorted(index["analyses"])}, indent=2))
            return EXIT_FAILURE if index["failed"] or not index["analyses"] else EXIT_OK
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
