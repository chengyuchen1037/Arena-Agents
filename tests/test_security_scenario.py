"""Scenario validation / ordering regressions owned by sec-api (review item 30)."""

from __future__ import annotations

import copy

import pytest

from app.engine.expressions import seat_order
from app.engine.scenario import ScenarioError, validate_scenario


def _definition(do: list, hooks: dict | None = None) -> dict:
    definition = {
        "schema_version": 1,
        "id": "t",
        "version": "1",
        "entry": "start",
        "workflow": [{"id": "start", "do": do}],
    }
    if hooks is not None:
        definition["hooks"] = hooks
    return copy.deepcopy(definition)


def test_break_outside_loop_is_rejected_at_validation():
    with pytest.raises(ScenarioError, match="break"):
        validate_scenario(_definition([{"break": None}]))
    with pytest.raises(ScenarioError, match="break"):
        validate_scenario(
            _definition([{"if": {"when": "true", "do": [{"break": None}]}}])
        )
    with pytest.raises(ScenarioError, match="break"):
        validate_scenario(
            _definition([], hooks={"ev": [{"do": [{"break": None}]}]})
        )
    with pytest.raises(ScenarioError, match="break"):
        validate_scenario(
            _definition([{"if": {"when": "true", "do": [], "else": [{"break": None}]}}])
        )


def test_break_inside_loops_including_nested_if_is_allowed():
    validate_scenario(
        _definition([{"while": {"when": "true", "do": [{"break": None}]}}])
    )
    validate_scenario(
        _definition(
            [
                {
                    "for_each": {
                        "items": "[1]",
                        "as": "x",
                        "do": [
                            {"if": {"when": "true", "do": [{"break": None}], "else": [{"break": None}]}}
                        ],
                    }
                }
            ]
        )
    )


def test_break_after_loop_ends_is_still_rejected():
    do = [
        {"while": {"when": "true", "do": [{"break": None}]}},
        {"break": None},
    ]
    with pytest.raises(ScenarioError, match="break"):
        validate_scenario(_definition(do))


def test_seat_order_sorts_numerically_without_zero_padding():
    players = [{"id": f"p{n}", "seat": str(n)} for n in (10, 2, 1, 12, 3, 11)]
    assert [p["id"] for p in seat_order(players)] == ["p1", "p2", "p3", "p10", "p11", "p12"]


def test_seat_order_mixed_prefixes_and_padding():
    players = [
        {"id": "b10", "seat": "B10"},
        {"id": "a2", "seat": "A2"},
        {"id": "b2", "seat": "B2"},
        {"id": "a10", "seat": "A10"},
        {"id": "pad", "seat": "A02x"},
    ]
    assert [p["id"] for p in seat_order(players)] == ["a2", "pad", "a10", "b2", "b10"]
