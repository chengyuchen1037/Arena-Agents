"""Free discussions (D13, D20): hard per-member quota, advisory-only time / chars / silence."""

from __future__ import annotations

from app.world.chat import floor_view
from app.world.types import MODERATOR_ID, Code

from .helpers import (
    do_pass,
    entries,
    envelopes,
    flush,
    make_world,
    mod,
    new_channel,
    open_free,
    speak,
    targets,
    ui,
)

# defaults.free in the test scenario: 100 s, 3 messages each, 30 chars each, 100 chars in total, silence 40 s


def _free(**limits):
    world = make_world()
    new_channel(world, "c1", mode="free")
    if limits:
        assert mod(world, "update_channel", channel_id="c1", limits=limits).ok
        flush(world)
    return world


def _advisories(world) -> list[dict]:
    deliveries = world.drain_outbox()
    out = []
    for d in deliveries:
        if d.envelope.payload.get("event") == "free.advisory":
            assert d.target == MODERATOR_ID
            out.append(d.envelope.payload)
    return out


# --------------------------------------------------------------------------- opening a free discussion


def test_a_free_channel_starts_with_the_scenario_defaults() -> None:
    world = make_world()
    world.clock.set(12)
    new_channel(world, "c1", mode="free")
    free = world.channel("c1").free
    assert (free.duration_s, free.max_messages_per_member, free.max_chars_per_message) == (100, 3, 30)
    assert (free.max_chars_total, free.silence_s) == (100, 40)
    assert not hasattr(free, "debounce_s")  # v0.12.1: nobody waits before being woken
    assert free.writers is None and free.counts == {} and free.chars_used == 0 and free.notified == {}
    assert free.started_gt == 12 and free.last_message_gt == 12


def test_open_floor_free_with_writers_and_limits() -> None:
    world = make_world()
    new_channel(world, "c1")
    receipt = open_free(world, "c1", holders=["N1", "N2"], max_messages_per_member=1, duration_s=50, keep_queues=True)
    assert receipt.ok and receipt.data["writers"] == ["N1", "N2"]
    free = world.channel("c1").free
    assert world.channel("c1").mode == "free" and free.writers == ["N1", "N2"]
    assert (free.max_messages_per_member, free.duration_s, free.silence_s) == (1, 50, 40)  # the rest is default
    [opened] = entries(world, "floor.opened")
    assert opened.data["group_id"] is None and opened.data["mode"] == "free" and opened.data["holders"] == ["N1", "N2"]
    assert opened.data["limits"]["max_messages_per_member"] == 1
    assert speak(world, "N1", "c1", "我在名单里").ok
    outsider = speak(world, "N3", "c1", "我不在")
    assert outsider.code == Code.NO_FLOOR and outsider.data == {"writers": ["N1", "N2"]}
    assert speak(world, "N1", "c1", "第二句").code == Code.QUOTA_EXCEEDED
    deliveries, events = flush(world)
    assert ui(events, "floor.changed")[0]["floor"]["kind"] == "free"
    # opening a free discussion hands out no tokens, so nobody is woken up by it
    assert not envelopes(deliveries, event="floor.granted")


def test_reopening_a_free_discussion_restarts_it() -> None:
    world = _free()
    assert speak(world, "N1", "c1", "一").ok
    world.clock.advance(70)
    world.tick()
    flush(world)
    assert world.channel("c1").free.notified  # an advisory fired
    open_free(world, "c1")
    free = world.channel("c1").free
    assert free.counts == {} and free.chars_used == 0 and free.notified == {} and free.started_gt == 70


# --------------------------------------------------------------------------- hard limits


def test_the_quota_counts_messages_and_chars_of_players_only() -> None:
    world = _free()
    assert speak(world, "N1", "c1", "abcde").ok
    assert speak(world, "N2", "c1", "fgh").ok
    assert mod(world, "send_message", channel_id="c1", content="主持人说的话不计入").ok
    free = world.channel("c1").free
    assert free.counts == {"N1": 1, "N2": 1} and free.chars_used == 8


def test_advisory_limits_never_block_speech() -> None:
    world = _free()
    for pid in ("N1", "N2", "N3", "N4"):  # 4 x 3 x 30 chars = 360 > 100 advisory total
        for _ in range(3):
            assert speak(world, pid, "c1", "字" * 30).ok
    world.clock.advance(500)  # far beyond duration_s
    assert world.channel("c1").free.chars_used == 360
    assert speak(world, "N1", "c1", "多说一句").code == Code.QUOTA_EXCEEDED  # only the quota is hard


def test_pass_is_a_no_op_in_free_discussions() -> None:
    world = _free()
    seq = world.seq
    assert do_pass(world, "N1", "c1").ok and world.seq == seq


# --------------------------------------------------------------------------- advisories (D20)


def test_time_advisory_fires_once_and_the_channel_stays_open() -> None:
    world = _free(silence_s=1000)
    world.clock.advance(99.9)
    world.tick()
    assert _advisories(world) == []
    world.clock.advance(0.1)
    world.tick()
    [advisory] = _advisories(world)
    assert advisory["reason"] == "time" and advisory["channel_id"] == "c1"
    assert advisory["duration_s"] == 100 and advisory["elapsed_s"] == 100.0
    world.tick()
    world.clock.advance(50)
    world.tick()
    assert _advisories(world) == []  # once
    channel = world.channel("c1")
    assert channel.mode == "free" and channel.free is not None  # never closed by itself
    assert speak(world, "N1", "c1", "还可以继续聊").ok


def test_chars_advisory_fires_on_the_tick_after_the_total_is_reached() -> None:
    world = _free(silence_s=1000)
    for pid, n in (("N1", 30), ("N1", 30), ("N2", 30)):
        assert speak(world, pid, "c1", "字" * n).ok
    flush(world)
    world.tick()
    assert _advisories(world) == []  # 90 < 100
    assert speak(world, "N3", "c1", "字" * 30).ok  # 120 chars now
    assert _advisories(world) == []  # speaking itself never notifies: only tick() does
    world.tick()
    [advisory] = _advisories(world)
    assert advisory["reason"] == "chars" and advisory["chars_used"] == 120 and advisory["max_chars_total"] == 100
    assert advisory["message_counts"] == {"N1": 2, "N2": 1, "N3": 1}
    assert speak(world, "N4", "c1", "字" * 30).ok
    world.tick()
    assert _advisories(world) == []


def test_silence_advisory_counts_from_the_start_when_nobody_spoke() -> None:
    world = make_world()
    world.clock.set(10)
    new_channel(world, "c1", mode="free")
    world.clock.advance(39.9)
    world.tick()
    assert _advisories(world) == []
    world.clock.advance(0.1)
    world.tick()
    [advisory] = _advisories(world)
    assert advisory["reason"] == "silence" and advisory["silent_s"] == 40.0
    world.clock.advance(100)
    world.tick()
    assert [a["reason"] for a in _advisories(world)] == ["time"]  # silence already told, time is new


def test_a_new_message_re_arms_the_silence_advisory() -> None:
    world = _free(duration_s=5000)
    world.clock.advance(40)
    world.tick()
    assert [a["reason"] for a in _advisories(world)] == ["silence"]
    world.clock.advance(10)
    assert speak(world, "N1", "c1", "有人说话了").ok
    flush(world)
    world.clock.advance(39)
    world.tick()
    assert _advisories(world) == []  # silence is measured from the last message
    world.clock.advance(1)
    world.tick()
    assert [a["reason"] for a in _advisories(world)] == ["silence"]  # a second time, after a new message


def test_moderator_messages_also_break_the_silence() -> None:
    world = _free(duration_s=5000)
    world.clock.advance(30)
    assert mod(world, "send_message", channel_id="c1", content="大家怎么看？").ok
    flush(world)
    world.clock.advance(39)
    world.tick()
    assert _advisories(world) == []
    world.clock.advance(1)
    world.tick()
    assert [a["reason"] for a in _advisories(world)] == ["silence"]


def test_all_reasons_can_fire_in_the_same_tick_each_once() -> None:
    world = _free()
    for pid in ("N1", "N2"):
        assert speak(world, pid, "c1", "字" * 30).ok and speak(world, pid, "c1", "字" * 30).ok
    flush(world)
    world.clock.advance(200)
    world.tick()
    assert sorted(a["reason"] for a in _advisories(world)) == ["chars", "silence", "time"]
    world.clock.advance(200)
    world.tick()
    assert _advisories(world) == []


def test_the_advisory_is_an_urgent_floor_envelope_for_the_moderator() -> None:
    world = _free(silence_s=1000)
    assert speak(world, "N2", "c1", "你好").ok
    flush(world)
    world.clock.advance(100)
    world.tick()
    deliveries, _ = flush(world)
    assert [d.target for d in deliveries] == ["moderator"]
    [env] = [d.envelope for d in deliveries]
    assert (env.kind, env.priority, env.interrupt, env.channel_id) == ("floor", "urgent", False, "c1")
    assert env.payload["event"] == "free.advisory" and env.payload["reason"] == "time"
    assert env.payload["message_counts"] == {"N2": 1} and env.payload["chars_used"] == 2
    assert env.payload["max_messages_per_member"] == 3 and env.payload["writers"] is None


def test_new_free_limits_re_arm_every_advisory() -> None:
    world = _free()
    world.clock.advance(100)
    world.tick()
    assert sorted(a["reason"] for a in _advisories(world)) == ["silence", "time"]
    assert world.channel("c1").free.notified == {"time": True, "silence": True}
    # the limits are absolute (measured from the start): 180 s in total, i.e. 80 s more
    receipt = mod(world, "update_channel", channel_id="c1", limits={"duration_s": 180, "max_messages_per_member": 4})
    assert receipt.ok and receipt.data["changes"]["limits"] == {"duration_s": 180, "max_messages_per_member": 4}
    assert world.channel("c1").free.notified == {}
    world.clock.advance(39)
    world.tick()
    assert _advisories(world) == []
    world.clock.advance(1)  # t=140: 40 s without a message again
    world.tick()
    assert [a["reason"] for a in _advisories(world)] == ["silence"]
    world.clock.advance(40)  # t=180
    world.tick()
    assert [a["reason"] for a in _advisories(world)] == ["time"]


def test_limits_apply_immediately_to_the_hard_limits() -> None:
    world = _free()
    for i in range(3):
        assert speak(world, "N1", "c1", f"第{i}句").ok
    assert speak(world, "N1", "c1", "不行").code == Code.QUOTA_EXCEEDED
    assert mod(world, "update_channel", channel_id="c1", limits={"max_messages_per_member": 5}).ok
    assert speak(world, "N1", "c1", "现在可以了").ok
    assert mod(world, "update_channel", channel_id="c1", limits={"max_chars_per_message": 3}).ok
    assert speak(world, "N2", "c1", "一二三四五").truncated == {"dropped_chars": 2, "kept_chars": 3}


def test_limits_must_not_already_be_exceeded() -> None:
    world = _free()
    assert speak(world, "N1", "c1", "字" * 30).ok
    world.clock.advance(60)
    elapsed = mod(world, "update_channel", channel_id="c1", limits={"duration_s": 50})
    assert elapsed.code == Code.INVALID_PARAMS and "60" in elapsed.message
    chars = mod(world, "update_channel", channel_id="c1", limits={"max_chars_total": 30})
    assert chars.code == Code.INVALID_PARAMS and "30" in chars.message
    assert world.channel("c1").free.duration_s == 100 and world.channel("c1").free.max_chars_total == 100


def test_limits_only_make_sense_for_free_channels() -> None:
    world = make_world()
    new_channel(world, "c1")  # closed
    refused = mod(world, "update_channel", channel_id="c1", limits={"duration_s": 5})
    assert refused.code == Code.INVALID_PARAMS and "不是自由讨论" in refused.message
    # entering free mode at the same time is fine
    receipt = mod(world, "update_channel", channel_id="c1", mode="free", limits={"duration_s": 5, "silence_s": 3})
    assert receipt.ok
    free = world.channel("c1").free
    assert (free.duration_s, free.silence_s, free.max_messages_per_member) == (5, 3, 3)


def test_next_deadline_follows_the_pending_advisories() -> None:
    world = _free()
    assert world.next_deadline() == 40.0  # silence
    world.clock.advance(40)
    world.tick()
    flush(world)
    assert world.next_deadline() == 100.0  # time
    world.clock.advance(60)
    world.tick()
    flush(world)
    assert world.next_deadline() is None  # nothing left to tell the moderator
    assert speak(world, "N1", "c1", "字" * 30).ok and speak(world, "N2", "c1", "字" * 30).ok
    assert world.next_deadline() == 140.0  # the silence timer is armed again by the new messages (t=100 + 40 s)
    assert speak(world, "N3", "c1", "字" * 30).ok and speak(world, "N4", "c1", "字" * 30).ok
    assert world.next_deadline() == world.clock.now()  # the chars advisory is due at the next tick


# --------------------------------------------------------------------------- ending a free discussion


def test_close_floor_ends_the_discussion_and_tells_the_writers() -> None:
    world = _free()
    assert mod(world, "update_channel", channel_id="c1", readonly_members=["N4"]).ok
    flush(world)
    receipt = mod(world, "close_floor", channel_id="c1")
    assert receipt.ok
    channel = world.channel("c1")
    assert channel.mode == "closed" and channel.free is None
    closed = entries(world, "floor.closed")[0]
    assert closed.data == {"group_id": None, "channel_id": "c1", "reason": "closed"}
    deliveries, events = flush(world)
    assert sorted(targets(deliveries, "floor.revoked")) == ["N1", "N2", "N3"]  # not the read-only N4
    [env] = envelopes(deliveries, target="N1", event="floor.revoked")
    assert (env.priority, env.interrupt, env.payload["reason"]) == ("normal", True, "closed")
    assert ui(events, "floor.changed")[-1]["floor"]["kind"] == "none"
    assert speak(world, "N1", "c1", "还能说吗").code == Code.CHANNEL_CLOSED
    world.clock.advance(1000)
    world.tick()
    assert _advisories(world) == []  # a closed channel has no advisories


def test_free_view_reports_progress() -> None:
    world = _free()
    assert speak(world, "N1", "c1", "字" * 10).ok
    world.clock.advance(7.25)
    assert speak(world, "N2", "c1", "字" * 5).ok
    world.clock.advance(3)
    view = floor_view(world, world.channel("c1"))
    assert view["kind"] == "free" and view["active"] is None and view["queue"] == []
    assert view["free"]["chars_used"] == 15 and view["free"]["max_chars_total"] == 100
    assert view["free"]["elapsed_s"] == 10.2 and view["free"]["silent_s"] == 3.0 and view["free"]["duration_s"] == 100.0
    assert view["free"]["message_counts"] == {"N1": 1, "N2": 1}
    assert (view["free"]["max_messages_per_member"], view["free"]["writers"]) == (3, None)


def test_a_free_message_wakes_every_member_who_can_read_it() -> None:
    world = _free()
    assert mod(world, "update_channel", channel_id="c1", readonly_members=["N4"]).ok
    flush(world)
    assert speak(world, "N1", "c1", "大家好").ok
    deliveries, _ = flush(world)
    priority = {d.target: d.envelope.priority for d in deliveries if d.envelope.payload["event"] == "message.new"}
    # the moderator, the other writers *and* the read-only member: everybody but the author
    assert priority == {"moderator": "normal", "N2": "normal", "N3": "normal", "N4": "normal"}


def test_members_who_cannot_answer_any_more_are_woken_all_the_same() -> None:
    world = _free()
    for i in range(3):  # N2 uses up its quota
        assert speak(world, "N2", "c1", f"第{i}句").ok
    flush(world)
    assert speak(world, "N1", "c1", "N2 还能回答吗").ok
    deliveries, _ = flush(world)
    assert {d.target: d.envelope.priority for d in deliveries} == {
        "moderator": "normal", "N2": "normal", "N3": "normal", "N4": "normal",
    }  # N2 can read it (and see that it cannot answer): no engine-side saving


def test_writers_outside_the_allowed_set_are_woken_too() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_free(world, "c1", holders=["N1", "N2"])
    assert speak(world, "N1", "c1", "名单外的人只能听").ok
    deliveries, _ = flush(world)
    assert {d.target: d.envelope.priority for d in deliveries} == {
        "moderator": "normal", "N2": "normal", "N3": "normal", "N4": "normal",
    }


def test_a_frozen_free_channel_still_delivers_the_moderators_words() -> None:
    world = _free()
    world.chat.freeze_channel("c1", "ability:x")
    flush(world)
    assert mod(world, "send_message", channel_id="c1", content="冻结中的公告").ok
    deliveries, _ = flush(world)
    assert {d.target for d in deliveries} == {"N1", "N2", "N3", "N4"}


def test_no_debounce_hint_is_attached_to_any_delivery() -> None:
    world = _free()
    assert speak(world, "N1", "c1", "大家好").ok
    assert speak(world, "N1", "dm-N1", "私聊").ok
    deliveries = world.drain_outbox()
    assert deliveries and all("debounce_s" not in d.envelope.payload for d in deliveries)
