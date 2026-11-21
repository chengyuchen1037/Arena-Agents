"""The soft pause and the untimed public floor in the real orchestrator (v0.13.1, G1-G15, contract section 12.2).

The orchestrator under test is the real :class:`GameOrchestrator` (real world, runtime, brain) with scripted agents and the
4-player ``minimal_test`` scenario (``tests/test_takeover_runtime.py``'s ``Table``); the model of an agent can be held back
to make a call "in flight" at the moment of the click.  The person is a script that makes the calls the HTTP layer makes.
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable
from typing import Any

import pytest

import app.game.orchestrator as orchestrator_module
import game_fakes
from app.game.orchestrator import GameOrchestrator, TakeoverError
from app.game.softpause_runtime import SoftPauseError
from app.runtime.clock import GameClock
from app.runtime.llm_gate import GatedEngineLlm
from app.world.types import MODERATOR_ID, Envelope, Intent
from game_fakes import PLAYERS, real_orch
from test_takeover_runtime import OPEN_PUBLIC, RecordingLlm, Table, passive, settle, talk_once, wait_for
from v12.bots import Call, tool_response

IDS = [p["id"] for p in PLAYERS]  # p01 .. p04
HELD = {"state": "held", "in_flight": 0}


def async_test(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Like ``game_fakes.async_test`` with a hard timeout (a hung soft pause must fail, not hang the suite)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 60))

    return wrapper


class HoldingLlm(RecordingLlm):
    """A scripted model whose answers can be held back: the call is logged and decided at once (like a request that was
    sent), the answer is returned when ``holds[agent_id]`` is set - so the call is *in flight* for as long as the test likes.
    A call that is cancelled while it waits is noted in ``cancelled``."""

    def __init__(
        self,
        world: Any,
        policies: dict[str, Any],
        log: dict[str, list[list[dict[str, Any]]]],
        holds: dict[str, asyncio.Event],
        cancelled: list[str] | None = None,
    ):
        super().__init__(world, policies, log)
        self.holds = holds
        self.cancelled = cancelled if cancelled is not None else []

    async def complete(self, agent_id, messages, tools, *, max_tokens=None):  # type: ignore[no-untyped-def]
        response = await super().complete(agent_id, messages, tools, max_tokens=max_tokens)
        gate = self.holds.get(agent_id)
        if gate is not None:
            try:
                await gate.wait()
            except asyncio.CancelledError:
                self.cancelled.append(agent_id)
                raise
        return response


class SoftTable(Table):
    """A ``Table`` whose model can hold answers back (``holds``); ``cancelled`` lists the calls that were cancelled."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, agents: Callable[[Any, str], list[Call]] = passive, **kw: Any) -> None:
        super().__init__(monkeypatch, agents=agents, **kw)
        self.holds: dict[str, asyncio.Event] = {}
        self.cancelled: list[str] = []
        policies = {MODERATOR_ID: kw.get("moderator") or self.moderator, **{i: agents for i in self.ids}}
        self.orch.llm_port_factory = lambda o: HoldingLlm(o.world, policies, self.log, self.holds, self.cancelled)

    def hold(self, agent_id: str) -> asyncio.Event:
        self.holds[agent_id] = event = asyncio.Event()
        return event


class FakeTime:
    """A stand-in for ``time.monotonic`` that moves only when the test moves it (the game clock runs on it)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def fake_game_clock(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    """From now on every game clock of the orchestrator is driven by the returned :class:`FakeTime`: a game second passes when
    the test says so (and only while the clock runs), no matter how long the machine takes."""
    fake = FakeTime()
    monkeypatch.setattr(orchestrator_module, "GameClock", functools.partial(GameClock, time_source=fake))
    return fake


async def world_ticks(orch: GameOrchestrator, n: int = 4) -> None:
    """Wait until the supervisor has ticked the world ``n`` more times.  It polls in real time; this waits for those polls
    (events), not for a duration, so it is as slow as the machine is and no slower."""
    count = 0
    original = orch.world.tick

    def counting() -> None:
        nonlocal count
        count += 1
        original()

    orch.world.tick = counting  # type: ignore[method-assign]
    try:
        await wait_for(lambda: count >= n, timeout=30)
    finally:
        del orch.world.tick  # (the class method is visible again)


def sessions_updated(orch: GameOrchestrator) -> list[dict[str, Any]]:
    return [e.data for e in orch.bus._ring if e.type == "session.updated"]


def ring_index(orch: GameOrchestrator, predicate: Callable[[Any], bool]) -> int:
    return max(i for i, e in enumerate(orch.bus._ring) if predicate(e))


async def held_game(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> SoftTable:
    """A started game with seat p02 played by a person, soft-paused and fully held."""
    table = SoftTable(monkeypatch, **kw)
    await table.orch.takeover("p02")
    await table.orch.start_or_resume()
    await settle(table.orch)
    await table.orch.soft_pause()
    await wait_for(lambda: table.orch.soft_pause_payload() == HELD)
    return table


QUEUE_LIMITS = {"max_messages": 3, "duration_s": 30}
OPEN_QUEUE: list[Call] = [
    *OPEN_PUBLIC,
    ("set_floor_queue", {"channel_id": "public", "order": ["p02", "p01"], "limits": QUEUE_LIMITS}),
]


# ==================================================================================== the control rules


@async_test
async def test_the_soft_pause_needs_a_running_game_and_a_played_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    table = SoftTable(monkeypatch)
    orch = table.orch
    with pytest.raises(SoftPauseError) as err:  # nothing runs yet
        await orch.soft_pause()
    assert err.value.status == 409 and "没有在运行" in str(err.value)
    await orch.takeover("p02")
    with pytest.raises(SoftPauseError) as err:  # a seat is played but the game has not started
        await orch.soft_pause()
    assert "没有在运行" in str(err.value)
    with pytest.raises(SoftPauseError) as err:
        await orch.soft_resume()
    assert err.value.status == 409 and "没有处于软暂停" in str(err.value)
    await orch.release_takeover()

    await orch.start_or_resume()  # running, but nobody plays a seat: there is nobody to give time to
    with pytest.raises(SoftPauseError) as err:
        await orch.soft_pause()
    assert err.value.status == 409 and "只有接管座位时才能软暂停" in str(err.value)
    assert orch.soft_pause_payload() is None
    await orch.shutdown()


@async_test
async def test_the_game_stays_running_and_the_soft_pause_is_idempotent_and_can_be_left(monkeypatch: pytest.MonkeyPatch) -> None:
    table = SoftTable(monkeypatch)
    orch = table.orch
    await orch.takeover("p02")
    await orch.start_or_resume()
    await settle(orch)
    assert orch.clock_running() is True and orch.soft_pause_payload() is None
    await orch.soft_pause()
    await wait_for(lambda: orch.soft_pause_payload() == HELD)
    assert orch.session.status == "running" and orch.world.running and orch.clock.paused  # not a new status
    assert orch.clock_running() is False
    frozen = orch.clock.now()
    again = await orch.soft_pause()  # already soft-paused: nothing happens
    assert again is orch.session and orch.soft_pause_payload() == HELD and orch.clock.now() == frozen
    await orch.soft_resume()
    assert orch.soft_pause_payload() is None and orch.clock_running() is True and not orch.clock.paused
    with pytest.raises(SoftPauseError):
        await orch.soft_resume()
    await orch.shutdown()


@async_test
async def test_taking_over_and_giving_back_stay_refused_while_the_game_is_soft_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    table = await held_game(monkeypatch)
    orch = table.orch
    with pytest.raises(TakeoverError) as err:
        await orch.release_takeover()
    assert err.value.status == 409 and "暂停" in str(err.value)
    with pytest.raises(TakeoverError):
        await orch.takeover("p03")
    assert orch.takeover_seat == "p02"
    # starting is a no-op: it does not let the agents go (only "继续" of the takeover bar does)
    clock = orch.clock.now()
    assert await orch.start_or_resume() is orch.session
    assert orch.soft_pause_payload() == HELD and orch.clock.paused and orch.clock.now() == clock
    await orch.shutdown()


# ==================================================================================== the two phases


@async_test
async def test_a_soft_pause_lets_the_answer_in_flight_finish_then_holds_everybody_and_a_release_makes_one_call_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = SoftTable(monkeypatch)
    orch = table.orch
    answer = table.hold(MODERATOR_ID)
    table.moderator.plan(OPEN_QUEUE)  # what the moderator's first answer does
    await orch.takeover("p02")
    await orch.start_or_resume()
    await wait_for(lambda: orch._soft_gate is not None and orch._soft_gate.in_flight == 1)  # its first call is on its way

    await orch.soft_pause()
    assert orch.soft_pause_payload() == {"state": "draining", "in_flight": 1}
    frozen = orch.clock.now()
    click = sessions_updated(orch)[-1]
    assert click["soft_pause"] == {"state": "draining", "in_flight": 1} and click["clock_running"] is False
    assert click["status"] == "running" and click["clock"] == round(frozen, 3)  # the click's event carries the frozen clock
    public = orch.public_session()
    assert public["soft_pause"] == {"state": "draining", "in_flight": 1} and public["clock_running"] is False
    assert not orch.world.channel("public")  # the answer has not come back yet

    answer.set()  # ... it finishes after the click: written, its tools run, and then the moderator holds
    await wait_for(lambda: orch.soft_pause_payload() == HELD)
    assert orch.world.channel("public") is not None and orch.world.channel("public").group is not None  # the tools ran
    assert table.calls(MODERATOR_ID) == 1
    await wait_for(lambda: orch.agent_state.get(MODERATOR_ID) == "held")
    assert orch.public_session()["moderator"]["state"] == "held"
    # the clients heard it in this order: what the answer produced first, then "everybody is held"
    await wait_for(lambda: sessions_updated(orch)[-1]["soft_pause"] == HELD)
    updated = ring_index(orch, lambda e: e.type == "session.updated" and e.data.get("soft_pause") == HELD)
    created = ring_index(orch, lambda e: e.type == "channel.created" and e.data["channel"]["id"] == "public")
    assert created < updated
    assert orch.clock.now() == frozen and orch.session.status == "running"

    # everybody else was woken by the channel and holds in front of its first call; the person speaks meanwhile
    await wait_for(lambda: all(orch.agent_state.get(i) == "held" for i in ("p01", "p03", "p04")))
    assert all(table.calls(i) == 0 for i in ("p01", "p03", "p04"))
    spoken = orch.seat_speak("public", "我想了很久才说这句")
    assert spoken["accepted"] is True  # the person acts during a soft pause (G4)
    await asyncio.sleep(0.3)
    assert orch.clock.now() == frozen and table.calls(MODERATOR_ID) == 1 and table.calls("p01") == 0
    participants = {p["id"]: p["state"] for p in orch.public_session()["participants"]}
    assert participants["p02"] == "human" and participants["p01"] == "held"

    await orch.soft_resume()
    assert orch.soft_pause_payload() is None and orch.clock_running() is True and not orch.clock.paused
    await settle(orch)
    # one call each, whatever arrived meanwhile (the person's words included) was merged into it
    assert [table.calls(i) for i in ("p01", "p03", "p04")] == [1, 1, 1]
    assert "我想了很久才说这句" in json.dumps(table.log["p01"][0], ensure_ascii=False)
    assert table.calls(MODERATOR_ID) == 2
    assert all(orch.agent_state.get(i) == "sleeping" for i in (MODERATOR_ID, "p01", "p03", "p04"))
    assert sessions_updated(orch)[-1]["soft_pause"] is None and sessions_updated(orch)[-1]["clock_running"] is True
    await orch.shutdown()


@async_test
async def test_draining_counts_the_answers_down_and_changes_are_published(monkeypatch: pytest.MonkeyPatch) -> None:
    table = SoftTable(monkeypatch)
    orch = table.orch
    first, second = table.hold(MODERATOR_ID), table.hold("p01")
    await orch.takeover("p02")
    await orch.start_or_resume()
    await wait_for(lambda: orch._soft_gate.in_flight == 1)  # the moderator
    # wake p01 as well, so that two answers are on their way
    from app.world.types import Envelope

    orch._supervisor.deliver("p01", Envelope(kind="notice", priority="urgent", payload={"event": "test.poke"}))
    await wait_for(lambda: orch._soft_gate.in_flight == 2)
    await orch.soft_pause()
    assert orch.soft_pause_payload() == {"state": "draining", "in_flight": 2}

    first.set()
    await wait_for(lambda: orch.soft_pause_payload() == {"state": "draining", "in_flight": 1})
    await wait_for(lambda: sessions_updated(orch)[-1]["soft_pause"] == {"state": "draining", "in_flight": 1})
    second.set()
    await wait_for(lambda: orch.soft_pause_payload() == HELD)
    await wait_for(lambda: sessions_updated(orch)[-1]["soft_pause"] == HELD)
    phases: list[Any] = []
    for update in sessions_updated(orch):
        if update["soft_pause"] is not None and (not phases or phases[-1] != update["soft_pause"]):
            phases.append(update["soft_pause"])
    assert phases == [
        {"state": "draining", "in_flight": 2}, {"state": "draining", "in_flight": 1}, HELD,
    ]  # every change was published, in order
    await orch.shutdown()


@async_test
async def test_leaving_a_soft_pause_that_is_still_draining_lets_the_answers_through_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    table = SoftTable(monkeypatch)
    orch = table.orch
    answer = table.hold(MODERATOR_ID)
    table.moderator.plan(OPEN_QUEUE)
    await orch.takeover("p02")
    await orch.start_or_resume()
    await wait_for(lambda: orch._soft_gate.in_flight == 1)
    await orch.soft_pause()
    await orch.soft_resume()  # "继续" during the draining phase: the soft pause is simply off
    assert orch.soft_pause_payload() is None and orch.clock_running() is True
    answer.set()
    await settle(orch)
    assert orch.world.channel("public") is not None and orch.agent_state.get(MODERATOR_ID) == "sleeping"
    assert orch.soft_pause_payload() is None
    await orch.shutdown()


@async_test
async def test_the_release_in_the_very_turn_the_last_answer_comes_in_leaves_nobody_held(monkeypatch: pytest.MonkeyPatch) -> None:
    table = SoftTable(monkeypatch)
    orch = table.orch
    answer = table.hold(MODERATOR_ID)
    table.moderator.plan(OPEN_QUEUE)
    await orch.takeover("p02")
    await orch.start_or_resume()
    await wait_for(lambda: orch._soft_gate.in_flight == 1)
    await orch.soft_pause()
    assert orch.soft_pause_payload() == {"state": "draining", "in_flight": 1}
    answer.set()  # the answer is on its way back ...
    await orch.soft_resume()  # ... and "继续" is clicked before the loop has run anything (neither of them yields)
    await settle(orch)
    assert orch.soft_pause_payload() is None and orch.clock_running() is True and not orch.clock.paused
    assert orch.world.channel("public") is not None and orch.world.channel("public").group is not None  # the answer's tools ran
    assert orch.agent_state.get(MODERATOR_ID) == "sleeping" and orch._soft_gate.in_flight == 0 and orch._soft_gate.held == 0
    assert all(state in ("sleeping", "human") for state in orch.agent_state.values())  # nobody is left "held"
    last = sessions_updated(orch)[-1]
    assert last["soft_pause"] is None and last["clock_running"] is True
    await orch.shutdown()


@async_test
async def test_an_interrupt_during_the_draining_cancels_nothing_and_is_merged_after_the_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """G2: no call is cancelled.  The moderator's last batch (it was in flight when the person clicked) takes the floor away
    from an agent whose answer is on its way: the answer is NOT thrown away (its tokens are spent); the agent's tool meets
    the commit gate (refused: the floor is gone), the revoke is merged into its next message once the pause is over."""
    table = SoftTable(monkeypatch, agents=talk_once())
    orch = table.orch
    answer = table.hold("p01")  # p01's answer is held back: its call is in flight
    await orch.takeover("p02")
    await orch.start_or_resume()
    await table.run([*OPEN_PUBLIC], [("set_floor_queue", {"channel_id": "public", "order": ["p01", "p02"], "limits": QUEUE_LIMITS})])
    await wait_for(lambda: orch._soft_gate.in_flight == 1 and table.calls("p01") == 1)  # (p03 and p04 have answered already)
    await orch.soft_pause()
    assert orch.soft_pause_payload() == {"state": "draining", "in_flight": 1}

    # what the tool "close_floor" of the moderator's last batch does (the tools are synchronous: the commit gate)
    closed = orch.world.submit(Intent(actor_id=MODERATOR_ID, type="mod.close_floor", payload={"channel_id": "public"}, based_on_seq=orch.world.seq))
    assert closed.ok
    await settle(orch)
    assert table.cancelled == [] and table.calls("p01") == 1  # the interrupt cancelled nothing and rebuilt nothing
    assert orch.soft_pause_payload() == {"state": "draining", "in_flight": 1} and orch.agent_state.get("p01") == "thinking"

    answer.set()  # the answer comes in: it is written, its tool meets the commit gate ...
    await wait_for(lambda: orch.soft_pause_payload() == HELD)
    usage = orch.public_session()["usage_stats"]["by_actor"]["p01"]
    assert usage["calls"] == 1 and usage["tool_calls"] == 1 and usage["rejections"] == 1  # paid for, counted - not wasted
    assert table.cancelled == [] and table.calls("p01") == 1 and orch.agent_state.get("p01") == "held"

    await orch.soft_resume()  # ... and the model hears of the revoke, in the one message of its next call
    await wait_for(lambda: table.calls("p01") == 2)
    await settle(orch)
    second = table.log["p01"][1]
    result = json.dumps([m for m in second if m.get("role") == "tool"], ensure_ascii=False)
    assert "NO_FLOOR" in result or "CHANNEL_CLOSED" in result  # the commit gate refused what the answer said
    assert "发言权被收回" in json.dumps(second[-1], ensure_ascii=False)  # the interrupt, merged
    assert table.cancelled == [] and orch.soft_pause_payload() is None
    await orch.shutdown()


class WireClient:
    """Stands in for ``LLMClient.chat_stream`` behind the real port: every request blocks until ``release`` is set."""

    def __init__(self) -> None:
        self.started = 0
        self.cancelled = 0
        self.release = asyncio.Event()

    def reconfigure(self, cfg: Any) -> None:  # (``reload_config`` at a start keeps a client that can be reconfigured)
        return None

    async def chat_stream(self, messages: Any, **kwargs: Any) -> Any:
        self.started += 1
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return tool_response([("sleep", {})])


@async_test
async def test_with_a_concurrency_limit_only_the_calls_on_the_wire_are_in_flight_and_nothing_is_sent_after_the_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``runtime.llm_concurrency = 1``: four agents want a model, one request is on the wire, three wait for the slot.  The
    soft pause must count ONE answer on its way (not four) and must not let the three that wait send anything afterwards."""
    monkeypatch.setattr(
        game_fakes, "CFG", {**game_fakes.CFG, "runtime": {**game_fakes.CFG["runtime"], "llm_concurrency": 1}}
    )
    table = SoftTable(monkeypatch)
    orch = table.orch
    orch.llm_port_factory = None  # the real port (``GatedEngineLlm``) over a fake client
    client = WireClient()
    orch.llm = client
    await orch.takeover("p02")
    await orch.start_or_resume()  # the moderator is woken: its request is on the wire
    supervisor = orch._supervisor
    port = supervisor.agents[MODERATOR_ID].llm
    assert isinstance(port, GatedEngineLlm) and port.soft_gate is orch._soft_gate  # wired by ``_build_runtime``
    await wait_for(lambda: client.started == 1)
    for seat in ("p01", "p03", "p04"):  # three more agents want a model: they queue for the one slot
        supervisor.deliver(seat, Envelope(kind="notice", priority="urgent", payload={"event": "test.poke"}))
    await wait_for(lambda: port.gate.waiting == 3)
    assert orch._soft_gate.in_flight == 1  # not 4: a call that only waits for a slot is not an answer on its way

    await orch.soft_pause()
    assert orch.soft_pause_payload() == {"state": "draining", "in_flight": 1}
    client.release.set()  # the request on the wire finishes ...
    await wait_for(lambda: orch.soft_pause_payload() == HELD)
    await wait_for(lambda: all(orch.agent_state.get(seat) == "held" for seat in ("p01", "p03", "p04")))
    await settle(orch)
    assert client.started == 1  # ... and none of the three that waited was sent (before: they went out after the click)
    assert port.gate.in_flight == 0 and port.gate.waiting == 0 and orch._soft_gate.in_flight == 0

    await orch.soft_resume()  # the three ask again, after the pause, as usual
    await wait_for(lambda: client.started == 4, timeout=30)
    await settle(orch)
    assert orch._soft_gate.in_flight == 0 and client.cancelled == 0 and orch.soft_pause_payload() is None
    await orch.shutdown()


# ==================================================================================== the clock


@async_test
async def test_the_clock_stands_still_so_a_timed_floor_and_a_request_do_not_run_out_and_go_on_afterwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = fake_game_clock(monkeypatch)  # game seconds pass only when this test lets them: no timing luck involved
    table = SoftTable(monkeypatch)
    orch = table.orch
    await orch.takeover("p02")
    await orch.start_or_resume()
    await table.run(
        [*OPEN_PUBLIC, ("create_channel", {"channel_id": "team", "name": "小队", "members": ["p02", "p03"], "type": "group"})],
        [
            ("set_floor_queue", {"channel_id": "team", "order": ["p02"], "limits": {"max_messages": 3, "duration_s": 30}}),
            ("open_request", {"to": ["p02"], "kind": "choice", "question": "投谁？", "duration_s": 30,
                              "options": [{"id": "p01", "label": "一号"}, {"id": "p03", "label": "三号"}]}),
        ],
    )
    state = orch.takeover_payload()
    assert state["channels"]["team"]["can_write"] is True and state["requests"] and not state["requests"][0]["answered"]
    fake.advance(10)  # ten game seconds go by while the game runs: twenty are left
    await orch.soft_pause()
    await wait_for(lambda: orch.soft_pause_payload() == HELD)
    frozen = orch.clock.now()
    left = orch.takeover_payload()["channels"]["team"]["floor"]["seconds_left"]
    request_left = orch.takeover_payload()["requests"][0]["seconds_left"]
    assert left == 20.0 and request_left == 20.0

    fake.advance(1000)  # however long the person thinks (wall time), no game second passes ...
    await world_ticks(orch)  # ... although the world is looked at (ticked) all the time
    after = orch.takeover_payload()
    assert orch.clock.now() == frozen and orch.clock.paused
    assert after["channels"]["team"]["can_write"] is True and after["channels"]["team"]["floor"]["seconds_left"] == left
    assert after["requests"][0]["seconds_left"] == request_left and not orch.world.state.requests["r1"].answers
    assert orch.world.state.requests["r1"].status == "open"  # nobody was timed out, nothing was defaulted
    # the person answers during the pause (G4) - the request is theirs to answer
    assert orch.seat_respond("r1", "p03", "想好了")["accepted"] is True

    await orch.soft_resume()
    assert orch.clock_running() is True and orch.clock.now() == frozen  # the clock goes on from where it stopped
    fake.advance(19.5)  # nearly all of what was left, used up after the pause
    await world_ticks(orch)
    assert orch.takeover_payload()["channels"]["team"]["can_write"] is True  # not yet: the floor kept its time
    fake.advance(1.0)  # now it is over
    await wait_for(lambda: not orch.takeover_payload()["channels"]["team"]["can_write"], timeout=30)
    ended = [e for e in orch.world.ledger if e.type == "floor.token_ended" and e.channel_id == "team"]
    assert ended[-1].data["holder"] == "p02" and ended[-1].data["reason"] == "timeout"
    assert ended[-1].gt == pytest.approx(frozen + 20.5, abs=0.01)  # exactly when the time it had left was used up
    await orch.shutdown()


@async_test
async def test_a_hard_pause_after_a_soft_pause_freezes_the_clock_once_and_a_start_runs_it_again(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_game_clock(monkeypatch)
    table = await held_game(monkeypatch)
    orch = table.orch
    frozen = orch.clock.now()
    fake.advance(50)
    await orch.pause("user")
    assert orch.clock.paused and orch.clock.now() == frozen
    fake.advance(50)
    await orch.start_or_resume()
    assert not orch.clock.paused and orch.clock_running() is True and orch.clock.now() == frozen
    fake.advance(3)
    assert orch.clock.now() == pytest.approx(frozen + 3.0)  # the pauses (soft and hard) cost no game time at all
    await orch.shutdown()


# ==================================================================================== everything that stops the agents clears it


async def assert_cleared(orch: GameOrchestrator, status: str) -> None:
    assert orch.soft_pause_payload() is None and orch._soft_gate is None and orch.clock_running() is False
    assert orch.session.status == status and orch.clock.paused
    last = sessions_updated(orch)[-1]
    assert last["soft_pause"] is None and last["clock_running"] is False
    public = orch.public_session()
    assert public["soft_pause"] is None and public["clock_running"] is False


@async_test
async def test_a_hard_pause_clears_the_soft_pause_and_the_next_start_has_an_open_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    table = await held_game(monkeypatch)
    orch = table.orch
    assert orch.public_session()["soft_pause"] == HELD
    await orch.request_pause()  # the top bar's "暂停" during a soft pause is the hard pause
    await assert_cleared(orch, "paused")
    assert orch.session.pause_reason == "user"
    assert all(state in ("sleeping", "human") for state in orch.agent_state.values())  # nobody stays "held"

    await orch.start_or_resume()  # a new runtime: a new, open gate
    assert orch._soft_gate is not None and orch._soft_gate.closed is False and orch.soft_pause_payload() is None
    calls = table.calls(MODERATOR_ID)
    await table.poke()
    await wait_for(lambda: table.calls(MODERATOR_ID) > calls)  # agents call their models again
    await orch.shutdown()


@async_test
async def test_the_immediate_hard_pause_cancels_an_answer_that_never_comes_back_during_the_draining(monkeypatch: pytest.MonkeyPatch) -> None:
    table = SoftTable(monkeypatch)
    orch = table.orch
    table.hold(MODERATOR_ID)  # never released: a hung call
    await orch.takeover("p02")
    await orch.start_or_resume()
    await wait_for(lambda: orch._soft_gate.in_flight == 1)
    await orch.soft_pause()
    await asyncio.sleep(0.2)
    assert orch.soft_pause_payload() == {"state": "draining", "in_flight": 1}  # it just keeps waiting for the answer
    await orch.pause("user")  # "立即暂停": the in-flight call is cancelled, the soft pause is gone
    await assert_cleared(orch, "paused")
    assert orch.agent_state[MODERATOR_ID] == "sleeping" and orch._supervisor is None
    await orch.shutdown()
    await asyncio.sleep(0.05)
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()] == []


@async_test
async def test_force_pause_and_a_silent_halt_clear_it_too(monkeypatch: pytest.MonkeyPatch) -> None:
    table = await held_game(monkeypatch)
    orch = table.orch
    await orch.force_pause(silent=True)
    assert orch.soft_pause_payload() is None and orch._soft_gate is None and orch.clock.paused
    await orch.shutdown()


@async_test
async def test_the_end_of_the_game_clears_it_even_when_the_final_answer_arrives_during_the_draining(monkeypatch: pytest.MonkeyPatch) -> None:
    table = SoftTable(monkeypatch)
    orch = table.orch
    answer = table.hold(MODERATOR_ID)
    table.moderator.plan([("end_game", {"winner": "红队", "summary": "结束了"})])
    await orch.takeover("p02")
    await orch.start_or_resume()
    await wait_for(lambda: orch._soft_gate.in_flight == 1)
    await orch.soft_pause()
    answer.set()
    await wait_for(lambda: orch.session.status == "finished")
    await assert_cleared(orch, "finished")
    await orch.shutdown()


@async_test
async def test_hold_game_clears_it(monkeypatch: pytest.MonkeyPatch) -> None:
    table = SoftTable(monkeypatch)
    orch = table.orch
    answer = table.hold(MODERATOR_ID)
    table.moderator.plan([("hold_game", {"reason": "主持人要处理一下"})])
    await orch.takeover("p02")
    await orch.start_or_resume()
    await wait_for(lambda: orch._soft_gate.in_flight == 1)
    await orch.soft_pause()
    answer.set()
    await wait_for(lambda: orch.session.status == "paused")
    await assert_cleared(orch, "paused")
    assert orch.session.pause_reason == "hold"
    await orch.shutdown()


@async_test
async def test_the_budget_limit_clears_it(monkeypatch: pytest.MonkeyPatch) -> None:
    table = await held_game(monkeypatch)
    orch = table.orch
    await orch.pause("budget", hold_reason="已达到预算上限")
    await assert_cleared(orch, "paused")
    assert orch.session.pause_reason == "budget"
    await orch.shutdown()


@async_test
async def test_a_shutdown_clears_it_and_leaves_no_task_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    table = await held_game(monkeypatch)
    orch = table.orch
    await orch.shutdown()
    await assert_cleared(orch, "paused")
    await asyncio.sleep(0.05)
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()] == []


@async_test
async def test_a_new_game_clears_it_and_nobody_plays_a_seat_in_it(monkeypatch: pytest.MonkeyPatch) -> None:
    table = await held_game(monkeypatch)
    orch = table.orch
    old = orch.session.session_id
    await orch.new_game()
    assert orch.session.session_id != old and orch.session.status == "idle"
    assert orch.soft_pause_payload() is None and orch._soft_gate is None
    public = orch.public_session()
    assert public["soft_pause"] is None and public["clock_running"] is False and public["takeover"]["seat"] is None
    await orch.shutdown()


@async_test
async def test_loading_or_importing_a_game_is_refused_while_it_runs_soft_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    table = await held_game(monkeypatch)
    orch = table.orch
    with pytest.raises(RuntimeError):
        await orch.reload_from_data_dir()  # an archive import's last step
    with pytest.raises(RuntimeError):
        await orch.load_game(orch.session.session_id[:-1] + ("0" if orch.session.session_id[-1] != "0" else "1"))
    assert orch.soft_pause_payload() == HELD  # nothing changed
    await orch.shutdown()


@async_test
async def test_the_soft_pause_is_never_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    table = await held_game(monkeypatch)
    orch = table.orch
    payload = orch.session_payload()
    assert "soft_pause" not in json.dumps(payload) and "clock_running" not in json.dumps(payload)
    assert payload["status"] == "running"  # (like any running game: it comes back hard-paused after a restart)
    await orch.shutdown()


# ==================================================================================== the fields of the session


@async_test
async def test_every_session_update_carries_the_soft_pause_and_the_clock_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    table = SoftTable(monkeypatch)
    orch = table.orch
    idle = orch.public_session()
    assert idle["soft_pause"] is None and idle["clock_running"] is False
    await orch.takeover("p02")
    await orch.start_or_resume()
    assert orch.public_session()["clock_running"] is True
    await settle(orch)
    await orch.soft_pause()
    await wait_for(lambda: orch.soft_pause_payload() == HELD)
    await orch.soft_resume()
    await orch.pause("user")
    updates = sessions_updated(orch)
    assert len(updates) >= 4
    assert all("soft_pause" in u and "clock_running" in u for u in updates)
    assert [u["clock_running"] for u in updates][-3:] == [False, True, False]  # soft pause, soft resume, hard pause
    assert updates[-3]["soft_pause"] is not None and updates[-2]["soft_pause"] is None
    await orch.shutdown()


# ==================================================================================== the untimed public floor


@async_test
async def test_a_person_holds_a_public_floor_without_a_deadline_and_the_watchdog_leaves_the_moderator_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        game_fakes, "CFG", {**game_fakes.CFG, "runtime": {**game_fakes.CFG["runtime"], "watchdog": 0.4, "max_watchdog_wakes": 1}}
    )  # (a start re-reads the configuration: this is the one the orchestrator sees)
    table = SoftTable(monkeypatch)
    orch = table.orch
    await orch.takeover("p02")
    await orch.start_or_resume()
    await table.run([*OPEN_PUBLIC], [("set_floor_queue", {"channel_id": "public", "order": ["p02", "p01"], "limits": {"max_messages": 2, "duration_s": 0.5}})])
    seat = orch.takeover_payload()["channels"]["public"]
    assert seat["can_write"] and seat["floor"]["mine"] and seat["floor"]["untimed"] is True
    assert seat["floor"]["seconds_left"] is None and seat["floor"]["deadline_clock"] is None
    view = orch.public_session()["channels"]
    public = next(c for c in view if c["id"] == "public")
    assert public["floor"]["untimed"] is True and public["floor"]["seconds_left"] is None
    granted = [e for e in orch.world.ledger if e.type == "floor.granted" and e.channel_id == "public"]
    assert granted[0].data["untimed"] is True and "untimed" not in granted[0].data.get("limits", {})
    moderator_calls = table.calls(MODERATOR_ID)

    def watchdog_woke_the_moderator() -> bool:  # (the watchdog's wake-up is the only place this sentence comes from)
        return any("对局已经静止" in json.dumps(call, ensure_ascii=False) for call in table.log.get(MODERATOR_ID, []))

    # The watchdog looks once per poll (a quarter of its period).  Wait for twenty looks - five periods, however slow the
    # machine is - and see what every one of them found: not quiet, because a person holds a floor without a deadline.
    supervisor = orch._supervisor
    looks: list[bool] = []
    quiet = supervisor._system_quiet

    def counting() -> bool:
        answer = quiet()
        looks.append(answer)
        return answer

    supervisor._system_quiet = counting  # type: ignore[method-assign]
    await wait_for(lambda: len(looks) >= 20, timeout=60)
    assert not any(looks) and supervisor._waiting_on_untimed_floor() is True
    still = orch.takeover_payload()["channels"]["public"]
    assert still["can_write"] and still["floor"]["untimed"] is True
    assert table.calls(MODERATOR_ID) == moderator_calls and not watchdog_woke_the_moderator()  # nobody "does something about the silence"
    assert orch._stall_notice == "" and not any(e.type == "floor.token_ended" for e in orch.world.ledger)

    spoken = orch.seat_speak("public", "想了很久，说完了。")  # a person may take all the time they want, and the limits hold
    assert spoken["accepted"] and spoken["code"] is None
    assert orch.seat_pass("public")["accepted"] is True
    # The positive control: the floor is gone, the world is silent for real now - and the very same watchdog (it was armed all
    # along) does wake the moderator.  Without this the silence above would prove nothing.
    await wait_for(watchdog_woke_the_moderator, timeout=60)
    assert supervisor._waiting_on_untimed_floor() is False
    await orch.shutdown()


@async_test
async def test_the_moderator_closing_the_floor_of_the_person_works_like_for_anybody_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """G12: the moderator's rules are not held back for the person (holding them back would show that somebody plays)."""
    table = SoftTable(monkeypatch)
    orch = table.orch
    await orch.takeover("p02")
    await orch.start_or_resume()
    await table.run([*OPEN_PUBLIC], [("set_floor_queue", {"channel_id": "public", "order": ["p02", "p01"], "limits": QUEUE_LIMITS})])
    before = orch.takeover_payload()
    assert before["channels"]["public"]["floor"]["untimed"] is True and before["attention"] == ["public"]
    supervisor = orch._supervisor
    assert supervisor._waiting_on_untimed_floor() is True

    await table.run([("close_floor", {"channel_id": "public"})])  # the moderator takes the floor away
    after = orch.takeover_payload()
    state = after["channels"]["public"]
    assert state["can_write"] is False and state["can_pass"] is False and after["attention"] == []
    refused = orch.seat_speak("public", "我还想说")
    assert refused["accepted"] is False and refused["code"] in ("NO_FLOOR", "CHANNEL_CLOSED")
    ended = [e for e in orch.world.ledger if e.type == "floor.token_ended" and e.data.get("holder") == "p02"]
    assert ended and ended[-1].data["reason"] == "revoked"
    assert orch.world.untimed_floor_open() is False and supervisor._waiting_on_untimed_floor() is False  # nobody is waited for
    public = next(c for c in orch.public_session()["channels"] if c["id"] == "public")
    assert public["floor"]["kind"] == "none" and public["floor"]["untimed"] is False
    await orch.shutdown()


@async_test
async def test_the_other_floors_and_the_other_seats_keep_their_deadlines(monkeypatch: pytest.MonkeyPatch) -> None:
    table = SoftTable(monkeypatch)
    orch = table.orch
    await orch.takeover("p02")
    await orch.start_or_resume()
    await table.run(
        [*OPEN_PUBLIC, ("create_channel", {"channel_id": "team", "name": "小队", "members": ["p02", "p03"], "type": "group"})],
        [("set_floor_queue", {"channel_id": "team", "order": ["p02"], "limits": {"max_messages": 2, "duration_s": 30}}),
         ("open_request", {"to": ["p02"], "kind": "text", "question": "说点什么", "duration_s": 25})],
    )
    state = orch.takeover_payload()
    team = state["channels"]["team"]["floor"]
    assert team["untimed"] is False and team["seconds_left"] is not None and team["deadline_clock"] is not None
    assert state["requests"][0]["seconds_left"] is not None and state["requests"][0]["deadline_clock"] is not None
    await table.run([("set_floor_queue", {"channel_id": "public", "order": ["p01"], "limits": {"max_messages": 2, "duration_s": 20}})])
    agent = orch.world.channel("public").group.tokens[0]
    assert agent.holder == "p01" and agent.deadline is not None  # an agent in the public channel: timed
    await orch.shutdown()


@async_test
async def test_giving_the_seat_back_makes_its_floor_timed_again_and_taking_it_over_again_makes_it_untimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = SoftTable(monkeypatch)
    orch = table.orch
    await orch.takeover("p02")
    await orch.start_or_resume()
    await table.run([*OPEN_PUBLIC], [("set_floor_queue", {"channel_id": "public", "order": ["p02"], "limits": {"max_messages": 2, "duration_s": 40}})])
    token = orch.world.channel("public").group.tokens[0]
    assert token.deadline is None and token.remaining == 40.0
    await orch.pause("user")
    clock = orch.clock.now()
    last_id = orch.bus.last_id
    await orch.release_takeover()  # the seat goes back to its agent: its floor has a clock again, with what it had left
    assert token.deadline == pytest.approx(clock + 40.0, abs=0.01) and token.remaining is None
    floors = [e.data for e in orch.bus._ring if e.type == "floor.changed" and e.id > last_id]
    assert floors and floors[-1]["floor"]["untimed"] is False  # the clients were told
    await orch.takeover("p02")
    assert token.deadline is None and token.remaining == pytest.approx(40.0, abs=0.01)
    assert orch.takeover_payload()["channels"]["public"]["floor"]["untimed"] is True
    await orch.shutdown()


@async_test
async def test_after_a_restart_the_game_is_hard_paused_and_the_played_seat_is_untimed_again(monkeypatch: pytest.MonkeyPatch) -> None:
    table = SoftTable(monkeypatch)
    orch = table.orch
    await orch.takeover("p02")
    await orch.start_or_resume()
    await table.run([*OPEN_PUBLIC], [("set_floor_queue", {"channel_id": "public", "order": ["p02", "p01"], "limits": QUEUE_LIMITS})])
    await orch.soft_pause()  # (a game that is soft-paused when the process ends)
    await wait_for(lambda: orch.soft_pause_payload() == HELD)
    await orch.shutdown()
    payload = json.loads(json.dumps(orch.session_payload()))
    assert payload["takeover"]["seat"] == "p02"

    restarted = real_orch(monkeypatch)  # a new process: nothing but the save
    loaded = restarted._validate_session_data(payload)
    await restarted._adopt_loaded_session(loaded, "已恢复上次对局")
    assert restarted.soft_pause_payload() is None and restarted.clock_running() is False
    assert restarted.session.status == "paused" and restarted._supervisor is None
    assert restarted.world.untimed_holders == frozenset({"p02"})  # set again from the takeover state, not from the save
    seat = restarted.takeover_payload()["channels"]["public"]["floor"]
    assert seat["untimed"] is True and seat["seconds_left"] is None
    token = restarted.world.channel("public").group.tokens[0]
    assert token.holder == "p02" and token.deadline is None and token.remaining == 30.0
    await restarted.release_takeover()  # given back after the restart: timed again with the time it had left
    assert token.deadline == pytest.approx(restarted.clock.now() + 30.0, abs=0.01)
    await restarted.shutdown()


@async_test
async def test_a_new_game_world_and_a_rebuilt_table_keep_the_played_seat_untimed(monkeypatch: pytest.MonkeyPatch) -> None:
    table = SoftTable(monkeypatch)
    orch = table.orch
    await orch.takeover("p03")
    assert orch.world.untimed_holders == frozenset({"p03"})
    world = orch.world
    orch.apply_table()  # a new world (what a new line-up does to a game that has not started: v0.14 has no re-deal), the same person
    assert orch.world is not world and orch.world.untimed_holders == frozenset({"p03"})
    await orch.release_takeover()
    assert orch.world.untimed_holders == frozenset()
    await orch.takeover("p04")
    await orch.new_game()  # a new game has nobody at the keyboard
    assert orch.takeover_seat is None and orch.world.untimed_holders == frozenset()
    await orch.shutdown()


@async_test
async def test_every_way_to_get_another_world_tells_it_who_is_played(monkeypatch: pytest.MonkeyPatch) -> None:
    """``untimed_holders`` is not part of a save: whatever builds or installs a world sets it again from the session that is
    live afterwards - a rename (an idle game is rebuilt), the rebuild at start-up, a new game, loading a saved game."""
    table = SoftTable(monkeypatch)
    orch = table.orch
    await orch.takeover("p03")
    assert orch.world.untimed_holders == frozenset({"p03"})

    world = orch.world
    await orch.bots_changed(roster=True)  # what a new name or avatar does to an idle game: the same seed, a new world
    assert orch.world is not world and orch.world.untimed_holders == frozenset({"p03"})

    world = orch.world
    orch._reload_fresh_game_from_data_dir()  # the first game is rebuilt when the data directory is locked at start-up
    assert orch.world is not world and orch.world.untimed_holders == frozenset({"p03"})

    saved = orch.session.session_id
    orch.game_store.save(saved, json.loads(json.dumps(orch.session_payload())))  # (the in-memory store of the tests)
    await orch.new_game()  # a new game: ITS session has nobody at the keyboard, although the old one had (see _new_world)
    assert orch.session.session_id != saved and orch.takeover_seat is None
    assert orch.world.untimed_holders == frozenset()

    await orch.load_game(saved)  # the saved game comes back, paused, with its person
    assert orch.session.session_id == saved and orch.takeover_seat == "p03"
    assert orch.world.untimed_holders == frozenset({"p03"})

    orch.settings_store.last_id = saved  # an imported archive: the data directory is read again and the last game restored
    await orch.new_game()
    assert orch.world.untimed_holders == frozenset()
    await orch.reload_from_data_dir()
    assert orch.session.session_id == saved and orch.takeover_seat == "p03"
    assert orch.world.untimed_holders == frozenset({"p03"})

    await orch.release_takeover()  # and from here the take-over / give-back keep it in step, as always
    assert orch.world.untimed_holders == frozenset()
    await orch.shutdown()
