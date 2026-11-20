"""Playing a seat (v0.13, F2-F12): the orchestrator side of taking a seat over.

The orchestrator under test is the real :class:`GameOrchestrator` (real world, runtime, brain) with scripted
agents (``tests/v12/bots.py``) and the 4-player ``minimal_test`` scenario; only the storage is a fake
(``tests/game_fakes.py``).  The person is a script too: it calls ``seat_speak`` / ``seat_respond`` ... exactly like
the HTTP layer does.  Nothing here calls a model.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest

import app.game.takeover_runtime as takeover_module
import game_fakes
from app.game.orchestrator import AUTOSAVE_EVENT_TYPES, GameOrchestrator, NotReadyError, TakeoverError
from app.game.session import HumanSpan
from app.llm.types import LLMResponse
from app.world.chat import speech_gate
from app.world.seat import seat_status
from app.world.types import ENGINE_ID, MODERATOR_ID, Envelope, Receipt, UiEvent
from game_fakes import BINDING, PLAYERS, all_bound, async_test, real_orch
from learning_fakes import ScriptedLlm, by_kind
from v12.bots import BotLlm, Call, simple_player

IDS = [p["id"] for p in PLAYERS]  # p01 .. p04


# ------------------------------------------------------------------------------------------------ helpers
def passive(world: Any, me: str) -> list[Call]:
    """An agent that never does anything (it sleeps whenever it is woken)."""
    return []


def talk_once() -> Callable[[Any, str], list[Call]]:
    """An agent that says one thing in ``public`` the first time it is woken (a refusal does not matter)."""
    done: set[str] = set()

    def policy(world: Any, me: str) -> list[Call]:
        if me in done:
            return []
        done.add(me)
        return [("speak", {"channel_id": "public", "content": f"我是 {me}，大家好。"})]

    return policy


class Steered:
    """A moderator the test steers: the batches of calls it is given are made one after the other."""

    def __init__(self) -> None:
        self.batches: list[list[Call]] = []

    def plan(self, *batches: list[Call]) -> None:
        self.batches.extend(batches)

    def __call__(self, world: Any, me: str) -> list[Call]:
        return self.batches.pop(0) if self.batches else []


class RecordingLlm(BotLlm):
    """A scripted model that remembers every conversation it was asked to continue (per agent)."""

    def __init__(self, world: Any, policies: dict[str, Callable[[Any, str], list[Call]]], log: dict[str, list[list[dict[str, Any]]]]) -> None:
        super().__init__(world, policies)
        self.log = log

    async def complete(self, agent_id, messages, tools, *, max_tokens=None):  # type: ignore[no-untyped-def]
        self.log.setdefault(agent_id, []).append(copy.deepcopy(messages))
        return await super().complete(agent_id, messages, tools, max_tokens=max_tokens)


class Table:
    """An orchestrator with scripted agents, the steered moderator and the log of every model call."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        agents: Callable[[Any, str], list[Call]] = passive,
        moderator: Callable[[Any, str], list[Call]] | None = None,
        bound: bool = True,
        ids: list[str] | None = None,
    ) -> None:
        """``ids``: other seat ids than ``p01``..``p04`` (the roster store only knows ``N1``.. - a review needs them)."""
        self.ids = list(ids or IDS)
        if ids is not None:
            cfg = copy.deepcopy(game_fakes.CFG)
            cfg["participants"] = [{"id": i, "seat": str(n), "name": f"P{n}", "persona": ""} for n, i in enumerate(ids, start=1)]
            monkeypatch.setattr(game_fakes, "CFG", cfg)
        bindings = {a: BINDING for a in [MODERATOR_ID, *self.ids]}
        self.orch: GameOrchestrator = real_orch(monkeypatch, defaults=bindings if bound else None)
        self.moderator = Steered()
        self.log: dict[str, list[list[dict[str, Any]]]] = {}
        policies: dict[str, Callable[[Any, str], list[Call]]] = {
            MODERATOR_ID: moderator or self.moderator,
            **{i: agents for i in self.ids},
        }
        self.orch.llm_port_factory = lambda o: RecordingLlm(o.world, policies, self.log)

    def calls(self, agent_id: str) -> int:
        return len(self.log.get(agent_id, []))

    async def poke(self) -> None:
        """Wake the moderator (it then makes its next planned batch)."""
        assert self.orch._supervisor is not None
        self.orch._supervisor.deliver(MODERATOR_ID, Envelope(kind="notice", priority="urgent", payload={"event": "test.poke"}))
        await asyncio.sleep(0)

    async def run(self, *batches: list[Call]) -> None:
        """Plan ``batches`` and let the moderator make the first one (the game must be running)."""
        self.moderator.plan(*batches)
        await self.poke()
        await settle(self.orch)


async def wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def settle(orch: GameOrchestrator, quiet: float = 0.15) -> None:
    """Wait until the world has been quiet for a while (every woken agent has finished its run)."""
    seq = -1
    stable_since = asyncio.get_running_loop().time()
    while True:
        await asyncio.sleep(0.02)
        now = asyncio.get_running_loop().time()
        current = (orch.world.seq, len(orch.bus._ring))
        if current != seq:
            seq, stable_since = current, now
        elif now - stable_since >= quiet:
            return


def ids_of(orch: GameOrchestrator, role: str) -> list[str]:
    return [p.id for p in orch.world.state.players if p.role == role]


async def start_and_pause(orch: GameOrchestrator) -> None:
    """Start the game (that deals the identities, v0.14 H52) and stop it again: a seat can be taken over by what it holds."""
    await orch.start_or_resume()
    await orch.pause("user")


def pushes(orch: GameOrchestrator, after: int = 0) -> list[dict[str, Any]]:
    """The ``takeover.updated`` payloads published after bus id ``after``."""
    return [e.data["takeover"] for e in orch.bus._ring if e.type == "takeover.updated" and e.id > after]


def strip_clock(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_clock(v) for k, v in value.items() if k != "seconds_left"}
    if isinstance(value, list):
        return [strip_clock(v) for v in value]
    return value


OPEN_PUBLIC: list[Call] = [("create_channel", {"channel_id": "public", "name": "公共频道", "members": IDS, "type": "public", "mode": "free"})]


# ------------------------------------------------------------------------------------ taking over / giving back
@async_test
async def test_only_a_player_seat_while_the_game_is_not_running_one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    with pytest.raises(TakeoverError) as err:
        await orch.takeover(MODERATOR_ID)
    assert err.value.status == 422 and "主持人" in str(err.value)
    with pytest.raises(TakeoverError) as err:
        await orch.takeover("p99")
    assert err.value.status == 404
    with pytest.raises(TakeoverError) as err:
        await orch.takeover("")
    assert err.value.status == 404

    seq = orch.world.seq
    await orch.takeover("p02")  # a game that has not started
    assert orch.session.takeover.seat == "p02" and orch.takeover_seat == "p02"
    assert orch.session.human_spans == [HumanSpan(seat="p02", from_seq=seq, to_seq=None)]
    assert orch.agent_state["p02"] == "human"

    await orch.takeover("p02")  # the same seat again: nothing happens
    assert len(orch.session.human_spans) == 1
    with pytest.raises(TakeoverError) as err:
        await orch.takeover("p03")
    assert err.value.status == 409 and "请先退出" in str(err.value)

    await orch.release_takeover()
    assert orch.session.takeover.seat is None and orch.takeover_seat is None
    assert orch.session.human_spans == [HumanSpan(seat="p02", from_seq=seq, to_seq=orch.world.seq)]
    await orch.release_takeover()  # nobody is played: nothing happens
    assert len(orch.session.human_spans) == 1
    assert orch.agent_state["p02"] == "sleeping"


@async_test
async def test_a_game_that_runs_cannot_be_taken_over_or_given_back_but_a_paused_or_finished_one_can(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    await orch.start_or_resume()  # running, nobody plays
    assert orch.session.status == "running"
    with pytest.raises(TakeoverError) as err:
        await orch.takeover("p02")
    assert err.value.status == 409 and "暂停" in str(err.value) and orch.takeover_seat is None
    await orch.pause("user")

    await orch.takeover("p01")  # paused: allowed
    await orch.start_or_resume()  # running, with the seat played
    with pytest.raises(TakeoverError) as err:
        await orch.release_takeover()
    assert err.value.status == 409 and "暂停" in str(err.value)
    assert orch.takeover_seat == "p01"
    with pytest.raises(TakeoverError) as err:
        await orch.takeover("p02")  # (one seat at a time, whatever the status)
    assert err.value.status == 409

    await orch.pause("user")  # paused: the seat can be given back and another one taken
    await orch.release_takeover()
    await orch.takeover("p03")
    assert orch.takeover_seat == "p03"
    assert [(s.seat, s.to_seq is None) for s in orch.session.human_spans] == [("p01", False), ("p03", True)]

    await orch.start_or_resume()
    await table.run([("end_game", {"winner": "测试队", "summary": "结束"})])
    await wait_for(lambda: orch.session.status == "finished")
    await orch.release_takeover()  # a finished game: allowed
    assert orch.takeover_seat is None
    assert orch.session.human_spans[-1].to_seq == orch.world.seq
    with pytest.raises(TakeoverError) as err:
        await orch.takeover("p02")  # ... but a finished game cannot be taken over
    assert err.value.status == 409 and "已结束" in str(err.value)
    await orch.shutdown()


# ------------------------------------------------------------------------------------ the seat has no agent
@async_test
async def test_the_seat_has_no_agent_and_nothing_is_called_or_counted_for_it(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch, agents=talk_once())
    orch = table.orch
    await orch.takeover("p03")
    table.moderator.plan(
        [*OPEN_PUBLIC, ("send_message", {"channel_id": "public", "content": "大家好，欢迎。"})],
        [("send_message", {"channel_id": "public", "content": "第二条消息。"})],
    )
    await orch.start_or_resume()
    await wait_for(lambda: table.calls("p01") >= 1 and table.calls("p02") >= 1)
    await table.poke()
    await settle(orch)
    assert orch._supervisor is not None
    assert set(orch._supervisor.agents) == {MODERATOR_ID, "p01", "p02", "p04"}  # no agent for p03
    assert table.calls("p03") == 0 and table.calls("p01") >= 1
    usage = orch.public_session()["usage_stats"]["by_actor"]
    assert "p03" not in usage and usage["p01"]["activations"] >= 1
    # the messages the world addressed to the seat were dropped on the way: nothing woke anything for it
    assert not [e for e in orch.bus._ring if e.type in ("agent.status", "agent.metrics") and e.data.get("id") == "p03"]

    seat_state = {p["id"]: p for p in orch.public_session()["participants"]}["p03"]
    assert seat_state["state"] == "human" and seat_state["sleep_until"] is None
    assert {p["id"]: p["state"] for p in orch.public_session()["participants"] if p["id"] != "p03"}
    await orch.pause("user")
    assert orch.agent_state["p03"] == "human"  # a stopped game puts the agents to sleep, not the person
    assert {p["id"]: p for p in orch.public_session()["participants"]}["p03"]["state"] == "human"
    await orch.shutdown()


# ------------------------------------------------------------------------- the rules are the engine's
@async_test
async def test_a_turn_follows_the_engines_limits_for_the_person_as_for_an_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    await orch.takeover("p02")
    await orch.start_or_resume()
    limits = {"max_messages": 2, "max_chars_per_message": 20, "max_chars_total": 50, "duration_s": 60}
    await table.run([
        ("create_channel", {"channel_id": "public", "name": "公共频道", "members": IDS, "type": "public"}),
        ("set_floor_queue", {"channel_id": "public", "order": ["p02", "p01"], "limits": limits}),
    ])
    state = orch.takeover_payload()
    mine = state["channels"]["public"]
    assert mine["can_write"] and mine["max_chars"] == 20 and mine["can_pass"]
    assert mine["floor"]["mine"] and mine["floor"]["messages_left"] == 2 and mine["floor"]["chars_left"] == 50
    assert state["attention"] == ["public"]

    too_long = orch.seat_speak("public", "字" * 30)  # over the per-message limit: sent back, never cut (v0.14, H16)
    assert not too_long["accepted"] and too_long["code"] == "TOO_LONG" and too_long["data"] == {"chars": 30, "limit": 20}
    assert not [e for e in orch.world.ledger if e.type == "message" and e.actor_id == "p02"]  # nothing was written ...
    still = orch.takeover_payload()["channels"]["public"]
    assert still["can_write"] and still["floor"]["mine"] and still["floor"]["messages_left"] == 2 and still["floor"]["chars_left"] == 50  # ... or counted
    first = orch.seat_speak("public", "字" * 20)  # exactly the limit goes through, as it is
    assert first["accepted"] and first["code"] is None and not first.get("truncated")
    speech = [e for e in orch.world.ledger if e.type == "message" and e.actor_id == "p02"][-1]
    assert speech.data["content"] == "字" * 20 and not speech.data.get("truncated")
    await settle(orch, 0.05)
    left = orch.takeover_payload()["channels"]["public"]
    assert left["floor"]["messages_left"] == 1 and left["floor"]["chars_left"] == 30 and left["max_chars"] == 20

    second = orch.seat_speak("public", "第二条")
    assert second["accepted"]
    third = orch.seat_speak("public", "第三条")  # the token is used up and the turn passed on
    assert not third["accepted"] and third["code"] == "NO_FLOOR"
    after = orch.takeover_payload()
    assert after["channels"]["public"]["can_write"] is False and after["attention"] == []
    assert not orch.seat_pass("public")["accepted"]  # nothing to pass any more

    # the actor is ALWAYS the seat: there is no way to name another one
    assert {e.actor_id for e in orch.world.ledger if e.type == "message" and e.channel_id == "public"} == {"p02"}
    await orch.shutdown()


@async_test
async def test_a_person_can_pass_and_a_token_that_runs_out_ends_like_an_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    await orch.takeover("p02")
    await orch.start_or_resume()
    await table.run([
        ("create_channel", {"channel_id": "public", "name": "公共频道", "members": IDS, "type": "public"}),
        ("create_channel", {"channel_id": "team", "name": "小队频道", "members": ["p02", "p03"], "type": "group"}),
        ("set_floor_queue", {"channel_id": "public", "order": ["p02", "p03"], "limits": {"max_messages": 3, "duration_s": 60}}),
    ])
    assert orch.takeover_payload()["channels"]["public"]["can_pass"] is True
    passed = orch.seat_pass("public")
    assert passed["accepted"] and orch.takeover_payload()["channels"]["public"]["can_write"] is False
    assert orch.seat_speak("public", "我已经让出发言权了")["code"] == "NO_FLOOR"

    # the deadline is the world's: in any channel but a public one (v0.13.1, G10: there the person's turn has no deadline,
    # see tests/test_softpause_runtime.py) the person's turn runs out by itself, just like an agent's
    await table.run([("set_floor_queue", {"channel_id": "team", "order": ["p02"], "limits": {"max_messages": 3, "duration_s": 0.3}})])
    assert orch.takeover_payload()["channels"]["team"]["can_write"] is True
    await wait_for(lambda: not orch.takeover_payload()["channels"]["team"]["can_write"], timeout=5)
    late = orch.seat_speak("team", "来晚了")
    assert not late["accepted"] and late["code"] in ("NO_FLOOR", "CHANNEL_CLOSED")  # (the engine closes a finished floor)
    await orch.shutdown()


@async_test
async def test_a_free_discussion_counts_the_persons_messages_and_sends_back_what_is_too_long(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    await orch.takeover("p04")
    await orch.start_or_resume()
    await table.run(
        [*OPEN_PUBLIC],
        [("open_floor", {"channel_id": "public", "mode": "free", "limits": {"max_messages_per_member": 2, "max_chars_per_message": 10}})],
    )
    state = orch.takeover_payload()["channels"]["public"]
    assert state["can_write"] and state["max_chars"] == 10 and state["floor"]["kind"] == "free" and state["floor"]["messages_left"] == 2
    long = orch.seat_speak("public", "字" * 14)  # sent back, not cut and not counted (v0.14, H16)
    assert not long["accepted"] and long["code"] == "TOO_LONG" and long["data"] == {"chars": 14, "limit": 10}
    assert orch.takeover_payload()["channels"]["public"]["floor"]["messages_left"] == 2
    one = orch.seat_speak("public", "字" * 10)
    assert one["accepted"] and not one.get("truncated")
    assert orch.seat_speak("public", "第二句")["accepted"]
    third = orch.seat_speak("public", "第三句")
    assert not third["accepted"] and third["code"] == "QUOTA_EXCEEDED"
    assert orch.takeover_payload()["channels"]["public"]["can_write"] is False
    # the private chat of the seat has its own, longer limit (the scenario's ``dm`` default)
    dm = orch.takeover_payload()["channels"]["dm-p04"]
    assert dm["can_write"] and dm["max_chars"] == orch.world.spec.defaults.dm.max_chars_per_message
    await orch.shutdown()


@async_test
async def test_a_draft_written_across_an_invalidating_entry_is_stale_until_it_is_sent_again(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    await orch.takeover("p02")
    await orch.start_or_resume()
    await table.run([*OPEN_PUBLIC])
    began = orch.takeover_payload()["seq"]  # the person starts a draft here ...
    await table.run([("set_phase", {"text": "第 2 轮"})])  # ... the moderator changes the phase meanwhile
    stale = orch.seat_speak("public", "写了很久的草稿", began)
    assert not stale["accepted"] and stale["code"] == "STALE" and stale["data"]["latest_seq"] == orch.world.seq
    assert not [e for e in orch.world.ledger if e.type == "message" and e.actor_id == "p02"]
    again = orch.seat_speak("public", "写了很久的草稿", orch.takeover_payload()["seq"])
    assert again["accepted"]
    assert orch.seat_speak("public", "不带 based_on_seq 的发言")["accepted"]  # omitted: what is current
    assert orch.seat_speak("public", "过大的 based_on_seq 被截到当前", 10**9)["accepted"]
    await orch.shutdown()


@async_test
async def test_requests_are_answered_changed_and_refused_like_an_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    await orch.takeover("p02")
    await orch.start_or_resume()
    options = [{"id": "p01", "label": "一号"}, {"id": "p03", "label": "三号"}]
    await table.run([*OPEN_PUBLIC])
    await table.run([("open_request", {"to": ["p02", "p03"], "kind": "choice", "question": "选一个", "options": options, "duration_s": 60, "share_answers": True, "auto_close": "unanimous"})])
    assert {r["id"] for r in orch.takeover_payload()["requests"]} == {"r1"}  # the second request is not open yet
    assert pushes(orch)[-1]["requests"][0]["id"] == "r1"
    await table.run([("open_request", {"to": ["p02"], "kind": "text", "question": "写点什么", "duration_s": 60})])
    rows = {r["id"]: r for r in orch.takeover_payload()["requests"]}
    assert set(rows) == {"r1", "r2"} and rows["r1"]["kind"] == "choice" and rows["r2"]["max_chars"] == orch.world.spec.defaults.request.max_text_chars
    assert not rows["r1"]["answered"] and rows["r1"]["deadline_clock"] is not None
    assert set(orch.takeover_payload()["attention"]) >= {rows["r1"]["channel_id"]}

    assert orch.seat_respond("r1", "nobody")["code"] == "INVALID_TARGET"
    assert orch.seat_respond("r9", "p01")["code"] == "UNKNOWN_REQUEST"
    ok = orch.seat_respond("r1", "p01", "因为他发言靠谱")
    assert ok["accepted"] and ok["data"]["replaced"] is False and ok["data"]["closed"] is False
    mine = {r["id"]: r for r in orch.takeover_payload()["requests"]}["r1"]
    assert mine["answered"] and mine["my_answer"] == {"value": "p01", "label": "一号", "reason": "因为他发言靠谱"}
    changed = orch.seat_respond("r1", "p03")  # the answer of an open request may be changed
    assert changed["accepted"] and changed["data"]["replaced"] is True
    long_text = orch.seat_respond("r2", "答" * (orch.world.spec.defaults.request.max_text_chars + 5))
    assert long_text["accepted"] and long_text["truncated"]["dropped_chars"] == 5 and long_text["data"]["closed"] is True
    assert orch.seat_respond("r2", "再答一次")["code"] == "REQUEST_CLOSED"  # a closed request is closed
    assert {r["id"] for r in orch.takeover_payload()["requests"]} == {"r1"}
    await orch.shutdown()


@async_test
async def test_an_enabled_ability_is_offered_used_once_and_validated_by_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    await start_and_pause(orch)  # (the identities are dealt when the game starts, H52: a seat is picked by its identity afterwards)
    seat = ids_of(orch, "red")[0]
    await orch.takeover(seat)
    await orch.start_or_resume()
    await table.run([*OPEN_PUBLIC])
    assert orch.takeover_payload()["abilities"] == []  # nothing is enabled yet
    assert orch.seat_ability("shout", {"declaration": "x"})["code"] == "ABILITY_DISABLED"

    await table.run([("enable_ability", {"ability_id": "shout"})])
    await table.poke()
    await settle(orch)
    [ability] = orch.takeover_payload()["abilities"]  # pushed (the moderator went back to sleep after enabling it)
    assert pushes(orch)[-1]["abilities"] == [ability]
    assert ability["id"] == "shout" and ability["quota_left"] == 1 and ability["description"]
    assert ability["params"] == [{"name": "declaration", "type": "text", "optional": False, "max_chars": 60}]
    assert orch.seat_ability("nope", {})["code"] == "UNKNOWN_ABILITY"
    assert orch.seat_ability("shout", {})["code"] == "INVALID_PARAMS"
    assert orch.seat_ability("shout", {"declaration": "x", "extra": 1})["code"] == "INVALID_PARAMS"

    used = orch.seat_ability("shout", {"declaration": "我是红队，我来了！"})
    assert used["accepted"] and used["data"]["uses_left"] == 0
    assert orch.seat_ability("shout", {"declaration": "再来一次"})["code"] == "QUOTA_EXCEEDED"
    assert orch.takeover_payload()["abilities"] == []
    # the reveal is a public message of the seat, and the engine froze the channel (nobody may write now)
    reveal = [e for e in orch.world.ledger if e.type == "message" and e.data.get("kind") == "ability"]
    assert len(reveal) == 1 and reveal[0].actor_id == seat and "我是红队，我来了！" in reveal[0].data["content"]
    frozen = orch.takeover_payload()["channels"]["public"]
    assert frozen["can_write"] is False and "冻结" in frozen["why_not"]
    await orch.shutdown()


@async_test
async def test_a_seat_that_does_not_hold_the_ability_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    await start_and_pause(orch)
    blue = ids_of(orch, "blue")[0]
    await orch.takeover(blue)
    await orch.start_or_resume()
    await table.run([*OPEN_PUBLIC], [("enable_ability", {"ability_id": "shout"})])
    await table.poke()
    await settle(orch)
    assert orch.takeover_payload()["abilities"] == []  # blue holds nothing
    assert orch.seat_ability("shout", {"declaration": "x"})["code"] == "FORBIDDEN"
    await orch.shutdown()


@async_test
async def test_nothing_is_accepted_unless_a_seat_is_played_and_the_game_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    for action in (
        lambda: orch.seat_speak("public", "hi"),
        lambda: orch.seat_pass("public"),
        lambda: orch.seat_respond("r1", "x"),
        lambda: orch.seat_ability("shout", {}),
    ):
        with pytest.raises(TakeoverError) as err:
            action()
        assert err.value.status == 409 and "请先接管" in str(err.value)

    await orch.takeover("p02")
    seq = orch.world.seq
    for action in (lambda: orch.seat_speak("dm-p02", "hi"), lambda: orch.seat_pass("dm-p02")):
        with pytest.raises(TakeoverError) as err:  # idle
            action()
        assert err.value.status == 409 and "还没有开始" in str(err.value)
    await orch.start_or_resume()
    await orch.pause("user")
    with pytest.raises(TakeoverError) as err:  # paused
        orch.seat_speak("dm-p02", "hi")
    assert err.value.status == 409 and "已暂停" in str(err.value)
    await orch.start_or_resume()
    await table.run([("end_game", {"winner": "测试队", "summary": "结束"})])
    await wait_for(lambda: orch.session.status == "finished")
    with pytest.raises(TakeoverError) as err:  # finished
        orch.seat_speak("dm-p02", "hi")
    assert err.value.status == 409 and "已结束" in str(err.value)
    assert not [e for e in orch.world.ledger if e.type == "message" and e.actor_id == "p02"]
    assert orch.world.seq > seq
    await orch.shutdown()


# ---------------------------------------------------------------------------------------- takeover.updated
GATE_MODES: list[tuple[str, list[Call]]] = [
    ("free", [("create_channel", {"channel_id": "side", "name": "旁路", "members": IDS, "mode": "free"})]),
    ("closed", [("update_channel", {"channel_id": "side", "mode": "closed"})]),
    ("listen", [("update_channel", {"channel_id": "side", "mode": "listen"})]),
    ("queue", [("set_floor_queue", {"channel_id": "side", "order": ["p01", "p02", "p03"], "limits": {"duration_s": 60}})]),
    ("queue-paused", [("pause_queue", {"channel_id": "side"})]),
    ("queue-resumed", [("resume_queue", {"channel_id": "side"})]),
    ("turn-p02", [("open_floor", {"channel_id": "side", "mode": "turn", "holders": ["p02"], "limits": {"max_messages": 2}})]),
    ("free-limited", [("open_floor", {"channel_id": "side", "mode": "free", "holders": ["p02"], "limits": {"max_messages_per_member": 1}})]),
    ("readonly", [("update_channel", {"channel_id": "side", "readonly_members": ["p02"]})]),
    ("dm-closed", [("update_channel", {"channel_id": "dm-p02", "mode": "closed"})]),
]


@async_test
async def test_the_pushed_state_is_the_gates_state_after_every_change_of_the_world(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    await orch.takeover("p02")
    await orch.start_or_resume()
    await table.run([*OPEN_PUBLIC])
    checked = 0
    for name, batch in GATE_MODES:
        table.moderator.plan(batch)
        await table.poke()
        await settle(orch, 0.12)
        pushed = pushes(orch)[-1]
        assert strip_clock(pushed) == strip_clock(orch.public_session()["takeover"]), name
        assert strip_clock(pushed) == strip_clock({**orch.takeover_payload()}), name
        for channel in orch.world.state.channels:
            if "p02" not in channel.members:
                continue
            gate = speech_gate(orch.world, channel, "p02")
            state = pushed["channels"][channel.id]
            assert state["can_write"] is (not isinstance(gate, Receipt)), (name, channel.id, state)
            if isinstance(gate, Receipt):
                assert state["code"] == gate.code, (name, channel.id)
            else:
                assert state["max_chars"] == gate.max_chars, (name, channel.id)
            checked += 1
    assert checked >= len(GATE_MODES) * 2
    json.dumps(pushes(orch))  # the wire format
    await orch.shutdown()


@async_test
async def test_pushes_are_merged_per_loop_turn_skipped_when_nothing_changed_and_never_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    reasons: list[str] = []
    real_schedule = orch.schedule_autosave

    def spy(reason: str) -> None:
        reasons.append(reason)
        real_schedule(reason)

    orch.schedule_autosave = spy  # type: ignore[method-assign]
    await orch.takeover("p01")
    assert reasons == ["takeover"]  # taking over is part of the save ...
    base = orch.bus.last_id
    for _ in range(25):  # a burst of events inside ONE loop turn, the world unchanged
        orch._publish(UiEvent(type="request.progress", data={}))
    assert pushes(orch, base) == []  # nothing yet: the push is scheduled after this turn
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert pushes(orch, base) == []  # ... and nothing changed, so there is nothing to publish

    channel = orch.world.channel("dm-p01")
    assert channel is not None
    channel.mode = "closed"  # (the world changes; the events that follow are what makes the push happen)
    for _ in range(25):
        orch._publish(UiEvent(type="request.progress", data={}))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    [pushed] = pushes(orch, base)  # ONE push for the whole burst
    assert pushed["channels"]["dm-p01"]["can_write"] is False and pushed["seat"] == "p01"

    quiet = orch.bus.last_id
    channel.mode = "free"
    for event_type in ("agent.metrics", "budget.updated", "budget.warning", "budget.reached"):
        orch._publish(UiEvent(type=event_type, data={}))
    await asyncio.sleep(0.02)
    assert pushes(orch, quiet) == []  # these cannot change what the person may do: no recomputation
    orch._publish(UiEvent(type="agent.status", data={"id": "p02", "state": "sleeping"}))  # but this can (a moderator run ended)
    await asyncio.sleep(0.02)
    assert pushes(orch, quiet)[-1]["channels"]["dm-p01"]["can_write"] is True
    # a push is never a reason to save
    assert reasons == ["takeover"] and "takeover.updated" not in AUTOSAVE_EVENT_TYPES


@async_test
async def test_taking_over_and_giving_back_are_pushed_at_once_and_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    orch._autosave_enabled = True
    base = orch.bus.last_id
    await orch.takeover("p03")
    [first] = pushes(orch, base)
    assert first["seat"] == "p03" and "reshuffle_available" not in first and "dm-p03" in first["channels"]
    await orch.release_takeover()
    assert pushes(orch, base)[-1] == {"seat": None}
    await orch.flush_autosave("test")
    saved = orch.game_store.data[orch.session.session_id]
    assert saved["takeover"] == {"seat": None} and saved["human_spans"][0]["seat"] == "p03" and saved["human_spans"][0]["to_seq"] is not None
    await orch.shutdown()


# ---------------------------------------------------------------------------- the identities are dealt at the start (H52)
@async_test
async def test_the_identities_are_dealt_when_the_game_starts_and_never_again_even_with_a_seat_taken_over(monkeypatch: pytest.MonkeyPatch) -> None:
    """v0.14 replaces the re-deal of v0.13 (F5): nothing is dealt before the start - for the person who plays a seat either - and exactly
    once at the start, whatever happens to the seat meanwhile."""
    table = Table(monkeypatch)
    orch = table.orch
    await orch.takeover("p02")
    assert orch.world.state.dealt is False and all(not p.role and not p.faction and not p.tags for p in orch.world.state.players)
    board = orch.world.board("p02")
    assert not board["me"].get("role") and not board["me"].get("faction") and not board["me"].get("tags")  # what the person is shown
    assert "reshuffle_available" not in orch.public_session()["takeover"] and orch.public_session()["dealt"] is False

    await orch.start_or_resume()
    assert orch.world.state.dealt and orch.public_session()["dealt"] is True and all(p.role for p in orch.world.state.players)
    roles = {p.id: p.role for p in orch.world.state.players}
    deals = [e for e in orch.world.ledger if e.type == "player.updated" and e.data.get("reason") == "deal"]
    assert len(deals) == len(IDS)
    await orch.pause("user")
    await orch.release_takeover()
    await orch.takeover("p03")
    await orch.start_or_resume()  # resumed: the cards stay as they are
    assert {p.id: p.role for p in orch.world.state.players} == roles
    assert len([e for e in orch.world.ledger if e.type == "player.updated" and e.data.get("reason") == "deal"]) == len(IDS)
    await orch.shutdown()


@async_test
async def test_a_start_record_means_the_game_has_started_even_when_the_status_says_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    assert orch.game_has_started() is False
    orch.session.started_at = "2026-11-18T08:00:00+00:00"  # (set when the game starts)
    assert orch.game_has_started()
    orch.session.started_at = None
    orch.world.commit("game.status", ENGINE_ID, {"status": "running", "reason": "started"}, visible_to="*")  # a ledger record
    assert orch.game_has_started()


def test_the_re_deal_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """F5 is deleted (v0.14, H52): no ``shuffle_identities``, no ``reshuffle_available``, no route."""
    orch = Table(monkeypatch).orch
    assert not hasattr(orch, "shuffle_identities") and not hasattr(orch, "reshuffle_available") and not hasattr(orch, "_guard_reshuffle")


@async_test
async def test_a_takeover_cannot_slip_in_while_the_game_is_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    results = await asyncio.gather(orch.start_or_resume(), orch.takeover("p02"), return_exceptions=True)
    assert orch.session.status == "running"
    assert isinstance(results[1], TakeoverError) and orch.takeover_seat is None  # the start came first
    assert set(orch._supervisor.agents) == {MODERATOR_ID, *IDS}  # type: ignore[union-attr]
    await orch.shutdown()


# ------------------------------------------------------------------- bindings are still needed (F9)
@async_test
async def test_the_seat_of_a_person_still_needs_its_model_binding_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    await orch.takeover("p02")
    orch.session.model_bindings.pop("p02")
    with pytest.raises(NotReadyError) as err:  # (v0.14: the start check lists it)
        await orch.start_or_resume()
    assert [(p["scope"], p["seat"]) for p in err.value.problems] == [("binding", "p02")]
    assert orch.session.status == "idle" and not orch.world.state.dealt
    issues = orch.refresh_binding_issues()
    assert [i.actor_id for i in issues] == ["p02"]
    orch.session.model_bindings["p02"] = all_bound()["p02"]
    await orch.start_or_resume()
    assert orch.session.status == "running" and "p02" not in orch._supervisor.agents  # type: ignore[union-attr]
    await orch.shutdown()


# ---------------------------------------------------------- giving the seat back: the agent starts over
@async_test
async def test_a_given_back_seat_starts_again_from_the_full_board_with_the_persons_words_in_its_history(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch, agents=talk_once())
    orch = table.orch
    table.moderator.plan([*OPEN_PUBLIC, ("send_message", {"channel_id": "public", "content": "欢迎来到本局。"})])
    await orch.start_or_resume()
    await wait_for(lambda: table.calls("p02") >= 1)
    await settle(orch)
    earlier = table.calls("p02")
    assert earlier >= 1 and max(len(m) for m in table.log["p02"]) > 2  # p02's agent had a growing conversation
    await orch.pause("user")

    await orch.takeover("p02")
    await orch.start_or_resume()
    assert "p02" not in orch._supervisor.agents  # type: ignore[union-attr]
    spoken = orch.seat_speak("public", "我是真人，刚才这句话是我自己说的。")
    assert spoken["accepted"]
    await table.run([("send_message", {"channel_id": "public", "content": "第二条公告。"})])
    assert table.calls("p02") == earlier  # nothing was asked of the model for the seat
    await orch.pause("user")

    await orch.release_takeover()
    assert orch.takeover_seat is None
    assert orch._book.get("p02") is None  # its conversation was dropped ...
    await orch.start_or_resume()
    await table.run([("send_message", {"channel_id": "public", "content": "第三条公告：请继续。"})])
    await wait_for(lambda: table.calls("p02") > earlier)
    first_after = table.log["p02"][earlier]
    assert [m["role"] for m in first_after] == ["system", "user"]  # ... so it starts again: ONE message, the full one
    opening = str(first_after[1]["content"])
    assert "【状态板】" in opening and "【可见历史】" in opening
    assert "我是真人，刚才这句话是我自己说的。" in opening and "第二条公告。" in opening  # the person's words are in the history
    assert "人类" not in opening and "接管" not in opening and "真人操作" not in opening  # F6: nobody tells the agent
    await orch.shutdown()


# ----------------------------------------------------------------------------- restart / restore
@async_test
async def test_a_restart_restores_a_paused_game_that_is_still_taken_over(monkeypatch: pytest.MonkeyPatch) -> None:
    first = Table(monkeypatch, agents=talk_once())
    one = first.orch
    first.moderator.plan([*OPEN_PUBLIC, ("send_message", {"channel_id": "public", "content": "欢迎。"})])
    await one.start_or_resume()
    await settle(one)
    await one.pause("user")
    await one.takeover("p03")
    spans = [s.model_dump() for s in one.session.human_spans]
    await one.persist_session("manual")
    saved = one.game_store.data[one.session.session_id]
    assert saved["takeover"] == {"seat": "p03"} and [s["seat"] for s in saved["human_spans"]] == ["p03"]

    second = Table(monkeypatch, agents=talk_once())  # a "new process" over the same storage
    two = second.orch
    two.game_store, two.settings_store = one.game_store, one.settings_store
    report = await two.startup()
    assert report["restored"] == one.session.session_id and report["recovery_error"] is None
    assert two.session.status == "paused" and two.session.restored_from_save
    assert two.takeover_seat == "p03" and [s.model_dump() for s in two.session.human_spans] == spans
    public = two.public_session()
    assert {p["id"]: p["state"] for p in public["participants"]}["p03"] == "human"
    assert public["takeover"]["seat"] == "p03" and public["takeover"]["channels"].keys() >= {"public", "dm-p03"}
    await two.start_or_resume()
    assert "p03" not in two._supervisor.agents and two.session.status == "running"  # type: ignore[union-attr]
    assert two.seat_speak("public", "重启之后我还在。")["accepted"]
    await two.shutdown()


@async_test
async def test_continuing_another_game_from_the_history_keeps_the_seat_played_and_the_game_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_game`` (the history's "继续") and an imported archive go through the same adoption as a restart."""
    table = Table(monkeypatch, agents=talk_once())
    orch = table.orch
    orch._autosave_enabled = True
    table.moderator.plan([*OPEN_PUBLIC, ("send_message", {"channel_id": "public", "content": "欢迎。"})])
    await orch.start_or_resume()
    await settle(orch)
    await orch.pause("user")
    await orch.takeover("p03")
    first_id = orch.session.session_id
    spans = [s.model_dump() for s in orch.session.human_spans]
    await orch.persist_session("manual")

    await orch.new_game()  # another game is live now; nobody plays in it
    assert orch.session.session_id != first_id and orch.takeover_seat is None
    assert orch.public_session()["takeover"] == {"seat": None}
    await orch.takeover("p01")  # ... somebody plays a seat there (it is saved first when the other game is loaded)
    second_id = orch.session.session_id

    loaded = await orch.load_game(first_id)
    assert loaded.session_id == first_id and orch.session.status == "paused" and orch.session.restored_from_save
    assert orch.takeover_seat == "p03"  # the saved takeover is kept, the game did not start by itself
    assert [s.model_dump() for s in orch.session.human_spans] == spans and spans[0]["to_seq"] is None
    public = orch.public_session()
    assert {p["id"]: p["state"] for p in public["participants"]}["p03"] == "human"
    assert {p["id"]: p["state"] for p in public["participants"]}["p01"] == "sleeping"
    assert public["takeover"]["seat"] == "p03" and "reshuffle_available" not in public["takeover"]
    assert "dm-p03" in public["takeover"]["channels"] and orch._takeover_signature == ""
    snapshots = [e for e in orch.bus._ring if e.type == "snapshot"]
    assert snapshots[-1].data["session"]["takeover"]["seat"] == "p03"  # the clients were told with the snapshot

    await orch.start_or_resume()  # playing on: the seat has no agent, its actions are accepted
    assert "p03" not in orch._supervisor.agents  # type: ignore[union-attr]
    assert orch.seat_speak("public", "我又回来了。")["accepted"]
    await orch.pause("user")
    again = await orch.load_game(second_id)  # and the other game still has its own player
    assert again.session_id == second_id and orch.takeover_seat == "p01"
    await orch.shutdown()


def test_a_save_that_contradicts_itself_is_made_consistent_when_it_is_adopted(monkeypatch: pytest.MonkeyPatch) -> None:
    async def adopt(takeover: dict[str, Any], spans: list[dict[str, Any]]) -> GameOrchestrator:
        orch = Table(monkeypatch).orch
        payload = orch.session_payload()
        payload["takeover"] = takeover
        payload["human_spans"] = spans
        loaded = orch._validate_session_data(payload)
        await orch._adopt_loaded_session(loaded, "已导入")
        return orch

    async def scenario() -> None:
        # a seat the game does not have is played by nobody; its open stretch is closed where it began
        ghost = await adopt({"seat": "ghost"}, [{"seat": "ghost", "from_seq": 3, "to_seq": None}])
        assert ghost.takeover_seat is None and ghost.public_session()["takeover"]["seat"] is None
        assert ghost.session.human_spans[0].to_seq == 3
        # an open stretch of a seat that is not the played one is closed as well
        other = await adopt({"seat": "p02"}, [{"seat": "p04", "from_seq": 5, "to_seq": None}, {"seat": "p02", "from_seq": 6, "to_seq": None}])
        assert other.takeover_seat == "p02"
        assert [(s.seat, s.to_seq) for s in other.session.human_spans] == [("p04", 5), ("p02", None)]
        # a played seat always has its open stretch
        bare = await adopt({"seat": "p03"}, [])
        assert bare.takeover_seat == "p03"
        [span] = bare.session.human_spans
        assert (span.seat, span.to_seq) == ("p03", None) and span.from_seq == bare.world.seq
        # nobody played: nothing is invented
        nobody = await adopt({"seat": None}, [{"seat": "p01", "from_seq": 2, "to_seq": 9}])
        assert nobody.takeover_seat is None and [s.model_dump() for s in nobody.session.human_spans] == [{"seat": "p01", "from_seq": 2, "to_seq": 9}]

    asyncio.run(scenario())


@async_test
async def test_the_seat_a_person_played_is_reviewed_like_any_other_with_its_own_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """F8: the review and the record work from the world and the model binding, not from an agent of the seat.  v0.14 (H34): the bot of the
    seat a person played is told so - and only that one: nobody else is told who the person was."""
    ids = ["N1", "N2", "N3", "N4"]  # (the bots of the data directory sit at the seats N1, N2, ...)
    table = Table(monkeypatch, agents=simple_player, moderator=sample_moderator(ids, winner="红队"), ids=ids)
    orch = table.orch
    await orch.takeover("N2")
    person = asyncio.ensure_future(play_as_a_person(orch, "N2", []))
    await orch.start_or_resume()
    await wait_for(lambda: orch.session.status == "finished", timeout=30)
    await person
    assert orch.takeover_seat == "N2" and "N2" not in orch.usage.model_dump(mode="json")["by_actor"]

    port = ScriptedLlm(by_kind())
    orch.llm_port_factory = lambda o: port
    result = await orch.start_review()
    assert sorted(result["started"]) == sorted(ids) and result["deadline_s"] == 60  # the played seat is part of "review all"
    await wait_for(lambda: len(orch.session.review_done) == len(ids) and not orch.review_running_ids())
    assert sorted(port.counts) == sorted(ids)  # every seat was asked - as the seat, with its own model
    first = {seat: next(m for who, m in port.requests if who == seat)[1]["content"] for seat in ids}
    assert "有人代你出场" in first["N2"] and all("有人代你出场" not in first[seat] for seat in ids if seat != "N2")
    assert "人类" not in "".join(first[seat] for seat in ids if seat != "N2")  # (nobody else learns there was a person)
    store = orch._bots()
    bot_id = next(p.bot_id for p in orch.session.participants if p.id == "N2")
    assert "Bob" in store.read_part(bot_id, "people")  # what the review wrote is in its memory, at once
    won = next(p for p in orch.world.state.players if p.id == "N2").faction == "red"
    assert f"场次 1 · 胜 {1 if won else 0}" in store.summary(bot_id)["memory_stats"]["record"]  # the record is kept for it as for everybody
    assert orch.usage.actor("N2").calls == 3  # the review is the only thing that was ever accounted for the seat
    states = [e.data["state"] for e in orch.bus._ring if e.type == "review.progress"]
    assert states.count("done") == len(ids) and "error" not in states
    assert orch.review_view()["done"] == sorted(ids) and orch.review_view()["running"] is False
    await orch.shutdown()


@async_test
async def test_a_seat_state_that_cannot_be_computed_never_breaks_the_snapshot_and_offers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    await orch.takeover("p02")

    def boom(world: Any, seat: str) -> dict[str, Any]:
        raise RuntimeError("a bug in the seat state")

    monkeypatch.setattr(takeover_module, "seat_status", boom)
    public = orch.public_session()  # still a whole document: /api/state and the SSE snapshot keep working
    json.dumps(public)
    assert public["takeover"] == {
        "seat": "p02", "seq": orch.world.seq,
        "channels": {}, "requests": [], "abilities": [], "attention": [],
    }
    base = orch.bus.last_id
    orch._push_takeover(force=True)  # ... and so does the push (nothing raised, the page shows "禁言中")
    assert pushes(orch, base)[-1]["channels"] == {}
    await orch.shutdown()


# --------------------------------------------------------------------------------- the whole picture
@async_test
async def test_public_session_says_who_is_played_and_is_json(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch)
    orch = table.orch
    assert orch.public_session()["takeover"] == {"seat": None}
    await orch.takeover("p04")
    public = orch.public_session()
    json.dumps(public)
    takeover = public["takeover"]
    assert set(takeover) == {"seat", "seq", "channels", "requests", "abilities", "attention"}
    assert takeover["seat"] == "p04" and takeover["seq"] == orch.world.seq and set(takeover["channels"]) == {"dm-p04"}
    assert {p["id"]: p["state"] for p in public["participants"]}["p04"] == "human"
    assert strip_clock(takeover) == strip_clock({**seat_status(orch.world, "p04"), "seat": "p04"})
    await orch.release_takeover()
    assert orch.public_session()["takeover"] == {"seat": None}
    assert {p["id"]: p["state"] for p in orch.public_session()["participants"]}["p04"] == "sleeping"
    # a new game is nobody's: a person who plays is out of it
    await orch.takeover("p04")
    await orch.new_game()
    assert orch.takeover_seat is None and orch.public_session()["takeover"]["seat"] is None


# ------------------------------------------------------------------------------ a whole game, played
def sample_moderator(ids: list[str] = IDS, winner: str = "测试队") -> Callable[[Any, str], list[Call]]:
    """Opens the table, runs one speaking round, one sealed vote, announces the result, ends the game
    (``winner``: a faction name of the scenario - ``红队`` / ``蓝队`` - gives every player a win or a loss)."""
    state = {"stage": 0}

    def policy(world: Any, me: str) -> list[Call]:
        ledger = world.entries_visible(MODERATOR_ID)
        seen = {e.type for e in ledger}
        stage = state["stage"]
        if stage == 0:
            state["stage"] = 1
            return [
                ("create_channel", {"channel_id": "public", "name": "公共频道", "members": ids, "type": "public"}),
                ("set_phase", {"text": "第 1 轮 · 发言"}),
                ("set_floor_queue", {"channel_id": "public", "order": ids, "limits": {"max_messages": 1, "duration_s": 60}}),
            ]
        if stage == 1 and "floor.completed" in seen:
            state["stage"] = 2
            return [
                ("open_request", {"to": ids, "kind": "choice", "question": "请投票", "sealed": True, "duration_s": 60,
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
                ("end_game", {"winner": winner, "summary": "一轮即终"}),
            ]
        return []

    return policy


async def play_as_a_person(orch: GameOrchestrator, seat: str, log: list[tuple[str, dict[str, Any]]]) -> None:
    """A person at ``seat``: speaks when the floor is theirs and answers what is asked - through the same calls the
    HTTP layer makes, looking at nothing but ``takeover_payload()``."""
    while orch.session.status != "finished":
        await asyncio.sleep(0.01)
        if orch.session.status != "running" or orch.takeover_seat != seat:
            continue
        state = orch.takeover_payload()
        for channel_id, channel in state["channels"].items():
            floor = channel["floor"]
            if floor and floor["mine"] and channel["can_write"]:
                log.append(("speak", orch.seat_speak(channel_id, f"我是真人，坐在 {seat}。", state["seq"])))
        for request in state["requests"]:
            if not request["answered"]:
                options = [o["id"] for o in orch.world.state.requests[request["id"]].options]
                log.append(("respond", orch.seat_respond(request["id"], options[-1], "我的理由")))


@async_test
async def test_a_whole_game_is_played_to_the_end_with_a_person_at_one_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Table(monkeypatch, agents=simple_player, moderator=sample_moderator())
    orch = table.orch
    await orch.takeover("p02")
    log: list[tuple[str, dict[str, Any]]] = []
    person = asyncio.ensure_future(play_as_a_person(orch, "p02", log))
    await orch.start_or_resume()
    await wait_for(lambda: orch.session.status == "finished", timeout=30)
    await person

    speeches = [(e.actor_id, e.data["content"]) for e in orch.world.ledger if e.type == "message" and e.data.get("kind") == "speech"]
    assert [who for who, _ in speeches] == IDS  # one speech each, in order - the seat's is the person's
    assert dict(speeches)["p02"] == "我是真人，坐在 p02。"
    votes = {e.actor_id: e.data["value"] for e in orch.world.ledger if e.type == "request.answered"}
    assert set(votes) == set(IDS) and votes["p02"] == IDS[-1]
    assert orch.world.state.winner == "测试队"
    assert [kind for kind, receipt in log if not receipt["accepted"]] == []
    assert Counter(kind for kind, _ in log) == {"speak": 1, "respond": 1}
    # no model was asked anything for the seat, nothing was counted for it
    assert table.calls("p02") == 0 and all(table.calls(i) >= 1 for i in ("p01", "p03", "p04"))
    assert "p02" not in orch.public_session()["usage_stats"]["by_actor"]
    final = orch.public_session()
    assert final["status"] == "finished" and final["takeover"]["seat"] == "p02"
    assert {p["id"]: p["state"] for p in final["participants"]}["p02"] == "human"
    assert [(s.seat, s.to_seq) for s in orch.session.human_spans] == [("p02", None)]
    await orch.release_takeover()  # a finished game can give the seat back
    assert orch.session.human_spans[0].to_seq == orch.world.seq
    await orch.shutdown()
