"""Secondary scorer llm_judge: benchmark prompt and validation, labels, agreement, re-scoring of traces."""

from __future__ import annotations

import json

import pytest

import dg_fixture
import pr5_fakes as f
import run_helpers as h
from binding_gate import judging, runner
from binding_gate.contracts import AnswerCandidate
from binding_gate.datasets.deceptive_grounding import items_from_triple
from binding_gate.scorers import llm_judge as lj
from binding_gate.trace import read_traces


@pytest.fixture(autouse=True)
def _fresh():
    judging.reset_shared_state()
    f.JUDGE_CALLS.clear()
    yield
    judging.reset_shared_state()
    f.JUDGE_CALLS.clear()


def items():
    return {item.stratum: item for item in items_from_triple(dg_fixture.triple(1))}


def judge(behaviour="bench_auto", label="judge-a", provider="moonshot", **options):
    return {"name": "pr5_fake_judge", "label": label, "options": {"behaviour": behaviour, "provider": provider, **options}}


@pytest.fixture
def prices(tmp_path) -> str:
    path = tmp_path / "prices.toml"
    path.write_text(h.PRICES + f.FAKE_JUDGE_PRICE, encoding="utf-8")
    return path.as_posix()


def scorer(prices, behaviour="bench_auto", family="openai", **options):
    return lj.LlmJudgeScorer(judges=[judge(behaviour, **options)], model_family=family, max_usd=10.0, prices=prices)


def answered(source_id="cy_doc"):
    raw = json.dumps({"status": "answered", "value": "v", "quote": "q", "source_id": source_id})
    return AnswerCandidate.answered(value="v", quote="q", source_id=source_id, raw=raw)


# -- the benchmark's prompt -------------------------------------------------------- #


def test_prompt_labels_documents_by_drug_as_the_benchmark_does():
    item = items()["complete/prior_completing"]
    prompt = lj.build_judge_prompt(**lj.judge_inputs(item), response="RESPONSE")
    gold = item.gold
    assert prompt.startswith(f"[DOCUMENT 1 — about drug {gold['X']}]: {item.documents[0].text}\n\n")
    assert f"[DOCUMENT 2 — about drug {gold['Y']}]: {item.documents[1].text}" in prompt
    assert f"Query: {item.question}\n\nResponse: RESPONSE\n" in prompt
    assert f"STEP 1 — Claims about {gold['X']}:" in prompt and prompt.rstrip().endswith("}")


def test_absent_x_document_and_synthetic_competitor_name():
    absent = lj.build_judge_prompt(**lj.judge_inputs(items()["absent/synthetic_y"]), response="r")
    gold = items()["absent/synthetic_y"].gold
    assert absent.startswith(f"[DOCUMENT 1 — about drug {gold['X']}]: ABSENT\n")
    assert f"[DOCUMENT 2 — about drug {gold['Y_synthetic']}]:" in absent
    assert f"instead of answering about {gold['X']}" in absent and gold["Y"] + "]" not in absent


def test_other_datasets_are_refused():
    import builders as b

    with pytest.raises(ValueError, match="Deceptive Grounding"):
        lj.judge_inputs(b.item())


# -- validation and parsing, as in the benchmark ------------------------------------ #


def test_validation_and_labels():
    assert lj.label_from_judgment(lj.validate_judgment(f.benchmark_judgment("Document 2"))) == lj.LABEL_EAF_OR_SUBSTITUTION
    assert lj.label_from_judgment(f.benchmark_judgment("Document 1", substitution=True)) == lj.LABEL_EAF_OR_SUBSTITUTION
    assert lj.label_from_judgment(f.benchmark_judgment("Neither")) == lj.LABEL_CONFABULATION_ONLY
    assert lj.label_from_judgment(f.benchmark_judgment("Document 1")) == lj.LABEL_CORRECT_ATTRIBUTION
    assert lj.label_from_judgment(f.benchmark_judgment(None)) == lj.LABEL_NO_CLAIMS


@pytest.mark.parametrize(
    "change",
    [
        {"failure_count": 0},
        {"failure_detected": 1},
        {"entity_substitution_count": 1},
        {"claims": [{"quote": "x", "source": "Document 2", "label": "confabulation"}]},
        {"claims": [{"quote": " ", "source": "Document 2", "label": "entity_attribution_failure"}]},
        {"confabulation_count": -1},
    ],
)
def test_inconsistent_judgments_are_rejected(change):
    with pytest.raises(ValueError):
        lj.validate_judgment({**f.benchmark_judgment("Document 2"), **change})


def test_parser_prefers_the_fenced_block_then_the_outer_braces():
    good = json.dumps(f.benchmark_judgment("Document 1"))
    judgment, error = lj.parse_judgment(f"thinking {{not json}}\n```json\n{good}\n```")
    assert error is None and judgment["claims"][0]["source"] == "Document 1"
    judgment, error = lj.parse_judgment(f"Result: {good} done")
    assert error is None
    assert lj.parse_judgment("no json at all") == (None, "no JSON object in the judge reply")
    assert lj.parse_judgment("")[0] is None


# -- the scorer ---------------------------------------------------------------------- #


def test_answered_final_answer_is_judged_and_every_call_is_kept(prices):
    result = scorer(prices).score(items()["complete/prior_completing"], answered("cy_doc"))
    assert result.scorer == "llm_judge" and result.label == lj.LABEL_EAF_OR_SUBSTITUTION
    (call,) = result.details["model_calls"]
    assert call["purpose"] == "judge" and call["request"]["messages"][0]["content"] == lj.JUDGE_SYSTEM_PROMPT
    assert result.details["prompt_fingerprint"] == lj.PROMPT_FINGERPRINT


def test_abstentions_and_empty_outputs_are_not_sent(prices):
    s = scorer(prices)
    item = items()["complete/prior_completing"]
    assert s.score(item, AnswerCandidate.abstained(raw="{}")).label == lj.LABEL_ABSTAINED
    assert s.score(item, AnswerCandidate.unparseable(raw="<think>x</think> ", reason="r")).label == lj.LABEL_EMPTY_OUTPUT
    assert f.JUDGE_CALLS == []


def test_unparseable_final_answers_with_text_are_judged(prices):
    raw = "Yoladrug improved twelve patients [cy_doc]."
    result = scorer(prices).score(items()["complete/prior_completing"], AnswerCandidate.unparseable(raw=raw, reason="r"))
    assert result.label == lj.LABEL_EAF_OR_SUBSTITUTION and len(f.JUDGE_CALLS) == 1


@pytest.mark.parametrize(
    "options,label",
    [({"behaviour": "bench_bad"}, lj.LABEL_JUDGE_UNPARSEABLE), ({"finish_reason": "length"}, lj.LABEL_JUDGE_UNPARSEABLE),
     ({"behaviour": "crash"}, lj.LABEL_JUDGE_ERROR)],
)
def test_judge_failures_are_labelled_not_dropped(prices, options, label):
    spec = judge(**{"behaviour": "bench_auto", **options})
    s = lj.LlmJudgeScorer(judges=[spec], model_family="openai", max_usd=10.0, prices=prices)
    result = s.score(items()["complete/prior_completing"], answered())
    assert result.label == label and result.details["model_calls"]


def test_family_rule_is_enforced_when_the_scorer_is_built(prices):
    with pytest.raises(judging.JudgeFamilyError):
        scorer(prices, family="moonshot")


# -- agreement ----------------------------------------------------------------------- #


def test_cohen_kappa_known_values():
    pairs = [("a", "a")] * 20 + [("a", "b")] * 5 + [("b", "a")] * 10 + [("b", "b")] * 15
    assert lj.cohen_kappa(pairs) == pytest.approx(0.4)
    assert lj.cohen_kappa([("a", "a"), ("b", "b")]) == pytest.approx(1.0)
    assert lj.cohen_kappa([("a", "a"), ("a", "a")]) is None  # chance agreement is 1: undefined
    assert lj.cohen_kappa([]) is None


# -- end to end: run, re-score, agreement ------------------------------------------- #


@pytest.fixture
def baseline_run(tmp_path):
    """A no_gate dev run on the synthetic checkout with one honest and one wrong-entity model."""
    models = [
        {"name": "pr5_family_fake", "label": "good", "options": {"model_id": "fake-cheap", "behaviour": "target", "provider": "openai"}},
        {"name": "pr5_family_fake", "label": "bad", "options": {"model_id": "fake-cheap", "behaviour": "competitor", "provider": "google"}},
    ]
    repo, config, _raw = h.setup(tmp_path, models=models, repeats=1)
    (repo / "configs" / "prices.toml").write_text(h.PRICES + f.FAKE_JUDGE_PRICE, encoding="utf-8")
    summary = runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "runs", confirm_spend=True, log=lambda m: None)
    return repo, summary.run_dir


def rescore_config(behaviour="bench_auto", max_usd=10.0, **judge_options):
    return lj.RescoreConfig.parse(
        {"rescore_version": 1, "name": "t", "scorer": "llm_judge", "max_usd": max_usd, "concurrency": 3,
         "judges": [judge(behaviour, **judge_options)]}
    )


def test_rescoring_writes_scores_beside_the_trace_and_agrees_with_provenance(baseline_run):
    repo, run_dir = baseline_run
    run = lj.load_run(run_dir, repo_root=repo)
    config = rescore_config()
    (estimate,) = lj.estimate(config, [run], repo_root=repo)
    # good answers 10 of 15 conditions per triple and abstains in the 5 absent ones; bad always answers.
    assert estimate.calls == 3 * 10 + 3 * 15 and estimate.static_labels == {"abstained": 15}
    assert estimate.cost_low > 0 and f.JUDGE_CALLS == []
    trace_before = (run_dir / "traces.jsonl").read_bytes()
    with pytest.raises(lj.RescoreError, match="confirm-spend"):
        lj.rescore(config, [run], repo_root=repo, confirm_spend=False)
    summary = lj.rescore(config, [run], repo_root=repo, confirm_spend=True, log=lambda m: None)
    assert summary.judge_calls == 75 and summary.lines_written == 90 and not summary.budget_exceeded
    assert (run_dir / "traces.jsonl").read_bytes() == trace_before
    scores = lj.read_scores(run_dir)
    assert len(scores) == 90 and {e["judge_hash"] for e in scores.values()} == {config.judge_hash}
    rows = {r["model"]: r for r in lj.agreement_rows(list(read_traces(run_dir)), scores)}
    assert rows["bad"]["raw_agreement"] == 1.0 and rows["bad"]["both_positive"] == 45
    assert rows["good"]["raw_agreement"] == 1.0 and rows["good"]["judged_population"] == 30
    assert rows["bad"]["judge_rate_non_null_control"] == rows["bad"]["R_non_null_control"] == 1.0
    # Resuming scores nothing twice.
    again = lj.rescore(config, [run], repo_root=repo, confirm_spend=True, log=lambda m: None)
    assert again.lines_written == 0 and len(f.JUDGE_CALLS) == 75


def test_judge_failures_count_as_disagreement(baseline_run):
    repo, run_dir = baseline_run
    run = lj.load_run(run_dir, repo_root=repo)
    lj.rescore(rescore_config("bench_bad"), [run], repo_root=repo, confirm_spend=True, log=lambda m: None)
    rows = {r["model"]: r for r in lj.agreement_from_run_dirs([run_dir])}
    bad = rows["bad"]
    assert bad["judge_failures"] == 45 and bad["judged_population"] == 45
    assert bad["raw_agreement"] == 0.0 and bad["kappa_parsed_only"] is None
    assert "wrong_entity | judge_unparseable" in bad["crosstab"]


def test_family_clash_refuses_before_any_call(baseline_run):
    repo, run_dir = baseline_run
    run = lj.load_run(run_dir, repo_root=repo)
    with pytest.raises(lj.RescoreError, match="family"):
        lj.rescore(rescore_config(provider="google"), [run], repo_root=repo, confirm_spend=True, log=lambda m: None)
    assert f.JUDGE_CALLS == []


def test_budget_cap_stops_the_rescoring_and_leaves_it_resumable(baseline_run):
    repo, run_dir = baseline_run
    run = lj.load_run(run_dir, repo_root=repo)
    per_call = (2000 * 2.0 + 300 * 10.0) / 1_000_000
    config = rescore_config(max_usd=per_call * 4.5)
    with pytest.raises(lj.RescoreError, match="low estimate"):
        lj.rescore(config, [run], repo_root=repo, confirm_spend=True, log=lambda m: None)
    # A cap above the low estimate but below the real cost (the fake reports more tokens than assumed).
    tight = lj.RescoreConfig.parse(
        {"rescore_version": 1, "name": "t", "scorer": "llm_judge", "max_usd": 0.5, "concurrency": 1,
         "judges": [judge(input_tokens=200000)]}
    )
    summary = lj.rescore(tight, [run], repo_root=repo, confirm_spend=True, log=lambda m: None)
    assert summary.budget_exceeded and summary.judge_calls == 2  # 0.40 USD each: the second reaches the cap
    with pytest.raises(lj.RescoreError, match="different judge configuration"):
        lj.rescore(rescore_config(), [run], repo_root=repo, confirm_spend=True, log=lambda m: None)


def test_command_line(baseline_run, tmp_path, capsys):
    repo, run_dir = baseline_run
    config_path = tmp_path / "rescore.toml"
    config_path.write_text(
        'rescore_version = 1\nname = "t"\nscorer = "llm_judge"\nmax_usd = 10.0\n'
        '[[judges]]\nname = "pr5_fake_judge"\nlabel = "judge-a"\n[judges.options]\nbehaviour = "bench_auto"\n',
        encoding="utf-8",
    )
    common = ["--config", str(config_path), "--repo-root", str(repo), str(run_dir)]
    assert lj.main(["dry-run", *common]) == 0 and "DRY RUN" in capsys.readouterr().out
    assert lj.main(["run", *common]) == 1 and f.JUDGE_CALLS == []
    assert lj.main(["run", "--confirm-spend", *common]) == 0
    out_json = tmp_path / "agreement.json"
    assert lj.main(["agreement", str(run_dir), "--json", str(out_json)]) == 0
    assert "LLM-JUDGE CROSS-CHECK" in capsys.readouterr().out
    assert {row["model"] for row in json.loads(out_json.read_text())} == {"good", "bad"}


def test_rescore_config_is_validated():
    base = {"rescore_version": 1, "name": "t", "scorer": "llm_judge", "max_usd": 1.0, "judges": [judge()]}
    lj.RescoreConfig.parse(base)
    for change in ({"max_usd": 0}, {"scorer": "provenance"}, {"judges": []}, {"surprise": 1}, {"rescore_version": 2}):
        with pytest.raises(lj.RescoreError):
            lj.RescoreConfig.parse({**base, **change})


def test_module_command_runs_in_a_fresh_process(baseline_run, tmp_path):
    """``python -m`` must not register the scorer twice (see the decorator comment in the module)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    _repo, run_dir = baseline_run
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "binding_gate.scorers.llm_judge", "agreement", str(run_dir)],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "not yet scored 45" in result.stdout
