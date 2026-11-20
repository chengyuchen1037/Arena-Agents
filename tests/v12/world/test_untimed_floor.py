"""Public floors without a deadline for the seat a person plays (v0.13.1, G10-G15, contract section 12.2).

The world knows ``untimed_holders`` (set by the orchestrator from the takeover state, never saved).  A token held by one
of them in a channel of ``type == "public"`` has no deadline: ``deadline is None``, ``remaining`` keeps its nominal time,
``floor.granted`` says ``untimed: true``, ``FloorView.untimed`` / ``seat_status(...).floor.untimed`` tell the client.
Everything else keeps its clock - other channels, other holders, free discussions, requests - and the message / character
limits of the token stay.  The state board and ``get_ledger`` must not give the person away (G15).
"""

from __future__ import annotations

import copy
import json

import pytest

from app.game.replay import build_replay
from app.world.board import build_board
from app.world.chat import floor_view, token_untimed
from app.world.seat import seat_status
from app.world.types import MODERATOR_ID, Code

from .helpers import (
    ChatWorld,
    ManualClock,
    do_pass,
    entries,
    envelopes,
    flush,
    make_world,
    mod,
    new_channel,
    ok,
    open_free,
    open_queue,
    open_turn,
    speak,
    ui,
)
from .test_game_integration import make_full_world
from .test_game_integration import speak as full_speak
from .test_game_kit import mod as full_mod
from .test_replay import IDS, Run, fold_channels, payload_of


def public_world(holders: tuple[str, ...] = ("N2",)) -> ChatWorld:
    """Four players, a public channel ``pub`` and a group channel ``team``; ``holders`` are played by a person."""
    world = make_world()
    world.set_untimed_holders(holders)
    new_channel(world, "pub", type_="public")
    new_channel(world, "team", members=["N1", "N2"], type_="group")
    return world


def token(world: ChatWorld, channel_id: str, holder: str):
    group = world.channel(channel_id).group
    assert group is not None, channel_id
    return next(t for t in group.tokens if t.holder == holder)


# =========================================================================== the token

def test_a_played_seat_gets_a_public_floor_without_a_deadline_and_an_agent_keeps_its_clock() -> None:
    world = public_world()
    open_queue(world, "pub", ["N1", "N2"], duration_s=30, max_messages=2)
    agent = token(world, "pub", "N1")
    assert agent.status == "active" and agent.deadline == 30.0 and agent.remaining is None
    assert ok(do_pass(world, "N1", "pub")) and token(world, "pub", "N2").status == "active"

    person = token(world, "pub", "N2")
    assert person.deadline is None and person.remaining == 30.0  # no deadline; the nominal time is kept for a release
    assert token_untimed(world, world.channel("pub"), person) is True
    granted = entries(world, "floor.granted", channel="pub")
    assert [e.data.get("untimed") for e in granted] == [None, True]  # only the person's grant says so
    assert world.next_deadline() is None and world.untimed_floor_open() is True

    world.clock.advance(100_000)  # however long they think ...
    world.tick()
    assert token(world, "pub", "N2").status == "active"  # ... nobody takes the floor away
    assert entries(world, "floor.token_ended", channel="pub")[-1].data["holder"] == "N1"


def test_the_limits_of_an_untimed_token_still_hold() -> None:
    world = public_world()
    open_queue(world, "pub", ["N2", "N1"], duration_s=30, max_messages=2, max_chars_per_message=10)
    refused = speak(world, "N2", "pub", "字" * 25)
    assert refused.code == Code.TOO_LONG and refused.data == {"chars": 25, "limit": 10}  # the message cap is unchanged
    assert token(world, "pub", "N2").messages_used == 0 and token(world, "pub", "N2").status == "active"
    assert ok(speak(world, "N2", "pub", "字" * 10)).truncated is None  # the first message
    world.clock.advance(5000)
    assert ok(speak(world, "N2", "pub", "第二条"))  # the second (and last) message
    ended = entries(world, "floor.token_ended", channel="pub")
    assert ended[-1].data["holder"] == "N2" and ended[-1].data["reason"] == "used"  # it ends by use, not by time
    assert token(world, "pub", "N1").status == "active"  # and the queue moves on
    assert speak(world, "N2", "pub", "还想说").code == Code.NO_FLOOR


def test_the_floor_view_says_untimed_and_draws_no_countdown() -> None:
    world = public_world()
    open_turn(world, "pub", ["N1", "N2"], duration_s=30)
    mixed = floor_view(world, world.channel("pub"))
    assert mixed["untimed"] is False and mixed["seconds_left"] == 30.0  # an agent's countdown is still running
    assert ok(do_pass(world, "N1", "pub"))
    only_person = floor_view(world, world.channel("pub"))
    assert only_person["active"] == "N2" and only_person["untimed"] is True and only_person["seconds_left"] is None
    # the view is in the channel views of the snapshot and in the events, too
    flush(world)
    assert ok(speak(world, "N2", "pub", "嗯"))
    _, events = flush(world)
    assert ui(events, "floor.changed")[-1]["floor"]["untimed"] is True


def test_other_channels_other_holders_and_free_discussions_keep_their_deadlines() -> None:
    world = public_world()
    open_queue(world, "team", ["N2"], duration_s=40)  # a group channel: the person's floor there is timed
    team = token(world, "team", "N2")
    assert team.deadline == 40.0 and team.remaining is None
    assert floor_view(world, world.channel("team"))["untimed"] is False
    assert [e.data.get("untimed") for e in entries(world, "floor.granted", channel="team")] == [None]
    assert world.untimed_floor_open() is False

    open_queue(world, "pub", ["N3"], duration_s=20)  # an agent in the public channel: timed
    assert token(world, "pub", "N3").deadline == 20.0
    assert world.untimed_floor_open() is False

    open_free(world, "pub", duration_s=50)  # a free discussion is not a floor token: its timers are unchanged
    assert world.next_deadline() is not None and world.untimed_floor_open() is False
    assert floor_view(world, world.channel("pub"))["untimed"] is False
    world.clock.advance(41)
    world.tick()
    assert entries(world, "floor.token_ended", channel="team")[-1].data["reason"] == "timeout"


def test_a_channel_counts_as_public_by_its_type_and_never_by_its_id() -> None:
    world = make_world()
    world.set_untimed_holders(["N2"])
    new_channel(world, "town-square", type_="group")  # (a name that sounds public is not enough)
    new_channel(world, "lobby", type_="public")
    open_queue(world, "town-square", ["N2"], duration_s=30)
    open_queue(world, "lobby", ["N2"], duration_s=30)
    assert token(world, "town-square", "N2").deadline == 30.0
    assert token(world, "lobby", "N2").deadline is None


# =========================================================================== pause, resume, freeze

def test_pausing_and_resuming_a_group_respect_an_untimed_token() -> None:
    world = public_world()
    open_queue(world, "pub", ["N2", "N1"], duration_s=30)
    person = token(world, "pub", "N2")
    world.clock.advance(100)
    ok(mod(world, "pause_queue", channel_id="pub"))
    assert person.deadline is None and person.remaining == 30.0  # nothing to freeze: it keeps the nominal time
    paused = floor_view(world, world.channel("pub"))
    assert paused["paused"] is True and paused["untimed"] is True and paused["seconds_left"] is None
    assert world.untimed_floor_open() is False  # a paused floor is the moderator's to resume: the watchdog may poke

    world.clock.advance(100)
    flush(world)
    ok(mod(world, "resume_queue", channel_id="pub"))
    assert person.deadline is None and person.remaining == 30.0  # resuming does not start a timer
    assert world.untimed_floor_open() is True
    deliveries, _ = flush(world)
    assert len(envelopes(deliveries, target="N2", event="floor.granted")) == 1  # the holder is told again, as always
    world.clock.advance(10_000)
    world.tick()
    assert person.status == "active"


def test_a_frozen_channel_keeps_the_untimed_token_and_the_thaw_does_not_start_a_timer() -> None:
    world = public_world()
    open_queue(world, "pub", ["N2"], duration_s=30)
    assert world.chat.freeze_channel("pub", "test") is True
    person = token(world, "pub", "N2")
    assert person.deadline is None and person.remaining == 30.0 and world.untimed_floor_open() is False
    ok(mod(world, "resume_queue", channel_id="pub"))
    assert person.deadline is None and world.untimed_floor_open() is True


def test_a_token_handed_over_while_the_queue_is_paused_is_untimed_after_the_resume() -> None:
    world = public_world()
    open_queue(world, "pub", ["N1", "N2"], duration_s=30)
    ok(mod(world, "pause_queue", channel_id="pub"))
    assert do_pass(world, "N1", "pub").code == Code.NO_FLOOR  # a paused queue waits for the moderator
    # a member who is removed loses the floor even then: the next token is activated *inside* the paused group
    ok(mod(world, "update_channel", channel_id="pub", remove_members=["N1"]))
    person = token(world, "pub", "N2")
    assert person.status == "active" and person.deadline is None and person.remaining == 30.0
    assert entries(world, "floor.granted", channel="pub")[-1].data["untimed"] is True
    flush(world)
    ok(mod(world, "resume_queue", channel_id="pub"))
    assert person.deadline is None and world.untimed_floor_open() is True
    world.clock.advance(10_000)
    world.tick()
    assert person.status == "active"


def test_a_token_handed_over_by_a_pass_is_untimed_from_the_start() -> None:
    world = public_world()
    open_queue(world, "pub", ["N1", "N2"], duration_s=30)
    assert ok(do_pass(world, "N1", "pub"))
    person = token(world, "pub", "N2")
    assert person.status == "active" and person.deadline is None
    assert entries(world, "floor.granted", channel="pub")[-1].data["untimed"] is True


# =========================================================================== the moderator's rules still work (G12)

def test_closing_the_floor_of_the_person_works_like_for_anybody_else_and_nobody_waits_any_more() -> None:
    world = public_world()
    open_queue(world, "pub", ["N2", "N1"], duration_s=30)
    assert world.untimed_floor_open() is True and floor_view(world, world.channel("pub"))["untimed"] is True
    flush(world)

    assert ok(mod(world, "close_floor", channel_id="pub"))  # no special treatment: a closed floor is closed (G12)
    ended = entries(world, "floor.token_ended", channel="pub")
    assert ended[-1].data["holder"] == "N2" and ended[-1].data["reason"] == "revoked"
    deliveries, events = flush(world)
    assert len(envelopes(deliveries, target="N2", event="floor.revoked")) == 1  # the holder is told, as anybody is
    view = floor_view(world, world.channel("pub"))
    assert view["kind"] == "none" and view["untimed"] is False and view["seconds_left"] is None
    assert ui(events, "floor.changed")[-1]["floor"]["untimed"] is False
    assert world.untimed_floor_open() is False and world.next_deadline() is None  # nothing is waited for any more
    state = seat_status(world, "N2")["channels"]["pub"]
    assert state["can_write"] is False and state["code"] in (Code.NO_FLOOR, Code.CHANNEL_CLOSED) and state["can_pass"] is False
    assert speak(world, "N2", "pub", "还没说完").code in (Code.NO_FLOOR, Code.CHANNEL_CLOSED)


def test_replacing_the_floor_of_the_person_by_a_new_queue_revokes_it_and_the_new_token_is_judged_afresh() -> None:
    world = public_world()
    open_queue(world, "pub", ["N2"], duration_s=30)
    old = token(world, "pub", "N2")
    flush(world)
    open_queue(world, "pub", ["N1", "N2"], duration_s=20)  # the moderator sets a new queue: N1 first, the person second
    assert old.status == "done" and old.ended_reason == "revoked"
    assert token(world, "pub", "N1").deadline == 20.0  # an agent's turn: timed
    assert floor_view(world, world.channel("pub"))["untimed"] is False and world.untimed_floor_open() is False
    assert ok(do_pass(world, "N1", "pub"))
    person = token(world, "pub", "N2")
    assert person.deadline is None and person.remaining == 20.0 and world.untimed_floor_open() is True


# =========================================================================== taking over / giving back

def test_taking_over_a_seat_that_already_holds_a_floor_freezes_its_remaining_time_and_giving_back_restores_it() -> None:
    world = make_world()
    new_channel(world, "pub", type_="public")
    open_queue(world, "pub", ["N2"], duration_s=30)
    world.clock.advance(10)
    flush(world)
    seq = world.seq
    person = token(world, "pub", "N2")
    assert person.deadline == 30.0

    world.set_untimed_holders(["N2"])  # a person takes the seat over (the game is paused: the clock stands still)
    assert person.deadline is None and person.remaining == 20.0
    assert world.seq == seq  # nothing is written to the ledger: nobody is told
    _, events = flush(world)
    assert ui(events, "floor.changed")[-1]["floor"]["untimed"] is True
    world.clock.advance(1000)
    world.tick()
    assert person.status == "active" and world.untimed_floor_open() is True

    world.set_untimed_holders([])  # given back: timed again, with exactly the time it had left
    assert person.deadline == 1010.0 + 20.0 and person.remaining is None
    _, events = flush(world)
    assert ui(events, "floor.changed")[-1]["floor"]["untimed"] is False
    assert world.seq == seq and world.untimed_floor_open() is False
    world.clock.advance(19)
    world.tick()
    assert person.status == "active"
    world.clock.advance(2)
    world.tick()
    assert entries(world, "floor.token_ended", channel="pub")[-1].data["reason"] == "timeout"


def test_taking_over_a_seat_whose_group_is_paused_only_changes_the_view() -> None:
    world = make_world()
    new_channel(world, "pub", type_="public")
    open_queue(world, "pub", ["N2"], duration_s=30)
    world.clock.advance(12)
    ok(mod(world, "pause_queue", channel_id="pub"))
    person = token(world, "pub", "N2")
    assert person.deadline is None and person.remaining == 18.0
    flush(world)
    world.set_untimed_holders(["N2"])
    assert person.deadline is None and person.remaining == 18.0  # already frozen: the numbers stay
    _, events = flush(world)
    assert ui(events, "floor.changed")[-1]["floor"]["untimed"] is True
    ok(mod(world, "resume_queue", channel_id="pub"))
    assert person.deadline is None  # untimed after the resume
    world.set_untimed_holders([])
    assert person.deadline == world.clock.now() + 18.0  # and timed again with what it had left


def test_only_the_floors_of_the_seat_that_changed_are_touched() -> None:
    world = make_world()
    new_channel(world, "pub", type_="public")
    new_channel(world, "team", members=["N1", "N2"], type_="group")
    open_queue(world, "pub", ["N1"], duration_s=30)
    open_queue(world, "team", ["N2"], duration_s=30)
    flush(world)
    world.set_untimed_holders(["N2"])
    assert token(world, "pub", "N1").deadline == 30.0  # an agent's
    assert token(world, "team", "N2").deadline == 30.0  # in a group channel
    assert flush(world)[1] == []  # nothing changed for anybody: no event


def test_setting_the_same_holders_again_does_nothing_and_a_failing_module_hook_is_isolated() -> None:
    world = public_world()
    open_queue(world, "pub", ["N2"], duration_s=30)
    flush(world)
    before = copy.deepcopy(world.state.model_dump())
    world.set_untimed_holders(["N2"])
    world.set_untimed_holders(iter(["N2"]))
    assert world.state.model_dump() == before and flush(world) == ([], [])

    def broken(_before: frozenset[str]) -> None:
        raise RuntimeError("module bug")

    world.chat.untimed_changed = broken  # type: ignore[method-assign]
    world.set_untimed_holders([])  # the set is changed although a module failed to re-judge
    assert world.untimed_holders == frozenset()


def test_the_untimed_holders_are_not_part_of_a_save_and_the_orchestrator_sets_them_again_after_a_load() -> None:
    world = public_world()
    open_queue(world, "pub", ["N2"], duration_s=30)
    world.set_running(False, "pause")
    data = json.loads(json.dumps(world.dump(), ensure_ascii=False))
    assert "untimed" not in json.dumps(data["state"])  # the token only holds its nominal remaining time
    restored = ChatWorld.restore(data, world.spec, ManualClock(data["clock_now"]))
    assert restored.untimed_holders == frozenset()
    person = token(restored, "pub", "N2")
    assert person.deadline is None and person.remaining == 30.0

    restored.set_untimed_holders(["N2"])  # the same person plays: nothing to change
    assert person.deadline is None and person.remaining == 30.0
    assert floor_view(restored, restored.channel("pub"))["untimed"] is True

    other = ChatWorld.restore(data, world.spec, ManualClock(data["clock_now"]))
    other.set_untimed_holders(["N2"])
    other.set_untimed_holders([])  # nobody plays any more: the floor is timed again with its nominal time
    assert token(other, "pub", "N2").deadline == other.clock.now() + 30.0


# =========================================================================== the seat state, the board, the ledger

def _full_world_with_a_queue(holder: str = "u1"):
    world = make_full_world()
    world.set_untimed_holders([holder])
    limits = {"max_messages": 2, "max_chars_total": 100, "max_chars_per_message": 40, "duration_s": 30}
    assert full_mod(world, "set_floor_queue", channel_id="public", order=[holder, "u2"], limits=limits).ok
    return world


def test_seat_status_reports_an_untimed_floor_without_a_clock() -> None:
    world = _full_world_with_a_queue()
    state = seat_status(world, "u1")["channels"]["public"]
    assert state["can_write"] and state["can_pass"] is True and state["max_chars"] == 40
    assert state["floor"] == {
        "kind": "queue", "mine": True, "paused": False, "messages_left": 2, "chars_left": 100,
        "seconds_left": None, "deadline_clock": None, "untimed": True,
    }
    other = seat_status(world, "u2")["channels"]["public"]["floor"]
    assert other["untimed"] is False and other["mine"] is False
    world.clock.advance(5000)
    assert seat_status(world, "u1")["channels"]["public"]["floor"]["seconds_left"] is None  # the clock is not looked at
    assert full_speak(world, "u1", "public").ok


def test_seat_status_of_a_timed_floor_and_of_a_free_discussion_says_untimed_false() -> None:
    world = make_full_world()
    free = seat_status(world, "u1")["channels"]["public"]["floor"]
    assert free is not None and free["kind"] == "free" and free["untimed"] is False
    assert full_mod(world, "set_floor_queue", channel_id="public", order=["u1"], limits={"duration_s": 30}).ok
    timed = seat_status(world, "u1")["channels"]["public"]["floor"]
    assert timed["untimed"] is False and timed["seconds_left"] == 30.0 and timed["deadline_clock"] == 30.0


def test_seat_status_of_a_paused_untimed_floor_is_paused_and_still_untimed() -> None:
    world = _full_world_with_a_queue()
    assert full_mod(world, "pause_queue", channel_id="public").ok
    floor = seat_status(world, "u1")["channels"]["public"]["floor"]
    assert floor["paused"] is True and floor["untimed"] is True
    assert floor["seconds_left"] is None and floor["deadline_clock"] is None


def test_the_board_never_says_untimed_it_shows_the_nominal_length_of_the_turn() -> None:
    world = _full_world_with_a_queue()
    world.clock.advance(500)  # long past the nominal 30 seconds
    for viewer in (MODERATOR_ID, "u2", "u3"):
        board = build_board(world, viewer)
        public = next(c for c in board["channels"] if c["id"] == "public")
        assert public["floor"]["active"] == "u1" and public["floor"]["seconds_left"] == 30.0, viewer
        assert "untimed" not in json.dumps(board, ensure_ascii=False), viewer
    # the same as an ordinary holder shows - a number, never "no deadline" - on a world without a played seat
    plain = make_full_world()
    assert full_mod(plain, "set_floor_queue", channel_id="public", order=["u1", "u2"], limits={"duration_s": 30}).ok
    plain_public = next(c for c in build_board(plain, MODERATOR_ID)["channels"] if c["id"] == "public")
    assert isinstance(plain_public["floor"]["seconds_left"], float)


def test_the_floor_view_the_spectators_get_may_say_untimed_but_the_agents_get_no_such_word() -> None:
    world = _full_world_with_a_queue()
    # (the god view of the interface is told: it is how the person sees "不限时")
    assert floor_view(world, world.channel("public"))["untimed"] is True
    held = world.drain_outbox()
    texts = json.dumps([d.envelope.payload for d in held], ensure_ascii=False)
    assert "untimed" not in texts and "不限时" not in texts


def test_get_ledger_hides_the_untimed_mark_from_the_moderator_but_the_ledger_keeps_it() -> None:
    world = _full_world_with_a_queue()
    receipt = full_mod(world, "get_ledger", types=["floor.granted"], channel_id="public")
    assert receipt.ok
    rows = receipt.data["entries"]
    assert rows and all("untimed" not in row["data"] for row in rows)
    assert "untimed" not in json.dumps(receipt.data, ensure_ascii=False)
    stored = [e for e in world.ledger if e.type == "floor.granted" and e.channel_id == "public"]
    assert stored[0].data["untimed"] is True  # the replay needs it


# =========================================================================== replay

def test_a_replay_shows_no_countdown_for_an_untimed_floor_and_matches_the_live_events_step_by_step() -> None:
    run = Run()
    w, clock = run.world, run.clock
    w.set_untimed_holders(["N2"])
    ok(mod(w, "create_channel", channel_id="public", name="公共", members=IDS, mode="closed", type="public"))
    run.step("create public")
    ok(mod(w, "create_channel", channel_id="wolves", name="小队", members=["N1", "N2"], mode="closed", type="group"))
    run.step("create a group channel")
    ok(mod(w, "set_floor_queue", channel_id="public", order=["N1", "N2", "N3"], limits={"duration_s": 30, "max_messages": 2}))
    run.step("open a queue in public: the first token is an agent's")
    ok(speak(w, "N1", "public", "我先说"))
    run.step("N1 speaks")
    ok(do_pass(w, "N1", "public"))
    run.step("N1 passes: the person's untimed floor starts")
    clock.advance(500)
    ok(speak(w, "N2", "public", "想了很久"))
    run.step("the person speaks after a long time")
    ok(mod(w, "pause_queue", channel_id="public"))
    run.step("pause")
    clock.advance(50)
    ok(mod(w, "resume_queue", channel_id="public"))
    run.step("resume")
    ok(do_pass(w, "N2", "public"))
    run.step("the person passes: the next, timed floor starts")
    clock.advance(40)
    w.tick()
    run.step("N3 times out")
    ok(mod(w, "set_floor_queue", channel_id="wolves", order=["N2"], limits={"duration_s": 30}))
    run.step("a timed floor of the person in a group channel")
    ok(mod(w, "open_floor", channel_id="public", mode="turn", holders=["N1", "N2"], limits={"duration_s": 30}))
    run.step("a turn group: one agent, one person")

    events = [(ev["type"], ev["data"]) for ev in run.replay["events"]]
    floors = [data["floor"] for type_, data in events if type_ == "floor.changed" and data["channel_id"] == "public"]
    assert any(f["untimed"] is True and f["seconds_left"] is None and f["active"] == "N2" for f in floors)
    assert any(f["untimed"] is False and f["seconds_left"] is not None for f in floors)
    grants = [e.data.get("untimed") for e in entries(w, "floor.granted", channel="public")]
    assert True in grants and None in grants
    # the channel views fold to the same thing the live stream built (the clock-bound numbers aside)
    final = fold_channels({}, events)
    assert final["public"]["floor"]["kind"] == "turn" and final["public"]["floor"]["untimed"] is False
    assert final["wolves"]["floor"]["untimed"] is False  # (that floor was timed: a group channel)
    assert payload_of(w, run.spec)["world"]["ledger"]  # (a whole save is what the projector reads)


def test_the_replay_of_an_older_game_without_the_mark_shows_timed_floors() -> None:
    run = Run()
    w, clock = run.world, run.clock
    ok(mod(w, "create_channel", channel_id="public", name="公共", members=IDS, mode="closed", type="public"))
    ok(mod(w, "set_floor_queue", channel_id="public", order=["N1"], limits={"duration_s": 30}))
    clock.advance(4)
    run.step("a floor of an older game")
    payload = payload_of(w, run.spec)
    for entry in payload["world"]["ledger"]:
        assert "untimed" not in entry["data"]  # (no mark: that is what a game of v0.13.0 looks like)
    replay = build_replay(payload)
    floors = [ev["data"]["floor"] for ev in replay["events"] if ev["type"] == "floor.changed"]
    assert floors and all(f["untimed"] is False for f in floors)
    assert floors[-1]["seconds_left"] is not None


@pytest.mark.parametrize("holder", ["N1", "N3"])
def test_the_world_knows_nothing_of_seat_ids(holder: str) -> None:
    """Which seat is untimed is data, not code: any participant id works."""
    world = make_world()
    world.set_untimed_holders([holder])
    new_channel(world, "pub", type_="public")
    open_queue(world, "pub", [holder], duration_s=30)
    assert token(world, "pub", holder).deadline is None
    other = next(p for p in ("N1", "N2", "N3", "N4") if p != holder)
    open_queue(world, "pub", [other], duration_s=30)
    assert token(world, "pub", other).deadline is not None
