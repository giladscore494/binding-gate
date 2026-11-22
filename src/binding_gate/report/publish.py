"""Publishing a run's non-sensitive results under ``results/<layer>/<run_id>/`` (PR 16; section 17 item 37).

``runs/`` is git-ignored: run directories hold traces with benchmark text and,
for layer 3, third-party web pages. The committed location of the results that
back a published number is ``results/layer<N>/<run_id>/`` (ARCHITECTURE.md
section 7; PREREGISTRATION.md section 15, R10). This module copies into it only
files on an allow-list and refuses everything else::

    python -m binding_gate.report publish runs/<run_id> [--results-dir results] [--file PATH ...]
        [--include-traces]

Allowed (paths relative to the run directory):

* ``manifest.json`` (required): run id, config and its hash, commit, freeze
  tag, trace SHA-256 - no item text;
* ``invocations.jsonl``: per invocation the caps, spend, searches and record
  counts (what the report's cost section reads);
* ``frozen_run.jsonl``: the frozen run's record (tag, commit, layer, rule,
  access-log tail);
* ``analysis/analysis.json`` (required), ``analysis/report.md`` and
  ``analysis/figures/*.png``: the statistics, computed from traces only;
* ``snapshots/publish_manifest.json`` (required for a run with snapshots,
  i.e. layer 3): URLs, times, HTTP status and hashes of pages and searches -
  no page text, titles or snippets (PREREGISTRATION.md section 14);
* ``traces.jsonl`` only with ``--include-traces`` and only for layer 1, whose
  documents are Apache-2.0 (attribution in NOTICE). Layer-2 traces carry
  CC BY-SA passages and layer-3 traces carry web pages without a known licence:
  refused.

Never published: ``traces.jsonl`` of layers 2 and 3, any other file under
``snapshots/`` (raw pages, extracted text, search responses), re-scoring
outputs ``scores.*.jsonl`` (judge outputs quote the documents) and any file not
on the list. Asking for one with ``--file`` refuses the whole publication;
without ``--file`` every allowed file present is copied and every other file is
listed, with the reason, in ``results_manifest.json``.

Checks before anything is written: the run is finished and its
``traces.jsonl`` matches the SHA-256 in its manifest; the analysis covers this
run (same run id and trace SHA-256) and the same layer; the target directory
does not exist yet (published results are never overwritten). Local absolute
paths in the copied text files (the directory holding the run, the home
directory) are replaced by ``runs`` and ``~``; such files are marked
``rewritten`` with both hashes. Nothing is committed: commit the new directory
in a pull request.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import binding_gate
from binding_gate.canonical import utc_now_iso
from binding_gate.stats.prereg import LAYER_BY_DATASET

RESULTS_DIRNAME = "results"
RESULTS_MANIFEST = "results_manifest.json"
PUBLISH_VERSION = 1

MANIFEST = "manifest.json"
TRACES = "traces.jsonl"
ANALYSIS_JSON = "analysis/analysis.json"
SNAPSHOT_MANIFEST = "snapshots/publish_manifest.json"
ALLOWED_FIXED = (MANIFEST, "invocations.jsonl", "frozen_run.jsonl", ANALYSIS_JSON, "analysis/report.md", SNAPSHOT_MANIFEST)
TEXT_SUFFIXES = (".json", ".jsonl", ".md")
TRACES_ALLOWED_LAYERS = {1: "Deceptive Grounding documents are Apache-2.0 (attribution in NOTICE)"}


class PublishError(RuntimeError):
    """Raised when a run's results cannot be published; nothing is written."""


@dataclass
class PublishResult:
    target: Path
    layer: int
    files: list[dict[str, Any]] = field(default_factory=list)
    not_published: list[dict[str, str]] = field(default_factory=list)


def layer_dirname(layer: int) -> str:
    return f"layer{layer}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def refusal_reason(relpath: str, layer: int, *, include_traces: bool = False) -> str | None:
    """Why ``relpath`` (relative to the run directory, POSIX) may not be published; ``None`` if it may."""
    if relpath in ALLOWED_FIXED:
        return None
    if relpath.startswith("analysis/figures/") and relpath.endswith(".png") and relpath.count("/") == 2:
        return None
    if relpath == TRACES:
        if layer not in TRACES_ALLOWED_LAYERS:
            return (f"traces of layer {layer} contain third-party text whose licence does not allow publication "
                    "(layer 2: 10-K statements filed by the companies, reuse terms unverified; "
                    "layer 3: web pages without a known licence)")
        if not include_traces:
            return "traces are published only on request (--include-traces); they are identified by their SHA-256"
        return None
    if relpath.startswith("snapshots/"):
        return "live-web snapshots (raw pages, extracted text, search responses) are third-party material"
    if relpath.startswith("scores.") and relpath.endswith(".jsonl"):
        return "re-scoring outputs quote the documents"
    return "not on the allow-list of published files"


def _sanitise(data: bytes, run_dir: Path) -> bytes:
    text = data.decode("utf-8")
    parent = str(run_dir.resolve().parent)
    home = str(Path.home())
    for prefix, replacement in ((parent, "runs"), (home, "~")):
        if prefix and prefix not in ("/", "."):
            text = text.replace(prefix, replacement)
    return text.encode("utf-8")


def publish_run(
    run_dir: str | Path,
    *,
    results_dir: str | Path = RESULTS_DIRNAME,
    files: Sequence[str] | None = None,
    include_traces: bool = False,
) -> PublishResult:
    """Copy the allowed files of ``run_dir`` to ``results_dir/layer<N>/<run_id>/`` (see the module docstring)."""
    run_dir = Path(run_dir)
    try:
        manifest = json.loads((run_dir / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PublishError(f"{run_dir}: no readable {MANIFEST}: {exc}") from exc
    run_id = manifest.get("run_id")
    dataset = (manifest.get("config") or {}).get("dataset", {}).get("name")
    layer = LAYER_BY_DATASET.get(dataset)
    if not isinstance(run_id, str) or not run_id or "/" in run_id or run_id.startswith("."):
        raise PublishError(f"{run_dir}: the manifest has no usable run_id")
    if layer is None:
        raise PublishError(f"{run_dir}: dataset {dataset!r} belongs to no pre-registered layer")

    present = sorted(p.relative_to(run_dir).as_posix() for p in run_dir.rglob("*") if p.is_file())
    explicit = [Path(f).as_posix() for f in files or ()]
    if include_traces and TRACES not in explicit:
        explicit.append(TRACES)
    requested = list(present) if files is None else list(explicit)

    reasons: list[str] = []
    for relpath in explicit:  # what was asked for by name must be allowed, or nothing is published
        if relpath not in present:
            reasons.append(f"{relpath}: no such file in {run_dir}")
        elif (why := refusal_reason(relpath, layer, include_traces=include_traces)) is not None:
            reasons.append(f"{relpath}: refused - {why}")
    if manifest.get("ended_at") is None:
        reasons.append("the run is unfinished (its manifest has no end time); only finished runs are published")
    traces = run_dir / TRACES
    if not traces.is_file():
        reasons.append(f"{TRACES} is missing, so the run cannot be verified against its manifest")
    elif manifest.get("traces_sha256") != _sha256(traces.read_bytes()):
        reasons.append(f"{TRACES} does not match the SHA-256 in the manifest (changed after the run)")
    if ANALYSIS_JSON not in present:
        reasons.append(f"{ANALYSIS_JSON} is missing: analyse the run first (cli analyze writes RUN_DIR/analysis/)")
    else:
        try:
            analysis = json.loads((run_dir / ANALYSIS_JSON).read_text(encoding="utf-8"))
        except ValueError as exc:
            reasons.append(f"{ANALYSIS_JSON} is not valid JSON: {exc}")
        else:
            runs = {r.get("run_id"): r for r in analysis.get("inputs", {}).get("runs", [])}
            if run_id not in runs:
                reasons.append(f"{ANALYSIS_JSON} does not analyse run {run_id}")
            elif runs[run_id].get("traces_sha256") != manifest.get("traces_sha256"):
                reasons.append(f"{ANALYSIS_JSON} analysed other traces of run {run_id} (SHA-256 differs)")
            if analysis.get("layer") != layer:
                reasons.append(f"{ANALYSIS_JSON} is layer {analysis.get('layer')}, the run is layer {layer}")
    if any(p.startswith("snapshots/") for p in present) and SNAPSHOT_MANIFEST not in present:
        reasons.append(f"the run has snapshots but no {SNAPSHOT_MANIFEST} (python -m binding_gate.research.snapshots)")
    target = Path(results_dir) / layer_dirname(layer) / run_id
    if target.exists():
        reasons.append(f"{target} already exists; published results are never overwritten")
    if reasons:
        raise PublishError("publication refused, nothing was written: " + "; ".join(reasons))

    chosen = [p for p in requested if refusal_reason(p, layer, include_traces=include_traces) is None]
    result = PublishResult(target=target, layer=layer)
    result.not_published = [
        {"path": p, "reason": refusal_reason(p, layer, include_traces=include_traces) or ""}
        for p in present if p not in chosen
    ]
    staged: list[tuple[str, bytes, dict[str, Any]]] = []
    for relpath in chosen:
        data = (run_dir / relpath).read_bytes()
        entry: dict[str, Any] = {"path": relpath, "source_sha256": _sha256(data)}
        if relpath.endswith(TEXT_SUFFIXES):
            cleaned = _sanitise(data, run_dir)
            entry["rewritten"] = cleaned != data
            data = cleaned
        else:
            entry["rewritten"] = False
        entry["sha256"] = _sha256(data)
        entry["bytes"] = len(data)
        staged.append((relpath, data, entry))

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{run_id}.partial"
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        for relpath, data, entry in staged:
            out = tmp / relpath
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            result.files.append(entry)
        record = {
            "publish_version": PUBLISH_VERSION,
            "package_version": binding_gate.__version__,
            "published_at": utc_now_iso(),
            "run_id": run_id,
            "layer": layer,
            "dataset": dataset,
            "split": manifest.get("config", {}).get("split"),
            "test_unlock_tag": manifest.get("test_unlock_tag"),
            "git_commit": manifest.get("git_commit"),
            "config_hash": manifest.get("config_hash"),
            "traces_sha256": manifest.get("traces_sha256"),
            "trace_count": manifest.get("trace_count"),
            "files": result.files,
            "not_published": result.not_published,
            "rules": "binding_gate.report.publish (PR 16): allow-list only; no page text, no third-party text "
            "unless its licence allows it; local absolute paths replaced",
        }
        (tmp / RESULTS_MANIFEST).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.rename(target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return result
