"""The per-agent state board (contracts §2.7): visibility differences, channel write status, floors."""

from __future__ import annotations

import json

import pytest

from app.world.board import MAX_RESOLVED_TICKETS, build_board
from app.world.state import ChannelState, FloorGroup, FreeState, Token
from app.world.types import MODERATOR_ID

from .test_game_kit import ask, choice_options, make_world, mod, player, role_holder

BOARD_KEYS = {"me", "phase", "status", "now", "players", "channels", "requests", "abilities", "tickets", "dashboard"}


def by_id(items, item_id):
    return next(i for i in items if i["id"] == item_id)


def add_channel(world, **fields) -> ChannelState:
    channel = ChannelState(**fields)
    world.state.channels.append(channel)
    return channel


def token(holder, status="active", **fields) -> Token:
    values = {
        "holder": holder,
        "max_messages": 3,
        "max_chars_per_message": 100,
        "max_chars_total": 300,
        "status": status,
        "duration_s": 60.0,
    }
    values.update(fields)
    return Token(**values)


def add_queue(world, *, paused=False) -> ChannelState:
    now = world.clock.now()
    tokens = [
        token("u1", "active", messages_used=1, chars_used=50, deadline=None if paused else now + 60, remaining=42.0 if paused else None),
        token("u2", "pending"),
        token("u3", "pending"),
        token("u4", "done", ended_reason="used", messages_used=3),
    ]
    group = FloorGroup(id="g1", channel_id="stage", mode="queue", tokens=tokens, paused=paused)
    return add_channel(
        world,
        id="stage",
        name="发言台",
        type="group",
        mode="queue",
        members=[MODERATOR_ID, "u1", "u2", "u3", "u4"],
        group=group,
    )


# --------------------------------------------------------------------------- shape / me / players


def test_board_shape_and_me_for_a_player_and_the_moderator():
    world = make_world()
    me = player(world, "u1")
    board = build_board(world, "u1")

    assert set(board) == BOARD_KEYS
    json.dumps(board, ensure_ascii=False)  # JSON-serializable
    assert board["status"] == "running" and board["phase"] == "" and board["now"] == 0.0
    brief = world.spec.setup.roles[me.role].brief
    assert board["me"] == {
        "id": "u1",
        "name": "甲",
        "seat": "1",
        "tags": me.tags,
        "attributes": me.attributes,
        "brief": brief,
        "role": me.role,
        "faction": me.faction,
        "is_moderator": False,
    }

    mod_board = build_board(world, MODERATOR_ID)
    assert set(mod_board) == BOARD_KEYS
    assert mod_board["me"]["is_moderator"] is True and mod_board["me"]["id"] == MODERATOR_ID
    assert mod_board["me"]["name"] == "主持人" and mod_board["me"]["tags"] == []


def test_board_reports_phase_status_and_game_time():
    world = make_world()
    mod(world, "set_phase", text="第二阶段")
    world.clock.advance(12.34)
    board = build_board(world, "u2")
    assert board["phase"] == "第二阶段" and board["now"] == 12.3
    mod(world, "end_game", winner="蓝队", summary="结束")
    assert build_board(world, "u2")["status"] == "finished"


def test_unknown_agent_raises():
    world = make_world()
    with pytest.raises(KeyError):
        build_board(world, "ghost")


def test_players_see_public_tags_of_others_but_everything_of_themselves():
    world = make_world()
    player(world, "u2").tags = ["active", "secret"]
    player(world, "u1").tags = ["active", "private-mine"]
    board = build_board(world, "u1")

    assert [p["id"] for p in board["players"]] == ["u1", "u2", "u3", "u4"]
    assert by_id(board["players"], "u2")["tags"] == ["active"]  # "secret" is not a public tag
    assert by_id(board["players"], "u1")["tags"] == ["active", "private-mine"]
    for row in board["players"]:
        assert set(row) == {"id", "seat", "name", "tags", "out"}  # no role / faction / attributes of anybody
    assert "secret" not in json.dumps(board, ensure_ascii=False)


def test_moderator_sees_every_tag_role_faction_and_attributes():
    world = make_world()
    player(world, "u2").tags = ["active", "secret"]
    board = build_board(world, MODERATOR_ID)
    row = by_id(board["players"], "u2")
    assert row["tags"] == ["active", "secret"]
    assert (row["role"], row["faction"]) == (player(world, "u2").role, player(world, "u2").faction)
    assert row["attributes"] == player(world, "u2").attributes
    assert all("role" in r and "attributes" in r for r in board["players"])


def test_out_flag_follows_the_scenario_out_tags():
    world = make_world()
    mod(world, "update_player", player_id="u3", add_tags=["out"])
    assert by_id(build_board(world, "u1")["players"], "u3")["out"] is True
    assert by_id(build_board(world, "u1")["players"], "u2")["out"] is False
    assert by_id(build_board(world, MODERATOR_ID)["players"], "u3")["out"] is True


def test_board_returns_copies_of_mutable_state():
    world = make_world()
    board = build_board(world, "u1")
    board["me"]["tags"].append("hacked")
    board["me"]["attributes"]["x"] = 1
    assert "hacked" not in player(world, "u1").tags and "x" not in player(world, "u1").attributes


# --------------------------------------------------------------------------- channels


def test_only_member_channels_are_listed():
    world = make_world()
    add_channel(world, id="side", name="旁路", type="group", mode="free", members=[MODERATOR_ID, "u2", "u3"])
    ids_u1 = [c["id"] for c in build_board(world, "u1")["channels"]]
    ids_u2 = [c["id"] for c in build_board(world, "u2")["channels"]]
    ids_mod = [c["id"] for c in build_board(world, MODERATOR_ID)["channels"]]
    assert "side" not in ids_u1 and "dm-u2" not in ids_u1 and "dm-u1" in ids_u1 and "public" in ids_u1
    assert "side" in ids_u2 and "dm-u1" not in ids_u2
    assert set(ids_mod) == {c.id for c in world.state.channels}


def test_channel_block_has_the_documented_fields():
    world = make_world()
    channel = by_id(build_board(world, "u1")["channels"], "public")
    assert set(channel) == {
        "id", "name", "type", "mode", "members", "readonly", "frozen", "system", "can_write", "why_not",
        "max_chars", "floor",
    }
    assert channel["can_write"] is True and channel["why_not"] is None and channel["system"] is False
    dm = by_id(build_board(world, "u1")["channels"], "dm-u1")
    assert dm["system"] is True and dm["type"] == "dm" and dm["can_write"] is True


def test_max_chars_is_the_cap_of_one_message_the_agent_could_write_now():
    world = make_world()
    defaults = world.spec.defaults
    # a free discussion without a FreeState of its own and a system DM use the scenario defaults
    assert by_id(build_board(world, "u1")["channels"], "public")["max_chars"] == defaults.free.max_chars_per_message
    assert by_id(build_board(world, "u1")["channels"], "dm-u1")["max_chars"] == defaults.dm.max_chars_per_message
    # the moderator is not capped by the gate; whoever cannot write has no cap to report
    assert by_id(build_board(world, MODERATOR_ID)["channels"], "public")["max_chars"] is None
    world.channel("public").mode = "closed"
    assert by_id(build_board(world, "u1")["channels"], "public")["max_chars"] is None
    # a free discussion reports its own per-message limit
    free = FreeState(max_messages_per_member=3, max_chars_per_message=77, max_chars_total=500, duration_s=300, silence_s=30)
    add_channel(world, id="chat", name="讨论", type="group", mode="free", members=[MODERATOR_ID, "u1", "u2"], free=free)
    assert by_id(build_board(world, "u1")["channels"], "chat")["max_chars"] == 77
    # a token: the per-message cap, clipped to what is left of its total
    tokens = [token("u1", max_chars_per_message=100, max_chars_total=300, chars_used=250)]
    add_channel(
        world,
        id="talk",
        name="轮次",
        type="group",
        mode="turn",
        members=[MODERATOR_ID, "u1", "u2"],
        group=FloorGroup(id="g2", channel_id="talk", mode="turn", tokens=tokens),
    )
    assert by_id(build_board(world, "u1")["channels"], "talk")["max_chars"] == 50
    assert by_id(build_board(world, "u2")["channels"], "talk")["max_chars"] is None  # no token: cannot write


def code_of(block) -> str:
    assert block["can_write"] is False and isinstance(block["why_not"], str)
    code, _, reason = block["why_not"].partition("：")
    assert reason.strip(), block["why_not"]
    return code


@pytest.mark.parametrize(
    "mode,code",
    [("closed", "CHANNEL_CLOSED"), ("listen", "CHANNEL_CLOSED")],
)
def test_closed_and_listen_channels_cannot_be_written(mode, code):
    world = make_world()
    world.channel("public").mode = mode
    block = by_id(build_board(world, "u1")["channels"], "public")
    assert code_of(block) == code
    assert by_id(build_board(world, MODERATOR_ID)["channels"], "public")["can_write"] is True


def test_closed_system_dm_says_the_private_chat_was_closed():
    world = make_world()
    world.channel("dm-u1").mode = "closed"
    block = by_id(build_board(world, "u1")["channels"], "dm-u1")
    assert code_of(block) == "CHANNEL_CLOSED" and "私聊" in block["why_not"]
    # the moderator still can; other players' DMs are unaffected
    assert by_id(build_board(world, MODERATOR_ID)["channels"], "dm-u1")["can_write"] is True
    assert by_id(build_board(world, "u2")["channels"], "dm-u2")["can_write"] is True


def test_frozen_channel_blocks_everybody_but_the_moderator():
    world = make_world()
    world.channel("public").frozen = True
    block = by_id(build_board(world, "u1")["channels"], "public")
    assert block["frozen"] is True and code_of(block) == "NO_FLOOR" and "冻结" in block["why_not"]
    assert by_id(build_board(world, MODERATOR_ID)["channels"], "public")["can_write"] is True


def test_closed_mode_is_reported_before_frozen():
    world = make_world()
    channel = world.channel("public")
    channel.mode, channel.frozen = "closed", True
    assert code_of(by_id(build_board(world, "u1")["channels"], "public")) == "CHANNEL_CLOSED"


def test_free_channel_rules_readonly_writers_and_quota():
    world = make_world()
    free = FreeState(
        writers=None,
        max_messages_per_member=2,
        max_chars_per_message=100,
        max_chars_total=500,
        duration_s=300,
        silence_s=30,
        started_gt=0.0,
        counts={"u1": 2, "u2": 1},
        chars_used=120,
        last_message_gt=4.0,
    )
    chat = add_channel(
        world, id="chat", name="讨论", type="group", mode="free", members=[MODERATOR_ID, "u1", "u2", "u3", "u4"], free=free
    )
    chat.readonly = ["u3"]
    free.writers = None

    assert code_of(by_id(build_board(world, "u1")["channels"], "chat")) == "QUOTA_EXCEEDED"
    assert by_id(build_board(world, "u2")["channels"], "chat")["can_write"] is True
    assert code_of(by_id(build_board(world, "u3")["channels"], "chat")) == "NO_FLOOR"  # read-only member
    assert by_id(build_board(world, "u4")["channels"], "chat")["can_write"] is True

    free.writers = ["u2"]
    assert by_id(build_board(world, "u2")["channels"], "chat")["can_write"] is True
    assert code_of(by_id(build_board(world, "u4")["channels"], "chat")) == "NO_FLOOR"  # not in writers
    assert code_of(by_id(build_board(world, "u1")["channels"], "chat")) == "NO_FLOOR"  # writers is checked before quota


def test_free_block_reports_counters_and_clocks():
    world = make_world()
    free = FreeState(
        max_messages_per_member=3,
        max_chars_per_message=100,
        max_chars_total=500,
        duration_s=300,
        silence_s=30,
        started_gt=10.0,
        counts={"u1": 1},
        chars_used=77,
        last_message_gt=25.0,
    )
    add_channel(world, id="chat", name="讨论", type="group", mode="free", members=[MODERATOR_ID, "u1", "u2"], free=free)
    world.clock.set(40.0)

    mine = by_id(build_board(world, "u1")["channels"], "chat")["floor"]
    assert mine["kind"] == "free" and mine["active"] is None and mine["queue"] == [] and mine["mine"] is None
    assert mine["free"] == {
        "my_messages_left": 2,
        "max_messages_per_member": 3,
        "chars_used": 77,
        "max_chars_total": 500,
        "elapsed_s": 30.0,
        "duration_s": 300,
        "silent_s": 15.0,
        "writers": None,
    }
    assert by_id(build_board(world, "u2")["channels"], "chat")["floor"]["free"]["my_messages_left"] == 3
    assert by_id(build_board(world, MODERATOR_ID)["channels"], "chat")["floor"]["free"]["my_messages_left"] is None

    # nobody has spoken yet: silence counts from the start of the discussion
    free.last_message_gt = 0.0
    assert by_id(build_board(world, "u1")["channels"], "chat")["floor"]["free"]["silent_s"] == 30.0
    # system DMs have no free-discussion limits
    assert by_id(build_board(world, "u1")["channels"], "dm-u1")["floor"]["free"] is None


def test_queue_floor_active_pending_and_done_tokens():
    world = make_world()
    add_queue(world)

    first = by_id(build_board(world, "u1")["channels"], "stage")
    assert first["can_write"] is True and first["why_not"] is None
    floor = first["floor"]
    assert floor["kind"] == "queue" and floor["active"] == "u1" and floor["paused"] is False
    assert floor["queue"] == [
        {"holder": "u1", "state": "active", "messages_used": 1, "max_messages": 3},
        {"holder": "u2", "state": "pending", "messages_used": 0, "max_messages": 3},
        {"holder": "u3", "state": "pending", "messages_used": 0, "max_messages": 3},
        {"holder": "u4", "state": "done", "messages_used": 3, "max_messages": 3},
    ]
    assert floor["mine"] == {"messages_left": 2, "chars_per_message": 100, "chars_left": 250, "seconds_left": 60.0}
    assert floor["seconds_left"] == 60.0

    world.clock.advance(15.25)
    assert by_id(build_board(world, "u1")["channels"], "stage")["floor"]["mine"]["seconds_left"] == 44.8

    waiting = by_id(build_board(world, "u3")["channels"], "stage")
    assert code_of(waiting) == "NO_FLOOR" and "第 3 位" in waiting["why_not"] and "u1" in waiting["why_not"]
    assert waiting["floor"]["mine"] is None and waiting["floor"]["active"] == "u1"
    finished = by_id(build_board(world, "u4")["channels"], "stage")
    assert code_of(finished) == "NO_FLOOR" and "结束" in finished["why_not"]

    mod_block = by_id(build_board(world, MODERATOR_ID)["channels"], "stage")
    assert mod_block["can_write"] is True and mod_block["floor"]["mine"] is None
    assert [q["state"] for q in mod_block["floor"]["queue"]] == ["active", "pending", "pending", "done"]


def test_paused_queue_blocks_the_active_holder_and_shows_the_frozen_remaining_time():
    world = make_world()
    add_queue(world, paused=True)
    block = by_id(build_board(world, "u1")["channels"], "stage")
    assert code_of(block) == "NO_FLOOR" and "暂停" in block["why_not"]
    assert block["floor"]["paused"] is True
    assert block["floor"]["seconds_left"] == 42.0
    world.clock.advance(100)  # remaining time does not move while paused
    assert by_id(build_board(world, "u1")["channels"], "stage")["floor"]["seconds_left"] == 42.0


def test_turn_floor_with_several_active_holders():
    world = make_world()
    tokens = [token("u1", deadline=30.0), token("u2", deadline=30.0)]
    add_channel(
        world,
        id="talk",
        name="轮次",
        type="group",
        mode="turn",
        members=[MODERATOR_ID, "u1", "u2", "u3"],
        group=FloorGroup(id="g2", channel_id="talk", mode="turn", tokens=tokens),
    )
    for holder in ("u1", "u2"):
        block = by_id(build_board(world, holder)["channels"], "talk")
        assert block["can_write"] is True and block["floor"]["kind"] == "turn"
        assert block["floor"]["mine"]["seconds_left"] == 30.0
    other = by_id(build_board(world, "u3")["channels"], "talk")
    assert code_of(other) == "NO_FLOOR" and other["floor"]["active"] == "u1"
    assert [q["state"] for q in other["floor"]["queue"]] == ["active", "active"]


def test_token_with_used_up_budget_is_not_writable():
    world = make_world()
    tokens = [token("u1", messages_used=3)]
    add_channel(
        world,
        id="talk",
        name="轮次",
        type="group",
        mode="turn",
        members=[MODERATOR_ID, "u1"],
        group=FloorGroup(id="g2", channel_id="talk", mode="turn", tokens=tokens),
    )
    block = by_id(build_board(world, "u1")["channels"], "talk")
    assert code_of(block) == "NO_FLOOR" and "用完" in block["why_not"]


def test_turn_channel_without_a_group_has_no_floor():
    world = make_world()
    add_channel(world, id="talk", name="轮次", type="group", mode="turn", members=[MODERATOR_ID, "u1"])
    block = by_id(build_board(world, "u1")["channels"], "talk")
    assert code_of(block) == "NO_FLOOR" and block["floor"]["queue"] == [] and block["floor"]["active"] is None


def test_closed_channel_has_floor_kind_none():
    world = make_world()
    world.channel("public").mode = "closed"
    assert by_id(build_board(world, "u1")["channels"], "public")["floor"]["kind"] == "none"


# --------------------------------------------------------------------------- requests


def open_request(world, targets, **overrides) -> str:
    payload = {"to": targets, "kind": "choice", "question": "同意吗？", "options": choice_options()}
    payload.update(overrides)
    receipt = mod(world, "open_request", **payload)
    assert receipt.ok, receipt
    return receipt.data["request_id"]


def test_players_see_only_their_own_open_requests():
    world = make_world()
    mine = open_request(world, ["u1", "u2"], duration_s=40)
    open_request(world, ["u3"])
    closed = open_request(world, ["u1"])
    ask(world, "u1", "respond", request_id=closed, value="yes")  # closes it

    rows = build_board(world, "u1")["requests"]
    assert [r["id"] for r in rows] == [mine]
    row = rows[0]
    assert row == {
        "id": mine,
        "kind": "choice",
        "question": "同意吗？",
        "options": choice_options(),
        "seconds_left": 40.0,
        "sealed": False,
        "share_answers": False,
        "auto_close": "all_answered",
        "answered": False,
        "to": ["u1", "u2"],
    }
    assert build_board(world, "u4")["requests"] == []

    world.clock.advance(15)
    assert build_board(world, "u1")["requests"][0]["seconds_left"] == 25.0

    ask(world, "u1", "respond", request_id=mine, value="no", reason="理由")
    answered = build_board(world, "u1")["requests"][0]
    assert answered["answered"] is True and answered["my_answer"] == {"value": "no", "reason": "理由"}
    assert "answered_by" not in answered
    assert build_board(world, "u2")["requests"][0]["answered"] is False


def test_moderator_sees_all_open_requests_with_progress_but_never_sealed_answers():
    world = make_world()
    plain = open_request(world, ["u1", "u2", "u3"], default="no")
    sealed = open_request(world, ["u2", "u4"], sealed=True)
    ask(world, "u3", "respond", request_id=plain, value="yes")
    ask(world, "u2", "respond", request_id=plain, value="no")
    ask(world, "u4", "respond", request_id=sealed, value="yes", reason="绝密理由")

    rows = {r["id"]: r for r in build_board(world, MODERATOR_ID)["requests"]}
    assert set(rows) == {plain, sealed}
    assert rows[plain]["answered_by"] == ["u2", "u3"] and rows[plain]["default"] == "no"
    assert rows[sealed]["answered_by"] == ["u4"] and rows[sealed]["sealed"] is True
    assert "绝密理由" not in json.dumps(build_board(world, MODERATOR_ID), ensure_ascii=False)

    mod(world, "cancel_request", request_id=plain)
    assert [r["id"] for r in build_board(world, MODERATOR_ID)["requests"]] == [sealed]


# --------------------------------------------------------------------------- abilities


def test_player_board_lists_only_abilities_the_player_holds():
    world = make_world()
    holder = role_holder(world, "alpha")
    other = next(p.id for p in world.state.players if p.id != holder)

    mine = build_board(world, holder)["abilities"]
    assert {a["id"] for a in mine} == {"strike", "ping"}
    strike = by_id(mine, "strike")
    assert strike == {
        "id": "strike",
        "description": "对一名玩家发动 strike。",
        "enabled": False,
        "uses_left": 1,
        "params": {"target": "player_id", "note": "text(最多10字)?"},
    }
    assert {a["id"] for a in build_board(world, other)["abilities"]} == {"ping"}

    mod(world, "enable_ability", ability_id="strike")
    assert by_id(build_board(world, holder)["abilities"], "strike")["enabled"] is True
    ask(world, holder, "use_ability", ability_id="strike", params={"target": other})
    assert by_id(build_board(world, holder)["abilities"], "strike")["uses_left"] == 0

    mod(world, "enable_ability", ability_id="ping", holders=[holder])
    assert by_id(build_board(world, holder)["abilities"], "ping")["enabled"] is True
    assert by_id(build_board(world, other)["abilities"], "ping")["enabled"] is False  # holder by rule, but not allowed

    mod(world, "update_player", player_id=holder, add_tags=["out"])  # no longer matches alive(...)
    assert {a["id"] for a in build_board(world, holder)["abilities"]} == set()


def test_moderator_sees_every_ability_with_state_and_holders():
    world = make_world()
    holder = role_holder(world, "alpha")
    mod(world, "enable_ability", ability_id="ping", holders=["u1", "u2"])
    ask(world, "u1", "use_ability", ability_id="ping")
    rows = build_board(world, MODERATOR_ID)["abilities"]
    assert [r["id"] for r in rows] == ["strike", "ping"]
    strike, ping = rows
    assert strike["enabled"] is False and strike["holders"] == [holder] and strike["quota"] == 1
    assert ping["enabled"] is True and ping["allowed"] == ["u1", "u2"] and ping["uses"] == {"u1": 1}
    assert sorted(ping["holders"]) == ["u1", "u2", "u3", "u4"]


# --------------------------------------------------------------------------- tickets / dashboard


def test_tickets_players_see_their_own_and_the_moderator_sees_all():
    world = make_world()
    ask(world, "u1", "challenge", content="我的第一个质询")
    ask(world, "u2", "challenge", content="另一个人的质询")
    mod(world, "resolve_challenge", ticket_id="t1", verdict="upheld", reply="成立")

    mine = build_board(world, "u1")["tickets"]
    assert mine == [{"id": "t1", "player_id": "u1", "status": "resolved", "preview": "我的第一个质询", "verdict": "upheld"}]
    assert [t["id"] for t in build_board(world, "u2")["tickets"]] == ["t2"]
    assert build_board(world, "u3")["tickets"] == []
    assert [t["id"] for t in build_board(world, MODERATOR_ID)["tickets"]] == ["t1", "t2"]


def test_resolved_tickets_are_capped_but_open_ones_always_stay():
    world = make_world()
    total = MAX_RESOLVED_TICKETS + 5
    for i in range(total):
        ask(world, "u1", "challenge", content=f"问题 {i}")
    for i in range(1, total + 1):
        mod(world, "resolve_challenge", ticket_id=f"t{i}", verdict="rejected", reply="驳回")
    ask(world, "u1", "challenge", content="最后一个仍然开着")

    for agent in (MODERATOR_ID, "u1"):
        ids = [t["id"] for t in build_board(world, agent)["tickets"]]
        assert len(ids) == MAX_RESOLVED_TICKETS + 1
        assert ids[0] == "t6" and ids[-1] == f"t{total + 1}"  # the five oldest resolved ones were dropped


def test_the_dashboard_is_on_the_moderators_board_only_never_a_players():
    world = make_world()
    mod(world, "update_dashboard", factions=[{"name": "红队", "alive": 1, "total": 1}], tag_stats=[{"name": "存活", "count": 4}])
    dash = build_board(world, MODERATOR_ID)["dashboard"]
    assert dash["factions"][0]["name"] == "红队" and dash["tag_stats"][0]["count"] == 4
    dash["factions"].clear()
    assert world.state.dashboard.factions  # a copy
    # faction statistics would leak hidden information (who was exiled) to the players
    for agent in world.everyone():
        if agent != MODERATOR_ID:
            assert build_board(world, agent)["dashboard"] == {"factions": [], "tag_stats": []}
