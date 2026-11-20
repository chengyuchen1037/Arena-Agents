"""``bench.stats``: the statistics of the acceptance report, on data whose answer is known (no model, no file).

What is pinned: the intervals are reproducible (fixed seed) and cover what they should (a known distribution, many trials); the samples of one question are
resampled as a cluster, so they do not make a bot look surer than it is; the paired test points the right way; kappa has the values a hand calculation gives
(perfect agreement = 1, chance = about 0, a small table worked out on paper, the weighted variants) and the confusion matrix is the square one the
calibration command prints.
"""

from __future__ import annotations

import ast
import math
import random
from pathlib import Path

import pytest

from bench import stats

SOURCE = Path(stats.__file__)


def width(interval: stats.Interval) -> float:
    return interval.hi - interval.lo


# ---------------------------------------------------------------------------------------------------------------------- intervals
def test_the_same_seed_gives_the_same_interval_and_another_seed_another_one() -> None:
    rng = random.Random(3)
    data = [rng.gauss(0, 1) for _ in range(40)]
    first = stats.bootstrap_ci(data, n=500, seed=11)
    assert first == stats.bootstrap_ci(data, n=500, seed=11)
    assert first.point == pytest.approx(sum(data) / len(data)) and first.lo < first.point < first.hi
    assert first != stats.bootstrap_ci(data, n=500, seed=12)
    assert stats.bootstrap_ci(data, n=500, seed="a text seed") == stats.bootstrap_ci(data, n=500, seed="a text seed")  # (a label is a seed, too)


def test_the_interval_covers_the_true_mean_about_95_percent_of_the_time() -> None:
    rng = random.Random(1)
    trials, covered = 200, 0
    for trial in range(trials):
        sample = [rng.gauss(0, 1) for _ in range(30)]
        _point, lo, hi = stats.bootstrap_ci(sample, n=400, seed=trial)
        covered += lo <= 0.0 <= hi
    assert 0.90 <= covered / trials <= 0.99, covered / trials  # (fixed seeds: the number is the same on every machine)


def test_a_wider_confidence_level_is_a_wider_interval_and_the_statistic_can_be_any_function() -> None:
    data = [float(v) for v in range(1, 31)]
    narrow, wide = stats.bootstrap_ci(data, alpha=0.20, n=500, seed=1), stats.bootstrap_ci(data, alpha=0.01, n=500, seed=1)
    assert width(wide) > width(narrow)
    median = stats.bootstrap_ci(data, stat=lambda xs: sorted(xs)[len(xs) // 2], n=300, seed=1)
    assert median.point == 16.0 and median.lo <= 16.0 <= median.hi  # (the point is the statistic of the data, not of the replicates)


def test_nothing_or_one_value_do_not_raise() -> None:
    nothing = stats.bootstrap_ci([])
    assert all(math.isnan(v) for v in nothing)
    assert stats.bootstrap_ci([0.25]) == stats.Interval(0.25, 0.25, 0.25)
    assert stats.bootstrap_ci([3.0, 3.0, 3.0], n=50).lo == 3.0
    point, lo, hi = stats.bootstrap_ci([1.0, 2.0], n=50)  # (an Interval unpacks)
    assert lo <= point <= hi


def test_the_samples_of_one_question_are_resampled_as_a_cluster() -> None:
    """Ten questions, five samples each, every question answered the same way five times: 50 values, but only 10 independent facts."""
    clusters = {f"q{i}": [1.0] * 5 if i < 5 else [0.0] * 5 for i in range(10)}
    flat = [v for group in clusters.values() for v in group]
    naive = stats.bootstrap_ci(flat, n=1000, seed=5)
    clustered = stats.cluster_bootstrap_ci(clusters, n=1000, seed=5)
    assert naive.point == clustered.point == 0.5
    assert width(clustered) > 1.5 * width(naive), (naive, clustered)  # (treating the 50 as independent would look 2x surer than the data allow)
    assert clustered.lo < 0.3 and clustered.hi > 0.7


def test_the_cluster_interval_covers_the_true_rate_of_a_bank_of_questions_of_different_difficulty() -> None:
    """Questions whose chance of being answered right varies from 0.4 to 0.9 (a true rate of 0.65 over the bank), three samples each: the interval of the rate over the
    bank - the two-level one and the ratio form the report uses - holds it in nearly every bank (the two levels are conservative: they count the variation between
    the questions, which a fixed bank does not have, and the one inside them)."""
    rng = random.Random(21)
    trials, hits, ratio_hits = 100, 0, 0
    for trial in range(trials):
        bank = [[1.0 if rng.random() < 0.4 + 0.5 * (q / 19) else 0.0 for _ in range(3)] for q in range(20)]
        _point, lo, hi = stats.cluster_bootstrap_ci(bank, n=300, seed=trial)
        hits += lo <= 0.65 <= hi
        ratio = stats.cluster_ratio_ci([[(v, 1.0) for v in group] for group in bank], n=300, seed=trial)
        ratio_hits += ratio.lo <= 0.65 <= ratio.hi
        assert ratio.point == pytest.approx(sum(sum(g) for g in bank) / 60)
    assert 0.88 <= hits / trials <= 1.0 and 0.88 <= ratio_hits / trials <= 1.0, (hits, ratio_hits)


def test_the_two_levels_and_the_questions_only_agree_on_the_point_and_the_within_level_is_a_little_wider() -> None:
    rng = random.Random(8)
    clusters = [[float(rng.random() < 0.3 + 0.05 * i) for _ in range(4)] for i in range(12)]
    both = stats.cluster_bootstrap_ci(clusters, n=1500, seed=2)
    only = stats.cluster_bootstrap_ci(clusters, n=1500, seed=2, within=False)
    assert both.point == only.point
    assert width(both) >= 0.9 * width(only)  # (conservative: never much narrower)
    assert stats.cluster_bootstrap_ci(list(map(list, clusters)), n=300, seed=2) == stats.cluster_bootstrap_ci(clusters, n=300, seed=2)


def test_empty_clusters_are_ignored_and_a_dict_or_a_list_may_hold_them() -> None:
    a = stats.cluster_bootstrap_ci({"q1": [1.0, 0.0], "q2": [], "q3": [1.0]}, n=200, seed=1)
    b = stats.cluster_bootstrap_ci([[1.0, 0.0], [1.0]], n=200, seed=1)
    assert a == b
    assert all(math.isnan(v) for v in stats.cluster_bootstrap_ci({"q": []}))
    assert stats.cluster_bootstrap_ci({"q": [0.5]}) == stats.Interval(0.5, 0.5, 0.5)


def test_the_cluster_bootstrap_takes_any_statistic() -> None:
    clusters = {i: [float(i), float(i) + 0.5] for i in range(10)}
    interval = stats.cluster_bootstrap_ci(clusters, stat=max, n=200, seed=1)
    assert interval.point == 9.5 and interval.hi <= 9.5 and interval.lo >= 0.5


def test_a_ratio_of_sums_is_not_the_mean_of_ratios() -> None:
    """A rate over a varying number of speeches: 1 of 1 in one question, 1 of 9 in another: the pooled rate is 2/10, not (1 + 1/9) / 2."""
    clusters = {"a": [(1.0, 1.0)], "b": [(1.0, 9.0)]}
    interval = stats.cluster_ratio_ci(clusters, n=300, seed=1)
    assert interval.point == pytest.approx(0.2)
    assert stats.cluster_ratio_ci({"a": [(0.0, 0.0)]}).point != stats.cluster_ratio_ci({"a": [(0.0, 0.0)]}).point  # (0 / 0: nan - no data, no number)
    assert stats.cluster_ratio_ci({"a": [(3.0, 4.0)]}) == stats.Interval(0.75, 0.75, 0.75)


def test_a_transform_is_applied_to_the_replicates_not_to_the_interval_of_the_untransformed() -> None:
    """The absolute bias |p - 0.5| is not monotone in p: an interval of p that straddles 0.5 must reach down to 0."""
    clusters = {i: [(float(i % 2), 1.0)] * 3 for i in range(10)}  # p = 0.5 exactly
    raw = stats.cluster_ratio_ci(clusters, n=500, seed=1)
    biased = stats.cluster_ratio_ci(clusters, n=500, seed=1, transform=lambda p: abs(p - 0.5))
    assert raw.point == 0.5 and biased.point == 0.0
    assert biased.lo == pytest.approx(0.0, abs=1e-9) and biased.hi > 0.0  # (the lower end is 0; the upper end is the larger distance from 0.5)
    assert biased.hi == pytest.approx(max(abs(raw.lo - 0.5), abs(raw.hi - 0.5)), abs=0.06)


def test_a_proportion_has_a_wilson_interval_that_is_never_degenerate() -> None:
    assert stats.wilson_interval(0, 10) == pytest.approx((0.0, 0.2775), abs=1e-4)
    assert stats.wilson_interval(10, 10) == pytest.approx((0.7225, 1.0), abs=1e-4)
    lo, hi = stats.wilson_interval(5, 10)
    assert (lo, hi) == pytest.approx((0.2366, 0.7634), abs=1e-4)
    assert stats.wilson_interval(0, 0) == (0.0, 1.0)  # (nothing is known)
    assert stats.wilson_interval(0, 10)[0] == 0.0 and stats.wilson_interval(10, 10)[1] == 1.0  # (exact ends, not 2.8e-17)
    assert stats.wilson_interval(50, 100)[1] - stats.wilson_interval(50, 100)[0] < stats.wilson_interval(5, 10)[1] - stats.wilson_interval(5, 10)[0]
    assert stats.wilson_interval(9, 10, alpha=0.2)[0] > stats.wilson_interval(9, 10)[0]


def test_quantiles_are_linear_between_the_neighbours() -> None:
    assert stats.quantile([1, 2, 3, 4], 0.5) == 2.5 and stats.quantile([1, 2, 3, 4], 0) == 1 and stats.quantile([1, 2, 3, 4], 1) == 4
    assert stats.quantile([7], 0.9) == 7 and math.isnan(stats.quantile([], 0.5))
    lo, hi = stats.percentile_interval(list(range(101)), alpha=0.10)
    assert (lo, hi) == (5.0, 95.0)
    assert all(math.isnan(v) for v in stats.percentile_interval([]))


def test_every_number_of_a_report_has_a_seed_of_its_own_and_adding_one_changes_no_other() -> None:
    assert stats.derive_seed(7, "L1", "bias", "bot:Alice") == stats.derive_seed(7, "L1", "bias", "bot:Alice")
    assert stats.derive_seed(7, "L1", "bias", "bot:Alice") != stats.derive_seed(7, "L1", "bias", "bot:Bob")
    assert stats.derive_seed(7, "x") != stats.derive_seed(8, "x") and stats.derive_seed("a", "b") != stats.derive_seed("a b")
    assert stats.derive_seed(7, "x") == 9646028769132121576  # (pinned: a change of the algorithm changes every interval of every committed report)


# ---------------------------------------------------------------------------------------------------------------------- paired comparison
def test_the_paired_test_says_a_is_better_when_it_is_and_not_when_it_is_not() -> None:
    items = range(20)
    rng = random.Random(4)
    better = {i: 0.8 + rng.uniform(-0.05, 0.05) for i in items}
    worse = {i: 0.5 + rng.uniform(-0.05, 0.05) for i in items}
    forward = stats.paired_bootstrap(better, worse, n=800, seed=1)
    assert forward.n_items == 20 and forward.diff == pytest.approx(0.3, abs=0.03) and forward.lo > 0.2 and forward.p_value < 0.01
    assert forward.a == pytest.approx(0.8, abs=0.03) and forward.b == pytest.approx(0.5, abs=0.03)
    backward = stats.paired_bootstrap(worse, better, n=800, seed=1)
    assert backward.diff == pytest.approx(-forward.diff) and backward.hi < 0 and backward.p_value > 0.99  # (A worse than B: the p of "A is better" is near 1)


def test_two_arms_that_are_the_same_do_not_come_out_different() -> None:
    rng = random.Random(9)
    base = [float(rng.random() < 0.6) for _ in range(40)]
    a = {i: base[i] for i in range(40)}
    b = {i: float(rng.random() < 0.6) for i in range(40)}  # (the same rate, other luck)
    result = stats.paired_bootstrap(a, b, n=1000, seed=3)
    assert result.lo < 0 < result.hi and 0.05 < result.p_value < 0.95
    identical = stats.paired_bootstrap(a, dict(a), n=200, seed=3)
    assert identical.diff == 0 and identical.lo == identical.hi == 0 and identical.p_value == 1.0  # (nothing to tell apart: not "significant")


def test_only_the_items_both_arms_have_count_and_a_list_is_the_samples_of_the_item() -> None:
    a = {"q1": [1.0, 1.0, 0.0], "q2": 1.0, "q3": 0.0, "only-a": 1.0}
    b = {"q1": [0.0, 0.0, 0.0], "q2": [0.0], "q3": 0.0, "only-b": 0.0, "empty": []}
    result = stats.paired_bootstrap(a, b, n=300, seed=1)
    assert result.n_items == 3 and result.diff == pytest.approx(((2 / 3 + 1 + 0) - 0) / 3)  # (the mean of the items' means)
    nothing = stats.paired_bootstrap({"x": 1.0}, {"y": 1.0})
    assert nothing.n_items == 0 and all(math.isnan(v) for v in (nothing.diff, nothing.lo, nothing.hi, nothing.p_value))


def test_the_ratio_form_gives_a_gate_on_the_baseline_a_plain_question() -> None:
    """``candidate <= 0.5 * baseline`` on a rate: the margin is 0.5 * baseline - candidate, positive = the gate is met."""
    cand = {f"q{i}": [(0.0, 1.0)] * 7 + [(1.0, 1.0)] * (i % 2) for i in range(30)}  # 15 flagged speeches of 225: 6.7 %
    base = {f"q{i}": [(1.0, 1.0)] * 4 + [(0.0, 1.0)] * 4 for i in range(30)}  # 50 %
    met = stats.paired_ratio_bootstrap(cand, base, margin=lambda c, b: 0.5 * b - c, n=600, seed=1)
    assert met.diff == pytest.approx(0.5 * 0.5 - 15 / 225) and met.a == pytest.approx(15 / 225) and met.b == 0.5
    assert met.p_value < 0.01 and met.lo > 0 and met.n_items == 30
    missed = stats.paired_ratio_bootstrap(base, cand, margin=lambda c, b: 0.5 * b - c, n=600, seed=1)  # (a 50 % candidate against a 6 % baseline)
    assert missed.diff < 0 and missed.p_value > 0.99
    # few samples of each question: the resampling inside the questions makes the baseline so uncertain that "half" is not shown - honestly so
    few_cand = {f"q{i}": [(0.0, 1.0)] * 3 + [(1.0, 1.0)] * (i % 2) for i in range(12)}
    few_base = {f"q{i}": [(1.0, 1.0)] * 2 + [(0.0, 1.0)] * 2 for i in range(12)}
    shaky = stats.paired_ratio_bootstrap(few_cand, few_base, margin=lambda c, b: 0.5 * b - c, n=600, seed=1)
    assert shaky.diff == pytest.approx(0.25 - 6 / 42) and shaky.p_value > 0.01 and shaky.lo < 0
    steady = stats.paired_ratio_bootstrap(few_cand, few_base, margin=lambda c, b: 0.5 * b - c, n=600, seed=1, within=False)  # (the questions only)
    assert steady.p_value < 0.01 and steady.lo > 0
    assert stats.paired_ratio_bootstrap({"a": [(1.0, 0.0)]}, {"a": [(1.0, 1.0)]}).n_items == 1  # (0 in the denominator: nothing to compare)
    assert math.isnan(stats.paired_ratio_bootstrap({"a": [(1.0, 0.0)]}, {"a": [(1.0, 1.0)]}).diff)


# ---------------------------------------------------------------------------------------------------------------------- effect sizes
def test_cohens_h_and_d_have_their_textbook_values() -> None:
    assert stats.cohens_h(0.5, 0.5) == 0.0
    assert stats.cohens_h(1.0, 0.0) == pytest.approx(math.pi)
    assert stats.cohens_h(0.9, 0.5) == pytest.approx(0.9273, abs=1e-4) and stats.cohens_h(0.5, 0.9) == pytest.approx(-0.9273, abs=1e-4)
    assert stats.cohens_d([1, 2, 3, 4], [2, 3, 4, 5]) == pytest.approx(-0.7746, abs=1e-4)
    assert stats.cohens_d([1, 2, 3, 4], [1, 2, 3, 4]) == 0.0
    assert stats.cohens_d([5, 5, 5], [1, 1, 1]) == math.inf and stats.cohens_d([1, 1, 1], [5, 5, 5]) == -math.inf  # (no spread, different means)
    assert math.isnan(stats.cohens_d([1], [2, 3]))
    paired = stats.cohens_d([3, 5, 7, 9], [2, 3, 5, 6], paired=True)  # differences 1, 2, 2, 3: mean 2, sd 0.8165
    assert paired == pytest.approx(2 / 0.8165, abs=1e-3)
    with pytest.raises(ValueError):
        stats.cohens_d([1, 2], [1], paired=True)


def test_the_words_for_an_effect() -> None:
    assert [stats.effect_label(v) for v in (0.0, 0.19, 0.2, -0.49, 0.5, 0.79, -0.8, 3)] == ["可忽略", "可忽略", "小", "小", "中", "中", "大", "大"]
    assert stats.effect_label(math.nan) == "—"


# ---------------------------------------------------------------------------------------------------------------------- kappa
def test_perfect_agreement_is_one_and_the_opposite_is_below_zero() -> None:
    labels = list("AABBCCABCA")
    assert stats.cohens_kappa(labels, labels) == 1.0
    assert stats.cohens_kappa(list("AABB"), list("BBAA")) == -1.0  # (every item the other way round)


def test_independent_labelings_are_about_zero() -> None:
    rng = random.Random(12)
    a = [rng.choice("xyz") for _ in range(3000)]
    b = [rng.choice("xyz") for _ in range(3000)]
    assert abs(stats.cohens_kappa(a, b)) < 0.05
    skewed_a = ["yes"] * 90 + ["no"] * 10  # (a lot of agreement by habit alone: 85 %, and still no more than chance gives)
    skewed_b = ["yes"] * 85 + ["no"] * 5 + ["yes"] * 5 + ["no"] * 5
    assert stats.cohens_kappa(skewed_a, skewed_b) < 0.55


def test_kappa_of_a_table_worked_out_by_hand() -> None:
    """a-labels x x x y y z against b-labels x x y y z z: agreement 4/6, chance (3*2 + 2*2 + 1*2) / 36 = 1/3: kappa = (2/3 - 1/3) / (1 - 1/3) = 0.5."""
    a, b = list("xxxyyz"), list("xxyyzz")
    assert stats.cohens_kappa(a, b) == pytest.approx(0.5)
    assert stats.agreement(a, b) == pytest.approx(4 / 6)
    # ordered labels: a near miss costs less than a far one (linear 0.625, quadratic 0.75, worked out on paper)
    assert stats.cohens_kappa(a, b, weights="linear", order=["x", "y", "z"]) == pytest.approx(0.625)
    assert stats.cohens_kappa(a, b, weights="quadratic", order=["x", "y", "z"]) == pytest.approx(0.75)
    assert stats.cohens_kappa(list("xxyy"), list("xxyy"), weights="linear") == 1.0
    with pytest.raises(ValueError, match="unknown weights"):
        stats.cohens_kappa(a, b, weights="cubic")


def test_the_two_by_two_table_of_a_judge_and_a_person() -> None:
    """20 both-yes, 5 judge-yes / person-no, 10 judge-no / person-yes, 15 both-no: agreement 0.7, chance (25*30 + 25*20) / 2500 = 0.5: kappa 0.4."""
    judge = ["yes"] * 25 + ["no"] * 25
    person = ["yes"] * 20 + ["no"] * 5 + ["yes"] * 10 + ["no"] * 15
    assert stats.cohens_kappa(judge, person) == pytest.approx(0.4)
    assert stats.confusion(judge, person) == {"no": {"no": 15, "yes": 10}, "yes": {"no": 5, "yes": 20}}
    assert stats.cohens_kappa(person, judge) == pytest.approx(0.4)  # (symmetric)


def test_a_kappa_that_cannot_be_computed_is_nan_not_an_error() -> None:
    assert math.isnan(stats.cohens_kappa([], []))
    assert math.isnan(stats.cohens_kappa(["x"] * 5, ["x"] * 5))  # (they agree, but one single label throughout says nothing about the judge)
    with pytest.raises(ValueError, match="same length"):
        stats.cohens_kappa(["x"], ["x", "y"])
    with pytest.raises(ValueError, match="same length"):
        stats.confusion(["x"], [])
    with pytest.raises(ValueError, match="same length"):
        stats.kappa_ci(["x"], [])


def test_the_interval_of_kappa_contains_it_and_is_reproducible() -> None:
    judge = ["yes"] * 25 + ["no"] * 25
    person = ["yes"] * 20 + ["no"] * 5 + ["yes"] * 10 + ["no"] * 15
    kappa, lo, hi = stats.kappa_ci(judge, person, n=1000, seed=3)
    assert kappa == pytest.approx(0.4) and lo < 0.4 < hi and 0.1 < lo < 0.35 and 0.45 < hi < 0.75
    assert (kappa, lo, hi) == stats.kappa_ci(judge, person, n=1000, seed=3)
    assert stats.kappa_ci(["a", "b"] * 20, ["a", "b"] * 20, n=100) == stats.Interval(1.0, 1.0, 1.0)  # (perfect: nothing to vary)
    more = stats.kappa_ci(judge * 4, person * 4, n=1000, seed=3)
    assert width(more) < width(stats.Interval(kappa, lo, hi))  # (four times the items: a narrower interval)
    gone = stats.kappa_ci(["x"] * 6, ["x"] * 6, n=50)
    assert math.isnan(gone.point) and math.isnan(gone.lo)
    assert all(math.isnan(v) for v in stats.kappa_ci([], []))


def test_the_signatures_the_calibration_command_relies_on() -> None:
    """``cohens_kappa(a, b) -> float``, ``kappa_ci(a, b, ...) -> (kappa, lo, hi)``, ``confusion(a, b) -> {row: {col: count}}`` - called with plain lists of strings."""
    a, b = ["correct", "wrong", "correct", "partial"], ["correct", "wrong", "wrong", "partial"]
    value = stats.cohens_kappa(a, b)
    assert isinstance(value, float)
    kappa, lo, hi = stats.kappa_ci(a, b, n=100, seed=1)
    assert kappa == value and isinstance(lo, float) and isinstance(hi, float)
    matrix = stats.confusion(a, b)
    assert matrix == {"correct": {"correct": 1, "partial": 0, "wrong": 1}, "partial": {"correct": 0, "partial": 1, "wrong": 0}, "wrong": {"correct": 0, "partial": 0, "wrong": 1}}
    assert list(stats.confusion(a, b, order=["wrong", "partial", "correct"])) == ["wrong", "partial", "correct"]  # (the order of the labels is the caller's)
    assert stats.KAPPA_THRESHOLD == 0.6  # (H48: the judge counts for acceptance from here up)


# ---------------------------------------------------------------------------------------------------------------------- the module itself
def test_the_module_needs_nothing_but_the_standard_library() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imported = {
        (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
        for node in ast.walk(tree)
        for alias in (node.names if isinstance(node, (ast.Import, ast.ImportFrom)) else [])
    }
    assert imported <= {"__future__", "hashlib", "math", "random", "statistics", "collections", "typing"}, imported
