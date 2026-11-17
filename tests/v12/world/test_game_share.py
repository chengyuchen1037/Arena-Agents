"""v0.12.1: group-visible requests (``share_answers``), ``auto_close``, request results / views and the
ability enable / disable semantics (contracts section 10.1)."""

from __future__ import annotations

from app.world.board import build_board
from app.world.projection import request_view
from app.world.types import MODERATOR_ID, Code

from .test_game_kit import ask, choice_options, deliveries, entries, events, make_world, mod, to

WOLVES = ["u1", "u2", "u3"]


def open_group(world, *, targets=WOLVES, **overrides) -> str:
    payload = {
        "to": list(targets),
        "kind": "choice",
        "question": "今晚刀谁？",
        "options": choice_options(),
        "share_answers": True,
    }
    payload.update(overrides)
    receipt = mod(world, "open_request", **payload)
    assert receipt.ok, receipt
    return receipt.data["request_id"]


def answer(world, actor, rid, value="yes", **extra):
    return ask(world, actor, "respond", request_id=rid, value=value, **extra)


# ------------------------------------------------------------------------ share_answers


def test_a_shared_answer_is_visible_to_every_recipient_and_wakes_the_others():
    world = make_world()
    rid = open_group(world)
    deliveries(world), events(world)
    assert answer(world, "u1", rid, "yes", reason="我先拍").ok

    entry = entries(world, "request.answered")[0]
    assert entry.visible_to_ids == [MODERATOR_ID, "u1", "u2", "u3"]
    items = deliveries(world)
    told = [d for d in items if d.envelope.payload.get("event") == "request.answer"]
    assert sorted(d.target for d in told) == [MODERATOR_ID, "u2", "u3"]  # nobody tells u1 about his own answer
    payload = told[0].envelope.payload
    assert (payload["request_id"], payload["player_id"], payload["value"], payload["reason"]) == (rid, "u1", "yes", "我先拍")
    assert payload["label"] and payload["replaced"] is False
    assert told[0].envelope.kind == "request" and told[0].envelope.seq == entry.seq

    ui = events(world, "request.answer")
    assert len(ui) == 1 and ui[0].data["visible_to_ids"] == [MODERATOR_ID, "u1", "u2", "u3"]
    assert ui[0].data["player_id"] == "u1" and ui[0].data["value"] == "yes"


def test_a_player_who_is_not_a_recipient_never_sees_a_shared_request_or_its_answers():
    world = make_world()
    rid = open_group(world)
    answer(world, "u1", rid, "yes")
    assert not any(e for e in entries(world, "request.answered") if "u4" in e.visible_to_ids)
    assert build_board(world, "u4")["requests"] == []
    assert all(d.target != "u4" for d in deliveries(world))


def test_the_board_lists_the_current_answers_and_a_changed_answer_replaces_the_old_one():
    world = make_world()
    rid = open_group(world)
    answer(world, "u1", rid, "yes")
    answer(world, "u2", rid, "no", reason="不同意")
    for viewer in ("u3", MODERATOR_ID):
        row = build_board(world, viewer)["requests"][0]
        assert row["share_answers"] is True and row["auto_close"] == "all_answered"
        assert [(a["player_id"], a["value"]) for a in row["answers"]] == [("u1", "yes"), ("u2", "no")]
        assert row["answers"][1]["reason"] == "不同意" and row["answers"][0]["label"]
    assert build_board(world, "u3")["requests"][0]["answered"] is False  # u3 himself has not answered yet

    deliveries(world)
    assert answer(world, "u2", rid, "yes").ok  # changes his mind
    row = build_board(world, "u3")["requests"][0]
    assert [(a["player_id"], a["value"]) for a in row["answers"]] == [("u1", "yes"), ("u2", "yes")]
    again = [d for d in deliveries(world) if d.envelope.payload.get("event") == "request.answer"]
    assert again and all(d.envelope.payload["replaced"] is True for d in again)


def test_a_private_request_does_not_share_and_a_sealed_one_stays_sealed():
    world = make_world()
    rid = mod(world, "open_request", to=WOLVES, kind="choice", question="？", options=choice_options()).data["request_id"]
    deliveries(world)
    answer(world, "u1", rid, "yes")
    assert entries(world, "request.answered")[0].visible_to_ids == [MODERATOR_ID, "u1"]
    assert [d for d in deliveries(world) if d.envelope.payload.get("event") == "request.answer"] == []
    assert "answers" not in build_board(world, "u2")["requests"][0]

    sealed = mod(world, "open_request", to=WOLVES, kind="choice", question="密？", options=choice_options(), sealed=True).data["request_id"]
    answer(world, "u2", sealed, "no")
    assert entries(world, "request.answered")[-1].visible_to_ids == []
    # ... but the spectator stream still carries every answer (the front end filters it per viewpoint)
    ui = [e for e in events(world, "request.answer") if e.data["request_id"] == sealed]
    assert ui and ui[0].data["visible_to_ids"] == []


def test_sealed_and_shared_cannot_be_combined():
    world = make_world()
    seq = world.seq
    receipt = mod(world, "open_request", to=WOLVES, kind="choice", question="？", options=choice_options(), sealed=True, share_answers=True)
    assert not receipt.ok and receipt.code == Code.INVALID_PARAMS and world.seq == seq


# ------------------------------------------------------------------------------ auto_close


def test_unanimous_keeps_the_request_open_until_everybody_agrees():
    world = make_world()
    rid = open_group(world, auto_close="unanimous")
    assert answer(world, "u1", rid, "yes").ok
    assert answer(world, "u2", rid, "no").ok
    third = answer(world, "u3", rid, "yes")
    assert third.ok and third.data["closed"] is False and "不一致" in third.message
    assert world.state.requests[rid].status == "open"

    # the dissenter comes around: now they all agree and the request closes at once
    last = answer(world, "u2", rid, "yes")
    assert last.ok and last.data["closed"] is True and "一致" in last.message
    request = world.state.requests[rid]
    assert request.status == "closed" and request.result["reason"] == "unanimous"
    assert request.result["tally"] == {"yes": 3, "no": 0}
    closed = entries(world, "request.closed")[0]
    assert closed.data["reason"] == "unanimous" and closed.data["leaders"] == ["yes"]
    told = [d for d in deliveries(world) if d.target == MODERATOR_ID and d.envelope.payload.get("event") == "request.closed"]
    assert told and told[0].envelope.payload["reason"] == "unanimous"


def test_unanimous_that_never_agrees_closes_at_the_deadline_with_the_tally():
    world = make_world()
    rid = open_group(world, auto_close="unanimous", duration_s=20)
    answer(world, "u1", rid, "yes")
    answer(world, "u2", rid, "yes")
    answer(world, "u3", rid, "no")
    world.clock.advance(21)
    world.tick()
    request = world.state.requests[rid]
    assert request.status == "closed" and request.result["reason"] == "deadline"
    assert request.result["tally"] == {"yes": 2, "no": 1} and request.result["leaders"] == ["yes"]
    assert request.result["missing"] == [] and request.result["defaulted"] == []


def test_auto_close_deadline_never_closes_early_not_even_when_everybody_answered():
    world = make_world()
    rid = open_group(world, auto_close="deadline", duration_s=10)
    for pid in WOLVES:
        assert answer(world, pid, rid, "yes").data["closed"] is False
    assert world.state.requests[rid].status == "open"
    world.clock.advance(11)
    world.tick()
    assert world.state.requests[rid].result["reason"] == "deadline"


def test_the_default_all_answered_still_closes_at_once_and_unanimous_needs_a_choice():
    world = make_world()
    rid = open_group(world)
    for pid, value in zip(WOLVES, ("yes", "no", "yes")):
        answer(world, pid, rid, value)
    assert world.state.requests[rid].status == "closed" and world.state.requests[rid].result["reason"] == "all_answered"
    text = mod(world, "open_request", to=WOLVES, kind="text", question="说说", auto_close="unanimous")
    assert not text.ok and text.code == Code.INVALID_PARAMS


def test_cancel_records_a_result_and_reopen_keeps_the_sharing_options():
    world = make_world()
    rid = open_group(world, auto_close="unanimous")
    assert mod(world, "cancel_request", request_id=rid).ok
    assert world.state.requests[rid].result == {"reason": "cancelled", "tally": None, "leaders": None, "defaulted": [], "missing": []}
    reopened = mod(world, "reopen_request", request_id=rid)
    assert reopened.ok
    new = world.state.requests[reopened.data["request_id"]]
    assert new.share_answers is True and new.auto_close == "unanimous" and new.result is None


# ------------------------------------------------------------------------------ views


def test_the_god_view_of_a_request_carries_every_answer_even_sealed_ones_and_the_result():
    world = make_world()
    rid = mod(world, "open_request", to=["u1", "u2"], kind="choice", question="？", options=choice_options(), sealed=True, default="no").data["request_id"]
    answer(world, "u1", rid, "yes", reason="因为")
    view = request_view(world, world.state.requests[rid])
    assert view["sealed"] is True and view["share_answers"] is False and view["auto_close"] == "all_answered"
    assert view["answers"] == [{"player_id": "u1", "value": "yes", "label": view["answers"][0]["label"], "reason": "因为", "defaulted": False}]
    assert view["result"] is None and view["answered_by"] == ["u1"] and view["opened_seq"] == world.state.requests[rid].opened_seq

    world.clock.advance(31)
    world.tick()
    view = request_view(world, world.state.requests[rid])
    assert view["status"] == "closed" and view["result"]["reason"] == "deadline"
    assert [(a["player_id"], a["defaulted"]) for a in view["answers"]] == [("u1", False), ("u2", True)]
    assert view["result"]["defaulted"] == ["u2"] and view["result"]["tally"] == {"yes": 1, "no": 1}


# ------------------------------------------------------------------------------ abilities


def test_disable_with_holders_removes_only_those_players_and_the_last_one_switches_it_off():
    world = make_world()
    assert mod(world, "enable_ability", ability_id="ping").ok  # every holder (everybody alive)
    assert mod(world, "disable_ability", ability_id="ping", holders=["u1"]).ok
    state = world.state.abilities["ping"]
    assert state.enabled is True and state.allowed == ["u2", "u3", "u4"]
    assert ask(world, "u1", "use_ability", ability_id="ping").code == Code.ABILITY_DISABLED
    assert ask(world, "u2", "use_ability", ability_id="ping").ok
    assert entries(world, "ability.disabled")[0].data == {"ability_id": "ping", "holders": ["u1"]}

    assert mod(world, "disable_ability", ability_id="ping", holders=["u2", "u3"]).ok
    assert world.state.abilities["ping"].allowed == ["u4"]
    last = mod(world, "disable_ability", ability_id="ping", holders=["u4"])
    assert last.ok and "整体关闭" in last.message
    assert world.state.abilities["ping"].enabled is False and world.state.abilities["ping"].allowed is None


def test_disable_without_holders_closes_it_for_everybody_and_clears_the_limit():
    world = make_world()
    mod(world, "enable_ability", ability_id="ping", holders=["u1"])
    assert mod(world, "disable_ability", ability_id="ping").ok
    state = world.state.abilities["ping"]
    assert state.enabled is False and state.allowed is None
    assert entries(world, "ability.disabled")[-1].data == {"ability_id": "ping"}


def test_an_empty_holders_list_is_an_error_for_both_directions_and_changes_nothing():
    world = make_world()
    seq = world.seq
    for tool in ("enable_ability", "disable_ability"):
        receipt = mod(world, tool, ability_id="ping", holders=[])
        assert not receipt.ok and receipt.code == Code.INVALID_PARAMS and "空列表" in receipt.message or "empty" in receipt.message.lower()
    assert world.seq == seq and world.state.abilities["ping"].enabled is False
