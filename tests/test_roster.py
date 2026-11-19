"""v0.12.1 roster store (E13–E16, E21, E24) and the v0.13.1 memory / persona (G37–G44): names, avatars, the persona
and memory files, the structured memory (record / impressions / lessons / legacy) with its limits, the migration of a
v0.13.0 file, the merge of a review, the fixed-format persona.

(The memory pack - a zip of the persona / memory files - is gone in v0.13: F26.)"""

from __future__ import annotations

import json
import re

import pytest

from app.storage.roster import (
    DEFAULT_NAMES,
    EXPERIENCE_CHARS,
    EXPERIENCE_ITEMS,
    IMPRESSION_CHARS,
    LEGACY_CHARS,
    LIMITS,
    MAX_NAME_CHARS,
    MAX_TEXT_CHARS,
    MEMORY_CHARS,
    PERSONA_CHARS,
    PERSONA_ITEM_CHARS,
    RECORD_PROMPT_CHARS,
    MemoryContext,
    MemoryParts,
    RecordTally,
    ReviewOutput,
    RosterError,
    RosterInputError,
    RosterStore,
    check_memory,
    check_text,
    clean_experience,
    clean_name,
    clean_text,
    default_name,
    fit_memory,
    is_no_change,
    join_memory,
    memory_chars,
    memory_for_prompt,
    merge_review,
    normalize_persona,
    parse_memory,
    parse_review_output,
    persona_for_prompt,
    render_memory,
    split_memory,
)
from app.storage.roster import PROMPT_IMPRESSION_NOTE

IDS = tuple(f"N{i}" for i in range(1, 13))
NAMES = dict(zip(IDS, DEFAULT_NAMES))
CTX4 = MemoryContext("N3", ("N1", "N2", "N3", "N4"), {"N1": "Alice", "N2": "Bob", "N3": "Carol", "N4": "David"})
FENCE = "```"
BOM = "\ufeff"


@pytest.fixture
def store(tmp_path) -> RosterStore:
    return RosterStore(tmp_path)


def ctx12(owner: str = "N3", names: dict[str, str] | None = None) -> MemoryContext:
    return MemoryContext(owner, IDS, dict(NAMES if names is None else names))


def write_memory(store: RosterStore, text: str, seat: str = "N3") -> None:
    store._dir.mkdir(parents=True, exist_ok=True)
    (store._dir / f"{seat}.memory.md").write_text(text, encoding="utf-8")


def memory_file(store: RosterStore, seat: str = "N3") -> str:
    return (store._dir / f"{seat}.memory.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------ defaults
def test_a_fresh_roster_has_the_twelve_default_names_and_empty_texts(store):
    data = store.get()
    assert data["memory_enabled"] is True
    assert [row["id"] for row in data["seats"]] == [f"N{i}" for i in range(1, 13)]
    assert [row["name"] for row in data["seats"]] == DEFAULT_NAMES
    assert all(row["persona"] == "" and row["memory"] == "" and row["avatar"] is None for row in data["seats"])
    assert [row["seat"] for row in data["seats"]] == list(range(1, 13)) and data["seats"][2]["default_name"] == "Carol"
    assert not (store._dir / "roster.json").exists()  # reading never writes


def test_the_limits_of_the_contract_are_handed_to_the_panel_and_every_seat_has_its_memory_parts(store):
    data = store.get()
    assert data["limits"] == {
        "impression_chars": 80, "experience_items": 3, "experience_chars": 300, "memory_chars": 1500,
        "legacy_chars": 600, "persona_chars": 500, "persona_item_chars": 40,
    } == LIMITS
    third = data["seats"][2]
    assert third["memory_parts"] == {
        "record": "", "impressions": {f"N{i}": "" for i in range(1, 13) if i != 3}, "experience": [], "legacy": "",
    }
    assert list(third["memory_parts"]["impressions"]) == [f"N{i}" for i in range(1, 13) if i != 3]  # the others, in seat order
    assert set(third) == {"seat", "id", "name", "default_name", "avatar", "persona", "memory", "memory_parts"}


def test_default_names_have_no_digits_and_are_unique():
    assert len(set(DEFAULT_NAMES)) == 12 and not any(ch.isdigit() for name in DEFAULT_NAMES for ch in name)
    assert default_name(13) == "玩家13"


# ------------------------------------------------------------------------------------ names
def test_a_new_name_persists_across_a_restart(store, tmp_path):
    store.set_name("N3", "  小红  ")
    assert RosterStore(tmp_path).entry("N3")["name"] == "小红"
    assert RosterStore(tmp_path).entry("N1")["name"] == "Alice"  # the others keep their defaults


@pytest.mark.parametrize("bad", ["", "   ", "x" * (MAX_NAME_CHARS + 1), "a<b", 'say "hi"', "back\\slash", "tab\there", None])
def test_bad_names_are_refused_with_a_reason(store, bad):
    with pytest.raises(RosterError):
        store.set_name("N1", bad)
    assert store.entry("N1")["name"] == "Alice"


def test_names_must_be_unique_ignoring_case_and_a_seat_may_keep_its_own(store):
    store.set_name("N1", "Bob2")
    with pytest.raises(RosterError, match="已被 N1 使用"):
        store.set_name("N2", "bob2")
    with pytest.raises(RosterError, match="已被 N2 使用"):
        store.set_name("N1", "Bob")  # Bob is N2's default name
    store.set_name("N1", "Bob2")  # re-saving its own name is fine


def test_unknown_seats_are_rejected_everywhere(store):
    for call in (lambda: store.set_name("N13", "x"), lambda: store.set_persona("p01", "x"), lambda: store.set_memory("moderator", "x"),
                 lambda: store.set_avatar("N0", {"v": 1}), lambda: store.entry("ghost")):
        with pytest.raises(RosterError, match="没有这个座位"):
            call()


def test_a_hand_edited_roster_file_with_a_broken_name_falls_back_to_the_default(store):
    store._dir.mkdir(parents=True)
    (store._dir / "roster.json").write_text(json.dumps({"seats": {"N1": {"name": "<script>"}, "N2": {"name": "Zed"}}}), encoding="utf-8")
    assert [store.entry("N1")["name"], store.entry("N2")["name"]] == ["Alice", "Zed"]
    (store._dir / "roster.json").write_text("{not json", encoding="utf-8")
    assert store.entry("N1")["name"] == "Alice"


# ------------------------------------------------------------------------------------ avatars
def test_ensure_avatars_fills_only_the_missing_ones_and_is_stable(store):
    made: list[int] = []

    def make(seat, taken):
        made.append(seat)
        return {"v": 1, "face": f"f{seat:02d}", "taken": len(taken)}

    assert store.ensure_avatars(make) is True and made == [*range(1, 13), 0]  # the moderator (seat 0) comes last
    assert [row["avatar"]["taken"] for row in store.get()["seats"]] == list(range(12))  # each saw the earlier ones
    assert store.entry("moderator")["avatar"]["taken"] == 12  # ... the moderator saw all twelve players
    made.clear()
    assert store.ensure_avatars(make) is False and made == []  # nothing missing: nothing generated, nothing written
    store.set_avatar("N5", {"v": 1, "face": "fXX"})
    assert store.entry("N5")["avatar"] == {"v": 1, "face": "fXX"} and store.entry("N4")["avatar"]["face"] == "f04"


def test_set_all_avatars_replaces_the_listed_seats(store):
    store.set_all_avatars({"N1": {"v": 1, "face": "a"}, "N2": {"v": 1, "face": "b"}})
    assert [store.entry("N1")["avatar"]["face"], store.entry("N2")["avatar"]["face"], store.entry("N3")["avatar"]] == ["a", "b", None]
    with pytest.raises(RosterError):
        store.set_avatar("N1", {})
    with pytest.raises(RosterError):
        store.set_avatar("N1", "not a dict")  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------ the moderator's avatar
def test_the_moderator_is_not_a_seat_it_only_has_an_avatar(store):
    assert store.get()["moderator"] == {"id": "moderator", "name": "主持人", "avatar": None}
    assert [row["id"] for row in store.get()["seats"]] == [f"N{i}" for i in range(1, 13)]  # still twelve seats
    assert store.entry("moderator") == {"id": "moderator", "name": "主持人", "avatar": None}
    store.set_avatar("moderator", {"v": 1, "face": "f07"})
    assert store.entry("moderator")["avatar"] == {"v": 1, "face": "f07"} and store.snapshot()["moderator"]["avatar"] == {"v": 1, "face": "f07"}
    assert RosterStore(store.data_dir).get()["moderator"]["avatar"] == {"v": 1, "face": "f07"}  # persisted for the next games
    assert store.entry("N1")["avatar"] is None  # the seats are untouched
    store.set_all_avatars({"moderator": {"v": 1, "face": "f08"}, "N2": {"v": 1, "face": "x"}})
    assert store.entry("moderator")["avatar"]["face"] == "f08" and store.entry("N2")["avatar"]["face"] == "x"


def test_the_moderator_has_no_name_persona_or_memory_to_set(store):
    for call in (lambda: store.set_name("moderator", "Boss"), lambda: store.set_persona("moderator", "x"), lambda: store.set_memory("moderator", "x")):
        with pytest.raises(RosterError, match="没有这个座位"):
            call()
    with pytest.raises(RosterError):
        store.set_avatar("moderator", {})
    assert store.get()["moderator"]["name"] == "主持人"
    assert "moderator" not in {row["id"] for row in store.get()["seats"]}  # the moderator is not a seat


def test_an_old_roster_without_a_moderator_gets_one_and_keeps_every_player(store):
    store.set_all_avatars({f"N{i}": {"v": 1, "face": f"old{i}"} for i in range(1, 13)})  # a roster written by an earlier version
    assert store.entry("moderator")["avatar"] is None
    assert store.ensure_avatars(lambda seat, taken: {"v": 1, "face": f"new{seat}", "taken": len(taken)}) is True
    assert store.entry("moderator")["avatar"] == {"v": 1, "face": "new0", "taken": 12}  # picked to differ from all twelve
    assert [store.entry(f"N{i}")["avatar"]["face"] for i in (1, 12)] == ["old1", "old12"]  # the players were not re-rolled


# ------------------------------------------------------------------------------------ persona / memory files
def test_persona_and_memory_are_plain_files_that_survive_and_can_be_edited_outside(store, tmp_path):
    store.set_persona("N2", "寡言，爱做记录。")
    store.set_memory("N2", "## 印象\n- N1 Alice：爱抢话。")
    assert (store._dir / "N2.persona.md").read_text(encoding="utf-8") == "寡言，爱做记录。\n"
    fresh = RosterStore(tmp_path)
    assert fresh.entry("N2")["persona"] == "寡言，爱做记录。" and fresh.entry("N2")["memory_parts"]["impressions"]["N1"] == "爱抢话。"
    (store._dir / "N2.persona.md").write_text("手工改过的设定\n", encoding="utf-8")  # an outside edit
    assert RosterStore(tmp_path).entry("N2")["persona"] == "手工改过的设定"
    # the memory file is written in the canonical form, so that an export and an import give the same bytes back
    assert memory_file(store, "N2") == store.entry("N2")["memory"] + "\n"


def test_empty_text_removes_the_file_and_long_text_is_refused(store):
    store.set_persona("N1", "x")
    assert (store._dir / "N1.persona.md").exists()
    store.set_persona("N1", "  \n ")
    assert not (store._dir / "N1.persona.md").exists() and store.entry("N1")["persona"] == ""
    with pytest.raises(RosterError, match="最多"):
        store.set_memory("N1", "字" * (MAX_TEXT_CHARS + 1))
    with pytest.raises(RosterError, match="空字符"):
        store.set_persona("N1", "a\x00b")
    store.set_memory_parts("N1", experience=["先听后说"])
    assert (store._dir / "N1.memory.md").exists()
    store.set_memory("N1", "  \n ")  # nothing left to remember: no file
    assert not (store._dir / "N1.memory.md").exists() and store.entry("N1")["memory"] == ""


def test_the_memory_switch_decides_what_a_game_snapshot_carries(store):
    store.set_persona("N1", "谨慎")
    store.set_memory_parts("N1", impressions={"N2": "常输"})
    snap = store.snapshot()
    assert snap["memory_enabled"] is True and "常输" in snap["seats"][0]["memory"] and snap["seats"][0]["persona"] == "谨慎"
    assert "memory_parts" not in snap["seats"][0]  # a game takes the texts, not the editor's view of them
    store.set_memory_enabled(False)
    off = store.snapshot()
    assert off["memory_enabled"] is False and off["seats"][0]["memory"] == "" and off["seats"][0]["persona"] == "谨慎"
    assert "常输" in store.entry("N1")["memory"]  # the file itself is untouched
    assert RosterStore(store.data_dir).get()["memory_enabled"] is False  # and the switch persisted


def test_a_snapshot_is_a_copy_later_edits_do_not_reach(store):
    store.set_memory_parts("N1", impressions={"N2": "旧印象"})
    snap = store.snapshot()
    store.set_memory_parts("N1", impressions={"N2": "改过的印象"})
    assert "旧印象" in snap["seats"][0]["memory"] and "改过的印象" not in snap["seats"][0]["memory"]


# ------------------------------------------------------------------------------------ memory sections
CANON4 = (
    "## 战绩\n场次 7，胜 4，负 3，胜率 57%\n- 狼人：3 场，胜 2\n\n"
    "## 印象\n- N1 Alice：发言短，站边快，常被狼利用；可信度中。\n- N2 Bob：（暂无）\n- N4 David：逻辑细，抓细节准。\n\n"
    "## 经验\n- 第一天别急着站边，先听完警上发言。\n- 女巫第一晚救人收益高。\n\n"
    "## 旧记忆（待整理）\n第一局：太保守。"
)


def test_the_canonical_memory_text_parses_and_renders_back_to_itself():
    parts = parse_memory(CANON4, CTX4)
    assert parts.record == "场次 7，胜 4，负 3，胜率 57%\n- 狼人：3 场，胜 2"  # the record is kept word for word
    assert parts.impressions == {"N1": "发言短，站边快，常被狼利用；可信度中。", "N2": "", "N4": "逻辑细，抓细节准。"}
    assert parts.experience == ["第一天别急着站边，先听完警上发言。", "女巫第一晚救人收益高。"]
    assert parts.legacy == "第一局：太保守。"
    assert render_memory(parts, CTX4) == CANON4
    assert render_memory(fit_memory(parse_memory(CANON4, CTX4), CTX4), CTX4) == CANON4  # nothing in it is over a limit
    assert render_memory(MemoryParts(), CTX4) == "" and MemoryParts().empty  # nothing to remember: no text, no file


def test_parsing_is_lenient_and_rendering_canonical_one_row_per_other_seat_in_seat_order():
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
    text = render_memory(parts, CTX4)
    assert text.splitlines()[:4] == ["## 印象", "- N1 Alice：同一个座位写了两行，后面的算数", "- N2 Bob：编号列表", "- N4 David：冒号是英文的"]
    assert render_memory(parse_memory(text, CTX4), CTX4) == text  # parsing the canonical text changes nothing
    # the placeholder and "nothing" are the same thing
    placeholder = parse_memory("## 印象\n- N1 Alice：（暂无）\n- N2 Bob：(暂无)\n- N4 David：暂无", CTX4)
    assert placeholder.impressions == {"N1": "", "N2": "", "N4": ""} and placeholder.empty


def test_a_name_is_only_a_label_a_stored_name_with_a_colon_is_read_and_a_wrapped_line_continues_its_impression():
    ctx = MemoryContext("N3", CTX4.ids, {"N1": "Re:zero", "N2": "Bob", "N3": "Carol", "N4": "David"})
    # (a label written with the name as it was: the colon of the name does not end the label)
    parts = parse_memory("## 印象\n- N1 Re:zero：爱带节奏\n- N2 Bob：发言很短，\n  站边很快。\n- N4：没有名字也行", ctx)
    assert parts.impressions == {"N1": "爱带节奏", "N2": "发言很短， 站边很快。", "N4": "没有名字也行"}
    text = render_memory(parts, ctx)
    assert "- N1 Re zero：爱带节奏" in text  # ... and a label is written without it (it would end the label when read back)
    assert parse_memory(text, ctx).impressions == parts.impressions and render_memory(parse_memory(text, ctx), ctx) == text
    renamed = MemoryContext("N3", CTX4.ids, {**ctx.names, "N1": "Zed"})
    assert "- N1 Zed：爱带节奏" in render_memory(parts, renamed)  # (the key is the seat: a rename only changes the label)
    assert parse_memory(text, renamed).impressions == parts.impressions  # ... and what was written still reads back after one


def test_a_memory_text_is_split_and_joined_by_its_headings():
    text = "开头的话\n\n## 战绩\n场次 1，胜 1\n\n## 印象\n- N1 Alice：x\n\n## 经验\n"
    sections = split_memory(text)
    assert list(sections) == ["", "战绩", "印象", "经验"] and sections[""] == "开头的话" and sections["经验"] == ""
    assert join_memory(sections) == "开头的话\n\n## 战绩\n场次 1，胜 1\n\n## 印象\n- N1 Alice：x"  # (an empty section is left out)
    assert split_memory("") == {} and split_memory("只是一句手写的话") == {"": "只是一句手写的话"}


# ------------------------------------------------------------------------------------ the migration of a v0.13.0 file
V0130 = (
    "## 战绩\n场次 3，胜 2，负 1，胜率 67%\n- 狼人：2 场，胜 1\n- 平民：1 场，胜 1\n\n"
    "## 对局记忆\n第一局：我太保守。\n\n第二局：被带偏了。\n\n第三局：" + "字" * 700 + "\n\n"
    "## 经验与印象\nBob 爱撒谎。\n"
)


def test_a_v0130_file_is_migrated_when_it_is_read_without_a_model_without_losing_the_record_or_failing(store):
    write_memory(store, V0130)
    path = store._dir / "N3.memory.md"
    on_disk = path.read_bytes()
    entry = store.entry("N3")
    parts = entry["memory_parts"]
    assert parts["record"] == "场次 3，胜 2，负 1，胜率 67%\n- 狼人：2 场，胜 1\n- 平民：1 场，胜 1"  # word for word
    assert parts["experience"] == [] and not any(parts["impressions"].values()) and set(parts["impressions"]) == set(IDS) - {"N3"}
    legacy = parts["legacy"]
    assert 0 < len(legacy) <= LEGACY_CHARS and legacy.startswith("…") and legacy.endswith("Bob 爱撒谎。")  # the newer text is kept
    assert "第一局" not in legacy and "第三局" not in legacy  # ... the oldest goes
    assert path.read_bytes() == on_disk  # reading does not touch the file: it is rewritten by the next save
    text = entry["memory"]
    assert text.startswith("## 战绩\n场次 3，胜 2") and "## 印象\n- N1 Alice：（暂无）" in text and text.endswith(legacy)
    assert "## 对局记忆" not in text and "## 经验与印象" not in text and "\n## 旧记忆（待整理）\n" in text
    # migrating twice is the same as migrating once
    ctx = store.memory_context("N3")
    assert render_memory(fit_memory(parse_memory(text, ctx), ctx), ctx) == text
    # the next save writes the new format and keeps the record
    store.set_memory_parts("N3", experience=["先听后说。"])
    rewritten = memory_file(store)
    assert "## 对局记忆" not in rewritten and "## 印象" in rewritten and "- 先听后说。" in rewritten
    assert rewritten == store.entry("N3")["memory"] + "\n" and store.entry("N3")["memory_parts"]["record"] == parts["record"]


@pytest.mark.parametrize(
    "old,record,legacy",
    [
        ("只是一句手写的话", "", "只是一句手写的话"),  # no headings at all
        ("开头文字\n\n## 战绩\n场次 1，胜 1", "场次 1，胜 1", "开头文字"),
        ("## 战绩\n- 赢过一次\n\n## 我的备注\ny", "- 赢过一次", "【我的备注】\ny"),  # any other section is kept, with its title
        ("## 经验与印象\n- 谨慎\n", "", "- 谨慎"),
        ("## 战绩\n场次 1，胜 1\n", "场次 1，胜 1", ""),  # a record only: nothing to tidy up
    ],
)
def test_an_old_file_loses_nothing_what_is_not_the_record_the_impressions_or_the_lessons_is_the_legacy_text(store, old, record, legacy):
    write_memory(store, old)
    parts = store.entry("N3")["memory_parts"]
    assert (parts["record"], parts["legacy"]) == (record, legacy)
    assert parts["experience"] == [] and not any(parts["impressions"].values())


def test_a_file_over_the_new_limits_opens_cut_without_an_error_and_can_be_saved_again(store):
    impressions = "\n".join(f"- N{i} {NAMES[f'N{i}']}：" + "字" * 200 for i in (1, 2, 4))
    lessons = "\n".join(f"- 经验{i}：" + "字" * 200 for i in range(1, 6))
    write_memory(store, f"## 印象\n{impressions}\n\n## 经验\n{lessons}\n\n## 旧记忆（待整理）\n" + "旧" * 900)
    parts = store.entry("N3")["memory_parts"]
    assert all(len(text) <= IMPRESSION_CHARS for text in parts["impressions"].values())
    assert parts["impressions"]["N1"].endswith("…") and len(parts["impressions"]["N1"]) == IMPRESSION_CHARS  # a cut text says so
    assert 1 <= len(parts["experience"]) <= EXPERIENCE_ITEMS and sum(len(i) for i in parts["experience"]) <= EXPERIENCE_CHARS
    assert len(parts["legacy"]) <= LEGACY_CHARS
    text = store.entry("N3")["memory"]
    store.set_memory("N3", text)  # what the panel shows can be saved as it stands: it never says "too long" about its own text
    assert store.entry("N3")["memory"] == text
    write_memory(store, "## 经验\n" + "\n".join(f"- 条{i}" for i in range(1, 6)))
    assert store.entry("N3")["memory_parts"]["experience"] == ["条1", "条2", "条3"]  # at most three lessons


def test_fit_memory_shortens_the_longest_texts_first_when_the_whole_is_over_the_limit():
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
    check_memory(fitted, ctx)  # ... and the result passes a manual save


# ------------------------------------------------------------------------------------ the limits of a manual save
def test_a_manual_save_over_a_limit_is_refused_and_says_which_part_how_long_and_the_limit(store):
    names = {sid: chr(0x7532 + i) * 20 for i, sid in enumerate(IDS)}  # twenty characters each: the longest labels there are
    for sid, name in names.items():
        store.set_name(sid, name)
    cases = [
        (lambda: store.set_memory_parts("N3", impressions={"N5": "字" * 81}), f"N5 {names['N5']} 的印象最多 80 字（现在 81 字）"),
        (lambda: store.set_memory_parts("N3", experience=["a", "b", "c", "d"]), "经验最多 3 条（现在 4 条）"),
        (lambda: store.set_memory_parts("N3", experience=["字" * 150, "字" * 151]), "经验合计最多 300 字（现在 301 字）"),
        (lambda: store.set_memory_parts("N3", legacy="字" * 601), "旧记忆最多 600 字（现在 601 字）"),
        (lambda: store.set_memory("N3", "## 印象\n- N5：" + "字" * 81), "的印象最多 80 字（现在 81 字）"),
        (lambda: store.set_memory("N3", "## 经验\n- a\n- b\n- c\n- d"), "经验最多 3 条"),
        (lambda: store.set_persona("N3", "字" * 501), "性格设定最多 500 个字符（现在 501 个）"),
        (lambda: store.set_memory_parts("N3", impressions={"N5": "a\x00b"}), "空字符"),
    ]
    for call, message in cases:
        with pytest.raises(RosterError, match=re.escape(message)):
            call()
    assert not (store._dir / "N3.memory.md").exists() and not (store._dir / "N3.persona.md").exists()  # nothing was written
    # the whole memory has a limit of its own: eleven full impressions and full lessons do not fit behind twenty-character names
    full = {sid: "字" * 80 for sid in IDS if sid != "N3"}
    with pytest.raises(RosterError, match=r"整份记忆.*最多 1500 字（现在 \d+ 字）"):
        store.set_memory_parts("N3", impressions=full, experience=["经" * 100] * 3)
    # ... what is at the limit is fine
    store.set_memory_parts("N3", impressions={"N5": "字" * 80}, experience=["经" * 100] * 3, legacy="旧" * 600)
    store.set_persona("N3", "字" * PERSONA_CHARS)
    assert len(store.entry("N3")["memory_parts"]["impressions"]["N5"]) == 80 and len(store.entry("N3")["persona"]) == 500


def test_memory_parts_change_what_they_name_and_keep_the_rest_and_the_record(store):
    store.apply_review("N3", role="狼人", won=True, impressions={"N1": "发言短", "N2": "爱附和"}, experience=["先听后说"])
    record = store.entry("N3")["memory_parts"]["record"]
    store.set_memory_parts("N3", impressions={"N2": "改了", "N5": "新增"})
    parts = store.entry("N3")["memory_parts"]
    assert (parts["impressions"]["N1"], parts["impressions"]["N2"], parts["impressions"]["N5"]) == ("发言短", "改了", "新增")
    assert parts["experience"] == ["先听后说"] and parts["record"] == record
    store.set_memory_parts("N3", impressions={"N1": ""}, experience=[])  # "" = no impression, [] = no lessons
    parts = store.entry("N3")["memory_parts"]
    assert parts["impressions"]["N1"] == "" and parts["experience"] == [] and parts["impressions"]["N2"] == "改了"
    store.set_memory_parts("N3", experience=["  - 带着符号的一条  ", "", "第二条\n第二行"])  # lessons are one-line texts
    assert store.entry("N3")["memory_parts"]["experience"] == ["带着符号的一条", "第二条 第二行"]


def test_an_impression_of_oneself_or_of_a_seat_that_does_not_exist_is_an_input_error_not_a_too_long(store):
    for impressions, message in (({"N3": "自己"}, "不能给自己"), ({"N13": "x"}, "没有这个座位"), ({"ghost": "x"}, "没有这个座位")):
        with pytest.raises(RosterInputError, match=message):
            store.set_memory_parts("N3", impressions=impressions)
    assert issubclass(RosterInputError, RosterError)
    small = RosterStore(store.data_dir, seats=4)  # a table of four: the others of N2 are N1, N3 and N4
    assert list(small.entry("N2")["memory_parts"]["impressions"]) == ["N1", "N3", "N4"]
    with pytest.raises(RosterInputError, match="没有这个座位"):
        small.set_memory_parts("N2", impressions={"N7": "x"})


def test_a_whole_text_is_parsed_checked_and_kept_in_the_canonical_form_with_the_record_as_written(store):
    store.set_memory("N3", "## 战绩\n场次 9，胜 9\n\n## 印象\n- N5：爱悍跳\nN6: 沉默\n- N3：自己\n- N77：鬼\n\n## 经验\n- 先听后说")
    parts = store.entry("N3")["memory_parts"]
    assert parts["record"] == "场次 9，胜 9" and parts["impressions"]["N5"] == "爱悍跳" and parts["impressions"]["N6"] == "沉默"
    assert "N3" not in parts["impressions"] and "N77" not in parts["impressions"] and parts["experience"] == ["先听后说"]
    assert memory_file(store).startswith("## 战绩\n场次 9，胜 9\n\n## 印象\n- N1 Alice：（暂无）")
    # an old-format or free text is accepted too: it becomes the legacy text
    store.set_memory("N4", "## 经验与印象\nBob 爱撒谎")
    assert store.entry("N4")["memory_parts"]["legacy"] == "Bob 爱撒谎"
    # a text the panel read can be saved as it is
    text = store.entry("N3")["memory"]
    store.set_memory("N3", text)
    assert store.entry("N3")["memory"] == text


def test_validate_texts_raises_what_a_save_would_raise_and_writes_nothing(store):
    with pytest.raises(RosterError, match="性格设定最多"):
        store.validate_texts("N1", persona="字" * 501)
    with pytest.raises(RosterError, match="印象最多"):
        store.validate_texts("N1", memory_parts={"impressions": {"N2": "字" * 81}})
    with pytest.raises(RosterInputError):
        store.validate_texts("N1", memory_parts={"impressions": {"N1": "x"}})
    with pytest.raises(RosterError, match="没有这个座位"):
        store.validate_texts("N99", persona="x")
    store.validate_texts("N1", persona="好", memory="## 经验\n- x", memory_parts={"experience": ["x"]})
    assert not list(store._dir.glob("N1.*"))


def test_impressions_are_keyed_by_the_seat_and_the_name_is_a_label_that_follows_the_roster(store):
    store.set_memory_parts("N3", impressions={"N5": "爱悍跳"})
    assert "- N5 Eve：爱悍跳" in store.entry("N3")["memory"]
    store.set_name("N5", "Evelyn")
    entry = store.entry("N3")
    assert "- N5 Evelyn：爱悍跳" in entry["memory"] and entry["memory_parts"]["impressions"]["N5"] == "爱悍跳"
    assert "- N5 Eve：爱悍跳" in memory_file(store)  # the file is only rewritten by the next save ...
    store.set_memory_parts("N3", experience=["x"])
    assert "- N5 Evelyn：爱悍跳" in memory_file(store)  # ... which writes the new label


# ------------------------------------------------------------------------------------ what a prompt gets
def test_the_prompt_memory_lists_only_what_there_is_and_says_that_the_seats_stay_but_the_identities_do_not():
    ctx = ctx12("N3")
    assert memory_for_prompt(MemoryParts(), ctx) == ""
    parts = MemoryParts(
        record="场次 1，胜 1", impressions={**{sid: "" for sid in ctx.others}, "N5": "爱悍跳"}, experience=["先听后说"], legacy="旧笔记"
    )
    text = memory_for_prompt(parts, ctx)
    assert text == f"## 战绩\n场次 1，胜 1\n\n## 印象\n{PROMPT_IMPRESSION_NOTE}\n- N5 Eve：爱悍跳\n\n## 经验\n- 先听后说\n\n## 旧记忆（待整理）\n旧笔记"
    assert "（暂无）" not in text and "N1 Alice" not in text  # no rows for seats he has no impression of
    only_record = memory_for_prompt(MemoryParts(record="场次 1，胜 1"), ctx)
    assert only_record == "## 战绩\n场次 1，胜 1"


def test_a_long_old_persona_is_cut_for_the_prompt_but_the_file_and_the_panel_keep_it(store):
    long_persona = "很长的旧性格设定。" * 200
    store._dir.mkdir(parents=True)
    (store._dir / "N1.persona.md").write_text(long_persona + "\n", encoding="utf-8")
    assert store.entry("N1")["persona"] == long_persona  # GET gives the text as it is (the panel says "超出上限")
    cut = store.snapshot()["seats"][0]["persona"]
    assert len(cut) == PERSONA_CHARS and cut.endswith("…") and long_persona.startswith(cut[:-1])
    assert persona_for_prompt("短") == "短" and persona_for_prompt("  \n") == "" and persona_for_prompt("字" * 500) == "字" * 500
    assert (store._dir / "N1.persona.md").read_text(encoding="utf-8") == long_persona + "\n"  # the file is not touched


# ------------------------------------------------------------------------------------ the persona format
def test_a_persona_draft_is_normalized_to_the_fixed_format_and_what_is_outside_the_template_is_dropped():
    raw = (
        "好的，以下是设定：\n\n"
        "**【定位】** 冷静的逻辑派，喜欢用票型说话。\n"
        "### 【说话风格】\n- 句子短，先结论后理由。\n* 很少用感叹号。\n"
        "【思考方式】\n每轮复盘票型，找出跟票异常的人。\n"  # plain lines (no bullets) are the items
        "【决策偏好】\n- 证据不足时宁可弃票。\n- 不跟风。\n- 第三条。\n- 第四条超出三条，被丢弃。\n"
        "压力下：被怀疑时摆出完整时间线。\n"  # a heading without brackets, the text on the same line
        "【口头禅】我们看票。\n\n"
        "希望这份指引对你有帮助！"
    )
    expected = (
        "【定位】冷静的逻辑派，喜欢用票型说话。\n"
        "【说话风格】\n- 句子短，先结论后理由。\n- 很少用感叹号。\n"
        "【思考方式】\n- 每轮复盘票型，找出跟票异常的人。\n"
        "【决策偏好】\n- 证据不足时宁可弃票。\n- 不跟风。\n- 第三条。\n"
        "【压力下】\n- 被怀疑时摆出完整时间线。\n"
        "【口头禅】我们看票。"
    )
    assert normalize_persona(raw) == expected
    assert normalize_persona(expected) == expected  # the fixed format is a fixed point
    assert normalize_persona(FENCE + "markdown\n" + expected.replace("【定位】", "【定位】 ") + "\n" + FENCE).startswith("【定位】冷静")
    # a section that is left out stays out, an empty catch-phrase is no line
    assert normalize_persona("【定位】只有一句\n【口头禅】\n【压力下】\n") == "【定位】只有一句"
    for no_template in ("", "   ", "你说话慢条斯理，爱讲冷笑话。", FENCE + "\n只有一句\n" + FENCE):
        assert normalize_persona(no_template) == ""


def test_persona_lines_are_cut_to_forty_characters_and_the_whole_to_five_hundred_dropping_whole_lines_last_section_first():
    long_line = "字" * 60
    sections = "".join(f"【{h}】\n" + "".join(f"- {long_line}\n" for _ in range(5)) for h in ("说话风格", "思考方式", "决策偏好", "压力下"))
    text = normalize_persona(f"【定位】{long_line}\n{sections}【口头禅】{long_line}")
    assert len(text) <= PERSONA_CHARS
    seen: dict[str, list[str]] = {}
    heading = ""
    for line in text.splitlines():
        if line.startswith("【"):
            heading, _, body = line.partition("】")
            heading = heading[1:]
            seen[heading] = [body] if body else []
        else:
            assert line.startswith("- ")
            seen[heading].append(line[2:])
    assert set(seen) == {"定位", "说话风格", "思考方式", "决策偏好", "压力下", "口头禅"}
    for items in seen.values():
        assert all(len(item) == PERSONA_ITEM_CHARS and item.endswith("…") for item in items)  # (every line was longer than 40)
    counts = [len(seen[h]) for h in ("说话风格", "思考方式", "决策偏好", "压力下")]
    assert counts == [2, 2, 2, 2]  # 3 / 3 / 3 / 2 would be too long: the fullest section loses its last line first, the later one on ties
    # what fits is not touched
    short = "【定位】一句话\n【说话风格】\n- 甲\n- 乙\n- 丙\n- 丁\n【压力下】\n- 戊\n- 己\n- 庚"
    assert normalize_persona(short) == "【定位】一句话\n【说话风格】\n- 甲\n- 乙\n- 丙\n【压力下】\n- 戊\n- 己"


# ------------------------------------------------------------------------------------ the answer of a review model
def test_a_review_answer_as_json_also_in_a_code_fence_or_between_two_sentences():
    ctx = ctx12("N3")
    body = '{"impressions": {"N1": "发言短", "n2": "**爱**附和", "N3": "自己", "N99": "鬼", "N4 David": "逻辑细", "N5": "  "}, "experience": ["- 先听后说", "别太早表态"]}'
    for raw in (body, f"{FENCE}json\n{body}\n{FENCE}", f"好的，这是我的更新：\n{body}\n希望有帮助"):
        out = parse_review_output(raw, ctx)
        assert out.mode == "json"
        assert out.impressions == {"N1": "发言短", "N2": "爱附和", "N4": "逻辑细"}  # the owner, an unknown seat and an empty text are gone
        assert out.experience == ["先听后说", "别太早表态"]
    assert parse_review_output('{"impressions": {}, "experience": []}', ctx) == ReviewOutput({}, None, "json")  # "nothing has changed"
    assert parse_review_output('{"experience": ["只有经验"]}', ctx) == ReviewOutput({}, ["只有经验"], "json")
    as_list = parse_review_output('{"impressions": ["N1：发言短", "- N2 Bob：爱附和"], "experience": "第一条\\n第二条"}', ctx)
    assert as_list == ReviewOutput({"N1": "发言短", "N2": "爱附和"}, ["第一条", "第二条"], "json")


def test_a_review_answer_as_lines_when_there_is_no_json():
    ctx = ctx12("N3")
    raw = "我的更新：\nN1：发言短\n- N2 Bob: 爱附和，\n  经常跟票。\n**N4 David**：逻辑细\nN3：自己\nN99：鬼\n经验：\n- 先听后说\n- 别太早表态\n"
    out = parse_review_output(raw, ctx)
    assert out.mode == "lines"
    assert out.impressions == {"N1": "发言短", "N2": "爱附和， 经常跟票。", "N4": "逻辑细"}
    assert out.experience == ["先听后说", "别太早表态"]
    assert parse_review_output("N1：发言短", ctx) == ReviewOutput({"N1": "发言短"}, None, "lines")
    assert parse_review_output("## 经验\n- 只有一条经验", ctx) == ReviewOutput({}, ["只有一条经验"], "lines")


@pytest.mark.parametrize(
    "garbage",
    [
        "", "   ", "这一局我发言太少，下次早点表态。", "{not json", '{"foo": 1}', "[1, 2]", "N1 没有冒号也没有结构", "N99：不存在的座位",
        '{"impressions": {"N99": "只有不存在的座位"}}', '{"impressions": {"N3": "只有自己"}}', '{"impressions": {"N1": {"text": "嵌套的对象"}}}',
        '{"impressions": "不是对象"}', '好的：{思考} {"foo": 1}',
    ],
)
def test_a_review_answer_that_says_nothing_usable_is_none(garbage):
    assert parse_review_output(garbage, ctx12("N3")) is None


def test_the_json_is_found_between_chatter_that_has_braces_of_its_own_and_an_empty_impression_is_still_an_answer():
    ctx = ctx12("N3")
    out = parse_review_output('我先想想{先说结论}：\n{"impressions": {"N1": "发言短"}, "experience": ["先听后说"]}\n（完）{谢谢}', ctx)
    assert out == ReviewOutput({"N1": "发言短"}, ["先听后说"], "json")
    # "none yet" for everybody is an answer too: nothing changes, and the review counts
    assert parse_review_output('{"impressions": {"N1": "（暂无）", "N2": ""}}', ctx) == ReviewOutput({}, None, "json")
    # a second object without the keys of an answer does not hide the one that has them
    assert parse_review_output('{"note": 1} {"impressions": {"N2": "爱附和"}}', ctx) == ReviewOutput({"N2": "爱附和"}, None, "json")


def test_merging_a_review_keeps_the_seats_it_left_out_cuts_the_texts_and_deletes_the_legacy_text():
    ctx = ctx12("N3")
    before = MemoryParts(
        record="场次 1，胜 1", impressions={**{sid: "" for sid in ctx.others}, "N1": "旧的一", "N2": "旧的二"}, experience=["旧经验"], legacy="旧笔记"
    )
    after = merge_review(before, ReviewOutput({"N2": "新的二", "N4": "字" * 200, "N3": "自己", "N99": "鬼"}, None, "json"), ctx)
    assert after.impressions["N1"] == "旧的一" and after.impressions["N2"] == "新的二"  # left out: kept
    assert len(after.impressions["N4"]) == IMPRESSION_CHARS and after.impressions["N4"].endswith("…")
    assert "N3" not in after.impressions and "N99" not in after.impressions
    assert after.experience == ["旧经验"] and after.legacy == "" and after.record == "场次 1，胜 1"  # (the review has handed the legacy text over)
    assert before.legacy == "旧笔记" and before.impressions["N2"] == "旧的二"  # the input is not changed
    kept = merge_review(before, ReviewOutput({}, ["新经验一", "新经验二"], "json"), ctx, keep_legacy=True)
    assert kept.experience == ["新经验一", "新经验二"] and kept.legacy == "旧笔记"


# ------------------------------------------------------------------------------------ the record
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


# ------------------------------------------------------------------------------------ clearing
def test_clear_empties_the_memory_and_optionally_the_persona(store):
    store.set_persona("N1", "性格")
    store.set_memory_parts("N1", experience=["记忆"])
    store.set_memory("N2", "记忆二")
    assert store.clear() == 2
    assert store.entry("N1")["memory"] == "" and store.entry("N2")["memory"] == "" and store.entry("N1")["persona"] == "性格"
    store.set_memory("N1", "又有了")
    assert store.clear(include_persona=True) == 2 and store.entry("N1")["persona"] == ""
    assert store.clear() == 0


# ------------------------------------------------------------------------------------ review
def test_a_review_updates_the_record_and_revises_the_impressions_the_file_is_overwritten_not_appended_to(store):
    first = store.apply_review("N3", role="狼人", won=True, impressions={"N1": "发言短，站边快。", "N5": "爱悍跳。"}, experience=["先听后说。"])
    parts = store.entry("N3")["memory_parts"]
    assert parts["record"].splitlines()[0] == "场次 1，胜 1，负 0，胜率 100%" and "- 狼人：1 场，胜 1" in parts["record"]
    assert (parts["impressions"]["N1"], parts["impressions"]["N5"], parts["impressions"]["N2"]) == ("发言短，站边快。", "爱悍跳。", "")
    assert parts["experience"] == ["先听后说。"]
    assert first.text == store.entry("N3")["memory"] and first.text + "\n" == memory_file(store)  # saved, in the canonical form
    assert first.changes == {
        "impressions": [{"id": "N1", "before": "", "after": "发言短，站边快。"}, {"id": "N5", "before": "", "after": "爱悍跳。"}],
        "experience": {"before": [], "after": ["先听后说。"]},
    }

    second = store.apply_review("N3", role="平民", won=False, impressions={"N1": "改观了：其实很可靠。"}, experience=None)
    parts = store.entry("N3")["memory_parts"]
    assert parts["record"].splitlines()[0] == "场次 2，胜 1，负 1，胜率 50%"
    assert parts["impressions"]["N1"] == "改观了：其实很可靠。" and parts["impressions"]["N5"] == "爱悍跳。"  # N5 was left out: kept
    assert parts["experience"] == ["先听后说。"]  # no new lessons: the old ones stay
    assert second.changes == {"impressions": [{"id": "N1", "before": "发言短，站边快。", "after": "改观了：其实很可靠。"}], "experience": None}
    assert second.text.count("- N1 Alice：") == 1 and "发言短，站边快。" not in second.text  # revised, not appended: one line per seat
    assert RosterStore(store.data_dir).entry("N3")["memory"] == second.text  # saved


def test_a_review_drops_the_owner_and_unknown_seats_and_cuts_a_long_impression(store):
    result = store.apply_review("N3", role="平民", won=True, impressions={"N3": "自己", "N99": "鬼", "N2": "字" * 200})
    parts = store.entry("N3")["memory_parts"]
    assert "N3" not in parts["impressions"] and "N99" not in parts["impressions"] and "自己" not in result.text
    assert len(parts["impressions"]["N2"]) == IMPRESSION_CHARS and parts["impressions"]["N2"].endswith("…")
    assert result.changes["impressions"] == [{"id": "N2", "before": "", "after": parts["impressions"]["N2"]}]
    again = store.apply_review("N3", role="平民", won=True, impressions={"N2": parts["impressions"]["N2"]})
    assert again.changes == {"impressions": [], "experience": None}  # the same words again: nothing changed


def test_a_review_of_an_unknown_outcome_changes_the_impressions_but_not_the_record(store):
    result = store.apply_review("N1", role="", won=None, impressions={"N2": "爱附和"})
    parts = store.entry("N1")["memory_parts"]
    assert parts["record"] == "" and parts["impressions"]["N2"] == "爱附和" and "## 战绩" not in result.text
    store.apply_review("N1", role="平民", won=True, impressions={})  # (a seat the model said nothing about: the record still counts)
    assert store.entry("N1")["memory_parts"]["record"].startswith("场次 1，胜 1")
    assert store.entry("N1")["memory_parts"]["impressions"]["N2"] == "爱附和"


def test_a_review_tidies_up_a_v0130_file_the_record_stays_and_the_legacy_section_goes(store):
    write_memory(store, V0130)
    before = store.entry("N3")["memory_parts"]
    assert before["legacy"]
    store.apply_review("N3", role="狼人", won=True, impressions={"N2": "爱撒谎，下次别信。"}, experience=["别太保守。"])
    parts = store.entry("N3")["memory_parts"]
    assert parts["legacy"] == "" and "旧记忆" not in store.entry("N3")["memory"] and "## 对局记忆" not in memory_file(store)
    tally = RecordTally.parse(parts["record"])
    assert (tally.games, tally.wins) == (4, 3) and tally.by_role == {"狼人": [3, 2], "平民": [1, 1]}  # three games counted before, one now
    assert parts["impressions"]["N2"] == "爱撒谎，下次别信。" and parts["experience"] == ["别太保守。"]


def test_a_review_answer_that_is_too_long_for_the_whole_is_cut_to_fit_and_what_it_writes_can_be_saved_again(store):
    names = {sid: chr(0x7532 + i) * 20 for i, sid in enumerate(IDS)}
    for sid, name in names.items():
        store.set_name(sid, name)
    store.apply_review("N3", role="平民", won=True, impressions={sid: "字" * 80 for sid in IDS if sid != "N3"}, experience=["经" * 100] * 3)
    ctx, parts = store.load_memory("N3")
    assert memory_chars(parts, ctx) <= MEMORY_CHARS
    text = store.entry("N3")["memory"]
    store.set_memory("N3", text)
    assert store.entry("N3")["memory"] == text


def test_clean_name_strips_and_returns_the_text():
    assert clean_name("  Zoë  ") == "Zoë"


# ====================================================================================== second round (lead's review)
# ------------------------------------------------------------------------------------ a byte-order mark
RECORD3 = "场次 3，胜 2，负 1，胜率 67%\n- 狼人：2 场，胜 1\n- 平民：1 场，胜 1"
NEW_FORMAT = "## 战绩\n" + RECORD3 + "\n\n## 印象\n- N1 Alice：发言短\n- N2 Bob：（暂无）\n- N4 David：（暂无）\n\n## 经验\n- 先听后说\n"


def write_bytes(store: RosterStore, name: str, text: str, *, newline: str = "\n", bom: bool = True):
    """A file as an editor that puts a byte-order mark in front of it (and maybe uses CRLF) writes it."""
    store._dir.mkdir(parents=True, exist_ok=True)
    path = store._dir / name
    path.write_bytes((BOM if bom else "").encode("utf-8") + text.replace("\n", newline).encode("utf-8"))
    return path


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["LF", "CRLF"])
@pytest.mark.parametrize("text", [NEW_FORMAT, V0130], ids=["new format", "v0.13.0"])
def test_a_memory_file_with_a_byte_order_mark_keeps_its_record_and_the_next_review_counts_on(store, text, newline):
    path = write_bytes(store, "N3.memory.md", text, newline=newline)
    on_disk = path.read_bytes()
    assert on_disk.startswith(b"\xef\xbb\xbf")
    entry = store.entry("N3")
    parts = entry["memory_parts"]
    assert parts["record"] == RECORD3  # the mark did not turn "## 战绩" into plain text: the record is found
    assert not entry["memory"].startswith(BOM) and entry["memory"].startswith("## 战绩\n场次 3")
    assert "战绩" not in parts["legacy"] and "场次" not in parts["legacy"]  # ... and it did not end up in the legacy text either
    assert path.read_bytes() == on_disk  # reading does not rewrite the file
    store.apply_review("N3", role="狼人", won=True, impressions={"N2": "爱附和"})
    tally = RecordTally.parse(store.entry("N3")["memory_parts"]["record"])
    assert (tally.games, tally.wins, tally.by_role) == (4, 3, {"狼人": [3, 2], "平民": [1, 1]})  # counted on, not reset to one game
    written = path.read_bytes()
    assert not written.startswith(b"\xef\xbb\xbf") and b"\r" not in written  # what the review wrote is plain UTF-8, LF


def test_a_byte_order_mark_in_front_of_a_text_is_not_part_of_it():
    assert split_memory(BOM + "## 战绩\n场次 1，胜 1\n\n## 印象\n- N1 Alice：x") == {"战绩": "场次 1，胜 1", "印象": "- N1 Alice：x"}
    assert parse_memory(BOM + NEW_FORMAT, CTX4).record == RECORD3
    assert clean_text(BOM + "文字\r\n", "测试") == "文字"


def test_the_persona_and_the_roster_file_may_have_a_byte_order_mark_too(store):
    write_bytes(store, "N1.persona.md", "冷静的分析派\n", newline="\r\n")
    write_bytes(store, "roster.json", json.dumps({"seats": {"N2": {"name": "Zed"}}}, ensure_ascii=False))
    assert store.entry("N1")["persona"] == "冷静的分析派" and store.snapshot()["seats"][0]["persona"] == "冷静的分析派"
    assert store.entry("N2")["name"] == "Zed"  # (a roster.json that cannot be read hands out the defaults - and the next save forgets the real names)
    store.set_name("N5", "Evelyn")
    assert store.entry("N2")["name"] == "Zed" and store.entry("N5")["name"] == "Evelyn"


# ------------------------------------------------------------------------------------ what cannot be written
LONE = "ok\ud800"


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.set_persona("N1", LONE),
        lambda s: s.set_memory("N1", "## 经验\n- " + LONE),
        lambda s: s.set_memory_parts("N1", impressions={"N2": LONE}),
        lambda s: s.set_memory_parts("N1", experience=[LONE]),
        lambda s: s.set_memory_parts("N1", legacy=LONE),
        lambda s: s.set_name("N1", "Zed\udfff"),
        lambda s: s.validate_name("N1", LONE),
        lambda s: s.validate_texts("N1", persona=LONE),
        lambda s: s.validate_texts("N1", memory=LONE),
        lambda s: s.validate_texts("N1", memory_parts={"impressions": {"N2": LONE}}),
    ],
)
def test_a_text_that_cannot_be_written_is_refused_up_front_with_a_plain_message_and_nothing_is_written(store, call):
    with pytest.raises(RosterError, match="无法保存"):  # (a lone surrogate used to fail at the write, after other parts were saved)
        call(store)
    assert not store._dir.exists() or not any(store._dir.iterdir())
    assert store.entry("N1")["name"] == "Alice"
    check_text("正常的文字 😀", "测试")  # a pair of surrogates is an ordinary character


# ------------------------------------------------------------------------------------ colons in names
@pytest.mark.parametrize("bad", ["A:B", "A：B", ":", "：Zed", "Zed:", "x : y"])
def test_a_new_name_cannot_contain_a_colon_of_either_kind(store, bad):
    for call in (lambda: store.set_name("N1", bad), lambda: store.validate_name("N1", bad), lambda: clean_name(bad)):
        with pytest.raises(RosterError, match="冒号"):
            call()
    assert store.entry("N1")["name"] == "Alice" and not (store._dir / "roster.json").exists()
    assert clean_name(bad, allow_colon=True) == bad.strip()  # (how a name that is already stored is read)


def test_a_stored_name_with_a_colon_is_still_shown_and_the_labels_written_for_it_have_none(store):
    store._dir.mkdir(parents=True)
    (store._dir / "roster.json").write_text(json.dumps({"seats": {"N3": {"name": "A：B"}, "N4": {"name": "x:y"}}}, ensure_ascii=False), encoding="utf-8")
    assert [store.entry("N3")["name"], store.entry("N4")["name"]] == ["A：B", "x:y"]  # read as it is stored
    # a label as an earlier build wrote it (the name with its colon in it) still reads back while that is the name
    write_memory(store, "## 印象\n- N3 A：B：发言短，站边快\n- N4 x:y：稳健\n", "N2")
    assert [store.entry("N2")["memory_parts"]["impressions"][s] for s in ("N3", "N4")] == ["发言短，站边快", "稳健"]
    # what is written now has no colon in the label (it would end the label early when the line is read back) ...
    store.set_memory_parts("N1", impressions={"N3": "发言短，站边快", "N4": "稳健"})
    text = memory_file(store, "N1")
    assert "- N3 A B：发言短，站边快" in text and "- N4 x y：稳健" in text
    assert [store.entry("N1")["memory_parts"]["impressions"][s] for s in ("N3", "N4")] == ["发言短，站边快", "稳健"]
    store.set_name("N3", "Carol")  # ... so that renaming the seat does not turn the end of its old name into the start of the impression
    store.set_name("N4", "David")
    assert [store.entry("N1")["memory_parts"]["impressions"][s] for s in ("N3", "N4")] == ["发言短，站边快", "稳健"]
    assert store.entry("N2")["memory_parts"]["impressions"]["N3"] == "B：发言短，站边快"  # (the old build's label cannot be told once the name is gone)


# ------------------------------------------------------------------------------------ texts that say nothing
@pytest.mark.parametrize(
    "text",
    [
        "", "   ", None, "…", "...", "。。。", "—", "——", "（暂无）", "暂无", "无", "没有", "无变化", "没有变化", "（无变化）", "保持原样", "保持不变", "同上",
        "同上。", "无新信息", "没有新信息", "暂无新信息", "无新信息，保持原样", "沿用旧印象", "略", "- 无", "N/A", "n/a", "No change", "none", "None.", "unchanged",
        "😀",  # (symbols only: nothing that could replace what he thought of a player)
    ],
)
def test_a_text_that_says_nothing_is_not_a_text(text):
    assert is_no_change(text)


@pytest.mark.parametrize(
    "text",
    [
        "发言短", "无法判断他的立场", "没有新信息可是他很可疑", "他的发言和上局一致", "保持怀疑", "不变的站边习惯", "同上一局，爱悍跳",
        "none of his votes matched", "no change in tone but votes late", "发言短 😀", "1",
    ],
)
def test_a_real_text_that_merely_contains_such_words_is_kept(text):
    assert not is_no_change(text)


def test_only_strings_are_lessons_never_the_text_of_something_else():
    assert clean_experience(["先听后说", None, 5, {"a": 1}, ["x"], True, 1.5, "", "（暂无）", "- 别太早表态"]) == ["先听后说", "别太早表态"]
    assert clean_experience(None) == [] and clean_experience(5) == [] and clean_experience({"a": "b"}) == []
    assert clean_experience("一条\n- 两条\n\n") == ["一条", "两条"]


def test_a_review_answer_that_echoes_the_example_of_the_prompt_or_says_no_change_changes_nothing():
    from app.agents.review import answer_example

    ctx = ctx12("N3")
    # the example of the prompt, given back as it is: "…" is no impression and no lesson
    assert parse_review_output(answer_example("N1"), ctx) == ReviewOutput({}, None, "json")
    said_nothing = (
        '{"impressions": {"N1": "…", "N2": "保持原样", "N4": "无变化", "N5": "N/A", "N6": "（暂无）", "N7": "同上。", "N8": "No change"},'
        ' "experience": ["…", "无新信息", "保持原样"]}'
    )
    assert parse_review_output(said_nothing, ctx) == ReviewOutput({}, None, "json")  # (an answer, but one that changes nothing)
    assert parse_review_output('N1：…\nN2：无变化\n经验：\n- 保持原样', ctx) == ReviewOutput({}, None, "lines")
    # ... a real text next to them is taken, the placeholders are not
    mixed = parse_review_output('{"impressions": {"N1": "…", "N2": "发言短"}, "experience": ["…", "先听后说"]}', ctx)
    assert mixed == ReviewOutput({"N2": "发言短"}, ["先听后说"], "json")


def test_only_strings_count_in_a_review_answer_and_numbers_and_objects_are_not_turned_into_text():
    ctx = ctx12("N3")
    out = parse_review_output('{"impressions": {"N1": "发言短"}, "experience": [null, 5, {"a": 1}, ["x"], true, "真经验", 1.5]}', ctx)
    assert out == ReviewOutput({"N1": "发言短"}, ["真经验"], "json")  # (not "None", "5", "{'a': 1}", "['x']", "True", "1.5")
    assert parse_review_output('{"impressions": ["N1：发言短", 5, null, {"a": 1}]}', ctx) == ReviewOutput({"N1": "发言短"}, None, "json")
    # nothing of the right shape: not an answer
    assert parse_review_output('{"experience": [null, 5, {"a": 1}]}', ctx) is None
    assert parse_review_output('{"impressions": {"N1": 5, "N2": null, "N4": {"t": "x"}, "N5": ["x"]}, "experience": [null]}', ctx) is None


@pytest.mark.parametrize(
    "garbage",
    ["[" * 8000, '{"a":' * 3000, '{"impressions": ' + "[" * 5000 + "]" * 5000 + "}", "{" * 8000, '{"impressions": {"N1": "x"}, "pad": ' + "[" * 6000 + "}"],
    ids=["array", "object", "inside impressions", "braces", "valid object then a pit"],
)
def test_deeply_nested_garbage_is_an_unusable_answer_not_a_crash(garbage):
    assert parse_review_output(garbage, ctx12("N3")) is None  # (a RecursionError used to escape: no review_failed line, no clean error)


# ------------------------------------------------------------------------------------ a review and an edit at the same time
def mem(impressions=None, experience=None, *, legacy="", record="") -> MemoryParts:
    ctx = ctx12("N3")
    return MemoryParts(
        record=record, impressions={**{sid: "" for sid in ctx.others}, **(impressions or {})}, experience=list(experience or []), legacy=legacy
    )


def test_a_review_answer_that_gives_back_what_it_was_shown_changes_nothing_and_keeps_the_legacy_text():
    ctx = ctx12("N3")
    base = mem({"N1": "旧一", "N2": "旧二"}, ["旧经验"], legacy="旧笔记", record="场次 1，胜 1")
    again = merge_review(base.copy(), ReviewOutput({"N1": "旧一", "N2": "旧二"}, ["旧经验"], "json"), ctx, base=base)
    assert again == base  # (the prompt allows "copy it as it is": that is no news, and the notes were not tidied up)


def test_the_legacy_text_is_deleted_only_when_the_review_really_took_an_impression_or_a_lesson():
    ctx = ctx12("N3")
    base = mem({"N1": "旧一"}, legacy="旧笔记")
    saying_nothing = [
        ReviewOutput({}, None, "json"),  # {"impressions": {}, "experience": []}
        ReviewOutput({"N1": "…", "N2": "保持原样"}, ["无变化"], "json"),  # only placeholders
        ReviewOutput({"N1": "旧一"}, None, "json"),  # only what he was shown
        ReviewOutput({"N99": "鬼", "N3": "自己"}, None, "json"),  # an unknown seat and himself
        ReviewOutput({}, ["…", "同上"], "json"),
    ]
    for output in saying_nothing:
        assert merge_review(base.copy(), output, ctx, base=base).legacy == "旧笔记", output
    assert merge_review(base.copy(), ReviewOutput({"N2": "新的二"}, None, "json"), ctx, base=base).legacy == ""
    assert merge_review(base.copy(), ReviewOutput({}, ["新经验"], "json"), ctx, base=base).legacy == ""
    assert merge_review(base.copy(), ReviewOutput({"N2": "新的二"}, None, "json"), ctx, base=base, keep_legacy=True).legacy == "旧笔记"
    # the legacy text the user edited meanwhile is his, whatever the review took
    edited = base.copy()
    edited.legacy = "用户刚改的旧记忆"
    assert merge_review(edited, ReviewOutput({"N2": "新的二"}, None, "json"), ctx, base=base).legacy == "用户刚改的旧记忆"


def test_a_text_the_user_changed_while_the_model_worked_stays_as_he_left_it():
    ctx = ctx12("N3")
    base = mem({"N1": "旧一", "N2": "旧二", "N4": ""}, ["旧经验"])
    now = base.copy()
    now.impressions["N1"] = "用户改的一"
    now.experience = ["用户改的经验"]
    answer = ReviewOutput({"N1": "模型新的一", "N2": "模型新的二", "N4": "模型新的四"}, ["模型新经验"], "json")
    after = merge_review(now, answer, ctx, base=base)
    assert after.impressions["N1"] == "用户改的一" and after.experience == ["用户改的经验"]  # edited meanwhile: his stay
    assert after.impressions["N2"] == "模型新的二" and after.impressions["N4"] == "模型新的四"  # untouched meanwhile: the model's
    # a text the model gave back as it was shown does not overwrite his edit either (this is the common case)
    copy = ReviewOutput({"N1": "旧一", "N2": "旧二"}, ["旧经验"], "json")
    assert merge_review(now, copy, ctx, base=base) == now


def test_a_memory_cleared_while_the_model_worked_is_not_brought_back_by_the_answer():
    ctx = ctx12("N3")
    base = mem({"N1": "旧一", "N2": "旧二"}, ["旧经验"])
    answer = ReviewOutput({"N1": "旧一", "N2": "模型的新二", "N4": "全新的四"}, ["旧经验"], "json")
    after = merge_review(mem(), answer, ctx, base=base)  # the user wiped everything meanwhile
    assert after.impressions["N1"] == "" and after.experience == []  # given back as it was shown: no news, nothing comes back
    assert after.impressions["N2"] == ""  # (the model changed it, but he cleared it: his empty text stays)
    assert after.impressions["N4"] == "全新的四"  # nothing was there before and nothing is there now: the model's text
    assert merge_review(mem(), ReviewOutput({}, ["全新的经验"], "json"), ctx, base=mem()).experience == ["全新的经验"]


def test_apply_review_with_the_memory_the_model_was_shown_does_not_undo_an_edit_or_a_clear(store):
    store.set_memory_parts("N1", impressions={"N2": "旧二", "N3": "旧三"}, experience=["旧经验"], legacy="旧笔记")
    _ctx, base = store.load_memory("N1")
    store.set_memory_parts("N1", impressions={"N2": "用户刚改的二", "N3": "用户改的三"})  # the user edits while the model works
    result = store.apply_review(
        "N1", role="平民", won=True, impressions={"N2": "旧二", "N3": "模型新三", "N4": "模型新四"}, experience=["旧经验"], base=base
    )
    parts = store.entry("N1")["memory_parts"]
    assert parts["impressions"]["N2"] == "用户刚改的二"  # the old text given back: no news, his edit is not overwritten
    assert parts["impressions"]["N3"] == "用户改的三"  # the model had a new text, but he edited it meanwhile
    assert parts["impressions"]["N4"] == "模型新四" and parts["experience"] == ["旧经验"] and parts["legacy"] == ""
    assert result.changes == {"impressions": [{"id": "N4", "before": "", "after": "模型新四"}], "experience": None}  # (against what was there)
    assert RecordTally.parse(parts["record"]).games == 1

    _ctx, base = store.load_memory("N1")
    store.clear()  # "clear all memories" while the model works
    store.apply_review("N1", role="平民", won=False, impressions={"N2": "用户刚改的二", "N5": "新五"}, experience=["旧经验"], base=base)
    parts = store.entry("N1")["memory_parts"]
    assert parts["impressions"]["N2"] == "" and parts["impressions"]["N3"] == "" and parts["experience"] == []  # nothing came back
    assert parts["impressions"]["N5"] == "新五"  # ... a text about a seat there was nothing about is new
    assert RecordTally.parse(parts["record"]).games == 1  # (the game that was just reviewed is the only one counted: the old record was cleared)


def test_without_a_base_a_review_is_what_it_always_was_and_a_text_equal_to_the_current_one_is_no_change(store):
    store.set_memory_parts("N1", impressions={"N2": "旧二"})
    result = store.apply_review("N1", role="平民", won=True, impressions={"N2": "旧二", "N3": "新三"})
    assert result.changes["impressions"] == [{"id": "N3", "before": "", "after": "新三"}]


# ------------------------------------------------------------------------------------ a heading in the legacy text
@pytest.mark.parametrize("heading", ["## 印象", "## 战绩", "## 经验", "## 旧记忆（待整理）", "##\t印象", "## 任意标题", "##  战绩  "])
def test_the_legacy_text_cannot_carry_a_line_that_would_become_a_section(store, heading):
    texts = (
        f"第一行\n{heading}\n- N1 Alice：被注入的印象",
        f"{heading}\n只有标题",
        f"第一行\u2028{heading}\n场次 99，胜 99，负 0，胜率 100%",  # (the line breaks of splitlines count: the file is read that way)
        f"第一行\x85{heading}",
        f"第一行\r{heading}",
        f"第一行\x0b{heading}",
    )
    for text in texts:
        for call in (lambda: store.set_memory_parts("N2", legacy=text), lambda: store.validate_texts("N2", memory_parts={"legacy": text})):
            with pytest.raises(RosterError, match="小节标题"):
                call()
    assert not (store._dir / "N2.memory.md").exists()


def test_the_legacy_text_of_a_migrated_file_can_be_saved_back_unchanged_and_lines_that_are_no_headings_are_fine(store):
    write_memory(store, V0130)
    legacy = store.entry("N3")["memory_parts"]["legacy"]
    before = store.entry("N3")["memory"]
    for _ in range(2):  # GET -> PUT, twice: the same text, the same file
        store.set_memory_parts("N3", legacy=legacy)
        assert store.entry("N3")["memory"] == before and memory_file(store) == before + "\n"
    store.set_memory_parts("N3", impressions={"N1": "发言短"})
    assert store.entry("N3")["memory_parts"]["legacy"] == legacy and store.entry("N3")["memory_parts"]["record"] == RECORD3
    odd = "###三级\n##没空格\n  ## 缩进的\n正文 ## 行内\n- N1 Alice：不是印象，只是旧笔记里的一行\n场次 99，胜 99，负 0，胜率 100%"
    store.set_memory_parts("N3", legacy=odd)
    parts = store.entry("N3")["memory_parts"]
    assert parts["legacy"] == odd and parts["record"] == RECORD3 and parts["impressions"]["N1"] == "发言短"  # (still the legacy text: not an impression, not the record)
    assert store.entry("N3")["memory"] == render_memory(*reversed(store.load_memory("N3")))


# ------------------------------------------------------------------------------------ the persona draft is a fixed point
def test_a_template_heading_inside_a_line_of_a_persona_draft_does_not_make_it_drift():
    raw = (
        "【定位】冷静\n【说话风格】\n- 句子短\n【压力下】- 内联\n【决策偏好】\n- 证据不足时宁可弃票。 【口头禅】别急\n"
        "# 【思考方式】【压力下】- 套着标题\n【口头禅】- 我们看票\n"
    )
    out = normalize_persona(raw)
    assert out == (
        "【定位】冷静\n【说话风格】\n- 句子短\n【思考方式】\n- 套着标题\n【决策偏好】\n- 证据不足时宁可弃票。 别急\n【压力下】\n- 内联\n【口头禅】我们看票"
    )
    assert normalize_persona(out) == out  # normalizing a draft again changes nothing (it used to, for lines like these)
    for line in out.splitlines():  # a template heading only stands at the start of a line, never inside an item
        assert line.startswith("【") or ("【" not in line and "】" not in line)
    assert "- - " not in out  # a mark that came with a text is not stacked on the one that is written
    assert normalize_persona("【定位】x\n【说话风格】\n- - - 三层\n* 1. 数字") == "【定位】x\n【说话风格】\n- 三层\n- 数字"


# ------------------------------------------------------------------------------------ bounds and parity
def test_the_record_a_prompt_gets_is_bounded_but_the_file_keeps_all_of_it():
    ctx = ctx12("N3")
    parts = MemoryParts(record="场次 9，胜 9，负 0，胜率 100%\n" + "\n".join(f"- 身份{i}：{i} 场，胜 {i}" for i in range(600)))
    prompt = memory_for_prompt(parts, ctx)
    assert prompt.startswith("## 战绩\n场次 9，胜 9") and prompt.endswith("…") and len(prompt) <= len("## 战绩\n") + RECORD_PROMPT_CHARS
    assert "身份599" in render_memory(parts, ctx)  # (nothing is cut in the file: only what a prompt gets of it)
    short = MemoryParts(record=RECORD3)
    assert memory_for_prompt(short, ctx) == "## 战绩\n" + RECORD3


def test_the_limits_the_review_prompts_quote_are_the_limits_of_the_store():
    from app.agents import review
    from app.agents.brain import PERSONA_PROMPT_CHARS

    assert set(review.DEFAULT_LIMITS) <= set(LIMITS)
    assert {key: LIMITS[key] for key in review.DEFAULT_LIMITS} == review.DEFAULT_LIMITS  # (the agents package does not import the store)
    assert PERSONA_PROMPT_CHARS == PERSONA_CHARS
