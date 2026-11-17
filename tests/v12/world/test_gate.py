"""The commit gate (``WorldCore._submit``): every stage in order, plus the status switch (contracts §2.2)."""

from __future__ import annotations

import pytest

from app.world.types import ENGINE_ID, MODERATOR_ID, Code, Intent

from .helpers import (
    flush,
    make_spec,
    make_world,
    mod,
    new_channel,
    open_queue,
    speak,
    submit,
)

# ----------------------------------------------------------------------- stage 1: type / running / actor


def test_unknown_intent_type_is_invalid_params() -> None:
    world = make_world()
    receipt = submit(world, "N1", "dance", {"channel_id": "dm-N1"})
    assert not receipt.ok and receipt.code == Code.INVALID_PARAMS
    assert "dance" in receipt.message


def test_unknown_type_is_checked_before_the_running_state() -> None:
    world = make_world(running=False)
    assert submit(world, "N1", "dance").code == Code.INVALID_PARAMS


def test_nothing_is_accepted_while_the_game_is_not_running() -> None:
    world = make_world(running=False)
    before = world.seq
    for actor, type_, payload in [
        ("N1", "speak", {"channel_id": "dm-N1", "content": "hi"}),
        ("moderator", "mod.send_message", {"channel_id": "dm-N1", "content": "hi"}),
    ]:
        receipt = submit(world, actor, type_, payload)
        assert not receipt.ok and receipt.code == Code.GAME_NOT_RUNNING
    assert world.seq == before


def test_running_is_checked_before_the_actor() -> None:
    world = make_world(running=False)
    assert submit(world, "ghost", "speak", {"channel_id": "x", "content": "y"}).code == Code.GAME_NOT_RUNNING


@pytest.mark.parametrize("actor", ["ghost", ENGINE_ID, "", "P01"])
def test_unknown_participant(actor: str) -> None:
    world = make_world()
    receipt = submit(world, actor, "speak", {"channel_id": "dm-N1", "content": "hi"})
    assert not receipt.ok and receipt.code == Code.UNKNOWN_PLAYER


def test_unknown_participant_is_checked_before_forbidden() -> None:
    world = make_world()
    assert submit(world, "ghost", "mod.close_floor", {"channel_id": "dm-N1"}).code == Code.UNKNOWN_PLAYER


# ----------------------------------------------------------------------- stage 2: who may do what


@pytest.mark.parametrize(
    "type_,payload",
    [
        ("mod.send_message", {"channel_id": "dm-N1", "content": "hi"}),
        ("mod.announce", {"channel_id": "dm-N1", "template_id": "notice", "params": {"text": "x"}}),
        ("mod.close_floor", {"channel_id": "dm-N1"}),
        ("mod.create_channel", {"channel_id": "zz", "name": "zz"}),
    ],
)
def test_a_player_cannot_use_moderator_intents(type_: str, payload: dict) -> None:
    world = make_world()
    receipt = submit(world, "N1", type_, payload)
    assert not receipt.ok and receipt.code == Code.FORBIDDEN
    assert "主持人" in receipt.message


@pytest.mark.parametrize(
    "type_,payload",
    [
        ("speak", {"channel_id": "dm-N1", "content": "hi"}),
        ("pass", {"channel_id": "dm-N1"}),
        ("speak_template", {"channel_id": "dm-N1", "template_id": "count", "params": {"n": 1}}),
        ("respond", {"request_id": "r1", "value": "a"}),
        ("challenge", {"content": "unfair"}),
    ],
)
def test_the_moderator_cannot_use_player_intents(type_: str, payload: dict) -> None:
    world = make_world()
    receipt = submit(world, MODERATOR_ID, type_, payload)
    assert not receipt.ok and receipt.code == Code.FORBIDDEN
    assert "玩家" in receipt.message


def test_forbidden_is_checked_before_the_payload() -> None:
    world = make_world()
    assert submit(world, "N1", "mod.send_message", {"bogus": 1}).code == Code.FORBIDDEN


# ----------------------------------------------------------------------- stage 3: payload validation


@pytest.mark.parametrize(
    "type_,payload",
    [
        ("speak", {}),  # missing fields
        ("speak", {"channel_id": "dm-N1"}),
        ("speak", {"channel_id": "dm-N1", "content": ""}),  # empty
        ("speak", {"channel_id": "dm-N1", "content": "x" * 20001}),  # absurdly long (not merely truncated)
        ("speak", {"channel_id": "dm-N1", "content": "hi", "extra": True}),  # unknown field
        ("speak", {"channel_id": 7, "content": "hi"}),  # wrong type
        ("pass", {}),
        ("mod.open_floor", {"channel_id": "c1", "mode": "turn", "holders": []}),  # turn needs a holder
        ("mod.open_floor", {"channel_id": "c1", "mode": "queue", "holders": ["N1"]}),
        ("mod.set_floor_queue", {"channel_id": "c1", "order": []}),
        ("mod.create_channel", {"channel_id": "bad id!", "name": "x"}),
        ("mod.create_channel", {"channel_id": "ok", "name": ""}),
        ("mod.update_channel", {"channel_id": "c1", "mode": "turn"}),  # turn is entered via open_floor
        ("mod.update_channel", {"channel_id": "c1", "free_limits": {"duration_s": -1}}),
    ],
)
def test_invalid_payloads_are_invalid_params(type_: str, payload: dict) -> None:
    world = make_world()
    actor = MODERATOR_ID if type_.startswith("mod.") else "N1"
    receipt = submit(world, actor, type_, payload)
    assert not receipt.ok and receipt.code == Code.INVALID_PARAMS, receipt
    assert receipt.message.startswith("参数无效")


def test_a_rejected_submit_never_changes_the_world() -> None:
    world = make_world()
    before = (world.seq, world.state.model_dump())
    flush(world)
    submit(world, "N1", "speak", {"channel_id": "dm-N1", "content": ""})
    submit(world, "ghost", "pass", {"channel_id": "dm-N1"})
    submit(world, "N1", "mod.close_floor", {"channel_id": "dm-N1"})
    assert (world.seq, world.state.model_dump()) == before
    assert world.drain_outbox() == [] and world.drain_events() == []


# ----------------------------------------------------------------------- stage 4: STALE


def _invalidating_world():
    world = make_world(spec=make_spec(invalidates_drafts=["phase.changed"]))
    new_channel(world, "c1", mode="free")
    return world


def test_stale_when_an_invalidating_entry_follows_based_on_seq() -> None:
    world = _invalidating_world()
    seen = world.seq
    world.commit("phase.changed", MODERATOR_ID, {"text": "next"}, visible_to="*")
    receipt = speak(world, "N1", "c1", "我的草稿", based_on=seen)
    assert not receipt.ok and receipt.code == Code.STALE
    assert receipt.data["latest_seq"] == world.seq
    assert not [e for e in world.ledger if e.type == "message"]


def test_not_stale_when_the_draft_saw_the_invalidating_entry() -> None:
    world = _invalidating_world()
    world.commit("phase.changed", MODERATOR_ID, {"text": "next"}, visible_to="*")
    assert speak(world, "N1", "c1", "看过了", based_on=world.seq).ok


def test_ordinary_entries_do_not_make_drafts_stale() -> None:
    world = _invalidating_world()
    seen = world.seq
    assert speak(world, "N2", "c1", "别人先说了").ok
    assert speak(world, "N1", "c1", "我的草稿", based_on=seen).ok


def test_speak_template_is_also_stale_checked() -> None:
    world = _invalidating_world()
    seen = world.seq
    world.commit("phase.changed", MODERATOR_ID, {"text": "next"}, visible_to="*")
    receipt = submit(world, "N1", "speak_template", {"channel_id": "c1", "template_id": "count", "params": {"n": 2}}, based_on=seen)
    assert receipt.code == Code.STALE


def test_only_speech_is_stale_checked() -> None:
    world = _invalidating_world()
    seen = world.seq
    world.commit("phase.changed", MODERATOR_ID, {"text": "next"}, visible_to="*")
    # pass is not stale-checked (it gets its normal answer), neither are moderator intents
    assert submit(world, "N1", "pass", {"channel_id": "c1"}, based_on=seen).code != Code.STALE
    assert mod(world, "send_message", channel_id="c1", content="公告").ok


def test_without_invalidating_types_nothing_is_ever_stale() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")
    world.commit("phase.changed", MODERATOR_ID, {"text": "next"}, visible_to="*")
    assert speak(world, "N1", "c1", "hi", based_on=0).ok


# ----------------------------------------------------------------------- stage 5: CONSTRAINT


def test_constraint_violation_is_rejected_with_its_message() -> None:
    spec = make_spec(
        constraints=[{"applies_to": ["speak"], "expr": "${channel.mode != 'listen'}", "message": "只听频道不能发言"}]
    )
    world = make_world(spec=spec)
    new_channel(world, "c1", mode="listen")
    receipt = speak(world, "N1", "c1", "hi")
    assert not receipt.ok and receipt.code == Code.CONSTRAINT
    assert receipt.message == "只听频道不能发言"


def test_constraint_is_checked_before_the_handler() -> None:
    # the channel is closed (the handler would say CHANNEL_CLOSED) but the constraint speaks first
    spec = make_spec(constraints=[{"applies_to": ["speak"], "expr": "${false}", "message": "no"}])
    world = make_world(spec=spec)
    new_channel(world, "c1", mode="closed")
    assert speak(world, "N1", "c1", "hi").code == Code.CONSTRAINT


def test_constraint_can_look_at_the_actor() -> None:
    spec = make_spec(constraints=[{"applies_to": ["speak"], "expr": "${len(actor.tags) > 0}", "message": "需要标签"}])
    world = make_world(spec=spec)
    new_channel(world, "c1", mode="free")
    assert speak(world, "N1", "c1", "hi").code == Code.CONSTRAINT
    world.player("N1").tags.append("vip")
    assert speak(world, "N1", "c1", "hi").ok
    assert speak(world, "N2", "c1", "hi").code == Code.CONSTRAINT


def test_constraints_only_apply_to_the_listed_intents() -> None:
    spec = make_spec(constraints=[{"applies_to": ["speak_template"], "expr": "${false}"}])
    world = make_world(spec=spec)
    new_channel(world, "c1", mode="free")
    assert speak(world, "N1", "c1", "hi").ok
    receipt = submit(world, "N1", "speak_template", {"channel_id": "c1", "template_id": "count", "params": {"n": 1}})
    assert receipt.code == Code.CONSTRAINT
    assert receipt.message == "违反了剧本约束。"  # default message


def test_a_broken_constraint_expression_is_an_internal_error() -> None:
    spec = make_spec(constraints=[{"applies_to": ["speak"], "expr": "${no_such_name}"}])
    world = make_world(spec=spec)
    new_channel(world, "c1", mode="free")
    receipt = speak(world, "N1", "c1", "hi")
    assert not receipt.ok and receipt.code == Code.INTERNAL_ERROR


# ----------------------------------------------------------------------- stage 6: the handler & its failure modes


def test_a_crashing_handler_becomes_an_internal_error_receipt() -> None:
    world = make_world()

    def boom(intent: Intent, payload: object) -> None:
        raise RuntimeError("kaboom")

    world._handlers["pass"] = boom  # type: ignore[assignment]
    receipt = submit(world, "N1", "pass", {"channel_id": "dm-N1"})  # must not raise
    assert not receipt.ok and receipt.code == Code.INTERNAL_ERROR
    # the world keeps working afterwards
    assert speak(world, "N1", "dm-N1", "still alive").ok


def test_an_intent_nobody_handles_is_an_internal_error() -> None:
    world = make_world()  # no game module in this world
    receipt = mod(world, "hold_game", reason="x")
    assert not receipt.ok and receipt.code == Code.INTERNAL_ERROR


def test_duplicate_handler_registration_is_refused() -> None:
    from app.world.chat import ChatModule

    world = make_world()
    with pytest.raises(RuntimeError):
        world.add_module(ChatModule(world))


def test_every_rejection_code_used_by_receipts_is_known() -> None:
    from app.world.types import Receipt

    with pytest.raises(ValueError):
        Receipt.reject("MADE_UP")
    assert {Code.NO_FLOOR, Code.NOT_MEMBER, Code.CHANNEL_CLOSED, Code.STALE} <= Code.ALL


# ----------------------------------------------------------------------- notifier


def test_the_notifier_fires_only_when_something_was_queued() -> None:
    world = make_world()
    calls: list[int] = []
    world.set_notifier(lambda: calls.append(1))
    assert not speak(world, "N1", "dm-N1", "").ok  # invalid -> nothing queued
    assert calls == []
    assert speak(world, "N1", "dm-N1", "hello moderator").ok
    assert calls == [1]
    world.drain_outbox()
    world.drain_events()
    world.tick()  # nothing due
    assert calls == [1]


def test_a_failing_notifier_never_breaks_the_world() -> None:
    world = make_world()

    def bad() -> None:
        raise RuntimeError("listener bug")

    world.set_notifier(bad)
    assert speak(world, "N1", "dm-N1", "hello").ok


# ----------------------------------------------------------------------- the status switch (WorldPort.set_running)


def test_set_running_writes_status_entries_once_per_change() -> None:
    world = make_world(running=False)
    assert world.state.status == "idle" and not world.running
    world.set_running(True, "start")
    world.set_running(True, "start")  # no change -> no entry
    world.set_running(False, "pause")
    world.set_running(False, "pause")
    world.set_running(True, "resume")
    statuses = [(e.data["status"], e.data["reason"]) for e in world.ledger if e.type == "game.status"]
    assert statuses == [("running", "start"), ("paused", "pause"), ("running", "resume")]
    assert all(e.visible_to_ids == world.everyone() for e in world.ledger if e.type == "game.status")
    events = [e.data["status"] for e in world.drain_events() if e.type == "game.status"]
    assert events == ["running", "paused", "running"]


def test_pausing_a_game_that_never_started_keeps_it_idle() -> None:
    world = make_world(running=False)
    world.set_running(False, "noop")
    assert world.state.status == "idle"
    assert not [e for e in world.ledger if e.type == "game.status"]


def test_continuing_a_finished_game_withdraws_the_verdict() -> None:
    world = make_world()
    world.state.status = "finished"  # what ``mod.end_game`` does
    world.state.winner, world.state.summary = "A队", "好局"
    world.set_running(False, "finished")  # stays finished
    assert world.state.status == "finished" and not world.running
    flush(world)
    world.set_running(True, "whatever")
    assert world.state.status == "running" and world.running
    assert world.state.winner is None and world.state.summary == ""
    last = [e for e in world.ledger if e.type == "game.status"][-1]
    assert last.data == {"status": "running", "reason": "continued"}


def test_tick_is_a_no_op_while_the_game_is_not_running() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["N1", "N2"])
    world.set_running(False, "pause")
    world.clock.advance(1000)
    world.tick()
    assert world.channel("c1").group.tokens[0].status == "active"
    world.set_running(True, "resume")
    world.tick()
    assert world.channel("c1").group.tokens[0].status == "done"


# ----------------------------------------------------------------------- kernel services used by every module


def test_deliver_merges_the_payload_and_mirrors_the_channel_id() -> None:
    world = make_world()
    world.deliver("N1", "request", "request.opened", payload={"kind": "choice", "question": "？"}, channel_id="c9", seq=4)
    world.deliver("N2", "notice", "x", priority="urgent", interrupt=True, extra_key=1)
    first, second = world.drain_outbox()
    assert first.target == "N1" and first.envelope.channel_id == "c9" and first.envelope.seq == 4
    assert first.envelope.payload == {"event": "request.opened", "channel_id": "c9", "kind": "choice", "question": "？"}
    assert (second.envelope.priority, second.envelope.interrupt) == ("urgent", True)
    assert second.envelope.payload == {"event": "x", "extra_key": 1}  # no channel: no channel_id key


def test_commit_audiences() -> None:
    world = make_world()
    new_channel(world, "c1", members=["N2", "N1"])
    assert world.commit("phase.changed", ENGINE_ID, {}, visible_to="*").visible_to_ids == world.everyone()
    assert world.commit("phase.changed", ENGINE_ID, {}, visible_to="N3").visible_to_ids == ["N3"]  # one id, not letters
    assert world.commit("phase.changed", ENGINE_ID, {}, visible_to=["N2", "N1", "N2"]).visible_to_ids == ["N2", "N1"]
    assert world.commit("phase.changed", ENGINE_ID, {}, visible_to=[]).visible_to_ids == []  # god view only
    assert world.commit("phase.changed", ENGINE_ID, {}, channel_id="c1").visible_to_ids == ["moderator", "N2", "N1"]
    with pytest.raises(ValueError):
        world.commit("phase.changed", ENGINE_ID, {})
    seqs = [e.seq for e in world.ledger]
    assert seqs == list(range(1, len(seqs) + 1))  # gap-free


def test_a_module_that_crashes_in_tick_does_not_stop_the_others() -> None:
    from app.world.core import WorldModule

    class Broken(WorldModule):
        name = "broken"

        def tick(self) -> None:
            raise RuntimeError("module bug")

    world = make_world()
    world.add_module(Broken(world))
    new_channel(world, "c1")
    open_queue(world, "c1", ["N1", "N2"])
    world.clock.advance(60)
    world.tick()  # must not raise; the chat module's timeout is still processed
    assert world.channel("c1").group.tokens[0].status == "done"


def _priorities(world) -> dict[str, str]:
    return {d.target: d.envelope.priority for d in world.drain_outbox()}


def test_message_priority_in_system_dms() -> None:
    world = make_world(n=2)
    assert speak(world, "N1", "dm-N1", "hi").ok
    assert _priorities(world) == {"moderator": "normal"}  # a player writing to the moderator
    assert mod(world, "send_message", channel_id="dm-N1", content="hi").ok
    assert _priorities(world) == {"N1": "normal"}  # the moderator answering


def test_message_priority_elsewhere() -> None:
    world = make_world(n=2)
    new_channel(world, "closed1", members=["N1", "N2"], mode="closed")
    new_channel(world, "free1", members=["N1", "N2"], mode="free")
    assert mod(world, "send_message", channel_id="closed1", content="hi").ok
    assert _priorities(world) == {"N1": "ambient", "N2": "ambient"}
    assert speak(world, "N1", "free1", "hi").ok
    assert _priorities(world) == {"moderator": "ambient", "N2": "normal"}
