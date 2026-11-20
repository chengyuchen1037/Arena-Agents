"""The default bot library (``bots/``, H44): twelve L0s that all pass the checks and are all different, and what the first run does with them."""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import pytest

from app import avatars
from app.storage import botfiles as bf
from app.storage import bots as bots_mod
from app.storage.bots import BotStore
from app.storage.roster import DEFAULT_NAMES

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "bots"
NAMES = list(DEFAULT_NAMES)

# What an L0 must not say: the words of one particular game (the same list the engine packages are scanned for), and tactics.
FORBIDDEN = (
    "白狼王", "预言家", "女巫", "猎人", "守卫", "平民", "狼人", "警长", "上警", "退水", "警左", "警右", "屠边", "归票", "过麦",
    "战术指南", "Plan A", "出人", "狼队", "werewolf", "villager", "white_wolf", "witch",
    "好人", "坏人", "阵营", "金水", "银水", "查杀", "悍跳", "对跳", "站边", "倒钩", "冲锋", "抗推", "自爆", "刀口", "警徽",
)
TACTIC_MARKERS = ("应该投", "必须投", "一定要投", "先投", "不要投", "第一天", "首夜", "开局就", "发言顺序")


def library_core(name: str):
    return bf.read_core(LIBRARY / name / "core")


def all_files() -> list[Path]:
    return sorted(p for p in LIBRARY.rglob("*") if p.is_file() and p.name != "README.md")


def grams(text: str, n: int = 4) -> set[str]:
    flat = re.sub(r"\s+", "", text)
    return {flat[i : i + n] for i in range(len(flat) - n + 1)}


def longest_common_substring(a: str, b: str) -> int:
    best, previous = 0, [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        current = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                current[j] = previous[j - 1] + 1
                best = max(best, current[j])
        previous = current
    return best


def samples_of(voice: str) -> list[str]:
    return re.findall(r"「([^」]+)」", voice)


def range_of(voice: str) -> tuple[int, int]:
    found = re.findall(r"^篇幅：(\d+)–(\d+) 字$", voice, re.MULTILINE)
    assert len(found) == 1, voice
    return int(found[0][0]), int(found[0][1])


# ======================================================================================================================
# the files
# ======================================================================================================================
def test_the_library_is_the_twelve_default_names_in_folders():
    assert sorted(p.name for p in LIBRARY.iterdir() if p.is_dir()) == sorted(NAMES)
    for name in NAMES:
        assert sorted(p.name for p in (LIBRARY / name).iterdir()) == ["bot.json", "core"]
        assert sorted(p.name for p in (LIBRARY / name / "core").iterdir()) == ["identity.md", "mind.md", "voice.md"]
        meta = json.loads((LIBRARY / name / "bot.json").read_text(encoding="utf-8"))
        assert meta["name"] == name and meta["format"] == 1 and set(meta) == {"format", "name", "avatar"}  # (no id, no initialization: those are made on install)


def test_the_files_are_plain_utf8_with_unix_line_ends():
    for path in all_files():
        data = path.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf"), path  # no BOM
        assert b"\r" not in data, path
        assert data.endswith(b"\n") and not data.endswith(b"\n\n"), path
        data.decode("utf-8")


@pytest.mark.parametrize("name", NAMES)
def test_every_default_core_passes_the_checks_of_a_real_import(name: str):
    core = library_core(name)
    assert core.complete and bf.validate_core(core, require_complete=True) == []
    assert core.extras == ()
    # (it survives the merged file that is exported and imported: ``<名字>.core.md``)
    merged = bf.render_core_markdown(core, name)
    parsed = bf.parse_core_markdown(merged)
    assert parsed.core == core and parsed.name == name and bf.render_core_markdown(parsed.core, parsed.name) == merged
    assert 0 < len(core.identity) <= 300 and 0 < len(core.voice) <= 600 and 0 < len(core.mind) <= 400


# ======================================================================================================================
# what they say
# ======================================================================================================================
@pytest.mark.parametrize("name", NAMES)
def test_the_voice_is_prose_with_three_to_five_samples_and_a_length_range(name: str):
    voice = library_core(name).voice
    samples = samples_of(voice)
    assert 3 <= len(samples) <= 5, samples
    assert all(len(s) >= 4 for s in samples) and len(set(samples)) == len(samples)
    low, high = range_of(voice)
    assert 30 <= low < high <= 280 and high - low >= 50
    prose = voice.split("\n")[0]
    assert len(prose) >= 60 and not re.search(r"^\s*(?:[-*•]|\d+[.、)）])", voice, re.MULTILINE)  # connected prose, no bullets or numbers
    assert voice.splitlines()[-1] == f"篇幅：{low}–{high} 字"


@pytest.mark.parametrize("name", NAMES)
def test_every_bot_plays_to_win_and_has_the_same_attitude_in_its_own_words(name: str):
    core = library_core(name)
    assert "赢" in core.identity  # the premise of every one of them
    mind = core.mind
    # what happened comes first ...
    assert any(word in mind for word in ("出局", "投了谁", "记录", "票", "事实")), mind
    # ... fine words are not evidence ...
    assert any(word in mind for word in ("漂亮", "动听", "太圆", "流畅", "顺", "嗓门", "气势", "头头是道", "再有条理", "对不上", "再动人", "热闹")), mind
    # ... and a call that the result proves wrong is given up
    assert any(word in mind for word in ("改", "认", "换", "撤回", "纠正", "承认", "看偏", "打脸")), mind


def test_nothing_in_the_library_is_about_a_particular_game_or_a_tactic():
    for path in all_files():
        text = path.read_text(encoding="utf-8").replace("老好人", "")  # ("老好人" is a kind of person, not the game's "good guys")
        for word in (*FORBIDDEN, *TACTIC_MARKERS):
            assert word not in text, f"{path.relative_to(ROOT)}: {word!r}"


def test_the_twelve_cores_are_all_different():
    cores = {name: library_core(name) for name in NAMES}
    for field in ("identity", "voice", "mind"):
        texts = [getattr(core, field) for core in cores.values()]
        assert len(set(texts)) == 12, field
    # no sample is used twice, and no two bots share a length range
    all_samples = [s for core in cores.values() for s in samples_of(core.voice)]
    assert len(all_samples) == len(set(all_samples))
    ranges = [range_of(core.voice) for core in cores.values()]
    assert len(set(ranges)) == 12 and len({low for low, _ in ranges}) >= 8 and len({high for _, high in ranges}) >= 8
    assert min(low for low, _ in ranges) <= 40 and max(high for _, high in ranges) >= 250  # (from the terse to the talkative)
    # ... and they are not the same text in other words: little is shared between any two (each bot uses its own words)
    for x, y in itertools.combinations(NAMES, 2):
        for field in ("identity", "voice", "mind"):
            a, b = getattr(cores[x], field), getattr(cores[y], field)
            overlap = len(grams(a) & grams(b)) / max(1, len(grams(a) | grams(b)))
            assert overlap <= 0.15, (field, x, y, overlap)
            assert longest_common_substring(a, b) <= 18, (field, x, y)


# ======================================================================================================================
# the avatars
# ======================================================================================================================
def test_the_avatars_are_valid_fixed_and_clearly_different():
    found = {name: json.loads((LIBRARY / name / "bot.json").read_text(encoding="utf-8"))["avatar"] for name in NAMES}
    for name, description in found.items():
        assert bots_mod.validate_avatar(description) == description, name  # (the stored form is the validated one)
        assert bots_mod.avatar_code(description) and len(bots_mod.avatar_code(description)) == 24
    for x, y in itertools.combinations(NAMES, 2):
        assert avatars.description_distance(found[x], found[y]) >= 3, (x, y)
    assert len({json.dumps(d, sort_keys=True) for d in found.values()}) == 12


# ======================================================================================================================
# first run, import
# ======================================================================================================================
def test_a_new_data_directory_gets_exactly_these_twelve_bots(tmp_path: Path):
    store = BotStore(tmp_path / "data", library_dir=LIBRARY)
    store.bootstrap()
    ids = store.lineup()
    assert [store.name_of(i) for i in ids] == NAMES
    for bot_id, name in zip(ids, NAMES):
        meta = json.loads((store.bot_dir(bot_id) / "bot.json").read_text(encoding="utf-8"))
        assert store.core(bot_id) == library_core(name) and store.core(bot_id).complete
        assert meta["origin"] == "default" and meta["init"]["status"] == "uninitialized" and meta["id"] == bot_id
        assert meta["avatar"] == json.loads((LIBRARY / name / "bot.json").read_text(encoding="utf-8"))["avatar"]  # (the same faces every time)
        assert sorted(p.name for p in store.bot_dir(bot_id).iterdir()) == ["bot.json", "core"]
        assert [i["code"] for i in store.problems(bot_id)] == ["not_initialized"]  # (nothing else is wrong with a default bot)
    assert store.check_integrity() == []
    assert store.moderator()["avatar"] and store.moderator()["avatar"] not in [store.summary(i)["avatar"] for i in ids]
    assert [d["name"] for d in store.defaults()] == NAMES and all(d["identity"] and d["avatar"] for d in store.defaults())


def test_the_library_of_the_program_is_found_from_a_temporary_program_directory(tmp_path: Path):
    folder = bots_mod.default_library_dir()
    assert folder.is_dir() and sorted(p.name for p in folder.iterdir() if p.is_dir()) == sorted(NAMES)
    assert [d["name"] for d in BotStore(tmp_path / "no-data-here").defaults()] == NAMES  # (the store of a data directory that does not exist yet)


@pytest.mark.parametrize("name", NAMES)
def test_any_default_can_be_imported_into_a_seat(tmp_path: Path, name: str):
    BotStore(tmp_path / "data", seats=3, library_dir=tmp_path / "empty").bootstrap()  # a table of blank bots: Alice, Bob, Carol
    store = BotStore(tmp_path / "data", seats=3, library_dir=LIBRARY)
    store.bootstrap()
    assert [store.name_of(i) for i in store.lineup()] == ["Alice", "Bob", "Carol"]  # (a table that exists is not rebuilt)
    seat = store.lineup()[2]
    taken = {store.name_of(i).casefold() for i in store.lineup() if i != seat}
    if name.casefold() in taken:
        with pytest.raises(bots_mod.NameTaken):  # (the name of another bot at this table)
            store.import_default(seat, name)
        assert store.name_of(seat) == "Carol"
        return
    store.import_default(seat, name)
    assert store.name_of(seat) == name and store.core(seat) == library_core(name) and store.core(seat).complete
    assert json.loads((store.bot_dir(seat) / "bot.json").read_text(encoding="utf-8"))["avatar"] == json.loads((LIBRARY / name / "bot.json").read_text(encoding="utf-8"))["avatar"]
