"""The one-command frozen test run (PREREGISTRATION.md section 12; ARCHITECTURE.md section 6).

``python -m binding_gate.cli frozen-test-run --config configs/test_layer1.toml
--tag prereg-freeze-1 --purpose "..." --confirm-spend`` does, in this order:

1. **Config checks** (no data is touched, nothing is logged): the config's
   split is ``test``; it has the pre-registered number of repeats and the
   reference arm ``no_gate``; the config file lies inside the repository and is
   tracked by git (so the clean-tree condition covers it); the runs directory is
   outside the repository or git-ignored.
2. ``--confirm-spend`` must be given (a test run makes paid model calls).
3. **Lock pre-check.** The conditions of ``testlock.load_test_split`` are
   evaluated first. If any fails, ``load_test_split`` itself is called with no
   items, so that the refusal is written to ``audit/test_access_log.jsonl`` by
   the one official function and ``TestSplitLocked`` is raised. No preflight
   and no model call happens before the lock is known to open.
4. **The run**: ``runner.execute_run`` with the tag and purpose. Inside it the
   test items are obtained exclusively through ``testlock.load_test_split``
   (one logged access per invocation), and the tag is recorded in the run
   manifest.
5. **The analysis** (``binding_gate.stats``): ``analysis.json``, ``report.md``
   and figures under ``<runs_dir>/<run_id>/analysis/``, plus
   ``<runs_dir>/<run_id>/frozen_run.jsonl`` tying the run to the tag, the commit
   and the access-log entry.

If a model's budget cap is reached, the run is left unfinished (resumable with
``--resume``; that is a further logged access) and no analysis is written.

After the run, commit ``audit/test_access_log.jsonl`` (ARCHITECTURE.md
section 6). This module never edits the pre-registration or the log by hand.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from binding_gate import gitinfo, runner, testlock
from binding_gate.canonical import sha256_text, utc_now_iso
from binding_gate.config import RunConfig, load_config
from binding_gate.contracts import Split
from binding_gate.stats import prereg
from binding_gate.stats.analysis import analyse_run_dirs, write_outputs

FROZEN_RUN_LOG = "frozen_run.jsonl"
"""One JSON line per invocation (a resumed run adds a line; nothing is overwritten)."""
ANALYSIS_DIRNAME = "analysis"


class FrozenRunError(RuntimeError):
    """Raised when a frozen test run must not start."""


@dataclass
class FrozenRunResult:
    run_dir: Path
    summary: dict[str, Any]
    analysis_dir: Path | None = None
    written: dict[str, Any] = field(default_factory=dict)
    aborted: bool = False


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=False)


def _inside(path: Path, root: Path) -> Path | None:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return None


def config_refusals(config: RunConfig, config_path: Path, repo_root: Path, runs_dir: Path) -> list[str]:
    """Reasons the config cannot be used for a frozen test run (empty list: it can)."""
    reasons: list[str] = []
    if config.split is not Split.TEST:
        reasons.append(f"the config's split is {config.split.value!r}; a frozen test run needs split = \"test\"")
    if config.repeats != prereg.REPEATS:
        reasons.append(f"repeats = {config.repeats}; the pre-registration fixes {prereg.REPEATS}")
    if prereg.REFERENCE_ARM not in {arm.label for arm in config.arms}:
        reasons.append(f"the config has no arm labelled {prereg.REFERENCE_ARM!r} (the reference of every comparison)")
    relative = _inside(config_path, repo_root)
    if relative is None:
        reasons.append(f"the config {config_path} is outside the repository {repo_root}")
    elif _git(repo_root, "ls-files", "--error-unmatch", "--", relative.as_posix()).returncode != 0:
        reasons.append(f"the config {relative.as_posix()} is not tracked by git; commit it before the freeze")
    runs_relative = _inside(runs_dir, repo_root)
    if runs_relative is not None:
        probe = (runs_relative / "probe-run").as_posix()
        if _git(repo_root, "check-ignore", "-q", "--", probe).returncode != 0:
            reasons.append(
                f"the runs directory {runs_relative.as_posix()} is inside the repository but not git-ignored; "
                "traces written there would make the working tree dirty"
            )
    return reasons


def lock_precheck(repo_root: Path, tag: str, purpose: str) -> None:
    """Raise ``TestSplitLocked`` (after logging the refusal) unless the lock would open.

    Uses the lock's own condition check; a refusal is logged by calling
    ``testlock.load_test_split`` with no items, so the log has exactly one entry
    per refused attempt and the official function stays the only writer.
    """
    reasons, _head = testlock._unlock_refusals(repo_root, tag, purpose)  # read-only evaluation
    if reasons:
        testlock.load_test_split([], repo_root=repo_root, confirm_tag=tag, purpose=purpose)
        raise testlock.TestSplitLocked(reasons)  # pragma: no cover - load_test_split raises first


def _access_log_tail(repo_root: Path) -> dict[str, Any]:
    entries = testlock.read_access_log(repo_root / testlock.ACCESS_LOG_RELPATH)
    log_path = repo_root / testlock.ACCESS_LOG_RELPATH
    lines = [line for line in log_path.read_text(encoding="utf-8").split("\n") if line] if log_path.exists() else []
    return {
        "entries": len(entries),
        "last_event": entries[-1].get("event") if entries else None,
        "last_line_sha256": sha256_text(lines[-1]) if lines else None,
    }


def frozen_test_run(
    config_path: str | Path,
    *,
    repo_root: str | Path,
    tag: str,
    purpose: str,
    confirm_spend: bool,
    runs_dir: str | Path = "runs",
    selection: str | None = None,
    resume_dir: str | Path | None = None,
    figures: bool = True,
    log: Callable[[str], None] = print,
) -> FrozenRunResult:
    """Run the frozen test configuration and its analysis (see the module docstring)."""
    repo_root = Path(repo_root)
    config_path = Path(config_path)
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    runs_dir = Path(runs_dir)
    if not runs_dir.is_absolute():
        runs_dir = repo_root / runs_dir
    config = load_config(config_path)
    reasons = config_refusals(config, config_path, repo_root, runs_dir)
    if reasons:
        raise FrozenRunError("frozen test run refused: " + "; ".join(reasons))
    if not confirm_spend:
        raise FrozenRunError(
            "frozen test run refused: it makes paid model calls; pass --confirm-spend after checking the plan"
        )

    lock_precheck(repo_root, tag, purpose)
    started = utc_now_iso()
    relative_config = _inside(config_path, repo_root)
    summary = runner.execute_run(
        config,
        repo_root=repo_root,
        runs_dir=runs_dir,
        confirm_spend=True,
        config_path=relative_config.as_posix() if relative_config else config_path,
        selection=selection,
        resume_dir=resume_dir,
        test_unlock_tag=tag,
        purpose=purpose,
        log=log,
    )
    result = FrozenRunResult(run_dir=summary.run_dir, summary=summary.to_dict(), aborted=summary.budget_exceeded)
    record: dict[str, Any] = {
        "event": "frozen_test_run",
        "tag": tag,
        "purpose": purpose,
        "config_path": relative_config.as_posix() if relative_config else str(config_path),
        "config_hash": config.config_hash,
        "head_commit": gitinfo.head_commit(repo_root),
        "selection": selection or "all",
        "started_at": started,
        "run_summary": result.summary,
        "access_log_after_load": _access_log_tail(repo_root),
        "analysis_parameters": prereg.as_dict(),
    }
    if not summary.budget_exceeded:
        analysis = analyse_run_dirs([summary.run_dir])
        result.analysis_dir = summary.run_dir / ANALYSIS_DIRNAME
        result.written = write_outputs(analysis, result.analysis_dir, figures=figures)
        record["analysis"] = {
            "dir": ANALYSIS_DIRNAME,
            "figure_note": result.written.get("figure_note"),
            "summary": analysis["summary"],
        }
    else:
        record["analysis"] = None
    record["ended_at"] = utc_now_iso()
    with (summary.run_dir / FROZEN_RUN_LOG).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    return result
