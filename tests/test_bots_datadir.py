"""The bots with the REAL storage layer (what a user's normal launch does), on the isolated data directory.

``tests/test_orchestrator_v14.py`` runs the table through fake stores; here ``startup()`` is the real one: the bots must live in ``<data>/bots``
(made at the first start from the default library, or from a v0.13 ``roster/`` that is there), come back after a restart, and be picked up when a
file is edited by hand.  (These cases replace ``tests/test_roster_datadir.py``, which did the same for the roster of v0.13.)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from game_fakes import async_test

from app.game.orchestrator import GameOrchestrator
from app.storage import botfiles as bf
from app.storage import reset_stores_for_tests
from app.storage.bots import get_bot_store, legacy_persona_to_core
from app.storage.datadir import held_data_dir

NAMES = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]
PERSONA = "【定位】冷静的逻辑派，喜欢用票型说话。\n【说话风格】\n- 句子短。\n【思考方式】\n- 先看票型。"
V0130 = "## 战绩\n场次 2，胜 1，负 1，胜率 50%\n\n## 对局记忆\n第一局：太保守。\n\n## 经验与印象\nBob 爱撒谎。\n"


@pytest.fixture()
def data(isolated_external_storage: Path) -> Path:
    reset_stores_for_tests()
    yield isolated_external_storage
    reset_stores_for_tests()


async def boot() -> GameOrchestrator:
    orch = GameOrchestrator()
    report = await orch.startup()
    assert report["recovery_error"] is None, report
    return orch


async def restart(orch: GameOrchestrator) -> GameOrchestrator:
    await orch.shutdown()
    reset_stores_for_tests()  # a new process: no store singleton survives
    return await boot()


def bot_folders(root: Path) -> list[str]:
    folder = root / "bots"
    return sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []


@async_test
async def test_the_first_game_gets_the_table_of_the_data_directory_and_the_files_live_there(data):
    orch = await boot()
    assert held_data_dir() == data.resolve()  # startup() locked the directory
    store = get_bot_store()
    lineup = store.lineup()
    assert [store.name_of(bot_id) for bot_id in lineup] == NAMES
    assert bot_folders(data) == sorted(lineup) and (data / "bots" / "lineup.json").is_file() and not (data / "roster").exists(), "the roster is gone for good"
    assert all(store.core(bot_id).complete for bot_id in lineup), "the default library's bots have an L0 of their own"
    payload = orch.bots_payload()
    assert payload["lineup"] == lineup and [b["name"] for b in payload["bots"]] == NAMES
    view = orch.public_session()
    assert [p["name"] for p in view["participants"]] == NAMES and [p["bot_id"] for p in view["participants"]] == lineup
    assert view["moderator"]["avatar"] == store.moderator()["avatar"] and all(p["avatar"] for p in view["participants"])
    await orch.shutdown()


@async_test
async def test_names_texts_and_avatars_survive_a_restart_and_the_new_game_uses_them(data):
    orch = await boot()
    store = get_bot_store()
    first, third = store.lineup()[0], store.lineup()[2]
    store.update_bot(third, name="Caroline")
    assert store.write_part(first, "people", "- Eve：上局骗过我，爱悍跳。").ok
    assert store.write_part(first, "arch_notes", "先听完再表态。").ok
    rolled = store.regenerate_moderator_avatar()
    await orch.bots_changed(roster=True)
    files = {p.relative_to(data / "bots" / first).as_posix() for p in (data / "bots" / first).rglob("*") if p.is_file()}
    assert {"bot.json", "core/identity.md", "people.md", "arch/notes.md"} <= files, "plain files, editable by hand"

    orch = await restart(orch)
    store = get_bot_store()
    assert store.name_of(third) == "Caroline" and store.moderator()["avatar"] == rolled["avatar"]
    assert bf.parse_people(store.read_part(first, "people")) == {"Eve": "上局骗过我，爱悍跳。"} and store.read_part(first, "arch_notes") == "先听完再表态。"
    view = orch.public_session()  # the first game of the new process was built from the stored table
    assert view["participants"][2]["name"] == "Caroline" and view["moderator"]["avatar"] == rolled["avatar"]
    assert [p["bot_id"] for p in view["participants"]][:3] == store.lineup()[:3]
    await orch.new_game()  # ... and so does every game after it
    assert orch.public_session()["participants"][2]["name"] == "Caroline"
    await orch.shutdown()


@async_test
async def test_a_hand_edited_bot_file_is_picked_up_at_the_next_start(data):
    orch = await boot()
    store = get_bot_store()
    second = store.lineup()[1]
    await orch.shutdown()
    reset_stores_for_tests()
    meta_path = data / "bots" / second / "bot.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["name"] = "Bobby"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (data / "bots" / second / "core" / "voice.md").write_text("手工写的说话方式。\n", encoding="utf-8")
    orch = await boot()
    store = get_bot_store()
    assert store.name_of(second) == "Bobby" and store.core(second).voice == "手工写的说话方式。"
    await orch.new_game()
    assert orch.session.participants[1].name == "Bobby"
    await orch.shutdown()


@async_test
async def test_a_v013_roster_is_migrated_at_the_first_start_and_its_files_are_left_alone(data):
    """H45 / H59 / G41: the roster of v0.13 becomes the first table of bots; reading it never rewrites a file of it."""
    roster = data / "roster"
    roster.mkdir(parents=True)
    names = {"N1": {"name": "Alice"}, "N2": {"name": "Bobby"}, "N3": {"name": "Carol"}}
    (roster / "roster.json").write_text(json.dumps({"memory_enabled": True, "seats": names}, ensure_ascii=False), encoding="utf-8")
    (roster / "N2.persona.md").write_text(PERSONA, encoding="utf-8")
    (roster / "N2.memory.md").write_text(V0130, encoding="utf-8")
    before = {p.name: p.read_bytes() for p in sorted(roster.iterdir())}

    orch = await boot()
    store = get_bot_store()
    lineup = store.lineup()
    assert [store.name_of(bot_id) for bot_id in lineup][:3] == ["Alice", "Bobby", "Carol"] and len(lineup) == 12
    bobby = lineup[1]
    assert store.core(bobby) == legacy_persona_to_core(PERSONA), "the persona became the L0 of the bot"
    record = bf.read_record(store.bot_dir(bobby))
    assert (record.games, record.wins) == (2, 1), "the record is kept"
    assert store.read_part(bobby, "people") == "", "what v0.13.0 wrote as free text is not migrated"
    assert store.core(lineup[0]).complete, "H59: a bot with the name of a default bot and no persona got that bot's L0"
    assert {p.name: p.read_bytes() for p in sorted(roster.iterdir())} == before, "the old roster files are not touched"
    assert [p["name"] for p in orch.public_session()["participants"]][:3] == ["Alice", "Bobby", "Carol"]
    await orch.new_game()
    assert orch.session.participants[1].name == "Bobby" and orch.session.participants[1].bot_id == bobby
    await orch.shutdown()

    orch = await restart(orch)  # (the second start: nothing is migrated again, nothing changes)
    assert get_bot_store().lineup() == lineup and {p.name: p.read_bytes() for p in sorted(roster.iterdir())} == before
    await orch.shutdown()
