"""v0.12 engine contract: workspace-aware startup and the safe ``switch_workspace`` transaction.

Ported from the v0.11 ``test_orchestrator_workspace.py``: the guarantees are the same, the subject is now
the real :class:`~app.game.orchestrator.GameOrchestrator` (real world, real runtime; only the storage is
faked).  Everything runs against an in-memory fake of ``app.storage.workspace`` (injected through
``orch.workspace_module``) and per-workspace fake stores, with every folder inside ``tmp_path``.
Nothing touches the real workspace, the user's directories or the project's ``.env`` / ``saves``.

What a "game" is here: ``orch.session`` (bindings, flags) + ``orch.world`` (the rules world) +
``orch.usage``.  A saved game is a real v3 save (``orch.session_payload()``) as the session store holds it.
"""

from __future__ import annotations

import asyncio
import copy
import sys
import threading
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from app.engine.llm import LLMClient
from app.game.orchestrator import GameOrchestrator
from app.game.session import SESSION_SCHEMA_VERSION, GameSession
from app.providers.models import ModelBinding, StorageError
from app.world.types import MODERATOR_ID
from game_fakes import BINDING, all_bound, async_test, bots_for, idle_moderator, real_orch

OTHER_BINDING = ModelBinding(provider_id="prov", model_id="small", reasoning_depth="low")
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


# ------------------------------------------------------------------------- fakes ---


@dataclass(frozen=True)
class FakeInfo:
    root: Path
    data_dir: Path
    id: str
    name: str


class FakeWorkspaces:
    """Just enough of ``app.storage.workspace`` (contract signatures) to drive the engine."""

    WORKSPACE_MARKER = ".arena-workspace.json"

    class WorkspaceError(ValueError):
        pass

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.enabled = True
        self.active: FakeInfo | None = None
        self.locked_by_others: set[Path] = set()
        self.data: dict[Path, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.fail_ensure: Exception | None = None
        self.ensure_report: dict[str, Any] = {"status": "ok"}
        self.default_root = tmp / "default"

    # ---- contract surface
    def workspace_enabled(self) -> bool:
        return self.enabled

    def get_active_workspace(self) -> FakeInfo | None:
        return self.active

    def get_active_workspace_payload(self, *, include_path: bool) -> dict[str, Any]:
        if self.active is None:
            return {"name": None, "id": None}
        payload: dict[str, Any] = {"name": self.active.name, "id": self.active.id}
        if include_path:
            payload["path"] = str(self.active.root)
        return payload

    def _info(self, root: Path) -> FakeInfo:
        return FakeInfo(root=root, data_dir=root / ".arena", id=f"id-{root.name}", name=root.name)

    def open_workspace(self, path: Path) -> FakeInfo:
        root = Path(path).resolve()
        if not (root / self.WORKSPACE_MARKER).is_file():
            raise self.WorkspaceError("not a workspace")
        self.calls.append(f"open:{root.name}")
        return self._info(root)

    def create_workspace(
        self, path: Path, *, name: str | None = None, initialize_existing: bool = False
    ) -> FakeInfo:
        root = Path(path).resolve()
        if root.exists() and any(root.iterdir()) and not initialize_existing:
            raise self.WorkspaceError("目录非空，必须确认初始化")
        root.mkdir(parents=True, exist_ok=True)
        (root / self.WORKSPACE_MARKER).write_text("{}", encoding="utf-8")
        (root / ".arena").mkdir(exist_ok=True)
        self.calls.append(f"create:{root.name}")
        return self._info(root)

    def activate_workspace(self, info: FakeInfo) -> None:
        if info.root in self.locked_by_others:
            raise self.WorkspaceError("工作文件夹正被另一个进程使用")
        self.data.setdefault(info.root, {"sessions": {}, "defaults": {}, "last": None})
        self.active = info
        self.calls.append(f"activate:{info.root.name}")

    def deactivate_workspace(self) -> None:
        self.calls.append("deactivate")
        self.active = None

    def ensure_initial_workspace(self) -> tuple[FakeInfo, dict[str, Any]]:
        self.calls.append("ensure_initial")
        if self.fail_ensure is not None:
            raise self.fail_ensure
        root = self.default_root.resolve()
        info = self.create_workspace(root) if not (root / self.WORKSPACE_MARKER).exists() else self._info(root)
        self.activate_workspace(info)
        return info, dict(self.ensure_report)

    # ---- helpers for tests
    def make_workspace(self, name: str) -> Path:
        root = (self.tmp / name).resolve()
        before = len(self.calls)
        self.create_workspace(root)
        del self.calls[before:]  # test setup is not part of the engine's call log
        self.data.setdefault(root, {"sessions": {}, "defaults": {}, "last": None})
        return root

    def store(self) -> dict[str, Any]:
        if self.active is None:
            raise RuntimeError("store used while no workspace is active")
        return self.data[self.active.root]


class WsSessions:
    """The encrypted session store of whatever workspace is active (``on_save`` runs in the save thread)."""

    def __init__(self, ws: FakeWorkspaces) -> None:
        self.ws = ws
        self.fail: str | None = None
        self.on_save: Callable[[Path, str], None] | None = None
        self.log: list[tuple[str, str]] = []  # (workspace name, session id)

    def save(self, session_id: str, payload: dict[str, Any]) -> None:
        if self.on_save:
            self.on_save(self.ws.active.root, session_id)  # type: ignore[union-attr]
        if self.fail:
            raise StorageError(self.fail)
        self.ws.store()["sessions"][session_id] = copy.deepcopy(payload)
        self.log.append((self.ws.active.root.name, session_id))  # type: ignore[union-attr]

    def load(self, session_id: str) -> dict[str, Any]:
        sessions = self.ws.store()["sessions"]
        if session_id not in sessions:
            raise StorageError("存档不存在或已损坏")
        return copy.deepcopy(sessions[session_id])


class WsSettings:
    """Default bindings + the last-session pointer of whatever workspace is active."""

    def __init__(self, ws: FakeWorkspaces) -> None:
        self.ws = ws

    def get_default_bindings(self) -> dict[str, ModelBinding]:
        return dict(self.ws.store()["defaults"])

    def save_default_bindings(self, bindings: dict[str, ModelBinding]) -> None:
        self.ws.store()["defaults"] = dict(bindings)

    def get_last_session_id(self) -> str | None:
        return self.ws.store()["last"]

    def set_last_session_id(self, session_id: str) -> None:
        self.ws.store()["last"] = session_id


class FakeLLM:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


@dataclass
class Env:
    orch: GameOrchestrator
    ws: FakeWorkspaces
    sessions: WsSessions
    settings: WsSettings
    old_llm: FakeLLM
    cache_clears: list[int]
    events: list[str]

    @property
    def root_a(self) -> Path:
        return (self.ws.tmp / "ws-a").resolve()


def make_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    activate: bool = True,
    paused: bool = True,
    record_events: bool = True,
) -> Env:
    """A real orchestrator wired to the workspace fakes; its current game is the recognizable "OLD GAME"."""
    ws = FakeWorkspaces(tmp_path)
    sessions, settings = WsSessions(ws), WsSettings(ws)
    if activate:
        root = ws.make_workspace("ws-a")
        ws.activate_workspace(ws._info(root))
        ws.calls.clear()
    orch = real_orch(monkeypatch)  # real world / runtime, in-memory providers
    orch.settings_store = settings
    orch.session_store = sessions
    orch.workspace_module = ws
    orch._autosave_enabled = True
    orch._autosave_delay = 0.01
    old_llm = FakeLLM()
    orch.llm = old_llm
    cache_clears: list[int] = []
    orch.store_cache_clearer = lambda: cache_clears.append(1)
    events: list[str] = []
    if record_events:

        async def record(event: str = "state_update") -> None:
            events.append(event)

        orch.broadcast_state = record  # type: ignore[method-assign]
    orch.world.state.phase = "OLD GAME"
    if paused:  # a game that was started and stopped
        orch.world.state.status = "paused"
        orch.session.status = "paused"
    return Env(orch, ws, sessions, settings, old_llm, cache_clears, events)


def seed_saved_game(
    monkeypatch: pytest.MonkeyPatch,
    ws: FakeWorkspaces,
    root: Path,
    *,
    phase: str = "TARGET GAME",
    status: str = "running",
    bindings: dict[str, ModelBinding] | None = None,
) -> dict[str, Any]:
    """Put a real v3 save into ``root``'s session store and point its last-session pointer at it."""
    payload = real_orch(monkeypatch).session_payload()
    payload["status"] = status
    payload["world"]["state"]["status"] = status
    payload["world"]["state"]["phase"] = phase
    payload["model_bindings"] = {
        actor: binding.model_dump(mode="json") for actor, binding in (all_bound() if bindings is None else bindings).items()
    }
    data = ws.data.setdefault(root, {"sessions": {}, "defaults": {}, "last": None})
    data["sessions"][payload["session_id"]] = payload
    data["last"] = payload["session_id"]
    return payload


def damaged_v3_save(session_id: str = "broken-game") -> dict[str, Any]:
    """A save that claims to be v3 but cannot be loaded (the "files are kept, recovery mode" case)."""
    return {"schema_version": SESSION_SCHEMA_VERSION, "session_id": session_id, "participants": "nope"}


# -------------------------------------------------------------------- the switch ---


@async_test
async def test_switch_saves_current_game_then_restores_the_targets_last_game_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    old_session, old_world = env.orch.session, env.orch.world
    generation = env.orch._session_generation
    target = env.ws.make_workspace("ws-b")
    saved = seed_saved_game(monkeypatch, env.ws, target)

    result = await env.orch.switch_workspace(target)

    # the old game went into the OLD workspace (and its pointer), never into the new one
    assert env.sessions.log == [("ws-a", old_session.session_id)]
    assert env.ws.data[env.root_a]["last"] == old_session.session_id
    assert old_session.session_id not in env.ws.data[target]["sessions"]
    stored = env.ws.data[env.root_a]["sessions"][old_session.session_id]
    assert stored["schema_version"] == SESSION_SCHEMA_VERSION and stored["world"]["state"]["phase"] == "OLD GAME"  # as it was
    # the target's last game is restored paused, flagged, with its own bindings
    session = env.orch.session
    assert session is result["session"] and session.session_id == saved["session_id"]
    assert env.orch.world.state.phase == "TARGET GAME" and session.status == "paused"
    assert env.orch.world is not old_world and not env.orch.world.running and env.orch.clock.paused
    assert env.orch.world.state.status == "paused"  # "running" in the file, never resumed by itself
    assert session.restored_from_save and session.restore_notice == "已恢复上次对局（已暂停）"
    assert session.model_bindings["p01"] == BINDING and session.binding_issues == []
    assert result["restored"] == saved["session_id"] and result["changed"] is True
    assert result["recovery_error"] is None
    assert result["workspace"]["name"] == "ws-b" and result["workspace"]["enabled"] is True
    assert env.ws.active.root == target and env.orch._supervisor is None
    assert env.orch._session_generation > generation  # a slow save of the old game can no longer move the pointer
    # runtime was rebuilt
    assert env.cache_clears == [1]
    assert isinstance(env.orch.llm, LLMClient) and env.orch.llm is not env.old_llm
    assert env.old_llm.closed == 1
    assert env.events[-1] == "workspace_changed" and env.events.count("workspace_changed") == 1
    assert not env.orch._switching_workspace


@async_test
async def test_target_without_a_saved_game_gets_a_fresh_idle_game_with_the_targets_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    old_world, old_session_id = env.orch.world, env.orch.session.session_id
    env.orch.session.model_bindings = {"p01": OTHER_BINDING}  # old workspace's binding
    env.ws.data[env.root_a]["defaults"] = {"p01": OTHER_BINDING}
    target = env.ws.make_workspace("ws-b")
    env.ws.data[target]["defaults"] = {"p02": BINDING, MODERATOR_ID: BINDING}

    result = await env.orch.switch_workspace(target)

    session = env.orch.session
    assert result["restored"] is None and session.status == "idle" and session.session_id != old_session_id
    assert set(session.model_bindings) == {"p02", MODERATOR_ID}  # target defaults, nothing carried over
    assert not session.restored_from_save
    assert env.orch.world is not old_world and env.orch.world.state.phase != "OLD GAME"
    assert env.orch.world.state.status == "idle"
    assert {i.actor_id for i in session.binding_issues} == {"p01", "p03", "p04"}
    assert env.orch._autosave_enabled
    # the old game (with its own binding) is safe in the old workspace
    saved_old = env.ws.data[env.root_a]["sessions"][old_session_id]
    assert saved_old["model_bindings"]["p01"]["model_id"] == "small"


@async_test
async def test_a_failed_save_of_the_current_game_aborts_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    target = env.ws.make_workspace("ws-b")
    old_session, old_world, old_llm = env.orch.session, env.orch.world, env.orch.llm
    env.sessions.fail = "磁盘已满"

    with pytest.raises(StorageError, match="磁盘已满"):
        await env.orch.switch_workspace(target)

    assert env.ws.active.root == env.root_a
    assert env.orch.session is old_session and env.orch.world is old_world and env.orch.llm is old_llm
    assert env.ws.calls == []  # target was never even opened, let alone activated
    assert env.cache_clears == [] and "workspace_changed" not in env.events
    assert "磁盘已满" in old_session.autosave_error  # the user is told, the game is not lost
    assert not env.orch._switching_workspace


@async_test
async def test_a_locked_or_invalid_target_leaves_the_current_workspace_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    old_session, old_world, old_llm = env.orch.session, env.orch.world, env.orch.llm
    locked = env.ws.make_workspace("ws-locked")
    env.ws.locked_by_others.add(locked)

    with pytest.raises(FakeWorkspaces.WorkspaceError, match="另一个进程"):
        await env.orch.switch_workspace(locked)

    assert env.ws.active.root == env.root_a
    assert env.orch.session is old_session and env.orch.world is old_world and env.orch.llm is old_llm
    assert env.old_llm.closed == 0 and "workspace_changed" not in env.events
    assert not env.orch._switching_workspace and not env.orch._recovery_pending
    assert not env.orch._workspace_failed
    # and it is still fully usable afterwards (autosave still lands in the right workspace)
    await env.orch.persist_session("manual")
    assert env.sessions.log[-1] == ("ws-a", old_session.session_id)
    assert env.ws.data[locked]["sessions"] == {} and env.ws.data[locked]["last"] is None  # nothing written there

    # an invalid (not a workspace) target changes nothing either, and nothing is created behind our back
    plain = tmp_path / "just-a-folder"
    plain.mkdir()
    with pytest.raises(FakeWorkspaces.WorkspaceError, match="创建/初始化"):
        await env.orch.switch_workspace(plain)
    assert env.ws.active.root == env.root_a and not (plain / FakeWorkspaces.WORKSPACE_MARKER).exists()
    assert env.orch.session is old_session and env.old_llm.closed == 0


@async_test
async def test_failure_after_activation_rolls_back_to_the_previous_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    old_session, old_world = env.orch.session, env.orch.world
    target = env.ws.make_workspace("ws-b")
    seed_saved_game(monkeypatch, env.ws, target)
    attempts: list[int] = []

    def flaky_clear() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("cannot reset stores")

    env.orch.store_cache_clearer = flaky_clear

    with pytest.raises(RuntimeError, match="cannot reset stores"):
        await env.orch.switch_workspace(target)

    assert env.ws.calls[-2:] == ["activate:ws-b", "activate:ws-a"]  # rolled back
    assert env.ws.active.root == env.root_a
    assert len(attempts) == 2  # caches were cleared again for the restored workspace
    assert env.orch.session is old_session and env.orch.world is old_world  # not half-switched
    assert env.orch.llm is env.old_llm and env.old_llm.closed == 0 and "workspace_changed" not in env.events
    assert not env.orch._workspace_failed and not env.orch._switching_workspace
    # the target's game was never loaded, and nothing of it was written
    assert env.orch.session.session_id != env.ws.data[target]["last"]
    assert env.orch.workspace_payload()["degraded"] is False


@async_test
async def test_a_failed_rollback_leaves_a_clearly_degraded_state_that_nothing_can_save_into(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    old_session = env.orch.session
    target = env.ws.make_workspace("ws-b")
    other = env.ws.make_workspace("ws-c")

    def clear_always_fails() -> None:
        raise RuntimeError("cannot reset stores")

    real_activate = env.ws.activate_workspace

    def activate(info: FakeInfo) -> None:
        if info.root == env.root_a and "activate:ws-b" in env.ws.calls:
            raise FakeWorkspaces.WorkspaceError("旧工作文件夹已被其他进程占用")  # cannot go back
        real_activate(info)

    env.orch.store_cache_clearer = clear_always_fails
    env.ws.activate_workspace = activate  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="cannot reset stores"):  # the original failure is what the caller sees
        await env.orch.switch_workspace(target)

    # we do not pretend everything is fine: the app is degraded and refuses to save into who-knows-where
    assert env.orch._workspace_failed and env.orch._recovery_pending
    assert env.orch.session is old_session and env.orch.llm is env.old_llm  # nothing was swapped
    payload = env.orch.workspace_payload()
    assert payload["degraded"] is True and payload["status"] == "degraded" and payload["last_session"] is None
    assert await env.orch.persist_session("autosave", raise_errors=False) is False
    with pytest.raises(RuntimeError, match="工作文件夹不可用"):
        await env.orch.persist_session("manual")
    with pytest.raises(RuntimeError, match="不可用"):
        await env.orch.start_or_resume()
    log_before = list(env.sessions.log)

    # picking a usable workspace recovers (the old game is NOT saved into a workspace of unknown state)
    env.ws.activate_workspace = real_activate  # type: ignore[method-assign]
    env.orch.store_cache_clearer = lambda: env.cache_clears.append(1)
    result = await env.orch.switch_workspace(other)
    assert result["changed"] and env.ws.active.root == other
    assert not env.orch._workspace_failed and not env.orch._recovery_pending
    assert env.sessions.log == log_before and env.orch.workspace_payload()["degraded"] is False


@async_test
async def test_running_game_blocks_the_switch_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    target = env.ws.make_workspace("ws-b")
    env.orch.session.status = "running"
    with pytest.raises(RuntimeError, match="暂停"):
        await env.orch.switch_workspace(target)

    env.orch.session.status = "paused"
    env.orch._supervisor = object()  # type: ignore[assignment]  # a live supervisor counts as "running"
    try:
        with pytest.raises(RuntimeError, match="暂停"):
            await env.orch.switch_workspace(target)
    finally:
        env.orch._supervisor = None

    assert env.sessions.log == [] and env.ws.calls == [] and env.cache_clears == []
    assert env.ws.active.root == env.root_a and "workspace_changed" not in env.events


@async_test
async def test_a_really_running_game_blocks_the_switch_and_a_paused_one_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path, paused=False)
    env.orch._autosave_enabled = False  # the running game must not add saves of its own to this check
    bots_for(env.orch, idle_moderator)  # scripted agents: no network, the moderator just waits
    target = env.ws.make_workspace("ws-b")
    started = await env.orch.start_or_resume()
    try:
        assert started.status == "running" and env.orch._supervisor is not None and env.orch.world.running
        with pytest.raises(RuntimeError, match="暂停"):  # the API answers 409 for this
            await env.orch.switch_workspace(target)
        assert env.sessions.log == [] and env.ws.calls == [] and env.ws.active.root == env.root_a
        assert env.orch.session is started and env.orch._supervisor is not None  # the game was not disturbed
    finally:
        await env.orch.pause()

    assert env.orch.session.status == "paused" and env.orch._supervisor is None
    result = await env.orch.switch_workspace(target)  # paused: now it is allowed
    assert result["changed"] and env.ws.active.root == target
    assert env.sessions.log == [("ws-a", started.session_id)]  # the paused game was saved into its own workspace


@async_test
async def test_a_restored_game_can_be_resumed_after_the_switch_and_then_saves_into_the_new_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    bots_for(env.orch, idle_moderator)  # scripted agents: the moderator just waits
    target = env.ws.make_workspace("ws-b")
    saved = seed_saved_game(monkeypatch, env.ws, target, status="paused")
    old_session_id = env.orch.session.session_id
    await env.orch.switch_workspace(target)
    assert env.orch.session.session_id == saved["session_id"] and env.orch.session.status == "paused"
    env.sessions.log.clear()

    session = await env.orch.start_or_resume()  # the user presses "continue"
    try:
        assert session.status == "running" and env.orch._supervisor is not None and env.orch.world.running
        assert not env.orch.clock.paused  # the game clock runs again
        await asyncio.sleep(0.3)  # the debounced autosaves of a running game
    finally:
        await env.orch.pause()
    await env.orch.flush_autosave("test")

    assert env.orch.session.status == "paused" and env.orch.clock.paused
    assert env.sessions.log and all(entry == ("ws-b", saved["session_id"]) for entry in env.sessions.log)
    assert env.ws.data[target]["last"] == saved["session_id"]
    assert saved["session_id"] not in env.ws.data[env.root_a]["sessions"]  # the other workspace is untouched
    assert env.ws.data[env.root_a]["sessions"][old_session_id]["world"]["state"]["phase"] == "OLD GAME"


@async_test
async def test_a_game_started_after_the_switch_writes_its_trace_log_into_the_new_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    # the data directory of the active workspace is <workspace>/.arena; the trace goes to <data dir>/logs
    monkeypatch.setattr("app.storage.paths.get_data_dir", lambda: env.ws.active.data_dir)
    env.orch._autosave_enabled = False
    bots_for(env.orch, idle_moderator)
    target = env.ws.make_workspace("ws-b")
    saved = seed_saved_game(monkeypatch, env.ws, target, status="paused")

    await env.orch.switch_workspace(target)
    await env.orch.start_or_resume()
    try:
        await asyncio.sleep(0.3)  # the moderator is woken with "game.resumed" and answers
    finally:
        await env.orch.pause()

    files = sorted((target / ".arena" / "logs" / saved["session_id"]).glob("trace-*.jsonl"))
    assert files and '"event":"wake"' in files[0].read_text(encoding="utf-8")
    assert not (env.root_a / ".arena" / "logs").exists()  # nothing of this game was logged into the old workspace


@async_test
async def test_switching_to_the_active_workspace_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    session, world = env.orch.session, env.orch.world

    result = await env.orch.switch_workspace(tmp_path / "ws-a")

    assert result["changed"] is False and env.orch.session is session and env.orch.world is world
    assert env.cache_clears == [] and env.old_llm.closed == 0 and env.orch.llm is env.old_llm
    assert "workspace_changed" not in env.events
    assert result["restored"] is None and not env.orch._switching_workspace


@async_test
async def test_damaged_last_game_in_the_target_opens_in_recovery_and_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the real broadcast is used here: "workspace_changed" normally schedules an autosave of the new game
    env = make_env(monkeypatch, tmp_path, record_events=False)
    old_session_id = env.orch.session.session_id
    target = env.ws.make_workspace("ws-b")
    env.ws.data[target]["last"] = "broken-game"
    env.ws.data[target]["sessions"]["broken-game"] = damaged_v3_save()

    result = await env.orch.switch_workspace(target)  # the workspace itself is fine: it switches
    await asyncio.sleep(0.1)  # (an autosave would have fired by now: the debounce is 0.01 s)

    assert result["changed"] and result["restored"] is None and result["recovery_error"]
    assert env.orch._recovery_pending and "已保留" in env.orch.session.restore_notice
    assert "broken-game" in env.orch.session.restore_notice
    assert env.orch.session.status == "idle" and not env.orch.session.restored_from_save
    before = copy.deepcopy(env.ws.data[target])
    env.orch.schedule_autosave("status_change")  # nothing may be scheduled ...
    await env.orch.flush_autosave()  # ... and an explicit flush refuses too
    await asyncio.sleep(0.05)
    assert env.ws.data[target] == before  # placeholder never written over it
    assert env.ws.data[target]["last"] == "broken-game"  # pointer untouched
    assert env.sessions.log == [("ws-a", old_session_id)]  # only the pre-switch save, into A


@async_test
async def test_a_last_game_pointer_to_a_missing_file_is_kept_and_flagged_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    target = env.ws.make_workspace("ws-b")
    env.ws.data[target]["last"] = "vanished-game"  # the pointer names a file that is gone

    result = await env.orch.switch_workspace(target)

    assert result["changed"] and result["restored"] is None and env.orch._recovery_pending
    assert "vanished-game" in env.orch.session.restore_notice and env.ws.data[target]["last"] == "vanished-game"
    assert env.ws.data[target]["sessions"] == {}  # and nothing was invented to replace it


@async_test
async def test_an_older_engine_save_in_the_target_is_kept_untouched_and_a_fresh_game_is_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3: a v0.11 save is not a failure state — no recovery mode, the user is told, saving the new game works."""
    env = make_env(monkeypatch, tmp_path)
    target = env.ws.make_workspace("ws-b")
    old = {"session_id": "oldsave1", "schema_version": 2, "status": "paused", "records": [{"id": "rec_0001"}]}
    env.ws.data[target]["sessions"]["oldsave1"] = copy.deepcopy(old)
    env.ws.data[target]["last"] = "oldsave1"

    result = await env.orch.switch_workspace(target)

    assert result["changed"] and result["restored"] is None and result["recovery_error"] is None
    assert not env.orch._recovery_pending and env.orch.session.status == "idle"
    assert "旧版本" in env.orch.session.restore_notice and "不兼容" in env.orch.session.restore_notice
    assert env.ws.data[target]["sessions"]["oldsave1"] == old  # never modified
    await env.orch.persist_session("progress")  # saving the new game is fine; the pointer moves to it
    assert env.ws.data[target]["last"] == env.orch.session.session_id
    assert env.ws.data[target]["sessions"]["oldsave1"] == old


@async_test
async def test_a_finished_game_is_restored_as_finished_not_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    target = env.ws.make_workspace("ws-b")
    saved = seed_saved_game(monkeypatch, env.ws, target, phase="THE END", status="finished")

    result = await env.orch.switch_workspace(target)

    assert result["restored"] == saved["session_id"]
    assert env.orch.session.status == "finished" and env.orch.world.state.status == "finished"
    assert not env.orch.world.running and env.orch.clock.paused and env.orch._supervisor is None
    assert env.orch.session.restore_notice == "已恢复上次对局（已结束）"


@async_test
async def test_leaving_a_workspace_in_recovery_mode_never_saves_the_placeholder_game(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    broken = env.ws.make_workspace("ws-b")
    env.ws.data[broken]["last"] = "broken-game"
    env.ws.data[broken]["sessions"]["broken-game"] = damaged_v3_save()
    await env.orch.switch_workspace(broken)
    assert env.orch._recovery_pending
    saves_before = list(env.sessions.log)
    files_before = copy.deepcopy(env.ws.data[broken])

    result = await env.orch.switch_workspace(env.ws.make_workspace("ws-c"))  # keep going: nothing to save first

    assert result["changed"] and env.ws.active.root == (tmp_path / "ws-c").resolve()
    assert env.sessions.log == saves_before  # the empty placeholder was not written into the damaged workspace
    assert env.ws.data[broken] == files_before
    assert not env.orch._recovery_pending  # a healthy workspace clears recovery mode


@async_test
async def test_the_world_usage_and_bindings_of_the_old_workspace_never_leak_into_the_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    old_usage = env.orch.usage
    old_usage.record_usage("p01", prompt_tokens=100, completion_tokens=10)
    env.orch.session.model_bindings = {"p01": OTHER_BINDING}
    first_session = env.orch.session
    target = env.ws.make_workspace("ws-b")

    await env.orch.switch_workspace(target)  # a -> b: a fresh game in b

    assert env.orch.usage is not old_usage and env.orch.usage.total_tokens == 0  # accounting is per game
    assert env.orch.session.model_bindings == {} and env.orch.session.session_id != first_session.session_id
    second_session = env.orch.session
    env.orch.usage.record_usage("p02", prompt_tokens=7, completion_tokens=1)
    env.orch.world.state.phase = "IN B"

    await env.orch.switch_workspace(env.root_a)  # b -> a: a's own game comes back with its own numbers

    assert env.orch.session.session_id == first_session.session_id
    assert env.orch.usage.total_tokens == 110 and set(env.orch.usage.by_actor) == {"p01"}
    assert env.orch.session.model_bindings == {"p01": OTHER_BINDING}
    stored_b = env.ws.data[target]["sessions"][second_session.session_id]
    assert stored_b["usage_stats"]["total_tokens"] == 8 and stored_b["world"]["state"]["phase"] == "IN B"


# ---------------------------------------------------- how the target is opened/created ---


@async_test
async def test_missing_or_unmarked_folders_need_an_explicit_create_or_initialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    missing = tmp_path / "does-not-exist"
    with pytest.raises(FakeWorkspaces.WorkspaceError, match="创建/初始化"):
        await env.orch.switch_workspace(missing)
    assert not missing.exists()  # nothing was created behind the user's back

    result = await env.orch.switch_workspace(missing, create_if_missing=True)
    assert result["workspace"]["name"] == "does-not-exist" and (missing / ".arena-workspace.json").is_file()

    # an existing non-empty folder is only adopted with initialize_existing, files untouched
    docs = tmp_path / "my-docs"
    docs.mkdir()
    (docs / "thesis.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(FakeWorkspaces.WorkspaceError):
        await env.orch.switch_workspace(docs, create_if_missing=True)
    assert env.ws.active.root == missing.resolve()  # still where we were
    await env.orch.switch_workspace(docs, initialize_existing=True)
    assert env.ws.active.root == docs.resolve()
    assert (docs / "thesis.txt").read_text(encoding="utf-8") == "keep me"


@async_test
async def test_relative_paths_and_a_disabled_feature_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    with pytest.raises(FakeWorkspaces.WorkspaceError, match="绝对路径"):
        await env.orch.switch_workspace("relative/dir")
    assert env.sessions.log == []  # rejected before anything was saved or touched
    env.ws.enabled = False
    with pytest.raises(ValueError, match="未启用"):
        await env.orch.switch_workspace(tmp_path / "ws-a")


@async_test
async def test_the_old_workspace_is_never_modified_by_a_switch_away_and_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    first_session = env.orch.session
    first_seq = env.orch.world.seq
    target = env.ws.make_workspace("ws-b")

    await env.orch.switch_workspace(target)  # a -> b (fresh game in b)
    second_session = env.orch.session
    env.orch.world.state.phase = "IN B"
    env.orch.world.state.status = "paused"
    env.orch.session.status = "paused"
    await env.orch.switch_workspace(env.root_a)  # b -> a

    assert env.orch.session.session_id == first_session.session_id  # a's last game is back
    assert env.orch.world.state.phase == "OLD GAME" and env.orch.world.seq == first_seq
    assert env.ws.data[target]["sessions"][second_session.session_id]["world"]["state"]["phase"] == "IN B"
    assert first_session.session_id not in env.ws.data[target]["sessions"]
    assert second_session.session_id not in env.ws.data[env.root_a]["sessions"]  # no cross-writes
    assert env.ws.data[env.root_a]["last"] == first_session.session_id
    assert env.ws.data[target]["last"] == second_session.session_id
    assert [name for name, _ in env.sessions.log] == ["ws-a", "ws-b"]  # one save per workspace left, each in its own


# ----------------------------------------------------- serialization / isolation ---


@async_test
async def test_no_autosave_of_the_old_game_can_land_in_the_new_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    old_session = env.orch.session
    target = env.ws.make_workspace("ws-b")
    env.orch._autosave_delay = 30
    env.orch.schedule_autosave("scenario_step_done")  # a pending (30 s) autosave timer of the old game
    pending = env.orch._autosave_task
    assert pending is not None and not pending.done()

    scheduled_during_switch: list[bool] = []

    def poke(root: Path, session_id: str) -> None:
        # runs inside the pre-switch save (a worker thread), while the switch is in progress
        env.orch.schedule_autosave("during-switch")
        scheduled_during_switch.append(env.orch._autosave_dirty)

    env.sessions.on_save = poke
    await env.orch.switch_workspace(target)

    assert pending.done() and scheduled_during_switch == [False]  # nothing scheduled mid-switch
    assert env.sessions.log == [("ws-a", old_session.session_id)]  # exactly one save, to A
    assert old_session.session_id not in env.ws.data[target]["sessions"]


@async_test
async def test_a_save_requested_during_the_switch_waits_and_lands_in_the_new_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    old_session = env.orch.session
    target = env.ws.make_workspace("ws-b")
    entered, release = threading.Event(), threading.Event()

    def block_first_save(root: Path, session_id: str) -> None:
        if not entered.is_set():  # only the switch's own save of the old game blocks
            entered.set()
            assert release.wait(10)

    env.sessions.on_save = block_first_save
    first = asyncio.create_task(env.orch.switch_workspace(target))
    while not entered.is_set():
        await asyncio.sleep(0.005)

    late = asyncio.create_task(env.orch.persist_session("late"))  # e.g. a manual save from the API
    await asyncio.sleep(0.1)
    assert not late.done()  # the switch owns the persist lock: no interleaving, no write into a half state
    release.set()
    await first
    assert await asyncio.wait_for(late, 5) is True

    new_session = env.orch.session
    assert new_session.session_id != old_session.session_id
    assert env.sessions.log == [("ws-a", old_session.session_id), ("ws-b", new_session.session_id)]
    assert old_session.session_id not in env.ws.data[target]["sessions"]  # the old game stayed in A
    assert env.ws.data[target]["last"] == new_session.session_id


@async_test
async def test_a_switch_waits_for_an_autosave_that_is_already_writing_and_never_redirects_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    session_id = env.orch.session.session_id
    target = env.ws.make_workspace("ws-b")
    entered, release = threading.Event(), threading.Event()

    def block_first_save(root: Path, sid: str) -> None:
        if not entered.is_set():  # the autosave that is in flight when the user switches
            entered.set()
            assert release.wait(10)

    env.sessions.on_save = block_first_save
    env.orch.schedule_autosave("progress")
    while not entered.is_set():
        await asyncio.sleep(0.005)
    assert env.orch._persisting  # the encrypted write of the old game is under way, in A

    switching = asyncio.create_task(env.orch.switch_workspace(target))
    await asyncio.sleep(0.1)
    assert not switching.done()  # the switch does not run over the write ...
    assert env.ws.active.root == env.root_a and env.ws.calls == []  # ... nor touch the workspace meanwhile
    release.set()
    result = await asyncio.wait_for(switching, 5)

    assert result["changed"] and env.ws.active.root == target
    assert env.sessions.log == [("ws-a", session_id), ("ws-a", session_id)]  # the autosave, then the switch's own
    assert session_id not in env.ws.data[target]["sessions"]  # neither of them landed in the new workspace


@async_test
async def test_after_the_switch_the_new_game_autosaves_into_the_new_workspace_and_the_bus_is_told(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path, record_events=False)  # the real broadcast: events go to orch.bus
    old_session = env.orch.session
    target = env.ws.make_workspace("ws-b")
    _, queue = env.orch.bus.subscribe()

    await env.orch.switch_workspace(target)
    await asyncio.sleep(0.15)  # the autosave debounce (0.01 s) of "workspace_changed"

    new_session = env.orch.session
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    types_seen = [e.type for e in events]
    assert "workspace.changed" in types_seen and types_seen.index("workspace.changed") < types_seen.index("snapshot")
    snapshot = next(e for e in events if e.type == "snapshot")
    assert snapshot.data["session"]["session_id"] == new_session.session_id  # spectators see the new game
    assert env.sessions.log[0] == ("ws-a", old_session.session_id)  # the old game went to A first ...
    assert env.sessions.log[1:] and all(entry == ("ws-b", new_session.session_id) for entry in env.sessions.log[1:])
    assert env.ws.data[target]["last"] == new_session.session_id  # ... and everything after went to B


@async_test
async def test_mutating_operations_and_a_second_switch_are_refused_while_switching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    target = env.ws.make_workspace("ws-b")
    entered, release = threading.Event(), threading.Event()

    def block(root: Path, session_id: str) -> None:
        entered.set()
        assert release.wait(10)

    env.sessions.on_save = block
    first = asyncio.create_task(env.orch.switch_workspace(target))
    while not entered.is_set():
        await asyncio.sleep(0.005)

    other = env.ws.make_workspace("ws-c")
    with pytest.raises(RuntimeError, match="正在切换"):
        await env.orch.switch_workspace(other)
    with pytest.raises(RuntimeError, match="正在切换"):
        await env.orch.new_game()
    with pytest.raises(RuntimeError, match="正在切换"):
        await env.orch.start_or_resume()
    with pytest.raises(RuntimeError, match="正在切换"):
        await env.orch.update_model_bindings({"p01": BINDING})
    with pytest.raises(RuntimeError, match="正在切换"):
        await env.orch.load_session_data(GameSession().model_dump(mode="json"))

    release.set()
    await first
    assert env.ws.active.root == target and not env.orch._switching_workspace
    assert env.ws.data[target]["defaults"] == {}  # the refused binding update wrote nothing
    assert env.orch.session.model_bindings == {}  # ... and the refused import / new game changed nothing


@async_test
async def test_ui_chosen_reasoning_effort_survives_the_new_llm_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    old = LLMClient(env.orch.cfg, provider_store=env.orch.provider_store)
    old.reasoning_effort = "xhigh"
    env.orch.llm = old
    await env.orch.switch_workspace(env.ws.make_workspace("ws-b"))
    assert env.orch.llm is not old and env.orch.llm.reasoning_effort == "xhigh"
    assert env.orch.llm.provider_store is env.orch.provider_store
    await old.aclose()


@async_test
async def test_a_failing_close_of_the_old_llm_client_does_not_fail_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)

    async def boom() -> None:
        raise RuntimeError("close failed")

    env.old_llm.aclose = boom  # type: ignore[method-assign]
    result = await env.orch.switch_workspace(env.ws.make_workspace("ws-b"))

    assert result["changed"] and env.ws.active.root == (tmp_path / "ws-b").resolve()
    assert env.orch.llm is not env.old_llm and env.events[-1] == "workspace_changed"  # the switch completed


# ---------------------------------------------------------------------- startup ---


def fake_migration(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> None:
    module = types.ModuleType("app.migration")

    def migrate_legacy_data(base_dir: Path, cfg: dict[str, Any]) -> dict[str, Any]:
        order.append("migrate")  # recorded only; never touches the disk
        return {"status": "ok", "sessions_migrated": 0}

    module.migrate_legacy_data = migrate_legacy_data  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.migration", module)


def startup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Env, list[str]]:
    env = make_env(monkeypatch, tmp_path, activate=False, paused=False)  # the constructor's pristine placeholder
    env.orch._autosave_enabled = False
    env.orch.reload_config = lambda: True  # type: ignore[method-assign]
    order: list[str] = []
    fake_migration(monkeypatch, order)
    real_ensure = env.ws.ensure_initial_workspace

    def ensure() -> Any:
        order.append("ensure_initial")
        return real_ensure()

    env.ws.ensure_initial_workspace = ensure  # type: ignore[method-assign]
    env.orch.store_cache_clearer = lambda: order.append("clear_caches")
    return env, order


@async_test
async def test_startup_activates_the_workspace_before_migration_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, order = startup_env(tmp_path, monkeypatch)
    default_root = env.ws.default_root.resolve()
    env.ws.make_workspace("default")  # the default workspace exists with a saved game and defaults
    saved = seed_saved_game(monkeypatch, env.ws, default_root, phase="LAST TIME")
    env.ws.data[default_root]["defaults"] = {"p01": BINDING}
    env.orch.world.state.phase = "placeholder"

    report = await env.orch.startup()

    assert order == ["ensure_initial", "clear_caches", "migrate"]
    assert report["workspace"] == {"status": "ok"} and report["recovery_error"] is None
    assert report["restored"] == saved["session_id"]
    assert env.orch.world.state.phase == "LAST TIME" and env.orch.session.status == "paused"
    assert env.orch.session.restored_from_save and env.orch._supervisor is None
    assert env.orch._autosave_enabled and not env.orch._workspace_failed
    assert env.ws.active.root == default_root


@async_test
async def test_startup_rebuilds_the_fresh_game_with_the_workspaces_default_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, order = startup_env(tmp_path, monkeypatch)
    env.ws.make_workspace("default")
    env.ws.data[env.ws.default_root.resolve()]["defaults"] = {"p02": BINDING}
    env.orch.session.model_bindings = {"p01": OTHER_BINDING}  # came from the pre-workspace dir

    report = await env.orch.startup()

    assert report["restored"] is None
    assert set(env.orch.session.model_bindings) == {"p02"}  # not the stale pre-workspace default
    assert {i.actor_id for i in env.orch.session.binding_issues} == {MODERATOR_ID, "p01", "p03", "p04"}
    assert env.orch._autosave_enabled


@pytest.mark.parametrize("mode", ["raises", "reports_error"])
@async_test
async def test_workspace_failure_at_startup_degrades_without_touching_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    env, order = startup_env(tmp_path, monkeypatch)
    if mode == "raises":
        env.ws.fail_ensure = env.ws.WorkspaceError("旧数据复制失败")
    else:
        env.ws.ensure_report = {"status": "failed", "error": "目标 .arena 已损坏"}

    report = await env.orch.startup()  # must not raise

    assert "migrate" not in order  # legacy migration did not run against an undefined workspace
    assert report["recovery_error"] and env.orch._recovery_pending and env.orch._workspace_failed
    assert not env.orch._autosave_enabled
    assert "未被改动" in env.orch.session.restore_notice or "未改动" in env.orch.session.restore_notice
    # nothing can be saved, bound or started into the legacy global directory ...
    with pytest.raises(RuntimeError, match="工作文件夹不可用"):
        await env.orch.persist_session("manual")
    assert await env.orch.persist_session("autosave", raise_errors=False) is False
    with pytest.raises(RuntimeError, match="不可用"):
        await env.orch.update_model_bindings({"p01": BINDING})
    with pytest.raises(RuntimeError, match="不可用"):
        await env.orch.start_or_resume()
    assert env.sessions.log == []
    payload = env.orch.workspace_payload()
    assert payload["degraded"] is True and payload["status"] == "degraded" and payload["last_session"] is None

    # ... but the user can recover by choosing a workspace (nothing to save first)
    target = env.ws.make_workspace("rescue")
    result = await env.orch.switch_workspace(target)
    assert result["changed"] and not env.orch._workspace_failed and not env.orch._recovery_pending
    assert env.orch._autosave_enabled and env.orch.workspace_payload()["degraded"] is False
    assert env.sessions.log == []  # the placeholder game was not "saved" anywhere on the way


@async_test
async def test_startup_without_the_workspace_feature_keeps_the_v010_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, order = startup_env(tmp_path, monkeypatch)
    env.ws.enabled = False

    report = await env.orch.startup()

    assert order == ["migrate"] and "ensure_initial" not in env.ws.calls
    assert report["workspace"] is None and env.ws.active is None
    assert env.orch.workspace_payload() == {"enabled": False}
    with pytest.raises(ValueError, match="未启用"):
        await env.orch.switch_workspace(tmp_path / "x")


def test_missing_storage_workspace_module_means_feature_off(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = GameOrchestrator.__new__(GameOrchestrator)
    monkeypatch.setitem(sys.modules, "app.storage.workspace", None)  # import raises ImportError
    assert orch._ws() is None and orch._workspace_enabled() is False
    assert orch.workspace_payload() == {"enabled": False}


def test_a_workspace_module_that_fails_its_enabled_check_counts_as_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch = GameOrchestrator.__new__(GameOrchestrator)
    ws = FakeWorkspaces(tmp_path)
    orch.workspace_module = ws

    def broken() -> bool:
        raise OSError("settings unreadable")

    ws.workspace_enabled = broken  # type: ignore[method-assign]
    assert orch._workspace_enabled() is False  # "off", not a crash
    assert orch.workspace_payload() == {"enabled": False}


# --------------------------------------------------------------- payload / shutdown ---


@async_test
async def test_workspace_payload_hides_the_path_for_remote_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    full = env.orch.workspace_payload(include_path=True)
    remote = env.orch.workspace_payload(include_path=False)

    assert full["enabled"] is True and full["name"] == "ws-a" and full["path"].endswith("ws-a")
    assert full["last_session"] == env.orch.session.session_id and full["status"] == "ok"
    assert full["degraded"] is False and full["id"] == "id-ws-a"
    assert "path" not in remote and remote["name"] == "ws-a"
    assert str(tmp_path) not in str(remote)


@async_test
async def test_an_unreadable_active_workspace_still_reports_the_feature_as_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)

    def broken(*, include_path: bool) -> dict[str, Any]:
        raise OSError("marker unreadable")

    env.ws.get_active_workspace_payload = broken  # type: ignore[method-assign]
    payload = env.orch.workspace_payload()

    assert payload["enabled"] is True and payload["name"] is None and payload["id"] is None  # no crash
    assert payload["degraded"] is False and payload["last_session"] == env.orch.session.session_id


@async_test
async def test_shutdown_saves_into_the_active_workspace_then_releases_its_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    order: list[str] = []
    env.sessions.on_save = lambda root, sid: order.append("save")
    original = env.ws.deactivate_workspace
    env.ws.deactivate_workspace = lambda: (order.append("deactivate"), original())[1]  # type: ignore[method-assign]

    await env.orch.shutdown()

    assert order == ["save", "deactivate"]
    assert env.sessions.log == [("ws-a", env.orch.session.session_id)]
    assert env.old_llm.closed == 1 and env.ws.active is None
    assert env.ws.data[env.root_a]["last"] == env.orch.session.session_id


@async_test
async def test_shutdown_with_the_feature_disabled_never_touches_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(monkeypatch, tmp_path)
    env.ws.enabled = False
    await env.orch.shutdown()
    assert "deactivate" not in env.ws.calls
