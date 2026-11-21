"""The v0.14 bot: shared, dependency-free types (docs/v0.12-contracts.md §13).

``app.bots.types`` is pure data (no I/O, no imports from the rest of ``app``): the storage layer builds these objects
from a bot's files, the agent layer (Composer, BotBrain, learning tools) reads them, the learning jobs and the
orchestrator pass them around.  Like ``app.world`` / ``app.runtime`` / ``app.agents`` this package is engine-neutral:
no game vocabulary, no fixed seat ids.
"""
