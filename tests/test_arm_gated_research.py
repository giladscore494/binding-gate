"""Arms gated_research and no_gate_live with a fake search API, a fake web, a fake model and decomposer."""

from __future__ import annotations

import json

import pytest

import live_fakes as lf
from binding_gate import registry
from binding_gate.answering import ModelCallFailed
from binding_gate.arms.gated import clear_decomposition_memo
from binding_gate.contracts import AnswerStatus, ArmInput, Decision
from binding_gate.research import context as rc
from binding_gate.research.session import clear_sessions, session_for
from binding_gate.research.snapshots import SnapshotStore
from binding_gate.runner import CachedClient, ResponseCache, is_cache_hit


@pytest.fixture
def world(tmp_path, monkeypatch):
    clear_sessions()
    clear_decomposition_memo()
    lf.MODEL_CALLS.clear()
    lf.DECOMPOSE_CALLS.clear()
    web, serp = lf.install(monkeypatch)
    context = rc.RunContext(run_dir=tmp_path / "run")
    yield web, serp, context, tmp_path
    clear_sessions()
    clear_decomposition_memo()


def build(context, spec, model=None):
    model = model or CachedClient(lf.LiveFakeModel(label="m"), 0, ResponseCache())
    with rc.activate(context):
        return registry.create("arm", spec["name"], model=model, **spec["options"])


def arms(context, max_attempts=2, decomposer_mode="ok", model=None):
    model = model or CachedClient(lf.LiveFakeModel(label="m"), 0, ResponseCache())
    specs = lf.arm_specs(max_attempts, decomposer_mode)
    return build(context, specs[0], model), build(context, specs[1], model)


def item(question=lf.Q_EXAMPLE):
    return ArmInput(item_id="sealqa/x", question=question)


def test_the_first_attempt_is_shared_and_the_re_search_recovers(world):
    web, serp, context, tmp_path = world
    baseline, gated = arms(context)
    base = baseline.run(item())
    out = gated.run(item())

    # no_gate: the first page (Sample Games) answers "Southport"; robots-disallowed and 404 pages are skipped.
    assert base.final_answer.value == "Southport" and len(base.attempts) == 1
    assert [c.purpose for c in base.model_calls] == ["search", "fetch", "fetch", "fetch", "answer"]
    # gated_research: SAME first attempt (searches, fetches and the answer come from the caches) ...
    first = out.attempts[0]
    assert first.answer == base.final_answer and first.evidence == base.attempts[0].evidence
    assert all(is_cache_hit(c) for c in out.model_calls[:5])
    assert first.verdict.decision is Decision.RESEARCH
    assert {r.split(":")[0] for r in first.verdict.reasons} >= {"entity_in_window", "qualifiers_in_window"}
    # ... then ONE targeted re-search with the template query, new pages only, and an accepted answer.
    assert serp.queries == [lf.Q_EXAMPLE, lf.RESEARCH_QUERY_EXAMPLE]
    second = out.attempts[1]
    assert second.verdict.decision is Decision.ACCEPT and out.final_answer == second.answer
    assert out.final_answer.value == "Northbridge"
    assert [c.purpose for c in out.model_calls[5:]] == ["decompose", "search", "fetch", "answer"]
    assert out.constraints.entity.name == "Example Games"
    assert web.page_urls_requested().count(lf.PAGE_A) == 1  # the excluded URL is not fetched again
    assert len(lf.MODEL_CALLS) == 2  # one shared first answer, one re-search answer


def test_evidence_references_point_to_stored_snapshots_by_id_and_hash(world):
    web, serp, context, tmp_path = world
    _baseline, gated = arms(context)
    out = gated.run(item())
    store = SnapshotStore.for_run(tmp_path / "run")
    pages = {p.snapshot_id: p for p in store.pages()}
    for attempt in out.attempts:
        for n, ref in enumerate(attempt.evidence, start=1):
            page = pages[ref.id]
            assert ref.url == page.final_url and page.ok
            shown = store.get_text(ref.sha256)  # the shown text is stored under the referenced hash
            assert store.get_text(page.text_sha256).startswith(shown)
    # the document the model cited is evidence[n-1] of its attempt
    cited = out.final_answer.source_id
    assert cited == "web1" and pages[out.attempts[1].evidence[0].id].url == lf.PAGE_D
    assert json.loads(out.model_calls[1].response.text)["snapshot_id"] in pages


def test_accept_on_the_first_attempt_makes_no_re_search(world):
    web, serp, context, tmp_path = world
    _baseline, gated = arms(context)
    out = gated.run(item(lf.Q_SAMPLE))
    assert len(out.attempts) == 1 and out.attempts[0].verdict.decision is Decision.ACCEPT
    assert out.final_answer.value == "Southport" and serp.queries == [lf.Q_SAMPLE]


def test_research_at_the_last_allowed_attempt_ends_in_an_abstention(world):
    web, serp, context, tmp_path = world
    _baseline, gated = arms(context, max_attempts=1)
    out = gated.run(item())
    assert len(out.attempts) == 1 and out.attempts[0].verdict.decision is Decision.RESEARCH
    assert out.final_answer.status is AnswerStatus.ABSTAINED and "maximum of 1 attempts" in out.final_answer.reason
    assert serp.queries == [lf.Q_EXAMPLE]


def test_a_re_search_without_new_pages_or_that_fails_ends_in_an_abstention(world):
    web, serp, context, tmp_path = world
    serp.results[lf.RESEARCH_QUERY_EXAMPLE] = lf.organic(lf.PAGE_A, lf.PAGE_GONE)  # both already used
    _b, gated = arms(context)
    out = gated.run(item())
    assert len(out.attempts) == 1 and out.attempts[0].verdict.decision is Decision.RESEARCH
    assert out.final_answer.status is AnswerStatus.ABSTAINED and "found no new page" in out.final_answer.reason
    assert out.model_calls[-1].purpose == "search"  # used URLs are not fetched again

    clear_sessions()
    clear_decomposition_memo()
    serp.fail_queries = {lf.RESEARCH_QUERY_EXAMPLE}
    _b, gated = arms(rc.RunContext(run_dir=tmp_path / "run2"))
    out = gated.run(item())
    assert len(out.attempts) == 1 and "re-search failed" in out.final_answer.reason
    assert [c.purpose for c in out.model_calls][-3:] == ["search", "search", "search"]  # 1 + 2 retries, all logged
    assert all(c.error for c in out.model_calls[-3:])


def test_abstentions_and_unparseable_answers_pass_through_without_decomposition(world):
    web, serp, context, tmp_path = world
    for behaviour, status in (("abstain", AnswerStatus.ABSTAINED), ("garbage", AnswerStatus.UNPARSEABLE)):
        clear_sessions()
        model = CachedClient(lf.LiveFakeModel(label=behaviour, behaviour=behaviour), 0, ResponseCache())
        _b, gated = arms(rc.RunContext(run_dir=tmp_path / behaviour), model=model)
        out = gated.run(item())
        assert out.final_answer.status is status and out.attempts[0].verdict is None and out.constraints is None
    assert lf.DECOMPOSE_CALLS == []


def test_failures_of_the_first_search_the_answer_or_the_decomposer_raise_with_every_call(world):
    web, serp, context, tmp_path = world
    serp.status_sequence = [401]
    _b, gated = arms(context)
    with pytest.raises(ModelCallFailed) as info:
        gated.run(item())
    assert [c.purpose for c in info.value.model_calls] == ["search"]

    clear_sessions()
    _b, gated = arms(rc.RunContext(run_dir=tmp_path / "r2"), decomposer_mode="fail")
    with pytest.raises(ModelCallFailed) as info:
        gated.run(item())
    assert [c.purpose for c in info.value.model_calls] == ["search", "fetch", "fetch", "fetch", "answer", "decompose"]

    clear_sessions()
    model = CachedClient(lf.LiveFakeModel(label="c", behaviour="crash"), 0, ResponseCache())
    baseline, _g = arms(rc.RunContext(run_dir=tmp_path / "r3"), model=model)
    with pytest.raises(ModelCallFailed) as info:
        baseline.run(item())
    assert [c.purpose for c in info.value.model_calls] == ["search", "fetch", "fetch", "fetch", "answer"]


def test_options_are_validated_and_a_run_context_is_required(world):
    web, serp, context, tmp_path = world
    spec = lf.arm_specs()[1]
    model = lf.LiveFakeModel()
    with pytest.raises(ValueError, match="run context"):
        registry.create("arm", "gated_research", model=model, **spec["options"])
    for bad, message in (
        ({"max_attempts": 4}, "max_attempts"),
        ({"max_attempts": 0}, "max_attempts"),
        ({"search": None}, "options.search"),
        ({"decomposer": None}, "decomposer"),
        ({"checks": None}, "checks"),
        ({"presentation": "plain"}, "presentation"),
        ({"documents_per_attempt": 0}, "documents_per_attempt"),
        ({"fetch": {"bogus": 1}}, "unknown option"),
    ):
        with pytest.raises(ValueError, match=message), rc.activate(context):
            registry.create("arm", "gated_research", model=model, **{**spec["options"], **bad})


def test_sessions_are_shared_per_run_and_resume_from_the_store(world):
    web, serp, context, tmp_path = world
    a = session_for(context, lf.SEARCH_SPEC, lf.FETCH_OPTIONS)
    assert session_for(context, lf.SEARCH_SPEC, lf.FETCH_OPTIONS) is a
    a.gather(lf.Q_EXAMPLE)
    requests_before = len(web.requests)
    clear_sessions()  # a new process resuming the same run directory
    resumed = session_for(rc.RunContext(run_dir=tmp_path / "run"), lf.SEARCH_SPEC, lf.FETCH_OPTIONS)
    evidence = resumed.gather(lf.Q_EXAMPLE)
    assert serp.queries == [lf.Q_EXAMPLE] and len(web.requests) == requests_before  # nothing searched or fetched again
    assert all(is_cache_hit(c) for c in evidence.calls) and len(evidence.documents) == 1


def test_dry_run_sessions_use_no_network_and_worst_case_pages(world):
    web, serp, context, tmp_path = world
    dry = rc.RunContext(run_dir=None, dry_run=True)
    _b, gated = arms(dry)
    out = gated.run(item())
    assert serp.urls == [] and web.requests == []
    assert len(out.attempts[0].evidence) == 5
    shown = [m for m in lf.MODEL_CALLS[-1][1].messages[1].content.split("[Document ")[1:]]
    assert len(shown) == 5
    assert not (tmp_path / "run").exists()
