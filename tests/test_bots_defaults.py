"""The default bot library (``bots/``, H44): twelve L0s that all pass the checks and are all different, what the first run does with them - and what
happens when the library carries the result of a real initialization.

The library is ``bots/<name>/bot.json`` + ``core/`` (the L0).  It MAY also carry what ``tools/init_default_bots.py --write-library`` puts there: an
``init`` record in ``bot.json`` (the model, the time, the four hashes) and the bot's own memory (``arch/notes.md``, ``rules/<scenario>/{digest,
playbook,questions}.md``).  Both shapes are valid and this file checks both: against the shipped library, whatever it holds today, and against
made-up libraries - one without any initialization, one with every bot initialized, a mixed one and one whose initialization is out of date.  A bot
that carries a ``ready`` record is ready in a new data directory exactly as long as its hashes match the L0, the contract and the scenario of a
fresh installation (``tests/test_bots_library_fresh.py`` is the guard that keeps the shipped ones matching); one that carries none is "not
initialized".  What a bot says about the game in its memory is the game's business - the rule "no game vocabulary, no tactics" is the L0's.
"""

from __future__ import annotations

import itertools
import json
import re
import shutil
from pathlib import Path

import pytest

from app import avatars
from app.bots.types import SourceHashes, core_hash
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

CORE_FILES = ["identity.md", "mind.md", "voice.md"]
#: What an initialized library bot adds to ``bot.json`` and ``core/``: its operating notes and its rules memory (and nothing else: no impressions, no record).
MEMORY_FOLDERS = {"arch", "rules"}
RULE_FILES = ("digest.md", "playbook.md", "questions.md")
REQUIRED_RULE_FILES = ("digest.md", "playbook.md")
#: The memory a made-up initialized bot gets (any text that passes the store's checks will do).
NOTES = "我读过平台说明，知道被唤醒不等于必须行动。\n"
DIGEST = "我读过规则，知道每一局的身份都是重新发的。\n"
PLAYBOOK = "## 假设\n- [把握:低|支持:0|反例:0|来源:初始化|#发言] 先听完前面的人怎么说，再决定要不要表态。\n"
QUESTIONS = "我还不确定别人的发言和他们的行动是不是总对得上。\n"


def library_core(name: str):
    return bf.read_core(LIBRARY / name / "core")


def all_files(library: Path = LIBRARY) -> list[Path]:
    return sorted(p for p in library.rglob("*") if p.is_file() and p.name != "README.md")


def l0_files(library: Path = LIBRARY) -> list[Path]:
    """The files that say who a bot is: ``bot.json`` and ``core/*.md`` (not the memory an initialization wrote: that is about the game it learned)."""
    return sorted(p for p in all_files(library) if p.name == "bot.json" or p.parent.name == "core")


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
# the layout of a library, with or without initialized bots (the one function every library below goes through)
# ======================================================================================================================
def check_library_layout(library: Path) -> set[str]:
    """Assert that ``library`` is a valid default library and return the names of its bots that carry an initialization.

    A bot folder holds ``bot.json`` (``format``, ``name``, ``avatar`` and optionally ``init``) and ``core/`` (the three L0 files); a bot whose ``init`` is
    ``ready`` also holds the memory the record promises - ``arch/notes.md`` and ``rules/<scenario>/{digest,playbook}.md`` (``questions.md`` is optional) -
    and a bot that is not initialized holds nothing but its L0.  No id, no origin, no impressions, no record: those are made on installation."""
    folders = sorted(p for p in library.iterdir() if p.is_dir())
    assert sorted(p.name for p in folders) == sorted(NAMES)
    initialized: set[str] = set()
    for folder in folders:
        name = folder.name
        entries = {p.name for p in folder.iterdir()}
        assert {"bot.json", "core"} <= entries <= {"bot.json", "core", *MEMORY_FOLDERS}, f"{name}: {sorted(entries)}"
        assert sorted(p.name for p in (folder / "core").iterdir()) == CORE_FILES, name
        meta = json.loads((folder / "bot.json").read_text(encoding="utf-8"))
        assert meta["name"] == name and meta["format"] == 1, name
        assert {"format", "name", "avatar"} <= set(meta) <= {"format", "name", "avatar", "init"}, f"{name}: {sorted(meta)}"
        record = bots_mod.normalize_init(meta.get("init"))
        assert record["status"] in ("uninitialized", "ready"), f"{name}: a library ships a ready bot or an uninitialized one, not {record['status']!r}"
        if record["status"] == "uninitialized":
            assert entries == {"bot.json", "core"}, f"{name}: memory without an initialization record"
            continue
        # a ready record: who made it, with what, and the memory it promises
        assert record["model"] and record["at"] and record["scenario_id"], name
        assert set(record["hashes"]) == {"core", "contract", "scenario", "materials"}, name
        assert all(re.fullmatch(r"[0-9a-f]{64}", record["hashes"][key]) for key in ("core", "contract", "scenario")), name
        assert record["hashes"]["core"] == core_hash(bf.read_core(folder / "core")), f"{name}: the L0 changed after its initialization"
        sid = bf.check_scenario_id(record["scenario_id"])
        # (listed from the disk: ``iter_bot_files`` leaves out what is not a file of a bot, and that is exactly what must be seen here)
        found = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()} - {"bot.json", *(f"core/{filename}" for filename in CORE_FILES)}
        allowed = {"arch/notes.md", *(f"rules/{sid}/{filename}" for filename in RULE_FILES)}
        assert found <= allowed, f"{name}: files a library does not ship: {sorted(found - allowed)}"
        required = {"arch/notes.md", *(f"rules/{sid}/{filename}" for filename in REQUIRED_RULE_FILES)}
        assert required <= found, f"{name}: the record says ready but {sorted(required - found)} are missing"
        for rel in sorted(found):
            part = bots_mod.BotStore._part_of(rel)
            assert part is not None and (folder / rel).read_text(encoding="utf-8").strip(), f"{name}: {rel} is empty"
            try:
                bf.normalize_part(part, (folder / rel).read_text(encoding="utf-8"))  # (the checks of an import: formats and limits)
            except bf.BotFileError as exc:
                raise AssertionError(f"{name}: {rel} would be refused by an import: {exc}") from None
        initialized.add(name)
    return initialized


def fresh_install_hashes() -> SourceHashes:
    """What the contract, the default scenario and the (absent) materials hash to on a FRESH installation: the stores of the data directory of this
    test (a scratch one: conftest) copy the shipped defaults on their first use and the application hashes what they hold."""
    from app.config import load_config
    from app.game.sources import Sources
    from app.storage.paths import get_data_dir

    assert Path(get_data_dir()).resolve() != (ROOT / "data").resolve(), "the hashes of a fresh installation are made in a scratch data directory, never data/"
    return Sources(load_config, lambda: True).hashes()


# ---- made-up libraries ------------------------------------------------------------------------------------------------
def plain_library(target: Path) -> Path:
    """A library as it was before any initialization: ``bot.json`` (format, name, avatar) and ``core/`` only - built from the L0s of the shipped one."""
    for name in NAMES:
        (target / name / "core").mkdir(parents=True)
        for filename in CORE_FILES:
            shutil.copyfile(LIBRARY / name / "core" / filename, target / name / "core" / filename)
        meta = json.loads((LIBRARY / name / "bot.json").read_text(encoding="utf-8"))
        kept = {key: meta[key] for key in ("format", "name", "avatar")}
        (target / name / "bot.json").write_text(json.dumps(kept, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return target


def initialize(library: Path, name: str, hashes: SourceHashes, *, questions: bool = True) -> None:
    """Make the library bot ``name`` an initialized one the way ``tools/init_default_bots.py --write-library`` leaves it: the ``init`` record with the
    four hashes of ``hashes`` and of its own L0, the operating notes and the rules memory of the scenario."""
    folder = library / name
    meta = json.loads((folder / "bot.json").read_text(encoding="utf-8"))
    meta["init"] = {
        "status": "ready",
        "at": 1_763_600_500,
        "model": "a-model",
        "duration_s": 74.2,
        "error": None,
        "scenario_id": hashes.scenario_id,
        "hashes": {
            "core": core_hash(bf.read_core(folder / "core")),
            "contract": hashes.contract,
            "scenario": hashes.scenario,
            "materials": hashes.materials,
        },
    }
    (folder / "bot.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    (folder / "arch").mkdir(exist_ok=True)
    (folder / "arch" / "notes.md").write_text(NOTES, encoding="utf-8", newline="\n")
    rules = folder / "rules" / hashes.scenario_id
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "digest.md").write_text(DIGEST, encoding="utf-8", newline="\n")
    (rules / "playbook.md").write_text(PLAYBOOK, encoding="utf-8", newline="\n")
    if questions:
        (rules / "questions.md").write_text(QUESTIONS, encoding="utf-8", newline="\n")


@pytest.fixture()
def hashes() -> SourceHashes:
    return fresh_install_hashes()


@pytest.fixture()
def library_without_init(tmp_path: Path) -> Path:
    return plain_library(tmp_path / "library-plain" / "bots")


@pytest.fixture()
def library_with_init(tmp_path: Path, hashes: SourceHashes) -> Path:
    """Every one of the twelve initialized (one of them without the optional open questions)."""
    library = plain_library(tmp_path / "library-initialized" / "bots")
    for name in NAMES:
        initialize(library, name, hashes, questions=name != "Leo")
    return library


@pytest.fixture()
def library_with_some_init(tmp_path: Path, hashes: SourceHashes) -> Path:
    library = plain_library(tmp_path / "library-mixed" / "bots")
    for name in ("Alice", "Carol", "Leo"):
        initialize(library, name, hashes)
    return library


# ======================================================================================================================
# the files
# ======================================================================================================================
def test_the_library_is_the_twelve_default_names_in_folders():
    """The shipped library, whatever it holds today: the L0s, and - when a bot carries an initialization - its record and its memory."""
    initialized = check_library_layout(LIBRARY)
    assert initialized <= set(NAMES)


def test_a_library_without_any_initialization_is_a_valid_library(library_without_init: Path):
    assert check_library_layout(library_without_init) == set()
    for name in NAMES:
        assert sorted(p.name for p in (library_without_init / name).iterdir()) == ["bot.json", "core"]
        meta = json.loads((library_without_init / name / "bot.json").read_text(encoding="utf-8"))
        assert set(meta) == {"format", "name", "avatar"} and meta["name"] == name and meta["format"] == 1  # (no id, no initialization: those are made on install)


def test_a_library_with_initialized_bots_is_a_valid_library(library_with_init: Path, library_with_some_init: Path, hashes: SourceHashes):
    assert check_library_layout(library_with_init) == set(NAMES)
    assert check_library_layout(library_with_some_init) == {"Alice", "Carol", "Leo"}
    alice = library_with_init / "Alice"
    assert sorted(p.name for p in alice.iterdir()) == ["arch", "bot.json", "core", "rules"]
    assert sorted(p.name for p in (alice / "rules").iterdir()) == [hashes.scenario_id]
    assert sorted(p.name for p in (alice / "rules" / hashes.scenario_id).iterdir()) == list(RULE_FILES)
    assert not (library_with_init / "Leo" / "rules" / hashes.scenario_id / "questions.md").exists()  # (the open questions are optional)
    assert sorted(p.name for p in (library_with_some_init / "Bob").iterdir()) == ["bot.json", "core"]  # (a bot that was not initialized has its L0 only)


def _broken(library: Path, how: str) -> None:
    """Break the library in one way that a hand edit or a bad run of the tool could."""
    folder = library / "Alice"
    meta = json.loads((folder / "bot.json").read_text(encoding="utf-8"))
    sid = meta["init"]["scenario_id"]
    if how == "ready without its notes":
        (folder / "arch" / "notes.md").unlink()
    elif how == "ready without its playbook":
        (folder / "rules" / sid / "playbook.md").unlink()
    elif how == "an empty digest":
        (folder / "rules" / sid / "digest.md").write_text("\n", encoding="utf-8", newline="\n")
    elif how == "a failed record":
        meta["init"] = {"status": "failed", "error": "超时", "hashes": None}
        (folder / "bot.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8", newline="\n")
    elif how == "memory without a record":
        meta.pop("init")
        (folder / "bot.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8", newline="\n")
    elif how == "an uninitialized record with memory":
        meta["init"] = {"status": "uninitialized"}
        (folder / "bot.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8", newline="\n")
    elif how == "impressions":
        (folder / "people.md").write_text("- Bob：爱抢话。\n", encoding="utf-8", newline="\n")
    elif how == "a record of the games":
        (folder / "record.json").write_text('{"games": 3, "wins": 1, "by_role": {}}\n', encoding="utf-8", newline="\n")
    elif how == "a file of another kind":
        (folder / "rules" / sid / "notes.txt").write_text("x\n", encoding="utf-8", newline="\n")
    elif how == "an id":
        meta["id"] = "b-12345678"
        (folder / "bot.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8", newline="\n")
    elif how == "a changed L0":
        identity = folder / "core" / "identity.md"
        identity.write_text(identity.read_text(encoding="utf-8").rstrip("\n") + "又加了一句。\n", encoding="utf-8", newline="\n")
    elif how == "a note over its limit":
        (folder / "arch" / "notes.md").write_text("字" * 801 + "\n", encoding="utf-8", newline="\n")
    elif how == "a missing hash":
        meta["init"]["hashes"].pop("contract")
        (folder / "bot.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8", newline="\n")
    else:  # pragma: no cover - a typo in this file
        raise AssertionError(how)


@pytest.mark.parametrize(
    "how",
    [
        "ready without its notes",
        "ready without its playbook",
        "an empty digest",
        "a failed record",
        "memory without a record",
        "an uninitialized record with memory",
        "impressions",
        "a record of the games",
        "a file of another kind",
        "an id",
        "a changed L0",
        "a note over its limit",
        "a missing hash",
    ],
)
def test_the_layout_check_still_guards_a_library_that_may_carry_initializations(library_with_init: Path, how: str):
    """The relaxed check is not an open door: a library that ships a half-written initialization, a failed one, the learning of a game or a bot whose
    L0 was edited after its initialization is refused."""
    _broken(library_with_init, how)
    with pytest.raises(AssertionError):
        check_library_layout(library_with_init)


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


def test_nothing_in_the_l0_of_the_library_is_about_a_particular_game_or_a_tactic():
    """The L0 says who a bot is, how it talks and how it thinks - nothing about a game.  (What an initialization wrote into ``arch/`` and ``rules/`` is
    exactly about the game it read: it is not looked at here.)"""
    assert l0_files()
    for path in l0_files():
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
def check_a_new_data_directory(library: Path, data_root: Path, hashes: SourceHashes, expected_ready: set[str]) -> BotStore:
    """The first run on ``library``: twelve bots in the order of the names, each with the L0 and the avatar of its folder and with every other file of
    the folder byte for byte; ready (with nothing to fix) exactly when the folder carries a ``ready`` record - its hashes match the L0, the contract and
    the scenario of this installation - and "not initialized" otherwise."""
    store = BotStore(data_root / "data", library_dir=library)
    store.bootstrap()
    ids = store.lineup()
    assert [store.name_of(i) for i in ids] == NAMES
    for bot_id, name in zip(ids, NAMES):
        folder = library / name
        meta = json.loads((store.bot_dir(bot_id) / "bot.json").read_text(encoding="utf-8"))
        assert store.core(bot_id) == bf.read_core(folder / "core") and store.core(bot_id).complete
        assert meta["origin"] == "default" and meta["id"] == bot_id
        assert meta["avatar"] == json.loads((folder / "bot.json").read_text(encoding="utf-8"))["avatar"]  # (the same faces every time)
        installed = {rel: path.read_bytes() for rel, path in bots_mod.iter_bot_files(store.bot_dir(bot_id)) if rel != "bot.json"}
        shipped = {rel: path.read_bytes() for rel, path in bots_mod.iter_bot_files(folder) if rel != "bot.json"}
        assert installed == shipped  # (the L0 and, when there is one, the memory of its initialization: nothing more, nothing less)
        summary = store.summary(bot_id, sources=hashes)
        problems = [i["code"] for i in store.problems(bot_id, sources=hashes)]
        if name in expected_ready:
            assert meta["init"]["status"] == "ready" and meta["init"]["hashes"] and meta["init"]["scenario_id"] == hashes.scenario_id
            assert summary["init"]["status"] == "ready" and summary["init"]["stale"] == []
            assert problems == []  # (nothing else is wrong with a default bot: it can sit at the table as it is)
            assert store.is_frozen(bot_id)
        else:
            assert meta["init"]["status"] == "uninitialized"
            assert summary["init"]["status"] == "uninitialized"
            assert problems == ["not_initialized"]  # (nothing else is wrong with a default bot)
            assert not store.is_frozen(bot_id)
    assert store.check_integrity() == []
    assert store.moderator()["avatar"] and store.moderator()["avatar"] not in [store.summary(i)["avatar"] for i in ids]
    assert [d["name"] for d in store.defaults()] == NAMES and all(d["identity"] and d["avatar"] for d in store.defaults())
    return store


def test_a_new_data_directory_gets_exactly_these_twelve_bots(tmp_path: Path, hashes: SourceHashes):
    """The shipped library: its bots that carry an initialization are ready from the first start, the others are not initialized."""
    check_a_new_data_directory(LIBRARY, tmp_path, hashes, check_library_layout(LIBRARY))


def test_a_new_data_directory_from_a_library_without_initializations_has_twelve_bots_to_initialize(tmp_path: Path, library_without_init: Path, hashes: SourceHashes):
    check_a_new_data_directory(library_without_init, tmp_path, hashes, set())


def test_a_new_data_directory_from_a_library_with_initializations_has_ready_bots(tmp_path: Path, library_with_init: Path, library_with_some_init: Path, hashes: SourceHashes):
    check_a_new_data_directory(library_with_init, tmp_path / "all", hashes, set(NAMES))
    store = check_a_new_data_directory(library_with_some_init, tmp_path / "some", hashes, {"Alice", "Carol", "Leo"})
    ready = [store.name_of(i) for i in store.lineup() if store.summary(i, sources=hashes)["init"]["status"] == "ready"]
    assert ready == ["Alice", "Carol", "Leo"]


def test_an_initialization_that_does_not_match_this_installation_is_shown_as_needing_a_new_one(tmp_path: Path, library_with_init: Path, hashes: SourceHashes):
    """The ``ready`` record is only as good as its four hashes: when the contract, the scenario or the L0 differ from the ones of this installation the
    bot is ⚠ ("stale") with the sources that differ named - never silently ready, and not blocked from the table for any other reason."""
    changed_core = library_with_init / "Alice" / "core" / "identity.md"
    changed_core.write_text(changed_core.read_text(encoding="utf-8").rstrip("\n") + "又加了一句。\n", encoding="utf-8", newline="\n")
    other_contract = SourceHashes(contract="0" * 64, scenario=hashes.scenario, materials=hashes.materials, scenario_id=hashes.scenario_id)
    other_scenario = SourceHashes(contract=hashes.contract, scenario="1" * 64, materials=hashes.materials, scenario_id=hashes.scenario_id)
    store = BotStore(tmp_path / "data", library_dir=library_with_init)
    store.bootstrap()
    by_name = {store.name_of(i): i for i in store.lineup()}
    alice, bob = by_name["Alice"], by_name["Bob"]
    assert store.summary(alice, sources=hashes)["init"]["status"] == "stale" and store.summary(alice, sources=hashes)["init"]["stale"] == ["core"]
    assert [i["code"] for i in store.problems(alice, sources=hashes)] == ["stale"]
    assert store.summary(bob, sources=hashes)["init"]["status"] == "ready"
    assert store.summary(bob, sources=other_contract)["init"]["stale"] == ["contract"]
    assert store.summary(bob, sources=other_scenario)["init"]["stale"] == ["scenario"]
    assert [i["code"] for i in store.problems(bob, sources=other_contract)] == ["stale"]


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
    # (the whole bot is taken: what its initialization wrote comes with it, and nothing of what the seat had before stays)
    installed = {rel: path.read_bytes() for rel, path in bots_mod.iter_bot_files(store.bot_dir(seat)) if rel != "bot.json"}
    shipped = {rel: path.read_bytes() for rel, path in bots_mod.iter_bot_files(LIBRARY / name) if rel != "bot.json"}
    assert installed == shipped


def test_a_default_with_an_initialization_is_imported_with_its_memory_and_stays_ready(tmp_path: Path, library_with_init: Path, hashes: SourceHashes):
    BotStore(tmp_path / "data", seats=3, library_dir=tmp_path / "empty").bootstrap()
    store = BotStore(tmp_path / "data", seats=3, library_dir=library_with_init)
    store.bootstrap()
    seat = store.lineup()[2]  # Carol, a blank bot: no L0 of its own, no initialization
    assert store.summary(seat, sources=hashes)["init"]["status"] == "uninitialized"
    store.import_default(seat, "Leo")
    assert store.name_of(seat) == "Leo" and store.summary(seat, sources=hashes)["init"]["status"] == "ready"
    assert store.read_part(seat, "arch_notes") == NOTES.strip() and store.read_part(seat, "digest", hashes.scenario_id) == DIGEST.strip()
    assert store.read_part(seat, "questions", hashes.scenario_id) == ""  # (Leo's library folder has no open questions)
    assert store.problems(seat, sources=hashes) == [] and store.is_frozen(seat)
    store.import_default(seat, "Kate")  # ... and the next one replaces it as a whole (Kate has open questions, Leo had none)
    assert store.name_of(seat) == "Kate" and store.read_part(seat, "questions", hashes.scenario_id) == QUESTIONS.strip()
