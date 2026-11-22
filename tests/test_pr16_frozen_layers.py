"""PR 16: frozen test runs for layers 2 and 3, one analysis rule per layer, and published results.

Section 17 items 37 and 39 of the pre-registration. Every frozen run here works in a temporary
git repository with its own audit log, on synthetic datasets (invented SealQA rows, the invented
GRAB-RAG stand-in) and a fake web; nothing touches the network or the real test split, and the
repository's audit/test_access_log.jsonl is never written (checked at the end of this file).
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

import builders as b
import dg_fixture
import grab_rag_fixture as gfx
import live_fakes as lf
import run_helpers as h
import sealqa_fixture as sfx
from binding_gate import cli, frozen_run, runner
from binding_gate.arms.gated import clear_decomposition_memo
from binding_gate.config import ComponentSpec, load_config
from binding_gate.contracts import (
    AnswerCandidate,
    ArmOutput,
    Attempt,
    CheckResult,
    CheckStatus,
    Decision,
    ModelRequest,
    ModelResponse,
    Split,
    TokenUsage,
    Verdict,
)
from binding_gate.datasets.sealqa import group_id_of
from binding_gate.models._http import PreflightResult, RetryPolicy
from binding_gate.registry import create, register
from binding_gate.report import __main__ as report_cli
from binding_gate.report.publish import RESULTS_MANIFEST, PublishError, publish_run, refusal_reason
from binding_gate.research.session import clear_sessions
from binding_gate.split import assign_split
from binding_gate.stats import prereg
from binding_gate.stats.analysis import analyse_run_dirs
from binding_gate.stats.layers import LayerError, analyse_layer, layer_of_run_dirs
from binding_gate.testlock import ACCESS_LOG_RELPATH, EVENT_LOADED, EVENT_REFUSED, TestSplitLocked, read_access_log
from binding_gate.trace import read_manifest, read_traces

REAL_REPO = Path(__file__).resolve().parents[1]
CONFIGS = REAL_REPO / "configs"
TAG = "prereg-freeze-3"

# --------------------------------------------------------------------------- #
# The committed test configs of layers 2 and 3
# --------------------------------------------------------------------------- #

PAIRS = [
    ("test_live.toml", "dev_live.toml", {"max_usd", "max_usd_by_model", "max_searches"}),
    ("test_grab_rag_supportive.toml", "dev_grab_rag_supportive.toml", {"max_usd", "max_usd_by_model"}),
]
SPLIT_DEPENDENT_TOP = {"name", "description", "split"}


@pytest.mark.parametrize(("test_name", "dev_name", "runner_keys"), PAIRS, ids=lambda v: str(v))
def test_each_test_config_is_its_dev_counterpart_except_split_dependent_fields(test_name, dev_name, runner_keys):
    test, dev = load_config(CONFIGS / test_name).to_dict(), load_config(CONFIGS / dev_name).to_dict()
    assert test["split"] == "test" and dev["split"] == "dev"
    strip = lambda raw: {k: v for k, v in raw.items() if k not in SPLIT_DEPENDENT_TOP | {"runner"}}  # noqa: E731
    assert strip(test) == strip(dev)  # models, dataset pin, every arm option, scorers, repeats, limit
    runner_strip = lambda raw: {k: v for k, v in raw.get("runner", {}).items() if k not in runner_keys}  # noqa: E731
    assert runner_strip(test) == runner_strip(dev)  # concurrency and price file
    assert load_config(CONFIGS / test_name).repeats == prereg.REPEATS and test.get("limit") is None
    assert "sample" not in test and not test.get("runner", {}).get("seed_responses_from")


def test_test_live_runs_slots_a_and_e_with_the_planned_caps_and_a_month_of_searches():
    config = load_config(CONFIGS / "test_live.toml")
    settings = runner.RunnerSettings.from_config(config)
    assert [m.label for m in config.models] == ["openai-low", "google-low"]  # section 11, layer 3: A and E
    assert [(a.name, a.label) for a in config.arms] == [("no_gate_live", "no_gate"), ("gated_research", "gated_research")]
    assert [s.name for s in config.scorers] == ["sealqa_answer"]
    caps = {m.label: settings.cap_for(m.label) for m in config.models}
    assert caps == {"openai-low": 0.55, "google-low": 1.25}
    assert settings.max_searches == 250  # SerpApi Free plan, month 2; resumed in month 3


def test_test_grab_rag_runs_slot_a_with_no_gate_and_gated_and_the_planned_cap():
    config = load_config(CONFIGS / "test_grab_rag_supportive.toml")
    settings = runner.RunnerSettings.from_config(config)
    assert [m.label for m in config.models] == ["openai-low"]
    assert [a.label for a in config.arms] == ["no_gate", "gated"]  # the gated arm: the cost side (M2)
    assert config.dataset.options["conditions"] == ["supportive"]
    assert {s.name for s in config.scorers} == {"provenance", "grab_rag_answer"}
    assert settings.cap_for("openai-low") == 0.6


@pytest.mark.parametrize("name", ["test_live.toml", "test_grab_rag_supportive.toml", "test_layer1.toml"])
def test_committed_test_configs_pass_every_frozen_run_check_that_does_not_depend_on_git(name):
    config = load_config(CONFIGS / name)
    reasons = frozen_run.config_refusals(config, CONFIGS / name, REAL_REPO, REAL_REPO / "runs")
    assert not [r for r in reasons if "tracked" not in r], reasons


def test_config_check_refuses_a_layer_without_its_rule_scorer_or_an_unknown_dataset():
    config = load_config(CONFIGS / "test_live.toml")
    path = CONFIGS / "test_live.toml"
    no_scorer = dataclasses.replace(config, scorers=(ComponentSpec("provenance", {}),))
    assert any("'sealqa_answer' score" in r for r in frozen_run.config_refusals(no_scorer, path, REAL_REPO, REAL_REPO / "runs"))
    layer2 = load_config(CONFIGS / "test_grab_rag_supportive.toml")
    no_scorer2 = dataclasses.replace(layer2, scorers=(ComponentSpec("provenance", {}),))
    assert any("'grab_rag_answer' score" in r for r in frozen_run.config_refusals(no_scorer2, path, REAL_REPO, REAL_REPO / "runs"))
    other = dataclasses.replace(config, dataset=ComponentSpec("some_other_dataset", {}))
    assert any("no pre-registered layer" in r for r in frozen_run.config_refusals(other, path, REAL_REPO, REAL_REPO / "runs"))


# --------------------------------------------------------------------------- #
# Synthetic frozen repositories
# --------------------------------------------------------------------------- #


def to_toml(raw: dict) -> str:
    """Any JSON-shaped config as TOML (inline tables and arrays; every key quoted)."""

    def value(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float, str)):
            return json.dumps(v)
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        return "{" + ", ".join(f"{json.dumps(k)} = {value(x)}" for k, x in v.items()) + "}"

    return "".join(f"{json.dumps(k)} = {value(v)}\n" for k, v in raw.items())


def frozen_repo(tmp_path: Path, raw: dict, *, prices: str = h.PRICES) -> Path:
    repo = h.make_repo(tmp_path, prereg_status="FROZEN")
    (repo / "configs" / "prices.toml").write_text(prices, encoding="utf-8")
    (repo / "configs" / "test_synth.toml").write_text(to_toml(raw), encoding="utf-8")
    (repo / ".gitignore").write_text("runs/\n", encoding="utf-8")
    b.git(repo, "add", "-A")
    b.git(repo, "commit", "-q", "-m", "freeze")
    b.git(repo, "tag", TAG)
    return repo


def run_frozen(repo: Path, **kwargs):
    kwargs.setdefault("tag", TAG)
    kwargs.setdefault("purpose", "unit test of a frozen layer run")
    kwargs.setdefault("confirm_spend", True)
    kwargs.setdefault("log", lambda message: None)
    kwargs.setdefault("figures", False)
    return frozen_run.frozen_test_run("configs/test_synth.toml", repo_root=repo, **kwargs)


def events(repo: Path) -> list[str]:
    return [entry["event"] for entry in read_access_log(repo / ACCESS_LOG_RELPATH)]


# -- layer 3 ------------------------------------------------------------------------- #

LIVE_PRICES = h.PRICES + """
[[price]]
model_id = "serpapi/google"
input_per_mtok = 0.0
output_per_mtok = 0.0
per_call_usd = 0.0
source = "test"
accessed = "2026-11-22"
[[price]]
model_id = "binding-gate/fetch@1"
input_per_mtok = 0.0
output_per_mtok = 0.0
source = "test"
accessed = "2026-11-22"
"""
PAGE_TRIAL = "https://trial.test/games"


def find_test_split_question(template: str, start: int = 2030) -> tuple[str, int]:
    """The first invented "Which city hosted the <year> ...?" question that falls in the test split."""
    for year in range(start, start + 400):
        question = template.format(year=year)
        if assign_split("sealqa", group_id_of(question)) is Split.TEST:
            return question, year
    raise AssertionError("no test-split question found")  # pragma: no cover


@pytest.fixture
def live_world(tmp_path, monkeypatch):
    clear_sessions()
    clear_decomposition_memo()
    lf.MODEL_CALLS.clear()
    lf.DECOMPOSE_CALLS.clear()
    web, serp = lf.install(monkeypatch)
    # A test-split question whose first search shows the Sample Games page (wrong entity) and whose
    # targeted re-search finds the right page: no_gate is incorrect, gated_research recovers (M4).
    q_trial, year = find_test_split_question("Which city hosted the {year} Trial Games?")
    serp.results[q_trial] = lf.organic(lf.PAGE_A)
    serp.results[f'"Trial Games" {year} host city'] = lf.organic(PAGE_TRIAL)
    web.pages[PAGE_TRIAL] = (200, {"Content-Type": "text/html"},
                             f"<p>The Trial Games {year} were hosted by Eastham after a long bid.</p>".encode())
    web.pages["https://trial.test/robots.txt"] = (404, {}, b"")
    assert assign_split("sealqa", group_id_of(lf.Q_SAMPLE)) is Split.TEST and assign_split("sealqa", group_id_of(lf.Q_EXAMPLE)) is Split.DEV
    hard = [
        sfx.row(0, question=lf.Q_EXAMPLE, answer="Northbridge"),  # dev: never run here
        sfx.row(1, question=lf.Q_SAMPLE, answer="Southport"),  # test: accepted on the first attempt
        sfx.row(2, question=q_trial, answer="Eastham"),  # test: RESEARCH, then correct
    ]
    options = sfx.write(tmp_path / "sealqa", monkeypatch, hard, [dict(hard[0])])
    raw = {
        "config_version": 1,
        "name": "frozen-live-test",
        "split": "test",
        "repeats": prereg.REPEATS,
        "dataset": {"name": "sealqa", "options": options},
        "models": [{"name": "fake_live_model", "label": "good", "options": {"model_id": "fake-cheap"}}],
        "arms": lf.arm_specs(),
        "scorers": [{"name": "sealqa_answer"}],
        "runner": {"concurrency": 2, "max_usd": 5.0, "max_searches": 50},
    }
    yield raw, web, serp
    clear_sessions()
    clear_decomposition_memo()


def test_layer3_frozen_run_is_refused_while_a_draft_and_logged(tmp_path, live_world):
    raw, _web, serp = live_world
    repo = h.make_repo(tmp_path, prereg_status="DRAFT")
    (repo / "configs" / "prices.toml").write_text(LIVE_PRICES, encoding="utf-8")
    (repo / "configs" / "test_synth.toml").write_text(to_toml(raw), encoding="utf-8")
    (repo / ".gitignore").write_text("runs/\n", encoding="utf-8")
    b.git(repo, "add", "-A")
    b.git(repo, "commit", "-q", "-m", "draft")
    b.git(repo, "tag", TAG)
    with pytest.raises(TestSplitLocked, match="not 'FROZEN'"):
        run_frozen(repo)
    assert events(repo) == [EVENT_REFUSED]
    assert serp.queries == [] and lf.MODEL_CALLS == [] and not (repo / "runs").exists()


def test_layer3_frozen_run_analyses_with_the_sealqa_rule_and_decides_h4(tmp_path, live_world, capsys):
    raw, _web, serp = live_world
    repo = frozen_repo(tmp_path, raw, prices=LIVE_PRICES)
    result = run_frozen(repo)
    assert events(repo) == [EVENT_LOADED] and not result.aborted
    loaded = read_access_log(repo / ACCESS_LOG_RELPATH)[-1]
    assert loaded["datasets"] == ["sealqa"] and loaded["n_items"] == 2
    assert lf.Q_EXAMPLE not in serp.queries  # the dev question is never run in a test run
    records = list(read_traces(result.run_dir))
    assert {r.split for r in records} == {Split.TEST} and len(records) == 2 * 2

    analysis = json.loads((result.analysis_dir / "analysis.json").read_text(encoding="utf-8"))
    assert analysis["layer"] == 3 and analysis["split"] == "test"
    assert analysis["outcome_rule"]["scorer"] == "sealqa_answer"
    assert analysis["parameters"]["correct_rule_is_placeholder"] is False
    good = analysis["models"]["good"]
    est = good["estimates"]
    assert est["arm/no_gate/correct_all"]["value"] == 0.5  # Sample correct, Trial incorrect (wrong page)
    assert est["arm/gated_research/correct_all"]["value"] == 1.0  # the re-search recovered Trial
    assert est["arm/gated_research/m4"]["value"] == 1.0  # M4: 1 of 1 RESEARCH first attempts ended correct
    h4 = good["hypotheses"]["H4"]
    assert h4["status"] == "SUPPORTED" and h4["confirmatory"] is True, h4
    for name in ("H1", "H2", "H3"):
        assert good["hypotheses"][name]["status"] == "NOT EVALUABLE"
    assert analysis["summary"]["H4"]["supported_confirmatory"] == ["good"]

    record = json.loads((result.run_dir / frozen_run.FROZEN_RUN_LOG).read_text(encoding="utf-8").splitlines()[-1])
    assert record["layer"] == 3 and record["outcome_rule"] == "sealqa_answer (layer 3)"
    assert record["decision"].startswith("H4") and record["analysis"]["summary"]["H4"]["supported"] == ["good"]

    # cli analyze on the same run directory gives the same numbers with the same rule.
    capsys.readouterr()
    out = tmp_path / "reanalysis"
    assert cli.main(["analyze", str(result.run_dir), "--out", str(out), "--no-figures"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["outcome_rule"] == "sealqa_answer (layer 3)"
    assert json.loads((out / "analysis.json").read_text(encoding="utf-8"))["models"] == analysis["models"]


def test_layer3_h4_is_not_evaluable_without_the_research_arm(tmp_path, live_world):
    raw, _web, _serp = live_world
    raw = {**raw, "arms": lf.arm_specs()[:1]}
    repo = frozen_repo(tmp_path, raw, prices=LIVE_PRICES)
    result = run_frozen(repo)
    analysis = json.loads((result.analysis_dir / "analysis.json").read_text(encoding="utf-8"))
    h4 = analysis["models"]["good"]["hypotheses"]["H4"]
    assert h4["status"] == "NOT EVALUABLE" and "gated_research" in json.dumps(h4)


def test_layer3_frozen_run_stopped_by_the_search_quota_writes_no_analysis_and_resumes(tmp_path, live_world):
    raw, _web, serp = live_world
    raw = {**raw, "runner": {**raw["runner"], "max_searches": 3}}  # one unit is counted at 1 + 2 searches
    repo = frozen_repo(tmp_path, raw, prices=LIVE_PRICES)
    result = run_frozen(repo)
    assert result.aborted and result.analysis_dir is None
    assert read_manifest(result.run_dir).ended_at is None
    record = json.loads((result.run_dir / frozen_run.FROZEN_RUN_LOG).read_text(encoding="utf-8").splitlines()[-1])
    assert record["analysis"] is None and record["run_summary"]["search_limit_reached"] is True
    # The quota "renews": the same command with --resume is a further logged access and finishes the run.
    resumed = run_frozen(repo, resume_dir=result.run_dir)
    assert events(repo) == [EVENT_LOADED, EVENT_LOADED]
    assert not resumed.aborted and (resumed.analysis_dir / "analysis.json").exists()
    assert len((result.run_dir / frozen_run.FROZEN_RUN_LOG).read_text(encoding="utf-8").splitlines()) == 2


# -- layer 2 ------------------------------------------------------------------------- #

_DOC = re.compile(r"\[Document \d+ — source: (\w+)\]\n(.*?)(?=\n\n\[Document |\n\nQuestion: )", re.DOTALL)


@register("model", "pr16_first_passage_fake")
class FirstPassageFake:
    """Answers with the first 20 words of the first shown passage as value and quote (no network)."""

    def __init__(self, *, label: str | None = None, model_id: str = "fake-cheap"):
        self.name = label or model_id
        self.model_id = model_id
        self.max_output_tokens = 1024
        self.retry = RetryPolicy(max_retries=0, backoff_seconds=0.0)

    def preflight(self) -> PreflightResult:
        return PreflightResult(self.name, self.model_id, True, "listed")

    def complete(self, request: ModelRequest) -> ModelResponse:
        source, passage = _DOC.findall(request.messages[-1].content)[0]
        words = " ".join(passage.split()[:20])
        text = json.dumps({"status": "answered", "value": words, "quote": words, "source_id": source})
        return ModelResponse(text=text, model=self.model_id, usage=TokenUsage(100, 20))


@register("arm", "pr16_test_gate")
class Doc1BlockingGate:
    """Test stand-in for a gate: the no_gate candidate, blocked when it cites ``doc1``."""

    name = "pr16_test_gate"

    def __init__(self, *, model, presentation: str = "grab_rag_context"):
        self.inner = create("arm", "no_gate", model=model, presentation=presentation)

    def run(self, arm_input):
        output = self.inner.run(arm_input)
        answer = output.final_answer
        if answer.status.value != "answered":
            return output
        evidence = output.attempts[0].evidence
        if answer.source_id != "doc1":
            verdict = Verdict(Decision.ACCEPT, check_results=(CheckResult("quote_verbatim", CheckStatus.PASS),))
            return ArmOutput((Attempt(answer, evidence, verdict),), answer, model_calls=output.model_calls)
        verdict = Verdict(Decision.ABSTAIN, reasons=("test block",), check_results=(CheckResult("entity_in_window", CheckStatus.FAIL),))
        final = AnswerCandidate.abstained(raw=answer.raw, reason="blocked by the test gate")
        return ArmOutput((Attempt(answer, evidence, verdict),), final, model_calls=output.model_calls)


def layer2_raw(tmp_path: Path) -> dict:
    options = gfx.write_checkout(tmp_path / "grab-rag", export=False)
    options["supportive_questions"] = "all"
    return {
        "config_version": 1,
        "name": "frozen-grab-rag-test",
        "split": "test",
        "repeats": prereg.REPEATS,
        "dataset": {"name": "grab_rag", "options": options},
        "models": [{"name": "pr16_first_passage_fake", "label": "fake", "options": {"model_id": "fake-cheap"}}],
        "arms": [
            {"name": "no_gate", "label": "no_gate", "options": {"presentation": "grab_rag_context"}},
            {"name": "pr16_test_gate", "label": "gated", "options": {"presentation": "grab_rag_context"}},
        ],
        "scorers": [{"name": "provenance"}, {"name": "grab_rag_answer"}],
        "runner": {"concurrency": 2, "max_usd": 5.0},
    }


def test_layer2_frozen_run_reports_grab_rag_answer_metrics_without_a_decision(tmp_path, capsys):
    repo = frozen_repo(tmp_path, layer2_raw(tmp_path))
    result = run_frozen(repo)
    assert events(repo) == [EVENT_LOADED]
    records = list(read_traces(result.run_dir))
    assert records and {r.split for r in records} == {Split.TEST}
    labels = {(r.arm, r.item_id): next(s.label for s in r.scores if s.scorer == "grab_rag_answer") for r in records}

    analysis = json.loads((result.analysis_dir / "analysis.json").read_text(encoding="utf-8"))
    assert analysis["layer"] == 2 and analysis["outcome_rule"]["scorer"] == "grab_rag_answer"
    assert analysis["outcome_rule"]["decision"].startswith("none")
    fake = analysis["models"]["fake"]
    # Correct answers are grab_rag_answer's "correct" labels (not provenance's target-grounded).
    n_correct = sum(1 for (arm, _i), label in labels.items() if arm == "no_gate" and label == "correct")
    assert fake["outcome_counts"]["no_gate"].get("target_grounded", 0) == n_correct > 0
    assert fake["estimates"]["arm/gated/m2"]["value"] is not None  # the cost side is measured
    assert all(fake["hypotheses"][name]["status"] == "NOT EVALUABLE" for name in ("H1", "H2", "H3", "H4"))
    assert not any(fake["hypotheses"][name]["confirmatory"] for name in ("H1", "H2", "H3", "H4"))
    assert fake["controlled_layer_claim"].startswith("no decision")
    assert set(analysis["layer2"]["misled_counts"]["fake"]) == {"gated", "no_gate"}

    record = json.loads((result.run_dir / frozen_run.FROZEN_RUN_LOG).read_text(encoding="utf-8").splitlines()[-1])
    assert record["layer"] == 2 and record["outcome_rule"] == "grab_rag_answer (layer 2)"
    assert record["decision"].startswith("none")

    capsys.readouterr()
    assert cli.main(["analyze", str(result.run_dir), "--out", str(tmp_path / "re"), "--no-figures"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["outcome_rule"] == "grab_rag_answer (layer 2)"


# -- one layer per analysis ---------------------------------------------------------- #


def layer1_run(tmp_path: Path):
    repo, config, _raw = h.setup(tmp_path / "layer1")
    return runner.execute_run(config, repo_root=repo, runs_dir=tmp_path / "runs1", confirm_spend=True, log=lambda m: None)


def test_layer1_dispatch_is_the_unchanged_provenance_analysis(tmp_path):
    summary = layer1_run(tmp_path)
    analysis, rule = analyse_layer([summary.run_dir], resamples=200)
    assert rule.layer == 1 and rule.scorer == "provenance"
    assert analysis == analyse_run_dirs([summary.run_dir], resamples=200)  # byte-identical layer-1 output
    assert "outcome_rule" not in analysis


def test_mixed_layers_are_refused(tmp_path):
    summary1 = layer1_run(tmp_path)
    repo2, config2, _ = h.setup(tmp_path / "l2-dummy")
    raw2 = layer2_raw(tmp_path)
    raw2["split"] = "dev"
    from binding_gate.config import parse_config

    summary2 = runner.execute_run(parse_config(raw2), repo_root=repo2, runs_dir=tmp_path / "runs2", confirm_spend=True,
                                  log=lambda m: None)
    assert layer_of_run_dirs([summary2.run_dir]) == 2
    with pytest.raises(LayerError, match="analyse each dataset separately"):
        analyse_layer([summary1.run_dir, summary2.run_dir])
    with pytest.raises(LayerError, match="expected run directories of layer 3"):
        analyse_layer([summary2.run_dir], expected_layer=3)
    assert cli.main(["analyze", str(summary1.run_dir), str(summary2.run_dir), "--no-figures"]) == cli.EXIT_FAILURE


# --------------------------------------------------------------------------- #
# Published results (section 17, item 37)
# --------------------------------------------------------------------------- #


def published_files(target: Path) -> set[str]:
    return {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}


def test_publish_copies_only_allowed_layer3_files_and_never_page_text(tmp_path, live_world):
    raw, _web, _serp = live_world
    repo = frozen_repo(tmp_path, raw, prices=LIVE_PRICES)
    result = run_frozen(repo)
    results_dir = tmp_path / "results"
    published = publish_run(result.run_dir, results_dir=results_dir)
    target = results_dir / "layer3" / read_manifest(result.run_dir).run_id
    assert published.target == target and published.layer == 3
    assert published_files(target) == {
        "manifest.json", "invocations.jsonl", "frozen_run.jsonl", "analysis/analysis.json", "analysis/report.md",
        "snapshots/publish_manifest.json", RESULTS_MANIFEST,
    }
    not_published = {e["path"]: e["reason"] for e in published.not_published}
    assert "traces.jsonl" in not_published and "licence" in not_published["traces.jsonl"]
    assert "snapshots/index.jsonl" in not_published and any(p.startswith("snapshots/blobs/") for p in not_published)
    everything = "".join(p.read_text(encoding="utf-8") for p in target.rglob("*") if p.is_file())
    assert "hosted by" not in everything and "large crowds" not in everything  # no page text anywhere
    assert str(result.run_dir.parent) not in everything  # local absolute path replaced
    manifest = json.loads((target / RESULTS_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["layer"] == 3 and manifest["split"] == "test" and manifest["test_unlock_tag"] == TAG
    assert manifest["traces_sha256"] == read_manifest(result.run_dir).traces_sha256
    for entry in manifest["files"]:
        assert entry["sha256"] == __import__("hashlib").sha256((target / entry["path"]).read_bytes()).hexdigest()

    with pytest.raises(PublishError, match="already exists"):
        publish_run(result.run_dir, results_dir=results_dir)
    for asked in (["traces.jsonl"], ["snapshots/index.jsonl"], ["manifest.json", "snapshots/index.jsonl"]):
        with pytest.raises(PublishError, match="refused"):
            publish_run(result.run_dir, results_dir=tmp_path / "other", files=asked)
    with pytest.raises(PublishError, match="layer 3 contain third-party text"):
        publish_run(result.run_dir, results_dir=tmp_path / "other", include_traces=True)
    assert not (tmp_path / "other").exists() or not any((tmp_path / "other").rglob("*.json"))


def test_publish_refuses_unfinished_tampered_or_unanalysed_runs(tmp_path):
    summary = layer1_run(tmp_path)
    with pytest.raises(PublishError, match="analysis.json is missing"):
        publish_run(summary.run_dir, results_dir=tmp_path / "results")
    assert cli.main(["analyze", str(summary.run_dir), "--no-figures"]) == cli.EXIT_OK
    traces = summary.run_dir / "traces.jsonl"
    original = traces.read_bytes()
    traces.write_bytes(original + b"\n")
    with pytest.raises(PublishError, match="does not match the SHA-256"):
        publish_run(summary.run_dir, results_dir=tmp_path / "results")
    traces.write_bytes(original)
    manifest_path = summary.run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps({**manifest, "ended_at": None}), encoding="utf-8")
    with pytest.raises(PublishError, match="unfinished"):
        publish_run(summary.run_dir, results_dir=tmp_path / "results")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert not (tmp_path / "results").exists()


def test_publish_layer1_with_traces_on_request_and_through_the_cli(tmp_path, capsys):
    summary = layer1_run(tmp_path)
    assert cli.main(["analyze", str(summary.run_dir), "--no-figures"]) == cli.EXIT_OK
    capsys.readouterr()
    code = report_cli.main(["publish", str(summary.run_dir), "--results-dir", str(tmp_path / "results"), "--include-traces"])
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    target = Path(printed["published"])
    assert target.parent.name == "layer1" and "traces.jsonl" in published_files(target)
    assert (target / "traces.jsonl").read_bytes() == (summary.run_dir / "traces.jsonl").read_bytes()
    assert report_cli.main(["publish", str(summary.run_dir), "--results-dir", str(tmp_path / "results")]) == 1
    assert "already exists" in capsys.readouterr().err


def test_refusal_rules():
    assert refusal_reason("analysis/figures/primary_pair.png", 3) is None
    assert refusal_reason("analysis/figures/sub/x.png", 1) is not None
    assert refusal_reason("scores.llm_judge.jsonl", 1) == "re-scoring outputs quote the documents"
    assert refusal_reason("traces.jsonl", 2).startswith("traces of layer 2")
    assert refusal_reason("traces.jsonl", 1) is not None and refusal_reason("traces.jsonl", 1, include_traces=True) is None
    assert refusal_reason("snapshots/blobs/abc", 3).startswith("live-web snapshots")
    assert refusal_reason("notes.txt", 1) == "not on the allow-list of published files"


# --------------------------------------------------------------------------- #
# Documentation
# --------------------------------------------------------------------------- #


def test_preregistration_closes_items_37_and_39_in_version_0_7():
    text = (REAL_REPO / "PREREGISTRATION.md").read_text(encoding="utf-8")
    assert "**Document version:** 0.7" in text and "**Status:** DRAFT" in text
    section17 = text.split("## 17.", 1)[1].split("## 18.", 1)[0]
    for number in (37, 39):
        item = section17.split(f"\n{number}. ", 1)[1].split(f"\n{number + 1}. ", 1)[0]
        assert "Closed in version 0.7 (PR 16)" in item, number
    section12 = text.split("## 12. Test-split discipline", 1)[1].split("## 13.", 1)[0]
    assert "configs/test_live.toml" in section12 and "configs/test_grab_rag_supportive.toml" in section12


def test_the_real_repository_access_log_was_not_written():
    log = REAL_REPO / ACCESS_LOG_RELPATH
    assert not [e for e in read_access_log(log) if e.get("event") == EVENT_LOADED]
    assert log.read_bytes() == b""


# --------------------------------------------------------------------------- #
# The test configs' arms run on dev (offline dry runs, synthetic data, no key)
# --------------------------------------------------------------------------- #


def test_test_grab_rag_config_dry_runs_on_dev_without_any_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    repo = h.make_repo(tmp_path)
    (repo / "configs" / "prices.toml").write_text((CONFIGS / "prices.toml").read_text(), encoding="utf-8")
    raw = load_config(CONFIGS / "test_grab_rag_supportive.toml").to_dict()
    options = gfx.write_checkout(tmp_path / "grab-rag", export=False)
    raw["split"] = "dev"
    raw["dataset"]["options"] = {**raw["dataset"]["options"], **{k: options[k] for k in ("root", "commit", "data_sha256")}}
    from binding_gate.config import parse_config

    report = runner.dry_run(parse_config(raw), repo_root=repo)
    assert report.split == "dev" and report.arms == ("no_gate", "gated")
    assert {a.role for rows in report.auxiliary.values() for a in rows} == {"decomposer"}


def test_test_live_config_dry_runs_on_dev_offline_and_its_caps_cover_the_high_estimate(tmp_path, monkeypatch):
    clear_sessions()
    for var in ("SERPAPI_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    web, serp = lf.install(monkeypatch, key=None)
    hard = [sfx.row(i) for i in range(60)]
    options = sfx.write(tmp_path / "sealqa", monkeypatch, hard, [dict(r) for r in hard[:20]])
    raw = load_config(CONFIGS / "test_live.toml").to_dict()
    raw["split"] = "dev"
    raw["dataset"]["options"] = options
    from binding_gate.config import parse_config

    report = runner.dry_run(parse_config(raw), repo_root=REAL_REPO)
    assert serp.urls == [] and web.requests == []
    for model in report.models:
        high = report.model_total(model.label, "cost_high")
        assert high is not None and high < model.cap
    clear_sessions()
