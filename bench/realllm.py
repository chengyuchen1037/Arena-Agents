"""The real-model mode of the benchmark (off by default; see ``python -m bench run --help``).

A real run reaches a model with what the user passes *explicitly* - ``--base-url``, ``--model``, ``--protocol`` and the name of an
environment variable that holds the key (``--api-key-env``, ``ARENA_BENCH_API_KEY`` by default) - or, to reuse a provider the app already knows,
``--providers-file <providers.json> --provider <id>``: the address, protocol, model, context window and prices are read from that one file (only
read; the format of ``<data>/providers.json``) and the key still comes from the environment variable, never from the file.  The provider *store*
of the app (its encrypted keys, its stores in the data directory) is never opened: the engine's own ``LLMClient`` is given an in-memory provider
store with that one provider, so the request code that real games use - protocol adapters, streaming, retries, reasoning depth - is what a
benchmark call goes through, and nothing else.  The key lives only in the client; no result, log or error message carries it
(``bench.safety.scrub``), and a base URL is recorded without any user-info or query string (``safe_url``).

The cost brake is :class:`bench.runner.Budget` (``--max-calls``, required with ``--real``): every model call the agent loop makes is counted
before it is made, and the run makes no more than the budget allows.  What the budget does not count is the *re-sending* of one call by the
client when the service answers 429 / 5xx or the connection drops - at most ``max_attempts`` HTTP requests per counted call (``--attempts``,
3 by default); a request that failed produces no answer and is not billed.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

PROTOCOLS = ("openai_chat", "openai_responses", "anthropic_messages")
REASONING = ("low", "medium", "high", "xhigh")
PROVIDER_ID = "bench"
DEFAULT_KEY_ENV = "ARENA_BENCH_API_KEY"
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class RealModeError(RuntimeError):
    """The real-model mode cannot start (a missing flag, a missing key): says what to pass."""


def safe_url(url: str) -> str:
    """``url`` as it may be written down: scheme, host, port and path - no user-info (``user:pass@``), no query string, no fragment."""
    parts = urlsplit(url.strip())
    try:
        port = parts.port
    except ValueError:
        port = None
    host = parts.hostname or ""
    if ":" in host:  # an IPv6 literal
        host = f"[{host}]"
    return urlunsplit((parts.scheme, host + (f":{port}" if port else ""), parts.path, "", ""))


@dataclass(frozen=True)
class RealConfig:
    """The model a real run talks to.  The key is not part of it (it is read from the environment when the port is built)."""

    base_url: str
    model: str
    protocol: str = "openai_chat"
    api_key_env: str = DEFAULT_KEY_ENV
    reasoning: str = "high"
    context_window: int = 1_000_000
    timeout_s: float = 180.0
    temperature: float = 0.7
    max_attempts: int = 3  # HTTP requests per counted call (the client re-sends a request the service answered with 429 / 5xx)
    input_price_per_m: float = 0.0  # USD per million tokens (only for the cost estimate)
    output_price_per_m: float = 0.0
    auto_append_path: bool = True  # the provider's "the address is a base URL, the client adds the path" switch
    provider: str = ""  # the entry of ``--providers-file`` the values came from (informational)

    def check(self) -> None:
        """Raise :class:`RealModeError` (in Chinese, saying what to pass) for anything a real run cannot start with.  Never echoes a key."""
        if not self.base_url.strip():
            raise RealModeError("真实模型模式需要 --base-url（模型服务的地址，或环境变量 ARENA_BENCH_BASE_URL）。")
        if urlsplit(self.base_url.strip()).scheme not in ("http", "https"):
            raise RealModeError("--base-url 要以 http:// 或 https:// 开头。")
        if not self.model.strip():
            raise RealModeError("真实模型模式需要 --model（模型 id，或环境变量 ARENA_BENCH_MODEL）。")
        if self.protocol not in PROTOCOLS:
            raise RealModeError(f"--protocol 只能是 {'、'.join(PROTOCOLS)}。")
        if self.reasoning not in REASONING:
            raise RealModeError(f"--reasoning 只能是 {'、'.join(REASONING)}。")
        if not ENV_NAME.match(self.api_key_env):
            # (somebody passed the key itself: do not print it back)
            raise RealModeError("--api-key-env 要写放着 Key 的环境变量的名字（字母、数字、下划线），不是 Key 本身。")
        if self.max_attempts < 1 or self.timeout_s <= 0 or self.context_window <= 0:
            raise RealModeError("--attempts 至少是 1，--timeout 要大于 0，--context-window 要大于 0。")

    def describe(self) -> dict[str, Any]:
        """What ``run.json`` records (never the key, never a user-info part of the URL)."""
        return {
            "base_url": safe_url(self.base_url), "model": self.model, "protocol": self.protocol, "reasoning": self.reasoning,
            "context_window": self.context_window, "api_key_env": self.api_key_env, "max_attempts": self.max_attempts,
            "temperature": self.temperature, "input_price_per_m": self.input_price_per_m, "output_price_per_m": self.output_price_per_m,
            "provider": self.provider,
        }


def load_provider_entry(path: str | Path, provider_id: str, model_id: str = "") -> dict[str, Any]:
    """The non-secret part of one provider, read from a providers file the user names (``--providers-file``; the format of ``<data>/providers.json``:
    ``{"schema_version": 1, "providers": [{"id", "protocol", "base_url", "models": [...], ...}]}``): address, protocol, the model's context window and
    prices.  **The key is never taken from the file** (an ``api_key`` in it is ignored, as the app itself does): it comes from the environment variable.
    ``model_id`` empty = the provider's first model.  The file is only read; raises :class:`RealModeError` (saying what is there) when it cannot be used."""
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise RealModeError(f"读不了 --providers-file {target.name}（{type(exc).__name__}）。") from None
    items = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise RealModeError('--providers-file 的格式不对：要像 <数据目录>/providers.json（{"schema_version": 1, "providers": [...]}）。')
    providers = [p for p in items if isinstance(p, dict) and isinstance(p.get("id"), str)]
    chosen = next((p for p in providers if p["id"] == provider_id), None)
    if chosen is None:
        raise RealModeError(f"--providers-file 里没有 provider {provider_id!r}。有：{'、'.join(p['id'] for p in providers) or '（没有）'}。")
    models = [m for m in chosen.get("models") or [] if isinstance(m, dict) and isinstance(m.get("id"), str)]
    entry = next((m for m in models if m["id"] == model_id), None) if model_id else (models[0] if models else None)
    if entry is None:
        have = "、".join(m["id"] for m in models) or "（没有）"
        raise RealModeError(f"provider {provider_id} 里没有模型 {model_id!r}。有：{have}。" if model_id else f"provider {provider_id} 里没有任何模型：用 --model 指明。")

    def number(value: Any, default: float) -> float:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default

    return {
        "base_url": str(chosen.get("base_url") or ""),
        "protocol": str(chosen.get("protocol") or "openai_chat"),
        "auto_append_path": chosen.get("auto_append_path") is not False,
        "model": entry["id"],
        "context_window": int(number(entry.get("context_window_tokens"), 1_000_000)),
        "input_price_per_m": number(entry.get("input_price_per_m"), 0.0),
        "output_price_per_m": number(entry.get("output_price_per_m"), 0.0),
    }


class InlineProviderStore:
    """The one-provider store ``LLMClient`` asks to resolve a binding (``resolve(binding) -> (provider, model)``): in memory, from the
    flags - the app's provider table is never read."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def resolve(self, binding: Any) -> tuple[Any, Any]:
        from app.providers.models import ModelNotFound, ProviderNotFound

        if getattr(binding, "provider_id", None) != self.provider.id:
            raise ProviderNotFound(f"基准里没有 provider {getattr(binding, 'provider_id', None)!r}。")
        for model in self.provider.models:
            if model.id == binding.model_id:
                return self.provider, model
        raise ModelNotFound(f"provider {self.provider.id} 里没有模型 {binding.model_id!r}。")


@dataclass
class RealPort:
    """The model port of a real run (``port`` is what the ``Agent`` calls) and what to close afterwards."""

    port: Any
    client: Any
    binding: Any
    secrets: tuple[str, ...]

    async def aclose(self) -> None:
        await self.client.aclose()


def build_real_port(config: RealConfig, *, environ: Mapping[str, str] | None = None, max_concurrent: int = 0) -> RealPort:
    """The port for ``config``: the engine's ``LLMClient`` over an in-memory provider, behind the usual concurrency gate.  The key is read from
    the environment variable ``config.api_key_env`` (never from an argument).  Nothing is sent: the HTTP client is made on the first call.
    Raises :class:`RealModeError` when something is missing."""
    config.check()
    env = os.environ if environ is None else environ
    key = (env.get(config.api_key_env) or "").strip()
    if not key:
        raise RealModeError(f"环境变量 {config.api_key_env} 里没有 API Key。把 Key 放进这个环境变量再运行（不要写在命令行里）。")
    from app.engine.llm import LLMClient
    from app.providers.models import ModelBinding, ModelEntry, ProviderConfig
    from app.runtime.llm_gate import GatedEngineLlm, LlmGate

    provider = ProviderConfig(
        id=PROVIDER_ID, name="bench", protocol=config.protocol, base_url=config.base_url.strip(), api_key=key,  # type: ignore[arg-type]
        auto_append_path=config.auto_append_path,
        models=[
            ModelEntry(
                id=config.model, input_price_per_m=config.input_price_per_m, output_price_per_m=config.output_price_per_m,
                context_window_tokens=config.context_window,
            )
        ],
        source="bench",
    )
    binding = ModelBinding(
        provider_id=PROVIDER_ID, model_id=config.model, reasoning_depth=config.reasoning,  # type: ignore[arg-type]
        context_window_tokens=config.context_window,
    )
    client = LLMClient(
        {
            "llm": {
                "temperature": config.temperature,
                "max_tokens": 8192,
                "timeout_seconds": config.timeout_s,
                "retry": {"max_attempts": config.max_attempts},
                # (``extra_body`` is only sent to DeepSeek's own endpoints: ``LLMClient.build_target``)
                "thinking": {"enabled": True, "reasoning_effort": config.reasoning, "extra_body": {"thinking": {"type": "enabled"}}},
            }
        },
        provider_store=InlineProviderStore(provider),
    )
    port = GatedEngineLlm(client, lambda _agent: binding, LlmGate(max_concurrent or None), timeout_s=config.timeout_s)
    return RealPort(port=port, client=client, binding=binding, secrets=(key,))


__all__ = [
    "DEFAULT_KEY_ENV",
    "ENV_NAME",
    "InlineProviderStore",
    "PROTOCOLS",
    "REASONING",
    "RealConfig",
    "RealModeError",
    "RealPort",
    "build_real_port",
    "load_provider_entry",
    "safe_url",
]
