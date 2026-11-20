"""v0.12 engine contract: per-actor model bindings — every agent (moderator included) needs one."""

from __future__ import annotations

import asyncio

import pytest

from app.game.bindings import (
    BindingError,
    actor_label,
    compute_binding_issues,
    context_window_for,
    fill_missing_from_defaults,
    normalized_binding,
    required_actors,
)
from app.game.orchestrator import GameOrchestrator, NotReadyError
from app.game.session import GameSession, Participant
from app.providers.models import ModelBinding, ModelEntry, ModelNotFound, ProviderConfig, ProviderNotFound
from app.world.types import MODERATOR_ID
from game_fakes import (
    BINDING,
    PLAYERS,
    PROVIDER,
    FakeProviders,
    FakeSettings,
    all_bound,
    async_test,
    bots_for,
    bound,
    idle_moderator,
    real_orch,
)


def make_session(count: int = 3, **kw) -> GameSession:
    return GameSession(
        participants=[Participant(id=f"p0{i}", seat=f"S{i}", name=f"N{i}") for i in range(1, count + 1)], **kw
    )


def provider_with(key: str = "k", models: list[ModelEntry] | None = None) -> ProviderConfig:
    return PROVIDER.model_copy(update={"api_key": key, **({"models": models} if models is not None else {})})


# --------------------------------------------------------------------------- the rules ---


def test_every_agent_including_the_moderator_needs_a_binding() -> None:
    assert required_actors(make_session(3)) == [MODERATOR_ID, "p01", "p02", "p03"]
    assert actor_label(make_session(), MODERATOR_ID) == "主持人"
    assert actor_label(make_session(), "p02") == "N2（p02）"
    assert actor_label(make_session(), "ghost") == "ghost"


def test_issues_cover_missing_unknown_provider_unknown_model_and_missing_key() -> None:
    session = make_session(5)
    session.model_bindings = {
        MODERATOR_ID: bound("big"),
        "p01": bound("big"),
        "p02": bound("big", pid="gone"),
        "p03": bound("nope"),
        # p04 unbound, p05 uses a provider without a key
    }
    session.model_bindings["p05"] = bound("big", pid="nokey")
    store = FakeProviders(provider_with(), provider_with(key="").model_copy(update={"id": "nokey"}))
    issues = {i.actor_id: i.message for i in compute_binding_issues(session, store)}
    assert set(issues) == {"p02", "p03", "p04", "p05"}
    assert "尚未选择模型" in issues["p04"] and "不存在" in issues["p02"] and "nope" in issues["p03"]
    assert "API Key" in issues["p05"]


def test_a_fully_bound_game_has_no_issues() -> None:
    session = make_session(2, model_bindings=all_bound())
    assert compute_binding_issues(session, FakeProviders(PROVIDER)) == []


def test_storage_failures_are_reported_not_raised() -> None:
    class Broken:
        def resolve(self, binding):
            raise OSError("disk gone")

    session = make_session(1, model_bindings=all_bound())
    issues = compute_binding_issues(session, Broken())
    assert {i.actor_id for i in issues} == {MODERATOR_ID, "p01"} and all("无法读取" in i.message for i in issues)


def test_fill_missing_copies_defaults_only_for_agents_of_this_game() -> None:
    session = make_session(2, model_bindings={"p01": bound("small")})
    defaults = {**all_bound(), "p09": BINDING}  # p09 is not in this game
    fill_missing_from_defaults(session, defaults)
    assert session.model_bindings["p01"].model_id == "small"  # an existing binding is never replaced
    assert set(session.model_bindings) == {MODERATOR_ID, "p01", "p02"}
    session.model_bindings["p02"].reasoning_depth = "low"
    assert defaults["p02"].reasoning_depth == "high"  # a copy, not an alias


def test_context_window_never_exceeds_what_the_model_supports() -> None:
    entry = ModelEntry(id="m", input_price_per_m=1, output_price_per_m=1, context_window_tokens=8000)
    assert normalized_binding(bound(window=1_000_000), entry).context_window_tokens == 8000
    assert normalized_binding(bound(window=4000), entry).context_window_tokens == 4000
    session = make_session(1, model_bindings={"p01": bound(window=123_456)})
    assert context_window_for(session, "p01") == 123_456
    assert context_window_for(session, "p02") == 1_000_000 and context_window_for(session, None) == 1_000_000


# ------------------------------------------------------------------- through the orchestrator ---


@async_test
async def test_new_games_start_from_the_saved_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch, defaults={**all_bound(), "p09": BINDING})
    game = await orch.new_game()
    assert set(game.model_bindings) == {MODERATOR_ID, *(p["id"] for p in PLAYERS)}
    assert orch.refresh_binding_issues() == [] and game.binding_issues == []


@async_test
async def test_start_refuses_while_any_agent_lacks_a_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """v0.14 (H25): a missing model is one of the problems of the start check (409 NOT_READY, scope ``binding``); nothing is dealt."""
    orch = real_orch(monkeypatch, defaults={"p01": BINDING})
    with pytest.raises(NotReadyError) as info:
        await orch.start_or_resume()
    assert isinstance(info.value, RuntimeError) and info.value.status == 409 and info.value.code == "NOT_READY"
    problems = info.value.problems
    assert {p["scope"] for p in problems} == {"binding"}
    assert {p["seat"] or p["name"] for p in problems} == {"主持人", "p02", "p03", "p04"}
    assert all(p["items"][0]["code"] == "no_binding" and p["items"][0]["fix"] == "bind_model" for p in problems)
    assert "不能开局" in str(info.value) and "主持人" in str(info.value)
    assert orch.session.status == "idle" and orch._supervisor is None and not orch.world.running
    assert not orch.world.state.dealt and all(not p.role for p in orch.world.state.players)  # (nothing was dealt)


@async_test
async def test_a_resumed_game_whose_model_is_gone_is_a_binding_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the start of a game has the start check; resuming one keeps the old, plain refusal."""
    orch = real_orch(monkeypatch, defaults=all_bound())
    bots_for(orch, idle_moderator)
    await orch.start_or_resume()
    await orch.pause()
    orch.provider_store = FakeProviders()  # the provider is gone
    orch.ensure_bindings_ready = GameOrchestrator.ensure_bindings_ready.__get__(orch)  # type: ignore[method-assign]
    with pytest.raises(BindingError) as info:
        await orch.start_or_resume()
    assert {i.actor_id for i in info.value.issues} == {MODERATOR_ID, *(p["id"] for p in PLAYERS)}
    assert orch.session.status == "paused" and orch._supervisor is None


@async_test
async def test_update_bindings_merges_normalizes_and_saves_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch, providers=(provider_with(models=[
        ModelEntry(id="small", input_price_per_m=1, output_price_per_m=2, context_window_tokens=8000),
        ModelEntry(id="big", input_price_per_m=3, output_price_per_m=6)]),))
    events: list[str] = []

    async def record(event: str = "x") -> None:
        events.append(event)

    orch.broadcast_state = record  # type: ignore[method-assign]
    result = await orch.update_model_bindings({MODERATOR_ID: bound("big"), "p01": {"provider_id": "prov", "model_id": "small"}})

    assert result["p01"].context_window_tokens == 8000  # clamped to the model's window
    assert orch.settings_store.defaults[MODERATOR_ID].model_id == "big"  # persisted as the user's defaults
    assert events == ["bindings_updated"]
    assert {i.actor_id for i in orch.session.binding_issues} == {"p02", "p03", "p04"}

    await orch.update_model_bindings({"p01": None})  # None removes (and forgets the default)
    assert "p01" not in orch.session.model_bindings and "p01" not in orch.settings_store.defaults


@async_test
async def test_update_bindings_validates_everything_before_applying_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    with pytest.raises(ValueError, match="未知的参与者"):
        await orch.update_model_bindings({"ghost": bound()})
    with pytest.raises(ValueError, match="必须是对象"):
        await orch.update_model_bindings([])  # type: ignore[arg-type]
    with pytest.raises(ProviderNotFound):
        await orch.update_model_bindings({"p01": bound(), "p02": bound(pid="gone")})
    with pytest.raises(ModelNotFound):
        await orch.update_model_bindings({"p01": bound(), "p02": bound("nope")})
    assert orch.session.model_bindings == {} and orch.settings_store.defaults == {}

    orch.settings_store.fail_save_defaults = True  # the defaults could not be persisted
    with pytest.raises(RuntimeError, match="disk full"):
        await orch.update_model_bindings({"p01": bound()})
    assert orch.session.model_bindings == {}  # ...so nothing was applied either


@async_test
async def test_bindings_cannot_change_while_a_game_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch, defaults=all_bound())
    bots_for(orch, idle_moderator)
    await orch.start_or_resume()
    try:
        with pytest.raises(RuntimeError, match="对局进行中"):
            await orch.update_model_bindings({"p01": bound("small")})
        assert orch.session.model_bindings["p01"].model_id == "big"
    finally:
        await orch.shutdown()


@async_test
async def test_a_game_with_all_bindings_starts_through_the_real_check(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch, defaults=all_bound())
    orch.llm_port_factory = lambda o: __import__("v12.bots", fromlist=["BotLlm"]).BotLlm(o.world, {})
    await orch.start_or_resume()  # no BindingError: the real ensure_bindings_ready() passed
    assert orch.session.status == "running"
    await orch.shutdown()
    assert asyncio.get_running_loop() is not None


@async_test
async def test_provider_keys_are_registered_with_the_plaintext_trace_scrubber(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "totally-custom-key-without-a-known-shape-123"
    orch = real_orch(monkeypatch, defaults=all_bound(), providers=(provider_with(key=secret),))
    bots_for(orch, idle_moderator)
    await orch.start_or_resume()
    try:
        trace = orch._supervisor.trace  # type: ignore[union-attr]
        trace.write("content", "p01", text=f"the model echoed {secret} by accident")
        import json as _json

        line = trace.current_path.read_text(encoding="utf-8").splitlines()[-1]
        assert secret not in line and "echoed" in _json.loads(line)["text"]
    finally:
        await orch.shutdown()
