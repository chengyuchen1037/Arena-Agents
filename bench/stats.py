"""Statistics of the acceptance report (docs/v0.14-plan.md §6.5: "每题采样 k 次，用 bootstrap 给出置信区间"; "裁判 rubric 固定版本，校准一致性 κ ≥ 0.6 才计入验收").

Pure functions over plain numbers and strings: the standard library only (no numpy) and a FIXED seed - the same data give the same intervals on
every machine and in every run.  Nothing here knows a bot, a suite or a file; ``bench.report`` and ``python -m bench calibrate`` use it.

* **Intervals** (percentile bootstrap, 95 % by default): :func:`bootstrap_ci` for independent values; :func:`cluster_bootstrap_ci` / :func:`cluster_ratio_ci`
  for values that come in clusters - the samples of one question are NOT independent, so the questions are resampled first and then the samples
  within each chosen question (two levels; ``within=False`` resamples the questions only);  :func:`wilson_interval` for a plain proportion.
* **Comparisons**: :func:`paired_bootstrap` - the same questions put to two arms (a bot and the baseline, a bot with and without a layer):
  the difference, its interval and the one-sided p-value for "A is better than B"; :func:`paired_ratio_bootstrap` for gates on ratios
  (``cand <= 0.5 * baseline``);  :func:`cohens_h` (proportions) and :func:`cohens_d` (means) as effect sizes.
* **Agreement** of two labelings (a judge and a person): :func:`cohens_kappa` (unweighted, or linear / quadratic weights for ordered labels),
  :func:`kappa_ci`, :func:`confusion`.

Signatures that other modules rely on::

    cohens_kappa(a: Sequence[str], b: Sequence[str]) -> float
    kappa_ci(a, b, *, n=2000, alpha=0.05, seed=DEFAULT_SEED) -> (kappa, lo, hi)
    confusion(a, b) -> dict[str, dict[str, int]]          # confusion[label of a][label of b] = how many items

Every interval function returns an :class:`Interval` (a ``(point, lo, hi)`` tuple).  A statistic that cannot be computed - no data, a kappa
whose expected agreement is 1 - is ``nan`` (``math.isnan``), never an exception: the data decide that, not the programmer.
"""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NamedTuple

DEFAULT_SEED = 20261120
DEFAULT_RESAMPLES = 2000
DEFAULT_ALPHA = 0.05
#: Below this many observations an interval is only a rough guide (the report says so).
SMALL_SAMPLE = 10
#: Agreement of a judge with the people who labelled the calibration set: below it the judge's verdicts do not count for acceptance (H48).
KAPPA_THRESHOLD = 0.6
NAN = float("nan")

Seed = int | str


class Interval(NamedTuple):
    """A point estimate and the ends of its interval (unpackable: ``point, lo, hi = bootstrap_ci(...)``)."""

    point: float
    lo: float
    hi: float


class Paired(NamedTuple):
    """The result of a paired comparison of A against B over the same items.

    ``diff`` = the statistic of A minus that of B (by default mean A - mean B); ``lo`` / ``hi`` its interval; ``p_value`` the one-sided p-value of
    "A is better than B" (small = A really is better; about 0.5 = no difference; near 1 = A is worse); ``n_items`` how many items both arms
    have (the only ones that count); ``a`` / ``b`` the two arms' own values over those items."""

    diff: float
    lo: float
    hi: float
    p_value: float
    n_items: int
    a: float
    b: float


# ------------------------------------------------------------------------------------------------------------------------- basics
def mean(values: Sequence[float]) -> float:
    """The arithmetic mean (``nan`` for no values).  The default ``stat`` of the bootstrap functions; passing THIS function selects their fast path."""
    return sum(values) / len(values) if len(values) else NAN


_MEANS = (mean, statistics.fmean, statistics.mean)


def derive_seed(seed: Seed, *labels: object) -> int:
    """A 64-bit seed from a base seed and labels (``derive_seed(7, "L1", "bias", "bot:Alice")``): every number of a report gets a seed of its own, so
    adding a number to the report does not change the interval of any other."""
    text = "\x1f".join([str(seed), *(str(label) for label in labels)])
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """The ``q``-quantile of already sorted values, linear interpolation between the neighbours (what numpy calls "linear")."""
    count = len(sorted_values)
    if count == 0:
        return NAN
    if count == 1:
        return float(sorted_values[0])
    position = (count - 1) * min(1.0, max(0.0, q))
    low, high = math.floor(position), math.ceil(position)
    return float(sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low))


def percentile_interval(replicates: Sequence[float], alpha: float = DEFAULT_ALPHA) -> tuple[float, float]:
    """The ``alpha / 2`` and ``1 - alpha / 2`` percentiles of bootstrap replicates (``nan`` pair for none)."""
    ordered = sorted(r for r in replicates if not math.isnan(r))
    if not ordered:
        return NAN, NAN
    return quantile(ordered, alpha / 2), quantile(ordered, 1 - alpha / 2)


def _rng(seed: Seed) -> random.Random:
    return random.Random(derive_seed(seed))


def _clusters_of(clusters: Mapping[Any, Sequence[Any]] | Sequence[Sequence[Any]]) -> list[list[Any]]:
    groups = clusters.values() if isinstance(clusters, Mapping) else clusters
    return [list(group) for group in groups if len(group) > 0]


# ------------------------------------------------------------------------------------------------------------------------- bootstrap
def bootstrap_ci(
    values: Sequence[float],
    stat: Callable[[Sequence[float]], float] = mean,
    n: int = DEFAULT_RESAMPLES,
    alpha: float = DEFAULT_ALPHA,
    seed: Seed = DEFAULT_SEED,
) -> Interval:
    """The percentile bootstrap interval of ``stat`` over INDEPENDENT ``values`` (``n`` resamples with replacement, fixed ``seed``).

    ``point`` is ``stat`` of the data itself, not the mean of the replicates.  One value gives a degenerate interval; no value gives ``nan``s.
    The default ``stat`` is :func:`mean`; any function of a list works (``statistics.median``, a proportion ...)."""
    data = list(values)
    if not data:
        return Interval(NAN, NAN, NAN)
    point = float(stat(data))
    if len(data) == 1:
        return Interval(point, point, point)
    rng, size = _rng(seed), len(data)
    replicates: list[float] = []
    if stat in _MEANS:
        for _ in range(n):
            replicates.append(sum(rng.choices(data, k=size)) / size)
    else:
        for _ in range(n):
            replicates.append(float(stat(rng.choices(data, k=size))))
    lo, hi = percentile_interval(replicates, alpha)
    return Interval(point, lo, hi)


def cluster_bootstrap_ci(
    clusters: Mapping[Any, Sequence[float]] | Sequence[Sequence[float]],
    stat: Callable[[Sequence[float]], float] = mean,
    n: int = DEFAULT_RESAMPLES,
    alpha: float = DEFAULT_ALPHA,
    seed: Seed = DEFAULT_SEED,
    *,
    within: bool = True,
) -> Interval:
    """The two-level bootstrap interval of ``stat`` over values that come in clusters (the samples of each question).

    Level 1 draws as many clusters as there are, with replacement; level 2 (``within=True``, the default) draws, inside every drawn cluster, as
    many of its values as it has, with replacement; ``stat`` is applied to everything drawn.  Resampling the samples as if they were independent
    would make a bot that was asked 5 times per question look 5 times surer than it is.  The two levels are conservative (a little wide); with
    ``within=False`` only the clusters are resampled."""
    groups = _clusters_of(clusters)
    flat = [value for group in groups for value in group]
    if not flat:
        return Interval(NAN, NAN, NAN)
    point = float(stat(flat))
    if len(flat) == 1:
        return Interval(point, point, point)
    rng, count = _rng(seed), len(groups)
    replicates: list[float] = []
    if stat in _MEANS:
        sums, sizes = [sum(g) for g in groups], [len(g) for g in groups]
        for _ in range(n):
            chosen = rng.choices(range(count), k=count)
            if within:
                total, size = 0.0, 0
                for index in chosen:
                    group = groups[index]
                    total += sum(rng.choices(group, k=len(group)))
                    size += len(group)
            else:
                total, size = sum(sums[i] for i in chosen), sum(sizes[i] for i in chosen)
            replicates.append(total / size)
    else:
        for _ in range(n):
            drawn: list[float] = []
            for index in rng.choices(range(count), k=count):
                group = groups[index]
                drawn.extend(rng.choices(group, k=len(group)) if within else group)
            replicates.append(float(stat(drawn)))
    lo, hi = percentile_interval(replicates, alpha)
    return Interval(point, lo, hi)


def _complex_clusters(clusters: Mapping[Any, Sequence[tuple[float, float]]] | Sequence[Sequence[tuple[float, float]]]) -> list[list[complex]]:
    """``(numerator, denominator)`` pairs as complex numbers: ``sum`` of a draw then adds both at C speed (``real`` = Σ numerators, ``imag`` = Σ denominators)."""
    return [[complex(num, den) for num, den in group] for group in _clusters_of(clusters)]


def cluster_ratio_ci(
    clusters: Mapping[Any, Sequence[tuple[float, float]]] | Sequence[Sequence[tuple[float, float]]],
    n: int = DEFAULT_RESAMPLES,
    alpha: float = DEFAULT_ALPHA,
    seed: Seed = DEFAULT_SEED,
    *,
    within: bool = True,
    transform: Callable[[float], float] | None = None,
) -> Interval:
    """Like :func:`cluster_bootstrap_ci` for a RATIO of sums: every observation is ``(numerator, denominator)`` and the statistic is
    Σ numerators / Σ denominators (a rate over a varying number of speeches, a proportion: ``(1, 1)`` / ``(0, 1)``).  ``transform`` is applied to
    the point and to every replicate (``abs`` for a bias, say: it is not monotone, so the interval is built from the transformed replicates)."""
    groups = _complex_clusters(clusters)
    total = sum((sum(g) for g in groups), 0j)
    if total.imag == 0:
        return Interval(NAN, NAN, NAN)
    shape = transform or (lambda value: value)
    point = float(shape(total.real / total.imag))
    if sum(len(g) for g in groups) == 1:
        return Interval(point, point, point)
    rng, count = _rng(seed), len(groups)
    sums = [sum(g) for g in groups]
    replicates: list[float] = []
    for _ in range(n):
        chosen = rng.choices(range(count), k=count)
        if within:
            drawn = 0j
            for index in chosen:
                group = groups[index]
                drawn += sum(rng.choices(group, k=len(group)))
        else:
            drawn = sum((sums[i] for i in chosen), 0j)
        if drawn.imag:
            replicates.append(float(shape(drawn.real / drawn.imag)))
    lo, hi = percentile_interval(replicates, alpha)
    return Interval(point, lo, hi)


def _difference(a: float, b: float) -> float:
    return a - b


def paired_ratio_bootstrap(
    a_items: Mapping[Any, Sequence[tuple[float, float]]],
    b_items: Mapping[Any, Sequence[tuple[float, float]]],
    margin: Callable[[float, float], float] = _difference,
    n: int = DEFAULT_RESAMPLES,
    alpha: float = DEFAULT_ALPHA,
    seed: Seed = DEFAULT_SEED,
    *,
    within: bool = True,
) -> Paired:
    """The paired comparison of two arms over the same items, for statistics that are ratios of sums.

    ``a_items`` / ``b_items`` map an item (a question) to the ``(numerator, denominator)`` observations of the arm on it (one per sample).  Only the
    items both arms have count.  Each replicate draws items (the same draw for both arms: that is the pairing), optionally the samples within them
    (independently per arm), computes both arms' ratios ``a*`` and ``b*`` and ``margin(a*, b*)`` (default: ``a* - b*``).  ``margin`` makes a gate
    on the baseline a plain "greater than 0" question: ``cand >= base + 0.2`` is ``margin = lambda c, b: c - b - 0.2``.  The p-value is one-sided,
    "margin > 0": (1 + replicates with margin <= 0) / (1 + replicates)."""
    items = sorted((item for item in a_items if item in b_items and len(a_items[item]) > 0 and len(b_items[item]) > 0), key=str)
    if not items:
        return Paired(NAN, NAN, NAN, NAN, 0, NAN, NAN)
    a_groups = [[complex(num, den) for num, den in a_items[item]] for item in items]
    b_groups = [[complex(num, den) for num, den in b_items[item]] for item in items]
    a_total, b_total = sum((sum(g) for g in a_groups), 0j), sum((sum(g) for g in b_groups), 0j)
    if a_total.imag == 0 or b_total.imag == 0:
        return Paired(NAN, NAN, NAN, NAN, len(items), NAN, NAN)
    a_point, b_point = a_total.real / a_total.imag, b_total.real / b_total.imag
    point = float(margin(a_point, b_point))
    rng, count = _rng(seed), len(items)
    a_sums, b_sums = [sum(g) for g in a_groups], [sum(g) for g in b_groups]
    replicates: list[float] = []
    for _ in range(n):
        chosen = rng.choices(range(count), k=count)
        a_drawn = b_drawn = 0j
        if within:
            for index in chosen:
                a_group, b_group = a_groups[index], b_groups[index]
                a_drawn += sum(rng.choices(a_group, k=len(a_group)))
                b_drawn += sum(rng.choices(b_group, k=len(b_group)))
        else:
            for index in chosen:
                a_drawn += a_sums[index]
                b_drawn += b_sums[index]
        if a_drawn.imag and b_drawn.imag:
            replicates.append(float(margin(a_drawn.real / a_drawn.imag, b_drawn.real / b_drawn.imag)))
    if not replicates:
        return Paired(point, NAN, NAN, NAN, len(items), a_point, b_point)
    lo, hi = percentile_interval(replicates, alpha)
    p_value = (1 + sum(1 for value in replicates if value <= 0)) / (len(replicates) + 1)
    return Paired(point, lo, hi, p_value, len(items), a_point, b_point)


def paired_bootstrap(
    a_by_item: Mapping[Any, float | Sequence[float]],
    b_by_item: Mapping[Any, float | Sequence[float]],
    n: int = DEFAULT_RESAMPLES,
    alpha: float = DEFAULT_ALPHA,
    seed: Seed = DEFAULT_SEED,
    *,
    within: bool = True,
) -> Paired:
    """Is arm A better than arm B on the same questions?  ``a_by_item[item]`` is A's value on the item - a number, or the list of its samples (the
    item's value is then their mean).  Returns the :class:`Paired` result: ``diff`` = mean(A) - mean(B) over the items both have, its percentile
    interval and the one-sided p-value of "A > B" (see :func:`paired_ratio_bootstrap`; two-level like :func:`cluster_bootstrap_ci`).

    For "lower is better" statistics pass the arms the other way round."""

    def as_observations(side: Mapping[Any, float | Sequence[float]]) -> dict[Any, list[tuple[float, float]]]:
        """Every sample ``x`` of an item with ``k`` samples becomes ``(x / k, 1 / k)``: the sums of a draw are then the sum of the drawn items' means
        over the number of items drawn - the mean of the item means, whatever the number of samples of each."""
        out: dict[Any, list[tuple[float, float]]] = {}
        for item, value in side.items():
            values = [float(value)] if isinstance(value, (int, float)) else [float(v) for v in value]
            if values:
                weight = 1.0 / len(values)
                out[item] = [(v * weight, weight) for v in values]
        return out

    return paired_ratio_bootstrap(as_observations(a_by_item), as_observations(b_by_item), _difference, n, alpha, seed, within=within)


# ------------------------------------------------------------------------------------------------------------------------- proportions
def wilson_interval(successes: float, n: float, alpha: float = DEFAULT_ALPHA) -> tuple[float, float]:
    """The Wilson score interval of a proportion ``successes / n``: sensible for small ``n`` and for 0 % / 100 % (where the bootstrap of a
    proportion has no spread at all).  No observations: ``(0.0, 1.0)`` - nothing is known."""
    if n <= 0:
        return 0.0, 1.0
    z = statistics.NormalDist().inv_cdf(1 - alpha / 2)
    p = min(1.0, max(0.0, successes / n))
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    lo, hi = centre - half, centre + half
    # (at 0 / n and n / n the exact ends are 0 and 1: the floating point arithmetic must not leave 2.8e-17 there)
    return (0.0 if successes <= 0 or lo < 1e-12 else lo), (1.0 if successes >= n or hi > 1 - 1e-12 else hi)


def cohens_h(p1: float, p2: float) -> float:
    """Cohen's h, the effect size of two proportions: ``2 asin √p1 - 2 asin √p2`` (positive when ``p1`` is larger; 0.2 small, 0.5 medium, 0.8 large)."""
    return 2 * math.asin(math.sqrt(min(1.0, max(0.0, p1)))) - 2 * math.asin(math.sqrt(min(1.0, max(0.0, p2))))


def cohens_d(a: Sequence[float], b: Sequence[float], *, paired: bool = False) -> float:
    """Cohen's d, the effect size of two means: (mean a - mean b) / the pooled standard deviation; with ``paired`` the mean of the differences over
    their standard deviation (d_z; ``a`` and ``b`` are the same items in the same order).  ``±inf`` when the spread is 0 and the means differ,
    0.0 when they do not, ``nan`` for too few values."""
    if paired:
        if len(a) != len(b):
            raise ValueError("paired values must have the same length")
        diffs = [x - y for x, y in zip(a, b)]
        if len(diffs) < 2:
            return NAN
        centre, spread = statistics.fmean(diffs), statistics.stdev(diffs)
    else:
        if len(a) < 2 or len(b) < 2:
            return NAN
        centre = statistics.fmean(a) - statistics.fmean(b)
        pooled = ((len(a) - 1) * statistics.variance(a) + (len(b) - 1) * statistics.variance(b)) / (len(a) + len(b) - 2)
        spread = math.sqrt(pooled)
    if spread == 0:
        return 0.0 if centre == 0 else math.copysign(math.inf, centre)
    return centre / spread


def effect_label(value: float) -> str:
    """Cohen's words for the size of an effect (h or d): 可忽略 / 小 / 中 / 大."""
    if math.isnan(value):
        return "—"
    size = abs(value)
    return "可忽略" if size < 0.2 else "小" if size < 0.5 else "中" if size < 0.8 else "大"


# ------------------------------------------------------------------------------------------------------------------------- kappa
def _categories(a: Sequence[str], b: Sequence[str], order: Sequence[str] | None) -> list[str]:
    seen = set(a) | set(b)
    if order is None:
        return sorted(seen, key=str)
    listed = [c for c in order if c in seen]
    return listed + sorted((c for c in seen if c not in set(order)), key=str)


def _weight(weights: str | None, i: int, j: int, k: int) -> float:
    if weights is None:
        return 0.0 if i == j else 1.0
    if k <= 1:
        return 0.0
    distance = abs(i - j) / (k - 1)
    if weights == "linear":
        return distance
    if weights == "quadratic":
        return distance * distance
    raise ValueError(f"unknown weights {weights!r} (None, 'linear' or 'quadratic')")


def _kappa(observed: list[list[int]], total: int, weights: str | None) -> float:
    size = len(observed)
    rows = [sum(row) for row in observed]
    cols = [sum(observed[i][j] for i in range(size)) for j in range(size)]
    disagree = sum(_weight(weights, i, j, size) * observed[i][j] for i in range(size) for j in range(size)) / total
    expected = sum(_weight(weights, i, j, size) * rows[i] * cols[j] for i in range(size) for j in range(size)) / (total * total)
    if expected == 0:
        return NAN  # both labelings use one single label (or the weights ignore every difference): there is nothing to agree about
    return 1.0 - disagree / expected


def cohens_kappa(a: Sequence[str], b: Sequence[str], *, weights: str | None = None, order: Sequence[str] | None = None) -> float:
    """Cohen's kappa of two labelings of the same items: (observed agreement - agreement expected by chance) / (1 - expected).  1 = perfect, 0 = no
    better than chance, below 0 = worse.  ``weights`` ``"linear"`` / ``"quadratic"`` makes a near miss cost less than a far one (the labels must then
    be ordered: ``order`` lists them, else they are sorted).  ``nan`` when it is undefined: no items, or both raters use one single label throughout
    (they agree, but the set cannot tell a good judge from a stubborn one: the caller must not count it).  Different lengths raise ``ValueError``."""
    if len(a) != len(b):
        raise ValueError(f"the two labelings must have the same length ({len(a)} != {len(b)})")
    if not a:
        return NAN
    cats = _categories(a, b, order)
    index = {c: i for i, c in enumerate(cats)}
    observed = [[0] * len(cats) for _ in cats]
    for x, y in zip(a, b):
        observed[index[x]][index[y]] += 1
    return _kappa(observed, len(a), weights)


def kappa_ci(
    a: Sequence[str],
    b: Sequence[str],
    *,
    n: int = DEFAULT_RESAMPLES,
    alpha: float = DEFAULT_ALPHA,
    seed: Seed = DEFAULT_SEED,
    weights: str | None = None,
    order: Sequence[str] | None = None,
) -> Interval:
    """Kappa and its percentile bootstrap interval (the ITEMS are resampled, both labels of an item together).  A resample whose kappa is undefined
    is left out; when fewer than half are defined the interval is ``nan``.  Unpacks as ``kappa, lo, hi``."""
    if len(a) != len(b):
        raise ValueError(f"the two labelings must have the same length ({len(a)} != {len(b)})")
    point = cohens_kappa(a, b, weights=weights, order=order)
    if not a:
        return Interval(NAN, NAN, NAN)
    cats = _categories(a, b, order)
    index = {c: i for i, c in enumerate(cats)}
    left, right = [index[x] for x in a], [index[y] for y in b]
    rng, size = _rng(seed), len(cats)
    replicates: list[float] = []
    for _ in range(n):
        observed = [[0] * size for _ in range(size)]
        for item in rng.choices(range(len(left)), k=len(left)):
            observed[left[item]][right[item]] += 1
        value = _kappa(observed, len(left), weights)
        if not math.isnan(value):
            replicates.append(value)
    if len(replicates) * 2 < n:
        return Interval(point, NAN, NAN)
    lo, hi = percentile_interval(replicates, alpha)
    return Interval(point, lo, hi)


def confusion(a: Sequence[str], b: Sequence[str], *, order: Sequence[str] | None = None) -> dict[str, dict[str, int]]:
    """``confusion[label of a][label of b]`` = how many items ``a`` labelled so and ``b`` labelled so.  Square: every label either rater used is a
    row AND a column (zeros included), in ``order`` (default: sorted), so the matrix is the same shape whoever is asked."""
    if len(a) != len(b):
        raise ValueError(f"the two labelings must have the same length ({len(a)} != {len(b)})")
    cats = _categories(a, b, order)
    matrix = {row: {col: 0 for col in cats} for row in cats}
    for x, y in zip(a, b):
        matrix[x][y] += 1
    return matrix


def agreement(a: Sequence[str], b: Sequence[str]) -> float:
    """The observed agreement: the share of items both raters labelled the same (``nan`` for none)."""
    if len(a) != len(b):
        raise ValueError(f"the two labelings must have the same length ({len(a)} != {len(b)})")
    return sum(1 for x, y in zip(a, b) if x == y) / len(a) if a else NAN


__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_RESAMPLES",
    "DEFAULT_SEED",
    "KAPPA_THRESHOLD",
    "SMALL_SAMPLE",
    "Interval",
    "Paired",
    "agreement",
    "bootstrap_ci",
    "cluster_bootstrap_ci",
    "cluster_ratio_ci",
    "cohens_d",
    "cohens_h",
    "cohens_kappa",
    "confusion",
    "derive_seed",
    "effect_label",
    "kappa_ci",
    "mean",
    "paired_bootstrap",
    "paired_ratio_bootstrap",
    "percentile_interval",
    "quantile",
    "wilson_interval",
]
