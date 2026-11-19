"""End-to-end engine persistence against the real stores (temp data dir, test key): games are plain JSON, only the keys are encrypted."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest

import app.game.orchestrator as orchestrator_module
from app.game.orchestrator import GameOrchestrator
from app.providers.models import ModelBinding
from app.providers.store import get_provider_store
from app.storage import reset_stores_for_tests
from app.storage.games import get_game_store
from app.storage.secrets import SecretStore
from app.storage.settings import get_default_bindings, get_last_session_id
from app.world.types import MODERATOR_ID
from game_fakes import CFG, PLAYERS, async_test


@pytest.fixture(autouse=True)
def small_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator_module, "load_config", lambda *a, **k: copy.deepcopy(CFG))


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


def bind_everybody(provider_id: str) -> dict[str, ModelBinding]:
    return {
        actor: ModelBinding(provider_id=provider_id, model_id="m1", reasoning_depth="low")
        for actor in [MODERATOR_ID, *(p["id"] for p in PLAYERS)]
    }


@async_test
async def test_game_survives_a_restart_paused_with_bindings_and_costs(data_dir: Path) -> None:
    provider_id = make_provider()

    first = GameOrchestrator()
    first._autosave_delay = 0.01
    await first.update_model_bindings(bind_everybody(provider_id))
    first.world.state.phase = "第 2 天"
    first.usage.record_usage(
        "p01", prompt_tokens=1_000_000, completion_tokens=0, provider_id=provider_id, model_id="m1",
        input_price_per_m=1.0, output_price_per_m=2.0,
    )
    first.session.status = "running"  # was mid-game when the process died
    first.world.state.status = "running"
    session_id = first.session.session_id
    await first.persist_session("test")

    # ---- "restart": a brand-new process-level orchestrator over the same data dir ----
    reset_stores_for_tests()
    second = GameOrchestrator()
    report = await second.startup()

    assert report["restored"] == session_id and report["recovery_error"] is None
    game = second.session
    assert game.session_id == session_id and second.world.state.phase == "第 2 天"
    assert game.status == "paused" and second._supervisor is None  # never auto-resumes
    assert game.restored_from_save and game.restore_notice == "已恢复上次对局（已暂停）"
    assert game.model_bindings["p03"].provider_id == provider_id
    assert game.model_bindings["p03"].context_window_tokens == 64_000  # model's real window
    assert game.binding_issues == []
    assert second.usage.total_cost_usd == pytest.approx(1.0)
    assert second.usage.by_model[f"{provider_id}::m1"].calls == 1

    # Ready to resume: no start-time binding error.
    second.ensure_bindings_ready()
    await second.shutdown()


@async_test
async def test_games_are_readable_json_and_only_the_api_key_is_encrypted(data_dir: Path) -> None:
    provider_id = make_provider()
    orch = GameOrchestrator()
    await orch.update_model_bindings({"p01": ModelBinding(provider_id=provider_id, model_id="m1")})
    orch.world.commit("message", MODERATOR_ID, {"content": "PLAINTEXT-MARKER-9981", "kind": "speech"}, visible_to="*")
    await orch.persist_session("test")

    files = [p for p in data_dir.rglob("*") if p.is_file()]
    assert files
    # the game is an ordinary JSON file you can open, chat included ...
    game_file = data_dir / "games" / orch.session.session_id / "game.json"
    text = game_file.read_text(encoding="utf-8")
    assert "PLAINTEXT-MARKER-9981" in text and json.loads(text)["session_id"] == orch.session.session_id
    assert (data_dir / "games" / "index.json").is_file()
    # ... and so are the providers and the settings; none of them holds the key
    assert "Test" in (data_dir / "providers.json").read_text(encoding="utf-8")
    assert SECRET not in (data_dir / "settings.json").read_text(encoding="utf-8")
    # the key exists exactly once, encrypted: nowhere in the data directory in the clear, and not in the key file either
    secrets_file = data_dir / "secrets.v1.enc"
    assert secrets_file.is_file() and SECRET.encode() not in secrets_file.read_bytes()
    assert [p for p in files if SECRET.encode() in p.read_bytes()] == []
    assert SecretStore(data_dir).read_all() == {provider_id: SECRET}
    assert get_default_bindings()["p01"].model_id == "m1"
    assert get_last_session_id() == orch.session.session_id
    # And the saved game is readable through the store, with ids only.
    saved = get_game_store().load(orch.session.session_id)
    assert saved["model_bindings"]["p01"]["provider_id"] == provider_id
    assert SECRET not in json.dumps(saved)
    await orch.shutdown()


@async_test
async def test_deleting_the_bound_provider_surfaces_a_rebind_requirement_not_a_fallback(data_dir: Path) -> None:
    provider_id = make_provider()
    orch = GameOrchestrator()
    await orch.update_model_bindings(bind_everybody(provider_id))
    await orch.persist_session("test")
    get_provider_store().delete_provider(provider_id)

    reset_stores_for_tests()
    restarted = GameOrchestrator()
    await restarted.startup()

    assert restarted.session.session_id == orch.session.session_id
    assert len(restarted.session.binding_issues) == 5  # the moderator and the four players
    assert all("Provider" in i.message for i in restarted.session.binding_issues)
    with pytest.raises(RuntimeError, match="需要重新绑定"):
        await restarted.start_or_resume()
    assert restarted.session.status == "idle"  # the refused start changed nothing (the saved game never started)
    assert restarted.world.running is False
    await restarted.shutdown()
