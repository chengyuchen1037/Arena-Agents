from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from app.api import routes
from app.game.session import GameSession
from sec_api_fakes import SECRET_KEY, bind, install


@pytest.fixture
def env(monkeypatch):
    return install(monkeypatch)


def test_state_serves_the_public_session_but_save_payload_retains_the_world(env) -> None:
    async def run_test() -> None:
        session = GameSession(
            session_id="state-save-markers",
            world={"marker": "WORLD_MARKER"},
            scenario_spec={"id": "x", "version": "1"},
        )
        env.orch.session = session

        state = await routes.get_state()
        response = await routes.save_session()

        # /state is the spectator view (orchestrator.public_session): no raw world dump inside
        assert "world" not in state["session"] and "scenario_spec" not in state["session"]
        assert "WORLD_MARKER" not in json.dumps(state)

        assert isinstance(response, routes.JSONResponse)
        response_payload = json.loads(response.body)
        assert "saved_path" not in response_payload
        assert response_payload["session"]["world"] == {"marker": "WORLD_MARKER"}
        env.orch.persist_session.assert_awaited_once_with("manual")

    asyncio.run(run_test())


def test_state_exposes_restore_signal_and_binding_issues(env) -> None:
    async def run_test() -> None:
        env.orch.restored_from_save = True
        provider = env.providers.seed(models=("m1",))
        env.session.model_bindings = {"moderator": bind(provider.id)}
        state = await routes.get_state()
        assert state["restored"] is True
        actors = {issue["actor_id"] for issue in state["binding_issues"]}
        assert actors == {"p01", "p02"}  # moderator is bound, players are not
        assert SECRET_KEY not in json.dumps(state)

    asyncio.run(run_test())


def test_state_includes_the_workspace_summary_and_keeps_direct_calls_working(env) -> None:
    async def run_test() -> None:
        # v0.11: /state carries the active-workspace summary (never the API key).
        state = await routes.get_state()
        assert "workspace" in state
        assert state["workspace"]["enabled"] is False  # fake workspace module: disabled
        assert SECRET_KEY not in json.dumps(state)

        env.workspace.set_enabled(True)
        state = await routes.get_state()
        assert state["workspace"]["enabled"] is True
        assert state["workspace"]["name"] == "Test Workspace"
        assert state["workspace"]["path"].endswith("Test")  # unit call: no Request -> local

    asyncio.run(run_test())


def test_new_game_request_randomize_defaults_to_none() -> None:
    assert routes.NewGameRequest().randomize_identities is None


def test_new_game_without_body_passes_none_to_orchestrator(env) -> None:
    async def run_test() -> None:
        await routes.control_new_game(None)
        await routes.control_new_game(routes.NewGameRequest())
        await routes.control_new_game(routes.NewGameRequest(randomize_identities=False))
        assert [call.kwargs for call in env.orch.new_game.await_args_list] == [
            {"randomize_identities": None},
            {"randomize_identities": None},
            {"randomize_identities": False},
        ]

    asyncio.run(run_test())


def test_start_refuses_when_any_actor_lacks_a_binding(env) -> None:
    async def run_test() -> None:
        provider = env.providers.seed(models=("m1",))
        env.session.model_bindings = {"moderator": bind(provider.id)}
        with pytest.raises(HTTPException) as excinfo:
            await routes.control_start()
        assert excinfo.value.status_code == 409
        assert "p01" in excinfo.value.detail
        env.orch.start_or_resume.assert_not_awaited()

        env.session.model_bindings = {
            actor: bind(provider.id) for actor in ("moderator", "p01", "p02")
        }
        await routes.control_start()
        env.orch.start_or_resume.assert_awaited_once()

    asyncio.run(run_test())
