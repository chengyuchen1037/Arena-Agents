"""The real-model mode of the benchmark (off by default; see ``python -m bench run --help``).

A real run reaches a model with what the user passes *explicitly* - ``--base-url``, ``--model``, ``--protocol`` and the name of an
environment variable that holds the key (``--api-key-env``, ``ARENA_BENCH_API_KEY`` by default).  The provider table of the app (its encrypted
keys, its bindings) is never opened: the engine's own ``LLMClient`` is given an in-memory provider store with that one provider, so the request
code that real games use - protocol adapters, streaming, retries, reasoning depth - is what a benchmark call goes through, and nothing else.  The
key lives only in the client; no result, log or error message carries it (``bench.safety.scrub``).

The cost brake is :class:`bench.runner.Budget` (``--max-calls``, required with ``--real``): the benchmark never makes a call it did not
count.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

PROTOCOLS = ("openai_chat", "openai_responses", "anthropic_messages")
REASONING = ("low", "medium", "high", "xhigh")
PROVIDER_ID = "bench"
DEFAULT_KEY_ENV = "ARENA_BENCH_API_KEY"


class RealModeError(RuntimeError):
    """The real-model mode cannot start (a missing flag, a missing key): says what to pass."""


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
    input_price_per_m: float = 0.0  # USD per million tokens (only for the cost estimate in run.json)
    output_price_per_m: float = 0.0
    secrets: tuple[str, ...] = field(default=(), repr=False)

    def check(self) -> None:
        if not self.base_url.strip():
            raise RealModeError("真实模型模式需要 --base-url（模型服务的地址）。")
        if not self.model.strip():
            raise RealModeError("真实模型模式需要 --model（模型 id）。")
        if self.protocol not in PROTOCOLS:
            raise RealModeError(f"--protocol 只能是 {'、'.join(PROTOCOLS)}。")
        if self.reasoning not in REASONING:
            raise RealModeError(f"--reasoning 只能是 {'、'.join(REASONING)}。")

    def describe(self) -> dict[str, Any]:
        """What ``run.json`` records (never the key)."""
        return {
            "base_url": self.base_url, "model": self.model, "protocol": self.protocol, "reasoning": self.reasoning,
            "context_window": self.context_window, "api_key_env": self.api_key_env,
            "input_price_per_m": self.input_price_per_m, "output_price_per_m": self.output_price_per_m,
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
    the environment variable ``config.api_key_env`` (never from an argument).  Raises :class:`RealModeError` when something is missing."""
    config.check()
    env = os.environ if environ is None else environ
    key = (env.get(config.api_key_env) or "").strip()
    if not key:
        raise RealModeError(f"环境变量 {config.api_key_env} 里没有 API Key。把 Key 放进这个环境变量再运行（不要写在命令行里）。")
    from app.engine.llm import LLMClient
    from app.providers.models import ModelBinding, ModelEntry, ProviderConfig
    from app.runtime.llm_gate import GatedEngineLlm, LlmGate

    provider = ProviderConfig(
        id=PROVIDER_ID, name="bench", protocol=config.protocol, base_url=config.base_url, api_key=key,  # type: ignore[arg-type]
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
                "thinking": {"enabled": True, "reasoning_effort": config.reasoning, "extra_body": {"thinking": {"type": "enabled"}}},
            }
        },
        provider_store=InlineProviderStore(provider),
    )
    port = GatedEngineLlm(client, lambda _agent: binding, LlmGate(max_concurrent or None), timeout_s=config.timeout_s)
    return RealPort(port=port, client=client, binding=binding, secrets=(key,))


__all__ = [
    "DEFAULT_KEY_ENV",
    "InlineProviderStore",
    "PROTOCOLS",
    "REASONING",
    "RealConfig",
    "RealModeError",
    "RealPort",
    "build_real_port",
]
