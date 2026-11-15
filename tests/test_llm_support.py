"""Shared fakes for the ``tests/test_llm_*.py`` suites (no network access).

This module has no tests of its own; it provides a scripted fake of
``AsyncOpenAI`` plus helpers for building stream chunks and API errors.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError

REQUEST_URL = "https://example.test/chat/completions"

#: Where each protocol adapter looks up its SDK client class (patch these, never the network).
CHAT_SDK = "app.llm.openai_chat.AsyncOpenAI"
RESPONSES_SDK = "app.llm.openai_responses.AsyncOpenAI"
ANTHROPIC_SDK = "app.llm.anthropic_messages.AsyncAnthropic"


def make_request() -> httpx.Request:
    return httpx.Request("POST", REQUEST_URL)


def connection_error() -> APIConnectionError:
    return APIConnectionError(request=make_request())


def status_error(
    status: int,
    message: str = "boom",
    headers: dict[str, str] | None = None,
    body: Any = None,
) -> APIStatusError:
    response = httpx.Response(status, request=make_request(), headers=headers or {})
    return APIStatusError(
        message,
        response=response,
        body=body if body is not None else {"error": {"message": message}},
    )


def tc_delta(
    index: int,
    *,
    id: str | None = None,
    name: str | None = None,
    args: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        index=index,
        id=id,
        function=SimpleNamespace(name=name, arguments=args),
    )


def make_chunk(
    content: str | None = None,
    *,
    reasoning: str | None = None,
    tool_calls: list[Any] | None = None,
    finish: str | None = None,
    usage: tuple[int, int] | None = None,
    no_choices: bool = False,
) -> SimpleNamespace:
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        model_extra=None,
    )
    choices = [] if no_choices else [SimpleNamespace(delta=delta, finish_reason=finish)]
    usage_obj = (
        SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1])
        if usage
        else None
    )
    return SimpleNamespace(choices=choices, usage=usage_obj)


class FakeStream:
    """Async iterator over chunks; an ``Exception`` item is raised when reached."""

    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)

    def __aiter__(self) -> "FakeStream":
        return self

    async def __anext__(self) -> Any:
        if not self._items:
            raise StopAsyncIteration
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _Completions:
    def __init__(self, owner: "FakeAsyncOpenAI") -> None:
        self._owner = owner

    async def create(self, **kwargs: Any) -> FakeStream:
        owner = self._owner
        owner.calls.append(kwargs)
        gate = owner.gate
        if gate is not None:
            await gate.wait()
        if not owner.script:
            raise AssertionError("FakeAsyncOpenAI: script exhausted")
        step = owner.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return FakeStream(step)


class FakeAsyncOpenAI:
    """Scripted stand-in for ``AsyncOpenAI``.

    ``script`` is a list consumed one entry per ``create`` call. An entry is
    either an exception (raised by ``create``) or a list of stream items
    (chunks, or exceptions raised mid-stream).
    """

    instances: list["FakeAsyncOpenAI"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.script: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        self.gate: Any = None
        self.closed = 0
        self.chat = SimpleNamespace(completions=_Completions(self))
        # Responses API and Anthropic Messages share the same scripted ``create``.
        self.responses = _Completions(self)
        self.messages = _Completions(self)
        FakeAsyncOpenAI.instances.append(self)

    async def close(self) -> None:
        self.closed += 1


class SharedScriptFactory:
    """Callable replacing ``AsyncOpenAI``; every instance shares one script."""

    def __init__(self, script: list[Any] | None = None) -> None:
        self.script: list[Any] = list(script or [])
        self.instances: list[FakeAsyncOpenAI] = []
        self.gate: Any = None

    def __call__(self, **kwargs: Any) -> FakeAsyncOpenAI:
        inst = FakeAsyncOpenAI(**kwargs)
        inst.script = self.script  # shared list
        inst.gate = self.gate
        self.instances.append(inst)
        return inst


def base_cfg(**llm_overrides: Any) -> dict[str, Any]:
    llm: dict[str, Any] = {
        "api_key": "test-key",
        "default_model": "test-model",
        "thinking": {"enabled": True, "reasoning_effort": "high"},
        "retry": {"jitter": 0},
    }
    llm.update(llm_overrides)
    return {"llm": llm}


def ns(**kwargs: Any) -> SimpleNamespace:
    """Attribute bag used to fake SDK event / model objects."""
    return SimpleNamespace(**kwargs)


def ev(type: str, **kwargs: Any) -> SimpleNamespace:
    """A stream event (Responses API / Anthropic Messages)."""
    return SimpleNamespace(type=type, **kwargs)


class FakeStore:
    """Minimal ``ProviderStore`` stand-in: ``resolve(binding) -> (provider, model)``."""

    def __init__(self, *providers: Any) -> None:
        self.providers = {p.id: p for p in providers}

    def resolve(self, binding: Any) -> tuple[Any, Any]:
        from app.providers.models import ModelNotFound, ProviderNotFound

        provider = self.providers.get(binding.provider_id)
        if provider is None:
            raise ProviderNotFound(binding.provider_id)
        for model in provider.models:
            if model.id == binding.model_id:
                return provider, model
        raise ModelNotFound(binding.model_id)


def make_provider(
    protocol: str = "openai_chat",
    *,
    id: str = "prov1",
    base_url: str = "https://api.example.test/v1",
    api_key: str = "sk-test-secret",
    models: tuple[tuple[str, float, float, int], ...] = (("model-a", 1.5, 6.0, 200_000),),
    name: str = "测试 Provider",
) -> Any:
    from app.providers.models import ModelEntry, ProviderConfig

    return ProviderConfig(
        id=id,
        name=name,
        protocol=protocol,  # type: ignore[arg-type]
        base_url=base_url,
        api_key=api_key,
        models=[
            ModelEntry(
                id=m, input_price_per_m=i, output_price_per_m=o, context_window_tokens=w
            )
            for m, i, o, w in models
        ],
    )


def make_binding(
    provider_id: str = "prov1",
    model_id: str = "model-a",
    depth: str = "high",
    window: int = 1_000_000,
) -> Any:
    from app.providers.models import ModelBinding

    return ModelBinding(
        provider_id=provider_id,
        model_id=model_id,
        reasoning_depth=depth,  # type: ignore[arg-type]
        context_window_tokens=window,
    )
