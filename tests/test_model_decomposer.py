"""The registered model decomposer: registry discovery, logging, retries, failures and the per-run cache."""

from __future__ import annotations

import threading

import pytest

import decomposer_fakes
from binding_gate import registry, runner
from binding_gate.answering import ModelCallFailed
from binding_gate.contracts import Decomposition, QualifierKind
from binding_gate.decomposers import cache as cache_module
from binding_gate.decomposers.decomposition_format import build_decompose_request

QUESTION = "What does the literature say about using xanadrug1 to treat zeta syndrome 1?"
OTHER = "What does the literature say about using xanadrug2 to treat zeta syndrome 2?"


@pytest.fixture(autouse=True)
def _clean():
    cache_module.clear_shared_caches()
    decomposer_fakes.CALLS.clear()
    yield
    cache_module.clear_shared_caches()
    decomposer_fakes.CALLS.clear()


def make(behaviour: str = "reference", *, cache: str = "process", label: str = "decomposer", **model_options):
    return registry.create(
        "decomposer",
        "model_decomposer",
        model={"name": "decomposer_fake", "label": label, "options": {"behaviour": behaviour, **model_options}},
        cache=cache,
        sleep=lambda _seconds: None,
    )


# --- registration ---------------------------------------------------------------------------------- #


def test_only_the_model_decomposer_is_registered_and_it_satisfies_the_protocol():
    assert registry.available("decomposer") == ["model_decomposer"]
    decomposer = make()
    assert decomposer.name == "model_decomposer"
    assert callable(decomposer.decompose)
    assert decomposer.client.name == "decomposer"
    assert decomposer.describe()["prompt_version"] == 1


def test_default_label_and_option_validation():
    default = registry.create("decomposer", "model_decomposer", model={"name": "decomposer_fake"})
    assert default.client.name == "decomposer"
    with pytest.raises(TypeError):
        registry.create("decomposer", "model_decomposer")
    with pytest.raises(ValueError, match="must be a table"):
        registry.create("decomposer", "model_decomposer", model="decomposer_fake")
    with pytest.raises(ValueError, match="unknown key"):
        registry.create("decomposer", "model_decomposer", model={"name": "decomposer_fake", "model_id": "x"})
    with pytest.raises(ValueError, match="cache must be one of"):
        make(cache="forever")
    with pytest.raises(ValueError, match="unknown decomposer prompt version"):
        registry.create("decomposer", "model_decomposer", model={"name": "decomposer_fake"}, prompt_version=2)
    with pytest.raises(registry.RegistryError):
        registry.create("decomposer", "model_decomposer", model={"name": "no_such_client"})


# --- one decomposition ----------------------------------------------------------------------------- #


def test_decompose_returns_constraints_and_logs_the_call():
    decomposition = make(cache="none").decompose(QUESTION)
    assert isinstance(decomposition, Decomposition)
    c = decomposition.constraints
    assert c.question == QUESTION
    assert c.entity.name == "xanadrug1"
    assert [(q.kind, q.text) for q in c.qualifiers] == [(QualifierKind.OTHER, "zeta syndrome 1")]
    (call,) = decomposition.model_calls
    assert call.purpose == "decompose" and call.client == "decomposer"
    assert call.request == build_decompose_request(QUESTION)
    assert call.response is not None and call.response.usage.input_tokens == 800


def test_unparseable_reply_raises_with_reason_raw_text_and_calls():
    with pytest.raises(Exception) as caught:
        make("prose", cache="none").decompose(QUESTION)
    exc = caught.value
    assert type(exc).__name__ == "DecompositionFailed"
    assert "not exactly one JSON value" in exc.reason
    assert exc.raw.startswith("Here is the decomposition")
    assert len(exc.model_calls) == 1 and exc.model_calls[0].response.text == exc.raw
    assert exc.cached is False


def test_retries_are_logged_like_answer_calls():
    decomposition = make(cache="none", fail_first=2).decompose(QUESTION)
    calls = decomposition.model_calls
    assert [c.error is not None for c in calls] == [True, True, False]
    assert "HTTP 503" in calls[0].error


def test_permanent_failure_raises_model_call_failed_with_the_calls():
    with pytest.raises(ModelCallFailed) as caught:
        make("crash", cache="none").decompose(QUESTION)
    assert len(caught.value.model_calls) == 1 and "HTTP 400" in caught.value.model_calls[0].error
    with pytest.raises(ValueError):
        make().decompose("  ")


# --- the per-run cache ------------------------------------------------------------------------------ #


def test_one_decomposition_per_question_shared_by_every_instance_with_the_same_config():
    first, second = make(fail_first=1), make(fail_first=1)  # e.g. two arms (model x repeat)
    original = first.decompose(QUESTION)
    again = second.decompose(QUESTION)
    assert len(decomposer_fakes.CALLS) == 2  # one failed attempt + one success, nothing more
    assert again.constraints == original.constraints
    assert [c.error is not None for c in original.model_calls] == [True, False]
    (hit,) = again.model_calls  # only the successful call is copied, flagged as a cache hit
    assert hit.response.raw["cache_hit"] is True and runner.is_cache_hit(hit)
    assert not any(runner.is_cache_hit(c) for c in original.model_calls)
    assert hit.request == original.model_calls[-1].request
    assert first.cache is second.cache and first.cache.stats.computed == 1 and first.cache.stats.hits == 1
    other = first.decompose(OTHER)
    assert other.constraints.question == OTHER and len(decomposer_fakes.CALLS) == 4  # 1 failure + 1 success


def test_parse_failures_are_cached_but_transport_failures_are_not():
    broken = make("garbage")
    with pytest.raises(Exception) as first:
        broken.decompose(QUESTION)
    with pytest.raises(Exception) as second:
        make("garbage").decompose(QUESTION)
    assert len(decomposer_fakes.CALLS) == 1  # no second request for an unparseable reply
    assert first.value.cached is False and second.value.cached is True
    assert all(runner.is_cache_hit(c) for c in second.value.model_calls)
    assert second.value.reason == first.value.reason

    crashing = make("crash", label="other-label")
    for _ in range(2):
        with pytest.raises(ModelCallFailed):
            crashing.decompose(QUESTION)
    assert len(decomposer_fakes.CALLS) == 3  # asked again: no reply existed to cache


def test_cache_key_is_config_hash_and_exact_question_text():
    base = make()
    assert make().config_hash == base.config_hash
    assert make(cache="none").config_hash == base.config_hash  # the cache mode does not change results
    assert make(label="dec-2").config_hash != base.config_hash
    assert make(model_id="fake-dear").config_hash != base.config_hash
    base.decompose(QUESTION)
    make(label="dec-2").decompose(QUESTION)  # different config -> its own decomposition
    base.decompose(QUESTION + " ")  # different text -> its own decomposition
    assert len(decomposer_fakes.CALLS) == 3
    assert cache_module.cache_key(base.config_hash, QUESTION) != cache_module.cache_key(base.config_hash, QUESTION + " ")


def test_cache_none_asks_every_time_and_flip_shows_independent_repeats():
    decomposer = make("flip", cache="none")
    with pytest.raises(Exception):
        decomposer.decompose(QUESTION)
    assert decomposer.decompose(QUESTION).constraints.entity.name == "xanadrug1"
    assert len(decomposer_fakes.CALLS) == 2 and decomposer.cache is None


def test_concurrent_identical_questions_call_the_model_once():
    decomposer = make()
    results: list[Decomposition] = []
    lock = threading.Lock()

    def work():
        result = decomposer.decompose(QUESTION)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(decomposer_fakes.CALLS) == 1 and len(results) == 8
    assert len({r.constraints for r in results}) == 1
    assert sum(not runner.is_cache_hit(r.model_calls[0]) for r in results) == 1


def test_preload_seeds_the_cache_and_hit_key_matches_the_runner():
    assert cache_module.CACHE_HIT_KEY == runner.CACHE_HIT_KEY
    earlier = make(cache="none").decompose(QUESTION)
    decomposer_fakes.CALLS.clear()
    decomposer = make()
    decomposer.cache.preload(earlier)
    assert decomposer.decompose(QUESTION).constraints == earlier.constraints
    assert decomposer_fakes.CALLS == []
