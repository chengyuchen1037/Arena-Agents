"""v0.10 engine contract: per-actor model bindings, price-snapshot accounting, schema v2."""

from __future__ import annotations

import asyncio
import copy
import functools
import json
import tempfile
from pathlib import Path
from typing import Any, Callable

import pytest

from app.engine.bindings import BindingError, compute_binding_issues, context_window_for
from app.engine.context import _build_context_window_status_text
from app.engine.llm import LLMResponse, ToolCallData
from app.engine.orchestrator import GameOrchestrator, SessionLoadError
from app.engine.participant_loop import assistant_tool_message
from app.engine.session_migration import migrate_session_payload
from app.engine.state import (
    APP_ENGINE_VERSION,
    Channel,
    FeedRecord,
    GameSession,
    Participant,
)
from app.providers.models import (
    ModelBinding,
    ModelEntry,
    ModelNotFound,
    ProviderConfig,
    ProviderNotFound,
)

TEST_CFG = {
    "prompts": {
        "moderator_system": "__test_missing_moderator_prompt__.md",
        "participant_system": "__test_missing_participant_prompt__.md",
    },
    "knowledge_base": {
        "docs_dir": str(Path(tempfile.gettempdir()) / "arena_test_missing_knowledge")
    },
}


def async_test(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# ------------------------------------------------------------------------ fakes ---

class FakeProviders:
    """Just the part of ProviderStore the engine uses."""

    def __init__(self, *providers: ProviderConfig) -> None:
        self.providers = {p.id: p for p in providers}

    def resolve(self, binding: ModelBinding) -> tuple[ProviderConfig, ModelEntry]:
        provider = self.providers.get(binding.provider_id)
        if provider is None:
            raise ProviderNotFound(binding.provider_id)
        for entry in provider.models:
            if entry.id == binding.model_id:
                return provider, entry
        raise ModelNotFound(binding.model_id)


class FakeSettings:
    def __init__(self, defaults: dict[str, ModelBinding] | None = None) -> None:
        self.defaults = dict(defaults or {})
        self.last_id: str | None = None
        self.fail_save_defaults = False

    def get_default_bindings(self) -> dict[str, ModelBinding]:
        return dict(self.defaults)

    def save_default_bindings(self, bindings: dict[str, ModelBinding]) -> None:
        if self.fail_save_defaults:
            raise RuntimeError("disk full")
        self.defaults = dict(bindings)

    def get_last_session_id(self) -> str | None:
        return self.last_id

    def set_last_session_id(self, session_id: str) -> None:
        self.last_id = session_id


def provider(pid: str = "prov", *, key: str = "k", models: list[ModelEntry] | None = None) -> ProviderConfig:
    return ProviderConfig(
        id=pid,
        name=pid,
        protocol="openai_chat",
        base_url="https://example.invalid/v1",
        api_key=key,
        models=models
        if models is not None
        else [
            ModelEntry(id="small", input_price_per_m=1.0, output_price_per_m=2.0, context_window_tokens=8000),
            ModelEntry(id="big", input_price_per_m=3.0, output_price_per_m=6.0),
        ],
    )


def make_session(count: int = 3, scenario: bool = True, status: str = "paused", **kw: Any) -> GameSession:
    return GameSession(
        status=status,
        participants=[Participant(id=f"p0{i}", seat=f"S{i}", name=f"N{i}") for i in range(1, count + 1)],
        channels=[
            Channel(id="public", name="P", type="public", members=[f"p0{i}" for i in range(1, count + 1)])
        ],
        scenario_snapshot={"definition": {"id": "x"}} if scenario else {},
        **kw,
    )


def make_orch(session: GameSession, llm: Any = None, **stores: Any) -> GameOrchestrator:
    orch = GameOrchestrator.__new__(GameOrchestrator)
    orch.cfg = TEST_CFG
    orch.session = session
    orch.llm = llm
    orch._subscribers = set()
    orch.provider_store = stores.get("providers", FakeProviders(provider()))
    orch.settings_store = stores.get("settings", FakeSettings())
    orch.session_store = stores.get("sessions")
    return orch


def bind(model: str = "big", pid: str = "prov", depth: str = "high", window: int = 1_000_000) -> ModelBinding:
    return ModelBinding(provider_id=pid, model_id=model, reasoning_depth=depth, context_window_tokens=window)


# -------------------------------------------------------------- usage accounting ---

def test_cost_uses_each_calls_price_snapshot_and_history_is_never_recomputed() -> None:
    session = make_session()
    cheap = LLMResponse(
        prompt_tokens=1_000_000, completion_tokens=0,
        provider_id="prov", model_id="m", input_price_per_m=1.0, output_price_per_m=2.0,
    )
    session.record_response_usage("p01", cheap)
    # The user edits the model price afterwards: the next call carries the new snapshot.
    pricey = LLMResponse(
        prompt_tokens=1_000_000, completion_tokens=500_000,
        provider_id="prov", model_id="m", input_price_per_m=10.0, output_price_per_m=20.0,
    )
    session.record_response_usage("p02", pricey)

    stats = session.usage_stats
    assert stats.total_cost_usd == pytest.approx(1.0 + 10.0 + 10.0)  # not (2M * 10) retroactively
    assert stats.prompt_tokens == 2_000_000 and stats.completion_tokens == 500_000
    assert stats.total_tokens == 2_500_000
    model = stats.by_model["prov::m"]
    assert model.calls == 2 and model.cost_usd == pytest.approx(21.0)
    assert (model.input_price_per_m, model.output_price_per_m) == (10.0, 20.0)  # latest snapshot
    assert stats.by_actor["p01"].cost_usd == pytest.approx(1.0)
    assert stats.by_actor["p02"].cost_usd == pytest.approx(20.0)


def test_free_model_snapshot_of_zero_is_honoured_and_unrouted_calls_use_legacy_prices() -> None:
    session = make_session()
    session.usage_stats.input_price_per_m = 0.5
    session.usage_stats.output_price_per_m = 0.5
    free = LLMResponse(prompt_tokens=1_000_000, provider_id="p", model_id="free",
                       input_price_per_m=0.0, output_price_per_m=0.0)
    session.record_response_usage("p01", free)
    assert session.usage_stats.total_cost_usd == 0.0

    legacy = LLMResponse(prompt_tokens=1_000_000, completion_tokens=1_000_000)  # no provider/model
    session.record_response_usage("p01", legacy)
    assert session.usage_stats.total_cost_usd == pytest.approx(1.0)
    assert list(session.usage_stats.by_model) == ["p::free"]  # unrouted calls have no model row


def test_tiny_costs_accumulate_instead_of_rounding_to_zero() -> None:
    session = make_session()
    for _ in range(100):
        session.record_usage("p01", prompt_tokens=1, completion_tokens=0, provider_id="p",
                             model_id="m", input_price_per_m=0.27, output_price_per_m=1.1)
    assert session.usage_stats.total_cost_usd == pytest.approx(100 * 0.27e-6, rel=1e-6)


def test_legacy_add_token_usage_still_works_and_zero_calls_are_ignored() -> None:
    session = make_session()
    session.add_token_usage(0, 0)
    assert session.usage_stats.by_actor == {}
    session.add_token_usage(1_000_000, 0)
    assert session.usage_stats.total_cost_usd == pytest.approx(0.27)


# --------------------------------------------------------- context window per binding ---

def test_context_window_follows_the_actors_binding() -> None:
    session = make_session()
    session.model_bindings = {"p01": bind("small", window=4000), "p02": bind("big")}
    blob = "x" * 100
    small = _build_context_window_status_text(session, blob, "p01")
    big = _build_context_window_status_text(session, blob, "p02")
    unbound = _build_context_window_status_text(session, blob, "p03")
    assert "/ 4000`" in small
    assert "/ 1000000`" in big
    assert "/ 1000000`" in unbound  # legacy session-wide window
    assert context_window_for(session, "p01") == 4000
    assert context_window_for(session, None) == 1_000_000


# ----------------------------------------------------------- bindings reach the LLM ---

class RecordingLLM:
    def __init__(self, respond: Callable[[str], LLMResponse]) -> None:
        self.respond = respond
        self.bindings: list[Any] = []

    async def chat_stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        binding = kwargs.get("binding")
        self.bindings.append(binding)
        return self.respond(binding.model_id if binding else "")


def priced(content: str, model: str, price: float) -> LLMResponse:
    return LLMResponse(
        content=content, prompt_tokens=1_000_000, completion_tokens=0,
        provider_id="prov", model_id=model, input_price_per_m=price, output_price_per_m=price,
    )


@async_test
async def test_every_participant_call_carries_that_participants_binding_and_cost() -> None:
    session = make_session(2, status="running")
    session.model_bindings = {"p01": bind("small"), "p02": bind("big")}
    llm = RecordingLLM(lambda model: priced(f"hi from {model}", model, 1.0 if model == "small" else 3.0))
    orch = make_orch(session, llm)

    text1, _ = await orch._run_participant_speak("p01", "public", "")
    text2, _ = await orch._run_participant_speak("p02", "public", "")

    assert (text1, text2) == ("hi from small", "hi from big")
    assert [b.model_id for b in llm.bindings] == ["small", "big"]
    assert session.usage_stats.by_actor["p01"].cost_usd == pytest.approx(1.0)
    assert session.usage_stats.by_actor["p02"].cost_usd == pytest.approx(3.0)
    assert set(session.usage_stats.by_model) == {"prov::small", "prov::big"}


@async_test
async def test_batch_followups_and_legacy_moderator_use_their_own_bindings() -> None:
    session = make_session(1, scenario=False, status="running")
    session.model_bindings = {"p01": bind("small"), "moderator": bind("big", depth="low")}
    session.moderator_messages = [{"role": "user", "content": "go"}]

    # A speech that ends with only a thought triggers the empty-speech follow-up (another call).
    answers = iter([LLMResponse(thought="thinking", content="", provider_id="prov", model_id="small"),
                    priced("final words", "small", 1.0)])
    llm = RecordingLLM(lambda model: next(answers) if model == "small" else LLMResponse(
        content="no tools", provider_id="prov", model_id="big"))
    orch = make_orch(session, llm)

    text, _ = await orch._run_participant_speak("p01", "public", "")
    assert text == "final words"
    assert [b.model_id for b in llm.bindings] == ["small", "small"]

    await orch._moderator_step()
    assert llm.bindings[-1].model_id == "big" and llm.bindings[-1].reasoning_depth == "low"
    assert "moderator" in session.usage_stats.by_actor


def test_assistant_tool_message_keeps_provider_private_only_when_present() -> None:
    call = ToolCallData(id="c1", name="t", arguments_json="{}")
    plain = assistant_tool_message(LLMResponse(tool_calls=[call]))
    assert "_provider_private" not in plain
    private = {"provider_id": "prov", "protocol": "anthropic_messages", "blocks": [{"type": "thinking"}]}
    kept = assistant_tool_message(LLMResponse(tool_calls=[call], provider_private=private))
    assert kept["_provider_private"] == private


# ------------------------------------------------------------- binding validation ---

def test_binding_issues_name_the_actor_and_never_fall_back_silently() -> None:
    session = make_session(5)
    session.model_bindings = {
        "p01": bind("big"),                      # fine
        "p02": bind("missing-model"),            # model gone
        "p03": bind("big", pid="ghost"),         # provider gone
        "p04": bind("big", pid="nokey"),         # provider without an API key
    }
    store = FakeProviders(provider(), provider("nokey", key=" "))
    issues = {i.actor_id: i.message for i in compute_binding_issues(session, store, TEST_CFG)}
    assert set(issues) == {"p02", "p03", "p04", "p05"}
    assert "missing-model" in issues["p02"] and "不存在" in issues["p03"]
    assert "API Key" in issues["p04"] and "尚未选择模型" in issues["p05"]


def test_moderator_binding_is_required_only_for_legacy_games() -> None:
    scenario_game = make_session(1)
    legacy_game = make_session(1, scenario=False)
    for session in (scenario_game, legacy_game):
        session.model_bindings = {"p01": bind()}
    store = FakeProviders(provider())
    assert compute_binding_issues(scenario_game, store, TEST_CFG) == []
    assert [i.actor_id for i in compute_binding_issues(legacy_game, store, TEST_CFG)] == ["moderator"]


def test_resume_is_blocked_when_existing_context_exceeds_the_new_models_window() -> None:
    session = make_session(1)
    session.model_bindings = {"p01": bind("small", window=4000)}
    for index in range(20):  # each line is clipped to 900 chars in the visible history
        session.records.append(
            FeedRecord(
                id=f"rec_{index:04d}", timestamp="t", stream="channel", channel_id="public",
                kind="message", actor_id="p01", actor_name="N1", content="字" * 900,
            )
        )
    issues = compute_binding_issues(session, FakeProviders(provider()), TEST_CFG)
    assert len(issues) == 1 and "压缩上下文" in issues[0].message


@async_test
async def test_start_is_refused_with_a_clear_chinese_error_until_bound() -> None:
    session = make_session(2, status="idle")
    orch = make_orch(session, None)
    orch.reload_config = lambda: True  # type: ignore[method-assign]
    started: list[bool] = []

    async def fake_loop() -> None:
        started.append(True)

    orch._run_scenario_loop = fake_loop  # type: ignore[method-assign]

    with pytest.raises(BindingError) as info:
        await orch.start_or_resume()
    assert isinstance(info.value, RuntimeError)  # routes already map RuntimeError -> 400
    assert "需要重新绑定" in str(info.value) and "N1" in str(info.value)
    assert session.status == "idle" and not started
    assert {i.actor_id for i in session.binding_issues} == {"p01", "p02"}

    session.model_bindings = {"p01": bind(), "p02": bind()}
    session.restored_from_save, session.restore_notice = True, "x"
    await orch.start_or_resume()
    await asyncio.sleep(0)
    assert session.status == "running" and started == [True]
    assert session.restored_from_save is False and session.restore_notice == ""
    assert session.binding_issues == []


# ------------------------------------------------------------- update_model_bindings ---

@async_test
async def test_update_bindings_rejected_while_running() -> None:
    session = make_session(status="running")
    orch = make_orch(session)
    with pytest.raises(RuntimeError, match="进行中"):
        await orch.update_model_bindings({"p01": bind()})
    assert session.model_bindings == {}


@async_test
async def test_update_bindings_validates_everything_before_applying_anything() -> None:
    session = make_session()
    settings = FakeSettings()
    orch = make_orch(session, settings=settings)

    with pytest.raises(ValueError, match="未知的参与者"):
        await orch.update_model_bindings({"p99": bind()})
    with pytest.raises(ProviderNotFound):
        await orch.update_model_bindings({"p01": bind(), "p02": bind(pid="ghost")})
    with pytest.raises(ModelNotFound):
        await orch.update_model_bindings({"p01": bind("nope")})
    with pytest.raises(ValueError):
        await orch.update_model_bindings({"p01": {"provider_id": "prov", "model_id": "big", "reasoning_depth": "max"}})
    assert session.model_bindings == {} and settings.defaults == {}

    settings.fail_save_defaults = True
    with pytest.raises(RuntimeError, match="disk full"):
        await orch.update_model_bindings({"p01": bind()})
    assert session.model_bindings == {}  # nothing half-applied


@async_test
async def test_update_bindings_merges_normalizes_window_and_persists_defaults() -> None:
    session = make_session()
    settings = FakeSettings({"p03": bind("big")})
    orch = make_orch(session, settings=settings)
    events: list[str] = []

    async def record(event: str = "x") -> None:
        events.append(event)

    orch.broadcast_state = record  # type: ignore[method-assign]

    result = await orch.update_model_bindings({"p01": bind("small"), "p02": bind("big", window=200_000)})

    assert result["p01"].context_window_tokens == 8000  # capped by the model's real window
    assert result["p02"].context_window_tokens == 200_000  # user may lower it
    assert set(settings.defaults) == {"p01", "p02", "p03"}  # merged into saved defaults
    assert session.model_bindings["p01"] == settings.defaults["p01"]
    assert events == ["bindings_updated"]
    assert {i.actor_id for i in session.binding_issues} == {"p03"}  # p03 still unbound in-game

    await orch.update_model_bindings({"p02": None})  # None removes
    assert "p02" not in session.model_bindings and "p02" not in settings.defaults


@async_test
async def test_changing_the_moderator_model_strips_provider_private_and_reissues_ids() -> None:
    session = make_session(scenario=False)
    session.model_bindings = {"moderator": bind("small")}
    session.moderator_messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "_provider_private": {"provider_id": "prov", "x": 1},
         "tool_calls": [{"id": "toolu_ABC", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "toolu_ABC", "content": "done"},
    ]
    orch = make_orch(session)

    await orch.update_model_bindings({"moderator": bind("small")})  # same model: untouched
    assert session.moderator_messages[1]["tool_calls"][0]["id"] == "toolu_ABC"
    assert "_provider_private" in session.moderator_messages[1]

    await orch.update_model_bindings({"moderator": bind("big")})
    assistant, tool = session.moderator_messages[1], session.moderator_messages[2]
    assert "_provider_private" not in assistant
    assert assistant["tool_calls"][0]["id"] == "call_1" and tool["tool_call_id"] == "call_1"
    assert tool["content"] == "done"


# ----------------------------------------------------------- defaults for new games ---

@async_test
async def test_new_game_starts_from_default_bindings_for_existing_actors_only() -> None:
    orch = GameOrchestrator()
    orch.session.status = "idle"
    orch.provider_store = FakeProviders(provider())
    orch.settings_store = FakeSettings({"p01": bind("big"), "moderator": bind("small"), "p99": bind("big")})
    orch._subscribers = set()

    game = await orch.new_game()
    assert set(game.model_bindings) == {"p01", "moderator"}  # p99 is not an actor of this game
    game.model_bindings["p01"].reasoning_depth = "low"
    assert orch.settings_store.defaults["p01"].reasoning_depth == "high"  # no aliasing of defaults
    assert game.schema_version == 2 and game.version.startswith("v0.1")  # from config.yml app.version


# -------------------------------------------------------------- schema migration ---

def v1_payload() -> dict[str, Any]:
    payload = GameSession(status="running", phase_text="old game").model_dump(mode="json")
    for key in ("schema_version", "model_bindings", "binding_issues", "restored_from_save",
                "restore_notice", "autosave_error"):
        payload.pop(key)
    payload["version"] = payload["engine_version"] = "v0.9"
    payload["usage_stats"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                              "total_cost_usd": 1.5, "input_price_per_m": 0.27,
                              "output_price_per_m": 1.1, "context_window_tokens": 1_000_000}
    return payload


def test_v1_payload_migrates_to_schema_2_without_inventing_bindings_or_mutating_input() -> None:
    payload = v1_payload()
    original = copy.deepcopy(payload)

    migrated = migrate_session_payload(payload)

    assert payload == original  # input untouched
    assert migrated["schema_version"] == 2 and migrated["version"] == APP_ENGINE_VERSION
    assert migrated["model_bindings"] == {}
    session = GameSession.model_validate(migrated)
    assert session.usage_stats.total_cost_usd == 1.5  # historical cost preserved as-is
    assert session.usage_stats.by_model == {} and session.usage_stats.by_actor == {}


def test_v2_payload_roundtrips_and_transient_fields_are_not_trusted() -> None:
    session = make_session()
    session.model_bindings = {"p01": bind()}
    data = session.model_dump(mode="json")
    data["binding_issues"] = [{"actor_id": "p01", "message": "forged"}]
    data["autosave_error"] = "forged"
    migrated = migrate_session_payload(data)
    assert "binding_issues" not in migrated and "autosave_error" not in migrated
    loaded = GameSession.model_validate(migrated)
    assert loaded.model_bindings["p01"] == bind() and loaded.binding_issues == []


@pytest.mark.parametrize("bad", [3, "2", True, None])
def test_unsupported_schema_versions_are_rejected(bad: Any) -> None:
    payload = v1_payload()
    payload["schema_version"] = bad
    with pytest.raises((ValueError, SessionLoadError)):
        GameOrchestrator._validate_session_data(payload)


def test_corrupt_bindings_in_a_save_fail_validation_cleanly() -> None:
    payload = v1_payload()
    payload["model_bindings"] = {"p01": {"provider_id": "x", "model_id": "y", "api_key": "leak"}}
    with pytest.raises(SessionLoadError):
        GameOrchestrator._validate_session_data(payload)  # extra fields are forbidden

    payload["model_bindings"] = {"p01": "not-an-object"}
    with pytest.raises(SessionLoadError):
        GameOrchestrator._validate_session_data(payload)


def test_saved_game_contains_only_ids_never_keys_or_urls() -> None:
    session = make_session()
    session.model_bindings = {"p01": bind()}
    text = json.dumps(session.model_dump(mode="json"))
    for forbidden in ("api_key", "base_url", "example.invalid", "Authorization"):
        assert forbidden not in text
