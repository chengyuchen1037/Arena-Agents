"""v0.11 engine contract: workspace-aware startup and the safe ``switch_workspace`` transaction.

Everything here runs against an in-memory fake of ``app.storage.workspace`` (injected through
``orch.workspace_module``) and per-workspace fake stores, with all folders inside ``tmp_path``.
Nothing touches the real workspace, user directories or the project's ``.env`` / ``saves``.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import sys
import tempfile
import threading
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from app.engine.llm import LLMClient
from app.engine.orchestrator import GameOrchestrator
from app.engine.state import Channel, GameSession, Participant
from app.providers.models import (
    ModelBinding,
    ModelEntry,
    ProviderConfig,
    ProviderNotFound,
    StorageError,
)


def async_test(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


CFG = {
    "prompts": {"moderator_system": "__x.md", "participant_system": "__y.md"},
    "knowledge_base": {"docs_dir": str(Path(tempfile.gettempdir()) / "arena_test_missing_knowledge")},
    "participants": [
        {"id": "p01", "name": "N1", "seat": "S1"},
        {"id": "p02", "name": "N2", "seat": "S2"},
    ],
}
BINDING = ModelBinding(provider_id="prov", model_id="m")
OTHER_BINDING = ModelBinding(provider_id="prov", model_id="m", reasoning_depth="low")
PROVIDER = ProviderConfig(
    id="prov", name="prov", protocol="openai_chat", base_url="https://example.invalid", api_key="k",
    models=[ModelEntry(id="m", input_price_per_m=1.0, output_price_per_m=1.0)],
)


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


class Providers:
    def resolve(self, binding: ModelBinding) -> tuple[ProviderConfig, ModelEntry]:
        if binding.provider_id != "prov":
            raise ProviderNotFound(binding.provider_id)
        return PROVIDER, PROVIDER.models[0]


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


def make_env(tmp_path: Path, *, activate: bool = True) -> Env:
    ws = FakeWorkspaces(tmp_path)
    sessions, settings = WsSessions(ws), WsSettings(ws)
    orch = GameOrchestrator.__new__(GameOrchestrator)
    orch.cfg = CFG
    orch.scenario = None
    orch.llm = FakeLLM()
    orch._subscribers = set()
    orch.provider_store = Providers()
    orch.settings_store = settings
    orch.session_store = sessions
    orch.workspace_module = ws
    orch._autosave_enabled = True
    orch._autosave_delay = 0.01
    cache_clears: list[int] = []
    orch.store_cache_clearer = lambda: cache_clears.append(1)
    events: list[str] = []

    async def record(event: str = "state_update") -> None:
        events.append(event)

    orch.broadcast_state = record  # type: ignore[method-assign]
    old_llm = orch.llm
    if activate:
        root = ws.make_workspace("ws-a")
        ws.activate_workspace(ws._info(root))
        ws.calls.clear()
    orch.session = GameSession(
        status="paused",
        participants=[Participant(id="p01", seat="S1", name="N1"), Participant(id="p02", seat="S2", name="N2")],
        channels=[Channel(id="public", name="P", type="public", members=["p01", "p02"])],
        phase_text="OLD GAME",
    )
    return Env(orch, ws, sessions, settings, old_llm, cache_clears, events)


def seed_saved_game(ws: FakeWorkspaces, root: Path, *, phase: str = "TARGET GAME", status: str = "running") -> GameSession:
    game = GameSession(
        status=status,  # type: ignore[arg-type]
        participants=[Participant(id="p01", seat="S1", name="N1"), Participant(id="p02", seat="S2", name="N2")],
        phase_text=phase,
        model_bindings={"p01": BINDING, "p02": BINDING, "moderator": BINDING},
    )
    data = ws.data.setdefault(root, {"sessions": {}, "defaults": {}, "last": None})
    data["sessions"][game.session_id] = game.model_dump(mode="json")
    data["last"] = game.session_id
    return game


# -------------------------------------------------------------------- the switch ---

@async_test
async def test_switch_saves_current_game_then_restores_the_targets_last_game_paused(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    old_session = env.orch.session
    target = env.ws.make_workspace("ws-b")
    saved = seed_saved_game(env.ws, target)

    result = await env.orch.switch_workspace(target)

    # the old game went into the OLD workspace (and its pointer), never into the new one
    root_a = (tmp_path / "ws-a").resolve()
    assert env.sessions.log == [("ws-a", old_session.session_id)]
    assert env.ws.data[root_a]["last"] == old_session.session_id
    assert old_session.session_id not in env.ws.data[target]["sessions"]
    # the target's last game is restored paused, flagged, with its own bindings
    session = env.orch.session
    assert session is result["session"] and session.session_id == saved.session_id
    assert session.phase_text == "TARGET GAME" and session.status == "paused"
    assert session.restored_from_save and session.restore_notice == "已恢复上次对局（已暂停）"
    assert session.model_bindings["p01"] == BINDING and session.binding_issues == []
    assert result["restored"] == saved.session_id and result["changed"] is True
    assert result["recovery_error"] is None
    assert result["workspace"]["name"] == "ws-b" and result["workspace"]["enabled"] is True
    assert env.ws.active.root == target and env.orch._loop_task is None
    # runtime was rebuilt
    assert env.cache_clears == [1]
    assert isinstance(env.orch.llm, LLMClient) and env.orch.llm is not env.old_llm
    assert env.old_llm.closed == 1
    assert env.events[-1] == "workspace_changed" and env.events.count("workspace_changed") == 1
    assert not env.orch._switching_workspace


@async_test
async def test_target_without_a_saved_game_gets_a_fresh_idle_game_with_the_targets_defaults(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    env.orch.session.model_bindings = {"p01": OTHER_BINDING}  # old workspace's binding
    env.ws.data[(tmp_path / "ws-a").resolve()]["defaults"] = {"p01": OTHER_BINDING}
    target = env.ws.make_workspace("ws-b")
    env.ws.data[target]["defaults"] = {"p02": BINDING, "moderator": BINDING}

    result = await env.orch.switch_workspace(target)

    session = env.orch.session
    assert result["restored"] is None and session.status == "idle"
    assert set(session.model_bindings) == {"p02", "moderator"}  # target defaults, nothing carried over
    assert not session.restored_from_save and session.phase_text != "OLD GAME"
    assert {i.actor_id for i in session.binding_issues} == {"p01"}
    assert env.orch._autosave_enabled


@async_test
async def test_a_failed_save_of_the_current_game_aborts_the_switch(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    target = env.ws.make_workspace("ws-b")
    old_session, old_llm = env.orch.session, env.orch.llm
    env.sessions.fail = "磁盘已满"

    with pytest.raises(StorageError, match="磁盘已满"):
        await env.orch.switch_workspace(target)

    assert env.ws.active.root == (tmp_path / "ws-a").resolve()
    assert env.orch.session is old_session and env.orch.llm is old_llm
    assert env.ws.calls == []  # target was never even opened, let alone activated
    assert env.cache_clears == [] and "workspace_changed" not in env.events
    assert not env.orch._switching_workspace


@async_test
async def test_a_locked_or_invalid_target_leaves_the_current_workspace_untouched(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    old_session, old_llm = env.orch.session, env.orch.llm
    locked = env.ws.make_workspace("ws-locked")
    env.ws.locked_by_others.add(locked)

    with pytest.raises(FakeWorkspaces.WorkspaceError, match="另一个进程"):
        await env.orch.switch_workspace(locked)

    assert env.ws.active.root == (tmp_path / "ws-a").resolve()
    assert env.orch.session is old_session and env.orch.llm is old_llm
    assert env.old_llm.closed == 0 and "workspace_changed" not in env.events
    assert not env.orch._switching_workspace and not env.orch._recovery_pending
    # and it is still fully usable afterwards (autosave still lands in the right workspace)
    await env.orch.persist_session("manual")
    assert env.sessions.log[-1] == ("ws-a", old_session.session_id)


@async_test
async def test_failure_after_activation_rolls_back_to_the_previous_workspace(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    old_session = env.orch.session
    target = env.ws.make_workspace("ws-b")
    attempts: list[int] = []

    def flaky_clear() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("cannot reset stores")

    env.orch.store_cache_clearer = flaky_clear

    with pytest.raises(RuntimeError, match="cannot reset stores"):
        await env.orch.switch_workspace(target)

    assert env.ws.calls[-2:] == ["activate:ws-b", "activate:ws-a"]  # rolled back
    assert env.ws.active.root == (tmp_path / "ws-a").resolve()
    assert len(attempts) == 2  # caches were cleared again for the restored workspace
    assert env.orch.session is old_session and "workspace_changed" not in env.events
    assert not env.orch._workspace_failed and not env.orch._switching_workspace


@async_test
async def test_running_game_or_active_loop_blocks_the_switch_without_side_effects(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    target = env.ws.make_workspace("ws-b")
    env.orch.session.status = "running"
    with pytest.raises(RuntimeError, match="暂停"):
        await env.orch.switch_workspace(target)

    env.orch.session.status = "paused"
    env.orch._loop_task = asyncio.create_task(asyncio.sleep(60))
    try:
        with pytest.raises(RuntimeError, match="暂停"):
            await env.orch.switch_workspace(target)
    finally:
        env.orch._loop_task.cancel()
        await asyncio.gather(env.orch._loop_task, return_exceptions=True)
        env.orch._loop_task = None

    assert env.sessions.log == [] and env.ws.calls == [] and env.cache_clears == []
    assert env.ws.active.root == (tmp_path / "ws-a").resolve()


@async_test
async def test_switching_to_the_active_workspace_changes_nothing(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    session = env.orch.session

    result = await env.orch.switch_workspace(tmp_path / "ws-a")

    assert result["changed"] is False and env.orch.session is session
    assert env.cache_clears == [] and env.old_llm.closed == 0
    assert "workspace_changed" not in env.events


@async_test
async def test_damaged_last_game_in_the_target_opens_in_recovery_and_is_never_overwritten(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    target = env.ws.make_workspace("ws-b")
    env.ws.data[target]["last"] = "broken-game"
    env.ws.data[target]["sessions"]["broken-game"] = {"participants": "nope"}

    result = await env.orch.switch_workspace(target)  # the workspace itself is fine: it switches

    assert result["changed"] and result["restored"] is None and result["recovery_error"]
    assert env.orch._recovery_pending and "已保留" in env.orch.session.restore_notice
    before = copy.deepcopy(env.ws.data[target])
    await env.orch.broadcast_state("status_change")
    await env.orch.flush_autosave()
    await asyncio.sleep(0.05)
    assert env.ws.data[target] == before  # placeholder never written over it


# ---------------------------------------------------- how the target is opened/created ---

@async_test
async def test_missing_or_unmarked_folders_need_an_explicit_create_or_initialize(tmp_path: Path) -> None:
    env = make_env(tmp_path)
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
async def test_relative_paths_and_a_disabled_feature_are_rejected(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    with pytest.raises(FakeWorkspaces.WorkspaceError, match="绝对路径"):
        await env.orch.switch_workspace("relative/dir")
    env.ws.enabled = False
    with pytest.raises(ValueError, match="未启用"):
        await env.orch.switch_workspace(tmp_path / "ws-a")


@async_test
async def test_the_old_workspace_is_never_modified_by_a_switch_away_and_back(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    root_a = (tmp_path / "ws-a").resolve()
    first_session = env.orch.session
    target = env.ws.make_workspace("ws-b")

    await env.orch.switch_workspace(target)  # a -> b (fresh game in b)
    second_session = env.orch.session
    second_session.phase_text = "IN B"
    await env.orch.switch_workspace(root_a)  # b -> a

    assert env.orch.session.session_id == first_session.session_id  # a's last game is back
    assert env.orch.session.phase_text == "OLD GAME"
    assert env.ws.data[target]["sessions"][second_session.session_id]["phase_text"] == "IN B"
    assert first_session.session_id not in env.ws.data[target]["sessions"]
    assert second_session.session_id not in env.ws.data[root_a]["sessions"]  # no cross-writes


# ----------------------------------------------------- serialization / isolation ---

@async_test
async def test_no_autosave_of_the_old_game_can_land_in_the_new_workspace(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    old_session = env.orch.session
    target = env.ws.make_workspace("ws-b")
    env.orch._autosave_delay = 30
    await env.orch.broadcast_state("scenario_step_done")  # recorded only (fake broadcast) ...
    env.orch.schedule_autosave("scenario_step_done")  # ... so schedule the pending timer by hand
    pending = env.orch._autosave_task
    assert pending is not None and not pending.done()

    scheduled_during_switch: list[bool] = []

    def poke(root: Path, session_id: str) -> None:
        # runs inside the pre-switch save, while the switch is in progress
        env.orch.schedule_autosave("during-switch")
        scheduled_during_switch.append(env.orch._autosave_dirty)

    env.sessions.on_save = poke
    await env.orch.switch_workspace(target)

    assert pending.done() and scheduled_during_switch == [False]  # nothing scheduled mid-switch
    assert env.sessions.log == [("ws-a", old_session.session_id)]  # exactly one save, to A
    assert old_session.session_id not in env.ws.data[target]["sessions"]


@async_test
async def test_mutating_operations_and_a_second_switch_are_refused_while_switching(tmp_path: Path) -> None:
    env = make_env(tmp_path)
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


@async_test
async def test_ui_chosen_reasoning_effort_survives_the_new_llm_client(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    old = LLMClient(CFG, provider_store=env.orch.provider_store)
    old.reasoning_effort = "xhigh"
    env.orch.llm = old
    await env.orch.switch_workspace(env.ws.make_workspace("ws-b"))
    assert env.orch.llm is not old and env.orch.llm.reasoning_effort == "xhigh"
    assert env.orch.llm.provider_store is env.orch.provider_store
    await old.aclose()


# ---------------------------------------------------------------------- startup ---

def fake_migration(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> None:
    module = types.ModuleType("app.migration")

    def migrate_legacy_data(base_dir: Path, cfg: dict[str, Any]) -> dict[str, Any]:
        order.append("migrate")  # recorded only; never touches the disk
        return {"status": "ok", "sessions_migrated": 0}

    module.migrate_legacy_data = migrate_legacy_data  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.migration", module)


def startup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Env, list[str]]:
    env = make_env(tmp_path, activate=False)
    env.orch._autosave_enabled = False
    env.orch.session.status = "idle"  # the constructor's placeholder game is pristine
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
    saved = seed_saved_game(env.ws, default_root, phase="LAST TIME")
    env.ws.data[default_root]["defaults"] = {"p01": BINDING}
    env.orch.session.phase_text = "placeholder"

    report = await env.orch.startup()

    assert order == ["ensure_initial", "clear_caches", "migrate"]
    assert report["workspace"] == {"status": "ok"} and report["recovery_error"] is None
    assert report["restored"] == saved.session_id
    assert env.orch.session.phase_text == "LAST TIME" and env.orch.session.status == "paused"
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
    assert {i.actor_id for i in env.orch.session.binding_issues} == {"p01", "moderator"}


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
    assert env.orch.workspace_payload()["degraded"] is True

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


# --------------------------------------------------------------- payload / shutdown ---

@async_test
async def test_workspace_payload_hides_the_path_for_remote_callers(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    full = env.orch.workspace_payload(include_path=True)
    remote = env.orch.workspace_payload(include_path=False)

    assert full["enabled"] is True and full["name"] == "ws-a" and full["path"].endswith("ws-a")
    assert full["last_session"] == env.orch.session.session_id and full["status"] == "ok"
    assert "path" not in remote and remote["name"] == "ws-a"
    assert str(tmp_path) not in str(remote)


@async_test
async def test_shutdown_saves_into_the_active_workspace_then_releases_its_lock(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    order: list[str] = []
    env.sessions.on_save = lambda root, sid: order.append("save")
    original = env.ws.deactivate_workspace
    env.ws.deactivate_workspace = lambda: (order.append("deactivate"), original())[1]  # type: ignore[method-assign]

    await env.orch.shutdown()

    assert order == ["save", "deactivate"]
    assert env.sessions.log == [("ws-a", env.orch.session.session_id)]
    assert env.old_llm.closed == 1 and env.ws.active is None


@async_test
async def test_shutdown_with_the_feature_disabled_never_touches_the_workspace(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.ws.enabled = False
    await env.orch.shutdown()
    assert "deactivate" not in env.ws.calls
