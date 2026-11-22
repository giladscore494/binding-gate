"""Synthetic gated runs for the audit, report and end-to-end tests (no network, no model)."""

from __future__ import annotations

import json
from pathlib import Path

import gate_fakes  # noqa: F401 - registers the fake decomposer
import run_helpers as h
from binding_gate import runner
from binding_gate.arms.gated import clear_decomposition_memo
from binding_gate.gates.binding import ALL_CHECKS

DECOMPOSER = {"name": "fake_decomposer", "options": {}}


def gated_arm(label: str = "gated", checks=ALL_CHECKS) -> dict:
    return {"name": "gated", "label": label, "options": {"checks": list(checks), "decomposer": DECOMPOSER}}


def ablation_arm(removed: str) -> dict:
    return gated_arm(f"gated_minus_{removed}", [c for c in ALL_CHECKS if c != removed])


def gated_run(tmp_path: Path, *, arms=None, repeats: int = 2, **overrides):
    """Run no_gate + gated (and optional extra arms) on the synthetic benchmark; return ``(repo, summary)``."""
    clear_decomposition_memo()
    arms = arms or [{"name": "no_gate"}, gated_arm()]
    repo, config, _raw = h.setup(tmp_path, arms=arms, repeats=repeats, **overrides)
    summary = runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None)
    clear_decomposition_memo()
    return repo, summary


def write_judge_scores(run_dir: Path, label_for) -> int:
    """Write a ``scores.llm_judge.jsonl`` beside the trace: ``label_for(record)`` gives the label (or None)."""
    from binding_gate.trace import read_traces

    lines = []
    for record in read_traces(run_dir):
        label = label_for(record)
        if label is None:
            continue
        lines.append(json.dumps({
            "item_id": record.item_id, "arm": record.arm, "model": record.model, "repeat": record.repeat,
            "judge_hash": "0" * 64, "prompt_fingerprint": "test", "scored_at": "2026-11-22T10:00:00+00:00",
            "score": {"scorer": "llm_judge", "label": label, "details": {}},
        }))
    (run_dir / "scores.llm_judge.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)
