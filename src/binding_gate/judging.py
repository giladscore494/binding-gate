"""Shared plumbing for the components that use an LLM as a judge (PR 5).

Two components use a judge model: the ``llm_judge_checks`` arm (the gate's
checks performed by an LLM) and the ``llm_judge`` secondary scorer
(PREREGISTRATION.md section 6.5). Both need the same four things, kept here so
that the rule is written once:

1. **The judge-family rule** (PREREGISTRATION.md section 5): a judge must come
   from a model family different from the model under test. The family of a
   client is derived from its ``provider`` attribute (set by every PR 2 HTTP
   client) through :data:`FAMILY_BY_PROVIDER`. Judges are configured as an
   ordered list; :func:`choose_judge` takes the first one whose family is known
   and differs from the family of the model under test. If none qualifies, the
   component refuses (no silent substitution). If the family of the model
   under test cannot be determined, nothing is judged either.
2. **A hard budget cap for judge calls.** The PR 2 runner charged only the
   models listed in ``[[models]]`` of a run config. Since PR 8 it also prices
   judge calls (by the model id in the response) and charges them to the cap of
   the record's model; this separate cap is kept as a judge-specific limit. :class:`JudgeBudget`
   charges every judge call from the token usage the provider reports (or a
   conservative estimate when none is reported) and refuses further calls once
   the cap is reached.
3. **A free preflight** of the judge model id, done once per process before
   the first judge call.
4. **JSON extraction** for strict-JSON judge replies (:func:`json_objects_with_key`)
   and the benchmark's removal of ``<think>`` blocks (:func:`strip_think`).

Nothing here calls a model by itself.
"""

from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from binding_gate import registry
from binding_gate.contracts import ModelCall

FAMILY_BY_PROVIDER: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "google",
    "moonshot": "moonshot",
}
"""Provider of a model client -> model family used by the judge-family rule.
Every PR 2 client declares ``provider``; a client whose provider is not listed
has an unknown family and can neither be judged nor act as a judge."""


class JudgeError(RuntimeError):
    """Base class. ``model_calls`` holds every call made for the item so far, so
    the runner writes them into the error record (ARCHITECTURE.md section 8)."""

    def __init__(self, message: str, model_calls: Sequence[ModelCall] = ()):
        super().__init__(message)
        self.model_calls = tuple(model_calls)


class JudgeFamilyError(JudgeError):
    """No configured judge satisfies the judge-family rule for this model under test."""


class JudgeBudgetExceeded(JudgeError):
    """The hard budget cap for judge calls has been reached; no further call is made."""


class JudgeUnavailable(JudgeError):
    """The judge's free preflight failed (for example an unknown model id)."""


class JudgeOutputUnparseable(JudgeError):
    """The judge replied, but not with the required structure. Never repaired."""


# --------------------------------------------------------------------------- #
# Families and judge selection
# --------------------------------------------------------------------------- #


def unwrap_client(client: Any) -> Any:
    """The provider client behind the runner's per-repeat cache wrapper.

    ``binding_gate.runner.CachedClient`` keeps the wrapped client in ``_inner``
    and does not forward ``provider``. Reading that attribute here is the one
    place where PR 5 relies on the wrapper's internals; if it changes, the family
    becomes unknown and judging refuses (it never guesses).
    """
    seen = 0
    while hasattr(client, "_inner") and seen < 5:
        client = client._inner
        seen += 1
    return client


def model_family(client: Any) -> str | None:
    """Family of a model client, or ``None`` if it cannot be determined."""
    provider = getattr(unwrap_client(client), "provider", None)
    if not isinstance(provider, str):
        return None
    return FAMILY_BY_PROVIDER.get(provider)


@dataclass(frozen=True)
class JudgeSpec:
    """One configured judge: ``{name = <model component>, label = ..., options = {...}}``."""

    name: str
    label: str
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, where: str, raw: Any) -> JudgeSpec:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{where} must be a table with name, label and options")
        unknown = sorted(set(raw) - {"name", "label", "options"})
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {unknown}")
        name, label, options = raw.get("name"), raw.get("label"), raw.get("options", {})
        if not isinstance(name, str) or not name:
            raise ValueError(f"{where}.name must be a non-empty string (a registered model component)")
        if not isinstance(label, str) or not label:
            raise ValueError(f"{where}.label must be a non-empty string (written to traces as the client)")
        if not isinstance(options, Mapping):
            raise ValueError(f"{where}.options must be a table")
        return cls(name=name, label=label, options=json.loads(json.dumps(dict(options))))

    def build(self) -> Any:
        """Create the judge client. Needs no API key (keys are read at call time)."""
        return registry.create("model", self.name, label=self.label, **self.options)


def parse_judges(where: str, raw: Any) -> tuple[JudgeSpec, ...]:
    """An ordered, non-empty list of judge specs with unique labels."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise ValueError(f"{where} must be a non-empty list of judge tables (first = preferred)")
    specs = tuple(JudgeSpec.parse(f"{where}[{i}]", entry) for i, entry in enumerate(raw))
    labels = [s.label for s in specs]
    if len(set(labels)) != len(labels):
        raise ValueError(f"{where}: judge labels must be unique")
    return specs


def choose_judge(judges: Sequence[tuple[JudgeSpec, Any]], under_test_family: str | None) -> tuple[JudgeSpec, Any]:
    """The first ``(spec, client)`` whose family is known and differs from the model under test.

    Raises :class:`JudgeFamilyError` when the family of the model under test is
    unknown or no judge qualifies.
    """
    if under_test_family is None:
        raise JudgeFamilyError(
            "the model family of the model under test cannot be determined, so the judge-family "
            "rule (PREREGISTRATION.md section 5) cannot be checked; nothing is judged"
        )
    rejected = []
    for spec, client in judges:
        family = model_family(client)
        if family is not None and family != under_test_family:
            return spec, client
        rejected.append(f"{spec.label} (family {family or 'unknown'})")
    raise JudgeFamilyError(
        f"no configured judge comes from a family different from the model under test "
        f"(family {under_test_family}); rejected: {', '.join(rejected)}. Add a judge of another "
        "family to the list; no judge is substituted silently"
    )


# --------------------------------------------------------------------------- #
# Budget cap for judge calls
# --------------------------------------------------------------------------- #


def _request_chars(call: ModelCall) -> int:
    return sum(len(message.content) for message in call.request.messages)


@dataclass
class JudgeBudget:
    """Thread-safe spend counter with a hard cap, in US dollars.

    ``input_per_mtok`` / ``output_per_mtok`` are list prices per million tokens.
    A call without reported usage is charged as ``ceil(prompt characters /
    chars_per_token)`` input tokens plus ``max_output_tokens`` output tokens,
    which errs on the expensive side (the same rule as the PR 2 runner).
    """

    max_usd: float
    input_per_mtok: float
    output_per_mtok: float
    max_output_tokens: int
    chars_per_token: float = 4.0
    spent_usd: float = 0.0
    calls_charged: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def call_cost(self, call: ModelCall) -> float:
        if call.response is None:
            return 0.0  # a failed call returns no tokens and is not billed
        usage = call.response.usage
        if usage is None:
            tokens_in = math.ceil(_request_chars(call) / self.chars_per_token)
            tokens_out = self.max_output_tokens
        else:
            tokens_in, tokens_out = usage.input_tokens, usage.output_tokens
        return (tokens_in * self.input_per_mtok + tokens_out * self.output_per_mtok) / 1_000_000

    def ensure_available(self, model_calls: Sequence[ModelCall] = ()) -> None:
        """Raise :class:`JudgeBudgetExceeded` if the cap has been reached."""
        with self._lock:
            if self.spent_usd >= self.max_usd:
                raise JudgeBudgetExceeded(
                    f"judge budget cap reached: ${self.spent_usd:.4f} spent >= cap ${self.max_usd:.2f}; "
                    "no further judge call is made",
                    model_calls,
                )

    def charge(self, calls: Sequence[ModelCall]) -> float:
        cost = sum(self.call_cost(call) for call in calls)
        with self._lock:
            self.spent_usd += cost
            self.calls_charged += len(calls)
        return cost


_BUDGETS: dict[tuple[Any, ...], JudgeBudget] = {}
_BUDGETS_LOCK = threading.Lock()


def shared_budget(key: tuple[Any, ...], factory: Any) -> JudgeBudget:
    """One budget per key per process, so that the many arm instances the runner
    builds (one per model and repeat) share a single cap."""
    with _BUDGETS_LOCK:
        budget = _BUDGETS.get(key)
        if budget is None:
            budget = factory()
            _BUDGETS[key] = budget
        return budget


def reset_shared_state() -> None:
    """Forget all shared budgets and preflight results (for tests)."""
    with _BUDGETS_LOCK:
        _BUDGETS.clear()
    with _PREFLIGHT_LOCK:
        _PREFLIGHTS.clear()


def judge_price(prices_path: str | Path, model_id: str) -> tuple[float, float, float]:
    """``(input_per_mtok, output_per_mtok, chars_per_token)`` of ``model_id`` from a price file."""
    from binding_gate.pricing import load_prices

    book = load_prices(prices_path)
    price = book.get(model_id)
    return price.input_per_mtok, price.output_per_mtok, book.assumptions.chars_per_token


# --------------------------------------------------------------------------- #
# Preflight once per process
# --------------------------------------------------------------------------- #

_PREFLIGHTS: dict[tuple[str, str], tuple[bool, str]] = {}
_PREFLIGHT_LOCK = threading.Lock()


def preflight_once(client: Any) -> None:
    """Run the judge client's free model-id check once per process; raise if it fails."""
    check = getattr(client, "preflight", None)
    if check is None:
        return
    key = (client.name, str(getattr(client, "model_id", client.name)))
    with _PREFLIGHT_LOCK:
        if key not in _PREFLIGHTS:
            result = check()
            _PREFLIGHTS[key] = (bool(result.ok), str(result.detail))
        ok, detail = _PREFLIGHTS[key]
    if not ok:
        raise JudgeUnavailable(f"judge {client.name!r} failed its preflight: {detail}")


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #

_THINK = re.compile(r"<think>.*?(?:</think>|$)", flags=re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    """Remove ``<think>...</think>`` blocks and trim, as the Deceptive Grounding
    benchmark does before judging (``release_utils.answer_text``)."""
    return _THINK.sub("", text).strip()


def json_objects_with_key(text: str, key: str) -> list[dict[str, Any]]:
    """Every top-level JSON object in ``text`` that has ``key``, left to right.

    Same scan as the answer parser (``binding_gate.parsing``): at every ``{``
    not inside an object already decoded, try to decode one JSON value; text
    around the objects is ignored. Nothing is repaired.
    """
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    position = 0
    while True:
        start = text.find("{", position)
        if start < 0:
            return found
        try:
            value, end = decoder.raw_decode(text, start)
        except (json.JSONDecodeError, RecursionError):
            position = start + 1
            continue
        if isinstance(value, dict) and key in value:
            found.append(value)
        position = end
