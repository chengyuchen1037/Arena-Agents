"""Floors: ``turn`` groups and ``queue``s — grants, advancement, timeouts, pause / resume, closing."""

from __future__ import annotations

import pytest

from app.world.chat import channel_view, floor_view
from app.world.types import MODERATOR_ID, Code

from .helpers import (
    PLAYERS,
    do_pass,
    entries,
    envelopes,
    flush,
    make_world,
    mod,
    msgs,
    new_channel,
    open_free,
    open_queue,
    open_turn,
    speak,
    targets,
    ui,
)


def _world(members=None):
    world = make_world()
    new_channel(world, "c1", members=members)
    return world


# --------------------------------------------------------------------------- opening a turn group


def test_open_floor_turn_grants_every_holder_at_once() -> None:
    world = _world()
    receipt = open_turn(world, "c1", ["N1", "N2"], keep_queues=True)
    assert receipt.data["holders"] == ["N1", "N2"] and receipt.data["group_id"] == "g1"
    channel = world.channel("c1")
    assert channel.mode == "turn" and not channel.frozen and channel.group.mode == "turn"
    assert [t.status for t in channel.group.tokens] == ["active", "active"]
    assert [t.deadline for t in channel.group.tokens] == [60.0, 60.0]

    opened, g1, g2 = entries(world, "floor.", channel="c1", prefix=True)
    assert opened.type == "floor.opened" and opened.actor_id == MODERATOR_ID and receipt.seq == opened.seq
    assert opened.data == {
        "group_id": "g1",
        "mode": "turn",
        "holders": ["N1", "N2"],
        "limits": {"max_messages": 2, "max_chars_per_message": 20, "max_chars_total": 50, "duration_s": 60.0},
    }
    assert [e.type for e in (g1, g2)] == ["floor.granted", "floor.granted"]
    assert g1.data == {
        "group_id": "g1",
        "holder": "N1",
        "limits": {"max_messages": 2, "max_chars_per_message": 20, "max_chars_total": 50, "duration_s": 60.0},
    }  # no position / total for a turn group
    assert all(e.visible_to_ids == ["moderator", *PLAYERS] for e in (opened, g1, g2))

    deliveries, events = flush(world)
    granted = [d for d in deliveries if d.envelope.payload["event"] == "floor.granted"]
    assert [d.target for d in granted] == ["N1", "N2"]
    env = granted[0].envelope
    assert (env.kind, env.priority, env.interrupt, env.channel_id, env.seq) == ("floor", "urgent", False, "c1", g1.seq)
    assert env.payload == {
        "event": "floor.granted",
        "channel_id": "c1",
        "mode": "turn",
        "max_messages": 2,
        "max_chars_per_message": 20,
        "max_chars_total": 50,
        "seconds": 60.0,
    }
    assert [e.type for e in events] == ["floor.changed", "channel.updated"]
    assert ui(events, "floor.changed")[0]["floor"]["kind"] == "turn"
    assert ui(events, "channel.updated")[0]["channel"]["mode"] == "turn"


def test_every_holder_of_a_turn_group_speaks_independently() -> None:
    world = _world()
    open_turn(world, "c1", ["N1", "N2"])
    assert speak(world, "N2", "c1", "我先说").ok
    assert speak(world, "N1", "c1", "我也说").ok
    assert speak(world, "N3", "c1", "我不是持有人").code == Code.NO_FLOOR
    tokens = world.channel("c1").group.tokens
    assert [t.messages_used for t in tokens] == [1, 1]


def test_a_turn_group_completes_when_every_token_is_done_and_wakes_the_moderator() -> None:
    world = _world()
    open_turn(world, "c1", ["N1", "N2"])
    assert do_pass(world, "N1", "c1").ok
    deliveries, _ = flush(world)
    assert not envelopes(deliveries, target="moderator", event="floor.completed")  # one token is still running
    assert speak(world, "N2", "c1", "第一句").ok
    assert speak(world, "N2", "c1", "第二句").ok  # messages used up -> done -> the group completes
    deliveries, events = flush(world)

    completed = entries(world, "floor.completed")[0]
    results = [
        {"holder": "N1", "reason": "pass", "messages_used": 0, "chars_used": 0},
        {"holder": "N2", "reason": "used", "messages_used": 2, "chars_used": 6},
    ]
    assert completed.data == {"group_id": "g1", "channel_id": "c1", "mode": "turn", "reason": "completed", "results": results}
    [env] = envelopes(deliveries, event="floor.completed")
    assert [d.target for d in deliveries if d.envelope.payload["event"] == "floor.completed"] == ["moderator"]
    assert (env.kind, env.priority, env.interrupt, env.channel_id, env.seq) == ("floor", "urgent", False, "c1", completed.seq)
    assert env.payload == {
        "event": "floor.completed",
        "channel_id": "c1",
        "group_id": "g1",
        "mode": "turn",
        "results": results,
    }
    channel = world.channel("c1")
    assert channel.mode == "closed" and channel.group is None
    assert ui(events, "floor.changed")[-1]["floor"]["kind"] == "none"
    assert ui(events, "channel.updated")[-1]["channel"]["mode"] == "closed"
    # nobody can speak any more; the moderator still can
    assert speak(world, "N2", "c1", "再说一句").code == Code.CHANNEL_CLOSED
    assert mod(world, "send_message", channel_id="c1", content="谢谢大家").ok


def test_open_floor_validation() -> None:
    world = _world(members=["N1", "N2"])
    assert mod(world, "open_floor", channel_id="nowhere", holders=["N1"]).code == Code.UNKNOWN_CHANNEL
    assert mod(world, "open_floor", channel_id="c1", holders=["ghost"]).code == Code.UNKNOWN_PLAYER
    outsider = mod(world, "open_floor", channel_id="c1", holders=["N1", "N3"])
    assert outsider.code == Code.NOT_MEMBER and outsider.data["not_members"] == ["N3"]
    assert mod(world, "open_floor", channel_id="c1", holders=["moderator"]).code == Code.INVALID_TARGET
    # the keys of ``limits`` must fit the mode (details: test_chat_limits.py)
    assert mod(world, "open_floor", channel_id="c1", holders=["N1"], limits={"max_messages_per_member": 1}).code == Code.INVALID_PARAMS
    assert (
        mod(world, "open_floor", channel_id="c1", mode="free", holders=["N1"], limits={"max_messages": 1}).code
        == Code.INVALID_PARAMS
    )
    assert world.channel("c1").mode == "closed" and world.channel("c1").group is None
    assert not entries(world, "floor.", prefix=True)


def test_system_dms_do_not_take_tokens() -> None:
    world = make_world()
    assert mod(world, "open_floor", channel_id="dm-N1", holders=["N1"]).code == Code.INVALID_TARGET
    assert mod(world, "open_floor", channel_id="dm-N1", mode="free", holders=[]).code == Code.INVALID_TARGET
    assert mod(world, "set_floor_queue", channel_id="dm-N1", order=["N1"]).code == Code.INVALID_TARGET
    assert world.channel("dm-N1").mode == "free"


def test_default_limits_can_be_overridden_per_grant() -> None:
    world = _world()
    receipt = open_turn(world, "c1", ["N1"], max_messages=1, max_chars_per_message=5, max_chars_total=100, duration_s=7)
    assert receipt.data["limits"] == {"max_messages": 1, "max_chars_per_message": 5, "max_chars_total": 100, "duration_s": 7.0}
    assert world.next_deadline() == 7.0
    first = speak(world, "N1", "c1", "一二三四五六七")
    assert first.truncated == {"dropped_chars": 2, "kept_chars": 5} and first.floor_left["messages"] == 0
    assert speak(world, "N1", "c1", "x").code == Code.CHANNEL_CLOSED  # the only token is spent: group completed


def test_a_new_grant_replaces_the_running_group() -> None:
    world = _world()
    open_queue(world, "c1", ["N1", "N2"])
    receipt = open_turn(world, "c1", ["N3"], keep_queues=True)
    assert receipt.data["group_id"] == "g2"
    deliveries, _ = flush(world)
    [revoked] = envelopes(deliveries, event="floor.revoked")
    assert (revoked.kind, revoked.priority, revoked.interrupt) == ("floor", "normal", True)
    assert revoked.payload == {"event": "floor.revoked", "channel_id": "c1", "reason": "replaced"}
    assert targets(deliveries, "floor.revoked") == ["N1"]  # only the active token holder is told
    assert targets(deliveries, "floor.granted") == ["N3"]
    ended = entries(world, "floor.token_ended")
    assert [(e.data["holder"], e.data["reason"], e.data["cause"]) for e in ended] == [("N1", "revoked", "replaced")]
    assert speak(world, "N1", "c1", "我的发言权被换掉了").code == Code.NO_FLOOR
    assert speak(world, "N3", "c1", "新的持有人").ok
    # no floor.completed for a revoked group: the moderator did that on purpose
    assert not entries(world, "floor.completed")


# --------------------------------------------------------------------------- queues


def test_a_queue_activates_one_token_at_a_time() -> None:
    world = _world()
    receipt = open_queue(world, "c1", ["N3", "N1", "N2"], keep_queues=True)
    assert receipt.data["order"] == ["N3", "N1", "N2"]
    assert [t.status for t in world.channel("c1").group.tokens] == ["active", "pending", "pending"]
    assert [t.deadline for t in world.channel("c1").group.tokens] == [60.0, None, None]
    opened, granted = entries(world, "floor.", channel="c1", prefix=True)
    assert receipt.seq == opened.seq
    assert opened.data["order"] == ["N3", "N1", "N2"] and "holders" not in opened.data
    assert granted.data["holder"] == "N3" and granted.data["position"] == 1 and granted.data["total"] == 3
    deliveries, _ = flush(world)
    [env] = envelopes(deliveries, target="N3", event="floor.granted")
    assert env.payload["mode"] == "queue" and env.payload["position"] == 1 and env.payload["total"] == 3
    assert targets(deliveries, "floor.granted") == ["N3"]  # the others hear nothing yet
    assert speak(world, "N1", "c1", "还没到我").code == Code.NO_FLOOR


def test_the_queue_advances_after_used_pass_and_timeout() -> None:
    world = _world()
    open_queue(world, "c1", ["N1", "N2", "N3", "N4"])
    # N1 uses up its two messages
    assert speak(world, "N1", "c1", "一").ok and speak(world, "N1", "c1", "二").ok
    deliveries, events = flush(world)
    assert targets(deliveries, "floor.granted") == ["N2"]
    [granted] = envelopes(deliveries, target="N2", event="floor.granted")
    assert granted.payload["position"] == 2 and granted.payload["total"] == 4 and granted.payload["seconds"] == 60.0
    assert ui(events, "floor.changed")[-1]["floor"]["active"] == "N2"
    # N2 passes
    assert do_pass(world, "N2", "c1").ok
    assert targets(world.drain_outbox(), "floor.granted") == ["N3"]
    # N3 runs out of time
    world.clock.advance(60)
    world.tick()
    deliveries, _ = flush(world)
    assert targets(deliveries, "floor.granted") == ["N4"]
    reasons = [(e.data["holder"], e.data["reason"]) for e in entries(world, "floor.token_ended")]
    assert reasons == [("N1", "used"), ("N2", "pass"), ("N3", "timeout")]
    assert world.channel("c1").group.tokens[3].status == "active"


def test_a_finished_queue_wakes_the_moderator_with_every_result() -> None:
    world = _world()
    open_queue(world, "c1", ["N1", "N2", "N3"])
    assert speak(world, "N1", "c1", "甲").ok
    assert do_pass(world, "N1", "c1").ok  # one message used, then passes
    world.clock.advance(5)
    assert speak(world, "N2", "c1", "乙乙").ok and speak(world, "N2", "c1", "丙").ok
    flush(world)
    world.clock.advance(60)
    world.tick()  # N3 never speaks and times out
    deliveries, _ = flush(world)
    [env] = envelopes(deliveries, target="moderator", event="floor.completed")
    assert env.priority == "urgent" and env.kind == "floor"
    assert env.payload["mode"] == "queue" and env.payload["group_id"] == "g1"
    assert env.payload["results"] == [
        {"holder": "N1", "reason": "pass", "messages_used": 1, "chars_used": 1},
        {"holder": "N2", "reason": "used", "messages_used": 2, "chars_used": 3},
        {"holder": "N3", "reason": "timeout", "messages_used": 0, "chars_used": 0},
    ]
    assert world.channel("c1").mode == "closed" and world.channel("c1").group is None
    assert world.next_deadline() is None
    # only one wake-up for the whole queue (not one per token)
    assert len(entries(world, "floor.completed")) == 1


def test_queue_positions_may_repeat_a_holder() -> None:
    world = _world()
    open_queue(world, "c1", ["N1", "N2", "N1"])
    assert do_pass(world, "N1", "c1").ok
    assert do_pass(world, "N2", "c1").ok
    deliveries, _ = flush(world)
    [again] = envelopes(deliveries, target="N1", event="floor.granted")
    assert again.payload["position"] == 3 and again.payload["total"] == 3
    assert speak(world, "N1", "c1", "我的第二个位置").ok
    assert do_pass(world, "N1", "c1").ok
    [done] = entries(world, "floor.completed")
    assert [r["holder"] for r in done.data["results"]] == ["N1", "N2", "N1"]


def test_set_floor_queue_validation() -> None:
    world = _world(members=["N1", "N2"])
    assert mod(world, "set_floor_queue", channel_id="nowhere", order=["N1"]).code == Code.UNKNOWN_CHANNEL
    assert mod(world, "set_floor_queue", channel_id="c1", order=["N1", "ghost"]).code == Code.UNKNOWN_PLAYER
    assert mod(world, "set_floor_queue", channel_id="c1", order=["N1", "N3"]).code == Code.NOT_MEMBER
    assert mod(world, "set_floor_queue", channel_id="c1", order=["moderator"]).code == Code.INVALID_TARGET
    assert world.channel("c1").group is None and not entries(world, "floor.", prefix=True)


# --------------------------------------------------------------------------- timeouts


def test_a_token_times_out_exactly_at_its_deadline() -> None:
    world = _world()
    open_queue(world, "c1", ["N1", "N2"])
    world.clock.advance(59.9)
    world.tick()
    assert world.channel("c1").group.tokens[0].status == "active"
    world.clock.advance(0.1)
    world.tick()
    assert world.channel("c1").group.tokens[0].status == "done"
    deliveries, _ = flush(world)
    [revoked] = envelopes(deliveries, target="N1", event="floor.revoked")
    assert (revoked.kind, revoked.priority, revoked.interrupt) == ("floor", "normal", True)
    assert revoked.payload["reason"] == "timeout" and revoked.channel_id == "c1"
    assert targets(deliveries, "floor.granted") == ["N2"]
    ended = entries(world, "floor.token_ended")[0]
    assert ended.data == {"group_id": "g1", "holder": "N1", "reason": "timeout", "messages_used": 0, "chars_used": 0}


def test_tick_is_idempotent() -> None:
    world = _world()
    open_queue(world, "c1", ["N1", "N2"])
    world.clock.advance(60)
    world.tick()
    seq = world.seq
    flush(world)
    world.tick()
    world.tick()
    assert world.seq == seq and world.drain_outbox() == []


def test_a_long_gap_only_expires_the_active_token() -> None:
    world = _world()
    open_queue(world, "c1", ["N1", "N2", "N3"])
    world.clock.advance(1000)
    world.tick()
    tokens = world.channel("c1").group.tokens
    assert [t.status for t in tokens] == ["done", "active", "pending"]
    assert tokens[1].deadline == 1060.0  # the next speaker gets a full, fresh slice
    world.tick()
    assert tokens[1].status == "active"


def test_a_turn_group_times_out_together_and_completes_once() -> None:
    world = _world()
    open_turn(world, "c1", ["N1", "N2", "N3"])
    assert speak(world, "N2", "c1", "说了一句").ok
    flush(world)
    world.clock.advance(60)
    world.tick()
    deliveries, _ = flush(world)
    assert sorted(targets(deliveries, "floor.revoked")) == ["N1", "N2", "N3"]
    assert [e.payload["reason"] for e in envelopes(deliveries, event="floor.revoked")] == ["timeout"] * 3
    assert len(envelopes(deliveries, target="moderator", event="floor.completed")) == 1
    [done] = entries(world, "floor.completed")
    assert [r["reason"] for r in done.data["results"]] == ["timeout"] * 3
    assert done.data["results"][1]["messages_used"] == 1


def test_next_deadline_tracks_the_earliest_running_token() -> None:
    world = _world()
    assert world.next_deadline() is None
    open_queue(world, "c1", ["N1", "N2"], duration_s=30)
    assert world.next_deadline() == 30.0
    world.clock.advance(10)
    assert do_pass(world, "N1", "c1").ok
    assert world.next_deadline() == 40.0  # N2 started at t=10
    assert do_pass(world, "N2", "c1").ok
    assert world.next_deadline() is None


# --------------------------------------------------------------------------- pause / resume (D18)


def test_pause_freezes_the_remaining_time_and_resume_continues_it() -> None:
    world = _world()
    open_queue(world, "c1", ["N1", "N2"])
    world.clock.advance(20)
    receipt = mod(world, "pause_queue", channel_id="c1")
    assert receipt.ok
    token = world.channel("c1").group.tokens[0]
    assert token.deadline is None and token.remaining == 40.0
    assert world.channel("c1").group.paused
    paused = entries(world, "floor.paused")[0]
    assert paused.data == {"group_id": "g1", "channel_id": "c1", "reason": "paused"}
    assert paused.visible_to_ids == ["moderator", *PLAYERS]
    deliveries, events = flush(world)
    assert targets(deliveries, "floor.paused") == ["N1"]
    [env] = envelopes(deliveries, event="floor.paused")
    assert (env.kind, env.priority, env.interrupt) == ("floor", "normal", True)
    assert ui(events, "floor.changed")[-1]["floor"]["paused"] is True
    assert ui(events, "floor.changed")[-1]["floor"]["seconds_left"] == 40.0

    # while paused: nobody can speak or pass, no timer runs
    assert speak(world, "N1", "c1", "暂停中").code == Code.NO_FLOOR
    assert do_pass(world, "N1", "c1").code == Code.NO_FLOOR
    assert world.next_deadline() is None
    world.clock.advance(1000)
    world.tick()
    assert token.status == "active" and token.remaining == 40.0

    receipt = mod(world, "resume_queue", channel_id="c1")
    assert receipt.ok
    assert token.deadline == 1060.0 and token.remaining is None and not world.channel("c1").group.paused
    assert world.next_deadline() == 1060.0
    resumed = entries(world, "floor.resumed")[0]
    assert resumed.data == {"group_id": "g1", "channel_id": "c1", "reason": "resumed"}
    deliveries, _ = flush(world)
    [granted] = envelopes(deliveries, target="N1", event="floor.granted")
    assert granted.priority == "urgent" and not granted.interrupt and granted.payload["seconds"] == 40.0
    assert targets(deliveries, "floor.granted") == ["N1"]
    assert speak(world, "N1", "c1", "我回来了").ok
    world.clock.advance(40)
    world.tick()
    assert token.status == "done" and token.ended_reason == "timeout"


def test_pausing_a_turn_group_freezes_every_token() -> None:
    world = _world()
    open_turn(world, "c1", ["N1", "N2"])
    world.clock.advance(10)
    assert mod(world, "pause_queue", channel_id="c1").ok
    deliveries, _ = flush(world)
    assert sorted(targets(deliveries, "floor.paused")) == ["N1", "N2"]
    assert [t.remaining for t in world.channel("c1").group.tokens] == [50.0, 50.0]
    assert mod(world, "resume_queue", channel_id="c1").ok
    assert [t.deadline for t in world.channel("c1").group.tokens] == [60.0, 60.0]


def test_pause_and_resume_are_idempotent() -> None:
    world = _world()
    open_queue(world, "c1", ["N1", "N2"])
    seq = world.seq
    again = mod(world, "resume_queue", channel_id="c1")  # nothing is paused
    assert again.ok and "无需恢复" in again.message and world.seq == seq
    assert mod(world, "pause_queue", channel_id="c1").ok
    seq = world.seq
    twice = mod(world, "pause_queue", channel_id="c1")
    assert twice.ok and "已经处于暂停" in twice.message and world.seq == seq


def test_pause_resume_need_a_running_floor() -> None:
    world = _world()
    assert mod(world, "pause_queue", channel_id="c1").code == Code.INVALID_TARGET
    assert mod(world, "resume_queue", channel_id="c1").code == Code.INVALID_TARGET
    assert mod(world, "pause_queue", channel_id="nowhere").code == Code.UNKNOWN_CHANNEL
    assert mod(world, "resume_queue", channel_id="nowhere").code == Code.UNKNOWN_CHANNEL
    assert mod(world, "close_floor", channel_id="nowhere").code == Code.UNKNOWN_CHANNEL
    open_free(world, "c1")
    assert mod(world, "pause_queue", channel_id="c1").code == Code.INVALID_TARGET


def test_a_queue_can_be_paused_before_its_first_message_and_still_advance_on_resume() -> None:
    world = _world()
    open_queue(world, "c1", ["N1", "N2"])
    assert mod(world, "pause_queue", channel_id="c1").ok
    assert mod(world, "resume_queue", channel_id="c1").ok
    assert do_pass(world, "N1", "c1").ok
    assert world.channel("c1").group.tokens[1].status == "active"


def test_the_next_token_of_a_paused_queue_does_not_start_its_clock() -> None:
    world = _world()
    open_queue(world, "c1", ["N1", "N2", "N3"])
    assert mod(world, "pause_queue", channel_id="c1").ok
    flush(world)
    # the moderator drops N1 from the channel while paused: N2 becomes active but stays frozen
    assert mod(world, "update_channel", channel_id="c1", remove_members=["N1"]).ok
    second = world.channel("c1").group.tokens[1]
    assert second.status == "active" and second.deadline is None and second.remaining == 60.0
    assert not targets(world.drain_outbox(), "floor.granted")  # nothing is delivered while paused
    world.clock.advance(5)
    assert mod(world, "resume_queue", channel_id="c1").ok
    assert second.deadline == 65.0
    deliveries, _ = flush(world)
    assert targets(deliveries, "floor.granted") == ["N2"]


# --------------------------------------------------------------------------- close_floor


def test_close_floor_revokes_the_group_without_waking_the_moderator() -> None:
    world = _world()
    open_queue(world, "c1", ["N1", "N2"])
    receipt = mod(world, "close_floor", channel_id="c1")
    assert receipt.ok
    channel = world.channel("c1")
    assert channel.mode == "closed" and channel.group is None and not channel.frozen
    closed = entries(world, "floor.closed")[0]
    assert closed.data == {"group_id": "g1", "channel_id": "c1", "reason": "closed"} and closed.actor_id == MODERATOR_ID
    ended = entries(world, "floor.token_ended")
    assert [(e.data["holder"], e.data["reason"], e.data["cause"]) for e in ended] == [("N1", "revoked", "closed")]
    deliveries, events = flush(world)
    [revoked] = envelopes(deliveries, event="floor.revoked")
    assert (revoked.priority, revoked.interrupt, revoked.payload["reason"]) == ("normal", True, "closed")
    assert targets(deliveries, "floor.revoked") == ["N1"]  # N2 never had the floor
    assert not envelopes(deliveries, target="moderator")
    assert ui(events, "floor.changed")[-1]["floor"]["kind"] == "none"
    assert speak(world, "N1", "c1", "还能说吗").code == Code.CHANNEL_CLOSED


def test_close_floor_of_a_turn_group_notifies_all_active_holders() -> None:
    world = _world()
    open_turn(world, "c1", ["N1", "N2", "N3"])
    do_pass(world, "N3", "c1")
    flush(world)
    assert mod(world, "close_floor", channel_id="c1").ok
    assert sorted(targets(world.drain_outbox(), "floor.revoked")) == ["N1", "N2"]


def test_close_floor_on_a_channel_without_a_floor_is_a_no_op() -> None:
    world = _world()
    seq = world.seq
    receipt = mod(world, "close_floor", channel_id="c1")
    assert receipt.ok and "无需关闭" in receipt.message and world.seq == seq
    new_channel(world, "radio", mode="listen")
    assert mod(world, "close_floor", channel_id="radio").ok
    assert world.channel("radio").mode == "listen"  # a listen-only channel keeps its mode


def test_a_closed_floor_can_be_opened_again() -> None:
    world = _world()
    open_queue(world, "c1", ["N1"])
    assert mod(world, "close_floor", channel_id="c1").ok
    receipt = open_queue(world, "c1", ["N2"], keep_queues=True)
    assert receipt.data["group_id"] == "g2"
    assert speak(world, "N2", "c1", "第二轮").ok


# --------------------------------------------------------------------------- members leaving a floor


def test_removing_the_active_holder_hands_the_floor_on() -> None:
    world = _world()
    open_queue(world, "c1", ["N1", "N2"])
    assert mod(world, "update_channel", channel_id="c1", remove_members=["N1"]).ok
    deliveries, _ = flush(world)
    [revoked] = envelopes(deliveries, target="N1", event="floor.revoked")
    assert revoked.interrupt and revoked.payload["reason"] == "removed"
    assert targets(deliveries, "floor.granted") == ["N2"]
    assert speak(world, "N1", "c1", "我已经不在了").code == Code.NOT_MEMBER
    assert speak(world, "N2", "c1", "轮到我").ok


def test_removing_the_last_holder_completes_the_group() -> None:
    world = _world()
    open_queue(world, "c1", ["N1"])
    assert mod(world, "update_channel", channel_id="c1", remove_members=["N1"]).ok
    assert entries(world, "floor.completed")[0].data["results"][0]["reason"] == "revoked"
    assert world.channel("c1").mode == "closed"
    assert envelopes(world.drain_outbox(), target="moderator", event="floor.completed")


# --------------------------------------------------------------------------- restart / resume deliveries


def test_redeliver_tells_running_holders_again_with_their_remaining_time() -> None:
    world = _world()
    open_queue(world, "c1", ["N1", "N2"])
    world.clock.advance(25)
    world.redeliver()
    deliveries, _ = flush(world)
    assert targets(deliveries, "floor.granted") == ["N1"]
    [env] = envelopes(deliveries, event="floor.granted")
    assert env.priority == "urgent" and env.payload["seconds"] == 35.0 and env.payload["position"] == 1
    assert env.seq == entries(world, "floor.granted")[0].seq  # refers to the entry that granted the floor


def test_redeliver_skips_paused_and_finished_floors() -> None:
    world = _world()
    open_queue(world, "c1", ["N1"])
    assert mod(world, "pause_queue", channel_id="c1").ok
    flush(world)
    world.redeliver()
    assert world.drain_outbox() == []
    assert mod(world, "close_floor", channel_id="c1").ok
    flush(world)
    world.redeliver()
    assert world.drain_outbox() == []


# --------------------------------------------------------------------------- views


def test_floor_view_of_a_queue() -> None:
    world = _world()
    open_queue(world, "c1", ["N1", "N2"])
    world.clock.advance(12.34)
    assert speak(world, "N1", "c1", "一").ok
    view = floor_view(world, world.channel("c1"))
    assert view == {
        "kind": "queue",
        "active": "N1",
        "paused": False,
        "seconds_left": 47.7,
        "queue": [
            {"holder": "N1", "state": "active", "messages_used": 1, "max_messages": 2},
            {"holder": "N2", "state": "pending", "messages_used": 0, "max_messages": 2},
        ],
        "free": None,
    }
    assert mod(world, "pause_queue", channel_id="c1").ok
    paused = floor_view(world, world.channel("c1"))
    assert paused["paused"] is True and paused["seconds_left"] == 47.7


def test_floor_view_without_a_floor() -> None:
    world = _world()
    assert floor_view(world, world.channel("c1")) == {
        "kind": "none",
        "active": None,
        "paused": False,
        "seconds_left": None,
        "queue": [],
        "free": None,
    }


def test_channel_view_matches_the_contract_shape() -> None:
    world = _world(members=["N2", "N1"])
    view = channel_view(world, world.channel("c1"))
    assert set(view) == {"id", "name", "type", "mode", "members", "readonly", "frozen", "system", "owner", "floor"}
    assert view["members"] == ["moderator", "N2", "N1"] and view["owner"] is None and view["system"] is False
    dm = channel_view(world, world.channel("dm-N3"))
    assert dm["system"] is True and dm["owner"] == "N3" and dm["type"] == "dm" and dm["mode"] == "free"
    assert dm["floor"]["kind"] == "none"  # a system DM has no floor to manage


@pytest.mark.parametrize("mode", ["turn", "queue"])
def test_messages_in_floor_channels_wake_everybody_who_can_read_them(mode: str) -> None:
    world = _world()
    (open_turn if mode == "turn" else open_queue)(world, "c1", ["N1"])
    assert speak(world, "N1", "c1", "大家好").ok
    deliveries, _ = flush(world)
    messages = envelopes(deliveries, event="message.new")
    assert {d.target for d in deliveries if d.envelope.payload["event"] == "message.new"} == {"moderator", "N2", "N3", "N4"}
    assert {e.priority for e in messages} == {"normal"} and not any(e.interrupt for e in messages)
    assert msgs(world, "c1")[0].visible_to_ids == ["moderator", *PLAYERS]
