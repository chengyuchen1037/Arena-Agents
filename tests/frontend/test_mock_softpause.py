"""The UI mock plays the soft pause and the public floor without a deadline (v0.13.1, contract §12.2).

The real backend (app/game/softpause_runtime.py, app/world/chat.py) lets the model calls in flight finish and then
holds every agent in front of a gate while the game clock stands still; a person who plays a seat holds a public floor
with no deadline.  The mock's script is not an agent runtime, so it plays the same picture - this file checks that

1. the soft pause is refused when it does not apply (with the real backend's sentences), runs through its two phases
   (draining with a falling count, then held), freezes the game clock at the click, keeps the person's own actions
   possible and lets the script go on when it is continued;
2. a hard pause, the end of the game and a new game clear it;
3. the person's turn in a public channel has no deadline (``untimed``, no countdown) and the script waits for it as long
   as it takes, while a turn in another channel still runs out;
4. every event it publishes matches the contract (``validate_event``) and the real client store reduces the log to the
   session the mock holds (``replay_check.js --human``, which now compares ``soft_pause`` and ``clock_running`` too).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
MOCK_PATH = ROOT / "tools" / "mock_ui_server.py"


def _load_mock():
    spec = importlib.util.spec_from_file_location("arena_mock_ui_server_softpause", MOCK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mock = _load_mock()

_WINDOWS_NODE = Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"


def _find_node() -> str | None:
    env = os.environ.get("NODE_BIN")
    if env and Path(env).exists():
        return env
    return shutil.which("node") or (str(_WINDOWS_NODE) if _WINDOWS_NODE.exists() else None)


NODE = _find_node()


def run(coro_factory):
    """Run ``coro_factory()`` on the virtual-time loop of the mock (the whole script takes milliseconds)."""
    return asyncio.run(coro_factory(), loop_factory=mock.VirtualLoop)


def make_game(**kw):
    bus = mock.EventBus(ring=100000)
    game = mock.MockGame(bus, speed=1.0, seed=7, budget=mock.default_budget(None, False), **kw)
    return bus, game


async def until(predicate, *, limit: float = 600.0, step: float = 0.05):
    """Let (virtual) time pass until ``predicate()`` is true; ``limit`` seconds of virtual time at most."""
    loop = asyncio.get_running_loop()
    end = loop.time() + limit
    while not predicate():
        assert loop.time() < end, "the situation never came"
        await asyncio.sleep(step)


def node(game, aid: str) -> dict:
    return game.session["moderator"] if aid == mock.MODERATOR else game.player(aid)


def states(game, *, skip: str | None = None) -> dict[str, str]:
    return {a: node(game, a)["state"] for a in [mock.MODERATOR, *mock.PLAYER_IDS] if a != skip}


def events(bus, *types: str, after: int = 0) -> list[dict]:
    return [e for e in bus.ring if e["id"] > after and (not types or e["type"] in types)]


def updates(bus, after: int = 0) -> list[dict]:
    return [e["data"] for e in events(bus, "session.updated", after=after)]


def start_with(game, seat: str) -> asyncio.Task:
    """The person takes ``seat`` over and the scripted game starts."""
    game.take_over(seat)
    game.gate.set()
    game.set_status("running", reason="started")
    game._spawn(game.play())  # noqa: SLF001  (the game owns the task: cancel_task() stops it)
    return game.task


async def night_of_the_wolves(game, seat: str = "N3") -> None:
    """The wolves' request is open and some of their agents are thinking: model calls are in flight."""
    await until(lambda: game.session["requests"] and any(s == "thinking" for s in states(game, skip=seat).values()))


async def stop(game) -> None:
    await game._cancel_task()  # noqa: SLF001
    game.gate.clear()


# ---------------------------------------------------------------------------------- the refusals
def test_the_soft_pause_is_refused_when_it_does_not_apply_with_the_real_backends_sentences() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        with pytest.raises(mock.TakeoverError) as err:  # idle: nothing runs
            game.soft_pause()
        assert (err.value.status, err.value.detail) == (409, "对局没有在运行。")
        game.take_over("N3")
        with pytest.raises(mock.TakeoverError) as err:  # a seat is played, but the game has not started
            game.soft_pause()
        assert (err.value.status, err.value.detail) == (409, "对局没有在运行。")
        with pytest.raises(mock.TakeoverError) as err:
            game.soft_resume()
        assert (err.value.status, err.value.detail) == (409, "没有处于软暂停。")
        game.release_seat()
        game.gate.set()
        game.set_status("running", reason="started")
        with pytest.raises(mock.TakeoverError) as err:  # running, but nobody to give time to
            game.soft_pause()
        assert (err.value.status, err.value.detail) == (409, "只有接管座位时才能软暂停。")
        game.gate.clear()
        game.set_status("paused", reason="user", pause_reason="user")
        game.take_over("N3")
        with pytest.raises(mock.TakeoverError) as err:  # paused (hard): not running
            game.soft_pause()
        assert err.value.detail == "对局没有在运行。"
        assert game.soft is None and game.session["soft_pause"] is None
        assert not [e for e in events(bus, "agent.status") if e["data"]["state"] == "held"]

    run(scenario)


# ---------------------------------------------------------------------------------- the two phases
def test_draining_counts_the_answers_in_flight_down_then_everybody_is_held_and_the_clock_stands_still() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        task = start_with(game, "N3")
        await night_of_the_wolves(game)
        thinking = sorted(a for a, s in states(game, skip="N3").items() if s == "thinking")
        awake_not_thinking = sorted(a for a, s in states(game, skip="N3").items() if s == "acting")
        asleep = sorted(a for a, s in states(game, skip="N3").items() if s == "sleeping")
        assert thinking and asleep, "the situation has calls in flight and agents that sleep"
        frozen = game.clock
        mark = bus.latest_id

        game.soft_pause()
        first = updates(bus, mark)[0]
        # the click: the clock stops now, the game still "runs"; the count is the number of calls in flight
        assert first["status"] == "running" and first["clock_running"] is False
        assert first["soft_pause"] == {"state": "draining", "in_flight": len(thinking)}
        assert first["clock"] == pytest.approx(frozen, abs=0.06)
        assert game.running() is False and game.clock_running() is False
        assert game.session["soft_pause"] == first["soft_pause"] and game.session["clock_running"] is False
        # an agent that was acting has reached the gate at once, the ones that think are still on their way
        assert all(node(game, a)["state"] == "held" for a in awake_not_thinking)
        assert all(node(game, a)["state"] == "thinking" for a in thinking)

        await until(lambda: game.soft == {"state": "held", "in_flight": 0}, limit=30.0)
        sequence = [u["soft_pause"] for u in updates(bus, mark)]
        assert [s["state"] for s in sequence] == ["draining"] * len(thinking) + ["held"]
        assert [s["in_flight"] for s in sequence] == list(range(len(thinking), 0, -1)) + [0], "the count falls by one per answer"
        assert all(u["clock_running"] is False and u["clock"] == first["clock"] for u in updates(bus, mark)), "the frozen clock travels"
        # everybody that was awake is held, the sleepers sleep on, the person's card says human
        after = states(game)
        assert all(after[a] == "held" for a in thinking + awake_not_thinking)
        assert all(after[a] == "sleeping" for a in asleep)
        assert after["N3"] == "human"
        # the held agents have no wake-up time, and nothing but "held" was said (the answers came back in order)
        held_events = [e["data"] for e in events(bus, "agent.status", after=mark)]
        assert {d["state"] for d in held_events} == {"held"} and all(d["until"] is None for d in held_events)
        assert sorted(d["id"] for d in held_events) == sorted(thinking + awake_not_thinking)

        # nothing moves: a minute of waiting changes neither the clock nor the game
        seen = len(bus.ring)
        before = (game.clock, len(game.session["messages"]), len(game.session["requests"][0]["answers"]))
        await asyncio.sleep(60.0)
        assert (game.clock, len(game.session["messages"]), len(game.session["requests"][0]["answers"])) == before
        assert len(bus.ring) == seen, "no event at all"
        assert game.session["status"] == "running"
        # a second click, or the start button, change nothing
        game.soft_pause()
        game.start()
        assert len(bus.ring) == seen and game.soft == {"state": "held", "in_flight": 0}

        for ev in bus.ring:
            assert mock.validate_event(ev) == [], ev
        assert mock.validate_session(game.snapshot_payload()) == []
        await stop(game)
        assert task.done()

    run(scenario)


def test_with_nothing_in_flight_the_pause_is_held_at_once() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        game.take_over("N3")
        game.gate.set()
        game.set_status("running", reason="started")  # (everybody sleeps: nothing is on its way)
        mark = bus.latest_id
        game.soft_pause()
        sequence = [u["soft_pause"] for u in updates(bus, mark)]
        assert sequence == [{"state": "held", "in_flight": 0}]
        assert game.soft_task is None
        assert not events(bus, "agent.status", after=mark)

    run(scenario)


def test_the_person_acts_during_the_pause_the_script_stays_still_and_continuing_lets_it_go_on() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        task = start_with(game, "N3")
        await night_of_the_wolves(game)
        thinking = sorted(a for a, s in states(game, skip="N3").items() if s == "thinking")
        game.soft_pause()
        await until(lambda: game.soft == {"state": "held", "in_flight": 0}, limit=30.0)
        mark = bus.latest_id
        clock = game.clock

        # the person still reads, writes and answers: the commit gate stays open
        request = game.session["requests"][0]
        assert request["status"] == "open" and "N3" in request["to"]
        taken = game.compute_takeover()
        assert [r["id"] for r in taken["requests"]] == [request["id"]]
        assert taken["channels"]["dm-N3"]["can_write"] is True
        rc = game.seat_speak("dm-N3", "我先想想，请主持人别催。", taken["seq"])
        assert rc["accepted"] is True
        rc = game.seat_respond(request["id"], "N5")
        assert rc["accepted"] is True and rc["data"]["replaced"] is False
        types = [e["type"] for e in events(bus, after=mark)]
        assert "message.committed" in types and "request.answer" in types
        assert {a["player_id"] for a in request["answers"]} == {"N3"}, "only the person: the agents wait"
        assert game.clock == clock and game.soft == {"state": "held", "in_flight": 0}
        # a message that wakes agents does not start them: they wait in front of the gate (held), whatever they were told
        game.seat_speak("dm-N3", "还在想。", game.compute_takeover()["seq"])
        game.commit("public", mock.MODERATOR, "（模拟）一条公共消息", "announce")
        game.stir({mock.MODERATOR}, 3)
        await asyncio.sleep(30.0)
        assert not {a for a, s in states(game, skip="N3").items() if s in ("thinking", "acting")}, "nobody starts a call in a soft pause"
        assert game.clock == clock

        # 继续: the held agents go back to what they were doing, the clock runs, the script goes on
        mark = bus.latest_id
        game.soft_resume()
        resumed = updates(bus, mark)[-1]
        assert resumed["soft_pause"] is None and resumed["clock_running"] is True and resumed["status"] == "running"
        assert resumed["clock"] == pytest.approx(clock, abs=0.001)
        assert game.soft is None and game.running() and game.clock_running()
        back = {e["data"]["id"]: e["data"]["state"] for e in events(bus, "agent.status", after=mark)}
        assert set(thinking) <= set(back) and all(back[a] == "thinking" for a in thinking)
        assert "held" not in back.values()
        await until(lambda: len({a["player_id"] for a in request["answers"]}) >= 3, limit=30.0)  # the wolves answer now
        assert game.clock > clock
        assert not any(s == "held" for s in states(game).values())

        for ev in bus.ring:
            assert mock.validate_event(ev) == [], ev
        await stop(game)

    run(scenario)


def test_continuing_while_still_draining_cancels_the_pause_and_no_late_answer_comes() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        task = start_with(game, "N3")
        await night_of_the_wolves(game)
        game.soft_pause()
        assert game.soft["state"] == "draining"
        mark = bus.latest_id
        game.soft_resume()
        assert game.soft is None and game.soft_task is None
        assert updates(bus, mark)[-1]["soft_pause"] is None
        await asyncio.sleep(5.0)
        assert not [u for u in updates(bus, mark) if u["soft_pause"] is not None], "the drain task is gone: no 'held' comes later"
        assert not any(s == "held" for s in states(game).values())
        await stop(game)

    run(scenario)


# ---------------------------------------------------------------------------------- what clears it
def test_a_hard_pause_clears_the_soft_pause_the_start_button_resumes_normally() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        task = start_with(game, "N3")
        await night_of_the_wolves(game)
        game.soft_pause()
        await until(lambda: game.soft == {"state": "held", "in_flight": 0}, limit=30.0)
        clock = game.clock
        mark = bus.latest_id
        game.pause()  # the top bar's 暂停 in a soft pause = a hard pause (G7)
        assert game.soft is None and game.session["status"] == "paused"
        last = updates(bus, mark)[-1]
        assert (last["status"], last["soft_pause"], last["clock_running"]) == ("paused", None, False)
        assert {s for a, s in states(game, skip="N3").items()} == {"sleeping"}, "the held agents sleep, like in every pause"
        assert not game.running()
        # a hard pause can be given back (the seat), and the game goes on the old way
        game.start()
        assert game.session["status"] == "running" and game.soft is None and game.clock_running()
        await asyncio.sleep(3.0)
        assert game.clock > clock
        # a soft pause again, then 'start' is a no-op while it lasts
        game.soft_pause()
        seen = len(bus.ring)
        game.start()
        assert len(bus.ring) == seen and game.soft is not None
        await stop(game)

    run(scenario)


def test_the_end_of_the_game_and_a_new_game_clear_the_soft_pause() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        start_with(game, "N3")
        await night_of_the_wolves(game)
        game.soft_pause()
        await game.jump_to_end()
        assert game.soft is None and game.soft_task is None
        final = game.snapshot_payload()
        assert (final["status"], final["soft_pause"], final["clock_running"]) == ("finished", None, False)
        assert mock.validate_session(final) == []
        # a soft pause in a game that is over is refused
        with pytest.raises(mock.TakeoverError) as err:
            game.soft_pause()
        assert err.value.detail == "对局没有在运行。"

        bus, other = make_game()
        start_with(other, "N3")
        await night_of_the_wolves(other)
        other.soft_pause()
        await other.reset()
        assert other.soft is None and other.soft_task is None
        fresh = other.snapshot_payload()
        assert (fresh["status"], fresh["soft_pause"], fresh["clock_running"]) == ("idle", None, False)
        assert fresh["takeover"] == {"seat": None, "reshuffle_available": True}

    run(scenario)


def test_the_budget_pause_is_a_hard_pause_too() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        game.budget_limits.update({"max_tokens": 1, "force_stop": True})
        start_with(game, "N3")
        await night_of_the_wolves(game)
        game.soft_pause()
        game.set_budget(max_tokens=1, max_cost_usd=None, force_stop=True)  # the limit is reached: pause(reason="budget")
        assert game.session["status"] == "paused" and game.session["pause_reason"] == "budget"
        assert game.soft is None and game.session["soft_pause"] is None
        await stop(game)

    run(scenario)


# ---------------------------------------------------------------------------------- the public floor without a deadline
def test_the_persons_turn_in_a_public_channel_has_no_deadline_and_the_script_waits_for_it() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        game.take_over("N4")
        game.gate.set()
        game.set_status("running", reason="started")
        game.create_channel("public", "公共频道", mock.EVERYONE, ctype="public")
        assert game.channel("public")["type"] == "public"
        task = asyncio.ensure_future(
            game.run_queue("public", ["N2", "N4", "N6"], {"N2": ("先说一句。", 0.5), "N6": ("再说一句。", 0.5)})
        )
        await until(lambda: game.token is not None)

        floors = [e["data"]["floor"] for e in events(bus, "floor.changed") if e["data"]["channel_id"] == "public"]
        assert [(f["active"], f["untimed"], f["seconds_left"]) for f in floors[:2]] == [("N2", False, 90.0), ("N4", True, None)]
        for f in floors:
            assert mock.validate_floor(f) == []
        # the seat's own state: no countdown, no deadline
        mine = game.compute_takeover()["channels"]["public"]
        assert mine["floor"]["mine"] is True and mine["floor"]["untimed"] is True
        assert (mine["floor"]["seconds_left"], mine["floor"]["deadline_clock"]) == (None, None)
        assert mine["can_write"] and mine["can_pass"] and mine["floor"]["messages_left"] == mock.TOKEN_MAX_MESSAGES
        assert mock.validate_takeover(game.compute_takeover()) == []
        # the others still see a timed floor on the agents' turns (it is the person's turn that has none)

        # ten minutes of silence: nobody takes the floor from the person
        await asyncio.sleep(600.0)
        assert not task.done() and game.token is not None
        assert game.channel("public")["floor"]["active"] == "N4" and game.channel("public")["floor"]["untimed"] is True
        assert game.compute_takeover()["channels"]["public"]["can_write"] is True

        # the person speaks - the limits (messages, characters) are the engine's, and ending the turn goes on
        assert game.seat_speak("public", "我想了很久。", None)["accepted"]
        assert game.seat_pass("public")["accepted"]
        await asyncio.wait_for(task, timeout=60.0)
        assert [m["from"] for m in game.session["messages"] if m["channel_id"] == "public"] == ["N2", "N4", "N6"]
        after = [e["data"]["floor"] for e in events(bus, "floor.changed") if e["data"]["channel_id"] == "public"]
        assert after[-2]["active"] == "N6" and after[-2]["untimed"] is False and after[-2]["seconds_left"] == 90.0
        assert after[-1]["untimed"] is False  # (the queue is over)

    run(scenario)


def test_a_turn_in_any_other_channel_still_runs_out_and_the_agents_turns_are_timed() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        game.take_over("N4")
        game.gate.set()
        game.set_status("running", reason="started")
        game.create_channel("circle", "圆桌", mock.EVERYONE)  # an ordinary (group) channel
        assert game.channel("circle")["type"] == "group"
        task = asyncio.ensure_future(game.run_queue("circle", ["N4", "N6"], {"N6": ("轮到我了。", 0.5)}))
        await until(lambda: game.token is not None)
        mine = game.compute_takeover()["channels"]["circle"]["floor"]
        assert mine["untimed"] is False and mine["seconds_left"] is not None and mine["deadline_clock"] is not None
        floors = [e["data"]["floor"] for e in events(bus, "floor.changed")]
        assert floors[0]["active"] == "N4" and floors[0]["untimed"] is False and floors[0]["seconds_left"] == 90.0
        await asyncio.wait_for(task, timeout=600.0)  # a silent person: the time runs out after 90 s of game time
        assert [m["from"] for m in game.session["messages"] if m["channel_id"] == "circle"] == ["N6"]

    run(scenario)


def test_the_timed_turn_helper_gives_the_seat_a_countdown_that_a_soft_pause_stops() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        with pytest.raises(mock.TakeoverError) as err:
            game.timed_turn_allowed()
        assert (err.value.status, err.value.detail) == (409, "请先接管一个座位。")
        game.take_over("N4")
        with pytest.raises(mock.TakeoverError) as err:
            game.timed_turn_allowed()
        assert err.value.detail == "对局没有在运行。"
        game.gate.set()
        game.set_status("running", reason="started")
        game.timed_turn_allowed()
        game.spawn_bg(game.timed_turn(45.0))
        await until(lambda: game.token is not None)
        with pytest.raises(mock.TakeoverError) as err:  # one turn at a time
            game.timed_turn_allowed()
        assert err.value.detail == "你现在已经持有一个发言权。"

        channel = game.channel(game.ROUNDTABLE)
        assert channel["type"] == "group" and channel["mode"] == "queue", "an ordinary channel: its turns are timed"
        mine = game.compute_takeover()["channels"][game.ROUNDTABLE]
        floor = mine["floor"]
        assert (floor["mine"], floor["untimed"]) == (True, False)
        assert floor["seconds_left"] == pytest.approx(45.0, abs=1.0) and floor["deadline_clock"] == pytest.approx(game.clock + floor["seconds_left"], abs=0.2)
        assert mine["can_write"] and mine["can_pass"] and mine["floor"]["messages_left"] == mock.TOKEN_MAX_MESSAGES
        granted = [e["data"]["floor"] for e in events(bus, "floor.changed") if e["data"]["channel_id"] == game.ROUNDTABLE][0]
        assert (granted["active"], granted["untimed"], granted["seconds_left"]) == ("N4", False, 45.0)
        assert game.compute_takeover()["attention"] == [game.ROUNDTABLE]

        # a soft pause stops the countdown: two minutes later the deadline is the same and the turn is still the person's
        game.soft_pause()
        await until(lambda: game.soft == {"state": "held", "in_flight": 0}, limit=30.0)
        deadline, left = floor["deadline_clock"], game.compute_takeover()["channels"][game.ROUNDTABLE]["floor"]["seconds_left"]
        await asyncio.sleep(120.0)
        again = game.compute_takeover()["channels"][game.ROUNDTABLE]["floor"]
        assert (again["deadline_clock"], again["seconds_left"], again["mine"]) == (deadline, left, True)
        assert game.token is not None
        # the person can still speak (the world is open), then 继续: the rest of the time runs out
        assert game.seat_speak(game.ROUNDTABLE, "我在圆桌上说一句。", None)["accepted"]
        game.soft_resume()
        await until(lambda: game.token is None, limit=100.0)
        assert game.channel(game.ROUNDTABLE)["mode"] == "closed" and game.channel(game.ROUNDTABLE)["floor"]["kind"] == "none"
        for ev in bus.ring:
            assert mock.validate_event(ev) == [], ev
        await stop(game)

    run(scenario)


def test_the_free_discussion_and_the_requests_are_never_untimed() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        game.take_over("N3")
        game.gate.set()
        game.set_status("running", reason="started")
        game.create_channel("public", "公共频道", mock.EVERYONE, ctype="public")
        game.open_free("public")
        st = game.compute_takeover()["channels"]["public"]["floor"]
        assert st["kind"] == "free" and st["untimed"] is False
        rid = game.open_request("投票", ["N1", "N2"], ["N3", "N4"], sealed=False, seconds=60.0)
        request = game.compute_takeover()["requests"][0]
        assert request["id"] == rid and request["deadline_clock"] is not None and request["seconds_left"] is not None
        assert "untimed" not in request
        assert game.channel("public")["floor"]["untimed"] is False
        for ev in bus.ring:
            assert mock.validate_event(ev) == [], ev

    run(scenario)


def test_the_validator_knows_the_new_shapes_and_rejects_broken_ones() -> None:
    ok_floor = {"kind": "queue", "active": "N4", "paused": False, "seconds_left": None, "untimed": True, "queue": [], "free": None}
    assert mock.validate_floor(ok_floor) == []
    assert mock.validate_floor({**ok_floor, "untimed": False, "seconds_left": 12.5}) == []
    assert mock.validate_floor({k: v for k, v in ok_floor.items() if k != "untimed"}) == [], "an older server has no such key"
    assert mock.validate_floor({**ok_floor, "untimed": "yes"}) != []
    assert mock.validate_floor({**ok_floor, "seconds_left": 30.0}) != [], "an untimed floor has no countdown"

    assert mock.validate_soft_pause(None) == []
    assert mock.validate_soft_pause({"state": "draining", "in_flight": 2}) == []
    assert mock.validate_soft_pause({"state": "held", "in_flight": 0}) == []
    for bad in ({"state": "held", "in_flight": 1}, {"state": "paused", "in_flight": 0}, {"state": "draining"}, {"state": "draining", "in_flight": -1},
                {"state": "draining", "in_flight": True}, {"state": "draining", "in_flight": 1, "extra": 1}, "held", []):
        assert mock.validate_soft_pause(bad) != [], bad

    update = {"status": "running", "soft_pause": {"state": "held", "in_flight": 0}, "clock_running": False, "clock": 12.5}
    assert mock.validate_event({"id": 1, "type": "session.updated", "data": update}) == []
    assert mock.validate_event({"id": 1, "type": "session.updated", "data": {"status": "running", "soft_pause": None}}) != [], "they travel together"
    assert mock.validate_event({"id": 1, "type": "session.updated", "data": {"status": "running", "clock_running": "no", "soft_pause": None}}) != []
    assert mock.validate_event({"id": 1, "type": "agent.status", "data": {"id": "N4", "state": "held", "until": None}}) == []
    assert mock.validate_event({"id": 1, "type": "agent.status", "data": {"id": "N4", "state": "held", "until": 5}}) != [], "only a sleeping agent has a wake-up time"
    assert mock.validate_event({"id": 1, "type": "agent.status", "data": {"id": "N4", "state": "paused", "until": None}}) != []

    s = mock.MockGame(mock.EventBus(), speed=1.0, seed=7, budget=mock.default_budget(None, False)).snapshot_payload()
    assert (s["soft_pause"], s["clock_running"]) == (None, False) and mock.validate_session(s) == []
    assert mock.validate_session({**s, "soft_pause": {"state": "held", "in_flight": 0}, "clock_running": False}) != [], "idle: no soft pause"
    running = {**s, "status": "running"}
    assert mock.validate_session({**running, "soft_pause": {"state": "held", "in_flight": 0}, "clock_running": False}) == []
    assert mock.validate_session({**running, "soft_pause": {"state": "held", "in_flight": 0}, "clock_running": True}) != []
    assert mock.validate_session({**running, "soft_pause": None, "clock_running": False}) != [], "a running game has a running clock"
    assert mock.validate_session({**s, "clock_running": True}) != [], "idle: the clock does not run"


# ---------------------------------------------------------------------------------- the real client store
async def soft_paused_game(seat: str) -> dict:
    bus, game = make_game()
    initial = game.snapshot_payload()
    task = start_with(game, seat)
    await night_of_the_wolves(game, seat)
    game.soft_pause()  # the person thinks ...
    await until(lambda: game.soft == {"state": "held", "in_flight": 0}, limit=30.0)
    request = game.session["requests"][0]
    if seat in request["to"]:
        game.seat_respond(request["id"], "N5")  # ... and answers meanwhile
    await asyncio.sleep(20.0)
    game.soft_resume()
    bot_task = asyncio.ensure_future(person(game, seat))
    await task
    await game.drain_bg()
    bot_task.cancel()
    return {"initial": initial, "events": list(bus.ring), "final": game.snapshot_payload()}


async def person(game, seat: str) -> None:
    """A minimal person: answers what waits, speaks when the floor is theirs."""
    spoke_free = False
    try:
        while game.session["status"] != "finished" and game.takeover_seat == seat:
            await game.wait(0.2)
            if game.session["status"] != "running":
                continue
            t = game.compute_takeover()
            for r in t["requests"]:
                if r["answered"]:
                    continue
                req = next(x for x in game.session["requests"] if x["id"] == r["id"])
                value = req["options"][0]["id"] if r["kind"] == "choice" else "我是真人。"
                game.seat_respond(r["id"], value)
            for cid, st in t["channels"].items():
                floor = st["floor"]
                if floor and floor["mine"] and st["can_write"]:
                    game.seat_speak(cid, f"（真人）轮到我了：{cid}", t["seq"])
                    game.seat_pass(cid)
                elif cid == "public" and floor and floor["kind"] == "free" and st["can_write"] and not spoke_free:
                    spoke_free = bool(game.seat_speak(cid, "（真人）自由讨论里说一句。", t["seq"])["accepted"])
    except asyncio.CancelledError:
        pass


@pytest.mark.skipif(NODE is None, reason="node executable not found")
@pytest.mark.parametrize("seat", ["N3", "N5"])
def test_the_client_store_reduces_the_log_of_a_game_with_a_soft_pause_to_the_servers_session(seat: str, tmp_path: Path) -> None:
    out = run(lambda: soft_paused_game(seat))
    types = [e["type"] for e in out["events"]]
    assert any(e["type"] == "agent.status" and e["data"]["state"] == "held" for e in out["events"]), "the pause showed"
    updates_ = [e["data"] for e in out["events"] if e["type"] == "session.updated"]
    assert any(u["soft_pause"] and u["soft_pause"]["state"] == "draining" for u in updates_)
    assert any(u["soft_pause"] and u["soft_pause"]["state"] == "held" for u in updates_)
    assert all(("soft_pause" in u) and ("clock_running" in u) and ("clock" in u) for u in updates_), "every session.updated carries the three"
    assert out["final"]["status"] == "finished" and out["final"]["soft_pause"] is None and out["final"]["clock_running"] is False
    assert "session.updated" in types
    for ev in out["events"]:
        assert mock.validate_event(ev) == [], ev
    log = tmp_path / "replay.json"
    log.write_text(json.dumps({**out, "duration": 0}, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [NODE, str(ROOT / "tests" / "frontend" / "replay_check.js"), str(log), "--human"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "replay ok" in result.stdout


# ---------------------------------------------------------------------------------- HTTP
def _client(speed: float = 6.0):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app = mock.create_app(speed=speed, budget_tokens=0)  # (no demo budget: it would pause a fast game)
    return app, TestClient(app)


def _wait_for(predicate, timeout: float = 15.0, step: float = 0.03):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out")


def test_http_soft_pause_endpoints_answer_with_the_documented_statuses_and_sentences() -> None:
    app, c = _client()
    with c as client:
        refused = client.post("/api/control/soft-pause")
        assert refused.status_code == 409 and refused.json()["detail"] == "对局没有在运行。"
        none = client.post("/api/control/soft-resume")
        assert none.status_code == 409 and none.json()["detail"] == "没有处于软暂停。"
        assert client.post("/api/takeover", json={"seat": "N3"}).status_code == 200
        assert client.post("/api/control/soft-pause").json()["detail"] == "对局没有在运行。"  # a seat, but not running

        assert client.post("/api/control/start").json()["session"]["status"] == "running"
        _wait_for(lambda: any(p["state"] == "thinking" for p in client.get("/api/state").json()["session"]["participants"] if p["id"] != "N3"))
        paused = client.post("/api/control/soft-pause")
        assert paused.status_code == 200 and paused.json()["ok"] is True
        session = paused.json()["session"]
        assert mock.validate_session(session) == []
        assert session["status"] == "running" and session["clock_running"] is False
        assert session["soft_pause"]["state"] in {"draining", "held"}
        # again: nothing changes (200) - and the start button is a no-op while it lasts
        assert client.post("/api/control/soft-pause").status_code == 200
        assert client.post("/api/control/start").json()["session"]["soft_pause"] is not None
        held = _wait_for(lambda: (s := client.get("/api/state").json()["session"])["soft_pause"] == {"state": "held", "in_flight": 0} and s)
        assert held["clock_running"] is False and held["status"] == "running"
        assert {p["state"] for p in held["participants"] if p["id"] != "N3"} <= {"held", "sleeping"}
        clock = held["clock"]
        time.sleep(0.4)
        assert client.get("/api/state").json()["session"]["clock"] == clock, "the clock stands still"
        # the person can still act (the seat endpoints work while the game 'runs')
        spoke = client.post("/api/seat/speak", json={"channel_id": "dm-N3", "content": "我在想。"})
        assert spoke.status_code == 200 and spoke.json()["accepted"] is True

        resumed = client.post("/api/control/soft-resume").json()["session"]
        assert resumed["soft_pause"] is None and resumed["clock_running"] is True and mock.validate_session(resumed) == []
        again = client.post("/api/control/soft-resume")
        assert again.status_code == 409 and again.json()["detail"] == "没有处于软暂停。"
        # the top bar's 暂停 in a soft pause is a hard pause
        assert client.post("/api/control/soft-pause").status_code == 200
        hard = client.post("/api/control/pause").json()["session"]
        assert (hard["status"], hard["soft_pause"], hard["clock_running"]) == ("paused", None, False)
        assert client.post("/api/control/soft-resume").status_code == 409


def test_http_the_timed_turn_helper_answers_with_the_documented_statuses() -> None:
    app, c = _client()
    with c as client:
        refused = client.post("/api/mock/timed-turn", json={"seconds": 30})
        assert refused.status_code == 409 and refused.json()["detail"] == "请先接管一个座位。"
        assert client.post("/api/takeover", json={"seat": "N3"}).status_code == 200
        assert client.post("/api/mock/timed-turn", json={"seconds": 30}).json()["detail"] == "对局没有在运行。"
        assert client.post("/api/control/start").json()["session"]["status"] == "running"
        assert client.post("/api/mock/timed-turn", json={"seconds": 1}).status_code == 422
        assert client.post("/api/mock/timed-turn", json={"seconds": "x"}).status_code == 400
        ok = client.post("/api/mock/timed-turn", json={"seconds": 120})
        assert ok.status_code == 200 and ok.json() == {"ok": True, "channel_id": "roundtable", "seconds": 120.0}
        taken = _wait_for(lambda: (t := client.get("/api/state").json()["session"]["takeover"])["channels"].get("roundtable", {}).get("floor") and t)
        floor = taken["channels"]["roundtable"]["floor"]
        assert (floor["mine"], floor["untimed"]) == (True, False) and floor["seconds_left"] <= 120.0 and floor["deadline_clock"] is not None
        assert mock.validate_takeover(taken) == []
        assert client.post("/api/mock/timed-turn", json={"seconds": 30}).status_code == 409  # one turn at a time
        spoke = client.post("/api/seat/speak", json={"channel_id": "roundtable", "content": "圆桌上的一句话。"}).json()
        assert spoke["accepted"] is True
        assert client.post("/api/seat/pass", json={"channel_id": "roundtable"}).json()["accepted"] is True


def test_http_a_running_game_without_a_played_seat_cannot_be_soft_paused() -> None:
    app, c = _client()
    with c as client:
        assert client.post("/api/control/start").json()["session"]["status"] == "running"
        refused = client.post("/api/control/soft-pause")
        assert refused.status_code == 409 and refused.json()["detail"] == "只有接管座位时才能软暂停。"
        state = client.get("/api/state").json()["session"]
        assert state["soft_pause"] is None and state["clock_running"] is True
        assert client.post("/api/control/pause").json()["session"]["clock_running"] is False
