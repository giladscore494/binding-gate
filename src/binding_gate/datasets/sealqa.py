"""Dataset adapter for SealQA (layer 3: live web search).

Source: the Hugging Face dataset ``vtllms/sealqa`` (Pham et al., "SealQA: Raising
the Bar for Reasoning in Search-Augmented Language Models", ICLR 2026, arXiv
2506.01062), licence Apache-2.0 (dataset card and ``LICENSE`` file), not gated,
pinned at revision :data:`PINNED_REVISION` (the dataset's ``main`` on
2026-11-22, last modified 2026-11-04). The dataset card says the answers are
reviewed and updated over time ("Next update: Dec 9 2026"), so the revision pin
matters: gold answers are those of this revision.

What was verified, and how (2026-11-22)
---------------------------------------
* Revision, file list, sizes and SHA-256 of every file: from the Hub API
  (``/api/datasets/vtllms/sealqa`` and ``.../tree/main``); the ``LICENSE`` and
  ``README.md`` were downloaded at the pinned revision and their hashes checked.
* Column names and types per subset: from the Hub's Croissant metadata
  (``/api/datasets/vtllms/sealqa/croissant``): ``question``, ``answer``,
  ``urls`` (list of strings), ``freshness``, ``question_types`` (list of
  strings), ``effective_year``, ``search_results``, ``topic``, ``canary``;
  ``longseal`` additionally ``golds``, ``12_docs``, ``20_docs``, ``30_docs``
  (lists of documents with ``date``, ``text``, ``title``).
* **Not verified:** the parquet files themselves could not be downloaded from the
  build sandbox (the Hub's file CDN was blocked by the sandbox proxy), so the row
  counts (reported: seal_0 111, seal_hard 254, longseal 254), the actual values
  and their types are known only from the paper and the metadata. The adapter
  checks everything it relies on when the files are converted (``prepare``)
  and loaded; the first ``prepare`` on a machine with access settles it.
* From the paper (arXiv HTML, read 2026-11-22): Seal-Hard "includes Seal-0 and
  additional difficult questions"; ``freshness`` is never / slow / fast;
  ``search_results`` is the expected kind of search results, "conflict" or
  "unhelpful"; ``effective_year`` is the year the answer last changed; answers
  are graded by a GPT-4o-mini auto-rater with a SimpleQA-style template
  (CORRECT / INCORRECT / NOT_ATTEMPTED); no alias lists are provided.

Which subsets are used, and why
-------------------------------
* ``seal_hard`` (all 254 reported questions, which include the 111 of
  ``seal_0``) is the layer-3 item set. ``seal_0`` is read too, only to mark its
  questions (stratum ``seal_0/...``), so results can be reported for Seal-0
  separately without counting any question twice.
* ``longseal`` is NOT part of layer 3: it is a long-context test in which the
  documents (one gold page among 12-30 hard negatives) are supplied with the
  question, so nothing is searched; and its questions are the Seal-Hard
  questions again. Using it would mean a fourth, offline condition on the same
  questions, not live web evidence. It is not downloaded.

Mapping onto :class:`~binding_gate.contracts.Item`
--------------------------------------------------
* ``group_id`` = ``id`` = ``"sealqa/" + first 16 hex digits of
  SHA-256(normalize_text(question))``. SealQA rows have no id; the question
  text is the row's identity (section 4: the unit for SealQA is the row, i.e.
  the question). A question that occurs in both subsets is one item; if its
  answer differed between the files the adapter would refuse.
* ``question`` = the row's question, verbatim; ``documents`` = () (evidence is
  fetched at run time).
* ``gold`` = ``answer``, ``answers`` (``[answer]``; SealQA provides no aliases),
  ``freshness``, ``question_types``, ``effective_year``, ``search_results``,
  ``topic``, ``annotator_urls`` (the row's ``urls``: sources the annotators
  cited, which would reveal the answer, hence gold), ``subsets`` and
  ``dataset_revision``.
* ``stratum`` = ``"<seal_0|seal_hard>/<freshness>"`` (``seal_hard`` = in
  Seal-Hard but not in Seal-0).

Getting the data (``prepare``, run by the workflow; needs the ``sealqa`` extra)
-------------------------------------------------------------------------------
``python -m binding_gate.cli dataset-prepare --config <config>`` downloads the
files of :data:`FILE_MANIFEST` from
``https://huggingface.co/datasets/vtllms/sealqa/resolve/<revision>/<file>``
into ``root`` (under ``data/raw/``, git-ignored), checks size and SHA-256 of
each, converts the two parquet files to JSON Lines (``seal_hard.jsonl``,
``seal_0.jsonl``; one canonical JSON object per row, columns unchanged) with
``pyarrow``, and writes ``conversion.json`` (source hashes, pyarrow version,
row counts, SHA-256 of each JSON Lines file). Loading needs only the standard
library: it re-checks the parquet hashes against the pins and the JSON Lines
hashes against ``conversion.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from binding_gate.canonical import canonical_json, sha256_json, sha256_text
from binding_gate.contracts import Document, Item, Message
from binding_gate.registry import register
from binding_gate.textnorm import normalize_text

DATASET_NAME = "sealqa"
HF_DATASET = "vtllms/sealqa"
REPOSITORY_URL = f"https://huggingface.co/datasets/{HF_DATASET}"
PINNED_REVISION = "0e75db526ad93dd039714897b7631afc03510e30"

FILE_MANIFEST: dict[str, tuple[int, str]] = {
    "LICENSE": (11357, "58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd"),
    "README.md": (4056, "6e3513121ee6197a3a96292c4a65ead3072d66071f288ca1fb543740d4468fc4"),
    "seal-hard.parquet": (49966, "b13fbacabac4b055dcabfae161df4dc661e31e915507324790779d107ab53e7e"),
    "seal-0.parquet": (27169, "89c729bd0a13c56847bb30cbdce1e091f7b832bbe9929d3744357cedcf4e33be"),
}
"""File -> (size in bytes, SHA-256) at :data:`PINNED_REVISION`, from the Hub API
(``lfs.oid`` is the SHA-256 of an LFS file's content). ``longseal.parquet``
(73 MB) is deliberately absent: it is not used."""

SUBSETS: dict[str, tuple[str, str]] = {
    "seal_hard": ("seal-hard.parquet", "seal_hard.jsonl"),
    "seal_0": ("seal-0.parquet", "seal_0.jsonl"),
}
DEFAULT_SUBSETS = ("seal_hard", "seal_0")
CONVERSION_NAME = "conversion.json"
REQUIRED_COLUMNS = frozenset({"question", "answer"})
EXPECTED_COLUMNS = frozenset(
    {"question", "answer", "urls", "freshness", "question_types", "effective_year", "search_results", "topic", "canary"}
)
"""Columns listed by the Hub's Croissant metadata for seal_0 and seal_hard (2026-11-22)."""
REPORTED_ROWS = {"seal_0": 111, "seal_hard": 254}
"""Row counts reported by the dataset card / paper; recorded, checked only as a warning."""


class DatasetError(RuntimeError):
    """Raised when the SealQA files are missing, altered or not in the expected shape."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_url(revision: str, filename: str) -> str:
    return f"{REPOSITORY_URL}/resolve/{revision}/{filename}"


def group_id_of(question: str) -> str:
    """``sealqa/`` + 16 hex digits of SHA-256 of the normalised question (see the module docstring)."""
    return "sealqa/" + sha256_text(normalize_text(question))[:16]


# --------------------------------------------------------------------------- #
# prepare: download at the pinned revision and convert parquet -> JSON Lines
# --------------------------------------------------------------------------- #

Downloader = Callable[[str, Path], None]


def urllib_download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "binding-gate-dataset-prepare"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as handle:
        for chunk in iter(lambda: response.read(1 << 20), b""):
            handle.write(chunk)


def _json_value(value: Any, where: str) -> Any:
    """Make a parquet value JSON-compatible without changing its meaning; refuse anything else."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise DatasetError(f"{where}: NaN or infinity")
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_value(v, f"{where}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, dict):
        return {str(k): _json_value(v, f"{where}.{k}") for k, v in value.items()}
    raise DatasetError(f"{where}: value of type {type(value).__name__} cannot be stored as JSON")


def parquet_rows(path: Path) -> tuple[list[dict[str, Any]], str]:
    """Rows of a parquet file as plain Python values, and the pyarrow version used."""
    try:
        import pyarrow
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise DatasetError(
            "converting SealQA's parquet files needs pyarrow: pip install -e '.[sealqa]'"
        ) from exc
    return pq.read_table(path).to_pylist(), str(pyarrow.__version__)


def prepare(
    root: str | Path,
    *,
    revision: str = PINNED_REVISION,
    download: Downloader = urllib_download,
    read_parquet: Callable[[Path], tuple[list[dict[str, Any]], str]] = parquet_rows,
) -> dict[str, Any]:
    """Download, verify and convert (see the module docstring). Returns the conversion record."""
    if revision != PINNED_REVISION:
        raise DatasetError(f"only the pinned revision {PINNED_REVISION} has a file manifest; got {revision}")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for filename, (size, sha) in FILE_MANIFEST.items():
        path = root / filename
        if not (path.is_file() and path.stat().st_size == size and file_sha256(path) == sha):
            tmp = path.with_name(path.name + ".part")
            download(resolve_url(revision, filename), tmp)
            if tmp.stat().st_size != size or file_sha256(tmp) != sha:
                actual = (tmp.stat().st_size, file_sha256(tmp))
                tmp.unlink()
                raise DatasetError(f"{filename}: got {actual}, expected size {size} and SHA-256 {sha}")
            tmp.replace(path)
    outputs: dict[str, Any] = {}
    versions: set[str] = set()
    for subset, (parquet_name, jsonl_name) in SUBSETS.items():
        rows, version = read_parquet(root / parquet_name)
        versions.add(version)
        if not rows:
            raise DatasetError(f"{parquet_name} has no rows")
        columns = set(rows[0])
        if any(set(row) != columns for row in rows):
            raise DatasetError(f"{parquet_name}: rows do not all have the same columns")
        missing = sorted(REQUIRED_COLUMNS - columns)
        if missing:
            raise DatasetError(f"{parquet_name} lacks the required column(s) {missing}; columns: {sorted(columns)}")
        lines = [canonical_json(_json_value(row, f"{parquet_name} row {i}")) for i, row in enumerate(rows)]
        text = "".join(line + "\n" for line in lines)
        (root / jsonl_name).write_text(text, encoding="utf-8", newline="\n")
        outputs[subset] = {
            "source": parquet_name,
            "source_sha256": FILE_MANIFEST[parquet_name][1],
            "jsonl": jsonl_name,
            "jsonl_sha256": sha256_text(text),
            "rows": len(rows),
            "reported_rows": REPORTED_ROWS.get(subset),
            "columns": sorted(columns),
            "unexpected_columns": sorted(columns - EXPECTED_COLUMNS),
            "missing_expected_columns": sorted(EXPECTED_COLUMNS - columns),
        }
    conversion = {
        "conversion_version": 1,
        "dataset": HF_DATASET,
        "revision": revision,
        "pyarrow_version": sorted(versions)[0] if len(versions) == 1 else sorted(versions),
        "subsets": outputs,
    }
    (root / CONVERSION_NAME).write_text(json.dumps(conversion, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return conversion


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #


def _text_field(row: dict[str, Any], key: str, where: str, *, required: bool) -> str | None:
    value = row.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise DatasetError(f"{where}: {key!r} must be a {'non-empty ' if required else ''}string, got {value!r:.60}")
    return value


def _list_field(row: dict[str, Any], key: str, where: str) -> list[str]:
    value = row.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise DatasetError(f"{where}: {key!r} must be a list of strings")
    return list(value)


# Registered only when imported as a module, not when run with ``python -m`` (as in llm_judge_checks).
@register("dataset", DATASET_NAME) if __name__ != "__main__" else (lambda component: component)
class SealQAAdapter:
    """Options (``[dataset.options]``):

    ``root``        directory of the prepared files (required; under ``data/raw/``)
    ``revision``    must equal :data:`PINNED_REVISION` (default)
    ``subsets``     subsets read; default ``["seal_hard", "seal_0"]`` (see the module docstring)
    ``repository``  where the files come from (informational; used by ``dataset-pin``)
    """

    name = DATASET_NAME
    pin_method = "prepare"
    """Tells the workflow to run ``cli dataset-prepare`` instead of cloning a git repository."""

    def __init__(
        self,
        *,
        root: str,
        revision: str = PINNED_REVISION,
        subsets: Sequence[str] = DEFAULT_SUBSETS,
        repository: str = REPOSITORY_URL,
    ):
        if not isinstance(root, str) or not root:
            raise ValueError("root must be a non-empty path")
        if revision != PINNED_REVISION:
            raise ValueError(f"revision must be the pinned {PINNED_REVISION}")
        if isinstance(subsets, str) or not subsets or any(s not in SUBSETS for s in subsets) or len(set(subsets)) != len(subsets):
            raise ValueError(f"subsets must be a non-empty list of distinct names from {sorted(SUBSETS)}")
        if subsets[0] != "seal_hard":
            raise ValueError("subsets must start with 'seal_hard' (the item set; seal_0 only marks questions)")
        self.root = Path(root)
        self.revision = revision
        self.commit = revision
        self.repository = repository
        self.subsets = tuple(subsets)
        self._verified: dict[str, Any] | None = None

    def prepare(self, **kwargs: Any) -> dict[str, Any]:
        return prepare(self.root, revision=self.revision, **kwargs)

    def verify(self) -> dict[str, Any]:
        """Check the prepared files against the pins and ``conversion.json``; return the latter."""
        if self._verified is not None:
            return self._verified
        path = self.root / CONVERSION_NAME
        if not path.is_file():
            raise DatasetError(
                f"{path} is missing: prepare the data first with "
                "`python -m binding_gate.cli dataset-prepare --config <config>` (needs the sealqa extra)"
            )
        conversion = json.loads(path.read_text(encoding="utf-8"))
        if conversion.get("revision") != self.revision:
            raise DatasetError(f"{path} is for revision {conversion.get('revision')}, the config pins {self.revision}")
        for subset in self.subsets:
            parquet_name, jsonl_name = SUBSETS[subset]
            size, sha = FILE_MANIFEST[parquet_name]
            source = self.root / parquet_name
            if not source.is_file() or source.stat().st_size != size or file_sha256(source) != sha:
                raise DatasetError(f"{source} does not match the pinned SHA-256 {sha}")
            entry = conversion.get("subsets", {}).get(subset)
            if not entry or entry.get("source_sha256") != sha:
                raise DatasetError(f"{path} has no conversion of {parquet_name} at the pinned hash")
            jsonl = self.root / jsonl_name
            if not jsonl.is_file() or file_sha256(jsonl) != entry.get("jsonl_sha256"):
                raise DatasetError(f"{jsonl} does not match the hash recorded in {CONVERSION_NAME}")
        self._verified = conversion
        return conversion

    def fingerprint(self) -> str:
        conversion = self.verify()
        return sha256_json(
            {
                "dataset": HF_DATASET,
                "revision": self.revision,
                "subsets": {s: conversion["subsets"][s]["jsonl_sha256"] for s in self.subsets},
            }
        )

    def rows(self, subset: str) -> list[dict[str, Any]]:
        self.verify()
        path = self.root / SUBSETS[subset][1]
        rows = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise DatasetError(f"{path}:{number}: not a JSON object")
            rows.append(row)
        return rows

    def iter_items(self) -> Iterator[Item]:
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for subset in self.subsets:
            for number, row in enumerate(self.rows(subset), start=1):
                where = f"{SUBSETS[subset][1]} row {number}"
                question = _text_field(row, "question", where, required=True)
                answer = _text_field(row, "answer", where, required=True)
                assert question is not None and answer is not None
                gid = group_id_of(question)
                if gid in merged:
                    known = merged[gid]
                    if known["answer"].strip() != answer.strip():
                        raise DatasetError(f"{where}: the same question has a different answer in another row")
                    if subset not in known["subsets"]:
                        known["subsets"].append(subset)
                    continue
                if subset != "seal_hard":
                    # A Seal-0 question missing from Seal-Hard contradicts the paper; keep it visible.
                    raise DatasetError(f"{where}: question not found in seal_hard (the paper says Seal-0 is a subset)")
                merged[gid] = {
                    "question": question,
                    "answer": answer,
                    "freshness": _text_field(row, "freshness", where, required=False),
                    "question_types": _list_field(row, "question_types", where),
                    "effective_year": None if row.get("effective_year") is None else str(row.get("effective_year")),
                    "search_results": _text_field(row, "search_results", where, required=False),
                    "topic": _text_field(row, "topic", where, required=False),
                    "annotator_urls": _list_field(row, "urls", where),
                    "subsets": [subset],
                }
                order.append(gid)
        for gid in order:
            data = merged[gid]
            in_seal_0 = "seal_0" in data["subsets"]
            yield Item(
                id=gid,
                group_id=gid,
                dataset=DATASET_NAME,
                question=data["question"],
                documents=(),
                gold={
                    "answer": data["answer"],
                    "answers": [data["answer"]],
                    "freshness": data["freshness"],
                    "question_types": data["question_types"],
                    "effective_year": data["effective_year"],
                    "search_results": data["search_results"],
                    "topic": data["topic"],
                    "annotator_urls": data["annotator_urls"],
                    "subsets": data["subsets"],
                    "dataset_revision": self.revision,
                },
                stratum=f"{'seal_0' if in_seal_0 else 'seal_hard'}/{data['freshness'] or 'unknown'}",
            )


# --------------------------------------------------------------------------- #
# Presentation to the model (answering.PRESENTATIONS["sealqa_live_web"])
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = (
    "You are a question-answering assistant. Answer the question using only the web pages "
    "provided below, which a web search returned for the question."
)
"""Written for this study (SealQA prescribes no prompt). It names no entity and gives no
hint about conflicting or misleading pages, so that the gate, not the prompt, is what differs
between arms."""

NO_DOCUMENTS = "No web page could be retrieved for this question."


def live_web_messages(question: str, documents: Sequence[Document], answer_instructions: str) -> tuple[Message, ...]:
    """System message, the pages as ``[Document n — source: webN]`` blocks with their URL, the
    question, and the structured-answer instruction (PREREGISTRATION.md section 6.2)."""
    if documents:
        retrieved = documents[0].metadata.get("retrieved_on")
        header = "Web pages returned by a search" + (f" on {retrieved}" if retrieved else "") + ":"
        blocks = "\n\n".join(
            f"[Document {n} — source: {d.id}]\nURL: {d.url or d.metadata.get('url', '')}\n{d.text}"
            for n, d in enumerate(documents, start=1)
        )
        context = f"{header}\n\n{blocks}"
    else:
        context = NO_DOCUMENTS
    user = f"{context}\n\nQuestion: {question}"
    if answer_instructions:
        user = f"{user}\n\n{answer_instructions}"
    return (Message("system", SYSTEM_PROMPT), Message("user", user))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m binding_gate.datasets.sealqa")
    parser.add_argument("command", choices=["prepare"])
    parser.add_argument("--root", required=True, help="target directory under data/raw/")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(prepare(args.root), indent=2, sort_keys=True))
    except (DatasetError, OSError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
