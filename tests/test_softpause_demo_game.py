"""A whole scripted game of the bundled 12-player scenario in which a person soft-pauses now and then (v0.13.1, plan §6.1).

The demo bots (``app/game/demo.py``) play every other seat and the moderator through the real stack, as in
``tests/test_takeover_demo_game.py``.  The person takes a soft pause when something waits for them: the agents are held
(no model call is made, the game clock stands still), the person acts meanwhile - the engine's rules are the same -, then
lets the agents go and the game plays on to its end.  The person's turns in the public channel have no deadline, and no
turn of theirs ends by time.  No model is called.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.game.orchestrator import GameOrchestrator
from test_takeover_demo_game import Person, build, wait_for

HELD = {"state": "held", "in_flight": 0}


class SoftPerson(Person):
    """Plays like :class:`Person` and takes a soft pause the first few times something waits for them."""

    def __init__(self, orch: GameOrchestrator, seat: str, pauses: int = 3) -> None:
        super().__init__(orch, seat)
        self.pauses_left = pauses
        self.pauses: list[dict[str, Any]] = []

    def act(self, state: dict[str, Any]) -> None:
        for channel_id, channel in state["channels"].items():
            floor = channel["floor"]
            if floor and floor["mine"] and channel["can_write"]:
                receipt = self.orch.seat_speak(channel_id, f"（真人）轮到我了：{channel_id}", state["seq"])
                self.log.append(("speak", channel_id, receipt))
            elif floor and floor["kind"] == "free" and channel["can_write"] and channel_id not in self.spoke_free and channel_id != f"dm-{self.seat}":
                self.spoke_free.add(channel_id)
                self.log.append(("speak", channel_id, self.orch.seat_speak(channel_id, "（真人）自由讨论里说一句。", state["seq"])))
        for request in state["requests"]:
            if not request["answered"]:
                receipt = self.orch.seat_respond(request["id"], self.answer_for(request["id"]), "我的理由")
                self.log.append(("respond", request["id"], receipt))

    async def think(self) -> None:
        """A soft pause: the agents are held, the clock stands still, the person acts, then everybody goes on."""
        orch = self.orch
        model = orch.llm_port_factory(orch)  # the demo model of this game (its call counter)
        await orch.soft_pause()
        await wait_for(lambda: orch.soft_pause_payload() in (HELD, None), timeout=60)
        if orch.soft_pause_payload() is None:  # the game moved on without us (it was ended or stopped meanwhile)
            return
        record: dict[str, Any] = {"calls": model.calls, "clock": orch.clock.now(), "acted": 0, "ok": True}
        await asyncio.sleep(0.3)
        state = orch.takeover_payload()
        if orch.soft_pause_payload() is not None and orch.session.status == "running":
            before = len(self.log)
            self.check_state_against_the_gate(state)
            self.act(state)  # (a person acts during the pause: the same rules, the same endpoints)
            record["acted"] = len(self.log) - before
            await asyncio.sleep(0.2)
            record["ok"] = model.calls == record["calls"] and orch.clock.now() == record["clock"]
        self.pauses.append(record)
        if orch.soft_pause_payload() is not None:
            await orch.soft_resume()

    async def play(self) -> None:
        orch = self.orch
        while orch.session.status != "finished":
            await asyncio.sleep(0.01)
            if orch.session.status != "running" or orch.takeover_seat != self.seat:
                continue
            state = orch.takeover_payload()
            self.check_state_against_the_gate(state)
            if state["attention"] and self.pauses_left > 0:
                self.pauses_left -= 1
                await self.think()
                continue
            self.act(state)


def test_a_person_who_soft_pauses_now_and_then_plays_a_scripted_game_to_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        # (the deal is random: a seat that is killed in the first night never gets anything to do - then another game is played)
        for _attempt in range(6):
            orch = build(monkeypatch)
            await orch.start_or_resume()  # (the identities are dealt when the game starts, H52: the seat is picked by its identity afterwards)
            await orch.pause("user")
            seat = next(p.id for p in orch.world.state.players if p.role == "villager")
            await orch.takeover(seat)
            person = SoftPerson(orch, seat)
            playing = asyncio.ensure_future(person.play())
            await orch.start_or_resume()
            await wait_for(lambda: orch.session.status in ("finished", "paused"))
            assert orch.session.status == "finished", (orch.session.status, orch.session.hold_reason)
            await playing
            if person.pauses:
                break
            await orch.shutdown()
        else:
            raise AssertionError("the person never got anything to do in six games")

        assert orch.world.state.winner in ("好人阵营", "狼人阵营")
        # every soft pause was a real one: no model call, no tick of the clock, while the agents were held
        assert person.pauses and all(p["ok"] for p in person.pauses), person.pauses
        assert sum(p["acted"] for p in person.pauses) >= 1  # the person did something during a pause, and the engine took it
        assert all(receipt["accepted"] for _kind, _target, receipt in person.log), [
            (k, t, r["code"], r["message"]) for k, t, r in person.log if not r["accepted"]
        ]
        # the pauses were announced to the clients, and every one of them ended
        held = [e for e in orch.bus._ring if e.type == "session.updated" and e.data.get("soft_pause") == HELD]
        assert len(held) >= 1 and orch.soft_pause_payload() is None and orch.clock_running() is False
        assert orch.public_session()["soft_pause"] is None and orch._stall_notice == ""
        # the person's turns in the public channel had no deadline and none of them ended by time
        ledger = orch.world.ledger
        public = {c.id for c in orch.world.state.channels if c.type == "public"}
        grants = [e for e in ledger if e.type == "floor.granted" and e.channel_id in public and e.data.get("holder") == seat]
        assert grants and all(e.data.get("untimed") is True for e in grants)
        ended = [e for e in ledger if e.type == "floor.token_ended" and e.channel_id in public and e.data.get("holder") == seat]
        assert ended and all(e.data["reason"] != "timeout" for e in ended)
        others = [e for e in ledger if e.type == "floor.granted" and e.channel_id in public and e.data.get("holder") != seat]
        assert others and all("untimed" not in e.data for e in others)  # the agents' turns keep their clock
        assert [(s.seat, s.to_seq) for s in orch.session.human_spans] == [(seat, None)]
        await orch.shutdown()

    asyncio.run(scenario())
