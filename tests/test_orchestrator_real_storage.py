"""End-to-end engine persistence against the real encrypted stores (temp data dir, test key)."""

from __future__ import annotations

import asyncio
import base64
import functools
import json
from pathlib import Path
from typing import Any, Callable

import pytest

import app.config
import app.migration
from app.engine.orchestrator import GameOrchestrator
from app.providers.models import ModelBinding
from app.providers.store import get_provider_store
from app.storage import reset_stores_for_tests
from app.storage.sessions import get_session_store
from app.storage.settings import get_default_bindings, get_last_session_id


def async_test(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


REAL_PROJECT_DIR = Path(app.config.BASE_DIR)  # captured at import, before any patching


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
def isolated_migration(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """No test in this module may ever migrate (or delete) the real project's .env / saves.

    ``startup()`` looks up ``app.migration.migrate_legacy_data`` when it runs, so it is replaced by
    a recording no-op that never opens ``base_dir``.  (``BASE_DIR`` itself is left alone: the
    engine still needs the real ``config.yml`` / scenarios / prompts.)  The fixture additionally
    verifies that the real project's ``.env`` and ``saves/`` are byte-for-byte untouched in
    size and mtime after every test.
    """
    calls: list[Path] = []

    def fake_migrate(base_dir: Path, cfg: dict[str, Any]) -> dict[str, Any]:
        calls.append(Path(base_dir))  # recorded only; nothing is read, moved or deleted
        return {"status": "ok", "providers_migrated": 0, "sessions_migrated": 0}

    monkeypatch.setattr(app.migration, "migrate_legacy_data", fake_migrate)
    before = _project_fingerprint()
    yield calls
    assert _project_fingerprint() == before, "the real project .env/saves were modified by a test"


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "arena-data"
    monkeypatch.setenv("ARENA_DATA_DIR", str(target))
    monkeypatch.setenv("ARENA_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
        monkeypatch.delenv(name, raising=False)
    reset_stores_for_tests()
    yield target
    reset_stores_for_tests()


SECRET = "sk-test-SECRET-VALUE-123456"


def make_provider() -> str:
    store = get_provider_store()
    provider = store.create_provider("Test", "openai_chat", "https://api.example.com/v1", SECRET)
    store.add_model(provider.id, "m1", 1.0, 2.0, 64_000)
    return provider.id


@async_test
async def test_game_survives_a_restart_paused_with_bindings_and_costs(
    data_dir: Path, isolated_migration: list[Path]
) -> None:
    provider_id = make_provider()

    first = GameOrchestrator()
    first._subscribers = set()
    first._autosave_delay = 0.01
    first.session.status = "paused"
    bindings = {p.id: ModelBinding(provider_id=provider_id, model_id="m1", reasoning_depth="low")
                for p in first.session.participants}
    await first.update_model_bindings(bindings)
    first.session.phase_text = "第 2 天"
    first.session.record_usage("p01", prompt_tokens=1_000_000, completion_tokens=0,
                               provider_id=provider_id, model_id="m1",
                               input_price_per_m=1.0, output_price_per_m=2.0)
    first.session.status = "running"  # was mid-game when the process died
    session_id = first.session.session_id
    await first.persist_session("test")

    # ---- "restart": a brand-new process-level orchestrator over the same data dir ----
    reset_stores_for_tests()
    second = GameOrchestrator()
    second._subscribers = set()
    report = await second.startup()
    assert len(isolated_migration) == 1  # ran against the empty temp project only
    assert report["migration"]["sessions_migrated"] == 0

    assert report["restored"] == session_id and report["recovery_error"] is None
    game = second.session
    assert game.session_id == session_id and game.phase_text == "第 2 天"
    assert game.status == "paused" and second._loop_task is None  # never auto-resumes
    assert game.restored_from_save and game.restore_notice == "已恢复上次对局（已暂停）"
    assert game.model_bindings["p07"].provider_id == provider_id
    assert game.model_bindings["p07"].context_window_tokens == 64_000  # model's real window
    assert game.binding_issues == []
    assert game.usage_stats.total_cost_usd == pytest.approx(1.0)
    assert game.usage_stats.by_model[f"{provider_id}::m1"].calls == 1

    # Ready to resume: no start-time binding error.
    second.ensure_bindings_ready()
    await second.shutdown()


@async_test
async def test_nothing_secret_or_readable_reaches_disk(data_dir: Path) -> None:
    provider_id = make_provider()
    orch = GameOrchestrator()
    orch._subscribers = set()
    await orch.update_model_bindings(
        {"p01": ModelBinding(provider_id=provider_id, model_id="m1")}
    )
    orch.session.add_record(stream="channel", channel_id=None, kind="note", actor_id="moderator",
                            content="PLAINTEXT-MARKER-9981")
    await orch.persist_session("test")

    session_files = [p for p in data_dir.rglob("*") if p.is_file()]
    assert session_files
    blob = b"".join(p.read_bytes() for p in session_files if "settings" not in p.name)
    assert SECRET.encode() not in blob and b"PLAINTEXT-MARKER-9981" not in blob  # encrypted
    settings = (data_dir / "settings.json").read_text(encoding="utf-8")
    assert SECRET not in settings and "sk-" not in settings
    assert get_default_bindings()["p01"].model_id == "m1"
    assert get_last_session_id() == orch.session.session_id
    # And the saved game is readable through the store, with ids only.
    saved = get_session_store().load(orch.session.session_id)
    assert saved["model_bindings"]["p01"]["provider_id"] == provider_id
    assert SECRET not in json.dumps(saved)
    await orch.shutdown()


@async_test
async def test_deleting_the_bound_provider_surfaces_a_rebind_requirement_not_a_fallback(
    data_dir: Path,
    isolated_migration: list[Path],
) -> None:
    provider_id = make_provider()
    orch = GameOrchestrator()
    orch._subscribers = set()
    await orch.update_model_bindings(
        {p.id: ModelBinding(provider_id=provider_id, model_id="m1") for p in orch.session.participants}
    )
    await orch.persist_session("test")
    get_provider_store().delete_provider(provider_id)

    reset_stores_for_tests()
    restarted = GameOrchestrator()
    restarted._subscribers = set()
    await restarted.startup()
    assert len(isolated_migration) == 1

    assert restarted.session.session_id == orch.session.session_id
    assert len(restarted.session.binding_issues) == 12
    assert all("Provider" in i.message for i in restarted.session.binding_issues)
    with pytest.raises(RuntimeError, match="需要重新绑定"):
        await restarted.start_or_resume()
    assert restarted.session.status == "idle"  # the refused start changed nothing
    await restarted.shutdown()
