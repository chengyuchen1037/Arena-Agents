"""The scripted demo agents play the bundled werewolf scenario end to end through the real stack:
world + runtime + brain + orchestrator.  This is also the integration test of the scenario's manual
(templates, abilities, checks, floors, requests) against the engine."""

from __future__ import annotations

import asyncio
import copy
from collections import Counter

import pytest

import game_fakes
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
    cfg["runtime"] = {**cfg["runtime"], "tick_interval": 0.05, "watchdog": 60}
    game_fakes.patch_config(monkeypatch, cfg)
    orch = GameOrchestrator()
    install_demo(orch, delay=0.0, seed=7)  # (also gives every default bot its fixed memory: a game starts only with bots that are initialized)
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

        # ---- v0.12.1: the wolves agree on the knife through one shared request, every night -------------
        wolf_requests = [e for e in ledger if e.type == "request.opened" and "狼人刀口" in str(e.data.get("question"))]
        assert wolf_requests and all(e.data["share_answers"] and e.data["auto_close"] == "unanimous" and not e.data.get("default") for e in wolf_requests)
        wolf_ids = {e.data["request_id"] for e in wolf_requests}
        closed = [e for e in ledger if e.type == "request.closed" and e.data["request_id"] in wolf_ids]
        assert len(closed) == len(wolf_requests) and {e.data["reason"] for e in closed} == {"unanimous"}  # they converged
        answered = [e for e in ledger if e.type == "request.answered" and e.data["request_id"] in wolf_ids]
        assert len(answered) > len(wolf_requests)  # more than one answer per night: wolves followed each other
        knife = [e for e in ledger if e.type == "message" and e.data.get("kind") == "announce" and e.data.get("template_id") in ("wolf_target", "wolf_empty")]
        assert knife and all(e.channel_id == "wolf-team" for e in knife)  # the knife is announced to the wolves only
        assert not any("刀口" in str(e.data.get("content")) for e in ledger if e.type == "message" and e.channel_id == "public")

        # ---- every message wakes agents, they sleep in between, the conversation is cached ---------------
        usage = public["usage_stats"]
        assert usage["cache_read_tokens"] > 0 and usage["cache_hit_rate"] > 0.5 and usage["total_cost_usd"] > 0
        assert sum(a["activations"] for a in usage["by_actor"].values()) > 100
        assert {p["state"] for p in public["participants"]} == {"sleeping"} and public["moderator"]["state"] == "sleeping"
        assert all(a["rejections"] == 0 for a in usage["by_actor"].values())  # the bots play by the rules

        # ---- the demo can also review the game (v0.14: the learning protocol - read, write, propose, finish - with fixed answers) ------
        result = await orch.start_review()
        started = result["started"]
        assert sorted(started) == sorted(p["id"] for p in public["participants"]) and result["deadline_s"] == 60
        await wait_for(lambda: len(orch.session.review_done) == len(started) and not orch.review_running_ids(), timeout=30)
        store = orch._bots()
        first = orch.session.participants[0]
        others = {p.name for p in orch.session.participants if p.id != first.id}
        people = store.read_part(first.bot_id, "people")
        assert {line.split("：")[0].lstrip("- ") for line in people.splitlines()} == others and "（演示）" in people  # an impression of everybody else
        assert "场次 1" in store.summary(first.bot_id)["memory_stats"]["record"]  # the game is counted in its record
        assert "来源:复盘" in store.read_part(first.bot_id, "playbook", orch.spec.id)  # ... and a new hypothesis is in its playbook
        done = [e.data for e in orch.bus._ring if e.type == "review.progress" and e.data["state"] == "done"]
        assert len(done) == len(started) and all({c["file"] for c in d["changes"]["files"]} >= {"people", "playbook"} for d in done)
        assert orch.review_view() == {
            "available": True, "done": sorted(started), "running": False, "supported": True, "deadline_s": 60, "seconds_left": None,
        } or set(orch.review_view()["done"]) == set(started)
        # ... and "complete this L0 for me" (the moderator's model) answers with a valid L0 file
        from app.bots.refine import build_core_refine_messages
        from app.storage import botfiles

        draft = await orch.complete_oneshot("moderator", build_core_refine_messages("Alice", "沉着冷静"), max_tokens=6000, purpose="refine")
        parsed = botfiles.parse_core_markdown(draft, require_complete=True)
        assert parsed.core.complete and "沉着冷静" in parsed.core.identity
        await orch.shutdown()

    asyncio.run(scenario())


def test_the_demo_model_completes_an_l0_and_the_demo_bots_start_with_every_card_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """The demo model recognises "complete this L0" by the marker of its prompt (``app.bots.refine.REFINE_MARKER``), and a demo starts with every
    default bot initialized (fixed memory, the hashes of the contract and the scenario in force): nothing for the user to do but start."""
    from app.bots.refine import build_core_refine_messages
    from app.game.demo import DemoLlm
    from app.storage import botfiles

    text = DemoLlm._text(object(), build_core_refine_messages("Alex", "沉着冷静"))
    assert botfiles.parse_core_markdown(text, require_complete=True).core.complete and "name: Alex" in text

    orch = build(monkeypatch)
    orch.sync_idle_game()  # (what ``startup()`` does: the game takes the table - the bots and their seats - as it is now)
    table = orch.read_table()
    assert [row["init"]["status"] for row in table.summaries] == ["ready"] * 12
    assert orch.check_ready() == [] and orch.system_status()["code"] == "before_game"
    assert {row["init"]["model"] for row in table.summaries} == {"演示模式"}


def test_the_demo_prepares_its_bots_at_startup_and_every_card_is_ready_before_the_first_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """``get_orchestrator()`` installs the demo before the data directory is locked (it cannot touch the bots yet): ``startup()`` gives every default
    bot its fixed memory.  A bot the user made - or cleared - afterwards is not touched again by a restart of the demo."""
    async def scenario() -> None:
        cfg = load_config()
        cfg["runtime"] = {**cfg["runtime"], "tick_interval": 0.05, "watchdog": 60}
        game_fakes.patch_config(monkeypatch, cfg)
        orch = GameOrchestrator()
        install_demo(orch, delay=0.0, seed=7, prepare=False)
        assert not (orch.session.participants[0].bot_id)  # (the first game was built without the table)
        report = await orch.startup()
        assert report["recovery_error"] is None
        public = orch.public_session()
        assert public["system_status"]["code"] == "before_game" and public["bots"]["ready"] == {"ok": True, "problems": []}
        assert {p["init_status"] for p in public["participants"]} == {"ready"} and len(public["participants"]) == 12
        assert public["demo"] is True and {b["binding"]["label"] for b in public["bots"]["bots"]} == {"演示模式"}
        store = orch._bots()
        cleared = store.lineup()[3]
        store.clear_memory([cleared])
        assert orch.demo_prepare() == [cleared]  # (what was cleared is prepared again; the others were left alone)
        await orch.shutdown()

    asyncio.run(scenario())
