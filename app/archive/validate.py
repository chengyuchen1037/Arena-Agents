"""What an archive member must contain, kind by kind (shared by the exporter's self-check and the importer).

Structure and checksums are the importer's business (:mod:`app.archive.importer`); this module judges the *content*
of one member that has been read into memory.  Every message names the member and the reason, never its content.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.archive.errors import ArchiveFormatError, safe_text
from app.archive.format import (
    JSON_KINDS,
    KIND_GAME,
    KIND_GAME_PREV,
    KIND_GAMES_INDEX,
    KIND_LOG,
    KIND_PRICING,
    KIND_PROVIDERS,
    KIND_ROSTER_JSON,
    KIND_ROSTER_TEXT,
    KIND_SECRETS,
    KIND_SETTINGS,
)
from app.archive.kdf import ENDPOINT_FIELDS, parse_secrets_header

_SEAT_ID = re.compile(r"^N(?:[1-9]|[1-9][0-9])$")
_SECRET_FIELD = re.compile(r"^api[_\- ]?key$", re.IGNORECASE)
_MAX_TEXT_BYTES = 1024 * 1024
_MAX_JSON_DEPTH = 24


@dataclass
class Inventory:
    """What the checked members add up to: the numbers and names the summary shows."""

    games: dict[str, dict[str, Any]] = field(default_factory=dict)  # game id -> a few facts about it
    prev_games: set[str] = field(default_factory=set)
    provider_ids: list[str] = field(default_factory=list)
    provider_names: list[str] = field(default_factory=list)
    provider_endpoints: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> protocol / base_url / auto_append_path
    seat_ids: set[str] = field(default_factory=set)
    log_games: set[str] = field(default_factory=set)
    has_secrets: bool = False


def load_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ArchiveFormatError(f"{safe_text(label)} 不是有效的 JSON。") from None


def contains_secret_field(value: Any, depth: int = 0) -> bool:
    """``True`` when a mapping anywhere inside carries a field named like an API key (``api_key`` / ``apiKey`` ...)."""
    if depth > _MAX_JSON_DEPTH:
        return True  # nobody nests a provider list this deep: refuse rather than guess
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_FIELD.match(key):
                return True
            if contains_secret_field(item, depth + 1):
                return True
    elif isinstance(value, list):
        return any(contains_secret_field(item, depth + 1) for item in value)
    return False


def endpoint_of(provider: dict[str, Any]) -> dict[str, Any]:
    """The three fields of a provider that decide where a request goes (the key is sealed together with them)."""
    auto = provider.get("auto_append_path", True)
    endpoint: dict[str, Any] = {
        "protocol": provider.get("protocol") if isinstance(provider.get("protocol"), str) else "",
        "base_url": provider.get("base_url") if isinstance(provider.get("base_url"), str) else "",
        "auto_append_path": auto if isinstance(auto, bool) else True,
    }
    assert tuple(endpoint) == ENDPOINT_FIELDS
    return endpoint


def providers_of(data: Any, label: str = "providers.json") -> list[dict[str, Any]]:
    """The provider list of a ``providers.json`` (``{"schema_version", "providers": [...]}``) with every check."""
    items = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ArchiveFormatError(f"{label} 里没有 providers 列表。")
    if contains_secret_field(data):
        raise ArchiveFormatError(f"{label} 里含有 API Key 字段：API Key 只能放在加密的 secrets.enc 里。")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ArchiveFormatError(f"{label} 里有一个没有 id 的 Provider。")
        if item["id"] in seen:
            raise ArchiveFormatError(f"{label} 里的 Provider id 重复：{safe_text(item['id'])}")
        seen.add(item["id"])
    return items


def check_game(raw: bytes, game_id: str, *, label: str) -> dict[str, Any]:
    """A game file: JSON, save format v5, the ``GameSession`` model, and the directory's id.  Returns its facts."""
    from app.game.session import SESSION_SCHEMA_VERSION, GameSession

    payload = load_json(raw, label)
    if not isinstance(payload, dict):
        raise ArchiveFormatError(f"{safe_text(label)} 不是对局文件。")
    schema = payload.get("schema_version")
    if schema != SESSION_SCHEMA_VERSION:
        raise ArchiveFormatError(
            f"对局 {safe_text(game_id)} 的存档格式是 v{safe_text(schema, 12)}，需要 v{SESSION_SCHEMA_VERSION}"
            "（v0.12.1 的对局请先用 tools/migrate_v0121_to_archive.py 转换）。"
        )
    try:
        session = GameSession.model_validate(payload)
    except Exception:  # noqa: BLE001 - pydantic errors can echo the offending value: say where, not what
        raise ArchiveFormatError(f"对局 {safe_text(game_id)} 没有通过结构校验（{safe_text(label)}）。") from None
    if session.session_id != game_id:
        raise ArchiveFormatError(f"{safe_text(label)} 里的对局编号与目录名不一致。")
    world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
    state = world.get("state") if isinstance(world.get("state"), dict) else {}
    return {
        "session_id": session.session_id,
        "title": session.title,
        "scenario_title": session.scenario_title or session.scenario_name,
        "status": session.status,
        "winner": state.get("winner") if isinstance(state.get("winner"), str) else None,
        "created_at": session.created_at,
        "human_involved": bool(session.human_spans),
        "players": len(session.participants),
    }


def check_member(kind: str, game_id: str | None, name: str, raw: bytes | None, inventory: Inventory) -> None:
    """Check one member whose content is ``raw`` (``None`` for kinds that are only stored: the logs) and add it up."""
    if kind == KIND_LOG:
        if game_id:
            inventory.log_games.add(game_id)
        return
    if raw is None:
        raise ArchiveFormatError(f"{safe_text(name)} 没有读到内容。")
    if kind == KIND_SECRETS:
        parse_secrets_header(raw)
        inventory.has_secrets = True
        return
    if kind == KIND_ROSTER_TEXT:
        if len(raw) > _MAX_TEXT_BYTES:
            raise ArchiveFormatError(f"{safe_text(name)} 太大了。")
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ArchiveFormatError(f"{safe_text(name)} 不是 UTF-8 文本。") from None
        inventory.seat_ids.add(name.split("/", 1)[1].split(".", 1)[0])
        return
    if kind not in JSON_KINDS:
        return
    if kind in (KIND_GAME, KIND_GAME_PREV):
        assert game_id is not None
        facts = check_game(raw, game_id, label=name)
        if kind == KIND_GAME:
            inventory.games[game_id] = facts
        else:
            inventory.prev_games.add(game_id)
        return
    data = load_json(raw, name)
    if kind == KIND_PROVIDERS:
        items = providers_of(data, name)
        inventory.provider_ids = [str(item["id"]) for item in items]
        inventory.provider_names = [str(item.get("name") or item["id"]) for item in items]
        inventory.provider_endpoints = {str(item["id"]): endpoint_of(item) for item in items}
    elif kind == KIND_ROSTER_JSON:
        if not isinstance(data, dict):
            raise ArchiveFormatError(f"{safe_text(name)} 不是对象。")
        seats = data.get("seats")
        if isinstance(seats, dict):
            inventory.seat_ids.update(key for key in seats if isinstance(key, str) and _SEAT_ID.match(key))
    elif kind == KIND_GAMES_INDEX:
        if not isinstance(data, (dict, list)):
            raise ArchiveFormatError(f"{safe_text(name)} 的结构不对。")
    elif kind in (KIND_SETTINGS, KIND_PRICING):
        if not isinstance(data, dict):
            raise ArchiveFormatError(f"{safe_text(name)} 不是对象。")
