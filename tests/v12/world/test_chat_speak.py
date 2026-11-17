"""``speak`` / ``pass`` / ``mod.send_message``: permissions, rejection codes, truncation (D19), system DMs."""

from __future__ import annotations

import pytest

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
    open_queue,
    open_turn,
    speak,
    submit,
)

# --------------------------------------------------------------------------- rejection codes


def test_unknown_channel_lists_the_channels_the_actor_can_use() -> None:
    world = make_world()
    new_channel(world, "c1", members=["p01", "p02"])
    receipt = speak(world, "p01", "nowhere", "hi")
    assert not receipt.ok and receipt.code == Code.UNKNOWN_CHANNEL
    assert receipt.data["channels"] == ["dm-p01", "c1"]  # never somebody else's DM
    assert "nowhere" in receipt.message


def test_not_a_member() -> None:
    world = make_world()
    new_channel(world, "c1", members=["p01", "p02"], mode="free")
    receipt = speak(world, "p03", "c1", "hi")
    assert not receipt.ok and receipt.code == Code.NOT_MEMBER
    assert speak(world, "p02", "dm-p01", "hi").code == Code.NOT_MEMBER  # somebody else's system DM
    assert not msgs(world, "c1") and not msgs(world, "dm-p01")


@pytest.mark.parametrize("mode,fragment", [("closed", "已关闭"), ("listen", "只听")])
def test_closed_and_listen_channels_refuse_players(mode: str, fragment: str) -> None:
    world = make_world()
    new_channel(world, "c1", mode=mode)
    receipt = speak(world, "p01", "c1", "hi")
    assert not receipt.ok and receipt.code == Code.CHANNEL_CLOSED
    assert fragment in receipt.message
    # ... but the moderator can always write
    assert mod(world, "send_message", channel_id="c1", content="只有我能说").ok


def test_turn_channel_without_a_token_means_no_floor() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_turn(world, "c1", ["p01"])
    receipt = speak(world, "p02", "c1", "插话")
    assert not receipt.ok and receipt.code == Code.NO_FLOOR
    assert receipt.data == {"holders": ["p01"], "current": None}
    assert "没有本轮发言权" in receipt.message and "玩家1（p01）" in receipt.message


def test_queue_tells_waiting_and_finished_players_apart() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["p01", "p02", "p03"])
    waiting = speak(world, "p03", "c1", "我先说")
    assert waiting.code == Code.NO_FLOOR and "还没轮到你" in waiting.message
    assert waiting.data == {"holders": ["p01"], "current": "p01"}
    assert do_pass(world, "p01", "c1").ok
    done = speak(world, "p01", "c1", "我再说一句")
    assert done.code == Code.NO_FLOOR and "已经结束" in done.message
    assert done.data["current"] == "p02"


def test_free_channel_quota_is_per_member_and_hard() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")  # defaults.free: 3 messages per member
    for i in range(3):
        assert speak(world, "p01", "c1", f"第{i}句").ok
    receipt = speak(world, "p01", "c1", "第四句")
    assert not receipt.ok and receipt.code == Code.QUOTA_EXCEEDED
    assert receipt.data == {"used": 3, "max_messages": 3}
    assert speak(world, "p02", "c1", "我还没说过").ok  # somebody else is unaffected
    assert mod(world, "send_message", channel_id="c1", content="主持人不受限").ok
    assert len(msgs(world, "c1")) == 5


def test_whitespace_only_content_is_invalid() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")
    receipt = speak(world, "p01", "c1", "   \n ")
    assert not receipt.ok and receipt.code == Code.INVALID_PARAMS
    assert world.channel("c1").free.counts == {}  # nothing was booked


# --------------------------------------------------------------------------- the happy path


def test_a_committed_message_has_the_catalog_shape() -> None:
    world = make_world()
    new_channel(world, "c1", members=["p01", "p02"], mode="free")
    receipt = speak(world, "p01", "c1", "  你好，各位。 ")
    assert receipt.ok and receipt.truncated is None
    [entry] = msgs(world, "c1")
    assert entry.seq == receipt.seq
    assert entry.actor_id == "p01" and entry.channel_id == "c1"
    assert entry.visible_to_ids == ["moderator", "p01", "p02"]
    assert entry.data == {"content": "你好，各位。", "kind": "speech", "sender_name": "玩家1"}


def test_a_token_message_reports_what_is_left() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["p01"])
    world.clock.advance(15)
    receipt = speak(world, "p01", "c1", "一二三四五六七八九十")  # 10 chars
    assert receipt.ok
    assert receipt.floor_left == {"messages": 1, "chars": 40, "seconds": 45.0}


def test_free_receipt_reports_the_personal_quota() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")
    receipt = speak(world, "p01", "c1", "12345")
    assert receipt.floor_left == {"messages": 2, "chars": 95, "seconds": 100.0}


# --------------------------------------------------------------------------- truncation (D19)


def test_overlong_messages_are_truncated_not_rejected() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["p01"])  # 20 chars per message
    text = "甲" * 27
    receipt = speak(world, "p01", "c1", text)
    assert receipt.ok
    assert receipt.truncated == {"dropped_chars": 7, "kept_chars": 20}
    assert "7" in receipt.message and "20" in receipt.message
    [entry] = msgs(world, "c1")
    assert entry.data["content"] == "甲" * 20
    assert entry.data["truncated"] == {"dropped_chars": 7, "kept_chars": 20}
    assert receipt.floor_left["chars"] == 30  # only what was kept counts


def test_a_message_of_exactly_the_limit_is_not_truncated() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["p01"])
    receipt = speak(world, "p01", "c1", "乙" * 20)
    assert receipt.ok and receipt.truncated is None
    assert "truncated" not in msgs(world, "c1")[0].data


def test_the_remaining_total_also_truncates_and_then_the_floor_is_gone() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["p01", "p02"], max_messages=5, max_chars_per_message=20, max_chars_total=30)
    assert speak(world, "p01", "c1", "甲" * 20).ok
    receipt = speak(world, "p01", "c1", "乙" * 20)  # only 10 chars of the total are left
    assert receipt.ok and receipt.truncated == {"dropped_chars": 10, "kept_chars": 10}
    assert receipt.floor_left["chars"] == 0
    assert "用尽" in receipt.message
    # the token ended by itself (reason: used) although messages were left ...
    ended = entries(world, "floor.token_ended")[0]
    assert ended.data["reason"] == "used" and ended.data["messages_used"] == 2 and ended.data["chars_used"] == 30
    # ... so the next attempt is simply "no floor" (the queue moved on to p02)
    again = speak(world, "p01", "c1", "丙")
    assert again.code == Code.NO_FLOOR and again.data["current"] == "p02"


def test_a_zero_char_budget_is_no_floor() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_turn(world, "c1", ["p01"])
    token = world.channel("c1").group.tokens[0]
    token.chars_used = token.max_chars_total  # an exhausted token that somehow stayed active
    receipt = speak(world, "p01", "c1", "还能说吗")
    assert receipt.code == Code.NO_FLOOR and "用尽" in receipt.message


def test_free_messages_are_truncated_to_the_discussion_limit() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")  # defaults.free.max_chars_per_message = 30
    receipt = speak(world, "p01", "c1", "丁" * 45)
    assert receipt.truncated == {"dropped_chars": 15, "kept_chars": 30}
    assert world.channel("c1").free.chars_used == 30  # the advisory total counts kept chars


def test_moderator_free_text_is_truncated_to_its_own_limit() -> None:
    world = make_world()
    new_channel(world, "c1")
    receipt = mod(world, "send_message", channel_id="c1", content="戊" * 55)
    assert receipt.ok and receipt.truncated == {"dropped_chars": 15, "kept_chars": 40}
    assert len(msgs(world, "c1")[0].data["content"]) == 40
    empty = mod(world, "send_message", channel_id="c1", content="  ")
    assert empty.code == Code.INVALID_PARAMS


# --------------------------------------------------------------------------- pass


def test_pass_ends_the_token_and_moves_the_queue_on() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["p01", "p02"])
    receipt = do_pass(world, "p01", "c1")
    assert receipt.ok
    ended = entries(world, "floor.token_ended")[0]
    assert ended.seq == receipt.seq and ended.data["reason"] == "pass"
    assert speak(world, "p02", "c1", "轮到我了").ok


def test_pass_in_a_free_channel_is_a_harmless_no_op() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")
    before = world.seq
    receipt = do_pass(world, "p01", "c1")
    assert receipt.ok and world.seq == before


def test_pass_without_a_token_is_no_floor() -> None:
    world = make_world()
    new_channel(world, "c1")
    assert do_pass(world, "p01", "c1").code == Code.NO_FLOOR  # closed channel
    open_queue(world, "c1", ["p01", "p02"])
    assert do_pass(world, "p02", "c1").code == Code.NO_FLOOR  # still waiting
    assert do_pass(world, "p03", "c1").code == Code.NO_FLOOR  # not in the queue at all
    assert do_pass(world, "p01", "c1").ok
    assert do_pass(world, "p01", "c1").code == Code.NO_FLOOR  # already gave it up


def test_pass_unknown_channel_and_non_member() -> None:
    world = make_world()
    new_channel(world, "c1", members=["p01"])
    assert do_pass(world, "p01", "nowhere").code == Code.UNKNOWN_CHANNEL
    assert do_pass(world, "p02", "c1").code == Code.NOT_MEMBER


# --------------------------------------------------------------------------- system DMs (D23)


def test_players_talk_to_the_moderator_in_their_dm_without_a_token() -> None:
    world = make_world()
    for i in range(10):  # the number of messages is never limited
        assert speak(world, "p01", "dm-p01", f"问题{i}").ok
    assert len(msgs(world, "dm-p01")) == 10


def test_dm_messages_are_cut_to_the_dm_limit() -> None:
    world = make_world()
    receipt = speak(world, "p01", "dm-p01", "己" * 40)  # defaults.dm = 25
    assert receipt.truncated == {"dropped_chars": 15, "kept_chars": 25}
    assert receipt.floor_left is None  # no token, nothing to report


def test_a_closed_system_dm_refuses_the_player_but_not_the_moderator() -> None:
    world = make_world()
    assert mod(world, "update_channel", channel_id="dm-p01", mode="closed").ok
    receipt = speak(world, "p01", "dm-p01", "你好？")
    assert not receipt.ok and receipt.code == Code.CHANNEL_CLOSED
    assert "你与主持人的私聊已被关闭" in receipt.message
    template = submit(world, "p01", "speak_template", {"channel_id": "dm-p01", "template_id": "count", "params": {"n": 1}})
    assert template.code == Code.CHANNEL_CLOSED
    assert do_pass(world, "p01", "dm-p01").code == Code.NO_FLOOR
    # the moderator may still write (and announce) there
    assert mod(world, "send_message", channel_id="dm-p01", content="请安静").ok
    assert mod(world, "announce", channel_id="dm-p01", template_id="notice", params={"text": "已警告"}).ok
    # other players are unaffected
    assert speak(world, "p02", "dm-p02", "我正常").ok
    # re-opening restores the player's voice
    assert mod(world, "update_channel", channel_id="dm-p01", mode="free").ok
    assert speak(world, "p01", "dm-p01", "谢谢").ok


def test_readonly_members_cannot_write_in_a_free_channel() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")
    assert mod(world, "update_channel", channel_id="c1", readonly_members=["p02"]).ok
    receipt = speak(world, "p02", "c1", "我能说吗")
    assert receipt.code == Code.NO_FLOOR and "只读" in receipt.message
    assert speak(world, "p01", "c1", "我可以").ok


def test_speaking_does_not_leak_into_other_channels() -> None:
    world = make_world()
    new_channel(world, "c1", members=["p01", "p02"], mode="free")
    new_channel(world, "c2", members=["p03", "p04"], mode="free")
    assert speak(world, "p01", "c1", "只有 c1 听得到").ok
    seen = {pid: [e.data["content"] for e in world.entries_visible(pid) if e.type == "message"] for pid in PLAYERS}
    assert seen["p01"] == seen["p02"] == ["只有 c1 听得到"]
    assert seen["p03"] == seen["p04"] == []
    assert [e for e in world.entries_visible(MODERATOR_ID) if e.type == "message"]
    deliveries, _ = flush(world)
    assert {d.target for d in deliveries if d.envelope.kind == "message"} == {"moderator", "p02"}
    assert envelopes(deliveries, target="p03") == []
