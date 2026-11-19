"""v0.12 engine contract: automatic encrypted saves, restore as paused, startup, shutdown (D3)."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest

from app.game.orchestrator import SessionLoadError
from app.game.session import SESSION_SCHEMA_VERSION, GameSession
from app.providers.models import ModelBinding, StorageError
from app.storage.datadir import held_data_dir, release_data_dir
from app.world.types import MODERATOR_ID, UiEvent
from game_fakes import BINDING, async_test, bots_for, idle_moderator, real_orch


def saved_payload(monkeypatch: pytest.MonkeyPatch, *, status: str = "paused") -> dict[str, Any]:
    """A real v3 save of a (started and stopped) game, as the store would hold it."""
    orch = real_orch(monkeypatch, defaults={"p01": BINDING})
    payload = orch.session_payload()
    payload["status"] = status
    payload["world"]["state"]["status"] = status
    payload["model_bindings"] = {"p01": BINDING.model_dump(mode="json")}
    return payload


# ------------------------------------------------------------------- persist_session ---


@async_test
async def test_persist_saves_a_full_json_snapshot_and_then_moves_the_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch.session.model_bindings = {"p01": BINDING}
    session = orch.session

    assert await orch.persist_session("manual") is True

    saved = orch.game_store.data[session.session_id]
    assert saved["schema_version"] == SESSION_SCHEMA_VERSION == 5
    assert saved["world"]["ledger"] and saved["world"]["state"]["players"]  # the whole game, dealt
    assert saved["scenario_spec"]["id"] == "minimal_test"
    assert saved["model_bindings"]["p01"]["model_id"] == "big"
    assert "api_key" not in str(saved) and "example.invalid" not in str(saved)
    assert orch.settings_store.last_id == session.session_id


@async_test
async def test_failed_manual_save_raises_and_marks_the_session_but_never_loses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch.game_store.fail_save = "磁盘不可写"
    events: list[str] = []

    async def record(event: str = "x") -> None:
        events.append(event)

    orch.broadcast_state = record  # type: ignore[method-assign]
    session = orch.session

    with pytest.raises(StorageError):
        await orch.persist_session("manual")
    assert "磁盘不可写" in session.autosave_error and "manual" in session.autosave_error
    assert events == ["autosave_error"]
    assert orch.settings_store.last_id is None  # pointer not moved on failure
    assert orch.session is session

    assert await orch.persist_session("progress", raise_errors=False) is False  # autosave: no raise
    orch.game_store.fail_save = None
    assert await orch.persist_session("manual") is True
    assert session.autosave_error == ""  # cleared by the next success


@async_test
async def test_slow_save_never_points_last_session_at_a_replaced_game(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    old = orch.session
    other = GameSession()

    def swap_during_save(session_id: str, payload: dict[str, Any]) -> None:
        orch._session_generation += 1  # the game is replaced while the write is in flight
        orch.session = other

    orch.game_store.on_save = swap_during_save
    assert await orch.persist_session("progress", raise_errors=False) is True
    assert old.session_id in orch.game_store.data  # its own file is fine...
    assert orch.settings_store.last_id is None  # ...but the pointer was not moved to a stale game


# ----------------------------------------------------------------------- autosave ---


@async_test
async def test_autosave_is_debounced_and_only_triggered_by_state_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch._autosave_enabled = True
    for _ in range(50):  # status / metrics chatter never saves
        orch._publish(UiEvent(type="agent.status", data={"id": "p01", "state": "thinking"}))
        orch._publish(UiEvent(type="agent.metrics", data={"id": "p01"}))
        orch._publish(UiEvent(type="request.progress", data={}))
    await asyncio.sleep(0.1)
    assert orch.game_store.saves == []

    for index in range(30):
        orch.world.state.phase = f"step {index}"
        orch._publish(UiEvent(type="phase.changed", data={"text": f"step {index}"}))
    await asyncio.sleep(0.15)

    assert 1 <= len(orch.game_store.saves) <= 2  # coalesced, not 30 writes
    saved = orch.game_store.data[orch.session.session_id]
    assert saved["world"]["state"]["phase"] == "step 29"  # the trailing save has the latest state


@async_test
async def test_autosave_does_nothing_until_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch._publish(UiEvent(type="game.status", data={}))
    await asyncio.sleep(0.05)
    assert orch.game_store.saves == []


@async_test
async def test_flush_writes_now_and_cancels_the_pending_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch._autosave_enabled = True
    orch._autosave_delay = 30
    orch._publish(UiEvent(type="game.status", data={}))
    task = orch._autosave_task
    assert task is not None and not task.done() and orch.game_store.saves == []

    await orch.flush_autosave("test")

    assert task.done() and len(orch.game_store.saves) == 1


@async_test
async def test_autosave_failure_is_reported_not_raised_into_the_game(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch._autosave_enabled = True
    orch.game_store.fail_save = "locked"
    orch._publish(UiEvent(type="message.committed", data={}))  # must not raise
    await asyncio.sleep(0.1)
    assert "locked" in orch.session.autosave_error
    assert orch.session.status == "idle"


# --------------------------------------------------------------------- shutdown ---


@async_test
async def test_shutdown_pauses_flushes_the_save_then_closes_the_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    bots_for(orch, idle_moderator)
    orch._autosave_enabled = True
    await orch.start_or_resume()
    assert orch.session.status == "running" and orch._supervisor is not None
    order: list[str] = []
    orch.game_store.on_save = lambda sid, payload: order.append(f"save:{payload['status']}")

    class FakeLLM:
        closed = False

        async def aclose(self) -> None:
            order.append("llm-closed")
            self.closed = True

    orch.llm = FakeLLM()
    session = orch.session

    await orch.shutdown()

    assert orch._supervisor is None and not orch.world.running
    assert "save:paused" in order and order[-1] == "llm-closed"
    assert order.index("save:paused") < order.index("llm-closed")  # saved as paused, before closing
    assert orch.llm.closed and session.status == "paused"
    assert orch.settings_store.last_id == session.session_id
    count = len([o for o in order if o.startswith("save:")])
    orch._publish(UiEvent(type="game.status", data={}))  # after shutdown nothing is scheduled
    await asyncio.sleep(0.05)
    assert len(orch.game_store.saves) == count
    await orch.shutdown()  # idempotent


@async_test
async def test_shutdown_of_an_idle_game_does_not_force_it_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch._autosave_enabled = True
    await orch.shutdown()
    assert orch.session.status == "idle"
    assert orch.game_store.data[orch.session.session_id]["status"] == "idle"


# ------------------------------------------------------------------------ restore ---


@async_test
async def test_last_game_is_restored_paused_never_running(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = saved_payload(monkeypatch, status="running")
    orch = real_orch(monkeypatch)
    orch.game_store.data[payload["session_id"]] = payload
    orch.settings_store.last_id = payload["session_id"]
    generation = orch._session_generation

    restored = await orch._restore_last_session()

    assert restored == payload["session_id"]
    assert orch.session.session_id == payload["session_id"]
    assert orch.session.status == "paused" and orch._supervisor is None and not orch.world.running
    assert orch.world.state.status == "paused" and orch.clock.paused
    assert orch.session.restored_from_save is True and orch.restored_from_save is True
    assert orch.session.restore_notice == "已恢复上次对局（已暂停）"
    assert orch._session_generation == generation + 1
    # moderator and p02..p04 have no binding: reported, never silently filled with another provider
    assert {i.actor_id for i in orch.session.binding_issues} == {MODERATOR_ID, "p02", "p03", "p04"}
    assert not orch._recovery_pending


@async_test
async def test_restore_with_vanished_provider_reports_issues_without_rebinding(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = saved_payload(monkeypatch)
    payload["model_bindings"] = {a: BINDING.model_dump(mode="json") for a in (MODERATOR_ID, "p01", "p03", "p04")}
    payload["model_bindings"]["p02"] = ModelBinding(provider_id="gone", model_id="x").model_dump(mode="json")
    orch = real_orch(monkeypatch)
    orch.game_store.data[payload["session_id"]] = payload
    orch.settings_store.last_id = payload["session_id"]

    await orch._restore_last_session()

    assert orch.session.model_bindings["p02"].provider_id == "gone"  # untouched
    issues = {i.actor_id: i.message for i in orch.session.binding_issues}
    assert set(issues) == {"p02"} and "不存在" in issues["p02"]


@async_test
async def test_no_pointer_means_a_fresh_game_and_no_recovery_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    assert await orch._restore_last_session() is None
    assert not orch._recovery_pending and not orch.session.restored_from_save


@pytest.mark.parametrize("failure", ["unreadable", "invalid"])
@async_test
async def test_unrecoverable_last_game_keeps_files_and_suppresses_autosave(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch = real_orch(monkeypatch)
    fresh = orch.session
    orch._autosave_enabled = True
    orch.settings_store.last_id = "broken-game"
    if failure == "invalid":
        orch.game_store.data["broken-game"] = {"schema_version": SESSION_SCHEMA_VERSION, "session_id": "broken-game", "participants": "nope"}
    else:
        def unreadable(session_id: str) -> dict[str, Any]:
            raise StorageError("对局文件和它的上一份副本都无法读取。")

        orch.game_store.load = unreadable  # the files are there but cannot be read

    assert await orch._restore_last_session() is None

    assert orch.session is fresh and orch._recovery_pending
    assert "已保留" in fresh.restore_notice and "broken-game" in fresh.restore_notice
    assert orch.recovery_error

    orch.schedule_autosave("status_change")
    await orch.flush_autosave()
    await asyncio.sleep(0.05)
    assert orch.game_store.saves == []  # the placeholder never overwrites anything
    assert orch.settings_store.last_id == "broken-game"  # pointer untouched
    if failure == "invalid":
        assert orch.game_store.data["broken-game"]["participants"] == "nope"


@async_test
async def test_a_last_game_whose_file_is_simply_gone_gives_a_fresh_game_without_recovery_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleted by hand, or left out of an imported archive: nothing is there to protect, so autosave stays on."""
    orch = real_orch(monkeypatch)
    fresh = orch.session
    orch._autosave_enabled = True
    orch.settings_store.last_id = "vanished"

    assert await orch._restore_last_session() is None

    assert orch.session is fresh and not orch._recovery_pending and not orch.recovery_error
    assert "已经不存在" in fresh.restore_notice
    await orch.persist_session("progress")
    assert orch.settings_store.last_id == fresh.session_id  # the pointer moves on to the new game


@async_test
async def test_an_older_engine_save_is_kept_untouched_and_a_fresh_game_is_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    """D3: a v0.11 save is not a failure state — no recovery mode, the user is told, autosave works."""
    orch = real_orch(monkeypatch)
    orch._autosave_enabled = True
    old = {"session_id": "oldsave1", "schema_version": 2, "status": "paused", "records": [{"id": "rec_0001"}]}
    orch.game_store.data["oldsave1"] = copy.deepcopy(old)
    orch.settings_store.last_id = "oldsave1"
    fresh = orch.session

    assert await orch._restore_last_session() is None

    assert orch.session is fresh and not orch._recovery_pending
    assert "旧版本" in fresh.restore_notice and "不兼容" in fresh.restore_notice
    assert orch.game_store.data["oldsave1"] == old  # never modified
    await orch.persist_session("progress")  # saving the new game is fine; the pointer moves to it
    assert orch.settings_store.last_id == fresh.session_id and orch.game_store.data["oldsave1"] == old


@async_test
async def test_explicit_new_game_leaves_recovery_mode_and_saves_again(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch, defaults={"p01": BINDING})
    orch._autosave_enabled = True
    orch._recovery_pending = True
    orch.settings_store.last_id = "broken-game"

    game = await orch.new_game()
    await asyncio.sleep(0.1)

    assert not orch._recovery_pending
    assert game.session_id in orch.game_store.data
    assert orch.settings_store.last_id == game.session_id
    assert game.restored_from_save is False and "p01" in game.model_bindings  # default binding


# ------------------------------------------------------------------------ startup ---


@async_test
async def test_startup_locks_the_data_directory_restores_the_last_game_paused_and_enables_autosave(
    monkeypatch: pytest.MonkeyPatch, isolated_external_storage: Path
) -> None:
    payload = saved_payload(monkeypatch, status="running")
    payload["world"]["state"]["phase"] = "restored!"
    orch = real_orch(monkeypatch, defaults={"p01": BINDING})
    orch.game_store.data[payload["session_id"]] = payload
    orch.settings_store.last_id = payload["session_id"]
    assert not orch._autosave_enabled and held_data_dir() is None

    report = await orch.startup()

    assert held_data_dir() == isolated_external_storage.resolve()  # (the first thing startup does)
    assert set(report) == {"restored", "recovery_error"}  # no workspace, no migration any more
    assert report["restored"] == payload["session_id"] and report["recovery_error"] is None
    assert orch.session.status == "paused" and orch.world.state.phase == "restored!"
    assert orch._autosave_enabled and orch._supervisor is None


@async_test
async def test_startup_survives_a_broken_last_session(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch.settings_store.last_id = "corrupt"
    orch.game_store.data["corrupt"] = {"schema_version": SESSION_SCHEMA_VERSION, "session_id": "corrupt", "participants": "nope"}

    report = await orch.startup()

    assert report["restored"] is None and report["recovery_error"]
    assert orch._recovery_pending and orch._autosave_enabled
    assert orch.session.status == "idle"


@async_test
async def test_startup_without_a_last_game_keeps_the_fresh_one(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch, defaults={"p01": BINDING})
    fresh = orch.session
    report = await orch.startup()
    assert report["restored"] is None and orch.session is fresh
    assert {i.actor_id for i in fresh.binding_issues} >= {"p02", MODERATOR_ID}  # refreshed against the stores


@async_test
async def test_a_finished_game_is_restored_as_finished_not_resumed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = saved_payload(monkeypatch, status="finished")
    payload["world"]["state"]["phase"] = "THE END"
    orch = real_orch(monkeypatch)
    orch.game_store.data[payload["session_id"]] = payload
    orch.settings_store.last_id = payload["session_id"]

    assert await orch._restore_last_session() == payload["session_id"]

    assert orch.session.status == "finished" and orch.world.state.status == "finished"
    assert not orch.world.running and orch.clock.paused and orch._supervisor is None
    assert orch.session.restore_notice == "已恢复上次对局（已结束）"


@async_test
async def test_shutdown_saves_then_gives_the_data_directory_back(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    await orch.startup()
    assert held_data_dir() is not None
    order: list[str] = []
    orch.game_store.on_save = lambda sid, payload: order.append("save")
    original = release_data_dir

    import app.game.session_runtime as session_runtime

    monkeypatch.setattr(session_runtime, "release_data_dir", lambda: (order.append("release"), original())[1])

    await orch.shutdown()

    assert order[-1] == "release" and order.count("release") == 1 and "save" in order[:-1]  # saved, then let go
    assert held_data_dir() is None and orch.game_store.data[orch.session.session_id]["status"] == "idle"


# ------------------------------------------------------------------------- import ---


@async_test
async def test_importing_a_save_pauses_it_and_applies_default_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = saved_payload(monkeypatch, status="running")
    payload["session_id"] = "imported1"
    payload["model_bindings"] = {}
    orch = real_orch(monkeypatch, defaults={"p01": BINDING})
    orch._autosave_enabled = True
    live = orch.session

    game = await orch.load_session_data(payload)
    await asyncio.sleep(0.1)

    assert orch.session is game and game is not live and game.session_id == "imported1"
    assert game.schema_version == SESSION_SCHEMA_VERSION and game.status == "paused" and not orch.world.running
    assert game.restored_from_save and game.restore_notice == "已导入存档（已暂停）"
    assert game.model_bindings["p01"] == BINDING  # from the user's defaults, no provider created
    assert "imported1" in orch.game_store.data and orch.settings_store.last_id == "imported1"


@async_test
async def test_import_that_fails_validation_changes_nothing_and_saves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch._autosave_enabled = True
    live = orch.session
    for bad in ({"schema_version": 99}, {"schema_version": 2, "session_id": "old"}, [1, 2], None):
        with pytest.raises(SessionLoadError):
            await orch.load_session_data(bad)  # type: ignore[arg-type]
    broken_world = saved_payload(monkeypatch)
    broken_world["world"] = {"format": 1, "state": {"players": "nope"}}
    with pytest.raises(SessionLoadError):
        await orch.load_session_data(broken_world)
    await asyncio.sleep(0.05)
    assert orch.session is live and orch.game_store.saves == []


# ------------------------------------------------------------------- other games (F34) ---


def other_game(monkeypatch: pytest.MonkeyPatch, session_id: str = "other001", *, status: str = "running", **changes: Any) -> dict[str, Any]:
    payload = saved_payload(monkeypatch, status=status)
    payload["session_id"] = session_id
    payload["world"]["state"]["phase"] = "THE OTHER GAME"
    payload.update(changes)
    return payload


@async_test
async def test_loading_a_game_saves_the_live_one_first_and_brings_the_other_back_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch, defaults={"p01": BINDING})
    orch._autosave_enabled = True
    orch.game_store.data["other001"] = other_game(monkeypatch)
    live = orch.session
    live.title = "the live one"

    game = await orch.load_game("other001")
    await asyncio.sleep(0.1)  # (the autosave of the broadcast)

    assert orch.game_store.saves[0] == live.session_id  # the game that was live is safe before anything changes
    assert orch.game_store.data[live.session_id]["title"] == "the live one"
    assert orch.session is game and game.session_id == "other001" and game is not live
    assert game.status == "paused" and not orch.world.running and orch.clock.paused and orch._supervisor is None
    assert orch.world.state.phase == "THE OTHER GAME" and orch.world.state.status == "paused"  # "running" on disk, never resumed
    assert game.restored_from_save and game.restore_notice == "已载入对局（已暂停）"
    assert orch.settings_store.last_id == "other001"  # it is the game the next start comes back to
    assert not orch._recovery_pending


@async_test
async def test_a_loaded_game_can_be_continued_like_any_paused_game(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    bots_for(orch, idle_moderator)
    orch.game_store.data["other001"] = other_game(monkeypatch)
    await orch.load_game("other001")
    started = await orch.start_or_resume()
    try:
        assert started.status == "running" and started.session_id == "other001" and orch._supervisor is not None
    finally:
        await orch.pause()


@async_test
async def test_loading_the_live_game_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch._autosave_enabled = True
    live = orch.session
    assert await orch.load_game(live.session_id) is live
    assert orch.session is live and orch.game_store.saves == []  # not even a save


@async_test
async def test_a_running_game_cannot_be_left_for_another_one(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    bots_for(orch, idle_moderator)
    orch.game_store.data["other001"] = other_game(monkeypatch)
    await orch.start_or_resume()
    live = orch.session
    try:
        with pytest.raises(RuntimeError, match="暂停"):
            await orch.load_game("other001")
        assert orch.session is live and live.status == "running" and orch.game_store.saves == []
    finally:
        await orch.pause()


@async_test
async def test_loading_an_unknown_invalid_old_or_inconsistent_game_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.game.orchestrator import SessionLoadError
    from app.storage.games import GameNotFoundError

    orch = real_orch(monkeypatch)
    orch._autosave_enabled = True
    live = orch.session
    orch.game_store.data["old-one"] = {"session_id": "old-one", "schema_version": 4, "status": "paused"}
    orch.game_store.data["liar"] = other_game(monkeypatch, "someone-else")
    orch.game_store.data["broken"] = {"schema_version": SESSION_SCHEMA_VERSION, "session_id": "broken", "participants": "nope"}

    with pytest.raises(GameNotFoundError):
        await orch.load_game("nothing")
    for bad_id in ("../escape", "a/b", "CON", ""):
        with pytest.raises(ValueError):
            await orch.load_game(bad_id)
    with pytest.raises(SessionLoadError, match="migrate_v0121_to_archive"):
        await orch.load_game("old-one")  # a v0.12.1 game: the message says how to bring it over
    with pytest.raises(SessionLoadError, match="不一致"):
        await orch.load_game("liar")
    with pytest.raises(SessionLoadError):
        await orch.load_game("broken")

    assert orch.session is live and orch.game_store.saves == []  # the live game was not even saved for these
    assert orch.game_store.data["old-one"] == {"session_id": "old-one", "schema_version": 4, "status": "paused"}


@async_test
async def test_a_failed_save_of_the_live_game_cancels_the_load(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch._autosave_enabled = True
    orch.game_store.data["other001"] = other_game(monkeypatch)
    live, world = orch.session, orch.world
    orch.game_store.fail_save = "磁盘已满"

    with pytest.raises(StorageError, match="磁盘已满"):
        await orch.load_game("other001")

    assert orch.session is live and orch.world is world and "磁盘已满" in live.autosave_error  # told, not lost
    assert orch.settings_store.last_id is None
    orch.game_store.fail_save = None
    assert (await orch.load_game("other001")).session_id == "other001"  # and it works once the disk does


@async_test
async def test_the_placeholder_of_a_recovery_state_is_not_saved_when_another_game_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch._autosave_enabled = True
    orch._recovery_pending = True  # the last game was damaged: the empty placeholder must never be written
    orch.game_store.data["other001"] = other_game(monkeypatch)

    await orch.load_game("other001")

    assert orch.game_store.saves == [] or all(sid == "other001" for sid in orch.game_store.saves)
    assert not orch._recovery_pending and orch.session.session_id == "other001"


# ----------------------------------------------------------------- the timestamps (F22) ---


def finishing_moderator() -> Any:
    state = {"done": False}

    def policy(world: Any, me: str) -> list[Any]:
        if state["done"]:
            return []
        state["done"] = True
        return [("end_game", {"winner": "测试阵营", "summary": "测试"})]

    return policy


async def until(predicate: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.02)


@async_test
async def test_created_started_and_finished_at_follow_the_life_of_a_game(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timezone

    orch = real_orch(monkeypatch)
    bots_for(orch, finishing_moderator())
    game = orch.session
    created = datetime.fromisoformat(game.created_at)
    assert created.tzinfo is not None and abs((datetime.now(timezone.utc) - created).total_seconds()) < 60  # UTC, now
    assert game.started_at is None and game.finished_at is None

    await orch.start_or_resume()
    started = game.started_at
    assert started is not None and datetime.fromisoformat(started) >= created
    await until(lambda: game.status == "finished")
    finished = game.finished_at
    assert finished is not None and datetime.fromisoformat(finished) >= datetime.fromisoformat(started)
    assert game.started_at == started and orch.session_payload()["finished_at"] == finished  # it is part of the save

    await orch.start_or_resume()  # "continue" a finished game
    try:
        assert game.finished_at is None and game.started_at == started  # continued: not finished any more, same start
    finally:
        await orch.pause()


@async_test
async def test_a_game_that_is_paused_and_resumed_keeps_its_start(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    bots_for(orch, idle_moderator)
    game = orch.session
    await orch.start_or_resume()
    started = game.started_at
    await orch.pause()
    await asyncio.sleep(0.02)
    await orch.start_or_resume()
    try:
        assert game.started_at == started and game.finished_at is None
    finally:
        await orch.pause()


@async_test
async def test_a_game_saved_without_a_start_time_gets_one_when_it_is_started(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = other_game(monkeypatch, status="paused", started_at=None, created_at=None)  # e.g. converted from a v0.12.1 game
    orch = real_orch(monkeypatch)
    bots_for(orch, idle_moderator)
    orch.game_store.data["other001"] = payload
    await orch.load_game("other001")
    assert orch.session.started_at is None
    await orch.start_or_resume()
    try:
        assert orch.session.started_at is not None
    finally:
        await orch.pause()
