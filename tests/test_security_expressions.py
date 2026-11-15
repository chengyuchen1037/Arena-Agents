"""Regression tests: arithmetic in scenario expressions must be size-bounded."""

from __future__ import annotations

import pytest

from app.engine.expressions import ExpressionError, evaluate


@pytest.mark.parametrize(
    "expression",
    [
        "'x' * 100000000",
        "100000000 * 'x'",
        "[0] * 100000000",
        "(1, 2) * 99999999999",
        "'ab' * 50001",
        "9999999999 * 9999999999 * 'x'",
        "'x' * 10 * 10 * 10 * 10 * 10 * 10 * 10 * 10",
        "2 ** 100000",
        "10 ** 10 ** 10",
        "9 ** 9 ** 9",
        "(2 ** 1000) ** 1000",
        "2 ** 1001",
        "2.0 ** 100000",
        "(2 ** 1000) * (2 ** 1000) * (2 ** 1000) * (2 ** 1000) * (2 ** 1000)",
    ],
)
def test_oversized_results_raise_expression_error(expression):
    with pytest.raises(ExpressionError):
        evaluate(expression)


def test_repeated_doubling_is_bounded():
    expression = "('x' * 1000) * 1000"
    with pytest.raises(ExpressionError):
        evaluate(expression)
    names = {"s": "x" * 60_000}
    with pytest.raises(ExpressionError):
        evaluate("s + s", names)


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("'ab' * 3", "ababab"),
        ("3 * [1]", [1, 1, 1]),
        ("[1, 2] * 0", []),
        ("'x' * -5", ""),
        ("2 * 3", 6),
        ("2 ** 10", 1024),
        ("2 ** -1", 0.5),
        ("(2 ** 100) * (2 ** 100)", 2**200),
        ("'a' + 'b'", "ab"),
        ("[1] + [2]", [1, 2]),
        ("1.5 * 2", 3.0),
        ("True * 'x'", "x"),
    ],
)
def test_normal_arithmetic_still_works(expression, expected):
    assert evaluate(expression) == expected


@pytest.mark.parametrize(
    "expression",
    ["1 / 0", "1 // 0", "5 % 0", "0 ** -1", "'a' * 'b'", "(-8) ** 0.5", "1 + 'a'"],
)
def test_arithmetic_failures_become_expression_errors(expression):
    with pytest.raises(ExpressionError):
        evaluate(expression)


def test_overlong_and_deeply_nested_expressions_are_rejected():
    with pytest.raises(ExpressionError):
        evaluate("1 + " * 5000 + "1")
    with pytest.raises(ExpressionError):
        evaluate("(" * 500 + "1" + ")" * 500)
    with pytest.raises(ExpressionError):
        evaluate("-" * 3000 + "1")
