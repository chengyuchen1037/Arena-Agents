"""Frozen v0.12 contract: what the async runtime and the agents may call on the world.

``app.world.world.World`` implements :class:`WorldPort`; the runtime tests use a small fake that
implements the same protocol, so the runtime can be built and tested without the real world.

Every method is **synchronous and non-blocking**: the world is single-threaded state living on the
event loop, so a call is atomic with respect to every other coroutine (there is no ``await`` inside
the commit gate).  Side effects of a call are queued and must be collected by the caller right after
it returns: ``drain_outbox()`` (mailbox deliveries) and ``drain_events()`` (spectator events); the
runtime registers :meth:`set_notifier` so it is told whenever there is something to drain.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from app.world.spec import ScenarioSpec
from app.world.types import AgentMemory, Delivery, Intent, LedgerEntry, Receipt, UiEvent


class Clock(Protocol):
    """Game clock in seconds.  It does not advance while the game is paused (D18)."""

    def now(self) -> float: ...


@runtime_checkable
class WorldPort(Protocol):
    spec: ScenarioSpec
    clock: Clock

    # -- state -----------------------------------------------------------------
    @property
    def seq(self) -> int:
        """Seq of the last ledger entry (0 for an empty ledger)."""

    @property
    def running(self) -> bool:
        """The commit gate accepts intents only while running (``GAME_NOT_RUNNING`` otherwise)."""

    def set_running(self, running: bool, reason: str = "") -> None:
        """Open / close the gate (pause / resume / finish).  Writes a ``game.status`` entry when the
        status changes.  ``set_running(True)`` after ``finished`` continues the game (D8)."""

    # -- the only way to change the world --------------------------------------
    def submit(self, intent: Intent) -> Receipt:
        """The commit gate.  Never raises for bad input: returns a rejected :class:`Receipt`."""

    def tick(self) -> None:
        """Process every timer that expired at ``clock.now()`` (token / request deadlines, queue
        advancement, free-discussion advisories).  Idempotent; a no-op while not running."""

    def next_deadline(self) -> float | None:
        """Earliest pending timer in game-clock seconds, or ``None`` when no timer is pending."""

    def redeliver(self) -> None:
        """Queue deliveries again for work that is still open (active floors, open requests, open
        tickets).  Called when a game is resumed or restored."""

    # -- side-effect queues -----------------------------------------------------
    def set_notifier(self, callback: Callable[[], None] | None) -> None:
        """``callback()`` is called synchronously after any ``submit`` / ``tick`` / ``set_running``
        that queued deliveries or events."""

    def drain_outbox(self) -> list[Delivery]:
        """Mailbox deliveries produced since the last drain, in commit order."""

    def drain_events(self) -> list[UiEvent]:
        """Spectator events produced since the last drain, in commit order."""

    # -- reading ----------------------------------------------------------------
    def entries_visible(
        self, agent_id: str, *, after_seq: int = 0, limit: int | None = None
    ) -> list[LedgerEntry]:
        """Ledger entries whose ``visible_to_ids`` contain ``agent_id`` with ``seq > after_seq``,
        oldest first.  ``limit`` keeps the *newest* ``limit`` entries."""

    def board(self, agent_id: str) -> dict[str, Any]:
        """The agent's *state board* (JSON-serializable; shape in docs/v0.12-contracts.md §6)."""

    def display_name(self, agent_id: str) -> str:
        """Human name of a player / the moderator / the engine."""

    # -- private memory (notes + compaction) -------------------------------------
    def memory(self, agent_id: str) -> AgentMemory: ...

    def write_note(self, agent_id: str, title: str, content: str, mode: str = "overwrite") -> str: ...

    def read_notes_text(self, agent_id: str) -> str: ...

    def compact(self, agent_id: str, summary: str, keep_recent_count: int = 10) -> dict[str, Any]:
        """Store ``summary`` and set ``summary_upto_seq`` so that only the newest ``keep_recent_count``
        entries visible to the agent stay un-summarized."""
