"""``app.storage.bots.BotStore``: the bots at the table (contract §13.2): line-up, freezing and renaming, the initialization
record and its derived status, the memory, proposals, the start check, installing a bot directory, concurrency."""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest

from app.bots.types import INIT_HASHES, BotMind, CoreFiles, SourceHashes, core_hash
from app.storage import botfiles as bf
from app.storage import bots as bots_mod
from app.storage.bots import (
    RESET_CONFIRM_TEXT,
    WILL_CLEAR,
    BotError,
    BotNotFound,
    BotStore,
    LineupError,
    NameTaken,
    NeedsConfirm,
)
from app.storage.settings import SettingsStore

SID = "demo"
SRC = SourceHashes(contract="c1", scenario="s1", materials="", scenario_id=SID)
INIT_FILES = {
    "arch_notes": "我的操作笔记：先看公告，再决定。",
    "digest": "我理解的规则要点。",
    "playbook": "- [把握:中|支持:1|反例:0|来源:初始化|#投票] 先看票型再听发言。\n- [把握:低|支持:0|反例:0|来源:初始化] 平票时多听一轮。",
    "questions": "还不确定平票怎么处理。",
}
CORE_TEXT = {"identity": "一个话不多的老玩家。", "voice": "你说话短。\n「行。」「再说一遍？」「我只看票。」\n篇幅：30–80 字", "mind": "先看发生了什么，再听谁说了什么。"}


class Clock:
    """A clock that moves one second per call, so that everything has its own time."""

    def __init__(self, start: int = 1_763_600_000) -> None:
        self.now = start

    def __call__(self) -> float:
        self.now += 1
        return float(self.now)


def write_bot_dir(path: Path, name: str, *, core: dict[str, str] | None = None, avatar: dict | None = None, **extra) -> Path:
    """A bot directory the way the default library keeps it (``bot.json`` + ``core/*.md``)."""
    (path / "core").mkdir(parents=True, exist_ok=True)
    meta = {"format": 1, "name": name, "origin": "default", **extra}
    if avatar is not None:
        meta["avatar"] = avatar
    (path / "bot.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    for key, text in (CORE_TEXT if core is None else core).items():
        (path / "core" / f"{key}.md").write_text(text + "\n", encoding="utf-8")
    return path


def make_library(root: Path, names: tuple[str, ...] = ("Alice", "Bob", "Carol")) -> Path:
    for name in names:
        write_bot_dir(root / name, name, core={"identity": f"{name}：一句话身份。", "voice": f"{name} 的说话方式。\n「好。」", "mind": f"{name} 的分析方式。"})
    return root


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def lib(tmp_path: Path) -> Path:
    return make_library(tmp_path / "library")


@pytest.fixture
def store(tmp_path: Path, lib: Path, clock: Clock) -> BotStore:
    bot_store = BotStore(tmp_path / "data", seats=3, library_dir=lib, clock=clock)
    bot_store.bootstrap()
    return bot_store


def ids_by_name(store: BotStore) -> dict[str, str]:
    return {name: bot_id for bot_id, name in store.names().items()}


def init_bot(store: BotStore, bot_id: str, *, sources: SourceHashes = SRC, files: dict[str, str] | None = None) -> None:
    store.commit_init(bot_id, SID, INIT_FILES if files is None else files, model="deepseek-flash", duration_s=12.34, sources=sources)


def raw_meta(store: BotStore, bot_id: str) -> dict:
    return json.loads((store.bot_dir(bot_id) / "bot.json").read_text(encoding="utf-8"))


def dir_names(store: BotStore) -> list[str]:
    return sorted(p.name for p in store.root.iterdir())


# ======================================================================================================================
# first run, line-up, names
# ======================================================================================================================
def test_the_first_use_of_a_data_directory_copies_the_default_library(store: BotStore, lib: Path):
    order = store.lineup()
    assert len(order) == 3 and len(set(order)) == 3 and all(bots_mod.BOT_ID_RE.match(i) for i in order)
    assert store.bot_ids() == order
    assert [store.name_of(i) for i in order] == ["Alice", "Bob", "Carol"]
    first = order[0]
    assert store.core(first) == CoreFiles("Alice：一句话身份。", "Alice 的说话方式。\n「好。」", "Alice 的分析方式。")
    meta = raw_meta(store, first)
    assert meta["id"] == first and meta["name"] == "Alice" and meta["origin"] == "default" and meta["format"] == 1
    assert meta["init"]["status"] == "uninitialized" and isinstance(meta["created_at"], int)
    assert meta["avatar"] and meta["avatar"]["v"] == 1  # (the library has none: a fresh one is picked)
    assert len({json.dumps(raw_meta(store, i)["avatar"], sort_keys=True) for i in order}) == 3
    lineup = json.loads((store.root / "lineup.json").read_text(encoding="utf-8"))
    assert lineup["format"] == 1 and lineup["order"] == order and lineup["memory_enabled"] is True
    assert store.moderator()["avatar"] and store.moderator() == {"id": "moderator", "name": "主持人", "avatar": lineup["moderator"]["avatar"]}
    assert store.moderator()["avatar"] not in [raw_meta(store, i)["avatar"] for i in order]
    # the library itself is not touched, and a second bootstrap changes nothing
    assert sorted(p.name for p in lib.iterdir()) == ["Alice", "Bob", "Carol"]
    before = (store.root / "lineup.json").read_bytes()
    store.bootstrap()
    assert (store.root / "lineup.json").read_bytes() == before and store.lineup() == order


def test_every_default_bot_that_the_library_does_not_have_is_a_blank_bot(tmp_path: Path, clock: Clock):
    library = make_library(tmp_path / "lib", ("Bob", "Zed"))  # (Zed is no default name: after the default ones)
    store = BotStore(tmp_path / "data", seats=4, library_dir=library, clock=clock)
    store.bootstrap()
    assert list(store.names().values()) == ["Bob", "Zed", "Alice", "Carol"]  # the library first, blank ones take the unused default names
    blank = store.lineup()[2]
    assert store.core(blank).missing == ("identity", "voice", "mind") and raw_meta(store, blank)["origin"] == "user"
    assert store.core(store.lineup()[0]).complete


def test_without_a_library_the_table_is_set_with_blank_bots(tmp_path: Path):
    store = BotStore(tmp_path / "data", seats=12, library_dir=tmp_path / "nothing")
    store.bootstrap()
    assert list(store.names().values()) == ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]
    assert all(not store.core(i).complete for i in store.lineup())
    assert len(store.defaults()) == 0


def test_the_library_order_and_a_name_that_is_used_twice(tmp_path: Path, clock: Clock):
    library = tmp_path / "lib"
    for folder, name in (("c", "Carol"), ("a", "Alice"), ("z", "Zoe"), ("a2", "alice")):  # (a duplicate name, any case)
        write_bot_dir(library / folder, name)
    store = BotStore(tmp_path / "data", seats=4, library_dir=library, clock=clock)
    store.bootstrap()
    assert list(store.names().values()) == ["Alice", "alice2", "Carol", "Zoe"]  # (never two bots with one name; the default names first)


def test_the_line_up_can_be_changed_to_any_permutation(store: BotStore):
    a, b, c = store.lineup()
    assert store.set_lineup([c, a, b]) == [c, a, b]
    assert store.lineup() == [c, a, b] and store.seat_of(c) == "N1" and store.seat_of(b) == "N3"
    assert json.loads((store.root / "lineup.json").read_text(encoding="utf-8"))["order"] == [c, a, b]
    assert [store.name_of(i) for i in store.lineup()] == ["Carol", "Alice", "Bob"]  # names, avatars, memory follow the bot
    for bad in ([a, b], [a, b, c, c], [a, a, b], [a, b, "b-00000000"], [a, b, 7], "abc", None, []):
        with pytest.raises(LineupError) as info:
            store.set_lineup(bad)
        assert info.value.status == 422 and ("排列" in str(info.value) or "列表" in str(info.value))
    assert store.lineup() == [c, a, b]  # a refused line-up changes nothing
    assert store.seat_of("b-ffffffff") is None


def test_names_belong_to_one_bot_in_any_case(store: BotStore):
    ids = ids_by_name(store)
    assert store.bot_id_of("alice") == ids["Alice"] == store.bot_id_of("  ALICE ")
    assert store.bot_id_of("Nobody") is None and store.bot_id_of("") is None
    assert store.validate_name("  新名字 ") == "新名字"
    with pytest.raises(NameTaken) as info:
        store.validate_name("BOB")
    assert info.value.status == 409 and info.value.code == "name_taken" and "已被 N2 使用" in str(info.value)
    assert store.validate_name("Bob", exclude=ids["Bob"]) == "Bob"  # its own name is no conflict
    for bad, fragment in [("", "不能为空"), ("   ", "不能为空"), ("x" * 21, "最多 20"), ("a:b", "冒号"), ("a：b", "冒号"), ('a"b', "引号"), ("a<b", "尖括号"), ("a\x00b", "控制字符")]:
        with pytest.raises(BotError) as bad_info:
            store.validate_name(bad)
        assert fragment in str(bad_info.value) and bad_info.value.status == 400, bad
    with pytest.raises(BotNotFound):
        store.name_of("b-ffffffff")
    with pytest.raises(BotNotFound):
        store.name_of("../escape")  # an id is the only thing that becomes part of a path
    with pytest.raises(BotNotFound):
        store.bot_dir("b-xyz")


def test_the_memory_switch_and_the_moderator_live_in_the_line_up_file(store: BotStore):
    assert store.memory_enabled() is True
    store.set_memory_enabled(False)
    assert store.memory_enabled() is False and json.loads((store.root / "lineup.json").read_text(encoding="utf-8"))["memory_enabled"] is False
    assert store.lineup() == store.bot_ids()  # (the line-up is untouched)
    desc = store.moderator()["avatar"]
    other = bots_mod.pick_avatars(1, [desc])[0]
    assert store.set_moderator_avatar(other)["avatar"] == other and store.moderator()["avatar"] == other
    with pytest.raises(BotError):
        store.set_moderator_avatar({"v": 1})
    with pytest.raises(BotError):
        store.set_moderator_avatar("not a description")
    assert store.regenerate_moderator_avatar()["avatar"] != other
    assert store.memory_enabled() is False


def test_a_line_up_that_lost_bots_is_repaired(store: BotStore, tmp_path: Path):
    a, b, c = store.lineup()
    shutil.rmtree(store.bot_dir(b))
    assert store.lineup() == [a, c]
    fresh = BotStore(store.data_dir, seats=3, library_dir=store.library_dir(), clock=Clock(2_000_000_000))
    fresh.bootstrap()
    order = fresh.lineup()
    assert len(order) == 3 and order[0] == a and order[2] == c and order[1] not in (a, b, c)  # the new bot takes the seat of the lost one
    assert sorted(fresh.names().values()) == ["Alice", "Bob", "Carol"]  # (the library bot nobody has is created again)


def test_a_missing_line_up_file_is_rebuilt_from_the_bot_directories_oldest_first(store: BotStore):
    original = store.lineup()
    (store.root / "lineup.json").unlink()
    (store.root / ".install-b-00000000-abc").mkdir()  # an interrupted install: swept
    fresh = BotStore(store.data_dir, seats=3, library_dir=store.library_dir())
    fresh.bootstrap()
    assert sorted(fresh.lineup()) == sorted(original) and len(fresh.lineup()) == 3
    assert not (store.root / ".install-b-00000000-abc").exists()
    (store.root / "lineup.json").write_text("not json", encoding="utf-8")
    again = BotStore(store.data_dir, seats=3, library_dir=store.library_dir())
    again.bootstrap()
    assert sorted(again.lineup()) == sorted(original)


# ======================================================================================================================
# freezing, renaming, the avatar
# ======================================================================================================================
def test_a_bot_that_has_not_learned_anything_is_changed_in_place(store: BotStore):
    ids = ids_by_name(store)
    bob = ids["Bob"]
    assert store.is_frozen(bob) is False
    store.write_part(bob, "people", "- Alice：爱带节奏。", SID)  # (impressions and a record do not freeze a bot)
    store.add_record(bob, "角色", True)
    assert store.update_bot(bob, name="  Bobby ") == bob
    assert store.name_of(bob) == "Bobby" and raw_meta(store, bob)["name"] == "Bobby"
    assert store.read_part(bob, "people") == "- Alice：爱带节奏。".replace("- Alice：爱带节奏。", "- Alice：爱带节奏。")
    assert store.load_mind(bob, SID, others=["Alice"]).record.startswith("场次 1")  # the memory stays: same bot
    assert store.lineup() == list(ids.values())
    new_avatar = bots_mod.pick_avatars(1, [raw_meta(store, i)["avatar"] for i in ids.values()])[0]
    assert store.update_bot(bob, avatar=new_avatar) == bob and raw_meta(store, bob)["avatar"] == new_avatar
    assert store.update_bot(bob, name="Bobby", avatar=new_avatar) == bob  # nothing to change
    assert store.update_bot(bob) == bob
    assert store.update_bot(bob, name="BOBBY") == bob and store.name_of(bob) == "BOBBY"  # (a change of case is a change)


def test_a_bad_change_is_refused_before_anything_is_written(store: BotStore):
    ids = ids_by_name(store)
    alice = ids["Alice"]
    init_bot(store, alice)  # frozen: the checks come before the question
    for kwargs, error in [
        ({"name": "bob"}, NameTaken),
        ({"name": ""}, BotError),
        ({"name": "a:b"}, BotError),
        ({"avatar": {"v": 1}}, BotError),
        ({"avatar": "face"}, BotError),
        ({"name": "Fine", "avatar": {"v": 1}}, BotError),
    ]:
        with pytest.raises(error):
            store.update_bot(alice, confirm_reset=True, **kwargs)
    assert store.name_of(alice) == "Alice" and store.lineup() == list(ids.values())
    assert bf.read_part(store.bot_dir(alice), "arch_notes") == INIT_FILES["arch_notes"]  # untouched
    with pytest.raises(BotNotFound):
        store.update_bot("b-ffffffff", name="X")


def test_a_frozen_bot_needs_a_confirmed_reset_and_becomes_a_new_identity(store: BotStore, monkeypatch):
    ids = ids_by_name(store)
    alice, bob = ids["Alice"], ids["Bob"]
    # what the bot has learned: the initialization, impressions, a record, a pending proposal; and an impression of it in another bot
    init_bot(store, alice)
    store.write_part(alice, "people", "- Bob：话少。", SID)
    store.add_record(alice, "角色", True)
    pid = store.stage_core(alice, "voice", "更短的说话方式。", "复盘里发现", "复盘 g1")
    store.write_part(bob, "people", "- Alice：爱带节奏。", SID)
    old_core = store.core(alice)
    old_dir = store.bot_dir(alice)
    settings = SettingsStore(store.data_dir)
    from app.providers.models import ModelBinding

    binding = ModelBinding(provider_id="p", model_id="m", reasoning_depth="low")
    settings.save_bot_binding(alice, binding)
    assert store.is_frozen(alice)

    with pytest.raises(NeedsConfirm) as info:
        store.update_bot(alice, name="Alicia")
    err = info.value
    assert err.status == 409 and err.code == "needs_confirm" and err.will_clear == list(WILL_CLEAR) == ["初始化结果", "操作笔记", "规则要点", "打法", "待解问题", "印象", "战绩"]
    assert str(err) == RESET_CONFIRM_TEXT and "底层设定保留" in str(err)
    with pytest.raises(NeedsConfirm):
        store.update_bot(alice, avatar=bots_mod.pick_avatars(1, [])[0])
    assert store.lineup()[0] == alice and store.name_of(alice) == "Alice" and old_dir.is_dir()  # nothing happened

    new_id = store.update_bot(alice, name="Alicia", confirm_reset=True)
    assert new_id != alice and bots_mod.BOT_ID_RE.match(new_id)
    assert store.lineup() == [new_id, bob, ids["Carol"]]  # the same seat
    assert store.name_of(new_id) == "Alicia" and store.bot_id_of("Alice") is None
    assert not old_dir.exists() and sorted(p.name for p in store.root.iterdir() if p.is_dir() and p.name.startswith((".", "b-")) and p.name != new_id and p.name not in ids.values()) == []
    new_dir = store.bot_dir(new_id)
    assert sorted(p.name for p in new_dir.iterdir()) == ["bot.json", "core"]  # only L0 (and the new bot.json) was kept
    assert store.core(new_id) == old_core
    meta = raw_meta(store, new_id)
    assert meta["id"] == new_id and meta["name"] == "Alicia" and meta["init"]["status"] == "uninitialized" and meta["init"]["hashes"] is None
    assert store.is_frozen(new_id) is False
    assert store.staging(new_id) == []  # the proposals are dropped (and with them the old directory)
    # the other bot's impression of the old name stays, but is not mounted for the new one
    assert store.read_part(bob, "people") == "- Alice：爱带节奏。"
    assert store.load_mind(bob, SID, others=["Alicia"]).people == {}
    assert store.load_mind(bob, SID, others=["Alice"]).people == {"Alice": "爱带节奏。"}  # (someone called Alice again would be a new bot)
    # the default binding moved to the new id
    assert settings.get_bot_binding(new_id) == binding and settings.get_bot_binding(alice) is None
    # no copy of the old memory is anywhere
    assert [p for p in store.root.rglob("*") if p.is_file() and "初始化" in p.read_text(encoding="utf-8", errors="ignore") and "bot.json" not in p.name] == []
    assert pid not in [p.stem for p in store.root.rglob("*.json")]


def test_the_avatar_of_a_frozen_bot_follows_the_same_rule(store: BotStore):
    alice = ids_by_name(store)["Alice"]
    init_bot(store, alice)
    taken = [raw_meta(store, i)["avatar"] for i in store.lineup()]
    new_avatar = bots_mod.pick_avatars(1, taken)[0]
    with pytest.raises(NeedsConfirm):
        store.regenerate_avatar(alice)
    new_id = store.regenerate_avatar(alice, confirm_reset=True)
    assert new_id != alice and raw_meta(store, new_id)["avatar"] not in taken and store.name_of(new_id) == "Alice"
    assert store.update_bot(new_id, avatar=new_avatar) == new_id  # not frozen any more: in place


@pytest.mark.parametrize("how", ["init_ready", "notes", "digest_other_scenario", "playbook", "questions"])
def test_what_makes_a_bot_frozen(store: BotStore, how: str):
    bot = store.lineup()[1]
    assert not store.is_frozen(bot)
    if how == "init_ready":
        init_bot(store, bot)
        for part in ("arch_notes", "digest", "playbook", "questions"):
            store.clear_part(bot, part, SID)
        assert raw_meta(store, bot)["init"]["status"] == "ready"
    elif how == "notes":
        assert store.write_part(bot, "arch_notes", "笔记", SID).ok
    elif how == "digest_other_scenario":
        assert store.write_part(bot, "digest", "另一个剧本的要点", "other_scenario").ok
    elif how == "playbook":
        assert store.write_part(bot, "playbook", "- [把握:低] 一条", SID).ok
    else:
        assert store.write_part(bot, "questions", "一个问题", SID).ok
    assert store.is_frozen(bot) and store.summary(bot, sources=SRC)["frozen"] is True
    with pytest.raises(NeedsConfirm):
        store.update_bot(bot, name="Other")


def test_regenerating_every_avatar_skips_the_frozen_bots(store: BotStore):
    ids = ids_by_name(store)
    init_bot(store, ids["Bob"])
    before = {name: raw_meta(store, i)["avatar"] for name, i in ids.items()}
    moderator = store.moderator()["avatar"]
    changed, skipped = store.regenerate_avatars()
    assert changed == [ids["Alice"], ids["Carol"]] and skipped == [ids["Bob"]]
    after = {name: raw_meta(store, i)["avatar"] for name, i in ids.items()}
    assert after["Bob"] == before["Bob"] and after["Alice"] != before["Alice"] and after["Carol"] != before["Carol"]
    assert len({json.dumps(a, sort_keys=True) for a in [*after.values(), moderator]}) == 4  # all different, the moderator's too
    assert store.moderator()["avatar"] == moderator and store.lineup() == list(ids.values())


# ======================================================================================================================
# the initialization record
# ======================================================================================================================
def test_a_committed_initialization_is_ready_while_nothing_it_depends_on_changes(store: BotStore, clock: Clock):
    bot = store.lineup()[0]
    assert store.summary(bot, sources=SRC)["init"] == {"status": "uninitialized", "at": None, "model": None, "duration_s": None, "error": None, "stale": []}
    init_bot(store, bot)
    init = store.summary(bot, sources=SRC)["init"]
    assert init["status"] == "ready" and init["model"] == "deepseek-flash" and init["duration_s"] == 12.3 and init["error"] is None
    assert isinstance(init["at"], int) and init["stale"] == []
    stored = raw_meta(store, bot)["init"]
    assert stored["status"] == "ready" and stored["scenario_id"] == SID
    assert stored["hashes"] == {"core": core_hash(store.core(bot)), "contract": "c1", "scenario": "s1", "materials": ""}
    assert set(stored["hashes"]) == set(INIT_HASHES)
    assert {n: store.read_part(bot, n, SID) for n in INIT_FILES} == {
        "arch_notes": INIT_FILES["arch_notes"],
        "digest": INIT_FILES["digest"],
        "playbook": bf.render_playbook(bf.parse_playbook(INIT_FILES["playbook"])).strip(),
        "questions": INIT_FILES["questions"],
    }
    assert store.is_frozen(bot)


@pytest.mark.parametrize(
    "changed,expected",
    [
        (SourceHashes("c2", "s1", "", SID), ["contract"]),
        (SourceHashes("c1", "s2", "", SID), ["scenario"]),
        (SourceHashes("c1", "s1", "m1", SID), ["materials"]),
        (SourceHashes("c2", "s2", "m1", SID), ["contract", "scenario", "materials"]),
        (SourceHashes("c1", "s1", "", "other"), ["scenario"]),  # (the scenario the record was made for is another one)
    ],
)
def test_the_status_becomes_stale_when_a_source_changes_and_says_which(store: BotStore, changed: SourceHashes, expected: list[str]):
    bot = store.lineup()[0]
    init_bot(store, bot)
    init = store.summary(bot, sources=changed)["init"]
    assert init["status"] == "stale" and init["stale"] == expected
    assert raw_meta(store, bot)["init"]["status"] == "ready"  # only the three statuses are stored: this one is derived


def test_editing_the_core_makes_the_bot_stale_and_so_does_an_accepted_proposal(store: BotStore):
    bot = store.lineup()[0]
    init_bot(store, bot)
    assert store.summary(bot, sources=SRC)["init"]["status"] == "ready"
    parsed = store.set_core_markdown(bot, bf.render_core_markdown(CoreFiles("新的身份", "新的说话方式。", "新的分析方式。"), "Alice"))
    assert parsed.name == "Alice"
    init = store.summary(bot, sources=SRC)["init"]
    assert init["status"] == "stale" and init["stale"] == ["core"]
    # putting the same text back makes it ready again: the hash follows the text
    store.set_core(bot, CoreFiles("Alice：一句话身份。", "Alice 的说话方式。\n「好。」", "Alice 的分析方式。"))
    assert store.summary(bot, sources=SRC)["init"]["status"] == "ready"
    pid = store.stage_core(bot, "mind", "另一种分析方式。", "试试", "复盘 g1")
    assert store.summary(bot, sources=SRC)["init"]["status"] == "ready"  # a pending proposal changes nothing
    store.accept_staging(bot, pid)
    assert store.summary(bot, sources=SRC)["init"]["status"] == "stale" and store.summary(bot, sources=SRC)["init"]["stale"] == ["core"]


def test_a_running_job_is_initializing_whatever_was_stored(store: BotStore):
    a, b, c = store.lineup()
    init_bot(store, b)
    store.fail_init(c, "超时")
    for bot in (a, b, c):
        assert store.summary(bot, sources=SRC, running=True)["init"]["status"] == "initializing"
    assert store.summary(b, sources=SourceHashes("zzz", "s1", "", SID), running=True)["init"]["stale"] == ["contract"]  # (still says why)
    assert store.summary(a, sources=SRC)["init"]["status"] == "uninitialized"


def test_a_failed_initialization_keeps_the_old_files_and_says_why(store: BotStore):
    bot = store.lineup()[0]
    init_bot(store, bot)
    before = {n: store.read_part(bot, n, SID) for n in INIT_FILES}
    store.fail_init(bot, "超过 120 秒，没有写完。", model="deepseek-flash", duration_s=120.04)
    init = store.summary(bot, sources=SRC)["init"]
    assert init["status"] == "failed" and init["error"] == "超过 120 秒，没有写完。" and init["duration_s"] == 120.0 and init["model"] == "deepseek-flash"
    assert {n: store.read_part(bot, n, SID) for n in INIT_FILES} == before  # the old files are as they were
    assert raw_meta(store, bot)["init"]["hashes"] is None and store.is_frozen(bot)
    store.fail_init(bot, "")
    assert store.summary(bot, sources=SRC)["init"]["error"] == "初始化没有完成。"
    init_bot(store, bot)  # a retry that works
    ok = store.summary(bot, sources=SRC)["init"]
    assert ok["status"] == "ready" and ok["error"] is None


def test_a_set_of_files_is_committed_completely_or_not_at_all(store: BotStore):
    bot = store.lineup()[0]
    init_bot(store, bot)
    before = {n: store.read_part(bot, n, SID) for n in INIT_FILES}
    stored = raw_meta(store, bot)
    cases = [
        ({"digest": "要点", "playbook": INIT_FILES["playbook"]}, ["缺少必需的文件：操作笔记（arch_notes）。"]),
        ({**INIT_FILES, "digest": "  "}, ["「规则要点」不能为空。"]),
        ({**INIT_FILES, "arch_notes": "字" * 801}, ["「操作笔记」最多 800 字（现在 801 字）。"]),
        ({**INIT_FILES, "playbook": "没有条目的一段话"}, None),
        ({**INIT_FILES, "people": "- Bob：话少"}, ["不认识的文件「people」，初始化只能写 arch_notes、digest、playbook、questions。"]),
        ({"arch_notes": "字" * 801, "digest": "字" * 801, "playbook": ""}, None),
    ]
    for files, expected in cases:
        with pytest.raises(bf.BotFileError) as info:
            store.commit_init(bot, SID, files, model="m", duration_s=1, sources=SRC)
        if expected is not None:
            assert info.value.problems == expected, info.value.problems
    with pytest.raises(bf.BotFileError) as many:
        store.commit_init(bot, SID, cases[-1][0], model="m", duration_s=1, sources=SRC)
    assert len(many.value.problems) == 3  # every fault, not the first
    assert {n: store.read_part(bot, n, SID) for n in INIT_FILES} == before and raw_meta(store, bot) == stored
    with pytest.raises(bf.BotFileError):
        store.commit_init(bot, "../x", INIT_FILES, model="m", duration_s=1, sources=SRC)


def test_a_new_initialization_revises_the_files_and_leaves_the_ones_it_does_not_mention(store: BotStore):
    bot = store.lineup()[0]
    init_bot(store, bot)
    store.commit_init(bot, SID, {"arch_notes": "改过的笔记", "digest": "改过的要点", "playbook": "- [把握:高|支持:2] 改过的一条"}, model="m2", duration_s=3, sources=SRC)
    assert store.read_part(bot, "arch_notes", SID) == "改过的笔记"
    assert store.read_part(bot, "questions", SID) == INIT_FILES["questions"]  # (not mentioned: kept)
    store.commit_init(bot, SID, {**INIT_FILES, "questions": ""}, model="m3", duration_s=3, sources=SRC)
    assert store.read_part(bot, "questions", SID) == ""  # (mentioned and empty: cleared)
    assert not (store.bot_dir(bot) / "rules" / SID / "questions.md").exists()
    assert store.summary(bot, sources=SRC)["init"]["model"] == "m3"


def test_the_core_hash_of_the_start_of_a_run_is_used_when_it_is_given(store: BotStore):
    bot = store.lineup()[0]
    started = core_hash(store.core(bot))
    store.set_core(bot, CoreFiles("改了身份", "改了说话方式。", "改了分析方式。"))  # edited while the job ran
    store.commit_init(bot, SID, INIT_FILES, model="m", duration_s=1, sources=SRC, core_hash_at_start=started)
    init = store.summary(bot, sources=SRC)["init"]
    assert init["status"] == "stale" and init["stale"] == ["core"]


def test_a_ready_record_without_hashes_cannot_be_trusted(store: BotStore):
    bot = store.lineup()[0]
    meta = raw_meta(store, bot)
    meta["init"] = {"status": "ready", "at": 5, "model": "x"}
    (store.bot_dir(bot) / "bot.json").write_text(json.dumps(meta), encoding="utf-8")
    init = store.summary(bot, sources=SRC)["init"]
    assert init["status"] == "stale" and init["stale"] == list(INIT_HASHES)
    meta["init"] = {"status": "initializing", "hashes": {"core": "x"}}  # (a status that is never stored is read as not initialized)
    (store.bot_dir(bot) / "bot.json").write_text(json.dumps(meta), encoding="utf-8")
    assert store.summary(bot, sources=SRC)["init"]["status"] == "uninitialized"


# ======================================================================================================================
# the memory
# ======================================================================================================================
def test_the_parts_of_the_memory_are_written_and_read_through_the_store(store: BotStore):
    bot = store.lineup()[0]
    assert store.write_part(bot, "digest", "我的规则要点", SID).ok
    assert store.read_part(bot, "digest", SID) == "我的规则要点" and store.read_part(bot, "digest", "other") == ""
    result = store.write_part(bot, "digest", "字" * 812, SID)
    assert not result.ok and result.message.startswith("「规则要点」最多 800 字（现在 812 字）。") and store.read_part(bot, "digest", SID) == "我的规则要点"
    assert store.write_part(bot, "people", "- Bob：话少", SID).ok and store.read_part(bot, "people") == "- Bob：话少"
    assert not store.write_part(bot, "nonsense", "x", SID).ok
    assert not store.write_part(bot, "digest", "x", "../escape").ok
    assert store.clear_part(bot, "digest", SID).ok and store.read_part(bot, "digest", SID) == ""
    with pytest.raises(BotNotFound):
        store.write_part("b-ffffffff", "digest", "x", SID)
    with pytest.raises(BotError):
        store.read_part(bot, "digest", "../escape")
    assert store.summary(bot, sources=SRC)["init"]["status"] == "uninitialized"  # editing the memory is not an initialization


def test_editing_the_memory_of_an_initialized_bot_does_not_change_its_status(store: BotStore):
    bot = store.lineup()[0]
    init_bot(store, bot)
    assert store.write_part(bot, "playbook", "- [把握:高|支持:9] 用户改过的", SID).ok
    assert store.write_part(bot, "arch_notes", "用户改过的笔记", SID).ok
    assert store.summary(bot, sources=SRC)["init"]["status"] == "ready"


def test_clearing_what_a_bot_learned_keeps_its_core_and_its_proposals(store: BotStore):
    a, b, c = store.lineup()
    for bot in (a, b):
        init_bot(store, bot)
        store.write_part(bot, "people", "- Carol：话多", SID)
        store.add_record(bot, "角色", True)
    pid = store.stage_core(a, "voice", "新的说话方式", "原因", "复盘 g1")
    assert store.clear_memory([a]) == [a]
    assert not (store.bot_dir(a) / "arch").exists() and not (store.bot_dir(a) / "rules").exists()
    assert not (store.bot_dir(a) / "people.md").exists() and not (store.bot_dir(a) / "record.json").exists()
    assert store.summary(a, sources=SRC)["init"]["status"] == "uninitialized" and raw_meta(store, a)["init"]["hashes"] is None
    assert store.core(a).complete and [p["id"] for p in store.staging(a)] == [pid]
    assert store.read_part(b, "arch_notes") == INIT_FILES["arch_notes"] and store.summary(b, sources=SRC)["init"]["status"] == "ready"  # the others
    assert store.clear_memory() == [a, b, c]
    assert store.summary(b, sources=SRC)["init"]["status"] == "uninitialized" and store.read_part(b, "people") == ""
    with pytest.raises(BotNotFound):
        store.clear_memory(["b-ffffffff"])


def test_clearing_the_core_leaves_the_rest_and_the_bot_cannot_start(store: BotStore):
    a, b, c = store.lineup()
    init_bot(store, a)
    assert store.clear_core([a]) == [a]
    assert store.core(a) == CoreFiles() and store.core(b).complete
    assert not list((store.bot_dir(a) / "core").glob("*.md"))
    assert store.read_part(a, "arch_notes") == INIT_FILES["arch_notes"]
    assert store.summary(a, sources=SRC)["init"]["status"] == "stale"
    assert [i["code"] for i in store.problems(a, sources=SRC)] == ["core_missing", "stale"]
    assert store.clear_core() == [a, b, c] and all(not store.core(i).complete for i in (a, b, c))


def test_the_record_counts_finished_games(store: BotStore):
    bot = store.lineup()[0]
    store.add_record(bot, "角色甲", True)
    store.add_record(bot, "角色甲", False)
    store.add_record(bot, "角色乙", None)  # nobody knows who won: nothing counted
    assert store.load_mind(bot, SID).record == "场次 2，胜 1，负 1，胜率 50%\n- 角色甲：2 场，胜 1"
    assert store.summary(bot, sources=SRC)["memory_stats"]["record"] == "场次 2 · 胜 1 · 胜率 50%"
    assert json.loads((store.bot_dir(bot) / "record.json").read_text(encoding="utf-8")) == {"games": 2, "wins": 1, "by_role": {"角色甲": [2, 1]}}
    with pytest.raises(BotNotFound):
        store.add_record("b-ffffffff", "x", True)


def test_a_bot_is_loaded_for_a_game_with_the_impressions_of_the_people_in_it(store: BotStore):
    a, b, c = store.lineup()
    init_bot(store, a)
    store.write_part(a, "people", "- Bob：话少，爱跟票。\n- carol：爱带节奏。\n- Zed：不在座。\n- Alice：自己对自己。", SID)
    store.add_record(a, "角色", True)
    mind = store.load_mind(a, SID, others=["Carol", "Bob"])
    assert isinstance(mind, BotMind) and mind.bot_id == a and mind.name == "Alice"
    assert mind.core == store.core(a)
    assert mind.arch_notes == INIT_FILES["arch_notes"]
    assert mind.rules.scenario_id == SID and mind.rules.digest == INIT_FILES["digest"] and mind.rules.questions == INIT_FILES["questions"]
    assert [(e.text, e.confidence, e.support, e.tags) for e in mind.rules.playbook] == [("先看票型再听发言。", "中", 1, ("投票",)), ("平票时多听一轮。", "低", 0, ())]
    assert mind.people == {"Carol": "爱带节奏。", "Bob": "话少，爱跟票。"} and list(mind.people) == ["Carol", "Bob"]  # only those in the game, as they are called now, in the order given
    assert mind.record.startswith("场次 1，胜 1")
    assert BotMind.from_dict(json.loads(json.dumps(mind.to_dict(), ensure_ascii=False))) == mind
    # the bot itself in ``others`` is no one to have an impression of; nobody there -> no impressions
    assert store.load_mind(a, SID, others=["Alice", "Zed"]).people == {"Zed": "不在座。"}
    assert store.load_mind(a, SID).people == {}
    # memory off: L0 only
    bare = store.load_mind(a, SID, memory=False, others=["Bob"])
    assert bare.core == store.core(a) and bare.arch_notes == "" and bare.people == {} and bare.record == "" and bare.rules.digest == "" and bare.rules.playbook == ()
    # another scenario has nothing yet
    other = store.load_mind(a, "other", others=["Bob"])
    assert other.rules.digest == "" and other.rules.playbook == () and other.arch_notes == INIT_FILES["arch_notes"] and other.people == {"Bob": "话少，爱跟票。"}
    with pytest.raises(BotError):
        store.load_mind(a, "../escape")
    with pytest.raises(BotNotFound):
        store.load_mind("b-ffffffff", SID)


# ======================================================================================================================
# proposals
# ======================================================================================================================
def test_a_review_can_only_propose_and_the_user_decides(store: BotStore):
    bot = store.lineup()[0]
    old_voice = store.core(bot).voice
    pid = store.stage_core(bot, "voice", "  新的说话方式。\r\n再短一点。 ", "  看了接管者的发言\n学到的 ", "复盘 game-1")
    assert pid.startswith("p-") and bots_mod.PROPOSAL_ID_RE.match(pid)
    (proposal,) = store.staging(bot)
    assert proposal == {
        "id": pid, "section": "voice", "before": old_voice, "after": "新的说话方式。\n再短一点。",
        "reason": "看了接管者的发言 学到的", "source": "复盘 game-1", "created_at": proposal["created_at"],
    }
    assert json.loads((store.bot_dir(bot) / "staging" / f"{pid}.json").read_text(encoding="utf-8")) == proposal
    assert store.core(bot).voice == old_voice  # nothing is written to L0 by proposing
    assert store.summary(bot, sources=SRC)["staging"] == 1 and store.detail(bot, SID, sources=SRC)["staging"] == [proposal]
    store.accept_staging(bot, pid)
    assert store.core(bot).voice == "新的说话方式。\n再短一点。" and store.staging(bot) == []
    assert not (store.bot_dir(bot) / "staging" / f"{pid}.json").exists()
    with pytest.raises(BotNotFound):
        store.accept_staging(bot, pid)  # once
    pid2 = store.stage_core(bot, "identity", "另一个身份。", "r", "s")
    store.reject_staging(bot, pid2)
    assert store.staging(bot) == [] and store.core(bot).identity == "Alice：一句话身份。"
    for call in (store.reject_staging, store.accept_staging):
        with pytest.raises(BotNotFound):
            call(bot, "p-00000000")
        with pytest.raises(BotNotFound):
            call(bot, "../../bot")
    assert store.staging(bot) == []


def test_proposals_are_ordered_and_checked_like_the_text_they_would_become(store: BotStore):
    bot = store.lineup()[0]
    first = store.stage_core(bot, "mind", "第一", "r", "s")
    second = store.stage_core(bot, "mind", "第二", "r", "s")
    assert [p["id"] for p in store.staging(bot)] == [first, second]
    for section, text, fragment in [
        ("identity", "身" * 301, "「身份」最多 300 字（现在 301 字）。"),
        ("voice", "说" * 601, "「说话方式」最多 600 字"),
        ("mind", "  ", "提案内容不能为空"),
        ("mind", "有\x00字符", "不能包含空字符"),
        ("other", "x", "分区必须是"),
        ("extra:", "x", "分区必须是"),
        ("extra:新分区", "附" * 301, "「附加：新分区」最多 300 字"),
    ]:
        with pytest.raises(bf.BotFileError) as info:
            store.stage_core(bot, section, text, "r", "s")
        assert fragment in str(info.value), (section, str(info.value))
    assert len(store.staging(bot)) == 2
    for i in range(bots_mod.MAX_STAGING - 2):
        store.stage_core(bot, "mind", f"提案 {i}", "r", "s")
    with pytest.raises(bf.BotFileError) as full:
        store.stage_core(bot, "mind", "再来一条", "r", "s")
    assert "待确认的提案已经有 30 条" in str(full.value)
    (store.bot_dir(bot) / "staging" / "p-bad00000.json").write_text("not json", encoding="utf-8")
    (store.bot_dir(bot) / "staging" / "p-bad00001.json").write_text(json.dumps({"id": "p-other"}), encoding="utf-8")
    assert len(store.staging(bot)) == 30  # (a damaged file is no proposal)


def test_an_extra_section_is_proposed_by_slug_or_by_title_and_created_when_it_is_new(store: BotStore):
    bot = store.lineup()[0]
    store.set_core(bot, CoreFiles("身份", "说话。", "分析。", (("口头禅", "「好。」"),)))
    by_title = store.stage_core(bot, "extra:口头禅", "「行。」", "r", "s")
    by_slug = store.stage_core(bot, "extra:x1", "「嗯。」", "r", "s")
    new = store.stage_core(bot, "extra:Habit", "新的附加。", "r", "s")
    sections = {p["id"]: p for p in store.staging(bot)}
    assert sections[by_title]["section"] == "extra:x1" == sections[by_slug]["section"]  # (both name the same file)
    assert sections[by_title]["before"] == "「好。」" and sections[new]["section"] == "extra:Habit" and sections[new]["before"] == ""
    store.accept_staging(bot, by_slug)
    store.accept_staging(bot, new)
    assert store.core(bot).extras == (("Habit", "新的附加。"), ("口头禅", "「嗯。」"))  # (the extras are read in the order of their slugs)
    assert store.core_on_disk(bot).slugs == ("habit", "x1")
    detail = store.detail(bot, SID, sources=SRC)["core"]
    assert [(e["title"], e["slug"], e["text"], e["chars"]) for e in detail["extras"]] == [("Habit", "habit", "新的附加。", 5), ("口头禅", "x1", "「嗯。」", 4)]


def test_a_fourth_extra_section_is_refused_and_the_proposal_stays(store: BotStore):
    bot = store.lineup()[0]
    store.set_core(bot, CoreFiles("身份", "说话。", "分析。", (("一", "x"), ("二", "x"), ("三", "x"))))
    with pytest.raises(bf.BotFileError) as info:
        store.stage_core(bot, "extra:四", "x", "r", "s")
    assert "最多 3 份" in str(info.value)
    pid = store.stage_core(bot, "extra:二", "改", "r", "s")  # an existing one is fine
    # (the table changes between proposing and accepting: the L0 gets a limit it did not have)
    proposal = json.loads((store.bot_dir(bot) / "staging" / f"{pid}.json").read_text(encoding="utf-8"))
    proposal.update(id="p-11111111", section="extra:新的")
    (store.bot_dir(bot) / "staging" / "p-11111111.json").write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(bf.BotFileError) as refused:
        store.accept_staging(bot, "p-11111111")
    assert "最多 3 份" in str(refused.value) and "p-11111111" in [p["id"] for p in store.staging(bot)]
    assert len(store.core(bot).extras) == 3


# ======================================================================================================================
# summary, detail, the start check
# ======================================================================================================================
SUMMARY_KEYS = {"id", "name", "avatar", "avatar_code", "origin", "core", "init", "frozen", "memory_stats", "staging"}


def test_the_summary_has_the_shape_of_the_contract(store: BotStore):
    bot = store.lineup()[0]
    store.set_core(bot, CoreFiles("身份。", "你说话短。\n「行。」", "先看事实。", (("口头禅", "「行。」"),)))
    init_bot(store, bot)
    store.write_part(bot, "people", "- Bob：话少\n- Carol：话多", SID)
    store.add_record(bot, "角色", True)
    data = store.summary(bot, sources=SRC)
    assert set(data) == SUMMARY_KEYS
    assert data["id"] == bot and data["name"] == "Alice" and data["origin"] == "default" and data["frozen"] is True and data["staging"] == 0
    assert data["avatar"]["v"] == 1 and data["avatar_code"] == bots_mod.avatar_code(data["avatar"]) and len(data["avatar_code"]) == 24
    assert data["core"] == {
        "complete": True, "missing": [], "chars": {"identity": 3, "voice": 10, "mind": 5},
        "extras": [{"title": "口头禅", "slug": "x1", "chars": 4}], "updated_at": data["core"]["updated_at"],
    }
    assert isinstance(data["core"]["updated_at"], int)
    assert data["memory_stats"] == {
        "arch_notes": len(INIT_FILES["arch_notes"]), "digest": len(INIT_FILES["digest"]), "playbook": 2,
        "questions": len(INIT_FILES["questions"]), "people": 2, "record": "场次 1 · 胜 1 · 胜率 100%",
    }
    assert set(data["init"]) == {"status", "at", "model", "duration_s", "error", "stale"}
    # without a scenario there is no scenario memory to count
    assert store.summary(bot)["memory_stats"]["playbook"] == 0 and store.summary(bot)["memory_stats"]["arch_notes"] == len(INIT_FILES["arch_notes"])
    empty = store.summary(store.lineup()[1], sources=SRC)
    assert empty["core"]["complete"] is True and empty["memory_stats"] == {"arch_notes": 0, "digest": 0, "playbook": 0, "questions": 0, "people": 0, "record": ""}
    assert json.loads(json.dumps(data))  # plain JSON


def test_the_detail_adds_the_core_the_memory_and_the_proposals(store: BotStore):
    bot = store.lineup()[1]
    store.set_core(bot, CoreFiles("身份。", "你说话短。", "先看事实。", (("口头禅", "「行。」"),)))
    init_bot(store, bot)
    store.write_part(bot, "people", "- Alice：话多", SID)
    store.add_record(bot, "角色", False)
    pid = store.stage_core(bot, "voice", "更短。", "r", "复盘 g")
    data = store.detail(bot, SID, sources=SRC)
    assert set(data) == SUMMARY_KEYS | {"memory"} and isinstance(data["staging"], list) and data["staging"][0]["id"] == pid
    core = data["core"]
    assert core["markdown"] == bf.render_core_markdown(store.core(bot), "Bob") and bf.parse_core_markdown(core["markdown"]).core == store.core(bot)
    assert core["sections"] == {"identity": "身份。", "voice": "你说话短。", "mind": "先看事实。"}
    assert core["extras"] == [{"title": "口头禅", "slug": "x1", "chars": 4, "text": "「行。」"}]
    assert core["complete"] is True and core["chars"] == {"identity": 3, "voice": 5, "mind": 5}
    memory = data["memory"]
    assert memory["scenario_id"] == SID and memory["arch_notes"] == INIT_FILES["arch_notes"] and memory["digest"] == INIT_FILES["digest"]
    assert [e["text"] for e in memory["playbook"]] == ["先看票型再听发言。", "平票时多听一轮。"] and memory["playbook"][0]["tags"] == ["投票"]
    assert memory["playbook_md"].startswith("## 假设\n- [把握:中|支持:1|反例:0|来源:初始化|#投票] 先看票型") and memory["questions"] == INIT_FILES["questions"]
    assert memory["people"] == {"Alice": "话多"} and memory["people_md"] == "- Alice：话多" and memory["record"] == "场次 1，胜 0，负 1，胜率 0%\n- 角色：1 场，胜 0"
    # another scenario: the same notes / impressions / record, nothing of the scenario's own
    other = store.detail(bot, "other", sources=SRC)["memory"]
    assert other["scenario_id"] == "other" and other["digest"] == "" and other["playbook"] == [] and other["arch_notes"] == INIT_FILES["arch_notes"] and other["people"] == {"Alice": "话多"}
    # the default is the scenario of the sources
    assert store.detail(bot, sources=SRC)["memory"]["scenario_id"] == SID
    with pytest.raises(BotNotFound):
        store.detail("b-ffffffff", SID, sources=SRC)


def test_the_start_check_names_every_reason(store: BotStore):
    a, b, c = store.lineup()
    # a: no core at all; b: a core that is not complete; c: fine core, never initialized
    store.clear_core([a])
    store.set_core(b, CoreFiles("只有身份", "", ""))
    assert [(i["code"], i["fix"]) for i in store.problems(a, sources=SRC)] == [("core_missing", "import_core"), ("not_initialized", "init")]
    items = store.problems(b, sources=SRC)
    assert [(i["code"], i["fix"]) for i in items] == [("core_incomplete", "edit_core"), ("not_initialized", "init")]
    assert items[0]["message"] == "底层设定不完整：缺少「说话方式」、「分析方式」。"
    assert [i["code"] for i in store.problems(c, sources=SRC)] == ["not_initialized"]
    assert all(set(i) == {"code", "message", "fix"} and i["message"] for bot in (a, b, c) for i in store.problems(bot, sources=SRC))
    # a core that is over a limit (edited by hand on disk) is reported too
    (store.bot_dir(c) / "core" / "voice.md").write_text("说" * 612 + "\n", encoding="utf-8")
    assert store.problems(c, sources=SRC)[0] == {"code": "core_incomplete", "message": "底层设定不完整：「说话方式」最多 600 字（现在 612 字）。", "fix": "edit_core"}
    (store.bot_dir(c) / "core" / "voice.md").write_text(CORE_TEXT["voice"] + "\n", encoding="utf-8")
    init_bot(store, c)
    assert store.problems(c, sources=SRC) == []
    assert store.problem_entry(c, sources=SRC) is None
    # stale: says what changed, in words
    (item,) = store.problems(c, sources=SourceHashes("c2", "s2", "", SID))
    assert item == {"code": "stale", "message": "初始化之后官方契约、剧本变了，需要重新初始化。", "fix": "init"}
    (item,) = store.problems(c, sources=SourceHashes("c1", "s1", "mm", SID))
    assert "学习材料" in item["message"]
    store.set_core(c, CoreFiles("改", "改。", "改。"))
    assert "底层设定" in store.problems(c, sources=SRC)[0]["message"]
    # failed, running
    store.fail_init(c, "没有写完。")
    assert store.problems(c, sources=SRC) == [{"code": "init_failed", "message": "上次初始化没有成功：没有写完。", "fix": "init"}]
    assert store.problems(c, sources=SRC, running=True)[0]["code"] == "init_running"
    # a required memory file that is gone
    init_bot(store, c, sources=SRC)
    store.set_core(c, CoreFiles("Alice：一句话身份。", "Alice 的说话方式。\n「好。」", "Alice 的分析方式。"))
    init_bot(store, c)
    assert store.problems(c, sources=SRC) == []
    store.clear_part(c, "playbook", SID)
    store.clear_part(c, "arch_notes", SID)
    (item,) = store.problems(c, sources=SRC)
    assert item["code"] == "not_initialized" and item["message"] == "缺少必需的记忆文件：操作笔记、打法，请重新初始化。" and item["fix"] == "init"
    # the Problem as the dialog groups it
    entry = store.problem_entry(c, sources=SRC)
    assert entry == {"scope": "bot", "bot_id": c, "seat": "N3", "name": "Carol", "items": [item]}


# ======================================================================================================================
# installing a bot directory (.arenabot import, the default library)
# ======================================================================================================================
def export_to(store: BotStore, bot_id: str, target: Path) -> Path:
    """What the files stream does with a ``.arenabot``: the files of the bot, unpacked into a directory."""
    for rel, path in store.bot_files(bot_id):
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target / rel)
    return target


def test_a_bot_directory_replaces_a_bot_completely_and_keeps_its_id_and_seat(store: BotStore, tmp_path: Path):
    a, b, c = store.lineup()
    # a rich bot in another table, to be imported into seat 2
    other = BotStore(tmp_path / "other", seats=3, library_dir=tmp_path / "nowhere", clock=Clock(1_900_000_000))
    other.bootstrap()
    donor = other.lineup()[0]
    other.update_bot(donor, name="Donor")
    other.set_core(donor, CoreFiles("捐赠者的身份", "捐赠者说话。", "捐赠者分析。", (("口头禅", "「好。」"),)))
    init_bot(other, donor)
    other.write_part(donor, "people", "- Alice：爱带节奏", SID)
    other.add_record(donor, "角色", True)
    other.stage_core(donor, "mind", "新的分析方式", "原因", "复盘 g")
    staged = export_to(other, donor, tmp_path / "staged")
    donor_avatar = raw_meta(other, donor)["avatar"]

    store.install_bot_dir(b, staged)
    assert store.lineup() == [a, b, c]  # the id and the seat did not change
    assert store.name_of(b) == "Donor" and raw_meta(store, b)["avatar"] == donor_avatar and raw_meta(store, b)["id"] == b
    assert raw_meta(store, b)["origin"] == "import"
    assert store.core(b) == other.core(donor)
    assert store.read_part(b, "arch_notes", SID) == INIT_FILES["arch_notes"] and store.read_part(b, "people") == "- Alice：爱带节奏"
    assert store.load_mind(b, SID).record.startswith("场次 1，胜 1")
    assert [p["section"] for p in store.staging(b)] == ["mind"]
    # the status follows from the hashes in the file: the same contract / scenario -> ready (L0 hash is the same text)
    assert store.summary(b, sources=SRC)["init"]["status"] == "ready"
    assert store.summary(b, sources=SourceHashes("other", "s1", "", SID))["init"]["status"] == "stale"
    assert dir_names(store) == sorted([a, b, c, "lineup.json"]) and not list(store.bot_dir(b).glob("*.tmp"))
    # the next import replaces everything again: nothing of the first one is left behind
    bare = write_bot_dir(tmp_path / "bare", "Bare")
    store.install_bot_dir(b, bare)
    assert store.name_of(b) == "Bare" and store.read_part(b, "arch_notes", SID) == "" and store.staging(b) == [] and store.load_mind(b, SID).record == ""
    assert store.summary(b, sources=SRC)["init"]["status"] == "uninitialized"
    assert not (store.bot_dir(b) / "rules").exists() and not (store.bot_dir(b) / "people.md").exists()
    assert raw_meta(store, b)["avatar"] and raw_meta(store, b)["avatar"] != donor_avatar  # (the file had none: a new one)


def test_the_id_and_the_unknown_files_in_a_bot_directory_are_ignored(store: BotStore, tmp_path: Path):
    a, b, _ = store.lineup()
    staged = write_bot_dir(tmp_path / "s", "Newcomer", id=a, created_at=1_700_000_000)  # (an id of someone else)
    (staged / "notes.txt").write_text("not ours", encoding="utf-8")
    (staged / "core" / "extra-BAD.md").write_text("# 无效\n\nslug 不合法", encoding="utf-8")
    (staged / "rules" / "bad dir").mkdir(parents=True)
    (staged / "rules" / "bad dir" / "digest.md").write_text("不会被带入", encoding="utf-8")
    (staged / "evil").mkdir()
    (staged / "evil" / "x.md").write_text("x", encoding="utf-8")
    store.install_bot_dir(b, staged)
    assert store.name_of(b) == "Newcomer" and store.name_of(a) == "Alice" and store.lineup()[:2] == [a, b]
    assert raw_meta(store, b)["id"] == b and raw_meta(store, b)["created_at"] == 1_700_000_000
    assert sorted(str(p.relative_to(store.bot_dir(b))).replace("\\", "/") for p in store.bot_dir(b).rglob("*") if p.is_file()) == [
        "bot.json", "core/identity.md", "core/mind.md", "core/voice.md",
    ]


def test_a_bot_directory_that_is_not_acceptable_changes_nothing(store: BotStore, tmp_path: Path):
    a, b, c = store.lineup()
    init_bot(store, b)
    snapshot = {rel: path.read_bytes() for rel, path in store.bot_files(b)}

    def attempt(staged: Path, error: type[Exception]) -> Exception:
        with pytest.raises(error) as info:
            store.install_bot_dir(b, staged)
        assert {rel: path.read_bytes() for rel, path in store.bot_files(b)} == snapshot  # the old bot is as it was
        assert dir_names(store) == sorted([a, b, c, "lineup.json"])  # no temporary directory
        return info.value

    err = attempt(write_bot_dir(tmp_path / "t1", "alice"), NameTaken)  # (another bot's name, any case)
    assert err.status == 409 and err.code == "name_taken" and "已被 N1 使用" in str(err)
    bad_core = write_bot_dir(tmp_path / "t2", "Fine", core={**CORE_TEXT, "voice": "说" * 612, "mind": "x\n# 标题行\ny"})
    problems = attempt(bad_core, bf.BotFileError).problems
    assert "「说话方式」最多 600 字（现在 612 字）。" in problems and any("「分析方式」里不能有以“# ”开头的行" in p for p in problems)
    bad_memory = write_bot_dir(tmp_path / "t3", "Fine")
    (bad_memory / "arch").mkdir()
    (bad_memory / "arch" / "notes.md").write_text("字" * 801, encoding="utf-8")
    (bad_memory / "rules" / SID).mkdir(parents=True)
    (bad_memory / "rules" / SID / "playbook.md").write_text("只是一段话", encoding="utf-8")
    problems = attempt(bad_memory, bf.BotFileError).problems
    assert any(p.startswith("arch/notes.md：「操作笔记」最多 800 字") for p in problems) and any(p.startswith(f"rules/{SID}/playbook.md：没有读出任何一条打法") for p in problems)
    broken = tmp_path / "t4"
    broken.mkdir()
    (broken / "bot.json").write_text("{not json", encoding="utf-8")
    assert "bot.json" in str(attempt(broken, bf.BotFileError))
    assert "bot.json" in str(attempt(tmp_path / "t5-does-not-exist", bf.BotFileError)) or "不存在" in str(attempt(tmp_path / "t5-does-not-exist", bf.BotFileError))
    nameless = write_bot_dir(tmp_path / "t6", "")
    assert "名字" in str(attempt(nameless, bf.BotFileError))
    with pytest.raises(BotNotFound):
        store.install_bot_dir("b-ffffffff", write_bot_dir(tmp_path / "t7", "Fine"))


def test_a_failure_while_swapping_in_the_new_directory_leaves_the_old_bot(store: BotStore, tmp_path: Path, monkeypatch):
    a, b, c = store.lineup()
    before = {rel: path.read_bytes() for rel, path in store.bot_files(b)}

    def boom(target: Path, fresh: Path) -> None:
        raise bots_mod.StorageError("disk full")

    monkeypatch.setattr(bots_mod, "_swap_dir", boom)
    with pytest.raises(bots_mod.StorageError):
        store.install_bot_dir(b, write_bot_dir(tmp_path / "new", "Newcomer"))
    assert {rel: path.read_bytes() for rel, path in store.bot_files(b)} == before and store.name_of(b) == "Bob"
    assert dir_names(store) == sorted([a, b, c, "lineup.json"])


def test_the_swap_puts_the_old_directory_back_when_the_second_rename_fails(tmp_path: Path, monkeypatch):
    target, fresh = tmp_path / "target", tmp_path / "fresh"
    target.mkdir()
    (target / "a.txt").write_text("old", encoding="utf-8")
    fresh.mkdir()
    (fresh / "a.txt").write_text("new", encoding="utf-8")
    real_rename = Path.rename

    def flaky(self: Path, other):
        if self == fresh:
            raise OSError("no")
        return real_rename(self, other)

    monkeypatch.setattr(Path, "rename", flaky)
    with pytest.raises(bots_mod.StorageError):
        bots_mod._swap_dir(target, fresh)
    monkeypatch.undo()
    assert (target / "a.txt").read_text(encoding="utf-8") == "old" and fresh.is_dir()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fresh", "target"]
    bots_mod._swap_dir(target, fresh)
    assert (target / "a.txt").read_text(encoding="utf-8") == "new" and sorted(p.name for p in tmp_path.iterdir()) == ["target"]


def test_a_bot_of_the_default_library_can_be_imported_into_a_seat(store: BotStore, lib: Path):
    a, b, c = store.lineup()
    write_bot_dir(lib / "Zoe", "Zoe", core={"identity": "Zoe 身份。", "voice": "Zoe 说话。", "mind": "Zoe 分析。"})
    assert [d["name"] for d in store.defaults()] == ["Alice", "Bob", "Carol", "Zoe"]
    assert set(store.defaults()[0]) == {"name", "avatar", "identity"} and store.defaults()[3]["identity"] == "Zoe 身份。"
    store.import_default(c, "zoe")  # any case
    assert store.name_of(c) == "Zoe" and store.core(c).identity == "Zoe 身份。" and raw_meta(store, c)["origin"] == "default"
    with pytest.raises(NameTaken):
        store.import_default(c, "Alice")  # (another bot at the table has it)
    store.import_default(a, "Alice")  # its own default is fine: the bot is reset to it
    with pytest.raises(BotNotFound):
        store.import_default(c, "Nobody")
    with pytest.raises(BotNotFound):
        store.import_default("b-ffffffff", "Zoe")
    assert store.name_of(c) == "Zoe"


def test_a_long_identity_of_the_library_is_shortened_for_the_list(tmp_path: Path):
    library = tmp_path / "lib"
    write_bot_dir(library / "Alice", "Alice", core={"identity": "第一句话。" + "后面很长" * 40, "voice": "v", "mind": "m"})
    write_bot_dir(library / "Bob", "Bob", core={"identity": "没有句号" * 30, "voice": "v", "mind": "m"})
    shown = {d["name"]: d["identity"] for d in BotStore(tmp_path / "data", library_dir=library).defaults()}
    assert shown["Alice"] == "第一句话。" and shown["Bob"].endswith("…") and len(shown["Bob"]) == 60


# ======================================================================================================================
# integrity (after a whole data directory was replaced)
# ======================================================================================================================
def test_a_table_that_is_fine_has_no_integrity_problems(store: BotStore):
    assert store.check_integrity() == []


def test_the_integrity_check_names_what_is_wrong(store: BotStore, tmp_path: Path):
    a, b, c = store.lineup()
    (store.bot_dir(b) / "core" / "voice.md").write_text("说" * 612 + "\n", encoding="utf-8")
    shutil.rmtree(store.bot_dir(c))
    problems = store.check_integrity()
    assert any("玩家「Bob」：「说话方式」最多 600 字（现在 612 字）。" == p for p in problems)
    assert any(f"阵容里的玩家 {c} 没有对应的 bot 目录。" == p for p in problems) and any("阵容里有 3 个玩家，应该是 3 个" not in p for p in problems)
    raw = json.loads((store.root / "lineup.json").read_text(encoding="utf-8"))
    raw["order"] = [a, a]
    (store.root / "lineup.json").write_text(json.dumps(raw), encoding="utf-8")
    problems = store.check_integrity()
    assert "阵容里有 2 个玩家，应该是 3 个。" in problems and "阵容里有重复的玩家。" in problems
    (store.root / "lineup.json").unlink()
    assert store.check_integrity()[0].startswith("缺少阵容文件")
    names_twice = BotStore(tmp_path / "twice", seats=2)
    names_twice.bootstrap()
    x, y = names_twice.lineup()
    meta = raw_meta(names_twice, y)
    meta["name"] = "ALICE"
    (names_twice.bot_dir(y) / "bot.json").write_text(json.dumps(meta), encoding="utf-8")
    assert "玩家「ALICE」和另一个玩家重名了。" in names_twice.check_integrity()


# ======================================================================================================================
# concurrency
# ======================================================================================================================
def test_many_threads_changing_the_same_bots_never_corrupt_them(store: BotStore):
    a, b, c = store.lineup()
    errors: list[BaseException] = []
    texts = [f"第 {i} 份笔记：" + "字" * 50 for i in range(6)]

    def writer(index: int) -> None:
        try:
            for round_ in range(12):
                assert store.write_part(a, "arch_notes", texts[index], SID).ok
                assert store.read_part(a, "arch_notes", SID) in texts
                store.add_record(a, "角色", round_ % 2 == 0)
                assert store.write_part(b, "people", f"- Carol：印象 {index}-{round_}", SID).ok
                pid = store.stage_core(c, "mind", f"提案 {index}-{round_}", "r", "s")
                store.reject_staging(c, pid)
                store.summary(a, sources=SRC)
                store.set_lineup([a, b, c] if round_ % 2 else [c, b, a])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert store.read_part(a, "arch_notes", SID) in texts
    assert bf.read_record(store.bot_dir(a)).games == 6 * 12  # every game counted
    assert store.staging(c) == []
    assert sorted(store.lineup()) == sorted([a, b, c])
    assert bf.parse_people(store.read_part(b, "people")).get("Carol", "").startswith("印象")
    assert sorted(p.name for p in store.root.iterdir()) == sorted([a, b, c, "lineup.json"])
    assert not [p for p in store.root.rglob("*") if p.name.endswith(".tmp")]


def test_renames_at_the_same_time_keep_every_name_unique(store: BotStore):
    ids = list(ids_by_name(store).values())
    results: list[object] = []

    def rename(bot_id: str) -> None:
        try:
            results.append(store.update_bot(bot_id, name="Same"))
        except BaseException as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=rename, args=(i,)) for i in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    winners = [r for r in results if isinstance(r, str)]
    losers = [r for r in results if isinstance(r, NameTaken)]
    assert len(winners) == 1 and len(losers) == 2
    assert sorted(n.casefold() for n in store.names().values()).count("same") == 1


# ======================================================================================================================
# the process-wide store
# ======================================================================================================================
def test_the_process_wide_store_follows_the_data_directory(isolated_external_storage: Path, monkeypatch, tmp_path: Path):
    from app.storage import clear_store_caches

    assert not (isolated_external_storage / "bots").exists()  # (nothing is created until the store is asked for)
    first = bots_mod.get_bot_store()
    assert first is bots_mod.get_bot_store() and (isolated_external_storage / "bots" / "lineup.json").is_file()
    assert len(first.lineup()) == 12 and first.data_dir == isolated_external_storage.resolve()
    other = tmp_path / "other-data"
    monkeypatch.setenv("ARENA_DATA_DIR", str(other))
    second = bots_mod.get_bot_store()  # (no reset needed: the directory changed)
    assert second is not first and (other / "bots" / "lineup.json").is_file()
    clear_store_caches()
    assert bots_mod.get_bot_store() is not second  # (what the tests' fixtures use to forget a store)
    monkeypatch.setenv("ARENA_DATA_DIR", str(isolated_external_storage))
    assert bots_mod.get_bot_store(seats=2).seats == 2 and len(bots_mod.get_bot_store(seats=2).lineup()) == 2


# ======================================================================================================================
# more of the store
# ======================================================================================================================
def test_a_bot_that_is_not_frozen_gets_a_new_avatar_in_place(store: BotStore):
    ids = ids_by_name(store)
    bob = ids["Bob"]
    before = raw_meta(store, bob)["avatar"]
    assert store.regenerate_avatar(bob) == bob
    after = raw_meta(store, bob)["avatar"]
    assert after != before and after not in [raw_meta(store, i)["avatar"] for i in ids.values() if i != bob]
    assert store.moderator()["avatar"] != after


def test_a_reset_that_fails_half_way_leaves_the_old_bot_and_no_new_directory(store: BotStore, monkeypatch):
    a, b, c = store.lineup()
    init_bot(store, a)
    before = {rel: path.read_bytes() for rel, path in store.bot_files(a)}

    def boom(raw):
        raise bots_mod.StorageError("disk full")

    monkeypatch.setattr(store, "_write_lineup_raw", boom)
    with pytest.raises(bots_mod.StorageError):
        store.update_bot(a, name="Alicia", confirm_reset=True)
    monkeypatch.undo()
    assert store.lineup() == [a, b, c] and store.name_of(a) == "Alice"
    assert {rel: path.read_bytes() for rel, path in store.bot_files(a)} == before
    assert dir_names(store) == sorted([a, b, c, "lineup.json"])  # the half-made new directory is gone


def test_the_files_of_a_bot_are_listed_for_the_export(store: BotStore):
    bot = store.lineup()[0]
    store.set_core(bot, CoreFiles("身份。", "说话。", "分析。", (("口头禅", "「好。」"),)))
    init_bot(store, bot)
    store.write_part(bot, "people", "- Bob：话少", SID)
    store.add_record(bot, "角色", True)
    store.stage_core(bot, "voice", "新的", "r", "s")
    (store.bot_dir(bot) / "scratch.txt").write_text("not part of the bot", encoding="utf-8")
    (store.bot_dir(bot) / "rules" / "not a scenario").mkdir()
    (store.bot_dir(bot) / "rules" / "not a scenario" / "digest.md").write_text("x", encoding="utf-8")
    listed = store.bot_files(bot)
    assert [rel for rel, _ in listed] == [
        "bot.json", "core/identity.md", "core/voice.md", "core/mind.md", "core/extra-x1.md", "arch/notes.md",
        f"rules/{SID}/digest.md", f"rules/{SID}/playbook.md", f"rules/{SID}/questions.md", "people.md", "record.json",
        listed[-1][0],
    ]
    assert listed[-1][0].startswith("staging/p-") and listed[-1][0].endswith(".json")
    assert all(path.is_file() for _rel, path in listed)
    assert bots_mod.iter_bot_files(store.bot_dir(bot)) == listed
    with pytest.raises(BotNotFound):
        store.bot_files("b-ffffffff")
    assert store.validate_bot_dir(store.bot_dir(bot), for_bot=bot) == "Alice"
    with pytest.raises(NameTaken):
        store.validate_bot_dir(store.bot_dir(bot), for_bot=store.lineup()[1])  # (it would collide with its original)


def test_the_limits_of_the_payload_are_the_constants_of_the_types():
    limits = bots_mod.limits_payload()
    assert limits["core_limits"] == {"identity": 300, "voice": 600, "mind": 400} and limits["core_extra_max"] == 3 and limits["core_extra_chars"] == 300
    assert (limits["arch_notes_chars"], limits["digest_chars"], limits["questions_chars"]) == (800, 800, 400)
    assert (limits["playbook_max"], limits["playbook_entry_chars"], limits["people_max"], limits["impression_chars"]) == (20, 80, 30, 80)
    assert (limits["name_chars"], limits["init_timeout_s"], limits["review_timeout_s"]) == (20, 120, 60)
    assert limits["memory_parts"] == ["arch_notes", "digest", "playbook", "questions", "people"]
    assert limits["part_labels"]["questions"] == "待解问题"
    assert json.loads(json.dumps(limits))


def test_the_derived_status_is_a_pure_function_of_what_is_stored_and_what_is_now():
    stored = bots_mod.normalize_init({
        "status": "ready", "at": 7, "model": "m", "duration_s": 1.5, "scenario_id": "demo",
        "hashes": {"core": "k", "contract": "c", "scenario": "s", "materials": ""},
    })
    now = SourceHashes("c", "s", "", "demo")
    assert bots_mod.derive_init(stored, "k", now)["status"] == "ready"
    assert bots_mod.derive_init(stored, "k2", now) == {"status": "stale", "at": 7, "model": "m", "duration_s": 1.5, "error": None, "stale": ["core"]}
    assert bots_mod.derive_init(stored, "k", SourceHashes("c", "s", "", "other"))["stale"] == ["scenario"]
    assert bots_mod.derive_init(stored, "k", SourceHashes("c2", "s", "", "other"))["stale"] == ["contract", "scenario"]  # (in the order of the hashes)
    assert bots_mod.derive_init(stored, "k", SourceHashes("c", "s", "", ""))["status"] == "ready"  # (no scenario asked for: only the hashes count)
    assert bots_mod.derive_init(stored, "k2", now, running=True)["status"] == "initializing"
    failed = bots_mod.normalize_init({"status": "failed", "error": "  超时 ", "hashes": {"core": "x"}})
    assert failed["hashes"] is None and failed["error"] == "超时"
    assert bots_mod.derive_init(failed, "k", now)["error"] == "超时" and bots_mod.derive_init(stored, "k", now)["error"] is None
    # junk is read as "not initialized"
    for junk in (None, "x", 5, [], {"status": ["ready"]}, {"status": "weird", "at": "now", "duration_s": True, "model": 3}):
        assert bots_mod.normalize_init(junk) == bots_mod.empty_init()


def test_the_line_up_can_be_read_without_a_store(store: BotStore, tmp_path: Path):
    assert bots_mod.read_lineup_ids(store.data_dir) == store.lineup()
    assert bots_mod.read_lineup_ids(tmp_path / "nothing") == []
    (store.root / "lineup.json").write_text(json.dumps({"order": ["b-00000001", "not an id", 5, "b-00000001", "b-00000002"]}), encoding="utf-8")
    assert bots_mod.read_lineup_ids(store.data_dir) == ["b-00000001", "b-00000002"]  # (what is listed, valid ids only, once)
    (store.root / "lineup.json").write_text("[1, 2]", encoding="utf-8")
    assert bots_mod.read_lineup_ids(store.data_dir) == []
    (store.root / "lineup.json").write_text("not json", encoding="utf-8")
    assert bots_mod.read_lineup_ids(store.data_dir) == []


def test_a_hand_made_bot_json_with_odd_values_is_read_gently(store: BotStore):
    bot = store.lineup()[0]
    (store.bot_dir(bot) / "bot.json").write_text(
        json.dumps({"id": "b-someoneelse", "name": "Odd:Name", "avatar": [], "created_at": "yesterday", "origin": "mystery", "init": "no"}),
        encoding="utf-8",
    )
    data = store.summary(bot, sources=SRC)
    assert data["id"] == bot  # the directory decides the id
    assert data["name"] == "Odd:Name"  # (a stored name is shown as it is, even one that could not be typed now)
    assert data["avatar"] is None and data["avatar_code"] is None and data["origin"] == "user" and data["init"]["status"] == "uninitialized"
    (store.bot_dir(bot) / "bot.json").write_text('{"name": 7}', encoding="utf-8")
    assert store.name_of(bot) == "7"
    (store.bot_dir(bot) / "bot.json").write_text("[]", encoding="utf-8")
    with pytest.raises(BotNotFound):
        store.name_of(bot)  # (not a bot any more)
    assert bot not in store.lineup()


def test_the_default_library_cannot_rebuild_a_table_that_has_bots(store: BotStore):
    before = store.lineup()
    with pytest.raises(BotError) as info:
        store.install_defaults()
    assert "已经有玩家了" in str(info.value) and store.lineup() == before


def test_the_hash_of_the_core_is_available_for_a_learning_run(store: BotStore):
    bot = store.lineup()[0]
    assert store.core_hash_of(bot) == core_hash(store.core(bot))
    started = store.core_hash_of(bot)
    store.set_core(bot, CoreFiles("另一个身份", "说话。", "分析。"))
    assert store.core_hash_of(bot) != started
    with pytest.raises(BotNotFound):
        store.core_hash_of("b-ffffffff")


def test_the_tags_of_the_scenario_can_be_handed_to_a_write_of_the_playbook(store: BotStore):
    bot = store.lineup()[0]
    result = store.write_part(bot, "playbook", "- [把握:中|#投票|#陌生] 一条", SID, known_tags=["投票", "发言"])
    assert result.ok and result.message.endswith("按它检索不到：#陌生。")
    assert [e.tags for e in bf.parse_playbook(store.read_part(bot, "playbook", SID))] == [("投票", "陌生")]
    assert store.write_part(bot, "playbook", "- [把握:中|#投票] 一条", SID, known_tags=["投票"]).message.count("标签") == 0
