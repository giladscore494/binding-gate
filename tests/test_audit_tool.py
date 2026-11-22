"""The human-audit tool (PREREGISTRATION.md section 13): sampling, blind labelling, analysis.

Everything runs on synthetic traces from the scripted fake models in temporary
directories; nothing touches the real test split or calls a model.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

import audit_helpers as ah
import html_check
from binding_gate.audit_tool import prereg
from binding_gate.audit_tool.__main__ import main as audit_main
from binding_gate.audit_tool.analysis import analyse_audit, clopper_pearson
from binding_gate.audit_tool.html import render_labelling_page
from binding_gate.audit_tool.labels import load_labels, new_labels, save_labels, validate_labels
from binding_gate.audit_tool.sampling import (
    ACCEPTED,
    BLOCKED,
    AuditError,
    draw_sample,
    load_runs,
    load_sample,
    recover_documents,
    write_sample,
)
from binding_gate.audit_tool.terminal import label_interactively
from binding_gate.audit_tool.views import blinded_view, reveal_payload
from binding_gate.contracts import TraceRecord
from binding_gate.trace import read_manifest


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("audit-run")
    arms = [{"name": "no_gate"}, ah.gated_arm(), ah.ablation_arm("premise_present")]
    _repo, summary = ah.gated_run(tmp, arms=arms)
    assert summary.closed
    return summary.run_dir


@pytest.fixture(scope="module")
def sample(run_dir) -> dict:
    return draw_sample(load_runs([run_dir]))


# -- sampling --------------------------------------------------------------------- #


def test_sample_has_the_preregistered_composition_and_records_its_inputs(run_dir, sample):
    # 3 dev triples x 15 conditions x 2 repeats; "good" quotes the target (10 conditions have one),
    # "bad" always quotes the competitor. Only the full gate counts, not the ablation.
    assert sample["population"] == {BLOCKED: {"all": 90}, ACCEPTED: {"all": 60}}
    assert sample["composition"] == {BLOCKED: {"all": 50}, ACCEPTED: {"all": 50}}
    assert sample["shortfall"] == [] and sample["judge_run"] is False and sample["as_preregistered"] is True
    assert sample["split"] == "dev" and sample["datasets"] == ["deceptive_grounding"]
    manifest = read_manifest(run_dir)
    (run,) = sample["runs"]
    assert (run["run_id"], run["traces_sha256"], run["config_hash"]) == (
        manifest.run_id, manifest.traces_sha256, manifest.config_hash)
    assert sample["parameters"]["seed"] == prereg.AUDIT_SEED
    cases = sample["cases"]
    assert [c["display_number"] for c in cases] == list(range(1, 101))
    assert {c["arm"] for c in cases} == {"gated"}
    assert len({c["case_id"] for c in cases}) == 100
    for case in cases:
        decision = case["verdict"]["decision"]
        assert (case["half"] == ACCEPTED) == (decision == "accept")
        assert case["weight"]["exact"] == ("60/50" if case["half"] == ACCEPTED else "90/50")
        assert all(d["recovered"] for d in case["documents"]) and case["prompt_fallback"] is None
        assert case["question"].startswith("What does the literature say about using ")
        expected = "target_grounded" if case["half"] == ACCEPTED else "wrong_entity"
        assert case["candidate_provenance"] == expected
        assert case["judge_label"] is None and case["scorer_disagreement"] is None
    # blocked and accepted cases are interleaved in the display order
    assert {c["half"] for c in cases[:20]} == {BLOCKED, ACCEPTED}


def test_sample_is_deterministic_and_does_not_depend_on_record_order(run_dir, sample):
    again = draw_sample(load_runs([run_dir]))
    assert again == sample
    runs = load_runs([run_dir])
    runs[0].records.reverse()
    shuffled = draw_sample(runs)
    assert [c["case_id"] for c in shuffled["cases"]] == [c["case_id"] for c in sample["cases"]]


def test_scorer_disagreements_are_oversampled_with_weights(run_dir, tmp_path):
    copy = tmp_path / "run"
    shutil.copytree(run_dir, copy)

    def label(record):
        if record.arm != "no_gate":
            return None
        if record.model == "bad":  # ours: wrong_entity in every record
            return "correct_attribution" if record.repeat == 0 else "eaf_or_substitution"
        if record.final_answer.status.value == "answered" and record.item_id.endswith("complete__null_control") and record.repeat == 0:
            return "judge_error"  # a judge failure never agrees
        return "correct_attribution" if record.final_answer.status.value == "answered" else "abstained"

    ah.write_judge_scores(copy, label)
    sample = draw_sample(load_runs([copy]))
    assert sample["judge_run"] is True
    assert sample["population"][BLOCKED] == {"scorer_disagreement": 45, "other": 45}
    n_judge_errors = sample["population"][ACCEPTED]["scorer_disagreement"]
    assert n_judge_errors == 3  # one complete__null_control item per dev triple, repeat 0
    assert sample["population"][ACCEPTED]["other"] == 60 - n_judge_errors
    assert sample["composition"][BLOCKED] == {"scorer_disagreement": 25, "other": 25}
    assert sample["composition"][ACCEPTED] == {"scorer_disagreement": 3, "other": 47}
    weights = {(c["half"], c["stratum"]): c["weight"]["exact"] for c in sample["cases"]}
    assert weights[(BLOCKED, "scorer_disagreement")] == "45/25"
    assert weights[(ACCEPTED, "scorer_disagreement")] == "3/3"
    assert weights[(ACCEPTED, "other")] == "57/47"
    for case in sample["cases"]:
        assert case["scorer_disagreement"] == (case["stratum"] == "scorer_disagreement")


def test_shortfall_takes_the_whole_half_and_says_so(run_dir):
    sample = draw_sample(load_runs([run_dir]), n_blocked=200)
    assert sample["composition"][BLOCKED] == {"all": 90}
    assert sample["shortfall"] and "only 90" in sample["shortfall"][0]
    assert sample["as_preregistered"] is False
    assert {c["weight"]["exact"] for c in sample["cases"] if c["half"] == BLOCKED} == {"90/90"}


def test_mixed_splits_and_empty_populations_are_refused(run_dir):
    runs = load_runs([run_dir])
    runs[0].records.append(TraceRecord.from_dict({**runs[0].records[0].to_dict(), "split": "test", "item_id": "other"}))
    with pytest.raises(AuditError, match="mix the dev and the test split"):
        draw_sample(runs)
    with pytest.raises(AuditError, match="no gate decisions"):
        draw_sample(load_runs([run_dir]), arms=("no_such_arm",))


def test_changed_trace_file_is_refused(run_dir, tmp_path):
    copy = tmp_path / "run"
    shutil.copytree(run_dir, copy)
    with (copy / "traces.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("\n")
    with pytest.raises(AuditError, match="SHA-256 differs"):
        load_runs([copy])


def test_documents_are_recovered_by_hash_and_fall_back_to_the_masked_prompt(run_dir):
    record = next(r for r in load_runs([run_dir])[0].records if r.arm == "gated" and r.attempts[0].verdict)
    documents, fallback = recover_documents(record)
    assert fallback is None and all(d["recovered"] for d in documents)
    data = record.to_dict()
    data["attempts"][0]["evidence"][0]["sha256"] = "0" * 64
    broken = TraceRecord.from_dict(data)
    documents, fallback = recover_documents(broken)
    assert documents[0]["recovered"] is False and documents[0]["text"] is None and fallback
    case = {"documents": documents, "prompt_fallback": fallback, "candidate": {"source_id": "cy_doc", "value": "v", "quote": "q"}}
    view = blinded_view(case)
    assert "cx_doc" not in view["prompt_fallback"] and "cy_doc" not in view["prompt_fallback"]
    assert "[Document 1]" in view["prompt_fallback"]


# -- the labelling page ---------------------------------------------------------------- #


def test_labelling_page_is_well_formed_offline_and_hides_the_verdict(sample, tmp_path):
    digest = write_sample(sample, tmp_path / "sample.json")
    page = render_labelling_page(sample, digest)
    checker = html_check.check(page)
    assert html_check.external_references(page) == []
    assert "http://" not in page and "https://" not in page
    # nothing that reveals the verdict, the role of a document or the item is in the page as text
    for case in sample["cases"]:
        for reason in case["verdict"]["reasons"]:
            assert reason not in page
        assert case["item_id"] not in page
    assert '"decision"' not in page and "cx_doc" not in page and "cy_doc" not in page
    assert "entity_in_window" not in page and "no_closer_competitor" not in page
    # every case: three label options, a disabled lock button, a hidden reveal box that decodes to the verdict
    sections = [attrs for tag, attrs in checker.tags if tag == "section"]
    assert len(sections) == 100
    radios = [attrs for tag, attrs in checker.tags if tag == "input" and attrs.get("type") == "radio"]
    assert len(radios) == 300 and {r["value"] for r in radios} == set(prereg.LABELS)
    buttons = [attrs for tag, attrs in checker.tags if tag == "button" and "lock" in (attrs.get("class") or "")]
    assert len(buttons) == 100 and all(b.get("disabled") == "disabled" for b in buttons)
    reveals = [attrs for tag, attrs in checker.tags if tag == "div" and attrs.get("class") == "reveal"]
    assert len(reveals) == 100 and all(r.get("hidden") == "hidden" for r in reveals)
    by_id = {c["case_id"]: c for c in sample["cases"]}
    for section_attrs, reveal_attrs in zip(sections, reveals):
        decoded = json.loads(base64.b64decode(reveal_attrs["data-reveal"]).decode("utf-8"))
        assert decoded == json.loads(json.dumps(reveal_payload(by_id[section_attrs["data-case"]])))
    # progress storage is guarded, and labels leave only through the export button
    script = re.search(r"<script>(.*?)</script>", page, re.DOTALL).group(1)
    assert script.count("localStorage") == 2 and script.count("try {") >= 2
    assert 'id="export"' in page and "label_entered_before_reveal: true" in script
    meta = json.loads(re.search(r'<script type="application/json" id="audit-meta">(.*?)</script>', page, re.DOTALL).group(1))
    assert meta["sample_sha256"] == digest


def test_labelling_page_script_is_valid_javascript(sample, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the script is checked where it is available")
    page = render_labelling_page(sample, "a" * 64)
    script = tmp_path / "page.js"
    script.write_text(re.search(r"<script>(.*?)</script>", page, re.DOTALL).group(1), encoding="utf-8")
    result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# -- terminal labelling ---------------------------------------------------------------- #


def test_terminal_labelling_reveals_only_after_the_label_and_resumes(sample, tmp_path):
    digest = write_sample(sample, tmp_path / "sample.json")
    out = tmp_path / "labels.json"
    said: list[str] = []
    answers = iter(["x", "n", "evidence is about another drug", "2", "s", "y", "", "", "q"])
    asked_at: list[int] = []

    def ask(prompt):
        asked_at.append(len(said))
        return next(answers)

    labels = label_interactively(sample, digest, out, auditor="tester", ask=ask, say=said.append)
    assert [e["label"] for e in labels["labels"]] == ["no", "yes"]
    assert labels["labels"][0]["decomposition"] == "wrong_entity" and labels["labels"][0]["note"].startswith("evidence")
    assert labels["labels"][1]["decomposition"] == "not_assessed"
    first_reveal = next(i for i, line in enumerate(said) if line.startswith("Gate verdict"))
    first_label_answer = asked_at[1]  # "x" was rejected, "n" is the label
    assert first_reveal > first_label_answer
    assert not any(line.startswith("Gate verdict") for line in said[:first_label_answer])
    # resume: already labelled cases are not asked again
    said.clear()
    answers = iter(["c", "", "", "q"])
    labels = label_interactively(sample, digest, out, ask=lambda p: next(answers), say=said.append)
    assert [e["label"] for e in labels["labels"]] == ["no", "yes", "cannot_tell"]
    assert load_labels(out, sample, digest)["auditor"] == "tester"


def test_label_files_are_validated_against_their_sample(sample, tmp_path):
    digest = write_sample(sample, tmp_path / "sample.json")
    case_id = sample["cases"][0]["case_id"]
    good = {"case_id": case_id, "label": "yes", "note": "", "decomposition": "not_assessed",
            "labelled_at": None, "revealed_at": None, "label_entered_before_reveal": True}
    labels = new_labels(digest)
    labels["labels"] = [good]
    validate_labels(labels, sample, digest)
    for bad, message in (
        ({**labels, "sample_sha256": "f" * 64}, "belong to sample"),
        ({**labels, "labels": [{**good, "case_id": "nope"}]}, "not in the sample"),
        ({**labels, "labels": [good, good]}, "twice"),
        ({**labels, "labels": [{**good, "label": "maybe"}]}, "is not one of"),
        ({**labels, "labels": [{**good, "decomposition": "?"}]}, "unknown decomposition"),
        ({**labels, "labels": [{**good, "label_entered_before_reveal": False}]}, "before the verdict was revealed"),
    ):
        with pytest.raises(AuditError, match=message):
            validate_labels(bad, sample, digest)


# -- analysis ------------------------------------------------------------------------------ #


def test_clopper_pearson_matches_known_values():
    assert clopper_pearson(0, 0) is None
    low, high = clopper_pearson(0, 10)
    assert low == 0.0 and abs(high - (1 - 0.025 ** 0.1)) < 1e-9
    low, high = clopper_pearson(10, 10)
    assert high == 1.0 and abs(low - 0.025 ** 0.1) < 1e-9
    # reference values: beta quantiles from scipy.stats.beta.ppf, computed once (scipy is not a dependency)
    low, high = clopper_pearson(5, 10)
    assert abs(low - 0.18708602844739855) < 1e-9 and abs(high - 0.8129139715526015) < 1e-9
    low, high = clopper_pearson(50, 50)
    assert abs(low - 0.025 ** (1 / 50)) < 1e-9
    low, high = clopper_pearson(43, 50)
    assert abs(low - 0.7326039975029917) < 1e-9 and abs(high - 0.9418082996596279) < 1e-9
    with pytest.raises(ValueError):
        clopper_pearson(3, 2)


def scripted_labels(sample, digest, plan):
    """``plan(case) -> (label, decomposition, note)``."""
    labels = new_labels(digest, "scripted auditor")
    for case in sample["cases"]:
        label, decomposition, note = plan(case)
        labels["labels"].append({"case_id": case["case_id"], "label": label, "note": note, "decomposition": decomposition,
                                 "labelled_at": None, "revealed_at": None, "label_entered_before_reveal": True})
    return labels


def test_agreement_disagreements_and_decomposer_share(sample, tmp_path):
    digest = write_sample(sample, tmp_path / "sample.json")
    accepted = [c for c in sample["cases"] if c["half"] == ACCEPTED]
    blocked = [c for c in sample["cases"] if c["half"] == BLOCKED]
    special = {
        accepted[0]["case_id"]: ("no", "wrong_entity", "it is about the competitor"),
        accepted[1]["case_id"]: ("no", "not_assessed", ""),
        accepted[2]["case_id"]: ("cannot_tell", "not_assessed", ""),
        blocked[0]["case_id"]: ("yes", "constraints_correct", ""),
        blocked[1]["case_id"]: ("yes", "constraints_correct", ""),
        blocked[2]["case_id"]: ("yes", "constraints_correct", ""),
        blocked[3]["case_id"]: ("cannot_tell", "not_assessed", ""),
    }

    def plan(case):
        return special.get(case["case_id"], ("yes" if case["half"] == ACCEPTED else "no", "not_assessed", ""))

    labels = scripted_labels(sample, digest, plan)
    result = analyse_audit(sample, digest, [labels])
    assert result["complete"] and result["n_labelled"] == 100
    acc, blk = result["per_half"][ACCEPTED], result["per_half"][BLOCKED]
    assert (acc["agreement"]["k"], acc["agreement"]["n"]) == (47, 49)
    assert (blk["agreement"]["k"], blk["agreement"]["n"]) == (46, 49)
    assert acc["cannot_tell"] == 1 and blk["cannot_tell"] == 1
    assert result["per_half"][ACCEPTED]["agreement"]["ci"] == [round(x, 6) for x in clopper_pearson(47, 49)]
    sensitivity = result["sensitivity_cannot_tell_as_disagreement"]
    assert (sensitivity["per_half"][ACCEPTED]["k"], sensitivity["per_half"][ACCEPTED]["n"]) == (47, 50)
    assert (result["sample_agreement_unweighted"]["k"], result["sample_agreement_unweighted"]["n"]) == (93, 98)
    weighted = result["population_weighted_agreement"]
    expected = Fraction(90, 150) * Fraction(46, 49) + Fraction(60, 150) * Fraction(47, 49)
    assert abs(weighted["value"] - float(expected)) < 1e-12
    assert weighted["ci"][0] <= weighted["value"] <= weighted["ci"][1]
    assert "alpha/2" in weighted["method"]
    kinds = sorted(d["kind"] for d in result["disagreements"])
    assert kinds == ["false accept"] * 2 + ["false block"] * 3
    note = next(d for d in result["disagreements"] if d["note"])
    assert "it is about the competitor" in note["description"] and note["constraints"]["entity"].startswith("xanadrug")
    dec = result["decomposer"]
    assert (dec["gate_errors"], dec["assessed"], dec["decomposer_fault"]) == (5, 4, 1)
    assert dec["share"]["value"] == 0.25
    assert result["single_auditor"] is True and result["limitations"]
    assert "Clopper-Pearson" in result["ci_method"]


def test_two_auditors_give_an_inter_rater_kappa(sample, tmp_path):
    digest = write_sample(sample, tmp_path / "sample.json")
    first = scripted_labels(sample, digest, lambda c: ("yes" if c["half"] == ACCEPTED else "no", "not_assessed", ""))
    second = scripted_labels(sample, digest, lambda c: ("yes" if c["half"] == ACCEPTED else "no", "not_assessed", ""))
    second["auditor"] = "second"
    result = analyse_audit(sample, digest, [first, second])
    assert result["single_auditor"] is False and result["limitations"] == []
    (rater,) = result["inter_rater"]
    assert rater["n_common"] == 100 and rater["raw_agreement"] == 1.0 and rater["cohen_kappa"] == 1.0
    partial = new_labels(digest)
    partial["labels"] = first["labels"][:10]
    result = analyse_audit(sample, digest, [partial])
    assert result["complete"] is False and len(result["unlabelled"]) == 90


def test_command_line_sample_html_and_analyze(run_dir, tmp_path, capsys):
    sample_path, page = tmp_path / "sample.json", tmp_path / "audit.html"
    assert audit_main(["sample", str(run_dir), "--out", str(sample_path), "--html", str(page)]) == 0
    printed = capsys.readouterr().out
    assert '"composition"' in printed and page.exists()
    sample, digest = load_sample(sample_path)
    labels = scripted_labels(sample, digest, lambda c: ("yes" if c["half"] == ACCEPTED else "no", "not_assessed", ""))
    save_labels(labels, tmp_path / "labels.json")
    out = tmp_path / "audit.json"
    assert audit_main(["analyze", "--sample", str(sample_path), "--labels", str(tmp_path / "labels.json"), "--out", str(out)]) == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["sample_sha256"] == digest and result["auditors"][0]["labels_sha256"]
    assert result["per_half"][BLOCKED]["agreement"]["value"] == 1.0
    assert audit_main(["html", "--sample", str(sample_path), "--out", str(tmp_path / "again.html")]) == 0
    assert (tmp_path / "again.html").read_text(encoding="utf-8") == page.read_text(encoding="utf-8")
    assert audit_main(["analyze", "--sample", str(tmp_path / "missing.json"), "--labels", "x", "--out", str(out)]) == 1
