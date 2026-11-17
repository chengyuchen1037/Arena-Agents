"""One agent of the game: an **Actor** (inbox loop) plus a **Worker** (one run), v0.12 contracts §3.2.

Actor
    ``run()`` is a coroutine whose only suspension point is ``await mailbox.wait()``; it never calls
    the model and never waits for one.  It keeps the *pending buffer* (envelopes not consumed by a
    run yet), decides when to start a Worker, when to cancel it and what to inject into it.  The one
    bounded ``await`` inside it is waiting for a cancelled Worker to unwind.

Worker
    One *run*: build the conversation from the world (``brain.start_message``), then loop
    ``LLM call -> tools -> inject`` until the model stops calling tools, a tool ends the run, the round
    budget is used up, or the run is cancelled.  The conversation never outlives a run, so a
    cancellation can never leave an unpaired ``tool_call`` behind.  World and Brain calls are
    synchronous and fast; **the LLM call is the only ``await``** of a run, hence the only place a
    cancellation lands.

Wake-up policy (priority is set by the world, see docs/v0.12-contracts.md §2.6)

    =========  ===============================  ===============================================
    priority   Worker idle                      Worker running
    =========  ===============================  ===============================================
    urgent     start a run now                  injected at the next round boundary
    normal     start ``debounce_s`` after the   injected at the next round boundary
               *first* pending one (fixed
               window, not sliding)
    ambient    buffered only                    injected at the next round boundary
    =========  ===============================  ===============================================

* ``interrupt`` envelopes cancel the running Worker when their ``channel_id`` / ``payload.request_id``
  matches the run's *focus* (the channels / requests of everything the run consumed).  After a
  cancellation the Actor restarts - at once, without debounce - only if an urgent / normal envelope
  is still pending; otherwise it goes back to idle (a revoked floor does not cost another LLM call).
* Anti-starvation: after ``starvation_limit`` (2) consecutive cancellations an interrupt is injected
  instead of cancelling, until a run finishes normally.
* Every run gets a new ``gen_id``; a Worker whose generation is no longer current stops before it
  touches the world, so a late result of a cancelled run is simply discarded.
* A failing run (LLM exception, timeout, Brain bug) is retried after an exponential backoff (at most
  ``llm_retries`` times), then given up with an ``error`` event; other agents are never affected.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Literal

from app.llm.types import LLMResponse
from app.runtime.mailbox import Mailbox
from app.runtime.messages import (
    args_error_text,
    assistant_text_message,
    assistant_tool_message,
    ensure_call_ids,
    parse_call,
    system_message,
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
from app.world.types import Envelope, UiEvent

logger = logging.getLogger(__name__)

AgentKind = Literal["player", "moderator"]
AgentState = Literal["idle", "thinking", "acting", "stopped"]

#: Maximum LLM rounds of one run, by agent kind and execution depth (contracts §3.2).
DEFAULT_MAX_ROUNDS: dict[str, dict[str, int]] = {
    "player": {"low": 3, "medium": 5, "high": 8, "xhigh": 12},
    "moderator": {"low": 6, "medium": 10, "high": 16, "xhigh": 24},
}
_CONSUMED_CAP = 60  # urgent / normal envelopes remembered per run (for retries and rebuilds)


@dataclass
class AgentConfig:
    """Tunables shared by all agents of a game; mutate it in place to change them live."""

    debounce_s: float = 1.5  # NORMAL wake-up delay after the first pending normal envelope
    execution_depth: str = "medium"  # low | medium | high | xhigh (picks the round budget)
    max_rounds: dict[str, dict[str, int]] = field(default_factory=lambda: copy.deepcopy(DEFAULT_MAX_ROUNDS))
    max_tokens: int | None = None  # None -> ``world.spec.defaults.llm.max_tokens``
    starvation_limit: int = 2  # consecutive cancellations before an interrupt is injected instead
    llm_retries: int = 2  # re-queues of a failed run (so up to 3 attempts)
    retry_backoff_s: float = 1.0  # first backoff; doubled for every further failure
    retry_backoff_max_s: float = 30.0
    cancel_grace_s: float = 5.0  # how long the Actor waits for a cancelled Worker to unwind
    ambient_cap: int = 200  # ambient envelopes kept while idle (oldest dropped)
    trace_messages: bool = False  # also trace the full conversation of every LLM call

    def rounds_for(self, kind: str) -> int:
        table = self.max_rounds.get(kind) or self.max_rounds.get("player") or {}
        rounds = table.get(self.execution_depth) or table.get("medium") or 5
        return max(1, int(rounds))


@dataclass
class _Run:
    gen: int
    reasons: list[Envelope]
    ambient: list[Envelope]
    attempt: int = 0
    focus_channels: set[str] = field(default_factory=set)
    focus_requests: set[str] = field(default_factory=set)
    consumed: list[Envelope] = field(default_factory=list)
    seen_seq: int = 0
    rounds: int = 0
    end_reason: str = ""

    def absorb(self, envelopes: list[Envelope]) -> None:
        """Remember urgent / normal envelopes this run has consumed and widen its focus."""
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
        self.mailbox = Mailbox()
        self.status: AgentState = "stopped"
        self.stats: dict[str, int] = {"runs": 0, "cancels": 0, "failures": 0, "gave_up": 0, "injections": 0}

        self._gen = 0  # generation of the current / latest run
        self._run: _Run | None = None
        self._task: asyncio.Task[None] | None = None
        self._abandoned: list[asyncio.Task[None]] = []  # cancelled workers that did not unwind in time
        self._pending: list[Envelope] = []  # envelopes no run has consumed yet, in arrival order
        self._ambient_count = 0
        self._normal_due: float | None = None  # loop time at which the first pending normal is due
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._retry_handle: asyncio.TimerHandle | None = None
        self._carry: list[Envelope] = []  # reasons of a failed run, waiting for the retry
        self._failures = 0  # consecutive failed runs
        self._cancel_streak = 0  # consecutive cancelled runs (anti-starvation)
        self._restart_now = False  # a run was just cancelled: pending normal envelopes skip the debounce
        self._cancelling = False
        self._stopped = False  # set by ``stop()``; ``activate()`` re-arms a stopped agent
        self._actor_running = False

    # ================================================================== public API ==
    @property
    def gen_id(self) -> int:
        """Generation of the current (or most recent) run."""
        return self._gen

    @property
    def busy(self) -> bool:
        """A Worker is alive (thinking or acting)."""
        return self._run is not None

    @property
    def pending(self) -> list[Envelope]:
        """A copy of the envelopes no run has consumed yet."""
        return list(self._pending)

    def deliver(self, envelope: Envelope) -> None:
        """Put an envelope into the inbox.  Synchronous and non-blocking (called by the pump)."""
        self.mailbox.put(envelope)

    def is_quiet(self) -> bool:
        """Idle with nothing that could start a run: used by the supervisor's watchdog."""
        return (
            self._run is None
            and not self._cancelling
            and len(self.mailbox) == 0
            and not any(e.priority != "ambient" for e in self._pending)
            and self._debounce_handle is None
            and self._retry_handle is None
            and not self._carry
        )

    def activate(self) -> None:
        """Re-arm a stopped agent *before* a new :meth:`run` task is created (the supervisor does it).

        Doing it here rather than at the top of ``run()`` means a ``stop()`` issued before the new
        task has had its first turn is not forgotten.  Everything volatile is reset: a new session
        rebuilds its context from the world.
        """
        self._stopped = False
        self._reset_volatile()

    async def run(self) -> None:
        """The Actor main loop; returns after :meth:`stop` and re-raises its own cancellation."""
        if self._actor_running:
            raise RuntimeError(f"actor of {self.agent_id} is already running")
        self._actor_running = True
        self._set_status("idle")
        try:
            while True:
                if self._stopped:
                    break
                await self.mailbox.wait()
                if self._stopped:
                    break
                for envelope in self.mailbox.drain_arrival():
                    if self._stopped:
                        break
                    try:
                        await self._absorb(envelope)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - the Actor must survive a bug in one envelope
                        self._internal_error("absorb", envelope)
                try:
                    self._schedule()
                except Exception:  # noqa: BLE001
                    self._internal_error("schedule")
        finally:
            self._actor_running = False
            self._teardown_sync()

    def stop(self) -> "_Stopping":
        """Stop the agent: cancel its Worker, cancel the timers, drop everything pending, mark it ``stopped``.

        The stop itself is synchronous and idempotent.  The returned object may be awaited
        (``await agent.stop()``) to wait until the cancelled Worker has really unwound; the
        :class:`~app.runtime.supervisor.Supervisor` does so, which is what makes ``stop()`` leak nothing.
        """
        return _Stopping(self, self._request_stop())

    def _request_stop(self) -> list["asyncio.Task[None]"]:
        self._stopped = True
        self._gen += 1  # whatever a Worker still holds is now stale
        self._cancel_timers()
        tasks = [t for t in (self._task, *self._abandoned) if t is not None and not t.done()]
        for task in tasks:
            task.cancel()
        self._run = None
        self._task = None
        self._cancelling = False
        self._reset_volatile()
        self._set_status("stopped")
        self.mailbox.poke()  # let the Actor loop notice and return
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
        """Forget everything that belongs to a session: buffers, timers' state, counters."""
        self.mailbox.clear()
        self._cancel_timers()
        self._pending = []
        self._ambient_count = 0
        self._normal_due = None
        self._carry = []
        self._failures = 0
        self._cancel_streak = 0
        self._restart_now = False

    # ================================================================ Actor internals ==
    async def _absorb(self, envelope: Envelope) -> None:
        """Take one envelope into the pending buffer, cancelling the Worker when it asks for it."""
        run = self._run
        if envelope.interrupt and run is not None and not self._cancelling and self._matches_focus(run, envelope):
            if self._cancel_streak < self.config.starvation_limit:
                await self._cancel_run(envelope)
                if self._stopped:  # stop() arrived while we waited for the worker to unwind
                    return
            else:
                # Starvation guard: this interrupt will be injected at the next round boundary instead.
                self._trace(
                    "inject_instead_of_cancel", gen=run.gen, streak=self._cancel_streak, envelope=_brief(envelope)
                )
        self._buffer(envelope)

    @staticmethod
    def _matches_focus(run: _Run, envelope: Envelope) -> bool:
        if envelope.channel_id and envelope.channel_id in run.focus_channels:
            return True
        request_id = envelope.payload.get("request_id")
        return isinstance(request_id, str) and request_id in run.focus_requests

    def _buffer(self, envelope: Envelope) -> None:
        self._pending.append(envelope)
        if envelope.priority == "normal":
            if self._normal_due is None:
                hint = envelope.payload.get("debounce_s")
                delay = hint if isinstance(hint, (int, float)) and hint >= 0 else self.config.debounce_s
                self._normal_due = _now() + float(delay)
        elif envelope.priority == "ambient":
            self._ambient_count += 1
            if self._ambient_count > self.config.ambient_cap:
                for index, old in enumerate(self._pending):
                    if old.priority == "ambient":
                        del self._pending[index]
                        self._ambient_count -= 1
                        break

    def _schedule(self) -> None:
        """Decide what the pending buffer means right now (idempotent, synchronous)."""
        if self._stopped or self._cancelling or self._run is not None:
            return  # a live run calls ``_schedule`` again when it ends
        restart_now, self._restart_now = self._restart_now, False
        has_normal = any(e.priority == "normal" for e in self._pending)
        if (
            any(e.priority == "urgent" for e in self._pending)
            or (self._carry and self._retry_handle is None)
            # contracts §3.2: after a cancellation the run restarts *at once* when urgent / normal
            # envelopes are still pending (there is no point in letting the debounce run out now)
            or (restart_now and has_normal)
        ):
            self._start_run()
            return
        if self._normal_due is not None and has_normal:
            self._arm_debounce()
        else:
            self._normal_due = None
        self._set_status("idle")

    def _arm_debounce(self) -> None:
        if self._debounce_handle is not None or self._normal_due is None:
            return
        delay = max(0.0, self._normal_due - _now())
        self._debounce_handle = asyncio.get_running_loop().call_later(delay, self._on_debounce)

    def _on_debounce(self) -> None:
        self._debounce_handle = None
        if self._stopped or self._cancelling or self._run is not None:
            return
        if any(e.priority != "ambient" for e in self._pending):
            self._guarded_start()

    def _on_retry(self) -> None:
        self._retry_handle = None
        if self._stopped or self._cancelling or self._run is not None:
            return  # a newer run took over (and merged the carried reasons) or the actor is busy
        if self._carry:
            self._guarded_start()

    def _guarded_start(self) -> None:
        try:
            self._start_run()
        except Exception:  # noqa: BLE001 - a timer callback must not die silently
            self._internal_error("start_run")

    def _cancel_timers(self) -> None:
        for handle in (self._debounce_handle, self._retry_handle):
            if handle is not None:
                handle.cancel()
        self._debounce_handle = None
        self._retry_handle = None

    def _start_run(self) -> None:
        """Consume the pending buffer and spawn a Worker for it."""
        self._cancel_timers()
        carried, self._carry = self._carry, []
        reasons = carried + [e for e in self._pending if e.priority != "ambient"]
        ambient = [e for e in self._pending if e.priority == "ambient"]
        self._pending = []
        self._ambient_count = 0
        self._normal_due = None
        self._gen += 1
        run = _Run(gen=self._gen, reasons=reasons, ambient=ambient, attempt=self._failures)
        run.absorb(reasons)
        self._run = run
        task = asyncio.get_running_loop().create_task(
            self._worker_main(run), name=f"worker:{self.agent_id}:{run.gen}"
        )
        self._task = task
        task.add_done_callback(lambda finished, run=run: self._on_task_done(run, finished))
        self._set_status("thinking")
        self._trace(
            "wake",
            gen=run.gen,
            attempt=run.attempt,
            reasons=[_brief(e) for e in reasons],
            ambient=len(ambient),
        )

    async def _cancel_run(self, envelope: Envelope) -> None:
        run, task = self._run, self._task
        if run is None:
            return
        self._cancel_streak += 1
        self.stats["cancels"] += 1
        self._gen += 1  # from now on the run is stale: whatever it still produces is discarded
        self._cancelling = True
        self._trace("cancel", gen=run.gen, streak=self._cancel_streak, envelope=_brief(envelope))
        try:
            if task is not None:
                await self._terminate(task)
        finally:
            self._cancelling = False
            self._restart_now = True  # consumed by the ``_schedule`` that closes this batch
            if self._run is run:
                self._run = None
                self._task = None

    async def _terminate(self, task: asyncio.Task[None]) -> None:
        """Cancel ``task`` and wait (bounded) until it has unwound."""
        if task.done():
            return
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=self.config.cancel_grace_s)
        if not done:
            logger.error("worker of %s did not stop within %.1fs; abandoning it", self.agent_id, self.config.cancel_grace_s)
            self._trace("error", message="worker did not stop after cancellation; abandoned")
            if task not in self._abandoned:
                self._abandoned.append(task)

    def _teardown_sync(self) -> None:
        """Last-resort cleanup when the Actor task itself is cancelled."""
        self._stopped = True
        self._cancel_timers()
        for task in (self._task, *self._abandoned):
            if task is not None and not task.done():
                task.cancel()
        self._set_status("stopped")

    # ================================================================ run lifecycle ==
    def _stale(self, run: _Run) -> bool:
        return self._stopped or run.gen != self._gen or self._run is not run

    def _on_task_done(self, run: _Run, task: asyncio.Task[None]) -> None:
        """Safety net: retrieve exceptions and notice a worker that was cancelled behind our back."""
        if task in self._abandoned:
            self._abandoned.remove(task)
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
        self._carry = []
        self.stats["runs"] += 1
        self._trace("run_end", gen=run.gen, reason=run.end_reason or "done", rounds=run.rounds, seen_seq=run.seen_seq)
        self._schedule()

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
            self._carry = list(run.consumed)
            self._retry_handle = asyncio.get_running_loop().call_later(delay, self._on_retry)
            self._trace("retry", gen=run.gen, attempt=self._failures, delay_s=round(delay, 3))
        else:
            self._failures = 0
            self._carry = []
            self.stats["gave_up"] += 1
            self._trace("give_up", gen=run.gen, message=text)
            self._publish(
                "error",
                message=f"{self._display_name()}（{self.agent_id}）的本次行动失败，已放弃：{text}",
                agent_id=self.agent_id,
            )
        self._schedule()

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

    async def _run_rounds(self, run: _Run) -> None:
        aid = self.agent_id
        brain = self.brain
        wake = WakeContext(
            agent_id=aid, kind="start", gen_id=run.gen, reasons=list(run.reasons), ambient=list(run.ambient)
        )
        text, seen = brain.start_message(aid, wake)
        run.seen_seq = int(seen)
        system = brain.system_prompt(aid)
        tools = brain.tool_schemas(aid)
        messages: list[dict[str, Any]] = [system_message(system), user_message(text)]
        max_rounds = self.config.rounds_for(self.kind)
        max_tokens = self._max_tokens()
        nudged = False

        for round_idx in range(max_rounds):
            run.rounds = round_idx + 1
            last_round = round_idx + 1 >= max_rounds
            self._set_status("thinking")
            started = _now()
            resp = await self.llm.complete(aid, list(messages), tools, max_tokens=max_tokens)
            if self._stale(run):  # superseded while we waited (e.g. a model that ignored the cancel)
                run.end_reason = "stale"
                self._trace("discard", gen=run.gen, reason="stale after llm call")
                return
            self._account_llm(run, round_idx, resp, _now() - started, messages)

            if not resp.tool_calls:
                if last_round:
                    run.end_reason = "rounds" if self._has_actionable_pending() else "no_tool"
                    return
                parts: list[str] = []
                if not nudged:
                    nudge = self._brain_call("no_tool_nudge", brain.no_tool_nudge, aid, resp.content or "", round_idx)
                    if nudge:
                        nudged = True
                        parts.append(nudge)
                        self._trace("nudge", gen=run.gen, round=round_idx)
                if self._has_actionable_pending():
                    injected = self._inject(run)
                    if injected:
                        parts.append(injected)
                if not parts:
                    run.end_reason = "no_tool"
                    return
                reply = assistant_text_message(resp)
                if reply is not None:
                    messages.append(reply)
                messages.append(user_message("\n\n".join(parts)))
                continue

            ensure_call_ids(resp, prefix=f"call_{run.gen}_{round_idx}")
            messages.append(assistant_tool_message(resp))
            self._set_status("acting")
            ends_run = False
            rebuild = False
            for call in resp.tool_calls:
                if self._stale(run):  # never touch the world on behalf of a superseded run
                    run.end_reason = "stale"
                    return
                outcome = self._run_tool(run, call)
                messages.append(tool_message(call.id, outcome.text))
                ends_run = ends_run or outcome.ends_run
                rebuild = rebuild or outcome.rebuild
            if ends_run:
                run.end_reason = "ends_run"
                return
            if last_round:
                run.end_reason = "rounds"
                return
            if rebuild:
                extra = [e for e in self._pending if e.priority != "ambient"]
                run.absorb(extra)
                self._pending = [e for e in self._pending if e.priority == "ambient"]
                self._ambient_count = len(self._pending)
                self._normal_due = None
                again = WakeContext(
                    agent_id=aid, kind="start", gen_id=run.gen, reasons=list(run.consumed), ambient=[]
                )
                text, seen = brain.start_message(aid, again)
                run.seen_seq = int(seen)
                messages = [system_message(system), user_message(text)]
                self._trace("rebuild", gen=run.gen, round=round_idx, seen_seq=run.seen_seq)
                continue
            injected = self._inject(run)
            if injected:
                messages.append(user_message(injected))
        run.end_reason = run.end_reason or "rounds"

    def _has_actionable_pending(self) -> bool:
        return any(e.priority != "ambient" for e in self._pending)

    def _inject(self, run: _Run) -> str | None:
        """Hand everything pending to the Brain; returns the user message to append, if any."""
        reasons = [e for e in self._pending if e.priority != "ambient"]
        ambient = [e for e in self._pending if e.priority == "ambient"]
        self._pending = []
        self._ambient_count = 0
        self._normal_due = None
        run.absorb(reasons)
        ctx = WakeContext(
            agent_id=self.agent_id,
            kind="inject",
            gen_id=run.gen,
            reasons=reasons,
            ambient=ambient,
            after_seq=run.seen_seq,
        )
        result = self._brain_call("inject_message", self.brain.inject_message, self.agent_id, ctx)
        if not result:
            return None
        text, new_seen = result
        if not text:
            return None
        run.seen_seq = max(run.seen_seq, int(new_seen))
        self.stats["injections"] += 1
        self._trace(
            "inject", gen=run.gen, reasons=[_brief(e) for e in reasons], ambient=len(ambient), seen_seq=run.seen_seq
        )
        return text

    def _brain_call(self, what: str, fn: Any, *args: Any) -> Any:
        """Call a non-essential Brain hook; a bug in it is traced and ignored instead of killing the run."""
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001
            logger.exception("brain.%s failed for %s", what, self.agent_id)
            self._trace("error", message=f"brain.{what} failed: {type(exc).__name__}: {exc}")
            return None

    def _run_tool(self, run: _Run, call: Any) -> ToolOutcome:
        name = str(call.name or "")
        args, error = parse_call(call)
        self._count("tool_calls")
        if error:
            outcome = ToolOutcome(text=args_error_text(name, error))
        else:
            try:
                outcome = self.brain.execute_tool(self.agent_id, name, args, gen_id=run.gen, seen_seq=run.seen_seq)
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
        self, run: _Run, round_idx: int, resp: LLMResponse, elapsed: float, messages: list[dict[str, Any]]
    ) -> None:
        if self.usage is not None:
            try:
                self.usage.record_llm(self.agent_id, resp)
            except Exception:  # noqa: BLE001 - accounting must never kill a run
                logger.exception("usage.record_llm failed for %s", self.agent_id)
        thought = (resp.thought or "").strip()
        if thought:
            self._count("thinking_rounds")
        fields: dict[str, Any] = {
            "gen": run.gen,
            "round": round_idx,
            "provider": resp.provider_id,
            "model": resp.model_id,
            "prompt_tokens": resp.prompt_tokens,
            "completion_tokens": resp.completion_tokens,
            "finish_reason": resp.finish_reason,
            "tool_calls": [call.name for call in resp.tool_calls],
            "elapsed_ms": int(elapsed * 1000),
        }
        if self.config.trace_messages:
            fields["messages"] = list(messages)  # a snapshot: the conversation keeps growing
        self._trace("llm_call", **fields)
        if thought:
            self._trace("thought", gen=run.gen, round=round_idx, text=thought)
        if (resp.content or "").strip():
            # Without a ``speak`` call the plain text is only an inner monologue (D5): log it, nothing else.
            self._trace("content", gen=run.gen, round=round_idx, text=resp.content)

    # ================================================================ plumbing ==
    def _set_status(self, state: AgentState) -> None:
        if state == self.status:
            return
        self.status = state
        self._publish("agent.status", id=self.agent_id, state=state)

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
