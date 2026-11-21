"""``LegacyBrain``: the v0.13.1 player brain, kept exactly as it was (v0.14 H49, H58; contract §13.5).

v0.14 gives every seat a ``BotBrain`` (``app.agents.mind``) built from the bot's own layered memory.  Two things still need the
old one:

* a **v5 game** (saved before bots existed) that is continued: every seat plays on with the persona and the memory text it
  was saved with, and the prompts must read as they did when the game was played (H58);
* the **benchmark baseline**: "the v0.13 bot" is the control every v0.14 bot is compared with (H49).

So this brain is :class:`~app.agents.brain.ArenaBrain` itself, byte for byte: the same system prompt (``prompts/participant.md``
for a player), the same tools (the reference documents included), the same opening message, increments and nudges.  The
moderator is still served by ``ArenaBrain`` (it is not a bot, H46); a ``LegacyBrain`` can also be asked for the moderator's
prompts (it is the same class), but nothing in v0.14 does.

``tests/v12/agents/test_legacy.py`` locks the behaviour against a snapshot taken from the unchanged v0.13.1 code
(``legacy_baseline.json``).  The v0.14 additions - the composer's prompt, the ``jot`` / ``read_contract`` tools, the format
gate - live in ``mind.py`` / ``composer.py`` and are reached through ``profile="bot"`` options of the shared helpers; the
defaults of those helpers are the v0.13.1 behaviour this class relies on, so changing a default is a change of the baseline.
"""

from __future__ import annotations

from app.agents.brain import ArenaBrain

#: What the baseline is called in reports (``bench``) and in the logs.
LEGACY_LABEL = "v0.13.1"


class LegacyBrain(ArenaBrain):
    """The v0.13.1 brain: ``ArenaBrain(world, spec, cfg, *, docs_dir=None, personas=None, memories=None, history_chars=None)``.

    ``personas`` / ``memories`` are seat id -> the persona / the long-term memory text of the roster as it was when the game
    started (for a v5 game: the strings in the save file)."""

    label = LEGACY_LABEL


__all__ = ["LEGACY_LABEL", "LegacyBrain"]
