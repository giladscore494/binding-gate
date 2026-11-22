"""PR 18: fixed corpora of gate cases whose verdicts must stay byte-identical under the default options.

``tests/gate_identity_pr17.json`` holds, per group of cases, the SHA-256 of the
concatenated ``Verdict.to_json()`` of every case, computed with the gate code of
main at d8c33f8 (PRs 1-17, before the binding-scope options of PR 18 existed).
``tests/test_gate_scope_identity.py`` recomputes them with the current code.

Corpora (synthetic only; no benchmark text, nothing downloaded):

* ``dg/...``  - the synthetic Deceptive Grounding checkout of ``dg_fixture``
  (the inputs of the end-to-end test), every dev item, the scripted fake model
  (``target``, ``competitor``, ``paraphrase``) through the real presentation and
  parser, and the fake decomposer (``premise_terms`` C, X, XC, none);
* ``sec/...`` - the invented EDGAR fixture of ``sec_fixture`` (the layer-2
  fixtures), every item, every shown document, year columns 0-2, with
  constraints shaped as in ``test_sec_gate_compat`` (with and without the year
  alias).

Each corpus is evaluated under several sets of the PRE-EXISTING gate parameters
(:data:`PARAMETER_SETS`), so that non-default windows, scopes and tie rules are
covered too.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterator

import dg_fixture
import fakes
import gate_fakes
import sec_helpers
from binding_gate import registry
from binding_gate.answering import build_answer_request
from binding_gate.contracts import AnswerStatus, Entity, Qualifier, QualifierKind, QuestionConstraints
from binding_gate.datasets.sec_build import long_date
from binding_gate.datasets.sec_financials import SecFinancialsAdapter
from binding_gate.parsing import parse_answer
from binding_gate.split import select_dev

CHECKS = ["quote_verbatim", "entity_in_window", "qualifiers_in_window", "no_closer_competitor", "premise_present"]
BASE = dict(window_tokens=40, min_quote_tokens=5, lead_max_tokens=4, lead_min_repeats=2,
            premise_scope="document", competitor_tie="fail")
PARAMETER_SETS: dict[str, dict[str, Any]] = {
    "proposed": dict(BASE),
    "w10_pass": {**BASE, "window_tokens": 10, "competitor_tie": "pass"},
    "w80_premise_window": {**BASE, "window_tokens": 80, "premise_scope": "window"},
    "lead2_rep3": {**BASE, "lead_max_tokens": 2, "lead_min_repeats": 3, "window_tokens": 0},
}
GOLDEN = Path(__file__).with_name("gate_identity_pr17.json")


def dg_cases(root: Path) -> Iterator[tuple[str, Any, Any, Any]]:
    """``(case id, constraints, candidate, documents)`` for the synthetic DG checkout under ``root``."""
    options = dg_fixture.write_checkout(root)
    adapter = registry.create("dataset", "deceptive_grounding", **options)
    items = select_dev(adapter.iter_items())
    for behaviour in ("target", "competitor", "paraphrase"):
        model = fakes.ScriptedFake(behaviour=behaviour)
        for item in items:
            response = model.complete(build_answer_request(item.arm_input()))
            candidate = parse_answer(response.text)
            if candidate.status is not AnswerStatus.ANSWERED:
                continue
            for terms in ("C", "X", "XC", "none"):
                constraints = gate_fakes.FakeDecomposer(premise_terms=terms).decompose(item.question).constraints
                yield f"{behaviour}/{terms}/{item.id}", constraints, candidate, item.documents


def sec_constraints(item, *, with_alias: bool = True) -> QuestionConstraints:
    gold = item.gold
    date = gold["report_date"]
    aliases = (date[:4],) if with_alias else ()
    return QuestionConstraints(
        question=item.question,
        entity=Entity(name=gold["company"]["name"], aliases=tuple(gold["company"]["tickers"])),
        attribute=gold["metric"],
        qualifiers=(Qualifier(QualifierKind.TIME, f"fiscal year ended {long_date(date)}", aliases=aliases),),
        presuppositions=(),
    )


def sec_items(root: Path) -> list:
    options = sec_helpers.write_dataset(root)
    return list(SecFinancialsAdapter(**options).iter_items())


def sec_cases(root: Path) -> Iterator[tuple[str, Any, Any, Any]]:
    from binding_gate.contracts import AnswerCandidate

    for item in sec_items(root):
        content = "\n\n".join(f"[Document {n} — source: {d.id}]\n{d.text}" for n, d in enumerate(item.documents, 1))
        for index, _document in enumerate(item.documents):
            for column in (0, 1, 2):
                reply = sec_helpers.scripted_answer(content + "\n\nQuestion: " + item.question, document=index, column=column)
                if reply["status"] != "answered":
                    continue
                candidate = AnswerCandidate.answered(raw="{}", value=reply["value"], quote=reply["quote"], source_id=reply["source_id"])
                for with_alias in (True, False):
                    yield (f"{index}/{column}/{with_alias}/{item.id}", sec_constraints(item, with_alias=with_alias),
                           candidate, item.documents)


def digests(cases, extra: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """``group -> {"n", "sha256"}`` over every parameter set (``extra`` is added to each set)."""
    cases = list(cases)
    out: dict[str, dict[str, Any]] = {}
    for name, parameters in PARAMETER_SETS.items():
        gate = registry.create("gate", "binding", checks=CHECKS, **parameters, **(extra or {}))
        h = hashlib.sha256()
        for case_id, constraints, candidate, documents in cases:
            h.update(case_id.encode("utf-8") + b"\n")
            h.update(gate.evaluate(constraints, candidate, documents).to_json().encode("utf-8") + b"\n")
        out[name] = {"n": len(cases), "sha256": h.hexdigest()}
    return out


def corpora(tmp: Path) -> dict[str, list]:
    return {"dg": list(dg_cases(tmp / "dg")), "sec": list(sec_cases(tmp / "sec"))}


def all_digests(tmp: Path | dict[str, list], extra: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    cases = corpora(tmp) if isinstance(tmp, Path) else tmp
    return {f"{corpus}/{k}": v for corpus in ("dg", "sec") for k, v in digests(cases[corpus], extra).items()}


if __name__ == "__main__":  # regenerate the golden file (only ever with the pre-PR-18 gate)
    import json
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        result = all_digests(Path(tmp))
    GOLDEN.write_text(json.dumps({"source": sys.argv[1] if len(sys.argv) > 1 else "unknown", "groups": result},
                                 indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
