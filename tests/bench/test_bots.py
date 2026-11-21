"""``bench.bots``: the bots a run tries - the v0.13.1 baseline, v0.14 bots from the default library or a directory, and the layers that can be taken away."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from bench_helpers import LIBRARY_VOICE, example, make_library

from app.agents.legacy import LegacyBrain
from app.agents.mind import BotBrain
from app.bots.types import BotMind, CoreFiles
from bench.bots import (
    ABLATIONS,
    NEUTRAL_CORE,
    BotSpecError,
    _bot_id,
    ablate,
    bot_from_definition,
    check_playbook_text,
    check_spec_text,
    default_names,
    expand_bot_specs,
    legacy_bot,
    library_dir,
    load_bot_mind,
    make_bots,
    mind_bot,
    mind_from_definition,
    parse_bot_spec,
    pinned_bots,
    split_spec,
    split_spec_list,
)
from bench.play import build_scene
from bench.situation import parse_situation

VOICE = LIBRARY_VOICE


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    return make_library(tmp_path / "library")


# ------------------------------------------------------------------------------------------------ the v0.13.1 baseline


def test_the_baseline_is_the_v0_13_1_player_and_keeps_the_scripts_name_unless_given_one() -> None:
    bot = parse_bot_spec("legacy")
    assert (bot.label, bot.kind, bot.brain, bot.name, bot.mind, bot.memory_enabled) == ("legacy", "legacy", "LegacyBrain", "", None, True)
    assert bot.voice == "" and bot.oral_lists is False  # (the baseline has no voice: no length band to measure against; the gate's list rule is on for it)
    named = parse_bot_spec("legacy:Alice")
    assert named.label == "legacy:Alice" and named.name == "Alice" and named.describe() == {
        "label": "legacy:Alice", "kind": "legacy", "name": "Alice", "brain": "LegacyBrain", "memory_enabled": True, "options": [],
    }
    scene = build_scene(example("speak"), seat_name=named.name)
    brain = named.make_brain("N3", scene.world, scene.spec)
    assert isinstance(brain, LegacyBrain) and "你是 **N3 Alice**" in brain.system_prompt("N3") and "（没有特别设定，按你自己的判断行事。）" in brain.system_prompt("N3")
    assert "寡言" in legacy_bot("X", persona="寡言").make_brain("N3", scene.world, scene.spec).system_prompt("N3")  # (the roster texts of that version, when given)
    assert "上局输了" in legacy_bot(memory="上局输了").make_brain("N3", scene.world, scene.spec).system_prompt("N3")


def test_the_baselines_style_card_says_it_has_no_character() -> None:
    assert "v0.13.1 的默认玩家：没有设定性格" in parse_bot_spec("legacy:Alice").card and "v0.13.1" in parse_bot_spec("legacy").card


# ------------------------------------------------------------------------------------------------ v0.14 bots from a directory


def test_a_bot_directory_gives_the_snapshot_a_game_would_start_with(library: Path) -> None:
    mind = load_bot_mind("Zed", scenario_id="minimal_test", library=library)
    assert isinstance(mind, BotMind) and mind.name == "Zed" and mind.bot_id.startswith("b-") and len(mind.bot_id) == 10
    assert (mind.core.identity, mind.core.voice, mind.core.mind) == ("一个爱列点的老玩家。", VOICE, "先看事实。")
    assert mind.arch_notes == "我的操作笔记：发言前先看状态板。" and mind.rules.scenario_id == "minimal_test" and mind.rules.digest == "我理解这个游戏要看事实。"
    assert [(e.text, e.confidence, e.support, e.tags) for e in mind.rules.playbook] == [("先看规则给出的结果。", "高", 3, ("发言",)), ("不太确定的想法。", "低", 0, ())]
    assert mind.rules.questions == "平局怎么处理还不确定。" and mind.people == {"Alice": "话少，爱反问", "Bob": "话多"} and "红队" in mind.record
    other_scenario = load_bot_mind("Zed", scenario_id="werewolf_12p", library=library)
    assert other_scenario.rules.digest == "" and other_scenario.rules.playbook == () and other_scenario.arch_notes == mind.arch_notes  # (the rules memory is per scenario)
    plain = load_bot_mind("Zed", scenario_id="minimal_test", memory=False, library=library)
    assert plain.core == mind.core and plain.arch_notes == "" and plain.rules.digest == "" and plain.people == {} and plain.record == ""
    assert load_bot_mind("Yan", scenario_id="minimal_test", library=library).rules.playbook == ()  # (a bot that was never initialized: L0 only)
    assert load_bot_mind("Yan", library=library).name == "Yan"  # (no bot.json: the directory's name)


def test_a_bot_is_found_by_name_in_the_library_or_by_a_directory(library: Path, tmp_path: Path) -> None:
    assert load_bot_mind("zed", library=library).name == "Zed"  # (not case sensitive)
    assert load_bot_mind(str(library / "Yan")).name == "Yan"  # (a directory)
    for ref in ("Nobody", str(tmp_path / "nowhere")):
        with pytest.raises(BotSpecError, match="找不到 bot"):
            load_bot_mind(ref, library=library)
    incomplete = tmp_path / "Half"
    (incomplete / "core").mkdir(parents=True)
    (incomplete / "core" / "identity.md").write_text("只有身份。", encoding="utf-8")
    with pytest.raises(BotSpecError, match=r"底层设定不完整（缺少：voice、mind）"):
        load_bot_mind(str(incomplete))


def test_a_v0_14_bot_is_a_bot_brain_with_its_memory_mounted(library: Path) -> None:
    bot = parse_bot_spec("bot:Zed", scenario_id="minimal_test", library=library)
    assert (bot.label, bot.kind, bot.brain, bot.name, bot.memory_enabled) == ("bot:Zed", "bot", "BotBrain", "Zed", True) and bot.oral_lists is True
    scene = build_scene(example("speak"), seat_name=bot.name)
    brain = bot.make_brain("N3", scene.world, scene.spec)
    assert isinstance(brain, BotBrain)
    system = brain.system_prompt("N3")
    assert system.startswith("你是 Zed（本局编号 N3）。一个爱列点的老玩家。") and "我理解这个游戏要看事实。" in system and "先看规则给出的结果" in system
    assert "Alice：话少，爱反问。" in system and "不太确定的想法" not in system  # (low confidence is not mounted: it comes up through the hints only)
    desc = bot.describe()
    assert desc["playbook_entries"] == 2 and desc["people"] == 2 and len(desc["core_sha"]) == 12 and "老玩家" not in json.dumps(desc, ensure_ascii=False)
    assert bot.card == f"Zed\n身份：一个爱列点的老玩家。\n说话方式：{VOICE}"


# ------------------------------------------------------------------------------------------------ taking a layer away (M1)


def test_each_ablation_removes_exactly_its_layer(library: Path) -> None:
    full = load_bot_mind("Zed", scenario_id="minimal_test", library=library)
    assert ablate(full, ()) == full and ablate(full, ("nomem",)) == full  # (``nomem`` is the brain's switch, not a change of the mind)
    assert ablate(full, ("no-notes",)).arch_notes == "" and ablate(full, ("no-notes",)).rules == full.rules
    assert ablate(full, ("no-digest",)).rules.digest == "" and ablate(full, ("no-digest",)).rules.playbook == full.rules.playbook
    assert ablate(full, ("no-playbook",)).rules.playbook == () and ablate(full, ("no-playbook",)).rules.digest == full.rules.digest
    assert ablate(full, ("no-questions",)).rules.questions == "" and ablate(full, ("no-people",)).people == {} and ablate(full, ("no-record",)).record == ""
    blank = ablate(full, ("blank-core",))
    assert blank.core == NEUTRAL_CORE and blank.name == "Zed" and blank.arch_notes == full.arch_notes  # (the L0 is replaced, the memory stays)
    both = ablate(full, ("no-playbook", "no-people"))
    assert both.rules.playbook == () and both.people == {} and both.arch_notes == full.arch_notes
    assert set(ABLATIONS) == {"nomem", "no-notes", "no-digest", "no-playbook", "no-questions", "no-people", "no-record", "blank-core"}
    assert NEUTRAL_CORE.complete and "玩家" in NEUTRAL_CORE.identity


def test_an_ablated_bot_shows_the_model_a_different_prompt(library: Path) -> None:
    scene = build_scene(example("speak"), seat_name="Zed")

    def system(spec: str) -> str:
        return parse_bot_spec(spec, scenario_id="minimal_test", library=library).make_brain("N3", scene.world, scene.spec).system_prompt("N3")

    full = system("bot:Zed")
    assert "我理解这个游戏要看事实" in full and "我的操作笔记" in full and "Alice：" in full
    nomem = system("bot:Zed@nomem")
    assert "我理解这个游戏要看事实" not in nomem and "我的操作笔记" not in nomem and "Alice：" not in nomem and "一个爱列点的老玩家" in nomem  # (L0 + contract + rules)
    assert "先看规则给出的结果" not in system("bot:Zed@no-playbook") and "我理解这个游戏要看事实" in system("bot:Zed@no-playbook")
    assert "Alice：" not in system("bot:Zed@no-people") and "我的操作笔记" not in system("bot:Zed@no-notes")
    blank = system("bot:Zed@blank-core")
    assert "一个爱列点的老玩家" not in blank and "一名普通的玩家。" in blank and "我理解这个游戏要看事实" in blank
    assert len({system(s) for s in ("bot:Zed", "bot:Zed@nomem", "bot:Zed@no-playbook", "bot:Zed@no-people", "bot:Zed@no-notes", "bot:Zed@blank-core")}) == 6


# ------------------------------------------------------------------------------------------------ the spec strings


def test_a_spec_string_is_a_kind_a_reference_and_options() -> None:
    assert split_spec("bot:Alice@nomem,no-people") == ("bot", "Alice", ("nomem", "no-people")) and split_spec("legacy") == ("legacy", "", ())
    assert split_spec(" Bot : Alice ") == ("bot", "Alice", ()) and split_spec("bot:C:\\x\\y") == ("bot", "C:\\x\\y", ())


def test_a_list_of_specs_is_split_at_commas_except_the_commas_between_options() -> None:
    assert split_spec_list("legacy,bot:Alice@nomem,no-people,bot:Bob") == ["legacy", "bot:Alice@nomem,no-people", "bot:Bob"]
    assert split_spec_list("bot:Alice@no-notes,no-digest,blank-core") == ["bot:Alice@no-notes,no-digest,blank-core"]
    assert split_spec_list(" legacy , , bot:Alice ") == ["legacy", "bot:Alice"] and split_spec_list("") == []
    assert split_spec_list("legacy,no-people") == ["legacy", "no-people"]  # (an option name after a spec with no @ is not an option: it fails later, by name)
    assert split_spec_list("bot:A@nomem,default12") == ["bot:A@nomem", "default12"]  # (a group name is never an option)
    assert expand_bot_specs(["bot:A@nomem,no-people,bot:B", "legacy"]) == ["bot:A@nomem,no-people", "bot:B", "legacy"]
    with pytest.raises(BotSpecError, match="bot 的种类只能是 legacy 或 bot（现在是 'no-people'）"):
        parse_bot_spec("no-people")


@pytest.mark.parametrize(
    ("spec", "text"),
    [
        ("alice", "bot 的种类只能是 legacy 或 bot"),
        ("bot:", "bot: 后面要写默认库里的名字或一个目录"),
        ("bot:Zed@sparkles", "不认识的选项 sparkles"),
        ("legacy@nomem", "legacy 没有可以拿掉的层"),
        ("bot:Nobody", "找不到 bot 'Nobody'"),
    ],
)
def test_a_wrong_spec_says_what_is_wrong(spec: str, text: str, library: Path) -> None:
    with pytest.raises(BotSpecError, match=text):
        parse_bot_spec(spec, scenario_id="minimal_test", library=library)


def test_groups_expand_to_the_twelve_default_names_and_duplicates_are_refused(library: Path) -> None:
    assert expand_bot_specs(["legacy,bot:Zed", "bot:Yan"], library=library) == ["legacy", "bot:Zed", "bot:Yan"]
    twelve = expand_bot_specs(["default12"])
    assert twelve == [f"bot:{n}" for n in ("Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo")]
    assert expand_bot_specs(["legacy12"])[:2] == ["legacy:Alice", "legacy:Bob"] and len(expand_bot_specs(["legacy12"])) == 12
    assert default_names(library) == [] and default_names() == [s.split(":")[1] for s in twelve] and library_dir().name == "bots"  # (the names that are in DEFAULT_NAMES)
    with pytest.raises(BotSpecError, match="重复的 bot：bot:Zed"):
        expand_bot_specs(["bot:Zed", "bot:Zed"])
    with pytest.raises(BotSpecError, match="默认库里一个 bot 也没有"):
        expand_bot_specs(["default12"], library=library)


def test_the_shipped_default_library_gives_twelve_complete_bots() -> None:
    bots = make_bots(["default12"], scenario_id="minimal_test")
    assert len(bots) == 12 and len({b.name for b in bots}) == 12 and all(isinstance(b.mind, BotMind) and b.mind.core.complete for b in bots)
    assert len({b.card for b in bots}) == 12  # (a judge has twelve different cards to match speeches against: S2)
    scene = build_scene(example("speak"))
    assert len({b.make_brain("N3", scene.world, scene.spec).system_prompt("N3") for b in bots}) == 12


def test_make_bots_reads_the_contract_once_and_a_ready_mind_can_be_wrapped() -> None:
    bots = make_bots(["bot:Alice", "bot:Bob"], scenario_id="minimal_test")
    assert len(bots) == 2
    mind = BotMind(bot_id="b-00000001", name="Cy", core=CoreFiles(identity="话少。", voice="短。篇幅：10–50 字", mind="先看事实。"))
    wrapped = mind_bot(mind, label="mine", memory_enabled=False, options=("nomem",))
    assert (wrapped.label, wrapped.name, wrapped.memory_enabled, wrapped.options, wrapped.voice) == ("mine", "Cy", False, ("nomem",), "短。篇幅：10–50 字")
    assert mind_bot(mind).label == "bot:Cy"


# ------------------------------------------------------------------------------------------------ the bots a situation file brings

DEFINITION = {
    "name": "Zed",
    "core": {"identity": "一个爱列点的老玩家。", "voice": "你说话短。\n篇幅：30–90 字", "mind": "先看事实。"},
    "notes": "发言前先看状态板。", "digest": "平票时再投一次。", "questions": "夜里的规则还不确定。", "record": "场次 3 · 胜 1",
    "playbook": "- [把握:高|支持:3|反例:0|来源:复盘|#投票] 先看规则给出的结果。\n- [把握:低|支持:0|反例:0|来源:初始化] 不太确定的想法。",
    "people": {"Alice": "话少，爱反问"},
}


def test_a_snapshot_written_in_a_situation_is_the_same_mind_a_game_would_start_with() -> None:
    bot = bot_from_definition(DEFINITION, scenario_id="minimal_test")
    assert (bot.label, bot.kind, bot.brain, bot.name, bot.memory_enabled, bot.options) == ("bot:Zed", "bot", "BotBrain", "Zed", True, ())
    mind = bot.mind
    assert isinstance(mind, BotMind) and mind.core == CoreFiles(identity="一个爱列点的老玩家。", voice="你说话短。\n篇幅：30–90 字", mind="先看事实。")
    assert (mind.arch_notes, mind.rules.scenario_id, mind.rules.digest, mind.rules.questions, mind.record) == ("发言前先看状态板。", "minimal_test", "平票时再投一次。", "夜里的规则还不确定。", "场次 3 · 胜 1")
    assert [(e.text, e.confidence, e.support, e.counter, e.source, e.tags) for e in mind.rules.playbook] == [
        ("先看规则给出的结果。", "高", 3, 0, "复盘", ("投票",)), ("不太确定的想法。", "低", 0, 0, "初始化", ())]
    assert mind.people == {"Alice": "话少，爱反问"} and mind.bot_id == _bot_id("Zed") == _bot_id("zed")  # (the same name is the same bot in every run)
    scene = build_scene(example("speak"), seat_name="Zed")
    system = bot.make_brain("N3", scene.world, scene.spec).system_prompt("N3")
    assert system.startswith("你是 Zed（本局编号 N3）。一个爱列点的老玩家。") and "平票时再投一次。" in system and "先看规则给出的结果" in system and "Alice：话少，爱反问。" in system
    assert "不太确定的想法" not in system  # (low confidence is not mounted: it comes up through the hints only)
    assert bot_from_definition({**DEFINITION, "label": "zed-v2"}).label == "zed-v2"
    minimal = bot_from_definition({"name": "Cy", "core": {"identity": "话少。", "voice": "短。", "mind": "先看事实。"}})
    assert minimal.mind.rules.playbook == () and minimal.mind.people == {} and minimal.mind.arch_notes == "" and minimal.mind.record == "" and minimal.mind.rules.scenario_id == ""


def test_a_snapshot_with_a_missing_layer_or_a_line_that_is_no_entry_cannot_be_made() -> None:
    with pytest.raises(BotSpecError, match=r"bot Zed 的底层设定不完整（缺少：mind）"):
        mind_from_definition({"name": "Zed", "core": {"identity": "身份", "voice": "声音"}})
    with pytest.raises(BotSpecError, match="底层设定不完整"):
        mind_from_definition({"core": DEFINITION["core"]})
    with pytest.raises(BotSpecError, match="playbook 里有 1 行不是打法条目"):
        mind_from_definition({**DEFINITION, "playbook": "说明文字，不是条目"})
    too_many = "\n".join(f"- [把握:低|支持:0|反例:0|来源:初始化] 想法 {i}" for i in range(40))
    with pytest.raises(BotSpecError, match="playbook 有问题：打法最多"):
        mind_from_definition({**DEFINITION, "playbook": too_many})


def test_the_shape_of_a_spec_and_of_a_playbook_can_be_checked_without_loading_anything() -> None:
    check_spec_text("legacy")
    check_spec_text("bot:Alice@nomem,no-people")
    for text, message in (("", "bot 的种类只能是 legacy 或 bot"), ("bot:", "bot: 后面要写"), ("legacy@no-notes", "legacy 没有可以拿掉的层"), ("bot:x@nope", "不认识的选项 nope")):
        with pytest.raises(BotSpecError, match=message):
            check_spec_text(text)
    check_playbook_text("")
    check_playbook_text("## 假设\n- [把握:高|支持:3|反例:0|来源:复盘] 一条。\n- 没有方括号的一条")
    with pytest.raises(BotSpecError):
        check_playbook_text("一行\n两行")


def test_a_spec_in_a_situation_names_a_directory_next_to_the_file_or_a_bot_of_the_library(library: Path, tmp_path: Path) -> None:
    base = tmp_path / "situations"
    shutil.copytree(library / "Zed", base / "minds" / "zed")
    situation = parse_situation({
        "format": "arena-situation", "version": 1, "id": "pinned", "scenario": "minimal_test", "decision": {"seats": ["N3", "N4"]}, "steps": [],
        "bots": {"N3": "bot:./minds/zed@no-people", "N4": "bot:Yan"},
    })
    situation = replace(situation, path=str(base / "pinned.yaml"))
    pinned = pinned_bots(situation, library=library)
    assert list(pinned) == ["N3", "N4"] and pinned["N3"].label == "bot:./minds/zed@no-people" and pinned["N3"].name == "Zed" and pinned["N3"].mind.people == {}
    assert pinned["N3"].mind.rules.digest == "我理解这个游戏要看事实。" and pinned["N3"].mind.arch_notes.startswith("我的操作笔记")  # (its memory came along: the directory is a whole bot)
    assert pinned["N4"].name == "Yan" and pinned["N4"].mind.rules.playbook == ()  # (a name: looked up in the library)
    nowhere = replace(situation, bots={"N3": {"spec": "bot:./minds/nobody"}})
    with pytest.raises(BotSpecError, match=r"pinned（bots.N3）：找不到 bot './minds/nobody'"):
        pinned_bots(nowhere, library=library)
    assert pinned_bots(replace(situation, bots={}), library=library) == {}
