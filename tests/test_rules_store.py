"""``app.storage.rules.RulesStore``: the scenarios and their study materials in the data directory (contract §13.2, H8, H39).

The first run copies the default scenario, a default that was never touched follows the program, what the user imported (or edited by
hand) is theirs, a scenario is only saved when the loader accepts it, materials are small files named by an ASCII slug."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest

from app.bots.types import materials_hash, sha256_text
from app.storage import rules as rules_mod
from app.storage.rules import (
    MATERIAL_TITLE_CHARS,
    MAX_MATERIAL_BYTES,
    MAX_MATERIALS,
    MAX_SCENARIO_BYTES,
    MAX_SCENARIOS,
    RulesError,
    RulesNotFound,
    RulesStore,
    default_scenario_id,
    default_scenario_path,
    get_rules_store,
    scenario_info,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = (ROOT / "scenarios" / "werewolf_12p.md").read_text(encoding="utf-8")
MINIMAL = (ROOT / "scenarios" / "minimal_test.md").read_text(encoding="utf-8")
SID = "werewolf_12p"


class Clock:
    def __init__(self, start: int = 1_763_600_000) -> None:
        self.now = start

    def __call__(self) -> float:
        return float(self.now)


def with_version(text: str, version: str) -> str:
    return re.sub(r"^version:.*$", f"version: {version}", text, count=1, flags=re.MULTILINE)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def default_file(tmp_path: Path) -> Path:
    path = tmp_path / "default-scenario.md"
    path.write_text(SCENARIO, encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def store(tmp_path: Path, default_file: Path, clock: Clock) -> RulesStore:
    rules_store = RulesStore(tmp_path / "data", default_path=default_file, clock=clock)
    rules_store.ensure_installed()
    return rules_store


def refused(call, *args, **kwargs) -> RulesError:
    with pytest.raises(RulesError) as info:
        call(*args, **kwargs)
    return info.value


# ======================================================================================================================
# the first run
# ======================================================================================================================
def test_the_first_run_copies_the_default_scenario_into_the_data_directory(tmp_path: Path, default_file: Path, clock: Clock):
    data = tmp_path / "data"
    store = RulesStore(data, default_path=default_file, clock=clock)
    assert not data.exists()  # (building a store creates nothing)
    store.ensure_installed()
    folder = data / "rules" / SID
    assert (folder / "scenario.md").read_bytes() == SCENARIO.encode("utf-8")
    meta = json.loads((folder / "source.json").read_text(encoding="utf-8"))
    assert meta == {"origin": "default", "version": "2.2.0", "title": "12 人标准局狼人杀（暗牌局）", "imported_at": None, "sha256": sha256_text(SCENARIO)}
    assert store.ids() == [SID] and store.has(SID) and not store.has("nothing") and store.default_id() == SID
    assert store.scenario_text(SID) == SCENARIO and store.scenario_path(SID) == folder / "scenario.md"
    assert store.source(SID) == {"origin": "default", "version": "2.2.0", "imported_at": None, "sha256": sha256_text(SCENARIO)}
    assert store.materials(SID) == {} and not (folder / "materials").exists()  # (no materials by default, C10)
    before = {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}
    RulesStore(data, default_path=default_file, clock=clock).ensure_installed()
    assert {p.name: p.stat().st_mtime_ns for p in folder.iterdir()} == before  # (a second start changes nothing)


def test_the_real_default_is_the_scenario_config_yml_names(isolated_external_storage: Path):
    store = get_rules_store()
    assert store.ids() == [SID] and default_scenario_id() == SID and default_scenario_path().name == "werewolf_12p.md"
    assert store.scenario_text(SID) == SCENARIO  # (the hash the bots are stamped with is the one of the shipped file)
    assert sha256_text(store.scenario_text(SID)) == store.source(SID)["sha256"]
    assert (isolated_external_storage / "rules" / SID / "scenario.md").is_file()


def test_the_process_wide_store_follows_the_data_directory(isolated_external_storage: Path, monkeypatch, tmp_path: Path):
    from app.storage import clear_store_caches

    first = get_rules_store()
    assert first is get_rules_store()
    other = tmp_path / "other-data"
    monkeypatch.setenv("ARENA_DATA_DIR", str(other))
    second = get_rules_store()
    assert second is not first and (other / "rules" / SID / "scenario.md").is_file()
    clear_store_caches()
    assert get_rules_store() is not second


def test_a_default_that_cannot_be_loaded_is_logged_and_not_installed(tmp_path: Path, caplog):
    broken = tmp_path / "broken.md"
    broken.write_text(SCENARIO.replace("schema_version: 2", "schema_version: 3", 1), encoding="utf-8")
    store = RulesStore(tmp_path / "data", default_path=broken)
    with caplog.at_level("WARNING"):
        store.ensure_installed()
    assert "cannot be loaded" in caplog.text and store.ids() == []
    error = refused(store.reset, SID)
    assert error.status == 503 and error.code == "default_invalid" and "schema_version" in str(error)
    missing = RulesStore(tmp_path / "data2", default_path=tmp_path / "nothing.md")
    missing.ensure_installed()
    assert missing.ids() == [] and missing.default_id() is None
    assert refused(missing.reset, SID).code == "no_default"


# ======================================================================================================================
# unknown scenarios
# ======================================================================================================================
@pytest.mark.parametrize("sid", ["nothing", "", "../escape", "a/b", "a b", "x" * 80, "..", None, 5])
def test_an_unknown_or_impossible_id_is_not_found(store: RulesStore, sid):
    for call in (store.scenario_text, store.scenario_path, store.source, store.item, store.materials, store.material_infos):
        with pytest.raises(RulesNotFound) as info:
            call(sid)
        assert info.value.status == 404 and info.value.code == "not_found" and "没有这个剧本" in str(info.value)
    assert not store.has(sid)  # type: ignore[arg-type]
    assert not (store.data_dir / "escape").exists() and sorted(p.name for p in store.data_dir.iterdir()) == ["rules"]


# ======================================================================================================================
# a scenario the user hands in
# ======================================================================================================================
def test_an_imported_scenario_is_stored_normalized_and_is_the_users(store: RulesStore, clock: Clock):
    custom = with_version(SCENARIO, "2.3.0")
    messy = "\ufeff" + custom.replace("\n", "\r\n") + "\r\n\r\n   \r\n"
    clock.now = 1_763_700_000
    source = store.set_scenario(SID, messy)
    assert (store.root / SID / "scenario.md").read_bytes() == custom.encode("utf-8")  # (BOM gone, LF, one final newline)
    assert source == {"origin": "imported", "version": "2.3.0", "imported_at": 1_763_700_000, "sha256": sha256_text(custom)}
    item = store.item(SID)
    assert item["version"] == "2.3.0" and item["origin"] == "imported" and item["chars"] == len(custom) and item["problem"] is None
    assert item["title"] == "12 人标准局狼人杀（暗牌局）" and set(item) == {
        "id", "version", "title", "origin", "imported_at", "sha256", "chars", "materials", "problem",
    }
    assert json.loads((store.root / SID / "source.json").read_text(encoding="utf-8"))["version"] == "2.3.0"
    # putting back exactly what the program ships makes it the default again, by content
    back = store.set_scenario(SID, SCENARIO)
    assert back["origin"] == "default" and back["imported_at"] is None and back["version"] == "2.2.0"


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda t: "", "剧本是空的"),
        (lambda t: "   \n\n", "剧本是空的"),
        (lambda t: t.replace("schema_version: 2", "schema_version: 3", 1), "schema_version 必须是 2（收到 3）"),
        (lambda t: t.split("---", 2)[2], "剧本必须以 YAML front matter（---）开头"),
        (lambda t: t.replace("setup:", "setup: [", 1), "front matter 不是合法的 YAML"),
        (lambda t: t.replace("id: werewolf_12p", "id: other_game", 1), "剧本文件里的 id 是「other_game」，与这个剧本的 id「werewolf_12p」不同"),
        (lambda t: t.replace("manual_heading: 主持人手册", "manual_heading: 没有这一章", 1), "manual_heading"),
        (lambda t: t + "\x00", "含有空字符"),
        (lambda t: t + "\ud800", "无法保存的字符"),
        (lambda t: t + "\n" + "补" * MAX_SCENARIO_BYTES, f"剧本最多 {MAX_SCENARIO_BYTES // 1024} KB"),
    ],
)
def test_a_scenario_the_loader_would_refuse_is_refused_with_its_own_explanation_and_nothing_is_written(store: RulesStore, mutate, fragment):
    before = (store.root / SID / "scenario.md").read_bytes(), (store.root / SID / "source.json").read_bytes()
    error = refused(store.set_scenario, SID, mutate(SCENARIO))
    assert error.status == 400 and error.code == "invalid" and fragment in str(error) and error.problems
    assert store.validate_scenario(SID, mutate(SCENARIO)) == error.problems
    assert ((store.root / SID / "scenario.md").read_bytes(), (store.root / SID / "source.json").read_bytes()) == before


def test_the_message_of_the_loader_is_what_the_user_reads(store: RulesStore):
    error = refused(store.set_scenario, SID, SCENARIO.replace("schema_version: 2", "schema_version: 3", 1))
    assert error.problems == [f"{SID}.md: schema_version 必须是 2（收到 3）"]  # (the file is named by its id: no path of the server)
    assert store.validate_scenario(SID, SCENARIO) == []
    for bad in (None, 5):
        assert store.validate_scenario(SID, bad)  # type: ignore[arg-type]


def test_an_id_that_cannot_be_a_folder_name_is_refused_before_anything_else(store: RulesStore):
    for sid in ("../x", "a b", "", "x" * 65, ".hidden", "带中文", None):
        error = refused(store.set_scenario, sid, SCENARIO)
        assert error.status == 400 and "剧本 id 只能由字母、数字" in str(error), sid
        assert refused(store.reset, sid).status == 400
    assert not (store.data_dir / "x").exists() and store.ids() == [SID]


def test_a_new_scenario_can_be_added_and_the_default_stays_first(store: RulesStore, clock: Clock):
    clock.now = 1_763_750_000
    source = store.set_scenario("minimal_test", MINIMAL)
    assert source["origin"] == "imported" and source["imported_at"] == 1_763_750_000 and source["version"] == "1.0.0"
    store.set_scenario("a_scenario", MINIMAL.replace("id: minimal_test", "id: a_scenario", 1))
    assert store.ids() == [SID, "a_scenario", "minimal_test"]  # (the default first, then by id)
    assert [item["id"] for item in store.items()] == store.ids()
    assert store.item("minimal_test")["title"] == "最小测试剧本"
    # a scenario that is not the default has no default to go back to
    error = refused(store.reset, "minimal_test")
    assert error.code == "no_default" and "没有默认版本" in str(error) and store.scenario_text("minimal_test") == MINIMAL
    # the number of scenarios is limited
    for index in range(MAX_SCENARIOS - 3):
        store.set_scenario(f"s{index}", MINIMAL.replace("id: minimal_test", f"id: s{index}", 1))
    assert len(store.ids()) == MAX_SCENARIOS
    error = refused(store.set_scenario, "one_too_many", MINIMAL.replace("id: minimal_test", "id: one_too_many", 1))
    assert f"最多 {MAX_SCENARIOS} 个剧本" in str(error) and len(store.ids()) == MAX_SCENARIOS
    store.set_scenario("s0", MINIMAL.replace("id: minimal_test", "id: s0", 1).replace("1.0.0", "1.0.1"))  # (replacing one is fine)
    assert store.item("s0")["version"] == "1.0.1"


def test_reset_brings_the_default_back_and_keeps_the_materials(store: RulesStore, clock: Clock):
    store.set_material(SID, "notes", "我的学习材料")
    store.set_scenario(SID, with_version(SCENARIO, "9.9.9"))
    assert store.source(SID)["origin"] == "imported"
    source = store.reset(SID)
    assert source == {"origin": "default", "version": "2.2.0", "imported_at": None, "sha256": sha256_text(SCENARIO)}
    assert store.scenario_text(SID) == SCENARIO and store.materials(SID) == {"notes": "我的学习材料\n"}  # (the materials are the user's)
    assert store.reset(SID) == source


# ======================================================================================================================
# the program is upgraded, the user edits by hand
# ======================================================================================================================
def test_an_untouched_default_follows_the_program_when_it_ships_another_scenario(tmp_path: Path, default_file: Path, clock: Clock):
    data = tmp_path / "data"
    RulesStore(data, default_path=default_file, clock=clock).ensure_installed()
    newer = with_version(SCENARIO, "2.3.0")
    default_file.write_text(newer, encoding="utf-8", newline="\n")  # (a new version of the program)
    upgraded = RulesStore(data, default_path=default_file, clock=clock)
    upgraded.ensure_installed()
    assert upgraded.scenario_text(SID) == newer and upgraded.source(SID) == {
        "origin": "default", "version": "2.3.0", "imported_at": None, "sha256": sha256_text(newer),
    }


def test_what_the_user_imported_is_never_replaced_by_a_newer_default(tmp_path: Path, default_file: Path, clock: Clock):
    data = tmp_path / "data"
    first = RulesStore(data, default_path=default_file, clock=clock)
    first.ensure_installed()
    mine = with_version(SCENARIO, "mine")
    clock.now = 1_763_900_000
    first.set_scenario(SID, mine)
    default_file.write_text(with_version(SCENARIO, "2.3.0"), encoding="utf-8", newline="\n")
    again = RulesStore(data, default_path=default_file, clock=clock)
    again.ensure_installed()
    assert again.scenario_text(SID) == mine and again.source(SID)["origin"] == "imported" and again.source(SID)["imported_at"] == 1_763_900_000


def test_a_file_edited_by_hand_is_reported_as_the_users_and_kept(tmp_path: Path, default_file: Path, clock: Clock):
    data = tmp_path / "data"
    first = RulesStore(data, default_path=default_file, clock=clock)
    first.ensure_installed()
    edited = with_version(SCENARIO, "hand-made")
    (data / "rules" / SID / "scenario.md").write_text(edited, encoding="utf-8", newline="\n")  # (an editor, outside the program)
    source = first.source(SID)
    assert source["origin"] == "imported" and source["sha256"] == sha256_text(edited) and source["version"] == "hand-made"
    assert isinstance(source["imported_at"], int) and first.scenario_text(SID) == edited  # (nothing is cached: seen at once)
    default_file.write_text(with_version(SCENARIO, "2.3.0"), encoding="utf-8", newline="\n")
    restarted = RulesStore(data, default_path=default_file, clock=clock)
    restarted.ensure_installed()
    assert restarted.scenario_text(SID) == edited  # (not an untouched default: it stays)


def test_a_scenario_edited_into_something_the_loader_refuses_is_still_listed_with_its_problem(store: RulesStore):
    (store.root / SID / "scenario.md").write_text(SCENARIO.replace("schema_version: 2", "schema_version: 7", 1), encoding="utf-8", newline="\n")
    item = store.item(SID)
    assert item["problem"] == f"{SID}.md: schema_version 必须是 2（收到 7）" and item["title"] == SID and item["version"] == ""
    assert item["origin"] == "imported" and item["chars"] > 1000 and store.ids() == [SID]
    store.reset(SID)  # (and "恢复默认" fixes it)
    assert store.item(SID)["problem"] is None


def test_a_lost_or_foreign_source_file_is_rebuilt_from_the_scenario_itself(store: RulesStore):
    (store.root / SID / "source.json").unlink()
    assert store.source(SID)["origin"] == "default"
    store.set_scenario(SID, with_version(SCENARIO, "7.7.7"))
    (store.root / SID / "source.json").write_text("not json", encoding="utf-8")
    assert store.source(SID)["origin"] == "imported" and store.source(SID)["version"] == "7.7.7"
    (store.root / SID / "source.json").write_text(json.dumps({"origin": "default", "sha256": "0" * 64}), encoding="utf-8")
    assert store.source(SID)["origin"] == "imported"  # (a source that is not about this text is not believed)


def test_a_deleted_scenario_comes_back_at_the_next_start(tmp_path: Path, default_file: Path, clock: Clock):
    data = tmp_path / "data"
    store = RulesStore(data, default_path=default_file, clock=clock)
    store.ensure_installed()
    store.set_material(SID, "keep", "材料")
    (data / "rules" / SID / "scenario.md").unlink()
    assert store.ids() == [] and not store.has(SID)
    restarted = RulesStore(data, default_path=default_file, clock=clock)
    restarted.ensure_installed()
    assert restarted.ids() == [SID] and restarted.source(SID)["origin"] == "default" and restarted.materials(SID) == {"keep": "材料\n"}


# ======================================================================================================================
# study materials
# ======================================================================================================================
def test_a_material_round_trips_and_is_part_of_the_hash_the_bots_depend_on(store: RulesStore):
    assert materials_hash(store.materials(SID)) == ""
    info = store.set_material(SID, "strategy-1", "# 一篇策略文章\r\n\r\n先看事实。\r\n\r\n")
    assert info == {"slug": "strategy-1", "title": "一篇策略文章", "chars": len("# 一篇策略文章\n\n先看事实。\n"), "sha256": sha256_text("# 一篇策略文章\n\n先看事实。\n")}
    path = store.root / SID / "materials" / "strategy-1.md"
    assert path.read_bytes() == "# 一篇策略文章\n\n先看事实。\n".encode("utf-8")  # (normalized: LF, one final newline)
    assert store.materials(SID) == {"strategy-1": "# 一篇策略文章\n\n先看事实。\n"}
    assert store.material_text(SID, "strategy-1") == store.materials(SID)["strategy-1"]
    assert store.material(SID, "strategy-1") == {**info, "markdown": "# 一篇策略文章\n\n先看事实。\n"}
    first = materials_hash(store.materials(SID))
    assert first
    store.set_material(SID, "strategy-1", "改过的材料")  # (an edit changes the hash, so every bot becomes stale)
    assert materials_hash(store.materials(SID)) != first
    store.set_material(SID, "other", "第二份")
    assert list(store.materials(SID)) == ["other", "strategy-1"]  # (by slug)
    assert [m["slug"] for m in store.item(SID)["materials"]] == ["other", "strategy-1"]
    store.delete_material(SID, "other")
    store.delete_material(SID, "strategy-1")
    assert store.materials(SID) == {} and materials_hash(store.materials(SID)) == ""
    assert sorted(p.name for p in (store.root / SID).iterdir()) == ["materials", "scenario.md", "source.json"] and not list((store.root / SID / "materials").iterdir())


def test_the_title_is_given_or_the_first_heading_or_the_slug(store: RulesStore):
    assert store.set_material(SID, "a", "# 第一个标题\n正文\n# 第二个标题")["title"] == "第一个标题"
    assert store.set_material(SID, "b", "没有标题的正文")["title"] == "b"
    assert store.set_material(SID, "c", "```\n# 代码里的井号不是标题\n```\n## 小标题不算\n正文")["title"] == "c"
    assert store.set_material(SID, "d", "# 文本里的标题", title="  我自己起的\n名字 ")["title"] == "我自己起的 名字"
    sidecar = store.root / SID / "materials" / "d.json"
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {"title": "我自己起的 名字"}
    assert [m["title"] for m in store.material_infos(SID)] == ["第一个标题", "b", "c", "我自己起的 名字"]
    # a replacement is a whole replacement: a title left out is derived from the text again (the old explicit one is gone)
    assert store.set_material(SID, "d", "# 文本里的标题")["title"] == "文本里的标题" and not sidecar.exists()
    store.set_material(SID, "d", "正文", title="又起一个")
    store.delete_material(SID, "d")  # (both files go)
    assert not sidecar.exists() and not (store.root / SID / "materials" / "d.md").exists()
    assert len(store.set_material(SID, "long", "# " + "长" * 200)["title"]) == MATERIAL_TITLE_CHARS
    assert store.set_material(SID, "blank", "正文", title="   ")["title"] == "blank"  # (a blank title is no title)


@pytest.mark.parametrize("slug", ["", "Upper", "a b", "a_b", "-lead", "../x", "x" * 33, "带中文", "a.md", None, 7])
def test_a_slug_has_to_be_ascii_lowercase_digits_and_hyphens(store: RulesStore, slug):
    error = refused(store.set_material, SID, slug, "正文")
    assert error.status == 400 and "材料的标识只能用小写字母、数字和连字符" in str(error)
    assert store.materials(SID) == {}
    with pytest.raises(RulesNotFound):
        store.material_text(SID, slug)  # type: ignore[arg-type]
    with pytest.raises(RulesNotFound):
        store.delete_material(SID, slug)  # type: ignore[arg-type]


def test_what_a_material_may_be(store: RulesStore):
    assert "学习材料是空的" in str(refused(store.set_material, SID, "m", " \n\n "))
    assert "含有空字符" in str(refused(store.set_material, SID, "m", "a\x00b"))
    assert "无法保存的字符" in str(refused(store.set_material, SID, "m", "a\ud800b"))
    assert "必须是文字" in str(refused(store.set_material, SID, "m", 5))
    assert "标题必须是文字" in str(refused(store.set_material, SID, "m", "x", title=5))
    assert "标题里含有空字符" in str(refused(store.set_material, SID, "m", "x", title="a\x00"))
    assert f"标题最多 {MATERIAL_TITLE_CHARS} 字（现在 {MATERIAL_TITLE_CHARS + 1} 字）" in str(refused(store.set_material, SID, "m", "x", title="题" * (MATERIAL_TITLE_CHARS + 1)))
    too_big = refused(store.set_material, SID, "m", "补" * (MAX_MATERIAL_BYTES // 3 + 10))
    assert too_big.status == 413 and too_big.code == "too_large" and f"学习材料最多 {MAX_MATERIAL_BYTES // 1024} KB" in str(too_big)
    store.set_material(SID, "fits", "补" * (MAX_MATERIAL_BYTES // 3 - 10))  # (the limit counts bytes of UTF-8: 3 per Chinese character)
    assert store.materials(SID).keys() == {"fits"} and not (store.root / SID / "materials" / "m.md").exists()
    with pytest.raises(RulesNotFound):
        store.set_material("nothing", "m", "正文")


def test_the_number_of_materials_is_limited_but_a_replacement_is_not_counted(store: RulesStore):
    for index in range(MAX_MATERIALS):
        store.set_material(SID, f"m{index}", f"材料 {index}")
    error = refused(store.set_material, SID, "one-too-many", "再来一份")
    assert error.code == "too_many" and f"最多 {MAX_MATERIALS} 份学习材料" in str(error) and len(store.materials(SID)) == MAX_MATERIALS
    store.set_material(SID, "m0", "换一份内容")  # (replacing is fine)
    store.delete_material(SID, "m1")
    store.set_material(SID, "one-too-many", "现在可以了")
    assert len(store.materials(SID)) == MAX_MATERIALS


def test_a_material_that_is_not_there_is_not_found(store: RulesStore):
    for call in (store.material_text, store.material, store.delete_material):
        error = refused(call, SID, "nothing")
        assert error.status == 404 and "没有这份学习材料：nothing" in str(error)
    store.set_material(SID, "gone", "x")
    store.delete_material(SID, "gone")
    assert refused(store.delete_material, SID, "gone").status == 404  # (once)


def test_files_that_are_not_materials_are_ignored(store: RulesStore):
    folder = store.root / SID / "materials"
    store.set_material(SID, "real", "材料")
    (folder / "Bad Name.md").write_text("不是材料", encoding="utf-8")
    (folder / "notes.txt").write_text("不是材料", encoding="utf-8")
    (folder / "sub").mkdir()
    (folder / "sub.md").mkdir()  # (a directory named like a material)
    assert store.materials(SID) == {"real": "材料\n"} and [m["slug"] for m in store.item(SID)["materials"]] == ["real"]


def test_materials_belong_to_their_scenario(store: RulesStore):
    store.set_scenario("minimal_test", MINIMAL)
    store.set_material(SID, "mine", "狼的材料")
    store.set_material("minimal_test", "mine", "别的剧本的材料")
    assert store.materials(SID) == {"mine": "狼的材料\n"} and store.materials("minimal_test") == {"mine": "别的剧本的材料\n"}
    store.delete_material("minimal_test", "mine")
    assert store.materials(SID) == {"mine": "狼的材料\n"}


def test_materials_written_at_the_same_time_never_corrupt_each_other(store: RulesStore):
    errors: list[BaseException] = []

    def work(index: int) -> None:
        try:
            for round_ in range(8):
                store.set_material(SID, f"m{index}", f"第 {index} 份，第 {round_} 版")
                assert store.material_text(SID, f"m{index}").startswith(f"第 {index} 份")
                store.item(SID)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert store.materials(SID) == {f"m{i}": f"第 {i} 份，第 7 版\n" for i in range(6)}
    assert sorted(p.name for p in (store.root / SID / "materials").iterdir()) == sorted(f"m{i}.md" for i in range(6))  # (no temporary file left)


# ======================================================================================================================
# the default scenario of the program, and what a scenario says about itself
# ======================================================================================================================
def test_the_scenario_info_is_what_the_loader_makes_of_a_text():
    info = scenario_info(SCENARIO, where="x.md")
    assert (info.id, info.version, info.title, info.problem) == (SID, "2.2.0", "12 人标准局狼人杀（暗牌局）", None)
    bad = scenario_info("没有 front matter", where="x.md")
    assert bad.problem == "x.md: 剧本必须以 YAML front matter（---）开头" and bad.id == ""
    assert scenario_info(SCENARIO, where="x.md") is info  # (the verdict is remembered by the hash of the text)
    assert scenario_info(SCENARIO, where="y.md") is not info  # (and by the name the message uses)


def test_the_default_id_comes_from_the_file_and_the_config(tmp_path: Path):
    assert rules_mod.scenario_id_of_file(None) == "" and rules_mod.scenario_id_of_file(tmp_path / "nothing.md") == ""
    plain = tmp_path / "my_game.md"
    plain.write_text("没有 id 行\n", encoding="utf-8")
    assert rules_mod.scenario_id_of_file(plain) == "my_game"  # (the file's name when it says no id)
    odd = tmp_path / "odd name.md"
    odd.write_text("没有 id 行\n", encoding="utf-8")
    assert rules_mod.scenario_id_of_file(odd) == ""
    with_bom = tmp_path / "bom.md"
    with_bom.write_bytes(b"\xef\xbb\xbf---\nschema_version: 2\nid: from_bom\n---\n")
    assert rules_mod.scenario_id_of_file(with_bom) == "from_bom"
    assert default_scenario_id() == SID


def test_the_config_is_read_again_when_it_changes(tmp_path: Path, monkeypatch):
    import app.config as app_config

    program = tmp_path / "program"
    (program / "scenarios").mkdir(parents=True)
    (program / "scenarios" / "one.md").write_text(MINIMAL.replace("minimal_test", "one"), encoding="utf-8")
    (program / "scenarios" / "two.md").write_text(MINIMAL.replace("minimal_test", "two"), encoding="utf-8")
    monkeypatch.setattr(app_config, "BASE_DIR", program)
    assert default_scenario_path() is None and default_scenario_id() == ""  # (no config.yml)
    (program / "config.yml").write_text('scenario:\n  path: "scenarios/one.md"\n', encoding="utf-8")
    assert default_scenario_path() == program / "scenarios" / "one.md" and default_scenario_id() == "one"
    (program / "config.yml").write_text('scenario:\n  document: "scenarios/two.md"\n', encoding="utf-8")  # (the older key works too)
    assert default_scenario_id() == "two"
    (program / "config.yml").write_text("participants: []\n", encoding="utf-8")
    assert default_scenario_path() is None
    (program / "config.yml").write_text("{ not yaml", encoding="utf-8")
    assert default_scenario_path() is None


# ======================================================================================================================
# safety
# ======================================================================================================================
def test_a_symbolic_link_is_not_followed(store: RulesStore, tmp_path: Path):
    outside = tmp_path / "outside.md"
    outside.write_text("秘密内容", encoding="utf-8")
    store.set_material(SID, "real", "材料")
    link = store.root / SID / "materials" / "linked.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symbolic links here")
    assert store.materials(SID) == {"real": "材料\n"}
    with pytest.raises(RulesNotFound):
        store.material_text(SID, "linked")


def test_no_user_input_becomes_part_of_a_path_except_through_a_slug_or_an_id(store: RulesStore):
    for sid in ("../../etc", "..\\..\\x", "a/../b"):
        assert refused(store.set_scenario, sid, SCENARIO).status == 400
        with pytest.raises(RulesNotFound):
            store.scenario_text(sid)
    for slug in ("../x", "..\\x", "a/b"):
        assert refused(store.set_material, SID, slug, "x").status == 400
    assert sorted(p.name for p in store.data_dir.iterdir()) == ["rules"]
    assert sorted(p.name for p in store.root.iterdir()) == [SID]
