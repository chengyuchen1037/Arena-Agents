"""v0.10 engine contract: automatic encrypted saves, restore as paused, startup, shutdown."""

from __future__ import annotations

import asyncio
import copy
import functools
import sys
import tempfile
import types
from pathlib import Path
from typing import Any, Callable

import pytest

from app.engine.orchestrator import GameOrchestrator, SessionLoadError
from app.engine.state import Channel, GameSession, Participant
from app.providers.models import ModelBinding, ModelEntry, ProviderConfig, ProviderNotFound, StorageError

REAL_PROJECT_DIR = Path(__file__).resolve().parents[1]


def _project_fingerprint() -> tuple[Any, ...]:
    """Names + sizes + mtimes of the real project's .env and saves/ (never their contents)."""
    entries: list[tuple[str, int, int]] = []
    env_file = REAL_PROJECT_DIR / ".env"
    if env_file.exists():
        stat = env_file.stat()
        entries.append((".env", stat.st_size, stat.st_mtime_ns))
    saves = REAL_PROJECT_DIR / "saves"
    if saves.is_dir():
        for item in sorted(saves.iterdir()):
            stat = item.stat()
            entries.append((f"saves/{item.name}", stat.st_size, stat.st_mtime_ns))
    return tuple(entries)


@pytest.fixture(autouse=True)
def real_project_files_untouched() -> Any:
    before = _project_fingerprint()
    yield
    assert _project_fingerprint() == before, "a test modified the real project .env/saves"


TEST_CFG = {
    "prompts": {"moderator_system": "__x.md", "participant_system": "__y.md"},
    "knowledge_base": {"docs_dir": str(Path(tempfile.gettempdir()) / "arena_test_missing_knowledge")},
}


def async_test(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeSessions:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.saves: list[str] = []
        self.fail_save: str | None = None
        self.on_save: Callable[[str, dict[str, Any]], None] | None = None

    def save(self, session_id: str, payload: dict[str, Any]) -> None:
        if self.on_save:
            self.on_save(session_id, payload)
        if self.fail_save:
            raise StorageError(self.fail_save)
        self.saves.append(session_id)
        self.data[session_id] = copy.deepcopy(payload)

    def load(self, session_id: str) -> dict[str, Any]:
        if session_id not in self.data:
            raise StorageError("存档不存在或已损坏")
        return copy.deepcopy(self.data[session_id])

    def exists(self, session_id: str) -> bool:
        return session_id in self.data


class FakeSettings:
    def __init__(self, defaults: dict[str, ModelBinding] | None = None) -> None:
        self.defaults = dict(defaults or {})
        self.last_id: str | None = None

    def get_default_bindings(self) -> dict[str, ModelBinding]:
        return dict(self.defaults)

    def save_default_bindings(self, bindings: dict[str, ModelBinding]) -> None:
        self.defaults = dict(bindings)

    def get_last_session_id(self) -> str | None:
        return self.last_id

    def set_last_session_id(self, session_id: str) -> None:
        self.last_id = session_id


class FakeProviders:
    def __init__(self, *providers: ProviderConfig) -> None:
        self.providers = {p.id: p for p in providers}

    def resolve(self, binding: ModelBinding) -> tuple[ProviderConfig, ModelEntry]:
        provider = self.providers.get(binding.provider_id)
        if provider is None:
            raise ProviderNotFound(binding.provider_id)
        return provider, provider.models[0]


VALID_SNAPSHOT: dict[str, Any] = {
    "path": "mem.md",
    "checksum": "c",
    "body": "",
    "definition": {
        "schema_version": 1, "id": "x", "version": "1", "entry": "a",
        "workflow": [{"id": "a", "do": [], "next": "a"}],
    },
}
PROVIDER = ProviderConfig(
    id="prov", name="prov", protocol="openai_chat", base_url="https://example.invalid", api_key="k",
    models=[ModelEntry(id="m", input_price_per_m=1.0, output_price_per_m=1.0)],
)
BINDING = ModelBinding(provider_id="prov", model_id="m")


class FakeLLM:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def make_session(status: str = "paused", **kw: Any) -> GameSession:
    return GameSession(
        status=status,
        participants=[Participant(id="p01", seat="S1", name="N1"), Participant(id="p02", seat="S2", name="N2")],
        channels=[Channel(id="public", name="P", type="public", members=["p01", "p02"])],
        scenario_snapshot=VALID_SNAPSHOT,
        **kw,
    )


def make_orch(session: GameSession | None = None, *, enabled: bool = True) -> GameOrchestrator:
    orch = GameOrchestrator.__new__(GameOrchestrator)
    orch.cfg = TEST_CFG
    orch.session = session or make_session()
    orch.llm = FakeLLM()
    orch._subscribers = set()
    orch.provider_store = FakeProviders(PROVIDER)
    orch.settings_store = FakeSettings()
    orch.session_store = FakeSessions()
    orch._autosave_enabled = enabled
    orch._autosave_delay = 0.01
    return orch


def real_orch() -> GameOrchestrator:
    orch = GameOrchestrator()
    orch.provider_store = FakeProviders(PROVIDER)
    orch.settings_store = FakeSettings({"p01": BINDING})
    orch.session_store = FakeSessions()
    orch._subscribers = set()
    orch._autosave_delay = 0.01
    return orch


# ------------------------------------------------------------------- persist_session ---

@async_test
async def test_persist_saves_a_full_json_snapshot_and_then_moves_the_pointer() -> None:
    session = make_session(moderator_messages=[{"role": "user", "content": "kept"}])
    session.model_bindings = {"p01": BINDING}
    orch = make_orch(session)

    assert await orch.persist_session("manual") is True

    saved = orch.session_store.data[session.session_id]
    assert saved["moderator_messages"] == [{"role": "user", "content": "kept"}]  # full state
    assert saved["model_bindings"]["p01"]["model_id"] == "m" and saved["schema_version"] == 2
    assert "api_key" not in str(saved) and "example.invalid" not in str(saved)
    assert orch.settings_store.last_id == session.session_id


@async_test
async def test_failed_manual_save_raises_and_marks_the_session_but_never_loses_it() -> None:
    orch = make_orch()
    orch.session_store.fail_save = "磁盘不可写"
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

    assert await orch.persist_session("step_done", raise_errors=False) is False  # autosave: no raise
    orch.session_store.fail_save = None
    assert await orch.persist_session("manual") is True
    assert session.autosave_error == ""  # cleared by the next success


@async_test
async def test_slow_save_never_points_last_session_at_a_replaced_game() -> None:
    orch = make_orch()
    old = orch.session
    other = make_session()

    def swap_during_save(session_id: str, payload: dict[str, Any]) -> None:
        orch._session_generation += 1  # the game is replaced while the write is in flight
        orch.session = other

    orch.session_store.on_save = swap_during_save
    assert await orch.persist_session("step_done", raise_errors=False) is True
    assert old.session_id in orch.session_store.data  # its own file is fine...
    assert orch.settings_store.last_id is None  # ...but the pointer was not moved to a stale game


# ----------------------------------------------------------------------- autosave ---

@async_test
async def test_autosave_is_debounced_and_never_triggered_by_stream_chunks() -> None:
    orch = make_orch()
    for _ in range(50):
        await orch.broadcast_state("stream_chunk")
        await orch.broadcast_state("tool_executed")
        await orch.broadcast_state("thought_added")
    await asyncio.sleep(0.1)
    assert orch.session_store.saves == []

    for index in range(30):
        orch.session.phase_text = f"step {index}"
        await orch.broadcast_state("scenario_step_done")
    await asyncio.sleep(0.15)

    assert 1 <= len(orch.session_store.saves) <= 2  # coalesced, not 30 writes
    assert orch.session_store.data[orch.session.session_id]["phase_text"] == "step 29"  # trailing save


@async_test
async def test_autosave_does_nothing_until_enabled() -> None:
    orch = make_orch(enabled=False)
    await orch.broadcast_state("status_change")
    await asyncio.sleep(0.05)
    assert orch.session_store.saves == []


@async_test
async def test_flush_writes_now_and_cancels_the_pending_timer() -> None:
    orch = make_orch()
    orch._autosave_delay = 30
    await orch.broadcast_state("step_done")
    task = orch._autosave_task
    assert task is not None and not task.done() and orch.session_store.saves == []

    await orch.flush_autosave("test")

    assert task.done() and len(orch.session_store.saves) == 1


@async_test
async def test_autosave_failure_is_reported_not_raised_into_the_game_loop() -> None:
    orch = make_orch()
    orch.session_store.fail_save = "locked"
    await orch.broadcast_state("vote_done")  # must not raise
    await asyncio.sleep(0.1)
    assert "locked" in orch.session.autosave_error
    assert orch.session.status == "paused"


# --------------------------------------------------------------------- shutdown ---

@async_test
async def test_shutdown_pauses_flushes_the_save_then_closes_the_llm() -> None:
    session = make_session("running")
    orch = make_orch(session)
    order: list[str] = []
    orch.session_store.on_save = lambda sid, payload: order.append(f"save:{payload['status']}")

    async def never_ending() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            order.append("loop-cancelled")

    orch._loop_task = asyncio.create_task(never_ending())
    await asyncio.sleep(0)
    original_close = orch.llm.aclose

    async def close() -> None:
        order.append("llm-closed")
        await original_close()

    orch.llm.aclose = close  # type: ignore[method-assign]

    await orch.shutdown()

    assert order[0] == "loop-cancelled"
    assert order[-1] == "llm-closed" and "save:paused" in order  # saved as paused, before closing
    assert order.index("save:paused") < order.index("llm-closed")
    assert orch.llm.closed and session.status == "paused"
    assert orch.settings_store.last_id == session.session_id

    await orch.broadcast_state("status_change")  # after shutdown nothing is scheduled
    await asyncio.sleep(0.05)
    assert len([o for o in order if o.startswith("save:")]) == len(orch.session_store.saves)
    await orch.shutdown()  # idempotent


@async_test
async def test_shutdown_of_an_idle_game_does_not_force_it_paused() -> None:
    orch = make_orch(make_session("idle"))
    await orch.shutdown()
    assert orch.session.status == "idle"
    assert orch.session_store.data[orch.session.session_id]["status"] == "idle"


# ------------------------------------------------------------------------ restore ---

@async_test
async def test_last_game_is_restored_paused_never_running() -> None:
    saved = make_session("running", phase_text="第 3 天")
    saved.model_bindings = {"p01": BINDING}
    saved.moderator_messages = [{"role": "user", "content": "hi"}]
    orch = make_orch(make_session("idle"))
    orch.session_store.data[saved.session_id] = saved.model_dump(mode="json")
    orch.settings_store.last_id = saved.session_id
    generation = orch._session_generation

    restored = await orch._restore_last_session()

    assert restored == saved.session_id
    assert orch.session.session_id == saved.session_id and orch.session.phase_text == "第 3 天"
    assert orch.session.status == "paused" and orch._loop_task is None
    assert orch.session.restored_from_save is True and orch.restored_from_save is True
    assert orch.session.restore_notice == "已恢复上次对局（已暂停）"
    assert orch._session_generation == generation + 1
    # p02 has no binding: reported, never silently filled with another provider
    assert [i.actor_id for i in orch.session.binding_issues] == ["p02"]
    assert not orch._recovery_pending


@async_test
async def test_restore_with_vanished_provider_reports_issues_without_rebinding() -> None:
    saved = make_session("paused")
    saved.model_bindings = {"p01": BINDING, "p02": ModelBinding(provider_id="gone", model_id="x")}
    orch = make_orch(make_session("idle"))
    orch.session_store.data[saved.session_id] = saved.model_dump(mode="json")
    orch.settings_store.last_id = saved.session_id

    await orch._restore_last_session()

    assert orch.session.model_bindings["p02"].provider_id == "gone"  # untouched
    issues = {i.actor_id: i.message for i in orch.session.binding_issues}
    assert set(issues) == {"p02"} and "不存在" in issues["p02"]


@async_test
async def test_no_pointer_means_a_fresh_game_and_no_recovery_mode() -> None:
    orch = make_orch()
    assert await orch._restore_last_session() is None
    assert not orch._recovery_pending and not orch.session.restored_from_save


@pytest.mark.parametrize("failure", ["missing", "invalid"])
@async_test
async def test_unrecoverable_last_game_keeps_files_and_suppresses_autosave(failure: str) -> None:
    fresh = make_session("idle")
    orch = make_orch(fresh)
    orch.settings_store.last_id = "broken-game"
    if failure == "invalid":
        orch.session_store.data["broken-game"] = {"participants": "nope"}

    assert await orch._restore_last_session() is None

    assert orch.session is fresh and orch._recovery_pending
    assert "已保留" in fresh.restore_notice and "broken-game" in fresh.restore_notice
    assert orch.recovery_error

    await orch.broadcast_state("status_change")
    await orch.flush_autosave()
    await asyncio.sleep(0.05)
    assert orch.session_store.saves == []  # the placeholder never overwrites anything
    assert orch.settings_store.last_id == "broken-game"  # pointer untouched
    if failure == "invalid":
        assert orch.session_store.data["broken-game"] == {"participants": "nope"}


@async_test
async def test_explicit_new_game_leaves_recovery_mode_and_saves_again() -> None:
    orch = real_orch()
    orch._autosave_enabled = True
    orch._recovery_pending = True
    orch.settings_store.last_id = "broken-game"

    game = await orch.new_game()
    await asyncio.sleep(0.1)

    assert not orch._recovery_pending
    assert game.session_id in orch.session_store.data
    assert orch.settings_store.last_id == game.session_id
    assert game.restored_from_save is False and "p01" in game.model_bindings  # default binding


# ------------------------------------------------------------------------ startup ---

def fake_migration(monkeypatch: pytest.MonkeyPatch, behavior: Callable[..., Any]) -> list[Any]:
    calls: list[Any] = []
    module = types.ModuleType("app.migration")

    def migrate_legacy_data(base_dir: Path, cfg: dict[str, Any]) -> dict[str, Any]:
        calls.append((base_dir, bool(cfg)))  # recorded only; this fake never touches the disk
        return behavior()

    module.migrate_legacy_data = migrate_legacy_data  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.migration", module)
    return calls


@async_test
async def test_startup_migrates_then_restores_last_game_paused_and_enables_autosave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = fake_migration(monkeypatch, lambda: {"providers": 1, "sessions": 0})
    orch = real_orch()
    saved = make_session("running", phase_text="restored!")
    orch.session_store.data[saved.session_id] = saved.model_dump(mode="json")
    orch.settings_store.last_id = saved.session_id
    assert not orch._autosave_enabled

    report = await orch.startup()

    assert len(calls) == 1
    assert report["migration"] == {"providers": 1, "sessions": 0}
    assert report["restored"] == saved.session_id and report["recovery_error"] is None
    assert orch.session.status == "paused" and orch.session.phase_text == "restored!"
    assert orch._autosave_enabled and orch._loop_task is None


@async_test
async def test_startup_survives_a_failing_migration_and_a_broken_last_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> dict[str, Any]:
        raise RuntimeError("secret-path/.env exploded")

    fake_migration(monkeypatch, boom)
    orch = real_orch()
    orch.settings_store.last_id = "corrupt"

    report = await orch.startup()

    assert report["migration"] == {"error": "RuntimeError"}  # no message/paths leaked
    assert report["restored"] is None and report["recovery_error"]
    assert orch._recovery_pending and orch._autosave_enabled
    assert orch.session.status == "idle"


@async_test
async def test_startup_without_a_last_game_keeps_the_fresh_one(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_migration(monkeypatch, lambda: {})
    orch = real_orch()
    fresh = orch.session
    report = await orch.startup()
    assert report["restored"] is None and orch.session is fresh
    assert {i.actor_id for i in fresh.binding_issues} >= {"p02"}  # refreshed against the stores


# ------------------------------------------------------------------------- import ---

def v1_save() -> dict[str, Any]:
    payload = GameSession(
        status="running", session_id="oldsave1", scenario_snapshot=VALID_SNAPSHOT,
        participants=[Participant(id="p01", seat="S1", name="N1")],
    ).model_dump(mode="json")
    for key in ("schema_version", "model_bindings", "binding_issues", "restored_from_save",
                "restore_notice", "autosave_error"):
        payload.pop(key)
    payload["version"] = "v0.9"
    return payload


@async_test
async def test_importing_an_old_save_migrates_it_pauses_it_and_applies_default_bindings() -> None:
    orch = real_orch()
    orch._autosave_enabled = True
    live = orch.session

    game = await orch.load_session_data(v1_save())
    await asyncio.sleep(0.1)

    assert orch.session is game and game is not live and game.session_id == "oldsave1"
    assert game.schema_version == 2 and game.status == "paused"
    assert game.restored_from_save and game.restore_notice == "已导入存档（已暂停）"
    assert game.model_bindings["p01"] == BINDING  # from the user's defaults, no provider created
    assert game.binding_issues == []
    assert "oldsave1" in orch.session_store.data and orch.settings_store.last_id == "oldsave1"


@async_test
async def test_import_that_fails_validation_changes_nothing_and_saves_nothing() -> None:
    orch = real_orch()
    orch._autosave_enabled = True
    live = orch.session
    live.status = "paused"
    with pytest.raises(SessionLoadError):
        await orch.load_session_data({"schema_version": 99})
    await asyncio.sleep(0.05)
    assert orch.session is live and orch.session_store.saves == []
