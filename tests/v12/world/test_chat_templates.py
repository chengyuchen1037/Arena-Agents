"""Templates: ``speak_template`` (players) and ``mod.announce`` (moderator), contracts §2.3 / D6 / D15."""

from __future__ import annotations

import pytest

from app.world.types import Code

from .helpers import (
    entries,
    envelopes,
    flush,
    make_world,
    mod,
    msgs,
    new_channel,
    open_queue,
    speak,
    speak_template,
    submit,
)


def _free_world():
    world = make_world()
    new_channel(world, "c1", mode="free")
    return world


# --------------------------------------------------------------------------- happy paths


def test_a_player_template_is_rendered_and_recorded_structurally() -> None:
    world = _free_world()
    receipt = speak_template(world, "p01", "c1", "vote", target="p02", reason="他太安静")
    assert receipt.ok and receipt.truncated is None
    [entry] = msgs(world, "c1")
    assert entry.data["kind"] == "template"
    assert entry.data["content"] == "我投 玩家2（p02）。他太安静"
    assert entry.data["template_id"] == "vote"
    assert entry.data["params"] == {"target": "p02", "reason": "他太安静"}
    assert entry.data["sender_name"] == "玩家1"


def test_optional_params_may_be_left_out() -> None:
    world = _free_world()
    assert speak_template(world, "p01", "c1", "vote", target="p03").ok
    assert msgs(world, "c1")[0].data["content"] == "我投 玩家3（p03）。"
    assert msgs(world, "c1")[0].data["params"] == {"target": "p03"}


def test_player_lists_and_enums_and_ints_render() -> None:
    world = _free_world()
    assert speak_template(world, "p01", "c1", "shout", who=["p01", "p02"], word="yes").ok
    assert speak_template(world, "p01", "c1", "count", n="3").ok  # a numeric string is accepted
    texts = [e.data["content"] for e in msgs(world, "c1")]
    assert texts == ["玩家1（p01）、玩家2（p02） 说：yes", "数到 3"]
    assert msgs(world, "c1")[1].data["params"] == {"n": 3}


def test_text_params_are_truncated_to_their_declared_length() -> None:
    world = _free_world()
    assert speak_template(world, "p01", "c1", "vote", target="p02", reason="理" * 25).ok
    entry = msgs(world, "c1")[0]
    assert entry.data["params"]["reason"] == "理" * 10
    assert entry.data["content"].endswith("理" * 10)


def test_the_moderator_announces_with_kind_announce() -> None:
    world = _free_world()
    receipt = mod(world, "announce", channel_id="c1", template_id="notice", params={"text": "天亮了"})
    assert receipt.ok
    [entry] = msgs(world, "c1")
    assert entry.actor_id == "moderator"
    assert entry.data["kind"] == "announce"
    assert entry.data["content"] == "【公告】天亮了"
    assert entry.data["template_id"] == "notice" and entry.data["params"] == {"text": "天亮了"}
    deliveries, events = flush(world)
    assert {e.payload["message_kind"] for e in envelopes(deliveries, event="message.new")} == {"announce"}
    assert [e.data["kind"] for e in events if e.type == "message.committed"] == ["announce"]


def test_announce_works_where_players_cannot_write() -> None:
    world = make_world()
    new_channel(world, "closed1", mode="closed")
    new_channel(world, "listen1", mode="listen")
    new_channel(world, "frozen1", mode="free")
    assert world.chat.freeze_channel("frozen1", "ability:x")
    for cid in ("closed1", "listen1", "frozen1"):
        assert mod(world, "announce", channel_id=cid, template_id="notice", params={"text": "通知"}).ok, cid
        assert speak(world, "p01", cid, "我也想说").code in (Code.CHANNEL_CLOSED, Code.NO_FLOOR)


def test_a_template_is_not_truncated_but_still_costs_a_message_and_its_chars() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["p01"])  # 20 chars per message, 50 in total, 2 messages
    receipt = speak_template(world, "p01", "c1", "long")
    assert receipt.ok and receipt.truncated is None
    text = msgs(world, "c1")[0].data["content"]
    assert len(text) > 20  # longer than the per-message cap and nevertheless intact
    token = world.channel("c1").group.tokens[0]
    assert token.messages_used == 1 and token.chars_used == len(text)
    assert receipt.floor_left == {"messages": 1, "chars": 50 - len(text), "seconds": 60.0}
    # the free text that follows can only use what is left of the total
    follow_up = speak(world, "p01", "c1", "字" * 20)
    assert follow_up.truncated == {"dropped_chars": 20 - (50 - len(text)), "kept_chars": 50 - len(text)}


def test_templates_use_up_the_token_like_any_message() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["p01", "p02"])
    assert speak_template(world, "p01", "c1", "count", n=1).ok
    assert speak_template(world, "p01", "c1", "count", n=2).ok  # 2nd message: the token is spent
    assert entries(world, "floor.token_ended")[0].data["reason"] == "used"
    assert speak_template(world, "p01", "c1", "count", n=3).code == Code.NO_FLOOR


def test_templates_count_against_the_free_discussion_quota_and_chars() -> None:
    world = _free_world()
    for n in (1, 2, 3):
        assert speak_template(world, "p01", "c1", "count", n=n).ok
    assert speak_template(world, "p01", "c1", "count", n=4).code == Code.QUOTA_EXCEEDED
    assert world.channel("c1").free.counts == {"p01": 3}
    assert world.channel("c1").free.chars_used == sum(len(e.data["content"]) for e in msgs(world, "c1"))


# --------------------------------------------------------------------------- who may use a template


def test_players_cannot_use_moderator_only_templates() -> None:
    world = _free_world()
    receipt = speak_template(world, "p01", "c1", "notice", text="我是主持人")
    assert not receipt.ok and receipt.code == Code.FORBIDDEN
    assert not msgs(world, "c1")
    assert world.channel("c1").free.counts == {}


def test_the_moderator_cannot_announce_with_a_players_only_template() -> None:
    world = _free_world()
    receipt = mod(world, "announce", channel_id="c1", template_id="vote", params={"target": "p01"})
    assert not receipt.ok and receipt.code == Code.FORBIDDEN


def test_templates_open_to_everybody_work_for_both() -> None:
    world = _free_world()
    assert speak_template(world, "p01", "c1", "count", n=1).ok
    assert mod(world, "announce", channel_id="c1", template_id="count", params={"n": 2}).ok


def test_unknown_template_lists_the_usable_ones() -> None:
    world = _free_world()
    player = speak_template(world, "p01", "c1", "nope")
    assert player.code == Code.UNKNOWN_TEMPLATE
    assert player.data["templates"] == ["vote", "shout", "count", "long"]
    moderator = mod(world, "announce", channel_id="c1", template_id="nope")
    assert moderator.code == Code.UNKNOWN_TEMPLATE
    assert moderator.data["templates"] == ["notice", "shout", "count", "long"]


# --------------------------------------------------------------------------- parameter validation


@pytest.mark.parametrize(
    "template,params,code",
    [
        ("vote", {}, Code.INVALID_PARAMS),  # missing required target
        ("vote", {"target": "ghost"}, Code.INVALID_TARGET),  # not a player
        ("vote", {"target": "moderator"}, Code.INVALID_TARGET),
        ("vote", {"target": "p02", "colour": "red"}, Code.INVALID_PARAMS),  # unknown key
        ("shout", {"who": [], "word": "yes"}, Code.INVALID_TARGET),  # an empty player list names nobody
        ("shout", {"who": ["p01", "ghost"], "word": "yes"}, Code.INVALID_TARGET),
        ("shout", {"who": ["p01"], "word": "maybe"}, Code.INVALID_PARAMS),  # not in the enum
        ("count", {"n": 10}, Code.INVALID_PARAMS),  # out of range
        ("count", {"n": 0}, Code.INVALID_PARAMS),
        ("count", {"n": "many"}, Code.INVALID_PARAMS),
        ("count", {"n": True}, Code.INVALID_PARAMS),
    ],
)
def test_parameter_validation_rejects_without_side_effects(template: str, params: dict, code: str) -> None:
    world = _free_world()
    before = world.seq
    receipt = speak_template(world, "p01", "c1", template, **params)
    assert not receipt.ok and receipt.code == code, receipt
    assert world.seq == before
    assert world.channel("c1").free.counts == {} and world.channel("c1").free.chars_used == 0
    assert world.drain_outbox() == []


def test_moderator_announcements_are_validated_too() -> None:
    world = _free_world()
    assert mod(world, "announce", channel_id="c1", template_id="notice", params={}).code == Code.INVALID_PARAMS
    assert (
        mod(world, "announce", channel_id="c1", template_id="shout", params={"who": ["x"], "word": "yes"}).code
        == Code.INVALID_TARGET
    )
    assert mod(world, "announce", channel_id="nowhere", template_id="notice", params={"text": "x"}).code == Code.UNKNOWN_CHANNEL


# --------------------------------------------------------------------------- the floor comes first


def test_the_floor_check_precedes_the_template_checks() -> None:
    world = make_world()
    new_channel(world, "c1", mode="closed")
    assert speak_template(world, "p01", "c1", "nope").code == Code.CHANNEL_CLOSED
    new_channel(world, "c2")
    open_queue(world, "c2", ["p02"])
    assert speak_template(world, "p01", "c2", "count", n=99).code == Code.NO_FLOOR
    assert speak_template(world, "p01", "nowhere", "count", n=1).code == Code.UNKNOWN_CHANNEL
    new_channel(world, "c3", members=["p02"], mode="free")
    assert speak_template(world, "p01", "c3", "count", n=1).code == Code.NOT_MEMBER


def test_templates_are_stale_checked_like_speech() -> None:
    from app.world.types import MODERATOR_ID

    from .helpers import make_spec

    world = make_world(spec=make_spec(invalidates_drafts=["phase.changed"]))
    new_channel(world, "c1", mode="free")
    seen = world.seq
    world.commit("phase.changed", MODERATOR_ID, {"text": "x"}, visible_to="*")
    receipt = submit(
        world, "p01", "speak_template", {"channel_id": "c1", "template_id": "count", "params": {"n": 1}}, based_on=seen
    )
    assert receipt.code == Code.STALE
