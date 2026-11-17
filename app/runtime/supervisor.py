"""Supervisor: owns the actor tasks and connects them with the world (contracts §3.3).

* ``start(initial_wakes)`` spawns one Actor task per agent, delivers the initial wake-ups, asks the
  world to ``redeliver()`` open work, pumps once and starts the timer poll, the watchdog and the
  metrics publisher.
* The *pump* empties ``world.drain_outbox()`` (envelopes go to the target agent's mailbox, the
  ``ENGINE_ID`` target goes to ``on_engine``) and ``world.drain_events()`` (to the event sink).  It is
  triggered by the world's notifier through ``loop.call_soon`` (de-duplicated: any number of changes
  in one loop iteration cost one pump).
* ``world.tick()`` is polled every ``tick_interval`` real seconds (timers are game-clock based, the
  world decides what has expired; it is a no-op while paused).
* The *watchdog* (real time) notices a game that is ``running`` but silent: every agent idle, no
  timer pending, nothing in any mailbox and no ledger progress for ``watchdog_s`` -> an urgent
  ``notice/watchdog`` to the moderator; after ``max_watchdog_wakes`` wake-ups without progress
  ``on_stall(reason)`` is called (the orchestrator pauses the game).
* ``stop()`` stops polling and pumping first, then stops every agent (cancelling workers) and
  awaits all tasks: nothing is left running.

An exception in one agent, in ``on_engine`` or in ``world.tick()`` is logged and isolated; the
others keep running.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from app.runtime.agent import Agent
from app.runtime.ports import EventSink, TraceSink
from app.world.types import ENGINE_ID, MODERATOR_ID, Envelope, UiEvent

logger = logging.getLogger(__name__)


@dataclass
class SupervisorConfig:
    tick_interval: float = 0.25  # seconds between ``world.tick()`` polls
    watchdog_s: float = 20.0  # real seconds of silence before the moderator is woken
    max_watchdog_wakes: int = 3  # wake-ups without progress before ``on_stall``
    watchdog_poll_s: float | None = None  # None -> min(1.0, watchdog_s / 4)
    metrics_interval_s: float = 1.0  # ``agent.metrics`` is published at most this often per agent
    stop_grace_s: float = 5.0  # how long ``stop`` waits for an actor task to finish on its own
    max_actor_restarts: int = 3  # respawns of an actor that crashed (per agent)


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
        on_stall: Callable[[str], None] | None = None,
        usage: Any = None,
        trace: TraceSink | None = None,
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

        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._stopping = False
        self._stop_done: asyncio.Event | None = None
        self._pump_scheduled = False
        self._actor_tasks: dict[str, asyncio.Task[None]] = {}
        self._loop_tasks: list[asyncio.Task[None]] = []
        self._aux_tasks: set[asyncio.Task[None]] = set()
        self._restarts: dict[str, int] = {}
        self._last_metrics: dict[str, dict[str, Any]] = {}
        self._tick_error_logged = False
        self.stalled = False  # ``on_stall`` was called and no progress has been seen since

    # ================================================================ lifecycle ==
    @property
    def running(self) -> bool:
        return self._started and not self._stopping

    def start(self, initial_wakes: Mapping[str, list[Envelope]] | None = None) -> _Started:
        """Start every actor and the background loops (must be called inside the running loop)."""
        if self._started or self._stop_done is not None:
            raise RuntimeError("supervisor is already running")
        self._loop = asyncio.get_running_loop()
        self._started = True
        self._stopping = False
        self.stalled = False
        self._restarts.clear()
        self.world.set_notifier(self._notify)
        for agent in self.agents.values():
            self._spawn_actor(agent)
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
        loop_tasks, self._loop_tasks = self._loop_tasks, []
        for task in loop_tasks:
            task.cancel()
        await asyncio.gather(*loop_tasks, return_exceptions=True)
        for task in list(self._aux_tasks):
            task.cancel()
        await asyncio.gather(*self._aux_tasks, return_exceptions=True)
        self._aux_tasks.clear()
        # Agents: cancel workers, drop buffers, publish ``agent.status = stopped``.
        await asyncio.gather(*(agent.stop() for agent in self.agents.values()), return_exceptions=True)
        actor_tasks, self._actor_tasks = list(self._actor_tasks.values()), {}
        if actor_tasks:
            _, pending = await asyncio.wait(actor_tasks, timeout=self.config.stop_grace_s)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._publish_metrics()
        self.pump(final=True)

    def flush(self) -> None:
        """Publish pending world events / ENGINE notices right now (agent deliveries are dropped once stopped).

        Not normally needed: after :meth:`stop` the notifier stays registered, so what the world
        produces later (e.g. the ``game.status`` event of ``set_running(False)``) is still published
        on the next loop turn.
        """
        self.pump(final=not self._started)

    def deliver(self, agent_id: str, envelope: Envelope) -> bool:
        """Put an envelope into one agent's mailbox (orchestrator wake-ups); False for an unknown agent."""
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
            actor = self._actor_tasks.get(delivery.target)
            if actor is not None and actor.done():
                continue  # its inbox loop is gone (crashed for good): do not pile envelopes up
            try:
                agent.deliver(delivery.envelope)
            except Exception:  # noqa: BLE001 - one broken mailbox must not starve the others
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

    # ================================================================ actors ==
    def _spawn_actor(self, agent: Agent) -> None:
        assert self._loop is not None
        agent.activate()
        task = self._loop.create_task(agent.run(), name=f"actor:{agent.agent_id}")
        self._actor_tasks[agent.agent_id] = task
        task.add_done_callback(lambda finished, a=agent: self._on_actor_done(a, finished))

    def _on_actor_done(self, agent: Agent, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()  # always retrieve it
        if exc is None or self._stopping or self._actor_tasks.get(agent.agent_id) is not task:
            return
        logger.error("actor of %s crashed", agent.agent_id, exc_info=exc)
        restarts = self._restarts.get(agent.agent_id, 0)
        if restarts >= self.config.max_actor_restarts or self._loop is None:
            self._publish_event(
                UiEvent(type="error", data={"message": f"{agent.agent_id} 的收件循环反复崩溃，已停止该参与者。", "agent_id": agent.agent_id})
            )
            return
        self._restarts[agent.agent_id] = restarts + 1
        helper = self._loop.create_task(self._restart_actor(agent), name=f"restart:{agent.agent_id}")
        self._aux_tasks.add(helper)
        helper.add_done_callback(self._aux_tasks.discard)

    async def _restart_actor(self, agent: Agent) -> None:
        """Rebuild a crashed actor from the world: drop its volatile state, respawn it, redeliver open work."""
        await agent.stop()
        if self._stopping:
            return
        self._spawn_actor(agent)
        try:
            self.world.redeliver()
        except Exception:  # noqa: BLE001
            logger.exception("world.redeliver() failed after an actor restart")
        self.pump()

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

    def _system_quiet(self) -> bool:
        if not all(agent.is_quiet() for agent in self.agents.values()):
            return False
        try:
            return self.world.next_deadline() is None
        except Exception:  # noqa: BLE001
            return False

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
                    self.stalled = False
                    continue
                if not self.world.running or self.stalled or not self._system_quiet():
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
                if wakes >= cfg.max_watchdog_wakes:
                    self.stalled = True
                    reason = f"对局静止：已连续 {wakes} 次唤醒主持人，账本仍无进展（约 {idle_s:.0f} 秒）。"
                    self._trace("watchdog", MODERATOR_ID, action="stall", wakes=wakes, idle_s=round(idle_s, 1), seq=seq)
                    if self.on_stall is not None:
                        self.on_stall(reason)
                    continue
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
