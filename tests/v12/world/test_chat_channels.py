"""Channels: creation, system DMs (D23), updates, membership, visibility frozen at write time, deliveries."""

from __future__ import annotations

from app.world.chat import channel_view, system_dm_id
from app.world.state import WorldState
from app.world.types import MODERATOR_ID, Code

from .helpers import (
    PLAYERS,
    ChatWorld,
    ManualClock,
    entries,
    envelopes,
    flush,
    make_players,
    make_spec,
    make_world,
    mod,
    msgs,
    new_channel,
    open_queue,
    open_turn,
    speak,
    targets,
    ui,
)

# --------------------------------------------------------------------------- mod.create_channel


def test_create_channel_records_notifies_and_emits() -> None:
    world = make_world()
    receipt = new_channel(world, "ops", members=["p02", "p01", "p02"], mode="listen", type_="public", name="广播", keep_queues=True)
    assert receipt.data == {"channel_id": "ops", "mode": "listen", "members": ["moderator", "p02", "p01"]}
    channel = world.channel("ops")
    assert (channel.type, channel.mode, channel.name) == ("public", "listen", "广播")
    assert not channel.system and channel.owner is None and channel.free is None and channel.group is None

    [entry] = entries(world, "channel.created", channel="ops")
    assert entry.seq == receipt.seq and entry.actor_id == MODERATOR_ID
    assert entry.visible_to_ids == ["moderator", "p02", "p01"]
    assert entry.data == channel_view(world, channel)

    deliveries, events = flush(world)
    assert targets(deliveries, "channel.created") == ["p02", "p01"]  # the moderator made it, it needs no notice
    [env] = envelopes(deliveries, target="p01", event="channel.created")
    assert (env.kind, env.priority, env.interrupt, env.channel_id, env.sender) == ("notice", "ambient", False, "ops", "moderator")
    assert env.payload == {"event": "channel.created", "channel_id": "ops", "name": "广播", "mode": "listen", "type": "public"}
    assert [e.type for e in events] == ["channel.created"]
    assert events[0].data["channel"] == channel_view(world, channel)


def test_the_moderator_is_always_first_and_never_duplicated() -> None:
    world = make_world()
    new_channel(world, "a", members=["moderator", "p03", "p01"])
    new_channel(world, "b", members=[])
    assert world.channel("a").members == ["moderator", "p03", "p01"]
    assert world.channel("b").members == ["moderator"]


def test_a_new_channel_in_free_mode_gets_the_discussion_defaults() -> None:
    world = make_world()
    new_channel(world, "chat", mode="free")
    assert world.channel("chat").free is not None and world.channel("chat").free.max_messages_per_member == 3
    new_channel(world, "quiet", mode="closed")
    assert world.channel("quiet").free is None


def test_create_channel_rejections_leave_no_trace() -> None:
    world = make_world()
    new_channel(world, "c1")
    seq = world.seq
    again = mod(world, "create_channel", channel_id="c1", name="重复")
    assert again.code == Code.ALREADY_EXISTS and "c1" in again.message
    assert mod(world, "create_channel", channel_id="dm-p01", name="冒充").code == Code.ALREADY_EXISTS
    ghost = mod(world, "create_channel", channel_id="c2", name="x", members=["p01", "ghost"])
    assert ghost.code == Code.UNKNOWN_PLAYER and ghost.data["unknown"] == ["ghost"]
    assert world.channel("c2") is None and world.seq == seq


# --------------------------------------------------------------------------- system DMs (D23)


def test_system_dms_are_created_for_every_player() -> None:
    world = ChatWorld(make_spec(), ManualClock(), state=WorldState(players=make_players(3)))
    created = world.chat.create_system_dms()
    assert [c.id for c in created] == ["dm-p01", "dm-p02", "dm-p03"] == [system_dm_id(p.id) for p in world.state.players]
    dm = world.channel("dm-p02")
    assert (dm.type, dm.mode, dm.system, dm.owner) == ("dm", "free", True, "p02")
    assert dm.members == ["moderator", "p02"] and dm.name == "私聊 · 玩家2"
    assert dm.free is None and dm.group is None and not dm.frozen
    made = entries(world, "channel.created")
    assert [e.actor_id for e in made] == ["engine"] * 3
    assert made[1].visible_to_ids == ["moderator", "p02"] and made[1].data["system"] is True
    assert world.drain_outbox() == []  # nobody is told: the DM is infrastructure
    assert [e.type for e in world.drain_events()] == ["channel.created"] * 3
    # idempotent
    assert world.chat.create_system_dms() == []
    assert len(entries(world, "channel.created")) == 3


def test_a_system_dm_can_be_closed_renamed_and_reopened_but_not_reshaped() -> None:
    world = make_world()
    assert mod(world, "update_channel", channel_id="dm-p01", add_members=["p02"]).code == Code.FORBIDDEN
    assert mod(world, "update_channel", channel_id="dm-p01", remove_members=["p01"]).code == Code.FORBIDDEN
    assert mod(world, "update_channel", channel_id="dm-p01", readonly_members=["p01"]).code == Code.FORBIDDEN
    assert mod(world, "update_channel", channel_id="dm-p01", free_limits={"duration_s": 5}).code == Code.INVALID_PARAMS
    assert world.channel("dm-p01").members == ["moderator", "p01"]
    assert mod(world, "update_channel", channel_id="dm-p01", mode="closed", name="惩罚中").ok
    assert world.channel("dm-p01").mode == "closed" and world.channel("dm-p01").name == "惩罚中"
    assert mod(world, "update_channel", channel_id="dm-p01", mode="free").ok
    dm = world.channel("dm-p01")
    assert dm.mode == "free" and dm.free is None  # re-opening a DM never turns it into a limited discussion
    assert speak(world, "p01", "dm-p01", "一" * 10).ok


# --------------------------------------------------------------------------- mod.update_channel


def test_rename_members_and_readonly_in_one_call() -> None:
    world = make_world()
    new_channel(world, "c1", members=["p01", "p02", "p03"], mode="free")
    receipt = mod(
        world, "update_channel", channel_id="c1", name="新名字", add_members=["p04"], remove_members=["p03"], readonly_members=["p02"]
    )
    assert receipt.ok
    channel = world.channel("c1")
    assert channel.name == "新名字" and channel.members == ["moderator", "p01", "p02", "p04"] and channel.readonly == ["p02"]
    assert receipt.data["changes"] == {"name": "新名字", "added": ["p04"], "removed": ["p03"], "readonly": ["p02"]}
    [entry] = entries(world, "channel.updated")
    assert entry.data == {"changes": receipt.data["changes"]}
    # visible to the old and the new members
    assert entry.visible_to_ids == ["moderator", "p01", "p02", "p03", "p04"]
    deliveries, events = flush(world)
    assert sorted(targets(deliveries, "channel.updated")) == ["p01", "p02", "p03", "p04"]
    [env] = envelopes(deliveries, target="p03", event="channel.updated")  # the removed member is told too
    assert (env.kind, env.priority, env.interrupt) == ("notice", "ambient", False)
    assert env.payload["removed"] == ["p03"] and env.payload["added"] == ["p04"] and env.payload["name"] == "新名字"
    assert ui(events, "channel.updated")[-1]["channel"]["members"] == ["moderator", "p01", "p02", "p04"]


def test_update_channel_validation() -> None:
    world = make_world()
    new_channel(world, "c1", members=["p01", "p02"], mode="free")
    seq = world.seq
    assert mod(world, "update_channel", channel_id="nowhere", name="x").code == Code.UNKNOWN_CHANNEL
    assert mod(world, "update_channel", channel_id="c1", remove_members=["moderator"]).code == Code.FORBIDDEN
    assert mod(world, "update_channel", channel_id="c1", add_members=["ghost"]).code == Code.UNKNOWN_PLAYER
    assert mod(world, "update_channel", channel_id="c1", remove_members=["ghost"]).code == Code.UNKNOWN_PLAYER
    absent = mod(world, "update_channel", channel_id="c1", remove_members=["p03"])
    assert absent.code == Code.NOT_MEMBER and absent.data["not_members"] == ["p03"]
    both = mod(world, "update_channel", channel_id="c1", add_members=["p03"], remove_members=["p03"])
    assert both.code == Code.INVALID_PARAMS
    assert mod(world, "update_channel", channel_id="c1", readonly_members=["moderator"]).code == Code.FORBIDDEN
    assert mod(world, "update_channel", channel_id="c1", readonly_members=["ghost"]).code == Code.UNKNOWN_PLAYER
    outside = mod(world, "update_channel", channel_id="c1", readonly_members=["p03"])
    assert outside.code == Code.NOT_MEMBER and outside.data["not_members"] == ["p03"]
    # a read-only member must be a member *after* this very call
    assert mod(world, "update_channel", channel_id="c1", add_members=["p03"], readonly_members=["p03"]).ok
    assert world.seq > seq and world.channel("c1").readonly == ["p03"]


def test_adding_an_existing_member_is_ignored_and_a_no_op_writes_nothing() -> None:
    world = make_world()
    new_channel(world, "c1", members=["p01"])
    seq = world.seq
    receipt = mod(world, "update_channel", channel_id="c1", add_members=["p01", "moderator"])
    assert receipt.ok and receipt.seq is None and "没有需要更新" in receipt.message
    assert mod(world, "update_channel", channel_id="c1", mode="closed", name="频道c1").ok  # same values
    assert world.seq == seq and world.drain_events() == [] and world.drain_outbox() == []


def test_removing_a_member_also_drops_them_from_the_readonly_list() -> None:
    world = make_world()
    new_channel(world, "c1", members=["p01", "p02"], mode="free")
    assert mod(world, "update_channel", channel_id="c1", readonly_members=["p02"]).ok
    receipt = mod(world, "update_channel", channel_id="c1", remove_members=["p02"])
    assert receipt.ok and world.channel("c1").readonly == []
    assert receipt.data["changes"] == {"removed": ["p02"], "readonly": []}


def test_closed_channel_can_become_a_free_discussion_with_limits() -> None:
    world = make_world()
    new_channel(world, "c1")
    receipt = mod(world, "update_channel", channel_id="c1", mode="free", free_limits={"max_messages_per_member": 1})
    assert receipt.ok
    assert receipt.data["changes"]["mode"] == {"from": "closed", "to": "free"}
    free = world.channel("c1").free
    assert (free.max_messages_per_member, free.duration_s, free.started_gt) == (1, 100, 0.0)
    assert receipt.data["changes"]["free_limits"]["max_messages_per_member"] == 1
    assert speak(world, "p01", "c1", "一").ok and speak(world, "p01", "c1", "二").code == Code.QUOTA_EXCEEDED


def test_leaving_free_mode_ends_the_discussion_and_tells_the_writers() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")
    assert mod(world, "update_channel", channel_id="c1", readonly_members=["p04"]).ok
    assert speak(world, "p01", "c1", "一句").ok
    flush(world)
    receipt = mod(world, "update_channel", channel_id="c1", mode="listen")
    assert receipt.ok and receipt.data["changes"] == {"mode": {"from": "free", "to": "listen"}}
    assert world.channel("c1").free is None and world.channel("c1").mode == "listen"
    deliveries, _ = flush(world)
    assert sorted(targets(deliveries, "floor.revoked")) == ["p01", "p02", "p03"]
    [env] = envelopes(deliveries, target="p01", event="floor.revoked")
    assert env.interrupt and env.priority == "ambient" and env.payload["reason"] == "mode_changed"
    assert speak(world, "p01", "c1", "还能说").code == Code.CHANNEL_CLOSED


def test_changing_the_mode_of_a_floor_channel_revokes_its_tokens() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["p01", "p02"])
    receipt = mod(world, "update_channel", channel_id="c1", mode="free")
    assert receipt.ok
    channel = world.channel("c1")
    assert channel.group is None and channel.mode == "free" and channel.free is not None
    assert receipt.data["changes"]["mode"] == {"from": "queue", "to": "free"}
    ended = entries(world, "floor.token_ended")
    assert [(e.data["holder"], e.data["reason"], e.data["cause"]) for e in ended] == [("p01", "revoked", "mode_changed")]
    deliveries, _ = flush(world)
    assert targets(deliveries, "floor.revoked") == ["p01"]
    assert not entries(world, "floor.completed")  # a moderator's own change does not wake it


def test_a_floor_is_not_started_again_by_a_same_mode_update() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_turn(world, "c1", ["p01"])
    seq = world.seq
    assert mod(world, "update_channel", channel_id="c1", name="改个名").ok
    assert world.channel("c1").group is not None and world.channel("c1").mode == "turn"
    assert not entries(world, "floor.", after=seq, prefix=True)


# --------------------------------------------------------------------------- visibility is frozen at write time


def test_removed_members_do_not_see_what_comes_after_but_keep_what_came_before() -> None:
    world = make_world()
    new_channel(world, "c1", members=["p01", "p02", "p03"], mode="free")
    assert speak(world, "p01", "c1", "之前").ok
    assert mod(world, "update_channel", channel_id="c1", remove_members=["p02"]).ok
    assert speak(world, "p01", "c1", "之后").ok
    assert mod(world, "send_message", channel_id="c1", content="主持人也说了").ok

    def seen(agent: str) -> list[str]:
        return [e.data["content"] for e in world.entries_visible(agent) if e.type == "message" and e.channel_id == "c1"]

    assert seen("p02") == ["之前"]
    assert seen("p01") == seen("p03") == seen(MODERATOR_ID) == ["之前", "之后", "主持人也说了"]
    # the removal itself is visible to the removed member (it is a fact about them) ...
    assert [e.type for e in world.entries_visible("p02") if e.channel_id == "c1"][-1] == "channel.updated"
    # ... and p02 can no longer write
    assert speak(world, "p02", "c1", "我还在吗").code == Code.NOT_MEMBER
    # visibility of an old entry never changes retroactively
    assert msgs(world, "c1")[0].visible_to_ids == ["moderator", "p01", "p02", "p03"]


def test_added_members_only_see_what_comes_after() -> None:
    world = make_world()
    new_channel(world, "c1", members=["p01"], mode="free")
    assert speak(world, "p01", "c1", "早").ok
    assert mod(world, "update_channel", channel_id="c1", add_members=["p02"]).ok
    assert speak(world, "p01", "c1", "晚").ok
    texts = [e.data["content"] for e in world.entries_visible("p02") if e.type == "message"]
    assert texts == ["晚"]
    assert msgs(world, "c1")[0].visible_to_ids == ["moderator", "p01"]
    assert msgs(world, "c1")[1].visible_to_ids == ["moderator", "p01", "p02"]


def test_other_players_never_see_a_dm_but_the_moderator_sees_all() -> None:
    world = make_world()
    assert speak(world, "p01", "dm-p01", "私密问题").ok
    assert mod(world, "send_message", channel_id="dm-p02", content="给 p02 的话").ok
    for pid in PLAYERS:
        visible = [e.data["content"] for e in world.entries_visible(pid) if e.type == "message"]
        expected = {"p01": ["私密问题"], "p02": ["给 p02 的话"]}.get(pid, [])
        assert visible == expected, pid
    assert [e.data["content"] for e in world.entries_visible(MODERATOR_ID) if e.type == "message"] == ["私密问题", "给 p02 的话"]


def test_entries_visible_supports_after_seq_and_limit() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")
    for i in range(3):
        assert speak(world, "p01", "c1", f"第{i}句").ok
    first = msgs(world, "c1")[0]
    later = world.entries_visible("p02", after_seq=first.seq)
    assert [e.type for e in later] == ["message", "message"]
    newest = world.entries_visible("p02", limit=2)
    assert [e.data["content"] for e in newest] == ["第1句", "第2句"]


def test_floor_entries_are_visible_to_the_channel_members_only() -> None:
    world = make_world()
    new_channel(world, "c1", members=["p01", "p02"])
    open_queue(world, "c1", ["p01", "p02"])
    assert speak(world, "p01", "c1", "x").ok
    for e in entries(world, "floor.", channel="c1", prefix=True):
        assert e.visible_to_ids == ["moderator", "p01", "p02"], e.type
    assert not [e for e in world.entries_visible("p03") if e.type.startswith("floor.")]


# --------------------------------------------------------------------------- delivery priorities of messages


def test_system_dm_messages_wake_the_other_side_with_normal_priority() -> None:
    world = make_world()
    assert speak(world, "p01", "dm-p01", "主持人，我有疑问").ok
    deliveries, _ = flush(world)
    [env] = envelopes(deliveries, target="moderator")
    assert (env.kind, env.priority, env.interrupt) == ("message", "normal", False)
    assert env.payload["event"] == "message.new" and env.payload["message_kind"] == "speech"
    assert mod(world, "send_message", channel_id="dm-p01", content="请讲").ok
    assert mod(world, "announce", channel_id="dm-p01", template_id="notice", params={"text": "已记录"}).ok
    deliveries, _ = flush(world)
    assert [(d.target, d.envelope.priority) for d in deliveries] == [("p01", "normal"), ("p01", "normal")]


def test_messages_in_ordinary_channels_are_ambient_unless_the_discussion_is_free() -> None:
    world = make_world()
    new_channel(world, "closed1")
    new_channel(world, "listen1", mode="listen")
    new_channel(world, "free1", mode="free")
    assert mod(world, "send_message", channel_id="closed1", content="a").ok
    assert mod(world, "send_message", channel_id="listen1", content="b").ok
    assert mod(world, "send_message", channel_id="free1", content="c").ok
    deliveries, _ = flush(world)
    by_channel: dict[str, set[str]] = {}
    for d in deliveries:
        by_channel.setdefault(d.envelope.channel_id or "", set()).add(d.envelope.priority)
    assert by_channel == {"closed1": {"ambient"}, "listen1": {"ambient"}, "free1": {"normal"}}


def test_a_frozen_free_channel_stops_waking_its_members() -> None:
    world = make_world()
    open_free_channel = new_channel(world, "f", mode="free")
    assert open_free_channel.ok
    world.chat.freeze_channel("f", "ability:x")
    flush(world)
    assert mod(world, "send_message", channel_id="f", content="冻结中的公告").ok
    deliveries, _ = flush(world)
    assert {d.envelope.priority for d in deliveries} == {"ambient"}


def test_a_message_is_not_delivered_to_its_own_author() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")
    assert speak(world, "p01", "c1", "我说").ok
    deliveries, _ = flush(world)
    assert "p01" not in {d.target for d in deliveries}
    assert {d.target for d in deliveries} == {"moderator", "p02", "p03", "p04"}

