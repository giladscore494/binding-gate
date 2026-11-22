"""PR 22 (PREREGISTRATION.md section 18, amendment 1): the exploratory validation of the layer-1 STOP.

The free_form arm (request, contracts, scoring), the [sample] exclusion, judge scoring of both arms on the fixed
subsample, the paired comparison, the validate-layer1 gate, the ledger's refusal of stopped runs and its judge
session keys, the results-log allow-list, and the layer-2 stop rule. Fakes only: nothing is called or spent.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

import builders as b
import dg_fixture
import pr5_fakes as f
import run_helpers as h
from fakes import ScriptedFake, shown_documents
from binding_gate import baseline_report, judging, ledger, layer2_stop, pipeline, registry, results_log, runner, validation
from binding_gate.answering import (
    FREE_FORM_QUOTE,
    FREE_FORM_SOURCE_ID,
    build_answer_request,
    build_free_form_request,
    free_form_candidate,
    is_free_form,
)
from binding_gate.config import ConfigError, load_config, parse_config
from binding_gate.contracts import (
    AnswerCandidate,
    AnswerStatus,
    ArmOutput,
    Attempt,
    ModelResponse,
    ScoreResult,
    Split,
    TraceRecord,
)
from binding_gate.datasets.deceptive_grounding import AGENT_SYSTEM_PROMPT, doc_injection_messages, format_document_block, items_from_triple
from binding_gate.parsing import ANSWER_FORMAT_INSTRUCTIONS
from binding_gate.registry import register
from binding_gate.sampling import sample_items, stratified_sample
from binding_gate.scorers import llm_judge as lj
from binding_gate.scorers.provenance import ProvenanceScorer
from binding_gate.stats.analysis import load_trace_set
from binding_gate.stats.bootstrap import StatsError
from binding_gate.trace import read_traces

REPO_ROOT = Path(__file__).resolve().parents[1]
NULL = ["absent/null_control", "partial/null_control", "complete/null_control"]


@pytest.fixture(autouse=True)
def _fresh():
    judging.reset_shared_state()
    f.JUDGE_CALLS.clear()
    yield
    judging.reset_shared_state()
    f.JUDGE_CALLS.clear()


@register("model", "pr22_mode_fake")
class ModeFake(ScriptedFake):
    """Asked for the structure: quotes the target document, or abstains without one. Asked freely: answers from the
    target document when there is one and otherwise from the COMPETITOR (the failure free-form answers may show)."""

    def __init__(self, *, provider: str = "openai", **options: Any):
        super().__init__(**options)
        self.provider = provider

    def complete(self, request):
        prompt = request.messages[-1].content
        if "Output format (required" in prompt:
            return super().complete(request)
        documents = shown_documents(request)
        source = "cx_doc" if "cx_doc" in documents else "cy_doc"
        return ModelResponse(
            text=f"<think>reasoning</think>The literature says it works [1] (source {source}): {documents[source][:40]}",
            model=f"{self.model_id}-2026-01-01",
            usage=self._usage,
            raw={"truncated": False},
        )


def _items():
    return items_from_triple(dg_fixture.triple(1))


# --------------------------------------------------------------------------- #
# The free_form arm: request and representation
# --------------------------------------------------------------------------- #


def test_free_form_request_is_the_benchmark_prompt_verbatim_without_our_json_instruction():
    item = _items()[7]
    arm_input = item.arm_input()
    request = build_free_form_request(arm_input)
    system, user = request.messages
    assert (system.role, system.content) == ("system", AGENT_SYSTEM_PROMPT)
    assert user.content == (
        f"Retrieved literature documents:\n\n{format_document_block(arm_input.documents)}\n\n"
        f"Based on the above documents, {arm_input.question}"
    )
    assert request.messages == doc_injection_messages(arm_input.question, arm_input.documents, "")
    assert ANSWER_FORMAT_INSTRUCTIONS not in user.content and "JSON" not in user.content and "quote" not in user.content
    assert "Cite with [n] markers inline" in system.content  # the benchmark's own citation instruction stays
    structured = build_answer_request(arm_input)
    assert structured.messages[1].content == f"{user.content}\n\n{ANSWER_FORMAT_INSTRUCTIONS}"
    with pytest.raises(ValueError, match="no free-form variant"):
        build_free_form_request(arm_input, "sec_filings")


def test_free_form_answers_fit_the_frozen_contracts():
    answer = free_form_candidate("<think>hidden</think>  Drug X is effective [1].  ")
    assert answer.status is AnswerStatus.ANSWERED and answer.value == "Drug X is effective [1]."
    assert (answer.quote, answer.source_id) == (FREE_FORM_QUOTE, FREE_FORM_SOURCE_ID)
    assert answer.raw == "<think>hidden</think>  Drug X is effective [1].  " and is_free_form(answer)
    for empty in ("", "   ", "<think>only reasoning</think>"):
        candidate = free_form_candidate(empty)
        assert candidate.status is AnswerStatus.UNPARSEABLE and candidate.reason == "empty output" and not is_free_form(candidate)
    structured = AnswerCandidate.answered(value="v", quote="a b c d e", source_id="cx_doc", raw="{}")
    assert not is_free_form(structured) and not is_free_form(None)
    # the arm, built from the registry like every arm, returns a valid ArmOutput that round-trips
    item = _items()[3]
    model = registry.create("model", "pr22_mode_fake", model_id="fake-cheap")
    arm = registry.create("arm", "free_form", model=model)
    output = arm.run(item.arm_input())
    assert ArmOutput.from_json(output.to_json()) == output
    assert is_free_form(output.final_answer) and output.attempts[0].verdict is None
    assert len(output.model_calls) == 1 and output.model_calls[0].purpose == "answer"
    assert ANSWER_FORMAT_INSTRUCTIONS not in output.model_calls[0].request.messages[-1].content
    with pytest.raises(ValueError, match="no free-form variant"):
        registry.create("arm", "free_form", model=model, presentation="sec_filings")


def test_provenance_reports_free_form_answers_as_not_applicable():
    item = _items()[5]
    score = ProvenanceScorer().score(item, free_form_candidate("Something about cy_doc and cx_doc."))
    assert score.label == "not_applicable" and score.details["outcome"] == "not_applicable"
    # structured answers are scored exactly as before
    quote = " ".join(item.documents[-1].text.split()[:8])
    structured = AnswerCandidate.answered(value="v", quote=quote, source_id="cy_doc", raw="{}")
    assert ProvenanceScorer().score(item, structured).label in {"competitor_only", "both"}


# --------------------------------------------------------------------------- #
# Runs with fakes: the free_form run, the refusals, judge scoring, the report
# --------------------------------------------------------------------------- #

SAMPLE = {"per_stratum": 2, "seed": 20261122, "exclude_strata": NULL}
MODELS = [{"name": "pr22_mode_fake", "label": "mode", "options": {"model_id": "fake-cheap", "behaviour": "target", "provider": "openai"}},
          {"name": "pr5_family_fake", "label": "bad", "options": {"model_id": "fake-cheap", "behaviour": "competitor", "provider": "google"}}]


@pytest.fixture
def runs(tmp_path):
    repo, base_config, raw = h.setup(tmp_path, models=MODELS, repeats=1)
    (repo / "configs" / "prices.toml").write_text(h.PRICES + f.FAKE_JUDGE_PRICE, encoding="utf-8")
    log = lambda m: None  # noqa: E731
    structured = runner.execute_run(base_config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=log)
    free_raw = {**raw, "name": "free-form-test", "sample": SAMPLE, "arms": [{"name": "free_form", "label": "free_form"}]}
    free_config = parse_config(free_raw)
    free = runner.execute_run(free_config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=log)
    sample_config = tmp_path / "validation.toml"
    sample_config.write_text(
        'rescore_version = 1\nname = "v"\nscorer = "llm_judge"\nmax_usd = 10.0\n'
        '[sample]\nper_stratum = 2\nseed = 20261122\nexclude_strata = ["absent/null_control", "partial/null_control", '
        '"complete/null_control"]\n'
        '[[judges]]\nname = "pr5_fake_judge"\nlabel = "judge-a"\n[judges.options]\nbehaviour = "bench_auto"\n',
        encoding="utf-8",
    )
    return repo, structured.run_dir, free.run_dir, sample_config, free_raw


def test_the_free_form_run_uses_the_subsample_and_is_refused_by_the_confirmatory_analysis(runs):
    repo, structured_dir, free_dir, _config, free_raw = runs
    records = list(read_traces(free_dir))
    assert {r.arm for r in records} == {"free_form"} and len(records) == 2 * 12 * 2  # 2 models x 12 strata x 2
    assert not {r.stratum for r in records} & set(NULL)
    for record in records:
        (score,) = record.scores
        assert score.scorer == "provenance" and score.label == "not_applicable"
        assert TraceRecord.from_json(record.to_json()) == record
    with pytest.raises(baseline_report.ReportError, match="free-form answer"):
        baseline_report.record_outcome(records[0])
    with pytest.raises(StatsError, match="exploratory free-form"):
        load_trace_set([free_dir])
    load_trace_set([structured_dir])  # the structured run is unaffected
    # the free_form records never enter the stop-rule report of the no_gate arm
    assert runner.run_requirements(parse_config(free_raw))["baseline_report"] is False
    with pytest.raises(baseline_report.ReportError, match="no record of arm 'no_gate'"):
        baseline_report.report_from_run_dirs([free_dir])


def test_judge_scores_both_arms_on_the_same_subsample_and_the_report_pairs_them(runs, tmp_path):
    repo, structured_dir, free_dir, sample_config, _raw = runs
    config = lj.RescoreConfig.load(sample_config)
    structured_run = lj.load_run(structured_dir, repo_root=repo)
    free_run = lj.load_run(free_dir, repo_root=repo)
    (estimate,) = lj.estimate(config, [structured_run], repo_root=repo, arm="no_gate")
    # 24 subsample items per model; "mode" abstains (structured) in the 8 conditions without the target document
    assert estimate.calls == 24 + 16 and estimate.static_labels == {"abstained": 8} and f.JUDGE_CALLS == []
    summary = lj.rescore(config, [structured_run], repo_root=repo, confirm_spend=True, arm="no_gate", log=lambda m: None)
    assert summary.lines_written == 48 and not summary.budget_exceeded
    summary = lj.rescore(config, [free_run], repo_root=repo, confirm_spend=True, arm="free_form", log=lambda m: None)
    assert summary.lines_written == 48
    # the judge read the whole free-form reply without the <think> block, as the benchmark judge does
    free_prompts = [request.messages[-1].content for _name, request in f.JUDGE_CALLS[40:]]
    assert len(free_prompts) == 48 and all("<think>" not in p for p in free_prompts)
    assert sum("Response: The literature says it works [1]" in p for p in free_prompts) == 24  # model "mode"

    report = validation.build_report(sample_config=sample_config, structured_dirs=[structured_dir],
                                     free_form_dirs=[free_dir], repo_root=repo, baseline_workflow_run_id=7)
    mode, bad = report["models"]["mode"], report["models"]["bad"]
    # without the target document "mode" abstains when structured but answers from the competitor when free
    assert mode["structured"]["rates"]["eaf_or_substitution"] == 0.0 and mode["structured"]["labels"]["abstained"] == 8
    assert mode["free_form"]["rates"]["eaf_or_substitution"] == pytest.approx(8 / 24)  # the 8 items without cx_doc
    assert mode["paired"]["paired_items"] == 24 and mode["paired"]["only_free_form_positive"] == 8
    assert mode["paired"]["estimates"]["difference"] == pytest.approx(8 / 24)
    low, high = mode["paired"]["intervals"]["difference"]["low"], mode["paired"]["intervals"]["difference"]["high"]
    assert low <= 8 / 24 <= high
    # "bad" always quotes the competitor: judge and deterministic label agree on every structured record
    agreement = bad["structured"]["agreement"]
    assert agreement["raw_agreement"] == 1.0 and agreement["deterministic_rate"] == 1.0
    assert bad["structured"]["rates"]["eaf_or_substitution"] == 1.0 and bad["paired"]["estimates"]["difference"] == 0.0
    assert report["sample"]["exclude_strata"] == NULL and report["baseline_workflow_run_id"] == 7
    assert report["bootstrap"] == {"unit": "group_id", "seed": 20261122, "resamples": 10000, "level": "19/20"}

    # the report goes to the results-log branch: allowed name, no text, no secret-like content
    out = tmp_path / "out"
    out.mkdir()
    assert validation.main(["report", "--sample-config", str(sample_config), "--structured", str(structured_dir),
                            "--free-form", str(free_dir), "--repo-root", str(repo),
                            "--json", str(out / validation.REPORT_JSON), "--markdown", str(out / validation.REPORT_MD)]) == 0
    for name in (validation.REPORT_JSON, validation.REPORT_MD):
        data = (out / name).read_bytes()
        assert results_log.refusal_reason(f"validate-layer1--validation-report/{name}", data) is None, name
        assert b"cy_doc" not in data and b"literature says" not in data
    assert "EXPLORATORY" in (out / validation.REPORT_MD).read_text(encoding="utf-8")


def test_the_report_without_free_form_runs_and_with_unscored_records(runs):
    repo, structured_dir, _free_dir, sample_config, _raw = runs
    report = validation.build_report(sample_config=sample_config, structured_dirs=[structured_dir], repo_root=repo)
    mode = report["models"]["mode"]["structured"]
    assert mode["unscored"] == 24 and mode["rates"]["eaf_or_substitution"] == 0.0
    assert mode["rates_determinate_only"]["eaf_or_substitution"] is None
    assert "paired" not in report["models"]["mode"]
    assert any("V2" in n for n in report["notes"]) and any("no judge score" in n for n in report["notes"])


# --------------------------------------------------------------------------- #
# The paired comparison and the judge flags
# --------------------------------------------------------------------------- #


def unit(item: str, group: str, label: str) -> dict:
    entry = None if label == "unscored" else {"score": {"label": label, "details": {}}}
    return {"item_id": item, "group_id": group, "stratum": "complete/synthetic_y", "repeat": 0, **validation.judge_flags(entry)}


def test_paired_comparison_counts_and_exact_estimates():
    pos, neg = "eaf_or_substitution", "correct_attribution"
    structured = [unit("i1", "g1", pos), unit("i2", "g1", neg), unit("i3", "g2", neg), unit("i4", "g3", "judge_error"),
                  unit("i5", "g3", neg)]
    free = [unit("i1", "g1", pos), unit("i2", "g1", pos), unit("i3", "g2", pos), unit("i4", "g3", pos), unit("i6", "g4", pos)]
    result = validation.paired_comparison(structured, free)
    assert result["paired_items"] == 4 and result["unpaired_structured"] == 1 and result["unpaired_free_form"] == 1
    assert (result["both_positive"], result["only_structured_positive"], result["only_free_form_positive"]) == (1, 0, 3)
    e = result["estimates"]
    assert e["structured_rate"] == 0.25 and e["free_form_rate"] == 1.0 and e["difference"] == 0.75
    assert result["both_determinate"] == 3 and e["difference_determinate"] == pytest.approx(float(Fraction(2, 3)))
    interval = result["intervals"]["difference"]
    assert interval["low"] <= 0.75 <= interval["high"] and interval["defined"] == 10_000
    assert validation.paired_comparison(structured, free) == result  # deterministic (fixed seed)


def test_judge_flags_read_the_benchmark_judgment():
    judgment = lj.validate_judgment(f.benchmark_judgment("Neither"))
    flags = validation.judge_flags({"score": {"label": "confabulation_only", "details": {"judgment": judgment}}})
    assert flags["confabulation"] and not flags["eaf"] and not flags["substitution"] and flags["determinate"]
    judgment = f.benchmark_judgment("Document 1", substitution=True)
    flags = validation.judge_flags({"score": {"label": "eaf_or_substitution", "details": {"judgment": judgment}}})
    assert flags["substitution"] and flags["eaf_or_substitution"] and not flags["eaf"]
    assert not validation.judge_flags({"score": {"label": "judge_unparseable", "details": {}}})["determinate"]
    assert validation.judge_flags(None)["label"] == "unscored"


# --------------------------------------------------------------------------- #
# [sample] exclude_strata
# --------------------------------------------------------------------------- #


def test_exclude_strata_drops_whole_strata_and_keeps_the_nesting():
    items = [i for t in (1, 2, 3, 4) for i in items_from_triple(dg_fixture.triple(t))]
    plain = stratified_sample(items, per_stratum=2, seed=20261122)
    excluded = stratified_sample(items, per_stratum=2, seed=20261122, exclude_strata=NULL)
    assert {i.id for i in excluded} == {i.id for i in plain if i.stratum not in NULL}
    assert len(excluded) == 12 * 2
    small = sample_items(items, {"per_stratum": 1, "seed": 20261122, "exclude_strata": NULL})
    assert {i.id for i in small} <= {i.id for i in excluded}


def test_exclude_strata_is_validated_and_hashed_only_when_present():
    base = h.raw_config({"root": "x"}, sample={"per_stratum": 2, "seed": 1})
    plain = parse_config(base)
    assert plain.to_dict()["sample"] == {"per_stratum": 2, "seed": 1}
    with_exclusion = parse_config({**base, "sample": {"per_stratum": 2, "seed": 1, "exclude_strata": ["a/b"]}})
    assert with_exclusion.to_dict()["sample"]["exclude_strata"] == ["a/b"]
    for bad in ([], "a/b", ["a/b", "a/b"], [1], [""]):
        with pytest.raises(ConfigError, match="exclude_strata"):
            parse_config({**base, "sample": {"per_stratum": 2, "seed": 1, "exclude_strata": bad}})
    with pytest.raises(ConfigError, match="optionally"):
        parse_config({**base, "sample": {"per_stratum": 2, "seed": 1, "other": 1}})


def test_the_hash_of_every_existing_config_with_a_sample_is_unchanged():
    """Configs written before PR 22 have no exclude_strata, so their normalised form (and hash) did not change."""
    for path in sorted((REPO_ROOT / "configs").glob("*.toml")):
        if path.name in {"prices.toml", "budget.toml", "example.toml", "decomposer_eval_dev.toml"}:
            continue
        config = load_config(path)
        if config.sample is not None and path.name != "dev_validation_freeform.toml":
            assert set(config.to_dict()["sample"]) == {"per_stratum", "seed"}, path.name


# --------------------------------------------------------------------------- #
# The validate-layer1 gate, the stopped runs, the judge session keys
# --------------------------------------------------------------------------- #


def test_validate_gate_needs_a_final_stop(tmp_path, capsys):
    allowed, why = pipeline.validate_gate({"workflow_run_id": 45053502161, "decision": "STOP", "provisional": False})
    assert allowed and "amendment 1" in why
    for decision in ({"decision": "STOP", "provisional": True}, {"decision": "CONTINUE"}, {"decision": "IN-BETWEEN"}):
        assert not pipeline.validate_gate({"workflow_run_id": 1, **decision})[0]
    path = tmp_path / "decision.json"
    path.write_text(json.dumps({"workflow_run_id": 45053502161, "decision": "STOP", "provisional": False}), encoding="utf-8")
    assert pipeline.main(["validate-gate", "--decision", str(path)]) == 0
    assert "seed_run_id=45053502161" in capsys.readouterr().out
    path.write_text(json.dumps({"workflow_run_id": 3, "decision": "CONTINUE", "provisional": False}), encoding="utf-8")
    assert pipeline.main(["validate-gate", "--decision", str(path)]) == pipeline.EXIT_REFUSED


def test_the_ledger_refuses_stopped_runs_but_not_the_validation():
    stopped = ledger.stopped_configs(REPO_ROOT)
    assert "configs/test_layer1.toml" in stopped and "configs/dev_gated.toml" in stopped
    assert not ledger.stopped_refusals(pipeline.STAGES["validate-layer1"], REPO_ROOT)
    assert not ledger.stopped_refusals(pipeline.STAGES["layer2-dev"] + pipeline.STAGES["layer3-dev"], REPO_ROOT)
    assert len(ledger.stopped_refusals(pipeline.STAGES["dev-rest"], REPO_ROOT)) == len(pipeline.STAGES["dev-rest"]) - 1
    (reason,) = ledger.stopped_refusals(["configs/test_layer1.toml"], REPO_ROOT)
    assert "STOPPED" in reason and "45053502161" in reason
    data = {"total_usd": 1.14, "hard_limit_usd": 50.0, "complete": True, "active_runs": []}
    assert ledger.check(data, 1.0, what="x")["allowed"]
    result = ledger.check(data, 1.0, what="x", refusals=[reason])
    assert not result["allowed"] and result["reasons"] == [reason]


def test_the_ledger_cli_refuses_a_stopped_config(tmp_path, capsys):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"ledger_version": 1, "total_usd": 1.14, "hard_limit_usd": 50.0, "complete": True,
                                "active_runs": [], "entries": {}}), encoding="utf-8")
    common = ["check", "--ledger", str(path), "--repo-root", str(REPO_ROOT)]
    assert ledger.main([*common, "--config", "configs/test_layer1_midtier.toml"]) == ledger.EXIT_REFUSED
    assert "STOPPED" in capsys.readouterr().out
    assert ledger.main([*common, "--stage", "validate-layer1"]) == ledger.EXIT_OK
    assert ledger.main([*common, "--stage", "dev-rest"]) == ledger.EXIT_REFUSED


def test_independent_rescorings_of_one_run_are_separate_ledger_entries(runs, tmp_path, monkeypatch):
    import shutil

    repo, structured_dir, _free, sample_config, _raw = runs
    config = lj.RescoreConfig.load(sample_config)
    copy = tmp_path / "copy" / structured_dir.name
    shutil.copytree(structured_dir, copy)
    for minute, run_dir in enumerate((structured_dir, copy)):  # two independent judge sessions of the same run
        monkeypatch.setattr(lj, "utc_now_iso", lambda minute=minute: f"2026-11-22T1{minute}:00:00+00:00")
        lj.rescore(config, [lj.load_run(run_dir, repo_root=repo)], repo_root=repo, confirm_spend=True, log=lambda m: None)
    (first,) = ledger.judge_scores_spend(structured_dir, repo)
    (second,) = ledger.judge_scores_spend(copy, repo)
    assert first.key != second.key and first.key.rsplit(":", 1)[0] == second.key.rsplit(":", 1)[0]
    merged = ledger.merge([first, second])
    assert sum(e.usd for e in merged.values()) == pytest.approx(first.usd + second.usd)
    # a copy of the SAME score file keeps the key: counted once
    shutil.copy(structured_dir / lj.SCORES_NAME, copy / lj.SCORES_NAME)
    (again,) = ledger.judge_scores_spend(copy, repo)
    assert again.key == first.key


# --------------------------------------------------------------------------- #
# The layer-2 stop rule (section 10, layer 2)
# --------------------------------------------------------------------------- #


def sec_record(model: str, company: int, stratum: str, label: str | None) -> TraceRecord:
    common = dict(run_id="run-sec", config_hash="a" * 64, git_commit=b.COMMIT, item_id=f"sec/{company}/{stratum}",
                  group_id=f"sec/{company}", dataset="sec_financials", split=Split.DEV, arm="no_gate", model=model,
                  repeat=0, started_at=b.T0, ended_at=b.T1, stratum=stratum)
    if label is None:
        return TraceRecord(**common, error="ModelCallFailed: HTTP 500")
    answer = AnswerCandidate.answered(value="$1.2 billion", quote="a b c d e f", source_id="doc1", raw="{}")
    return TraceRecord(**common, attempts=(Attempt(answer=answer),), final_answer=answer,
                       scores=(ScoreResult("sec_answer", label, {}),))


def sec_records(model: str, wrong_entity: int, period: int, unmeasured: int = 0, n: int = 100) -> list[TraceRecord]:
    """``n`` companies x 3 conditions; ``wrong_entity`` wrong-entity answers among the peer-shown ones, ``period``
    wrong-period answers and ``unmeasured`` other_wrong answers anywhere."""
    out = []
    labels = iter(["wrong_entity"] * wrong_entity)
    period_labels = iter(["wrong_period_or_scale"] * period)
    other = iter(["other_wrong"] * unmeasured)
    for company in range(n):
        for stratum in ("present/none", "present/peer", "absent/peer"):
            if stratum != "present/none":
                label = next(labels, None) or next(period_labels, None) or next(other, None) or "correct"
            else:
                label = next(period_labels, None) or next(other, None) or "correct"
            out.append(sec_record(model, company, stratum, label))
    return out


def test_layer2_rule_mirrors_section_10():
    # R2E = wrong entity / 200 peer-shown records; R2P = wrong period or scale / 300 records
    rows = {r["model"]: r for r in layer2_stop.model_rows(
        sec_records("a", 20, 0) + sec_records("b", 0, 30) + sec_records("c", 9, 14) + sec_records("d", 2, 3))}
    assert rows["a"]["R2E"] == 0.10 and rows["a"]["decision"] == "CONTINUE"
    assert rows["b"]["R2P"] == 0.10 and rows["b"]["decision"] == "CONTINUE"
    assert rows["c"]["R2E"] == 0.045 and rows["c"]["R2P"] == pytest.approx(14 / 300) and rows["c"]["decision"] == "STOP"
    assert rows["d"]["decision"] == "STOP"
    assert layer2_stop.overall(list(rows.values())) == "CONTINUE"
    assert layer2_stop.overall([rows["c"], rows["d"]]) == "STOP"
    between = layer2_stop.model_rows(sec_records("e", 10, 0))
    assert between[0]["decision"] == "IN-BETWEEN" and layer2_stop.overall(between) == "IN-BETWEEN"
    check = layer2_stop.model_rows(sec_records("f", 0, 0, unmeasured=61))
    assert check[0]["measurement_check"] and layer2_stop.overall(check) == "MEASUREMENT-CHECK"
    # errors stay in the denominators
    with_errors = sec_records("g", 20, 0)
    with_errors[0] = sec_record("g", 0, "present/none", None)
    assert layer2_stop.model_rows(with_errors)[0]["labels_all"]["error"] == 1


def test_layer2_gate_uses_its_own_continue_note(tmp_path):
    decision = {"layer": 2, "workflow_run_id": 9, "decision": "IN-BETWEEN", "provisional": False}
    allowed, why = layer2_stop.gate(decision, notes_dir=tmp_path)
    assert not allowed and "dev_sec_baseline_continue.json" in why and "the rest of layer 2" in why
    (tmp_path / "dev_sec_baseline_continue.json").write_text(json.dumps(
        {"workflow_run_id": 9, "decision": "IN-BETWEEN", "continue": True, "reason": "r", "amendment": "18, 2"}), encoding="utf-8")
    assert layer2_stop.gate(decision, notes_dir=tmp_path)[0]
    assert layer2_stop.gate({**decision, "decision": "CONTINUE"}, notes_dir=tmp_path / "none")[0]
    assert not layer2_stop.gate({**decision, "decision": "STOP"}, notes_dir=tmp_path)[0]
    assert not layer2_stop.gate({**decision, "layer": 1, "decision": "CONTINUE"}, notes_dir=tmp_path)[0]
    # the layer-1 dev-rest gate is unchanged and does not read the layer-2 note
    assert not pipeline.dev_rest_gate({"workflow_run_id": 9, "decision": "IN-BETWEEN"}, notes_dir=tmp_path)[0]


def test_layer2_decision_files_and_commands(tmp_path, capsys):
    from binding_gate.canonical import sha256_json
    from binding_gate.contracts import RunManifest
    from binding_gate.trace import MANIFEST_NAME, TRACES_NAME

    run_dir = tmp_path / "run-sec"
    run_dir.mkdir()
    config = {"name": "dev-sec-baseline", "dataset": {"name": "sec_financials", "options": {}}}
    records = sec_records("openai-low", 25, 0, n=50)
    (run_dir / TRACES_NAME).write_text("".join(r.to_json() + "\n" for r in records), encoding="utf-8")
    manifest = RunManifest(run_id="run-sec", config=config, config_hash=sha256_json(config), git_commit=b.COMMIT,
                           git_dirty=False, package_version="0", python_version="3", started_at=b.T0, ended_at=b.T1)
    (run_dir / MANIFEST_NAME).write_text(manifest.to_json(), encoding="utf-8")
    out = tmp_path / "decision.json"
    assert pipeline.main(["layer2-decision", str(run_dir), "--workflow-run-id", "11", "--json", str(out),
                          "--markdown", str(tmp_path / "decision.md")]) == 0
    decision = json.loads(out.read_text(encoding="utf-8"))
    assert decision["decision"] == "CONTINUE" and decision["layer"] == 2 and decision["workflow_run_id"] == 11
    assert results_log.refusal_reason("layer2-dev--decision/decision.json", out.read_bytes()) is None
    assert pipeline.main(["layer2-gate", "--decision", str(out), "--notes-dir", str(tmp_path)]) == 0
    assert "allowed=true" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# The amendment and the documentation
# --------------------------------------------------------------------------- #


def test_the_amendment_is_recorded_as_post_hoc_and_changes_no_threshold():
    from binding_gate.report import prereg_doc

    text = (REPO_ROOT / "PREREGISTRATION.md").read_text(encoding="utf-8")
    assert prereg_doc.status(text) == "DRAFT" and prereg_doc.version(text).startswith("0.10 ")
    (row,) = prereg_doc.amendments(text)
    assert "AFTER seeing the layer-1 DEV baseline" in row["reason"] and "45053502161" in row["reason"]
    assert "test-split work for layer 1 remains stopped" in row["reason"] and row["test results seen before the change?"].startswith("No")
    section10 = prereg_doc.section(text, 10)
    for fact in ("### Layer 1 (the PR 2 stop rule)", "### Layer 2 (version 0.10, amendment 1", "R2E", "R2P",
                 "R = 2.0% (A", "12 items per condition", "free_form", "exploratory and post hoc"):
        assert fact in section10, fact
    # the layer-1 thresholds are untouched and the layer-2 rule uses the same numbers
    assert f"R ≥ {baseline_report.CONTINUE_PERCENT}%" in section10 and f"R < {baseline_report.CARRY_PERCENT}%" in section10
    assert "R2E ≥ 10% or R2P ≥ 10%" in section10 and "below 5% on both" in section10
    titles = [title for title, _ in prereg_doc.threats(text)]
    assert "The layer-1 stop metric may undercount (version 0.10, amendment 1)" in titles
    assert "The STOP was measured after a strong mitigation (version 0.10, amendment 1)" in titles
    assert any(t.startswith("Decisions taken after seeing the layer-1 baseline") for t in titles)
    section17 = prereg_doc.section(text, 17)
    for item in ("58. ", "59. ", "60. ", "61. ", "62. ", "*Closed in version 0.10 (amendment 1)"):
        assert item in section17, item


def test_the_running_guide_documents_the_stage_in_both_languages():
    running = (REPO_ROOT / "docs" / "RUNNING.md").read_text(encoding="utf-8")
    english, hebrew = running.split("## הוראות הפעלה בעברית", 1)
    row = "| Stage of the dev pipeline (docs/OWNER_SETUP.md) | `validate-layer1` |"
    for part in (english, hebrew):
        assert row in part and "| Use workflow from (Branch) | `main` |" in part
        assert "validation_report.json" in part and "configs/dev_validation_freeform.toml" in part
    assert "## 4a. After a layer-1 STOP" in english and "### שלב 4א" in hebrew
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Layer-1 baseline: STOP; validation in progress." in readme
    architecture = (REPO_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "`free_form` (PR 22, exploratory" in architecture and "PR 22 changed no contract" in architecture
