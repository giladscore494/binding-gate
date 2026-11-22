"""The label file shared by the browser interface and the terminal fallback.

Format (JSON)::

    {
      "format": "binding-gate-audit-labels",
      "version": 1,
      "sample_sha256": "<SHA-256 of the sample file the labels belong to>",
      "auditor": "<free text or null>",
      "labels": [
        {
          "case_id": "<from the sample>",
          "label": "yes" | "no" | "cannot_tell",
          "note": "<free text>",
          "decomposition": "not_assessed" | "constraints_correct" | "wrong_entity" | ...,
          "labelled_at": "<ISO time or null>",
          "revealed_at": "<ISO time or null>",
          "label_entered_before_reveal": true
        }
      ]
    }

``label`` answers the pre-registered question: is the quoted evidence about the
question's entity and qualifiers? It is entered while the gate's verdict is
hidden and is locked when the verdict is revealed; a label that was not entered
before the reveal is refused. ``decomposition`` is the auditor's comparison of
the decomposer's constraints (shown only after the reveal) with the question.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from binding_gate.audit_tool import prereg
from binding_gate.audit_tool.sampling import AuditError

LABELS_FORMAT = "binding-gate-audit-labels"
LABELS_VERSION = 1


def new_labels(sample_sha256: str, auditor: str | None = None) -> dict[str, Any]:
    return {
        "format": LABELS_FORMAT,
        "version": LABELS_VERSION,
        "sample_sha256": sample_sha256,
        "auditor": auditor,
        "labels": [],
    }


def validate_labels(labels: Mapping[str, Any], sample: Mapping[str, Any], sample_sha256: str, *, where: str = "labels") -> dict[str, Any]:
    """Check a label file against its sample; return it with entries keyed by case id order."""
    if labels.get("format") != LABELS_FORMAT or labels.get("version") != LABELS_VERSION:
        raise AuditError(f"{where}: not an audit label file of version {LABELS_VERSION}")
    if labels.get("sample_sha256") != sample_sha256:
        raise AuditError(
            f"{where}: the labels belong to sample {labels.get('sample_sha256')!r}, not to this sample ({sample_sha256})"
        )
    known = {case["case_id"] for case in sample["cases"]}
    seen: set[str] = set()
    for entry in labels.get("labels", []):
        case_id = entry.get("case_id")
        if case_id not in known:
            raise AuditError(f"{where}: case {case_id!r} is not in the sample")
        if case_id in seen:
            raise AuditError(f"{where}: case {case_id!r} is labelled twice")
        seen.add(case_id)
        if entry.get("label") not in prereg.LABELS:
            raise AuditError(f"{where}: case {case_id}: label {entry.get('label')!r} is not one of {list(prereg.LABELS)}")
        if entry.get("decomposition", "not_assessed") not in prereg.DECOMPOSITION_CATEGORIES:
            raise AuditError(f"{where}: case {case_id}: unknown decomposition category {entry.get('decomposition')!r}")
        if entry.get("label_entered_before_reveal") is not True:
            raise AuditError(f"{where}: case {case_id}: the label was not entered before the verdict was revealed")
        if not isinstance(entry.get("note", ""), str):
            raise AuditError(f"{where}: case {case_id}: note must be text")
    return dict(labels)


def load_labels(path: str | Path, sample: Mapping[str, Any], sample_sha256: str) -> dict[str, Any]:
    try:
        labels = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise AuditError(f"{path}: not a JSON file: {exc}") from None
    return validate_labels(labels, sample, sample_sha256, where=str(path))


def save_labels(labels: Mapping[str, Any], path: str | Path) -> None:
    """Write atomically (a crash never leaves a half-written label file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(labels, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)
