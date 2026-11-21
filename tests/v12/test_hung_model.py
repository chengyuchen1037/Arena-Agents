"""A model call that hangs must never stall the game: the speaking-time limit still fires, the
holder's worker is cancelled, and the next player gets the floor (the time limit is the engine's
protection against slow providers; real-model smoke tests showed multi-minute provider stalls)."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

import game_fakes
from app.game.orchestrator import GameOrchestrator
from app.world.types import MODERATOR_ID

from .bots import BotLlm, Call, simple_player
from .test_e2e_minimal import BASE_CFG, PLAYERS, wait_for


def moderator(world: Any, me: str) -> list[Call]:
    ids = [p["id"] for p in PLAYERS]
    if not any(e.type == "channel.created" and e.channel_id == "public" for e in world.entries_visible(me)):
        return [
            ("create_channel", {"channel_id": "public", "name": "公共频道", "members": ids, "type": "public"}),
            ("set_floor_queue", {"channel_id": "public", "order": ids, "limits": {"max_messages": 1, "duration_s": 1.5}}),
        ]
    return []


class HangingLlm(BotLlm):
    """The first call of ``victim`` never returns (until it is cancelled)."""

    def __init__(self, world: Any, policies: dict, victim: str) -> None:
        super().__init__(world, policies)
        self.victim = victim
        self.hung = False
        self.cancelled = False

    async def complete(self, agent_id, messages, tools, *, max_tokens=None):
        if agent_id == self.victim and not self.hung:
            self.hung = True
            try:
                await asyncio.sleep(120)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return await super().complete(agent_id, messages, tools, max_tokens=max_tokens)


def test_a_hanging_model_call_does_not_stall_the_game(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        game_fakes.patch_config(monkeypatch, BASE_CFG)
        orch = GameOrchestrator()
        game_fakes.ready_bots(orch)  # (v0.14: a game starts only with bots that are initialized)
        orch.ensure_bindings_ready = lambda: None  # type: ignore[method-assign]
        holder: dict[str, HangingLlm] = {}

        def factory(o: GameOrchestrator) -> HangingLlm:
            holder["llm"] = HangingLlm(o.world, {MODERATOR_ID: moderator, **{p["id"]: simple_player for p in PLAYERS}}, "u1")
            return holder["llm"]

        orch.llm_port_factory = factory
        await orch.start_or_resume()
        # u1 (first in the queue) hangs; its 1.5 s token times out, then everybody else speaks
        await wait_for(lambda: any(e.type == "floor.completed" for e in orch.world.ledger), timeout=20)
        ledger = orch.world.ledger
        ended = {e.data["holder"]: e.data["reason"] for e in ledger if e.type == "floor.token_ended"}
        assert ended["u1"] == "timeout" and all(ended[u] in ("used", "pass") for u in ("u2", "u3", "u4"))
        spoke = [e.actor_id for e in ledger if e.type == "message" and e.data.get("kind") == "speech"]
        assert spoke == ["u2", "u3", "u4"]  # the hung player was skipped, the others were not held up
        await asyncio.sleep(0.3)
        assert holder["llm"].cancelled  # the hung call was cancelled when its floor was revoked
        await orch.shutdown()
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert not [t for t in leftovers if "supervisor" in (t.get_name() or "") or "agent" in (t.get_name() or "")]

    asyncio.run(scenario())
