"""Requests: open / respond / sealed / close / default / expire / cancel / reopen / redeliver
(contracts §2.3, §2.5, §2.6, §8.2)."""

from __future__ import annotations

from app.world.state import WorldState
from app.world.types import ENGINE_ID, MODERATOR_ID, Code, LedgerEntry

from .test_game_kit import (
    PLAYER_IDS,
    ManualClock,
    ask,
    choice_options,
    deliveries,
    entries,
    events,
    make_world,
    mod,
    to,
)


def open_req(world, targets=("u1", "u2", "u3"), **overrides) -> str:
    payload = {"to": list(targets), "kind": "choice", "question": "是否同意？", "options": choice_options()}
    payload.update(overrides)
    receipt = mod(world, "open_request", **payload)
    assert receipt.ok, receipt
    return receipt.data["request_id"]


def answer(world, actor: str, request_id: str, value: str = "yes", **extra):
    return ask(world, actor, "respond", request_id=request_id, value=value, **extra)


# --------------------------------------------------------------------------- open


def test_open_request_creates_state_entry_deliveries_and_ui_event():
    world = make_world()
    receipt = mod(
        world,
        "open_request",
        to=["u2", "u1"],
        kind="choice",
        question="是否同意？",
        options=[{"id": "yes", "label": "同意"}, {"id": "no"}],
    )
    assert receipt.ok and receipt.data["request_id"] == "r1"
    assert receipt.seq == world.seq

    request = world.state.requests["r1"]
    assert request.status == "open" and request.to == ["u2", "u1"] and request.kind == "choice"
    assert request.deadline == 30.0  # defaults.request.duration_s
    assert request.options == [{"id": "yes", "label": "同意"}, {"id": "no", "label": "no"}]
    assert request.opened_seq == receipt.seq and request.created_gt == 0.0

    entry = entries(world, "request.opened")[0]
    assert entry.visible_to_ids == [MODERATOR_ID, "u2", "u1"]
    assert entry.data == {
        "request_id": "r1",
        "kind": "choice",
        "question": "是否同意？",
        "options": request.options,
        "to": ["u2", "u1"],
        "duration_s": 30.0,
        "sealed": False,
    }

    items = deliveries(world)
    assert [d.target for d in items] == ["u2", "u1"]
    env = to(items, "u1", "request.opened")[0].envelope
    assert (env.kind, env.priority, env.interrupt, env.seq) == ("request", "urgent", False, entry.seq)
    assert env.payload == {
        "event": "request.opened",
        "request_id": "r1",
        "kind": "choice",
        "question": "是否同意？",
        "options": request.options,
        "seconds": 30.0,
        "sealed": False,
    }
    ui = events(world, "request.opened")
    assert len(ui) == 1
    view = ui[0].data["request"]
    assert view["id"] == "r1" and view["status"] == "open" and view["answered_by"] == []
    assert view["seconds_left"] == 30.0 and view["to"] == ["u2", "u1"] and view["sealed"] is False


def test_open_request_accepts_custom_duration_text_kind_and_dedupes_targets():
    world = make_world()
    world.clock.advance(10)
    receipt = mod(world, "open_request", to=["u1", "u1", "u2"], kind="text", question="你的想法？", duration_s=5)
    assert receipt.ok
    request = world.state.requests[receipt.data["request_id"]]
    assert request.to == ["u1", "u2"] and request.kind == "text" and request.options == []
    assert request.deadline == 15.0


def test_open_request_rejects_unknown_targets_without_touching_state():
    world = make_world()
    seq = world.seq
    counters = dict(world.state.counters)
    receipt = mod(world, "open_request", to=["u1", "ghost"], kind="text", question="？")
    assert not receipt.ok and receipt.code == Code.UNKNOWN_PLAYER
    assert world.seq == seq and world.state.counters == counters
    assert world.state.requests == {} and deliveries(world) == []
    # the moderator is not a valid target either
    assert mod(world, "open_request", to=[MODERATOR_ID], kind="text", question="？").code == Code.UNKNOWN_PLAYER


def test_open_request_payload_validation_comes_from_the_gate():
    world = make_world()
    one_option = mod(world, "open_request", to=["u1"], kind="choice", question="？", options=[{"id": "a"}])
    assert one_option.code == Code.INVALID_PARAMS
    # players cannot open requests
    assert ask(world, "u1", "mod.open_request", to=["u1"], kind="text", question="x").code == Code.FORBIDDEN


# --------------------------------------------------------------------------- respond


def test_respond_validates_request_target_and_option():
    world = make_world()
    rid = open_req(world, targets=["u1", "u2"])
    world.drain_outbox()
    seq = world.seq

    assert answer(world, "u1", "r99").code == Code.UNKNOWN_REQUEST
    assert answer(world, "u3", rid).code == Code.FORBIDDEN  # not a target
    bad = answer(world, "u1", rid, value="maybe")
    assert bad.code == Code.INVALID_TARGET and "yes" in bad.message and "no" in bad.message
    assert world.seq == seq and world.state.requests[rid].answers == {}


def test_respond_choice_records_answer_entry_and_progress_event():
    world = make_world()
    rid = open_req(world)
    world.drain_events()

    receipt = answer(world, "u2", rid, "no", reason="我不同意")
    assert receipt.ok and receipt.data["replaced"] is False and receipt.data["closed"] is False
    entry = entries(world, "request.answered")[0]
    assert entry.actor_id == "u2" and entry.visible_to_ids == [MODERATOR_ID, "u2"]
    assert entry.data == {
        "request_id": rid,
        "player_id": "u2",
        "value": "no",
        "reason": "我不同意",
        "replaced": False,
    }
    stored = world.state.requests[rid].answers["u2"]
    assert (stored.value, stored.reason, stored.defaulted, stored.seq) == ("no", "我不同意", False, entry.seq)

    progress = events(world, "request.progress")
    assert progress[0].data == {"request_id": rid, "answered_by": ["u2"], "total": 3}
    # not everybody answered: nothing for the moderator yet
    assert to(deliveries(world), MODERATOR_ID) == []
    assert entries(world, "request.closed") == []


def test_respond_can_be_repeated_and_the_last_answer_wins():
    world = make_world()
    rid = open_req(world)
    answer(world, "u1", rid, "yes")
    receipt = answer(world, "u1", rid, "no")

    assert receipt.ok and receipt.data["replaced"] is True
    first, second = entries(world, "request.answered")
    assert first.data["replaced"] is False and second.data["replaced"] is True
    assert world.state.requests[rid].answers["u1"].value == "no"
    assert world.state.requests[rid].status == "open"  # still waiting for the others


def test_respond_text_is_truncated_to_the_configured_limit():
    world = make_world()
    rid = open_req(world, targets=["u1"], kind="text", question="说点什么", options=[])
    receipt = answer(world, "u1", rid, "一二三四五六七八九十" * 3)  # 30 chars, limit is 20

    assert receipt.ok and receipt.truncated == {"dropped_chars": 10, "kept_chars": 20}
    assert world.state.requests[rid].answers["u1"].value == ("一二三四五六七八九十" * 2)


def test_respond_blank_text_is_rejected_without_side_effects():
    world = make_world()
    rid = open_req(world, targets=["u1", "u2"], kind="text", question="说点什么", options=[])
    seq = world.seq
    receipt = answer(world, "u1", rid, "   ")
    assert receipt.code == Code.INVALID_PARAMS
    assert world.seq == seq and world.state.requests[rid].answers == {}


def test_respond_after_close_or_cancel_is_rejected():
    world = make_world()
    closed = open_req(world, targets=["u1"])
    answer(world, "u1", closed)
    assert world.state.requests[closed].status == "closed"
    assert answer(world, "u1", closed).code == Code.REQUEST_CLOSED

    cancelled = open_req(world, targets=["u1"])
    assert mod(world, "cancel_request", request_id=cancelled).ok
    assert answer(world, "u1", cancelled).code == Code.REQUEST_CLOSED


def test_respond_works_while_the_system_dm_is_closed():
    world = make_world()
    rid = open_req(world, targets=["u1"])
    world.channel("dm-u1").mode = "closed"
    assert answer(world, "u1", rid).ok  # respond is not speech


# --------------------------------------------------------------------------- closing


def test_everyone_answering_closes_the_request_immediately_in_seat_order():
    world = make_world()
    rid = open_req(world, targets=["u3", "u1", "u2"])
    answer(world, "u3", rid, "no", reason="三")
    answer(world, "u1", rid, "yes")
    world.drain_outbox()
    world.drain_events()
    last = answer(world, "u2", rid, "yes")

    assert last.ok and last.data["closed"] is True
    request = world.state.requests[rid]
    assert request.status == "closed"

    closed = entries(world, "request.closed")
    assert len(closed) == 1
    entry = closed[0]
    assert entry.actor_id == ENGINE_ID and entry.visible_to_ids == [MODERATOR_ID]
    assert entry.data["request_id"] == rid and entry.data["reason"] == "all_answered"
    assert [a["player_id"] for a in entry.data["answers"]] == ["u1", "u2", "u3"]  # seat order
    assert entry.data["answers"][2] == {"player_id": "u3", "value": "no", "reason": "三", "defaulted": False}
    assert entry.data["defaulted"] == [] and entry.data["missing"] == []

    items = deliveries(world)
    woken = to(items, MODERATOR_ID, "request.closed")
    assert len(woken) == 1
    env = woken[0].envelope
    assert (env.kind, env.priority, env.interrupt) == ("request", "urgent", False)
    assert env.payload["request_id"] == rid and env.payload["reason"] == "all_answered"
    assert env.payload["answers"] == entry.data["answers"]
    assert env.payload["defaulted"] == [] and env.payload["missing"] == []
    assert [d for d in items if d.envelope.payload.get("event") == "request.cancelled"] == []

    ui = events(world)
    closed_event = [e for e in ui if e.type == "request.closed"][0]
    assert closed_event.data == {
        "request_id": rid,
        "reason": "all_answered",
        "answered_by": ["u1", "u2", "u3"],
        "defaulted": [],
    }
    assert [e.data["answered_by"] for e in ui if e.type == "request.progress"] == [["u1", "u2", "u3"]]
    assert world.next_deadline() is None


def test_sealed_answers_are_invisible_to_everybody_until_the_request_closes():
    world = make_world()
    rid = open_req(world, targets=["u1", "u2"], sealed=True)
    answer(world, "u1", rid, "yes", reason="秘密理由")
    answer(world, "u1", rid, "no")  # replaced while sealed

    for agent in [MODERATOR_ID, *PLAYER_IDS]:
        assert [e for e in world.entries_visible(agent) if e.type == "request.answered"] == []
    answered = entries(world, "request.answered")
    assert len(answered) == 2 and all(e.visible_to_ids == [] for e in answered)
    # no delivery leaks the answers either
    assert to(world.drain_outbox(), MODERATOR_ID) == []
    # the UI progress event only carries who answered, never what
    progress = events(world, "request.progress")
    assert all(set(e.data) == {"request_id", "answered_by", "total"} for e in progress)

    answer(world, "u2", rid, "yes")
    closed = entries(world, "request.closed")[0]
    assert closed.visible_to_ids == [MODERATOR_ID]
    assert [(a["player_id"], a["value"]) for a in closed.data["answers"]] == [("u1", "no"), ("u2", "yes")]
    # still no player can read any answer, and the moderator reads them only through request.closed
    for agent in PLAYER_IDS:
        assert [e for e in world.entries_visible(agent) if e.type in ("request.answered", "request.closed")] == []
    assert [e.type for e in world.entries_visible(MODERATOR_ID) if e.type.startswith("request.")] == [
        "request.opened",
        "request.closed",
    ]


def test_deadline_closes_the_request_with_missing_players_and_cancels_their_work():
    world = make_world()
    rid = open_req(world, targets=["u1", "u2", "u3"], duration_s=20)
    answer(world, "u2", rid, "yes")
    world.drain_outbox()
    world.drain_events()

    assert world.next_deadline() == 20.0
    world.clock.advance(19)
    world.tick()
    assert world.state.requests[rid].status == "open" and entries(world, "request.closed") == []

    world.clock.advance(1)
    world.tick()
    assert world.state.requests[rid].status == "closed"
    entry = entries(world, "request.closed")[0]
    assert entry.data["reason"] == "deadline"
    assert entry.data["missing"] == ["u1", "u3"] and entry.data["defaulted"] == []
    assert [a["player_id"] for a in entry.data["answers"]] == ["u2"]

    items = deliveries(world)
    assert [d.envelope.priority for d in to(items, MODERATOR_ID, "request.closed")] == ["urgent"]
    cancelled = [d for d in items if d.envelope.payload.get("event") == "request.cancelled"]
    assert [d.target for d in cancelled] == ["u1", "u3"]  # the one who answered is left alone
    for d in cancelled:
        assert (d.envelope.kind, d.envelope.priority, d.envelope.interrupt) == ("request", "ambient", True)
        assert d.envelope.payload == {"event": "request.cancelled", "request_id": rid, "reason": "expired"}
    ui = events(world, "request.closed")
    assert ui[0].data["answered_by"] == ["u2"] and ui[0].data["reason"] == "deadline"

    # idempotent: a second tick changes nothing
    seq = world.seq
    world.tick()
    assert world.seq == seq and world.drain_outbox() == []
    assert world.next_deadline() is None


def test_default_answer_is_applied_to_those_who_did_not_answer():
    world = make_world()
    rid = open_req(world, targets=["u1", "u2", "u3"], default="no", duration_s=10)
    answer(world, "u1", rid, "yes")
    world.clock.advance(10)
    world.tick()

    request = world.state.requests[rid]
    assert request.answers["u2"].defaulted and request.answers["u2"].value == "no"
    assert request.answers["u1"].defaulted is False
    entry = entries(world, "request.closed")[0]
    assert entry.data["defaulted"] == ["u2", "u3"] and entry.data["missing"] == []
    assert entry.data["answers"] == [
        {"player_id": "u1", "value": "yes", "reason": "", "defaulted": False},
        {"player_id": "u2", "value": "no", "reason": "", "defaulted": True},
        {"player_id": "u3", "value": "no", "reason": "", "defaulted": True},
    ]
    cancelled = [d.target for d in world.drain_outbox() if d.envelope.payload.get("event") == "request.cancelled"]
    assert cancelled == ["u2", "u3"]  # defaulted players were not answering either: their work is void
    ui = events(world, "request.closed")[0]
    assert ui.data["answered_by"] == ["u1"] and ui.data["defaulted"] == ["u2", "u3"]


def test_text_default_is_truncated_when_the_request_opens():
    world = make_world()
    rid = open_req(world, targets=["u1"], kind="text", question="？", options=[], default="x" * 50)
    assert world.state.requests[rid].default == "x" * 20


def test_respond_after_the_deadline_but_before_the_tick_is_rejected_without_side_effects():
    world = make_world()
    rid = open_req(world, targets=["u1", "u2"], duration_s=10)
    world.clock.advance(11)
    seq = world.seq
    receipt = answer(world, "u1", rid)
    assert receipt.code == Code.REQUEST_CLOSED
    assert world.seq == seq and world.state.requests[rid].answers == {}
    world.tick()
    assert world.state.requests[rid].status == "closed"


def test_next_deadline_is_the_earliest_open_request():
    world = make_world()
    assert world.next_deadline() is None
    open_req(world, targets=["u1"], duration_s=50)
    short = open_req(world, targets=["u2"], duration_s=20)
    assert world.next_deadline() == 20.0
    mod(world, "cancel_request", request_id=short)
    assert world.next_deadline() == 50.0


# --------------------------------------------------------------------------- cancel / reopen


def test_cancel_request_notifies_every_target_and_interrupts():
    world = make_world()
    rid = open_req(world, targets=["u1", "u2"], sealed=True)
    answer(world, "u1", rid, "yes")
    world.drain_outbox()
    world.drain_events()

    receipt = mod(world, "cancel_request", request_id=rid)
    assert receipt.ok
    assert world.state.requests[rid].status == "cancelled"
    entry = entries(world, "request.cancelled")[0]
    assert entry.visible_to_ids == [MODERATOR_ID, "u1", "u2"]
    assert entry.data == {"request_id": rid, "reason": "cancelled"}

    items = deliveries(world)
    assert sorted(d.target for d in items) == ["u1", "u2"]
    for d in items:
        assert (d.envelope.kind, d.envelope.priority, d.envelope.interrupt) == ("request", "ambient", True)
        assert d.envelope.payload == {"event": "request.cancelled", "request_id": rid, "reason": "cancelled"}
    assert events(world, "request.closed")[0].data["reason"] == "cancelled"

    # sealed answers of a cancelled request never become visible
    for agent in [MODERATOR_ID, *PLAYER_IDS]:
        assert [e for e in world.entries_visible(agent) if e.type == "request.answered"] == []
    assert world.next_deadline() is None


def test_cancel_request_errors():
    world = make_world()
    assert mod(world, "cancel_request", request_id="r9").code == Code.UNKNOWN_REQUEST
    rid = open_req(world, targets=["u1"])
    mod(world, "cancel_request", request_id=rid)
    seq = world.seq
    assert mod(world, "cancel_request", request_id=rid).code == Code.REQUEST_CLOSED
    assert world.seq == seq


def test_reopen_clones_a_closed_request_with_a_new_id():
    world = make_world()
    rid = open_req(world, targets=["u1", "u2"], sealed=True, default="no", duration_s=15)
    answer(world, "u1", rid, "yes")
    answer(world, "u2", rid, "yes")
    world.drain_outbox()
    world.drain_events()

    receipt = mod(world, "reopen_request", request_id=rid)
    assert receipt.ok and receipt.data["request_id"] == "r2" and receipt.data["reopens"] == rid
    new = world.state.requests["r2"]
    old = world.state.requests[rid]
    assert new.reopens == rid and new.status == "open" and new.answers == {}
    assert (new.question, new.options, new.sealed, new.default, new.to) == (
        old.question, old.options, old.sealed, old.default, old.to,
    )
    assert new.deadline == world.clock.now() + 15  # same window as the original
    assert old.status == "closed"
    assert entries(world, "request.opened")[-1].data["reopens"] == rid
    assert sorted(d.target for d in deliveries(world) if d.envelope.priority == "urgent") == ["u1", "u2"]
    assert events(world, "request.opened")[0].data["request"]["id"] == "r2"


def test_reopen_can_retarget_and_change_the_duration():
    world = make_world()
    rid = open_req(world, targets=["u1", "u2"])
    mod(world, "cancel_request", request_id=rid)
    receipt = mod(world, "reopen_request", request_id=rid, to=["u3"], duration_s=7)
    assert receipt.ok
    new = world.state.requests[receipt.data["request_id"]]
    assert new.to == ["u3"] and new.deadline == 7.0


def test_reopen_errors():
    world = make_world()
    assert mod(world, "reopen_request", request_id="r9").code == Code.UNKNOWN_REQUEST
    rid = open_req(world, targets=["u1"])
    assert mod(world, "reopen_request", request_id=rid).code == Code.INVALID_TARGET  # still open
    mod(world, "cancel_request", request_id=rid)
    seq = world.seq
    assert mod(world, "reopen_request", request_id=rid, to=["ghost"]).code == Code.UNKNOWN_PLAYER
    assert world.seq == seq and set(world.state.requests) == {rid}


# --------------------------------------------------------------------------- redeliver / persistence


def test_redeliver_wakes_only_targets_who_still_have_to_answer():
    world = make_world()
    rid = open_req(world, targets=["u1", "u2", "u3"], duration_s=40)
    answer(world, "u1", rid)
    done = open_req(world, targets=["u4"])
    answer(world, "u4", done)  # closed
    expired = open_req(world, targets=["u4"], duration_s=1)
    world.clock.advance(5)  # past its deadline, tick has not run
    world.drain_outbox()

    world.redeliver()
    items = deliveries(world)
    assert sorted(d.target for d in items) == ["u2", "u3"]
    for d in items:
        assert d.envelope.priority == "urgent" and d.envelope.payload["request_id"] == rid
        assert d.envelope.payload["seconds"] == 35.0  # 40 - 5 game seconds left
    assert expired not in {d.envelope.payload["request_id"] for d in items}


def test_dump_and_load_preserve_requests_and_counters():
    world = make_world()
    rid = open_req(world, targets=["u1", "u2"], sealed=True)
    answer(world, "u1", rid, "yes")
    data = world.dump()

    from app.world.core import WorldCore
    from app.world.game import GameModule

    clone = WorldCore(
        world.spec,
        ManualClock(data["clock_now"]),
        state=WorldState.model_validate(data["state"]),
        ledger=[LedgerEntry.model_validate(e) for e in data["ledger"]],
    )
    game = GameModule(clone)
    clone.add_module(game)
    game.load(data["modules"]["game"])
    clone.set_running(True, "restored")

    assert clone.state.requests[rid].status == "open"
    assert clone.state.requests[rid].answers["u1"].value == "yes"
    assert ask(clone, "u2", "respond", request_id=rid, value="no").ok
    assert clone.state.requests[rid].status == "closed"
    # the id counter continues instead of restarting
    assert mod(clone, "open_request", to=["u1"], kind="text", question="？").data["request_id"] == "r2"
