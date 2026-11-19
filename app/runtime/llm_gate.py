"""Concurrency gate and timeout for every LLM call of a game.

v0.12.1: every delivered message wakes every agent that can see it, so thirteen agents may well want a
model at the same moment.  The gate is **not** a way to save money: by default it is open (``0`` = no
limit, nothing waits).  It exists for the provider's rate limits - lower ``llm_concurrency`` /
``llm_concurrency_per_provider`` in ``config.yml`` when a provider answers 429 too often (the client
retries those with a back-off).

:class:`LlmGate` bounds the number of concurrent calls globally and per provider.
:class:`GatedEngineLlm` adapts the engine's streaming ``LLMClient`` to the runtime's
:class:`~app.runtime.ports.LlmPort`: it passes ``on_chunk=None`` (nothing is streamed to the UI, D5),
resolves the agent's model binding through ``binding_for(agent_id)``, marks the moderator and puts a
hard ``asyncio.timeout`` around every call (reliability: a hung connection must not block an agent).

Cancellation is safe: a cancelled call leaves the ``async with`` blocks, so its slots are released
immediately.

v0.13.1 (G33, G34): with a :class:`~app.runtime.call_log.CallLog` every request leaves lines in the error log - each
HTTP attempt that failed and was repeated (the client's ``on_retry``), and the end of the request: ``ok``,
``timeout``, ``error`` or ``cancelled``.  :class:`LoggedLlm` does the same, one line per request, around any other
port (the scripted bots of the demo, the fakes of the tests).

v0.13.1 (G2, G3): :class:`GatedEngineLlm` can be given the soft-pause gate (:mod:`app.runtime.soft_gate`).  It then counts a
call as in flight only from the moment its concurrency slot is granted, and sends nothing while the gate is closed - so a
call that merely *queued* for a slot is neither counted nor sent after the click (see the class).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from app.llm.types import LLMResponse
from app.runtime.call_log import CallLog, CallProbe
from app.runtime.soft_gate import GateClosed, SoftGate
from app.world.types import MODERATOR_ID

DEFAULT_MAX_CONCURRENT = 0  # 0 / None = unlimited
DEFAULT_PER_PROVIDER = 0  # 0 / None = unlimited
DEFAULT_TIMEOUT_S = 300.0
UNKNOWN_PROVIDER = "default"


class LlmGate:
    """Global and per-provider concurrency limits (``None`` or ``0`` = unlimited, the default)."""

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


#: ``label(agent_id) -> (purpose, actor_id)``: what a request is for and whose line it is in the error log.
CallLabel = Callable[[str], tuple[str, str]]


def turn_label(agent_id: str) -> tuple[str, str]:
    """The default label: the request is the agent's own turn."""
    return "turn", agent_id


def _accepts_keyword(func: Any, name: str) -> bool:
    """Whether calling ``func(..., name=...)`` can work (a client written before ``on_retry`` existed cannot)."""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _probe_for(call_log: CallLog | None, label: CallLabel, agent_id: str, binding: Any) -> CallProbe | None:
    """The recorder of a request in the error log (``None``: no log, or the log cannot be asked - never an error)."""
    if call_log is None:
        return None
    try:
        purpose, actor_id = label(agent_id)
        return call_log.probe(
            actor_id,
            purpose,
            provider_id=getattr(binding, "provider_id", None) or None,
            model_id=getattr(binding, "model_id", None) or None,
        )
    except Exception:  # noqa: BLE001 - logging must never stop a model call
        return None


class GatedEngineLlm:
    """:class:`~app.runtime.ports.LlmPort` over the engine's ``LLMClient`` (production).

    ``client`` only needs ``async chat_stream(messages, *, is_moderator, tools, on_chunk,
    max_tokens_override, binding) -> LLMResponse``.  ``binding_for(agent_id)`` returns the agent's
    ``ModelBinding`` (``None`` falls back to the client's own config); its ``provider_id`` selects
    the per-provider limit.  The timeout covers the call itself (including the client's internal
    retries), not the time spent waiting for a slot.

    With a ``call_log`` every request is recorded in the error log (``label`` says whose line it is and what it is
    for; the default is the agent's own turn).  A client whose ``chat_stream`` takes ``on_retry`` also reports each
    attempt it repeats; one that does not (a fake) is called exactly as before.

    With a ``soft_gate`` (v0.13.1, the soft pause of :mod:`app.runtime.soft_gate`) the port counts its own calls as *in
    flight* - from the moment a slot is granted and the request is really about to be sent, never while a call only waits
    for a slot - and refuses to send once the gate is closed: a call whose slot comes while the soft pause is on gives the
    slot back at once and raises :class:`~app.runtime.soft_gate.GateClosed` (nothing was sent, nothing is logged); the agent
    waits at the gate and asks again after the pause.  The agents recognise such a port by its ``soft_gate`` attribute and
    do not count its calls a second time.
    """

    def __init__(
        self,
        client: Any,
        binding_for: Callable[[str], Any],
        gate: LlmGate | None = None,
        timeout_s: float | None = DEFAULT_TIMEOUT_S,
        *,
        moderator_id: str = MODERATOR_ID,
        call_log: CallLog | None = None,
        label: CallLabel | None = None,
        soft_gate: SoftGate | None = None,
    ) -> None:
        self.client = client
        self.binding_for = binding_for
        self.gate = gate if gate is not None else LlmGate()
        self.timeout_s = timeout_s if timeout_s and timeout_s > 0 else None
        self.moderator_id = moderator_id
        self.call_log = call_log
        self.label: CallLabel = label or turn_label
        self.soft_gate = soft_gate
        self._report_retries = call_log is not None and _accepts_keyword(getattr(client, "chat_stream", None), "on_retry")

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
        probe = _probe_for(self.call_log, self.label, agent_id, binding)
        async with self.gate.slot(provider_id):
            soft = self.soft_gate
            if soft is None:
                return await self._send(probe, agent_id, messages, tools, max_tokens, binding)
            if soft.closed:
                raise GateClosed()  # a soft pause began while this call waited for its slot: nothing is sent
            soft.call_started()  # (in flight from here on: the slot is ours and the request goes out now)
            try:
                return await self._send(probe, agent_id, messages, tools, max_tokens, binding)
            finally:
                soft.call_finished()

    async def _send(
        self,
        probe: CallProbe | None,
        agent_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int | None,
        binding: Any,
    ) -> LLMResponse:
        """The request itself, while a slot is held: one ``chat_stream`` under the timeout, recorded in the error log."""
        kwargs: dict[str, Any] = {
            "is_moderator": agent_id == self.moderator_id,
            "tools": tools or None,
            "on_chunk": None,
            "max_tokens_override": max_tokens,
            "binding": binding,
        }
        if probe is None:
            async with asyncio.timeout(self.timeout_s):
                return await self.client.chat_stream(messages, **kwargs)
        probe.start()  # (the time spent waiting for a slot is not the call's)
        if self._report_retries:
            kwargs["on_retry"] = probe.on_retry
        try:
            async with asyncio.timeout(self.timeout_s):
                resp = await self.client.chat_stream(messages, **kwargs)
        except asyncio.CancelledError:
            probe.cancelled()
            raise
        except Exception as exc:
            probe.failed(exc, timeout_s=self.timeout_s)
            raise
        probe.ok(resp)
        return resp


class LoggedLlm:
    """An :class:`~app.runtime.ports.LlmPort` that records every request of the port it wraps in the error log: one
    line per request (``ok`` / ``timeout`` / ``error`` / ``cancelled``) - the wrapped port retries nothing the log
    could see.  Used for the ports that are not a :class:`GatedEngineLlm` (the demo's scripted bots, test fakes);
    every other attribute of the wrapped port is reachable through it.  The wrapper's own state is private (``_inner``
    ...), so that it hides nothing the wrapped port has under a public name (a port that has a ``label`` or a
    ``call_log`` of its own - a :class:`GatedEngineLlm`, say - keeps them)."""

    def __init__(
        self,
        inner: Any,
        call_log: CallLog,
        binding_for: Callable[[str], Any] | None = None,
        label: CallLabel | None = None,
    ) -> None:
        self._inner = inner
        self._call_log = call_log
        self._binding_for = binding_for
        self._label: CallLabel = label or turn_label

    async def complete(
        self,
        agent_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        binding = None
        if self._binding_for is not None:
            try:
                binding = self._binding_for(agent_id)
            except Exception:  # noqa: BLE001 - the port decides what an unbound agent means, not the log
                binding = None
        probe = _probe_for(self._call_log, self._label, agent_id, binding)
        if probe is None:
            return await self._inner.complete(agent_id, messages, tools, max_tokens=max_tokens)
        probe.start()
        try:
            resp = await self._inner.complete(agent_id, messages, tools, max_tokens=max_tokens)
        except asyncio.CancelledError:
            probe.cancelled()
            raise
        except Exception as exc:
            probe.failed(exc)
            raise
        probe.ok(resp)
        return resp

    def __getattr__(self, name: str) -> Any:
        if name in ("_inner", "_call_log", "_binding_for", "_label"):  # (not set yet: do not recurse)
            raise AttributeError(name)
        return getattr(self._inner, name)
