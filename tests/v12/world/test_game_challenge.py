"""Challenges (质询) and tickets: open / resolve / truncation / closed DM (contracts §2.3, §2.6, §8.2)."""

from __future__ import annotations

from app.world.types import MODERATOR_ID, Code

from .test_game_kit import ask, deliveries, entries, events, make_world, mod


def challenge(world, actor="u1", content="第 3 条结算有误，请核对。", **extra):
    return ask(world, actor, "challenge", content=content, **extra)


def test_challenge_creates_a_ticket_a_dm_message_and_wakes_the_moderator():
    world = make_world()
    before = world.seq
    receipt = challenge(world, "u1")
    assert receipt.ok and receipt.data["ticket_id"] == "t1" and receipt.truncated is None

    ticket = world.state.tickets["t1"]
    assert (ticket.player_id, ticket.status, ticket.verdict) == ("u1", "open", None)
    assert ticket.preview == "第 3 条结算有误，请核对。" and ticket.opened_seq == receipt.seq

    message = entries(world, "message")[0]
    assert message.seq == receipt.seq and message.channel_id == "dm-u1" and message.actor_id == "u1"
    assert message.visible_to_ids == [MODERATOR_ID, "u1"]
    assert message.data["kind"] == "challenge" and message.data["ticket_id"] == "t1"
    assert message.data["content"] == "第 3 条结算有误，请核对。"
    assert message.data["sender_name"] == "甲"
    # no extra ledger entry besides the message
    assert [e.type for e in world.ledger if e.seq > before] == ["message"]

    items = deliveries(world)
    assert [d.target for d in items] == [MODERATOR_ID]
    env = items[0].envelope
    assert (env.kind, env.priority, env.channel_id, env.sender) == ("challenge", "urgent", "dm-u1", "u1")
    assert env.payload["event"] == "challenge.opened" and env.payload["ticket_id"] == "t1"

    ui = events(world)
    opened = [e for e in ui if e.type == "challenge.opened"]
    assert opened[0].data == {
        "ticket": {
            "id": "t1",
            "player_id": "u1",
            "status": "open",
            "verdict": None,
            "preview": "第 3 条结算有误，请核对。",
            "opened_seq": receipt.seq,
        }
    }
    assert [e.data["kind"] for e in ui if e.type == "message.committed"] == ["challenge"]


def test_challenges_are_unlimited_and_ticket_ids_count_up():
    world = make_world()
    ids = [challenge(world, "u1" if i % 2 == 0 else "u2", content=f"问题 {i}").data["ticket_id"] for i in range(7)]
    assert ids == [f"t{i}" for i in range(1, 8)]
    assert len(world.state.tickets) == 7
    assert [t.player_id for t in world.state.tickets.values()][:2] == ["u1", "u2"]


def test_challenge_content_is_truncated_to_the_dm_limit():
    world = make_world()  # defaults.dm.max_chars_per_message = 50
    receipt = challenge(world, content="字" * 80)
    assert receipt.ok and receipt.truncated == {"dropped_chars": 30, "kept_chars": 50}
    message = entries(world, "message")[0]
    assert message.data["content"] == "字" * 50
    assert message.data["truncated"] == {"dropped_chars": 30, "kept_chars": 50}
    assert len(world.state.tickets["t1"].preview) <= 60


def test_challenge_ref_seq_is_kept_only_when_it_points_into_the_ledger():
    world = make_world()
    ok = challenge(world, ref_seq=1)
    assert ok.ok and world.state.tickets["t1"].ref_seq == 1
    assert entries(world, "message")[0].data["ref_seq"] == 1
    far = challenge(world, ref_seq=9999)
    zero = challenge(world, ref_seq=0)
    assert far.ok and zero.ok
    assert world.state.tickets["t2"].ref_seq is None and world.state.tickets["t3"].ref_seq is None
    assert "ref_seq" not in entries(world, "message")[1].data


def test_challenge_is_refused_when_the_dm_is_closed_and_changes_nothing():
    world = make_world()
    for mode in ("closed", "listen"):
        world.channel("dm-u1").mode = mode
        seq = world.seq
        counters = dict(world.state.counters)
        receipt = challenge(world, "u1")
        assert receipt.code == Code.CHANNEL_CLOSED and "私聊" in receipt.message
        assert world.seq == seq and world.state.counters == counters and world.state.tickets == {}
    assert deliveries(world) == []
    # other players are unaffected
    assert challenge(world, "u2").ok

    world.state.channels.remove(world.channel("dm-u3"))  # no system DM at all
    assert challenge(world, "u3").code == Code.CHANNEL_CLOSED


def test_challenge_is_a_player_intent():
    world = make_world()
    assert ask(world, MODERATOR_ID, "challenge", content="x").code == Code.FORBIDDEN
    assert world.state.tickets == {}


def test_resolve_challenge_replies_in_the_dm_and_wakes_the_player():
    world = make_world()
    challenge(world, "u2", content="我的票被漏算了")
    world.drain_outbox()
    world.drain_events()

    receipt = mod(world, "resolve_challenge", ticket_id="t1", verdict="upheld", reply="成立，已更正。")
    assert receipt.ok
    ticket = world.state.tickets["t1"]
    assert (ticket.status, ticket.verdict) == ("resolved", "upheld")

    reply = entries(world, "message")[-1]
    assert reply.seq == receipt.seq and reply.channel_id == "dm-u2" and reply.actor_id == MODERATOR_ID
    assert reply.visible_to_ids == [MODERATOR_ID, "u2"]
    assert reply.data["kind"] == "reply" and reply.data["content"] == "成立，已更正。"
    assert reply.data["ticket_id"] == "t1" and reply.data["verdict"] == "upheld"

    items = deliveries(world)
    assert [d.target for d in items] == ["u2"]
    env = items[0].envelope
    assert (env.kind, env.priority, env.channel_id) == ("challenge", "urgent", "dm-u2")
    assert env.payload["event"] == "challenge.resolved" and env.payload["ticket_id"] == "t1"
    ui = events(world)
    assert [e.data for e in ui if e.type == "challenge.resolved"] == [{"ticket_id": "t1", "verdict": "upheld"}]


def test_resolve_challenge_errors_change_nothing():
    world = make_world()
    assert mod(world, "resolve_challenge", ticket_id="t1", verdict="rejected", reply="？").code == Code.UNKNOWN_TICKET
    challenge(world, "u1")
    assert mod(world, "resolve_challenge", ticket_id="t1", verdict="rejected", reply="驳回。").ok
    seq = world.seq
    again = mod(world, "resolve_challenge", ticket_id="t1", verdict="upheld", reply="再来一次")
    assert again.code == Code.INVALID_TARGET and "rejected" in again.message
    assert world.seq == seq and world.state.tickets["t1"].verdict == "rejected"
    # players cannot resolve
    assert ask(world, "u1", "mod.resolve_challenge", ticket_id="t1", verdict="upheld", reply="x").code == Code.FORBIDDEN


def test_moderator_can_reply_after_closing_the_dm_as_a_punishment():
    world = make_world()
    challenge(world, "u1")
    challenge(world, "u1", content="又一个问题")
    world.channel("dm-u1").mode = "closed"  # moderator punishes the harassment

    receipt = mod(world, "resolve_challenge", ticket_id="t1", verdict="rejected", reply="请不要重复质询。")
    assert receipt.ok
    assert entries(world, "message")[-1].data["kind"] == "reply"
    assert challenge(world, "u1").code == Code.CHANNEL_CLOSED

    world.channel("dm-u1").mode = "free"  # the moderator re-opens it
    assert challenge(world, "u1").ok
    assert mod(world, "resolve_challenge", ticket_id="t2", verdict="rejected", reply="已处理。").ok


def test_reply_is_truncated_to_the_moderator_limit():
    world = make_world()
    challenge(world)
    receipt = mod(world, "resolve_challenge", ticket_id="t1", verdict="rejected", reply="答" * 150)
    assert receipt.ok and receipt.truncated == {"dropped_chars": 50, "kept_chars": 100}
    assert entries(world, "message")[-1].data["content"] == "答" * 100


def test_redeliver_wakes_the_moderator_for_every_unresolved_ticket():
    world = make_world()
    challenge(world, "u1")
    challenge(world, "u2")
    mod(world, "resolve_challenge", ticket_id="t1", verdict="upheld", reply="好")
    world.drain_outbox()

    world.redeliver()
    items = deliveries(world)
    assert [d.target for d in items] == [MODERATOR_ID]
    env = items[0].envelope
    assert (env.kind, env.priority, env.channel_id, env.sender) == ("challenge", "urgent", "dm-u2", "u2")
    assert env.payload["event"] == "challenge.opened" and env.payload["ticket_id"] == "t2"
    assert env.seq == world.state.tickets["t2"].opened_seq
