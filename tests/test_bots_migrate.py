"""v0.13 roster -> v0.14 bots (H45): names and avatars kept, the persona mapped to L0 by its headings, impressions re-keyed by name,
experiences become low-confidence playbook entries, the record kept; nothing initialized, no model called, nothing of the old
directory touched, idempotent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.bots.types import CoreFiles
from app.providers.models import ModelBinding
from app.storage import botfiles as bf
from app.storage import bots as bots_mod
from app.storage.bots import BotStore, legacy_bot_id, legacy_persona_to_core
from app.storage.roster import DEFAULT_NAMES, parse_legacy_persona, read_legacy_roster
from app.storage.settings import SettingsStore

SID = "demo"

PERSONA = (
    "【定位】冷静的分析派\n"
    "【说话风格】\n- 话少\n- 爱反问\n"
    "【思考方式】\n- 先看票型\n- 再看发言\n"
    "【决策偏好】\n- 宁可慢一点\n"
    "【压力下】\n- 沉默\n"
    "【口头禅】我再想想"
)
MEMORY = (
    "## 战绩\n场次 5，胜 3，负 2，胜率 60%\n- 角色甲：3 场，胜 2\n- 角色乙：2 场，胜 1\n\n"
    "## 印象\n- N1 Alice：（暂无）\n- N2 Bob：发言短，站边快。\n- N3 Carol：爱带节奏，可信度中。\n\n"
    "## 经验\n- 第一天先听完发言再表态。\n- 少打断别人。\n"
)


def write_roster(
    directory: Path,
    *,
    seats: int = 3,
    names: dict[int, str | None] | None = None,
    personas: dict[int, str] | None = None,
    memories: dict[int, str] | None = None,
    memory_enabled: bool = True,
    avatars: bool = True,
) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    pics = bots_mod.pick_avatars(seats + 1, []) if avatars else [None] * (seats + 1)
    roster: dict = {"memory_enabled": memory_enabled, "seats": {}, "moderator": {"avatar": pics[-1]} if pics[-1] else {}}
    for k in range(1, seats + 1):
        row: dict = {}
        if pics[k - 1]:
            row["avatar"] = pics[k - 1]
        name = (names or {}).get(k, DEFAULT_NAMES[k - 1])
        if name is not None:
            row["name"] = name
        roster["seats"][f"N{k}"] = row
    (directory / "roster.json").write_text(json.dumps(roster, ensure_ascii=False), encoding="utf-8")
    for k, text in (personas or {}).items():
        (directory / f"N{k}.persona.md").write_text(text, encoding="utf-8")
    for k, text in (memories or {}).items():
        (directory / f"N{k}.memory.md").write_text(text, encoding="utf-8")
    return roster


def snapshot(directory: Path) -> dict[str, bytes]:
    return {p.relative_to(directory).as_posix(): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


def new_store(tmp_path: Path, seats: int = 3, **kwargs) -> BotStore:
    return BotStore(tmp_path / "data", seats=seats, library_dir=kwargs.pop("library_dir", tmp_path / "no-library"), **kwargs)


def by_name(store: BotStore) -> dict[str, str]:
    return {name: bot_id for bot_id, name in store.names().items()}


def raw_meta(store: BotStore, bot_id: str) -> dict:
    return json.loads((store.bot_dir(bot_id) / "bot.json").read_text(encoding="utf-8"))


# ======================================================================================================================
# the mapping of a persona
# ======================================================================================================================
def test_a_persona_is_mapped_to_l0_by_its_headings():
    core = legacy_persona_to_core(PERSONA)
    assert core == CoreFiles(
        identity="冷静的分析派",
        voice="话少\n爱反问\n口头禅：我再想想",
        mind="先看票型\n再看发言\n决策偏好：宁可慢一点\n压力下：沉默",
    )
    assert bf.validate_core(core, require_complete=True) == []


@pytest.mark.parametrize(
    "persona,expected",
    [
        ("", CoreFiles()),
        ("   \n\n  ", CoreFiles()),
        ("【定位】只有一句话", CoreFiles(identity="只有一句话")),
        ("【说话风格】\n- 话少", CoreFiles(voice="话少")),  # (no identity: L0 stays incomplete)
        ("一个手写的、没有标题的性格。\n第二行。", CoreFiles(identity="一个手写的、没有标题的性格。\n第二行。")),
        ("前面有一句话\n【定位】后面才是定位\n【说话风格】爱笑", CoreFiles(identity="后面才是定位\n前面有一句话", voice="爱笑")),
        ("定位：冒号写法也认\n说话风格：\n- 短句", CoreFiles(identity="冒号写法也认", voice="短句")),
        ("\ufeff【定位】带 BOM\r\n【说话风格】\r\n- 带 CRLF\r\n", CoreFiles(identity="带 BOM", voice="带 CRLF")),
        ("【定位】**加粗**的\n【口头禅】`代码`", CoreFiles(identity="加粗的", voice="口头禅：代码")),
        ("# 一个标题\n正文\n## 另一个", CoreFiles(identity="一个标题\n正文\n另一个")),  # (no line can split a section of the merged file)
    ],
)
def test_odd_personas_are_read_without_loss_or_error(persona, expected):
    core = legacy_persona_to_core(persona)
    assert core == expected
    assert bf.validate_core(core) == []


def test_a_persona_that_is_too_long_is_cut_with_an_ellipsis_not_refused():
    core = legacy_persona_to_core("【定位】" + "身" * 400 + "\n【说话风格】\n- " + "说" * 700 + "\n【思考方式】\n- " + "想" * 500)
    assert len(core.identity) == 300 and core.identity.endswith("…")
    assert len(core.voice) == 600 and len(core.mind) == 400 and core.voice.endswith("…")
    assert bf.validate_core(core) == []


def test_the_persona_reader_keeps_what_comes_before_the_first_heading():
    assert parse_legacy_persona("开头\n【定位】甲\n- 乙\n【压力下】\n1. 丙\n* 丁") == {"": ["开头"], "定位": ["甲", "乙"], "压力下": ["丙", "丁"]}
    assert parse_legacy_persona("") == {} and parse_legacy_persona(None) == {}  # type: ignore[arg-type]


# ======================================================================================================================
# the whole roster
# ======================================================================================================================
def test_a_roster_becomes_bots_and_the_old_directory_is_left_alone(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    roster = write_roster(
        roster_dir,
        names={2: "Bobby"},
        personas={1: PERSONA},
        memories={1: MEMORY.replace("- N3 Carol", "- N3 Carol"), 2: "## 战绩\n场次 1，胜 0，负 1，胜率 0%\n- 角色甲：1 场，胜 0\n"},
        memory_enabled=False,
    )
    before = snapshot(roster_dir)
    store = new_store(tmp_path)
    ids = store.import_legacy_roster(roster_dir, scenario_id=SID)
    assert snapshot(roster_dir) == before  # (read only)
    assert store.lineup() == ids and len(ids) == 3
    assert [store.name_of(i) for i in ids] == ["Alice", "Bobby", "Carol"]
    assert [raw_meta(store, i)["avatar"] for i in ids] == [roster["seats"][f"N{k}"]["avatar"] for k in (1, 2, 3)]
    assert store.moderator()["avatar"] == roster["moderator"]["avatar"] and store.memory_enabled() is False
    alice, bobby, carol = ids
    assert all(raw_meta(store, i)["origin"] == "migrated" and raw_meta(store, i)["init"]["status"] == "uninitialized" for i in ids)
    assert store.core(alice) == legacy_persona_to_core(PERSONA) and store.core(alice).complete
    assert store.core(bobby) == CoreFiles() and store.core(carol) == CoreFiles()  # (an empty persona: L0 is missing)
    # the impressions are kept by the NAME of the seat they were about (as the roster named it)
    assert bf.parse_people(store.read_part(alice, "people")) == {"Bobby": "发言短，站边快。", "Carol": "爱带节奏，可信度中。"}
    assert store.read_part(bobby, "people") == ""
    # the experiences became playbook entries with the lowest confidence, filed under the scenario
    entries = bf.parse_playbook(store.read_part(alice, "playbook", SID))
    assert [(e.text, e.confidence, e.support, e.counter, e.source, e.tags) for e in entries] == [
        ("第一天先听完发言再表态。", "低", 0, 0, "迁移自 v0.13 的经验", ()),
        ("少打断别人。", "低", 0, 0, "迁移自 v0.13 的经验", ()),
    ]
    assert store.read_part(bobby, "playbook", SID) == ""
    # the record is kept
    assert bf.read_record(store.bot_dir(alice)).by_role == {"角色甲": [3, 2], "角色乙": [2, 1]} and bf.read_record(store.bot_dir(alice)).games == 5
    assert store.load_mind(alice, SID).record.startswith("场次 5，胜 3，负 2，胜率 60%")
    assert store.load_mind(bobby, SID).record.startswith("场次 1，胜 0")
    assert store.load_mind(carol, SID).record == ""
    # nothing was initialized: no notes, no digest; and the ids are the ones the migration makes
    assert store.read_part(alice, "arch_notes") == "" and store.read_part(alice, "digest", SID) == ""
    assert ids == [legacy_bot_id(1, "Alice"), legacy_bot_id(2, "Bobby"), legacy_bot_id(3, "Carol")]
    # a bot that has experiences has learned something: it is frozen (renaming it needs a confirmation)
    assert store.is_frozen(alice) and not store.is_frozen(bobby)
    assert store.check_integrity() == []


def test_impressions_follow_the_names_not_the_seats(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    memories = {
        1: "## 印象\n- N2 Bob：话多。\n- N3 Carol：话少。\n- N1 自己：不会被收下\n- N9 Nobody：没有这个座位\n",
        2: "## 印象\n- N1 Alice：（暂无）\n- N3 Carol:半角冒号也行。\n",
        3: "## 印象\n- N1 Alice：爱带节奏。\n- N2 Bob： \n",
    }
    write_roster(roster_dir, names={1: "Ann", 2: "Ben", 3: "Cat"}, memories=memories)
    store = new_store(tmp_path)
    ann, ben, cat = store.import_legacy_roster(roster_dir, scenario_id=SID)
    assert bf.parse_people(store.read_part(ann, "people")) == {"Ben": "话多。", "Cat": "话少。"}
    assert bf.parse_people(store.read_part(ben, "people")) == {"Cat": "半角冒号也行。"}
    assert bf.parse_people(store.read_part(cat, "people")) == {"Ann": "爱带节奏。"}
    # (what is mounted in a game follows the names)
    assert store.load_mind(ann, SID, others=["Ben", "Cat"]).people == {"Ben": "话多。", "Cat": "话少。"}


def test_a_v0_13_0_memory_file_is_read_and_its_old_text_is_not_migrated(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    old = "## 战绩\n场次 2，胜 1，负 1，胜率 50%\n\n## 对局记忆\n第一局：太保守。\n\n## 经验与印象\nBob 爱撒谎。\n"
    write_roster(roster_dir, memories={1: old})
    (roster_dir / "N1.memory.md").write_bytes(b"\xef\xbb\xbf" + old.replace("\n", "\r\n").encode("utf-8"))  # (a BOM and CRLF too)
    store = new_store(tmp_path)
    alice = store.import_legacy_roster(roster_dir, scenario_id=SID)[0]
    assert bf.read_record(store.bot_dir(alice)).games == 2 and bf.read_record(store.bot_dir(alice)).wins == 1  # the record survives
    assert store.read_part(alice, "people") == "" and store.read_part(alice, "playbook", SID) == ""
    assert not any("太保守" in p.read_text(encoding="utf-8", errors="ignore") for p in store.root.rglob("*") if p.is_file())
    assert (roster_dir / "N1.memory.md").read_bytes().startswith(b"\xef\xbb\xbf")  # (and the old file is as it was)


def test_experiences_are_cut_to_an_entry_and_need_a_scenario_to_live_in(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    long_lesson = "很长的一条经验" * 20
    write_roster(roster_dir, memories={1: f"## 经验\n- {long_lesson}\n- 第二条\n- 第三条\n"})
    store = new_store(tmp_path)
    alice = store.import_legacy_roster(roster_dir, scenario_id=SID)[0]
    entries = bf.parse_playbook(store.read_part(alice, "playbook", SID))
    assert len(entries) == 3 and len(entries[0].text) == 80 and entries[0].text.endswith("…") and bf.check_playbook(entries) == []
    other = new_store(tmp_path / "other")
    alice2 = other.import_legacy_roster(roster_dir, scenario_id="")[0]  # (no scenario: nowhere to put them)
    assert not list((other.bot_dir(alice2) / "rules").glob("*")) if (other.bot_dir(alice2) / "rules").exists() else True
    assert not other.is_frozen(alice2)
    with pytest.raises(bf.BotFileError):
        new_store(tmp_path / "third").import_legacy_roster(roster_dir, scenario_id="../escape")


def test_the_default_scenario_of_the_program_is_used_when_none_is_given(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    write_roster(roster_dir, memories={1: "## 经验\n- 一条经验\n"})
    assert bots_mod.default_scenario_id() == "werewolf_12p"  # (config.yml: scenarios/werewolf_12p.md, its front matter id)
    store = new_store(tmp_path)
    alice = store.import_legacy_roster(roster_dir)[0]
    assert (store.bot_dir(alice) / "rules" / "werewolf_12p" / "playbook.md").is_file()
    assert [e.text for e in bf.parse_playbook(store.read_part(alice, "playbook", "werewolf_12p"))] == ["一条经验"]


# ======================================================================================================================
# odd rosters
# ======================================================================================================================
def test_names_that_are_not_valid_any_more_are_made_valid_and_unique(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    write_roster(roster_dir, seats=5, names={1: "Re:Mix", 2: "re mix", 3: "a" * 30, 4: None, 5: "bob"}, memories={5: "## 印象\n- N1 Re:Mix：旧名字里带冒号。\n"})
    store = new_store(tmp_path, seats=5)
    ids = store.import_legacy_roster(roster_dir, scenario_id=SID)
    names = [store.name_of(i) for i in ids]
    assert names == ["Re Mix", "re mix2", "Carol", "David", "bob"]  # (colon -> space; the same name twice: numbered; too long or none: the default of the seat, as v0.13 read it)
    assert len({n.casefold() for n in names}) == 5 and all(store.validate_name(n, exclude=i) == n for n, i in zip(names, ids))
    assert bf.parse_people(store.read_part(ids[4], "people")) == {"Re Mix": "旧名字里带冒号。"}  # (the key is the name as it is now)
    assert store.load_mind(ids[4], SID, others=["Re Mix"]).people == {"Re Mix": "旧名字里带冒号。"}


def test_a_missing_or_broken_roster_still_gives_a_table(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    store = new_store(tmp_path)
    ids = store.import_legacy_roster(empty, scenario_id=SID)
    assert [store.name_of(i) for i in ids] == ["Alice", "Bob", "Carol"] and all(not store.core(i).complete for i in ids)
    assert all(raw_meta(store, i)["avatar"] for i in ids) and store.moderator()["avatar"]  # (no avatars in the roster: new ones)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "roster.json").write_text("not json", encoding="utf-8")
    (broken / "N2.persona.md").write_bytes(b"\xff\xfe\x00bad bytes")
    other = new_store(tmp_path / "x")
    assert len(other.import_legacy_roster(broken, scenario_id=SID)) == 3
    nowhere = new_store(tmp_path / "y")
    assert len(nowhere.import_legacy_roster(tmp_path / "does-not-exist", scenario_id=SID)) == 3
    assert read_legacy_roster(tmp_path / "does-not-exist", 2).seats[1].name == "Bob"


def test_the_seats_of_the_old_roster_beyond_the_table_are_ignored(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    write_roster(roster_dir, seats=12, personas={7: PERSONA, 2: PERSONA}, memories={2: "## 印象\n- N7 Grace：爱带节奏。\n- N3 Carol：话少。\n"})
    store = new_store(tmp_path, seats=3)
    alice, bob, carol = store.import_legacy_roster(roster_dir, scenario_id=SID)
    assert store.core(bob).complete and bf.parse_people(store.read_part(bob, "people")) == {"Carol": "话少。"}  # (Grace is not at this table)


def test_an_avatar_that_is_not_in_the_roster_gets_a_new_one_distinct_from_the_others(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    roster = write_roster(roster_dir, seats=3)
    del roster["seats"]["N2"]["avatar"]
    (roster_dir / "roster.json").write_text(json.dumps(roster, ensure_ascii=False), encoding="utf-8")
    store = new_store(tmp_path)
    ids = store.import_legacy_roster(roster_dir, scenario_id=SID)
    avatars = [raw_meta(store, i)["avatar"] for i in ids]
    assert avatars[0] == roster["seats"]["N1"]["avatar"] and avatars[2] == roster["seats"]["N3"]["avatar"]
    assert avatars[1] and avatars[1] not in (avatars[0], avatars[2], store.moderator()["avatar"])


# ======================================================================================================================
# idempotent, replacing, the bindings
# ======================================================================================================================
def test_migrating_the_same_roster_twice_gives_the_same_bots(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    write_roster(roster_dir, personas={1: PERSONA}, memories={1: MEMORY})
    store = new_store(tmp_path, clock=lambda: 1_763_600_000.0)
    first = store.import_legacy_roster(roster_dir, scenario_id=SID)
    state = snapshot(store.root)
    second = store.import_legacy_roster(roster_dir, scenario_id=SID)
    assert second == first and snapshot(store.root) == state  # (byte for byte)
    assert sorted(p.name for p in store.root.iterdir()) == sorted([*first, "lineup.json"])
    # a table that exists is left alone when asked not to replace it
    store.write_part(first[0], "digest", "我自己写的要点", SID)
    assert store.import_legacy_roster(roster_dir, scenario_id=SID, replace=False) == first
    assert store.read_part(first[0], "digest", SID) == "我自己写的要点"
    store.import_legacy_roster(roster_dir, scenario_id=SID)  # (replace: what was written since is gone)
    assert store.read_part(first[0], "digest", SID) == ""


def test_the_bots_that_were_there_are_replaced_with_their_bindings(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    write_roster(roster_dir, names={1: "Ann"})
    store = new_store(tmp_path, library_dir=tmp_path / "lib")
    store.bootstrap()
    old = store.lineup()
    settings = SettingsStore(store.data_dir)
    low = ModelBinding(provider_id="p", model_id="m1", reasoning_depth="low")
    high = ModelBinding(provider_id="p", model_id="m2", reasoning_depth="high")
    settings.save_bot_binding(old[0], low)  # (a binding of a bot that is about to be replaced)
    # what v0.13 saved by seat sits in settings.json next to it, as in a data directory that is being restored from an old archive
    payload = json.loads((store.data_dir / "settings.json").read_text(encoding="utf-8"))
    payload["default_bindings"].update({"N2": high.model_dump(mode="json"), "moderator": low.model_dump(mode="json")})
    (store.data_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")
    new = store.import_legacy_roster(roster_dir, scenario_id=SID)
    assert not set(old) & set(new) and sorted(p.name for p in store.root.iterdir()) == sorted([*new, "lineup.json"])  # the old directories are gone
    stored = json.loads((store.data_dir / "settings.json").read_text(encoding="utf-8"))["default_bindings"]
    assert set(stored) == {new[1], "moderator"} and settings.get_bot_binding(new[1]) == high  # (seat 2 -> the bot in seat 2; the dead bot's is forgotten)
    assert settings.get_default_bindings()["N2"] == high


def test_a_bot_without_a_persona_gets_the_default_core_of_its_name(tmp_path: Path):
    """H59: v0.13 personas were empty by default; a bot that keeps its default name gets the default library's L0 for it."""
    lib = tmp_path / "lib"
    for name in ("Alice", "Bob"):
        (lib / name / "core").mkdir(parents=True)
        (lib / name / "bot.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        for key in ("identity", "voice", "mind"):
            (lib / name / "core" / f"{key}.md").write_text(f"{name} 的 {key}\n", encoding="utf-8")
    roster_dir = tmp_path / "roster"
    write_roster(roster_dir, names={2: "Bobby"}, personas={1: PERSONA}, memories={3: MEMORY})
    store = new_store(tmp_path / "a", library_dir=lib)
    alice, bobby, carol = store.import_legacy_roster(roster_dir, scenario_id=SID)  # (the default: fill)
    assert store.core(alice) == legacy_persona_to_core(PERSONA)  # (a persona that was written is never replaced)
    assert store.core(bobby) == CoreFiles() and store.core(carol) == CoreFiles()  # (no default is named Bobby or Carol here)
    # a roster whose seats keep the default names
    write_roster(tmp_path / "roster2", names={2: "bob"}, memories={2: MEMORY.replace("N3 Carol", "N3 Carol")})
    other = new_store(tmp_path / "b", library_dir=lib)
    ids = other.import_legacy_roster(tmp_path / "roster2", scenario_id=SID)
    assert other.core(ids[0]).identity == "Alice 的 identity" and other.core(ids[1]).voice == "Bob 的 voice"  # (any case of the name)
    assert other.core(ids[2]) == CoreFiles()
    assert raw_meta(other, ids[1])["origin"] == "migrated" and other.name_of(ids[1]) == "bob"  # (the name stays what the roster had)
    assert bf.read_record(other.bot_dir(ids[1])).games == 5 and other.is_frozen(ids[1])  # (and so does the memory)
    assert other.summary(ids[0])["core"]["complete"] is True
    # the switch: without it the core stays missing, as in H45
    off = new_store(tmp_path / "c", library_dir=lib)
    ids = off.import_legacy_roster(tmp_path / "roster2", scenario_id=SID, fill_missing_core=False)
    assert all(off.core(i) == CoreFiles() for i in ids)


# ======================================================================================================================
# the first start of a v0.13 data directory
# ======================================================================================================================
def test_the_first_start_migrates_the_old_roster_of_the_data_directory(isolated_external_storage: Path):
    from app.storage import clear_store_caches

    data = isolated_external_storage
    roster_dir = data / "roster"
    write_roster(roster_dir, seats=12, names={1: "Ann", 12: "Zed"}, personas={1: PERSONA}, memories={1: MEMORY.replace("N2 Bob", "N2 Bob").replace("N3 Carol", "N3 Carol")})
    before = snapshot(roster_dir)
    store = bots_mod.get_bot_store()
    names = list(store.names().values())
    assert len(names) == 12 and names[0] == "Ann" and names[11] == "Zed" and names[1] == "Bob"
    assert store.core(store.lineup()[0]) == legacy_persona_to_core(PERSONA) and raw_meta(store, store.lineup()[0])["origin"] == "migrated"
    library = {b.name: b for b in store.library()}
    assert store.core(store.lineup()[1]) == bf.read_core(library["Bob"].path / "core") and store.core(store.lineup()[1]).complete  # (H59: default names get the default L0)
    assert store.core(store.lineup()[11]) == CoreFiles()  # (a bot that was renamed to something that is no default: no L0)
    assert snapshot(roster_dir) == before  # (the old roster is kept)
    assert bf.parse_people(store.read_part(store.lineup()[0], "people")) == {"Bob": "发言短，站边快。", "Carol": "爱带节奏，可信度中。"}
    # once the bots exist the roster is not read again
    (roster_dir / "roster.json").write_text(json.dumps({"seats": {"N1": {"name": "Changed"}}}), encoding="utf-8")
    clear_store_caches()
    assert bots_mod.get_bot_store().name_of(store.lineup()[0]) == "Ann"
    # a data directory with bots but a lost line-up is rebuilt from the bots, not from the roster
    (data / "bots" / "lineup.json").unlink()
    clear_store_caches()
    assert sorted(bots_mod.get_bot_store().lineup()) == sorted(store.lineup())


def test_a_migration_that_fails_falls_back_to_the_default_library(isolated_external_storage: Path, monkeypatch):
    data = isolated_external_storage
    write_roster(data / "roster", seats=12)

    def boom(self, roster_dir, **kwargs):
        # (it got as far as writing a bot)
        self._create_bot(name="Half", avatar=None, origin="migrated")
        raise RuntimeError("something is wrong with this roster")

    monkeypatch.setattr(BotStore, "import_legacy_roster", boom)
    store = bots_mod.get_bot_store()
    assert len(store.lineup()) == 12 and "Half" not in store.names().values()  # (nothing of the failed attempt stays)
    assert sorted(p.name for p in (data / "bots").iterdir()) == sorted([*store.lineup(), "lineup.json"])


def test_without_an_old_roster_the_first_start_uses_the_default_library(isolated_external_storage: Path):
    store = bots_mod.get_bot_store()
    assert len(store.lineup()) == 12 and [store.name_of(i) for i in store.lineup()] == list(DEFAULT_NAMES)
    assert all(raw_meta(store, i)["origin"] in ("default", "user") for i in store.lineup())


def test_the_old_reader_is_lenient_about_everything(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    roster_dir.mkdir()
    (roster_dir / "roster.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"seats": {"N1": {"name": "Ann", "avatar": {"v": 1}}, "N2": "oops", "N3": {"name": ["x"], "avatar": []}}}).encode("utf-8"))
    (roster_dir / "N1.persona.md").write_bytes(b"\xef\xbb\xbf" + "【定位】带 BOM 的\n".encode("utf-8"))
    (roster_dir / "N1.memory.md").write_text("## 印象\n- N2 Bob：话少\n", encoding="utf-8")
    legacy = read_legacy_roster(roster_dir, 3)
    assert legacy.memory_enabled is True and legacy.moderator_avatar is None
    assert [r.name for r in legacy.seats] == ["Ann", "Bob", "Carol"] and legacy.names == {"N1": "Ann", "N2": "Bob", "N3": "Carol"}
    assert legacy.seats[0].avatar == {"v": 1} and legacy.seats[1].avatar is None and legacy.seats[2].avatar is None
    assert legacy.seats[0].persona == "【定位】带 BOM 的" and legacy.seats[0].parts.impressions["N2"] == "话少"
    assert legacy.seats[1].persona == "" and legacy.seats[1].parts.empty


def test_a_legacy_name_is_made_valid_without_losing_what_can_be_kept():
    from app.storage.bots import _legacy_name

    assert _legacy_name("Re:Mix", 1) == "Re Mix" and _legacy_name("a：b", 1) == "a b" and _legacy_name("  x  y  ", 1) == "x y"
    assert _legacy_name("a" * 30, 3) == "a" * 20
    assert _legacy_name('a"b<c>d\\e', 1) == "abcde"
    assert _legacy_name("::", 4) == "David" and _legacy_name("", 2) == "Bob" and _legacy_name(None, 3) == "Carol"  # type: ignore[arg-type]
