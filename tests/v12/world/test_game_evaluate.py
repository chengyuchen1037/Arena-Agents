"""``mod.evaluate`` with ``args``: names bound for one evaluation of a scenario check."""

from __future__ import annotations

import pytest

import app.world.core as core_module
from app.world.game import MAX_EVAL_ARGS_CHARS, RESERVED_EXPR_NAMES, check_eval_args
from app.world.types import Code

from .test_game_kit import SPEC_DICT, make_spec, make_world, mod

CHECKS = {
    **SPEC_DICT["checks"],
    "speech_order": "${ids(seat_order(alive(), start, direction))}",
    "sum_two": "${a + b * 2}",
    "lookup": "${get(table, key, 'none')}",
    "big_ones": "${count([i for i in items if i > limit])}",
    "role_holder": "${first(by_role(role_name))}",
    "either": "${a if flag else b}",
    "echo": "${payload}",
}


def make(**overrides):
    return make_world(spec=make_spec(checks=CHECKS, **overrides))


def evaluate(world, check_id, **args):
    return mod(world, "evaluate", check_id=check_id, args=args)


# --------------------------------------------------------------------------- passing arguments


def test_args_are_bound_as_expression_variables():
    world = make()
    forward = evaluate(world, "speech_order", start="u3", direction="forward")
    assert forward.ok and forward.data["value"] == ["u3", "u4", "u1", "u2"]
    assert forward.data["check_id"] == "speech_order" and forward.seq is None

    backward = evaluate(world, "speech_order", start="u3", direction="right")
    assert backward.data["value"] == ["u3", "u2", "u1", "u4"]

    assert evaluate(world, "sum_two", a=3, b=4).data["value"] == 11
    assert evaluate(world, "lookup", table={"x": 7, "y": 8}, key="y").data["value"] == 8
    assert evaluate(world, "lookup", table={"x": 7}, key="zzz").data["value"] == "none"
    assert evaluate(world, "big_ones", items=[1, 5, 9, 12], limit=4).data["value"] == 3
    assert evaluate(world, "echo", payload={"nested": [1, 2, {"ok": True}], "none": None}).data["value"] == {
        "nested": [1, 2, {"ok": True}],
        "none": None,
    }


def test_a_player_result_is_converted_to_an_id_and_args_follow_the_live_state():
    world = make()
    holder = next(p.id for p in world.state.players if p.role == "alpha")
    assert evaluate(world, "role_holder", role_name="alpha").data["value"] == holder
    assert mod(world, "update_player", player_id=holder, add_tags=["out"]).ok
    order = evaluate(world, "speech_order", start="u1", direction="forward").data["value"]
    assert holder not in order and len(order) == 3  # ``alive()`` reflects the new state


def test_args_exist_for_one_evaluation_only_and_unused_args_are_ignored():
    world = make()
    assert evaluate(world, "sum_two", a=1, b=1, unused="x").data["value"] == 3
    again = evaluate(world, "sum_two", a=1)
    assert again.code == Code.INVALID_PARAMS and again.data["missing"] == "b"  # nothing was kept from the call before
    assert evaluate(world, "sum_two").data["missing"] == "a"


def test_evaluate_with_args_is_read_only():
    world = make()
    seq, counters = world.seq, dict(world.state.counters)
    assert evaluate(world, "speech_order", start="u2", direction="forward").ok
    assert evaluate(world, "sum_two", a=1).code == Code.INVALID_PARAMS
    assert world.seq == seq and world.state.counters == counters and world.drain_outbox() == []


def test_checks_without_args_keep_working():
    world = make()
    assert mod(world, "evaluate", check_id="red_alive").data["value"] == 1  # ``args`` defaults to {}
    assert evaluate(world, "red_alive", start="u1").data["value"] == 1  # extra names are harmless


# --------------------------------------------------------------------------- missing names


def test_a_missing_name_is_reported_by_name_with_invalid_params():
    world = make()
    seq = world.seq
    receipt = evaluate(world, "speech_order", start="u3")
    assert not receipt.ok and receipt.code == Code.INVALID_PARAMS
    assert "direction" in receipt.message and "start" in receipt.message  # what is missing, what was given
    assert receipt.data["missing"] == "direction"

    nothing = evaluate(world, "speech_order")
    assert nothing.code == Code.INVALID_PARAMS and nothing.data["missing"] == "start"
    assert "没有传入任何 args" in nothing.message
    assert world.seq == seq


def test_only_the_names_actually_evaluated_are_required():
    world = make()
    assert evaluate(world, "either", flag=True, a=1).data["value"] == 1  # ``b`` is on the untaken branch
    receipt = evaluate(world, "either", flag=False, a=1)
    assert receipt.code == Code.INVALID_PARAMS and receipt.data["missing"] == "b"


def test_other_expression_failures_stay_internal_errors():
    world = make()
    assert evaluate(world, "broken", x=1).code == Code.INTERNAL_ERROR  # unknown function in the scenario
    assert mod(world, "evaluate", check_id="nope", args={"x": 1}).code == Code.INVALID_TARGET
    bad_args_for_unknown_check = mod(world, "evaluate", check_id="nope", args={"1x": 1})
    assert bad_args_for_unknown_check.code == Code.INVALID_TARGET  # the check is looked up first


# --------------------------------------------------------------------------- illegal names


@pytest.mark.parametrize(
    "name",
    ["1abc", "a-b", "a b", "", "名字", "if", "for", "None", "a.b", "a\n", " a", "x$"],
)
def test_names_that_are_not_plain_identifiers_are_rejected(name):
    world = make()
    seq = world.seq
    receipt = mod(world, "evaluate", check_id="sum_two", args={name: 1, "a": 1, "b": 1})
    assert receipt.code == Code.INVALID_PARAMS and not receipt.ok
    assert world.seq == seq


@pytest.mark.parametrize("name", ["_x", "A1", "x_y2", "__dunder__", "a", "Start"])
def test_identifier_names_are_accepted(name):
    world = make()
    assert mod(world, "evaluate", check_id="red_alive", args={name: 1}).ok


@pytest.mark.parametrize("name", sorted(RESERVED_EXPR_NAMES))
def test_built_in_names_cannot_be_overridden(name):
    world = make()
    receipt = mod(world, "evaluate", check_id="red_alive", args={name: [1, 2]})
    assert receipt.code == Code.INVALID_PARAMS and name in receipt.message


def test_reserved_names_cover_the_functions_of_eval_expr(monkeypatch):
    """Guards against drift: every function ``eval_expr`` offers (plus ``players`` and the literals) is reserved."""
    seen: dict[str, set[str]] = {}

    class Spy(core_module.SafeExpression):
        def __init__(self, names=None, functions=None):
            seen["functions"] = set(functions or {})
            seen["names"] = set(names or {})
            super().__init__(names, functions)

    monkeypatch.setattr(core_module, "SafeExpression", Spy)
    make().eval_expr("${1}")
    assert seen["functions"] <= RESERVED_EXPR_NAMES
    assert seen["names"] <= RESERVED_EXPR_NAMES
    assert RESERVED_EXPR_NAMES - seen["functions"] - seen["names"] == {"true", "false", "null"}
    for name in ("alive", "by_role", "by_faction", "by_tag", "ids", "first", "count", "len", "sum", "min", "max",
                 "get", "tally", "leaders", "seat_order", "players"):
        assert name in RESERVED_EXPR_NAMES


def test_the_players_variable_cannot_be_shadowed():
    world = make()
    receipt = mod(world, "evaluate", check_id="red_alive", args={"players": []})
    assert receipt.code == Code.INVALID_PARAMS
    assert mod(world, "evaluate", check_id="red_alive").data["value"] == 1


# --------------------------------------------------------------------------- illegal values


@pytest.mark.parametrize(
    "value",
    [{1, 2}, (1, 2), b"bytes", object(), {1: "int key"}, {"ok": {2: "nested int key"}}, float("nan"), float("inf"), [object()]],
    ids=["set", "tuple", "bytes", "object", "int-key", "nested-int-key", "nan", "inf", "list-with-object"],
)
def test_values_must_be_plain_json(value):
    world = make()
    receipt = mod(world, "evaluate", check_id="echo", args={"payload": value})
    assert receipt.code == Code.INVALID_PARAMS and "payload" in receipt.message


def test_nesting_is_limited():
    world = make()
    shallow: object = "x"
    for _ in range(3):
        shallow = [shallow]
    assert mod(world, "evaluate", check_id="echo", args={"payload": shallow}).ok
    deep: object = "x"
    for _ in range(12):
        deep = [deep]
    assert mod(world, "evaluate", check_id="echo", args={"payload": deep}).code == Code.INVALID_PARAMS


def test_json_primitives_are_all_accepted():
    world = make()
    value = {"s": "文本", "i": 3, "f": 1.5, "t": True, "n": None, "l": [1, "a", None], "d": {"k": [False]}}
    receipt = mod(world, "evaluate", check_id="echo", args={"payload": value})
    assert receipt.ok and receipt.data["value"] == value


def test_total_size_is_limited():
    world = make()
    assert mod(world, "evaluate", check_id="echo", args={"payload": "x" * 3900}).ok
    too_big = mod(world, "evaluate", check_id="echo", args={"payload": "x" * 4100})
    assert too_big.code == Code.INVALID_PARAMS and str(MAX_EVAL_ARGS_CHARS) in too_big.message
    # the limit counts all args together ...
    split = mod(world, "evaluate", check_id="red_alive", args={"a": "x" * 1500, "b": "y" * 1500, "c": "z" * 1500})
    assert split.code == Code.INVALID_PARAMS
    # ... and characters, not bytes or escapes: CJK text is not counted as \uXXXX sequences
    assert mod(world, "evaluate", check_id="echo", args={"payload": "字" * 3500}).ok


def test_check_eval_args_reports_a_reason_only_for_bad_input():
    assert check_eval_args({}) is None
    assert check_eval_args({"start": "u1", "n": 3, "xs": [1, 2]}) is None
    assert "count" in (check_eval_args({"count": 1}) or "")
    assert check_eval_args({"x": {1, 2}}) is not None
