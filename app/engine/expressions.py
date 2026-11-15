from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable


class ExpressionError(ValueError):
    """Raised when a scenario expression is invalid or unsafe."""


@dataclass(frozen=True)
class ParticipantView:
    """Read-only, game-agnostic participant projection exposed to expressions."""

    id: str
    seat: str
    name: str
    tags: tuple[str, ...]
    data: Mapping[str, Any]


_FULL_EXPRESSION = re.compile(r"^\s*\$\{(.*)\}\s*$", re.DOTALL)
_INLINE_EXPRESSION = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_MAX_COMPREHENSION_ITEMS = 10_000


# Hard limits that keep a (possibly imported / forged) scenario from exhausting
# memory or CPU through a single arithmetic expression.
MAX_RESULT_ITEMS = 100_000  # max length of a str/bytes/list/tuple produced by + or *
MAX_INT_BITS = 4_096  # max bit length of an integer produced by * or **
MAX_POW_EXPONENT = 1_000
MAX_EXPRESSION_CHARS = 10_000


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _is_repeatable(value: Any) -> bool:
    return isinstance(value, (str, bytes, bytearray, list, tuple))


def _is_int(value: Any) -> bool:
    return isinstance(value, int)  # bool is an int subclass: True * "x" is legal


def _safe_add(left: Any, right: Any) -> Any:
    if _is_repeatable(left) and _is_repeatable(right):
        try:
            total = len(left) + len(right)
        except TypeError:
            total = 0
        if total > MAX_RESULT_ITEMS:
            raise ExpressionError("Expression result size limit exceeded")
    return left + right


def _safe_mult(left: Any, right: Any) -> Any:
    if _is_repeatable(left) and _is_int(right):
        sequence, count = left, right
    elif _is_repeatable(right) and _is_int(left):
        sequence, count = right, left
    else:
        sequence = None
        count = 0
    if sequence is not None:
        if count > 0 and len(sequence) * count > MAX_RESULT_ITEMS:
            raise ExpressionError("Expression result size limit exceeded")
        return sequence * count
    if _is_int(left) and _is_int(right):
        if abs(left).bit_length() + abs(right).bit_length() > MAX_INT_BITS:
            raise ExpressionError("Expression integer size limit exceeded")
    return left * right


def _safe_pow(left: Any, right: Any) -> Any:
    if _is_int(left) and _is_int(right):
        if right < 0:
            # Negative exponents yield a float; the result is always small.
            return left**right
        if right > MAX_POW_EXPONENT:
            raise ExpressionError("Exponent limit exceeded")
        if abs(left).bit_length() * right > MAX_INT_BITS:
            raise ExpressionError("Expression integer size limit exceeded")
        return left**right
    if isinstance(right, (int, float)) and not isinstance(right, bool) and abs(right) > MAX_POW_EXPONENT:
        raise ExpressionError("Exponent limit exceeded")
    result = left**right
    if isinstance(result, complex):
        raise ExpressionError("Complex results are not supported")
    return result


def tally(
    ballots: Sequence[Mapping[str, Any]],
    privileged_voters: Sequence[str] | None = None,
    privileged_weight: float = 1.0,
) -> dict[str, float]:
    """Count generic ballots, optionally applying document-supplied voter weights."""

    privileged = set(privileged_voters or [])
    result: dict[str, float] = {}
    for ballot in ballots:
        target = str(ballot.get("target_id", ""))
        voter = str(ballot.get("voter_id", ""))
        if not target:
            continue
        weight = float(privileged_weight) if voter in privileged else 1.0
        result[target] = float(result.get(target, 0.0)) + weight
    return result


def leaders(counts: Mapping[str, Any]) -> list[str]:
    """Return every key tied for the maximum numeric value, preserving order."""

    if not counts:
        return []
    numeric = {str(key): float(value) for key, value in counts.items()}
    highest = max(numeric.values())
    return [key for key, value in numeric.items() if value == highest]


_DIGIT_RUNS = re.compile(r"(\d+)")


def _natural_key(text: str) -> tuple[tuple[int, int, str], ...]:
    """Sort key comparing digit runs numerically ('2' < '10') and text lexically."""

    key: list[tuple[int, int, str]] = []
    for chunk in _DIGIT_RUNS.split(text):
        if not chunk:
            continue
        if chunk.isdigit():
            key.append((0, int(chunk), ""))
        else:
            key.append((1, 0, chunk))
    return tuple(key)


def seat_order(
    participants: Sequence[Any],
    start_id: str | None = None,
    direction: str | int = "forward",
    last_id: str | None = None,
) -> list[Any]:
    """Order participants by seat in a circular direction, optionally putting one last."""

    items = list(participants)
    items.sort(
        key=lambda item: _natural_key(
            str(item.seat if hasattr(item, "seat") else item.get("seat", ""))
        )
    )
    if not items:
        return []
    if start_id:
        start_index = next(
            (
                index
                for index, item in enumerate(items)
                if str(item.id if hasattr(item, "id") else item.get("id", "")) == str(start_id)
            ),
            0,
        )
        items = items[start_index:] + items[:start_index]
    reverse = direction in ("reverse", "backward", "right", -1, "-1")
    if reverse:
        if start_id:
            items = [items[0], *reversed(items[1:])]
        else:
            items.reverse()
    if last_id:
        items.sort(
            key=lambda item: str(item.id if hasattr(item, "id") else item.get("id", ""))
            == str(last_id)
        )
    return items


class SafeExpression:
    """Small AST interpreter for declarative scenarios; never calls Python eval."""

    def __init__(
        self,
        names: Mapping[str, Any] | None = None,
        functions: Mapping[str, Callable[..., Any]] | None = None,
    ) -> None:
        self.names = dict(names or {})
        self.functions = dict(functions or {})

    def evaluate(self, expression: str) -> Any:
        if not isinstance(expression, str) or not expression.strip():
            raise ExpressionError("Expression must be a non-empty string")
        if len(expression) > MAX_EXPRESSION_CHARS:
            raise ExpressionError("Expression is too long")
        try:
            tree = ast.parse(expression.strip(), mode="eval")
        except SyntaxError as exc:
            raise ExpressionError(f"Invalid expression {expression!r}: {exc.msg}") from exc
        except (RecursionError, MemoryError, ValueError) as exc:
            raise ExpressionError(f"Invalid expression: {type(exc).__name__}") from exc
        try:
            return self._eval(tree.body, self.names)
        except RecursionError as exc:
            raise ExpressionError("Expression is nested too deeply") from exc

    def _eval(self, node: ast.AST, names: dict[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in names:
                return names[node.id]
            if node.id == "true":
                return True
            if node.id == "false":
                return False
            if node.id == "null":
                return None
            raise ExpressionError(f"Unknown name: {node.id}")
        if isinstance(node, ast.List):
            return [self._eval(item, names) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(self._eval(item, names) for item in node.elts)
        if isinstance(node, ast.Dict):
            return {
                self._eval(key, names): self._eval(value, names)
                for key, value in zip(node.keys, node.values)
                if key is not None
            }
        if isinstance(node, ast.UnaryOp):
            value = self._eval(node.operand, names)
            if isinstance(node.op, ast.Not):
                return not value
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return +value
            raise ExpressionError(type(node.op).__name__)
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result: Any = True
                for value_node in node.values:
                    result = self._eval(value_node, names)
                    if not result:
                        return result
                return result
            if isinstance(node.op, ast.Or):
                result = False
                for value_node in node.values:
                    result = self._eval(value_node, names)
                    if result:
                        return result
                return result
            raise ExpressionError(type(node.op).__name__)
        if isinstance(node, ast.BinOp):
            left = self._eval(node.left, names)
            right = self._eval(node.right, names)
            operations: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
                ast.Add: _safe_add,
                ast.Sub: lambda a, b: a - b,
                ast.Mult: _safe_mult,
                ast.Div: lambda a, b: a / b,
                ast.FloorDiv: lambda a, b: a // b,
                ast.Mod: lambda a, b: a % b,
                ast.Pow: _safe_pow,
            }
            operation = operations.get(type(node.op))
            if not operation:
                raise ExpressionError(f"Unsupported operator: {type(node.op).__name__}")
            try:
                return operation(left, right)
            except ExpressionError:
                raise
            except (ZeroDivisionError, OverflowError, TypeError, MemoryError) as exc:
                raise ExpressionError(
                    f"Invalid arithmetic ({type(exc).__name__}): {exc}"
                ) from exc
        if isinstance(node, ast.Compare):
            left = self._eval(node.left, names)
            for operator, comparator in zip(node.ops, node.comparators):
                right = self._eval(comparator, names)
                if isinstance(operator, ast.Eq):
                    passed = left == right
                elif isinstance(operator, ast.NotEq):
                    passed = left != right
                elif isinstance(operator, ast.Lt):
                    passed = left < right
                elif isinstance(operator, ast.LtE):
                    passed = left <= right
                elif isinstance(operator, ast.Gt):
                    passed = left > right
                elif isinstance(operator, ast.GtE):
                    passed = left >= right
                elif isinstance(operator, ast.In):
                    passed = left in right
                elif isinstance(operator, ast.NotIn):
                    passed = left not in right
                elif isinstance(operator, ast.Is):
                    passed = left is right
                elif isinstance(operator, ast.IsNot):
                    passed = left is not right
                else:
                    raise ExpressionError(f"Unsupported comparison: {type(operator).__name__}")
                if not passed:
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            branch = node.body if self._eval(node.test, names) else node.orelse
            return self._eval(branch, names)
        if isinstance(node, ast.Subscript):
            target = self._eval(node.value, names)
            key = self._eval(node.slice, names)
            try:
                return target[key]
            except (KeyError, IndexError, TypeError) as exc:
                raise ExpressionError(f"Invalid subscript {key!r}") from exc
        if isinstance(node, ast.Slice):
            return slice(
                self._eval(node.lower, names) if node.lower else None,
                self._eval(node.upper, names) if node.upper else None,
                self._eval(node.step, names) if node.step else None,
            )
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ExpressionError("Private attribute access is forbidden")
            target = self._eval(node.value, names)
            if isinstance(target, ParticipantView):
                if node.attr in ("id", "seat", "name", "tags", "data"):
                    return getattr(target, node.attr)
            if isinstance(target, Mapping):
                if node.attr in target:
                    return target[node.attr]
                raise ExpressionError(f"Unknown mapping key: {node.attr}")
            raise ExpressionError("Attributes are only available on read-only scenario values")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExpressionError("Only named scenario functions may be called")
            function_name = node.func.id
            function = self.functions.get(function_name)
            if function is None:
                raise ExpressionError(f"Unknown or forbidden function: {function_name}")
            if node.keywords and any(keyword.arg is None for keyword in node.keywords):
                raise ExpressionError("Expanded keyword arguments are forbidden")
            args = [self._eval(arg, names) for arg in node.args]
            kwargs = {
                str(keyword.arg): self._eval(keyword.value, names)
                for keyword in node.keywords
            }
            return function(*args, **kwargs)
        if isinstance(node, (ast.ListComp, ast.SetComp)):
            return self._comprehension(node, names, set if isinstance(node, ast.SetComp) else list)
        if isinstance(node, ast.DictComp):
            return self._dict_comprehension(node, names)
        raise ExpressionError(f"Unsupported expression element: {type(node).__name__}")

    def _comprehension(
        self,
        node: ast.ListComp | ast.SetComp,
        names: dict[str, Any],
        factory: type[list[Any]] | type[set[Any]],
    ) -> Any:
        if len(node.generators) != 1 or node.generators[0].is_async:
            raise ExpressionError("Exactly one synchronous comprehension generator is required")
        generator = node.generators[0]
        if not isinstance(generator.target, ast.Name):
            raise ExpressionError("Comprehension target must be a simple name")
        iterable = self._eval(generator.iter, names)
        if not _is_sequence(iterable) and not isinstance(iterable, (set, frozenset)):
            raise ExpressionError("Comprehension value must be a finite sequence")
        values = []
        for index, item in enumerate(iterable):
            if index >= _MAX_COMPREHENSION_ITEMS:
                raise ExpressionError("Comprehension item limit exceeded")
            scoped = dict(names)
            scoped[generator.target.id] = item
            if all(self._eval(condition, scoped) for condition in generator.ifs):
                values.append(self._eval(node.elt, scoped))
        return factory(values)

    def _dict_comprehension(self, node: ast.DictComp, names: dict[str, Any]) -> dict[Any, Any]:
        if len(node.generators) != 1 or node.generators[0].is_async:
            raise ExpressionError("Exactly one synchronous comprehension generator is required")
        generator = node.generators[0]
        if not isinstance(generator.target, ast.Name):
            raise ExpressionError("Comprehension target must be a simple name")
        iterable = self._eval(generator.iter, names)
        if not _is_sequence(iterable) and not isinstance(iterable, (set, frozenset)):
            raise ExpressionError("Comprehension value must be a finite sequence")
        result: dict[Any, Any] = {}
        for index, item in enumerate(iterable):
            if index >= _MAX_COMPREHENSION_ITEMS:
                raise ExpressionError("Comprehension item limit exceeded")
            scoped = dict(names)
            scoped[generator.target.id] = item
            if all(self._eval(condition, scoped) for condition in generator.ifs):
                result[self._eval(node.key, scoped)] = self._eval(node.value, scoped)
        return result


def evaluate(
    expression: str,
    names: Mapping[str, Any] | None = None,
    functions: Mapping[str, Callable[..., Any]] | None = None,
) -> Any:
    return SafeExpression(names, functions).evaluate(expression)


def render_values(value: Any, evaluator: Callable[[str], Any]) -> Any:
    """Recursively resolve ${...} values and {{...}} text interpolation."""

    if isinstance(value, dict):
        return {key: render_values(item, evaluator) for key, item in value.items()}
    if isinstance(value, list):
        return [render_values(item, evaluator) for item in value]
    if isinstance(value, tuple):
        return tuple(render_values(item, evaluator) for item in value)
    if not isinstance(value, str):
        return value
    full = _FULL_EXPRESSION.fullmatch(value)
    if full:
        return evaluator(full.group(1).strip())

    def replace(match: re.Match[str]) -> str:
        resolved = evaluator(match.group(1).strip())
        if resolved is None:
            return ""
        if isinstance(resolved, bool):
            return "true" if resolved else "false"
        return str(resolved)

    return _INLINE_EXPRESSION.sub(replace, value)
