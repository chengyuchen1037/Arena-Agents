"""The game module together with the real chat module (what ``World`` will assemble).

Locks the pieces that cross the module boundary: the ability freeze (D25), the state board against the
real ``speech_gate`` of the commit gate, the punished system DM, and persistence of both modules.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.world.board import build_board
from app.world.chat import ChatModule, speech_gate
from app.world.core import WorldCore
from app.world.game import GameModule
from app.world.projection import world_snapshot
from app.world.spec import ScenarioSpec
from app.world.state import PlayerState, WorldState
from app.world.types import MODERATOR_ID, Code, LedgerEntry, Receipt

from .test_game_kit import PLAYER_IDS, PLAYER_NAMES, ManualClock, ask, make_spec, mod, role_holder


class GameChatWorld(WorldCore):
    """``WorldCore`` + ``ChatModule`` + ``GameModule`` (the same assembly as ``app.world.world.World``)."""

    chat: ChatModule
    game: GameModule

    def __init__(
        self,
        spec: ScenarioSpec,
        clock: Any,
        *,
        state: WorldState | None = None,
        ledger: list[LedgerEntry] | None = None,
        seed: int | None = None,
    ) -> None:
        super().__init__(spec, clock, state=state, ledger=ledger, seed=seed)
        self.chat = ChatModule(self)
        self.game = GameModule(self)
        self.add_module(self.chat)
        self.add_module(self.game)


def make_full_world(*, spec: ScenarioSpec | None = None, seed: int = 7) -> GameChatWorld:
    players = [PlayerState(id=pid, seat=str(i), name=PLAYER_NAMES[pid]) for i, pid in enumerate(PLAYER_IDS, start=1)]
    world = GameChatWorld(spec or make_spec(), ManualClock(), state=WorldState(players=players), seed=seed)
    world.chat.create_system_dms()
    world.game.deal()
    world.set_running(True, "start")
    receipt = mod(world, "create_channel", channel_id="public", name="公共频道", members=PLAYER_IDS, type="public", mode="free")
    assert receipt.ok, receipt
    world.drain_outbox()
    world.drain_events()
    return world


def speak(world: WorldCore, actor: str, channel_id: str, content: str = "你好") -> Receipt:
    return ask(world, actor, "speak", channel_id=channel_id, content=content)


def restore(world: GameChatWorld) -> GameChatWorld:
    data = world.dump()
    state = WorldState.model_validate(data["state"])
    state.status = "paused"
    clone = GameChatWorld(
        world.spec,
        ManualClock(data["clock_now"]),
        state=state,
        ledger=[LedgerEntry.model_validate(e) for e in data["ledger"]],
    )
    for module in clone.modules:
        module.load(data["modules"].get(module.name, {}))
    return clone


# --------------------------------------------------------------------------- assembly


def test_modules_register_without_clashes_and_the_intent_catalogue_is_complete():
    from app.world.intents import INTENT_MODELS

    world = make_full_world()
    assert set(world._handlers) == set(INTENT_MODELS)
    assert [m.name for m in world.modules] == ["chat", "game"]
    # the system DMs created by the chat module are found by the game module
    assert {c.id for c in world.state.channels if c.system} == {f"dm-{p}" for p in PLAYER_IDS}
    assert all(p.role for p in world.state.players)


# --------------------------------------------------------------------------- ability freeze (D25)


def test_ability_freezes_a_running_queue_and_reveals_despite_not_holding_the_floor():
    world = make_full_world()
    holder = role_holder(world, "alpha")
    speakers = [p for p in PLAYER_IDS if p != holder]
    assert mod(world, "set_floor_queue", channel_id="public", order=speakers).ok
    mod(world, "enable_ability", ability_id="strike")
    world.drain_outbox()
    world.drain_events()
    current, waiting = speakers[0], speakers[1]

    assert speak(world, holder, "public").code == Code.NO_FLOOR  # not the speaker
    receipt = ask(world, holder, "use_ability", ability_id="strike", params={"target": current})
    assert receipt.ok

    public = world.channel("public")
    assert public.frozen is True and public.group is not None and public.group.paused is True
    reveal = [e for e in world.ledger if e.type == "message" and e.data["kind"] == "ability"]
    assert len(reveal) == 1 and reveal[0].actor_id == holder and reveal[0].channel_id == "public"
    types = [e.type for e in world.ledger if e.seq > receipt.seq - 1]
    assert types.index("ability.used") < types.index("message") < types.index("floor.frozen")
    frozen = [e for e in world.ledger if e.type == "floor.frozen"][0]
    assert frozen.data["reason"] == "ability:strike" and frozen.data["channel_id"] == "public"

    items = world.drain_outbox()
    told = [d for d in items if d.envelope.payload.get("event") == "floor.frozen"]
    assert [d.target for d in told] == [current]  # only the one who could write is interrupted
    assert (told[0].envelope.priority, told[0].envelope.interrupt) == ("normal", True)
    woken = [d for d in items if d.target == MODERATOR_ID and d.envelope.payload.get("event") == "ability.used"]
    assert [d.envelope.priority for d in woken] == ["urgent"]

    # the speaker lost the floor; races end in NO_FLOOR
    late = speak(world, current, "public")
    assert late.code == Code.NO_FLOOR and "冻结" in late.message
    board = build_board(world, current)
    channel = next(c for c in board["channels"] if c["id"] == "public")
    assert channel["can_write"] is False and channel["frozen"] is True and channel["why_not"].startswith("NO_FLOOR")

    # the moderator lifts the freeze and the queue goes on
    assert mod(world, "resume_queue", channel_id="public").ok
    assert world.channel("public").frozen is False
    assert speak(world, current, "public").ok
    assert waiting in [t.holder for t in world.channel("public").group.tokens]


def test_ability_freezes_a_free_discussion_and_interrupts_its_writers():
    world = make_full_world()
    holder = role_holder(world, "alpha")
    mod(world, "enable_ability", ability_id="strike")
    world.drain_outbox()
    assert ask(world, holder, "use_ability", ability_id="strike", params={"target": "u1" if holder != "u1" else "u2"}).ok
    assert world.channel("public").frozen is True
    items = world.drain_outbox()
    interrupted = sorted(d.target for d in items if d.envelope.payload.get("event") == "floor.frozen")
    assert interrupted == sorted(PLAYER_IDS)
    for p in PLAYER_IDS:
        assert speak(world, p, "public").code == Code.NO_FLOOR
    assert speak(world, "u1", "dm-u1").ok  # private chats are not frozen


# --------------------------------------------------------------------------- board vs. commit gate


def _free_restricted(world):
    assert mod(world, "open_floor", channel_id="public", mode="free", holders=["u1", "u2", "u3"]).ok
    assert mod(world, "update_channel", channel_id="public", readonly_members=["u3"]).ok


def _free_quota(world):
    assert mod(world, "open_floor", channel_id="public", mode="free", free_limits={"max_messages_per_member": 1}).ok
    assert speak(world, "u1", "public").ok


def _queue(world):
    assert mod(world, "set_floor_queue", channel_id="public", order=["u1", "u2", "u3"]).ok


def _queue_after_pass(world):
    _queue(world)
    assert ask(world, "u1", "pass", channel_id="public").ok


def _queue_paused(world):
    _queue(world)
    assert mod(world, "pause_queue", channel_id="public").ok


def _queue_frozen(world):
    _queue(world)
    assert world.chat.freeze_channel("public", "test") is True


def _turn(world):
    assert mod(world, "open_floor", channel_id="public", mode="turn", holders=["u1", "u2"]).ok


def _closed(world):
    assert mod(world, "close_floor", channel_id="public").ok


def _listen(world):
    assert mod(world, "update_channel", channel_id="public", mode="listen").ok


def _dm_closed(world):
    assert mod(world, "update_channel", channel_id="dm-u1", mode="closed").ok


def _frozen_free(world):
    assert world.chat.freeze_channel("public", "test") is True


def _side_channel(world):
    assert mod(world, "create_channel", channel_id="side", name="旁路", members=["u2", "u3"], mode="free").ok


SCENARIOS = [
    pytest.param(lambda w: None, id="free-default"),
    pytest.param(_free_restricted, id="free-writers-and-readonly"),
    pytest.param(_free_quota, id="free-quota-used"),
    pytest.param(_queue, id="queue"),
    pytest.param(_queue_after_pass, id="queue-after-pass"),
    pytest.param(_queue_paused, id="queue-paused"),
    pytest.param(_queue_frozen, id="queue-frozen"),
    pytest.param(_turn, id="turn"),
    pytest.param(_closed, id="closed"),
    pytest.param(_listen, id="listen"),
    pytest.param(_dm_closed, id="dm-closed"),
    pytest.param(_frozen_free, id="free-frozen"),
    pytest.param(_side_channel, id="side-channel"),
]


@pytest.mark.parametrize("setup", SCENARIOS)
def test_board_can_write_and_why_not_agree_with_the_commit_gate(setup):
    world = make_full_world()
    setup(world)
    checked = 0
    for agent in [MODERATOR_ID, *PLAYER_IDS]:
        board = build_board(world, agent)
        listed = {c["id"] for c in board["channels"]}
        assert listed == {c.id for c in world.state.channels if agent in c.members}
        for block in board["channels"]:
            gate = speech_gate(world, world.channel(block["id"]), agent)
            allowed = not isinstance(gate, Receipt)
            assert block["can_write"] is allowed, (agent, block["id"], block["why_not"])
            if allowed:
                assert block["why_not"] is None
            else:
                assert block["why_not"].partition("：")[0] == gate.code, (agent, block["id"], block["why_not"], gate.message)
                assert block["why_not"].partition("：")[2]
            checked += 1
    assert checked >= 5 * 2


def test_board_floor_numbers_follow_the_real_token_accounting():
    world = make_full_world()
    assert mod(world, "set_floor_queue", channel_id="public", order=["u1", "u2"], limits={"max_messages": 3, "max_chars_total": 100, "max_chars_per_message": 40, "duration_s": 30}).ok
    assert speak(world, "u1", "public", "字" * 10).ok
    world.clock.advance(7)
    block = next(c for c in build_board(world, "u1")["channels"] if c["id"] == "public")
    assert block["floor"]["mine"] == {"messages_left": 2, "chars_per_message": 40, "chars_left": 90, "seconds_left": 23.0}
    # the chat module's own view agrees on who is speaking and for how long
    from app.world.chat import floor_view

    view = floor_view(world, world.channel("public"))
    assert view["active"] == block["floor"]["active"] == "u1" and view["seconds_left"] == block["floor"]["seconds_left"]
    assert view["queue"] == block["floor"]["queue"]


# --------------------------------------------------------------------------- challenge / punished DM


def test_punished_dm_blocks_speech_and_challenges_but_not_the_moderator_or_respond():
    world = make_full_world()
    assert ask(world, "u1", "challenge", content="第一条质询").ok
    assert mod(world, "update_channel", channel_id="dm-u1", mode="closed").ok

    assert ask(world, "u1", "challenge", content="再来一条").code == Code.CHANNEL_CLOSED
    assert speak(world, "u1", "dm-u1").code == Code.CHANNEL_CLOSED
    assert ask(world, "u2", "challenge", content="别人不受影响").ok

    # the moderator can still answer in the closed DM, and a request does not need the DM at all
    assert mod(world, "resolve_challenge", ticket_id="t1", verdict="rejected", reply="请不要重复质询。").ok
    rid = mod(world, "open_request", to=["u1"], kind="text", question="你的看法？").data["request_id"]
    assert ask(world, "u1", "respond", request_id=rid, value="没有意见").ok

    assert mod(world, "update_channel", channel_id="dm-u1", mode="free").ok
    assert ask(world, "u1", "challenge", content="恢复后可以了").ok
    assert speak(world, "u1", "dm-u1").ok


def test_challenge_and_reply_reach_the_right_mailboxes_with_the_real_message_priority():
    world = make_full_world()
    ask(world, "u3", "challenge", content="有问题")
    items = world.drain_outbox()
    assert [(d.target, d.envelope.kind, d.envelope.priority) for d in items] == [(MODERATOR_ID, "challenge", "urgent")]
    mod(world, "resolve_challenge", ticket_id="t1", verdict="upheld", reply="成立")
    items = world.drain_outbox()
    assert [(d.target, d.envelope.kind, d.envelope.priority) for d in items] == [("u3", "challenge", "urgent")]
    # an ordinary DM message from the player is only NORMAL for the moderator, and the moderator's is NORMAL back
    speak(world, "u3", "dm-u3", "普通私聊")
    assert [(d.target, d.envelope.priority) for d in world.drain_outbox()] == [(MODERATOR_ID, "normal")]


# --------------------------------------------------------------------------- snapshot with the real views


def test_snapshot_uses_the_real_channel_views():
    world = make_full_world()
    assert mod(world, "set_floor_queue", channel_id="public", order=["u1", "u2"]).ok
    speak(world, "u1", "public", "开场")
    snap = world_snapshot(world)
    public = next(c for c in snap["channels"] if c["id"] == "public")
    assert public["mode"] == "queue" and public["floor"]["kind"] == "queue" and public["floor"]["active"] == "u1"
    assert [c["id"] for c in snap["channels"]] == [c.id for c in world.state.channels]
    assert any(m["content"] == "开场" for m in snap["messages"])
    dm = next(c for c in snap["channels"] if c["id"] == "dm-u1")
    assert dm["system"] is True and dm["owner"] == "u1"


# --------------------------------------------------------------------------- persistence


def test_both_modules_survive_dump_and_load_and_resume_cleanly():
    world = make_full_world()
    holder = role_holder(world, "alpha")
    mod(world, "enable_ability", ability_id="strike")
    assert mod(world, "open_request", to=["u1", "u2"], kind="choice", question="？", options=[{"id": "a"}, {"id": "b"}], sealed=True).ok
    assert ask(world, "u1", "respond", request_id="r1", value="a").ok
    assert ask(world, "u2", "challenge", content="未结工单").ok
    assert ask(world, holder, "use_ability", ability_id="strike", params={"target": "u2" if holder != "u2" else "u1"}).ok
    world.game.write_note("u1", "记录", "第一条")
    world.set_running(False, "paused")

    clone = restore(world)
    assert clone.state.requests["r1"].status == "open" and clone.state.tickets["t1"].status == "open"
    assert clone.state.abilities["strike"].uses == {holder: 1}
    assert clone.channel("public").frozen is True
    assert clone.game.read_notes_text("u1") == "#### 📌 记录\n第一条"
    assert clone.seq == world.seq and clone.state.rng.state == world.state.rng.state

    clone.set_running(True, "resume")
    clone.drain_outbox()
    clone.redeliver()
    items = clone.drain_outbox()
    wakes = sorted((d.target, d.envelope.payload["event"]) for d in items if d.envelope.priority == "urgent")
    assert wakes == [(MODERATOR_ID, "challenge.opened"), ("u2", "request.opened")]
    assert ask(clone, "u2", "respond", request_id="r1", value="b").ok  # closes the request after the restore
    assert clone.state.requests["r1"].status == "closed"
    # the draw sequence continues exactly where the original left off
    assert [clone.rng.random() for _ in range(3)] == [world.rng.random() for _ in range(3)]


def test_request_deadline_after_restore_uses_the_restored_clock():
    world = make_full_world()
    assert mod(world, "open_request", to=["u1"], kind="text", question="？", duration_s=30).ok
    world.clock.advance(12)
    clone = restore(world)
    clone.set_running(True, "resume")
    assert clone.clock.now() == 12.0 and clone.next_deadline() == 30.0
    clone.clock.advance(18)
    clone.tick()
    assert clone.state.requests["r1"].status == "closed"
    closed = [e for e in clone.ledger if e.type == "request.closed"][0]
    assert closed.data["reason"] == "deadline" and closed.data["missing"] == ["u1"]
