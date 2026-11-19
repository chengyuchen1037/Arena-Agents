"""Supervisor: starts / stops the agents and connects them with the world (contracts §3.3 and §10.2).

* ``start(initial_wakes)`` arms every agent (their conversations and sleep timers live in the shared
  :class:`~app.runtime.conversation.ConversationBook` and are kept), delivers the initial wake-ups, asks the
  world to ``redeliver()`` open work, pumps once and starts the tick loop, the watchdog and the metrics
  publisher.  Agents are plain event-driven objects: there is no task per agent, only a task per *run*.
* The *pump* empties ``world.drain_outbox()`` (envelopes go to the target agent, the ``ENGINE_ID`` target
  goes to ``on_engine``) and ``world.drain_events()`` (to the event sink).  It is triggered by the world's
  notifier through ``loop.call_soon`` (de-duplicated: any number of changes in one loop iteration cost one
  pump, and everything delivered to an agent in it is one activation).
* The *tick loop* polls ``world.tick()`` (game-clock timers: the world decides what expired; it is a no-op
  while paused) and the agents' sleep timers: an agent whose ``sleep(seconds)`` ran out (game clock, frozen
  while paused) is woken with a ``timer.elapsed`` notice.
* The *watchdog* (real time) notices a game that is ``running`` but silent: every agent quiet, no world timer,
  no moderator timer and no ledger progress for ``watchdog_s`` -> an urgent ``notice/watchdog`` to the
  moderator (and only to the moderator).  After ``max_watchdog_wakes`` wake-ups without progress
  ``on_stall(reason)`` is called once; when the ledger advances again, ``on_stall(None)``.  The supervisor
  never pauses the game itself: the orchestrator shows a warning and the user decides.  It stays quiet while a
  soft pause is on (the gate is closed, v0.13.1) and while the world waits for a person who holds a floor that has
  no deadline (``world.untimed_floor_open()``).
* ``stop()`` stops polling and pumping first, then stops every agent (cancelling workers, publishing
  ``sleeping``) and awaits every task: nothing is left running.

An exception in one agent, in ``on_engine`` or in ``world.tick()`` is logged and isolated; the others keep
running.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from app.runtime.agent import Agent
from app.runtime.conversation import ConversationBook
from app.runtime.ports import EventSink, TraceSink
from app.runtime.soft_gate import SoftGate
from app.world.types import ENGINE_ID, MODERATOR_ID, Envelope, UiEvent

logger = logging.getLogger(__name__)


@dataclass
class SupervisorConfig:
    tick_interval: float = 0.25  # seconds between ``world.tick()`` / sleep-timer polls
    watchdog_s: float = 20.0  # real seconds of silence before the moderator is woken
    max_watchdog_wakes: int = 3  # fruitless wake-ups before ``on_stall``
    watchdog_poll_s: float | None = None  # None -> min(1.0, watchdog_s / 4)
    metrics_interval_s: float = 1.0  # ``agent.metrics`` is published at most this often per agent
    #: When set, applied to every agent's ``AgentConfig`` at ``start()`` (share of the context window that
    #: triggers the "please compact" reminder / the emergency trim).
    compact_at: float | None = None
    trim_at: float | None = None


class _Started:
    """``Supervisor.start`` is synchronous; awaiting its result is allowed and does nothing."""

    __slots__ = ()

    def __await__(self) -> Iterator[Any]:
        return iter(())


_STARTED = _Started()


class Supervisor:
    def __init__(
        self,
        world: Any,
        agents: Mapping[str, Agent] | Iterable[Agent],
        sink: EventSink,
        on_engine: Callable[[Envelope], None] | None = None,
        config: SupervisorConfig | None = None,
        *,
        on_stall: Callable[[str | None], None] | None = None,
        usage: Any = None,
        trace: TraceSink | None = None,
        book: ConversationBook | None = None,
        soft_gate: SoftGate | None = None,
    ) -> None:
        self.world = world
        self.agents: dict[str, Agent] = (
            dict(agents) if isinstance(agents, Mapping) else {a.agent_id: a for a in agents}
        )
        self.sink = sink
        self.on_engine = on_engine
        self.on_stall = on_stall
        self.usage = usage  # anything with ``metrics_for(agent_id) -> dict``; None -> each agent's own sink
        self.trace = trace
        self.config = config or SupervisorConfig()
        #: The conversations of this game.  The orchestrator keeps one book across pause -> resume (so the
        #: conversations and sleep timers survive) and resets it for a new game / import / restore.
        if book is None:
            first = next(iter(self.agents.values()), None)
            book = first.book if first is not None else ConversationBook()  # (an empty book is falsy)
        self.book = book
        for agent in self.agents.values():
            agent.book = book
        #: The soft-pause gate of this game (v0.13.1): every agent waits at it while it is closed, and the watchdog
        #: stays quiet (a person is thinking, the agents are held on purpose).  ``None`` = no soft pause.
        self.soft_gate = soft_gate
        if soft_gate is not None:
            for agent in self.agents.values():
                agent.soft_gate = soft_gate

        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._stopping = False
        self._stop_done: asyncio.Event | None = None
        self._pump_scheduled = False
        self._loop_tasks: list[asyncio.Task[None]] = []
        self._last_metrics: dict[str, dict[str, Any]] = {}
        self._tick_error_logged = False
        self.stalled = False  # ``on_stall(reason)`` was called and no progress has been seen since

    # ================================================================ lifecycle ==
    @property
    def running(self) -> bool:
        return self._started and not self._stopping

    def start(self, initial_wakes: Mapping[str, list[Envelope]] | None = None) -> _Started:
        """Arm every agent and start the background loops (must be called inside the running loop)."""
        if self._started or self._stop_done is not None:
            raise RuntimeError("supervisor is already running")
        self._loop = asyncio.get_running_loop()
        self._started = True
        self._stopping = False
        self.stalled = False
        self.world.set_notifier(self._notify)
        for agent in self.agents.values():
            self._apply_config(agent)
            agent.activate()
        for agent_id, envelopes in (initial_wakes or {}).items():
            agent = self.agents.get(agent_id)
            if agent is None:
                logger.warning("initial wake for unknown agent %r ignored", agent_id)
                continue
            for envelope in envelopes:
                agent.deliver(envelope)
        try:
            self.world.redeliver()
        except Exception:  # noqa: BLE001 - a broken redelivery must not prevent the game from starting
            logger.exception("world.redeliver() failed")
        self.pump()
        self._loop_tasks = [
            self._loop.create_task(self._tick_loop(), name="supervisor:tick"),
            self._loop.create_task(self._watchdog_loop(), name="supervisor:watchdog"),
            self._loop.create_task(self._metrics_loop(), name="supervisor:metrics"),
        ]
        return _STARTED

    def _apply_config(self, agent: Agent) -> None:
        if self.config.compact_at is not None:
            agent.config.compact_at = self.config.compact_at
        if self.config.trim_at is not None:
            agent.config.trim_at = self.config.trim_at

    async def stop(self) -> None:
        """Stop polling and pumping, stop every agent and wait until every task has ended."""
        if self._stop_done is not None:
            await self._stop_done.wait()  # a stop is already in progress
            return
        if not self._started:
            return
        done = asyncio.Event()
        self._stop_done = done
        try:
            await self._do_stop()
        finally:
            self._started = False
            self._stopping = False
            self._stop_done = None
            done.set()

    async def _do_stop(self) -> None:
        # While stopping nothing is pumped (``_notify`` ignores the world); the last pump below flushes.
        self._stopping = True
        # Agents first, and synchronously: cancel workers and scheduled activations, drop what is pending,
        # publish ``agent.status = sleeping`` (the conversations stay).  Nothing can start a run any more, not
        # even in the loop turns the awaits below give away.
        stopping = [agent.stop() for agent in self.agents.values()]
        loop_tasks, self._loop_tasks = self._loop_tasks, []
        for task in loop_tasks:
            task.cancel()
        await asyncio.gather(*loop_tasks, return_exceptions=True)
        await asyncio.gather(*stopping, return_exceptions=True)  # wait (bounded) for the cancelled workers
        self._publish_metrics()
        self.pump(final=True)
        # Let go of the trace file.  A paused or replaced game must not keep its log open: on Windows an open file
        # blocks moving or deleting the logs folder (deleting the game from the history, replacing the data
        # directory with an archive).  Closing is harmless - a later write simply reopens the file.
        close = getattr(self.trace, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - never let logging hygiene break a stop
                logger.debug("closing the trace log failed", exc_info=True)

    def flush(self) -> None:
        """Publish pending world events / ENGINE notices right now (agent deliveries are dropped once stopped).

        Not normally needed: after :meth:`stop` the notifier stays registered, so what the world
        produces later (e.g. the ``game.status`` event of ``set_running(False)``) is still published
        on the next loop turn.
        """
        self.pump(final=not self._started)

    def deliver(self, agent_id: str, envelope: Envelope) -> bool:
        """Hand an envelope to one agent (orchestrator wake-ups); False for an unknown agent."""
        agent = self.agents.get(agent_id)
        if agent is None:
            return False
        agent.deliver(envelope)
        return True

    # ================================================================ the pump ==
    def _notify(self) -> None:
        """World notifier: schedule one pump (de-duplicated).

        It stays registered after :meth:`stop`: late events and ENGINE notices are still routed, while
        deliveries to the (stopped) agents are dropped.
        """
        if self._pump_scheduled or self._stopping or self._loop is None:
            return
        self._pump_scheduled = True
        try:
            self._loop.call_soon(self._scheduled_pump)
        except RuntimeError:  # the loop is closed: nobody is listening any more
            self._pump_scheduled = False

    def _scheduled_pump(self) -> None:
        self.pump(final=not self._started)

    def pump(self, final: bool = False) -> None:
        """Move everything the world queued to the agents / the engine hook / the event sink.

        With ``final=True`` (after the agents stopped) deliveries to agents are dropped: a resumed
        game gets its open work back through ``world.redeliver()``.
        """
        self._pump_scheduled = False
        try:
            deliveries = self.world.drain_outbox()
        except Exception:  # noqa: BLE001
            logger.exception("world.drain_outbox() failed")
            deliveries = []
        for delivery in deliveries:
            if delivery.target == ENGINE_ID:
                self._to_engine(delivery.envelope)
                continue
            if final:
                continue
            agent = self.agents.get(delivery.target)
            if agent is None:
                logger.debug("delivery for unknown agent %r dropped", delivery.target)
                continue
            try:
                agent.deliver(delivery.envelope)
            except Exception:  # noqa: BLE001 - one broken agent must not starve the others
                logger.exception("could not deliver to %s", delivery.target)
        try:
            events = self.world.drain_events()
        except Exception:  # noqa: BLE001
            logger.exception("world.drain_events() failed")
            events = []
        for event in events:
            self._publish_event(event)

    def _to_engine(self, envelope: Envelope) -> None:
        if self.on_engine is None:
            return
        try:
            self.on_engine(envelope)
        except Exception:  # noqa: BLE001 - the orchestrator's hook must not break the pump
            logger.exception("on_engine failed for %s", envelope.payload.get("event"))

    def _publish_event(self, event: UiEvent) -> None:
        try:
            self.sink.publish(event)
        except Exception:  # noqa: BLE001
            logger.exception("event sink failed for %s", event.type)

    # ================================================================ polling loops ==
    async def _tick_loop(self) -> None:
        interval = max(0.005, self.config.tick_interval)
        while True:
            await asyncio.sleep(interval)
            try:
                self.world.tick()
            except Exception:  # noqa: BLE001 - a failing timer pass must not stop the next one
                if not self._tick_error_logged:
                    self._tick_error_logged = True
                    logger.exception("world.tick() failed")
                    self._publish_event(UiEvent(type="error", data={"message": "世界的计时器处理出错，已记录日志。"}))
            self.pump()
            self.poll_timers()

    def poll_timers(self) -> int:
        """Wake every agent whose ``sleep(seconds)`` has run out (game clock); returns how many woke.

        Runs after the pump on purpose: an agent the world just woke has its timer voided already."""
        try:
            now = float(self.world.clock.now())
        except Exception:  # noqa: BLE001 - no usable clock: no timers
            return 0
        woken = 0
        for agent in self.agents.values():
            try:
                if agent.timer_due(now) and agent.fire_timer(now):
                    woken += 1
            except Exception:  # noqa: BLE001 - one agent's timer must not break the others
                logger.exception("timer of %s failed", agent.agent_id)
        return woken

    def _system_quiet(self) -> bool:
        if not all(agent.is_quiet() for agent in self.agents.values()):
            return False
        moderator = self.agents.get(MODERATOR_ID)
        if moderator is not None and moderator.has_timer:
            return False  # the moderator chose to wait: that is its plan, not a stall
        try:
            if self._waiting_on_untimed_floor():
                return False  # a person holds a floor that has no deadline: the game waits for them, however long
            return self.world.next_deadline() is None
        except Exception:  # noqa: BLE001
            return False

    def _waiting_on_untimed_floor(self) -> bool:
        """The world is waiting for a person who holds a floor without a deadline (v0.13.1, G13).  Such a floor has
        no timer, so ``next_deadline()`` cannot tell: without this the moderator would be woken every
        ``watchdog_s`` (and finally a "the game is stuck" banner shown) while somebody is simply thinking."""
        asking = getattr(self.world, "untimed_floor_open", None)
        return bool(asking()) if callable(asking) else False

    def _call_on_stall(self, reason: str | None) -> None:
        if self.on_stall is None:
            return
        try:
            self.on_stall(reason)
        except Exception:  # noqa: BLE001 - the orchestrator's hook must not break the watchdog
            logger.exception("on_stall failed")

    async def _watchdog_loop(self) -> None:
        cfg = self.config
        poll = cfg.watchdog_poll_s or max(0.01, min(1.0, cfg.watchdog_s / 4))
        loop = asyncio.get_running_loop()
        last_seq = self.world.seq
        last_progress = loop.time()
        quiet_since: float | None = None
        wakes = 0
        while True:
            await asyncio.sleep(poll)
            try:
                now = loop.time()
                seq = self.world.seq
                if seq != last_seq:  # any ledger progress resets everything
                    last_seq, last_progress, quiet_since, wakes = seq, now, None, 0
                    if self.stalled:
                        self.stalled = False
                        self._trace("watchdog", MODERATOR_ID, action="recovered", seq=seq)
                        self._call_on_stall(None)
                    continue
                if self.soft_gate is not None and self.soft_gate.closed:
                    # A soft pause (v0.13.1, G8): the agents are held on purpose and the clock is frozen.  Nothing is
                    # counted - no quiet time, no fruitless wake-up, no banner - and the time spent here is not idle time.
                    # What was counted before the pause is forgotten (the silence is measured afresh afterwards), and a
                    # "the game is stuck" banner that was up when the pause began comes down: nothing is stuck now.
                    quiet_since = None
                    last_progress = now
                    wakes = 0
                    if self.stalled:
                        self.stalled = False
                        self._trace("watchdog", MODERATOR_ID, action="soft_pause", seq=seq)
                        self._call_on_stall(None)
                    continue
                if not self.world.running or not self._system_quiet():
                    quiet_since = None
                    if not self.world.running:
                        last_progress = now
                    continue
                if quiet_since is None:
                    quiet_since = now
                    continue
                if now - quiet_since < cfg.watchdog_s:
                    continue
                quiet_since = now
                idle_s = now - last_progress
                if wakes >= cfg.max_watchdog_wakes and not self.stalled:
                    # The last wake-up did not help either: warn once (the user decides), keep trying.
                    self.stalled = True
                    reason = f"对局静止：已连续 {wakes} 次唤醒主持人，账本仍无进展（约 {idle_s:.0f} 秒）。"
                    self._trace("watchdog", MODERATOR_ID, action="stall", wakes=wakes, idle_s=round(idle_s, 1), seq=seq)
                    self._call_on_stall(reason)
                wakes += 1
                self._trace("watchdog", MODERATOR_ID, action="wake", wakes=wakes, idle_s=round(idle_s, 1), seq=seq)
                self.deliver(
                    MODERATOR_ID,
                    Envelope(
                        kind="notice",
                        priority="urgent",
                        sender=ENGINE_ID,
                        payload={
                            "event": "watchdog",
                            "idle_s": round(idle_s, 1),
                            "attempt": wakes,
                            "max_attempts": cfg.max_watchdog_wakes,
                            "seq": seq,
                        },
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the watchdog itself must survive a failing hook
                logger.exception("watchdog iteration failed")

    async def _metrics_loop(self) -> None:
        interval = max(0.01, self.config.metrics_interval_s)
        while True:
            await asyncio.sleep(interval)
            self._publish_metrics()

    def _publish_metrics(self) -> None:
        """Publish ``agent.metrics`` for every agent whose numbers changed since the last time."""
        for agent_id, agent in self.agents.items():
            source = self.usage if self.usage is not None else agent.usage
            reader = getattr(source, "metrics_for", None)
            if reader is None:
                continue
            try:
                metrics = reader(agent_id)
            except Exception:  # noqa: BLE001
                logger.exception("metrics_for(%s) failed", agent_id)
                continue
            last = self._last_metrics.get(agent_id)
            if metrics == last:
                continue
            self._last_metrics[agent_id] = metrics
            if last is None and not any(value for key, value in metrics.items() if key != "id"):
                continue  # an agent that has not done anything yet needs no event
            self._publish_event(UiEvent(type="agent.metrics", data=dict(metrics)))

    def _trace(self, event: str, agent_id: str, **fields: Any) -> None:
        if self.trace is None:
            return
        try:
            self.trace.write(event, agent_id, **fields)
        except Exception:  # noqa: BLE001
            logger.debug("trace write failed", exc_info=True)
