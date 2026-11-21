"""The hard-fact desk of the state board (``world.board(agent)["facts"]``, v0.14, H10, contracts §13.4).

The scenario marks templates with ``record: fact | claim``; the board lists, oldest first (at most the newest 300), the
messages of the ledger the agent could read that were made with such a template - and every public declaration of an
ability, which is always a fact.  A row is ``{"seq", "phase", "kind", "actor_id", "text", "template_id"}``.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from app.world.board import MAX_FACTS, build_board
from app.world.types import ENGINE_ID, MODERATOR_ID
from app.world.world import World

from .helpers import ManualClock, flush
from .test_game_kit import PLAYER_IDS, PLAYER_NAMES, ask, make_spec, mod

TEMPLATES = {
    "notice": {"text": "【公告】{{text}}", "params": {"text": "text(40)"}, "users": "moderator", "record": "fact"},
    "result": {"text": "结果：{{target}}", "params": {"target": "player_id"}, "users": "moderator", "record": "fact"},
    "plain": {"text": "没有标记的公告", "users": "moderator"},
    "say": {"text": "我报：{{target}}", "params": {"target": "player_id"}, "users": "players", "record": "claim"},
    "word": {"text": "{{word}}", "params": {"word": "text(20)"}, "users": "any", "record": "claim"},
}


def new_world(*, running: bool = True) -> World:
    players = [{"id": pid, "seat": str(i), "name": PLAYER_NAMES[pid]} for i, pid in enumerate(PLAYER_IDS, start=1)]
    world = World.new_game(make_spec(templates=TEMPLATES), players, ManualClock(), seed=3)
    if running:
        world.set_running(True, "start")
    assert mod(world, "create_channel", channel_id="public", name="公共", type="public", mode="free", members=PLAYER_IDS).ok
    flush(world)
    return world


def notice(world: World, text: str = "x", channel: str = "public") -> int:
    receipt = mod(world, "announce", channel_id=channel, template_id="notice", params={"text": text})
    assert receipt.ok, receipt
    return int(receipt.seq or 0)


def facts(world: World, agent: str) -> list[dict[str, Any]]:
    return world.board(agent)["facts"]


def content(world: World, seq: int) -> str:
    return str(world.ledger[seq - 1].data["content"])


def recorded_seqs(world: World, agent: str) -> list[int]:
    """The same list worked out from first principles: what the agent can read (``entries_visible``) and the scenario marks."""
    marked = {tid for tid, t in world.spec.templates.items() if t.record}
    return [
        e.seq
        for e in world.entries_visible(agent)
        if e.type == "message" and (e.data.get("kind") == "ability" or e.data.get("template_id") in marked)
    ]


# --------------------------------------------------------------------------- shape, kinds, order


def test_a_world_without_recorded_messages_has_an_empty_desk_for_everyone() -> None:
    world = new_world()
    assert mod(world, "send_message", channel_id="public", content="只是一句话").ok
    for agent in (MODERATOR_ID, *PLAYER_IDS):
        board = world.board(agent)
        assert board["facts"] == []
        json.dumps(board, ensure_ascii=False)


def test_a_fact_and_a_claim_have_the_documented_shape_and_the_desk_is_in_seq_order() -> None:
    world = new_world()
    first = notice(world, "开始")
    assert mod(world, "set_phase", text="阶段一").ok
    result = mod(world, "announce", channel_id="public", template_id="result", params={"target": "u2"})
    claim = ask(world, "u3", "speak_template", channel_id="public", template_id="say", params={"target": "u2"})
    assert result.ok and claim.ok

    expected = [
        {"seq": first, "phase": "", "kind": "fact", "actor_id": None, "text": content(world, first), "template_id": "notice"},
        {
            "seq": result.seq, "phase": "阶段一", "kind": "fact", "actor_id": None,
            "text": content(world, result.seq), "template_id": "result",
        },
        {
            "seq": claim.seq, "phase": "阶段一", "kind": "claim", "actor_id": "u3",
            "text": content(world, claim.seq), "template_id": "say",
        },
    ]
    for agent in (MODERATOR_ID, *PLAYER_IDS):
        assert facts(world, agent) == expected, agent
    assert expected[0]["text"] == "【公告】开始" and "u2" in expected[1]["text"] and "u2" in expected[2]["text"]
    assert [f["seq"] for f in expected] == sorted(f["seq"] for f in expected)


def test_who_said_it_is_a_player_id_for_a_player_and_none_for_the_moderator_and_the_engine() -> None:
    world = new_world()
    ask(world, "u1", "speak_template", channel_id="public", template_id="word", params={"word": "我说的"})
    assert mod(world, "announce", channel_id="public", template_id="word", params={"word": "主持人用同一个模板"}).ok
    rows = facts(world, "u2")
    assert [(r["kind"], r["actor_id"], r["template_id"]) for r in rows] == [("claim", "u1", "word"), ("claim", None, "word")]
    # (the mark belongs to the template, not to the one who uses it: a template for both is a claim whoever posts it)
    assert ENGINE_ID not in {r["actor_id"] for r in rows} and MODERATOR_ID not in {r["actor_id"] for r in rows}


def test_unmarked_templates_free_speech_and_the_moderators_own_words_are_not_on_the_desk() -> None:
    world = new_world()
    assert mod(world, "announce", channel_id="public", template_id="plain", params={}).ok
    assert mod(world, "send_message", channel_id="public", content="主持人的话").ok
    assert ask(world, "u1", "speak", channel_id="public", content="玩家的话").ok
    assert ask(world, "u2", "challenge", content="质询").ok
    mark = notice(world, "唯一的一条")
    assert [f["seq"] for f in facts(world, "u4")] == [mark]
    # a message whose template the scenario does not know (any more) is not a record either
    world.post_message("public", MODERATOR_ID, "不认识的模板", kind="announce", template_id="ghost", params={})
    assert [f["seq"] for f in facts(world, "u4")] == [mark]


def test_the_phase_of_a_fact_is_the_phase_text_of_the_world_when_it_was_committed() -> None:
    world = new_world()
    seqs = [notice(world, "零")]
    for text in ("甲阶段", "乙阶段", "乙阶段", "丙阶段"):
        assert mod(world, "set_phase", text=text).ok
        seqs.append(notice(world, text))
    assert [f["phase"] for f in facts(world, "u1")] == ["", "甲阶段", "乙阶段", "乙阶段", "丙阶段"]
    assert [f["seq"] for f in facts(world, "u1")] == seqs
    assert mod(world, "set_phase", text="丁阶段").ok  # a later change never rewrites the past
    assert [f["phase"] for f in facts(world, "u1")][-1] == "丙阶段"


# --------------------------------------------------------------------------- who sees what


def test_a_fact_in_a_private_channel_is_on_the_board_of_its_members_only() -> None:
    world = new_world()
    assert mod(world, "create_channel", channel_id="secret", name="密室", members=["u1", "u2"], mode="closed").ok
    public = notice(world, "公开")
    secret = notice(world, "密室里的事实", channel="secret")
    assert [f["seq"] for f in facts(world, "u1")] == [f["seq"] for f in facts(world, "u2")] == [public, secret]
    assert [f["seq"] for f in facts(world, "u3")] == [f["seq"] for f in facts(world, "u4")] == [public]
    assert [f["seq"] for f in facts(world, MODERATOR_ID)] == [public, secret]

    # whoever joins later does not learn what was announced before (the audience of an entry is fixed when it is written)
    assert mod(world, "update_channel", channel_id="secret", add_members=["u3"]).ok
    later = notice(world, "加入之后", channel="secret")
    assert [f["seq"] for f in facts(world, "u3")] == [public, later]


def test_the_desk_is_exactly_what_the_agent_could_read_in_the_ledger() -> None:
    world = new_world()
    assert mod(world, "create_channel", channel_id="secret", name="密室", members=["u1", "u2"], mode="closed").ok
    assert mod(world, "create_channel", channel_id="pair", name="两人组", members=["u3", "u4"], mode="free").ok
    notice(world, "一")
    notice(world, "二", channel="secret")
    ask(world, "u3", "speak_template", channel_id="pair", template_id="word", params={"word": "组内的话"})
    ask(world, "u1", "speak_template", channel_id="public", template_id="say", params={"target": "u4"})
    assert mod(world, "announce", channel_id="pair", template_id="result", params={"target": "u1"}).ok
    mod(world, "update_player", player_id="u2", add_tags=["out"])  # (a player who is out still reads the ledger)
    for agent in (MODERATOR_ID, *PLAYER_IDS):
        assert [f["seq"] for f in facts(world, agent)] == recorded_seqs(world, agent), agent
    assert len({tuple(recorded_seqs(world, a)) for a in (MODERATOR_ID, *PLAYER_IDS)}) >= 3  # the views really differ


# --------------------------------------------------------------------------- abilities


def _holder(world: World) -> str:
    return next(p.id for p in world.state.players if p.role == "alpha")


def test_a_public_declaration_of_an_ability_is_always_a_fact_without_a_template() -> None:
    world = new_world()
    holder = _holder(world)
    target = next(pid for pid in PLAYER_IDS if pid != holder)
    assert mod(world, "set_phase", text="宣言阶段").ok
    assert mod(world, "enable_ability", ability_id="strike").ok
    receipt = ask(world, holder, "use_ability", ability_id="strike", params={"target": target})
    assert receipt.ok
    declaration = next(e for e in world.ledger if e.type == "message" and e.data["kind"] == "ability")
    for agent in (MODERATOR_ID, *PLAYER_IDS):
        assert facts(world, agent) == [
            {
                "seq": declaration.seq, "phase": "宣言阶段", "kind": "fact", "actor_id": holder,
                "text": declaration.data["content"], "template_id": None,
            }
        ], agent
    # an ability without a public declaration leaves nothing behind
    assert mod(world, "enable_ability", ability_id="ping").ok
    before = [f["seq"] for f in facts(world, "u1")]
    assert ask(world, "u1", "use_ability", ability_id="ping").ok
    assert [f["seq"] for f in facts(world, "u1")] == before


# --------------------------------------------------------------------------- the newest 300


def test_the_desk_keeps_the_newest_300_in_ascending_order() -> None:
    world = new_world()
    assert MAX_FACTS == 300
    seqs: list[int] = []
    for i in range(MAX_FACTS + 5):
        seqs.append(notice(world, f"第{i}条"))
        if i % 7 == 0:
            world.post_message("public", "u1", f"闲聊{i}", kind="speech")  # unrecorded noise in between (no quota here)
        if i % 50 == 0:
            flush(world)
    for agent in (MODERATOR_ID, "u1", "u4"):
        rows = facts(world, agent)
        assert len(rows) == MAX_FACTS
        assert [r["seq"] for r in rows] == seqs[5:]  # the five oldest dropped, the rest in order
        assert rows[0]["text"] == "【公告】第5条" and rows[-1]["text"] == f"【公告】第{MAX_FACTS + 4}条"
    # fewer than 300 are all kept
    small = new_world()
    kept = [notice(small, str(i)) for i in range(MAX_FACTS - 1)]
    assert [r["seq"] for r in facts(small, "u2")] == kept


def test_the_cap_counts_the_agents_own_view_not_the_whole_ledger() -> None:
    world = new_world()
    assert mod(world, "create_channel", channel_id="secret", name="密室", members=["u1"], mode="closed").ok
    for i in range(MAX_FACTS + 10):
        notice(world, f"密室{i}", channel="secret")  # only u1 and the moderator read these
    seen_by_everybody = [notice(world, "公开")]
    assert [r["seq"] for r in facts(world, "u2")] == seen_by_everybody  # u2 has one fact, not the last 300 of the ledger
    assert len(facts(world, "u1")) == MAX_FACTS and facts(world, "u1")[-1]["seq"] == seen_by_everybody[0]


# --------------------------------------------------------------------------- persistence and cost


def test_the_desk_is_the_same_after_a_save_and_a_load() -> None:
    world = new_world()
    holder = _holder(world)
    notice(world, "存档前")
    assert mod(world, "set_phase", text="阶段").ok
    ask(world, "u2", "speak_template", channel_id="public", template_id="say", params={"target": "u1"})
    assert mod(world, "enable_ability", ability_id="strike").ok
    ask(world, holder, "use_ability", ability_id="strike", params={"target": next(p for p in PLAYER_IDS if p != holder)})
    data = json.loads(json.dumps(world.dump()))
    restored = World.load(data, make_spec(templates=TEMPLATES), ManualClock(data["clock_now"]))
    for agent in (MODERATOR_ID, *PLAYER_IDS):
        assert facts(restored, agent) == facts(world, agent) and len(facts(world, agent)) == 3, agent


def test_building_the_board_of_a_long_game_stays_cheap() -> None:
    world = new_world()
    for i in range(1500):
        notice(world, f"事实{i % 10}")
        world.post_message("public", "u1", f"闲聊{i}", kind="speech")
        world.post_message("public", "u2", f"再聊{i}", kind="speech")
        if i % 100 == 0:
            flush(world)
    assert len(world.ledger) > 4000
    started = time.perf_counter()
    for agent in (MODERATOR_ID, *PLAYER_IDS):
        for _ in range(5):
            build_board(world, agent)
    per_board = (time.perf_counter() - started) / 25
    assert per_board < 0.25, f"one board of a {len(world.ledger)}-entry ledger took {per_board:.3f}s"  # (typically a few ms)
    assert [len(facts(world, a)) for a in (MODERATOR_ID, "u3")] == [MAX_FACTS, MAX_FACTS]


@pytest.mark.parametrize("agent", [MODERATOR_ID, "u1"])
def test_the_desk_is_plain_json_and_a_copy(agent: str) -> None:
    world = new_world()
    notice(world, "副本")
    rows = facts(world, agent)
    json.dumps(rows, ensure_ascii=False)
    rows[0]["text"] = "被改了"
    rows.clear()
    assert facts(world, agent)[0]["text"] == "【公告】副本"
