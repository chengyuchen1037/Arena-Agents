"""``request.closed`` carries ``tally`` / ``leaders`` for choice requests (entry data and the moderator's
envelope payload)."""

from __future__ import annotations

import pytest

from app.world.types import MODERATOR_ID

from .test_game_kit import ask, deliveries, entries, events, make_world, mod, to

THREE = [{"id": "a", "label": "甲案"}, {"id": "b", "label": "乙案"}, {"id": "c", "label": "丙案"}]
FOUR = ["u1", "u2", "u3", "u4"]


def open_vote(world, targets=FOUR, options=THREE, **overrides) -> str:
    payload = {"to": list(targets), "kind": "choice", "question": "选哪个？", "options": options}
    payload.update(overrides)
    receipt = mod(world, "open_request", **payload)
    assert receipt.ok, receipt
    return receipt.data["request_id"]


def vote(world, rid, votes: dict[str, str]) -> None:
    for pid, value in votes.items():
        assert ask(world, pid, "respond", request_id=rid, value=value).ok


def closed_entry(world):
    found = entries(world, "request.closed")
    assert len(found) == 1
    return found[0]


def test_a_clear_winner():
    world = make_world()
    rid = open_vote(world)
    vote(world, rid, {"u1": "a", "u2": "a", "u3": "b", "u4": "a"})
    data = closed_entry(world).data
    assert data["tally"] == {"a": 3, "b": 1, "c": 0}  # every option is listed, zero votes included
    assert data["leaders"] == ["a"]


def test_a_tie_lists_every_leader_in_option_order():
    world = make_world()
    rid = open_vote(world)
    vote(world, rid, {"u1": "b", "u2": "a", "u3": "b", "u4": "a"})
    data = closed_entry(world).data
    assert data["tally"] == {"a": 2, "b": 2, "c": 0} and data["leaders"] == ["a", "b"]


def test_a_three_way_tie():
    world = make_world()
    rid = open_vote(world, targets=["u1", "u2", "u3"])
    vote(world, rid, {"u3": "c", "u1": "a", "u2": "b"})
    data = closed_entry(world).data
    assert data["tally"] == {"a": 1, "b": 1, "c": 1} and data["leaders"] == ["a", "b", "c"]


def test_nobody_voted_gives_all_zero_and_no_leaders():
    world = make_world()
    rid = open_vote(world, duration_s=10)
    world.clock.advance(10)
    world.tick()
    data = closed_entry(world).data
    assert data["reason"] == "deadline" and data["missing"] == FOUR
    assert data["tally"] == {"a": 0, "b": 0, "c": 0}
    assert data["leaders"] == []
    assert world.state.requests[rid].status == "closed"


def test_defaulted_answers_are_counted():
    world = make_world()
    rid = open_vote(world, default="b", duration_s=10)
    vote(world, rid, {"u1": "a"})
    world.clock.advance(10)
    world.tick()
    data = closed_entry(world).data
    assert data["defaulted"] == ["u2", "u3", "u4"]
    assert data["tally"] == {"a": 1, "b": 3, "c": 0} and data["leaders"] == ["b"]


def test_defaults_can_tie_with_real_answers():
    world = make_world()
    open_vote(world, default="b", duration_s=10)
    vote(world, "r1", {"u1": "a", "u2": "a"})
    world.clock.advance(10)
    world.tick()
    data = closed_entry(world).data
    assert data["tally"] == {"a": 2, "b": 2, "c": 0} and data["leaders"] == ["a", "b"]


def test_only_the_last_answer_of_a_player_counts():
    world = make_world()
    rid = open_vote(world, targets=["u1", "u2"])
    vote(world, rid, {"u1": "a"})
    vote(world, rid, {"u1": "c"})  # replaces the first answer while the request is still open
    vote(world, rid, {"u2": "c"})  # the last answer closes the request
    data = closed_entry(world).data
    assert data["tally"] == {"a": 0, "b": 0, "c": 2} and data["leaders"] == ["c"]


def test_the_tally_follows_the_order_of_the_options():
    world = make_world()
    options = [{"id": "z"}, {"id": "m"}, {"id": "a"}]
    rid = open_vote(world, targets=["u1", "u2"], options=options)
    vote(world, rid, {"u1": "a", "u2": "m"})
    data = closed_entry(world).data
    assert list(data["tally"]) == ["z", "m", "a"] and data["leaders"] == ["m", "a"]


def test_the_moderator_envelope_carries_the_same_tally():
    world = make_world()
    rid = open_vote(world, targets=["u1", "u2", "u3"], sealed=True)
    world.drain_outbox()
    vote(world, rid, {"u1": "b", "u2": "b", "u3": "a"})
    entry = closed_entry(world)
    woken = to(deliveries(world), MODERATOR_ID, "request.closed")
    assert len(woken) == 1
    payload = woken[0].envelope.payload
    assert payload["tally"] == entry.data["tally"] == {"a": 1, "b": 2, "c": 0}
    assert payload["leaders"] == entry.data["leaders"] == ["b"]
    assert entry.visible_to_ids == [MODERATOR_ID]  # sealed votes still reach nobody but the moderator


def test_text_requests_have_no_tally():
    world = make_world()
    rid = mod(world, "open_request", to=["u1", "u2"], kind="text", question="说说看").data["request_id"]
    vote(world, rid, {"u1": "想法一", "u2": "想法二"})
    entry = closed_entry(world)
    assert "tally" not in entry.data and "leaders" not in entry.data
    woken = to(deliveries(world), MODERATOR_ID, "request.closed")
    assert "tally" not in woken[0].envelope.payload and "leaders" not in woken[0].envelope.payload


def test_the_ui_event_stays_free_of_vote_counts():
    world = make_world()
    rid = open_vote(world, targets=["u1", "u2"])
    world.drain_events()
    vote(world, rid, {"u1": "a", "u2": "a"})
    ui = events(world, "request.closed")
    assert ui[0].data == {"request_id": rid, "reason": "all_answered", "answered_by": ["u1", "u2"], "defaulted": []}


@pytest.mark.parametrize(
    "answers,reason,leaders",
    [
        ({"u1": "yes"}, "deadline", ["yes"]),
        ({"u1": "no", "u2": "no"}, "all_answered", ["no"]),
        ({}, "deadline", []),
    ],
)
def test_two_option_requests_whichever_way_they_close(answers, reason, leaders):
    world = make_world()
    mod(world, "open_request", to=["u1", "u2"], kind="choice", question="是否？", options=[{"id": "yes"}, {"id": "no"}], duration_s=5)
    vote(world, "r1", answers)
    world.clock.advance(5)
    world.tick()  # a no-op when the request already closed
    data = closed_entry(world).data
    assert data["reason"] == reason and data["leaders"] == leaders
