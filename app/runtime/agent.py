"""One agent of the game: an **Actor** (event-driven, never blocks) plus **Workers** (one run each).

v0.12.1 model (docs/v0.12-contracts.md section 10.2).  The runtime does not try to save money by calling
the model less: *every* delivered envelope wakes the agent at once.  Tokens are managed by orchestration -
a persistent, append-only conversation (so the provider's prefix cache hits), merging of whatever arrives
while a call is in flight, and compaction that happens rarely and on purpose.

States (only three): ``thinking`` (a model call is in flight), ``acting`` (tools run / between two calls),
``sleeping`` (no run: waiting for the next envelope, or for a timer the agent chose itself).

Actor
    :meth:`Agent.deliver` puts the envelope into ``pending``.  A sleeping agent schedules **one**
    activation with ``loop.call_soon``, so every envelope delivered in the same synchronous pump becomes a
    single activation; nothing waits, there is no debounce and no timer.  An envelope with ``interrupt``
    cancels the running Worker when its ``channel_id`` / ``payload.request_id`` matches the run's *focus*
    (the channels / requests of everything the run consumed): the in-flight call is dropped and the agent
    is activated again with everything pending.  After ``starvation_limit`` (2) consecutive cancellations
    the next interrupt is merged instead, until a run ends normally.

Worker (one *run* = one activation)
    ``brain.begin_run`` -> a FULL opening message (``brain.start_message``) creates the conversation, any
    later activation appends a DELTA (``brain.inject_message``; ``None`` = nothing to tell: back to sleep
    without a model call) -> loop ``model call -> tools``.  After every round of tools the envelopes that
    arrived meanwhile are merged into ONE message and the loop continues - there is no round limit.  The run
    ends when a tool asks for it (``sleep``, optionally with a timer) or the model answers without tools and
    the brain has nothing to nudge; if envelopes are pending then, the agent is activated again at once.

Conversation (:mod:`app.runtime.conversation`)
    ``[system] + messages``; ``system`` and ``tools`` never change after creation.  The assistant message
    and ALL its tool results are appended in one synchronous stretch (no ``await``), so a cancellation can
    never leave a ``tool_call`` unpaired; a cancelled or stale model call is never written.  At 60 % of the
    model's context window the agent is reminded once to call ``compact_context`` (a *rebuild*: a fresh
    FULL message); at 90 % the oldest complete rounds are trimmed as a last resort.

Every run gets a new ``gen_id``; a Worker whose generation is no longer current stops before it touches the
world.  A failing run (model exception, timeout, brain bug) is retried after an exponential back-off (at
most ``llm_retries`` times, the conversation untouched), then given up with an ``error`` event; other agents
are never affected.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.llm.types import LLMResponse
from app.runtime.conversation import Conversation, ConversationBook, tools_signature
from app.runtime.messages import (
    args_error_text,
    assistant_text_message,
    assistant_tool_message,
    ensure_call_ids,
    parse_call,
    tool_failure_text,
    tool_message,
    user_message,
)
from app.runtime.ports import (
    Brain,
    EventSink,
    LlmPort,
    ToolOutcome,
    TraceSink,
    UsageSink,
    WakeContext,
)
from app.world.types import ENGINE_ID, Envelope, UiEvent

logger = logging.getLogger(__name__)

#: Sent (once per run) when a call ends because it hit the output-token limit *without* any tool call:
#: a reasoning model can spend the whole budget on thinking, which would silently end the run.
CUT_OFF_NUDGE = (
    "（系统提示）你上一次的输出因长度上限被截断，没有完成任何操作。请大幅缩短思考，"
    "直接调用工具完成当前待办（例如 speak / respond / sleep）。"
)
#: Queued (once per conversation) when the input reaches ``compact_at`` of the model's context window.
COMPACT_NUDGE = "（系统提示）会话已接近上限，请先调用 compact_context 生成摘要，再继续。"
#: Queued after the emergency trim (``trim_at``) removed the oldest rounds.
TRIM_NOTICE = (
    "（系统提示）会话已超出上下文上限，为继续运行，最早的 {rounds} 轮对话已被裁剪（开局信息与最近的对话仍在）。"
    "需要的信息请查阅笔记或最新状态。"
)
TRUNCATION_REASONS = frozenset({"length", "max_tokens"})

AgentKind = Literal["player", "moderator"]
AgentState = Literal["thinking", "acting", "sleeping"]

_CONSUMED_CAP = 60  # envelopes remembered per run (for a rebuild's wake reasons)


@dataclass
class AgentConfig:
    """Tunables shared by all agents of a game; mutate it in place to change them live."""

    max_tokens: int | None = None  # None -> ``world.spec.defaults.llm.max_tokens``
    starvation_limit: int = 2  # consecutive cancellations before an interrupt is merged instead
    llm_retries: int = 2  # re-attempts of a failed run (so up to 3 attempts)
    retry_backoff_s: float = 1.0  # first back-off; doubled for every further failure
    retry_backoff_max_s: float = 30.0
    cancel_grace_s: float = 5.0  # how long a cancelled Worker may take to unwind before it is abandoned
    compact_at: float = 0.6  # share of the context window that triggers the "please compact" reminder
    trim_at: float = 0.9  # share of the context window that triggers the emergency trim
    trace_messages: bool = False  # also trace the full conversation of every LLM call


@dataclass
class _Run:
    gen: int
    reasons: list[Envelope]
    attempt: int = 0  # failed attempts before this one
    retry: bool = False  # a re-attempt of a failed run
    appended: bool = False  # the activation's message is part of the conversation
    focus_channels: set[str] = field(default_factory=set)
    focus_requests: set[str] = field(default_factory=set)
    consumed: list[Envelope] = field(default_factory=list)
    rounds: int = 0
    end_reason: str = ""
    sleep_s: float | None = None  # a timed sleep the run ended with
    prev_sleep_until: float | None = None  # the timer this activation voided (restored by a no-op run)
    prev_slept_s: float | None = None

    def absorb(self, envelopes: list[Envelope]) -> None:
        """Remember the envelopes this run has consumed and widen its focus."""
        for envelope in envelopes:
            self.consumed.append(envelope)
            if envelope.channel_id:
                self.focus_channels.add(envelope.channel_id)
            request_id = envelope.payload.get("request_id")
            if isinstance(request_id, str) and request_id:
                self.focus_requests.add(request_id)
        if len(self.consumed) > _CONSUMED_CAP:
            del self.consumed[: len(self.consumed) - _CONSUMED_CAP]


def _brief(envelope: Envelope) -> dict[str, Any]:
    """A compact description of an envelope for the trace."""
    out: dict[str, Any] = {
        "kind": envelope.kind,
        "event": envelope.payload.get("event"),
        "priority": envelope.priority,
        "seq": envelope.seq,
    }
    if envelope.channel_id:
        out["channel_id"] = envelope.channel_id
    if envelope.sender:
        out["sender"] = envelope.sender
    if envelope.interrupt:
        out["interrupt"] = True
    request_id = envelope.payload.get("request_id")
    if request_id:
        out["request_id"] = request_id
    return out


def _now() -> float:
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:  # pragma: no cover - only outside a running loop
        return time.monotonic()


class _Stopping:
    """Result of :meth:`Agent.stop`.  The stop has already happened; awaiting this waits (bounded)
    until the cancelled workers have unwound.  Not awaiting it is fine (no "never awaited" warning)."""

    __slots__ = ("_agent", "_tasks")

    def __init__(self, agent: "Agent", tasks: list["asyncio.Task[None]"]) -> None:
        self._agent = agent
        self._tasks = tasks

    def __await__(self) -> Any:
        return self._agent._await_stopped(self._tasks).__await__()


class Agent:
    """Actor + Worker of one participant (or the moderator)."""

    def __init__(
        self,
        agent_id: str,
        kind: AgentKind,
        world: Any,
        brain: Brain,
        llm: LlmPort,
        usage: UsageSink | None,
        trace: TraceSink | None,
        sink: EventSink,
        config: AgentConfig | None = None,
        *,
        book: ConversationBook | None = None,
        context_window: Callable[[], int] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.kind: AgentKind = kind
        self.world = world
        self.brain = brain
        self.llm = llm
        self.usage = usage
        self.trace = trace
        self.sink = sink
        self.config = config or AgentConfig()
        self.book = book if book is not None else ConversationBook()
        #: ``() -> int``: the context window (tokens) of this agent's model; ``None`` / ``<= 0`` = unknown
        #: (then neither the compaction reminder nor the emergency trim ever happens).
        self.context_window = context_window
        self.status: AgentState = "sleeping"
        self.stats: dict[str, int] = {
            "activations": 0, "runs": 0, "cancels": 0, "failures": 0, "gave_up": 0, "injections": 0,
        }

        self._gen = 0  # generation of the current / latest run
        self._run: _Run | None = None
        self._task: asyncio.Task[None] | None = None
        self._abandoned: list[asyncio.Task[None]] = []  # cancelled workers that did not unwind in time
        self._pending: list[Envelope] = []  # envelopes no run has consumed yet, in arrival order
        self._activation_handle: asyncio.Handle | None = None
        self._retry_handle: asyncio.TimerHandle | None = None
        self._failures = 0  # consecutive failed attempts
        self._cancel_streak = 0  # consecutive cancelled runs (anti-starvation)
        self._cancelling = False  # a cancelled worker is still unwinding
        self._cancel_target: asyncio.Task[None] | None = None
        self._grace_handle: asyncio.TimerHandle | None = None
        self._stopped = True  # armed by ``activate()``, disarmed by ``stop()``
        self._last_status: tuple[str, float | None] | None = None

    # ================================================================== public API ==
    @property
    def gen_id(self) -> int:
        """Generation of the current (or most recent) run."""
        return self._gen

    @property
    def busy(self) -> bool:
        """A Worker is alive (thinking or acting), or a cancelled one is still unwinding."""
        return self._run is not None or self._cancelling

    @property
    def pending(self) -> list[Envelope]:
        """A copy of the envelopes no run has consumed yet."""
        return list(self._pending)

    @property
    def sleep_until(self) -> float | None:
        """Game-clock deadline of the current timed sleep (``None``: none / sleeping until something arrives)."""
        conv = self.book.get(self.agent_id)
        return conv.sleep_until if conv is not None else None

    @property
    def has_timer(self) -> bool:
        return self.sleep_until is not None

    def is_quiet(self) -> bool:
        """Nothing can happen on its own: no run, nothing pending, no scheduled activation or retry.
        (A timed sleep is *not* counted: see :attr:`has_timer`.)"""
        return (
            self._run is None
            and not self._cancelling
            and not self._pending
            and self._activation_handle is None
            and self._retry_handle is None
        )

    def activate(self) -> None:
        """Arm the agent (the supervisor does it on start).  Everything volatile is reset - the conversation
        and its timer live in the :class:`ConversationBook` and are kept - and ``sleeping`` is published."""
        self._stopped = False
        self._reset_volatile()
        self._last_status = None
        self.status = "sleeping"
        self._set_status("sleeping")

    def stop(self) -> "_Stopping":
        """Stop the agent: cancel its Worker and every timer, drop what is pending and show ``sleeping``.

        The stop itself is synchronous and idempotent.  The returned object may be awaited
        (``await agent.stop()``) to wait until the cancelled Worker has really unwound; the
        :class:`~app.runtime.supervisor.Supervisor` does so, which is what makes ``stop()`` leak nothing.
        The conversation (and its sleep timer) stays in the book: a resumed game continues with it.
        """
        return _Stopping(self, self._request_stop())

    def deliver(self, envelope: Envelope) -> None:
        """Hand an envelope to the agent (synchronous, never blocks; called by the supervisor's pump).

        It becomes pending and wakes the agent: a sleeping agent is activated on the next loop iteration
        (all envelopes delivered meanwhile are one activation); a busy one gets it merged at the next round
        boundary - unless it is an ``interrupt`` that invalidates the work in flight."""
        if self._stopped:
            return
        self._pending.append(envelope)
        run = self._run
        if run is not None and not self._cancelling:
            if envelope.interrupt and self._matches_focus(run, envelope):
                if self._cancel_streak < self.config.starvation_limit:
                    self._cancel_run(envelope)
                else:
                    # Starvation guard: this interrupt is merged at the next round boundary instead.
                    self._trace(
                        "inject_instead_of_cancel", gen=run.gen, streak=self._cancel_streak, envelope=_brief(envelope)
                    )
            return
        self._request_activation()

    # ------------------------------------------------------------------- timers ---
    def timer_due(self, now: float) -> bool:
        """The agent sleeps with a timer that has run out (``now`` is the game clock, which is frozen while
        the game is paused)."""
        if self._stopped or self._run is not None or self._cancelling:
            return False
        if self._pending or self._activation_handle is not None or self._retry_handle is not None:
            return False  # about to be woken anyway
        deadline = self.sleep_until
        return deadline is not None and now >= deadline

    def fire_timer(self, now: float | None = None) -> bool:
        """Wake the agent with a ``timer.elapsed`` notice if its timer is due; returns whether it did."""
        at = self._clock_now() if now is None else float(now)
        if not self.timer_due(at):
            return False
        conv = self.book.get(self.agent_id)
        slept = conv.slept_s if conv is not None and conv.slept_s is not None else 0.0
        self._trace("timer", slept_s=slept, now=at)
        self.deliver(
            Envelope(
                kind="notice",
                priority="normal",
                sender=ENGINE_ID,
                payload={"event": "timer.elapsed", "slept_s": round(float(slept), 3)},
            )
        )
        return True

    # ============================================================ activation (Actor) ==
    def _request_activation(self) -> None:
        """Schedule ONE activation for the next loop iteration (a no-op while busy, retrying or stopped)."""
        if self._stopped or self._run is not None or self._cancelling:
            return
        if self._activation_handle is not None or self._retry_handle is not None:
            return
        try:
            self._activation_handle = asyncio.get_running_loop().call_soon(self._on_activation)
        except RuntimeError:  # no running loop (the loop is closing): nobody is listening any more
            self._activation_handle = None

    def _on_activation(self) -> None:
        self._activation_handle = None
        if self._stopped or self._run is not None or self._cancelling or self._retry_handle is not None:
            return
        if not self._pending:
            return
        try:
            self._start_run(retry=False)
        except Exception:  # noqa: BLE001 - a loop callback must not die silently
            self._internal_error("start_run")

    def _on_retry(self) -> None:
        self._retry_handle = None
        if self._stopped or self._run is not None or self._cancelling:
            return
        try:
            self._start_run(retry=True)
        except Exception:  # noqa: BLE001
            self._internal_error("retry")

    @staticmethod
    def _matches_focus(run: _Run, envelope: Envelope) -> bool:
        if envelope.channel_id and envelope.channel_id in run.focus_channels:
            return True
        request_id = envelope.payload.get("request_id")
        return isinstance(request_id, str) and request_id in run.focus_requests

    def _start_run(self, *, retry: bool) -> None:
        """Take everything pending and spawn a Worker for it."""
        reasons, self._pending = self._pending, []
        self._gen += 1
        run = _Run(gen=self._gen, reasons=reasons, attempt=self._failures, retry=retry)
        run.absorb(reasons)
        conv = self.book.get(self.agent_id)
        if conv is not None and conv.sleep_until is not None:
            # An arrival voids the timer of a sleep; a run that turns out to be a no-op restores it.
            run.prev_sleep_until, run.prev_slept_s = conv.sleep_until, conv.slept_s
            conv.sleep_until = None
            conv.slept_s = None
        self._run = run
        if not retry:
            self.stats["activations"] += 1
            self._count("activations")
        task = asyncio.get_running_loop().create_task(self._worker_main(run), name=f"worker:{self.agent_id}:{run.gen}")
        self._task = task
        task.add_done_callback(lambda finished, run=run: self._on_task_done(run, finished))
        self._trace(
            "wake", gen=run.gen, attempt=run.attempt, retry=retry, reasons=[_brief(e) for e in reasons]
        )

    def _cancel_run(self, envelope: Envelope) -> None:
        """Drop the in-flight work: bump the generation, cancel the Worker, and let it unwind.  The agent
        is activated again (with everything pending, the interrupt included) once the Worker is gone - or
        after ``cancel_grace_s`` if it ignores the cancellation (it is then abandoned and its late result
        discarded)."""
        run, task = self._run, self._task
        if run is None or task is None:
            return
        self._cancel_streak += 1
        self.stats["cancels"] += 1
        self._gen += 1  # from now on the run is stale: whatever it still produces is discarded
        self._trace("cancel", gen=run.gen, streak=self._cancel_streak, envelope=_brief(envelope))
        if not run.appended:
            self._pending[:0] = run.reasons  # cancelled before it could tell the agent anything: tell it next time
        self._run = None
        self._task = None
        if task.done():
            self._request_activation()
            return
        self._cancelling = True
        self._cancel_target = task
        task.cancel()
        self._grace_handle = asyncio.get_running_loop().call_later(
            self.config.cancel_grace_s, self._on_cancel_grace, task
        )

    def _on_cancel_grace(self, task: asyncio.Task[None]) -> None:
        self._grace_handle = None
        if self._cancel_target is not task:
            return
        logger.error("worker of %s did not stop within %.1fs; abandoning it", self.agent_id, self.config.cancel_grace_s)
        self._trace("error", message="worker did not stop after cancellation; abandoned")
        if not task.done() and task not in self._abandoned:
            self._abandoned.append(task)
        self._cancel_target = None
        self._cancelling = False
        self._request_activation()

    def _request_stop(self) -> list["asyncio.Task[None]"]:
        was_stopped = self._stopped
        self._stopped = True
        self._gen += 1  # whatever a Worker still holds is now stale
        tasks = [t for t in (self._task, self._cancel_target, *self._abandoned) if t is not None and not t.done()]
        for task in tasks:
            task.cancel()
        self._run = None
        self._task = None
        self._cancel_target = None
        self._cancelling = False
        self._reset_volatile()
        if not was_stopped:
            self._set_status("sleeping")  # a paused / finished game shows every agent as sleeping
        return tasks

    async def _await_stopped(self, tasks: list["asyncio.Task[None]"]) -> None:
        """Wait (bounded) for the cancelled workers of :meth:`stop`; a stubborn one is abandoned."""
        pending = [t for t in tasks if not t.done()]
        if not pending:
            return
        _, still_running = await asyncio.wait(pending, timeout=self.config.cancel_grace_s)
        for task in still_running:
            logger.error("worker of %s did not stop within %.1fs; abandoning it", self.agent_id, self.config.cancel_grace_s)
            self._trace("error", message="worker did not stop after cancellation; abandoned")
            if task not in self._abandoned:
                self._abandoned.append(task)

    def _reset_volatile(self) -> None:
        """Forget what belongs to one session of the actor: pending envelopes, timers, counters."""
        for handle in (self._activation_handle, self._retry_handle, self._grace_handle):
            if handle is not None:
                handle.cancel()
        self._activation_handle = None
        self._retry_handle = None
        self._grace_handle = None
        self._pending = []
        self._failures = 0
        self._cancel_streak = 0

    # ================================================================ run lifecycle ==
    def _stale(self, run: _Run) -> bool:
        return self._stopped or run.gen != self._gen or self._run is not run

    def _on_task_done(self, run: _Run, task: asyncio.Task[None]) -> None:
        """Safety net: retrieve exceptions, notice a cancelled worker that finally unwound, and notice a
        worker that was cancelled behind our back."""
        if task in self._abandoned:
            self._abandoned.remove(task)
        if self._cancel_target is task:  # the Worker we cancelled is gone: the agent may start again
            self._cancel_target = None
            self._cancelling = False
            if self._grace_handle is not None:
                self._grace_handle.cancel()
                self._grace_handle = None
            self._request_activation()
        if task.cancelled():
            if not self._stale(run):
                self._on_run_failed(run, RuntimeError("worker was cancelled unexpectedly"))
            return
        exc = task.exception()  # marks the exception as retrieved
        if exc is not None and not self._stale(run):
            self._on_run_failed(run, exc)

    def _on_run_done(self, run: _Run) -> None:
        if self._stale(run):
            return
        self._run = None
        self._task = None
        self._failures = 0
        self._cancel_streak = 0
        self.stats["runs"] += 1
        conv = self.book.get(self.agent_id)
        if conv is not None:
            if run.end_reason == "nothing" and not any(
                e.payload.get("event") == "timer.elapsed" for e in run.reasons
            ):
                # A no-op activation changes nothing: the sleep the arrival voided goes on.
                conv.sleep_until, conv.slept_s = run.prev_sleep_until, run.prev_slept_s
            elif self._pending or run.sleep_s is None:
                conv.sleep_until, conv.slept_s = None, None
            else:
                conv.sleep_until = self._clock_now() + run.sleep_s
                conv.slept_s = run.sleep_s
        self._trace(
            "run_end", gen=run.gen, reason=run.end_reason or "done", rounds=run.rounds,
            sleep_s=run.sleep_s, seen_seq=conv.seen_seq if conv is not None else 0,
        )
        if self._pending:
            self._request_activation()  # envelopes arrived while the run was ending: next activation
        else:
            self._set_status("sleeping")
            if conv is not None and conv.sleep_until is not None:
                self._trace("sleep", gen=run.gen, until=conv.sleep_until, slept_s=conv.slept_s)

    def _on_run_failed(self, run: _Run, exc: BaseException) -> None:
        if self._stale(run):
            return
        self._run = None
        self._task = None
        self._failures += 1
        self.stats["failures"] += 1
        text = f"{type(exc).__name__}: {exc}"
        logger.warning("run %d of %s failed (attempt %d): %s", run.gen, self.agent_id, self._failures, text)
        self._trace(
            "error",
            gen=run.gen,
            attempt=self._failures,
            message=text,
            traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-3000:],
        )
        if self._failures <= self.config.llm_retries:
            delay = min(self.config.retry_backoff_max_s, self.config.retry_backoff_s * 2 ** (self._failures - 1))
            if not run.appended:
                self._pending[:0] = run.reasons  # the agent was not told yet: the retry must include them
            self._retry_handle = asyncio.get_running_loop().call_later(delay, self._on_retry)
            self._trace("retry", gen=run.gen, attempt=self._failures, delay_s=round(delay, 3))
            return
        self._failures = 0
        self.stats["gave_up"] += 1
        self._trace("give_up", gen=run.gen, message=text)
        self._publish(
            "error",
            message=f"{self._display_name()}（{self.agent_id}）的本次行动失败，已放弃：{text}",
            agent_id=self.agent_id,
        )
        if self._pending:
            self._request_activation()
        else:
            self._set_status("sleeping")

    # ================================================================ the Worker ==
    async def _worker_main(self, run: _Run) -> None:
        try:
            await self._run_rounds(run)
        except Exception as exc:  # noqa: BLE001 - isolate every failure to this agent
            self._on_run_failed(run, exc)
            return
        self._on_run_done(run)

    def _max_tokens(self) -> int | None:
        if self.config.max_tokens:
            return int(self.config.max_tokens)
        try:
            return int(self.world.spec.defaults.llm.max_tokens)
        except Exception:  # noqa: BLE001 - a world without a spec just uses the client default
            return None

    def _window(self) -> int:
        provider = self.context_window
        if provider is None:
            return 0
        try:
            return max(0, int(provider() or 0))
        except Exception:  # noqa: BLE001 - an unreadable binding means "unknown window"
            logger.warning("context_window() failed for %s", self.agent_id, exc_info=True)
            return 0

    def _clock_now(self) -> float:
        try:
            return float(self.world.clock.now())
        except Exception:  # noqa: BLE001
            return 0.0

    def _open_conversation(self, run: _Run) -> Conversation | None:
        """Create the conversation (FULL opening message) or append this activation's DELTA.

        Returns ``None`` when there is nothing to tell (no model call is made).  Everything here is
        synchronous: the conversation never holds a half-built state."""
        aid, brain = self.agent_id, self.brain
        conv = self.book.get(aid)
        if conv is None:
            ctx = WakeContext(agent_id=aid, kind="start", gen_id=run.gen, reasons=list(run.reasons), after_seq=0)
            text, seen = brain.start_message(aid, ctx)
            tools = copy.deepcopy(brain.tool_schemas(aid))
            conv = Conversation(
                agent_id=aid,
                system=brain.system_prompt(aid),
                tools=tools,
                tools_sig=tools_signature(tools),
                seen_seq=int(seen),
            )
            conv.messages.append(user_message(text))
            self.book.put(conv)
            run.appended = True
            self._trace("conversation", gen=run.gen, epoch=conv.epoch, tools=conv.tools_sig, seen_seq=conv.seen_seq)
            return conv
        if run.retry and not run.reasons:
            run.appended = True  # a re-attempt of a call that failed: the same conversation, nothing new
            return conv
        ctx = WakeContext(
            agent_id=aid, kind="inject", gen_id=run.gen, reasons=list(run.reasons), after_seq=conv.seen_seq
        )
        result = brain.inject_message(aid, ctx)
        run.appended = True
        if not result or not result[0]:
            return conv if run.retry else None
        text, new_seen = result
        conv.seen_seq = max(conv.seen_seq, int(new_seen))
        conv.messages.append(user_message("\n\n".join([text, *conv.take_notes()])))
        self._trace("inject", gen=run.gen, kind="activation", reasons=[_brief(e) for e in run.reasons], seen_seq=conv.seen_seq)
        return conv

    async def _run_rounds(self, run: _Run) -> None:
        aid, brain = self.agent_id, self.brain
        brain.begin_run(aid)
        conv = self._open_conversation(run)
        if conv is None:
            run.end_reason = "nothing"
            return
        max_tokens = self._max_tokens()
        nudged = False
        cut_off_retried = False
        round_idx = 0

        while True:  # no round limit: the run ends when the agent sleeps (or has nothing more to do)
            run.rounds = round_idx + 1
            self._set_status("thinking")
            started = _now()
            resp = await self.llm.complete(aid, conv.request_messages(), conv.tools, max_tokens=max_tokens)
            if self._stale(run):  # superseded while we waited: the answer is dropped, never written
                run.end_reason = "stale"
                self._trace("discard", gen=run.gen, reason="stale after llm call")
                return
            self._account_llm(run, round_idx, resp, _now() - started, conv)

            if not resp.tool_calls:
                reply = assistant_text_message(resp)
                if reply is not None:
                    conv.messages.append(reply)  # what the model said stays part of its conversation
                parts: list[str] = []
                if str(resp.finish_reason or "").lower() in TRUNCATION_REASONS and not cut_off_retried:
                    cut_off_retried = True  # one recovery attempt per run, then the usual rules apply
                    parts.append(CUT_OFF_NUDGE)
                    self._trace("nudge", gen=run.gen, round=round_idx, why="length")
                elif not nudged:
                    nudge = self._brain_call("no_tool_nudge", brain.no_tool_nudge, aid, resp.content or "", round_idx)
                    if nudge:
                        nudged = True
                        parts.append(nudge)
                        self._trace("nudge", gen=run.gen, round=round_idx)
                self._maintain(conv)
                if self._pending:
                    merged = self._merge_pending(run, conv)
                    if merged:
                        parts.append(merged)
                if not parts:
                    run.end_reason = "no_tool"  # same as a sleep without a timer
                    return
                conv.messages.append(user_message("\n\n".join([*parts, *conv.take_notes()])))
                round_idx += 1
                continue

            ensure_call_ids(resp, prefix=f"call_{run.gen}_{round_idx}")
            self._set_status("acting")
            results: list[dict[str, Any]] = []
            ends_run = rebuild = False
            sleeps: list[float] = []
            for call in resp.tool_calls:  # no await in here: nothing can be cancelled half-way
                outcome = self._run_tool(run, conv, call)
                results.append(tool_message(call.id, outcome.text))
                ends_run = ends_run or outcome.ends_run
                rebuild = rebuild or outcome.rebuild
                if outcome.ends_run and outcome.sleep_s is not None:
                    seconds = _positive_seconds(outcome.sleep_s)
                    if seconds is not None:
                        sleeps.append(seconds)
            conv.messages.append(assistant_tool_message(resp))
            conv.messages.extend(results)
            if self._stale(run):  # an interrupt delivered from inside a tool: the batch is whole, the run is over
                run.end_reason = "stale"
                self._trace("discard", gen=run.gen, reason="stale after tools")
                return

            if rebuild:
                conv = self._rebuild(run, conv)
            else:
                self._maintain(conv)
            if ends_run:
                run.sleep_s = min(sleeps) if sleeps else None
                run.end_reason = "ends_run"
                return
            if not rebuild:
                parts = []
                if self._pending:
                    merged = self._merge_pending(run, conv)
                    if merged:
                        parts.append(merged)
                parts.extend(conv.take_notes())
                if parts:
                    conv.messages.append(user_message("\n\n".join(parts)))
            round_idx += 1

    # ------------------------------------------------------------ conversation upkeep ---
    def _merge_pending(self, run: _Run, conv: Conversation) -> str | None:
        """Hand everything that arrived meanwhile to the brain as ONE delta; returns its text, if any."""
        reasons, self._pending = self._pending, []
        run.absorb(reasons)
        ctx = WakeContext(
            agent_id=self.agent_id, kind="inject", gen_id=run.gen, reasons=reasons, after_seq=conv.seen_seq
        )
        result = self._brain_call("inject_message", self.brain.inject_message, self.agent_id, ctx)
        if not result:
            return None
        text, new_seen = result
        if not text:
            return None
        conv.seen_seq = max(conv.seen_seq, int(new_seen))
        self.stats["injections"] += 1
        self._trace("inject", gen=run.gen, kind="merge", reasons=[_brief(e) for e in reasons], seen_seq=conv.seen_seq)
        return text

    def _rebuild(self, run: _Run, conv: Conversation) -> Conversation:
        """``compact_context``: drop the conversation and start a new one from the world (FULL message).
        ``system`` and ``tools`` are kept byte for byte, so the provider's cache still covers them."""
        aid = self.agent_id
        self._brain_call("reset_conversation", self.brain.reset_conversation, aid)
        extra, self._pending = self._pending, []
        run.absorb(extra)
        ctx = WakeContext(agent_id=aid, kind="start", gen_id=run.gen, reasons=list(run.consumed), after_seq=0)
        text, seen = self.brain.start_message(aid, ctx)
        fresh = Conversation(
            agent_id=aid,
            system=conv.system,
            tools=conv.tools,
            tools_sig=conv.tools_sig,
            messages=[user_message(text)],
            seen_seq=int(seen),
            epoch=conv.epoch + 1,
        )
        self.book.put(fresh)
        self._trace("rebuild", gen=run.gen, epoch=fresh.epoch, seen_seq=fresh.seen_seq, dropped=len(conv.messages))
        return fresh

    def _maintain(self, conv: Conversation) -> None:
        """Compaction by orchestration: remind once at ``compact_at`` of the window, trim at ``trim_at``."""
        window = self._window()
        used = conv.last_input_tokens
        if window <= 0 or used <= 0:
            return
        cfg = self.config
        if cfg.trim_at and used >= cfg.trim_at * window:
            per_char = used / max(1, conv.total_chars())  # tokens per character of this conversation
            target = (cfg.compact_at or cfg.trim_at) * window
            rounds, dropped = conv.drop_oldest_rounds(int((used - target) / max(per_char, 1e-9)) + 1)
            if rounds:
                conv.last_input_tokens = max(0, int(used - dropped * per_char))
                conv.notes.append(TRIM_NOTICE.format(rounds=rounds))
                self._trace(
                    "trim", rounds=rounds, dropped_chars=dropped, used_before=used,
                    used_after=conv.last_input_tokens, window=window, epoch=conv.epoch,
                )
            else:
                self._trace("trim_failed", used=used, window=window)
            return
        if cfg.compact_at and used >= cfg.compact_at * window and not conv.compact_nudged:
            conv.compact_nudged = True
            conv.notes.append(COMPACT_NUDGE)
            self._trace("compact_nudge", used=used, window=window)

    def _brain_call(self, what: str, fn: Any, *args: Any) -> Any:
        """Call a non-essential Brain hook; a bug in it is traced and ignored instead of killing the run."""
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001
            logger.exception("brain.%s failed for %s", what, self.agent_id)
            self._trace("error", message=f"brain.{what} failed: {type(exc).__name__}: {exc}")
            return None

    def _run_tool(self, run: _Run, conv: Conversation, call: Any) -> ToolOutcome:
        name = str(call.name or "")
        args, error = parse_call(call)
        self._count("tool_calls")
        if error:
            outcome = ToolOutcome(text=args_error_text(name, error))
        else:
            try:
                outcome = self.brain.execute_tool(self.agent_id, name, args, gen_id=run.gen, seen_seq=conv.seen_seq)
            except Exception as exc:  # noqa: BLE001 - keep the call paired with a result
                logger.exception("tool %s failed for %s", name, self.agent_id)
                self._trace("error", gen=run.gen, message=f"tool {name} raised {type(exc).__name__}: {exc}")
                outcome = ToolOutcome(text=tool_failure_text(name, exc))
            if not isinstance(outcome, ToolOutcome):
                outcome = ToolOutcome(text=str(outcome))
        receipt = outcome.receipt
        if receipt is not None and not receipt.ok:
            self._count("rejections")
        self._trace(
            "tool_call",
            gen=run.gen,
            name=name,
            call_id=call.id,
            args=args if not error else {"_error": error, "_raw": call.arguments_json},
            result=outcome.text,
            ends_run=outcome.ends_run,
            sleep_s=outcome.sleep_s,
            rebuild=outcome.rebuild,
            shared=outcome.shared,
        )
        if receipt is not None:
            self._trace(
                "receipt",
                gen=run.gen,
                name=name,
                ok=receipt.ok,
                code=receipt.code,
                seq=receipt.seq,
                message=receipt.message,
                truncated=receipt.truncated,
                floor_left=receipt.floor_left,
            )
        return outcome

    def _account_llm(
        self, run: _Run, round_idx: int, resp: LLMResponse, elapsed: float, conv: Conversation
    ) -> None:
        if self.usage is not None:
            try:
                self.usage.record_llm(self.agent_id, resp)
            except Exception:  # noqa: BLE001 - accounting must never kill a run
                logger.exception("usage.record_llm failed for %s", self.agent_id)
        cache_read = int(getattr(resp, "cache_read_tokens", 0) or 0)
        cache_write = int(getattr(resp, "cache_write_tokens", 0) or 0)
        conv.last_input_tokens = int(resp.prompt_tokens or 0) + cache_read + cache_write
        thought = (resp.thought or "").strip()
        if thought:
            self._count("thinking_rounds")
        fields: dict[str, Any] = {
            "gen": run.gen,
            "round": round_idx,
            "epoch": conv.epoch,
            "provider": resp.provider_id,
            "model": resp.model_id,
            "prompt_tokens": resp.prompt_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "completion_tokens": resp.completion_tokens,
            "finish_reason": resp.finish_reason,
            "tool_calls": [call.name for call in resp.tool_calls],
            "elapsed_ms": int(elapsed * 1000),
        }
        if self.config.trace_messages:
            fields["messages"] = conv.request_messages()  # a snapshot: the conversation keeps growing
        self._trace("llm_call", **fields)
        if thought:
            self._trace("thought", gen=run.gen, round=round_idx, text=thought)
        if (resp.content or "").strip():
            # Without a ``speak`` call the plain text is only an inner monologue (D5): log it, nothing else.
            self._trace("content", gen=run.gen, round=round_idx, text=resp.content)

    # ================================================================ plumbing ==
    def _set_status(self, state: AgentState) -> None:
        until = self.sleep_until if state == "sleeping" else None
        key = (state, until)
        if key == self._last_status:
            return
        self._last_status = key
        self.status = state
        self._publish("agent.status", id=self.agent_id, state=state, until=until)

    def _publish(self, event_type: str, **data: Any) -> None:
        try:
            self.sink.publish(UiEvent(type=event_type, data=data))
        except Exception:  # noqa: BLE001 - the UI side must never break the game
            logger.exception("event sink failed for %s", event_type)

    def _display_name(self) -> str:
        try:
            return str(self.world.display_name(self.agent_id))
        except Exception:  # noqa: BLE001 - only used for a message
            return self.agent_id

    def _count(self, what: str) -> None:
        if self.usage is None:
            return
        try:
            self.usage.count(self.agent_id, what)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            logger.exception("usage.count(%s) failed for %s", what, self.agent_id)

    def _trace(self, event: str, **fields: Any) -> None:
        if self.trace is None:
            return
        try:
            self.trace.write(event, self.agent_id, **fields)
        except Exception:  # noqa: BLE001 - a broken trace must never break a run
            logger.debug("trace write failed", exc_info=True)

    def _internal_error(self, where: str, envelope: Envelope | None = None) -> None:
        logger.exception("actor of %s failed in %s", self.agent_id, where)
        self._trace("error", message=f"actor failure in {where}", envelope=_brief(envelope) if envelope else None)


def _positive_seconds(value: Any) -> float | None:
    """A usable sleep duration (> 0, finite) or ``None`` (= sleep until something arrives)."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None
