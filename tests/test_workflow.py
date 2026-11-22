"""Static checks of the manually triggered baseline workflow and its documentation."""

from __future__ import annotations

import re
from pathlib import Path

from binding_gate.secrets import KNOWN_SECRETS

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (REPO_ROOT / ".github" / "workflows" / "baseline.yml").read_text(encoding="utf-8")
DOCS = (REPO_ROOT / "docs" / "RUNNING_BASELINE.md").read_text(encoding="utf-8")


def test_workflow_is_started_only_by_hand():
    trigger_block = WORKFLOW.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert trigger_block.lstrip().startswith("workflow_dispatch:")
    for other in ("push:", "pull_request:", "schedule:", "workflow_run:"):
        assert other not in trigger_block


def test_inputs_and_safe_defaults():
    for name in ("config", "model", "dry_run", "confirm_spend"):
        assert re.search(rf"^      {name}:$", WORKFLOW, re.MULTILINE)
    assert 'default: "configs/dev_smoke.toml"' in WORKFLOW
    dry_run = WORKFLOW.split("      dry_run:", 1)[1].split("      confirm_spend:", 1)[0]
    confirm = WORKFLOW.split("      confirm_spend:", 1)[1].split("\npermissions:", 1)[0]
    assert "default: true" in dry_run and "default: false" in confirm


def test_paid_step_requires_confirmation_and_inputs_never_reach_a_shell_unquoted():
    paid = WORKFLOW.split("- name: Paid run", 1)[1].split("- name:", 1)[0]
    assert "!inputs.dry_run && inputs.confirm_spend" in paid and "--confirm-spend" in paid
    assert "Refuse a paid run that was not confirmed" in WORKFLOW
    # User-typed inputs are passed through environment variables, not pasted into scripts.
    for line in WORKFLOW.splitlines():
        if "${{ inputs.config }}" in line or "${{ inputs.model }}" in line:
            assert line.strip().startswith(("CONFIG:", "MODEL:", "group:"))


def test_only_documented_secrets_are_used_and_a_missing_one_skips_the_model():
    used = set(re.findall(r"secrets\.([A-Z_]+)", WORKFLOW))
    assert used == {
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "MOONSHOT_API_KEY",
        "SERPAPI_API_KEY", "RUNS_ENCRYPTION_PASSPHRASE",  # PR 8: web search key, artifact encryption
    }
    assert used <= set(KNOWN_SECRETS)
    assert '"$CODE" -eq 4' in WORKFLOW and "SKIPPED" in WORKFLOW
    assert "fail-fast: false" in WORKFLOW
    assert "contents: read" in WORKFLOW


def test_documentation_gives_the_run_dialog_values():
    for expected in ("configs/dev_smoke.toml", "configs/dev_baseline.toml", "Dry run", "I confirm that paid API calls may be made", "Run workflow"):
        assert expected in DOCS


# --------------------------------------------------------------------------- #
# PR 8: config-driven extras, dataset preparation, layer-3 runs
# --------------------------------------------------------------------------- #


def _job(name: str) -> str:
    """The text of one job (``plan``, ``run`` or ``report``)."""
    rest = WORKFLOW.split(f"\n  {name}:\n", 1)[1]
    return re.split(r"\n  [a-z]+:\n", rest, maxsplit=1)[0]


def _steps(job: str) -> dict[str, str]:
    """Step name -> the text of that step (only named steps)."""
    out = {}
    for block in re.split(r"\n      - ", _job(job))[1:]:
        if block.startswith("name: "):
            out[block.split("\n", 1)[0][len("name: "):]] = block
    return out


RUN_STEPS = _steps("run")


def test_plan_job_reads_the_requirements_of_the_config():
    plan = _job("plan")
    for output in ("models", "extras", "private_outputs", "single_job", "baseline_report"):
        assert f"      {output}: ${{{{ steps." in plan
    steps = _steps("plan")
    requirements = steps["Read what the config needs (extras, secrets, output privacy)"]
    assert 'binding_gate.cli requirements --config "$CONFIG"' in requirements and "GITHUB_OUTPUT" in requirements
    select = steps["Select models"]
    assert "SELECTED='[\"all\"]'" in select and '"$SINGLE_JOB" = "true"' in select


def test_optional_extras_are_installed_from_the_config_through_an_environment_variable():
    assert "      EXTRAS: ${{ needs.plan.outputs.extras }}" in _job("run")
    install = RUN_STEPS["Install package (with the optional extras the config needs)"]
    assert 'pip install -e ".[${EXTRAS}]"' in install and "pip install -e ." in install


def test_dataset_is_cloned_or_prepared_by_its_pin_method():
    step = RUN_STEPS["Get the dataset at its pinned version (into data/raw/, git-ignored)"]
    assert "method=" in step and '"$METHOD" = "prepare"' in step
    assert 'binding_gate.cli dataset-prepare --config "$CONFIG"' in step and 'git -C "$ROOT" fetch -q --depth 1' in step
    assert step.index("dataset-pin") < step.index("dataset-prepare")
    names = list(RUN_STEPS)
    assert names.index("Install package (with the optional extras the config needs)") < names.index(
        "Get the dataset at its pinned version (into data/raw/, git-ignored)"
    )  # the prepare step needs the extra installed first


def test_search_key_reaches_preflight_and_paid_run_and_a_missing_one_skips():
    for name in ("Preflight (free check that the model ids and the search account exist)", "Paid run"):
        assert "SERPAPI_API_KEY: ${{ secrets.SERPAPI_API_KEY }}" in RUN_STEPS[name]
        assert "${SELECTED_MODELS:-$MODEL}" in RUN_STEPS[name]
    assert "web search" in RUN_STEPS["Preflight (free check that the model ids and the search account exist)"]
    single = RUN_STEPS["Single live-web job - select every model whose API key is set"]
    assert "--available" in single and "SELECTED_MODELS=" in single and "single_job == 'true'" in single


def test_live_web_artifacts_are_uploaded_only_encrypted_and_the_paid_run_refuses_without_a_passphrase():
    refuse = RUN_STEPS["Refuse a live-web paid run whose artifacts could not be encrypted"]
    assert "private_outputs == 'true'" in refuse and "exit 1" in refuse
    names = list(RUN_STEPS)
    assert names.index("Refuse a live-web paid run whose artifacts could not be encrypted") < names.index("Paid run")
    assert "private_outputs != 'true'" in RUN_STEPS["Upload runs/ (traces, manifest, report)"]
    encrypt = RUN_STEPS["Encrypt runs/ of a live-web run (third-party page text is never uploaded in clear)"]
    assert "gpg --batch" in encrypt and "--symmetric --cipher-algo AES256" in encrypt
    assert "rm -f private/runs.tar.gz" in encrypt and "publish_manifest.json" in encrypt
    assert "path: private/runs.tar.gz.gpg" in RUN_STEPS["Upload the encrypted runs/ of a live-web run"]
    assert "path: public/" in RUN_STEPS["Upload the publish manifest of a live-web run (URLs, times, hashes; no page text)"]


def test_the_stop_rule_report_runs_only_where_it_applies():
    assert "baseline_report == 'true'" in RUN_STEPS["Baseline report for this model"]
    report = _job("report")
    assert "needs: [plan, run]" in report and "baseline_report == 'true'" in report
