"""The ``results-log`` branch: non-sensitive results the orchestrator can read without artifacts (PR 17).

The orchestrator of the study (an AI that drives the workflows through the GitHub
API) can read workflow logs and git branches, but cannot download Actions
artifacts (their storage host is unreachable from its sandbox). So, after every
workflow run, the machine-readable reports of that run are committed to a
dedicated branch, ``results-log`` (never ``main``; created as an orphan branch,
with no shared history, the first time):

``runs/<workflow run id>-<stage>/``
    one folder per finished run of ``dev pipeline`` (``<stage>`` = the stage),
    ``baseline`` started by hand (``baseline-<config stem>-<model>-<dry-run|paid>``)
    and ``frozen test run`` (``frozen-<layer>``), written by
    ``.github/workflows/results_log.yml`` (event ``workflow_run``) - so no job
    that holds an API key ever has a token that can write to the repository.
    A re-run attempt N > 1 gets ``-attempt<N>``. Inside: ``run.json`` (what the
    run was, which files were kept and which were left out, with the reason),
    ``<artifact name>/<path>`` for every kept file, and
    ``ledger-after-run/ledger.json``, the budget ledger computed right after the
    run (so its spend outlives the 90-day artifact retention).
``frozen/<layer>/<workflow run id>/``
    the published results of a frozen test run (``binding_gate.frozen_ci``,
    written by ``.github/workflows/frozen_test.yml``).

What may be committed (:func:`refusal_reason`; tested)
------------------------------------------------------
Only files on an ALLOW-LIST of report names (:data:`ALLOWED_NAMES`,
:data:`ALLOWED_PATTERNS`): the check-secrets report, the smoke usage checks, the
stop-rule decision, budget-ledger results, calibration reports, analyses
(``analysis.json``, ``report.md``, ``index.json``, ``*.log``), baseline
reports, the decomposer report, the judge agreement, layer-3 summaries (built
page-text free by ``binding_gate.layer3_summary``), run manifests and
``invocations.jsonl`` (config, hashes, caps, spend), the SEC dataset's
``manifest.json``, ``selection.json`` and the printed ``content_sha256``
(``prepare.json``), publish manifests of live-web runs (URLs and hashes) and
publication manifests. Then every kept file is SCANNED and left out if it is
not UTF-8 text, is larger than :data:`MAX_FILE_BYTES`, contains something that
looks like a secret (API-key, token or private-key patterns, an e-mail address
such as the SEC contact, or the value of any documented secret present in the
environment), or looks like page or document text (a JSON string longer than
:data:`MAX_STRING_CHARS`, a text-bearing key such as ``text``, ``content``,
``quote`` or ``messages`` holding more than :data:`MAX_TEXT_FIELD_CHARS`
characters, or a text line longer than :data:`MAX_STRING_CHARS`).

Never committed, whatever the scan says: traces (``traces.jsonl``: prompts with
benchmark documents, SEC statements or web pages), dataset files
(``items.jsonl``), decomposer records, judge scores (``scores.*.jsonl`` quote
the documents), live-web snapshots other than ``publish_manifest.json``,
encrypted or archived files, caches, seed runs, figures and anything else not on
the list. (The exceptions, the figures and the layer-1 traces of a frozen run,
whose documents are Apache-2.0, follow ``binding_gate.report.publish`` and are
handled by ``binding_gate.frozen_ci``.)

Committing (:func:`commit_to_branch`)
------------------------------------
In a temporary ``git worktree`` of the checkout (it shares the checkout's
credentials): fetch ``results-log`` (or start it as an orphan branch with a
README), put the folder in place, commit as ``github-actions[bot]``, push
without force. A push rejected because another run pushed first is retried
after ``git pull --rebase`` (folders of different runs never conflict); if the
rebase cannot apply (two runs created the orphan branch at once) the commit is
rebuilt on the fetched branch. Up to :data:`PUSH_ATTEMPTS` attempts. No
``concurrency`` group is used: GitHub would cancel a pending run of the group.

Commands (``python -m binding_gate.results_log <command>``)
-----------------------------------------------------------
``folder --run-id N --workflow NAME --title TITLE [--attempt N]``
    print the folder of a workflow run (``runs/<id>-<stage>``).
``collect SRC --out DIR --run-id N --workflow NAME --title TITLE [--attempt N] [--conclusion C] [--head-sha S]``
    copy the allowed files of downloaded artifacts (one sub-directory per
    artifact) into DIR and write ``DIR/run.json``.
``commit DIR --folder F [--repo .] [--message M]``
    commit DIR as folder F of the branch and push (GitHub Actions).
``export-ledgers --ref REF --out DIR [--repo .]``
    write every ``ledger.json`` of the branch at REF under DIR (read by the
    budget ledger; nothing if REF does not exist).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

from binding_gate.canonical import utc_now_iso

BRANCH = "results-log"
RUNS_DIR = "runs"
FROZEN_DIR = "frozen"
RUN_RECORD = "run.json"
LEDGER_AFTER_DIR = "ledger-after-run"
RESULTS_LOG_VERSION = 1
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
PUSH_ATTEMPTS = 6
MAX_FILE_BYTES = 5_000_000
MAX_TOTAL_BYTES = 60_000_000
MAX_STRING_CHARS = 2_000
MAX_TEXT_FIELD_CHARS = 300
STRING_LIMITS = {"ledger.json": 50_000, "ledger.md": 50_000}
"""Budget-ledger results are built from artifact names and numbers only; their problem lists can be long."""

ALLOWED_NAMES: dict[str, str] = {
    "report.json": "check-secrets report (secret NAMES and preflight results)",
    "report.md": "check-secrets report, or the report of one analysis",
    "smoke_usage_check.json": "smoke usage check (measured tokens vs the caps)",
    "smoke_usage_check.md": "smoke usage check (markdown)",
    "decision.json": "section 10 stop-rule decision",
    "decision.md": "section 10 stop-rule decision (markdown)",
    "ledger.json": "budget ledger result (spend entries, artifact ids)",
    "ledger.md": "budget ledger result (markdown)",
    "index.json": "index of the dev analyses",
    "analysis.json": "statistics computed from traces only",
    "calibration_dev_gated.json": "gate calibration report",
    "calibration_dev_gated.txt": "gate calibration report (text)",
    "baseline_report.json": "stop-rule report over every model",
    "baseline_report.txt": "stop-rule report (text)",
    "decomposer_report.json": "decomposer measurement against section 7",
    "judge_agreement.json": "judge agreement (section 6.5)",
    "judge_agreement.txt": "judge agreement (text)",
    "layer3_summary.json": "layer-3 summary (costs, outcomes, URLs, hashes; no page text)",
    "prepare.json": "SEC dataset: the printed output of dataset-prepare (content_sha256)",
    "prepare.md": "SEC dataset: summary (markdown)",
    "prepare_report.json": "SEC dataset: the hash to pin",
    "manifest.json": "run manifest (config, hashes, commit) or the SEC dataset manifest (no item text)",
    "selection.json": "SEC dataset: the company selection (CIKs, names, filings)",
    "invocations.jsonl": "per invocation: caps, spend, searches, record counts",
    "frozen_run.jsonl": "frozen run record (tag, commit, layer, rule, access-log tail)",
    "publish_manifest.json": "live-web publish manifest (URLs, times, hashes; no page text)",
    "results_manifest.json": "publication manifest (binding_gate.report.publish)",
    "frozen_summary.json": "frozen test run: what ran and what was committed",
    RUN_RECORD: "this folder's record",
}
ALLOWED_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"baseline_report_[A-Za-z0-9._-]+\.(json|txt)"), "stop-rule report of one model"),
    (re.compile(r"[A-Za-z0-9._-]+\.log"), "output of one analysis or calibration command"),
)
NEVER_NAMES: dict[str, str] = {
    "traces.jsonl": "traces carry prompts with benchmark documents, SEC statements or web pages",
    "items.jsonl": "dataset items carry document text",
    "decompositions.jsonl": "decomposer records carry the questions",
    "retrieval_log.jsonl": "the EDGAR retrieval log is not a result",
    "test_access_log.jsonl": "the access log is committed to main by the frozen workflow's pull request",
}
NEVER_DIRS = {"snapshots": "live-web snapshots are third-party material", "cache": "caches are not results",
              "seeds": "seed runs belong to earlier runs", "private": "encrypted material"}
NEVER_SUFFIXES = (".gpg", ".tar", ".gz", ".tgz", ".zip", ".png", ".jpg", ".pdf", ".pt", ".bin", ".safetensors")
TEXT_KEYS = frozenset({
    "text", "page_text", "content", "quote", "quotes", "snippet", "snippets", "html", "body", "raw", "raw_text",
    "extracted_text", "document", "documents", "messages", "prompt", "prompts", "completion", "output_text",
    "answer_text", "passage", "passages", "context", "title", "titles",
})
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("an API key (sk-...)", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("an e-mail address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
)
EXTRA_SECRET_ENV = ("GITHUB_TOKEN", "GH_TOKEN", "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")
README = """# results-log

Machine-written branch of binding-gate (PR 17; `src/binding_gate/results_log.py`). It never merges into `main`.

- `runs/<workflow run id>-<stage>/`: the non-sensitive reports of one finished workflow run
  (`run.json` lists what was kept and what was left out), and `ledger-after-run/ledger.json`.
- `frozen/<layer>/<workflow run id>/`: the published results of a frozen test run.

Never here: traces, dataset items, judge scores, web-page snapshots, encrypted files, secrets.
"""


class ResultsLogError(RuntimeError):
    """Raised when the results cannot be collected or committed."""


# --------------------------------------------------------------------------- #
# Folder names
# --------------------------------------------------------------------------- #


def _slug(text: str, limit: int = 80) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return (slug[:limit].rstrip("-.") or "run")


def stage_label(workflow: str, title: str) -> str:
    """The ``<stage>`` part of a folder, from the workflow's name and the run's title (``run-name``)."""
    from binding_gate.pipeline import STAGES

    title = title.strip()
    if workflow == "dev pipeline":
        stage = title.split(":", 1)[1].strip() if ":" in title else ""
        return stage if stage in STAGES else _slug(f"dev-pipeline-{stage or title}")
    if workflow == "baseline":
        match = re.fullmatch(r"baseline:\s*(\S+)\s*\((\S+),\s*(dry run|paid)\)", title)
        if match:
            config, model, mode = match.groups()
            return _slug(f"baseline-{PurePosixPath(config).stem}-{model}-{'dry-run' if mode == 'dry run' else 'paid'}")
        return _slug(f"baseline-{title}")
    if workflow == "frozen test run":
        match = re.fullmatch(r"frozen test run:\s*([A-Za-z0-9_]+)\b.*", title)
        return _slug(f"frozen-{match.group(1)}" if match else f"frozen-{title}")
    return _slug(workflow or title)


def run_folder(run_id: int | str, stage: str, attempt: int | str = 1) -> str:
    run_id = str(run_id)
    if not run_id.isdigit():
        raise ResultsLogError(f"workflow run id must be a number, not {run_id!r}")
    suffix = f"-attempt{int(attempt)}" if int(attempt) > 1 else ""
    return f"{RUNS_DIR}/{run_id}-{_slug(stage)}{suffix}"


def check_folder(folder: str) -> str:
    path = PurePosixPath(folder)
    if path.is_absolute() or ".." in path.parts or len(path.parts) < 2 or path.parts[0] not in (RUNS_DIR, FROZEN_DIR):
        raise ResultsLogError(f"folder must be runs/<...> or frozen/<...> inside the branch, not {folder!r}")
    if any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in path.parts):
        raise ResultsLogError(f"folder {folder!r} has characters outside A-Z a-z 0-9 . _ -")
    return path.as_posix()


# --------------------------------------------------------------------------- #
# The allow-list and the scan
# --------------------------------------------------------------------------- #


def name_refusal(relpath: str) -> str | None:
    """Why a file may not be committed judging by its path alone; ``None`` if its name is on the allow-list."""
    path = PurePosixPath(relpath)
    name = path.name
    if name in NEVER_NAMES:
        return NEVER_NAMES[name]
    if name.startswith("scores.") and name.endswith(".jsonl"):
        return "judge scores quote the documents"
    for part in path.parts[:-1]:
        if part in NEVER_DIRS and not (part == "snapshots" and name == "publish_manifest.json"):
            return NEVER_DIRS[part]
    if name.lower().endswith(NEVER_SUFFIXES):
        return "binary, archived or encrypted file"
    if name in ALLOWED_NAMES or any(p.fullmatch(name) for p, _why in ALLOWED_PATTERNS):
        return None
    return "not on the allow-list of report files"


def secret_values(environ: Mapping[str, str] | None = None) -> list[str]:
    """Values of documented secrets (and tokens) present in the environment, to be found in no committed file."""
    from binding_gate.secrets import KNOWN_SECRETS

    environ = os.environ if environ is None else environ
    names = list(KNOWN_SECRETS) + list(EXTRA_SECRET_ENV)
    return [environ[n] for n in names if len(environ.get(n, "") or "") >= 8]


def _json_problem(value: Any, key: str | None = None, limit: int = MAX_STRING_CHARS) -> str | None:
    if isinstance(value, str):
        if len(value) > limit:
            return f"a string of {len(value)} characters (possible page or document text)"
        if key is not None and key.lower() in TEXT_KEYS and len(value) > MAX_TEXT_FIELD_CHARS:
            return f"a {key!r} field of {len(value)} characters (possible page or document text)"
        return None
    if isinstance(value, dict):
        for k, v in value.items():
            problem = _json_problem(v, str(k), limit)
            if problem:
                return problem
    elif isinstance(value, list):
        for v in value:
            problem = _json_problem(v, key, limit)
            if problem:
                return problem
    return None


def content_refusal(relpath: str, data: bytes, *, secrets: Sequence[str] = ()) -> str | None:
    """Why the content of an allowed file may not be committed (see the module docstring); ``None`` if it may."""
    if len(data) > MAX_FILE_BYTES:
        return f"larger than {MAX_FILE_BYTES} bytes"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "not UTF-8 text"
    if "\x00" in text:
        return "not text (NUL byte)"
    for value in secrets:
        if value and value in text:
            return "contains the value of a secret"
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return f"contains what looks like {label}"
    name = PurePosixPath(relpath).name
    limit = STRING_LIMITS.get(name, MAX_STRING_CHARS)
    if name.endswith(".json"):
        try:
            parsed = json.loads(text)
        except ValueError:
            return "not valid JSON"
        problem = _json_problem(parsed, None, limit)
        if problem:
            return f"contains {problem}"
    elif name.endswith(".jsonl"):
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                return f"line {number} is not valid JSON"
            problem = _json_problem(parsed, None, limit)
            if problem:
                return f"line {number} contains {problem}"
    else:
        for number, line in enumerate(text.splitlines(), start=1):
            if len(line) > limit:
                return f"line {number} has {len(line)} characters (possible page or document text)"
    return None


def refusal_reason(relpath: str, data: bytes, *, secrets: Sequence[str] = ()) -> str | None:
    """Why ``relpath`` with content ``data`` may not be committed to the results-log branch; ``None`` if it may."""
    return name_refusal(relpath) or content_refusal(relpath, data, secrets=secrets)


# --------------------------------------------------------------------------- #
# Collecting the files of a workflow run
# --------------------------------------------------------------------------- #


def collect(
    src: str | Path,
    out: str | Path,
    *,
    meta: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Copy the allowed files under ``src`` (one sub-directory per artifact) to ``out``; write ``out/run.json``."""
    src, out = Path(src), Path(out)
    if out.exists() and any(out.iterdir()):
        raise ResultsLogError(f"{out} must be empty")
    out.mkdir(parents=True, exist_ok=True)
    secrets = secret_values(environ)
    kept: list[dict[str, Any]] = []
    left_out: list[dict[str, str]] = []
    total = 0
    files = sorted(p for p in src.rglob("*") if p.is_file() and not p.is_symlink()) if src.is_dir() else []
    for path in files:
        relpath = path.relative_to(src).as_posix()
        if PurePosixPath(relpath).name == RUN_RECORD:
            left_out.append({"path": relpath, "reason": f"{RUN_RECORD} is written by the collector"})
            continue
        why = name_refusal(relpath)
        data = b""
        if why is None:
            data = path.read_bytes()
            why = content_refusal(relpath, data, secrets=secrets)
        if why is None and total + len(data) > MAX_TOTAL_BYTES:
            why = f"the folder would exceed {MAX_TOTAL_BYTES} bytes"
        if why is not None:
            left_out.append({"path": relpath, "reason": why})
            continue
        target = out / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        total += len(data)
        kept.append({"path": relpath, "bytes": len(data), "sha256": _sha256(data)})
    record = {
        "results_log_version": RESULTS_LOG_VERSION,
        "collected_at": utc_now_iso(),
        **{k: meta[k] for k in sorted(meta)},
        "artifacts": sorted({PurePosixPath(f["path"]).parts[0] for f in kept + left_out if "/" in f["path"]}),
        "kept": kept,
        "left_out": left_out,
        "rules": "binding_gate.results_log: report files on an allow-list, scanned for secrets and page text",
    }
    data = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    problem = content_refusal(RUN_RECORD, data, secrets=secrets)
    if problem:  # the title or the paths themselves could carry something; never commit it
        record = {k: v for k, v in record.items() if k in ("results_log_version", "collected_at", "run_id", "kept")}
        record["left_out"] = [{"path": "*", "reason": f"details withheld: {problem}"}]
        data = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (out / RUN_RECORD).write_bytes(data)
    return record


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Git
# --------------------------------------------------------------------------- #


def _git(cwd: Path, *args: str, check: bool = True, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False, env=full_env)
    if check and result.returncode != 0:
        raise ResultsLogError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()[:500]}")
    return result


BOT_ENV = {"GIT_AUTHOR_NAME": BOT_NAME, "GIT_AUTHOR_EMAIL": BOT_EMAIL, "GIT_COMMITTER_NAME": BOT_NAME,
           "GIT_COMMITTER_EMAIL": BOT_EMAIL, "GIT_TERMINAL_PROMPT": "0"}


def remote_branch_exists(cwd: Path, remote: str, branch: str) -> bool:
    result = _git(cwd, "ls-remote", "--exit-code", "--heads", remote, branch, check=False)
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    raise ResultsLogError(f"git ls-remote {remote} failed: {result.stderr.strip()[:300]}")


def _prepare_tree(wt: Path, remote: str, branch: str) -> bool:
    """Check out the remote branch in the worktree, or start it as an orphan; True if it existed."""
    if remote_branch_exists(wt, remote, branch):
        _git(wt, "fetch", "-q", "--no-tags", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
        _git(wt, "checkout", "-q", "-f", "-B", branch, f"refs/remotes/{remote}/{branch}")
        _git(wt, "clean", "-fdq")
        return True
    _git(wt, "checkout", "-q", "--detach", check=False)
    _git(wt, "branch", "-D", "-q", branch, check=False)
    _git(wt, "checkout", "-q", "--orphan", branch)
    _git(wt, "rm", "-rfq", "--ignore-unmatch", ".")
    _git(wt, "clean", "-fdqx")
    (wt / "README.md").write_text(README, encoding="utf-8", newline="\n")
    return False


def _place(staging: Path, wt: Path, folder: str) -> None:
    target = wt / folder
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(staging, target)


def staging_refusals(staging: str | Path, *, exempt: Iterable[str] = (), environ: Mapping[str, str] | None = None) -> list[str]:
    """Every file under ``staging`` that may not be committed, with the reason (the last check before a commit).

    ``exempt`` lists relative paths allowed by another rule (the figures and the layer-1 traces of a frozen
    run, under ``binding_gate.report.publish``); traces are still checked for secrets, figures for being PNG.
    """
    staging = Path(staging)
    secrets = secret_values(environ)
    exempt = set(exempt)
    problems = []
    for path in sorted(p for p in staging.rglob("*") if p.is_file() or p.is_symlink()):
        relpath = path.relative_to(staging).as_posix()
        if path.is_symlink():
            problems.append(f"{relpath}: a symbolic link")
            continue
        data = path.read_bytes()
        if relpath in exempt and relpath.endswith(".png"):
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                problems.append(f"{relpath}: not a PNG image")
            continue
        if relpath in exempt:
            text = _decompress(relpath, data)
            if any(v and v.encode("utf-8") in text for v in secrets) or any(
                    p.search(text.decode("utf-8", "replace")) for label, p in SECRET_PATTERNS if label != "an e-mail address"):
                problems.append(f"{relpath}: contains what looks like a secret")
            continue
        why = refusal_reason(relpath, data, secrets=secrets)
        if why:
            problems.append(f"{relpath}: {why}")
    return problems


def _decompress(relpath: str, data: bytes) -> bytes:
    if relpath.endswith(".gz"):
        import gzip

        return gzip.decompress(data)
    return data


def commit_to_branch(
    repo: str | Path,
    staging: str | Path,
    folder: str,
    *,
    message: str,
    branch: str = BRANCH,
    remote: str = "origin",
    attempts: int = PUSH_ATTEMPTS,
    before_push: Callable[[int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    exempt: Iterable[str] = (),
) -> dict[str, Any]:
    """Commit ``staging`` as ``folder`` of ``branch`` and push (see the module docstring)."""
    repo, staging = Path(repo), Path(staging)
    folder = check_folder(folder)
    if not staging.is_dir():
        raise ResultsLogError(f"{staging} is not a directory")
    problems = staging_refusals(staging, exempt=exempt)
    if problems:
        raise ResultsLogError("nothing was committed; files that may not go to the results-log branch: " + "; ".join(problems))
    tmp = Path(tempfile.mkdtemp(prefix="results-log-"))
    wt = tmp / "worktree"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
    try:
        for attempt in range(1, attempts + 1):
            existed = _prepare_tree(wt, remote, branch)
            _place(staging, wt, folder)
            _git(wt, "add", "-A", "--", folder, *([] if existed else ["README.md"]))
            if _git(wt, "diff", "--cached", "--quiet", check=False).returncode == 0:
                return {"result": "unchanged", "folder": folder, "attempts": attempt,
                        "commit": _git(wt, "rev-parse", "HEAD").stdout.strip() if existed else None}
            _git(wt, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message, env=BOT_ENV)
            if before_push is not None:
                before_push(attempt)
            if _git(wt, "push", "-q", remote, f"HEAD:refs/heads/{branch}", check=False, env=BOT_ENV).returncode == 0:
                return {"result": "pushed", "folder": folder, "attempts": attempt,
                        "commit": _git(wt, "rev-parse", "HEAD").stdout.strip()}
            # Rejected (normally: another run pushed first). Rebase onto it and push again.
            rebased = _git(wt, "-c", "commit.gpgsign=false", "pull", "-q", "--rebase", "--no-tags", remote, branch,
                           check=False, env=BOT_ENV)
            if rebased.returncode == 0 and _git(wt, "push", "-q", remote, f"HEAD:refs/heads/{branch}", check=False,
                                                env=BOT_ENV).returncode == 0:
                return {"result": "pushed", "folder": folder, "attempts": attempt, "rebased": True,
                        "commit": _git(wt, "rev-parse", "HEAD").stdout.strip()}
            _git(wt, "rebase", "--abort", check=False)
            sleep(min(30.0, 2.0 * attempt))  # then rebuild the commit on the fetched branch
        raise ResultsLogError(f"could not push {folder} to {branch} after {attempts} attempts")
    finally:
        _git(repo, "worktree", "remove", "--force", str(wt), check=False)
        _git(repo, "worktree", "prune", check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def export_ledgers(repo: str | Path, ref: str, out: str | Path) -> list[str]:
    """Write every ``ledger.json`` of the tree at ``ref`` under ``out`` (same relative paths); [] if no such ref."""
    repo, out = Path(repo), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).returncode != 0:
        return []
    names = _git(repo, "ls-tree", "-r", "--name-only", "-z", ref).stdout.split("\0")
    written = []
    for name in sorted(n for n in names if n and PurePosixPath(n).name == "ledger.json"):
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            continue
        data = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{name}"], capture_output=True, check=True).stdout
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(name)
    return written


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def _meta(args: argparse.Namespace) -> dict[str, Any]:
    stage = stage_label(args.workflow, args.title)
    meta = {"run_id": int(args.run_id), "workflow": args.workflow, "title": args.title, "stage": stage,
            "attempt": int(args.attempt), "folder": run_folder(args.run_id, stage, args.attempt)}
    for key in ("conclusion", "head_sha", "head_branch", "event"):
        value = getattr(args, key, None)
        if value:
            meta[key] = value
    return meta


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m binding_gate.results_log", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def with_run(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--run-id", required=True)
        sub.add_argument("--workflow", required=True)
        sub.add_argument("--title", default="")
        sub.add_argument("--attempt", default="1")

    with_run(commands.add_parser("folder"))
    sub = commands.add_parser("collect")
    sub.add_argument("src")
    sub.add_argument("--out", required=True)
    with_run(sub)
    for key in ("--conclusion", "--head-sha", "--head-branch", "--event"):
        sub.add_argument(key, default="")
    sub = commands.add_parser("commit")
    sub.add_argument("staging")
    sub.add_argument("--folder", required=True)
    sub.add_argument("--repo", default=".")
    sub.add_argument("--message", default=None)
    sub = commands.add_parser("export-ledgers")
    sub.add_argument("--ref", required=True)
    sub.add_argument("--out", required=True)
    sub.add_argument("--repo", default=".")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "folder":
            print(_meta(args)["folder"])
            return 0
        if args.command == "collect":
            meta = _meta(args)
            record = collect(args.src, args.out, meta=meta)
            print(f"folder={meta['folder']}")
            print(f"kept {len(record['kept'])} file(s), left out {len(record['left_out'])}")
            for row in record["left_out"]:
                print(f"  left out {row['path']}: {row['reason']}")
            return 0
        if args.command == "commit":
            message = args.message or f"results-log: {args.folder}"
            result = commit_to_branch(args.repo, args.staging, args.folder, message=message)
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "export-ledgers":
            written = export_ledgers(args.repo, args.ref, args.out)
            print(f"{len(written)} ledger snapshot(s) read from {args.ref}")
            return 0
    except (ResultsLogError, OSError, ValueError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 1


def iter_ledger_files(root: str | Path) -> Iterable[Path]:
    """Every ``ledger.json`` under ``root`` (exported results-log snapshots)."""
    root = Path(root)
    return sorted(root.rglob("ledger.json")) if root.is_dir() else []


if __name__ == "__main__":
    sys.exit(main())
