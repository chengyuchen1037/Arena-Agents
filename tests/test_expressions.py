from __future__ import annotations

import pytest

from app.engine.expressions import (
    ExpressionError,
    ParticipantView,
    evaluate,
    leaders,
    render_values,
    seat_order,
    tally,
)


def _player(
    player_id: str,
    seat: str,
    name: str,
    score: int,
    group: str = "group-a",
) -> ParticipantView:
    return ParticipantView(
        id=player_id,
        seat=seat,
        name=name,
        tags=("active",),
        data={"score": score, "group": group},
    )


def test_safe_expression_evaluates_values_and_whitelisted_functions() -> None:
    names = {
        "values": [2, 4, 6],
        "mapping": {"key": "value"},
    }
    functions = {"count": len, "double": lambda value: value * 2}

    assert evaluate("count(values)", names, functions) == 3
    assert evaluate("double(values[1])", names, functions) == 8
    assert evaluate("mapping.key == 'value'", names) is True
    assert evaluate("1 + 2 * 3 == 7 and not false", names) is True
    assert evaluate("true", names) is True
    assert evaluate("null", names) is None


def test_participant_view_exposes_read_only_scenario_attributes() -> None:
    player = ParticipantView(
        id="actor-a",
        seat="01",
        name="Alex",
        tags=("active", "observer"),
        data={"score": 3, "nested": {"label": "ready"}},
    )
    names = {"player": player}

    assert evaluate("player.id", names) == "actor-a"
    assert evaluate("player.seat", names) == "01"
    assert evaluate("player.name", names) == "Alex"
    assert evaluate("player.tags[1]", names) == "observer"
    assert evaluate("player.data.score", names) == 3
    assert evaluate("player.data.nested.label", names) == "ready"


def test_comprehensions_support_filtering_and_scoped_participant_values() -> None:
    players = (
        _player("a", "01", "Alex", 1),
        _player("b", "02", "Blair", 2, "group-b"),
        _player("c", "03", "Casey", 3),
    )
    names = {"players": players}

    assert evaluate("[p.id for p in players if p.data.score >= 2]", names) == [
        "b",
        "c",
    ]
    assert evaluate("{p.data.group for p in players}", names) == {
        "group-a",
        "group-b",
    }
    assert evaluate(
        "{p.id: p.data.score + 1 for p in players if p.name != 'Casey'}",
        names,
    ) == {"a": 2, "b": 3}


@pytest.mark.parametrize(
    "expression",
    (
        "player.__class__",
        "import os",
        "__import__('os')",
        "player.name.upper()",
        "data.items()",
        "missing_name",
    ),
)
def test_unsafe_or_unknown_expressions_are_rejected(expression: str) -> None:
    names = {
        "player": _player("a", "01", "Alex", 1),
        "data": {"key": "value"},
    }

    with pytest.raises(ExpressionError):
        evaluate(expression, names)


def test_comprehension_requires_a_finite_sequence() -> None:
    names = {"values": (value for value in range(3))}

    with pytest.raises(ExpressionError, match="finite sequence"):
        evaluate("[value for value in values]", names)


def test_render_values_recurses_and_preserves_full_expression_types() -> None:
    values = {
        "amount": 7,
        "enabled": True,
        "nothing": None,
        "name": "Alex",
        "ids": ["a", "b"],
    }
    source = {
        "full_list": "${ ids }",
        "text": "enabled={{ enabled }}; empty={{ nothing }}; amount={{ amount }}",
        "nested": ["${ amount }", {"message": "Hello {{ name }}"}],
        "tuple": ("${ enabled }", 4),
        "unchanged": 9,
    }

    rendered = render_values(source, lambda expression: values[expression])

    assert rendered == {
        "full_list": ["a", "b"],
        "text": "enabled=true; empty=; amount=7",
        "nested": [7, {"message": "Hello Alex"}],
        "tuple": (True, 4),
        "unchanged": 9,
    }
    assert isinstance(rendered["full_list"], list)
    assert isinstance(rendered["tuple"], tuple)
    assert isinstance(rendered["tuple"][0], bool)


def test_tally_applies_document_supplied_weights_and_skips_empty_targets() -> None:
    ballots = [
        {"voter_id": "a", "target_id": "x"},
        {"voter_id": "b", "target_id": "x"},
        {"voter_id": "c", "target_id": "y"},
        {"voter_id": "a", "target_id": ""},
    ]

    assert tally(ballots) == {"x": 2.0, "y": 1.0}
    assert tally(ballots, privileged_voters=["a"], privileged_weight=1.5) == {
        "x": 2.5,
        "y": 1.0,
    }


def test_leaders_returns_all_tied_keys_in_input_order() -> None:
    assert leaders({"a": 1, "b": 3, "c": 3, "d": 2}) == ["b", "c"]
    assert leaders({}) == []


def test_seat_order_sorts_rotates_reverses_and_moves_last_id() -> None:
    players = [
        _player("c", "03", "Casey", 3),
        _player("a", "01", "Alex", 1),
        _player("d", "04", "Devon", 4),
        _player("b", "02", "Blair", 2),
    ]

    def ids(items: list[ParticipantView]) -> list[str]:
        return [item.id for item in items]

    assert ids(seat_order(players)) == ["a", "b", "c", "d"]
    assert ids(seat_order(players, start_id="c")) == ["c", "d", "a", "b"]
    assert ids(seat_order(players, start_id="c", direction="reverse")) == [
        "c",
        "b",
        "a",
        "d",
    ]
    assert ids(seat_order(players, last_id="b")) == ["a", "c", "d", "b"]
    assert seat_order([]) == []


def test_seat_order_also_accepts_mapping_participants() -> None:
    players = [{"id": "b", "seat": "02"}, {"id": "a", "seat": "01"}]

    assert seat_order(players) == [players[1], players[0]]
