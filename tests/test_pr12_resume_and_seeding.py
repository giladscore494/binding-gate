"""PR 12: decomposer cache preload on resume (section 17, item 31) and response seeding (item 32).

Everything runs in a temporary repository with the synthetic benchmark checkout
and scripted fake models; no network, no key, no test-split access.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

import decomposer_fakes
import fakes
import run_helpers as h
from binding_gate import answering, runner
from binding_gate.arms.gated import clear_decomposition_memo
from binding_gate.canonical import sha256_json
from binding_gate.config import parse_config
from binding_gate.contracts import Message, Split
from binding_gate.decomposers import cache as cache_module
from binding_gate.trace import MANIFEST_NAME, TRACES_NAME, read_manifest, read_traces

N_DEV_ITEMS = 3 * 15
QUESTIONS = 3  # distinct dev (X, C) questions of the synthetic checkout
ALL_CHECKS = ["quote_verbatim", "entity_in_window", "qualifiers_in_window", "no_closer_competitor", "premise_present"]


@pytest.fixture(autouse=True)
def _fresh_process_state():
    """Each test starts like a fresh process: no shared decomposition cache or memo."""
    cache_module.clear_shared_caches()
    clear_decomposition_memo()
    decomposer_fakes.CALLS.clear()
    yield
    cache_module.clear_shared_caches()
    clear_decomposition_memo()
    decomposer_fakes.CALLS.clear()


def new_process() -> None:
    """What a new CLI process starts with: empty process-wide caches."""
    cache_module.clear_shared_caches()
    clear_decomposition_memo()


def gated_arm(behaviour: str = "reference") -> dict:
    return {
        "name": "gated",
        "label": "gated",
        "options": {
            "checks": ALL_CHECKS,
            "decomposer": {
                "name": "model_decomposer",
                "options": {
                    "model": {
                        "name": "decomposer_fake",
                        "label": "decomposer",
                        "options": {"model_id": "fake-cheap", "behaviour": behaviour},
                    }
                },
            },
        },
    }


def run(repo, config, runs_dir, **kwargs):
    kwargs.setdefault("confirm_spend", True)
    kwargs.setdefault("log", lambda message: None)
    return runner.execute_run(config, repo_root=repo, runs_dir=runs_dir, **kwargs)


def interrupt_after(monkeypatch, n: int) -> None:
    real_append = runner.TraceWriter.append
    written = {"n": 0}

    def dying_append(self, record):
        if written["n"] == n:
            raise KeyboardInterrupt
        written["n"] += 1
        real_append(self, record)

    monkeypatch.setattr(runner.TraceWriter, "append", dying_append)


def decompose_calls(records):
    return [call for record in records for call in record.model_calls if call.purpose == "decompose"]


# =========================================================================== #
# Item 31: decomposer cache preload on resume
# =========================================================================== #


def _interrupted_gated_run(tmp_path, monkeypatch, behaviour="reference", after=200):
    repo, config, _raw = h.setup(
        tmp_path, arms=[{"name": "no_gate"}, gated_arm(behaviour)], runner={"concurrency": 1, "max_usd": 5.0}
    )
    interrupt_after(monkeypatch, after)
    with pytest.raises(KeyboardInterrupt):
        run(repo, config, tmp_path / "runs")
    monkeypatch.undo()
    (run_dir,) = (tmp_path / "runs").iterdir()
    assert read_manifest(run_dir).ended_at is None
    return repo, config, run_dir


def test_resumed_run_does_not_decompose_a_question_again(tmp_path, monkeypatch):
    repo, config, run_dir = _interrupted_gated_run(tmp_path, monkeypatch)
    # Repeat 0 (180 records) is complete, so every question was decomposed once.
    assert len(set(decomposer_fakes.CALLS)) == QUESTIONS and len(decomposer_fakes.CALLS) == QUESTIONS
    before = list(read_traces(run_dir))
    first = {}
    for record in before:
        if record.constraints is not None:
            first.setdefault(record.constraints.question, record.constraints)

    new_process()
    decomposer_fakes.CALLS.clear()
    summary = run(repo, config, tmp_path / "runs", resume_dir=run_dir)
    assert summary.closed and summary.error_records == 0
    assert decomposer_fakes.CALLS == [], "a resumed segment must not decompose a question again"

    records = list(read_traces(run_dir))
    fresh = [c for c in decompose_calls(records) if not runner.is_cache_hit(c)]
    assert len(fresh) == QUESTIONS  # one paid decomposition per question for the whole run
    for record in records:
        if record.constraints is not None:
            assert record.constraints == first[record.constraints.question]  # identical across segments
    resumed = [e for e in runner.read_invocations(run_dir) if e["event"] == "resumed"]
    assert resumed[0]["decompositions_preloaded"] == {"constraints": QUESTIONS, "failure": 0, "inconsistent_records": 0}


def test_without_the_preload_the_resumed_segment_would_decompose_again(tmp_path, monkeypatch):
    """Control: the test above detects the defect it guards against."""
    repo, config, run_dir = _interrupted_gated_run(tmp_path, monkeypatch)
    new_process()
    decomposer_fakes.CALLS.clear()
    monkeypatch.setattr(runner, "_preload_decompositions", lambda *a, **k: {})
    run(repo, config, tmp_path / "runs", resume_dir=run_dir)
    assert len(decomposer_fakes.CALLS) == QUESTIONS


def test_a_recorded_parse_failure_is_not_asked_again_after_resume(tmp_path, monkeypatch):
    repo, config, run_dir = _interrupted_gated_run(tmp_path, monkeypatch, behaviour="garbage")
    assert len(decomposer_fakes.CALLS) == QUESTIONS
    new_process()
    decomposer_fakes.CALLS.clear()
    summary = run(repo, config, tmp_path / "runs", resume_dir=run_dir)
    assert decomposer_fakes.CALLS == []
    assert summary.error_records > 0  # the gated records stay error records, as in one process
    resumed = [e for e in runner.read_invocations(run_dir) if e["event"] == "resumed"]
    assert resumed[0]["decompositions_preloaded"]["failure"] == QUESTIONS


def test_preload_from_call_accepts_only_its_own_calls():
    from binding_gate import registry

    def make(label="decomposer", behaviour="reference"):
        return registry.create(
            "decomposer", "model_decomposer",
            model={"name": "decomposer_fake", "label": label, "options": {"behaviour": behaviour}},
            sleep=lambda _s: None,
        )

    question = "What does the literature say about using xanadrug1 to treat zeta syndrome 1?"
    other = "What does the literature say about using xanadrug2 to treat zeta syndrome 2?"
    (call,) = make().decompose(question).model_calls
    new_process()
    decomposer_fakes.CALLS.clear()
    assert make(label="someone-else").preload_from_call(question, call) is None  # other client label
    assert make().preload_from_call(other, call) is None  # request is for another question
    assert make().preload_from_call(question, call) == "constraints"
    assert make().decompose(question).model_calls[0].response.raw["cache_hit"] is True
    assert decomposer_fakes.CALLS == []
    uncached = registry.create(
        "decomposer", "model_decomposer",
        model={"name": "decomposer_fake", "label": "decomposer", "options": {}}, cache="none",
    )
    assert uncached.preload_from_call(question, call) is None


# =========================================================================== #
# Item 32: seeding the response cache from earlier runs
# =========================================================================== #


def seed_run(tmp_path, *, repeats=2, selection=None, models=None):
    """A completed dev no_gate run inside the repository, under seedruns/."""
    repo, _config, raw = h.setup(tmp_path, repeats=repeats, **({"models": models} if models else {}))
    config = parse_config(raw)
    summary = run(repo, config, repo / "seedruns", selection=selection)
    assert summary.closed
    return repo, raw, summary.run_dir


def seeded_config(raw, seed_dirs, repo, **overrides):
    runner_table = {"concurrency": 3, "max_usd": 5.0,
                    "seed_responses_from": [d.relative_to(repo).as_posix() for d in seed_dirs]}
    return parse_config({**raw, "arms": [{"name": "no_gate"}, gated_arm()], "runner": runner_table, **overrides})


def answer_calls(records):
    return [call for record in records for call in record.model_calls if call.purpose == "answer"]


def test_seeded_answers_are_reused_free_and_carry_their_source(tmp_path):
    repo, raw, seed_dir = seed_run(tmp_path)
    seed_records = {(r.item_id, r.model, r.repeat): r for r in read_traces(seed_dir)}
    seed_id = read_manifest(seed_dir).run_id
    fakes.CALLS.clear()
    messages: list[str] = []
    config = seeded_config(raw, [seed_dir], repo)
    summary = run(repo, config, tmp_path / "runs", log=messages.append)
    assert summary.closed and summary.error_records == 0
    assert fakes.CALLS == [], "every answer request was seeded: the model under test was not called"
    records = list(read_traces(summary.run_dir))
    assert len(records) == N_DEV_ITEMS * 2 * 2 * 2
    for call in answer_calls(records):
        assert runner.is_cache_hit(call) and call.response.raw[runner.SEEDED_FROM_KEY] == seed_id
    for record in records:
        if record.arm == "no_gate":
            assert record.final_answer == seed_records[(record.item_id, record.model, record.repeat)].final_answer
    # Seeded calls cost nothing: only the decomposer (3 questions x 2 model jobs at most) is charged.
    decomposer_cost = sum(
        runner.CostModel(runner.load_prices(repo / "configs/prices.toml"), {"decomposer": "fake-cheap"}, {}).call_cost(c)
        for c in decompose_calls(records)
    )
    assert decomposer_cost > 0 and sum(summary.spend_usd.values()) == pytest.approx(decomposer_cost)
    started = runner.read_invocations(summary.run_dir)[0]
    (seed,) = started["seeds"]
    assert seed["run_id"] == seed_id and seed["closed"] is True
    assert seed["responses_by_model"] == {"bad": N_DEV_ITEMS * 2, "good": N_DEV_ITEMS * 2}
    assert any("seeded responses for good: 90" in m for m in messages)


def test_only_matching_keys_are_seeded_the_rest_is_paid(tmp_path):
    repo, raw, seed_dir = seed_run(tmp_path, repeats=1, selection="good")
    fakes.CALLS.clear()
    messages: list[str] = []
    summary = run(repo, seeded_config(raw, [seed_dir], repo, repeats=2), tmp_path / "runs", log=messages.append)
    paid = Counter(label for label, _request in fakes.CALLS)
    # good: repeat 0 seeded, repeat 1 paid; bad: never ran in the seed, all paid.
    assert paid == {"good": N_DEV_ITEMS, "bad": N_DEV_ITEMS * 2}
    seeded = [c for c in answer_calls(read_traces(summary.run_dir)) if runner.SEEDED_FROM_KEY in c.response.raw]
    assert {c.client for c in seeded} == {"good"}
    assert any("seeded responses for bad: 0 (WARNING" in m for m in messages)


def test_first_listed_seed_wins_and_the_dry_run_lists_the_seeds(tmp_path):
    repo, raw, seed_a = seed_run(tmp_path, repeats=1)
    config_b = parse_config(raw | {"repeats": 1})
    seed_b = run(repo, config_b, repo / "seedruns").run_dir
    config = seeded_config(raw, [seed_b, seed_a], repo)
    report = runner.dry_run(config, repo_root=repo)
    assert [s.run_id for s in report.seeds] == [read_manifest(seed_b).run_id, read_manifest(seed_a).run_id]
    assert report.seeds[0].responses_by_model == {"bad": N_DEV_ITEMS, "good": N_DEV_ITEMS}
    assert report.seeds[1].responses_by_model == {"bad": 0, "good": 0}  # every key already taken
    text = report.format()
    per_model = report.requests_per_repeat * report.repeats
    assert "SEEDED RESPONSES" in text and f"{N_DEV_ITEMS} of {per_model} answer calls seeded" in text
    # PR 13: seeded answers are left out of the calls and costs of the estimate
    assert report.seeded_calls == {"bad": N_DEV_ITEMS, "good": N_DEV_ITEMS}
    assert all(m.calls == per_model - N_DEV_ITEMS for m in report.models)
    assert report.to_dict()["seeds"][0]["path"] == seed_b.relative_to(repo).as_posix()


def test_transport_options_may_differ(tmp_path):
    repo, raw, seed_dir = seed_run(tmp_path)
    models = json.loads(json.dumps(raw["models"]))
    for model in models:
        model["options"]["max_retries"] = 4  # a transport option (runner.TRANSPORT_OPTIONS)
    fakes.CALLS.clear()
    run(repo, seeded_config(raw, [seed_dir], repo, models=models), tmp_path / "runs")
    assert fakes.CALLS == []


def test_resumed_seeded_run_still_pays_nothing_for_seeded_answers(tmp_path, monkeypatch):
    repo, raw, seed_dir = seed_run(tmp_path)
    config = seeded_config(raw, [seed_dir], repo, runner={
        "concurrency": 1, "max_usd": 5.0, "seed_responses_from": [seed_dir.relative_to(repo).as_posix()]
    })
    fakes.CALLS.clear()
    interrupt_after(monkeypatch, 30)
    with pytest.raises(KeyboardInterrupt):
        run(repo, config, repo / "runs")
    monkeypatch.undo()
    (run_dir,) = (repo / "runs").iterdir()
    new_process()
    summary = run(repo, config, repo / "runs", resume_dir=run_dir)
    assert summary.closed and fakes.CALLS == []
    # A run cannot seed itself.
    selfish = seeded_config(raw, [run_dir], repo)
    with pytest.raises(runner.RunnerError, match="cannot seed itself"):
        run(repo, selfish, repo / "runs", resume_dir=run_dir)


# -- refusals ----------------------------------------------------------------------- #


def rewrite_manifest(run_dir: Path, *, reopen: bool = False, **config_changes) -> None:
    """Edit the config stored in a seed's manifest (and its hash), as a tampered or foreign run would have."""
    data = json.loads((run_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    data["config"] = {**data["config"], **config_changes}
    data["config_hash"] = sha256_json(data["config"])
    if reopen:
        data.update(ended_at=None, trace_count=None, traces_sha256=None)
    (run_dir / MANIFEST_NAME).write_text(json.dumps(data), encoding="utf-8")


def refused(repo, raw, seed_dir, match, tmp_path, **overrides):
    config = seeded_config(raw, [seed_dir], repo, **overrides)
    fakes.CALLS.clear()
    with pytest.raises(runner.RunnerError, match=match):
        run(repo, config, tmp_path / "runs")
    with pytest.raises(runner.RunnerError, match=match):
        runner.dry_run(config, repo_root=repo)
    assert fakes.CALLS == [] and not (tmp_path / "runs").exists(), "a refused seed stops before any call or run directory"


def test_refuses_a_seed_from_a_test_split_run(tmp_path):
    repo, raw, seed_dir = seed_run(tmp_path)
    rewrite_manifest(seed_dir, split="test")
    refused(repo, raw, seed_dir, "only dev runs can seed a dev run", tmp_path)


def test_refuses_a_seed_holding_a_test_split_record(tmp_path):
    repo, raw, seed_dir = seed_run(tmp_path)
    rewrite_manifest(seed_dir, reopen=True)
    lines = (seed_dir / TRACES_NAME).read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["split"] = "test"
    (seed_dir / TRACES_NAME).write_text("\n".join([json.dumps(first), *lines[1:]]) + "\n", encoding="utf-8")
    refused(repo, raw, seed_dir, "from the 'test' split", tmp_path)


def test_seeding_is_refused_in_a_test_run(tmp_path):
    repo, raw, seed_dir = seed_run(tmp_path)
    config = seeded_config(raw, [seed_dir], repo, split="test")
    settings = runner.RunnerSettings.from_config(config)
    with pytest.raises(runner.RunnerError, match="only in dev runs"):
        runner.load_seed_responses(
            config, settings, repo_root=repo, plan=runner.Plan((), "x"), specs=config.models, expected=frozenset()
        )


def test_a_seeded_test_config_stops_before_the_lock_is_touched(tmp_path):
    from binding_gate import frozen_run
    from binding_gate.testlock import ACCESS_LOG_RELPATH

    repo, raw, seed_dir = seed_run(tmp_path)
    config = seeded_config(raw, [seed_dir], repo, split="test")
    with pytest.raises(runner.RunnerError, match="only in dev runs"):
        run(repo, config, tmp_path / "runs", test_unlock_tag="prereg-freeze-1", purpose="unit test")
    assert not (repo / ACCESS_LOG_RELPATH).exists(), "no test-split access may be logged"
    reasons = frozen_run.config_refusals(config, repo / "configs" / "x.toml", repo, tmp_path / "runs")
    assert any("seed runs" in reason for reason in reasons)


def test_refuses_another_dataset_pin_or_fingerprint(tmp_path):
    repo, raw, seed_dir = seed_run(tmp_path)
    original = (seed_dir / MANIFEST_NAME).read_text(encoding="utf-8")
    dataset = json.loads(original)["config"]["dataset"]
    rewrite_manifest(seed_dir, dataset={**dataset, "options": {**dataset["options"], "commit": "d" * 40}})
    refused(repo, raw, seed_dir, "dataset spec", tmp_path)
    (seed_dir / MANIFEST_NAME).write_text(original, encoding="utf-8")
    invocations = seed_dir / runner.INVOCATIONS_NAME
    entries = [json.loads(line) for line in invocations.read_text(encoding="utf-8").splitlines()]
    entries[0]["dataset_fingerprint"] = "0" * 64
    invocations.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    refused(repo, raw, seed_dir, "dataset fingerprint", tmp_path)
    invocations.unlink()
    refused(repo, raw, seed_dir, "dataset fingerprint", tmp_path)


def test_refuses_another_model_spec(tmp_path):
    repo, raw, seed_dir = seed_run(tmp_path)
    for change in ({"model_id": "fake-dear"}, {"max_output_tokens": 77}):
        models = json.loads(json.dumps(raw["models"]))
        models[0]["options"].update(change)
        refused(repo, raw, seed_dir, "has another model spec", tmp_path, models=models)


def test_refuses_another_request_format(tmp_path, monkeypatch):
    repo, raw, seed_dir = seed_run(tmp_path)
    original = answering.PRESENTATIONS["deceptive_grounding_doc_injection"]

    def changed_prompt(question, documents):
        return (*original(question, documents), Message("user", "Answer carefully."))

    monkeypatch.setitem(answering.PRESENTATIONS, "deceptive_grounding_doc_injection", changed_prompt)
    refused(repo, raw, seed_dir, "none of its .* answer calls .* is a request this run sends", tmp_path)


def test_refuses_a_seed_modified_after_it_was_closed_or_missing(tmp_path):
    repo, raw, seed_dir = seed_run(tmp_path)
    with (seed_dir / TRACES_NAME).open("a", encoding="utf-8") as handle:
        handle.write("\n")
    refused(repo, raw, seed_dir, "does not match the SHA-256", tmp_path)
    refused(repo, raw, repo / "seedruns" / "nope", "download the seed run first", tmp_path)


def test_refuses_live_web_configs():
    raw = h.raw_config({"root": "x"}, arms=[{"name": "no_gate_live", "options": {"search": {"name": "serpapi_google"}}}],
                       runner={"max_usd": 1.0, "seed_responses_from": ["seeds/x"]})
    config = parse_config(raw)
    with pytest.raises(runner.RunnerError, match="live-web"):
        runner.load_seed_responses(
            config, runner.RunnerSettings.from_config(config), repo_root=".", plan=runner.Plan((), "x"),
            specs=config.models, expected=frozenset(),
        )


@pytest.mark.parametrize(
    "value, match",
    [
        ("runs/x", "must be a list"),
        (["/abs/run"], "relative to the repository root"),
        (["seeds/../x"], "without '..'"),
        (["seeds/a", "seeds/a"], "twice"),
        ([""], "must be a list"),
    ],
)
def test_seed_paths_are_validated(value, match):
    raw = h.raw_config({"root": "x"}, runner={"max_usd": 1.0, "seed_responses_from": value})
    with pytest.raises(runner.RunnerError, match=match):
        runner.RunnerSettings.from_config(parse_config(raw))


def test_requirements_report_the_seed_runs(tmp_path):
    raw = h.raw_config({"root": "x"}, runner={"max_usd": 1.0, "seed_responses_from": ["seeds/a", "seeds/b"]})
    assert runner.run_requirements(parse_config(raw))["seed_runs"] == ["seeds/a", "seeds/b"]
    assert runner.run_requirements(parse_config(h.raw_config({"root": "x"})))["seed_runs"] == []
    assert Split.DEV.value == "dev"
