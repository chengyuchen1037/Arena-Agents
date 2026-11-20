"""Guard: the default bot library must never ship an initialization that is out of date (v0.14, H44).

The twelve default bots in ``bots/<name>/`` may carry the result of a real initialization (``tools/init_default_bots.py --write-library``): their
operating notes and rules memory and an ``init`` record in ``bot.json`` that says which L0, which official contract, which scenario and which study
materials it was made from (four hashes).  An installation that starts with such a library shows those bots ready (✔) - exactly as long as the hashes still
match.  So the day somebody edits ``prompts/contract.md``, the default scenario or the L0 of a library bot and does not run the tool again, the shipped bots
would silently show "needs re-initialization" (⚠) on every fresh installation: THIS test goes red instead, and says what to run.

Today the library has no initialized bot, so the check on the shipped files passes trivially; what keeps it honest is that the same function is tried here on
made-up libraries - right hashes (green), every kind of wrong one (red) - and on a library made with the very code the tool uses (the stores of a fresh data
directory hash the contract and the scenario, the bot store hashes the L0), changed one source at a time.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from tools_fakes import tree  # (also puts tools/ on the path)

import init_default_bots
from app.bots.types import CoreFiles, SourceHashes, core_hash, sha256_text
from app.storage import botfiles

ROOT = Path(__file__).resolve().parents[1]
FIX = "请重新运行 tools/init_default_bots.py"
SCENARIO_ID = "demo_scenario"
CURRENT = SourceHashes(contract=sha256_text("the contract"), scenario=sha256_text("the scenario"), materials="", scenario_id=SCENARIO_ID)


# ============================================================================================================ the real files
def test_the_shipped_library_has_no_initialized_bot_that_is_out_of_date() -> None:
    """Every ``ready`` bot of ``bots/`` must match the L0, the contract and the default scenario of this checkout (a fresh installation's hashes)."""
    hashes = init_default_bots.fresh_install_hashes()
    assert hashes.contract and hashes.scenario and hashes.scenario_id  # (the contract and the default scenario were found and hashed)
    problems = init_default_bots.library_problems(ROOT / "bots", hashes)
    assert not problems, "\n".join(problems) + "\n（契约、默认剧本或某个 bot 的底层设定改过之后，库里已初始化的 bot 要重新初始化：运行 tools/init_default_bots.py --write-library，审阅 git diff bots/ 再提交。）"


def test_the_hashes_of_a_fresh_installation_are_never_made_in_the_real_data_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """``fresh_install_hashes()`` makes the stores of the data directory it runs in: it refuses to run in the repository's own ``data/``."""
    import app.storage.paths as paths

    monkeypatch.setattr(paths, "get_data_dir", lambda: ROOT / "data")  # (only what the function is told: nothing here may ever reach the real directory)
    with pytest.raises(RuntimeError, match="scratch ARENA_DATA_DIR, never data/"):
        init_default_bots.fresh_install_hashes()


def test_the_library_is_what_the_tool_looks_at_by_default() -> None:
    assert init_default_bots.LIBRARY == ROOT / "bots" and (ROOT / "bots" / "Alice" / "bot.json").is_file()


# ============================================================================================================== made-up data
def make_bot(library: Path, name: str, *, init: dict[str, Any] | None = None, memory: bool = True, hashes: SourceHashes = CURRENT) -> Path:
    """A bot folder of a library: ``bot.json``, ``core/``, and - when it is initialized - its memory.  ``init`` overrides fields of the record."""
    folder = library / name
    core = CoreFiles(identity=f"你是{name}。", voice="说话平和。「我先听听。」\n篇幅：20–80 字", mind="先看事实，再下判断。")
    botfiles.write_core(folder / "core", core)
    record: dict[str, Any] = {
        "status": "ready",
        "at": 1_700_000_000,
        "model": "a-model",
        "duration_s": 12.5,
        "error": None,
        "scenario_id": hashes.scenario_id,
        "hashes": {"core": core_hash(botfiles.read_core(folder / "core")), "contract": hashes.contract, "scenario": hashes.scenario, "materials": hashes.materials},
    }
    record.update(init or {})
    (folder / "bot.json").write_text(json.dumps({"format": 1, "name": name, "avatar": None, "init": record}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if memory:
        (folder / "arch").mkdir(parents=True)
        (folder / "arch" / "notes.md").write_text("我读过平台说明。\n", encoding="utf-8")
        rules = folder / "rules" / str(record["scenario_id"])
        rules.mkdir(parents=True)
        (rules / "digest.md").write_text("我读过规则。\n", encoding="utf-8")
        (rules / "playbook.md").write_text("## 假设\n- [把握:低|支持:0|反例:0|来源:初始化] 先看事实。\n", encoding="utf-8")
    return folder


def test_a_library_whose_initialized_bots_match_is_green_and_so_is_one_without_any(tmp_path: Path) -> None:
    library = tmp_path / "bots"
    make_bot(library, "Alice")
    make_bot(library, "Bob", memory=False, init={"status": "uninitialized", "hashes": None, "scenario_id": None, "model": None, "at": None, "duration_s": None})
    make_bot(library, "Carol", memory=False, init={"status": "failed", "error": "超时", "hashes": None})
    assert init_default_bots.library_problems(library, CURRENT) == []  # (a bot that is not initialized is not looked at)
    empty = tmp_path / "empty"
    make_bot(empty, "Dan", memory=False, init={"status": "uninitialized", "hashes": None})
    assert init_default_bots.library_problems(empty, CURRENT) == []
    assert init_default_bots.library_problems(tmp_path / "no-such-library", CURRENT) == []


@pytest.mark.parametrize(
    "what, change, label",
    [
        ("the contract", {"hashes": {"contract": "0" * 64}}, "官方契约"),
        ("the scenario", {"hashes": {"scenario": "0" * 64}}, "剧本"),
        ("the study materials", {"hashes": {"materials": "f" * 64}}, "学习材料"),
        ("the L0", {"hashes": {"core": "1" * 64}}, "底层设定"),
        ("the scenario it was made for", {"scenario_id": "another_scenario"}, "剧本"),
    ],
)
def test_a_wrong_hash_is_red_and_says_which_source_changed_and_what_to_run(tmp_path: Path, what: str, change: dict[str, Any], label: str) -> None:
    library = tmp_path / "bots"
    folder = make_bot(library, "Alice")
    record = json.loads((folder / "bot.json").read_text(encoding="utf-8"))
    for key, value in change.items():
        if isinstance(value, dict):
            record["init"][key] = {**record["init"][key], **value}
        else:
            record["init"][key] = value
    (folder / "bot.json").write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    problems = init_default_bots.library_problems(library, CURRENT)
    assert len(problems) == 1, (what, problems)
    assert problems[0].startswith("bots/Alice 的初始化已经过期（") and label in problems[0] and FIX in problems[0], what


def test_an_edit_of_the_l0_after_the_initialization_is_red(tmp_path: Path) -> None:
    library = tmp_path / "bots"
    make_bot(library, "Alice")
    make_bot(library, "Bob")
    identity = library / "Alice" / "core" / "identity.md"
    identity.write_text(identity.read_text(encoding="utf-8").rstrip("\n") + "又加了一句。\n", encoding="utf-8", newline="\n")
    problems = init_default_bots.library_problems(library, CURRENT)
    assert len(problems) == 1 and "bots/Alice" in problems[0] and "底层设定" in problems[0]  # (only the bot that was edited)


def test_several_changed_sources_are_all_named(tmp_path: Path) -> None:
    library = tmp_path / "bots"
    make_bot(library, "Alice")
    changed = SourceHashes(contract=sha256_text("a new contract"), scenario=sha256_text("a new scenario"), materials="", scenario_id=SCENARIO_ID)
    (problem,) = init_default_bots.library_problems(library, changed)
    assert "官方契约、剧本在初始化之后改过" in problem and FIX in problem  # (in the order the start check names them)


def test_a_ready_record_without_its_memory_files_is_red(tmp_path: Path) -> None:
    library = tmp_path / "bots"
    make_bot(library, "Alice", memory=False)
    (problem,) = init_default_bots.library_problems(library, CURRENT)
    assert problem.startswith("bots/Alice 的初始化记录是“已就绪”，但缺少它的记忆文件：") and "arch/notes.md" in problem
    assert f"rules/{SCENARIO_ID}/digest.md" in problem and f"rules/{SCENARIO_ID}/playbook.md" in problem and FIX in problem


def test_a_bot_json_that_cannot_be_read_is_a_problem_too(tmp_path: Path) -> None:
    library = tmp_path / "bots"
    folder = make_bot(library, "Alice")
    (folder / "bot.json").write_text("{not json", encoding="utf-8")
    (problem,) = init_default_bots.library_problems(library, CURRENT)
    assert problem.startswith("bots/Alice/bot.json 读不出来")


# ============================================================= the real pipeline, changed one source at a time
def test_a_library_made_by_the_code_of_the_tool_goes_red_when_the_contract_the_scenario_or_the_l0_change_and_green_again_when_they_are_put_back(
    tmp_path: Path,
) -> None:
    """No model: one bot of a copy of the shipped library is initialized by hand the way the tool's run leaves it (the bot store of a fresh data
    directory, ``commit_init`` with the hashes the application takes, ``copy_bot_to_library``); then each source is changed in turn - the contract
    in the contract store, the scenario in the rules store, the L0 in the library - and put back."""
    from app.config import load_config
    from app.game.sources import Sources
    from app.storage.bots import BotStore
    from app.storage.contract import get_contract_store
    from app.storage.rules import get_rules_store

    shipped = tree(ROOT / "bots")
    library = tmp_path / "library" / "bots"
    shutil.copytree(ROOT / "bots", library)
    sources = Sources(load_config, lambda: True)
    hashes = sources.hashes()
    store = BotStore(tmp_path / "scratch-data", seats=12, library_dir=library)
    store.bootstrap()
    bot_id = next(b for b in store.lineup() if store.name_of(b) == "Alice")
    files = {"arch_notes": "我读过平台说明。", "digest": "我读过规则。", "playbook": "- [把握:低|支持:0|反例:0|来源:初始化|#发言] 先看事实。"}
    store.commit_init(bot_id, hashes.scenario_id, files, model="a-model", duration_s=3.0, sources=hashes)
    init_default_bots.copy_bot_to_library(store.bot_dir(bot_id), library / "Alice", store.init_record(bot_id), "a-model")
    assert init_default_bots.library_problems(library, sources.hashes()) == []  # (green: a fresh installation finds Alice ready)

    contract = get_contract_store()
    original = contract.current().markdown
    contract.set(original + "\n（补充的一句。）\n")
    (problem,) = init_default_bots.library_problems(library, sources.hashes())
    assert "官方契约" in problem and FIX in problem and "bots/Alice" in problem
    contract.set(original)  # put back: green again
    assert init_default_bots.library_problems(library, sources.hashes()) == []

    rules = get_rules_store()
    text = rules.scenario_text(hashes.scenario_id)
    rules.set_scenario(hashes.scenario_id, text.replace("title: 12 人标准局狼人杀（暗牌局）", "title: 12 人标准局狼人杀（暗牌局，改）", 1))
    (problem,) = init_default_bots.library_problems(library, sources.hashes())
    assert "剧本" in problem and FIX in problem
    rules.set_scenario(hashes.scenario_id, text)
    assert init_default_bots.library_problems(library, sources.hashes()) == []

    identity = library / "Alice" / "core" / "identity.md"
    old = identity.read_text(encoding="utf-8")
    identity.write_text(old.rstrip("\n") + "又加了一句。\n", encoding="utf-8", newline="\n")
    (problem,) = init_default_bots.library_problems(library, sources.hashes())
    assert "底层设定" in problem and FIX in problem
    identity.write_text(old, encoding="utf-8", newline="\n")
    assert init_default_bots.library_problems(library, sources.hashes()) == []
    assert tree(ROOT / "bots") == shipped  # (the shipped library itself was not touched by any of it)
