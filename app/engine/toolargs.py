"""Defensive parsing helpers for model-supplied tool arguments.

Everything the model sends is untrusted: JSON may be malformed, may not be an
object, and numeric fields may hold arbitrary strings.  These helpers never
raise for bad input unless the caller explicitly asks for strict parsing, and
the strict variants raise :class:`ToolArgumentError`, which the tool dispatchers
turn into a clear tool error for the model (instead of pausing the whole game).
"""

from __future__ import annotations

import json
from typing import Any

ARGS_ERROR_KEY = "_args_error"


class ToolArgumentError(ValueError):
    """A tool call carried arguments that cannot be used."""


def parse_tool_args(call: Any) -> tuple[dict[str, Any], str | None]:
    """Return ``(args, error)``; ``args`` is always a dict, ``error`` is a reason or None."""
    try:
        args = call.parse_args()
    except Exception as exc:  # noqa: BLE001 - parse_args must never take the game down
        return {}, f"参数解析失败：{exc}"
    if not isinstance(args, dict):
        return {}, "工具参数必须是 JSON 对象"
    marker = args.get(ARGS_ERROR_KEY)
    if marker:
        return {}, str(marker)
    if set(args) == {"_raw"}:
        return {}, "工具参数不是合法的 JSON 对象"
    return args, None


def args_error_result(name: str, error: str) -> str:
    """JSON tool result returned to a participant for unusable arguments."""
    return json.dumps(
        {"ok": False, "error": f"工具 {name} 的参数无效：{error}"}, ensure_ascii=False
    )


def arg_str(args: dict[str, Any], key: str, default: str = "") -> str:
    """Stripped string argument; missing/None falls back to *default* (never ``"None"``)."""
    value = args.get(key)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def arg_str_list(args: dict[str, Any], key: str) -> list[str]:
    """List of non-empty strings; a lone scalar becomes a one-item list."""
    value = args.get(key)
    if value is None:
        return []
    if isinstance(value, (str, int)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [text for item in value if item is not None and (text := str(item).strip())]


def arg_dict_list(args: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = args.get(key)
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def safe_int(
    value: Any,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Lenient int conversion that falls back to *default* instead of raising."""
    try:
        if isinstance(value, bool):
            number = int(value)
        elif isinstance(value, float):
            number = int(value)
        else:
            number = int(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def strict_int(
    value: Any,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Int conversion that reports a clear :class:`ToolArgumentError` for bad input."""
    if isinstance(value, bool):
        raise ToolArgumentError(f"{field} 必须是整数")
    try:
        if isinstance(value, float):
            if value != int(value):
                raise ValueError
            number = int(value)
        else:
            number = int(str(value).strip())
    except (TypeError, ValueError, OverflowError) as exc:
        raise ToolArgumentError(f"{field} 必须是整数，收到 {value!r}") from exc
    if minimum is not None and number < minimum:
        raise ToolArgumentError(f"{field} 不能小于 {minimum}")
    if maximum is not None and number > maximum:
        raise ToolArgumentError(f"{field} 不能大于 {maximum}")
    return number
