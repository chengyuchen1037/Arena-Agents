"""v0.12.1 roster store (E13–E16, E21, E24): names, avatars, persona / memory files, memory pack, review."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.storage.roster import (
    DEFAULT_NAMES,
    MAX_NAME_CHARS,
    MAX_TEXT_CHARS,
    RecordTally,
    RosterError,
    RosterStore,
    clean_name,
    default_name,
    join_memory,
    memory_for_prompt,
    split_memory,
)


@pytest.fixture
def store(tmp_path) -> RosterStore:
    return RosterStore(tmp_path)


def make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


# ------------------------------------------------------------------------------------ defaults
def test_a_fresh_roster_has_the_twelve_default_names_and_empty_texts(store):
    data = store.get()
    assert data["memory_enabled"] is True
    assert [row["id"] for row in data["seats"]] == [f"N{i}" for i in range(1, 13)]
    assert [row["name"] for row in data["seats"]] == DEFAULT_NAMES
    assert all(row["persona"] == "" and row["memory"] == "" and row["avatar"] is None for row in data["seats"])
    assert [row["seat"] for row in data["seats"]] == list(range(1, 13)) and data["seats"][2]["default_name"] == "Carol"
    assert not (store._dir / "roster.json").exists()  # reading never writes


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

    assert store.ensure_avatars(make) is True and made == list(range(1, 13))
    assert [row["avatar"]["taken"] for row in store.get()["seats"]] == list(range(12))  # each saw the earlier ones
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


# ------------------------------------------------------------------------------------ persona / memory files
def test_persona_and_memory_are_plain_files_that_survive_and_can_be_edited_outside(store, tmp_path):
    store.set_persona("N2", "寡言，爱做记录。")
    store.set_memory("N2", "## 经验与印象\n上局被 Alice 骗过。")
    assert (store._dir / "N2.persona.md").read_text(encoding="utf-8") == "寡言，爱做记录。\n"
    fresh = RosterStore(tmp_path)
    assert fresh.entry("N2")["persona"] == "寡言，爱做记录。" and "被 Alice 骗过" in fresh.entry("N2")["memory"]
    (store._dir / "N2.persona.md").write_text("手工改过的设定\n", encoding="utf-8")  # an outside edit
    assert RosterStore(tmp_path).entry("N2")["persona"] == "手工改过的设定"


def test_empty_text_removes_the_file_and_long_text_is_refused(store):
    store.set_persona("N1", "x")
    assert (store._dir / "N1.persona.md").exists()
    store.set_persona("N1", "  \n ")
    assert not (store._dir / "N1.persona.md").exists() and store.entry("N1")["persona"] == ""
    with pytest.raises(RosterError, match="最多"):
        store.set_memory("N1", "字" * (MAX_TEXT_CHARS + 1))
    with pytest.raises(RosterError, match="空字符"):
        store.set_persona("N1", "a\x00b")


def test_the_memory_switch_decides_what_a_game_snapshot_carries(store):
    store.set_persona("N1", "谨慎")
    store.set_memory("N1", "## 经验与印象\n常输")
    snap = store.snapshot()
    assert snap["memory_enabled"] is True and "常输" in snap["seats"][0]["memory"] and snap["seats"][0]["persona"] == "谨慎"
    store.set_memory_enabled(False)
    off = store.snapshot()
    assert off["memory_enabled"] is False and off["seats"][0]["memory"] == "" and off["seats"][0]["persona"] == "谨慎"
    assert "常输" in store.entry("N1")["memory"]  # the file itself is untouched
    assert RosterStore(store.data_dir).get()["memory_enabled"] is False  # and the switch persisted


def test_a_snapshot_is_a_copy_later_edits_do_not_reach(store):
    store.set_memory("N1", "## 经验与印象\n旧")
    snap = store.snapshot()
    store.set_memory("N1", "## 经验与印象\n新")
    assert "旧" in snap["seats"][0]["memory"] and "新" not in snap["seats"][0]["memory"]


# ------------------------------------------------------------------------------------ memory sections
def test_memory_sections_round_trip_and_unheaded_text_is_experience():
    text = "## 战绩\n场次 1，胜 1\n\n## 对局记忆\n第一局\n\n## 经验与印象\nBob 爱撒谎"
    sections = split_memory(text)
    assert sections["战绩"] == "场次 1，胜 1" and sections["对局记忆"] == "第一局" and sections["经验与印象"] == "Bob 爱撒谎"
    assert join_memory(sections) == text
    assert split_memory("只是一句手写的话")["经验与印象"] == "只是一句手写的话"
    assert memory_for_prompt("") == "" and memory_for_prompt("   ") == ""
    custom = split_memory("## 战绩\nx\n\n## 我的备注\ny")
    assert custom["我的备注"] == "y" and "## 我的备注\ny" in join_memory(custom)


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


# ------------------------------------------------------------------------------------ the memory pack
def test_export_then_import_gives_back_the_same_text(store, tmp_path):
    store.set_persona("N1", "性格一")
    store.set_memory("N1", "## 经验与印象\n记忆一")
    store.set_memory("N7", "## 对局记忆\n记忆七")
    pack = store.export_zip()
    names = sorted(zipfile.ZipFile(io.BytesIO(pack)).namelist())
    assert names == ["N1.memory.md", "N1.persona.md", "N7.memory.md"]  # empty seats are not packed

    other = RosterStore(tmp_path / "elsewhere")
    changes = other.import_zip(pack)
    assert [(c["id"], c["persona"], c["memory"]) for c in changes] == [("N1", True, True), ("N7", False, True)]
    assert other.entry("N1")["persona"] == "性格一" and other.entry("N7")["memory"] == "## 对局记忆\n记忆七"


def test_the_dry_run_lists_what_would_change_without_touching_anything(store):
    store.set_memory("N2", "旧记忆")
    pack = make_zip({"N2.memory.md": "新记忆", "N3.persona.md": "新性格", "N4.memory.md": ""})
    preview = store.preview_import(pack)
    assert [(c["id"], c["name"], c["persona"], c["memory"]) for c in preview] == [("N2", "Bob", False, True), ("N3", "Carol", True, False)]
    assert store.entry("N2")["memory"] == "旧记忆" and store.entry("N3")["persona"] == ""


def test_importing_leaves_seats_that_are_not_in_the_pack_alone(store):
    store.set_memory("N1", "留着")
    store.import_zip(make_zip({"N2.memory.md": "新的"}))
    assert store.entry("N1")["memory"] == "留着" and store.entry("N2")["memory"] == "新的"


@pytest.mark.parametrize(
    "data,message",
    [
        (b"", "空"),
        (b"not a zip at all", "zip"),
        (make_zip({"../evil.md": "x"}), "不认识的文件"),
        (make_zip({"N1.memory.md": "x", "notes.txt": "y"}), "不认识的文件"),
        (make_zip({"N01.memory.md": "x"}), "不认识的文件"),
        (make_zip({"sub/N1.memory.md": "x"}), "不认识的文件"),
        (make_zip({"N1.memory.md": "x" * (MAX_TEXT_CHARS * 4 + 1)}), "太大"),
    ],
)
def test_a_bad_pack_is_refused_whole_and_nothing_is_imported(store, data, message):
    store.set_memory("N1", "原样")
    with pytest.raises(RosterError, match=message):
        store.import_zip(data)
    assert store.entry("N1")["memory"] == "原样"


def test_oversized_and_non_utf8_packs_are_refused(store):
    with pytest.raises(RosterError, match="过大"):
        store.parse_zip(b"x" * (2 * 1024 * 1024 + 1))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("N1.memory.md", "坏字节".encode("gbk"))
    with pytest.raises(RosterError, match="UTF-8"):
        store.parse_zip(buffer.getvalue())
    many = make_zip({f"N{i}.memory.md": "x" for i in range(1, 10)} | {f"N{i}.persona.md": "x" for i in range(1, 10)})
    assert len(store.parse_zip(many)) == 9
    with pytest.raises(RosterError, match="太多"):
        store.parse_zip(make_zip({f"N{i % 99 + 1}.{kind}.md": "x" for i in range(60) for kind in ("memory", "persona")} | {f"N1.persona{i}": "x" for i in range(5)}))


def test_seats_beyond_the_table_are_ignored_not_an_error(store):
    store.import_zip(make_zip({"N40.memory.md": "远方", "N1.memory.md": "近处"}))
    assert store.entry("N1")["memory"] == "近处"


def test_clear_empties_the_memory_and_optionally_the_persona(store):
    store.set_persona("N1", "性格")
    store.set_memory("N1", "记忆")
    store.set_memory("N2", "记忆二")
    assert store.clear() == 2
    assert store.entry("N1")["memory"] == "" and store.entry("N2")["memory"] == "" and store.entry("N1")["persona"] == "性格"
    store.set_memory("N1", "又有了")
    assert store.clear(include_persona=True) == 2 and store.entry("N1")["persona"] == ""
    assert store.clear() == 0


# ------------------------------------------------------------------------------------ review
def test_a_review_updates_the_record_and_appends_the_players_own_text(store):
    text = store.apply_review("N3", role="狼人", won=True, review_text="我和 Bob 配合得不错，下次别太早暴露。")
    sections = split_memory(text)
    assert sections["战绩"].splitlines()[0] == "场次 1，胜 1，负 0，胜率 100%" and "- 狼人：1 场，胜 1" in sections["战绩"]
    assert sections["对局记忆"] == "我和 Bob 配合得不错，下次别太早暴露。"

    text = store.apply_review("N3", role="平民", won=False, review_text="被带偏了。")
    sections = split_memory(text)
    assert sections["战绩"].splitlines()[0] == "场次 2，胜 1，负 1，胜率 50%"
    assert sections["对局记忆"] == "我和 Bob 配合得不错，下次别太早暴露。\n\n被带偏了。"
    assert RosterStore(store.data_dir).entry("N3")["memory"] == text  # saved


def test_a_review_keeps_what_the_user_wrote_in_the_other_sections(store):
    store.set_memory("N1", "## 经验与印象\nAlice 总是第一个跳身份。")
    text = store.apply_review("N1", role="平民", won=True, review_text="")
    assert "Alice 总是第一个跳身份。" in text and "场次 1，胜 1" in text and "## 对局记忆" not in text  # an empty review adds nothing


def test_a_review_that_would_overflow_drops_the_oldest_game_memory_first(store):
    store.set_memory("N1", join_memory({"经验与印象": "核心经验"}))
    for index in range(40):
        store.apply_review("N1", role="平民", won=index % 2 == 0, review_text=f"第 {index} 局：" + "字" * 700)
    text = store.entry("N1")["memory"]
    assert len(text) <= MAX_TEXT_CHARS and "核心经验" in text and "场次 40" in text
    assert "第 39 局" in text and "第 0 局" not in text


def test_clean_name_strips_and_returns_the_text():
    assert clean_name("  Zoë  ") == "Zoë"
