"""A tiny synthetic stand-in for the GRAB-RAG checkout and its misleading-context export.

The eval records have exactly the keys and nesting of the real
``data/<dataset>/eval.jsonl`` lines (NQ-shaped: one gold passage and several
answer aliases; HotpotQA-shaped: two gold passages and one answer), and the
export lines have the shape :data:`binding_gate.datasets.grab_rag.EXPORT_PROCEDURE`
describes (GRAB-RAG's ``build_condition`` output: one edited ``{title, text}``
passage among four ``{pid, title, text, score}`` hard negatives). Every name,
question and passage is invented here; no GRAB-RAG, NQ or HotpotQA text is
copied. The only GRAB-RAG text in this repository is the system prompt in
``binding_gate.datasets.grab_rag`` (attributed in NOTICE).

The fixture's "QC" edit is a plain whole-word replacement of the answer by an
invented name ``Zorbland<n>``; it imitates the shape of GRAB-RAG's swap, not its
selection logic (which needs GLiNER).
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path

from binding_gate.contracts import Split
from binding_gate.datasets.grab_rag import (
    DATASET_NAME,
    SYSTEM_PROMPT,
    data_manifest_sha256,
    file_sha256,
    group_id_of,
)
from binding_gate.split import assign_split

FAKE_COMMIT = "a" * 40
SKIP_EVERY = 4
"""Every 4th question (qi % 4 == 3) gets ``passages: null`` in the export, like a
question for which GRAB-RAG's swap failed."""


def nq_record(n: int) -> dict:
    return {
        "id": f"-{900000 + n}",
        "question": f"who founded the invented village of brimvale{n}",
        "answers": [f"Odran Vellis{n}", f"O. Vellis{n}"],
        "gold_passages": [
            {
                "title": f"Brimvale{n}",
                "text": (
                    f"Brimvale{n} is an invented village on a fictional coast . It was founded in "
                    f"{1700 + n} by Odran Vellis{n} , a made-up cartographer , and later grew into a "
                    "small market town with a stone harbour ."
                ),
            }
        ],
        "dataset": "natural_questions",
    }


def hotpot_record(n: int) -> dict:
    return {
        "id": hashlib.sha1(f"hotpot-{n}".encode()).hexdigest()[:24],
        "question": f"Which invented river flows past the fictional fortress of Kestrel Hold{n}?",
        "answers": [f"Amberlune{n}"],
        "gold_passages": [
            {
                "title": f"Kestrel Hold{n}",
                "text": (
                    f"Kestrel Hold{n} is a fictional fortress built on a cliff above the Amberlune{n}. "
                    "Its walls were raised by an invented guild of masons over three made-up decades."
                ),
            },
            {
                "title": f"Amberlune{n}",
                "text": (
                    f"The Amberlune{n} is an invented river that rises in imaginary hills and flows "
                    "west to a fictional sea through several made-up towns."
                ),
            },
        ],
        "dataset": "hotpotqa",
    }


MAKERS = {"natural_questions": nq_record, "hotpotqa": hotpot_record}


def pick_records(label: str, n_dev: int, n_test: int) -> list[dict]:
    """Synthetic records of which exactly ``n_dev`` are dev and ``n_test`` test, sorted by id."""
    chosen: list[dict] = []
    have = {Split.DEV: 0, Split.TEST: 0}
    want = {Split.DEV: n_dev, Split.TEST: n_test}
    index = 0
    while have != want:
        record = MAKERS[label](index)
        split = assign_split(DATASET_NAME, group_id_of(label, record["id"]))
        if have[split] < want[split]:
            chosen.append(record)
            have[split] += 1
        index += 1
    return sorted(chosen, key=lambda r: r["id"])


def planted_name(record: dict) -> str:
    return "Zorbland" + re.sub(r"\D", "", record["answers"][0])


def edit_passage(passage: dict, answers: list[str], planted: str) -> dict:
    """Replace every whole-word occurrence of every answer alias (title and text)."""
    def swap(text: str) -> str:
        for answer in answers:
            text = re.sub(r"\b" + re.escape(answer) + r"\b", planted, text, flags=re.IGNORECASE)
        return text

    return {"title": swap(passage["title"]), "text": swap(passage["text"])}


def qc_passages(record: dict, qi: int, records: list[dict], seed: int = 42) -> list[dict] | None:
    """Fixture version of ``build_condition(..., "QC", rng)`` for one record."""
    if qi % SKIP_EVERY == SKIP_EVERY - 1:
        return None
    answers = record["answers"]
    contra = edit_passage(record["gold_passages"][0], answers, planted_name(record))
    pool = []
    for other in records:
        if other["id"] == record["id"]:
            continue
        for j, passage in enumerate(other["gold_passages"]):
            if not any(a.lower() in (passage["title"] + " " + passage["text"]).lower() for a in answers):
                pool.append({"pid": f"{other['id']}_{j}", "title": passage["title"], "text": passage["text"], "score": 0.5})
    negatives = [pool[i % len(pool)] for i in range(4)]
    passages = [contra] + negatives
    random.Random(seed + qi).shuffle(passages)
    return passages


def export_lines(records_by_label: dict[str, list[dict]], seed: int = 42) -> list[dict]:
    lines = []
    for label, records in records_by_label.items():
        for qi, record in enumerate(records):
            lines.append(
                {
                    "dataset": label,
                    "question_id": record["id"],
                    "condition": "QC",
                    "seed": seed,
                    "passages": qc_passages(record, qi, records, seed),
                }
            )
    return lines


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def write_checkout(
    root: Path,
    *,
    n_dev: int = 3,
    n_test: int = 3,
    with_git: bool = True,
    with_policies: bool = True,
    export: bool = True,
    records_by_label: dict[str, list[dict]] | None = None,
) -> dict:
    """Create the checkout under ``root`` (and the export beside it); return matching dataset options."""
    if records_by_label is None:
        records_by_label = {label: pick_records(label, n_dev, n_test) for label in MAKERS}
    for label, records in records_by_label.items():
        write_jsonl(root / "data" / label / "eval.jsonl", records)
    if with_git:
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text(FAKE_COMMIT + "\n", encoding="utf-8")
    if with_policies:
        (root / "src").mkdir()
        (root / "src" / "policies.py").write_text(
            f"import os\nsys_base = {SYSTEM_PROMPT!r}\nraise SystemExit('never executed')\n", encoding="utf-8"
        )
    options: dict = {"root": root.as_posix(), "commit": FAKE_COMMIT, "data_sha256": data_manifest_sha256(root)}
    if export:
        contexts = root.parent / "contexts" / "qc_contexts.jsonl"
        write_jsonl(contexts, export_lines(records_by_label))
        options.update(contexts=contexts.as_posix(), contexts_sha256=file_sha256(contexts))
    else:
        options.update(conditions=["supportive"])
    return options


def rewrite_export(options: dict, rows: list[dict]) -> dict:
    """Overwrite the export with ``rows`` and return options pinned to the new file."""
    path = Path(options["contexts"])
    write_jsonl(path, rows)
    return {**options, "contexts_sha256": file_sha256(path)}


def read_export(options: dict) -> list[dict]:
    return [json.loads(line) for line in Path(options["contexts"]).read_text(encoding="utf-8").splitlines()]
