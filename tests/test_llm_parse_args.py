"""ToolCallData.parse_args always yields a dict (report item 7, llm.py:39)."""

from __future__ import annotations

import pytest

from app.engine.llm import ARGS_ERROR_KEY, ToolCallData, args_error


def _tc(arguments: str, name: str = "vote") -> ToolCallData:
    return ToolCallData(id="c1", name=name, arguments_json=arguments)


@pytest.mark.parametrize("arguments", ["", "   ", "\n\t"])
def test_empty_arguments_are_an_empty_dict_without_error(arguments: str) -> None:
    tc = _tc(arguments)
    assert tc.parse_args() == {}
    assert tc.args_error is None
    assert tc.parse_args_checked() == ({}, None)


def test_json_object_is_returned_untouched() -> None:
    tc = _tc('{"target": "p3", "reason": "因为", "n": 2, "nested": {"a": [1, 2]}}')
    args = tc.parse_args()
    assert args == {"target": "p3", "reason": "因为", "n": 2, "nested": {"a": [1, 2]}}
    assert ARGS_ERROR_KEY not in args
    assert tc.args_error is None


def test_empty_object_is_valid() -> None:
    assert _tc("{}").parse_args() == {}
    assert _tc("{}").args_error is None


@pytest.mark.parametrize(
    "arguments, type_name",
    [
        ('["a", "b"]', "list"),
        ('"just a string"', "str"),
        ("42", "int"),
        ("3.14", "float"),
        ("null", "NoneType"),
        ("true", "bool"),
        ("[]", "list"),
    ],
)
def test_non_object_json_returns_dict_with_error_marker(arguments: str, type_name: str) -> None:
    tc = _tc(arguments, name="cast_vote")
    args = tc.parse_args()
    assert isinstance(args, dict)
    error = args[ARGS_ERROR_KEY]
    assert "cast_vote" in error and type_name in error
    assert args["_raw"] == arguments
    # attribute / helper agree and the dict is safe to .get() from
    assert tc.args_error == error
    assert args_error(args) == error
    assert args.get("target") is None
    assert tc.parse_args_checked() == (args, error)


@pytest.mark.parametrize("arguments", ["{not json", '{"a": ', "undefined", "{'a': 1}"])
def test_invalid_json_keeps_raw_and_adds_error_marker(arguments: str) -> None:
    tc = _tc(arguments)
    args = tc.parse_args()
    assert isinstance(args, dict)
    assert args["_raw"] == arguments
    assert args[ARGS_ERROR_KEY]
    assert tc.args_error == args[ARGS_ERROR_KEY]


def test_pathologically_nested_json_does_not_raise() -> None:
    tc = _tc("[" * 100000)
    args = tc.parse_args()
    assert isinstance(args, dict) and args_error(args)


def test_args_error_helper_on_ordinary_values() -> None:
    assert args_error({}) is None
    assert args_error({"target": 1}) is None
    assert args_error(None) is None
    assert args_error([1]) is None
    assert args_error({ARGS_ERROR_KEY: ""}) is None
    assert args_error({ARGS_ERROR_KEY: "bad"}) == "bad"


def test_parse_args_is_repeatable_and_does_not_mutate_the_call() -> None:
    tc = _tc("[1]")
    first, second = tc.parse_args(), tc.parse_args()
    assert first == second
    first["x"] = 1
    assert "x" not in tc.parse_args()
    assert tc.arguments_json == "[1]"
