"""PR 15: static checks of the dev pipeline workflow, the reusable baseline workflow, the composite actions
and the owner page. Every workflow and action file is parsed as YAML."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest
import yaml

from binding_gate import ledger, pipeline, runner
from binding_gate.config import load_config
from binding_gate.secrets import KNOWN_SECRETS

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ACTIONS = REPO_ROOT / ".github" / "actions"
YAML_FILES = sorted([*WORKFLOWS.glob("*.yml"), *ACTIONS.glob("*/action.yml")])


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def triggers(doc: dict) -> dict:
    return doc.get("on", doc.get(True))  # YAML 1.1 reads the key `on` as true


PIPE = load(WORKFLOWS / "dev_pipeline.yml")
BASE = load(WORKFLOWS / "baseline.yml")
LEDGER_ACTION = load(ACTIONS / "ledger-check" / "action.yml")
JOBS = PIPE["jobs"]
BASELINE_CALL = "./.github/workflows/baseline.yml"


def steps(job: dict) -> list[dict]:
    return job.get("steps", [])


def all_steps() -> list[tuple[str, str, dict]]:
    out = []
    for path in YAML_FILES:
        doc = load(path)
        jobs = doc.get("jobs") or {"composite": {"steps": doc["runs"]["steps"]}}
        for name, job in jobs.items():
            out += [(path.name if path.parent == WORKFLOWS else path.parent.name, name, s) for s in steps(job)]
    return out


# --------------------------------------------------------------------------- #
# Every file parses; triggers; inputs; no expression inside a shell script
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", YAML_FILES, ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_every_workflow_and_action_is_valid_yaml(path):
    doc = load(path)
    if path.parent == WORKFLOWS:
        assert triggers(doc) and doc["jobs"]
        for job in doc["jobs"].values():
            assert "runs-on" in job or "uses" in job
    else:
        assert doc["runs"]["using"] == "composite"
        assert all(s.get("shell") == "bash" for s in doc["runs"]["steps"] if "run" in s)


def test_no_expression_is_pasted_into_a_shell_script():
    """Inputs, outputs and secrets reach scripts only through environment variables."""
    for where, job, step in all_steps():
        if "run" in step:
            assert "${{" not in step["run"], (where, job, step.get("name"))


def test_the_pipeline_is_started_only_by_hand_with_the_documented_inputs():
    on = triggers(PIPE)
    assert set(on) == {"workflow_dispatch"}
    inputs = on["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"stage", "confirm_spend"}
    assert inputs["stage"]["type"] == "choice" and inputs["stage"]["options"] == list(pipeline.STAGES)
    assert inputs["stage"]["default"] == "check-secrets"
    assert inputs["confirm_spend"]["type"] == "boolean" and inputs["confirm_spend"]["default"] is False
    assert PIPE["permissions"] == {"contents": "read", "actions": "read"}


def test_baseline_is_reusable_and_unchanged_when_started_by_hand():
    on = triggers(BASE)
    assert list(on) == ["workflow_dispatch", "workflow_call"]
    dispatch, call = on["workflow_dispatch"]["inputs"], on["workflow_call"]["inputs"]
    assert list(dispatch) == ["config", "model", "dry_run", "confirm_spend", "seed_run_id"]
    assert set(call) == set(dispatch) | {"artifact_prefix", "seed_pattern"}
    assert call["artifact_prefix"]["default"] == "" and call["seed_pattern"]["default"] == "runs-*"
    assert call["dry_run"]["default"] is True and call["confirm_spend"]["default"] is False
    assert BASE["permissions"] == {"contents": "read", "actions": "read"}


# --------------------------------------------------------------------------- #
# Stage wiring
# --------------------------------------------------------------------------- #


def calls() -> dict[str, dict]:
    return {name: job for name, job in JOBS.items() if job.get("uses") == BASELINE_CALL}


def config_of(name: str, job: dict) -> str:
    if job.get("uses") == BASELINE_CALL:
        return job["with"]["config"]
    text = "\n".join(s.get("run", "") + str(s.get("with", "")) for s in steps(job))
    for config in ("configs/decomposer_eval_dev.toml", "configs/rescore/dev_llm_judge.toml",
                   "configs/rescore/validation_judge_structured.toml", "configs/rescore/validation_judge_freeform.toml"):
        if f"--config {config}" in text:
            return config
    return ""


def test_every_config_runs_through_baseline_with_a_predictable_prefix_and_the_user_confirmation():
    stage_of = {c: s for s in ("smoke", "dev-baseline", "dev-rest", "layer2-dev", "layer3-dev", "validate-layer1")
                for c in pipeline.STAGES[s]}
    seen = []
    for name, job in calls().items():
        w = job["with"]
        config = w["config"]
        seen.append(config)
        assert w["artifact_prefix"] == f"{stage_of[config]}--{Path(config).stem}--", name
        assert w["dry_run"] is False and w["confirm_spend"] == "${{ inputs.confirm_spend }}" and w["model"] == "all"
        assert job["secrets"] == "inherit"
        seeds = runner.run_requirements(load_config(REPO_ROOT / config))["seed_runs"]
        if seeds and config in pipeline.STAGES["layer2-dev"]:
            # PR 17: dev_sec_gated reuses the answers of dev_sec_baseline from THIS workflow run
            assert w["seed_run_id"] == "${{ github.run_id }}", name
            assert w["seed_pattern"] == pipeline.LAYER2_SEED_PATTERN
            assert "layer2-baseline" in job["needs"] and "needs.layer2-baseline.result == 'success'" in job["if"]
        elif seeds:
            assert w["seed_run_id"] == "${{ needs.dev-rest-gate.outputs.seed_run_id }}", name
            assert w["seed_pattern"] == "dev-baseline--dev_baseline--runs-*"
        else:
            assert "seed_run_id" not in w and "seed_pattern" not in w
    custom = [config_of(n, j) for n, j in JOBS.items() if j.get("uses") is None and config_of(n, j)]
    assert sorted(seen + custom) == sorted(pipeline.STAGES["all-dev"] + pipeline.STAGES["validate-layer1"])


def chain(start: str, stop: str) -> list[str]:
    """Jobs from ``start`` to ``stop``, each the one job whose needs name the previous one (besides the gate)."""
    order = [start]
    while order[-1] != stop:
        nxt = [n for n, j in JOBS.items() if order[-1] in (j.get("needs") or []) and n not in order]
        assert len(nxt) == 1, (order[-1], nxt)
        order.append(nxt[0])
    return order


def test_dev_rest_runs_its_configs_in_the_order_of_the_stage_definition():
    order = chain("decomposer-eval", "dev-analysis")
    configs = [config_of(n, JOBS[n]) for n in order if config_of(n, JOBS[n])]
    assert configs == list(pipeline.STAGES["dev-rest"])
    for name in order:
        assert "dev-rest-gate" in JOBS[name]["needs"] or name == "decomposer-eval" and JOBS[name]["needs"] == "dev-rest-gate"
        assert "needs.dev-rest-gate.result == 'success'" in JOBS[name]["if"] and "!cancelled()" in JOBS[name]["if"]


def test_stage_conditions_and_the_all_dev_chain():
    assert JOBS["check-secrets"]["if"] == "${{ inputs.stage == 'check-secrets' }}"
    assert "inputs.stage == 'smoke' || inputs.stage == 'all-dev'" in JOBS["smoke"]["if"]
    baseline_if = JOBS["dev-baseline"]["if"]
    assert "inputs.stage == 'dev-baseline'" in baseline_if
    assert "needs.smoke.result == 'success' && needs.smoke-check.result == 'success'" in baseline_if
    gate_if = JOBS["dev-rest-gate"]["if"]
    assert "inputs.stage == 'dev-rest'" in gate_if and "needs.dev-baseline-decision.result == 'success'" in gate_if
    layer3_if = JOBS["layer3-smoke"]["if"]
    assert "inputs.stage == 'layer3-dev'" in layer3_if and "inputs.stage == 'all-dev' && needs.dev-rest-gate.result == 'success'" in layer3_if
    assert "dev-analysis" in JOBS["layer3-smoke"]["needs"]  # all-dev: layer 3 after dev-rest
    assert "needs.layer3-smoke.result == 'success'" in JOBS["layer3-dev"]["if"]
    assert [JOBS["layer3-smoke"]["with"]["config"], JOBS["layer3-dev"]["with"]["config"]] == list(pipeline.STAGES["layer3-dev"])
    for name, job in JOBS.items():
        if name != "plan":
            needs = job.get("needs") or []
            needs = [needs] if isinstance(needs, str) else needs
            assert needs, name  # every job hangs below the plan job (directly or through the chain)


FREE = " && ".join(f"inputs.stage != '{s}'" for s in pipeline.FREE_STAGES)


def test_paid_stages_need_confirmation_and_check_secrets_spends_nothing():
    plan = {s.get("name"): s for s in steps(JOBS["plan"])}
    refuse = plan["Refuse a paid stage that was not confirmed"]
    assert refuse["if"] == f"${{{{ {FREE} && !inputs.confirm_spend }}}}" and "exit 1" in refuse["run"]
    text = yaml.safe_dump(JOBS["check-secrets"])
    assert "--confirm-spend" not in text and "binding_gate.pipeline check-secrets" in text
    assert "mode: report" in text.replace("'", "")
    assert "secrets." not in yaml.safe_dump(JOBS["plan"])
    # PR 17: the SEC dataset preparation is free (no model key reaches it) and only its own stage starts it
    prepare = JOBS["sec-dataset-prepare"]
    assert prepare["if"] == "${{ inputs.stage == 'sec-dataset-prepare' }}" and prepare["needs"] == "plan"
    text = yaml.safe_dump(prepare)
    assert set(re.findall(r"secrets\.([A-Z_]+)", text)) == {"SEC_CONTACT_EMAIL"} and "--confirm-spend" not in text
    assert "SEC_CONTACT_EMAIL" in str(JOBS["check-secrets"]["steps"])  # reported as set or not


def test_the_sec_dataset_is_prepared_with_a_cache_and_reports_the_hash_to_pin():
    named = {s.get("name"): s for s in steps(JOBS["sec-dataset-prepare"])}
    (cache,) = [s for s in steps(JOBS["sec-dataset-prepare"]) if s.get("uses") == "actions/cache@v4"]
    assert cache["with"]["path"] == "data/raw/sec-financials/cache"
    assert cache["with"]["key"] == ("sec-edgar-v1-${{ hashFiles('configs/datasets/sec_financials_v1.toml',"
                                    "'configs/datasets/sec_financials_selection_v1.json') }}")
    generate = named["Generate the SEC dataset (no model call; the contact e-mail is never written anywhere)"]
    assert generate["env"] == {"SEC_CONTACT_EMAIL": "${{ secrets.SEC_CONTACT_EMAIL }}"}
    assert "python -m binding_gate.cli dataset-prepare --config configs/dev_sec_baseline.toml" in generate["run"]
    names = list(named)
    assert names.index("Install package") < names.index(cache["name"]) < names.index(generate["name"])
    report = named["The content hash to pin (content_sha256=... in this log and on the results-log branch)"]
    assert "binding_gate.pipeline sec-prepare-report" in report["run"]
    upload = [s for s in steps(JOBS["sec-dataset-prepare"]) if "upload-artifact" in str(s.get("uses"))]
    assert [u["with"]["path"] for u in upload] == ["sec-dataset/"]
    # no SEC run is started by this stage, and no SEC run starts it: every SEC run refuses while UNPINNED
    for name, job in calls().items():
        assert "sec-dataset-prepare" not in str(job.get("needs", "")), name


def test_layer2_dev_is_wired_smoke_check_baseline_gated_analysis():
    assert [JOBS[n]["with"]["config"] for n in ("layer2-smoke", "layer2-baseline", "layer2-gated", "layer2-gated-scope")] == list(
        pipeline.STAGES["layer2-dev"])  # PR 18: the binding-scope candidates after the gated run
    # PR 22: the layer-2 stop rule sits between the baseline and the gated runs
    for name in ("layer2-gated", "layer2-gated-scope"):
        assert "layer2-decision" in JOBS[name]["needs"] and "needs.layer2-decision.outputs.allowed == 'true'" in JOBS[name]["if"]
    smoke_if = JOBS["layer2-smoke"]["if"]
    assert "inputs.stage == 'layer2-dev'" in smoke_if and "inputs.stage == 'all-dev' && needs.dev-rest-gate.result == 'success'" in smoke_if
    assert "dev-analysis" in JOBS["layer2-smoke"]["needs"]  # all-dev: layer 2 after dev-rest
    assert "needs.layer2-smoke-check.result == 'success'" in JOBS["layer2-baseline"]["if"]
    (pattern,) = [s["with"]["pattern"] for s in steps(JOBS["layer2-smoke-check"]) if "download-artifact" in str(s.get("uses"))]
    assert pattern == "layer2-dev--dev_sec_smoke--runs-*"
    patterns = [s["with"]["pattern"] for s in steps(JOBS["layer2-analysis"]) if "download-artifact" in str(s.get("uses"))]
    assert patterns == ["layer2-dev--dev_sec_baseline--runs-*", "layer2-dev--dev_sec_gated--runs-*",
                        "layer2-dev--dev_sec_gated_scope--runs-*"]
    assert "layer2-gated-scope" in JOBS["layer2-analysis"]["needs"]
    # layer 3 waits for layer 2 in all-dev but does not depend on its success (e.g. while the SEC pin is UNPINNED)
    assert "layer2-analysis" in JOBS["layer3-smoke"]["needs"] and "layer2" not in JOBS["layer3-smoke"]["if"]
    assert "dev-grab-rag" not in JOBS


def test_the_gate_and_the_decision_are_wired_to_the_right_commands_and_artifacts():
    gate = {s.get("name"): s for s in steps(JOBS["dev-rest-gate"])}
    command = gate["Refuse dev-rest unless the stop rule says CONTINUE (or a committed continue note)"]["run"]
    assert "binding_gate.pipeline dev-rest-gate --decision decision/decision.json --notes-dir results" in command
    assert "seed_run_id=" in command
    assert JOBS["dev-rest-gate"]["outputs"]["seed_run_id"] == "${{ steps.gate.outputs.seed_run_id }}"
    find = gate["The newest dev-baseline decision of the repository (dev-rest)"]
    assert "binding_gate.pipeline find-decision" in find["run"] and find["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
    decision = yaml.safe_dump(JOBS["dev-baseline-decision"])
    assert "binding_gate.pipeline decision" in decision and "dev-baseline--decision" in decision
    assert pipeline.DECISION_ARTIFACT == "dev-baseline--decision"


# --------------------------------------------------------------------------- #
# Budget ledger before every paid step
# --------------------------------------------------------------------------- #


def ledger_steps(job: dict) -> list[dict]:
    return [s for s in steps(job) if s.get("uses") == "./.github/actions/ledger-check"]


def test_the_ledger_guards_the_stage_and_every_paid_step():
    (plan,) = ledger_steps(JOBS["plan"])
    assert plan["with"]["mode"] == "check" and plan["with"]["stage"] == "${{ env.STAGE }}"
    assert plan["if"] == f"${{{{ {FREE} }}}}"
    for name, paid in (("decomposer-eval", "Paid measurement"), ("rescore", "Judge dry run, paid re-scoring, agreement"),
                       ("validate-judge-structured", "Judge dry run, paid judge scoring of the subsample (arm no_gate), agreement"),
                       ("validate-judge-freeform", "Judge dry run, paid judge scoring (arm free_form)")):
        names = [s.get("name") for s in steps(JOBS[name])]
        (check,) = ledger_steps(JOBS[name])
        assert check["with"]["mode"] == "check" and check["with"]["config"] == config_of(name, JOBS[name])
        assert names.index(check["name"]) < names.index(paid)
        assert "--confirm-spend" in steps(JOBS[name])[names.index(paid)]["run"]
    (base,) = ledger_steps(BASE["jobs"]["plan"])
    assert base["if"] == "${{ !inputs.dry_run && inputs.confirm_spend }}"
    assert base["with"]["config"] == "${{ env.CONFIG }}" and base["with"]["model"] == "${{ env.MODEL }}"
    names = [s.get("name") for s in steps(BASE["jobs"]["plan"])]
    assert names.index(base["name"]) < names.index("Select models")  # before any model job starts
    for job in (JOBS["plan"], JOBS["decomposer-eval"], JOBS["rescore"], JOBS["dev-rest-gate"], JOBS["check-secrets"],
                JOBS["validate-gate"], JOBS["validate-judge-structured"], JOBS["validate-judge-freeform"], BASE["jobs"]["plan"]):
        for step in ledger_steps(job):
            assert step["with"]["github-token"] == "${{ github.token }}"
            assert ledger.artifact_role(sample_name(step["with"]["artifact-name"])) == "ledger"


def test_the_ledger_action_fetches_computes_checks_reports_uploads_then_refuses():
    names = [s["name"] for s in LEDGER_ACTION["runs"]["steps"]]
    assert names == [
        "Read the ledger snapshots kept on the results-log branch (spend beyond the artifact retention)",  # PR 17
        "Download the artifacts of every earlier hand-started run (GitHub REST API)",
        "Compute the actual spend so far",
        "Check the spend so far plus the cap against the hard limit",
        "Ledger to the job summary",
        "Upload the ledger result (ledger.json, ledger.md)",
        "Refuse when the ledger says so (exit 5 = refused by the budget ledger)",
    ]
    text = (ACTIONS / "ledger-check" / "action.yml").read_text(encoding="utf-8")
    assert "binding_gate.ledger fetch" in text and "binding_gate.ledger compute" in text and "binding_gate.ledger check" in text
    assert "binding_gate.results_log export-ledgers --ref refs/remotes/origin/results-log --out ledger-results-log" in text
    assert "--extra-ledgers ledger-results-log" in text and "compute ledger-results-log --inputs ledger-inputs" in text
    assert "GITHUB_TOKEN: ${{ inputs.github-token }}" in text
    last = LEDGER_ACTION["runs"]["steps"][-1]
    assert last["if"] == "${{ inputs.mode == 'check' && steps.check.outputs.code != '0' }}" and "exit 1" in last["run"]


# --------------------------------------------------------------------------- #
# Artifacts: names, retention, and what later jobs download
# --------------------------------------------------------------------------- #

SAMPLES = {
    "${{ inputs.artifact_prefix }}": "dev-rest--dev_gated--",
    "${{ env.ATTEMPT_SUFFIX }}": "",
    "${{ matrix.model == 'all' && 'all-models' || matrix.model }}": "all-models",
    "${{ matrix.model }}": "openai-low",
    "${{ inputs.stage }}": "all-dev",
    "${{ inputs.artifact-name }}": "x--ledger",
    "${{ inputs.layer }}": "layer2_sec",  # PR 17: frozen_test.yml
}


def sample_name(template: str) -> str:
    for expression, value in SAMPLES.items():
        template = template.replace(expression, value)
    assert "${{" not in template, template
    return template


def uploads() -> list[tuple[str, str, dict]]:
    return [(w, j, s) for w, j, s in all_steps() if str(s.get("uses", "")).startswith("actions/upload-artifact")]


def test_every_upload_keeps_its_artifact_90_days_and_spend_artifacts_are_classified():
    assert ledger.RETENTION_DAYS == 90
    expected = {"runs/": "runs", "private/runs.tar.gz.gpg": "runs-encrypted", "layer3-summary/": "layer3-summary",
                "rescore/": "rescore", "ledger/": "ledger"}
    names = []
    for where, job, step in uploads():
        assert step["with"]["retention-days"] == 90, (where, job, step.get("name"))
        name = sample_name(step["with"]["name"])
        names.append(name)
        role = ledger.artifact_role(name)
        if job == "decomposer-eval":
            assert role == "decomposer-eval"
        elif step["with"]["path"] in expected:
            assert role == expected[step["with"]["path"]], name
        else:
            assert role is None, name  # reports, decisions, analyses, manifests: no spend
    assert len(names) >= 12


def test_attempt_suffix_and_the_names_of_a_rerun():
    for doc in (PIPE, BASE):
        assert doc["env"]["ATTEMPT_SUFFIX"] == "${{ github.run_attempt != '1' && format('-attempt{0}', github.run_attempt) || '' }}"
    assert ledger.artifact_role("dev-rest--dev_gated--runs-openai-low-attempt2") == "runs"


def produced(prefix: str) -> list[str]:
    """Names baseline.yml uploads when called with ``prefix`` for a config that is not live-web (dev-baseline, dev-rest)."""
    out = []
    for _w, job, step in uploads():
        if _w != "baseline.yml" or "private_outputs == 'true'" in str(step.get("if")):
            continue
        template = step["with"]["name"].replace("${{ inputs.artifact_prefix }}", prefix)
        out.append(sample_name(template))
    return out


def test_every_download_of_a_later_job_matches_what_an_earlier_job_uploads():
    smoke = produced(JOBS["smoke"]["with"]["artifact_prefix"])
    baseline = produced(JOBS["dev-baseline"]["with"]["artifact_prefix"])
    rest = [n for name, job in calls().items() if job["with"]["artifact_prefix"].startswith("dev-rest--")
            for n in produced(job["with"]["artifact_prefix"])]
    decision = [sample_name(s["with"]["name"]) for s in steps(JOBS["dev-baseline-decision"]) if "upload-artifact" in str(s.get("uses"))]

    def downloads(job: str) -> list[str]:
        return [s["with"]["pattern"] for s in steps(JOBS[job]) if str(s.get("uses", "")).startswith("actions/download-artifact")]

    (pattern,) = downloads("smoke-check")
    assert any(fnmatch.fnmatch(n, pattern) for n in smoke) and not any(fnmatch.fnmatch(n, pattern) for n in baseline)
    (pattern,) = downloads("dev-baseline-decision")
    assert [n for n in baseline if fnmatch.fnmatch(n, pattern)] == ["dev-baseline--dev_baseline--baseline-report"]
    (pattern,) = downloads("dev-rest-gate")
    assert all(fnmatch.fnmatch(n, pattern) for n in decision)
    seed = "dev-baseline--dev_baseline--runs-*"
    assert [n for n in baseline if fnmatch.fnmatch(n, seed)] == ["dev-baseline--dev_baseline--runs-openai-low"]
    (rescore,) = downloads("rescore")
    assert rescore == seed
    rest_pattern, base_pattern = downloads("dev-analysis")
    assert rest_pattern == "dev-rest--*--runs-*" and base_pattern == seed
    assert {n for n in rest if fnmatch.fnmatch(n, rest_pattern)} == {n for n in rest if ledger.artifact_role(n) == "runs"}


# --------------------------------------------------------------------------- #
# Layer-3 summary in baseline.yml; secrets; job limits
# --------------------------------------------------------------------------- #


def test_the_layer3_summary_is_written_before_encryption_and_uploaded_unencrypted():
    run = {s.get("name"): s for s in steps(BASE["jobs"]["run"])}
    names = list(run)
    summary = "Layer-3 summary inside the job (costs, metrics, outcomes; no page text, no quotes)"
    upload = "Upload the layer-3 summary (the budget ledger reads its costs)"
    encrypt = "Encrypt runs/ of a live-web run (third-party page text is never uploaded in clear)"
    assert names.index("Paid run") < names.index(summary) < names.index(upload) < names.index(encrypt)
    assert "needs.plan.outputs.private_outputs == 'true'" in run[summary]["if"] and "always()" in run[summary]["if"]
    assert "binding_gate.layer3_summary" in run[summary]["run"]
    assert run[summary]["env"]["RUNS_ENCRYPTION_PASSPHRASE"] == "${{ secrets.RUNS_ENCRYPTION_PASSPHRASE }}"
    assert run[upload]["with"]["path"] == "layer3-summary/"
    assert ledger.summary_name_for(sample_name(run["Upload the encrypted runs/ of a live-web run"]["with"]["name"])) == \
        sample_name(run[upload]["with"]["name"])


def test_only_documented_secrets_are_used_and_only_in_step_environments():
    for path in YAML_FILES:
        used = set(re.findall(r"secrets\.([A-Z_]+)", path.read_text(encoding="utf-8")))
        assert used <= set(KNOWN_SECRETS), path
    pipe_secrets = set(re.findall(r"secrets\.([A-Z_]+)", (WORKFLOWS / "dev_pipeline.yml").read_text(encoding="utf-8")))
    assert set(pipeline.STUDY_SECRETS) <= pipe_secrets
    for where, job, step in all_steps():
        if "with" in step:
            assert "secrets." not in str(step["with"]), (where, job)


def test_job_limits_stay_under_six_hours():
    for doc in (PIPE, BASE):
        for name, job in doc["jobs"].items():
            if "runs-on" in job:
                assert 0 < job["timeout-minutes"] <= 350, name


# --------------------------------------------------------------------------- #
# The owner page and its links
# --------------------------------------------------------------------------- #

OWNER = (REPO_ROOT / "docs" / "OWNER_SETUP.md").read_text(encoding="utf-8")


def test_owner_page_is_hebrew_first_and_lists_every_secret_with_the_menu_path():
    hebrew = OWNER.index("הגדרה חד-פעמית")
    english = OWNER.index("## English")
    assert hebrew < english
    for secret in pipeline.STUDY_SECRETS:
        assert OWNER.count(f"`{secret}`") >= 2, secret  # in both languages
    assert OWNER.count("Settings → Secrets and variables → Actions → New repository secret") >= 2
    assert "unverified" in OWNER.lower() and "לא אומת" in OWNER
    assert "serpapi.com" in OWNER.lower()


def test_owner_page_gives_the_run_dialog_of_every_stage_as_a_field_value_table():
    inputs = triggers(PIPE)["workflow_dispatch"]["inputs"]
    stage_label, confirm_label = inputs["stage"]["description"], inputs["confirm_spend"]["description"]
    for stage in pipeline.STAGES:
        assert f"| {stage_label} | `{stage}` |" in OWNER, stage
    assert OWNER.count("| Use workflow from (Branch) | `main` |") >= 2 * len(pipeline.STAGES)
    assert f"| {confirm_label} | unchecked |" in OWNER and f"| {confirm_label} | checked |" in OWNER
    assert f"| {confirm_label} | לא מסומן |" in OWNER and f"| {confirm_label} | מסומן |" in OWNER


def test_readme_and_running_guide_link_the_owner_page():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/OWNER_SETUP.md" in readme[: readme.index("## The failure studied")]
    running = (REPO_ROOT / "docs" / "RUNNING.md").read_text(encoding="utf-8")
    assert "OWNER_SETUP.md" in running and "dev_pipeline.yml" in running and "binding_gate.ledger" in running


# --------------------------------------------------------------------------- #
# PR 22: validate-layer1 (amendment 1) and the layer-2 stop rule
# --------------------------------------------------------------------------- #


def job_text(job: dict) -> str:
    """Every script, env and with-block of a job as one string (yaml.safe_dump would wrap long lines)."""
    parts = [str(job.get("if", "")), str(job.get("needs", ""))]
    for step in steps(job):
        parts += [step.get("run", ""), str(step.get("with", "")), str(step.get("env", ""))]
    return "\n".join(parts)


def test_validate_layer1_follows_a_stop_never_needs_continue_and_runs_v1_v2_then_the_report():
    gate = JOBS["validate-gate"]
    assert gate["needs"] == "plan" and "inputs.stage == 'validate-layer1'" in gate["if"]
    text = job_text(gate)
    assert "binding_gate.pipeline find-decision" in text and "binding_gate.pipeline validate-gate" in text
    assert "dev-rest-gate" not in text and "dev-rest-gate" not in str(JOBS["validate-judge-structured"]["needs"])
    (check,) = ledger_steps(gate)
    assert check["with"]["stage"] == "validate-layer1"
    assert gate["outputs"]["seed_run_id"] == "${{ steps.gate.outputs.seed_run_id }}"
    assert "validate-layer1" not in str(pipeline.STAGES["all-dev"]) and not set(pipeline.STAGES["validate-layer1"]) & set(
        pipeline.STAGES["all-dev"])
    # V1: the judge on the dev-baseline answers of the decision's workflow run, arm no_gate
    v1 = JOBS["validate-judge-structured"]
    (download,) = [s for s in steps(v1) if "download-artifact" in str(s.get("uses"))]
    assert download["with"]["pattern"] == pipeline.VALIDATION_SEED_PATTERN
    assert download["with"]["run-id"] == "${{ needs.validate-gate.outputs.seed_run_id }}"
    assert "--arm no_gate" in job_text(v1) and "judge_agreement.json" in job_text(v1)
    # V2: the free_form config through baseline.yml, then the judge on its runs of this workflow run
    v2 = JOBS["validate-freeform"]
    assert v2["with"]["config"] == "configs/dev_validation_freeform.toml" and "seed_run_id" not in v2["with"]
    j2 = JOBS["validate-judge-freeform"]
    assert "needs.validate-freeform.result == 'success'" in j2["if"] and "--arm free_form" in job_text(j2)
    (download,) = [s for s in steps(j2) if "download-artifact" in str(s.get("uses"))]
    assert download["with"]["pattern"] == "validate-layer1--dev_validation_freeform--runs-*" and "run-id" not in download["with"]
    produced_v2 = produced(v2["with"]["artifact_prefix"])
    assert any(fnmatch.fnmatch(n, download["with"]["pattern"]) for n in produced_v2)
    # the report reads both, and its upload is on the results-log allow-list
    report = JOBS["validate-report"]
    assert set(report["needs"]) == {"validate-gate", "validate-judge-structured", "validate-freeform", "validate-judge-freeform"}
    text = job_text(report)
    assert "python -m binding_gate.validation report" in text and "--sample-config configs/rescore/validation_judge_structured.toml" in text
    (upload,) = [s for s in steps(report) if "upload-artifact" in str(s.get("uses"))]
    assert ledger.artifact_role(sample_name(upload["with"]["name"])) is None
    from binding_gate import results_log

    assert results_log.name_refusal("validate-layer1--validation-report/validation_report.json") is None
    assert results_log.name_refusal("validate-layer1--validation-report/validation_report.md") is None
    for job in ("validate-judge-structured", "validate-judge-freeform"):
        (upload,) = [s for s in steps(JOBS[job]) if "upload-artifact" in str(s.get("uses"))]
        assert ledger.artifact_role(sample_name(upload["with"]["name"])) == "rescore"
        assert set(re.findall(r"secrets\.([A-Z_]+)", job_text(JOBS[job]))) == {"MOONSHOT_API_KEY"}


def test_the_layer2_decision_job_computes_the_rule_and_gates_the_rest_of_layer2():
    job = JOBS["layer2-decision"]
    assert job["needs"] == "layer2-baseline" and job["outputs"]["allowed"] == "${{ steps.gate.outputs.allowed }}"
    text = job_text(job)
    assert "binding_gate.pipeline layer2-decision" in text and "binding_gate.pipeline layer2-gate" in text
    (download,) = [s for s in steps(job) if "download-artifact" in str(s.get("uses"))]
    assert download["with"]["pattern"] == pipeline.LAYER2_SEED_PATTERN
    (upload,) = [s for s in steps(job) if "upload-artifact" in str(s.get("uses"))]
    assert sample_name(upload["with"]["name"]) == "layer2-dev--decision" and "secrets." not in text
