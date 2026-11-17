"""End-to-end: the whole stack (world + runtime + brain + orchestrator) plays a 4-player game with
scripted agents.  No network, no real model."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import app.game.orchestrator as orchestrator_module
from app.game.orchestrator import GameOrchestrator
from app.world.types import MODERATOR_ID

from .bots import BotLlm, Call, simple_player

PLAYERS = [{"id": f"u{i}", "seat": f"座位 {i}", "name": f"玩家{i}", "persona": ""} for i in range(1, 5)]
BASE_CFG: dict[str, Any] = {
    "app": {"title": "测试"},
    "scenario": {"path": "scenarios/minimal_test.md"},
    "participants": PLAYERS,
    "tag_styles": {},
    "llm": {},
    "knowledge_base": {"docs_dir": "knowledge"},
    "runtime": {"tick_interval": 0.05, "watchdog": 30, "llm_timeout_seconds": 10},
}


def moderator_policy() -> Any:
    """Opens the channel, runs one speaking round, one sealed vote, announces the result, ends."""
    state = {"stage": 0}

    def policy(world: Any, me: str) -> list[Call]:
        ids = [p["id"] for p in PLAYERS]
        ledger = world.entries_visible(MODERATOR_ID)
        seen = {e.type for e in ledger}
        stage = state["stage"]
        if stage == 0:
            state["stage"] = 1
            return [
                ("create_channel", {"channel_id": "public", "name": "公共频道", "members": ids, "type": "public"}),
                ("set_phase", {"text": "第 1 轮 · 发言"}),
                ("set_floor_queue", {"channel_id": "public", "order": ids, "limits": {"max_messages": 1}}),
            ]
        if stage == 1 and "floor.completed" in seen:
            state["stage"] = 2
            return [
                ("open_request", {"to": ids, "kind": "choice", "question": "请投票", "sealed": True, "duration_s": 20,
                                  "options": [{"id": i, "label": i} for i in ids]}),
                ("set_phase", {"text": "第 1 轮 · 投票"}),
            ]
        if stage == 2 and "request.closed" in seen:
            closed = [e for e in ledger if e.type == "request.closed"][-1]
            tally: dict[str, int] = {}
            for answer in closed.data.get("answers") or []:
                tally[answer["value"]] = tally.get(answer["value"], 0) + 1
            target = sorted(tally, key=lambda k: (-tally[k], k))[0]
            state["stage"] = 3
            return [
                ("update_player", {"player_id": target, "add_tags": ["出局"], "remove_tags": ["存活"]}),
                ("announce", {"channel_id": "public", "template_id": "out_notice", "params": {"players": [target]}}),
                ("end_game", {"winner": "测试队", "summary": "一轮即终"}),
            ]
        return []

    return policy


def build(monkeypatch: pytest.MonkeyPatch) -> GameOrchestrator:
    monkeypatch.setattr(orchestrator_module, "load_config", lambda *a, **k: dict(BASE_CFG))
    orch = GameOrchestrator()
    orch.ensure_bindings_ready = lambda: None  # type: ignore[method-assign]  # no providers in this test
    orch.llm_port_factory = lambda o: BotLlm(
        o.world, {MODERATOR_ID: moderator_policy(), **{p["id"]: simple_player for p in PLAYERS}}
    )
    return orch


async def wait_for(predicate: Any, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)


def test_a_whole_game_runs_to_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        orch = build(monkeypatch)
        assert orch.session.status == "idle" and len(orch.world.state.players) == 4
        # dealing happened at creation: every player has a role, the public snapshot hides nothing yet
        assert all(p.role for p in orch.world.state.players)
        await orch.start_or_resume()
        assert orch.session.status == "running"
        await wait_for(lambda: orch.session.status == "finished")

        types = [e.type for e in orch.world.ledger]
        assert types.count("floor.completed") == 1 and "request.closed" in types and types[-1] == "game.status"
        speeches = [e for e in orch.world.ledger if e.type == "message" and e.data.get("kind") == "speech"]
        assert len(speeches) == 4  # one per player, in seat order
        assert [e.actor_id for e in speeches] == ["u1", "u2", "u3", "u4"]
        assert orch.world.state.winner == "测试队"

        public = orch.public_session()
        assert public["demo"] is False
        assert public["status"] == "finished" and public["winner"] == "测试队"
        assert any(m["kind"] == "announce" for m in public["messages"])
        assert public["usage_stats"]["total_tokens"] > 0
        by_actor = public["usage_stats"]["by_actor"]
        assert by_actor[MODERATOR_ID]["tool_calls"] >= 5 and by_actor["u1"]["calls"] >= 1
        await orch.shutdown()

    asyncio.run(scenario())


def test_pause_resume_and_continue_after_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        orch = build(monkeypatch)
        await orch.start_or_resume()
        await orch.pause("user")
        assert orch.session.status == "paused" and not orch.world.running and orch.clock.paused
        paused_seq = orch.world.seq
        await asyncio.sleep(0.3)
        assert orch.world.seq == paused_seq  # nothing happens while paused
        await orch.start_or_resume()
        await wait_for(lambda: orch.session.status == "finished")
        # D8: a finished game can be continued
        await orch.start_or_resume()
        assert orch.session.status == "running" and orch.world.running
        statuses = [e.data["reason"] for e in orch.world.ledger if e.type == "game.status"]
        assert "continued" in statuses
        await orch.shutdown()

    asyncio.run(scenario())


def test_save_and_restore_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        orch = build(monkeypatch)
        await orch.start_or_resume()
        await wait_for(lambda: orch.session.status == "finished")
        payload = orch.session_payload()
        await orch.shutdown()

        other = build(monkeypatch)
        loaded = other._validate_session_data(payload)
        await other._adopt_loaded_session(loaded, "已导入")
        assert other.session.status == "finished" and other.session.restored_from_save
        assert other.world.seq == orch.world.seq and other.world.state.winner == "测试队"
        assert [e.seq for e in other.world.ledger] == [e.seq for e in orch.world.ledger]
        assert other.public_session()["usage_stats"]["total_tokens"] == orch.public_session()["usage_stats"]["total_tokens"]

    asyncio.run(scenario())


def test_incompatible_old_saves_are_rejected_politely(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = build(monkeypatch)
    with pytest.raises(orchestrator_module.SessionLoadError, match="旧版本"):
        orch._validate_session_data({"schema_version": 2, "session_id": "old"})
