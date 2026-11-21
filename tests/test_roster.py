"""What is left of the roster in v0.14 (``app/storage/roster.py``): the rules for a player's name and for a text that goes into a file, the reading of
a v0.13 memory file and roster directory - the input of the migration to bots (H45) - and the record tally.

The roster itself (a store that wrote ``<data>/roster/``, the review merge, the persona rendering, the prompt memory) is gone: v0.14 keeps the players
as bots.  Its tests went with it; ``tests/test_bots_*.py`` cover the bots (names: ``test_bots_store.py`` / ``test_bots_api.py``; the memory parts and
their limits: ``test_bots_files.py``; the review: ``test_learning_review.py``) and ``tests/test_bots_migrate.py`` covers what the migration makes of
what is read here.  What is tested here is what the migration *reads*: it is read-only, lenient and never loses what the old file said.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.storage.roster import (
    DEFAULT_NAMES,
    EXPERIENCE_CHARS,
    EXPERIENCE_ITEMS,
    IMPRESSION_CHARS,
    LEGACY_CHARS,
    MAX_NAME_CHARS,
    MEMORY_CHARS,
    PERSONA_CHARS,
    MemoryContext,
    MemoryParts,
    RecordTally,
    RosterError,
    check_text,
    clean_experience,
    clean_impression,
    clean_name,
    default_name,
    fit_memory,
    memory_chars,
    parse_impression_lines,
    parse_legacy_persona,
    parse_memory,
    read_legacy_roster,
    seat_id,
    split_memory,
)

IDS = tuple(f"N{i}" for i in range(1, 13))
NAMES = dict(zip(IDS, DEFAULT_NAMES))
CTX4 = MemoryContext("N3", ("N1", "N2", "N3", "N4"), {"N1": "Alice", "N2": "Bob", "N3": "Carol", "N4": "David"})
BOM = "\ufeff"
RECORD3 = "场次 3，胜 2，负 1，胜率 67%\n- 狼人：2 场，胜 1\n- 平民：1 场，胜 1"
#: a memory file as v0.13.1 wrote it
NEW_FORMAT = "## 战绩\n" + RECORD3 + "\n\n## 印象\n- N1 Alice：发言短\n- N2 Bob：（暂无）\n- N4 David：（暂无）\n\n## 经验\n- 先听后说\n"
#: ... and as v0.13.0 wrote it (a story per game, free text)
V0130 = (
    "## 战绩\n" + RECORD3 + "\n\n"
    "## 对局记忆\n第一局：我太保守。\n\n第二局：被带偏了。\n\n第三局：" + "字" * 700 + "\n\n"
    "## 经验与印象\nBob 爱撒谎。\n"
)
CANON4 = (
    "## 战绩\n场次 7，胜 4，负 3，胜率 57%\n- 狼人：3 场，胜 2\n\n"
    "## 印象\n- N1 Alice：发言短，站边快，常被狼利用；可信度中。\n- N2 Bob：（暂无）\n- N4 David：逻辑细，抓细节准。\n\n"
    "## 经验\n- 第一天别急着站边，先听完警上发言。\n- 女巫第一晚救人收益高。\n\n"
    "## 旧记忆（待整理）\n第一局：太保守。"
)


# ------------------------------------------------------------------------------------------------- names
def test_default_names_have_no_digits_and_are_unique():
    assert len(set(DEFAULT_NAMES)) == 12 and not any(ch.isdigit() for name in DEFAULT_NAMES for ch in name)
    assert default_name(13) == "玩家13"


def test_seats_and_their_default_names():
    assert [seat_id(i) for i in (1, 3, 12)] == ["N1", "N3", "N12"]
    assert [default_name(i) for i in (1, 2, 12)] == ["Alice", "Bob", "Leo"] and default_name(13) == "玩家13" and default_name(26) == "玩家26"


@pytest.mark.parametrize(
    "bad, message",
    [
        ("", "不能为空"),
        ("   ", "不能为空"),
        (None, "不能为空"),
        ("x" * (MAX_NAME_CHARS + 1), "最多 20 个字符"),
        ("a<b", "控制字符、引号、反斜杠或尖括号"),
        ('say "hi"', "控制字符、引号、反斜杠或尖括号"),
        ("back\\slash", "控制字符、引号、反斜杠或尖括号"),
        ("tab\there", "控制字符、引号、反斜杠或尖括号"),
        ("Zed\x00", "控制字符"),
        ("Zed\ud800", "无法保存"),
    ],
)
def test_bad_names_are_refused_with_a_reason(bad, message):
    with pytest.raises(RosterError, match=message):
        clean_name(bad)


def test_clean_name_strips_and_returns_the_text():
    assert clean_name("  Zoë  ") == "Zoë" and clean_name("小红") == "小红" and clean_name("x" * MAX_NAME_CHARS) == "x" * MAX_NAME_CHARS


@pytest.mark.parametrize("bad", ["A:B", "A：B", ":", "：Zed", "Zed:", "x : y"])
def test_a_new_name_cannot_contain_a_colon_of_either_kind(bad):
    """The name is the label in front of an impression (``- N5 Eve：…``): a colon in it would end the label early when the line is read back."""
    with pytest.raises(RosterError, match="冒号"):
        clean_name(bad)
    assert clean_name(bad, allow_colon=True) == bad.strip()  # (how a name that is already stored is read)


def test_a_text_that_cannot_be_written_is_refused_with_a_plain_message():
    for text in ("ok\ud800", "\udfff", "a\ud83d"):
        with pytest.raises(RosterError, match="无法保存"):
            check_text(text, "测试")
    with pytest.raises(RosterError, match="测试不能包含空字符"):
        check_text("a\x00b", "测试")
    check_text("正常的文字 😀", "测试")  # a pair of surrogates is an ordinary character
    check_text("", "测试")


# --------------------------------------------------------------------------------------- a memory file
def test_the_canonical_memory_text_is_parsed_part_by_part():
    parts = parse_memory(CANON4, CTX4)
    assert parts.record == "场次 7，胜 4，负 3，胜率 57%\n- 狼人：3 场，胜 2"  # the record is kept word for word
    assert parts.impressions == {"N1": "发言短，站边快，常被狼利用；可信度中。", "N2": "", "N4": "逻辑细，抓细节准。"}
    assert parts.experience == ["第一天别急着站边，先听完警上发言。", "女巫第一晚救人收益高。"]
    assert parts.legacy == "第一局：太保守。" and not parts.empty
    assert fit_memory(parse_memory(CANON4, CTX4), CTX4) == parts  # nothing in it is over a limit
    assert MemoryParts().empty and parse_memory("", CTX4).empty


def test_parsing_is_lenient_one_row_per_other_seat_in_seat_order():
    messy = (
        "## 印象\n"
        "N4 David: 冒号是英文的\n"  # no bullet, an ASCII colon
        "* N1：没有名字\n"  # another bullet, no name
        "1. N2 Bob：编号列表\n"
        "- N3 Carol：这是自己，被忽略\n"
        "- N9 Ghost：不存在的座位，被丢弃\n"
        "- N1 Alice：同一个座位写了两行，后面的算数\n\n"
        "## 经验\n* 一条\n• 两条\n3) 三条\n"
    )
    parts = parse_memory(messy, CTX4)
    assert list(parts.impressions) == ["N1", "N2", "N4"]  # the others, by seat number: not the owner, not N9
    assert parts.impressions == {"N1": "同一个座位写了两行，后面的算数", "N2": "编号列表", "N4": "冒号是英文的"}
    assert parts.experience == ["一条", "两条", "三条"]
    placeholder = parse_memory("## 印象\n- N1 Alice：（暂无）\n- N2 Bob：(暂无)\n- N4 David：暂无", CTX4)
    assert placeholder.impressions == {"N1": "", "N2": "", "N4": ""} and placeholder.empty  # the placeholder and "nothing" are the same thing


def test_a_name_is_only_a_label_a_stored_name_with_a_colon_is_read_and_a_wrapped_line_continues_its_impression():
    ctx = MemoryContext("N3", CTX4.ids, {"N1": "Re:zero", "N2": "Bob", "N3": "Carol", "N4": "David"})
    # (a label written with the name as it was: the colon of the name does not end the label)
    parts = parse_memory("## 印象\n- N1 Re:zero：爱带节奏\n- N2 Bob：发言很短，\n  站边很快。\n- N4：没有名字也行", ctx)
    assert parts.impressions == {"N1": "爱带节奏", "N2": "发言很短， 站边很快。", "N4": "没有名字也行"}
    renamed = MemoryContext("N3", CTX4.ids, {**ctx.names, "N1": "Zed"})
    # (the key is the seat and the name only a label: a label written WITHOUT a colon reads back after any rename ...)
    for label in ("Re zero", "Zed", "老名字", ""):
        assert parse_memory(f"## 印象\n- N1 {label}：爱带节奏".replace("N1 ：", "N1："), renamed).impressions["N1"] == "爱带节奏", label
    assert parse_impression_lines("- N1 Zed：爱带节奏", renamed) == {"N1": "爱带节奏"}
    # ... but a label as an earlier build wrote it, with the colon of the name in it, can only be told while that is still the name
    line = "## 印象\n- N1 Re:zero：爱带节奏"
    assert parse_memory(line, ctx).impressions["N1"] == "爱带节奏"
    assert parse_memory(line, renamed).impressions["N1"] == "zero：爱带节奏"


def test_a_memory_text_is_split_by_its_headings():
    text = "开头的话\n\n## 战绩\n场次 1，胜 1\n\n## 印象\n- N1 Alice：x\n\n## 经验\n"
    sections = split_memory(text)
    assert list(sections) == ["", "战绩", "印象", "经验"] and sections[""] == "开头的话" and sections["经验"] == ""
    assert split_memory("") == {} and split_memory("只是一句手写的话") == {"": "只是一句手写的话"}


def test_a_byte_order_mark_in_front_of_a_text_is_not_part_of_it():
    assert split_memory(BOM + "## 战绩\n场次 1，胜 1\n\n## 印象\n- N1 Alice：x") == {"战绩": "场次 1，胜 1", "印象": "- N1 Alice：x"}
    assert parse_memory(BOM + NEW_FORMAT, CTX4).record == RECORD3


@pytest.mark.parametrize(
    "old, record, legacy",
    [
        ("只是一句手写的话", "", "只是一句手写的话"),  # no headings at all
        ("开头文字\n\n## 战绩\n场次 1，胜 1", "场次 1，胜 1", "开头文字"),
        ("## 战绩\n- 赢过一次\n\n## 我的备注\ny", "- 赢过一次", "【我的备注】\ny"),  # any other section is kept, with its title
        ("## 经验与印象\n- 谨慎\n", "", "- 谨慎"),
        ("## 战绩\n场次 1，胜 1\n", "场次 1，胜 1", ""),  # a record only: nothing to tidy up
    ],
)
def test_an_old_file_loses_nothing_what_is_not_the_record_the_impressions_or_the_lessons_is_the_legacy_text(old, record, legacy):
    parts = parse_memory(old, CTX4)
    assert (parts.record, parts.legacy) == (record, legacy)
    assert parts.experience == [] and not any(parts.impressions.values())


def test_the_whole_of_a_memory_is_cut_to_fit_longest_texts_first():
    ctx = MemoryContext("N3", IDS, {sid: "名" * 60 for sid in IDS})  # (the longest names are 20: this is the extreme)
    parts = MemoryParts(impressions={sid: "字" * 80 for sid in ctx.others}, experience=["经" * 100] * 3)
    parts.impressions["N1"] = "短"
    assert memory_chars(parts, ctx) > MEMORY_CHARS
    fitted = fit_memory(parts, ctx)
    assert memory_chars(fitted, ctx) <= MEMORY_CHARS
    assert fitted.impressions["N1"] == "短"  # a short one stays whole
    lengths = [len(t) for sid, t in fitted.impressions.items() if sid != "N1"] + [len(t) for t in fitted.experience]
    assert max(lengths) - min(lengths) <= 1 and max(lengths) < 80  # the long ones lost evenly
    assert parts.impressions["N2"] == "字" * 80  # the input is not touched


def test_a_file_over_the_limits_is_cut_without_an_error():
    ctx = MemoryContext("N3", IDS, dict(NAMES))
    impressions = "\n".join(f"- N{i} {NAMES[f'N{i}']}：" + "字" * 200 for i in (1, 2, 4))
    lessons = "\n".join(f"- 经验{i}：" + "字" * 200 for i in range(1, 6))
    parts = fit_memory(parse_memory(f"## 印象\n{impressions}\n\n## 经验\n{lessons}\n\n## 旧记忆（待整理）\n" + "旧" * 900, ctx), ctx)
    assert all(len(text) <= IMPRESSION_CHARS for text in parts.impressions.values())
    assert parts.impressions["N1"].endswith("…") and len(parts.impressions["N1"]) == IMPRESSION_CHARS  # a cut text says so
    assert 1 <= len(parts.experience) <= EXPERIENCE_ITEMS and sum(len(i) for i in parts.experience) <= EXPERIENCE_CHARS
    assert len(parts.legacy) <= LEGACY_CHARS
    assert fit_memory(parse_memory("## 经验\n" + "\n".join(f"- 条{i}" for i in range(1, 6)), ctx), ctx).experience == ["条1", "条2", "条3"]  # at most three lessons


def test_impressions_and_lessons_are_cleaned_to_one_line_texts():
    assert [clean_impression(x) for x in ("（暂无）", "暂无", None, "a\nb", "**粗体**说话")] == ["", "", "", "a b", "**粗体**说话"]
    assert clean_experience(None) == [] and clean_experience(5) == [] and clean_experience({"a": "b"}) == []
    assert clean_experience("一条\n- 两条\n\n") == ["一条", "两条"]


def test_only_strings_are_lessons_never_the_text_of_something_else():
    assert clean_experience(["先听后说", None, 5, {"a": 1}, ["x"], True, 1.5, "", "（暂无）", "- 别太早表态"]) == ["先听后说", "别太早表态"]


# ------------------------------------------------------------------------------------------- the record
def test_the_record_tally_counts_games_wins_and_per_role_wins():
    tally = RecordTally()
    tally.add("狼人", True)
    tally.add("狼人", False)
    tally.add("预言家", True)
    text = tally.render()
    assert text.splitlines()[0] == "场次 3，胜 2，负 1，胜率 67%"
    assert "- 狼人：2 场，胜 1" in text and "- 预言家：1 场，胜 1" in text
    again = RecordTally.parse(text)
    assert (again.games, again.wins, again.by_role) == (3, 2, {"狼人": [2, 1], "预言家": [1, 1]})
    assert RecordTally().render().startswith("场次 0，胜 0，负 0，胜率 —")


def test_a_record_that_cannot_be_read_is_an_empty_one():
    for garbage in ("", "乱七八糟", "场次 x，胜 y", "- 狼人：多少场"):
        empty = RecordTally.parse(garbage)
        assert (empty.games, empty.wins, empty.by_role) == (0, 0, {}), garbage


# ------------------------------------------------------------------- a roster directory (what the migration reads)
def write(folder: Path, name: str, text: str, *, newline: str = "\n", bom: bool = False) -> Path:
    """A file as an editor writes it (maybe with a byte-order mark, maybe with CRLF)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes((BOM if bom else "").encode("utf-8") + text.replace("\n", newline).encode("utf-8"))
    return path


def test_a_missing_or_empty_roster_is_twelve_default_players(tmp_path):
    for folder in (tmp_path / "nothing-here", tmp_path):
        legacy = read_legacy_roster(folder)
        assert [s.id for s in legacy.seats] == list(IDS) and [s.name for s in legacy.seats] == DEFAULT_NAMES
        assert legacy.memory_enabled is True and legacy.moderator_avatar is None and legacy.names == NAMES
        assert all(s.persona == "" and s.avatar is None and s.parts.empty for s in legacy.seats)
    assert [s.name for s in read_legacy_roster(tmp_path, 3).seats] == ["Alice", "Bob", "Carol"]
    assert not list(tmp_path.iterdir()), "reading never writes"


def test_a_roster_is_read_as_it_is_stored(tmp_path):
    raw = {
        "memory_enabled": False,
        "seats": {"N1": {"name": "<script>"}, "N2": {"name": "A：B"}, "N3": {"name": "Zed", "avatar": {"v": 1, "face": "f01"}}, "N4": "junk"},
        "moderator": {"avatar": {"v": 1, "face": "f07"}},
    }
    write(tmp_path, "roster.json", json.dumps(raw, ensure_ascii=False))
    legacy = read_legacy_roster(tmp_path, 4)
    assert [(s.name, s.avatar) for s in legacy.seats] == [
        ("Alice", None),  # a name that is not allowed falls back to the default
        ("A：B", None),  # a stored name with a colon is shown as it is stored
        ("Zed", {"v": 1, "face": "f01"}),
        ("David", None),
    ]
    assert legacy.memory_enabled is False and legacy.moderator_avatar == {"v": 1, "face": "f07"}


@pytest.mark.parametrize("content", ["not json", "[]", "7", '"x"', "null", "{"])
def test_a_roster_file_that_cannot_be_used_gives_the_defaults(tmp_path, content):
    write(tmp_path, "roster.json", content)
    legacy = read_legacy_roster(tmp_path, 3)
    assert [s.name for s in legacy.seats] == ["Alice", "Bob", "Carol"] and legacy.memory_enabled is True


def test_the_persona_and_the_roster_file_may_have_a_byte_order_mark_too(tmp_path):
    write(tmp_path, "N1.persona.md", "冷静的分析派\n", newline="\r\n", bom=True)
    write(tmp_path, "roster.json", json.dumps({"seats": {"N2": {"name": "Zed"}}}, ensure_ascii=False), bom=True)
    legacy = read_legacy_roster(tmp_path, 3)
    assert legacy.seats[0].persona == "冷静的分析派" and legacy.seats[1].name == "Zed"


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["LF", "CRLF"])
@pytest.mark.parametrize("text", [NEW_FORMAT, V0130], ids=["new format", "v0.13.0"])
def test_a_memory_file_with_a_byte_order_mark_keeps_its_record(tmp_path, text, newline):
    path = write(tmp_path, "N3.memory.md", text, newline=newline, bom=True)
    on_disk = path.read_bytes()
    assert on_disk.startswith(b"\xef\xbb\xbf")
    parts = read_legacy_roster(tmp_path).seats[2].parts
    assert parts.record == RECORD3  # the mark did not turn "## 战绩" into plain text: the record is found
    assert "战绩" not in parts.legacy and "场次" not in parts.legacy  # ... and it did not end up in the legacy text either
    assert path.read_bytes() == on_disk  # reading does not rewrite the file


def test_a_v0130_file_is_read_without_a_model_without_losing_the_record_or_failing(tmp_path):
    path = write(tmp_path, "N3.memory.md", V0130)
    on_disk = path.read_bytes()
    parts = read_legacy_roster(tmp_path).seats[2].parts
    assert parts.record == RECORD3  # word for word
    assert parts.experience == [] and not any(parts.impressions.values()) and set(parts.impressions) == set(IDS) - {"N3"}
    assert 0 < len(parts.legacy) <= LEGACY_CHARS and parts.legacy.startswith("…") and parts.legacy.endswith("Bob 爱撒谎。")  # the newer text is kept
    assert "第一局" not in parts.legacy and "第三局" not in parts.legacy  # ... the oldest goes
    assert path.read_bytes() == on_disk


def test_the_impressions_of_a_v0131_file_are_read_by_seat_and_the_name_is_only_a_label(tmp_path):
    names = {"seats": {"N1": {"name": "Re:zero"}, "N2": {"name": "Bobby"}}}
    write(tmp_path, "roster.json", json.dumps(names, ensure_ascii=False))
    write(tmp_path, "N3.memory.md", "## 印象\n- N1 Re:zero：爱带节奏\n- N2 Bob：旧名字也认得\n- N4：没有名字也行\n- N3：自己\n- N77：鬼\n\n## 经验\n- 先听后说\n")
    parts = read_legacy_roster(tmp_path, 12).seats[2].parts
    assert parts.impressions["N1"] == "爱带节奏" and parts.impressions["N2"] == "旧名字也认得" and parts.impressions["N4"] == "没有名字也行"
    assert "N3" not in parts.impressions and "N77" not in parts.impressions and parts.experience == ["先听后说"]


def test_a_file_over_the_limits_opens_cut_without_an_error(tmp_path):
    impressions = "\n".join(f"- N{i} {NAMES[f'N{i}']}：" + "字" * 200 for i in (1, 2, 4))
    lessons = "\n".join(f"- 经验{i}：" + "字" * 200 for i in range(1, 6))
    write(tmp_path, "N3.memory.md", f"## 印象\n{impressions}\n\n## 经验\n{lessons}\n\n## 旧记忆（待整理）\n" + "旧" * 900)
    parts = read_legacy_roster(tmp_path).seats[2].parts
    assert all(len(text) <= IMPRESSION_CHARS for text in parts.impressions.values()) and len(parts.legacy) <= LEGACY_CHARS
    assert 1 <= len(parts.experience) <= EXPERIENCE_ITEMS and memory_chars(parts, MemoryContext("N3", IDS, dict(NAMES))) <= MEMORY_CHARS


def test_a_file_that_cannot_be_read_is_an_empty_memory(tmp_path):
    (tmp_path / "N1.memory.md").write_bytes(b"\xff\xfe\x00 not utf-8 \x80")
    (tmp_path / "N2.persona.md").mkdir()  # (a folder where a file should be)
    legacy = read_legacy_roster(tmp_path, 3)
    assert legacy.seats[0].parts.empty and legacy.seats[1].persona == ""


def test_the_persona_is_read_whole_and_split_by_its_headings(tmp_path):
    persona = "【定位】冷静的逻辑派。\n【说话风格】\n- 句子短。\n- 少用感叹号。\n【口头禅】我们看票。"
    write(tmp_path, "N1.persona.md", persona + "\n")
    assert read_legacy_roster(tmp_path, 1).seats[0].persona == persona
    assert parse_legacy_persona(persona) == {"定位": ["冷静的逻辑派。"], "说话风格": ["句子短。", "少用感叹号。"], "口头禅": ["我们看票。"]}
    assert PERSONA_CHARS == 500  # (the limit v0.13.1 put on a persona: ``app.agents.brain`` cuts an old, longer one to it - a test there keeps the two equal)
