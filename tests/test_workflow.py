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
    assert used == {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "MOONSHOT_API_KEY"}
    assert used <= set(KNOWN_SECRETS)
    assert '"$CODE" -eq 4' in WORKFLOW and "SKIPPED" in WORKFLOW
    assert "fail-fast: false" in WORKFLOW
    assert "contents: read" in WORKFLOW


def test_documentation_gives_the_run_dialog_values():
    for expected in ("configs/dev_smoke.toml", "configs/dev_baseline.toml", "Dry run", "I confirm that paid API calls may be made", "Run workflow"):
        assert expected in DOCS
