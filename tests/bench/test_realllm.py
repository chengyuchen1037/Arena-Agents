"""``bench.realllm``: the real-model mode - built from flags and one environment variable, never from the app's provider table, never sending
anything until a call is made (these tests never make one: the transport is a stub)."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from bench_helpers import bots, example

from app.engine.llm import LLMClient
from app.llm.types import LLMResponse
from app.providers.models import ModelBinding, ModelNotFound, ProviderNotFound
from app.runtime.llm_gate import GatedEngineLlm
from bench.fakes import FakeLlm, prose
from bench.realllm import (
    DEFAULT_KEY_ENV,
    ENV_NAME,
    PROTOCOLS,
    InlineProviderStore,
    RealConfig,
    RealModeError,
    build_real_port,
    load_provider_entry,
    safe_url,
)
from bench.runner import Budget, Job, RunConfig, run_suite

KEY = "sk-test-0123456789abcdef"
ENV = {DEFAULT_KEY_ENV: KEY}


def config(**over: Any) -> RealConfig:
    return RealConfig(**{"base_url": "https://api.example.test/v1", "model": "m-1", **over})


# ------------------------------------------------------------------------------------------------ what is asked for


def test_a_url_is_written_down_without_user_info_query_or_fragment() -> None:
    assert safe_url("https://user:pw@api.example.test:8443/v1/chat?key=abc&x=1#frag") == "https://api.example.test:8443/v1/chat"
    assert safe_url(" http://localhost:8000/v1 ") == "http://localhost:8000/v1" and safe_url("http://[::1]:8000/v1") == "http://[::1]:8000/v1"
    assert safe_url("http://h:99999999/x") == "http://h/x" and safe_url("") == ""


@pytest.mark.parametrize(
    ("change", "text"),
    [
        ({"base_url": ""}, "需要 --base-url"),
        ({"base_url": "api.example.test/v1"}, "--base-url 要以 http:// 或 https:// 开头"),
        ({"base_url": "ftp://x/y"}, "--base-url 要以 http:// 或 https:// 开头"),
        ({"model": " "}, "需要 --model"),
        ({"protocol": "grpc"}, "--protocol 只能是 openai_chat、openai_responses、anthropic_messages"),
        ({"reasoning": "max"}, "--reasoning 只能是 low、medium、high、xhigh"),
        ({"api_key_env": "sk-abc"}, "不是 Key 本身"),
        ({"api_key_env": "has space"}, "不是 Key 本身"),
        ({"api_key_env": ""}, "不是 Key 本身"),
        ({"max_attempts": 0}, "--attempts 至少是 1"),
        ({"timeout_s": 0}, "--timeout 要大于 0"),
        ({"context_window": 0}, "--context-window 要大于 0"),
    ],
)
def test_a_configuration_a_real_run_cannot_start_with_says_what_to_pass(change: dict[str, Any], text: str) -> None:
    with pytest.raises(RealModeError, match=text):
        config(**change).check()
    with pytest.raises(RealModeError):
        build_real_port(config(**change), environ=ENV)


def test_a_key_that_was_passed_as_the_variable_name_is_not_printed_back() -> None:
    with pytest.raises(RealModeError) as info:
        config(api_key_env="sk-secret-0123456789").check()
    assert "sk-secret-0123456789" not in str(info.value)
    assert ENV_NAME.match("ARENA_BENCH_API_KEY") and ENV_NAME.match("_x1") and not ENV_NAME.match("1x") and not ENV_NAME.match("a-b")
    assert PROTOCOLS == ("openai_chat", "openai_responses", "anthropic_messages")


def test_what_a_run_records_of_its_model_has_no_key_and_no_user_info() -> None:
    desc = config(base_url="https://me:hunter2@api.example.test/v1?token=zzz", input_price_per_m=0.5, output_price_per_m=2.0).describe()
    assert desc["base_url"] == "https://api.example.test/v1" and desc["model"] == "m-1" and desc["api_key_env"] == DEFAULT_KEY_ENV and desc["max_attempts"] == 3
    text = json.dumps(desc)
    assert "hunter2" not in text and "token=zzz" not in text and KEY not in text


# ------------------------------------------------------------------------------------------------ a provider the app already knows

PROVIDERS = {
    "schema_version": 1,
    "providers": [
        {"id": "deep", "name": "DeepSeek", "protocol": "openai_chat", "base_url": "https://api.deepseek.test", "auto_append_path": False,
         "api_key": "sk-IN-THE-FILE-0123456789", "source": "user",
         "models": [{"id": "flash", "input_price_per_m": 0.27, "output_price_per_m": 1.1, "context_window_tokens": 128000},
                    {"id": "pro", "input_price_per_m": 1.0, "output_price_per_m": 4.0}]},
        {"id": "anth", "name": "Anthropic", "protocol": "anthropic_messages", "base_url": "https://anth.example.test", "models": []},
    ],
}


def write_providers(path: Path, payload: Any = PROVIDERS) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_non_secret_part_of_a_provider_is_read_from_a_file_the_user_names(tmp_path: Path) -> None:
    path = write_providers(tmp_path / "providers.json")
    before = path.read_bytes()
    flash = load_provider_entry(path, "deep")  # (no model named: the provider's first)
    assert flash == {"base_url": "https://api.deepseek.test", "protocol": "openai_chat", "auto_append_path": False, "model": "flash", "context_window": 128000,
                     "input_price_per_m": 0.27, "output_price_per_m": 1.1}
    pro = load_provider_entry(path, "deep", "pro")
    assert (pro["model"], pro["context_window"], pro["input_price_per_m"], pro["output_price_per_m"]) == ("pro", 1_000_000, 1.0, 4.0)  # (the app's default window)
    assert "sk-IN-THE-FILE" not in json.dumps(flash) and "api_key" not in flash  # (a key in the file is never used, as the app itself ignores it)
    assert path.read_bytes() == before  # (only read)


@pytest.mark.parametrize(
    ("provider", "model", "text"),
    [
        ("nope", "", "里没有 provider 'nope'。有：deep、anth"),
        ("deep", "slow", "provider deep 里没有模型 'slow'。有：flash、pro"),
        ("anth", "", "provider anth 里没有任何模型：用 --model 指明"),
    ],
)
def test_a_provider_the_file_does_not_have_is_named_with_what_it_does_have(provider: str, model: str, text: str, tmp_path: Path) -> None:
    with pytest.raises(RealModeError, match=text):
        load_provider_entry(write_providers(tmp_path / "p.json"), provider, model)


def test_a_providers_file_that_cannot_be_read_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(RealModeError, match="读不了 --providers-file nowhere.json"):
        load_provider_entry(tmp_path / "nowhere.json", "deep")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RealModeError, match="读不了 --providers-file broken.json（JSONDecodeError）"):
        load_provider_entry(tmp_path / "broken.json", "deep")
    for payload in ([], {"providers": "x"}, {"schema_version": 1}):
        with pytest.raises(RealModeError, match="格式不对"):
            load_provider_entry(write_providers(tmp_path / "odd.json", payload), "deep")


def test_the_address_switch_of_a_provider_reaches_the_client(tmp_path: Path) -> None:
    entry = load_provider_entry(write_providers(tmp_path / "p.json"), "deep")
    real = build_real_port(config(base_url=entry["base_url"], auto_append_path=entry["auto_append_path"], provider="deep"), environ=ENV)
    provider, _model = real.client.provider_store.resolve(real.binding)
    assert provider.auto_append_path is False and provider.base_url == "https://api.deepseek.test"
    assert config(provider="deep").describe()["provider"] == "deep" and config().describe()["provider"] == ""


# ------------------------------------------------------------------------------------------------ the port


def test_the_port_comes_from_the_flags_and_one_environment_variable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARENA_DATA_DIR", str(tmp_path))

    def boom() -> Any:
        raise AssertionError("the app's provider table must not be opened")

    import app.providers.store as store

    monkeypatch.setattr(store, "get_provider_store", boom)
    real = build_real_port(config(api_key_env="MY_KEY", reasoning="low", context_window=200_000, timeout_s=77.0, max_attempts=2, temperature=0.3), environ={"MY_KEY": f"  {KEY}\n"})
    assert isinstance(real.port, GatedEngineLlm) and isinstance(real.client, LLMClient) and real.secrets == (KEY,)  # (the key as the variable holds it, trimmed)
    assert isinstance(real.binding, ModelBinding) and (real.binding.provider_id, real.binding.model_id, real.binding.reasoning_depth) == ("bench", "m-1", "low")
    target = real.client.build_target(real.binding)
    assert (target.provider_id, target.protocol, target.base_url, target.model_id, target.api_key) == ("bench", "openai_chat", "https://api.example.test/v1", "m-1", KEY)
    assert target.reasoning_depth == "low" and target.context_window_tokens == 200_000 and target.temperature == 0.3 and target.extra_body == {}  # (not a DeepSeek host)
    assert real.client.retry_max_attempts == 2 and real.client.timeout == 77.0 and real.port.timeout_s == 77.0 and real.port.binding_for("N3") is real.binding
    assert list(tmp_path.iterdir()) == []  # (nothing was written to the data directory, and nothing was read from the app's stores)


def test_a_deepseek_endpoint_gets_the_thinking_switch_and_others_do_not() -> None:
    deepseek = build_real_port(config(base_url="https://api.deepseek.com"), environ=ENV).client.build_target(ModelBinding(provider_id="bench", model_id="m-1"))
    assert deepseek.extra_body == {"thinking": {"type": "enabled"}}
    anthropic = build_real_port(config(protocol="anthropic_messages", base_url="https://api.anthropic.example"), environ=ENV)
    assert anthropic.client.build_target(anthropic.binding).protocol == "anthropic_messages"


def test_a_missing_or_blank_key_stops_the_run_and_names_the_variable_not_the_value() -> None:
    for env in ({}, {DEFAULT_KEY_ENV: ""}, {DEFAULT_KEY_ENV: "   "}):
        with pytest.raises(RealModeError, match=f"环境变量 {DEFAULT_KEY_ENV} 里没有 API Key"):
            build_real_port(config(), environ=env)
    with pytest.raises(RealModeError, match="环境变量 OTHER_KEY 里没有 API Key"):
        build_real_port(config(api_key_env="OTHER_KEY"), environ=ENV)  # (the default variable is set, the one asked for is not)


def test_the_inline_store_knows_exactly_its_provider_and_its_model() -> None:
    real = build_real_port(config(), environ=ENV)
    store = real.client.provider_store
    assert isinstance(store, InlineProviderStore)
    provider, model = store.resolve(real.binding)
    assert provider.id == "bench" and provider.api_key == KEY and model.id == "m-1" and KEY not in repr(provider)  # (the key is not in a repr)
    with pytest.raises(ProviderNotFound):
        store.resolve(ModelBinding(provider_id="deepseek", model_id="m-1"))
    with pytest.raises(ModelNotFound):
        store.resolve(ModelBinding(provider_id="bench", model_id="other"))


def test_the_real_mode_module_does_not_import_the_apps_stores() -> None:
    import bench.realllm as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not [m for m in imported if m.startswith(("app.storage", "app.providers.store", "app.settings"))], sorted(imported)
    assert "app.providers.models" in imported  # (the data classes of a provider: no state)


# ------------------------------------------------------------------------------------------------ through the production port, with a stub for the transport


def stub_transport(real: Any, scene_llm: FakeLlm, calls: list[dict[str, Any]]) -> None:
    """Replace what ``LLMClient`` does over the network by an answer from the fake model - everything above it (``GatedEngineLlm``, the guard, the
    agent) is the production code."""

    async def chat_stream(messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        calls.append({"messages": messages, **kwargs})
        resp = await scene_llm.complete("N3", messages, kwargs.get("tools"), max_tokens=kwargs.get("max_tokens_override"))
        resp.provider_id, resp.model_id = "bench", "m-1"
        return resp

    real.client.chat_stream = chat_stream


def test_a_run_through_the_production_port_counts_every_call_and_says_it_was_real() -> None:
    real = build_real_port(config(input_price_per_m=1.0, output_price_per_m=4.0), environ=ENV)
    calls: list[dict[str, Any]] = []

    def factory(job: Job, scene: Any) -> Any:
        stub_transport(real, FakeLlm(prose, world=scene.world, seat=scene.seat, bot=job.bot.label, situation=job.situation.id), calls)
        return real.port

    report = asyncio.run(run_suite([example("vote")], bots("legacy"), RunConfig(mode="real", model="m-1", run_id="r", max_calls=5), factory, budget=Budget(5), secrets=real.secrets))
    row = report.rows[0]
    assert (row["mode"], row["model"], row["status"], row["rounds"]) == ("real", "m-1", "ok", 1) and report.calls == 1 and len(calls) == 1
    call = calls[0]
    assert call["binding"] is real.binding and call["is_moderator"] is False and call["on_chunk"] is None and call["max_tokens_override"]  # (the spec's own limit)
    assert [t["function"]["name"] for t in call["tools"]][:2] == ["respond", "sleep"] or "respond" in [t["function"]["name"] for t in call["tools"]]
    assert call["messages"][0]["role"] == "system" and KEY not in json.dumps(call["messages"], ensure_ascii=False)  # (the prompt never carries the key)


def test_the_budget_stops_a_real_run_before_the_call_that_would_exceed_it() -> None:
    real = build_real_port(config(), environ=ENV)
    calls: list[dict[str, Any]] = []

    def factory(job: Job, scene: Any) -> Any:
        stub_transport(real, FakeLlm(prose, world=scene.world, seat=scene.seat, bot=job.bot.label, situation=job.situation.id, sample=job.sample), calls)
        return real.port

    budget = Budget(2)
    report = asyncio.run(run_suite([example("vote")], bots("legacy", "bot:Alice"), RunConfig(samples=3, mode="real", model="m-1", run_id="r", max_calls=2), factory, budget=budget))
    assert len(calls) == 2 == budget.calls == report.calls and len(report.rows) == 2 and report.skipped == 4 and report.by_status() == {"ok": 2}


def test_an_error_text_that_carries_the_key_is_scrubbed_from_the_row() -> None:
    real = build_real_port(config(), environ=ENV)

    async def failing(messages: Any, **kwargs: Any) -> LLMResponse:
        raise RuntimeError(f"401 Unauthorized: key {KEY} is not valid")

    real.client.chat_stream = failing
    report = asyncio.run(run_suite([example("vote")], bots("legacy"), RunConfig(mode="real", model="m-1", run_id="r", agent_retries=0), lambda j, s: real.port, secrets=real.secrets))
    row = report.rows[0]
    assert row["status"] == "error" and row["errors"] and "401 Unauthorized" in row["errors"][0]["message"]
    assert KEY not in json.dumps(row, ensure_ascii=False) and "***" in row["errors"][0]["message"]


def test_the_answer_of_a_real_model_that_makes_no_tool_call_is_no_decision() -> None:
    real = build_real_port(config(), environ=ENV)

    async def chatty(messages: Any, **kwargs: Any) -> LLMResponse:
        return LLMResponse(thought="想了很久。", content="我觉得 N1 可疑。", tool_calls=[], prompt_tokens=100, completion_tokens=20, finish_reason="stop", provider_id="bench", model_id="m-1")

    real.client.chat_stream = chatty
    report = asyncio.run(run_suite([example("speak")], bots("bot:Alice"), RunConfig(mode="real", model="m-1", run_id="r"), lambda j, s: real.port))
    row = report.rows[0]
    assert row["status"] == "no_decision" and row["decision"] is None and row["monologues"] and row["thoughts"] and row["usage"]["prompt"] == 200
