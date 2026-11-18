"""Soft pause: let the work in flight finish, then hold every agent (v0.13.1, docs/v0.13.1-plan.md G1-G9).

Mixed into :class:`~app.game.orchestrator.GameOrchestrator`.  Contract: docs/v0.12-contracts.md section 12.2.

Scaffolding: filled in by the ``pause`` stream.
"""

from __future__ import annotations


class SoftPauseRuntimeMixin:
    """The soft-pause state of the orchestrator (a volatile flag, never saved: a restart comes back hard-paused)."""
