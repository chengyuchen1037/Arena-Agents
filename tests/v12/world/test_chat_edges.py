"""Corner cases of the chat module: floor replacement across kinds, member removal, UI events, timers."""

from __future__ import annotations

from app.world.types import MODERATOR_ID, Code

from .helpers import (
    PLAYERS,
    do_pass,
    entries,
    envelopes,
    flush,
    make_world,
    mod,
    new_channel,
    open_free,
    open_queue,
    open_turn,
    speak,
    targets,
    ui,
)

# --------------------------------------------------------------------------- replacing one floor by another


def test_opening_a_free_discussion_replaces_a_running_group() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_turn(world, "c1", ["p01", "p02"])
    receipt = open_free(world, "c1", keep_queues=True)
    assert receipt.ok
    channel = world.channel("c1")
    assert channel.mode == "free" and channel.group is None and channel.free is not None
    deliveries, _ = flush(world)
    assert sorted(targets(deliveries, "floor.revoked")) == ["p01", "p02"]
    assert {e.payload["reason"] for e in envelopes(deliveries, event="floor.revoked")} == {"replaced"}
    assert speak(world, "p03", "c1", "现在人人可以说").ok
    assert not entries(world, "floor.completed")


def test_opening_a_floor_ends_a_free_discussion_and_tells_its_writers() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")
    assert speak(world, "p01", "c1", "自由讨论中").ok
    flush(world)
    assert mod(world, "set_floor_queue", channel_id="c1", order=["p02", "p03"]).ok
    channel = world.channel("c1")
    assert channel.mode == "queue" and channel.free is None
    deliveries, _ = flush(world)
    revoked = {d.target for d in deliveries if d.envelope.payload["event"] == "floor.revoked"}
    assert revoked == set(PLAYERS)  # everybody who could write before has their draft invalidated
    assert {e.payload["reason"] for e in envelopes(deliveries, event="floor.revoked")} == {"replaced"}
    assert targets(deliveries, "floor.granted") == ["p02"]
    assert world.next_deadline() == 60.0  # no advisory timers survive the discussion


def test_restarting_a_free_discussion_does_not_revoke_anybody() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")
    assert speak(world, "p01", "c1", "一").ok
    flush(world)
    open_free(world, "c1", keep_queues=True, max_messages_per_member=1)
    deliveries, _ = flush(world)
    assert not envelopes(deliveries, event="floor.revoked")
    assert world.channel("c1").free.counts == {}


def test_a_new_queue_replaces_an_old_one_and_the_old_token_cannot_act() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["p01", "p02"])
    old = world.channel("c1").group.tokens[0]
    open_queue(world, "c1", ["p02", "p01"])
    assert old.status == "done" and old.ended_reason == "revoked"
    assert speak(world, "p01", "c1", "旧的发言权").code == Code.NO_FLOOR
    assert do_pass(world, "p01", "c1").code == Code.NO_FLOOR
    assert speak(world, "p02", "c1", "新队列的第一位").ok


# --------------------------------------------------------------------------- removing members from a free channel


def test_removing_a_free_writer_interrupts_it_and_updates_the_writers() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_free(world, "c1", holders=["p01", "p02", "p03"])
    assert mod(world, "update_channel", channel_id="c1", remove_members=["p02", "p04"]).ok
    deliveries, _ = flush(world)
    # p04 could not write (not a writer): no interruption; p02 could
    assert targets(deliveries, "floor.revoked") == ["p02"]
    assert world.channel("c1").free.writers == ["p01", "p03"]
    assert speak(world, "p02", "c1", "我被移出了").code == Code.NOT_MEMBER


# --------------------------------------------------------------------------- timers of several channels


def test_next_deadline_is_the_earliest_over_floors_and_discussions() -> None:
    world = make_world()
    new_channel(world, "queue")
    new_channel(world, "chat", mode="free")
    open_queue(world, "queue", ["p01"], duration_s=25)
    assert world.next_deadline() == 25.0  # the token beats the 40 s silence advisory
    open_queue(world, "queue", ["p01"], duration_s=500)
    assert world.next_deadline() == 40.0


def test_tick_handles_every_channel_in_one_pass() -> None:
    world = make_world()
    for cid in ("a", "b"):
        new_channel(world, cid)
        open_queue(world, cid, ["p01", "p02"], duration_s=10)
    new_channel(world, "chat", mode="free")
    flush(world)
    world.clock.advance(41)
    world.tick()
    deliveries, _ = flush(world)
    assert sorted(e.channel_id for e in envelopes(deliveries, event="floor.granted")) == ["a", "b"]
    assert [e.payload["reason"] for e in envelopes(deliveries, target=MODERATOR_ID, event="free.advisory")] == ["silence"]


# --------------------------------------------------------------------------- UI events


def test_ui_events_follow_the_contract_shapes() -> None:
    world = make_world()
    new_channel(world, "c1", keep_queues=True)
    open_queue(world, "c1", ["p01"], keep_queues=True)
    assert speak(world, "p01", "c1", "说完").ok
    assert do_pass(world, "p01", "c1").ok  # the one-man queue is complete
    assert mod(world, "update_channel", channel_id="c1", name="改名").ok
    _, events = flush(world)
    kinds = {e.type for e in events}
    assert kinds <= {"channel.created", "channel.updated", "floor.changed", "message.committed"}
    channel_keys = {"id", "name", "type", "mode", "members", "readonly", "frozen", "system", "owner", "floor"}
    floor_keys = {"kind", "active", "paused", "seconds_left", "queue", "free"}
    for event in events:
        if event.type.startswith("channel."):
            assert set(event.data) == {"channel"} and set(event.data["channel"]) == channel_keys
            assert set(event.data["channel"]["floor"]) == floor_keys
        elif event.type == "floor.changed":
            assert set(event.data) == {"channel_id", "floor"} and set(event.data["floor"]) == floor_keys
    # the very last picture of the floor says there is none any more
    assert ui(events, "floor.changed")[-1]["floor"]["kind"] == "none"
    assert ui(events, "channel.updated")[-1]["channel"]["name"] == "改名"


def test_every_floor_state_change_emits_floor_changed() -> None:
    world = make_world()
    new_channel(world, "c1")
    steps = [
        lambda: mod(world, "set_floor_queue", channel_id="c1", order=["p01", "p02"]),
        lambda: speak(world, "p01", "c1", "一"),
        lambda: mod(world, "pause_queue", channel_id="c1"),
        lambda: mod(world, "resume_queue", channel_id="c1"),
        lambda: do_pass(world, "p01", "c1"),
        lambda: world.chat.freeze_channel("c1", "x"),
        lambda: mod(world, "close_floor", channel_id="c1"),
    ]
    for step in steps:
        flush(world)
        step()
        assert ui(world.drain_events(), "floor.changed"), step
    flush(world)
    world.clock.advance(0)
    world.tick()
    assert world.drain_events() == []  # a tick with nothing due is silent


# --------------------------------------------------------------------------- odds and ends


def test_a_listen_channel_is_heard_by_everybody_but_answered_by_nobody() -> None:
    world = make_world()
    new_channel(world, "radio", mode="listen")
    assert mod(world, "send_message", channel_id="radio", content="广播").ok
    deliveries, _ = flush(world)
    assert {d.target: d.envelope.priority for d in deliveries} == {pid: "ambient" for pid in PLAYERS}
    assert speak(world, "p01", "radio", "回应").code == Code.CHANNEL_CLOSED


def test_unknown_players_are_listed_with_the_valid_ids() -> None:
    world = make_world()
    new_channel(world, "c1")
    receipt = mod(world, "open_floor", channel_id="c1", holders=["张三"])
    assert receipt.code == Code.UNKNOWN_PLAYER
    assert receipt.data["players"] == PLAYERS and receipt.data["unknown"] == ["张三"]
    assert "p01" in receipt.message
