"""Concurrency gate and timeout for every LLM call of a game.

Thirteen agents may want a model at the same moment; providers rate-limit and the machine has a
finite number of sockets.  :class:`LlmGate` bounds the number of concurrent calls globally and per
provider.  :class:`GatedEngineLlm` adapts the engine's streaming ``LLMClient`` to the runtime's
:class:`~app.runtime.ports.LlmPort`: it passes ``on_chunk=None`` (nothing is streamed to the UI, D5),
resolves the agent's model binding through ``binding_for(agent_id)``, marks the moderator and puts a
hard ``asyncio.timeout`` around every call.

Cancellation is safe: a cancelled call leaves the ``async with`` blocks, so its slots are released
immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from app.llm.types import LLMResponse
from app.world.types import MODERATOR_ID

DEFAULT_MAX_CONCURRENT = 8
DEFAULT_PER_PROVIDER = 6
DEFAULT_TIMEOUT_S = 300.0
UNKNOWN_PROVIDER = "default"


class LlmGate:
    """Global and per-provider concurrency limits (``None`` or ``0`` = unlimited)."""

    def __init__(
        self,
        max_concurrent: int | None = DEFAULT_MAX_CONCURRENT,
        per_provider: int | None = DEFAULT_PER_PROVIDER,
        provider_limits: Mapping[str, int] | None = None,
    ) -> None:
        self.max_concurrent = max_concurrent or None
        self.per_provider = per_provider or None
        self.provider_limits: dict[str, int] = dict(provider_limits or {})
        self._global = asyncio.Semaphore(self.max_concurrent) if self.max_concurrent else None
        self._providers: dict[str, asyncio.Semaphore | None] = {}
        self.in_flight = 0  # calls holding a slot right now
        self.peak = 0  # highest ``in_flight`` seen
        self.waiting = 0  # calls queued for a slot
        self.in_flight_by_provider: dict[str, int] = {}
        self.peak_by_provider: dict[str, int] = {}

    def _provider_semaphore(self, provider_id: str) -> asyncio.Semaphore | None:
        if provider_id not in self._providers:
            limit = self.provider_limits.get(provider_id, self.per_provider)
            self._providers[provider_id] = asyncio.Semaphore(limit) if limit else None
        return self._providers[provider_id]

    @contextlib.asynccontextmanager
    async def slot(self, provider_id: str | None = None) -> AsyncIterator[None]:
        """Hold one call slot (the provider's first, then the global one).

        The provider slot is taken first so that calls waiting for a busy provider do not hold a
        global slot that other providers could use.  Every caller uses the same order, so there is no
        deadlock.
        """
        provider = provider_id or UNKNOWN_PROVIDER
        semaphore = self._provider_semaphore(provider)
        self.waiting += 1
        entered = False
        try:
            async with contextlib.AsyncExitStack() as stack:
                if semaphore is not None:
                    await stack.enter_async_context(semaphore)
                if self._global is not None:
                    await stack.enter_async_context(self._global)
                self.waiting -= 1
                entered = True
                self.in_flight += 1
                self.peak = max(self.peak, self.in_flight)
                now = self.in_flight_by_provider.get(provider, 0) + 1
                self.in_flight_by_provider[provider] = now
                self.peak_by_provider[provider] = max(self.peak_by_provider.get(provider, 0), now)
                try:
                    yield
                finally:
                    self.in_flight -= 1
                    self.in_flight_by_provider[provider] -= 1
        finally:
            if not entered:
                self.waiting -= 1


class GatedEngineLlm:
    """:class:`~app.runtime.ports.LlmPort` over the engine's ``LLMClient`` (production).

    ``client`` only needs ``async chat_stream(messages, *, is_moderator, tools, on_chunk,
    max_tokens_override, binding) -> LLMResponse``.  ``binding_for(agent_id)`` returns the agent's
    ``ModelBinding`` (``None`` falls back to the client's own config); its ``provider_id`` selects
    the per-provider limit.  The timeout covers the call itself (including the client's internal
    retries), not the time spent waiting for a slot.
    """

    def __init__(
        self,
        client: Any,
        binding_for: Callable[[str], Any],
        gate: LlmGate | None = None,
        timeout_s: float | None = DEFAULT_TIMEOUT_S,
        *,
        moderator_id: str = MODERATOR_ID,
    ) -> None:
        self.client = client
        self.binding_for = binding_for
        self.gate = gate if gate is not None else LlmGate()
        self.timeout_s = timeout_s if timeout_s and timeout_s > 0 else None
        self.moderator_id = moderator_id

    async def complete(
        self,
        agent_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        binding = self.binding_for(agent_id)
        provider_id = getattr(binding, "provider_id", None) or None
        async with self.gate.slot(provider_id):
            async with asyncio.timeout(self.timeout_s):
                return await self.client.chat_stream(
                    messages,
                    is_moderator=agent_id == self.moderator_id,
                    tools=tools or None,
                    on_chunk=None,
                    max_tokens_override=max_tokens,
                    binding=binding,
                )
