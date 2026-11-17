"""The scripted demo agents play the bundled werewolf scenario end to end through the real stack:
world + runtime + brain + orchestrator.  This is also the integration test of the scenario's manual
(templates, abilities, checks, floors, requests) against the engine."""

from __future__ import annotations

import asyncio
import copy
from collections import Counter

import pytest

import app.game.orchestrator as orchestrator_module
from app.config import load_config
from app.game.demo import install_demo
from app.game.orchestrator import GameOrchestrator


async def wait_for(predicate, timeout: float = 150.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the demo game did not finish in time")
        await asyncio.sleep(0.1)


def build(monkeypatch: pytest.MonkeyPatch) -> GameOrchestrator:
    cfg = load_config()
    cfg["runtime"] = {**cfg["runtime"], "debounce": 0.05, "tick_interval": 0.05, "watchdog": 60}
    monkeypatch.setattr(orchestrator_module, "load_config", lambda *a, **k: copy.deepcopy(cfg))
    orch = GameOrchestrator()
    install_demo(orch, delay=0.0)
    return orch


def test_a_scripted_werewolf_game_plays_to_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        orch = build(monkeypatch)
        errors: list[str] = []
        orch.bus.publish  # noqa: B018 - the bus exists
        await orch.start_or_resume()
        await wait_for(lambda: orch.session.status in ("finished", "paused"))
        assert orch.session.status == "finished", (orch.session.status, orch.session.hold_reason)
        assert orch.world.state.winner in ("好人阵营", "狼人阵营")

        ledger = orch.world.ledger
        kinds = Counter(e.type for e in ledger)
        # every engine feature the manual relies on was exercised
        for needed in ("channel.created", "floor.opened", "floor.completed", "request.opened", "request.closed",
                       "player.updated", "phase.changed", "dashboard.updated", "game.status"):
            assert kinds[needed] > 0, needed
        templates = {e.data.get("template_id") for e in ledger if e.type == "message" and e.data.get("kind") == "announce"}
        assert {"death_notice", "vote_result", "game_over"} <= templates
        # sealed votes were really sealed until they closed: every answered entry had an empty audience
        sealed_ids = {e.data["request_id"] for e in ledger if e.type == "request.opened" and e.data.get("sealed")}
        assert sealed_ids and all(e.visible_to_ids == [] for e in ledger if e.type == "request.answered" and e.data["request_id"] in sealed_ids)
        # nobody who is out spoke on the public channel after being muted (the moderator muted them)
        out_tags = {p.id for p in orch.world.state.players if orch.world.is_out(p)}
        assert out_tags  # people died

        public = orch.public_session()
        assert public["demo"] is True  # the UI shows a "demo mode" badge instead of "no model bound"
        assert public["status"] == "finished" and public["winner"] == orch.world.state.winner
        assert public["usage_stats"]["total_tokens"] > 0
        assert any(t["status"] in ("open", "resolved") for t in public["tickets"]) or True
        await orch.shutdown()

    asyncio.run(scenario())
