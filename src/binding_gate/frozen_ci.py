"""Helpers of the frozen test-run workflow ``.github/workflows/frozen_test.yml`` (PR 17).

The workflow runs ``python -m binding_gate.cli frozen-test-run`` for ONE layer at
exactly a freeze tag, in GitHub Actions, so that no step of the study needs a
local command line. It never weakens the test lock (``binding_gate.testlock``):
the lock's own conditions decide, and every access is logged by the lock itself.
This module only does what the workflow needs around the run:

``validate --layer L --tag T --confirm true|false``
    Refuse (exit 5) unless the layer is one of :data:`LAYER_CONFIGS`, the tag
    matches :data:`TAG_PATTERN` (``prereg-freeze-*``, safe characters only) and
    the spend was confirmed. Nothing is logged: like the CLI refusing a missing
    ``--confirm-spend``, this happens before the lock is reached. Prints
    ``config=``, ``per_model_jobs=``.
``pending-audit [--main-ref R]``
    Refuse (exit 5) while an ``audit-log/*`` branch holds access-log entries that
    ``main`` does not have yet (the pull request of an earlier frozen run is not
    merged): the hash chain allows only one line of entries.
``sync-access-log --base-out FILE [--main-ref R]``
    In the checkout of the tag: verify that the tag's committed access log is a
    prefix of ``main``'s (entries are only ever appended), then put ``main``'s log
    in the working tree (the lock ignores changes to that one file), so the new
    entries chain onto ``main``'s last entry; ``FILE`` keeps that base for the
    pull request.
``clean-check``
    Fail if anything but the access log is modified or untracked: the workflow's
    own hygiene check before the run (read-only; not an access).
``stage-results --runs-dir D --layer L --out DIR``
    The non-sensitive results of the run for the ``results-log`` branch:
    ``binding_gate.report.publish`` (allow-list; layer-1 traces included because
    their documents are Apache-2.0, compressed when large; layers 2 and 3: never)
    into ``DIR/<run id>/`` with its figures, plus ``DIR/frozen_summary.json``.
    For an unfinished run (budget cap or search quota) only the manifest, the
    invocations and the frozen-run record.
``commit-results DIR --layer L --run-id N``
    Commit DIR to ``results-log`` as ``frozen/<layer>/<run id>/``.
``audit-branch --new FILE --base FILE --tag T --run-id N --layer L``
    Check that the new access log is the base log plus appended entries of this
    tag (hash chain intact; the base is still ``main``'s log), then commit it on
    a new branch ``audit-log/frozen-<run id>`` from ``main`` and push it. The
    workflow then opens the pull request into ``main`` (``gh pr create``).
    Prints ``branch=`` and ``entries_added=`` (0: nothing to commit).

Why a pull request and not a direct push to ``main``: ``main`` stays reviewable
and may be protected; the change is checked here to be append-only (tested), but
merging stays a visible, separate act. One frozen run at a time
(``concurrency: frozen-test-run``) and :func:`pending_audit_branches` keep the
chain linear.

Per-model jobs: PREREGISTRATION.md section 17 item 18 (one access for all models,
or one per model) is still open, so :data:`PER_MODEL_JOBS` is false and the
workflow runs every model of the layer in ONE job and one access. A GitHub-hosted
job ends after 6 hours: the paid step stops after 330 minutes (job limit 350), the
run is then left unfinished and its records are still uploaded; finishing it is a
further logged access (``--resume``), which needs an amendment (section 18).
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from binding_gate import results_log, testlock
from binding_gate.canonical import sha256_text, utc_now_iso

LAYER_CONFIGS: dict[str, str] = {
    "layer1": "configs/test_layer1.toml",
    "layer1_midtier": "configs/test_layer1_midtier.toml",
    "layer2_sec": "configs/test_sec.toml",
    "layer3_live": "configs/test_live.toml",
}
"""Input ``layer`` of the workflow -> the frozen test config of that layer (PREREGISTRATION.md section 12)."""
LAYER1_INPUTS = ("layer1", "layer1_midtier")
TAG_PATTERN = re.compile(re.escape(testlock.FREEZE_TAG_PREFIX) + r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
PER_MODEL_JOBS = False
"""Section 17 item 18 is open: one job and one logged access for all models of a layer."""
AUDIT_BRANCH_PREFIX = "audit-log/"
MAIN_REF = "refs/remotes/origin/main"
TRACES_GZIP_ABOVE_BYTES = 40_000_000
TRACES_MAX_GZIP_BYTES = 95_000_000
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REFUSED = 5


class FrozenCIError(RuntimeError):
    """Raised when a step of the frozen workflow must not go on."""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise FrozenCIError(f"git {' '.join(args)} failed: {result.stderr.strip()[:400]}")
    return result


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


def validate(layer: str, tag: str, confirm: str | bool) -> list[str]:
    """Reasons the workflow refuses its inputs (empty: they are acceptable)."""
    reasons = []
    if layer not in LAYER_CONFIGS:
        reasons.append(f"layer must be one of {sorted(LAYER_CONFIGS)}, not {layer!r}")
    if not isinstance(tag, str) or not TAG_PATTERN.fullmatch(tag):
        reasons.append(f"tag must be a freeze tag {testlock.FREEZE_TAG_PREFIX}<name> (letters, digits, . _ -), not {tag!r}")
    if str(confirm).strip().lower() != "true":
        reasons.append("confirm_spend is not checked: a frozen test run makes paid model calls (and, when the lock "
                       "opens, a logged test-split access)")
    return reasons


# --------------------------------------------------------------------------- #
# The access log
# --------------------------------------------------------------------------- #


def _lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line]


def _chain_ok(text: str, where: str) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "log.jsonl"
        path.write_text(text, encoding="utf-8")
        try:
            return testlock.read_access_log(path)
        except testlock.AccessLogError as exc:
            raise FrozenCIError(f"{where}: {exc}") from exc


def _show(repo: Path, ref: str, path: str) -> str:
    result = _git(repo, "show", f"{ref}:{path}", check=False)
    if result.returncode != 0:
        if "does not exist" in result.stderr or "exists on disk, but not in" in result.stderr:
            return ""
        raise FrozenCIError(f"cannot read {path} at {ref}: {result.stderr.strip()[:300]}")
    return result.stdout


def is_prefix(base: str, new: str) -> bool:
    """True if the lines of ``base`` are exactly the first lines of ``new``."""
    a, b = _lines(base), _lines(new)
    return b[: len(a)] == a


def sync_access_log(repo: str | Path, base_out: str | Path, *, main_ref: str = MAIN_REF) -> dict[str, Any]:
    """Put ``main``'s access log in the working tree of the tag checkout (see the module docstring)."""
    repo = Path(repo)
    relpath = testlock.ACCESS_LOG_RELPATH
    at_tag = _show(repo, "HEAD", relpath)
    at_main = _show(repo, main_ref, relpath)
    _chain_ok(at_tag, f"{relpath} at the tag")
    entries = _chain_ok(at_main, f"{relpath} on main")
    if not is_prefix(at_tag, at_main):
        raise FrozenCIError(f"{relpath} on main does not continue the log of the tag (an entry was changed or removed)")
    (repo / relpath).parent.mkdir(parents=True, exist_ok=True)
    (repo / relpath).write_text(at_main, encoding="utf-8", newline="\n")
    Path(base_out).write_text(at_main, encoding="utf-8", newline="\n")
    return {"base_entries": len(entries), "tag_entries": len(_lines(at_tag)),
            "base_last_line_sha256": sha256_text(_lines(at_main)[-1]) if at_main.strip() else None}


def pending_audit_branches(repo: str | Path, *, main_ref: str = MAIN_REF, remote: str = "origin") -> list[str]:
    """``audit-log/*`` branches whose access log has entries ``main``'s log does not have."""
    repo = Path(repo)
    at_main = _show(repo, main_ref, testlock.ACCESS_LOG_RELPATH)
    refs = _git(repo, "for-each-ref", "--format=%(refname)", f"refs/remotes/{remote}/{AUDIT_BRANCH_PREFIX}").stdout.split()
    pending = []
    for ref in refs:
        log = _show(repo, ref, testlock.ACCESS_LOG_RELPATH)
        if not is_prefix(log, at_main):
            pending.append(ref.removeprefix(f"refs/remotes/{remote}/"))
    return pending


def appended_entries(base: str, new: str, *, tag: str, tag_commit: str) -> list[dict[str, Any]]:
    """The entries ``new`` appends to ``base``; refuses anything but an append of this tag's entries."""
    if not is_prefix(base, new):
        raise FrozenCIError("the new access log does not start with the base log: an entry was changed or removed")
    entries = _chain_ok(new, "the new access log")
    added = entries[len(_lines(base)):]
    for number, entry in enumerate(added, start=len(_lines(base)) + 1):
        if entry.get("confirm_tag") != tag:
            raise FrozenCIError(f"access-log entry {number} names tag {entry.get('confirm_tag')!r}, not {tag!r}")
        if entry.get("head_commit") not in (tag_commit, None):
            raise FrozenCIError(f"access-log entry {number} was made at {entry.get('head_commit')!r}, not the tag {tag_commit}")
        if entry.get("event") not in (testlock.EVENT_LOADED, testlock.EVENT_REFUSED):
            raise FrozenCIError(f"access-log entry {number} has an unknown event {entry.get('event')!r}")
    return added


def audit_branch(
    repo: str | Path,
    new_log: str | Path,
    base_log: str | Path,
    *,
    tag: str,
    run_id: int | str,
    layer: str,
    main_ref: str = MAIN_REF,
    remote: str = "origin",
) -> dict[str, Any]:
    """Commit the appended access-log entries on ``audit-log/frozen-<run id>`` from ``main`` and push it."""
    repo = Path(repo)
    if not str(run_id).isdigit():
        raise FrozenCIError("the workflow run id must be a number")
    base, new = Path(base_log).read_text(encoding="utf-8"), Path(new_log).read_text(encoding="utf-8")
    current = _show(repo, main_ref, testlock.ACCESS_LOG_RELPATH)
    if _lines(current) != _lines(base):
        raise FrozenCIError("main's access log changed while the frozen run ran; its entries cannot be chained "
                            "automatically - commit the new log by hand (docs/RUNNING.md, step 10)")
    tag_commit = _git(repo, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}").stdout.strip()
    added = appended_entries(base, new, tag=tag, tag_commit=tag_commit)
    branch = f"{AUDIT_BRANCH_PREFIX}frozen-{run_id}"
    if not added:
        return {"branch": branch, "entries_added": 0, "pushed": False}
    if _git(repo, "ls-remote", "--exit-code", "--heads", remote, branch, check=False).returncode == 0:
        # a re-run attempt of this job: the branch is already there; accept it only with the same log
        _git(repo, "fetch", "-q", "--no-tags", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
        if _lines(_show(repo, f"refs/remotes/{remote}/{branch}", testlock.ACCESS_LOG_RELPATH)) != _lines(new):
            raise FrozenCIError(f"branch {branch} already exists with a different access log")
        return {"branch": branch, "entries_added": len(added), "pushed": False, "already_pushed": True}
    tmp = Path(tempfile.mkdtemp(prefix="audit-log-"))
    wt = tmp / "worktree"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt), main_ref)
    try:
        _git(wt, "checkout", "-q", "-B", branch)
        target = wt / testlock.ACCESS_LOG_RELPATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new if new.endswith("\n") else new + "\n", encoding="utf-8", newline="\n")
        _git(wt, "add", "--", testlock.ACCESS_LOG_RELPATH)
        changed = _git(wt, "diff", "--cached", "--name-only").stdout.split()
        if changed != [testlock.ACCESS_LOG_RELPATH]:
            raise FrozenCIError(f"the audit commit would change {changed}, not only the access log")
        events = ", ".join(sorted({e["event"] for e in added}))
        message = (f"audit: access log of the frozen test run ({layer}, tag {tag}, workflow run {run_id})\n\n"
                   f"{len(added)} entr{'y' if len(added) == 1 else 'ies'} appended by binding_gate.testlock ({events}); "
                   "checked append-only by binding_gate.frozen_ci.")
        env_identity = ["-c", f"user.name={results_log.BOT_NAME}", "-c", f"user.email={results_log.BOT_EMAIL}",
                        "-c", "commit.gpgsign=false"]
        _git(wt, *env_identity, "commit", "-q", "-m", message)
        _git(wt, "push", "-q", remote, f"HEAD:refs/heads/{branch}")
        commit = _git(wt, "rev-parse", "HEAD").stdout.strip()
    finally:
        _git(repo, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(tmp, ignore_errors=True)
    return {"branch": branch, "entries_added": len(added), "pushed": True, "commit": commit,
            "events": [e["event"] for e in added]}


def clean_check(repo: str | Path) -> list[str]:
    from binding_gate import gitinfo

    return gitinfo.dirty_paths(Path(repo), ignore=[testlock.ACCESS_LOG_RELPATH])


# --------------------------------------------------------------------------- #
# Results for the results-log branch
# --------------------------------------------------------------------------- #


def _run_dirs(runs_dir: Path) -> list[Path]:
    return sorted(m.parent for m in runs_dir.glob("*/manifest.json") if (m.parent / "traces.jsonl").is_file())


def stage_results(runs_dir: str | Path, layer: str, out: str | Path) -> dict[str, Any]:
    """Stage the non-sensitive results of the frozen run(s) under ``runs_dir`` (see the module docstring)."""
    from binding_gate.report.publish import PublishError, publish_run

    if layer not in LAYER_CONFIGS:
        raise FrozenCIError(f"unknown layer {layer!r}")
    runs_dir, out = Path(runs_dir), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    include_traces = layer in LAYER1_INPUTS
    summary: dict[str, Any] = {"frozen_ci_version": 1, "staged_at": utc_now_iso(), "layer": layer,
                               "config": LAYER_CONFIGS[layer], "runs": []}
    for run_dir in _run_dirs(runs_dir) if runs_dir.is_dir() else []:
        row: dict[str, Any] = {"run_dir": run_dir.name}
        with tempfile.TemporaryDirectory() as tmp:
            try:
                published = publish_run(run_dir, results_dir=tmp, include_traces=include_traces)
            except PublishError as exc:
                row["published"] = False
                row["publish_refused"] = str(exc)[:1000]
                target = out / run_dir.name
                for name in ("manifest.json", "invocations.jsonl", "frozen_run.jsonl"):
                    path = run_dir / name
                    if path.is_file() and results_log.refusal_reason(name, path.read_bytes()) is None:
                        target.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, target / name)
            else:
                row["published"] = True
                target = out / published.target.name
                shutil.copytree(published.target, target)
                traces = target / "traces.jsonl"
                if traces.is_file() and traces.stat().st_size > TRACES_GZIP_ABOVE_BYTES:
                    packed = gzip.compress(traces.read_bytes(), mtime=0)
                    traces.unlink()
                    if len(packed) <= TRACES_MAX_GZIP_BYTES:
                        (target / "traces.jsonl.gz").write_bytes(packed)
                        row["traces"] = "traces.jsonl.gz (gzip of the published traces.jsonl)"
                    else:
                        row["traces"] = "left out: larger than the 100 MB file limit even compressed (in the artifact)"
        summary["runs"].append(row)
    (out / "frozen_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def publish_exemptions(staged: str | Path, layer: str) -> list[str]:
    """Staged files allowed by ``binding_gate.report.publish`` but not by the results-log name list.

    The figures ``<run id>/analysis/figures/*.png`` (drawn from the analysis only) and, for layer 1 only,
    ``<run id>/traces.jsonl[.gz]`` (Apache-2.0 documents).
    """
    staged = Path(staged)
    out = []
    for path in sorted(p for p in staged.rglob("*") if p.is_file()):
        parts = path.relative_to(staged).parts
        if len(parts) == 4 and parts[1:3] == ("analysis", "figures") and path.suffix == ".png":
            out.append("/".join(parts))
        elif layer in LAYER1_INPUTS and len(parts) == 2 and parts[1] in ("traces.jsonl", "traces.jsonl.gz"):
            out.append("/".join(parts))
    return out


def commit_results(repo: str | Path, staged: str | Path, *, layer: str, run_id: int | str) -> dict[str, Any]:
    if layer not in LAYER_CONFIGS or not str(run_id).isdigit():
        raise FrozenCIError("commit-results needs a known layer and a numeric workflow run id")
    folder = f"{results_log.FROZEN_DIR}/{layer}/{run_id}"
    return results_log.commit_to_branch(repo, staged, folder, message=f"results-log: {folder} (frozen test run)",
                                        exempt=publish_exemptions(staged, layer))


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m binding_gate.frozen_ci", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("validate")
    sub.add_argument("--layer", required=True)
    sub.add_argument("--tag", required=True)
    sub.add_argument("--confirm", required=True)
    sub = commands.add_parser("pending-audit")
    sub.add_argument("--repo", default=".")
    sub.add_argument("--main-ref", default=MAIN_REF)
    sub = commands.add_parser("sync-access-log")
    sub.add_argument("--repo", default=".")
    sub.add_argument("--main-ref", default=MAIN_REF)
    sub.add_argument("--base-out", required=True)
    sub = commands.add_parser("clean-check")
    sub.add_argument("--repo", default=".")
    sub = commands.add_parser("stage-results")
    sub.add_argument("--runs-dir", required=True)
    sub.add_argument("--layer", required=True)
    sub.add_argument("--out", required=True)
    sub = commands.add_parser("commit-results")
    sub.add_argument("staged")
    sub.add_argument("--layer", required=True)
    sub.add_argument("--run-id", required=True)
    sub.add_argument("--repo", default=".")
    sub = commands.add_parser("audit-branch")
    sub.add_argument("--new", required=True)
    sub.add_argument("--base", required=True)
    sub.add_argument("--tag", required=True)
    sub.add_argument("--run-id", required=True)
    sub.add_argument("--layer", required=True)
    sub.add_argument("--repo", default=".")
    sub.add_argument("--main-ref", default=MAIN_REF)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            reasons = validate(args.layer, args.tag, args.confirm)
            for reason in reasons:
                print(f"::error::REFUSED: {reason}. Nothing was run and nothing was logged.")
            if reasons:
                return EXIT_REFUSED
            print(f"config={LAYER_CONFIGS[args.layer]}")
            print(f"per_model_jobs={'true' if PER_MODEL_JOBS else 'false'}")
            return EXIT_OK
        if args.command == "pending-audit":
            pending = pending_audit_branches(args.repo, main_ref=args.main_ref)
            for branch in pending:
                print(f"::error::REFUSED: branch {branch} holds access-log entries main does not have yet: merge its pull "
                      "request first (the log is one hash chain). Nothing was run.")
            return EXIT_REFUSED if pending else EXIT_OK
        if args.command == "sync-access-log":
            print(json.dumps(sync_access_log(args.repo, args.base_out, main_ref=args.main_ref), sort_keys=True))
            return EXIT_OK
        if args.command == "clean-check":
            dirty = clean_check(args.repo)
            for path in dirty:
                print(f"::error::the working tree is not clean: {path} (the test lock would refuse; nothing was run)")
            return EXIT_FAILURE if dirty else EXIT_OK
        if args.command == "stage-results":
            summary = stage_results(args.runs_dir, args.layer, args.out)
            print(json.dumps(summary, indent=2, sort_keys=True))
            return EXIT_OK
        if args.command == "commit-results":
            print(json.dumps(commit_results(args.repo, args.staged, layer=args.layer, run_id=args.run_id), sort_keys=True))
            return EXIT_OK
        if args.command == "audit-branch":
            result = audit_branch(args.repo, args.new, args.base, tag=args.tag, run_id=args.run_id, layer=args.layer,
                                  main_ref=args.main_ref)
            print(f"branch={result['branch']}")
            print(f"entries_added={result['entries_added']}")
            return EXIT_OK
    except (FrozenCIError, results_log.ResultsLogError, OSError, ValueError) as exc:
        print(f"::error::{type(exc).__name__}: {exc}")
        return EXIT_FAILURE
    return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
