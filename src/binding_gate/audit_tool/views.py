"""What the auditor sees before the label (blinded view) and after it (reveal).

Before the label the auditor sees only: the question, the documents (labelled
"Document 1", "Document 2", ... in the order they were shown to the model; the
dataset's own source ids are hidden because in Deceptive Grounding they name the
role, ``cx_doc`` / ``cy_doc``), the candidate's value, its quote and which
document it cites. Hidden until the label is locked: the gate's verdict, its
reasons and check results, the decomposer's constraints, the item id and
condition, the model, the arm, the half and stratum of the sample, the sampling
weight, the scorers' labels and the original source ids.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_HEADER = re.compile(r"\[Document (\d+) — source: [^\]\n]*\]")


def blinded_view(case: Mapping[str, Any]) -> dict[str, Any]:
    documents = []
    cited = None
    for number, document in enumerate(case["documents"], start=1):
        label = f"Document {number}"
        documents.append({"label": label, "text": document["text"]})
        if document["id"] == case["candidate"]["source_id"] and cited is None:
            cited = label
    fallback = case.get("prompt_fallback")
    return {
        "question": case.get("question") or "(the question was not recorded in the trace)",
        "documents": documents,
        "prompt_fallback": None if fallback is None else _HEADER.sub(r"[Document \1]", fallback),
        "value": case["candidate"]["value"],
        "quote": case["candidate"]["quote"],
        "cited": cited or "a source id that matches none of the shown documents",
    }


def reveal_payload(case: Mapping[str, Any]) -> dict[str, Any]:
    verdict = case["verdict"]
    return {
        "decision": verdict["decision"],
        "reasons": list(verdict.get("reasons", [])),
        "checks": [
            {"check": r["check"], "status": r["status"]} for r in verdict.get("check_results", [])
        ],
        "constraints": constraints_summary(case.get("constraints")),
        "item_id": case["item_id"],
        "condition": case.get("condition"),
        "model": case["model"],
        "arm": case["arm"],
        "dataset": case["dataset"],
        "source_ids": [d["id"] for d in case["documents"]],
        "cited_source_id": case["candidate"]["source_id"],
    }


def constraints_summary(constraints: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if constraints is None:
        return None
    entity = constraints["entity"]
    return {
        "entity": entity["name"],
        "aliases": list(entity.get("aliases", [])),
        "attribute": constraints.get("attribute"),
        "qualifiers": [f"{q['text']} ({q['kind']})" for q in constraints.get("qualifiers", [])],
        "presuppositions": [
            f"{p['text']} [key terms: {', '.join(p.get('key_terms', [])) or 'none'}]"
            for p in constraints.get("presuppositions", [])
        ],
    }
