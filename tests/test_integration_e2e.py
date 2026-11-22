"""End to end on synthetic data: runner -> statistics -> public report -> human audit -> report.

One throwaway git repository, the synthetic Deceptive Grounding checkout of
``dg_fixture``, the scripted fake models (``target`` and ``competitor``) and the
fake decomposer. Arms: ``no_gate``, ``gated`` and one ablation. Nothing is
downloaded, no model is called, and the real test split and access log are
never touched (checked at the end).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import audit_helpers as ah
import fakes
import gate_fakes
import html_check
from binding_gate import runner
from binding_gate.audit_tool import prereg as audit_prereg
from binding_gate.audit_tool.analysis import analyse_audit
from binding_gate.audit_tool.html import render_labelling_page
from binding_gate.audit_tool.sampling import ACCEPTED, BLOCKED, draw_sample, load_runs, load_sample, write_sample
from binding_gate.audit_tool.terminal import label_interactively
from binding_gate.contracts import AnswerStatus, Decision
from binding_gate.plots import figures as plot_figures
from binding_gate.report.build import load_inputs, write_report
from binding_gate.stats.analysis import analyse_run_dirs, write_outputs
from binding_gate.trace import read_manifest, read_traces

REPO_ROOT = Path(__file__).resolve().parents[1]
N_DEV_ITEMS = 3 * 15  # 3 synthetic dev (X, C) pairs x 15 conditions
MODELS, REPEATS = 2, 2
ABLATION = "gated_minus_no_closer_competitor"


class ScriptedAuditor:
    """Answers the terminal prompts from the blinded view only: "yes" if the cited
    document names the question's drug, otherwise "no"."""

    def __init__(self) -> None:
        self.said: list[str] = []

    def say(self, line: str) -> None:
        self.said.append(line)

    def ask(self, prompt: str) -> str:
        if not prompt.startswith("Is the quoted evidence"):
            return ""  # no note; decomposition "not assessed"
        case = self.said[max(i for i, line in enumerate(self.said) if line.startswith("Case ")):]
        text = " ".join(case)
        assert "Gate verdict" not in text  # blind until the label is entered
        drug = re.search(r"using (\S+) to treat", text).group(1)
        cited = re.search(r"cited from (Document \d+)", text).group(1)
        document = case[case.index(f"--- {cited} ---") + 1]
        return "y" if drug.lower() in document.lower() else "n"


def test_runner_statistics_report_and_audit_end_to_end(tmp_path):
    gate_fakes.DECOMPOSE_CALLS.clear()
    arms = [{"name": "no_gate"}, ah.gated_arm(), ah.ablation_arm("no_closer_competitor")]
    _repo, summary = ah.gated_run(tmp_path, arms=arms, repeats=REPEATS)
    assert summary.closed and summary.error_records == 0
    records = list(read_traces(summary.run_dir))
    assert len(records) == N_DEV_ITEMS * 3 * MODELS * REPEATS

    # -- the no_gate candidate is reused: zero extra answer calls --------------------------
    assert len(fakes.CALLS) == N_DEV_ITEMS * MODELS * REPEATS
    for record in records:
        answer_calls = [c for c in record.model_calls if c.purpose == "answer"]
        assert len(answer_calls) == 1
        assert runner.is_cache_hit(answer_calls[0]) == (record.arm != "no_gate")
    assert sorted(Counter(gate_fakes.DECOMPOSE_CALLS).values()) == [1, 1, 1]  # one per question

    # -- the gate blocks every competitor-quoted answer of the "competitor" model --------------
    reference = {(r.item_id, r.model, r.repeat): r for r in records if r.arm == "no_gate"}
    for record in records:
        if record.arm == "no_gate" or record.model != "bad":
            continue
        (attempt,) = record.attempts
        assert attempt.answer.source_id == "cy_doc" and attempt.answer == reference[(record.item_id, record.model, record.repeat)].final_answer
        assert reference[(record.item_id, record.model, record.repeat)].scores[0].label == "competitor_only"
        assert attempt.verdict.decision in (Decision.ABSTAIN, Decision.RESEARCH)
        assert record.final_answer.status is AnswerStatus.ABSTAINED
    good_gated = [r for r in records if r.arm == "gated" and r.model == "good" and r.attempts[0].verdict]
    assert good_gated and all(r.attempts[0].verdict.decision is Decision.ACCEPT for r in good_gated)

    # -- statistics ---------------------------------------------------------------------------------
    analysis = analyse_run_dirs([summary.run_dir], resamples=200)
    assert analysis["split"] == "dev" and set(analysis["models"]) == {"good", "bad"}
    assert set(analysis["models"]["bad"]["arms"]) == {"no_gate", "gated", ABLATION}
    assert analysis["models"]["bad"]["estimates"]["arm/gated/wrong_all"]["value"] == 0.0
    assert analysis["models"]["bad"]["estimates"]["arm/no_gate/wrong_all"]["value"] == 1.0
    assert all(not h["confirmatory"] for m in analysis["models"].values() for h in m["hypotheses"].values())
    analysis_dir = tmp_path / "analysis"
    write_outputs(analysis, analysis_dir, figures=plot_figures.available())

    # -- report (before the audit) -----------------------------------------------------------------
    prereg = REPO_ROOT / "PREREGISTRATION.md"
    written = write_report(load_inputs([analysis_dir], prereg_path=prereg, run_dirs=[summary.run_dir]), tmp_path / "report")
    md = Path(written["markdown"]).read_text(encoding="utf-8")
    assert "| analysis | model | H1 | H2 | H3 | H4 | controlled-layer claim |" in md
    assert "No result in this report is confirmatory" in md and "(not confirmatory)" in md
    assert "split is 'dev', not 'test' (exploratory only)" in md
    assert f"| `{ABLATION}` |" in md
    assert "The human audit (section 13) has not been carried out" in md
    html_check.check(Path(written["html"]).read_text(encoding="utf-8"))

    # -- audit: sample, labelling page, scripted labelling, analysis ---------------------------------
    sample = draw_sample(load_runs([summary.run_dir]))
    assert sample["composition"] == {BLOCKED: {"all": audit_prereg.AUDIT_BLOCKED}, ACCEPTED: {"all": audit_prereg.AUDIT_ACCEPTED}}
    assert sample["population"] == {BLOCKED: {"all": N_DEV_ITEMS * REPEATS}, ACCEPTED: {"all": 3 * 10 * REPEATS}}
    assert sample["split"] == "dev" and sample["parameters"]["seed"] == audit_prereg.AUDIT_SEED
    assert sample["runs"][0]["traces_sha256"] == read_manifest(summary.run_dir).traces_sha256
    assert {c["arm"] for c in sample["cases"]} == {"gated"}  # the ablation is not audited
    sample_path = tmp_path / "audit" / "sample.json"
    digest = write_sample(sample, sample_path)
    page = render_labelling_page(*load_sample(sample_path))
    html_check.check(page)
    assert all(reason not in page for case in sample["cases"] for reason in case["verdict"]["reasons"])

    auditor = ScriptedAuditor()
    labels = label_interactively(sample, digest, tmp_path / "audit" / "labels.json", auditor="scripted",
                                 ask=auditor.ask, say=auditor.say)
    assert len(labels["labels"]) == 100
    audit = analyse_audit(sample, digest, [labels])
    assert audit["per_half"][ACCEPTED]["agreement"]["value"] == 1.0
    assert audit["per_half"][BLOCKED]["agreement"]["value"] == 1.0
    assert audit["disagreements"] == [] and audit["single_auditor"] is True
    audit_path = tmp_path / "audit" / "audit.json"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    # -- report with the audit ----------------------------------------------------------------------
    written = write_report(
        load_inputs([analysis_dir], prereg_path=prereg, run_dirs=[summary.run_dir], audit_path=audit_path),
        tmp_path / "report-final",
    )
    md = Path(written["markdown"]).read_text(encoding="utf-8")
    assert "Agreement between gate and auditor" in md and "100.0% (50/50)" in md
    assert "No disagreements between the gate and the auditor." in md
    assert "This audit used `dev` traces" in md
    html_check.check(Path(written["html"]).read_text(encoding="utf-8"))

    # -- the real repository was not touched ------------------------------------------------------------
    assert (REPO_ROOT / "audit" / "test_access_log.jsonl").read_bytes() == b""
