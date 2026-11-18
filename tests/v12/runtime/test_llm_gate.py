from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.llm.types import LLMResponse
from app.runtime.llm_gate import GatedEngineLlm, LlmGate
from app.runtime.ports import LlmPort

from .fakes import async_test, settle, wait_until


class FakeClient:
    """Stands in for ``LLMClient``: records calls and the concurrency it sees."""

    def __init__(self, delay: float = 0.03) -> None:
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        self.in_flight = 0
        self.peak = 0
        self.in_flight_by_binding: dict[Any, int] = {}
        self.peak_by_binding: dict[Any, int] = {}
        self.events: list[tuple[str, Any]] = []  # ("start" | "end", label)
        self.fail_with: BaseException | None = None

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        is_moderator: bool = False,
        tools: list[dict[str, Any]] | None = None,
        on_chunk: Any = None,
        max_tokens_override: int | None = None,
        binding: Any = None,
    ) -> LLMResponse:
        label = messages[0]["content"] if messages else None
        key = getattr(binding, "provider_id", None)
        self.calls.append(
            {
                "messages": messages,
                "is_moderator": is_moderator,
                "tools": tools,
                "on_chunk": on_chunk,
                "max_tokens_override": max_tokens_override,
                "binding": binding,
            }
        )
        self.events.append(("start", label))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.in_flight_by_binding[key] = self.in_flight_by_binding.get(key, 0) + 1
        self.peak_by_binding[key] = max(self.peak_by_binding.get(key, 0), self.in_flight_by_binding[key])
        try:
            await asyncio.sleep(self.delay)
            if self.fail_with is not None:
                raise self.fail_with
            return LLMResponse(content=f"done:{label}")
        finally:
            self.in_flight -= 1
            self.in_flight_by_binding[key] -= 1
            self.events.append(("end", label))


def binding(provider_id: str) -> SimpleNamespace:
    return SimpleNamespace(provider_id=provider_id, model_id="m")


def llm_for(client: FakeClient, providers: dict[str, str | None], gate: LlmGate, timeout_s: float | None = 5.0) -> GatedEngineLlm:
    def binding_for(agent_id: str) -> Any:
        provider = providers.get(agent_id)
        return binding(provider) if provider else None

    return GatedEngineLlm(client, binding_for, gate, timeout_s)


def msgs(label: str) -> list[dict[str, Any]]:
    return [{"role": "system", "content": label}]


def test_gated_engine_llm_satisfies_the_llm_port_protocol():
    llm: LlmPort = GatedEngineLlm(FakeClient(), lambda agent_id: None, LlmGate())
    assert llm is not None


@async_test()
async def test_call_arguments_reach_the_client_unchanged():
    client = FakeClient(delay=0)
    b = binding("prov")
    llm = GatedEngineLlm(client, lambda agent_id: b, LlmGate())
    tools = [{"type": "function", "function": {"name": "speak"}}]
    resp = await llm.complete("a1", msgs("x"), tools, max_tokens=123)
    assert resp.content == "done:x"
    call = client.calls[0]
    assert call["on_chunk"] is None  # nothing is streamed to the UI (D5)
    assert call["binding"] is b
    assert call["max_tokens_override"] == 123
    assert call["tools"] == tools
    assert call["is_moderator"] is False


@async_test()
async def test_moderator_is_marked_and_empty_tools_become_none():
    client = FakeClient(delay=0)
    llm = GatedEngineLlm(client, lambda agent_id: None, LlmGate())
    await llm.complete("moderator", msgs("m"), [])
    await llm.complete("a1", msgs("p"), None)
    assert client.calls[0]["is_moderator"] is True and client.calls[0]["tools"] is None
    assert client.calls[1]["is_moderator"] is False
    assert client.calls[0]["binding"] is None and client.calls[0]["max_tokens_override"] is None


@async_test()
async def test_global_limit_bounds_concurrent_calls():
    client = FakeClient(delay=0.04)
    gate = LlmGate(max_concurrent=2, per_provider=None)
    llm = llm_for(client, {f"a{i}": "prov" for i in range(6)}, gate)
    results = await asyncio.gather(*(llm.complete(f"a{i}", msgs(f"c{i}"), None) for i in range(6)))
    assert [r.content for r in results] == [f"done:c{i}" for i in range(6)]
    assert client.peak == 2  # busy, but never more than the limit
    assert gate.peak == 2 and gate.in_flight == 0 and gate.waiting == 0


@async_test()
async def test_provider_limit_bounds_calls_to_that_provider_only():
    client = FakeClient(delay=0.05)
    gate = LlmGate(max_concurrent=10, per_provider=None, provider_limits={"slow": 1})
    agents = {"s1": "slow", "s2": "slow", "s3": "slow", "f1": "fast", "f2": "fast"}
    llm = llm_for(client, agents, gate)
    await asyncio.gather(*(llm.complete(a, msgs(a), None) for a in agents))
    assert client.peak_by_binding["slow"] == 1  # serialized
    assert client.peak_by_binding["fast"] == 2  # unconstrained
    assert client.peak >= 3  # fast calls ran alongside the slow one
    assert gate.peak_by_provider["slow"] == 1


@async_test()
async def test_default_per_provider_limit_applies_to_every_provider():
    client = FakeClient(delay=0.03)
    gate = LlmGate(max_concurrent=None, per_provider=2)
    agents = {f"x{i}": "p1" for i in range(5)} | {f"y{i}": "p2" for i in range(3)}
    llm = llm_for(client, agents, gate)
    await asyncio.gather(*(llm.complete(a, msgs(a), None) for a in agents))
    assert client.peak_by_binding["p1"] == 2 and client.peak_by_binding["p2"] == 2
    assert client.peak == 4  # two providers x two slots


@async_test()
async def test_a_busy_provider_does_not_hog_global_slots_of_other_providers():
    # X allows 1 call, the global gate only 2.  If X's queued calls held a global slot while waiting for
    # X, Y could not start until X1 ended; the provider slot is taken first so Y starts at once.
    client = FakeClient(delay=0.12)
    gate = LlmGate(max_concurrent=2, per_provider=None, provider_limits={"X": 1})
    llm = llm_for(client, {"x1": "X", "x2": "X", "x3": "X", "y1": "Y"}, gate)
    tasks = [asyncio.create_task(llm.complete(a, msgs(a), None)) for a in ("x1", "x2", "x3")]
    await settle()
    tasks.append(asyncio.create_task(llm.complete("y1", msgs("y1"), None)))
    await asyncio.gather(*tasks)
    order = [label for kind, label in client.events if kind == "start"]
    assert order.index("y1") < order.index("x2")  # Y did not queue behind X's backlog
    end_x1 = client.events.index(("end", "x1"))
    assert client.events.index(("start", "y1")) < end_x1


@async_test()
async def test_timeout_raises_and_frees_the_slot():
    client = FakeClient(delay=5.0)
    gate = LlmGate(max_concurrent=1, per_provider=1)
    llm = llm_for(client, {"a1": "p"}, gate, timeout_s=0.05)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(TimeoutError):
        await llm.complete("a1", msgs("slow"), None)
    assert loop.time() - started < 1.0
    assert gate.in_flight == 0 and client.in_flight == 0  # the call was cancelled inside the client
    client.delay = 0.0
    assert (await asyncio.wait_for(llm.complete("a1", msgs("next"), None), 1)).content == "done:next"


@async_test()
async def test_timeout_does_not_count_the_wait_for_a_slot():
    client = FakeClient(delay=0.15)
    gate = LlmGate(max_concurrent=1, per_provider=None)
    llm = llm_for(client, {"a1": "p", "a2": "p"}, gate, timeout_s=0.3)
    first = asyncio.create_task(llm.complete("a1", msgs("first"), None))
    await settle()
    # waits ~0.15 s for the slot, then needs ~0.15 s itself: fine, only the call is timed (0.3 s)
    second = await asyncio.wait_for(llm.complete("a2", msgs("second"), None), 2)
    await first
    assert second.content == "done:second"


@async_test()
async def test_cancelling_a_call_releases_its_slot_immediately():
    client = FakeClient(delay=5.0)
    gate = LlmGate(max_concurrent=1, per_provider=1)
    llm = llm_for(client, {"a1": "p", "a2": "p"}, gate)
    running = asyncio.create_task(llm.complete("a1", msgs("long"), None))
    await wait_until(lambda: gate.in_flight == 1)
    waiting = asyncio.create_task(llm.complete("a2", msgs("queued"), None))
    await wait_until(lambda: gate.waiting == 1)
    client.delay = 0.0  # the running call is already asleep; only the queued one will see this
    running.cancel()  # what the Actor does to a stale worker
    await asyncio.gather(running, return_exceptions=True)
    assert (await asyncio.wait_for(waiting, 1)).content == "done:queued"  # inherited the freed slot
    assert gate.in_flight == 0 and gate.waiting == 0


@async_test()
async def test_cancelling_a_queued_call_does_not_leak_a_slot():
    client = FakeClient(delay=0.1)
    gate = LlmGate(max_concurrent=1, per_provider=1)
    llm = llm_for(client, {"a1": "p", "a2": "p", "a3": "p"}, gate)
    first = asyncio.create_task(llm.complete("a1", msgs("one"), None))
    await wait_until(lambda: gate.in_flight == 1)
    queued = asyncio.create_task(llm.complete("a2", msgs("two"), None))
    await wait_until(lambda: gate.waiting == 1)
    queued.cancel()
    await asyncio.gather(queued, return_exceptions=True)
    assert gate.waiting == 0
    await first
    assert (await asyncio.wait_for(llm.complete("a3", msgs("three"), None), 1)).content == "done:three"
    assert gate.in_flight == 0


@async_test()
async def test_client_errors_propagate_and_release_the_slot():
    client = FakeClient(delay=0)
    client.fail_with = RuntimeError("upstream 500")
    gate = LlmGate(max_concurrent=1, per_provider=1)
    llm = llm_for(client, {"a1": "p"}, gate)
    with pytest.raises(RuntimeError, match="upstream 500"):
        await llm.complete("a1", msgs("x"), None)
    assert gate.in_flight == 0
    client.fail_with = None
    assert (await llm.complete("a1", msgs("y"), None)).content == "done:y"


@async_test()
async def test_a_missing_binding_uses_the_shared_default_provider_slot():
    client = FakeClient(delay=0.03)
    gate = LlmGate(max_concurrent=None, per_provider=1)
    llm = llm_for(client, {}, gate)  # binding_for -> None for everyone (cfg fallback client)
    await asyncio.gather(*(llm.complete(f"a{i}", msgs(f"c{i}"), None) for i in range(3)))
    assert client.peak == 1
    assert all(call["binding"] is None for call in client.calls)


@async_test()
async def test_binding_lookup_errors_surface_to_the_caller():
    def binding_for(agent_id: str) -> Any:
        raise RuntimeError("no model bound")

    llm = GatedEngineLlm(FakeClient(), binding_for, LlmGate())
    with pytest.raises(RuntimeError, match="no model bound"):
        await llm.complete("a1", msgs("x"), None)


@async_test()
async def test_unlimited_gate_and_no_timeout():
    client = FakeClient(delay=0.02)
    gate = LlmGate(max_concurrent=None, per_provider=None)
    llm = GatedEngineLlm(client, lambda a: binding("p"), gate, timeout_s=None)
    await asyncio.gather(*(llm.complete(f"a{i}", msgs(f"c{i}"), None) for i in range(10)))
    assert client.peak == 10


# ------------------------------------------------- v0.12.1: the gate is open unless the user narrows it


@async_test()
async def test_the_default_gate_is_open_nothing_ever_waits():
    client = FakeClient(delay=0.03)
    gate = LlmGate()  # no arguments: the defaults are "unlimited" (the gate is for rate limits, not for saving money)
    assert gate.max_concurrent is None and gate.per_provider is None
    llm = GatedEngineLlm(client, lambda a: binding("p1" if a < "a5" else "p2"), gate, timeout_s=5)
    await asyncio.gather(*(llm.complete(f"a{i}", msgs(f"c{i}"), None) for i in range(13)))  # a whole table of agents
    assert client.peak == 13 and gate.peak == 13 and gate.waiting == 0 and gate.in_flight == 0


@async_test()
async def test_zero_means_unlimited_for_every_limit():
    # ``config.yml`` says ``llm_concurrency: 0`` / ``llm_concurrency_per_provider: 0``: "no limit"
    client = FakeClient(delay=0.03)
    gate = LlmGate(max_concurrent=0, per_provider=0, provider_limits={"fast": 0})
    agents = {f"a{i}": ("fast" if i % 2 else "slow") for i in range(8)}
    llm = llm_for(client, agents, gate)
    await asyncio.gather(*(llm.complete(a, msgs(a), None) for a in agents))
    assert client.peak == 8 and gate.waiting == 0
    assert gate.max_concurrent is None and gate.per_provider is None


@async_test()
async def test_a_provider_can_be_limited_while_the_rest_stays_open():
    client = FakeClient(delay=0.04)
    gate = LlmGate(max_concurrent=0, per_provider=0, provider_limits={"rate-limited": 2})
    agents = {f"r{i}": "rate-limited" for i in range(5)} | {f"o{i}": "open" for i in range(5)}
    llm = llm_for(client, agents, gate)
    await asyncio.gather(*(llm.complete(a, msgs(a), None) for a in agents))
    assert client.peak_by_binding["rate-limited"] == 2 and client.peak_by_binding["open"] == 5
