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
   separate cache file exists.
4. Dev runs use ``split.select_dev``; test runs go through
   ``testlock.load_test_split`` (the only path to test items) and record the tag.
5. Retries are bounded and configured per model; every attempt is a logged
   ``ModelCall`` (see ``binding_gate.answering.call_model``).
6. Nothing an arm returns is repaired or discarded.

Execution settings come from the ``[runner]`` table of the run config:

``concurrency``       number of (item, model, repeat) units in flight (default 4).
``max_usd``           hard budget cap in US dollars PER MODEL LABEL for the run.
                      Required for a paid run. The worst case for a whole run is
                      the sum of the caps of its models.
``max_usd_by_model``  optional table ``label -> cap`` overriding ``max_usd``.
``prices``            path of the price file (default ``configs/prices.toml``).

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
from binding_gate.split import select_dev
from binding_gate.testlock import load_test_split
from binding_gate.trace import TRACES_NAME, TraceWriter, read_traces

INVOCATIONS_NAME = "invocations.jsonl"
CACHE_HIT_KEY = "cache_hit"
SCORER_ERROR_LABEL = "scorer_error"
_RUNNER_KEYS = {"concurrency", "max_usd", "max_usd_by_model", "prices"}
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
        return cls(concurrency=concurrency, max_usd=max_usd, max_usd_by_model=by_model, prices=prices)

    def cap_for(self, label: str) -> float | None:
        return self.max_usd_by_model.get(label, self.max_usd)


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
    if not items:
        raise RunnerError("the dataset yielded no items for this split")
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise RunnerError("the dataset yielded duplicate item ids")
    return Plan(items=tuple(items), dataset_fingerprint=fingerprint)


def select_models(config: RunConfig, selection: str | None) -> tuple[ComponentSpec, ...]:
    """All models for ``None``/"all", otherwise the one whose label is ``selection``."""
    if selection is None or selection == "all":
        return config.models
    chosen = tuple(spec for spec in config.models if spec.label == selection)
    if not chosen:
        raise RunnerError(
            f"no model with label {selection!r} in the config; labels: {[s.label for s in config.models]}"
        )
    return chosen


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
# Cost
# --------------------------------------------------------------------------- #


def _request_chars(request: ModelRequest) -> int:
    return sum(len(message.content) for message in request.messages)


@dataclass(frozen=True)
class CostModel:
    """Prices plus what is needed to charge a call whose usage was not reported."""

    prices: PriceBook
    model_ids: dict[str, str]
    max_output_tokens: dict[str, int]
    unpriced_clients: set[str] = field(default_factory=set)

    def call_cost(self, call: ModelCall) -> float:
        """Dollars for one logged call. Failed and cached calls cost nothing."""
        if call.response is None or is_cache_hit(call):
            return 0.0
        model_id = self.model_ids.get(call.client)
        if model_id is None or not self.prices.has(model_id):
            # A client that is not a model of this config (none exists in PR 2). It cannot be
            # priced, so it is listed in the run summary instead of being charged silently.
            self.unpriced_clients.add(call.client)
            return 0.0
        usage = call.response.usage
        if usage is None:
            usage = TokenUsage(
                self.prices.assumptions.input_tokens(_request_chars(call.request)),
                self.max_output_tokens.get(call.client, 0),
            )
        return self.prices.get(model_id).cost(usage.input_tokens, usage.output_tokens)

    def record_cost(self, record: TraceRecord) -> float:
        return sum(self.call_cost(call) for call in record.model_calls)


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

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["arms"] = list(self.arms)
        data["models"] = [m.to_dict() for m in self.models]
        return data

    def total(self, name: str) -> float | None:
        values = [getattr(m, name) for m in self.models]
        return None if any(v is None for v in values) else sum(values)

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
            "tokens per call, billed as output; ceiling = every call uses its full max_output_tokens.",
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
        lines.append("")
        lines.append(
            "The cap column is the hard budget limit per model ([runner] max_usd); a paid run "
            "stops a model when its reported usage reaches it. Estimates are not measurements."
        )
        return "\n".join(lines)


def _build_scorers(config: RunConfig) -> list[Any]:
    return [registry.create("scorer", spec.name, **spec.options) for spec in config.scorers]


def _build_arms(config: RunConfig, client: Any) -> list[tuple[str, Any]]:
    return [
        (spec.label, registry.create("arm", spec.name, model=client, **spec.options))
        for spec in config.arms
    ]


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

    recorder = _RecordingClient("dry-run")
    arms = _build_arms(config, CachedClient(recorder, 0, ResponseCache()))
    for item in plan.items:
        for _label, arm in arms:
            arm.run(item.arm_input())
    requests = len(recorder.requests)
    characters = sum(_request_chars(request) for request in recorder.requests)

    assumptions = prices.assumptions
    input_tokens_per_repeat = sum(
        assumptions.input_tokens(_request_chars(request)) for request in recorder.requests
    )
    estimates: list[ModelEstimate] = []
    for spec, client in zip(specs, clients):
        model_id = model_id_of(client)
        calls = requests * config.repeats
        input_tokens = input_tokens_per_repeat * config.repeats
        max_output = getattr(client, "max_output_tokens", None)
        priced = prices.has(model_id)
        low = high = ceiling = None
        source = None
        if priced:
            price = prices.get(model_id)
            source = f"{price.source} (accessed {price.accessed})"
            visible = assumptions.visible_output_tokens
            low = price.cost(input_tokens, calls * (visible + assumptions.reasoning_tokens_low))
            high = price.cost(input_tokens, calls * (visible + assumptions.reasoning_tokens_high))
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
    )


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #


def preflight(config: RunConfig, selection: str | None = None) -> list[Any]:
    """Run the free model-id check of every selected model that offers one.

    Returns the list of ``PreflightResult``. Raises ``SecretError`` when a key is
    missing. Nothing billable is called.
    """
    results = []
    for spec in select_models(config, selection):
        client = build_model(spec)
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

    @property
    def budget_exceeded(self) -> bool:
        return bool(self.aborted_models)

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

    estimate = dry_run(
        config, repo_root=repo_root, selection=selection, test_unlock_tag=None, purpose=None
    ) if config.split is Split.DEV else None
    if estimate is not None:
        for model in estimate.models:
            if model.cost_low is not None and model.cap is not None and model.cost_low > model.cap:
                raise RunnerError(
                    f"model {model.label!r}: even the low cost estimate (${model.cost_low:.2f}) "
                    f"exceeds its budget cap (${model.cap:.2f}); raise the cap in the config or "
                    "reduce the run"
                )

    for label, client in clients.items():
        check = getattr(client, "preflight", None)
        if check is None:
            continue
        result = check()
        log(f"preflight {label}: {'OK' if result.ok else 'FAILED'} - {result.detail}")
        if not result.ok:
            raise RunnerError(f"preflight failed for model {label!r}: {result.detail}")

    plan = load_plan(config, repo_root=repo_root, test_unlock_tag=test_unlock_tag, purpose=purpose)
    scorers = _build_scorers(config)

    if resume_dir is None:
        writer = TraceWriter.start(
            runs_dir, config, repo_root=repo_root, config_path=config_path, test_unlock_tag=test_unlock_tag
        )
    else:
        writer = TraceWriter.resume(resume_dir, config, repo_root=repo_root)
    summary = RunSummary(run_dir=writer.run_dir, run_id=writer.manifest.run_id)
    summary.spend_usd = {spec.label: 0.0 for spec in specs}
    cost = CostModel(
        prices=prices,
        model_ids={spec.label: model_id_of(build_model(spec)) for spec in config.models},
        max_output_tokens={
            label: getattr(client, "max_output_tokens", 0) or 0 for label, client in clients.items()
        },
    )
    cache = ResponseCache()
    try:
        _preload(writer.run_dir, cache, cost, summary.spend_usd)
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
            },
        )
        arm_labels = [spec.label for spec in config.arms]
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
                    writer.append(record)
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
                    if unit.model_label in aborted:
                        continue
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
        if not aborted:
            writer.close()
            summary.closed = True
    finally:
        if not writer.closed:
            writer._handle.close()  # leave the manifest open: the run is visibly unfinished
        _append_invocation(
            writer.run_dir, {"event": "ended", "timestamp": utc_now_iso(), **summary.to_dict()}
        )
    return summary


def read_invocations(run_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(run_dir) / INVOCATIONS_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
