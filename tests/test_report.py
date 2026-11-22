"""The public report generator (PREREGISTRATION.md section 15) on synthetic analyses."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import audit_helpers as ah
import html_check
import stats_helpers as sh
from binding_gate.stats import prereg
from binding_gate.audit_tool.analysis import analyse_audit
from binding_gate.audit_tool.labels import new_labels
from binding_gate.audit_tool.sampling import draw_sample, load_runs, write_sample
from binding_gate.contracts import Split
from binding_gate.report import prereg_doc
from binding_gate.report.__main__ import main as report_main
from binding_gate.report.build import (
    COMMITMENT_SECTIONS,
    SECTIONS,
    InputFile,
    ReportError,
    ReportInputs,
    build_blocks,
    load_inputs,
    write_report,
)
from binding_gate.report.document import render_html, render_markdown
from binding_gate.stats.analysis import analyse, analyse_run_dirs

REPO_ROOT = Path(__file__).resolve().parents[1]
PREREG_PATH = REPO_ROOT / "PREREGISTRATION.md"
PREREG = PREREG_PATH.read_text(encoding="utf-8")
FROZEN = PREREG.replace("**Status:** DRAFT", "**Status:** FROZEN", 1)
# a valid 1x1 PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def records(*, split=Split.TEST, repeats=prereg.REPEATS, groups=8, gated_errors=0, gated_like_reference=False, with_faith=True):
    out = []
    for k in range(groups):
        for r in range(repeats):
            wrong = k % 2 == 0
            ref = "wrong_entity" if wrong else "target_grounded"
            gated = ref if gated_like_reference else ("abstention" if wrong else "target_grounded")
            if k < gated_errors:
                gated = "error"
            common = dict(item=f"i{k}", group=f"g{k}", repeat=r, split=split)
            out.append(sh.record(ref, arm="no_gate", **common))
            out.append(sh.record(gated, arm="gated", **common))
            if with_faith:
                out.append(sh.record(ref, arm="faithfulness_checker", **common))
    return out


def analysis_file(tmp_path: Path, analysis: dict, name: str = "a", figures: bool = False) -> Path:
    directory = tmp_path / name
    directory.mkdir(parents=True)
    (directory / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
    if figures:
        (directory / "figures").mkdir()
        (directory / "figures" / "primary_pair.png").write_bytes(PNG)
    return directory


def inputs_for(*analyses: dict, **kwargs) -> ReportInputs:
    return ReportInputs(
        analyses=[InputFile(a, f"analysis{i}.json", "0" * 64) for i, a in enumerate(analyses)],
        prereg_text=kwargs.pop("prereg_text", PREREG),
        **kwargs,
    )


@pytest.fixture(scope="module")
def dev_analysis() -> dict:
    return analyse(records(split=Split.DEV), resamples=200)


@pytest.fixture(scope="module")
def test_analysis() -> dict:
    return analyse(records())  # pre-registered bootstrap: confirmatory where the data allow


def markdown(inputs: ReportInputs) -> str:
    return render_markdown(build_blocks(inputs))


def section(md: str, anchor: str) -> str:
    start = md.index(f'<a id="{anchor}"></a>')
    following = [md.index(f'<a id="{a}"></a>') for a in SECTIONS if md.index(f'<a id="{a}"></a>') > start]
    return md[start: min(following) if following else len(md)]


# -- structure and confirmatory status ------------------------------------------------- #


def test_dev_report_has_every_section_in_order_and_nothing_confirmatory(dev_analysis):
    md = markdown(inputs_for(dev_analysis))
    positions = [md.index(f'<a id="{anchor}"></a>') for anchor in SECTIONS]
    assert positions == sorted(positions)
    for title in SECTIONS.values():
        assert f"## {title}" in md
    assert "No result in this report is confirmatory" in md
    assert "split is 'dev', not 'test' (exploratory only)" in md
    assert "None" in section(md, "confirmatory") and "### H1" not in section(md, "confirmatory")
    headline = section(md, "headline")
    assert "| H1 | H2 | H3 | H4 |" in headline
    assert "SUPPORTED (not confirmatory)" in headline and "(confirmatory)" not in headline
    assert "The pre-registration is DRAFT, not FROZEN" in md


def test_confirmatory_section_lists_only_what_the_analysis_marks_confirmatory(test_analysis):
    statuses = {h: (r["status"], r["confirmatory"]) for h, r in test_analysis["models"]["m"]["hypotheses"].items()}
    assert statuses == {"H1": ("SUPPORTED", True), "H2": ("SUPPORTED", True), "H3": ("SUPPORTED", True),
                        "H4": ("NOT EVALUABLE", False)}
    with pytest.raises(ReportError, match="has status DRAFT"):
        build_blocks(inputs_for(test_analysis))  # a confirmatory result needs the frozen plan
    md = markdown(inputs_for(test_analysis, prereg_text=FROZEN))
    assert "is DRAFT, not FROZEN" not in md
    confirmatory = section(md, "confirmatory")
    for h in ("H1", "H2", "H3"):
        assert f"### {h}, `m`" in confirmatory
    assert "H4" not in confirmatory
    assert "3 hypothesis result(s) are confirmatory" in md
    assert "SUPPORTED (confirmatory)" in section(md, "headline")
    # the exploratory section refers to the confirmatory one instead of repeating the decision
    assert "details in [Confirmatory results](#confirmatory)" in section(md, "exploratory")


def test_an_inconsistent_analysis_cannot_be_published_as_confirmatory(dev_analysis, test_analysis):
    tampered = copy.deepcopy(dev_analysis)
    tampered["models"]["m"]["hypotheses"]["H1"]["confirmatory"] = True
    with pytest.raises(ReportError, match="marked confirmatory.*split is 'dev'"):
        build_blocks(inputs_for(tampered))
    blocked = copy.deepcopy(test_analysis)
    blocked["confirmatory_blockers"] = ["unfinished run(s): ['x']"]
    with pytest.raises(ReportError, match="lists confirmatory blockers"):
        build_blocks(inputs_for(blocked, prereg_text=FROZEN))
    with pytest.raises(ReportError, match="two analyses"):
        build_blocks(inputs_for(test_analysis, test_analysis, prereg_text=FROZEN))
    with pytest.raises(ReportError, match="not an analysis file"):
        build_blocks(inputs_for({"dataset": "x"}))


# -- negative results, coverage and rates ---------------------------------------------- #


def test_negative_results_come_right_after_the_headline_at_the_same_level():
    analysis = analyse(records(gated_like_reference=True, gated_errors=1), resamples=200)
    md = markdown(inputs_for(analysis))
    headline, negative, confirmatory = (md.index(f"## {SECTIONS[a]}") for a in ("headline", "negative", "confirmatory"))
    assert headline < negative < confirmatory
    text = section(md, "negative")
    assert "**H1 NOT SUPPORTED** (not confirmatory)" in text and "`relative_reduction` failed" in text
    assert "**H4 NOT EVALUABLE**" in text
    # validity guard: 1 of 8 items has an error record in every repeat = 12.5% > 5%
    assert "arm `gated`: unparseable + error rate 12.5% exceeds the validity limit 5.0%" in text
    assert "Layer 2 (`sec_financials`) is not in this report" in text and "Layer 3 (`sealqa`) is not in this report" in text
    assert "`prompt_entity_check`" in text and "`gated_research`" in text
    assert "stop-rule outcome (section 10) was not supplied" in text
    assert "human audit (section 13) has not been carried out" in text
    # the failure rate sits next to every headline number
    assert "unparseable + error / all" in section(md, "headline") and "12.5%" in section(md, "headline")


def test_h1_that_fails_the_sensitivity_bound_is_listed_as_negative():
    # gated removes every wrong answer but crashes on 3 of 8 items: under the worst case
    # (section 6.4 item 7) those count as wrong, and the reduction of M1 vanishes
    analysis = analyse(records(gated_errors=3), resamples=500)
    h1 = analysis["models"]["m"]["hypotheses"]["H1"]
    assert h1["status"] == "SUPPORTED" and h1["sensitivity"]["status_under_worst_case"] == "NOT SUPPORTED"
    md = markdown(inputs_for(analysis))
    assert "| SUPPORTED | NOT SUPPORTED |" in section(md, "headline")
    assert "H1 is SUPPORTED but NOT SUPPORTED under the worst-case sensitivity bound" in section(md, "negative")


def test_every_arm_model_and_condition_is_in_the_exploratory_and_coverage_sections(dev_analysis):
    md = markdown(inputs_for(dev_analysis))
    exploratory = section(md, "exploratory")
    for arm in ("no_gate", "gated", "faithfulness_checker"):
        assert f"| `{arm}` |" in exploratory
    assert "M1 wrong among answered | M2 correct answers blocked" in exploratory
    assert "`complete/class_proximate`" in exploratory
    coverage = section(md, "coverage")
    assert "| 2 | `sec_financials` | **ABSENT** |" in coverage
    # stats_helpers puts every record in one condition, so 14 layer-1 conditions are reported missing
    assert "`absent/null_control`" in coverage


# -- section 15 commitments, threats, amendments ----------------------------------------- #


def test_every_commitment_of_section_15_is_mapped_to_a_section(dev_analysis):
    commitments = prereg_doc.commitments(PREREG)
    assert [c.ident for c in commitments] == [f"R{n}" for n in range(1, 13)]
    assert set(COMMITMENT_SECTIONS) == {c.ident for c in commitments}
    assert all(set(v) <= set(SECTIONS) for v in COMMITMENT_SECTIONS.values())
    table = section(markdown(inputs_for(dev_analysis)), "commitments")
    for c in commitments:
        assert f"| {c.ident} |" in table
    extended = PREREG.replace("- **R12**", "- **R13** a new promise.\n- **R12**")
    with pytest.raises(ReportError, match="R13"):
        build_blocks(inputs_for(dev_analysis, prereg_text=extended))


def test_threats_rules_and_amendments_are_quoted_from_the_preregistration(dev_analysis):
    threats = prereg_doc.threats(PREREG)
    assert len(threats) >= 10 and threats[0][0] == "Constructed clinical data"
    md = markdown(inputs_for(dev_analysis))
    for title, _text in threats:
        assert f"**{title}.**" in section(md, "threats")
    rules = prereg_doc.hypothesis_rules(PREREG)
    assert set(rules) == {"H1", "H2", "H3", "H4"}
    assert rules["H2"] in section(md, "headline")
    # PR 22: amendment 1 (the layer-1 STOP and its exploratory validation) is the first row of section 18
    (first,) = prereg_doc.amendments(PREREG)
    assert first["#"] == "1" and first["date"] == "2026-11-22" and "after seeing the layer-1 dev baseline" in first["reason"].lower()
    assert "Layer-1 STOP applied as written" in section(md, "deviations")
    row = next(line for line in PREREG.splitlines() if line.startswith("| 1 | 2026-11-22 |"))
    unamended = PREREG.replace(row, "| (none yet) | | | | | |")
    assert prereg_doc.amendments(unamended) == []
    assert "no amendment recorded" in section(markdown(inputs_for(dev_analysis, prereg_text=unamended)), "deviations")
    amended = PREREG.replace(row, row + "\n| 2 | 2027-01-02 | 9 | Slot F model id replaced | retired by the provider | no |")
    md = markdown(inputs_for(dev_analysis, prereg_text=amended))
    assert "Slot F model id replaced" in section(md, "deviations")
    assert prereg_doc.status(PREREG) == "DRAFT"


# -- HTML ------------------------------------------------------------------------------------ #


def test_html_is_self_contained_well_formed_and_matches_the_markdown(tmp_path, dev_analysis):
    directory = analysis_file(tmp_path, dev_analysis, figures=True)
    inputs = load_inputs([directory], prereg_path=PREREG_PATH)
    written = write_report(inputs, tmp_path / "out")
    page = Path(written["html"]).read_text(encoding="utf-8")
    html_check.check(page)
    assert "<script" not in page.lower()
    assert html_check.external_references(page) == []
    assert "http://" not in page and "https://" not in page.replace("https://claude", "")
    assert page.count('src="data:image/png;base64,') == 1
    md = Path(written["markdown"]).read_text(encoding="utf-8")
    assert "](figures/layer1_dev_primary_pair.png)" in md and Path(written["figures"][0]).read_bytes() == PNG
    for title in SECTIONS.values():
        assert f"## {title}" in md and f">{title}</h2>" in page.replace("&quot;", '"')
    assert md.count("\n|---|") == page.count("<table>")


def test_inline_markup_is_escaped(dev_analysis):
    hostile = copy.deepcopy(dev_analysis)
    hostile["models"]["<b>m</b>"] = hostile["models"].pop("m")
    page = render_html(build_blocks(inputs_for(hostile)), "t")
    assert "<b>m</b>" not in page and "&lt;b&gt;m&lt;/b&gt;" in page
    html_check.check(page)


# -- run directories, costs, baseline, deviations, audit ---------------------------------------- #


@pytest.fixture(scope="module")
def real_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("report-run")
    _repo, summary = ah.gated_run(tmp)
    analysis = analyse_run_dirs([summary.run_dir], resamples=100)
    directory = tmp / "analysis"
    directory.mkdir()
    (directory / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
    return summary.run_dir, directory, tmp


def test_costs_artifacts_and_unanalysed_runs_come_from_run_directories(real_run, tmp_path):
    run_dir, directory, _tmp = real_run
    failed = tmp_path / "failed-run"
    failed.mkdir()
    (failed / "manifest.json").write_text(json.dumps({"run_id": "crashed-run", "config_hash": "c" * 64, "git_commit": "d" * 40}))
    inputs = load_inputs([directory], prereg_path=PREREG_PATH, run_dirs=[run_dir, failed])
    md = markdown(inputs)
    costs = section(md, "costs")
    # 90 answer calls per model x (1,000 input tokens at $1/M + 500 output tokens at $2/M) = $0.18
    assert "| `good` | $0.1800 | $5.00 | no |" in costs and "| `bad` | $0.1800 | $5.00 | no |" in costs
    assert "`fake-decomposer`" in costs  # its calls are not priced and the report says so
    assert "| arm | paid_calls | cached_calls | failed_calls | input_tokens | output_tokens |" in costs
    artifacts = section(md, "artifacts")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["traces_sha256"] in artifacts and manifest["config_hash"] in artifacts and manifest["git_commit"] in artifacts
    assert "`crashed-run`" in artifacts
    assert "Run `crashed-run`" in section(md, "negative") and "is in no analysis and is unfinished" in section(md, "negative")


def test_a_run_directory_that_disagrees_with_the_analysis_is_refused(real_run, tmp_path):
    run_dir, directory, _tmp = real_run
    changed = tmp_path / "changed"
    changed.mkdir()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    manifest["traces_sha256"] = "e" * 64
    (changed / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ReportError, match="trace SHA-256 differs"):
        build_blocks(load_inputs([directory], prereg_path=PREREG_PATH, run_dirs=[changed]))


def test_stop_rule_deviations_and_audit_sections(real_run, tmp_path):
    run_dir, directory, _tmp = real_run
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"arm": "no_gate", "overall": "IN-BETWEEN", "provisional": False,
                                    "models": [{"model": "good", "R": 0.07, "ungrounded_or_unparseable_share": 0.01, "decision": "carry"}]}))
    deviations = tmp_path / "deviations.json"
    deviations.write_text(json.dumps([{"description": "smoke run repeated after an outage", "amendment": None},
                                      {"description": "slot F id changed", "amendment": 1}]))
    sample = draw_sample(load_runs([run_dir]))
    digest = write_sample(sample, tmp_path / "sample.json")
    labels = new_labels(digest, "auditor A")
    for case in sample["cases"]:
        label = "yes" if case["half"] == "accepted" or case["display_number"] == 2 else "no"
        labels["labels"].append({"case_id": case["case_id"], "label": label, "note": "", "decomposition": "not_assessed",
                                 "labelled_at": None, "revealed_at": None, "label_entered_before_reveal": True})
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps(analyse_audit(sample, digest, [labels], label_hashes=["9" * 64])))
    inputs = load_inputs([directory], prereg_path=PREREG_PATH, run_dirs=[run_dir], baseline_paths=[baseline],
                         audit_path=audit, deviations_path=deviations)
    md = markdown(inputs)
    assert "overall outcome **IN-BETWEEN**" in section(md, "stop-rule") and "| `good` | 7.0% |" in section(md, "stop-rule")
    assert "Stop-rule outcome in" in section(md, "negative")
    dev = section(md, "deviations")
    assert "| smoke run repeated after an outage | **unplanned** (no amendment) |" in dev and "| slot F id changed | amendment 1 |" in dev
    assert "drawn from `dev` traces" in dev
    audit_md = section(md, "audit")
    assert "Not the pre-registered audit" in audit_md and "Single auditor" in audit_md
    assert "| accepted | all | 60 | 50 | 60/50 |" in audit_md and "| blocked | all | 90 | 50 | 90/50 |" in audit_md
    expected_disagreements = sum(1 for c in sample["cases"] if c["half"] == "blocked" and c["display_number"] == 2)
    if expected_disagreements:
        assert "False block" in audit_md
    assert "Clopper-Pearson" in audit_md and "Population-weighted agreement" in audit_md
    assert "`" + "9" * 64 + "`" in section(md, "artifacts")


def test_command_line_build(real_run, tmp_path, capsys):
    run_dir, directory, _tmp = real_run
    out = tmp_path / "public"
    assert report_main(["build", "--analysis", str(directory), "--run-dir", str(run_dir),
                        "--preregistration", str(PREREG_PATH), "--out", str(out), "--title", "Synthetic report"]) == 0
    written = json.loads(capsys.readouterr().out)
    assert Path(written["markdown"]).read_text(encoding="utf-8").startswith('<a id="top"></a>\n\n# Synthetic report')
    html_check.check(Path(written["html"]).read_text(encoding="utf-8"))
    assert report_main(["build", "--analysis", str(tmp_path / "nothing"), "--preregistration", str(PREREG_PATH),
                        "--out", str(out)]) == 1
