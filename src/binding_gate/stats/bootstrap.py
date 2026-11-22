"""Statistic expressions and the cluster bootstrap (pure Python, deterministic).

Data layout
-----------
The analysis reduces every model's records to one row of **integer column
totals per cluster** (cluster = ``group_id``, the split unit: the (X, C) pair for
Deceptive Grounding). Each column is a sum over the items of the cluster of a
per-item quantity, such as "wrong-entity answers of arm ``gated``, averaged over
the repeats of that item". Per-item averages are made integers by multiplying
them by a common scale (see ``binding_gate.stats.metrics``); every statistic
below is a ratio or a difference of ratios, so the scale cancels.

A statistic is a small expression tree over column totals:

* :class:`Ratio` ``(sum of numerator columns) / (sum of denominator columns)``;
* :class:`Diff`  ``a - b`` of two statistics;
* :class:`RelativeReduction` ``1 - a / b``.

The same tree is evaluated exactly (``fractions.Fraction``, for point estimates
and threshold decisions) or in floating point (inside the bootstrap). A ratio
with a zero denominator is undefined (``None``), and so is anything built on it.

Cluster bootstrap
-----------------
For ``B`` resamples, draw ``G`` clusters with replacement from the ``G``
clusters of the model (``random.Random(seed).choices``), add up the column
totals of the drawn clusters (exact integer sums, so the order of summation
cannot matter), and evaluate every statistic on those totals. All statistics of
one model share the same resamples, so a paired difference between two arms is
resampled jointly, as the paired design requires.

The interval is the percentile interval at level ``L``: the ``(1-L)/2`` and
``(1+L)/2`` quantiles of the defined resample values, using linear
interpolation between order statistics (Hyndman and Fan type 7, the default of R
and NumPy): with sorted values ``v[0..m-1]`` and probability ``p``,
``h = (m-1)p``, ``q = v[floor(h)] + (h - floor(h)) * (v[floor(h)+1] - v[floor(h)])``.
Resamples in which a statistic is undefined are left out of its interval and
counted (``undefined``); the count is reported next to the interval.

Determinism: ``random.Random`` with an integer seed and ``choices`` produce
the same sequence on every platform and on every Python version since 3.6
(Mersenne Twister; ``choices`` is ``population[floor(random() * n)]``); the
golden-value test in ``tests/test_stats_bootstrap.py`` checks this on each CI
Python.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from fractions import Fraction
from operator import mul
from typing import Mapping, Sequence, Union

Number = Union[Fraction, float]


class StatsError(ValueError):
    """Raised when the data cannot support the requested analysis."""


# --------------------------------------------------------------------------- #
# Expressions
# --------------------------------------------------------------------------- #


class Expr:
    """A statistic of column totals."""

    def evaluate(self, totals: Sequence[int], *, exact: bool) -> Number | None:  # pragma: no cover
        raise NotImplementedError

    def columns(self) -> set[int]:  # pragma: no cover
        raise NotImplementedError


@dataclass(frozen=True)
class Ratio(Expr):
    numerator: tuple[int, ...]
    denominator: tuple[int, ...]

    def evaluate(self, totals: Sequence[int], *, exact: bool) -> Number | None:
        den = sum(totals[i] for i in self.denominator)
        if den == 0:
            return None
        num = sum(totals[i] for i in self.numerator)
        return Fraction(num, den) if exact else num / den

    def columns(self) -> set[int]:
        return set(self.numerator) | set(self.denominator)


@dataclass(frozen=True)
class Diff(Expr):
    a: Expr
    b: Expr

    def evaluate(self, totals: Sequence[int], *, exact: bool) -> Number | None:
        a = self.a.evaluate(totals, exact=exact)
        b = self.b.evaluate(totals, exact=exact)
        return None if a is None or b is None else a - b

    def columns(self) -> set[int]:
        return self.a.columns() | self.b.columns()


@dataclass(frozen=True)
class RelativeReduction(Expr):
    """``1 - a / b``; undefined when ``b`` is undefined or zero."""

    a: Expr
    b: Expr

    def evaluate(self, totals: Sequence[int], *, exact: bool) -> Number | None:
        a = self.a.evaluate(totals, exact=exact)
        b = self.b.evaluate(totals, exact=exact)
        if a is None or b is None or b == 0:
            return None
        return 1 - a / b

    def columns(self) -> set[int]:
        return self.a.columns() | self.b.columns()


# --------------------------------------------------------------------------- #
# Percentile interval
# --------------------------------------------------------------------------- #


def quantile_type7(sorted_values: Sequence[float], p: Fraction) -> float:
    """Type-7 quantile of already sorted values; ``p`` in [0, 1] as an exact fraction."""
    m = len(sorted_values)
    if m == 0:
        raise StatsError("no values")
    if not 0 <= p <= 1:
        raise StatsError("p must be in [0, 1]")
    h = (m - 1) * Fraction(p)
    low = math.floor(h)
    if low >= m - 1:
        return float(sorted_values[-1])
    weight = float(h - low)
    a, b = sorted_values[low], sorted_values[low + 1]
    return float(a) if weight == 0 else a + weight * (b - a)


@dataclass(frozen=True)
class Interval:
    low: float | None
    high: float | None
    defined: int
    undefined: int

    def excludes_zero_below(self) -> bool:
        """The whole interval is strictly below zero."""
        return self.high is not None and self.high < 0

    def to_dict(self) -> dict[str, object]:
        return {"low": self.low, "high": self.high, "defined": self.defined, "undefined": self.undefined}


def percentile_interval(values: Sequence[float], undefined: int, level: Fraction) -> Interval:
    if not values:
        return Interval(None, None, 0, undefined)
    ordered = sorted(values)
    tail = (1 - Fraction(level)) / 2
    return Interval(quantile_type7(ordered, tail), quantile_type7(ordered, 1 - tail), len(ordered), undefined)


# --------------------------------------------------------------------------- #
# The bootstrap
# --------------------------------------------------------------------------- #


def resample_counts(n_clusters: int, resamples: int, seed: int):
    """Yield, per resample, how often each of the ``n_clusters`` clusters was drawn."""
    if n_clusters < 1:
        raise StatsError("the bootstrap needs at least one cluster")
    rng = random.Random(seed)
    population = range(n_clusters)
    for _ in range(resamples):
        counts = [0] * n_clusters
        for index in rng.choices(population, k=n_clusters):
            counts[index] += 1
        yield counts


def cluster_bootstrap(
    rows: Sequence[Sequence[int]],
    statistics: Mapping[str, Expr],
    *,
    resamples: int,
    seed: int,
    level: Fraction,
) -> dict[str, Interval]:
    """Percentile intervals of ``statistics`` from a cluster bootstrap over ``rows``.

    ``rows[c][k]`` is the integer total of column ``k`` in cluster ``c``. The row
    order is the order clusters are indexed in the resampling; callers sort
    clusters by ``group_id`` so that results do not depend on input order.
    """
    if resamples < 1:
        raise StatsError("resamples must be >= 1")
    if not rows:
        raise StatsError("the bootstrap needs at least one cluster")
    width = len(rows[0])
    if any(len(row) != width for row in rows):
        raise StatsError("all cluster rows must have the same number of columns")
    needed = sorted(set().union(*(expr.columns() for expr in statistics.values()))) if statistics else []
    columns = {k: [row[k] for row in rows] for k in needed}
    samples: dict[str, list[float]] = {name: [] for name in statistics}
    undefined: dict[str, int] = {name: 0 for name in statistics}
    totals = [0] * width
    for counts in resample_counts(len(rows), resamples, seed):
        for k, column in columns.items():
            totals[k] = sum(map(mul, counts, column))
        for name, expr in statistics.items():
            value = expr.evaluate(totals, exact=False)
            if value is None:
                undefined[name] += 1
            else:
                samples[name].append(value)
    return {name: percentile_interval(samples[name], undefined[name], level) for name in statistics}
