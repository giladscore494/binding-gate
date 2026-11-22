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
    for config in ("configs/decomposer_eval_dev.toml", "configs/rescore/dev_llm_judge.toml"):
        if f"--config {config}" in text:
            return config
    return ""


def test_every_config_runs_through_baseline_with_a_predictable_prefix_and_the_user_confirmation():
    stage_of = {c: s for s in ("smoke", "dev-baseline", "dev-rest", "layer3-dev") for c in pipeline.STAGES[s]}
    seen = []
    for name, job in calls().items():
        w = job["with"]
        config = w["config"]
        seen.append(config)
        assert w["artifact_prefix"] == f"{stage_of[config]}--{Path(config).stem}--", name
        assert w["dry_run"] is False and w["confirm_spend"] == "${{ inputs.confirm_spend }}" and w["model"] == "all"
        assert job["secrets"] == "inherit"
        seeds = runner.run_requirements(load_config(REPO_ROOT / config))["seed_runs"]
        if seeds:
            assert w["seed_run_id"] == "${{ needs.dev-rest-gate.outputs.seed_run_id }}", name
            assert w["seed_pattern"] == "dev-baseline--dev_baseline--runs-*"
        else:
            assert "seed_run_id" not in w and "seed_pattern" not in w
    custom = [config_of(n, j) for n, j in JOBS.items() if j.get("uses") is None and config_of(n, j)]
    assert sorted(seen + custom) == sorted(pipeline.STAGES["all-dev"])


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


def test_paid_stages_need_confirmation_and_check_secrets_spends_nothing():
    plan = {s.get("name"): s for s in steps(JOBS["plan"])}
    refuse = plan["Refuse a paid stage that was not confirmed"]
    assert refuse["if"] == "${{ inputs.stage != 'check-secrets' && !inputs.confirm_spend }}" and "exit 1" in refuse["run"]
    text = yaml.safe_dump(JOBS["check-secrets"])
    assert "--confirm-spend" not in text and "binding_gate.pipeline check-secrets" in text
    assert "mode: report" in text.replace("'", "")
    assert "secrets." not in yaml.safe_dump(JOBS["plan"])


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
    assert plan["if"] == "${{ inputs.stage != 'check-secrets' }}"
    for name, paid in (("decomposer-eval", "Paid measurement"), ("rescore", "Judge dry run, paid re-scoring, agreement")):
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
    for job in (JOBS["plan"], JOBS["decomposer-eval"], JOBS["rescore"], JOBS["dev-rest-gate"], JOBS["check-secrets"], BASE["jobs"]["plan"]):
        for step in ledger_steps(job):
            assert step["with"]["github-token"] == "${{ github.token }}"
            assert ledger.artifact_role(sample_name(step["with"]["artifact-name"])) == "ledger"


def test_the_ledger_action_fetches_computes_checks_reports_uploads_then_refuses():
    names = [s["name"] for s in LEDGER_ACTION["runs"]["steps"]]
    assert names == [
        "Download the artifacts of every earlier hand-started run (GitHub REST API)",
        "Compute the actual spend so far",
        "Check the spend so far plus the cap against the hard limit",
        "Ledger to the job summary",
        "Upload the ledger result (ledger.json, ledger.md)",
        "Refuse when the ledger says so (exit 5 = refused by the budget ledger)",
    ]
    text = (ACTIONS / "ledger-check" / "action.yml").read_text(encoding="utf-8")
    assert "binding_gate.ledger fetch" in text and "binding_gate.ledger compute" in text and "binding_gate.ledger check" in text
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
