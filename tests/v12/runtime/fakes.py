"""Test doubles and helpers for the runtime tests (no pytest-asyncio: tests are plain functions wrapped by
the :func:`async_test` decorator, which runs the coroutine with ``asyncio.run``).

* :class:`FakeWorld`  implements ``WorldPort`` with scriptable ``submit`` and hand-fed outbox / events.
* :class:`FakeBrain`  implements the v0.12.1 ``Brain`` protocol (``begin_run`` / ``reset_conversation`` /
  FULL ``start_message`` / DELTA ``inject_message`` / nudge / tools).
* :class:`FakeLlm`    implements ``LlmPort`` (scripted responses, delays, errors, holds, cancellation) and
  keeps a *deep-copied snapshot* of every request, so tests can compare requests byte for byte.
* :class:`RecordingSink` / :class:`RecordingTrace` keep what the runtime published / traced.

Times: the runtime has no debounce any more; only retries, the cancel grace period, the supervisor's tick /
watchdog and the sleep timers use time.  Tests inject small real values (tens of milliseconds); on Windows
the loop clock ticks every ~15.6 ms, so assertions are tolerant.  Game time (sleep timers, the world's
deadlines) is a :class:`~app.runtime.clock.ManualClock` that only moves when the test advances it.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import itertools
import json
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.llm.types import LLMResponse, ToolCallData
from app.runtime.agent import Agent, AgentConfig
from app.runtime.clock import ManualClock
from app.runtime.conversation import ConversationBook
from app.runtime.ports import ToolOutcome, WakeContext
from app.runtime.usage import UsageBook
from app.world.spec import ScenarioSpec
from app.world.types import AgentMemory, Delivery, Envelope, Intent, LedgerEntry, Receipt, UiEvent

MODERATOR = "moderator"

# --------------------------------------------------------------------------- async test plumbing


def run_async(coro: Awaitable[Any], *, timeout: float = 20.0, leaks: bool = True) -> Any:
    """``asyncio.run`` with a hard timeout and (optionally) a check that no task outlived the test."""

    async def main() -> Any:
        result = await asyncio.wait_for(coro, timeout)  # type: ignore[arg-type]
        if leaks:
            for _ in range(10):  # let cancelled tasks finish their last step
                await asyncio.sleep(0)
            current = asyncio.current_task()
            alive = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
            assert not alive, f"leaked tasks: {[t.get_name() for t in alive]}"
        return result

    return asyncio.run(main())


def async_test(timeout: float = 20.0, leaks: bool = True) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: turn ``async def test_x(...)`` into a function pytest can run."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return run_async(fn(*args, **kwargs), timeout=timeout, leaks=leaks)

        return wrapper

    return decorate


async def wait_until(
    predicate: Callable[[], Any], timeout: float = 3.0, interval: float = 0.004, message: str = ""
) -> None:
    """Poll ``predicate`` until it is truthy; fail the test after ``timeout`` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for: {message or predicate}")
        await asyncio.sleep(interval)


async def settle(rounds: int = 6) -> None:
    """Give every ready callback / task a few turns of the event loop."""
    for _ in range(rounds):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- builders


def env(
    event: str = "message.new",
    *,
    kind: str = "message",
    priority: str = "normal",
    channel_id: str | None = None,
    interrupt: bool = False,
    seq: int = 0,
    sender: str | None = None,
    **payload: Any,
) -> Envelope:
    """An envelope with ``payload["event"] = event``."""
    return Envelope(
        seq=seq,
        kind=kind,  # type: ignore[arg-type]
        priority=priority,  # type: ignore[arg-type]
        interrupt=interrupt,
        channel_id=channel_id,
        sender=sender,
        payload={"event": event, **payload},
    )


def urgent(event: str = "floor.granted", **kw: Any) -> Envelope:
    kw.setdefault("kind", "floor")
    return env(event, priority="urgent", **kw)


def normal(event: str = "message.new", **kw: Any) -> Envelope:
    return env(event, priority="normal", **kw)


def revoked(channel_id: str, event: str = "floor.revoked") -> Envelope:
    """The interrupt that takes a floor away (it invalidates work in flight on ``channel_id``)."""
    return env(event, kind="floor", priority="normal", interrupt=True, channel_id=channel_id, reason="revoked")


_call_ids = itertools.count(1)


def llm_resp(
    *calls: tuple[str, dict[str, Any]] | ToolCallData,
    content: str = "",
    thought: str = "",
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    provider_id: str = "",
    model_id: str = "",
    input_price_per_m: float = 0.0,
    output_price_per_m: float = 0.0,
    cache_read_price_per_m: float = 0.0,
    cache_write_price_per_m: float = 0.0,
    finish_reason: str | None = None,
) -> LLMResponse:
    """An ``LLMResponse`` with tool calls given as ``("name", {args})``."""
    tool_calls: list[ToolCallData] = []
    for call in calls:
        if isinstance(call, ToolCallData):
            tool_calls.append(call)
        else:
            name, args = call
            tool_calls.append(ToolCallData(id=f"c{next(_call_ids)}", name=name, arguments_json=json.dumps(args)))
    return LLMResponse(
        thought=thought,
        content=content,
        tool_calls=tool_calls,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        finish_reason=finish_reason if finish_reason is not None else ("tool_calls" if tool_calls else "stop"),
        provider_id=provider_id,
        model_id=model_id,
        input_price_per_m=input_price_per_m,
        output_price_per_m=output_price_per_m,
        cache_read_price_per_m=cache_read_price_per_m,
        cache_write_price_per_m=cache_write_price_per_m,
    )


def sleep_resp(seconds: float | None = None, **kw: Any) -> LLMResponse:
    """The model calls ``sleep`` (optionally with a timer): the run ends."""
    return llm_resp(("sleep", {} if seconds is None else {"seconds": seconds}), **kw)


def speak_resp(content: str = "hello", channel_id: str = "pub", **kw: Any) -> LLMResponse:
    return llm_resp(("speak", {"channel_id": channel_id, "content": content}), **kw)


def assert_paired(messages: list[dict[str, Any]]) -> None:
    """Every assistant tool call has exactly one tool result directly behind its assistant message."""
    i, n = 0, len(messages)
    while i < n:
        message = messages[i]
        i += 1
        if message.get("role") == "tool":
            raise AssertionError(f"orphan tool result at {i - 1}: {message}")
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not calls:
            continue
        wanted = [c["id"] for c in calls]
        got = []
        while i < n and messages[i].get("role") == "tool":
            got.append(messages[i].get("tool_call_id"))
            i += 1
        if sorted(got) != sorted(wanted):
            raise AssertionError(f"tool calls {wanted} answered by {got}")


def dump(messages: list[dict[str, Any]]) -> list[str]:
    """Each message as its JSON text (what a provider's prefix cache sees, byte for byte)."""
    return [json.dumps(m, ensure_ascii=False, sort_keys=False) for m in messages]


# --------------------------------------------------------------------------- recording sinks


class RecordingSink:
    """``EventSink`` that remembers every published event."""

    def __init__(self) -> None:
        self.events: list[UiEvent] = []

    def publish(self, event: UiEvent) -> None:
        self.events.append(event)

    def of_type(self, type_: str) -> list[UiEvent]:
        return [e for e in self.events if e.type == type_]

    def statuses(self, agent_id: str) -> list[str]:
        return [e.data["state"] for e in self.events if e.type == "agent.status" and e.data.get("id") == agent_id]

    def status_events(self, agent_id: str) -> list[tuple[str, Any]]:
        return [
            (e.data["state"], e.data.get("until"))
            for e in self.events
            if e.type == "agent.status" and e.data.get("id") == agent_id
        ]


class RecordingTrace:
    """``TraceSink`` that remembers every event."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def write(self, event: str, agent_id: str, **fields: Any) -> None:
        self.records.append((event, agent_id, fields))

    def of(self, event: str, agent_id: str | None = None) -> list[dict[str, Any]]:
        return [f for e, a, f in self.records if e == event and (agent_id is None or a == agent_id)]

    def names(self, agent_id: str | None = None) -> list[str]:
        return [e for e, a, _ in self.records if agent_id is None or a == agent_id]


# --------------------------------------------------------------------------- fake world


class FakeWorld:
    """A scriptable ``WorldPort``.

    * ``submit`` records the intent and answers with ``on_submit(intent)`` (default: accept, ``seq += 1``).
    * ``push`` / ``emit`` queue deliveries / UI events by hand and call the registered notifier.
    * ``seq`` and ``running`` are plain read/write attributes; ``deadline`` is what ``next_deadline`` returns.
    * ``clock`` is a :class:`ManualClock`: the game clock (sleep timers, deadlines) moves only when advanced.
    """

    def __init__(self, clock: ManualClock | None = None, *, running: bool = True) -> None:
        self.spec = ScenarioSpec(id="fake", version="1")
        self.clock = clock or ManualClock()
        self.seq = 0
        self.running = running
        self.deadline: float | None = None
        self.names: dict[str, str] = {}
        self.ledger: list[LedgerEntry] = []
        self.outbox: list[Delivery] = []
        self.events: list[UiEvent] = []
        self.submitted: list[Intent] = []
        self.on_submit: Callable[[Intent], Receipt] | None = None
        self.on_tick: Callable[[], None] | None = None
        self.on_redeliver: Callable[[], None] | None = None
        self.tick_count = 0
        self.redeliver_count = 0
        self.notify_calls = 0
        self.notifier: Callable[[], None] | None = None

    # -- state
    def set_running(self, running: bool, reason: str = "") -> None:
        if bool(running) == self.running:
            return
        self.running = bool(running)
        self.events.append(UiEvent(type="game.status", data={"status": "running" if running else "paused", "reason": reason}))
        self._notify()

    # -- commit gate
    def submit(self, intent: Intent) -> Receipt:
        self.submitted.append(intent)
        if self.on_submit is not None:
            receipt = self.on_submit(intent)
        elif not self.running:
            receipt = Receipt.reject("GAME_NOT_RUNNING", "not running")
        else:
            self.seq += 1
            receipt = Receipt.accept(seq=self.seq)
        self._notify()
        return receipt

    def tick(self) -> None:
        self.tick_count += 1
        if self.running and self.on_tick is not None:
            self.on_tick()
        self._notify()

    def next_deadline(self) -> float | None:
        return self.deadline

    def redeliver(self) -> None:
        self.redeliver_count += 1
        if self.on_redeliver is not None:
            self.on_redeliver()
        self._notify()

    # -- side-effect queues
    def set_notifier(self, callback: Callable[[], None] | None) -> None:
        self.notifier = callback

    def drain_outbox(self) -> list[Delivery]:
        out, self.outbox = self.outbox, []
        return out

    def drain_events(self) -> list[UiEvent]:
        out, self.events = self.events, []
        return out

    def _notify(self) -> None:
        if self.notifier is not None and (self.outbox or self.events):
            self.notify_calls += 1
            self.notifier()

    # -- hand feeding
    def push(self, target: str, envelope: Envelope | None = None, **kw: Any) -> Envelope:
        envelope = envelope or env(**kw)
        self.outbox.append(Delivery(target=target, envelope=envelope))
        self._notify()
        return envelope

    def emit(self, type_: str, **data: Any) -> None:
        self.events.append(UiEvent(type=type_, data=data))
        self._notify()

    def bump(self, by: int = 1) -> int:
        """Pretend the ledger advanced (progress for the watchdog)."""
        self.seq += by
        return self.seq

    # -- reading
    def entries_visible(self, agent_id: str, *, after_seq: int = 0, limit: int | None = None) -> list[LedgerEntry]:
        out = [e for e in self.ledger if e.seq > after_seq and agent_id in e.visible_to_ids]
        return out[-limit:] if limit else out

    def board(self, agent_id: str) -> dict[str, Any]:
        return {"me": {"id": agent_id}, "seq": self.seq}

    def display_name(self, agent_id: str) -> str:
        return self.names.get(agent_id, agent_id)

    def memory(self, agent_id: str) -> AgentMemory:
        return AgentMemory()

    def write_note(self, agent_id: str, title: str, content: str, mode: str = "overwrite") -> str:
        return content

    def read_notes_text(self, agent_id: str) -> str:
        return ""

    def compact(self, agent_id: str, summary: str, keep_recent_count: int = 10) -> dict[str, Any]:
        return {"ok": True}


# --------------------------------------------------------------------------- fake brain

WORLD_TOOLS = frozenset({"speak", "pass", "respond", "announce"})


def describe(envelope: Envelope) -> str:
    event = envelope.payload.get("event") or envelope.kind
    where = f"@{envelope.channel_id}" if envelope.channel_id else ""
    label = envelope.payload.get("label")
    tag = f"#{label}" if label is not None else ""
    return f"{event}{where}{tag}"


class FakeBrain:
    """A scriptable v0.12.1 ``Brain``.  Everything it is asked is recorded for assertions."""

    def __init__(self, world: FakeWorld | None = None) -> None:
        self.world = world
        self.begins: list[str] = []
        self.resets: list[str] = []
        self.starts: list[WakeContext] = []
        self.injects: list[WakeContext] = []
        self.nudge_calls: list[tuple[str, str, int]] = []
        self.executed: list[dict[str, Any]] = []
        self.nudge_text: str | None = "NUDGE: call a tool"
        self.tool_handlers: dict[str, Callable[..., ToolOutcome]] = {}
        self.inject_hook: Callable[[WakeContext], tuple[str, int] | None] | None = None
        self.start_error: BaseException | None = None
        self.tool_error: BaseException | None = None
        self.inject_error: BaseException | None = None
        self.tools = [
            {"type": "function", "function": {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}}}
            for name in ("speak", "sleep", "compact_context")
        ]

    # -- Brain protocol
    def system_prompt(self, agent_id: str) -> str:
        return f"SYSTEM[{agent_id}]"

    def tool_schemas(self, agent_id: str) -> list[dict[str, Any]]:
        return self.tools

    def begin_run(self, agent_id: str) -> None:
        self.begins.append(agent_id)

    def reset_conversation(self, agent_id: str) -> None:
        self.resets.append(agent_id)

    def _seen(self) -> int:
        return self.world.seq if self.world is not None else 0

    def start_message(self, agent_id: str, ctx: WakeContext) -> tuple[str, int]:
        self.starts.append(ctx)
        if self.start_error is not None:
            raise self.start_error
        reasons = ",".join(describe(e) for e in ctx.reasons)
        return f"START[{agent_id}] gen={ctx.gen_id} reasons=[{reasons}]", self._seen()

    def inject_message(self, agent_id: str, ctx: WakeContext) -> tuple[str, int] | None:
        self.injects.append(ctx)
        if self.inject_error is not None:
            raise self.inject_error
        if self.inject_hook is not None:
            return self.inject_hook(ctx)
        if not ctx.reasons:
            return None
        reasons = ",".join(describe(e) for e in ctx.reasons)
        return f"DELTA[{agent_id}] after={ctx.after_seq} reasons=[{reasons}]", self._seen()

    def no_tool_nudge(self, agent_id: str, content: str, round_idx: int) -> str | None:
        self.nudge_calls.append((agent_id, content, round_idx))
        return self.nudge_text

    def execute_tool(self, agent_id: str, name: str, args: dict[str, Any], *, gen_id: int, seen_seq: int) -> ToolOutcome:
        self.executed.append({"agent_id": agent_id, "name": name, "args": args, "gen_id": gen_id, "seen_seq": seen_seq})
        if self.tool_error is not None:
            raise self.tool_error
        handler = self.tool_handlers.get(name)
        if handler is not None:
            return handler(agent_id, args, gen_id=gen_id, seen_seq=seen_seq)
        if name == "sleep":
            return ToolOutcome(text='{"ok":true}', ends_run=True, sleep_s=args.get("seconds"))
        if name == "compact_context":
            return ToolOutcome(text='{"ok":true}', rebuild=True, shared=True)
        if self.world is not None and name in WORLD_TOOLS:
            intent = Intent(
                intent_id=f"i{len(self.executed)}", actor_id=agent_id, gen_id=gen_id, based_on_seq=seen_seq, type=name, payload=args
            )
            receipt = self.world.submit(intent)
            return ToolOutcome(text=receipt.tool_json(), receipt=receipt)
        return ToolOutcome(text='{"ok":true}')

    # -- assertions helpers
    def names(self) -> list[str]:
        return [e["name"] for e in self.executed]

    def starts_of(self, agent_id: str) -> list[WakeContext]:
        return [c for c in self.starts if c.agent_id == agent_id]

    def injects_of(self, agent_id: str) -> list[WakeContext]:
        return [c for c in self.injects if c.agent_id == agent_id]


# --------------------------------------------------------------------------- fake LLM


@dataclass
class Step:
    """One scripted answer of :class:`FakeLlm`.

    ``delay`` sleeps first; ``hold`` waits for an event (an await point that a cancellation can hit);
    ``error`` is raised instead of answering; ``swallow_cancel_s`` > 0 makes the call *ignore* a
    cancellation for that long and then answer anyway (a misbehaving client).
    """

    resp: LLMResponse | None = None
    error: BaseException | None = None
    delay: float = 0.0
    hold: asyncio.Event | None = None
    swallow_cancel_s: float = 0.0


@dataclass
class LlmCall:
    agent_id: str
    messages: list[dict[str, Any]]  # a deep copy taken when the call was made
    tools: list[dict[str, Any]] | None  # ditto
    max_tokens: int | None
    index: int  # 1-based per agent
    started: float
    paired: bool = True  # every tool call of the request had its result (see ``assert_paired``)

    @property
    def lines(self) -> list[str]:
        """The messages as JSON text, for byte-for-byte prefix comparisons."""
        return dump(self.messages)


class FakeLlm:
    """A scriptable ``LlmPort``.  ``script(agent, *steps)`` queues answers; an empty script answers with
    ``default`` (a plain text answer without tool calls, which ends a run)."""

    def __init__(self, default: Callable[[LlmCall], LLMResponse] | None = None) -> None:
        self.scripts: dict[str, deque[Step | LLMResponse | BaseException]] = defaultdict(deque)
        self.default = default or (lambda call: LLMResponse(content=""))
        self.calls: list[LlmCall] = []
        self.cancelled: list[str] = []
        self.completed: list[str] = []
        self.in_flight = 0
        self.peak = 0

    def script(self, agent_id: str, *steps: Step | LLMResponse | BaseException) -> "FakeLlm":
        self.scripts[agent_id].extend(steps)
        return self

    def calls_of(self, agent_id: str) -> list[LlmCall]:
        return [c for c in self.calls if c.agent_id == agent_id]

    async def complete(
        self,
        agent_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        loop = asyncio.get_running_loop()
        index = len(self.calls_of(agent_id)) + 1
        snapshot = copy.deepcopy(messages)
        try:
            assert_paired(snapshot)
            paired = True
        except AssertionError:
            paired = False
        call = LlmCall(agent_id, snapshot, copy.deepcopy(tools), max_tokens, index, loop.time(), paired)
        self.calls.append(call)
        queue = self.scripts[agent_id]
        item = queue.popleft() if queue else None
        if item is None:
            step = Step(resp=self.default(call))
        elif isinstance(item, Step):
            step = item
        elif isinstance(item, BaseException):
            step = Step(error=item)
        else:
            step = Step(resp=item)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            try:
                if step.delay:
                    await asyncio.sleep(step.delay)
                if step.hold is not None:
                    await step.hold.wait()
            except asyncio.CancelledError:
                self.cancelled.append(agent_id)
                if step.swallow_cancel_s <= 0:
                    raise
                await asyncio.sleep(step.swallow_cancel_s)  # ignore the cancellation, answer late
            if step.error is not None:
                raise step.error
            assert step.resp is not None
            self.completed.append(agent_id)
            return step.resp
        finally:
            self.in_flight -= 1


# --------------------------------------------------------------------------- fake engine client


class FakeEngineClient:
    """Stands in for the engine's ``LLMClient`` behind ``GatedEngineLlm``.

    Answers are scripted per agent; the agent is recognised by the ``SYSTEM[<id>]`` prompt that
    :class:`FakeBrain` puts first in every conversation.  Every call is recorded with its keywords.
    """

    def __init__(self, script: dict[str, list[LLMResponse]] | None = None, *, default: LLMResponse | None = None) -> None:
        self.script: dict[str, deque[LLMResponse]] = {k: deque(v) for k, v in (script or {}).items()}
        self.default = default or LLMResponse(content="")
        self.calls: list[dict[str, Any]] = []

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
        system = str(messages[0].get("content", "")) if messages else ""
        agent_id = system[len("SYSTEM[") : -1] if system.startswith("SYSTEM[") else system
        self.calls.append(
            {
                "agent_id": agent_id,
                "messages": messages,
                "is_moderator": is_moderator,
                "tools": tools,
                "on_chunk": on_chunk,
                "max_tokens_override": max_tokens_override,
                "binding": binding,
            }
        )
        await asyncio.sleep(0)
        queue = self.script.get(agent_id)
        return queue.popleft() if queue else self.default


# --------------------------------------------------------------------------- one-stop rig


@dataclass
class Rig:
    """World + brain + llm + sinks + usage + conversation book for one or several agents."""

    world: FakeWorld = field(default_factory=FakeWorld)
    llm: FakeLlm = field(default_factory=FakeLlm)
    sink: RecordingSink = field(default_factory=RecordingSink)
    trace: RecordingTrace = field(default_factory=RecordingTrace)
    brain: FakeBrain = field(init=False)
    usage: UsageBook = field(default_factory=UsageBook)
    book: ConversationBook = field(default_factory=ConversationBook)

    def __post_init__(self) -> None:
        self.brain = FakeBrain(self.world)

    def agent(
        self,
        agent_id: str = "a1",
        kind: str = "player",
        config: AgentConfig | None = None,
        *,
        context_window: Callable[[], int] | None = None,
        **cfg: Any,
    ) -> Agent:
        """An :class:`Agent` wired to this rig with fast timings (override through ``cfg``)."""
        return Agent(
            agent_id,
            kind,  # type: ignore[arg-type]
            self.world,
            self.brain,
            self.llm,
            self.usage,
            self.trace,
            self.sink,
            config or fast_config(**cfg),
            book=self.book,
            context_window=context_window,
        )

    def conversation(self, agent_id: str = "a1") -> Any:
        return self.book.get(agent_id)


def fast_config(**overrides: Any) -> AgentConfig:
    """An :class:`AgentConfig` with test-sized timings."""
    values: dict[str, Any] = {
        "retry_backoff_s": 0.02,
        "retry_backoff_max_s": 0.08,
        "cancel_grace_s": 0.3,
    }
    values.update(overrides)
    return AgentConfig(**values)


class running_agent:
    """``async with running_agent(agent):`` arms the agent and stops it (awaiting its workers) at the end."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent

    async def __aenter__(self) -> Agent:
        self.agent.activate()
        return self.agent

    async def __aexit__(self, *exc_info: object) -> None:
        await self.agent.stop()
