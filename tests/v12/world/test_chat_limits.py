"""``limits`` of ``open_floor`` / ``update_channel`` (contracts §10.1.5): ONE object, its keys are checked
against the mode, a wrong key is refused with the full list of the keys the mode accepts and an example."""

from __future__ import annotations

import json

import pytest

from app.world.intents import FREE_LIMIT_KEYS, TOKEN_LIMIT_KEYS
from app.world.types import Code

from .helpers import entries, flush, make_world, mod, new_channel, open_free, open_turn, speak, submit

TOKEN_ONLY = ["max_messages"]
FREE_ONLY = ["max_messages_per_member", "silence_s"]
BOTH = ["max_chars_per_message", "max_chars_total", "duration_s"]


def _world(members=None):
    world = make_world()
    new_channel(world, "c1", members=members)
    return world


def _untouched(world, seq: int) -> None:
    assert world.seq == seq and world.channel("c1").group is None and world.channel("c1").free is None
    assert world.channel("c1").mode == "closed" and world.drain_outbox() == []


# --------------------------------------------------------------------------- the key lists


def test_the_two_key_lists_are_what_the_contract_says() -> None:
    assert TOKEN_LIMIT_KEYS == ("max_messages", "max_chars_per_message", "max_chars_total", "duration_s")
    assert FREE_LIMIT_KEYS == ("duration_s", "max_messages_per_member", "max_chars_per_message", "max_chars_total", "silence_s")
    assert set(TOKEN_LIMIT_KEYS) == set(TOKEN_ONLY) | set(BOTH)
    assert set(FREE_LIMIT_KEYS) == set(FREE_ONLY) | set(BOTH)


# --------------------------------------------------------------------------- open_floor


@pytest.mark.parametrize("key", FREE_ONLY)
def test_a_turn_floor_refuses_the_keys_of_a_free_discussion(key: str) -> None:
    world = _world()
    seq = world.seq
    receipt = mod(world, "open_floor", channel_id="c1", mode="turn", holders=["N1"], limits={key: 2, "duration_s": 30})
    assert not receipt.ok and receipt.code == Code.INVALID_PARAMS
    assert key in receipt.message and "mode=turn 只接受：" in receipt.message
    for allowed in TOKEN_LIMIT_KEYS:  # the complete list, in the documented order
        assert allowed in receipt.message
    assert ", ".join(TOKEN_LIMIT_KEYS) in receipt.message
    assert "正确示例：limits=" in receipt.message
    assert receipt.data["mode"] == "turn" and receipt.data["allowed"] == list(TOKEN_LIMIT_KEYS)
    assert receipt.data["rejected"] == [key]
    example = receipt.data["example"]
    assert set(example) <= set(TOKEN_LIMIT_KEYS) and json.dumps(example, ensure_ascii=False) in receipt.message
    _untouched(world, seq)  # a rejection changes nothing, writes nothing, delivers nothing


@pytest.mark.parametrize("key", TOKEN_ONLY)
def test_a_free_discussion_refuses_the_keys_of_a_token(key: str) -> None:
    world = _world()
    seq = world.seq
    receipt = mod(world, "open_floor", channel_id="c1", mode="free", limits={key: 2})
    assert not receipt.ok and receipt.code == Code.INVALID_PARAMS
    assert key in receipt.message and "mode=free 只接受：" in receipt.message
    assert ", ".join(FREE_LIMIT_KEYS) in receipt.message
    assert receipt.data["allowed"] == list(FREE_LIMIT_KEYS) and receipt.data["rejected"] == [key]
    assert set(receipt.data["example"]) <= set(FREE_LIMIT_KEYS)
    _untouched(world, seq)


def test_the_message_points_at_the_matching_key_of_the_other_mode() -> None:
    world = _world()
    in_turn = mod(world, "open_floor", channel_id="c1", mode="turn", holders=["N1"], limits={"max_messages_per_member": 1})
    assert "mode=turn 里对应的键是 max_messages" in in_turn.message
    in_free = mod(world, "open_floor", channel_id="c1", mode="free", limits={"max_messages": 1})
    assert "自由讨论里对应的键是 max_messages_per_member" in in_free.message
    silence = mod(world, "open_floor", channel_id="c1", mode="turn", holders=["N1"], limits={"silence_s": 5})
    assert "silence_s" in silence.message and "只用于 mode=free" in silence.message


def test_every_wrong_key_is_named() -> None:
    world = _world()
    receipt = mod(
        world, "open_floor", channel_id="c1", mode="turn", holders=["N1"],
        limits={"max_messages_per_member": 1, "silence_s": 5, "duration_s": 10},
    )
    assert receipt.code == Code.INVALID_PARAMS
    assert sorted(receipt.data["rejected"]) == ["max_messages_per_member", "silence_s"]


@pytest.mark.parametrize("key", BOTH)
def test_the_shared_keys_work_in_both_modes(key: str) -> None:
    world = _world()
    assert mod(world, "open_floor", channel_id="c1", mode="turn", holders=["N1"], limits={key: 7}).ok
    token = world.channel("c1").group.tokens[0]
    assert {"max_chars_per_message": token.max_chars_per_message, "max_chars_total": token.max_chars_total, "duration_s": token.duration_s}[key] == 7
    assert mod(world, "open_floor", channel_id="c1", mode="free", limits={key: 7}).ok
    assert getattr(world.channel("c1").free, key) == 7


def test_each_mode_applies_its_own_keys() -> None:
    world = _world()
    receipt = mod(
        world, "open_floor", channel_id="c1", mode="turn", holders=["N1", "N2"],
        limits={"max_messages": 1, "max_chars_per_message": 12, "max_chars_total": 30, "duration_s": 20},
    )
    assert receipt.ok and receipt.data["limits"] == {
        "max_messages": 1, "max_chars_per_message": 12, "max_chars_total": 30, "duration_s": 20.0,
    }
    receipt = mod(
        world, "open_floor", channel_id="c1", mode="free",
        limits={"duration_s": 77, "max_messages_per_member": 2, "max_chars_per_message": 9, "max_chars_total": 55, "silence_s": 8},
    )
    assert receipt.ok and receipt.data["limits"] == {
        "duration_s": 77.0, "max_messages_per_member": 2, "max_chars_per_message": 9, "max_chars_total": 55, "silence_s": 8.0,
    }
    free = world.channel("c1").free
    assert (free.duration_s, free.max_messages_per_member, free.max_chars_per_message, free.max_chars_total, free.silence_s) == (
        77.0, 2, 9, 55, 8.0,
    )


def test_values_are_checked_against_the_range_of_the_mode_after_the_keys() -> None:
    world = _world()
    seq = world.seq
    # 5000 s passes the union (OpenLimits allows 7200) but a token is limited to 3600
    too_long = mod(world, "open_floor", channel_id="c1", mode="turn", holders=["N1"], limits={"duration_s": 5000})
    assert too_long.code == Code.INVALID_PARAMS and "duration_s 不能大于 3600" in too_long.message
    assert "mode=turn 只接受：" in too_long.message and "正确示例" in too_long.message
    too_much = mod(world, "open_floor", channel_id="c1", mode="turn", holders=["N1"], limits={"max_chars_total": 60000})
    assert too_much.code == Code.INVALID_PARAMS and "max_chars_total 不能大于 50000" in too_much.message
    _untouched(world, seq)
    # ... while a free discussion may last two hours and write 200000 characters
    assert mod(world, "open_floor", channel_id="c1", mode="free", limits={"duration_s": 5000, "max_chars_total": 60000}).ok


def test_values_outside_even_the_union_are_refused_by_the_gate() -> None:
    world = _world()
    for limits in ({"duration_s": -1}, {"max_messages": 0}, {"max_chars_per_message": 100000}, {"nonsense": 1}):
        receipt = mod(world, "open_floor", channel_id="c1", mode="free", limits=limits)
        assert receipt.code == Code.INVALID_PARAMS and receipt.message.startswith("参数无效"), limits


def test_an_empty_limits_object_means_the_defaults() -> None:
    world = _world()
    assert mod(world, "open_floor", channel_id="c1", mode="turn", holders=["N1"], limits={}).ok
    assert world.channel("c1").group.tokens[0].max_messages == 2  # defaults.floor of the test scenario
    assert mod(world, "open_floor", channel_id="c1", mode="free", limits={}).ok
    assert world.channel("c1").free.max_messages_per_member == 3


def test_free_limits_is_only_an_alias_of_limits() -> None:
    world = _world()
    receipt = mod(world, "open_floor", channel_id="c1", mode="free", free_limits={"duration_s": 50, "silence_s": 9})
    assert receipt.ok and receipt.data["limits"]["duration_s"] == 50.0 and receipt.data["limits"]["silence_s"] == 9.0
    # the alias is checked against the mode like ``limits`` itself ...
    wrong = mod(world, "open_floor", channel_id="c1", mode="turn", holders=["N1"], free_limits={"silence_s": 9})
    assert wrong.code == Code.INVALID_PARAMS and "mode=turn 只接受：" in wrong.message
    # ... and ``limits`` wins when both are given
    both = mod(world, "open_floor", channel_id="c1", mode="free", limits={"duration_s": 60}, free_limits={"duration_s": 70})
    assert both.ok and both.data["limits"]["duration_s"] == 60.0
    update = mod(world, "update_channel", channel_id="c1", free_limits={"duration_s": 200})
    assert update.ok and update.data["changes"]["limits"] == {"duration_s": 200.0}


def test_set_floor_queue_takes_token_limits_only() -> None:
    world = _world()
    ok = submit(world, "moderator", "mod.set_floor_queue", {"channel_id": "c1", "order": ["N1"], "limits": {"max_messages": 1, "duration_s": 10}})
    assert ok.ok and world.channel("c1").group.tokens[0].max_messages == 1
    refused = submit(world, "moderator", "mod.set_floor_queue", {"channel_id": "c1", "order": ["N1"], "limits": {"silence_s": 5}})
    assert refused.code == Code.INVALID_PARAMS  # FloorLimits has no such key (the gate says so)


# --------------------------------------------------------------------------- update_channel


@pytest.mark.parametrize("key", TOKEN_ONLY)
def test_update_channel_limits_take_the_keys_of_a_free_discussion_only(key: str) -> None:
    world = _world()
    open_free(world, "c1")
    before = world.channel("c1").free.model_dump()
    receipt = mod(world, "update_channel", channel_id="c1", limits={key: 3})
    assert receipt.code == Code.INVALID_PARAMS
    assert "limits 只用于自由讨论（mode=free）" in receipt.message and "mode=free 只接受：" in receipt.message
    assert ", ".join(FREE_LIMIT_KEYS) in receipt.message and "正确示例：limits=" in receipt.message
    assert world.channel("c1").free.model_dump() == before


def test_update_channel_limits_need_a_free_discussion() -> None:
    world = _world()  # closed
    receipt = mod(world, "update_channel", channel_id="c1", limits={"duration_s": 50})
    assert receipt.code == Code.INVALID_PARAMS and "不是自由讨论" in receipt.message
    assert receipt.data["channel_mode"] == "closed" and "open_floor(mode=free" in receipt.message
    open_turn(world, "c1", ["N1"])
    receipt = mod(world, "update_channel", channel_id="c1", limits={"duration_s": 50})
    assert receipt.code == Code.INVALID_PARAMS and receipt.data["channel_mode"] == "turn"
    assert world.channel("c1").group is not None  # the refusal left the floor alone


def test_update_channel_can_turn_a_channel_into_a_free_discussion_with_limits() -> None:
    world = _world()
    receipt = mod(world, "update_channel", channel_id="c1", mode="free", limits={"duration_s": 40, "silence_s": 4})
    assert receipt.ok and receipt.data["changes"]["limits"]["duration_s"] == 40.0
    free = world.channel("c1").free
    assert (free.duration_s, free.silence_s, free.max_messages_per_member) == (40.0, 4.0, 3)
    [entry] = entries(world, "channel.updated")
    assert entry.data["changes"]["limits"]["silence_s"] == 4.0 and "free_limits" not in entry.data["changes"]


def test_update_channel_limits_extend_a_running_discussion_and_re_arm_it() -> None:
    world = _world()
    open_free(world, "c1", duration_s=30)
    world.clock.advance(31)
    world.tick()
    assert world.channel("c1").free.notified.get("time") is True
    flush(world)
    receipt = mod(world, "update_channel", channel_id="c1", limits={"duration_s": 90, "max_messages_per_member": 5})
    assert receipt.ok and receipt.data["changes"]["limits"] == {"duration_s": 90.0, "max_messages_per_member": 5}
    assert world.channel("c1").free.notified == {}
    for i in range(5):
        assert speak(world, "N1", "c1", f"第{i}句").ok
    assert speak(world, "N1", "c1", "第六句").code == Code.QUOTA_EXCEEDED


def test_update_channel_refuses_limits_for_a_system_dm() -> None:
    world = make_world()
    receipt = mod(world, "update_channel", channel_id="dm-N1", limits={"duration_s": 5})
    assert receipt.code == Code.INVALID_PARAMS and "系统私聊" in receipt.message


def test_a_refused_update_changes_nothing_else_either() -> None:
    world = _world()
    open_free(world, "c1")
    seq = world.seq
    receipt = mod(world, "update_channel", channel_id="c1", name="新名字", add_members=[], limits={"max_messages": 1})
    assert receipt.code == Code.INVALID_PARAMS
    assert world.channel("c1").name != "新名字" and world.seq == seq and world.drain_outbox() == []
