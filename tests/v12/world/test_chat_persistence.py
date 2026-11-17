"""``dump()`` / ``load()`` round trips: a restored world behaves exactly like the one that was saved."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.world.types import Code

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
    open_queue,
    open_turn,
    speak,
    speak_template,
    targets,
)


def roundtrip(world: ChatWorld, *, resume: bool = True) -> ChatWorld:
    """What the orchestrator does: pause, save, load into a brand-new world, resume (and redeliver)."""
    world.set_running(False, "pause")
    data = json.loads(json.dumps(world.dump(), ensure_ascii=False))  # through real JSON text
    restored = ChatWorld.restore(data, world.spec, ManualClock(data["clock_now"]))
    assert not restored.running  # a freshly loaded world keeps the gate closed until it is resumed
    if resume:
        restored.set_running(True, "resume")
        restored.redeliver()
    return restored


def bounce(world: ChatWorld) -> None:
    """The same pause / resume on a world that is *not* reloaded (the control group of the tests below)."""
    world.set_running(False, "pause")
    world.set_running(True, "resume")
    world.redeliver()


def facts(world: ChatWorld) -> list[tuple]:
    """The ledger without the wall-clock timestamp."""
    return [
        (e.seq, e.type, e.actor_id, e.channel_id, tuple(e.visible_to_ids), json.dumps(e.data, sort_keys=True, ensure_ascii=False))
        for e in world.ledger
    ]


# --------------------------------------------------------------------------- shape


def test_dump_is_plain_json_and_chat_adds_no_extra_state() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["p01", "p02"])
    data = world.dump()
    json.dumps(data)  # must not raise
    assert data["format"] == 1 and data["clock_now"] == 0.0
    assert data["modules"] == {"chat": {}}
    assert len(data["ledger"]) == world.seq


def test_a_restored_world_has_the_same_ledger_and_state() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")
    assert speak(world, "p01", "c1", "存档前的话").ok
    restored = roundtrip(world, resume=False)
    assert facts(restored) == facts(world)
    assert [e.ts for e in restored.ledger] == [e.ts for e in world.ledger]
    assert restored.state.model_dump() == world.state.model_dump()
    assert restored.seq == world.seq and restored.state.status == "paused"


def test_a_game_saved_while_running_comes_back_paused() -> None:
    world = make_world()
    data = json.loads(json.dumps(world.dump()))  # an autosave: the status inside is still "running"
    assert data["state"]["status"] == "running"
    restored = ChatWorld.restore(data, world.spec, ManualClock(data["clock_now"]))
    assert restored.state.status == "paused" and not restored.running
    assert speak(restored, "p01", "dm-p01", "不行").code == Code.GAME_NOT_RUNNING
    restored.set_running(True, "resume")
    assert [e.data for e in restored.ledger if e.type == "game.status"][-1] == {"status": "running", "reason": "resume"}
    assert speak(restored, "p01", "dm-p01", "可以了").ok


# --------------------------------------------------------------------------- floors


def test_a_running_queue_resumes_with_the_remaining_time_and_is_redelivered() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["p01", "p02"])
    world.clock.advance(25)
    assert speak(world, "p01", "c1", "一句").ok
    flush(world)

    restored = roundtrip(world)
    deliveries, _ = flush(restored)
    assert targets(deliveries, "floor.granted") == ["p01"]
    [env] = envelopes(deliveries, event="floor.granted")
    assert env.priority == "urgent" and env.payload["seconds"] == 35.0 and env.payload["position"] == 1
    token = restored.channel("c1").group.tokens[0]
    assert token.messages_used == 1 and token.chars_used == 2 and token.deadline == 60.0
    assert restored.next_deadline() == 60.0

    # the floor logic carries on: the same message budget, the same timeout, the same advancement
    assert speak(restored, "p01", "c1", "二句").ok  # the last message of p01
    assert speak(restored, "p01", "c1", "三句").code == Code.NO_FLOOR
    assert restored.channel("c1").group.tokens[1].status == "active"
    restored.clock.advance(60)
    restored.tick()
    assert restored.channel("c1").group is None and restored.channel("c1").mode == "closed"
    assert [e.data["reason"] for e in entries(restored, "floor.token_ended")] == ["used", "timeout"]


def test_group_ids_keep_counting_after_a_restore() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_turn(world, "c1", ["p01"])
    assert mod(world, "close_floor", channel_id="c1").ok
    restored = roundtrip(world)
    receipt = open_queue(restored, "c1", ["p02"], keep_queues=True)
    assert receipt.data["group_id"] == "g2"


def test_a_paused_queue_stays_paused_across_a_restore() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["p01", "p02"])
    world.clock.advance(10)
    assert mod(world, "pause_queue", channel_id="c1").ok
    flush(world)
    restored = roundtrip(world)
    assert not envelopes(restored.drain_outbox(), event="floor.granted")  # it is paused: nobody holds the floor now
    assert restored.next_deadline() is None
    assert speak(restored, "p01", "c1", "还在暂停").code == Code.NO_FLOOR
    restored.clock.advance(500)
    restored.tick()
    assert restored.channel("c1").group.tokens[0].remaining == 50.0
    assert mod(restored, "resume_queue", channel_id="c1").ok
    assert restored.channel("c1").group.tokens[0].deadline == restored.clock.now() + 50.0
    assert speak(restored, "p01", "c1", "恢复了").ok


def test_a_frozen_channel_stays_frozen_across_a_restore() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["p01"])
    world.chat.freeze_channel("c1", "ability:x")
    restored = roundtrip(world)
    assert restored.channel("c1").frozen
    assert speak(restored, "p01", "c1", "冻结着").code == Code.NO_FLOOR
    assert mod(restored, "resume_queue", channel_id="c1").ok
    assert speak(restored, "p01", "c1", "解冻了").ok


# --------------------------------------------------------------------------- free discussions


def test_a_free_discussion_keeps_its_counters_and_its_told_advisories() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")
    for i in range(3):
        assert speak(world, "p01", "c1", f"第{i}句").ok
    world.clock.advance(100)
    world.tick()
    told = sorted(e.payload["reason"] for e in envelopes(world.drain_outbox(), event="free.advisory"))
    assert told == ["silence", "time"]

    restored = roundtrip(world)
    free = restored.channel("c1").free
    assert free.counts == {"p01": 3} and free.notified == {"time": True, "silence": True} and free.chars_used == 9
    assert speak(restored, "p01", "c1", "还想说").code == Code.QUOTA_EXCEEDED
    flush(restored)
    restored.clock.advance(1000)
    restored.tick()
    assert not envelopes(restored.drain_outbox(), event="free.advisory")  # nothing is told twice
    assert restored.next_deadline() is None


def test_a_free_discussion_still_owes_the_advisories_it_had_not_sent() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")
    world.clock.advance(30)
    restored = roundtrip(world)
    assert restored.next_deadline() == 40.0
    restored.clock.advance(10)
    restored.tick()
    assert [e.payload["reason"] for e in envelopes(restored.drain_outbox(), event="free.advisory")] == ["silence"]


# --------------------------------------------------------------------------- the strongest check


def _script() -> list[Callable[[ChatWorld], Any]]:
    """A busy little game: queue, pause, free discussion, template, freeze, timeouts, closing."""

    def advance(dt: float) -> Callable[[ChatWorld], Any]:
        return lambda w: (w.clock.advance(dt), w.tick())

    return [
        lambda w: new_channel(w, "talk", keep_queues=True),  # 0
        lambda w: new_channel(w, "chat", mode="free", keep_queues=True),
        lambda w: open_queue(w, "talk", ["p01", "p02", "p03"], keep_queues=True),
        lambda w: speak(w, "p01", "talk", "我先说一句"),
        advance(12),
        lambda w: speak(w, "p01", "talk", "字" * 30),  # truncated, the token is used up
        lambda w: speak(w, "p02", "talk", "轮到我"),
        lambda w: mod(w, "pause_queue", channel_id="talk"),
        advance(300),  # the free discussion goes quiet and runs out of time meanwhile
        lambda w: speak(w, "p02", "talk", "暂停中"),  # 9: refused
        lambda w: mod(w, "resume_queue", channel_id="talk"),
        advance(30),
        lambda w: speak_template(w, "p02", "talk", "count", n=3),  # 12: p02 is done, p03 gets the floor
        lambda w: speak(w, "p03", "chat", "自由讨论"),
        lambda w: speak(w, "p04", "chat", "我也说"),
        advance(45),  # silence again
        lambda w: mod(w, "update_channel", channel_id="chat", free_limits={"duration_s": 400}),
        advance(10),
        lambda w: w.chat.freeze_channel("talk", "ability:x"),  # 18: p03 has 5 seconds left
        lambda w: speak(w, "p03", "talk", "冻结中"),  # refused
        lambda w: mod(w, "resume_queue", channel_id="talk"),
        advance(3),  # the extended free discussion reaches its new time limit
        lambda w: do_pass(w, "p03", "talk"),  # the queue is complete: the moderator is woken
        lambda w: mod(w, "update_channel", channel_id="chat", remove_members=["p04"]),
        lambda w: speak(w, "p04", "chat", "我被移出了"),  # refused
        lambda w: open_turn(w, "talk", ["p01", "p04"], keep_queues=True),
        advance(61),  # both tokens time out
        lambda w: mod(w, "close_floor", channel_id="chat"),
    ]


def test_a_world_restored_in_the_middle_of_a_game_behaves_exactly_like_the_original() -> None:
    script = _script()
    for cut in range(1, len(script)):
        reference = make_world()
        for step in script[:cut]:
            step(reference)
        bounce(reference)
        for step in script[cut:]:
            step(reference)

        saved = make_world()
        for step in script[:cut]:
            step(saved)
        resumed = roundtrip(saved)
        for step in script[cut:]:
            step(resumed)

        assert facts(resumed) == facts(reference), f"the ledger differs when restoring after step {cut}"
        # (the rng snapshot is only filled in by dump(): the reference world never dumped)
        assert resumed.state.model_dump(exclude={"rng"}) == reference.state.model_dump(exclude={"rng"}), (
            f"the state differs after step {cut}"
        )
        assert resumed.next_deadline() == reference.next_deadline()
        assert resumed.clock.now() == reference.clock.now()


def test_deliveries_after_a_restore_equal_those_of_the_original() -> None:
    script = _script()
    for cut in (3, 8, 11, 17, 21):
        reference = make_world()
        for step in script[:cut]:
            step(reference)
        resumed_source = make_world()
        for step in script[:cut]:
            step(resumed_source)
        bounce(reference)
        resumed = roundtrip(resumed_source)
        flush(reference)
        flush(resumed)
        for step in script[cut:]:
            step(reference)
            step(resumed)
        original = [(d.target, d.envelope.model_dump()) for d in reference.drain_outbox()]
        restored = [(d.target, d.envelope.model_dump()) for d in resumed.drain_outbox()]
        assert restored == original, f"deliveries differ when restoring after step {cut}"
