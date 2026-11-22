"""PR 18 dev tool: what each binding-scope setting does with SCRIPTED FAKE models. No model is called.

Usage (from the repository root, with the package installed)::

    python tests/scope_effect.py [--dg-root data/raw/deceptive-grounding-benchmark] [--json FILE]

* **Layer 1** - the Deceptive Grounding benchmark at its pinned commit (dataset options of
  ``configs/dev_gated.toml``; the checkout is untrusted data and only read), DEV items only
  (``split.select_dev``; the test split is never loaded). Answers come from the scripted fake model of
  ``tests/fakes.py`` through the real presentation and parser: ``target`` quotes the first eight words of the
  X document (a correct, target-grounded answer), ``competitor`` those of the competing document (a wrong-entity
  answer). Constraints come from the fake decomposer of ``tests/gate_fakes.py`` (entity X, qualifier C, key term
  C, taken verbatim from the question template).
* **Layer 2** - the invented SEC EDGAR fixture of ``tests/sec_fixture.py`` (no real filing exists before the
  first generation), every item. ``sec_helpers.scripted_answer`` quotes the metric's row from the target filing
  (current-year column: correct; prior-year column: a wrong-period answer) or from the peer's filing (wrong
  entity). Constraints are shaped as a decomposer is expected to produce them (entity = registrant, tickers as
  aliases, one time qualifier "fiscal year ended <date>" with the year as alias; ``test_sec_gate_compat``).

For each candidate setting (``gates.binding.BINDING_SCOPES``) the gate runs with the checks and parameters of
the gated arm of ``configs/dev_gated.toml`` (layer 1) or ``configs/dev_sec_gated.toml`` (layer 2) and the
setting's ``binding_scope``; the table gives the share of answers the gate does not ACCEPT ("blocked").

THESE NUMBERS ARE INDICATIVE ONLY. Fake models quote in one fixed way (the document's first words, or a whole
table row) and the fake decompositions are derived from the question template or the gold record; real models
and the real decomposer behave differently. They are not evidence for any hypothesis and are not part of the
pre-registration; the frozen setting is chosen only by the rule of PREREGISTRATION.md section 5.1 from real dev
traces (``python -m binding_gate.gates.calibration --select-scope``).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from binding_gate import registry  # noqa: E402

for _kind in registry.KIND_PACKAGES:
    registry.discover(_kind)

import fakes  # noqa: E402
import gate_fakes  # noqa: E402
import gate_identity  # noqa: E402
import sec_helpers  # noqa: E402
from binding_gate.answering import build_answer_request  # noqa: E402
from binding_gate.config import load_config  # noqa: E402
from binding_gate.contracts import AnswerCandidate, AnswerStatus, Decision  # noqa: E402
from binding_gate.gates.binding import BINDING_SCOPES  # noqa: E402
from binding_gate.parsing import parse_answer  # noqa: E402
from binding_gate.split import select_dev  # noqa: E402

REPO_ROOT = HERE.parent
NOTE = "indicative only: scripted fake models and fake decompositions, not evidence"


def gated_arm(config_name: str) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    config = load_config(REPO_ROOT / "configs" / config_name)
    arm = next(a for a in config.arms if a.label == "gated")
    return list(arm.options["checks"]), dict(arm.options["parameters"]), dict(config.dataset.options)


def tally(cases: Iterable[tuple[str, Any, Any, Any]], checks: list[str], parameters: dict[str, Any]) -> dict[str, Any]:
    """``group -> {n, blocked, by_setting}`` over every candidate setting."""
    cases = list(cases)
    out: dict[str, Any] = {}
    for setting in BINDING_SCOPES:
        gate = registry.create("gate", "binding", checks=checks, **parameters, binding_scope=setting)
        for group, constraints, candidate, documents in cases:
            row = out.setdefault(group, {"n": 0, "settings": {}})
            entry = row["settings"].setdefault(setting, {"blocked": 0, "failed_checks": Counter()})
            verdict = gate.evaluate(constraints, candidate, documents)
            if setting == next(iter(BINDING_SCOPES)):
                row["n"] += 1
            if verdict.decision is not Decision.ACCEPT:
                entry["blocked"] += 1
                for result in verdict.check_results:
                    if result.failed:
                        entry["failed_checks"][result.check] += 1
    for row in out.values():
        for entry in row["settings"].values():
            entry["share"] = entry["blocked"] / row["n"] if row["n"] else None
            entry["failed_checks"] = dict(sorted(entry["failed_checks"].items()))
    return out


def dg_cases(dataset_options: dict[str, Any]) -> Iterable[tuple[str, Any, Any, Any]]:
    adapter = registry.create("dataset", "deceptive_grounding", **dataset_options)
    items = select_dev(adapter.iter_items())
    decomposer = gate_fakes.FakeDecomposer(premise_terms="C")
    for behaviour, group in (("target", "target-quoted (correct)"), ("competitor", "competitor-quoted (wrong entity)")):
        model = fakes.ScriptedFake(behaviour=behaviour)
        for item in items:
            candidate = parse_answer(model.complete(build_answer_request(item.arm_input())).text)
            if candidate.status is AnswerStatus.ANSWERED:
                yield group, decomposer.decompose(item.question).constraints, candidate, item.documents


def sec_cases(root: Path) -> Iterable[tuple[str, Any, Any, Any]]:
    for item in gate_identity.sec_items(root):
        content = "\n\n".join(f"[Document {n} — source: {d.id}]\n{d.text}" for n, d in enumerate(item.documents, 1))
        roles = item.gold["document_roles"]
        for index, document in enumerate(item.documents):
            role = roles[document.id]
            columns = ((0, "target-quoted, current year (correct)"), (1, "target-quoted, prior year (wrong period)")) \
                if role == "target" else ((0, "competitor-quoted (wrong entity)"),)
            for column, group in columns:
                reply = sec_helpers.scripted_answer(content + "\n\nQuestion: " + item.question, document=index, column=column)
                if reply["status"] != "answered":
                    continue
                candidate = AnswerCandidate.answered(raw="{}", value=reply["value"], quote=reply["quote"], source_id=reply["source_id"])
                yield group, gate_identity.sec_constraints(item), candidate, item.documents


def measure(dg_options: dict[str, Any] | None, sec_root: Path) -> dict[str, Any]:
    """Both tables; ``dg_options`` are the dataset options of the benchmark checkout (``None``: skip layer 1)."""
    result: dict[str, Any] = {"note": NOTE, "settings": list(BINDING_SCOPES)}
    if dg_options is not None:
        checks, parameters, _options = gated_arm("dev_gated.toml")
        result["layer1_deceptive_grounding_dev"] = tally(dg_cases(dg_options), checks, parameters)
    checks, parameters, _options = gated_arm("dev_sec_gated.toml")
    result["layer2_sec_fixture"] = tally(sec_cases(sec_root), checks, parameters)
    return result


def markdown(result: dict[str, Any]) -> str:
    settings = result["settings"]
    lines = [f"Share of answers blocked (not ACCEPT) per binding_scope - {result['note']}", ""]
    for key, title in (("layer1_deceptive_grounding_dev", "Layer 1, Deceptive Grounding DEV split"),
                       ("layer2_sec_fixture", "Layer 2, invented SEC fixture")):
        if key not in result:
            continue
        lines += [f"### {title}", "", "| answers | n | " + " | ".join(settings) + " |",
                  "|---|---:|" + "---:|" * len(settings)]
        for group, row in result[key].items():
            cells = [f"{100 * row['settings'][s]['share']:.1f}%" for s in settings]
            lines.append(f"| {group} | {row['n']} | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dg-root", default="data/raw/deceptive-grounding-benchmark",
                        help="checkout of the benchmark at its pinned commit; 'none' to skip layer 1")
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)
    dg_options = None
    if args.dg_root != "none":
        dg_options = {**gated_arm("dev_gated.toml")[2], "root": args.dg_root}  # the pinned commit and data hash
    with tempfile.TemporaryDirectory() as tmp:
        result = measure(dg_options, Path(tmp) / "sec")
    print(markdown(result))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
