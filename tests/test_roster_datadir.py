"""The roster with the REAL storage layer (what a user's normal launch does), on the isolated data directory.

``tests/test_roster_runtime.py`` runs the roster through in-memory stores; here ``startup()`` is the real one, so the
roster must live in ``<data>/roster``, come back after a restart and be picked up when it is edited by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.game.orchestrator import GameOrchestrator
from app.storage import reset_stores_for_tests
from app.storage.datadir import held_data_dir
from game_fakes import async_test

NAMES = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]


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


def roster_files(root: Path) -> list[str]:
    folder = root / "roster"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


@async_test
async def test_the_first_game_gets_the_roster_of_the_data_directory_and_the_files_live_there(data):
    orch = await boot()
    assert held_data_dir() == data.resolve()  # startup() locked the directory
    payload = orch.roster_payload()
    assert [s["name"] for s in payload["seats"]] == NAMES and payload["editable"] is True
    assert all(s["avatar"] for s in payload["seats"]) and payload["moderator"]["avatar"]
    assert roster_files(data) == ["roster.json"]
    view = orch.public_session()
    assert [p["name"] for p in view["participants"]] == NAMES
    assert view["moderator"]["avatar"] == payload["moderator"]["avatar"]
    assert [p["avatar"] for p in view["participants"]] == [s["avatar"] for s in payload["seats"]]
    await orch.shutdown()


@async_test
async def test_names_texts_and_avatars_survive_a_restart_and_the_new_game_uses_them(data):
    orch = await boot()
    await orch.update_roster_entry("N3", name="Caroline")
    await orch.update_roster_entry("N1", persona="冷静的分析派", memory_parts={"impressions": {"N5": "上局骗过我，爱悍跳。"}})
    rolled = await orch.regenerate_avatars("moderator")
    before = orch.roster_payload()
    assert sorted(roster_files(data)) == ["N1.memory.md", "N1.persona.md", "roster.json"]  # plain files, editable by hand

    orch = await restart(orch)
    after = orch.roster_payload()
    assert after["seats"] == before["seats"] and after["moderator"] == before["moderator"] == {"id": "moderator", "name": "主持人", "avatar": rolled["avatar"]}
    view = orch.public_session()  # the first game of the new process was built from the stored roster
    assert view["participants"][2]["name"] == "Caroline" and view["moderator"]["avatar"] == rolled["avatar"]
    assert view["participants"][0]["avatar"] == before["seats"][0]["avatar"]
    assert orch.session.participants[0].persona == "冷静的分析派" and "- N5 Eve：上局骗过我，爱悍跳。" in orch.session.participants[0].memory
    assert orch.session.memory_enabled is True
    await orch.new_game()  # ... and so does every game after it
    assert orch.public_session()["participants"][2]["name"] == "Caroline"
    await orch.shutdown()


@async_test
async def test_a_v0130_memory_file_is_read_migrated_and_left_alone_until_the_next_save(data):
    """G41: the file of the old format is not rewritten by reading it (not at boot, not by the panel, not by a new game)."""
    orch = await boot()
    await orch.shutdown()
    reset_stores_for_tests()
    folder = data / "roster"
    old = "## 战绩\n场次 2，胜 1，负 1，胜率 50%\n\n## 对局记忆\n第一局：太保守。\n\n## 经验与印象\nBob 爱撒谎。\n"
    (folder / "N2.memory.md").write_text(old, encoding="utf-8")
    orch = await boot()
    entry = orch.roster_payload()["seats"][1]
    assert entry["memory_parts"]["record"] == "场次 2，胜 1，负 1，胜率 50%"
    assert entry["memory_parts"]["legacy"] == "第一局：太保守。\n\nBob 爱撒谎。" and not any(entry["memory_parts"]["impressions"].values())
    assert (folder / "N2.memory.md").read_text(encoding="utf-8") == old
    await orch.new_game()
    memory = orch.session.participants[1].memory  # the agent reads the old notes too, until the next review tidies them up
    assert "场次 2，胜 1" in memory and "Bob 爱撒谎。" in memory and "## 旧记忆（待整理）" in memory
    assert (folder / "N2.memory.md").read_text(encoding="utf-8") == old
    await orch.update_roster_entry("N2", memory_parts={"experience": ["先听后说"]})  # the first save writes the new format
    new = (folder / "N2.memory.md").read_text(encoding="utf-8")
    assert "## 对局记忆" not in new and "## 经验与印象" not in new and "- 先听后说" in new
    assert "## 战绩\n场次 2，胜 1，负 1，胜率 50%\n\n## 印象\n- N1 Alice：（暂无）" in new and "## 旧记忆（待整理）\n第一局：太保守。\n\nBob 爱撒谎。" in new
    await orch.shutdown()


@async_test
async def test_a_hand_edited_roster_file_is_picked_up_at_the_next_start(data):
    orch = await boot()
    await orch.shutdown()
    reset_stores_for_tests()
    folder = data / "roster"
    (folder / "N2.persona.md").write_text("手工写的设定\n", encoding="utf-8")
    raw = json.loads((folder / "roster.json").read_text(encoding="utf-8"))
    raw["seats"]["N2"] = {**raw["seats"].get("N2", {}), "name": "Bobby"}
    (folder / "roster.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    orch = await boot()
    assert orch.roster_payload()["seats"][1]["name"] == "Bobby"  # the panel reads the files
    # (the restored idle game keeps what it was saved with until the next new game / the start: see below)
    await orch.new_game()
    assert orch.session.participants[1].name == "Bobby" and orch.session.participants[1].persona == "手工写的设定"
    await orch.shutdown()
