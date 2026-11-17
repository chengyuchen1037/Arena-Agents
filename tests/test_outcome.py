"""Who won, from the free-text winner of ``end_game`` (E21)."""

from __future__ import annotations

import pytest

from app.game.outcome import LOST, UNKNOWN, WON, player_won, role_label
from app.world.spec import ScenarioSpec


def make_spec(**setup) -> ScenarioSpec:
    return ScenarioSpec.model_validate({
        "id": "t", "version": "1",
        "setup": {"roles": {"wolf": {"faction": "wolves", "tags": ["狼人", "存活"]}, "seer": {"faction": "good", "tags": ["预言家"]}, "plain": {"faction": "good"}}, **setup},
    })


NAMED = make_spec(faction_names={"wolves": "狼人阵营", "good": "好人阵营"})


@pytest.mark.parametrize(
    "faction,winner,expected",
    [
        ("good", "好人阵营", WON), ("wolves", "好人阵营", LOST), ("wolves", "狼人阵营", WON), ("good", "狼人阵营", LOST),
        ("good", "好人阵营获胜", WON), ("wolves", "好人阵营获胜", LOST),
        ("good", "  好人阵营  ", WON), ("good", "GOOD", WON), ("wolves", "wolves win", WON),
        ("good", "平局", UNKNOWN), ("good", "", UNKNOWN), ("good", None, UNKNOWN), ("", "好人阵营", UNKNOWN),
    ],
)
def test_the_winner_text_is_matched_against_faction_ids_and_names(faction, winner, expected):
    assert player_won(NAMED, faction, winner) is expected


def test_without_display_names_the_faction_ids_still_match_and_anything_else_is_unknown():
    spec = make_spec()
    assert player_won(spec, "good", "good") is WON and player_won(spec, "wolves", "good") is LOST
    assert player_won(spec, "good", "好人阵营") is UNKNOWN  # nothing to match the Chinese text against


def test_a_phrase_that_contains_both_factions_goes_to_the_one_with_the_longer_name():
    spec = make_spec(faction_names={"wolves": "狼人阵营", "good": "好人"})
    assert player_won(spec, "good", "狼人阵营获胜，好人惜败") is LOST  # the longer name decides
    assert player_won(spec, "wolves", "狼人阵营获胜，好人惜败") is WON


def test_a_role_is_labelled_with_its_first_tag_then_its_id():
    assert role_label(NAMED, "wolf") == "狼人" and role_label(NAMED, "seer") == "预言家"
    assert role_label(NAMED, "plain") == "plain" and role_label(NAMED, "") == "未知身份" and role_label(NAMED, "ghost") == "ghost"
