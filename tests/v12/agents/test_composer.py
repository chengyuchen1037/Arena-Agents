"""The Composer (v0.14 H12-H15; contract §13.5): the mounting plan, the budgets, the first-person prose, the hints."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.agents.composer as composer_module
from app.agents.composer import HINT_KINDS, Composer, ComposerBudgets, first_sentence, length_range, sha12
from app.bots.types import BotMind, CoreFiles, MountBlock, PlaybookEntry, RulesMemory, split_contract

from ..test_neutrality import FIXED_PARTICIPANT_ID_RE, FORBIDDEN_TERMS
from .bot_fakes import IDENTITY, MIND, PLAYBOOK, VOICE, contract, make_mind, make_scenario

LIST_OR_HEADING_RE = re.compile(r"^\s*(?:[-*+•·]\s|\d+[.)、）]\s*|[①-⑩]|[一二三四五六七八九十]+、|#{1,6}\s)")
MIDDLE, LOW_TAGGED, HIGH, MIDDLE_ACT, LOW_IDENTITY = PLAYBOOK  # (the order the user wrote them in)


def composer(mind: BotMind | None = None, **kw: object) -> Composer:
    kw.setdefault("spec", make_scenario())
    kw.setdefault("seat_id", "p1")
    return Composer(mind or make_mind(), contract(), **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------ the mounting plan (H12)


def test_the_system_prompt_follows_the_mounting_plan_in_order() -> None:
    comp = composer()
    text = comp.system_prompt()
    marks = [
        "你是 Alex（本局编号 p1）。" + IDENTITY,  # ① who I am ...
        VOICE.strip(),  # ... how I talk
        MIND.strip(),  # ② how I reason
        "口头禅：「行，听你的，出了再说。」",  # (its extra sections)
        "【我对这个游戏的理解】",  # ③ what I understand of this game: digest, playbook, record
        "我理解这个游戏的核心",
        "我很有把握的：",
        "我以往的战绩：场次 7 · 胜 4 · 胜率 57%。",
        "【我对在座各位的印象】",  # ④
        "Blair：话多，爱站边，前后常不一致。",
        "【这局游戏的官方规则】\n规则正文",  # ⑤
        "【平台怎么运作】",  # ⑥ the contract's must-read part, then my own notes
        "你只能通过工具影响这个世界。",
        "我自己整理的平台操作笔记：\n发言前先看状态板",
        "【再提醒一次】你是 Alex。",  # ⑦
    ]
    positions = [text.index(mark) for mark in marks]
    assert positions == sorted(positions) and len(set(positions)) == len(positions)
    assert [(b.id, b.layer) for b in comp.mounts()] == [
        ("core.identity", "L0"), ("core.voice", "L0"), ("core.mind", "L0"), ("core.extra", "L0"),
        ("l2.digest", "L2"), ("l2.playbook", "L2"), ("l2.record", "L2"), ("l2.people", "L2"), ("rules", "L2"),
        ("l1.contract", "L1"), ("l1.notes", "L1"), ("anchor", "L0"),
    ]


def test_the_contract_that_is_mounted_is_the_must_read_part_only() -> None:
    doc = contract()
    text = composer().system_prompt()
    assert doc.must_read in text
    assert doc.reference.split("\n", 1)[0] not in text and "### 回执码" not in text  # the reference is read on demand
    assert "你是这场由主持人主持的游戏里的一名玩家" in text  # (the one line of frame the composer adds)


def test_the_l0_is_whole_at_the_start_and_comes_back_in_the_anchor() -> None:
    core = CoreFiles(
        identity="甲" * 150 + "，" + "乙" * 140 + "。",
        voice=("丙" * 40 + "，" + "丁" * 20 + "。") * 9 + "\n篇幅：30–90 字",
        mind="戊" * 399 + "。",
        extras=(("口头禅", "己" * 299), ("习惯", "庚" * 299), ("忌讳", "辛" * 299)),
    )
    tiny = ComposerBudgets(understanding=1, people=1, notes=1, contract=1, anchor_sentence=20)
    text = composer(make_mind(core=core), budgets=tiny).system_prompt()
    assert text.startswith("你是 Alex（本局编号 p1）。" + core.identity)
    for part in (core.voice, core.mind, *(f"{title}：{body}" for title, body in core.extras)):
        assert part in text  # not one character of the L0 is cut, whatever the budgets are
    anchor = text[text.index("【再提醒一次】"):]
    assert text.endswith(anchor) and anchor.startswith("【再提醒一次】你是 Alex。")
    assert "说话的方式：" in anchor and anchor.endswith("篇幅 30–90 字。")
    assert len(anchor) < 140 and "戊" not in anchor  # the anchor is a reminder of one sentence each, not a second copy


def test_a_bot_without_the_optional_parts_is_just_what_it_has() -> None:
    bare = BotMind(bot_id="b-00000000", name="Dee", core=CoreFiles(identity="话少。", voice="你说话短。", mind="先看事实。"))
    comp = Composer(bare, contract(), spec=make_scenario())
    ids = [b.id for b in comp.mounts()]
    assert ids == ["core.identity", "core.voice", "core.mind", "rules", "l1.contract", "anchor"]
    assert comp.system_prompt().startswith("你是 Dee。话少。")  # (no seat given: no "本局编号")
    nameless = BotMind(bot_id="b-00000000", name="", core=CoreFiles(identity="话少。"))
    assert Composer(nameless, contract(), spec=make_scenario()).system_prompt().startswith("话少。")


# ------------------------------------------------------------------------------------------------ prose, not lists (H13)


def test_what_the_bot_learned_is_rendered_as_first_person_prose() -> None:
    text = composer().system_prompt()
    learned = text[text.index("【我对这个游戏的理解】"):text.index("【这局游戏的官方规则】")]
    assert not [line for line in learned.splitlines() if LIST_OR_HEADING_RE.match(line)]  # no bullet, no number, no markdown heading
    assert "我很有把握的：先看规则给出的结果，再看人说了什么。" in learned
    assert "我觉得多半成立、但还要继续验证的：两个人都说自己是同一个身份时，先看谁的结论和之后的行动对得上；轮到自己行动时，先想清楚手里的信息是谁给的。" in learned
    assert "它们都只是我目前的假设，要拿这一局的事实和结果去检验，对不上就改" in learned  # the openness: hypotheses, tested by what happens
    assert "不是谁是什么身份的结论" in text  # impressions are about style and habits, never about who somebody is
    assert "。。" not in text and "；；" not in text


def test_only_the_playbook_entries_of_middle_and_high_confidence_are_mounted() -> None:
    text = composer().system_prompt()
    for kept in (HIGH, MIDDLE, MIDDLE_ACT):
        assert kept.text.rstrip("。") in text
    for left_out in (LOW_TAGGED, LOW_IDENTITY):
        assert left_out.text.rstrip("。") not in text  # a low confidence hypothesis only comes up through the hints
    assert "（把握" not in text  # (the evidence of an entry belongs to the hint of a moment, not to the standing prompt)


def test_over_the_budget_the_weakest_entries_go_first_and_the_digest_stays_whole() -> None:
    entries = tuple(
        PlaybookEntry(f"想法{i:02d}" + "字" * 70, "中" if i % 2 else "高", support=i, counter=0, tags=()) for i in range(20)
    )
    mind = make_mind(rules=RulesMemory(scenario_id="x", digest="摘要" * 200, playbook=entries))

    def mounted(budget: int) -> list[int]:
        text = composer(mind, budgets=ComposerBudgets(understanding=budget)).system_prompt()
        assert "摘要" * 200 in text  # the digest is not what gets compressed
        return [i for i in range(20) if f"想法{i:02d}" in text]

    assert mounted(1000) == [6, 8, 10, 12, 14, 16, 18]  # the high ones with the most support (and no middle one yet)
    assert mounted(1600) == sorted([*range(0, 20, 2), 11, 13, 15, 17, 19])  # every high one first, then the middle ones with the most support
    assert mounted(50) == []  # nothing fits: no entry, no half entry
    assert len(mounted(100_000)) == 20


def test_only_the_impressions_of_those_who_are_seated_are_mounted() -> None:
    mind = make_mind(people={"Blair": "话多", "Casey": "话少", "Zed": "已经不在了", "Alex": "这是我自己", "  ": "没有名字", "Drew": "  "})
    text = composer(mind, seated=["Blair", "casey", "Drew"]).system_prompt()  # (the case of a name does not matter)
    assert "Blair：话多。" in text and "Casey：话少。" in text
    assert "Zed" not in text and "这是我自己" not in text and "没有名字" not in text and "Drew" not in text
    assert "Zed：已经不在了。" in composer(mind).system_prompt()  # without a list of the seated, the snapshot is taken as it is
    assert "Blair：话多。" in composer(mind).system_prompt() and "Alex：这是我自己" not in composer(mind).system_prompt()
    assert "【我对在座各位的印象】" not in composer(mind, seated=["Nobody"]).system_prompt()
    long_people = {f"人{i:02d}": "印" * 78 for i in range(30)}
    crowded = composer(make_mind(people=long_people), budgets=ComposerBudgets(people=500)).system_prompt()
    assert "人00：" in crowded and "人05：" in crowded and "人06：" not in crowded  # over the budget the impressions are what is compressed


def test_memory_off_mounts_the_l0_the_contract_and_the_rules_only() -> None:
    comp = composer(memory_enabled=False)
    text = comp.system_prompt()
    assert [b.id for b in comp.mounts()] == ["core.identity", "core.voice", "core.mind", "core.extra", "rules", "l1.contract", "anchor"]
    for gone in ("我理解这个游戏的核心", "我很有把握的", "Blair", "我以往的战绩", "发言前先看状态板", "【我对"):
        assert gone not in text
    assert IDENTITY in text and "你只能通过工具影响这个世界" in text and "规则正文" in text
    hint, manifest = comp.event_hint("speak", ["发言"])
    assert "你之前自己总结过" not in hint and manifest["entries"] == [] and comp.retrieve(["发言"]) == []


def test_a_notes_text_over_the_budget_is_cut_at_a_sentence() -> None:
    notes = "；".join(f"第{i}条笔记" + "字" * 30 + "。" for i in range(40))
    text = composer(make_mind(arch_notes=notes), budgets=ComposerBudgets(notes=300)).system_prompt()
    shown = text[text.index("我自己整理的平台操作笔记：\n") + len("我自己整理的平台操作笔记：\n"):text.index("【再提醒一次】")].strip()
    assert 150 < len(shown) <= 301 and shown.endswith("…") and shown.startswith("第0条笔记")


# ------------------------------------------------------------------------------------------------ one string, fixed (cache)


def test_the_system_prompt_is_one_string_that_nothing_changes() -> None:
    comp = composer()
    first = comp.system_prompt()
    comp.event_hint("speak", ["发言"])
    comp.event_hint("choose", ["投票"])
    comp.retrieve(["发言"])
    comp.situation_tags(event="floor.granted")
    assert comp.system_prompt() is first
    assert composer().system_prompt() == first  # the same inputs, the same bytes (a resumed game rebuilds the very same prompt)
    assert composer(spec=make_scenario([])).system_prompt() == first  # (the situations are for the hints, not for the prompt)
    assert composer(make_mind(name="Dana")).system_prompt() != first


def test_the_mount_manifest_has_ids_layers_lengths_and_hashes_and_matches_the_prompt() -> None:
    comp = composer()
    text, blocks = comp.system_prompt(), comp.mounts()
    assert all(isinstance(block, MountBlock) for block in blocks)
    position = 0
    for block in blocks:  # the blocks are the prompt, cut where the blank lines between them are
        piece = text[position:position + block.chars]
        assert sha12(piece) == block.sha and len(block.sha) == 12
        position += block.chars + 2
    assert position - 2 == len(text)
    dumped = json.dumps([block.to_dict() for block in blocks], ensure_ascii=False)
    assert set(blocks[0].to_dict()) == {"id", "layer", "chars", "sha"}
    for body in (IDENTITY, "先看规则给出的结果", "Blair", "发言前先看状态板", "规则正文"):
        assert body not in dumped  # a manifest never carries a body


# ------------------------------------------------------------------------------------------------ small pieces


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("你说话短，通常两三句话就停。爱反问。", 48, "你说话短，通常两三句话就停。"),
        ("一个老玩家，话不多", 48, "一个老玩家，话不多。"),  # (a sentence that has no end gets one)
        ("\n\n  先空行再说话！后面的不要\n第二行。", 48, "先空行再说话！"),
        ("甲" * 30 + "，" + "乙" * 30 + "。", 40, "甲" * 30 + "…"),  # too long: cut at the last comma that fits
        ("甲" * 100, 20, "甲" * 19 + "…"),  # no comma: cut at the limit
        ("「你急什么？」我只看票。", 48, "「你急什么？」"),
        ("", 48, ""),
        ("   \n  ", 48, ""),
    ],
)
def test_first_sentence(text: str, limit: int, expected: str) -> None:
    assert first_sentence(text, limit) == expected


@pytest.mark.parametrize(
    ("voice", "expected"),
    [
        ("篇幅：60–150 字", (60, 150)),
        ("篇幅 60-150 字", (60, 150)),
        ("篇幅:60~150字", (60, 150)),
        ("篇幅：60到150 字", (60, 150)),
        ("你说话短。\n篇幅：80—200 字\n", (80, 200)),
        ("篇幅：150–60 字", None),  # backwards
        ("篇幅：0–10 字", None),
        ("通常 60–150 字", None),  # only a line that says 篇幅 counts
        ("", None),
    ],
)
def test_length_range(voice: str, expected: tuple[int, int] | None) -> None:
    assert length_range(voice) == expected


def test_sha12_is_the_start_of_the_content_hash() -> None:
    from app.bots.types import sha256_text

    assert sha12("甲") == sha256_text("甲")[:12] and len(sha12("")) == 12


# ------------------------------------------------------------------------------------------------ situations (H14)


def test_situation_tags_follow_the_rules_the_scenario_declares() -> None:
    comp = composer()
    tags = comp.situation_tags
    assert tags(event="floor.granted", channel_id="pub", channel_type="public", phase="第 1 天 白天发言") == ["发言"]
    assert tags(event="floor.granted", channel_id="pub", channel_type="public", phase="警长竞选") == ["发言", "警上"]  # (in the declared order)
    assert tags(event="request.opened", phase="第 1 天 投票", request_kind="choice") == ["投票"]
    assert tags(event="request.opened", phase="第 1 夜", request_kind="choice") == ["投票", "行动"]  # every rule that fits
    assert tags(event="request.opened", phase="第 1 夜", request_kind="text") == ["行动"]
    assert tags(event="message.new", channel_id="dm-p1", channel_type="dm") == ["私聊"]
    assert tags(event="message.new", channel_type="public") == [] and tags() == []


def test_a_rule_may_be_an_object_and_a_rule_without_a_tag_is_ignored() -> None:
    rules = [
        SimpleNamespace(tag="甲", phase_contains="", channel_type="", channel_id="pub", events=[], request_kind=""),
        SimpleNamespace(tag="", phase_contains="", channel_type="", channel_id="", events=[], request_kind=""),
        {"tag": "乙"},  # (a rule that only has a tag fits everything)
        {"tag": "甲"},  # (a tag is listed once, however many rules give it)
    ]
    comp = composer(spec=make_scenario(rules))
    assert comp.situation_tags(channel_id="pub") == ["甲", "乙"] and comp.situation_tags(channel_id="x") == ["乙", "甲"]
    assert composer(spec=SimpleNamespace(rules_text="规则")).situation_tags(event="floor.granted") == []  # a scenario without the key


def test_the_situation_tags_can_be_told_to_a_bot_in_words() -> None:
    from app.agents.composer import describe_situations

    assert describe_situations(make_scenario()) == (
        "这个剧本声明的情境标签：发言（轮到发言）；投票（收到请求，选择类请求）；行动（收到请求，阶段含“夜”）；"
        "警上（轮到发言，阶段含“竞选”）；私聊（频道类型 dm）。"
    )
    twice = make_scenario([{"tag": "甲", "events": ["floor.granted"]}, {"tag": "甲", "channel_id": "pub"}, {"tag": "乙"}, {"tag": " "}])
    assert describe_situations(twice) == "这个剧本声明的情境标签：甲（轮到发言 或 频道 pub）；乙。"
    assert describe_situations(make_scenario([])) == "" and describe_situations(SimpleNamespace(rules_text="x")) == ""


# ------------------------------------------------------------------------------------------------ retrieval (H14)


def test_retrieval_finds_the_entries_of_the_situation_best_first() -> None:
    comp = composer()
    assert comp.retrieve(["发言"]) == [HIGH, MIDDLE]  # all three are tagged 发言 once: more confidence first (LOW_TAGGED is left out by the limit of two)
    assert comp.retrieve(["发言", "对跳"]) == [MIDDLE, HIGH]  # two matching tags beat one
    assert comp.retrieve(["发言"], limit=3) == [HIGH, MIDDLE, LOW_TAGGED]
    assert comp.retrieve(["不存在"]) == [] and comp.retrieve([]) == []
    assert comp.retrieve(["投票"]) == [HIGH]
    untagged = make_mind(rules=RulesMemory("x", playbook=(PlaybookEntry("没有标签的想法", "高"),)))
    assert composer(untagged).retrieve(["发言"]) == []


def test_identity_tags_count_for_an_act_only_and_less_than_a_situation() -> None:
    comp = composer()
    assert comp.retrieve(["行动"], identity_tags=["甲类"]) == [MIDDLE_ACT, LOW_IDENTITY]
    assert comp.retrieve([], identity_tags=["甲类"]) == [LOW_IDENTITY]
    _, speak = comp.event_hint("speak", [], identity_tags=["甲类"])
    assert speak["entries"] == []  # a turn to speak does not look at who the bot is
    _, act = comp.event_hint("act", ["行动"], identity_tags=["甲类"])
    assert act["entries"] == [sha12(MIDDLE_ACT.text), sha12(LOW_IDENTITY.text)]


def test_the_hint_has_at_most_the_entries_the_budget_allows() -> None:
    one = composer(budgets=ComposerBudgets(hint_entries=1))
    assert one.retrieve(["发言"]) == [HIGH]
    assert len(composer().retrieve(["发言"])) == 2


# ------------------------------------------------------------------------------------------------ the hints (H14)


def test_the_hint_of_a_turn_to_speak_has_the_voice_the_entries_and_the_one_thing_to_do() -> None:
    text, manifest = composer().event_hint("speak", ["发言"])
    assert text.startswith("【此刻的提醒】\n你的说话方式：你说话短，通常两三句话就停，很少超过一百五十字。篇幅 60–150 字。\n")
    assert "你之前自己总结过：先看规则给出的结果，再看人说了什么（把握：高，支持 5 次，反例 0 次）；" in text
    assert "两个人都说自己是同一个身份时，先看谁的结论和之后的行动对得上（把握：中，支持 3 次，反例 1 次）。" in text
    assert "这些只是你的假设，和眼前的事实对不上就不用。" in text
    assert text.endswith("先想清楚此刻最关键的一两件事，再一次说完整。")
    assert manifest == {"kind": "speak", "tags": ["发言"], "entries": [sha12(HIGH.text), sha12(MIDDLE.text)]}
    for body in ("你先看发生了什么", "口头禅", "Blair", "规则正文"):
        assert body not in text  # a hint is a reminder of the moment, not another copy of the prompt


def test_an_entry_that_is_not_confirmed_is_introduced_as_not_verified() -> None:
    text, manifest = composer().event_hint("speak", ["发言"])
    only_low = make_mind(rules=RulesMemory("x", playbook=(LOW_TAGGED,)))
    low_text, low_manifest = composer(only_low).event_hint("speak", ["发言"])
    assert "你还有一个想法，还没验证过：首夜的随手一步本来就没有道理可讲，追问它得不到信息。" in low_text
    assert "你之前自己总结过" not in low_text and "（把握" not in low_text  # (nothing to show for it yet)
    assert low_manifest["entries"] == [sha12(LOW_TAGGED.text)]
    mixed, _ = composer().event_hint("speak", ["发言"], identity_tags=[])
    assert "还没验证过" not in mixed and "还没验证过" not in text  # (the low one lost to the limit of two)
    three = Composer(make_mind(), contract(), spec=make_scenario(), budgets=ComposerBudgets(hint_entries=3)).event_hint("speak", ["发言"])[0]
    assert "你还有一个想法，还没验证过：首夜" in three


def test_a_hint_with_no_situation_still_has_the_voice_and_never_invents_entries() -> None:
    text, manifest = composer().event_hint("speak", [])
    assert text == (
        "【此刻的提醒】\n你的说话方式：你说话短，通常两三句话就停，很少超过一百五十字。篇幅 60–150 字。\n"
        "先想清楚此刻最关键的一两件事，再一次说完整。"
    )
    assert manifest == {"kind": "speak", "tags": [], "entries": []}
    voiceless = BotMind(bot_id="b-00000000", name="Dee", core=CoreFiles(identity="话少。"))
    assert Composer(voiceless, contract(), spec=make_scenario()).event_hint("speak", [])[0] == (
        "【此刻的提醒】\n先想清楚此刻最关键的一两件事，再一次说完整。"
    )


def test_the_hint_before_a_choice_asks_for_the_hardest_fact() -> None:
    text, manifest = composer().event_hint("choose", ["投票"])
    assert text.startswith("【此刻的提醒】\n作答之前先问自己：你认定的最硬的事实是什么？\n")
    assert "先看规则给出的结果，再看人说了什么" in text and "先想清楚" not in text and "你的说话方式" not in text
    assert manifest["kind"] == "choose" and manifest["tags"] == ["投票"]
    bare, bare_manifest = composer().event_hint("choose", [])
    assert bare == "【此刻的提醒】\n作答之前先问自己：你认定的最硬的事实是什么？" and bare_manifest["entries"] == []


def test_an_action_or_an_answer_gets_a_hint_only_when_the_bot_has_something_to_recall() -> None:
    comp = composer()
    assert comp.event_hint("act", []) == ("", {}) and comp.event_hint("answer", ["不存在"]) == ("", {})
    text, manifest = comp.event_hint("act", ["行动"])
    assert text.startswith("【此刻的提醒】\n你之前自己总结过：轮到自己行动时，先想清楚手里的信息是谁给的") and "你认定的最硬" not in text
    assert manifest["kind"] == "act"
    assert comp.event_hint("answer", ["投票"])[1]["kind"] == "answer"


def test_an_unknown_kind_is_no_hint_and_every_known_kind_is_listed() -> None:
    assert composer().event_hint("sing", ["发言"]) == ("", {})
    assert HINT_KINDS == ("speak", "choose", "act", "answer")
    for kind in HINT_KINDS:
        text, manifest = composer().event_hint(kind, ["发言", "投票", "行动"])
        assert not text or manifest["kind"] == kind


def test_a_tag_given_twice_counts_once() -> None:
    _, manifest = composer().event_hint("speak", ["发言", "发言", "", "发言"])
    assert manifest["tags"] == ["发言"]


# ------------------------------------------------------------------------------------------------ the Composer says nothing


def test_the_composer_has_no_world_and_no_way_to_say_anything() -> None:
    """H18: it prepares the context and nothing else.  No world, no intent, no submit, no model call."""
    source = Path(composer_module.__file__).read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    for word in ("submit(", "Intent", "WorldPort", "self.world", "complete(", "LlmPort", "speak_template("):
        assert word not in code, word
    comp = composer()
    assert not [name for name in dir(comp) if name.lstrip("_") in ("world", "llm", "submit", "speak", "say", "send")]


def test_the_module_is_game_neutral() -> None:
    text = Path(composer_module.__file__).read_text(encoding="utf-8")
    assert not [term for term in FORBIDDEN_TERMS if term in text] and not FIXED_PARTICIPANT_ID_RE.search(text)


def test_a_contract_without_a_must_read_part_still_gets_its_frame_and_a_runaway_one_is_cut() -> None:
    bare = split_contract("## 参考\n只有参考\n")
    text = Composer(make_mind(), bare, spec=make_scenario()).system_prompt()
    assert "【平台怎么运作】\n你是这场由主持人主持的游戏里的一名玩家" in text and "只有参考" not in text
    paragraphs = "\n\n".join("字" * 300 for _ in range(10))
    long = split_contract(f"## 必读\n{paragraphs}\n## 参考\n乙\n")
    mounted = Composer(make_mind(), long, spec=make_scenario()).system_prompt()
    assert mounted.count("字" * 300) == 3 and "字" * 301 not in mounted  # cut at a paragraph under the budget of the contract
