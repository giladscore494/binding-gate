"""The runner: one config file -> item x arm x model x repeat -> one trace record each.

Implements the requirements of ARCHITECTURE.md section 8:

1. Arm exceptions become a ``TraceRecord`` with ``error`` (and the model calls
   made so far); no item is skipped.
2. Arms receive ``item.arm_input()`` only; gold never leaves the runner/scorers.
3. **Response cache.** Within a run, the response to an identical
   ``ModelRequest`` for the same model label and repeat index is reused. The
   cache key is ``SHA-256(canonical JSON of {client, repeat, request})``. A
   reused response is marked ``raw["cache_hit"] = true`` in the reusing call and
   costs nothing. On resume the cache is rebuilt from the trace file, so no
   separate cache file exists. On resume the decomposers' caches are also
   seeded from the decompose calls in the trace (PR 12), so no question is
   decomposed twice in one run.
4. Dev runs use ``split.select_dev``; test runs go through
   ``testlock.load_test_split`` (the only path to test items) and record the tag.
5. Retries are bounded and configured per model; every attempt is a logged
   ``ModelCall`` (see ``binding_gate.answering.call_model``).
6. Nothing an arm returns is repaired or discarded.

Execution settings come from the ``[runner]`` table of the run config:

``concurrency``       number of (item, model, repeat) units in flight (default 4).
``max_usd``           hard budget cap in US dollars PER MODEL LABEL for the run,
                      covering the calls of that model's records, auxiliary calls
                      (decomposer, judge, search) included. Required for a paid
                      run. The worst case for a whole run is the sum of the caps.
``max_usd_by_model``  optional table ``label -> cap`` overriding ``max_usd``.
``prices``            path of the price file (default ``configs/prices.toml``).
``seed_responses_from`` optional list of earlier dev run directories (paths
                      relative to the repository root) whose answer responses
                      are loaded into the response cache before the first unit
                      (PR 12; see "Seeding" below). The entry ``"seeds/*"``
                      (PR 13) stands for every run directory under ``seeds/``.
``max_searches``      optional (PR 13): at most this many web searches in one
                      invocation (see "Search quota" below).

**Budget cap.** After each record is written, the cost of its (non-cached)
model calls is added to the spend of its model, computed from the token usage
the provider REPORTED and the price file. When a model's spend reaches its cap,
no further unit of that model is started; units already in flight finish and are
written (the overshoot is at most ``concurrency`` calls). The run is then left
unfinished (resumable) and reported as aborted. A response without reported
usage is charged at the estimate "prompt characters / chars_per_token input +
max_output_tokens output", which errs on the expensive side.

**Dry run.** :func:`dry_run` loads the items, runs every arm against a recording
client that returns a canned abstention, and so builds and counts every request
without any network access. No trace is written.

**Auxiliary calls (PR 8).** Besides the model under test, arms make paid calls
to other endpoints: the decomposer's model (``purpose = "decompose"``), LLM
judges (``"check"``) and, in layer 3, the web search API (``"search"``) and the
free page fetcher (``"fetch"``). :func:`auxiliary_clients` finds them in the
arm options (``decomposer``, ``judges``, ``search``). Every call in a trace
record is priced by the model or provider id IN THE RESPONSE
(``raw["requested_model"]``, else ``ModelResponse.model``; the client label is
only a fallback), and is charged to the budget cap of the record's model label,
so auxiliary calls fall under the same caps. Before the first paid call the
runner requires a price for every auxiliary client, runs their free
preflights, and checks the low estimate (model under test + auxiliary calls)
against the cap. The dry run estimates auxiliary calls per model job: the
first-attempt searches and fetches it observes (live-web arms use no-network
stand-ins in a dry run), plus upper bounds for decompositions (one per distinct
question), judge calls (one per item and repeat) and re-search attempts
(``max_attempts - 1`` per item and repeat).

**Seeding (PR 12, opt-in).** With ``seed_responses_from``, the answer
responses of earlier dev runs are put into the response cache under the same
key as the live cache (model label, repeat, request), after the run's own trace
on resume. A run that sends the same answer request for the same model label
and repeat then reuses that response instead of paying for it again: it is
recorded with ``raw["cache_hit"] = true`` and ``raw["seeded_from"] = <seed run
id>`` and costs nothing. This lets ``dev_gated``, the ablations and the
comparison baselines reuse the ``no_gate`` answers of the dev baseline. Seeds
are refused unless they are dev runs on the same dataset pin with the same
model specs and request format, and never in a test run or a live-web run
(:func:`load_seed_responses`). The dry run lists how many responses each seed
provides and, since PR 13, leaves them out of its cost estimates (so the low
estimate checked against the cap before a paid run counts only the calls that
are really paid). The wildcard ``"seeds/*"`` (PR 13) expands to every directory
under ``seeds/`` holding a ``manifest.json`` (sorted by name), so that a config
can name its seeds before their run ids exist: the workflow downloads the runs
of the workflow run given in ``seed_run_id`` into ``seeds/``. A paid run refuses
when the wildcard matches nothing; a dry run then estimates without seeds and
says so.

**Search quota (PR 13).** With ``max_searches`` set, the runner starts no unit
that could take the number of web searches made in this invocation (calls with
purpose ``search`` that were not cache hits) above the limit, counting every
unit in flight at its upper bound (the sum of ``max_attempts`` over the
live-web arms). The limit is lowered to the searches the provider's free
account check reports as left, when it reports them. The run is then left
unfinished and is resumed after the provider's monthly quota has renewed
(PREREGISTRATION.md section 3, layer 3, item 7: SerpApi's Free plan).

**Run context (PR 8).** Arms are built inside
``binding_gate.research.context.activate`` so that live-web arms can store
snapshots under ``runs/<run_id>/snapshots/`` (dry run: in memory).

Arms and scorers must be safe to call from several threads.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from binding_gate import registry
from binding_gate.canonical import canonical_json, sha256_json, utc_now_iso
from binding_gate.config import ComponentSpec, RunConfig
from binding_gate.contracts import (
    Item,
    ModelCall,
    ModelRequest,
    ModelResponse,
    ScoreResult,
    Split,
    TokenUsage,
    TraceRecord,
)
from binding_gate.pricing import PriceBook, PricingError, load_prices
from binding_gate.research import context as run_context
from binding_gate.split import select_dev
from binding_gate.testlock import load_test_split
from binding_gate.models._http import BALANCE_EXHAUSTED_PREFIX
from binding_gate.trace import TRACES_NAME, TraceWriter, read_traces

INVOCATIONS_NAME = "invocations.jsonl"
CACHE_HIT_KEY = "cache_hit"
SCORER_ERROR_LABEL = "scorer_error"
SEEDED_FROM_KEY = "seeded_from"
_RUNNER_KEYS = {"concurrency", "max_usd", "max_usd_by_model", "prices", "seed_responses_from", "max_searches"}
SEED_WILDCARD = "seeds/*"
"""``[runner].seed_responses_from`` entry for every run directory under ``seeds/`` (PR 13)."""
_DRY_RUN_TEXT = '{"status": "abstained", "reason": "dry run: no model was called"}'

Log = Callable[[str], None]


class RunnerError(RuntimeError):
    """Raised when a run must not start or cannot continue."""


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def _usd(where: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise RunnerError(f"{where} must be a number > 0 (US dollars)")
    return float(value)


@dataclass(frozen=True)
class RunnerSettings:
    """The validated ``[runner]`` table of a run config."""

    concurrency: int = 4
    max_usd: float | None = None
    max_usd_by_model: dict[str, float] = field(default_factory=dict)
    prices: str = "configs/prices.toml"
    seed_responses_from: tuple[str, ...] = ()
    max_searches: int | None = None

    @classmethod
    def from_config(cls, config: RunConfig) -> RunnerSettings:
        raw = config.runner
        unknown = sorted(set(raw) - _RUNNER_KEYS)
        if unknown:
            raise RunnerError(f"[runner]: unknown key(s) {unknown}; allowed: {sorted(_RUNNER_KEYS)}")
        concurrency = raw.get("concurrency", 4)
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 64:
            raise RunnerError("[runner].concurrency must be an integer between 1 and 64")
        max_usd = None if raw.get("max_usd") is None else _usd("[runner].max_usd", raw["max_usd"])
        by_model_raw = raw.get("max_usd_by_model", {})
        if not isinstance(by_model_raw, dict):
            raise RunnerError("[runner].max_usd_by_model must be a table of label -> dollars")
        labels = {spec.label for spec in config.models}
        strangers = sorted(set(by_model_raw) - labels)
        if strangers:
            raise RunnerError(f"[runner].max_usd_by_model names unknown model label(s) {strangers}")
        by_model = {k: _usd(f"[runner].max_usd_by_model.{k}", v) for k, v in by_model_raw.items()}
        prices = raw.get("prices", "configs/prices.toml")
        if not isinstance(prices, str) or not prices:
            raise RunnerError("[runner].prices must be a path")
        seeds = _seed_paths(raw.get("seed_responses_from", []))
        max_searches = raw.get("max_searches")
        if max_searches is not None and (
            isinstance(max_searches, bool) or not isinstance(max_searches, int) or max_searches < 1
        ):
            raise RunnerError("[runner].max_searches must be an integer >= 1")
        return cls(
            concurrency=concurrency, max_usd=max_usd, max_usd_by_model=by_model, prices=prices,
            seed_responses_from=seeds, max_searches=max_searches,
        )

    def cap_for(self, label: str) -> float | None:
        return self.max_usd_by_model.get(label, self.max_usd)


def _seed_paths(raw: Any) -> tuple[str, ...]:
    """Validate ``[runner].seed_responses_from``: relative run-directory paths, no duplicates."""
    where = "[runner].seed_responses_from"
    if not isinstance(raw, list) or not all(isinstance(p, str) and p.strip() for p in raw):
        raise RunnerError(f"{where} must be a list of run directory paths (strings)")
    for path in raw:
        if "*" in path and path != SEED_WILDCARD:
            raise RunnerError(f"{where}: {path!r}: the only pattern allowed is {SEED_WILDCARD!r}")
        pure = Path(path)
        if pure.is_absolute() or ".." in pure.parts:
            raise RunnerError(f"{where}: {path!r} must be a path relative to the repository root, without '..'")
    if len(set(raw)) != len(raw):
        raise RunnerError(f"{where} lists a run directory twice")
    if SEED_WILDCARD in raw and len(raw) > 1:
        raise RunnerError(f"{where}: {SEED_WILDCARD!r} already names every seed run; list nothing else")
    return tuple(raw)


def expand_seed_paths(paths: Sequence[str], repo_root: str | Path) -> tuple[str, ...]:
    """``seed_responses_from`` with :data:`SEED_WILDCARD` replaced by the run directories under ``seeds/``."""
    out: list[str] = []
    for path in paths:
        if path != SEED_WILDCARD:
            out.append(path)
            continue
        base = Path(repo_root) / "seeds"
        if base.is_dir():
            out += [f"seeds/{d.name}" for d in sorted(base.iterdir()) if d.is_dir() and (d / "manifest.json").is_file()]
    return tuple(out)


# --------------------------------------------------------------------------- #
# Loading items and components
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Plan:
    """The items of a run and where they came from."""

    items: tuple[Item, ...]
    dataset_fingerprint: str

    @property
    def n_groups(self) -> int:
        return len({item.group_id for item in self.items})


def _first_groups(items: Sequence[Item], limit: int) -> list[Item]:
    """Items of the first ``limit`` distinct groups, in input order."""
    kept: list[str] = []
    for item in items:
        if item.group_id not in kept:
            if len(kept) == limit:
                continue
            kept.append(item.group_id)
    wanted = set(kept)
    return [item for item in items if item.group_id in wanted]


def load_plan(
    config: RunConfig,
    *,
    repo_root: str | Path,
    test_unlock_tag: str | None = None,
    purpose: str | None = None,
) -> Plan:
    """Load the dataset and apply the split named in the config.

    Dev: ``split.select_dev`` (test items are dropped inside it, never returned).
    Test: only through ``testlock.load_test_split``, which refuses unless the
    pre-registration is frozen and tagged and which logs every attempt.
    """
    adapter = registry.create("dataset", config.dataset.name, **config.dataset.options)
    fingerprint = adapter.fingerprint()
    if config.split is Split.DEV:
        if test_unlock_tag is not None:
            raise RunnerError("a test unlock tag was given but the config's split is 'dev'")
        items = select_dev(adapter.iter_items())
        if config.limit is not None:
            items = _first_groups(items, config.limit)
    else:
        if not test_unlock_tag:
            raise RunnerError(
                "the config's split is 'test': a freeze tag (--test-unlock-tag) and a purpose are "
                "required, and the request is logged in audit/test_access_log.jsonl"
            )
        items = load_test_split(
            adapter.iter_items(), repo_root=repo_root, confirm_tag=test_unlock_tag, purpose=purpose or ""
        )
    if config.sample is not None:
        from binding_gate.sampling import stratified_sample

        items = stratified_sample(items, per_stratum=config.sample["per_stratum"], seed=config.sample["seed"])
    if not items:
        raise RunnerError("the dataset yielded no items for this split")
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise RunnerError("the dataset yielded duplicate item ids")
    return Plan(items=tuple(items), dataset_fingerprint=fingerprint)


def select_models(config: RunConfig, selection: str | None) -> tuple[ComponentSpec, ...]:
    """All models for ``None``/"all", otherwise the one whose label is ``selection``.

    A comma-separated list of labels selects several models (PR 8: the workflow
    runs a live-web config as ONE job over every model whose API key is set, so
    that all models share one rate-limited search budget and the same evidence).
    """
    if selection is None or selection == "all":
        return config.models
    wanted = [part.strip() for part in selection.split(",")] if "," in selection else [selection]
    labels = [s.label for s in config.models]
    unknown = [w for w in wanted if w not in labels]
    if unknown or not wanted:
        raise RunnerError(f"no model with label {unknown[0] if unknown else selection!r} in the config; labels: {labels}")
    return tuple(spec for spec in config.models if spec.label in wanted)


def build_model(spec: ComponentSpec) -> Any:
    """Create a model client; the runner passes the config label as ``label``."""
    return registry.create("model", spec.name, label=spec.label, **spec.options)


def model_id_of(client: Any) -> str:
    return getattr(client, "model_id", None) or client.name


# --------------------------------------------------------------------------- #
# Response cache
# --------------------------------------------------------------------------- #


def cache_key(client: str, repeat: int, request: ModelRequest) -> str:
    """Key of the response cache: same client label, same repeat, identical request."""
    return sha256_json({"client": client, "repeat": repeat, "request": request.to_dict()})


class ResponseCache:
    """Thread-safe map from :func:`cache_key` to the first response obtained for it."""

    def __init__(self) -> None:
        self._responses: dict[str, ModelResponse] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def put(self, key: str, response: ModelResponse) -> None:
        with self._guard:
            self._responses.setdefault(key, response)

    def get_or_call(self, key: str, call: Callable[[], ModelResponse]) -> tuple[ModelResponse, bool]:
        """Return ``(response, was_cached)``; concurrent identical requests call once."""
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            with self._guard:
                cached = self._responses.get(key)
            if cached is not None:
                return cached, True
            response = call()
            with self._guard:
                self._responses[key] = response
            return response, False

    def __len__(self) -> int:
        return len(self._responses)


class CachedClient:
    """A model client bound to one repeat index that answers repeated requests from the cache."""

    def __init__(self, inner: Any, repeat: int, cache: ResponseCache):
        self._inner = inner
        self._repeat = repeat
        self._cache = cache
        self.name = inner.name
        self.retry = getattr(inner, "retry", None)

    def complete(self, request: ModelRequest) -> ModelResponse:
        key = cache_key(self.name, self._repeat, request)
        response, cached = self._cache.get_or_call(key, lambda: self._inner.complete(request))
        if not cached:
            return response
        return ModelResponse(
            text=response.text,
            model=response.model,
            usage=response.usage,
            raw={**response.raw, CACHE_HIT_KEY: True},
        )


def is_cache_hit(call: ModelCall) -> bool:
    return call.response is not None and call.response.raw.get(CACHE_HIT_KEY) is True


# --------------------------------------------------------------------------- #
# Seeding the response cache from earlier runs (PR 12)
# --------------------------------------------------------------------------- #

TRANSPORT_OPTIONS = frozenset({"timeout_seconds", "max_retries", "backoff_seconds", "max_backoff_seconds"})
"""Model options that change how a request is delivered, never what is sent or answered.
They are ignored when a seed run's model spec is compared with the current one."""


@dataclass(frozen=True)
class SeedSummary:
    """What one seed run directory contributed (written to ``invocations.jsonl`` and the dry run)."""

    path: str
    run_id: str
    closed: bool
    responses_by_model: dict[str, int]
    unmatched_answer_calls: int

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _comparable_model(spec: ComponentSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "options": {k: v for k, v in spec.options.items() if k not in TRANSPORT_OPTIONS},
    }


def _uses_live_web(config: RunConfig) -> bool:
    return any(spec.options.get("search") is not None for spec in config.arms)


def _request_hash(request: ModelRequest) -> str:
    return sha256_json(request.to_dict())


def expected_answer_requests(config: RunConfig, plan: Plan) -> frozenset[str]:
    """Hashes of the answer requests this run sends first for its items (no network, no key).

    Every arm is run once per item against a recording client that abstains, as
    in the dry run; answer requests do not depend on the model or the repeat.
    """
    recorder = _RecordingClient("seed-check")
    with run_context.activate(run_context.RunContext(run_dir=None, dry_run=True)):
        arms = _build_arms(config, CachedClient(recorder, 0, ResponseCache()))
    for item in plan.items:
        for _label, arm in arms:
            arm.run(item.arm_input())
    return frozenset(_request_hash(request) for request in recorder.requests)


def load_seed_responses(
    config: RunConfig,
    settings: RunnerSettings,
    *,
    repo_root: str | Path,
    plan: Plan,
    specs: Sequence[ComponentSpec],
    expected: frozenset[str],
    exclude_run_id: str | None = None,
) -> tuple[dict[str, ModelResponse], list[SeedSummary]]:
    """Answer responses of earlier runs, keyed exactly like the live cache (opt-in, ``seed_responses_from``).

    Returns ``(cache key -> response, one summary per seed)``. A seeded
    response carries ``raw["seeded_from"] = <seed run id>``; when the run uses
    it, :class:`CachedClient` marks it ``raw["cache_hit"] = true`` like any
    reused response, so it costs nothing and the trace shows where it came from.

    Refused (:class:`RunnerError`, before any paid call and before a run
    directory is created) when the current run is not a dev run; when a seed
    directory has no manifest or trace, is the run being resumed, was run on
    another split or with a test unlock tag, holds a record of another split or
    run, or was modified after it was closed; when the dataset spec or the
    dataset fingerprint recorded in its ``invocations.jsonl`` differ from this
    run's; when a model label selected here has another model spec there
    (transport options aside: :data:`TRANSPORT_OPTIONS`); when either config
    uses the live web (answers depend on the evidence gathered in that run); and
    when a seed has answer calls for a selected model but none of them is a
    request this run sends (the prompt format or presentation differs).

    Used: successful, non-cached ``answer`` calls of the model under test
    (``call.client == record.model``) for the selected labels whose request is
    one this run sends. The key is :func:`cache_key` of (model label, repeat,
    request). When several seeds hold the same key, the first listed wins; the
    run's own trace (on resume) wins over every seed.
    """
    from binding_gate.config import parse_config
    from binding_gate.trace import read_manifest

    paths = expand_seed_paths(settings.seed_responses_from, repo_root)
    if not paths:
        return {}, []
    if config.split is not Split.DEV:
        raise RunnerError(
            "[runner].seed_responses_from is allowed only in dev runs: a test run makes all of its own calls"
        )
    if _uses_live_web(config):
        raise RunnerError(
            "[runner].seed_responses_from is not supported for live-web configs: their answers depend on the "
            "evidence gathered in each run"
        )
    selected = {spec.label: spec for spec in specs}
    seeded: dict[str, ModelResponse] = {}
    summaries: list[SeedSummary] = []
    for path_text in paths:
        where = f"seed {path_text}"
        run_dir = Path(repo_root) / path_text
        if not (run_dir / "manifest.json").is_file() or not (run_dir / TRACES_NAME).is_file():
            raise RunnerError(f"{where}: no manifest.json and traces.jsonl in {run_dir} (download the seed run first)")
        manifest = read_manifest(run_dir)
        if exclude_run_id is not None and manifest.run_id == exclude_run_id:
            raise RunnerError(f"{where} is the run being resumed; a run cannot seed itself")
        try:
            seed_config = parse_config(manifest.config)
        except Exception as exc:  # noqa: BLE001 - reported as a refusal
            raise RunnerError(f"{where}: its manifest holds an unreadable config: {_describe(exc)}") from exc
        if seed_config.split is not Split.DEV or manifest.test_unlock_tag is not None:
            raise RunnerError(
                f"{where} was run on the {seed_config.split.value!r} split; only dev runs can seed a dev run"
            )
        if seed_config.dataset != config.dataset:
            raise RunnerError(f"{where}: its dataset spec (name, pin, options) differs from this run's")
        if _uses_live_web(seed_config):
            raise RunnerError(f"{where} is a live-web run; its answers cannot seed another run")
        fingerprints = {
            entry.get("dataset_fingerprint")
            for entry in read_invocations(run_dir)
            if entry.get("event") in ("started", "resumed")
        }
        if not fingerprints:
            raise RunnerError(f"{where}: no {INVOCATIONS_NAME} entry records its dataset fingerprint")
        if fingerprints != {plan.dataset_fingerprint}:
            raise RunnerError(
                f"{where}: dataset fingerprint {sorted(map(str, fingerprints))} differs from this run's "
                f"{plan.dataset_fingerprint}"
            )
        seed_models = {spec.label: spec for spec in seed_config.models}
        for label, spec in selected.items():
            other = seed_models.get(label)
            if other is not None and _comparable_model(other) != _comparable_model(spec):
                raise RunnerError(
                    f"{where}: model label {label!r} has another model spec there "
                    f"({_comparable_model(other)} vs {_comparable_model(spec)})"
                )
        if manifest.traces_sha256 is not None:
            import hashlib

            digest = hashlib.sha256((run_dir / TRACES_NAME).read_bytes()).hexdigest()
            if digest != manifest.traces_sha256:
                raise RunnerError(f"{where}: traces.jsonl does not match the SHA-256 in its manifest")
        usable = {label for label in selected if label in seed_models}
        counts = {label: 0 for label in sorted(usable)}
        unmatched = 0
        candidates = 0
        matched = 0
        for record in read_traces(run_dir):
            if record.run_id != manifest.run_id:
                raise RunnerError(f"{where}: a trace record belongs to another run ({record.run_id})")
            if record.split is not Split.DEV:
                raise RunnerError(f"{where}: a trace record is from the {record.split.value!r} split")
            if record.model not in usable:
                continue
            for call in record.model_calls:
                if call.purpose != "answer" or call.client != record.model:
                    continue
                if call.response is None or is_cache_hit(call):
                    continue
                candidates += 1
                if _request_hash(call.request) not in expected:
                    unmatched += 1
                    continue
                matched += 1
                key = cache_key(record.model, record.repeat, call.request)
                if key in seeded:
                    continue
                seeded[key] = ModelResponse(
                    text=call.response.text,
                    model=call.response.model,
                    usage=call.response.usage,
                    raw={**call.response.raw, SEEDED_FROM_KEY: manifest.run_id},
                )
                counts[record.model] += 1
        if candidates and not matched:
            raise RunnerError(
                f"{where}: none of its {candidates} answer calls for the selected models is a request this run "
                "sends (the answer prompt format, the presentation or the items differ)"
            )
        summaries.append(
            SeedSummary(
                path=path_text,
                run_id=manifest.run_id,
                closed=manifest.ended_at is not None,
                responses_by_model=counts,
                unmatched_answer_calls=unmatched,
            )
        )
    return seeded, summaries


def seeded_totals(summaries: Sequence[SeedSummary], labels: Iterable[str]) -> dict[str, int]:
    """Seeded responses per selected model label over all seeds."""
    return {label: sum(s.responses_by_model.get(label, 0) for s in summaries) for label in labels}


# --------------------------------------------------------------------------- #
# Cost
# --------------------------------------------------------------------------- #


def _request_chars(request: ModelRequest) -> int:
    return sum(len(message.content) for message in request.messages)


def priced_id(call: ModelCall, prices: PriceBook, label_ids: dict[str, str]) -> str | None:
    """The price-book id of a logged call: the id in the response first, the label last.

    Order: ``response.raw["requested_model"]`` (the exact id the client was
    configured with and sent), ``response.model`` (the id the provider
    reported), then the configured id behind the call's client label. The first
    one with a price wins; ``None`` if none has a price.
    """
    candidates: list[Any] = []
    if call.response is not None:
        candidates += [call.response.raw.get("requested_model"), call.response.model]
    candidates.append(label_ids.get(call.client))
    for candidate in candidates:
        if isinstance(candidate, str) and prices.has(candidate):
            return candidate
    return None


@dataclass(frozen=True)
class CostModel:
    """Prices plus what is needed to charge a call whose usage was not reported."""

    prices: PriceBook
    model_ids: dict[str, str]
    max_output_tokens: dict[str, int]
    unpriced_clients: set[str] = field(default_factory=set)

    def call_cost(self, call: ModelCall) -> float:
        """Dollars for one logged call. Failed and cached calls cost nothing.

        Priced by :func:`priced_id` (the id in the response), so auxiliary calls
        (decomposer, judge, search) are charged like calls of the model under
        test. A call that cannot be priced is listed in the run summary instead
        of being charged silently; the runner refuses to start when it can see
        in advance that a client has no price.
        """
        if call.response is None or is_cache_hit(call):
            return 0.0
        model_id = priced_id(call, self.prices, self.model_ids)
        if model_id is None:
            self.unpriced_clients.add(call.client)
            return 0.0
        usage = call.response.usage
        if usage is None:
            usage = TokenUsage(
                self.prices.assumptions.input_tokens(_request_chars(call.request)),
                self.max_output_tokens.get(call.client, 0),
            )
        return self.prices.get(model_id).call_cost(usage.input_tokens, usage.output_tokens)

    def record_cost(self, record: TraceRecord) -> float:
        return sum(self.call_cost(call) for call in record.model_calls)


# --------------------------------------------------------------------------- #
# Auxiliary clients (decomposer, judges, search, fetch)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AuxiliaryClient:
    """A paid (or priced) endpoint used by an arm besides the model under test."""

    arm: str
    role: str  # "decomposer", "judge", "search", "fetch"
    purpose: str  # the ModelCall purpose of its calls
    client: Any
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.client.name

    @property
    def model_id(self) -> str:
        return model_id_of(self.client)

    @property
    def secret(self) -> str:
        return getattr(self.client, "secret_name", "") or ""


class _FetchInfo:
    """Describes the page fetcher for pricing; it has no key and no preflight."""

    secret_name = ""

    def __init__(self) -> None:
        from binding_gate.research.fetch import FETCH_MODEL_ID

        self.name = "fetch"
        self.model_id = FETCH_MODEL_ID
        self.max_output_tokens = None


def auxiliary_clients(config: RunConfig) -> tuple[list[AuxiliaryClient], list[str]]:
    """Every auxiliary client named in the arm options, and the problems met while building them.

    ``options.decomposer`` -> the decomposer's model client (if the decomposer
    has one, as ``model_decomposer`` does); ``options.judges`` -> every listed
    judge; ``options.search`` -> the web search client and the page fetcher.
    Building needs no API key. A component that cannot be built is reported as
    a problem (the arm itself will then fail with an error record) instead of
    stopping a dry run.
    """
    found: list[AuxiliaryClient] = []
    problems: list[str] = []
    for spec in config.arms:
        options = spec.options
        decomposer = options.get("decomposer")
        if isinstance(decomposer, dict) and isinstance(decomposer.get("name"), str):
            try:
                built = registry.create("decomposer", decomposer["name"], **dict(decomposer.get("options", {})))
            except Exception as exc:  # noqa: BLE001 - reported, see the docstring
                problems.append(f"arm {spec.label}: decomposer {decomposer['name']!r} cannot be built: {_describe(exc)}")
            else:
                client = getattr(built, "client", None)
                if client is not None:
                    found.append(AuxiliaryClient(spec.label, "decomposer", "decompose", client, {"decomposer": built, "spec": decomposer}))
        judges = options.get("judges")
        if judges is not None:
            from binding_gate.judging import parse_judges

            try:
                for judge in parse_judges(f"arm {spec.label}: judges", judges):
                    found.append(AuxiliaryClient(spec.label, "judge", "check", judge.build(), {"arm_options": options}))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"arm {spec.label}: judges cannot be built: {_describe(exc)}")
        search = options.get("search")
        if search is not None:
            from binding_gate.research.search import build_search_client

            try:
                found.append(AuxiliaryClient(spec.label, "search", "search", build_search_client(search)))
                found.append(AuxiliaryClient(spec.label, "fetch", "fetch", _FetchInfo()))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"arm {spec.label}: search client cannot be built: {_describe(exc)}")
    return found, problems


def distinct_clients(aux: Sequence[AuxiliaryClient]) -> list[AuxiliaryClient]:
    """One entry per (client class, label, model id, secret): what needs one preflight / key check."""
    seen: set[tuple[str, str, str, str]] = set()
    out = []
    for entry in aux:
        key = (type(entry.client).__name__, entry.label, entry.model_id, entry.secret)
        if key not in seen:
            seen.add(key)
            out.append(entry)
    return out


def missing_keys(config: RunConfig, selection: str | None) -> list[tuple[str, str]]:
    """``(label, secret name)`` of every selected model and every auxiliary client whose API key is not set."""
    from binding_gate import secrets

    missing = []
    for spec in select_models(config, selection):
        secret = getattr(build_model(spec), "secret_name", None)
        if secret and not secrets.has_secret(secret):
            missing.append((spec.label, secret))
    aux, _problems = auxiliary_clients(config)
    for entry in distinct_clients(aux):
        if entry.secret and not secrets.has_secret(entry.secret) and (entry.label, entry.secret) not in missing:
            missing.append((f"{entry.role} {entry.label} (arm {entry.arm})", entry.secret))
    return missing


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #


class _RecordingClient:
    """Stands in for a model during a dry run: records requests, calls nothing."""

    def __init__(self, name: str):
        self.name = name
        self.retry = None
        self.requests: list[ModelRequest] = []
        self._lock = threading.Lock()

    def complete(self, request: ModelRequest) -> ModelResponse:
        with self._lock:
            self.requests.append(request)
        return ModelResponse(text=_DRY_RUN_TEXT, model="dry-run")


@dataclass(frozen=True)
class ModelEstimate:
    label: str
    component: str
    model_id: str
    secret: str | None
    calls: int
    input_tokens: int
    max_output_tokens: int | None
    priced: bool
    cost_low: float | None
    cost_high: float | None
    cost_ceiling: float | None
    cap: float | None
    price_source: str | None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class AuxEstimate:
    """Estimated auxiliary calls of one kind for one model job (see :func:`dry_run`)."""

    role: str
    purpose: str
    client: str
    model_id: str | None
    calls_low: int
    calls_high: int
    input_tokens_low: int
    input_tokens_high: int
    max_output_tokens: int | None
    priced: bool
    cost_low: float | None
    cost_high: float | None
    cost_ceiling: float | None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class DryRunReport:
    config_name: str
    config_hash: str
    split: str
    dataset: str
    dataset_fingerprint: str
    n_groups: int
    n_items: int
    items_by_stratum: dict[str, int]
    repeats: int
    arms: tuple[str, ...]
    requests_per_repeat: int
    input_chars_per_repeat: int
    assumptions: dict[str, Any]
    prices_path: str
    prices_sha256: str
    models: tuple[ModelEstimate, ...]
    auxiliary: dict[str, tuple[AuxEstimate, ...]] = field(default_factory=dict)
    auxiliary_problems: tuple[str, ...] = ()
    seeds: tuple[SeedSummary, ...] = ()
    seed_note: str = ""
    seeded_calls: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["arms"] = list(self.arms)
        data["models"] = [m.to_dict() for m in self.models]
        data["auxiliary"] = {label: [a.to_dict() for a in rows] for label, rows in self.auxiliary.items()}
        data["auxiliary_problems"] = list(self.auxiliary_problems)
        data["seeds"] = [seed.to_dict() for seed in self.seeds]
        return data

    def total(self, name: str) -> float | None:
        values = [getattr(m, name) for m in self.models]
        return None if any(v is None for v in values) else sum(values)

    def auxiliary_total(self, label: str, name: str) -> float | None:
        """Sum of one cost column over the auxiliary rows of one model job (None if any is unpriced)."""
        values = [getattr(a, name) for a in self.auxiliary.get(label, ())]
        return None if any(v is None for v in values) else sum(values)

    def model_total(self, label: str, name: str) -> float | None:
        """Model under test plus auxiliary calls, for one model job."""
        model = next(m for m in self.models if m.label == label)
        own, aux = getattr(model, name), self.auxiliary_total(label, name)
        return None if own is None or aux is None else own + aux

    def format(self) -> str:
        def money(value: float | None) -> str:
            return "no price" if value is None else f"${value:,.2f}"

        a = self.assumptions
        lines = [
            "DRY RUN - no model API was called, nothing was spent, no trace was written.",
            f"config            : {self.config_name}  (hash {self.config_hash[:12]})",
            f"dataset           : {self.dataset}  (data hash {self.dataset_fingerprint[:12]})",
            f"split             : {self.split}",
            f"groups / items    : {self.n_groups} groups, {self.n_items} items",
            f"arms              : {', '.join(self.arms)}",
            f"repeats           : {self.repeats}",
            f"requests / repeat : {self.requests_per_repeat} per model "
            f"({self.input_chars_per_repeat:,} prompt characters)",
            f"prices            : {self.prices_path}  (sha256 {self.prices_sha256[:12]})",
            "token assumptions : input = characters / "
            f"{a['chars_per_token']}; visible output = {a['visible_output_tokens']} tokens per call; "
            f"hidden reasoning = {a['reasoning_tokens_low']} (low) to {a['reasoning_tokens_high']} (high) "
            "tokens per call, billed as output (a model id with its own reasoning_tokens_low/high in the "
            "price file uses those); ceiling = every call uses its full max_output_tokens.",
            "",
            f"{'model label':<24}{'model id':<26}{'calls':>7}{'input tok':>12}"
            f"{'low':>11}{'high':>11}{'ceiling':>11}{'cap':>10}",
        ]
        for m in self.models:
            lines.append(
                f"{m.label:<24}{m.model_id:<26}{m.calls:>7}{m.input_tokens:>12,}"
                f"{money(m.cost_low):>11}{money(m.cost_high):>11}{money(m.cost_ceiling):>11}"
                f"{('none' if m.cap is None else f'${m.cap:,.2f}'):>10}"
            )
        caps = [m.cap for m in self.models]
        cap_total = None if any(c is None for c in caps) else sum(caps)
        lines.append(
            f"{'TOTAL':<24}{'':<26}{sum(m.calls for m in self.models):>7}"
            f"{sum(m.input_tokens for m in self.models):>12,}"
            f"{money(self.total('cost_low')):>11}{money(self.total('cost_high')):>11}"
            f"{money(self.total('cost_ceiling')):>11}"
            f"{('none' if cap_total is None else f'${cap_total:,.2f}'):>10}"
        )
        if any(self.auxiliary.values()):
            lines.append("")
            lines.append("AUXILIARY CALLS per model job (decomposer, judge, web search, page fetch, re-search answers):")
            lines.append(
                f"{'model label':<16}{'role':<12}{'client':<14}{'model/provider id':<24}{'calls':>13}"
                f"{'low':>10}{'high':>10}{'ceiling':>10}"
            )
            for m in self.models:
                for a in self.auxiliary.get(m.label, ()):
                    lines.append(
                        f"{m.label:<16}{a.role:<12}{a.client:<14}{(a.model_id or '?'):<24}"
                        f"{f'{a.calls_low}-{a.calls_high}':>13}"
                        f"{money(a.cost_low):>10}{money(a.cost_high):>10}{money(a.cost_ceiling):>10}"
                    )
            lines.append("")
            lines.append(f"{'TOTAL per model job':<24}{'low':>11}{'high':>11}{'ceiling':>11}{'cap':>10}")
            for m in self.models:
                lines.append(
                    f"{m.label:<24}{money(self.model_total(m.label, 'cost_low')):>11}"
                    f"{money(self.model_total(m.label, 'cost_high')):>11}"
                    f"{money(self.model_total(m.label, 'cost_ceiling')):>11}"
                    f"{('none' if m.cap is None else f'${m.cap:,.2f}'):>10}"
                )
            lines.append(
                "Low = calls that happen whatever the model answers (first-attempt searches and fetches); "
                "high and ceiling add upper bounds: one decomposition per distinct question, one judge call "
                "per item and repeat, and max_attempts - 1 re-search attempts per item and repeat."
            )
        for problem in self.auxiliary_problems:
            lines.append(f"NOTE: {problem}")
        if self.seeds:
            lines.append("")
            lines.append(
                "SEEDED RESPONSES ([runner] seed_responses_from; reused at no cost and left out of the "
                "calls and costs above):"
            )
            for seed in self.seeds:
                state = "closed" if seed.closed else "unfinished"
                lines.append(f"  {seed.path}  (run {seed.run_id}, {state})")
            per_repeat = self.requests_per_repeat * self.repeats
            for m in self.models:
                count = self.seeded_calls.get(m.label, 0)
                lines.append(f"  {m.label:<22}{count:>7} of {per_repeat} answer calls seeded")
        if self.seed_note:
            lines.append("")
            lines.append(f"NOTE: {self.seed_note}")
        lines.append("")
        lines.append(
            "The cap column is the hard budget limit per model ([runner] max_usd); a paid run "
            "stops a model when its reported usage, auxiliary calls included, reaches it. "
            "Estimates are not measurements."
        )
        return "\n".join(lines)


def _build_scorers(config: RunConfig) -> list[Any]:
    return [registry.create("scorer", spec.name, **spec.options) for spec in config.scorers]


def _build_arms(config: RunConfig, client: Any) -> list[tuple[str, Any]]:
    return [
        (spec.label, registry.create("arm", spec.name, model=client, **spec.options))
        for spec in config.arms
    ]


def _price_aux(
    prices: PriceBook,
    *,
    role: str,
    purpose: str,
    client: str,
    model_id: str | None,
    calls: tuple[int, int],
    input_tokens: tuple[int, int],
    max_output_tokens: int | None,
    note: str = "",
) -> AuxEstimate:
    a = prices.assumptions
    low = high = ceiling = None
    priced = model_id is not None and prices.has(model_id)
    if priced:
        price = prices.get(model_id)  # type: ignore[arg-type]
        low = price.cost(input_tokens[0], calls[0] * (a.visible_output_tokens + prices.reasoning_low(model_id))) + price.calls_cost(calls[0])
        high = price.cost(input_tokens[1], calls[1] * (a.visible_output_tokens + prices.reasoning_high(model_id))) + price.calls_cost(calls[1])
        ceiling = (
            price.cost(input_tokens[1], calls[1] * max_output_tokens) + price.calls_cost(calls[1])
            if max_output_tokens
            else high
        )
    return AuxEstimate(
        role=role, purpose=purpose, client=client, model_id=model_id, calls_low=calls[0], calls_high=calls[1],
        input_tokens_low=input_tokens[0], input_tokens_high=input_tokens[1], max_output_tokens=max_output_tokens,
        priced=priced, cost_low=low, cost_high=high, cost_ceiling=ceiling, note=note,
    )


def _auxiliary_estimates(
    config: RunConfig,
    plan: Plan,
    prices: PriceBook,
    model_label: str,
    model_client: Any,
    arms: Sequence[tuple[str, Any]],
    aux: Sequence[AuxiliaryClient],
    observed: dict[tuple[str, str, str], list[int]],
    answer_chars: dict[str, int],
) -> list[AuxEstimate]:
    """Auxiliary rows of one model job (see the module docstring, "Auxiliary calls")."""
    assumptions = prices.assumptions
    n_items = len(plan.items)
    rows: list[AuxEstimate] = []
    live = [(label, arm) for label, arm in arms if hasattr(arm, "extra_attempt_bound")]
    extra_attempts = {label: n_items * config.repeats * (getattr(arm, "max_attempts", 1) - 1) for label, arm in live}
    bounds = {label: arm.extra_attempt_bound() for label, arm in live}
    fetch_per_attempt = max((b["fetch"] for b in bounds.values()), default=0)

    # Calls observed in the dry run (first attempts of live-web arms): searches and fetches.
    for (purpose, client, model_id), (calls, chars) in sorted(observed.items()):
        high = calls
        if purpose == "search":
            high += sum(extra_attempts[l] * bounds[l]["search"] for l in extra_attempts)
        elif purpose == "fetch":
            high = max(calls, n_items * fetch_per_attempt) + sum(extra_attempts[l] * bounds[l]["fetch"] for l in extra_attempts)
        tokens = assumptions.input_tokens(chars) if chars else 0
        rows.append(
            _price_aux(prices, role=purpose, purpose=purpose, client=client, model_id=model_id, calls=(calls, high),
                       input_tokens=(tokens, tokens), max_output_tokens=None,
                       note="observed in the dry run (shared by all arms and repeats of the run); high adds re-search bounds")
        )
    # Re-search attempts: further answer calls of the model under test.
    for label, extra in extra_attempts.items():
        if extra <= 0:
            continue
        calls = extra * bounds[label]["answer"]
        tokens = calls * assumptions.input_tokens(answer_chars.get(label, 0))
        rows.append(
            _price_aux(prices, role="re-answer", purpose="answer", client=model_label, model_id=model_id_of(model_client),
                       calls=(0, calls), input_tokens=(0, tokens), max_output_tokens=getattr(model_client, "max_output_tokens", None),
                       note=f"arm {label}: at most max_attempts - 1 further answer calls per item and repeat, largest prompt")
        )
    # Decomposers: at most one call per distinct question per process.
    questions = sorted({item.question for item in plan.items})
    done: set[str] = set()
    for entry in aux:
        if entry.role != "decomposer":
            continue
        key = canonical_json(entry.detail["spec"])
        if key in done:
            continue
        done.add(key)
        decomposer = entry.detail["decomposer"]
        request_for = getattr(decomposer, "request_for", None)
        chars = sum(_request_chars(request_for(q)) for q in questions) if request_for else 0
        rows.append(
            _price_aux(prices, role="decomposer", purpose="decompose", client=entry.label, model_id=entry.model_id,
                       calls=(0, len(questions)), input_tokens=(0, assumptions.input_tokens(chars)),
                       max_output_tokens=getattr(entry.client, "max_output_tokens", None),
                       note=f"arm {entry.arm}: at most one decomposition per distinct question (only answered candidates need one)")
        )
    # LLM judges (llm_judge_checks): at most one call per item and repeat.
    judged: set[str] = set()
    for entry in aux:
        if entry.role != "judge" or entry.arm in judged:
            continue
        judged.add(entry.arm)
        from binding_gate.arms.llm_judge_checks import PLACEHOLDER_CHARS, build_check_request, canonical_checks
        from binding_gate.contracts import AnswerCandidate

        placeholder = AnswerCandidate.answered(value="v" * PLACEHOLDER_CHARS, quote="q" * PLACEHOLDER_CHARS, source_id="x", raw="")
        checks = canonical_checks(entry.detail["arm_options"].get("checks"))
        chars = sum(_request_chars(build_check_request(i.question, i.documents, placeholder, checks)) for i in plan.items)
        calls = n_items * config.repeats
        rows.append(
            _price_aux(prices, role="judge", purpose="check", client=entry.label, model_id=entry.model_id,
                       calls=(0, calls), input_tokens=(0, assumptions.input_tokens(chars) * config.repeats),
                       max_output_tokens=getattr(entry.client, "max_output_tokens", None),
                       note=f"arm {entry.arm}: first listed judge, at most one call per item and repeat")
        )
    return rows


def dry_run(
    config: RunConfig,
    *,
    repo_root: str | Path,
    selection: str | None = None,
    test_unlock_tag: str | None = None,
    purpose: str | None = None,
) -> DryRunReport:
    """Build every request of the run without calling any API (see the module docstring)."""
    settings = RunnerSettings.from_config(config)
    prices = load_prices(Path(repo_root) / settings.prices)
    specs = select_models(config, selection)
    clients = [build_model(spec) for spec in specs]  # validates the options; needs no API key
    _build_scorers(config)
    plan = load_plan(config, repo_root=repo_root, test_unlock_tag=test_unlock_tag, purpose=purpose)
    aux, problems = auxiliary_clients(config)

    recorder = _RecordingClient("dry-run")
    with run_context.activate(run_context.RunContext(run_dir=None, dry_run=True)):
        arms = _build_arms(config, CachedClient(recorder, 0, ResponseCache()))
    observed: dict[tuple[str, str, str], list[int]] = {}
    answer_chars: dict[str, int] = {}
    for item in plan.items:
        for label, arm in arms:
            output = arm.run(item.arm_input())
            for call in output.model_calls:
                if call.purpose == "answer":
                    answer_chars[label] = max(answer_chars.get(label, 0), _request_chars(call.request))
                    continue
                if call.response is None or is_cache_hit(call):
                    continue
                entry = observed.setdefault((call.purpose, call.client, call.response.model), [0, 0])
                entry[0] += 1
                entry[1] += _request_chars(call.request) if call.purpose not in ("search", "fetch") else 0
    requests = len(recorder.requests)
    characters = sum(_request_chars(request) for request in recorder.requests)
    seeded, seeds = load_seed_responses(
        config, settings, repo_root=repo_root, plan=plan, specs=specs,
        expected=frozenset(_request_hash(r) for r in recorder.requests),
    )
    seed_note = ""
    if settings.seed_responses_from and not seeds:
        seed_note = (
            f"the config reuses earlier runs ({', '.join(settings.seed_responses_from)}) but none was found; "
            "this estimate assumes every answer call is paid, and a paid run refuses until the seed runs are "
            "downloaded (workflow input seed_run_id)"
        )

    assumptions = prices.assumptions
    request_tokens = [assumptions.input_tokens(_request_chars(request)) for request in recorder.requests]
    input_tokens_per_repeat = sum(request_tokens)
    estimates: list[ModelEstimate] = []
    auxiliary: dict[str, tuple[AuxEstimate, ...]] = {}
    seeded_calls: dict[str, int] = {}
    for spec, client in zip(specs, clients):
        model_id = model_id_of(client)
        calls = requests * config.repeats
        input_tokens = input_tokens_per_repeat * config.repeats
        skipped = 0
        if seeded:
            for repeat in range(config.repeats):
                for request, tokens in zip(recorder.requests, request_tokens):
                    if cache_key(spec.label, repeat, request) in seeded:
                        skipped += 1
                        input_tokens -= tokens
        seeded_calls[spec.label] = skipped
        calls -= skipped
        max_output = getattr(client, "max_output_tokens", None)
        priced = prices.has(model_id)
        low = high = ceiling = None
        source = None
        if priced:
            price = prices.get(model_id)
            source = f"{price.source} (accessed {price.accessed})"
            visible = assumptions.visible_output_tokens
            low = price.cost(input_tokens, calls * (visible + prices.reasoning_low(model_id)))
            high = price.cost(input_tokens, calls * (visible + prices.reasoning_high(model_id)))
            if max_output is not None:
                ceiling = price.cost(input_tokens, calls * max_output)
        estimates.append(
            ModelEstimate(
                label=spec.label,
                component=spec.name,
                model_id=model_id,
                secret=getattr(client, "secret_name", None) or None,
                calls=calls,
                input_tokens=input_tokens,
                max_output_tokens=max_output,
                priced=priced,
                cost_low=low,
                cost_high=high,
                cost_ceiling=ceiling,
                cap=settings.cap_for(spec.label),
                price_source=source,
            )
        )
        auxiliary[spec.label] = tuple(
            _auxiliary_estimates(config, plan, prices, spec.label, client, arms, aux, observed, answer_chars)
        )
    by_stratum: dict[str, int] = {}
    for item in plan.items:
        key = item.stratum or "(none)"
        by_stratum[key] = by_stratum.get(key, 0) + 1
    return DryRunReport(
        config_name=config.name,
        config_hash=config.config_hash,
        split=config.split.value,
        dataset=config.dataset.name,
        dataset_fingerprint=plan.dataset_fingerprint,
        n_groups=plan.n_groups,
        n_items=len(plan.items),
        items_by_stratum=by_stratum,
        repeats=config.repeats,
        arms=tuple(label for label, _arm in arms),
        requests_per_repeat=requests,
        input_chars_per_repeat=characters,
        assumptions=dict(assumptions.__dict__),
        prices_path=settings.prices,
        prices_sha256=prices.sha256,
        models=tuple(estimates),
        auxiliary=auxiliary,
        auxiliary_problems=tuple(problems),
        seeds=tuple(seeds),
        seed_note=seed_note,
        seeded_calls=seeded_calls,
    )


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #


EXTRAS_BY_COMPONENT: dict[tuple[str, str], str] = {
    ("arm", "faithfulness_checker"): "faithfulness",
    ("dataset", "sealqa"): "sealqa",
}
"""Optional dependency groups (pyproject ``[project.optional-dependencies]``) that a
component needs at run time or to prepare its data."""


def run_requirements(config: RunConfig) -> dict[str, Any]:
    """What a CI workflow needs to run ``config`` (printed by ``cli requirements``)."""
    from binding_gate.stats.prereg import LAYER_BY_DATASET

    extras = {EXTRAS_BY_COMPONENT[("dataset", config.dataset.name)]} if ("dataset", config.dataset.name) in EXTRAS_BY_COMPONENT else set()
    extras |= {EXTRAS_BY_COMPONENT[("arm", a.name)] for a in config.arms if ("arm", a.name) in EXTRAS_BY_COMPONENT}
    secret_names = {getattr(build_model(spec), "secret_name", "") for spec in config.models}
    aux, _problems = auxiliary_clients(config)
    secret_names |= {entry.secret for entry in aux}
    live = any(entry.role == "search" for entry in aux)
    faithfulness = any(a.name == "faithfulness_checker" for a in config.arms)
    weights = ""
    if faithfulness:
        from binding_gate.arms.faithfulness_checker import MINICHECK_CHECKPOINT, MINICHECK_WEIGHTS_REVISION

        weights = f"{MINICHECK_CHECKPOINT}@{MINICHECK_WEIGHTS_REVISION}"
    return {
        "extras": sorted(extras),
        "secrets": sorted(s for s in secret_names if s),
        "layer": LAYER_BY_DATASET.get(config.dataset.name, 0),
        "private_outputs": live,
        "single_job": live,
        "baseline_report": any(s.name == "provenance" for s in config.scorers),
        # PR 12: the MiniCheck preparation step of the workflow, and the cache key of its weights
        "faithfulness_checker": faithfulness,
        "faithfulness_weights": weights,
        # PR 12: run directories the workflow must download before the run ([runner] seed_responses_from)
        "seed_runs": list(RunnerSettings.from_config(config).seed_responses_from),
    }


def preflight_targets(config: RunConfig, selection: str | None = None) -> list[tuple[str, Any]]:
    """``(description, client)`` of every selected model and every distinct auxiliary client."""
    targets: list[tuple[str, Any]] = [(spec.label, build_model(spec)) for spec in select_models(config, selection)]
    aux, _problems = auxiliary_clients(config)
    for entry in distinct_clients(aux):
        targets.append((f"{entry.role} {entry.label} (arm {entry.arm})", entry.client))
    return targets


def preflight(config: RunConfig, selection: str | None = None) -> list[Any]:
    """Run the free check of every selected model and auxiliary client that offers one.

    Returns the list of ``PreflightResult``. Raises ``SecretError`` when a key is
    missing. Nothing billable is called.
    """
    results = []
    for _where, client in preflight_targets(config, selection):
        check = getattr(client, "preflight", None)
        if check is not None:
            results.append(check())
    return results


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


@dataclass
class RunSummary:
    run_dir: Path
    run_id: str
    records_written: int = 0
    records_already_present: int = 0
    error_records: int = 0
    spend_usd: dict[str, float] = field(default_factory=dict)
    aborted_models: list[str] = field(default_factory=list)
    unpriced_clients: list[str] = field(default_factory=list)
    closed: bool = False
    searches: int = 0
    search_limit: int | None = None
    search_limit_reached: bool = False
    balance_exhausted: bool = False
    balance_exhausted_records: int = 0

    @property
    def budget_exceeded(self) -> bool:
        """True when the run stopped early: a model's budget cap, the search quota (PR 13) or a
        provider's prepaid balance (PR 19) was reached."""
        return bool(self.aborted_models) or self.search_limit_reached or self.balance_exhausted

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_dir": self.run_dir.as_posix(),
            "run_id": self.run_id,
            "records_written": self.records_written,
            "records_already_present": self.records_already_present,
            "error_records": self.error_records,
            "spend_usd": {k: round(v, 6) for k, v in self.spend_usd.items()},
            "aborted_models": list(self.aborted_models),
            "unpriced_clients": list(self.unpriced_clients),
            "closed": self.closed,
            "searches": self.searches,
            "search_limit": self.search_limit,
            "search_limit_reached": self.search_limit_reached,
            "balance_exhausted": self.balance_exhausted,
            "balance_exhausted_records": self.balance_exhausted_records,
        }


def _describe(exc: BaseException) -> str:
    return (f"{type(exc).__name__}: {exc}"[:2000]).strip() or type(exc).__name__


def _append_invocation(run_dir: Path, entry: dict[str, Any]) -> None:
    with (run_dir / INVOCATIONS_NAME).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(entry) + "\n")


@dataclass(frozen=True)
class _Unit:
    item: Item
    model_label: str
    repeat: int


def _run_unit(
    unit: _Unit,
    arms: Sequence[tuple[str, Any]],
    scorers: Sequence[Any],
    done: frozenset[tuple[str, str, str, int]],
    identity: dict[str, Any],
    split: Split,
) -> list[TraceRecord]:
    """Run every not-yet-recorded arm for one (item, model, repeat); never raises for arm errors."""
    item = unit.item
    records: list[TraceRecord] = []
    for arm_label, arm in arms:
        if (item.id, arm_label, unit.model_label, unit.repeat) in done:
            continue
        common = dict(
            **identity,
            item_id=item.id,
            group_id=item.group_id,
            dataset=item.dataset,
            split=split,
            arm=arm_label,
            model=unit.model_label,
            repeat=unit.repeat,
            stratum=item.stratum,
        )
        started = utc_now_iso()
        try:
            output = arm.run(item.arm_input())
        except Exception as exc:  # noqa: BLE001 - requirement 1: record the failure, do not skip
            records.append(
                TraceRecord(
                    **common,
                    started_at=started,
                    ended_at=utc_now_iso(),
                    model_calls=tuple(getattr(exc, "model_calls", ())),
                    error=_describe(exc),
                )
            )
            continue
        scores: list[ScoreResult] = []
        for scorer in scorers:
            try:
                scores.append(scorer.score(item, output.final_answer))
            except Exception as exc:  # noqa: BLE001 - keep the arm output; make the failure visible
                scores.append(ScoreResult(scorer.name, SCORER_ERROR_LABEL, {"error": _describe(exc)}))
        records.append(
            TraceRecord(
                **common,
                started_at=started,
                ended_at=utc_now_iso(),
                constraints=output.constraints,
                attempts=output.attempts,
                final_answer=output.final_answer,
                model_calls=output.model_calls,
                scores=tuple(scores),
            )
        )
    return records


def _preload(run_dir: Path, cache: ResponseCache, cost: CostModel, spend: dict[str, float]) -> None:
    """On resume: rebuild the response cache and the spend per model from the trace file."""
    if not (run_dir / TRACES_NAME).exists():
        return
    for record in read_traces(run_dir):
        for call in record.model_calls:
            if call.response is not None and not is_cache_hit(call):
                cache.put(cache_key(call.client, record.repeat, call.request), call.response)
        if record.model in spend:
            spend[record.model] += cost.record_cost(record)


def _preload_decompositions(run_dir: Path, aux: Sequence[AuxiliaryClient], plan: Plan, log: Log) -> dict[str, int]:
    """On resume: seed each decomposer's process cache from the decompose calls in the trace.

    Pre-registration section 17, item 31. Without this a resumed segment starts
    with an empty decomposition cache and decomposes again every question the
    earlier segments had decomposed. Each decompose call recorded in the trace
    (the original or a cache-hit copy, which carries the same reply) is offered
    to every decomposer of the config with a process cache
    (``preload_from_call``); the one whose client label and request match takes
    it. The first recorded call per (decomposer, question) wins, which is the
    decomposition the earlier segment used for all its later records. Records
    whose constraints differ from it (possible only for traces written before
    this preload existed) are counted and reported, not changed.
    """
    counts = {"constraints": 0, "failure": 0, "inconsistent_records": 0}
    decomposers: dict[str, Any] = {}
    arm_decomposer: dict[str, str] = {}
    for entry in aux:
        if entry.role != "decomposer":
            continue
        built = entry.detail["decomposer"]
        if not hasattr(built, "preload_from_call") or getattr(built, "cache", None) is None:
            continue
        key = str(getattr(built, "config_hash", id(built)))
        decomposers.setdefault(key, built)
        arm_decomposer[entry.arm] = key
    if not decomposers or not (run_dir / TRACES_NAME).exists():
        return counts
    questions = {item.id: item.question for item in plan.items}
    done: set[tuple[str, str]] = set()
    records = list(read_traces(run_dir))
    for record in records:
        question = questions.get(record.item_id)
        if question is None:
            continue
        for call in record.model_calls:
            if call.purpose != "decompose" or call.response is None:
                continue
            for key, decomposer in decomposers.items():
                if (key, question) in done:
                    continue
                outcome = decomposer.preload_from_call(question, call)
                if outcome is not None:
                    done.add((key, question))
                    counts[outcome] += 1
                    break
    for record in records:
        key = arm_decomposer.get(record.arm)
        if key is None or record.constraints is None:
            continue
        cached = decomposers[key].cache.peek(record.constraints.question)
        if getattr(cached, "constraints", record.constraints) != record.constraints:
            counts["inconsistent_records"] += 1
    if counts["inconsistent_records"]:
        log(
            f"WARNING: {counts['inconsistent_records']} earlier record(s) carry constraints that differ from the "
            "first recorded decomposition of their question; the resumed segment uses the first one"
        )
    return counts


def execute_run(
    config: RunConfig,
    *,
    repo_root: str | Path,
    runs_dir: str | Path,
    confirm_spend: bool,
    config_path: str | Path | None = None,
    selection: str | None = None,
    resume_dir: str | Path | None = None,
    test_unlock_tag: str | None = None,
    purpose: str | None = None,
    log: Log = print,
) -> RunSummary:
    """Execute (or resume) a run. This is the only function that makes paid model calls.

    Order of safeguards before the first paid call: ``confirm_spend`` must be
    true; every selected model needs a budget cap and a price; the low cost
    estimate must fit under the cap; the free preflight of every selected model
    must confirm that its model id exists.
    """
    if not confirm_spend:
        raise RunnerError(
            "refusing to make paid model calls without explicit confirmation "
            "(pass --confirm-spend; use the dry-run command to see the estimate first)"
        )
    repo_root = Path(repo_root)
    settings = RunnerSettings.from_config(config)
    if settings.seed_responses_from and config.split is not Split.DEV:
        # Checked before anything else, so that no test-split access is logged for a run that must stop.
        raise RunnerError(
            "[runner].seed_responses_from is allowed only in dev runs: a test run makes all of its own calls"
        )
    if settings.seed_responses_from and not expand_seed_paths(settings.seed_responses_from, repo_root):
        raise RunnerError(
            f"[runner].seed_responses_from names {list(settings.seed_responses_from)} but no seed run directory "
            "was found under seeds/; download the seed runs first (workflow input seed_run_id), otherwise every "
            "answer would be paid again"
        )
    specs = select_models(config, selection)
    try:
        prices = load_prices(repo_root / settings.prices)
    except PricingError as exc:
        raise RunnerError(str(exc)) from exc
    clients = {spec.label: build_model(spec) for spec in specs}
    for spec in specs:
        if settings.cap_for(spec.label) is None:
            raise RunnerError(
                f"model {spec.label!r} has no budget cap: set [runner] max_usd in the run config"
            )
        if not prices.has(model_id_of(clients[spec.label])):
            raise RunnerError(
                f"{settings.prices} has no price for model id {model_id_of(clients[spec.label])!r}; "
                "the budget cap cannot be enforced without it"
            )
    aux, aux_problems = auxiliary_clients(config)
    for problem in aux_problems:
        log(f"WARNING: {problem}")
    for entry in distinct_clients(aux):
        if not prices.has(entry.model_id):
            raise RunnerError(
                f"{settings.prices} has no price for {entry.role} {entry.label!r} of arm {entry.arm!r} "
                f"(id {entry.model_id!r}); its calls could not be charged to the budget cap"
            )

    estimate = dry_run(
        config, repo_root=repo_root, selection=selection, test_unlock_tag=None, purpose=None
    ) if config.split is Split.DEV else None
    if estimate is not None:
        for model in estimate.models:
            low = estimate.model_total(model.label, "cost_low")
            if low is not None and model.cap is not None and low > model.cap:
                raise RunnerError(
                    f"model {model.label!r}: even the low cost estimate (${low:.2f}, auxiliary calls "
                    f"included) exceeds its budget cap (${model.cap:.2f}); raise the cap in the config or "
                    "reduce the run"
                )

    preflights = [(label, client) for label, client in clients.items()]
    preflights += [(f"{e.role} {e.label} (arm {e.arm})", e.client) for e in distinct_clients(aux)]
    for label, client in preflights:
        check = getattr(client, "preflight", None)
        if check is None:
            continue
        result = check()
        log(f"preflight {label}: {'OK' if result.ok else 'FAILED'} - {result.detail}")
        if not result.ok:
            raise RunnerError(f"preflight failed for {'model ' if label in clients else ''}{label!r}: {result.detail}")
    search_limit = settings.max_searches
    if search_limit is not None:
        for entry in distinct_clients(aux):
            left = getattr(entry.client, "searches_left", None)
            if entry.role == "search" and isinstance(left, int) and not isinstance(left, bool):
                if left < search_limit:
                    log(f"search quota: the account reports {left} searches left; this invocation uses at most {left}")
                search_limit = min(search_limit, left)
        log(f"search quota: at most {search_limit} web searches in this invocation ([runner] max_searches)")
    searches_per_unit = sum(
        int(spec.options.get("max_attempts", 1)) for spec in config.arms if spec.options.get("search") is not None
    )

    plan = load_plan(config, repo_root=repo_root, test_unlock_tag=test_unlock_tag, purpose=purpose)
    scorers = _build_scorers(config)
    seeded: dict[str, ModelResponse] = {}
    seeds: list[SeedSummary] = []
    if settings.seed_responses_from:
        from binding_gate.trace import read_manifest

        seeded, seeds = load_seed_responses(
            config, settings, repo_root=repo_root, plan=plan, specs=specs,
            expected=expected_answer_requests(config, plan),
            exclude_run_id=None if resume_dir is None else read_manifest(resume_dir).run_id,
        )
        for label, count in seeded_totals(seeds, [spec.label for spec in specs]).items():
            log(f"seeded responses for {label}: {count}" + ("" if count else " (WARNING: nothing to reuse)"))

    if resume_dir is None:
        writer = TraceWriter.start(
            runs_dir, config, repo_root=repo_root, config_path=config_path, test_unlock_tag=test_unlock_tag
        )
    else:
        writer = TraceWriter.resume(resume_dir, config, repo_root=repo_root)
    summary = RunSummary(run_dir=writer.run_dir, run_id=writer.manifest.run_id)
    summary.search_limit = search_limit
    summary.spend_usd = {spec.label: 0.0 for spec in specs}
    aux_ids = {e.label: e.model_id for e in aux}
    cost = CostModel(
        prices=prices,
        model_ids={**aux_ids, **{spec.label: model_id_of(build_model(spec)) for spec in config.models}},
        max_output_tokens={
            **{e.label: getattr(e.client, "max_output_tokens", 0) or 0 for e in aux},
            **{label: getattr(client, "max_output_tokens", 0) or 0 for label, client in clients.items()},
        },
    )
    cache = ResponseCache()
    try:
        _preload(writer.run_dir, cache, cost, summary.spend_usd)
        for key, response in seeded.items():
            cache.put(key, response)  # never replaces a response of this run's own trace
        decompositions = _preload_decompositions(writer.run_dir, aux, plan, log)
        done = writer.completed_keys()
        identity = {
            "run_id": writer.manifest.run_id,
            "config_hash": writer.manifest.config_hash,
            "git_commit": writer.manifest.git_commit,
        }
        _append_invocation(
            writer.run_dir,
            {
                "event": "started" if resume_dir is None else "resumed",
                "timestamp": utc_now_iso(),
                "selected_models": [spec.label for spec in specs],
                "dataset_fingerprint": plan.dataset_fingerprint,
                "n_groups": plan.n_groups,
                "n_items": len(plan.items),
                "prices_path": settings.prices,
                "prices_sha256": prices.sha256,
                "caps_usd": {spec.label: settings.cap_for(spec.label) for spec in specs},
                "concurrency": settings.concurrency,
                "records_already_present": len(done),
                "seeds": [seed.to_dict() for seed in seeds],
                "decompositions_preloaded": decompositions,
            },
        )
        arm_labels = [spec.label for spec in config.arms]
        with run_context.activate(run_context.RunContext(run_dir=writer.run_dir)):
            arms_by_model_repeat = {
                (label, repeat): _build_arms(config, CachedClient(client, repeat, cache))
                for label, client in clients.items()
                for repeat in range(config.repeats)
            }
        caps = {spec.label: settings.cap_for(spec.label) for spec in specs}
        aborted: set[str] = {
            label for label in caps if summary.spend_usd[label] >= caps[label]  # type: ignore[operator]
        }

        def units() -> Iterable[_Unit]:
            for repeat in range(config.repeats):
                for item in plan.items:
                    for spec in specs:
                        keys = [(item.id, arm, spec.label, repeat) for arm in arm_labels]
                        present = sum(key in done for key in keys)
                        summary.records_already_present += present
                        if present < len(keys):
                            yield _Unit(item, spec.label, repeat)

        def handle(finished: Iterable[Future[list[TraceRecord]]]) -> None:
            for future in finished:
                for record in future.result():
                    if record.error is not None and BALANCE_EXHAUSTED_PREFIX in record.error:
                        # PR 19: a provider's prepaid balance ran out. The record is NOT written
                        # (so a resume after topping up retries it) but is logged in
                        # invocations.jsonl; no further unit is started for any model.
                        summary.balance_exhausted = True
                        summary.balance_exhausted_records += 1
                        _append_invocation(
                            writer.run_dir,
                            {
                                "event": "balance_exhausted",
                                "at": utc_now_iso(),
                                "item_id": record.item_id,
                                "arm": record.arm,
                                "model": record.model,
                                "repeat": record.repeat,
                                "error": record.error[:500],
                            },
                        )
                        if summary.balance_exhausted_records == 1:
                            log(
                                f"BALANCE EXHAUSTED ({record.model}, arm {record.arm}): a provider reports "
                                "that the prepaid balance or quota is used up; no further calls are started. "
                                "Top up the account and resume this run with the same config and commit."
                            )
                        continue
                    writer.append(record)
                    summary.searches += sum(
                        1 for call in record.model_calls if call.purpose == "search" and not is_cache_hit(call)
                    )
                    summary.records_written += 1
                    summary.error_records += record.error is not None
                    summary.spend_usd[record.model] += cost.record_cost(record)
                    cap = caps[record.model]
                    if record.model not in aborted and summary.spend_usd[record.model] >= cap:  # type: ignore[operator]
                        aborted.add(record.model)
                        log(
                            f"BUDGET CAP REACHED for {record.model}: reported usage costs "
                            f"${summary.spend_usd[record.model]:.4f} >= cap ${cap:.2f}; "
                            "no further calls are started for this model"
                        )

        with ThreadPoolExecutor(max_workers=settings.concurrency) as pool:
            pending: set[Future[list[TraceRecord]]] = set()
            try:
                for unit in units():
                    if summary.balance_exhausted:
                        break
                    if unit.model_label in aborted:
                        continue
                    if search_limit is not None and searches_per_unit:
                        if summary.searches + searches_per_unit * (len(pending) + 1) > search_limit:
                            if not summary.search_limit_reached:
                                log(
                                    f"SEARCH QUOTA REACHED: {summary.searches} searches made, at most "
                                    f"{searches_per_unit} per unit and {len(pending)} unit(s) in flight, limit "
                                    f"{search_limit}; no further unit is started (resume after the quota renews)"
                                )
                            summary.search_limit_reached = True
                            break
                    pending.add(
                        pool.submit(
                            _run_unit,
                            unit,
                            arms_by_model_repeat[(unit.model_label, unit.repeat)],
                            scorers,
                            done,
                            identity,
                            config.split,
                        )
                    )
                    if len(pending) >= settings.concurrency:
                        finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                        handle(finished)
                while pending:
                    finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                    handle(finished)
            except BaseException:
                for future in pending:
                    future.cancel()
                raise

        summary.aborted_models = sorted(aborted)
        summary.unpriced_clients = sorted(cost.unpriced_clients)
        if summary.unpriced_clients:
            log(f"WARNING: calls by {summary.unpriced_clients} could not be priced and are not in the spend")
        if not aborted and not summary.search_limit_reached and not summary.balance_exhausted:
            writer.close()
            summary.closed = True
    finally:
        if not writer.closed:
            writer._handle.close()  # leave the manifest open: the run is visibly unfinished
        snapshots = writer.run_dir / "snapshots"
        if (snapshots / "index.jsonl").exists():
            from binding_gate.research.snapshots import SnapshotStore

            log(f"snapshot publish manifest: {SnapshotStore(snapshots).write_publish_manifest()}")
        _append_invocation(
            writer.run_dir, {"event": "ended", "timestamp": utc_now_iso(), **summary.to_dict()}
        )
    return summary


def read_invocations(run_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(run_dir) / INVOCATIONS_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
