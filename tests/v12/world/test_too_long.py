"""A player's speech over the limit of the moment is sent back whole - never cut (v0.14, H16, contracts §13.4).

Every kind of cap (a floor token's per-message cap clipped to what is left of its total, a free discussion's, a system DM's)
answers ``TOO_LONG`` with ``chars`` and ``limit``; nothing is written, nothing is booked, nothing is delivered, the floor
(and its clock) is exactly as it was, so the writer simply sends a shorter text.  The moderator's free text, a template, a
challenge and the answer to a text request are not speech of a player: they behave as before.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.world.board import build_board
from app.world.chat import floor_view
from app.world.seat import seat_status
from app.world.types import MODERATOR_ID, Code

from .helpers import (
    do_pass,
    entries,
    flush,
    make_spec,
    make_world,
    mod,
    msgs,
    new_channel,
    open_free,
    open_queue,
    open_turn,
    speak,
    speak_template,
)
from .test_game_integration import make_full_world
from .test_game_integration import speak as full_speak
from .test_game_kit import ask
from .test_game_kit import mod as full_mod

REFUSAL = "这条发言 {n} 个字，超过了单条上限 {limit} 字，没有发出，发言权还在。请删减后重发。"


def _frozen(world: Any) -> str:
    """Everything that is state: the ledger, the floors, the counters, the rng, the clock (``dump`` is a full snapshot)."""
    return json.dumps(world.dump(), sort_keys=True, ensure_ascii=False)


# ---- one setup per kind of cap: ``(channel, actor, limit)`` ----------------------------------------------------------


def _token(world: Any) -> tuple[str, str, int]:
    new_channel(world, "c1")
    open_queue(world, "c1", ["N1", "N2"], max_messages=3, max_chars_per_message=12, max_chars_total=100)
    return "c1", "N1", 12


def _token_turn(world: Any) -> tuple[str, str, int]:
    new_channel(world, "c1")
    open_turn(world, "c1", ["N1", "N2"], max_messages=2, max_chars_per_message=15, max_chars_total=100)
    return "c1", "N2", 15


def _token_remaining_total(world: Any) -> tuple[str, str, int]:
    new_channel(world, "c1")
    open_queue(world, "c1", ["N1"], max_messages=5, max_chars_per_message=20, max_chars_total=25)
    assert speak(world, "N1", "c1", "字" * 10).ok  # 15 of the total are left: that is the limit of the next message
    flush(world)
    return "c1", "N1", 15


def _free_limits(world: Any) -> tuple[str, str, int]:
    new_channel(world, "c1", mode="free")
    open_free(world, "c1", max_chars_per_message=9)
    return "c1", "N3", 9


def _free_default(world: Any) -> tuple[str, str, int]:
    new_channel(world, "c1", mode="free")  # defaults.free.max_chars_per_message = 30
    return "c1", "N1", 30


def _system_dm(world: Any) -> tuple[str, str, int]:
    return "dm-N2", "N2", 25  # defaults.dm.max_chars_per_message = 25


CAPS = [
    pytest.param(_token, id="token-queue"),
    pytest.param(_token_turn, id="token-turn"),
    pytest.param(_token_remaining_total, id="token-remaining-total"),
    pytest.param(_free_limits, id="free-limits"),
    pytest.param(_free_default, id="free-default"),
    pytest.param(_system_dm, id="system-dm"),
]


@pytest.mark.parametrize("setup", CAPS)
def test_every_kind_of_cap_sends_an_overlong_speech_back_and_changes_nothing(setup: Any) -> None:
    world = make_world()
    channel, actor, limit = setup(world)
    flush(world)
    before = _frozen(world)

    receipt = speak(world, actor, channel, "字" * (limit + 1))
    assert (receipt.ok, receipt.code) == (False, Code.TOO_LONG)
    assert receipt.data == {"chars": limit + 1, "limit": limit}
    assert receipt.message == REFUSAL.format(n=limit + 1, limit=limit)
    assert (receipt.seq, receipt.truncated, receipt.floor_left) == (None, None, None)
    assert _frozen(world) == before  # no ledger entry, no counter, no token / quota change, nothing else
    assert flush(world) == ([], [])  # nobody is woken, no spectator event

    # the same player sends the text that fits - exactly the limit - right away: the floor is still his
    fits = speak(world, actor, channel, "字" * limit)
    assert fits.ok and fits.truncated is None
    assert msgs(world, channel)[-1].data["content"] == "字" * limit
    assert "truncated" not in msgs(world, channel)[-1].data


@pytest.mark.parametrize("setup", CAPS)
def test_the_refusal_names_the_number_the_board_and_the_seat_status_show(setup: Any) -> None:
    world = make_world()
    channel, actor, limit = setup(world)
    block = next(c for c in build_board(world, actor)["channels"] if c["id"] == channel)
    assert block["max_chars"] == limit  # what a model reads on its board ...
    assert seat_status(world, actor)["channels"][channel]["max_chars"] == limit  # ... and a person on the seat's panel
    refused = speak(world, actor, channel, "字" * (limit + 3))
    assert refused.code == Code.TOO_LONG and refused.data["limit"] == limit == block["max_chars"]


def test_a_refusal_never_cuts_and_never_runs_out_but_the_clock_keeps_running() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["N1", "N2"], max_chars_per_message=20, duration_s=60)
    for _ in range(10):  # no limit on the number of refusals, none of them cuts anything
        refused = speak(world, "N1", "c1", "字" * 21)
        assert refused.code == Code.TOO_LONG and refused.truncated is None
    token = world.channel("c1").group.tokens[0]
    assert (token.status, token.messages_used, token.chars_used) == ("active", 0, 0)
    assert not msgs(world, "c1")

    world.clock.advance(61)  # ... and while the writer rewrites, the time of the floor runs as always
    world.tick()
    ended = entries(world, "floor.token_ended", channel="c1")
    assert [(e.data["holder"], e.data["reason"]) for e in ended] == [("N1", "timeout")]
    assert speak(world, "N1", "c1", "字" * 5).code == Code.NO_FLOOR  # too late: the floor went on to N2
    assert speak(world, "N2", "c1", "字" * 5).ok


def test_a_person_s_untimed_seat_meets_the_same_refusal_and_keeps_its_floor() -> None:
    """Humans and bots are the same to the gate: the seat a person plays holds a public floor with no deadline, and a
    text over the cap is sent back with the very same code and numbers."""
    world = make_world()
    new_channel(world, "pub", type_="public")
    world.set_untimed_holders(["N1"])
    open_queue(world, "pub", ["N1"], max_messages=2, max_chars_per_message=10, duration_s=30)
    token = world.channel("pub").group.tokens[0]
    assert token.deadline is None  # untimed
    refused = speak(world, "N1", "pub", "字" * 11)
    assert refused.code == Code.TOO_LONG and refused.data == {"chars": 11, "limit": 10}
    assert (token.status, token.messages_used, token.deadline) == ("active", 0, None)
    assert speak(world, "N1", "pub", "字" * 10).ok


# ---- the order of the checks -------------------------------------------------------------------------------------------


def test_the_rules_of_the_floor_come_before_the_length_check() -> None:
    world = make_world(spec=make_spec(invalidates_drafts=["channel.updated"]))
    new_channel(world, "c1", members=["N1", "N2"])
    open_queue(world, "c1", ["N1"], max_chars_per_message=5, max_messages=1)
    long = "字" * 200
    assert speak(world, "N2", "c1", long).code == Code.NO_FLOOR  # no token
    assert speak(world, "N3", "c1", long).code == Code.NOT_MEMBER
    assert speak(world, "N1", "nowhere", long).code == Code.UNKNOWN_CHANNEL
    assert speak(world, "N1", "c1", " \n ").code == Code.INVALID_PARAMS  # blank is "empty", not "too long"
    stale_from = world.seq
    assert mod(world, "update_channel", channel_id="c1", name="新名字").ok  # an entry type the scenario lets drafts expire on
    assert speak(world, "N1", "c1", long, based_on=stale_from).code == Code.STALE  # a draft over a change is stale first
    assert speak(world, "N1", "c1", long).code == Code.TOO_LONG

    new_channel(world, "closed", mode="closed")
    assert speak(world, "N1", "closed", long).code == Code.CHANNEL_CLOSED
    new_channel(world, "chat", mode="free")
    assert mod(world, "update_channel", channel_id="chat", limits={"max_messages_per_member": 1}).ok
    assert speak(world, "N1", "chat", "好").ok
    assert speak(world, "N1", "chat", long).code == Code.QUOTA_EXCEEDED  # the quota is spent: no point in counting letters
    assert world.chat.freeze_channel("chat", "ability:x")
    assert speak(world, "N2", "chat", long).code == Code.NO_FLOOR  # frozen


def test_the_limit_counts_the_characters_of_the_trimmed_text() -> None:
    world = make_world()
    new_channel(world, "c1", mode="free")
    assert mod(world, "update_channel", channel_id="c1", limits={"max_chars_per_message": 5}).ok
    assert speak(world, "N1", "c1", "一二\n三四").ok  # 5 characters: a line break counts like any other character
    refused = speak(world, "N1", "c1", "  一二 三四五  ")  # blanks around do not count, the one inside does: 6
    assert refused.code == Code.TOO_LONG and refused.data == {"chars": 6, "limit": 5}
    assert speak(world, "N1", "c1", "😀😀😀😀😀").ok  # a character is a character (a code point), not a byte


def test_the_tool_result_a_model_reads_is_compact_json_with_the_numbers() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_queue(world, "c1", ["N1"], max_chars_per_message=8)
    body = json.loads(speak(world, "N1", "c1", "字" * 12).tool_json())
    assert body == {
        "ok": False,
        "code": "TOO_LONG",
        "message": REFUSAL.format(n=12, limit=8),
        "data": {"chars": 12, "limit": 8},
    }


# ---- what is not a player's speech is unchanged ----------------------------------------------------------------------


def test_the_moderator_a_template_a_challenge_and_a_text_answer_behave_as_before() -> None:
    chat = make_world()
    new_channel(chat, "c1")
    open_queue(chat, "c1", ["N1"], max_chars_per_message=20, max_chars_total=50)
    # the moderator's free text is still cut (to ``defaults.moderator``), with ``truncated`` in the receipt
    cut = mod(chat, "send_message", channel_id="c1", content="戊" * 55)
    assert cut.ok and cut.truncated == {"dropped_chars": 15, "kept_chars": 40}
    # a template is longer than the per-message cap and goes out intact (it still costs a message and its characters)
    template = speak_template(chat, "N1", "c1", "long")
    assert template.ok and template.truncated is None
    assert len(msgs(chat, "c1")[-1].data["content"]) > 20

    game = make_full_world()  # kit scenario: ``dm`` 50 characters, the moderator 100, a text answer 20
    challenge = ask(game, "u1", "challenge", content="问" * 80)
    assert challenge.ok and challenge.truncated == {"dropped_chars": 30, "kept_chars": 50}
    rid = full_mod(game, "open_request", to=["u2"], kind="text", question="说说看").data["request_id"]
    answer = ask(game, "u2", "respond", request_id=rid, value="答" * 30)
    assert answer.ok and answer.truncated == {"dropped_chars": 10, "kept_chars": 20}
    reply = full_mod(game, "resolve_challenge", ticket_id="t1", verdict="upheld", reply="复" * 150)
    assert reply.ok and reply.truncated == {"dropped_chars": 50, "kept_chars": 100}


def test_a_zero_limit_is_still_no_floor_not_too_long() -> None:
    world = make_world()
    new_channel(world, "c1")
    open_turn(world, "c1", ["N1"])
    token = world.channel("c1").group.tokens[0]
    token.chars_used = token.max_chars_total
    assert speak(world, "N1", "c1", "字").code == Code.NO_FLOOR
    assert do_pass(world, "N1", "c1").ok


# ---- the board of a free discussion shows the cap of one message (H17) -------------------------------------------------


def test_the_board_and_the_floor_view_of_a_free_discussion_carry_the_cap_of_one_message() -> None:
    world = make_full_world()
    assert full_mod(world, "open_floor", channel_id="public", mode="free", limits={"max_chars_per_message": 77}).ok

    def free_block(agent: str) -> dict[str, Any]:
        channel = next(c for c in build_board(world, agent)["channels"] if c["id"] == "public")
        return channel["floor"]["free"]

    for agent in ("u1", "u2", MODERATOR_ID):
        assert free_block(agent)["max_chars_per_message"] == 77, agent
    view = floor_view(world, world.channel("public"))["free"]  # (the UI's FloorView of the same discussion)
    assert view["max_chars_per_message"] == 77
    assert {k: v for k, v in free_block("u1").items() if k in view} == {
        k: v for k, v in view.items() if k in free_block("u1") and k != "message_counts"
    }

    # a player's board names the same number as the limit of the channel; the moderator is not capped
    block = next(c for c in build_board(world, "u1")["channels"] if c["id"] == "public")
    assert block["max_chars"] == block["floor"]["free"]["max_chars_per_message"] == 77
    mod_block = next(c for c in build_board(world, MODERATOR_ID)["channels"] if c["id"] == "public")
    assert mod_block["max_chars"] is None and mod_block["floor"]["free"]["max_chars_per_message"] == 77

    # it follows the moderator's change of the limits
    assert full_mod(world, "update_channel", channel_id="public", limits={"max_chars_per_message": 55}).ok
    assert free_block("u2")["max_chars_per_message"] == 55
    assert floor_view(world, world.channel("public"))["free"]["max_chars_per_message"] == 55
    refused = full_speak(world, "u1", "public", "字" * 56)
    assert refused.code == Code.TOO_LONG and refused.data == {"chars": 56, "limit": 55}

    # a system DM has no free-discussion block (its cap is ``max_chars`` of the channel), a token floor keeps its own numbers
    dm = next(c for c in build_board(world, "u1")["channels"] if c["id"] == "dm-u1")
    assert dm["floor"]["free"] is None and dm["max_chars"] == world.spec.defaults.dm.max_chars_per_message
    assert full_mod(world, "set_floor_queue", channel_id="public", order=["u1"], limits={"max_chars_per_message": 33}).ok
    queue = next(c for c in build_board(world, "u1")["channels"] if c["id"] == "public")
    assert queue["floor"]["free"] is None and queue["floor"]["mine"]["chars_per_message"] == 33 and queue["max_chars"] == 33
