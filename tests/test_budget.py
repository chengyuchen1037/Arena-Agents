"""v0.12.1 spending limit of a game (E10, E22): pure logic."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.game.budget import OK, REACHED, WARNING, BudgetSettings, BudgetWatch, progress, public_view, state_of


def test_no_limit_never_warns_or_stops():
    settings = BudgetSettings()
    assert progress(settings, 10**9, 10**6) == 0.0 and state_of(settings, 10**9, 10**6) == OK
    assert BudgetWatch().update(settings, 10**9, 10**6) is None


@pytest.mark.parametrize("tokens,state", [(0, OK), (799, OK), (800, WARNING), (999, WARNING), (1000, REACHED), (5000, REACHED)])
def test_the_token_limit_has_a_warning_at_80_percent_and_is_reached_at_100(tokens, state):
    assert state_of(BudgetSettings(max_tokens=1000), tokens, 0.0) == state


def test_the_worse_of_the_two_limits_decides():
    settings = BudgetSettings(max_tokens=1_000_000, max_cost_usd=2.0)
    assert state_of(settings, 100, 1.7) == WARNING  # tokens are fine, the cost is at 85 %
    assert state_of(settings, 100, 2.0) == REACHED
    assert state_of(settings, 900_000, 0.1) == WARNING
    assert progress(settings, 500_000, 1.5) == 0.75


def test_blank_and_zero_mean_no_limit_and_nonsense_is_rejected():
    assert BudgetSettings(max_tokens="", max_cost_usd=None).max_tokens is None
    assert BudgetSettings(max_tokens=0, max_cost_usd=0).max_cost_usd is None
    for bad in ({"max_tokens": -5}, {"max_cost_usd": -0.1}, {"max_cost_usd": float("nan")}, {"max_tokens": 10**13}, {"max_tokens": "abc"}, {"surprise": 1}):
        with pytest.raises(ValidationError):
            BudgetSettings(**bad)


def test_the_public_view_has_everything_the_front_end_draws():
    view = public_view(BudgetSettings(max_tokens=1000, force_stop=True), 850, 0.123456789)
    assert view == {"max_tokens": 1000, "max_cost_usd": None, "force_stop": True, "used_tokens": 850,
                    "used_cost_usd": 0.123457, "progress": 0.85, "state": WARNING}


def test_each_threshold_is_announced_once_in_order():
    watch, settings = BudgetWatch(), BudgetSettings(max_tokens=1000)
    assert [watch.update(settings, t, 0) for t in (100, 790, 800, 850, 999, 1000, 1200, 2000)] == [None, None, WARNING, None, None, REACHED, None, None]


def test_a_jump_straight_past_the_warning_announces_only_reached():
    watch = BudgetWatch()
    assert watch.update(BudgetSettings(max_tokens=1000), 5000, 0) == REACHED


def test_raising_the_limit_rearms_the_announcements():
    watch = BudgetWatch()
    low = BudgetSettings(max_tokens=1000)
    assert watch.update(low, 1000, 0) == REACHED
    high = BudgetSettings(max_tokens=2000)
    assert watch.update(high, 1000, 0) is None  # 50 %: back to normal
    assert watch.update(high, 1700, 0) == WARNING
    assert watch.update(high, 2000, 0) == REACHED
    watch.reset()
    assert watch.update(high, 2000, 0) == REACHED  # a new game starts fresh


def test_lowering_the_limit_below_what_was_used_announces_at_once():
    watch = BudgetWatch()
    assert watch.update(BudgetSettings(max_tokens=10_000), 5000, 0) is None
    assert watch.update(BudgetSettings(max_tokens=4000), 5000, 0) == REACHED
