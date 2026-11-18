"""``seat_status``: what the person who plays a seat may do right now (v0.13, F12).

The numbers must be the commit gate's own: every scenario of the board / gate lock test is replayed here
for every seat, against ``speech_gate``, the state board and a real ``speak`` on a copy of the world.
"""

from __future__ import annotations

import json

import pytest

from app.world.board import build_board
from app.world.chat import speech_gate
from app.world.seat import seat_status
from app.world.types import MODERATOR_ID, Code, Receipt

from .test_game_integration import SCENARIOS, make_full_world, restore, speak
from .test_game_kit import PLAYER_IDS, ask, mod, role_holder


def _drain(world) -> None:
    world.drain_outbox()
    world.drain_events()


def _resumed_copy(world):
    clone = restore(world)
    clone.set_running(True, "resume")
    return clone


# --------------------------------------------------------------------------- locked to the gate


@pytest.mark.parametrize("setup", SCENARIOS)
def test_seat_status_agrees_with_the_gate_the_board_and_a_real_speak(setup):
    world = make_full_world()
    setup(world)
    checked = 0
    for seat in PLAYER_IDS:
        status = seat_status(world, seat)
        board = {c["id"]: c for c in build_board(world, seat)["channels"]}
        mine = {c.id for c in world.state.channels if seat in c.members}
        assert set(status["channels"]) == set(board) == mine
        for channel_id, state in status["channels"].items():
            gate = speech_gate(world, world.channel(channel_id), seat)
            allowed = not isinstance(gate, Receipt)
            where = (seat, channel_id, state["why_not"])
            assert state["can_write"] is allowed is board[channel_id]["can_write"], where
            assert state["max_chars"] == board[channel_id]["max_chars"], where
            if allowed:
                assert state["why_not"] is None and state["code"] is None, where
                assert state["max_chars"] == gate.max_chars and state["max_chars"] > 0, where
            else:
                assert state["code"] == gate.code == board[channel_id]["why_not"].partition("：")[0], where
                assert state["why_not"] and state["max_chars"] is None, where
            # the real thing: a message over the cap is cut exactly there; a refused one meets the same code
            clone = _resumed_copy(world)
            cap = state["max_chars"] or 10
            receipt = speak(clone, seat, channel_id, "字" * (cap + 7))
            assert receipt.ok is allowed, (where, receipt)
            if allowed:
                assert receipt.truncated == {"dropped_chars": 7, "kept_chars": cap}, where
            else:
                assert receipt.code == state["code"], where
            checked += 1
    assert checked >= 4 * 2


def test_seat_status_is_pure_json_and_leaves_the_world_alone():
    world = make_full_world()
    assert mod(world, "set_floor_queue", channel_id="public", order=["u1", "u2"]).ok
    assert mod(world, "open_request", to=["u1", "u2"], kind="choice", question="？", options=[{"id": "a", "label": "甲"}, {"id": "b", "label": "乙"}]).ok
    before = json.dumps(world.dump(), sort_keys=True)
    seq = world.seq
    first = seat_status(world, "u1")
    second = seat_status(world, "u1")
    assert first == second
    json.dumps(first)
    assert json.dumps(world.dump(), sort_keys=True) == before and world.seq == seq


def test_unknown_seat_and_the_moderator_are_refused():
    world = make_full_world()
    with pytest.raises(KeyError):
        seat_status(world, "ghost")
    with pytest.raises(KeyError):
        seat_status(world, MODERATOR_ID)  # the moderator is not a seat


def test_only_the_channels_of_the_seat_are_listed():
    world = make_full_world()
    assert mod(world, "create_channel", channel_id="side", name="旁路", members=["u2", "u3"], mode="free").ok
    assert set(seat_status(world, "u1")["channels"]) == {"public", "dm-u1"}
    assert set(seat_status(world, "u2")["channels"]) == {"public", "dm-u2", "side"}


# --------------------------------------------------------------------------- the three kinds of cap


def test_free_discussion_and_private_chat_report_their_message_cap():
    world = make_full_world()
    channels = seat_status(world, "u1")["channels"]
    free = world.spec.defaults.free
    dm = world.spec.defaults.dm
    assert channels["public"]["can_write"] and channels["public"]["max_chars"] == free.max_chars_per_message
    assert channels["dm-u1"]["can_write"] and channels["dm-u1"]["max_chars"] == dm.max_chars_per_message
    assert channels["dm-u1"]["floor"] is None and channels["dm-u1"]["can_pass"] is False
    # the moderator's limits for one free discussion override the default of the scenario
    assert mod(world, "open_floor", channel_id="public", mode="free", limits={"max_chars_per_message": 40}).ok
    assert seat_status(world, "u1")["channels"]["public"]["max_chars"] == 40


def test_free_discussion_counts_down_the_messages_left_and_stops_at_the_quota():
    world = make_full_world()
    assert mod(world, "open_floor", channel_id="public", mode="free", limits={"max_messages_per_member": 2}).ok
    state = seat_status(world, "u1")["channels"]["public"]
    assert state["floor"]["kind"] == "free" and state["floor"]["mine"] is False and state["floor"]["messages_left"] == 2
    assert state["can_pass"] is False
    assert speak(world, "u1", "public").ok
    assert seat_status(world, "u1")["channels"]["public"]["floor"]["messages_left"] == 1
    assert seat_status(world, "u2")["channels"]["public"]["floor"]["messages_left"] == 2  # per member
    assert speak(world, "u1", "public").ok
    state = seat_status(world, "u1")["channels"]["public"]
    assert state["can_write"] is False and state["code"] == Code.QUOTA_EXCEEDED and "条数" in state["why_not"]
    assert state["floor"]["messages_left"] == 0


def test_a_token_reports_what_is_left_of_it_and_how_long():
    world = make_full_world()
    limits = {"max_messages": 3, "max_chars_total": 100, "max_chars_per_message": 40, "duration_s": 30}
    assert mod(world, "set_floor_queue", channel_id="public", order=["u1", "u2"], limits=limits).ok
    state = seat_status(world, "u1")["channels"]["public"]
    assert state["can_write"] and state["max_chars"] == 40 and state["can_pass"] is True
    assert state["floor"] == {
        "kind": "queue", "mine": True, "paused": False,
        "messages_left": 3, "chars_left": 100, "seconds_left": 30.0, "deadline_clock": 30.0,
    }
    assert speak(world, "u1", "public", "字" * 10).ok
    world.clock.advance(7)
    state = seat_status(world, "u1")["channels"]["public"]
    assert state["floor"]["messages_left"] == 2 and state["floor"]["chars_left"] == 90
    assert state["floor"]["seconds_left"] == 23.0 and state["floor"]["deadline_clock"] == 30.0
    # what is left of the total clips the per-message cap
    assert speak(world, "u1", "public", "字" * 40).ok
    assert seat_status(world, "u1")["channels"]["public"]["max_chars"] == 40  # 50 left, 40 per message
    assert speak(world, "u1", "public", "字" * 40).ok  # the token ends (3 messages used)
    assert seat_status(world, "u1")["channels"]["public"]["can_write"] is False
    # u2 holds the floor now
    second = seat_status(world, "u2")["channels"]["public"]
    assert second["can_write"] is True and second["floor"]["mine"] is True and second["can_pass"] is True


def test_the_cap_is_clipped_to_what_is_left_of_the_total():
    world = make_full_world()
    limits = {"max_messages": 5, "max_chars_total": 60, "max_chars_per_message": 40, "duration_s": 30}
    assert mod(world, "open_floor", channel_id="public", mode="turn", holders=["u1"], limits=limits).ok
    assert speak(world, "u1", "public", "字" * 30).ok
    state = seat_status(world, "u1")["channels"]["public"]
    assert state["max_chars"] == 30 and state["floor"]["chars_left"] == 30  # 60 - 30, below the 40 per message


# --------------------------------------------------------------------------- not your turn / pass


def test_waiting_for_your_turn_says_so_without_internal_ids():
    world = make_full_world()
    assert mod(world, "set_floor_queue", channel_id="public", order=["u1", "u2", "u3"]).ok
    waiting = seat_status(world, "u3")["channels"]["public"]
    assert waiting["can_write"] is False and waiting["code"] == Code.NO_FLOOR and waiting["max_chars"] is None
    assert waiting["floor"]["mine"] is False and waiting["floor"]["messages_left"] is None
    assert waiting["can_pass"] is False
    assert "还没轮到你" in waiting["why_not"]
    # the speaker is named by nickname only (the gate's "<id> <name>" label is plain here)
    assert "甲" in waiting["why_not"] and "u1" not in waiting["why_not"]
    # after the pass of u1 the turn goes on to u2; u3 is still waiting
    assert ask(world, "u1", "pass", channel_id="public").ok
    assert seat_status(world, "u1")["channels"]["public"]["can_write"] is False
    assert "已经结束" in seat_status(world, "u1")["channels"]["public"]["why_not"]
    assert seat_status(world, "u2")["channels"]["public"]["can_pass"] is True


def test_pause_and_freeze_stop_both_writing_and_passing():
    world = make_full_world()
    assert mod(world, "set_floor_queue", channel_id="public", order=["u1", "u2"], limits={"duration_s": 50}).ok
    world.clock.advance(8)
    assert mod(world, "pause_queue", channel_id="public").ok
    state = seat_status(world, "u1")["channels"]["public"]
    assert state["can_write"] is False and state["can_pass"] is False and "暂停" in state["why_not"]
    assert state["floor"]["paused"] is True and state["floor"]["mine"] is True
    assert state["floor"]["seconds_left"] == 42.0 and state["floor"]["deadline_clock"] is None  # frozen
    world.clock.advance(100)
    assert seat_status(world, "u1")["channels"]["public"]["floor"]["seconds_left"] == 42.0
    assert seat_status(world, "u1")["attention"] == []  # nothing to do while it is paused
    assert mod(world, "resume_queue", channel_id="public").ok
    state = seat_status(world, "u1")["channels"]["public"]
    assert state["can_write"] and state["can_pass"] and state["floor"]["deadline_clock"] == world.clock.now() + 42.0

    assert world.chat.freeze_channel("public", "test") is True
    state = seat_status(world, "u1")["channels"]["public"]
    assert state["can_write"] is False and state["can_pass"] is False and "冻结" in state["why_not"]


def test_closed_and_listen_channels_and_a_punished_private_chat():
    world = make_full_world()
    assert mod(world, "close_floor", channel_id="public").ok
    closed = seat_status(world, "u1")["channels"]["public"]
    assert closed["can_write"] is False and closed["code"] == Code.CHANNEL_CLOSED and closed["floor"] is None
    assert mod(world, "update_channel", channel_id="public", mode="listen").ok
    assert "只听" in seat_status(world, "u1")["channels"]["public"]["why_not"]
    assert mod(world, "update_channel", channel_id="dm-u1", mode="closed").ok
    dm = seat_status(world, "u1")["channels"]["dm-u1"]
    assert dm["can_write"] is False and "私聊" in dm["why_not"]
    assert seat_status(world, "u2")["channels"]["dm-u2"]["can_write"] is True


# --------------------------------------------------------------------------- requests


def _ask_all(world, to, **extra):
    options = [{"id": "yes", "label": "同意"}, {"id": "no", "label": "反对"}]
    payload = {"to": to, "kind": "choice", "question": "同意吗？", "options": options, "duration_s": 60}
    payload.update(extra)
    receipt = mod(world, "open_request", **payload)
    assert receipt.ok, receipt
    return receipt.data["request_id"]


def test_open_requests_of_the_seat_with_my_answer_and_the_deadline():
    world = make_full_world()
    world.clock.advance(10)
    rid = _ask_all(world, ["u1", "u2"])
    state = seat_status(world, "u1")
    [row] = state["requests"]
    assert row == {
        "id": rid, "kind": "choice", "max_chars": None, "answered": False, "my_answer": None,
        "seconds_left": 60.0, "deadline_clock": 70.0, "channel_id": "public",
    }
    assert seat_status(world, "u3")["requests"] == []  # not addressed to u3
    assert state["attention"] == ["public"]
    world.clock.advance(15)
    assert seat_status(world, "u1")["requests"][0]["seconds_left"] == 45.0
    assert ask(world, "u1", "respond", request_id=rid, value="yes", reason="好").ok
    [row] = seat_status(world, "u1")["requests"]
    assert row["answered"] is True and row["my_answer"] == {"value": "yes", "label": "同意", "reason": "好"}
    assert seat_status(world, "u1")["attention"] == []  # answered: nothing waits any more (it may still be changed)
    assert seat_status(world, "u2")["requests"][0]["answered"] is False
    # a changed answer is the last one
    assert ask(world, "u1", "respond", request_id=rid, value="no").ok
    assert seat_status(world, "u1")["requests"][0]["my_answer"]["label"] == "反对"


def test_a_closed_request_is_gone_and_a_sealed_one_still_shows_my_own_answer():
    world = make_full_world()
    rid = _ask_all(world, ["u1"], sealed=True)
    assert ask(world, "u1", "respond", request_id=rid, value="yes").ok  # all answered: the request closes
    assert seat_status(world, "u1")["requests"] == []
    sealed = _ask_all(world, ["u1", "u2"], sealed=True)
    assert ask(world, "u1", "respond", request_id=sealed, value="no").ok
    [row] = seat_status(world, "u1")["requests"]
    assert row["my_answer"]["value"] == "no"


def test_a_text_request_and_the_channel_that_shows_its_card():
    world = make_full_world()
    assert mod(world, "create_channel", channel_id="duo", name="二人组", members=["u1", "u2"], mode="free").ok
    rid = mod(world, "open_request", to=["u1", "u2"], kind="text", question="说说看", duration_s=30).data["request_id"]
    rows = {r["id"]: r for r in seat_status(world, "u1")["requests"]}
    assert rows[rid]["kind"] == "text" and rows[rid]["channel_id"] == "duo"  # the smallest channel with both
    assert rows[rid]["max_chars"] == world.spec.defaults.request.max_text_chars == 20  # a text answer is cut there
    assert seat_status(world, "u1")["attention"] == ["duo"]
    alone = mod(world, "open_request", to=["u1"], kind="text", question="只问你", duration_s=30).data["request_id"]
    rows = {r["id"]: r for r in seat_status(world, "u1")["requests"]}
    assert rows[alone]["channel_id"] == "dm-u1"  # alone: the private chat is the smallest fit
    everyone = mod(world, "open_request", to=["u1", "u3"], kind="text", question="你们俩", duration_s=30).data["request_id"]
    rows = {r["id"]: r for r in seat_status(world, "u1")["requests"]}
    assert rows[everyone]["channel_id"] == "public"


# --------------------------------------------------------------------------- abilities


def test_only_enabled_abilities_the_seat_holds_are_listed_with_typed_params():
    world = make_full_world()
    holder = role_holder(world, "alpha")
    other = next(p for p in PLAYER_IDS if p != holder)
    assert seat_status(world, holder)["abilities"] == []  # nothing is enabled yet
    assert mod(world, "enable_ability", ability_id="strike").ok
    [ability] = seat_status(world, holder)["abilities"]
    assert ability["id"] == "strike" and ability["quota_left"] == 1 and "strike" in ability["description"]
    params = {p["name"]: p for p in ability["params"]}
    assert params["target"] == {"name": "target", "type": "player_id", "optional": False, "candidates": PLAYER_IDS}
    assert params["note"] == {"name": "note", "type": "text", "optional": True, "max_chars": 10}
    assert [p["name"] for p in ability["params"]] == ["target", "note"]  # the order of the scenario
    # the one who does not hold it sees nothing of it; "ping" is held by everybody and has no params
    assert [a["id"] for a in seat_status(world, other)["abilities"]] == []
    assert mod(world, "enable_ability", ability_id="ping").ok
    assert [a["id"] for a in seat_status(world, other)["abilities"]] == ["ping"]
    assert seat_status(world, other)["abilities"][0]["params"] == [] and seat_status(world, other)["abilities"][0]["quota_left"] == 2


def test_the_candidates_of_a_player_param_are_the_players_still_in_the_game():
    world = make_full_world()
    holder = role_holder(world, "alpha")
    out = next(p for p in PLAYER_IDS if p != holder)
    assert mod(world, "update_player", player_id=out, add_tags=["out"]).ok
    assert mod(world, "enable_ability", ability_id="strike").ok
    [ability] = seat_status(world, holder)["abilities"]
    target = next(p for p in ability["params"] if p["name"] == "target")
    assert out not in target["candidates"] and holder in target["candidates"]


def test_an_ability_for_chosen_holders_and_a_used_up_ability():
    world = make_full_world()
    holder = role_holder(world, "alpha")
    other = next(p for p in PLAYER_IDS if p != holder)
    assert mod(world, "enable_ability", ability_id="ping", holders=[other]).ok
    assert [a["id"] for a in seat_status(world, other)["abilities"]] == ["ping"]
    assert [a["id"] for a in seat_status(world, holder)["abilities"]] == []  # enabled for somebody else only
    assert ask(world, other, "use_ability", ability_id="ping").ok
    assert seat_status(world, other)["abilities"][0]["quota_left"] == 1
    assert ask(world, other, "use_ability", ability_id="ping").ok
    assert seat_status(world, other)["abilities"] == []  # quota used up
    assert mod(world, "enable_ability", ability_id="strike").ok
    assert ask(world, holder, "use_ability", ability_id="strike", params={"target": other}).ok
    assert seat_status(world, holder)["abilities"] == []
    # the ability froze the public channel: nobody can write there until the moderator rules
    assert seat_status(world, other)["channels"]["public"]["can_write"] is False


def test_every_param_type_is_described():
    from app.world.spec import ScenarioSpec

    from .test_game_kit import SPEC_DICT, make_spec

    import copy

    data = copy.deepcopy(SPEC_DICT)
    data["abilities"]["wide"] = {
        "holders": "${alive()}",
        "quota": 3,
        "enabled": True,
        "params": {
            "who": "player_ids",
            "side": "enum(左,右)",
            "count": "int(1,5)",
            "free": "int?",
            "reason": "text(80)",
        },
        "description": "全部参数类型。",
    }
    world = make_full_world(spec=ScenarioSpec.model_validate(data))
    [ability] = [a for a in seat_status(world, PLAYER_IDS[0])["abilities"] if a["id"] == "wide"]
    params = {p["name"]: p for p in ability["params"]}
    assert params["who"]["type"] == "player_ids" and params["who"]["candidates"] == PLAYER_IDS
    assert params["side"] == {"name": "side", "type": "enum", "optional": False, "values": ["左", "右"]}
    assert params["count"] == {"name": "count", "type": "int", "optional": False, "min": 1, "max": 5}
    assert params["free"] == {"name": "free", "type": "int", "optional": True, "min": None, "max": None}
    assert params["reason"]["max_chars"] == 80 and params["reason"]["optional"] is False
    assert make_spec() is not None


# --------------------------------------------------------------------------- attention


def test_attention_lists_the_channels_that_wait_for_the_player():
    world = make_full_world()
    assert seat_status(world, "u1")["attention"] == []
    assert mod(world, "set_floor_queue", channel_id="public", order=["u1", "u2"]).ok
    assert seat_status(world, "u1")["attention"] == ["public"]  # the floor is mine
    assert seat_status(world, "u2")["attention"] == []  # waiting is not an action
    assert ask(world, "u1", "pass", channel_id="public").ok
    assert seat_status(world, "u1")["attention"] == []
    assert seat_status(world, "u2")["attention"] == ["public"]
    rid = _ask_all(world, ["u2"])
    # the floor + the request card in my own private chat (creation order of the channels: the chats came first)
    assert seat_status(world, "u2")["attention"] == ["dm-u2", "public"]
    assert ask(world, "u2", "respond", request_id=rid, value="yes").ok
    assert seat_status(world, "u2")["attention"] == ["public"]


def test_the_status_carries_the_ledger_seq_it_was_computed_at():
    world = make_full_world()
    state = seat_status(world, "u1")
    assert state["seat"] == "u1" and state["seq"] == world.seq
    assert speak(world, "u1", "public").ok
    assert seat_status(world, "u1")["seq"] == world.seq > state["seq"]
