"""One decomposition per question within a run (used by ``model_decomposer``).

In the Deceptive Grounding layer every question occurs in 15 conditions (times
the number of triples of its (X, C) pair), and the runner runs every item for
every model and repeat. The gate must see the SAME constraints for all of them:
otherwise differences between conditions, models or repeats could come from a
different decomposition rather than from the answer. The decomposer therefore
keeps a cache keyed by

    SHA-256(canonical JSON of {"decomposer_config": <config hash>, "question": <question text>})

where the config hash covers everything that changes the decomposition (the
decomposer name, prompt version and prompt SHA-256, and the model client spec)
and the question text is used exactly as given (no normalisation).

What is cached
--------------
* a successful decomposition;
* a parse failure (``DecompositionFailed``): the one reply the model gave was
  unparseable, and asking again would be a second request, which the
  pre-registration forbids (it would amount to repairing by resampling).

A call that failed at the transport level after all retries (``ModelCallFailed``)
is NOT cached: no reply exists, so the next item asks again.

How cached results are logged
-----------------------------
The record whose call actually reached the model carries every logged
``ModelCall`` (failed retry attempts included). Every later use of the cached
result carries a copy of only the successful call, with
``response.raw["cache_hit"] = true`` (the runner's convention, see
``binding_gate.runner.CACHE_HIT_KEY``), so the trace shows where the
constraints came from while cost and failed-call counts are not double-counted.

Scope
-----
:func:`shared_cache` returns one cache per decomposer config hash for the whole
Python process. The runner builds one arm (and so one decomposer) per model and
repeat; sharing the cache by config hash is what gives all of them the same
decomposition. One CLI invocation runs one run segment, so "process" means "run
segment".

Resume (PR 12)
--------------
A resumed run is a new process with an empty cache. Before the first unit of a
resumed segment, the runner reads the decompose calls already in the run's
trace and seeds the cache with them
(``ModelDecomposer.preload_from_call``, which re-parses each recorded reply with
the same deterministic parser and stores the result with :meth:`DecompositionCache.preload`
or, for a recorded parse failure, :meth:`DecompositionCache.preload_failure`).
So no question is decomposed twice in one run, and every segment uses the
decomposition of the first one. The first recorded call for a question wins.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from binding_gate.canonical import sha256_json
from binding_gate.contracts import Decomposition, ModelCall, ModelResponse

CACHE_HIT_KEY = "cache_hit"
"""Same key as ``binding_gate.runner.CACHE_HIT_KEY`` (a test checks that they agree)."""


def cache_key(config_hash: str, question: str) -> str:
    return sha256_json({"decomposer_config": config_hash, "question": question})


def as_cache_hit(calls: tuple[ModelCall, ...]) -> tuple[ModelCall, ...]:
    """Copies of the successful calls in ``calls``, flagged ``raw["cache_hit"] = true``."""
    copies = []
    for call in calls:
        if call.response is None:
            continue
        response = ModelResponse(
            text=call.response.text,
            model=call.response.model,
            usage=call.response.usage,
            raw={**call.response.raw, CACHE_HIT_KEY: True},
        )
        copies.append(
            ModelCall(
                purpose=call.purpose,
                client=call.client,
                request=call.request,
                started_at=call.started_at,
                ended_at=call.ended_at,
                response=response,
            )
        )
    return tuple(copies)


@dataclass
class CacheStats:
    computed: int = 0
    hits: int = 0
    failures_cached: int = 0


class DecompositionCache:
    """Thread-safe map ``question -> decomposition`` for one decomposer configuration.

    ``compute(question)`` must return a :class:`Decomposition` or raise. An
    exception whose class is listed in ``cacheable_errors`` is stored and
    re-raised (rebuilt by ``rebuild_error`` with cache-hit calls) on every later
    use; any other exception propagates and nothing is stored.
    """

    def __init__(self, config_hash: str):
        self.config_hash = config_hash
        self.stats = CacheStats()
        self._entries: dict[str, Decomposition | BaseException] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    def key(self, question: str) -> str:
        return cache_key(self.config_hash, question)

    def preload(self, decomposition: Decomposition) -> None:
        """Seed the cache with a decomposition obtained earlier (e.g. read back from a trace)."""
        with self._guard:
            self._entries.setdefault(self.key(decomposition.constraints.question), decomposition)

    def preload_failure(self, question: str, error: BaseException) -> None:
        """Seed the cache with a cacheable failure obtained earlier for ``question`` (e.g. from a trace).

        Later uses re-raise it exactly as if it had been computed in this
        process (through ``rebuild_error`` of :meth:`get_or_compute`).
        """
        with self._guard:
            self._entries.setdefault(self.key(question), error)

    def peek(self, question: str) -> Decomposition | BaseException | None:
        """What is cached for ``question`` (a decomposition, a cached failure, or ``None``); no side effect."""
        with self._guard:
            return self._entries.get(self.key(question))

    def get_or_compute(
        self,
        question: str,
        compute: Callable[[str], Decomposition],
        *,
        cacheable_errors: tuple[type[BaseException], ...] = (),
        rebuild_error: Callable[[BaseException], BaseException] | None = None,
    ) -> Decomposition:
        key = self.key(question)
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            with self._guard:
                entry = self._entries.get(key)
            if entry is None:
                try:
                    result = compute(question)
                except cacheable_errors as exc:
                    with self._guard:
                        self._entries[key] = exc
                        self.stats.computed += 1
                        self.stats.failures_cached += 1
                    raise
                with self._guard:
                    self._entries[key] = result
                    self.stats.computed += 1
                return result
            with self._guard:
                self.stats.hits += 1
        if isinstance(entry, BaseException):
            if rebuild_error is None:
                raise entry
            raise rebuild_error(entry)
        return Decomposition(constraints=entry.constraints, model_calls=as_cache_hit(entry.model_calls))


_SHARED: dict[str, DecompositionCache] = {}
_SHARED_GUARD = threading.Lock()


def shared_cache(config_hash: str) -> DecompositionCache:
    """The process-wide cache of one decomposer configuration (see the module docstring)."""
    with _SHARED_GUARD:
        cache = _SHARED.get(config_hash)
        if cache is None:
            cache = _SHARED[config_hash] = DecompositionCache(config_hash)
        return cache


def clear_shared_caches() -> None:
    """Forget every shared cache (for tests and for a process that starts a second run)."""
    with _SHARED_GUARD:
        _SHARED.clear()
