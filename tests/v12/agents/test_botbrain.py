"""``BotBrain`` (v0.14 H12-H18, H53; contract §13.5): the tools, the messages, the hints, the output gate, ``jot`` / ``read_contract``,
and - above all - that nothing here can speak for a bot."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.agents.bot_tools as bot_tools_module
import app.agents.composer as composer_module
import app.agents.contract as contract_module
import app.agents.gates as gates_module
import app.agents.mind as mind_module
from app.agents.brain import NUDGE_TEXT, ArenaBrain
from app.agents.composer import sha12
from app.agents.legacy import LegacyBrain
from app.agents.mind import BotBrain
from app.bots.types import JOT_CHARS, JOT_MAX, JOT_NOTE_TITLE, BotMind, CoreFiles, PlaybookEntry, RulesMemory
from app.world.intents import Speak
from app.world.types import MODERATOR_ID, Code, Receipt

from .bot_fakes import (
    ACTION,
    ASK,
    CLOSED,
    FACTS,
    FLOOR_ONE,
    FLOOR_THREE,
    FREE,
    PLAYBOOK,
    VOTE,
    contract,
    floor_granted,
    make_mind,
    make_scenario,
    message_new,
    put_board,
    request_opened,
    wake,
)
from .fakes import FakeWorld, make_spec

MIDDLE, LOW_TAGGED, HIGH, MIDDLE_ACT, LOW_IDENTITY = PLAYBOOK
ME = "p1"


def make_brain(world: FakeWorld | None = None, *, mind: BotMind | None = None, scenario: object = None, **kw: object) -> tuple[FakeWorld, BotBrain]:
    world = world or FakeWorld()
    brain = BotBrain(world, scenario or make_scenario(), ME, mind or make_mind(), contract(), **kw)  # type: ignore[arg-type]
    return world, brain


def call(brain: BotBrain, name: str, **args: object):
    return brain.execute_tool(ME, name, dict(args), gen_id=2, seen_seq=7)


def result(outcome) -> dict:
    return json.loads(outcome.text)


def names(tools: list[dict]) -> list[str]:
    return [tool["function"]["name"] for tool in tools]


# ------------------------------------------------------------------------------------------------ one bot, one seat


def test_a_bot_brain_serves_its_own_seat_and_no_other() -> None:
    world, brain = make_brain()
    assert brain.system_prompt(ME) == brain.composer.system_prompt() and brain.tool_schemas(ME) is brain.tool_schemas(ME)
    asks = [
        lambda: brain.system_prompt("p2"),
        lambda: brain.system_prompt(MODERATOR_ID),
        lambda: brain.tool_schemas("p2"),
        lambda: brain.begin_run("p2"),
        lambda: brain.reset_conversation("p2"),
        lambda: brain.start_message("p2", wake("p2")),
        lambda: brain.inject_message("p2", wake("p2", kind="inject")),
        lambda: brain.no_tool_nudge("p2", "x", 0),
        lambda: brain.execute_tool("p2", "sleep", {}, gen_id=1, seen_seq=0),
        lambda: brain.mount_manifest("p2"),
        lambda: brain.hint_manifest("p2"),
    ]
    for ask in asks:
        with pytest.raises(ValueError, match="p1"):
            ask()
    assert world.submitted == []


def test_a_brain_made_for_a_world_that_cannot_say_who_is_seated_still_works() -> None:
    class Opaque(FakeWorld):
        def board(self, agent_id: str) -> dict:
            if getattr(self, "closed", False):
                raise KeyError(agent_id)
            return super().board(agent_id)

    world = Opaque()
    world.closed = True  # type: ignore[attr-defined]
    brain = BotBrain(world, make_scenario(), ME, make_mind(people={"Zed": "不在座"}), contract())
    assert "Zed：不在座。" in brain.system_prompt(ME)  # (without a list of the seated the snapshot is taken as it is)


def test_the_impressions_of_those_who_are_not_in_this_game_are_left_out() -> None:
    world = FakeWorld()  # the seats: p1 Alex (me), p2 Blair
    mind = make_mind(people={"Blair": "话多", "Casey": "话少"})
    _, brain = make_brain(world, mind=mind)
    prompt = brain.system_prompt(ME)
    assert "Blair：话多。" in prompt and "Casey" not in prompt  # (Casey is not at this table)


# ------------------------------------------------------------------------------------------------ the tools (H53)


def test_a_bot_has_the_world_tools_notes_jot_and_read_contract_and_no_reference_documents() -> None:
    _, brain = make_brain()
    tools = names(brain.tool_schemas(ME))
    assert tools == ["speak", "speak_template", "pass", "respond", "use_ability", "challenge_moderator", "sleep",
                     "write_note", "read_notes", "compact_context", "jot", "read_contract"]
    assert "list_documents" not in tools and "read_document" not in tools
    world = FakeWorld(make_spec(templates={}, abilities={}))
    bare = BotBrain(world, world.spec, ME, make_mind(), contract())
    assert "speak_template" not in names(bare.tool_schemas(ME)) and "use_ability" not in names(bare.tool_schemas(ME))
    # the moderator's tools did not change (it still reads the reference documents), and neither did the v0.13.1 player's
    moderator = ArenaBrain(world, world.spec, docs_dir=None).tool_schemas(MODERATOR_ID)
    assert {"list_documents", "read_document", "announce", "open_floor"} <= set(names(moderator))
    assert "list_documents" in names(LegacyBrain(world, world.spec, docs_dir=None).tool_schemas(ME))


def test_the_tool_schemas_are_provider_friendly_and_the_contract_names_every_one() -> None:
    from app.agents.contract import mentions

    _, brain = make_brain()
    tools = brain.tool_schemas(ME)
    text = json.dumps(tools, ensure_ascii=False)
    for forbidden in ("$defs", "$ref", "anyOf"):
        assert forbidden not in text
    for tool in tools:
        function = tool["function"]
        assert function["name"] and function["description"] and function["parameters"]["type"] == "object"
        assert mentions(contract().markdown, function["name"]), function["name"]  # (the default contract mentions every tool)


def test_speak_has_no_max_length_in_its_schema_but_the_model_still_checks_it() -> None:
    _, brain = make_brain()
    speak = next(t["function"] for t in brain.tool_schemas(ME) if t["function"]["name"] == "speak")
    assert "maxLength" not in json.dumps(speak)
    assert speak["parameters"]["required"] == ["channel_id", "content"] and speak["parameters"]["properties"]["content"]["minLength"] == 1
    assert Speak.model_json_schema()["properties"]["content"]["maxLength"] == 20000  # (the validation of the intent is untouched)
    for old in ("截掉", "把关键结论放在前面", "超出部分"):
        assert old not in speak["description"]  # the old advice that taught report-shaped speech, and a cut that no longer happens
    assert "TOO_LONG" in speak["description"] and "FORMAT" in speak["description"] and "发言权还在" in speak["description"]
    assert "maxLength" in json.dumps(LegacyBrain(FakeWorld(), make_spec(), docs_dir=None).tool_schemas(ME))  # (v0.13.1 as it was)


def test_the_new_tools_say_what_they_are_for() -> None:
    _, brain = make_brain()
    tools = {t["function"]["name"]: t["function"] for t in brain.tool_schemas(ME)}
    assert f"最多 {JOT_CHARS} 字" in tools["jot"]["description"] and f"每局最多 {JOT_MAX} 条" in tools["jot"]["description"]
    assert tools["jot"]["parameters"]["required"] == ["text"] and tools["read_contract"]["parameters"]["properties"]["part"]["type"] == "string"
    assert "required" not in tools["read_contract"]["parameters"]
    assert "回执码" in tools["read_contract"]["description"]
    assert "你的思考不会被保留" not in tools["write_note"]["description"] and "压缩" in tools["write_note"]["description"]


# ------------------------------------------------------------------------------------------------ the opening message


def test_the_opening_message_has_the_board_the_facts_the_memory_the_history_and_the_wake_reason() -> None:
    world, brain = make_brain()
    world.add("message", "p2", {"content": "大家好", "kind": "speech", "sender_name": "Blair"}, channel="pub")
    world.add("message", ME, {"content": "我先说", "kind": "speech", "sender_name": "Alex"}, channel="pub")
    put_board(world, channels=[FLOOR_ONE], facts=FACTS)
    text, seen = brain.start_message(ME, wake(ME, floor_granted()))
    assert seen == world.seq == 2
    marks = ["【状态板】", "【硬事实台】", "【我的记忆】", "【我最近的行动】", "【可见历史】", "【本次唤醒】", "【此刻的提醒】"]
    positions = [text.index(mark) for mark in marks]
    assert positions == sorted(positions)
    assert "#1 [公共频道[pub]] p2 Blair 说：大家好" in text and "#2 [公共频道[pub]] p1 Alex（你）说：我先说" in text
    assert "你是 p1 Alex（座位 S1）。" in text and "这次只能发 1 条，≤ 300 字" in text and "总共" not in text
    assert "〔第 1 夜〕\n#3 事实：今晚没有人出局。" in text and "#9 声明 p2 Blair：我投 p1。理由：他太安静" in text


def test_a_world_without_facts_has_no_such_section() -> None:
    world, brain = make_brain()
    text, _ = brain.start_message(ME, wake(ME))
    assert "【硬事实台】" not in text and "【状态板】" in text
    put_board(world, facts=None)
    assert "【硬事实台】" not in brain.start_message(ME, wake(ME))[0]
    put_board(world, facts=[{"nonsense": 1}, "x"])
    assert "【硬事实台】" not in brain.start_message(ME, wake(ME))[0]


def test_the_opening_message_after_a_compaction_has_the_summary_and_the_notes() -> None:
    world, brain = make_brain()
    for i in range(10):
        world.add("message", "p2", {"content": f"消息{i}", "kind": "speech"}, channel="pub")
    call(brain, "write_note", title="要点", content="第一条")
    done = call(brain, "compact_context", summary_markdown="旧事摘要", keep_recent_count=4)
    assert done.rebuild and done.shared
    brain.reset_conversation(ME)
    text, _ = brain.start_message(ME, wake(ME))
    assert "旧事摘要" in text and "第一条" in text and "消息9" in text and "消息0" not in text


def test_a_long_history_says_so_and_points_at_the_notes() -> None:
    world, brain = make_brain(history_chars=800)
    for i in range(60):
        world.add("message", "p2", {"content": f"第{i:02d}条发言" + "字" * 40, "kind": "speech"}, channel="pub")
    text, _ = brain.start_message(ME, wake(ME))
    assert "更早的" in text and "第59条发言" in text and "第00条发言" not in text and "compact_context" in text
    assert make_brain(history_chars={"player": 900})[1].history_chars == 900 and make_brain()[1].history_chars == 12_000


def test_the_recent_actions_show_what_the_bot_did_and_how_it_went() -> None:
    world, brain = make_brain()
    world.next_receipt = Receipt.reject(Code.NO_FLOOR, "无发言权")
    call(brain, "speak", channel_id="pub", content="被拒的话")
    world.next_receipt = Receipt.accept(7)
    call(brain, "speak", channel_id="pub", content="发出的话")
    call(brain, "sleep")
    section = brain.start_message(ME, wake(ME))[0].split("【我最近的行动】")[1].split("【可见历史】")[0]
    assert "被拒绝 NO_FLOOR：无发言权" in section and "成功（序号 #7）" in section and "sleep" not in section


# ------------------------------------------------------------------------------------------------ the increments and the hint (H14)


def start_with_board(world: FakeWorld, brain: BotBrain, *channels: dict, **changes: object) -> None:
    put_board(world, channels=list(channels), **changes)
    brain.start_message(ME, wake(ME))
    brain.hint_manifest(ME)


def test_nothing_new_is_nothing_to_tell() -> None:
    world, brain = make_brain()
    start_with_board(world, brain, CLOSED)
    assert brain.inject_message(ME, wake(ME, kind="inject", after=world.seq)) is None
    assert brain.hint_manifest(ME) is None


def test_an_increment_has_the_new_records_the_changes_and_the_wake_in_the_bot_style() -> None:
    world, brain = make_brain()
    start_with_board(world, brain, CLOSED)
    world.add("message", "p2", {"content": "轮到你了吗", "kind": "speech", "sender_name": "Blair"}, channel="pub")
    put_board(world, channels=[FLOOR_THREE])
    text, seen = brain.inject_message(ME, wake(ME, message_new(), kind="inject", after=0))  # type: ignore[misc]
    assert seen == world.seq and "【新动态】\n#1 [公共频道[pub]] p2 Blair 说：轮到你了吗" in text
    assert "还可发 3 条，单条 ≤ 300 字，总共还可写 600 字" in text and "你当前可以发言的频道：pub（queue）" in text
    assert "【本次唤醒】\n- 公共频道[pub] 里有 1 条新消息（p2 Blair）。" in text
    assert "【此刻的提醒】" not in text  # somebody spoke: that is no decision of the bot's


def test_a_turn_to_speak_gets_the_voice_the_entries_of_the_situation_and_the_one_thing_to_do() -> None:
    world, brain = make_brain()
    start_with_board(world, brain, CLOSED, phase="第 1 天")
    put_board(world, channels=[FLOOR_ONE], phase="第 1 天")
    text, _ = brain.inject_message(ME, wake(ME, floor_granted(), kind="inject", after=world.seq))  # type: ignore[misc]
    assert "【本次唤醒】\n- 你获得了 公共频道[pub] 的发言权：只能发 1 条，≤ 300 字，120 秒 内有效。" in text
    hint = text[text.index("【此刻的提醒】"):]
    assert text.endswith(hint) and hint.endswith("先想清楚此刻最关键的一两件事，再一次说完整。")
    assert "你的说话方式：你说话短，通常两三句话就停，很少超过一百五十字。篇幅 60–150 字。" in hint
    assert "你之前自己总结过：先看规则给出的结果，再看人说了什么（把握：高，支持 5 次，反例 0 次）；" in hint  # (tag 发言: floor.granted)
    manifest = brain.hint_manifest(ME)
    assert manifest == {"kind": "speak", "tags": ["发言"], "entries": [sha12(HIGH.text), sha12(MIDDLE.text)]}
    assert brain.hint_manifest(ME) is None  # (read once: the runtime never writes the same hint twice)


def test_the_tags_of_the_moment_come_from_the_scenario_and_the_phase() -> None:
    world, brain = make_brain()
    start_with_board(world, brain, CLOSED, phase="第 1 天 警长竞选")
    put_board(world, channels=[FLOOR_ONE], phase="第 1 天 警长竞选")
    brain.inject_message(ME, wake(ME, floor_granted(), kind="inject", after=world.seq))
    assert brain.hint_manifest(ME)["tags"] == ["发言", "警上"]  # type: ignore[index]
    plain_world, plain = make_brain(scenario=make_scenario([]))  # a scenario that declares no situation: no tag, no entry
    start_with_board(plain_world, plain, CLOSED)
    put_board(plain_world, channels=[FLOOR_ONE])
    text, _ = plain.inject_message(ME, wake(ME, floor_granted(), kind="inject", after=0))  # type: ignore[misc]
    assert "你之前自己总结过" not in text and "你的说话方式" in text
    assert plain.hint_manifest(ME) == {"kind": "speak", "tags": [], "entries": []}


def test_no_hint_when_the_floor_is_not_the_bots_to_use() -> None:
    world, brain = make_brain()
    start_with_board(world, brain, CLOSED)
    text, _ = brain.inject_message(ME, wake(ME, floor_granted(), kind="inject", after=0))  # type: ignore[misc]
    assert "【此刻的提醒】" not in text  # the envelope says "granted", the board says the bot cannot write: it believes the board
    assert brain.hint_manifest(ME) is None
    other = floor_granted(channel_id="elsewhere")
    put_board(world, channels=[FLOOR_ONE])
    assert "【此刻的提醒】" not in brain.inject_message(ME, wake(ME, other, kind="inject", after=0))[0]  # type: ignore[index]


def test_a_free_discussion_that_opens_for_the_bot_is_a_turn_to_speak_too() -> None:
    world, brain = make_brain()
    start_with_board(world, brain, CLOSED)
    put_board(world, channels=[FREE])
    text, _ = brain.inject_message(ME, wake(ME, message_new(MODERATOR_ID, "talk"), kind="inject", after=0))  # type: ignore[misc]
    assert "你的说话方式：" in text and text.endswith("先想清楚此刻最关键的一两件事，再一次说完整。")
    assert brain.hint_manifest(ME)["kind"] == "speak"  # type: ignore[index]
    again = brain.inject_message(ME, wake(ME, message_new("p2", "talk"), kind="inject", after=0))[0]  # type: ignore[index]
    assert "【此刻的提醒】" not in again  # (every later message of the discussion is only a message: it was open already)
    assert brain.hint_manifest(ME) is None


def test_a_vote_asks_for_the_hardest_fact() -> None:
    world, brain = make_brain()
    start_with_board(world, brain, CLOSED, phase="第 1 天 投票")
    put_board(world, requests=[VOTE], phase="第 1 天 投票")
    text, _ = brain.inject_message(ME, wake(ME, request_opened(VOTE), kind="inject", after=0))  # type: ignore[misc]
    hint = text[text.index("【此刻的提醒】"):]
    assert hint.startswith("【此刻的提醒】\n作答之前先问自己：你认定的最硬的事实是什么？") and "先想清楚" not in hint
    assert "先看规则给出的结果，再看人说了什么" in hint  # (the entry tagged 投票)
    assert brain.hint_manifest(ME) == {"kind": "choose", "tags": ["投票"], "entries": [sha12(HIGH.text)]}


def test_a_choice_asked_of_some_players_only_recalls_what_the_bot_learned_about_acting() -> None:
    world, brain = make_brain()
    scenario = make_scenario(ui=SimpleNamespace(status_tags=["存活"], out_tags=["出局"]))
    _, brain = make_brain(world, scenario=scenario)
    start_with_board(world, brain, CLOSED, phase="第 1 夜")
    board = put_board(world, requests=[ACTION], phase="第 1 夜")
    board["me"] = {**board["me"], "tags": ["存活", "甲类"]}
    text, _ = brain.inject_message(ME, wake(ME, request_opened(ACTION), kind="inject", after=0))  # type: ignore[misc]
    hint = text[text.index("【此刻的提醒】"):]
    assert "你认定的最硬的事实" not in hint  # not a choice everybody makes
    manifest = brain.hint_manifest(ME)
    assert manifest["kind"] == "act" and manifest["tags"] == ["投票", "行动"]  # type: ignore[index]
    assert manifest["entries"] == [sha12(HIGH.text), sha12(MIDDLE_ACT.text)]  # type: ignore[index]


def test_a_vote_that_only_some_may_cast_is_still_a_vote() -> None:
    """Found in a real decision point: the vote for a sheriff is asked of the players who did not run - not of everybody still in the
    game - and it took the bot for an action: no "what is the hardest fact".  Three players or more make a group choice."""
    world, brain = make_brain()
    start_with_board(world, brain, CLOSED, phase="第 1 天 警长竞选")
    players = [{"id": f"p{n}", "name": f"P{n}", "out": False, "tags": ["存活"]} for n in range(1, 6)]
    some = {**VOTE, "id": "r11", "to": ["p1", "p3", "p4"]}
    put_board(world, players=players, requests=[some], phase="第 1 天 警长竞选")
    text, _ = brain.inject_message(ME, wake(ME, request_opened(some), kind="inject", after=0))  # type: ignore[misc]
    assert "作答之前先问自己：你认定的最硬的事实是什么？" in text[text.index("【此刻的提醒】"):]
    assert brain.hint_manifest(ME)["kind"] == "choose"  # type: ignore[index]
    pair = {**VOTE, "id": "r12", "to": ["p1", "p3"]}  # (two players: a role doing something by itself)
    put_board(world, players=players, requests=[pair], phase="第 1 天 警长竞选")
    text, _ = brain.inject_message(ME, wake(ME, request_opened(pair, seq=6), kind="inject", after=0))  # type: ignore[misc]
    assert "你认定的最硬的事实" not in text
    assert (brain.hint_manifest(ME) or {}).get("kind", "act") == "act"


def test_the_bots_own_status_tags_do_not_pick_entries_but_its_identity_tags_do() -> None:
    entries = (
        PlaybookEntry("活着的时候的想法", "高", 1, 0, "", ("存活",)),
        PlaybookEntry("拿到这个身份才用得上的想法", "中", 1, 0, "", ("甲类",)),
    )
    mind = make_mind(rules=RulesMemory("x", playbook=entries))
    scenario = make_scenario([], ui=SimpleNamespace(status_tags=["存活"], out_tags=["出局"]))
    world, brain = make_brain(mind=mind, scenario=scenario)
    start_with_board(world, brain, CLOSED)
    board = put_board(world, requests=[ACTION])
    board["me"] = {**board["me"], "tags": ["存活", "甲类"]}
    brain.inject_message(ME, wake(ME, request_opened(ACTION), kind="inject", after=0))
    assert brain.hint_manifest(ME)["entries"] == [sha12(entries[1].text)]  # type: ignore[index]


def test_a_question_to_answer_in_words_gets_only_what_the_bot_recalls() -> None:
    world, brain = make_brain()
    start_with_board(world, brain, CLOSED)
    put_board(world, requests=[ASK])
    text, _ = brain.inject_message(ME, wake(ME, request_opened(ASK), kind="inject", after=0))  # type: ignore[misc]
    assert "【此刻的提醒】" not in text  # (kind text: no tag fits this scenario, so there is nothing to recall)
    assert brain.hint_manifest(ME) is None


def test_a_request_that_is_answered_already_or_gone_is_no_moment() -> None:
    world, brain = make_brain()
    start_with_board(world, brain, CLOSED)
    put_board(world, requests=[{**VOTE, "answered": True}])
    assert "【此刻的提醒】" not in brain.inject_message(ME, wake(ME, request_opened(VOTE), kind="inject", after=0))[0]  # type: ignore[index]
    put_board(world, requests=[])
    assert "【此刻的提醒】" not in brain.inject_message(ME, wake(ME, request_opened(VOTE), kind="inject", after=0))[0]  # type: ignore[index]


def test_two_decisions_at_once_share_one_hint() -> None:
    world, brain = make_brain()
    start_with_board(world, brain, CLOSED)
    put_board(world, channels=[FLOOR_ONE], requests=[VOTE])
    text, _ = brain.inject_message(ME, wake(ME, floor_granted(), request_opened(VOTE), kind="inject", after=0))  # type: ignore[misc]
    assert text.count("【此刻的提醒】") == 1 and "你的说话方式" in text and "你认定的最硬的事实是什么" in text
    manifest = brain.hint_manifest(ME)
    assert manifest["kind"] == "speak+choose" and manifest["tags"] == ["发言", "投票"]  # type: ignore[index]


def test_the_opening_message_may_carry_a_hint_too() -> None:
    world, brain = make_brain()
    put_board(world, channels=[FLOOR_ONE])
    text, _ = brain.start_message(ME, wake(ME, floor_granted()))
    assert text.endswith("先想清楚此刻最关键的一两件事，再一次说完整。") and "【此刻的提醒】" in text
    assert brain.hint_manifest(ME)["tags"] == ["发言"]  # type: ignore[index]
    plain, _ = brain.start_message(ME, wake(ME, message_new()))
    assert "【此刻的提醒】" not in plain and brain.hint_manifest(ME) is None


def test_a_brain_without_a_baseline_refreshes_the_board_and_the_facts_once() -> None:
    world, brain = make_brain()
    put_board(world, channels=[FLOOR_ONE], facts=FACTS)
    world.add("message", "p2", {"content": "你好", "kind": "speech", "sender_name": "Blair"}, channel="pub")
    first, _ = brain.inject_message(ME, wake(ME, kind="inject", after=0))  # type: ignore[misc]
    assert "（状态板完整刷新）" in first and "【状态板】" in first and "【硬事实台】" in first and "这次只能发 1 条" in first
    assert brain.inject_message(ME, wake(ME, kind="inject", after=world.seq)) is None  # (now it has one)
    brain.reset_conversation(ME)
    assert "（状态板完整刷新）" in brain.inject_message(ME, wake(ME, message_new(), kind="inject", after=0))[0]  # type: ignore[index]


def test_the_mount_manifest_is_the_system_prompt_blocks_then_the_opening_messages() -> None:
    world, brain = make_brain()
    assert [b["id"] for b in brain.mount_manifest(ME)] == [b.id for b in brain.composer.mounts()]  # (before any message: the prompt's blocks)
    put_board(world, channels=[FLOOR_ONE], facts=FACTS)
    text, _ = brain.start_message(ME, wake(ME, floor_granted()))
    blocks = brain.mount_manifest(ME)
    assert [b["layer"] for b in blocks][-7:] == ["L3"] * 7
    assert [b["id"] for b in blocks][-7:] == ["l3.board", "l3.facts", "l3.memory", "l3.actions", "l3.history", "l3.wake", "l3.hint"]
    assert all(set(b) == {"id", "layer", "chars", "sha"} for b in blocks) and json.dumps(blocks, ensure_ascii=False).count("先看规则") == 0
    assert sum(b["chars"] for b in blocks if b["layer"] == "L3") + 2 * 6 == len(text)  # (the opening message, cut where its blocks are)
    put_board(world, channels=[CLOSED], facts=None)
    brain.start_message(ME, wake(ME))
    assert [b["id"] for b in brain.mount_manifest(ME)][-5:] == ["l3.board", "l3.memory", "l3.actions", "l3.history", "l3.wake"]


# ------------------------------------------------------------------------------------------------ the output gate (H15)

LISTY = "我的看法：\n1. 他跳得太快\n2. 票也不对\n所以我投他。"


def speak(brain: BotBrain, content: str, channel: str = "pub"):
    return call(brain, "speak", channel_id=channel, content=content)


def test_a_speech_that_reads_like_a_report_is_sent_back_and_nothing_is_submitted() -> None:
    world, brain = make_brain()
    brain.start_message(ME, wake(ME, floor_granted()))
    out = speak(brain, LISTY)
    body = result(out)
    assert body["ok"] is False and body["code"] == Code.FORMAT and body["data"]["rule"] == "list" and body["data"]["rules"] == ["list"]
    assert "没有发出，发言权还在" in body["message"] and "连贯的口语" in body["message"]
    assert out.receipt is not None and out.receipt.ok is False and not out.ends_run and not out.shared
    assert world.submitted == []  # not a character of it reached the world
    assert "被拒绝 FORMAT" in brain.start_message(ME, wake(ME))[0]  # (and the bot is shown that it was)


def test_once_in_a_turn_the_same_text_goes_through_exactly_as_it_is() -> None:
    world, brain = make_brain()
    brain.start_message(ME, wake(ME, floor_granted()))
    assert result(speak(brain, LISTY))["code"] == Code.FORMAT
    again = speak(brain, LISTY)
    assert again.receipt is world.next_receipt and result(again)["ok"] is True  # (the world's own answer)
    assert [(i.type, i.actor_id, i.payload) for i in world.submitted] == [("speak", ME, {"channel_id": "pub", "content": LISTY})]


def test_the_turn_is_used_up_by_the_first_bounce_whatever_the_next_text_is() -> None:
    """"Once in the same floor": the bot was told once and has the right to say it as it wants - a *different* report-like text
    in that turn is not sent back a second time either (the point is that a speech is never held up for long)."""
    world, brain = make_brain()
    brain.start_message(ME, wake(ME, floor_granted()))
    assert result(speak(brain, LISTY))["code"] == Code.FORMAT
    other = speak(brain, "# 标题\n- a\n- b")
    assert result(other)["ok"] is True and world.submitted[-1].payload["content"] == "# 标题\n- a\n- b"


def test_a_speech_that_went_through_starts_a_new_turn_and_so_does_a_new_floor() -> None:
    world, brain = make_brain()
    brain.start_message(ME, wake(ME, floor_granted(seq=3)))
    assert result(speak(brain, LISTY))["code"] == Code.FORMAT
    assert result(speak(brain, "好的，我说完整的话。"))["ok"] is True  # the speech of this turn
    assert result(speak(brain, LISTY))["code"] == Code.FORMAT  # (the next one is looked at again)
    assert result(speak(brain, LISTY))["ok"] is True
    # the bot gave up in the middle of a turn; the next floor is looked at afresh
    world2, brain2 = make_brain()
    brain2.start_message(ME, wake(ME, floor_granted(seq=3)))
    assert result(speak(brain2, "p1：我先说"))["code"] == Code.FORMAT
    brain2.inject_message(ME, wake(ME, floor_granted(seq=8), kind="inject", after=0))
    assert result(speak(brain2, "p1：我先说"))["code"] == Code.FORMAT
    assert world2.submitted == []


def test_every_channel_has_its_own_turn() -> None:
    world, brain = make_brain()
    assert result(speak(brain, LISTY, "pub"))["code"] == Code.FORMAT
    assert result(speak(brain, LISTY, "talk"))["code"] == Code.FORMAT
    assert result(speak(brain, LISTY, "pub"))["ok"] is True and result(speak(brain, LISTY, "talk"))["ok"] is True
    assert len(world.submitted) == 2


def test_a_name_or_an_id_in_front_of_the_words_is_sent_back_and_every_reason_is_named() -> None:
    world, brain = make_brain()
    for text, rules in (("p1：我先说两句", ["prefix"]), ("Alex：我先说两句", ["prefix"]), ("Alex（p1）：\n# 我\n- a\n- b", ["prefix", "heading", "list"])):
        _, fresh = make_brain(world)
        data = result(speak(fresh, text))["data"]
        assert data["rule"] == rules[0] and data["rules"] == rules
    assert world.submitted == []
    assert result(speak(brain, "Blair：你昨天那票呢？"))["ok"] is True  # (somebody else's name is just a way to address them)


def test_a_voice_that_talks_in_lists_says_so_and_only_the_list_check_goes() -> None:
    mind = make_mind(core=CoreFiles(identity="话多。", voice="你爱分条说。\n允许口头列举\n篇幅：60–150 字", mind="先看事实。"))
    world, brain = make_brain(mind=mind)
    assert result(speak(brain, LISTY))["ok"] is True  # no FORMAT: the voice allows it
    assert result(speak(brain, "Alex：1. 一\n2. 二"))["code"] == Code.FORMAT  # (a prefix is still one)
    assert "允许口头列举" in brain.system_prompt(ME)  # (the user's text is mounted as it is)


def test_what_is_not_free_speech_is_not_looked_at() -> None:
    world, brain = make_brain()
    for name, args in (
        ("speak_template", {"channel_id": "pub", "template_id": "vote", "params": {"target": "p2", "reason": "1. 一\n2. 二"}}),
        ("challenge_moderator", {"content": "1. 不对\n2. 也不对"}),
        ("respond", {"request_id": "r7", "value": "p2", "reason": "- 一\n- 二"}),
        ("pass", {"channel_id": "pub"}),
    ):
        assert result(call(brain, name, **args))["ok"] is True, name
    assert [i.type for i in world.submitted] == ["speak_template", "challenge", "respond", "pass"]


def test_a_text_that_is_fine_is_submitted_byte_for_byte_and_the_world_has_the_last_word() -> None:
    world, brain = make_brain()
    text = "  我觉得他有问题。\n\n他昨天那票和今天的话对不上，你们怎么看？  "
    assert result(speak(brain, text))["ok"] is True
    assert world.submitted[-1].payload == {"channel_id": "pub", "content": text}  # nothing was trimmed, cut or tidied
    world.next_receipt = Receipt.reject(Code.TOO_LONG, "这条发言 400 个字，超过了单条上限 300 字，没有发出，发言权还在。请删减后重发。", chars=400, limit=300)
    too_long = speak(brain, "字" * 400)
    assert result(too_long)["code"] == Code.TOO_LONG and result(too_long)["data"] == {"chars": 400, "limit": 300}
    assert world.submitted[-1].payload["content"] == "字" * 400  # (it is the world that sends a long speech back, and it never cuts it)


def test_a_speech_the_gate_cannot_make_sense_of_is_left_to_the_world() -> None:
    world, brain = make_brain()
    for args in ({"content": LISTY}, {"channel_id": "pub"}, {"channel_id": "pub", "content": 12}, {"channel_id": "", "content": LISTY}):
        brain.execute_tool(ME, "speak", args, gen_id=1, seen_seq=0)
    assert len(world.submitted) == 4  # (the commit gate answers a speech without a channel or a text)


def test_only_a_bot_is_looked_at() -> None:
    """The moderator and the v0.13.1 player (``LegacyBrain``) never get a ``FORMAT``: they do not have that gate."""
    world = FakeWorld()
    legacy = LegacyBrain(world, world.spec, docs_dir=None)
    out = legacy.execute_tool(ME, "speak", {"channel_id": "pub", "content": LISTY}, gen_id=1, seen_seq=0)
    assert result(out)["ok"] is True and world.submitted[-1].payload["content"] == LISTY
    moderator = ArenaBrain(world, world.spec, docs_dir=None)
    out = moderator.execute_tool(MODERATOR_ID, "send_message", {"channel_id": "pub", "content": LISTY}, gen_id=1, seen_seq=0)
    assert result(out)["ok"] is True


def test_the_format_rejection_is_a_receipt_like_every_other() -> None:
    _, brain = make_brain()
    out = speak(brain, LISTY)
    assert out.receipt.code == Code.FORMAT and Code.FORMAT in Code.ALL  # type: ignore[union-attr]
    assert json.loads(out.receipt.tool_json())["data"]["rule"] == "list"  # type: ignore[union-attr]


# ------------------------------------------------------------------------------------------------ jot (H11, H53)


def test_jot_appends_a_line_to_the_note_the_review_reads() -> None:
    world, brain = make_brain()
    first = result(call(brain, "jot", text="p2 的票和他的话对不上"))
    assert first["ok"] is True and first["count"] == 1 and first["left"] == JOT_MAX - 1 and "第 1 条" in first["message"]
    second = result(call(brain, "jot", text="  赛后想想\n首夜为什么那样走  "))
    assert second["count"] == 2
    note = world.memory(ME).notes[JOT_NOTE_TITLE]
    assert note.splitlines() == ["p2 的票和他的话对不上", "赛后想想 首夜为什么那样走"]  # one line each: the newline of a clue is a space
    out = call(brain, "jot", text="x")
    assert out.shared and not out.ends_run and out.receipt is None and world.submitted == []
    assert "p2 的票和他的话对不上" in result(call(brain, "read_notes"))["notes"]  # (it is a note of the bot like any other)


def test_a_clue_that_is_too_long_is_refused_never_cut() -> None:
    world, brain = make_brain()
    body = result(call(brain, "jot", text="字" * (JOT_CHARS + 1)))
    assert body["ok"] is False and body["chars"] == JOT_CHARS + 1 and body["limit"] == JOT_CHARS
    assert f"{JOT_CHARS + 1} 字，超过了 {JOT_CHARS} 字的上限，没有记下" in body["error"] and "不会替你截断" in body["error"]
    assert JOT_NOTE_TITLE not in world.memory(ME).notes
    assert result(call(brain, "jot", text="字" * JOT_CHARS))["ok"] is True  # exactly the limit
    assert len(world.memory(ME).notes[JOT_NOTE_TITLE]) == JOT_CHARS


def test_at_most_ten_clues_a_game() -> None:
    world, brain = make_brain()
    for i in range(JOT_MAX):
        assert result(call(brain, "jot", text=f"线索{i}"))["ok"] is True
    body = result(call(brain, "jot", text="第十一条"))
    assert body["ok"] is False and f"本局最多记 {JOT_MAX} 条线索，已经记满" in body["error"] and body["count"] == JOT_MAX
    assert len(world.memory(ME).notes[JOT_NOTE_TITLE].splitlines()) == JOT_MAX and "第十一条" not in world.memory(ME).notes[JOT_NOTE_TITLE]
    # the count lives in the note, which is saved with the game: a brain built again for the same world goes on from there
    rebuilt = BotBrain(world, make_scenario(), ME, make_mind(), contract())
    assert result(call(rebuilt, "jot", text="再记一条"))["ok"] is False


def test_a_clue_must_be_a_text() -> None:
    world, brain = make_brain()
    for args in ({}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": None}):
        body = result(brain.execute_tool(ME, "jot", args, gen_id=1, seen_seq=0))
        assert body["ok"] is False and "text" in body["error"]
    bad = brain.execute_tool(ME, "jot", {"_args_error": "JSON 不合法"}, gen_id=1, seen_seq=0)
    assert bad.receipt is not None and bad.receipt.code == Code.INVALID_PARAMS  # (an unparseable call never reaches the tool)
    assert JOT_NOTE_TITLE not in world.memory(ME).notes


# ------------------------------------------------------------------------------------------------ read_contract (H7, H53)


def test_read_contract_gives_the_reference_part_whole_or_one_section() -> None:
    _, brain = make_brain()
    whole = result(call(brain, "read_contract"))
    assert whole["ok"] is True and whole["part"] == "全部" and whole["content"] == contract().reference.strip()
    one = result(call(brain, "read_contract", part="回执码"))
    assert one["part"] == "回执码" and one["content"].startswith("### 回执码") and "`TOO_LONG`" in one["content"] and "### 唤醒与休眠" not in one["content"]
    assert result(call(brain, "read_contract", part="jot"))["content"].count("### ") == 1
    assert result(call(brain, "read_contract", part=""))["part"] == "全部"
    assert result(call(brain, "read_contract", part=5))["ok"] is False  # (a number is no heading)


def test_an_unknown_section_names_the_ones_there_are() -> None:
    _, brain = make_brain()
    body = result(call(brain, "read_contract", part="不存在"))
    assert body["ok"] is False and "没有叫“不存在”的小节" in body["error"] and "回执码" in body["error"] and "信息边界" in body["error"]
    out = call(brain, "read_contract", part="回执码")
    assert out.shared and out.receipt is None and not out.ends_run


def test_a_contract_without_a_reference_says_so() -> None:
    from app.bots.types import split_contract

    world = FakeWorld()
    brain = BotBrain(world, make_scenario(), ME, make_mind(), split_contract("## 必读\n只有必读\n"))
    assert result(call(brain, "read_contract")) == {"ok": False, "error": "这份契约没有“参考”部分。"}


def test_the_reference_documents_are_gone() -> None:
    world, brain = make_brain()
    for name, args in (("list_documents", {}), ("read_document", {"doc_name": "strategy.md"})):
        out = call(brain, name, **args)
        assert out.receipt is not None and out.receipt.code == Code.INVALID_PARAMS and f"未知工具：{name}" in out.receipt.message
    assert world.submitted == []


# ------------------------------------------------------------------------------------------------ the rest of the tools


def test_sleep_and_the_notes_work_as_they_always_did() -> None:
    world, brain = make_brain()
    brain.begin_run(ME)
    slept = call(brain, "sleep", seconds=30)
    assert slept.ends_run and slept.sleep_s == 30.0
    assert result(call(brain, "write_note", title="要点", content="第一条"))["ok"] is True
    assert "第一条" in result(call(brain, "read_notes"))["notes"]
    assert call(brain, "wait").ends_run  # (the old name still sleeps, it is only not advertised)


def test_sleeping_with_a_duty_is_a_reminder_the_first_time() -> None:
    world, brain = make_brain()
    put_board(world, requests=[VOTE])
    brain.begin_run(ME)
    first = call(brain, "sleep")
    assert not first.ends_run and result(first)["code"] == "NOT_FINISHED" and "r7" in result(first)["message"]
    assert call(brain, "sleep").ends_run


def test_the_nudge_is_once_a_run_and_never_for_nothing() -> None:
    world, brain = make_brain()
    assert brain.no_tool_nudge(ME, "   ", 0) is None
    assert brain.no_tool_nudge(ME, "我想说点什么", 0) == NUDGE_TEXT
    assert brain.no_tool_nudge(ME, "我还想说", 1) is None
    brain.begin_run(ME)  # a new run arms it again
    put_board(world, requests=[VOTE])
    nudge = brain.no_tool_nudge(ME, "", 0)
    assert nudge is not None and nudge.startswith(NUDGE_TEXT) and "r7" in nudge and "没处理完" in nudge
    assert "不会发送到任何频道" in NUDGE_TEXT  # (a plain text is not a speech: the bot has to call speak itself)


# ------------------------------------------------------------------------------------------------ a game long: the prompt does not move


def test_the_system_prompt_and_the_tools_are_the_same_bytes_all_game_long() -> None:
    world, brain = make_brain()
    prompt, tools = brain.system_prompt(ME), json.dumps(brain.tool_schemas(ME), ensure_ascii=False)
    for round_no in range(3):
        put_board(world, channels=[FLOOR_ONE if round_no % 2 else FREE], facts=FACTS[: round_no + 1], phase=f"阶段{round_no}")
        world.add("message", "p2", {"content": f"第{round_no}句", "kind": "speech", "sender_name": "Blair"}, channel="pub")
        brain.begin_run(ME)
        brain.start_message(ME, wake(ME, floor_granted(seq=round_no + 1)))
        brain.inject_message(ME, wake(ME, request_opened(VOTE), kind="inject", after=0))
        speak(brain, LISTY)
        call(brain, "jot", text=f"线索{round_no}")
        call(brain, "write_note", title="n", content="x")
        brain.no_tool_nudge(ME, "x", 0)
        brain.hint_manifest(ME)
        brain.mount_manifest(ME)
        brain.reset_conversation(ME)
    assert brain.system_prompt(ME) is prompt or brain.system_prompt(ME) == prompt
    assert brain.system_prompt(ME) == prompt and json.dumps(brain.tool_schemas(ME), ensure_ascii=False) == tools
    rebuilt = BotBrain(world, make_scenario(), ME, make_mind(), contract())
    assert rebuilt.system_prompt(ME) == prompt  # (a game that is resumed rebuilds the prompt it began with)


def test_with_memory_off_only_the_l0_the_contract_and_the_rules_are_shown() -> None:
    world, brain = make_brain(memory_enabled=False)
    prompt = brain.system_prompt(ME)
    for gone in ("我理解这个游戏的核心", "Blair", "发言前先看状态板", "我以往的战绩"):
        assert gone not in prompt
    put_board(world, channels=[FLOOR_ONE])
    brain.start_message(ME, wake(ME, floor_granted()))
    assert brain.hint_manifest(ME) == {"kind": "speak", "tags": ["发言"], "entries": []}
    assert "你之前自己总结过" not in brain.inject_message(ME, wake(ME, floor_granted(), kind="inject", after=0))[0]  # type: ignore[index]


# ------------------------------------------------------------------------------------------------ nobody speaks for a bot (H18, C14)


def test_nothing_but_the_bots_own_speak_call_reaches_the_channel() -> None:
    """Every method that prepares what the bot sees, and every tool that is not speech, leaves the world untouched; the only intent
    of a speech is the one the model's own ``speak`` call asked for, with the model's own words."""
    world, brain = make_brain()
    put_board(world, channels=[FLOOR_ONE], requests=[VOTE], facts=FACTS, phase="第 1 天")
    brain.begin_run(ME)
    brain.system_prompt(ME)
    brain.tool_schemas(ME)
    brain.start_message(ME, wake(ME, floor_granted(), request_opened(VOTE)))
    brain.inject_message(ME, wake(ME, floor_granted(seq=9), kind="inject", after=0))
    brain.hint_manifest(ME)
    brain.mount_manifest(ME)
    brain.no_tool_nudge(ME, "我想说一段话但没有调用工具", 0)  # the text of a model that did not call a tool is never a speech
    brain.reset_conversation(ME)
    for name, args in (
        ("write_note", {"title": "t", "content": "c"}),
        ("read_notes", {}),
        ("jot", {"text": "线索"}),
        ("read_contract", {}),
        ("sleep", {}),
        ("compact_context", {"summary_markdown": "摘要"}),
        ("list_documents", {}),
        ("nope", {}),
    ):
        brain.execute_tool(ME, name, args, gen_id=1, seen_seq=0)
    speak(brain, LISTY)  # sent back by the gate
    assert world.submitted == []
    words = "这是模型自己调用 speak 时写下的话。"
    speak(brain, words)
    assert [(i.type, i.actor_id, i.payload) for i in world.submitted] == [("speak", ME, {"channel_id": "pub", "content": words})]


def test_the_agent_modules_have_no_other_way_to_submit_anything() -> None:
    """The commit gate is reached only through ``ToolExecutor.execute`` (the tools of a model's own call); the composer, the gate, the
    contract and the bot's brain have no ``submit`` and build no intent."""
    for module in (mind_module, composer_module, gates_module, contract_module, bot_tools_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        assert ".submit(" not in code and "Intent(" not in code, module.__name__
    classic = Path(__import__("app.agents.tools", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    assert classic.count("self.world.submit(") == 1  # the one place
