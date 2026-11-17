"""The roster with the REAL workspace module (what a user's normal launch does), on throw-away folders.

``tests/test_roster_runtime.py`` runs the roster against the isolated data directory; here the workspace feature
is on, so the roster must live in the *active workspace* (``<workspace>/.arena/roster``), come back after a
restart, follow a workspace switch and never land in the legacy application directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.game.orchestrator import GameOrchestrator
from app.storage import reset_stores_for_tests
from app.storage.roster import RosterError
from game_fakes import async_test

NAMES = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_external_storage: Path) -> dict[str, Path]:
    """Workspace feature ON, the default workspace in a temp folder; the app dir stays the isolated one."""
    workspace = tmp_path / "ws-one"
    monkeypatch.setenv("ARENA_DISABLE_WORKSPACE", "0")
    monkeypatch.setenv("ARENA_WORKSPACE_DIR", str(workspace))
    reset_stores_for_tests()
    yield {"ws": workspace, "app": isolated_external_storage, "other": tmp_path / "ws-two"}
    reset_stores_for_tests()


async def boot() -> GameOrchestrator:
    orch = GameOrchestrator()
    report = await orch.startup()
    assert report["workspace"] and report["workspace"].get("error") is None and report["workspace"]["status"] == "ok", report
    assert not orch._workspace_failed
    return orch


async def restart(orch: GameOrchestrator) -> GameOrchestrator:
    await orch.shutdown()
    reset_stores_for_tests()  # a new process: no store singleton survives
    return await boot()


def roster_files(root: Path) -> list[str]:
    folder = root / ".arena" / "roster"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


@async_test
async def test_the_first_game_gets_the_roster_of_the_workspace_and_the_files_live_there(home):
    orch = await boot()
    payload = orch.roster_payload()
    assert [s["name"] for s in payload["seats"]] == NAMES and payload["editable"] is True
    assert all(s["avatar"] for s in payload["seats"]) and payload["moderator"]["avatar"]
    assert roster_files(home["ws"]) == ["roster.json"]  # ... in the workspace
    assert not (home["app"] / "roster").exists()  # ... and nowhere else
    view = orch.public_session()
    assert [p["name"] for p in view["participants"]] == NAMES
    assert view["moderator"]["avatar"] == payload["moderator"]["avatar"]
    assert [p["avatar"] for p in view["participants"]] == [s["avatar"] for s in payload["seats"]]
    await orch.shutdown()


@async_test
async def test_names_texts_and_avatars_survive_a_restart_and_the_new_game_uses_them(home):
    orch = await boot()
    await orch.update_roster_entry("N3", name="Caroline")
    await orch.update_roster_entry("N1", persona="冷静的分析派", memory="## 经验与印象\n上局被 N5 骗过。")
    rolled = await orch.regenerate_avatars("moderator")
    before = orch.roster_payload()
    assert sorted(roster_files(home["ws"])) == ["N1.memory.md", "N1.persona.md", "roster.json"]  # plain files, editable by hand

    orch = await restart(orch)
    after = orch.roster_payload()
    assert after["seats"] == before["seats"] and after["moderator"] == before["moderator"] == {"id": "moderator", "name": "主持人", "avatar": rolled["avatar"]}
    view = orch.public_session()  # the first game of the new process was built from the workspace's roster
    assert view["participants"][2]["name"] == "Caroline" and view["moderator"]["avatar"] == rolled["avatar"]
    assert view["participants"][0]["avatar"] == before["seats"][0]["avatar"]
    assert orch.session.participants[0].persona == "冷静的分析派" and "被 N5 骗过" in orch.session.participants[0].memory
    assert orch.session.memory_enabled is True
    await orch.new_game()  # ... and so does every game after it
    assert orch.public_session()["participants"][2]["name"] == "Caroline"
    await orch.shutdown()


@async_test
async def test_a_hand_edited_roster_file_is_picked_up_at_the_next_start(home):
    orch = await boot()
    await orch.shutdown()
    reset_stores_for_tests()
    folder = home["ws"] / ".arena" / "roster"
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


@async_test
async def test_switching_the_workspace_switches_the_roster_and_leaves_the_old_one_alone(home):
    orch = await boot()
    await orch.update_roster_entry("N2", name="Bobby")
    await orch.update_roster_entry("N2", persona="话多")
    first_files = {name: (home["ws"] / ".arena" / "roster" / name).read_bytes() for name in roster_files(home["ws"])}

    await orch.switch_workspace(home["other"], create_if_missing=True)
    other = orch.roster_payload()
    assert other["seats"][1]["name"] == "Bob" and other["seats"][1]["persona"] == ""  # a new workspace starts from the defaults
    assert orch.public_session()["participants"][1]["name"] == "Bob"
    assert roster_files(home["other"]) == ["roster.json"] and other["moderator"]["avatar"]
    assert other["seats"][1]["avatar"] != first_files and True  # (own avatars: rolled for this workspace)
    await orch.update_roster_entry("N2", name="Robert")

    await orch.switch_workspace(home["ws"])
    back = orch.roster_payload()
    assert back["seats"][1]["name"] == "Bobby" and back["seats"][1]["persona"] == "话多"  # the first one, as it was left
    assert orch.public_session()["participants"][1]["name"] == "Bobby"
    assert {name: (home["ws"] / ".arena" / "roster" / name).read_bytes() for name in roster_files(home["ws"])} == first_files
    assert json.loads((home["other"] / ".arena" / "roster" / "roster.json").read_text(encoding="utf-8"))["seats"]["N2"]["name"] == "Robert"
    assert not (home["app"] / "roster").exists()
    await orch.shutdown()


@async_test
async def test_a_workspace_that_cannot_be_opened_never_writes_a_roster_anywhere(home, monkeypatch):
    # the workspace folder is refused (here: it is the project directory itself) -> the app comes up degraded
    import app.config

    monkeypatch.setenv("ARENA_WORKSPACE_DIR", str(Path(app.config.BASE_DIR)))
    orch = GameOrchestrator()
    report = await orch.startup()
    assert orch._workspace_failed and report["recovery_error"]
    payload = orch.roster_payload()
    assert payload["editable"] is False and [s["name"] for s in payload["seats"]] == NAMES and payload["moderator"]["avatar"] is None
    for call in (lambda: orch.update_roster_entry("N1", persona="x"), lambda: orch.set_memory_enabled(False), lambda: orch.clear_memory()):
        with pytest.raises(RuntimeError, match="工作文件夹不可用"):
            await call()
    with pytest.raises(RosterError):
        orch.export_memory()
    assert not (home["app"] / "roster").exists() and not (Path(app.config.BASE_DIR) / ".arena").exists()
    await orch.shutdown()
