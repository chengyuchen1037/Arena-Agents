"""UI projections: MessageView / RequestView / TicketView and the god-view snapshot (contracts §2.8, §8.1)."""

from __future__ import annotations

import json
import sys
import types

import pytest

from app.world.projection import message_view, request_view, seat_sorted, ticket_view, world_snapshot
from app.world.types import MODERATOR_ID

from .test_game_kit import ask, choice_options, entries, make_world, mod, player

SNAPSHOT_KEYS = {
    "players", "channels", "messages", "requests", "tickets", "dashboard", "phase", "status", "winner",
    "summary", "seq", "now", "public_tags", "ui",
}


@pytest.fixture
def chat_stub(monkeypatch):
    """Stands in for ``app.world.chat`` (world-chat's module): records the channels it was asked about."""
    module = types.ModuleType("app.world.chat")
    seen: list[str] = []

    def channel_view(world, channel):
        seen.append(channel.id)
        return {
            "id": channel.id,
            "name": channel.name,
            "type": channel.type,
            "mode": channel.mode,
            "members": list(channel.members),
            "readonly": list(channel.readonly),
            "frozen": channel.frozen,
            "system": channel.system,
            "owner": channel.owner,
            "floor": {"kind": "none"},
        }

    module.channel_view = channel_view  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.world.chat", module)
    return seen


def say(world, actor, text, channel="public", **kwargs):
    return world.post_message(channel, actor, text, kind=kwargs.pop("kind", "speech"), **kwargs)


# --------------------------------------------------------------------------- message_view


def test_message_view_has_the_documented_fields():
    world = make_world()
    entry = say(world, "u2", "大家好", truncated={"dropped_chars": 3, "kept_chars": 3})
    view = message_view(entry)
    assert view == {
        "seq": entry.seq,
        "ts": entry.ts,
        "channel_id": "public",
        "from": "u2",
        "from_name": "乙",
        "content": "大家好",
        "kind": "speech",
        "template_id": None,
        "truncated": {"dropped_chars": 3, "kept_chars": 3},
        "ticket_id": None,
        "visible_to_ids": world.channel("public").members,
    }


def test_message_view_carries_template_and_ticket_ids_and_matches_the_live_event():
    world = make_world()
    announce = say(world, MODERATOR_ID, "公告", kind="announce", template_id="notice", params={"x": 1})
    assert message_view(announce)["template_id"] == "notice" and message_view(announce)["from_name"] == "主持人"

    world.drain_events()
    ask(world, "u1", "challenge", content="有疑问")
    live = [e for e in world.drain_events() if e.type == "message.committed"][0].data
    stored = message_view(entries(world, "message")[-1])
    assert stored["ticket_id"] == "t1" and stored["kind"] == "challenge"
    assert stored == live  # snapshot and SSE event share one shape


def test_message_view_copies_the_audience():
    world = make_world()
    entry = say(world, "u1", "hi")
    view = message_view(entry)
    view["visible_to_ids"].append("nobody")
    assert "nobody" not in entry.visible_to_ids


# --------------------------------------------------------------------------- request / ticket views


def test_request_view_shape_and_progress():
    world = make_world()
    rid = mod(
        world, "open_request", to=["u3", "u1", "u2"], kind="choice", question="选哪个？", options=choice_options(),
        sealed=True, duration_s=50,
    ).data["request_id"]
    ask(world, "u3", "respond", request_id=rid, value="yes", reason="不该出现")
    view = request_view(world, world.state.requests[rid])

    assert view == {
        "id": rid,
        "kind": "choice",
        "question": "选哪个？",
        "options": choice_options(),
        "to": ["u3", "u1", "u2"],
        "answered_by": ["u3"],
        "status": "open",
        "sealed": True,
        "seconds_left": 50.0,
    }
    assert "不该出现" not in json.dumps(view, ensure_ascii=False)  # never any answer content

    world.clock.advance(12.34)
    assert request_view(world, world.state.requests[rid])["seconds_left"] == 37.7


def test_request_view_answered_by_follows_seat_order_naturally():
    world = make_world()
    for pid, seat in (("u1", "10"), ("u2", "2"), ("u3", "1"), ("u4", "3")):
        player(world, pid).seat = seat
    assert seat_sorted(world, ["u1", "u2", "u3", "u4", "ghost"]) == ["u3", "u2", "u4", "u1"]

    rid = mod(world, "open_request", to=["u1", "u2", "u3", "u4"], kind="text", question="？").data["request_id"]
    for pid in ("u1", "u4", "u2"):
        ask(world, pid, "respond", request_id=rid, value="x")
    assert request_view(world, world.state.requests[rid])["answered_by"] == ["u2", "u4", "u1"]


def test_closed_and_cancelled_request_views():
    world = make_world()
    rid = mod(
        world, "open_request", to=["u1", "u2"], kind="choice", question="？", options=choice_options(),
        default="no", duration_s=5,
    ).data["request_id"]
    ask(world, "u1", "respond", request_id=rid, value="yes")
    world.clock.advance(5)
    world.tick()
    closed = request_view(world, world.state.requests[rid])
    assert closed["status"] == "closed" and closed["seconds_left"] is None
    assert closed["answered_by"] == ["u1"]  # the defaulted answer of u2 does not count as answered

    other = mod(world, "open_request", to=["u3"], kind="text", question="？").data["request_id"]
    mod(world, "cancel_request", request_id=other)
    cancelled = request_view(world, world.state.requests[other])
    assert cancelled["status"] == "cancelled" and cancelled["seconds_left"] is None


def test_ticket_view_shape():
    world = make_world()
    ask(world, "u2", "challenge", content="这是一个\n很长的\n质询" + "。" * 100)
    ticket = world.state.tickets["t1"]
    view = ticket_view(ticket)
    assert set(view) == {"id", "player_id", "status", "verdict", "preview", "opened_seq"}
    assert view["id"] == "t1" and view["player_id"] == "u2" and view["status"] == "open" and view["verdict"] is None
    assert "\n" not in view["preview"] and len(view["preview"]) <= 60 and view["preview"].startswith("这是一个 很长的 质询")
    assert view["opened_seq"] == entries(world, "message")[-1].seq

    mod(world, "resolve_challenge", ticket_id="t1", verdict="upheld", reply="成立")
    assert ticket_view(ticket)["status"] == "resolved" and ticket_view(ticket)["verdict"] == "upheld"


# --------------------------------------------------------------------------- world_snapshot


def test_snapshot_has_exactly_the_documented_top_level_fields(chat_stub):
    world = make_world()
    snap = world_snapshot(world)
    assert set(snap) == SNAPSHOT_KEYS
    json.dumps(snap, ensure_ascii=False)
    assert snap["status"] == "running" and snap["winner"] is None and snap["summary"] == ""
    assert snap["seq"] == world.seq and snap["now"] == 0.0
    assert snap["public_tags"] == ["active", "out", "leader"]
    assert snap["ui"] == {"out_tags": ["out"], "status_tags": ["leader"], "highlight_tags": ["leader"]}
    assert snap["messages"] == [] and snap["requests"] == [] and snap["tickets"] == []
    assert snap["dashboard"] == {"factions": [], "tag_stats": []}


def test_snapshot_players_are_god_view_with_all_tags(chat_stub):
    world = make_world()
    mod(world, "update_player", player_id="u2", add_tags=["secret", "out"])
    snap = world_snapshot(world)
    assert [p["id"] for p in snap["players"]] == ["u1", "u2", "u3", "u4"]
    row = snap["players"][1]
    assert set(row) == {"id", "seat", "name", "tags", "out"}
    assert "secret" in row["tags"] and row["out"] is True and row["name"] == "乙" and row["seat"] == "2"
    assert snap["players"][0]["out"] is False


def test_snapshot_channels_come_from_the_chat_module(chat_stub):
    world = make_world()
    snap = world_snapshot(world)
    assert chat_stub == [c.id for c in world.state.channels]
    assert [c["id"] for c in snap["channels"]] == [c.id for c in world.state.channels]
    assert snap["channels"][-1]["floor"] == {"kind": "none"}


def test_snapshot_messages_are_only_messages_in_god_view(chat_stub):
    world = make_world()
    say(world, "u1", "公开发言")
    say(world, "u2", "私聊内容", channel="dm-u2")
    mod(world, "set_phase", text="其他条目不是消息")
    snap = world_snapshot(world)

    assert [m["content"] for m in snap["messages"]] == ["公开发言", "私聊内容"]
    private = snap["messages"][1]
    assert private["visible_to_ids"] == [MODERATOR_ID, "u2"]  # god view keeps it, with the audience for filtering
    assert set(private) == {
        "seq", "ts", "channel_id", "from", "from_name", "content", "kind", "template_id", "truncated",
        "ticket_id", "visible_to_ids",
    }


def test_snapshot_messages_are_capped_to_the_newest_ones(chat_stub):
    world = make_world()
    for i in range(7):
        say(world, "u1", f"消息 {i}")
    world.drain_outbox()
    snap = world_snapshot(world, max_messages=3)
    assert [m["content"] for m in snap["messages"]] == ["消息 4", "消息 5", "消息 6"]
    assert [m["seq"] for m in snap["messages"]] == sorted(m["seq"] for m in snap["messages"])
    assert world_snapshot(world, max_messages=0)["messages"] == []
    assert len(world_snapshot(world, max_messages=100)["messages"]) == 7


def test_snapshot_default_cap_is_two_thousand_messages(chat_stub):
    world = make_world()
    for i in range(2010):
        say(world, "u1", f"消息 {i}")
        if i % 200 == 0:
            world.drain_outbox()
            world.drain_events()
    snap = world_snapshot(world)
    assert len(snap["messages"]) == 2000
    assert snap["messages"][0]["content"] == "消息 10" and snap["messages"][-1]["content"] == "消息 2009"


def test_snapshot_includes_requests_tickets_dashboard_and_the_final_result(chat_stub):
    world = make_world()
    rid = mod(
        world, "open_request", to=["u1", "u2"], kind="choice", question="？", options=choice_options(), sealed=True
    ).data["request_id"]
    ask(world, "u1", "respond", request_id=rid, value="yes", reason="绝密理由")
    ask(world, "u2", "challenge", content="我有疑问")
    mod(world, "update_dashboard", factions=[{"name": "红队", "alive": 1, "total": 1}], tag_stats=[])
    mod(world, "set_phase", text="终局阶段")
    mod(world, "end_game", winner="蓝队", summary="蓝队胜利")
    snap = world_snapshot(world)

    assert [r["id"] for r in snap["requests"]] == [rid] and snap["requests"][0]["answered_by"] == ["u1"]
    assert [t["id"] for t in snap["tickets"]] == ["t1"] and snap["tickets"][0]["status"] == "open"
    assert snap["dashboard"]["factions"][0]["name"] == "红队"
    assert (snap["phase"], snap["status"], snap["winner"], snap["summary"]) == ("终局阶段", "finished", "蓝队", "蓝队胜利")
    assert snap["seq"] == world.seq
    # sealed answers never reach the snapshot
    assert "绝密理由" not in json.dumps(snap, ensure_ascii=False)


def test_snapshot_reports_the_game_clock(chat_stub):
    world = make_world()
    world.clock.advance(61.23456)
    assert world_snapshot(world)["now"] == 61.235
