"""``ChatModule.freeze_channel`` (contracts §2.4, D25): what an ability does to a channel's floor."""

from __future__ import annotations

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
    new_channel,
    open_free,
    open_queue,
    open_turn,
    speak,
    targets,
    ui,
)


def _world():
    world = make_world()
    new_channel(world, "c1")
    return world


# --------------------------------------------------------------------------- queues


def test_freezing_a_queue_stops_the_clock_and_the_floor() -> None:
    world = _world()
    open_queue(world, "c1", ["p01", "p02"])
    world.clock.advance(20)
    assert world.chat.freeze_channel("c1", "ability:boom") is True

    channel = world.channel("c1")
    token = channel.group.tokens[0]
    assert channel.frozen and channel.group.paused
    assert token.deadline is None and token.remaining == 40.0
    frozen = entries(world, "floor.frozen")[0]
    assert frozen.actor_id == "engine" and frozen.visible_to_ids == ["moderator", *PLAYERS]
    assert frozen.data == {"group_id": "g1", "channel_id": "c1", "reason": "ability:boom"}

    deliveries, events = flush(world)
    assert targets(deliveries, "floor.frozen") == ["p01"]  # only the one who could write; pending p02 hears nothing
    [env] = envelopes(deliveries, event="floor.frozen")
    assert (env.kind, env.priority, env.interrupt, env.channel_id) == ("floor", "ambient", True, "c1")
    assert env.payload == {"event": "floor.frozen", "channel_id": "c1", "reason": "ability:boom"}
    assert ui(events, "channel.updated")[-1]["channel"]["frozen"] is True
    assert ui(events, "floor.changed")[-1]["floor"]["paused"] is True

    # speak is refused for everybody, also a draft that raced the ability (D25)
    racing = speak(world, "p01", "c1", "我在思考时被冻结了", based_on=0)
    assert not racing.ok and racing.code == Code.NO_FLOOR and "冻结" in racing.message
    assert speak(world, "p02", "c1", "我也不行").code == Code.NO_FLOOR
    assert do_pass(world, "p01", "c1").code == Code.NO_FLOOR
    world.clock.advance(1000)
    world.tick()
    assert token.status == "active" and world.next_deadline() is None
    # the moderator keeps its voice
    assert mod(world, "send_message", channel_id="c1", content="我来处理").ok
    assert mod(world, "announce", channel_id="c1", template_id="notice", params={"text": "裁决中"}).ok


def test_freezing_is_idempotent_and_ignores_unknown_channels() -> None:
    world = _world()
    open_queue(world, "c1", ["p01"])
    assert world.chat.freeze_channel("c1", "a") is True
    seq = world.seq
    flush(world)
    assert world.chat.freeze_channel("c1", "b") is False
    assert world.seq == seq and world.drain_outbox() == [] and world.drain_events() == []
    assert world.chat.freeze_channel("nowhere", "a") is False


def test_resume_queue_lifts_the_freeze_and_restores_the_remaining_time() -> None:
    world = _world()
    open_queue(world, "c1", ["p01", "p02"])
    world.clock.advance(20)
    world.chat.freeze_channel("c1", "ability:x")
    flush(world)
    world.clock.advance(300)
    assert mod(world, "resume_queue", channel_id="c1").ok
    channel = world.channel("c1")
    assert not channel.frozen and not channel.group.paused
    assert channel.group.tokens[0].deadline == 360.0  # 300 + 20 + the 40 s that were left
    deliveries, events = flush(world)
    [granted] = envelopes(deliveries, target="p01", event="floor.granted")
    assert granted.payload["seconds"] == 40.0
    assert ui(events, "channel.updated")[-1]["channel"]["frozen"] is False
    assert speak(world, "p01", "c1", "冻结解除了").ok


def test_freezing_a_paused_queue_keeps_the_remaining_time_and_tells_nobody_twice() -> None:
    world = _world()
    open_queue(world, "c1", ["p01", "p02"])
    world.clock.advance(10)
    assert mod(world, "pause_queue", channel_id="c1").ok
    flush(world)
    world.clock.advance(100)
    assert world.chat.freeze_channel("c1", "ability:x")
    assert world.channel("c1").group.tokens[0].remaining == 50.0  # not recomputed
    assert targets(world.drain_outbox(), "floor.frozen") == []  # nobody could write anyway
    assert mod(world, "resume_queue", channel_id="c1").ok
    assert not world.channel("c1").frozen and world.channel("c1").group.tokens[0].deadline == 160.0


def test_every_way_of_reorganising_the_floor_lifts_the_freeze() -> None:
    for action in (
        lambda w: mod(w, "close_floor", channel_id="c1"),
        lambda w: mod(w, "update_channel", channel_id="c1", mode="closed"),
        lambda w: mod(w, "update_channel", channel_id="c1", mode="free"),
        lambda w: mod(w, "open_floor", channel_id="c1", holders=["p02"]),
        lambda w: mod(w, "set_floor_queue", channel_id="c1", order=["p02"]),
        lambda w: mod(w, "resume_queue", channel_id="c1"),
    ):
        world = _world()
        open_queue(world, "c1", ["p01"])
        world.chat.freeze_channel("c1", "ability:x")
        assert action(world).ok
        channel = world.channel("c1")
        assert not channel.frozen
        assert channel.group is None or not channel.group.paused  # a re-opened floor runs again


# --------------------------------------------------------------------------- turn groups


def test_freezing_a_turn_group_notifies_every_active_holder() -> None:
    world = _world()
    open_turn(world, "c1", ["p01", "p02", "p03"])
    assert do_pass(world, "p03", "c1").ok  # p03 is done: it can no longer write anyway
    flush(world)
    world.clock.advance(5)
    assert world.chat.freeze_channel("c1", "ability:x")
    deliveries, _ = flush(world)
    assert sorted(targets(deliveries, "floor.frozen")) == ["p01", "p02"]
    assert [t.remaining for t in world.channel("c1").group.tokens[:2]] == [55.0, 55.0]
    assert speak(world, "p01", "c1", "不行").code == Code.NO_FLOOR


# --------------------------------------------------------------------------- free discussions and DMs


def test_freezing_a_free_discussion_silences_the_players() -> None:
    world = _world()
    open_free(world, "c1")
    assert mod(world, "update_channel", channel_id="c1", readonly_members=["p04"]).ok
    assert speak(world, "p01", "c1", "冻结前的一句").ok
    flush(world)
    assert world.chat.freeze_channel("c1", "ability:x")
    deliveries, _ = flush(world)
    assert sorted(targets(deliveries, "floor.frozen")) == ["p01", "p02", "p03"]  # not the read-only p04, not the moderator
    assert speak(world, "p02", "c1", "不行").code == Code.NO_FLOOR
    assert mod(world, "send_message", channel_id="c1", content="主持人可以").ok
    # still free in nature: the members' quota and statistics are kept for when it resumes
    assert world.channel("c1").free.counts == {"p01": 1}


def test_a_frozen_free_discussion_gives_no_advisories_until_it_is_released() -> None:
    world = _world()
    open_free(world, "c1")
    world.chat.freeze_channel("c1", "ability:x")
    flush(world)
    world.clock.advance(500)
    world.tick()
    assert world.drain_outbox() == []
    assert world.next_deadline() is None
    assert mod(world, "resume_queue", channel_id="c1").ok  # no group: it simply lifts the freeze
    assert not world.channel("c1").frozen
    assert entries(world, "floor.resumed")[0].data == {"group_id": None, "channel_id": "c1", "reason": "resumed"}
    world.tick()
    assert sorted(e.payload["reason"] for e in envelopes(world.drain_outbox(), event="free.advisory")) == ["silence", "time"]


def test_update_channel_mode_free_lifts_a_freeze_without_restarting_the_discussion() -> None:
    world = _world()
    open_free(world, "c1")
    assert speak(world, "p01", "c1", "一句话").ok
    world.chat.freeze_channel("c1", "ability:x")
    receipt = mod(world, "update_channel", channel_id="c1", mode="free")
    assert receipt.ok and receipt.data["changes"] == {"unfrozen": True}
    assert not world.channel("c1").frozen and world.channel("c1").free.counts == {"p01": 1}
    assert speak(world, "p01", "c1", "又可以说了").ok


def test_a_system_dm_can_be_frozen_too() -> None:
    world = make_world()
    assert world.chat.freeze_channel("dm-p01", "ability:x")
    deliveries, _ = flush(world)
    assert targets(deliveries, "floor.frozen") == ["p01"]
    assert speak(world, "p01", "dm-p01", "主持人在吗").code == Code.NO_FLOOR
    assert speak(world, "p02", "dm-p02", "我这里没事").ok
    assert mod(world, "update_channel", channel_id="dm-p01", mode="free").ok
    assert speak(world, "p01", "dm-p01", "好了").ok


def test_views_show_the_freeze() -> None:
    world = _world()
    open_queue(world, "c1", ["p01"])
    world.chat.freeze_channel("c1", "ability:x")
    view = channel_view(world, world.channel("c1"))
    assert view["frozen"] is True and view["floor"]["paused"] is True and view["floor"]["seconds_left"] == 60.0
    assert floor_view(world, world.channel("c1"))["active"] == "p01"
    assert MODERATOR_ID in view["members"]
