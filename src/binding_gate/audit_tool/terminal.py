"""Terminal fallback for labelling: one case at a time, saved after every case.

``python -m binding_gate.audit_tool label --sample SAMPLE.json --out LABELS.json [--auditor NAME]``

For each unlabelled case (in the sample's display order) it prints the blinded
view, asks for the label (``y`` yes, ``n`` no, ``c`` cannot tell; ``s`` skips
the case, ``q`` quits), asks for an optional note, then reveals the verdict and
the decomposer's constraints and asks for the decomposition category. The label
cannot be changed after the reveal. The label file is rewritten atomically after
every case, so the session can be interrupted and resumed with the same command.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Callable, Mapping

from binding_gate.audit_tool import prereg
from binding_gate.audit_tool.labels import load_labels, new_labels, save_labels
from binding_gate.audit_tool.views import blinded_view, reveal_payload
from binding_gate.canonical import utc_now_iso

_KEYS = {"y": "yes", "n": "no", "c": "cannot_tell"}


def _wrap(text: str) -> str:
    return "\n".join(textwrap.fill(p, width=96) if p else "" for p in str(text).split("\n"))


def label_interactively(
    sample: Mapping[str, Any],
    sample_sha256: str,
    out: str | Path,
    *,
    auditor: str | None = None,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Run the session; return the label file as written."""
    out = Path(out)
    labels = load_labels(out, sample, sample_sha256) if out.exists() else new_labels(sample_sha256, auditor)
    if auditor and not labels.get("auditor"):
        labels["auditor"] = auditor
    done = {entry["case_id"] for entry in labels["labels"]}
    cases = sorted(sample["cases"], key=lambda c: c["display_number"])
    say(f"{len(done)} of {len(cases)} cases already labelled in {out}.")
    for case in cases:
        if case["case_id"] in done:
            continue
        view = blinded_view(case)
        say("=" * 96)
        say(f"Case {case['display_number']} of {len(cases)} ({case['case_id']})")
        say(_wrap(f"QUESTION: {view['question']}"))
        for document in view["documents"]:
            say(f"--- {document['label']} ---")
            say(_wrap(document["text"]) if document["text"] is not None else "(text not recoverable; full prompt follows)")
        if view["prompt_fallback"] is not None:
            say("--- full prompt shown to the model ---")
            say(_wrap(view["prompt_fallback"]))
        say(_wrap(f"CANDIDATE ANSWER: {view['value']}"))
        say(_wrap(f"QUOTE (cited from {view['cited']}): {view['quote']}"))
        answer = ""
        while answer not in (*_KEYS, "s", "q"):
            answer = ask(
                "Is the quoted evidence about the question's entity and qualifiers? "
                "[y]es / [n]o / [c]annot tell / [s]kip / [q]uit: "
            ).strip().lower()
        if answer == "q":
            break
        if answer == "s":
            continue
        labelled_at = utc_now_iso()
        note = ask("Note (optional, Enter to skip): ").strip()
        payload = reveal_payload(case)
        say("--- REVEAL (your label is now locked) ---")
        say(f"Gate verdict: {payload['decision'].upper()}")
        say(_wrap("Reasons: " + ("; ".join(payload["reasons"]) or "(none)")))
        say(_wrap("Checks: " + "; ".join(f"{c['check']}: {c['status']}" for c in payload["checks"])))
        constraints = payload["constraints"]
        if constraints:
            say(_wrap(f"Decomposer: entity {constraints['entity']!r}, aliases {constraints['aliases']}, "
                      f"qualifiers {constraints['qualifiers']}, presuppositions {constraints['presuppositions']}"))
        say(f"Item {payload['item_id']} / {payload['condition']}; model {payload['model']}; arm {payload['arm']}")
        categories = list(prereg.DECOMPOSITION_CATEGORIES)
        menu = ", ".join(f"{k}={c}" for k, c in enumerate(categories))
        choice = None
        while choice is None:
            raw = ask(f"Are the decomposer's constraints right? {menu} (Enter = 0): ").strip()
            if raw == "":
                choice = categories[0]
            elif raw.isdigit() and int(raw) < len(categories):
                choice = categories[int(raw)]
        labels["labels"].append(
            {
                "case_id": case["case_id"],
                "label": _KEYS[answer],
                "note": note,
                "decomposition": choice,
                "labelled_at": labelled_at,
                "revealed_at": utc_now_iso(),
                "label_entered_before_reveal": True,
            }
        )
        save_labels(labels, out)
    save_labels(labels, out)
    say(f"Saved {len(labels['labels'])} label(s) to {out}.")
    return labels
