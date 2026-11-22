"""A synthetic SealQA checkout in the documented shape (invented questions; no SealQA data).

The real parquet files cannot be part of the tests, so the fixture writes stand-in
"parquet" bytes, pins THEIR hashes in place of the real ones (``monkeypatch`` on
``FILE_MANIFEST``) and lets :func:`binding_gate.datasets.sealqa.prepare` convert
them with a fake reader that returns the rows. Everything after the reader -
hash checks, JSON Lines, ``conversion.json``, the adapter - is the real code.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from binding_gate.datasets import sealqa

FRESHNESS = ("never", "slow", "fast")


def row(i: int, *, question: str | None = None, answer: str | None = None) -> dict[str, Any]:
    return {
        "question": question or f"Which invented town number {i} is twinned with the invented city Q{i}?",
        "answer": answer or f"Town {i}",
        "urls": [f"https://annotator.example/{i}"],
        "freshness": FRESHNESS[i % 3],
        "question_types": ["Q2"],
        "effective_year": "2024",
        "search_results": "conflict" if i % 2 else "unhelpful",
        "topic": "Geography",
        "canary": "canary-string",
    }


def default_rows(n_hard: int = 30, n_zero: int = 12) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    hard = [row(i) for i in range(n_hard)]
    return hard, [dict(r) for r in hard[:n_zero]]


DOWNLOADS: list[str] = []


def write(root: Path, monkeypatch, hard: list[dict[str, Any]] | None = None, zero: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Prepare a synthetic checkout under ``root``; returns the dataset options for a config.

    The URLs requested from the fake downloader are appended to :data:`DOWNLOADS`."""
    if hard is None or zero is None:
        hard, zero = default_rows()
    contents = {
        "LICENSE": b"Apache License 2.0 (fixture)\n",
        "README.md": b"# SealQA fixture\n",
        "seal-hard.parquet": b"PAR1 fixture seal-hard " + str(len(hard)).encode(),
        "seal-0.parquet": b"PAR1 fixture seal-0 " + str(len(zero)).encode(),
    }
    manifest = {name: (len(data), hashlib.sha256(data).hexdigest()) for name, data in contents.items()}
    monkeypatch.setattr(sealqa, "FILE_MANIFEST", manifest)
    rows_by_file = {"seal-hard.parquet": hard, "seal-0.parquet": zero}
    def download(url: str, destination: Path) -> None:
        DOWNLOADS.append(url)
        destination.write_bytes(contents[url.rsplit("/", 1)[1]])

    def read_parquet(path: Path):
        return [dict(r) for r in rows_by_file[path.name]], "fixture-1.0"

    sealqa.prepare(root, download=download, read_parquet=read_parquet)
    return {"root": str(root), "revision": sealqa.PINNED_REVISION, "subsets": ["seal_hard", "seal_0"]}
