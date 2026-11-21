"""``persistence_suspended()`` and ``reload_from_data_dir()`` (v0.13; contract section 11.3): what an archive import
needs from the orchestrator when it replaces the data directory as a whole.

The stores are the real ones here (files in the isolated data directory); the second directory ("the archive") is made
with the same stores and copied over the first, the way the importer does it.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from app.engine.llm import LLMClient
from app.game.orchestrator import GameOrchestrator
from app.providers.store import ProviderStore, get_provider_store
from app.storage import reset_stores_for_tests
from app.storage.games import GameStore
from app.storage.bots import BotStore
from app.storage.secrets import SecretStore
from app.storage.settings import SettingsStore
from game_fakes import async_test, bots_for, idle_moderator, real_orch

KEY_NEW = "sk-test-NEW-secret-value-999999"
KEY_OLD = "sk-test-OLD-secret-value-111111"


def make_archive_dir(path: Path, *, game: bool = True, phase: str = "IMPORTED GAME", provider: bool = True) -> dict[str, str]:
    """A data directory as an import would bring it: a game, a provider with its key, the bots, bindings."""
    ids: dict[str, str] = {}
    if game:
        payload = GameOrchestrator().session_payload()
        payload["world"]["state"]["phase"] = phase
        payload["status"] = "paused"
        payload["world"]["state"]["status"] = "paused"
        GameStore(path).save(payload["session_id"], payload)
        SettingsStore(path).set_last_session_id(payload["session_id"])
        ids["game"] = payload["session_id"]
    if provider:
        created = ProviderStore(path).create_provider("Imported", "openai_chat", "https://api.example.com/v1", KEY_NEW)
        ProviderStore(path).add_model(created.id, "m1", 1.0, 2.0, 64_000)
        ids["provider"] = created.id
    table = BotStore(path)  # (v0.14: the bots - the third one is called "Imported")
    table.bootstrap()
    table.update_bot(table.lineup()[2], name="Imported")
    return ids


def replace_directory(live: Path, archive: Path) -> None:
    """What the importer does: everything but the lock goes, the archive's files come."""
    for child in live.iterdir():
        if child.name == ".lock":
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    shutil.copytree(archive, live, dirs_exist_ok=True)


def names_in(path: Path) -> set[str]:
    return {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file() and p.name != ".lock"}


@pytest.fixture()
def live(isolated_external_storage: Path) -> Path:
    reset_stores_for_tests()
    yield isolated_external_storage
    reset_stores_for_tests()


@pytest.fixture()
def archive(tmp_path: Path) -> Path:
    return tmp_path / "the-archive"


async def booted() -> GameOrchestrator:
    orch = GameOrchestrator()
    report = await orch.startup()
    assert report["recovery_error"] is None
    orch._autosave_delay = 0.02
    return orch


# ---------------------------------------------------------------------------- reloading
@async_test
async def test_a_replaced_data_directory_is_read_again_and_its_last_game_comes_back_paused(live: Path, archive: Path) -> None:
    orch = await booted()
    old_provider = get_provider_store().create_provider("Old", "openai_chat", "https://api.example.com/v1", KEY_OLD)
    old_id = orch.session.session_id
    await orch.persist_session("before")
    assert GameStore(live).exists(old_id)
    ids = make_archive_dir(archive)
    _, queue = orch.bus.subscribe()

    replace_directory(live, archive)
    session = await orch.reload_from_data_dir()

    assert session is orch.session and session.session_id == ids["game"] != old_id
    assert session.status == "paused" and orch.world.state.phase == "IMPORTED GAME" and not orch.world.running
    assert session.restored_from_save and session.restore_notice == "已恢复上次对局（已暂停）"
    # providers, keys and the bots of the new directory are what the program sees now
    assert [p.id for p in get_provider_store().list_providers()] == [ids["provider"]]
    assert get_provider_store().get_provider(ids["provider"]).api_key == KEY_NEW
    assert old_provider.id not in {p.id for p in get_provider_store().list_providers()}
    assert orch.bots_payload()["bots"][2]["name"] == "Imported"
    # nothing of the old game was written into the new directory, before or after
    await asyncio.sleep(0.2)
    assert not GameStore(live).exists(old_id)
    assert names_in(live) >= names_in(archive) and SecretStore(live).read_all() == {ids["provider"]: KEY_NEW}
    # the spectators are told: one snapshot of the new game
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    snapshots = [e for e in events if e.type == "snapshot"]
    assert snapshots and snapshots[-1].data["session"]["session_id"] == ids["game"]
    assert orch._autosave_enabled and not orch._recovery_pending
    await orch.shutdown()


@async_test
async def test_without_a_last_game_the_new_directory_gets_a_fresh_game_with_its_own_defaults(live: Path, archive: Path) -> None:
    orch = await booted()
    ids = make_archive_dir(archive, game=False)
    from app.providers.models import ModelBinding

    SettingsStore(archive).save_default_bindings({"N1": ModelBinding(provider_id=ids["provider"], model_id="m1", reasoning_depth="low")})
    old = orch.session

    replace_directory(live, archive)
    session = await orch.reload_from_data_dir()

    assert session is not old and session.status == "idle" and not session.restored_from_save
    assert session.model_bindings["N1"].provider_id == ids["provider"]  # the defaults of the new directory
    assert {i.actor_id for i in session.binding_issues} == {"moderator", *(f"N{i}" for i in range(2, 13))}
    await orch.shutdown()


@async_test
async def test_a_last_game_that_the_archive_does_not_contain_gives_a_fresh_game_and_a_note(live: Path, archive: Path) -> None:
    orch = await booted()
    make_archive_dir(archive, game=False)
    SettingsStore(archive).set_last_session_id("not-in-the-archive")

    replace_directory(live, archive)
    session = await orch.reload_from_data_dir()

    assert session.status == "idle" and not orch._recovery_pending
    assert "已经不存在" in session.restore_notice
    await orch.shutdown()


@async_test
async def test_a_damaged_last_game_is_kept_and_nothing_is_saved_over_it(live: Path, archive: Path) -> None:
    orch = await booted()
    ids = make_archive_dir(archive)
    broken = archive / "games" / ids["game"]
    (broken / "game.json").write_bytes(b"{ not json")
    (broken / "game.prev.json").unlink(missing_ok=True)

    replace_directory(live, archive)
    session = await orch.reload_from_data_dir()
    orch.schedule_autosave("status_change")
    await orch.flush_autosave()
    await asyncio.sleep(0.1)

    assert orch._recovery_pending and session.restore_notice.startswith("无法恢复上次对局")
    assert (live / "games" / ids["game"] / "game.json").read_bytes() == b"{ not json"  # untouched
    assert SettingsStore(live).get_last_session_id() == ids["game"]
    await orch.shutdown()


@async_test
async def test_the_llm_client_is_rebuilt_and_the_old_one_closed(live: Path, archive: Path) -> None:
    orch = await booted()
    closed: list[int] = []

    class OldClient:
        _effort_user_set = True
        reasoning_effort = "xhigh"

        async def aclose(self) -> None:
            closed.append(1)

    orch.llm = OldClient()  # type: ignore[assignment]
    make_archive_dir(archive, game=False, provider=False)
    replace_directory(live, archive)

    await orch.reload_from_data_dir()

    assert isinstance(orch.llm, LLMClient) and orch.llm.reasoning_effort == "xhigh" and closed == [1]  # a UI choice survives
    await orch.shutdown()


@async_test
async def test_reload_is_refused_while_a_game_runs_or_a_review_is_being_written(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    bots_for(orch, idle_moderator)
    await orch.start_or_resume()
    live_session = orch.session
    try:
        with pytest.raises(RuntimeError, match="暂停"):
            await orch.reload_from_data_dir()
        assert orch.session is live_session and live_session.status == "running"
    finally:
        await orch.pause()
    orch.review_running_ids = lambda: {"N1"}  # type: ignore[method-assign]  # (a review is being written)
    with pytest.raises(RuntimeError, match="复盘"):
        await orch.reload_from_data_dir()
    del orch.review_running_ids
    orch.init_running_ids = lambda: {"b-00000001"}  # type: ignore[method-assign]  # (an initialization is being run)
    with pytest.raises(RuntimeError, match="初始化"):
        await orch.reload_from_data_dir()
    del orch.init_running_ids


# ------------------------------------------------------------------ persistence_suspended
@async_test
async def test_inside_the_block_nothing_is_saved_and_after_it_everything_is(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch._autosave_enabled = True
    orch._autosave_delay = 0.02
    orch.schedule_autosave("pending")  # a debounced save is waiting when the block begins
    pending = orch._autosave_task
    assert pending is not None and not pending.done()

    async with orch.persistence_suspended():
        assert pending.done()  # cancelled on entry
        orch.schedule_autosave("during")  # dropped: no timer, no queue
        assert orch._autosave_task is pending and not orch._autosave_dirty
        assert await orch.persist_session("explicit", raise_errors=False) is False
        with pytest.raises(RuntimeError, match="正在被替换"):
            await orch.persist_session("explicit")
        assert await orch.flush_autosave("flush") is False  # (and no deadlock on the lock the block holds)
        await asyncio.sleep(0.15)
        assert orch.game_store.saves == []

    assert orch.game_store.saves == []  # the dropped requests are not replayed
    orch.schedule_autosave("after")
    await asyncio.sleep(0.15)
    assert orch.game_store.saves == [orch.session.session_id]
    assert await orch.persist_session("explicit") is True


@async_test
async def test_the_block_waits_for_a_write_that_is_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def slow_save(session_id: str, payload: dict[str, Any]) -> None:
        entered.set()
        assert release.wait(10)

    orch.game_store.on_save = slow_save
    saving = asyncio.create_task(orch.persist_session("slow"))
    while not entered.is_set():
        await asyncio.sleep(0.005)

    inside = asyncio.Event()

    async def block() -> None:
        async with orch.persistence_suspended():
            inside.set()

    waiting = asyncio.create_task(block())
    await asyncio.sleep(0.1)
    assert not inside.is_set() and not waiting.done()  # the write in flight is not run over
    release.set()
    await asyncio.wait_for(saving, 5)
    await asyncio.wait_for(waiting, 5)
    assert inside.is_set() and orch.game_store.saves == [orch.session.session_id]


@async_test
async def test_the_block_is_reentrant_and_always_gives_everything_back(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch._autosave_enabled = True
    async with orch.persistence_suspended():
        assert orch._persistence_holds == 1
        async with orch.persistence_suspended():
            assert orch._persistence_holds == 2
        assert orch._persistence_holds == 1 and orch._get_persist_lock().locked()
    assert orch._persistence_holds == 0 and not orch._get_persist_lock().locked()

    with pytest.raises(ValueError, match="boom"):
        async with orch.persistence_suspended():
            async with orch.persistence_suspended():
                raise ValueError("boom")
    assert orch._persistence_holds == 0 and not orch._get_persist_lock().locked()
    assert await orch.persist_session("explicit") is True  # saving works again


@async_test
async def test_the_whole_replacement_inside_one_block_keeps_the_old_game_out_of_the_new_directory(live: Path, archive: Path) -> None:
    """The importer's sequence: flush, then (suspended) replace the files and reload."""
    orch = await booted()
    old_id = orch.session.session_id
    await orch.flush_autosave("pre-import")
    assert GameStore(live).exists(old_id)
    ids = make_archive_dir(archive)
    orch.schedule_autosave("something changed meanwhile")  # a save is queued behind the flush

    async with orch.persistence_suspended():
        replace_directory(live, archive)
        await orch.reload_from_data_dir()  # inside the block: no deadlock, no save
        await asyncio.sleep(0.15)  # the queued save would have fired by now
        assert not GameStore(live).exists(old_id)

    await asyncio.sleep(0.15)
    assert not GameStore(live).exists(old_id) and orch.session.session_id == ids["game"]
    assert SettingsStore(live).get_last_session_id() == ids["game"]  # the pointer of the archive, untouched
    await orch.shutdown()
