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
    # run-name (PR 17) is the run's title in the UI and the API, never a shell
    for line in WORKFLOW.splitlines():
        if "${{ inputs.config }}" in line or "${{ inputs.model }}" in line:
            assert line.strip().startswith(("CONFIG:", "MODEL:", "group:", "run-name:"))


def test_only_documented_secrets_are_used_and_a_missing_one_skips_the_model():
    used = set(re.findall(r"secrets\.([A-Z_]+)", WORKFLOW))
    assert used == {
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "MOONSHOT_API_KEY",
        "SERPAPI_API_KEY", "RUNS_ENCRYPTION_PASSPHRASE",  # PR 8: web search key, artifact encryption
        "DEEPSEEK_API_KEY",  # PR 13: slot G (optional since PR 14; unused by the USD 50 study)
        "ZAI_API_KEY",  # PR 14: slots H and I (Z.ai GLM)
        "SEC_CONTACT_EMAIL",  # PR 17: EDGAR User-Agent contact, only for generating the SEC dataset
    }
    assert used <= set(KNOWN_SECRETS)
    # PR 14: the Z.ai key reaches every step that a model key reaches
    for step in ("Single live-web job - select every model whose API key is set",
                 "Preflight (free check that the model ids and the search account exist)", "Paid run"):
        block = WORKFLOW.split(f"- name: {step}", 1)[1].split("- name:", 1)[0]
        assert "ZAI_API_KEY: ${{ secrets.ZAI_API_KEY }}" in block and "OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}" in block
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
    for output in ("models", "extras", "private_outputs", "single_job", "baseline_report",
                   "faithfulness_checker", "faithfulness_weights", "seed_runs"):  # last three: PR 12
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
    # PR 15: the step is the composite action shared with dev_pipeline.yml; the config reaches it as a variable
    uses = RUN_STEPS["Get the dataset at its pinned version (into data/raw/, git-ignored)"]
    assert "uses: ./.github/actions/get-dataset" in uses and "config: ${{ env.CONFIG }}" in uses
    step = (REPO_ROOT / ".github" / "actions" / "get-dataset" / "action.yml").read_text(encoding="utf-8")
    assert "CONFIG: ${{ inputs.config }}" in step
    assert "method=" in step and '"$METHOD" = "prepare"' in step
    assert 'binding_gate.cli dataset-prepare --config "$CONFIG"' in step and 'git -C "$ROOT" fetch -q --depth 1' in step
    assert step.index("dataset-pin") < step.index("dataset-prepare")
    # PR 17: the SEC contact e-mail reaches the dataset step (and only through its env), with the EDGAR cache
    assert "SEC_CONTACT_EMAIL: ${{ secrets.SEC_CONTACT_EMAIL }}" in uses
    for name, block in RUN_STEPS.items():
        if "SEC_CONTACT_EMAIL" in block:
            assert name == "Get the dataset at its pinned version (into data/raw/, git-ignored)", name
    assert "actions/cache@v4" in step and "path: data/raw/sec-financials/cache" in step
    assert ("key: sec-edgar-v1-${{ hashFiles('configs/datasets/sec_financials_v1.toml',"
            "'configs/datasets/sec_financials_selection_v1.json') }}") in step
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


# --------------------------------------------------------------------------- #
# PR 11: the full operating guide docs/RUNNING.md
# --------------------------------------------------------------------------- #

RUNNING = (REPO_ROOT / "docs" / "RUNNING.md").read_text(encoding="utf-8")


def test_running_guide_uses_the_exact_dialog_labels_of_every_workflow_input():
    inputs = WORKFLOW.split("    inputs:\n", 1)[1].split("\npermissions:", 1)[0]
    descriptions = re.findall(r"^        description: (?:\"(.*)\"|'(.*)')$", inputs, re.MULTILINE)
    labels = [double or single for double, single in descriptions]
    assert len(labels) == 5  # PR 12 added seed_run_id
    for label in labels:
        assert f"| {label} |" in RUNNING, label
    assert "| Use workflow from (Branch) | `main` |" in RUNNING


def test_running_guide_names_only_existing_configs_and_modules_and_every_secret():
    import importlib.util

    configs = set(re.findall(r"configs/[\w/]+\.toml", RUNNING))
    assert configs and all((REPO_ROOT / path).is_file() for path in configs), sorted(
        p for p in configs if not (REPO_ROOT / p).is_file()
    )
    modules = set(re.findall(r"python -m (binding_gate[\w.]*)", RUNNING))
    assert modules and all(importlib.util.find_spec(m) is not None for m in modules), sorted(modules)
    for secret in set(re.findall(r"secrets\.([A-Z_]+)", WORKFLOW)):
        assert f"`{secret}`" in RUNNING


# --------------------------------------------------------------------------- #
# PR 12: MiniCheck preparation (section 17 item 40) and seed runs (item 32)
# --------------------------------------------------------------------------- #


def test_faithfulness_checker_is_prepared_and_verified_before_any_paid_call():
    names = list(RUN_STEPS)
    cache = "Cache the MiniCheck weights and NLTK data (faithfulness checker)"
    prepare = "Prepare and verify the faithfulness checker (pinned MiniCheck weights; no model API)"
    for name in (cache, prepare):
        assert "needs.plan.outputs.faithfulness_checker == 'true'" in RUN_STEPS[name]
    assert "python -m binding_gate.arms.faithfulness_checker prepare" in RUN_STEPS[prepare]
    assert "python -m binding_gate.arms.faithfulness_checker verify" in RUN_STEPS[prepare]
    assert "actions/cache@v4" in RUN_STEPS[cache] and "~/.cache/huggingface/hub" in RUN_STEPS[cache]
    assert "key: minicheck-${{ needs.plan.outputs.faithfulness_weights }}" in RUN_STEPS[cache]
    install = names.index("Install package (with the optional extras the config needs)")
    preflight = names.index("Preflight (free check that the model ids and the search account exist)")
    assert install < names.index(cache) < names.index(prepare) < preflight < names.index("Paid run")
    assert "secrets." not in RUN_STEPS[prepare]  # no key reaches the preparation step


def test_requirements_drive_the_faithfulness_step_for_the_baselines_config_only():
    from binding_gate import runner
    from binding_gate.arms.faithfulness_checker import MINICHECK_CHECKPOINT, MINICHECK_WEIGHTS_REVISION
    from binding_gate.config import load_config

    baselines = runner.run_requirements(load_config(REPO_ROOT / "configs" / "dev_baselines.toml"))
    assert baselines["faithfulness_checker"] is True and "faithfulness" in baselines["extras"]
    assert baselines["faithfulness_weights"] == f"{MINICHECK_CHECKPOINT}@{MINICHECK_WEIGHTS_REVISION}"
    assert "," not in baselines["faithfulness_weights"]  # GitHub cache keys must not contain commas
    gated = runner.run_requirements(load_config(REPO_ROOT / "configs" / "dev_gated.toml"))
    assert gated["faithfulness_checker"] is False and gated["faithfulness_weights"] == ""


def test_seed_runs_are_checked_downloaded_outside_runs_and_verified():
    assert "      seed_run_id:" in WORKFLOW and "actions: read" in WORKFLOW
    assert "  SEED_RUN_ID: ${{ inputs.seed_run_id }}" in WORKFLOW  # reaches shells only as a variable
    check = _steps("plan")["Check the seed runs of the config against the seed_run_id input"]
    assert '[ -n "$SEED_RUNS" ] && [ -z "$SEED_RUN_ID" ]' in check and '[ -z "$SEED_RUNS" ] && [ -n "$SEED_RUN_ID" ]' in check
    assert "*[!0-9]*" in check and "seeds/*/*" in check and "exit 1" in check
    download = RUN_STEPS["Download the seed runs named in the config (runs-* artifacts of the workflow run seed_run_id)"]
    assert "actions/download-artifact@v4" in download and "path: seeds" in download
    assert "run-id: ${{ inputs.seed_run_id }}" in download and "github-token: ${{ github.token }}" in download
    assert "needs.plan.outputs.seed_runs != ''" in download
    verify = RUN_STEPS["Check that every seed run was downloaded"]
    assert '"$SEED/manifest.json"' in verify and "exit 1" in verify
    names = list(RUN_STEPS)
    assert names.index("Check that every seed run was downloaded") < names.index("Dry run (no API call, no key needed)")
    # seeds/ is never part of what is uploaded or reported as the new run
    assert "path: runs/" in RUN_STEPS["Upload runs/ (traces, manifest, report)"]
    assert "seeds" not in RUN_STEPS["Baseline report for this model"]
