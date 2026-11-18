"""Parameter validation and text rendering shared by templates and abilities (contracts §2.3 ``speak_template`` / ``use_ability``).

Both are pure functions over a :class:`~app.world.spec.ParamSpec` mapping and the world's players.
"""

from __future__ import annotations

import re
from typing import Any

from app.world.spec import ParamSpec
from app.world.types import Code, Receipt

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def player_label(world: Any, player_id: str) -> str:
    """``<id> <name>`` (the id is what tools take, the name is what people say) for a known player, the raw
    id otherwise.  Everything a model reads names players this way."""
    player = world.player(player_id)
    return f"{player.id} {player.name}" if player else str(player_id)


def validate_params(
    specs: dict[str, ParamSpec], params: dict[str, Any], world: Any
) -> tuple[dict[str, Any] | None, Receipt | None]:
    """Validate ``params`` against ``specs``.

    Returns ``(clean_params, None)`` or ``(None, rejected_receipt)``.  Rules: unknown keys are
    rejected; missing required keys are rejected; ``player_id`` / ``player_ids`` must name existing
    players (``INVALID_TARGET``); ``enum`` must be one of the values; ``int`` must be an integer in
    range; ``text`` is stringified and **truncated** to ``max_chars``.  Optional params that are
    missing are omitted from the result.
    """
    unknown = sorted(set(params) - set(specs))
    if unknown:
        return None, Receipt.reject(
            Code.INVALID_PARAMS, f"未知参数：{', '.join(unknown)}；允许的参数：{', '.join(specs) or '（无）'}"
        )
    clean: dict[str, Any] = {}
    for name, spec in specs.items():
        if name not in params or params[name] is None or params[name] == "":
            if spec.optional:
                continue
            return None, Receipt.reject(Code.INVALID_PARAMS, f"缺少参数：{name}")
        value = params[name]
        if spec.type == "player_id":
            if not isinstance(value, str) or world.player(value) is None:
                return None, Receipt.reject(Code.INVALID_TARGET, f"参数 {name}：{value!r} 不是有效的玩家 id")
            clean[name] = value
        elif spec.type == "player_ids":
            items = value if isinstance(value, list) else [value]
            bad = [i for i in items if not isinstance(i, str) or world.player(i) is None]
            if bad or not items:
                return None, Receipt.reject(Code.INVALID_TARGET, f"参数 {name}：包含无效的玩家 id {bad!r}")
            clean[name] = [str(i) for i in items]
        elif spec.type == "enum":
            if str(value) not in spec.values:
                return None, Receipt.reject(
                    Code.INVALID_PARAMS, f"参数 {name} 必须是 {' / '.join(spec.values)} 之一"
                )
            clean[name] = str(value)
        elif spec.type == "int":
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                return None, Receipt.reject(Code.INVALID_PARAMS, f"参数 {name} 必须是整数")
            try:
                number = int(str(value).strip()) if not isinstance(value, (int, float)) else int(value)
            except ValueError:
                return None, Receipt.reject(Code.INVALID_PARAMS, f"参数 {name} 必须是整数")
            if (spec.min is not None and number < spec.min) or (spec.max is not None and number > spec.max):
                return None, Receipt.reject(
                    Code.INVALID_PARAMS, f"参数 {name} 必须在 {spec.min} 到 {spec.max} 之间"
                )
            clean[name] = number
        else:  # text
            clean[name] = str(value)[: spec.max_chars]
    return clean, None


def render_text(
    text: str, params: dict[str, Any], specs: dict[str, ParamSpec], world: Any, extra: dict[str, str] | None = None
) -> str:
    """Replace ``{{name}}`` placeholders.  ``player_id`` renders as ``名字（id）``, ``player_ids`` joined
    by ``、``; ``extra`` supplies additional ready-made strings (e.g. ``actor``)."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if extra and name in extra:
            return extra[name]
        if name not in params:
            return "" if name in specs else match.group(0)
        value = params[name]
        spec = specs.get(name)
        if spec is not None and spec.type == "player_id":
            return player_label(world, str(value))
        if spec is not None and spec.type == "player_ids":
            return "、".join(player_label(world, str(v)) for v in value)
        return str(value)

    return _PLACEHOLDER.sub(replace, text)
