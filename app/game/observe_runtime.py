"""What the user can look at afterwards: the call / error log and the thinking record (v0.13.1, G21-G26, G32-G36).

Mixed into :class:`~app.game.orchestrator.GameOrchestrator`.  Contract: docs/v0.12-contracts.md sections 12.3, 12.4.

Scaffolding: filled in by the ``observe`` stream.
"""

from __future__ import annotations


class ObserveRuntimeMixin:
    """The call log of the current game (in memory; rebuilt from the game's trace log when a game is loaded)."""
