"""The seat layouts the question bank is dealt on.  Each is a permutation of the pool of ``werewolf_12p`` (3 wolves + white wolf, 4 villagers, seer, witch,
hunter, guard); the names say who the seat under test is.  Varying the layout keeps one question from being another one with the numbers changed."""

from __future__ import annotations

from bench.cases.common import Layout, layout

#: key -> layout.  ``v*``: the bot is a villager;  ``witch`` / ``seer`` / ``guard``: the bot holds that role.
LAYOUTS: dict[str, Layout] = {
    # villagers 1 3 6 9
    "v1": layout(bot=3, seer=7, witch=11, hunter=8, guard=4, ww=9, wolves=[2, 10, 12]),
    # villagers 3 5 6 10
    "v2": layout(bot=6, seer=9, witch=2, hunter=12, guard=7, ww=11, wolves=[1, 4, 8]),
    # villagers 1 4 7 11
    "v3": layout(bot=4, seer=8, witch=10, hunter=3, guard=12, ww=9, wolves=[2, 6, 5]),
    # villagers 4 7 10 12
    "v4": layout(bot=10, seer=8, witch=1, hunter=6, guard=3, ww=11, wolves=[2, 5, 9]),
    # villagers 3 5 6 11
    "v5": layout(bot=5, seer=10, witch=7, hunter=2, guard=9, ww=12, wolves=[1, 4, 8]),
    # villagers 2 6 9 12
    "v6": layout(bot=9, seer=11, witch=4, hunter=1, guard=8, ww=10, wolves=[3, 5, 7]),
    # villagers 1 4 8 12
    "v7": layout(bot=8, seer=6, witch=9, hunter=11, guard=3, ww=10, wolves=[2, 5, 7]),
    # villagers 2 3 7 9
    "v8": layout(bot=7, seer=12, witch=5, hunter=1, guard=10, ww=11, wolves=[4, 6, 8]),
    # the bot is the witch
    "witch": layout(bot=11, seer=7, witch=11, hunter=8, guard=4, ww=9, wolves=[2, 10, 12]),
    # the bot is the seer
    "seer": layout(bot=7, seer=7, witch=11, hunter=8, guard=4, ww=9, wolves=[2, 10, 12]),
    # the bot is the guard
    "guard": layout(bot=4, seer=7, witch=11, hunter=8, guard=4, ww=9, wolves=[6, 10, 12]),
}

__all__ = ["LAYOUTS"]
