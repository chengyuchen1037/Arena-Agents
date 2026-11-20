"""v0.12 agent layer: prompts, tool schemas / execution, context building (the ``Brain``).
Game-agnostic.  See docs/v0.12-contracts.md §4.

v0.14 (§13.5, §13.6): a player is a bot with layered memory - ``composer`` (what the bot is shown, and where), ``mind``
(``BotBrain``, one per seat), ``gates`` (the output gate), ``bot_tools`` (``jot`` / ``read_contract``), ``contract`` (the official
contract) and ``learning_brain`` (initialization and review); ``legacy`` keeps the v0.13.1 player (``LegacyBrain``), ``brain``
(``ArenaBrain``) still serves the moderator."""
