"""Token prices and cost arithmetic for the dry-run estimate and the budget cap.

Prices live in a versioned TOML file (``configs/prices.toml``), one ``[[price]]``
entry per exact model id, each with its source and the date it was read. Nothing
here is fetched from the network. Prices are list prices in US dollars per one
million tokens; batch discounts, cached-input discounts and long-context
surcharges are not modelled.

PR 8 added ``per_call_usd`` (optional, default 0): a fixed charge per successful
call, for endpoints billed per request rather than per token (web search APIs).
A search API is entered with its provider id (for example ``serpapi/google``),
``per_call_usd`` and zero token prices; the free page fetcher of
``binding_gate.research`` is entered with every price zero, so that its calls
are priced (at nothing) instead of being reported as unpriced. Files without the
key keep their meaning, so ``prices_version`` stays 1.

File format (``prices_version = 1``)::

    prices_version = 1

    [assumptions]                    # used only by the dry-run estimate
    chars_per_token = 4.0            # input tokens ~= prompt characters / this
    visible_output_tokens = 250      # the JSON answer itself
    reasoning_tokens_low = 0         # hidden reasoning/thinking tokens per call,
    reasoning_tokens_high = 2000     # billed as output: low and high assumption

    [[price]]
    model_id = "exact-model-id"
    input_per_mtok = 0.10
    output_per_mtok = 0.50
    source = "where the price was read"
    accessed = "2026-11-22"
    note = "optional"
    per_call_usd = 0.0               # optional (PR 8): fixed charge per successful call
"""

from __future__ import annotations

import hashlib
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

PRICES_VERSION = 1
_TOP_KEYS = {"prices_version", "assumptions", "price"}
_ASSUMPTION_KEYS = {"chars_per_token", "visible_output_tokens", "reasoning_tokens_low", "reasoning_tokens_high"}
_PRICE_KEYS = {"model_id", "input_per_mtok", "output_per_mtok", "source", "accessed", "note", "per_call_usd"}


class PricingError(ValueError):
    """Raised when the price file is malformed or lacks a needed model."""


@dataclass(frozen=True)
class Price:
    model_id: str
    input_per_mtok: float
    output_per_mtok: float
    source: str
    accessed: str
    note: str = ""
    per_call_usd: float = 0.0

    def cost(self, input_tokens: float, output_tokens: float) -> float:
        """US dollars for the given token counts (token prices only; see :meth:`calls_cost`)."""
        return (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / 1_000_000

    def calls_cost(self, calls: float) -> float:
        """US dollars of the fixed per-call charge for ``calls`` successful calls."""
        return calls * self.per_call_usd

    def call_cost(self, input_tokens: float, output_tokens: float) -> float:
        """US dollars for ONE successful call with the given token counts."""
        return self.cost(input_tokens, output_tokens) + self.per_call_usd


@dataclass(frozen=True)
class Assumptions:
    chars_per_token: float
    visible_output_tokens: int
    reasoning_tokens_low: int
    reasoning_tokens_high: int

    def input_tokens(self, characters: int) -> int:
        return math.ceil(characters / self.chars_per_token)


@dataclass(frozen=True)
class PriceBook:
    assumptions: Assumptions
    prices: Mapping[str, Price]
    sha256: str
    path: str

    def get(self, model_id: str) -> Price:
        try:
            return self.prices[model_id]
        except KeyError:
            raise PricingError(
                f"{self.path} has no [[price]] entry for model id {model_id!r}; "
                "add one (with source and date) before running this model"
            ) from None

    def has(self, model_id: str) -> bool:
        return model_id in self.prices


def _number(where: str, value: Any, *, minimum: float, exclusive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PricingError(f"{where} must be a number")
    if value < minimum or (exclusive and value == minimum):
        raise PricingError(f"{where} must be {'>' if exclusive else '>='} {minimum}")
    return value


def _count(where: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PricingError(f"{where} must be an integer >= 0")
    return value


def _text(where: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PricingError(f"{where} must be a non-empty string")
    return value


def parse_prices(raw: Mapping[str, Any], *, sha256: str = "", path: str = "<prices>") -> PriceBook:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise PricingError(f"{path}: unknown top-level key(s) {unknown}")
    if raw.get("prices_version") != PRICES_VERSION:
        raise PricingError(f"{path}: prices_version must be {PRICES_VERSION}")
    assumed = raw.get("assumptions")
    if not isinstance(assumed, Mapping) or set(assumed) != _ASSUMPTION_KEYS:
        raise PricingError(f"{path}: [assumptions] must have exactly the keys {sorted(_ASSUMPTION_KEYS)}")
    assumptions = Assumptions(
        chars_per_token=_number("assumptions.chars_per_token", assumed["chars_per_token"], minimum=0, exclusive=True),
        visible_output_tokens=_count("assumptions.visible_output_tokens", assumed["visible_output_tokens"]),
        reasoning_tokens_low=_count("assumptions.reasoning_tokens_low", assumed["reasoning_tokens_low"]),
        reasoning_tokens_high=_count("assumptions.reasoning_tokens_high", assumed["reasoning_tokens_high"]),
    )
    if assumptions.reasoning_tokens_high < assumptions.reasoning_tokens_low:
        raise PricingError(f"{path}: reasoning_tokens_high must not be below reasoning_tokens_low")
    entries = raw.get("price", [])
    if not isinstance(entries, list):
        raise PricingError(f"{path}: prices must be given as [[price]] tables")
    prices: dict[str, Price] = {}
    for index, entry in enumerate(entries):
        where = f"{path}: price[{index}]"
        if not isinstance(entry, Mapping):
            raise PricingError(f"{where} must be a table")
        unknown = sorted(set(entry) - _PRICE_KEYS)
        if unknown:
            raise PricingError(f"{where}: unknown key(s) {unknown}")
        model_id = _text(f"{where}.model_id", entry.get("model_id"))
        if model_id in prices:
            raise PricingError(f"{where}: duplicate model_id {model_id!r}")
        prices[model_id] = Price(
            model_id=model_id,
            input_per_mtok=_number(f"{where}.input_per_mtok", entry.get("input_per_mtok"), minimum=0),
            output_per_mtok=_number(f"{where}.output_per_mtok", entry.get("output_per_mtok"), minimum=0),
            source=_text(f"{where}.source", entry.get("source")),
            accessed=_text(f"{where}.accessed", entry.get("accessed")),
            note=str(entry.get("note", "")),
            per_call_usd=_number(f"{where}.per_call_usd", entry.get("per_call_usd", 0.0), minimum=0),
        )
    return PriceBook(assumptions=assumptions, prices=prices, sha256=sha256, path=path)


def load_prices(path: str | Path) -> PriceBook:
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PricingError(f"cannot read price file {path}: {exc}") from exc
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise PricingError(f"{path}: invalid TOML: {exc}") from exc
    return parse_prices(raw, sha256=hashlib.sha256(data).hexdigest(), path=path.as_posix())
