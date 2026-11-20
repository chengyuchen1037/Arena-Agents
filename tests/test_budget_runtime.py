"""The orchestrator and the spending limit (contract 10.4; docs/v0.13-plan.md budget): a limit that stops the game, one that only warns, the
cost limit that counts money, what a save keeps - and the banner the stall watchdog shows.

(These cases lived in ``tests/test_roster_runtime.py`` with the roster logic that v0.14 replaced by the bots; the budget logic did not change.)
The orchestrator is the real one over fake stores (``game_fakes.make_table_orch``: four seats, every bot initialized, every actor with a model),
with scripted agents - no network."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from game_fakes import TABLE_IDS, async_test, chatty, events_of, idle, make_table_orch, wait_for
from v12.bots import BotLlm, simple_player

from app.game.orchestrator import GameOrchestrator
from app.world.types import MODERATOR_ID


def play_with(orch: GameOrchestrator, moderator_policy: Any) -> None:
    """Scripted agents (no network); the moderator does what ``moderator_policy`` says and the players answer each other."""
    orch.ensure_bindings_ready = lambda: None  # type: ignore[method-assign]
    orch.llm_port_factory = lambda o: BotLlm(o.world, {MODERATOR_ID: moderator_policy, **{seat: simple_player for seat in TABLE_IDS}})


@async_test
async def test_the_budget_defaults_to_the_saved_one_and_a_change_is_saved_for_the_next_game(monkeypatch):
    orch = make_table_orch(monkeypatch)
    assert orch.budget_payload()["state"] == "ok" and orch.session.budget.max_tokens is None
    view = await orch.set_budget({"max_tokens": 5000, "max_cost_usd": 1.5, "force_stop": True})
    assert view["max_tokens"] == 5000 and view["max_cost_usd"] == 1.5 and view["force_stop"] is True and view["used_tokens"] == 0
    assert orch._settings().budget_defaults == {"max_tokens": 5000, "max_cost_usd": 1.5, "force_stop": True}
    await orch.new_game()
    assert orch.session.budget.max_tokens == 5000  # per game, but a new game starts from the last choice
    with pytest.raises(Exception):
        await orch.set_budget({"max_tokens": -1})
    with pytest.raises(Exception):
        await orch.set_budget({"unknown": 1})


@async_test
async def test_reaching_a_stopping_limit_pauses_the_game_with_the_budget_reason(monkeypatch):
    orch = make_table_orch(monkeypatch)
    play_with(orch, chatty)
    await orch.set_budget({"max_tokens": 450, "force_stop": True})  # a scripted call is 150 tokens
    await orch.start_or_resume()
    await wait_for(lambda: orch.session.status == "paused")
    assert orch.session.pause_reason == "budget" and "预算" in orch._status_text()
    assert orch.usage.total_tokens >= 450 and orch.budget_payload()["state"] == "reached"
    assert events_of(orch, "budget.reached")
    assert not orch._is_active()
    with pytest.raises(RuntimeError, match="预算上限"):
        await orch.start_or_resume()  # it would only stop again at once
    await orch.set_budget({"max_tokens": 10**7, "force_stop": True})  # raise the limit ...
    await orch.start_or_resume()  # ... and the game goes on
    try:
        assert orch.session.status == "running" and orch.session.pause_reason == ""
    finally:
        await orch.pause()


@async_test
async def test_a_limit_without_force_stop_only_warns(monkeypatch):
    orch = make_table_orch(monkeypatch)
    play_with(orch, chatty)
    await orch.set_budget({"max_tokens": 300, "force_stop": False})
    await orch.start_or_resume()
    try:
        await wait_for(lambda: events_of(orch, "budget.reached"))
        await asyncio.sleep(0.2)
        assert orch.session.status == "running"  # the user chose to keep playing
        assert orch.budget_payload()["state"] == "reached"
    finally:
        await orch.pause()


@async_test
async def test_lowering_the_limit_below_what_is_used_stops_a_running_game_at_once(monkeypatch):
    orch = make_table_orch(monkeypatch)
    play_with(orch, chatty)
    await orch.start_or_resume()
    await wait_for(lambda: orch.usage.total_tokens > 0)
    await orch.set_budget({"max_tokens": 1, "force_stop": True})
    await wait_for(lambda: orch.session.status == "paused")
    assert orch.session.pause_reason == "budget"


@async_test
async def test_the_cost_limit_counts_money_not_tokens(monkeypatch):
    orch = make_table_orch(monkeypatch)
    orch.usage.record_usage("N1", prompt_tokens=1_000_000, completion_tokens=0, input_price_per_m=2.0, output_price_per_m=8.0)
    view = await orch.set_budget({"max_cost_usd": 2.5})
    assert view["used_cost_usd"] == 2.0 and view["state"] == "warning" and view["progress"] == 0.8
    orch.usage.record_usage("N1", prompt_tokens=250_000, input_price_per_m=2.0, output_price_per_m=8.0)
    orch._on_budget_changed(force=True)
    assert orch.budget_payload()["state"] == "reached"


@async_test
async def test_a_save_keeps_the_limit_and_a_game_that_is_loaded_has_it_again(monkeypatch):
    orch = make_table_orch(monkeypatch)
    await orch.set_budget({"max_tokens": 777, "force_stop": True})
    orch.session.review_done = ["N1"]
    payload = orch.session_payload()
    assert payload["budget"]["max_tokens"] == 777 and payload["review_done"] == ["N1"] and payload["memory_enabled"] is True
    other = make_table_orch(monkeypatch)
    await other.load_session_data(payload)
    assert other.session.budget.max_tokens == 777 and other.session.review_done == ["N1"]
    assert other.session.status == "idle" and len(other._book) == 0  # conversations are never saved


@async_test
async def test_the_watchdog_only_shows_a_banner_and_never_pauses(monkeypatch):
    orch = make_table_orch(monkeypatch)
    play_with(orch, idle)
    await orch.start_or_resume()
    try:
        orch._on_stall("对局已静止 60 秒，主持人也没有推进。")
        await wait_for(lambda: any("静止" in e.data.get("stall_notice", "") for e in events_of(orch, "session.updated")))
        assert orch.session.status == "running" and "静止" in orch.public_session()["stall_notice"]
        orch._on_stall(None)
        await wait_for(lambda: events_of(orch, "session.updated")[-1].data["stall_notice"] == "")
        assert orch.public_session()["stall_notice"] == "" and orch.session.status == "running"
    finally:
        await orch.pause()
