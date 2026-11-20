"""A whole scripted game of the bundled 12-player scenario with a person at one seat (v0.13, plan §6.1).

The demo bots (``app/game/demo.py``) play every other seat and the moderator through the real stack; the person is a
script that makes the very calls the HTTP layer makes (``seat_speak`` / ``seat_respond`` / ``seat_pass``), looking at
nothing but the seat state the server pushes.  No model is called.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

import game_fakes
from app.config import load_config
from app.game.demo import install_demo
from app.game.orchestrator import GameOrchestrator
from app.world.chat import speech_gate
from app.world.types import Receipt


def build(monkeypatch: pytest.MonkeyPatch) -> GameOrchestrator:
    cfg = load_config()
    cfg["runtime"] = {**cfg["runtime"], "tick_interval": 0.05, "watchdog": 60}
    game_fakes.patch_config(monkeypatch, cfg)
    orch = GameOrchestrator()
    install_demo(orch, delay=0.0, seed=7)  # (also gives every default bot its fixed memory: a game starts only with bots that are initialized)
    return orch


async def wait_for(predicate: Any, timeout: float = 150.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the demo game did not finish in time")
        await asyncio.sleep(0.05)


class Person:
    """Plays a seat: speaks when the floor is theirs (once in a free talk), answers every request, never breaks a rule."""

    def __init__(self, orch: GameOrchestrator, seat: str) -> None:
        self.orch = orch
        self.seat = seat
        self.log: list[tuple[str, str, dict[str, Any]]] = []
        self.checked = 0
        self.spoke_free: set[str] = set()
        # (the game was started and stopped once before the seat was taken over, H52: what the seat's agent did by then is not the person's)
        self.calls_at_takeover = orch.usage.by_actor[seat].calls if seat in orch.usage.by_actor else 0
        self.event_at_takeover = orch.bus.last_id

    def answer_for(self, request_id: str) -> str:
        request = self.orch.world.state.requests[request_id]
        if request.kind == "text":
            return "我是真人，这是我的回答。"
        ids = [o["id"] for o in request.options]
        for preferred in ("abstain", "none", "pass"):
            if preferred in ids:
                return preferred
        return next((i for i in ids if i != self.seat), ids[0])

    def check_state_against_the_gate(self, state: dict[str, Any]) -> None:
        world = self.orch.world
        for channel in world.state.channels:
            if self.seat not in channel.members:
                continue
            gate = speech_gate(world, channel, self.seat)
            assert state["channels"][channel.id]["can_write"] is (not isinstance(gate, Receipt)), channel.id
            self.checked += 1

    async def play(self) -> None:
        orch = self.orch
        while orch.session.status != "finished":
            await asyncio.sleep(0.01)
            if orch.session.status != "running" or orch.takeover_seat != self.seat:
                continue
            state = orch.takeover_payload()
            self.check_state_against_the_gate(state)
            for channel_id, channel in state["channels"].items():
                floor = channel["floor"]
                if floor and floor["mine"] and channel["can_write"]:
                    receipt = orch.seat_speak(channel_id, f"（真人）轮到我了：{channel_id}", state["seq"])
                    self.log.append(("speak", channel_id, receipt))
                elif floor and floor["kind"] == "free" and channel["can_write"] and channel_id not in self.spoke_free and channel_id != f"dm-{self.seat}":
                    self.spoke_free.add(channel_id)
                    self.log.append(("speak", channel_id, orch.seat_speak(channel_id, "（真人）自由讨论里说一句。", state["seq"])))
            for request in state["requests"]:
                if not request["answered"]:
                    receipt = orch.seat_respond(request["id"], self.answer_for(request["id"]), "我的理由")
                    self.log.append(("respond", request["id"], receipt))


async def play_a_game(monkeypatch: pytest.MonkeyPatch, role: str) -> tuple[GameOrchestrator, str, Person]:
    orch = build(monkeypatch)
    # (v0.14, H52: the identities are dealt when the game starts - so the game is started and stopped at once, and the person takes over the
    # seat that holds ``role``, as a person who picked it after the first deal would)
    await orch.start_or_resume()
    await orch.pause("user")
    seat = next(p.id for p in orch.world.state.players if p.role == role)
    await orch.takeover(seat)
    assert orch.public_session()["takeover"]["seat"] == seat
    person = Person(orch, seat)
    playing = asyncio.ensure_future(person.play())
    await orch.start_or_resume()
    assert seat not in orch._supervisor.agents  # type: ignore[union-attr]
    await wait_for(lambda: orch.session.status in ("finished", "paused"))
    assert orch.session.status == "finished", (orch.session.status, orch.session.hold_reason)
    await playing
    return orch, seat, person


@pytest.mark.parametrize("role", ["villager", "seer"])
def test_a_scripted_werewolf_game_plays_to_the_end_with_a_person_at_one_seat(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    async def scenario() -> None:
        # (the deal is random: a seat that is killed in the first night never gets to act - then another game is played)
        for _attempt in range(6):
            orch, seat, person = await play_a_game(monkeypatch, role)
            if person.log:
                break
            await orch.shutdown()
        else:
            raise AssertionError("the person never got to act in six games")

        assert orch.world.state.winner in ("好人阵营", "狼人阵营")
        ledger = orch.world.ledger
        # every action of the person was accepted: the person plays by the rules the interface shows
        assert all(receipt["accepted"] for _kind, _target, receipt in person.log), [
            (k, t, r["code"], r["message"]) for k, t, r in person.log if not r["accepted"]
        ]
        spoken = [e for e in ledger if e.type == "message" and e.actor_id == seat and e.data.get("kind") == "speech"]
        assert all(e.data["content"].startswith("（真人）") for e in spoken)
        answered = [e for e in ledger if e.type == "request.answered" and e.actor_id == seat]
        assert len(spoken) + len(answered) == len(person.log)  # what the person did is what the ledger has
        assert person.checked > 20  # the seat state was compared with the gate again and again
        # the seat never had an agent: nothing was called or counted for it
        public = orch.public_session()
        assert public["usage_stats"]["by_actor"].get(seat, {}).get("calls", 0) == person.calls_at_takeover
        assert {p["id"]: p["state"] for p in public["participants"]}[seat] == "human"
        assert not [
            e for e in orch.bus._ring
            if e.type in ("agent.status", "agent.metrics") and e.data.get("id") == seat and e.id > person.event_at_takeover
        ]
        # everybody else played: the ledger holds the other seats' speeches, votes and the moderator's rulings
        assert {e.actor_id for e in ledger if e.type == "message" and e.data.get("kind") == "speech"} - {seat}
        assert any(e.type == "request.closed" and e.data.get("reason") for e in ledger)
        pushes = [e for e in orch.bus._ring if e.type == "takeover.updated"]
        assert len(pushes) > 10 and all(e.data["takeover"]["seat"] == seat for e in pushes)
        assert [(s.seat, s.to_seq) for s in orch.session.human_spans] == [(seat, None)]
        await orch.release_takeover()
        assert orch.session.human_spans[0].to_seq == orch.world.seq
        await orch.shutdown()

    asyncio.run(scenario())
