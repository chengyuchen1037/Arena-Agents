"""What changed on an agent's state board between two snapshots (the ``【状态变化】`` of a DELTA message).

v0.12.1 keeps one append-only conversation per agent, so a later message must not repeat the whole
board.  Only *actionable* differences are reported - whether the agent may speak, its floor, requests,
abilities, its own tags - never clocks or counters that change all the time.  Everything else the
agent learns from the visible history entries of the same message.
"""

from __future__ import annotations

import json
from typing import Any

from app.agents.context import (
    STATUS_ZH,
    Namer,
    _ids,
    _render_floor,
    _safe,
    _secs,
    request_flags,
    shared_answers_text,
)


def _by_id(items: Any) -> dict[str, dict[str, Any]]:
    return {str(i.get("id")): i for i in (items or []) if isinstance(i, dict) and i.get("id") is not None}


def _floor_signature(channel: dict[str, Any]) -> tuple[Any, ...]:
    """What about a channel's floor matters for *acting* (not the seconds that keep ticking)."""
    floor = channel.get("floor") or {}
    mine = floor.get("mine") or {}
    free = floor.get("free") or {}
    return (
        bool(channel.get("can_write")),
        str(channel.get("why_not") or ""),
        floor.get("kind"),
        bool(floor.get("paused")),
        bool(floor.get("mine")),
        mine.get("messages_left"),
        free.get("my_messages_left"),
        tuple(free.get("writers") or ()) if free.get("writers") is not None else None,
    )


def _channel_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    changes: list[str] = []
    if old.get("name") != new.get("name"):
        changes.append(f"改名为「{new.get('name')}」")
    if old.get("mode") != new.get("mode"):
        changes.append(f"模式 {old.get('mode')} → {new.get('mode')}")
    if bool(old.get("frozen")) != bool(new.get("frozen")):
        changes.append("已冻结，等待主持人裁决" if new.get("frozen") else "已解除冻结")
    old_members, new_members = list(old.get("members") or []), list(new.get("members") or [])
    if old_members != new_members:
        added = [m for m in new_members if m not in old_members]
        removed = [m for m in old_members if m not in new_members]
        bits = []
        if added:
            bits.append("+" + "、".join(added))
        if removed:
            bits.append("-" + "、".join(removed))
        changes.append("成员 " + " ".join(bits))
    if list(old.get("readonly") or []) != list(new.get("readonly") or []):
        changes.append(f"只读成员：{_ids(new.get('readonly'))}")
    return changes


def _request_line(r: dict[str, Any], namer: Namer, *, is_mod: bool) -> str:
    options = "；".join(f"{o.get('id')}={o.get('label') or o.get('id')}" for o in (r.get("options") or []))
    head = f"- 新请求 {r.get('id')}（{r.get('kind')}）{r.get('question')}"
    if options:
        head += f" 选项：{options}"
    head += f"　剩余 {_secs(r.get('seconds_left'))}"
    if r.get("sealed"):
        head += "　密封"
    head += request_flags(r)
    if is_mod:
        head += f"　发给 {_ids(r.get('to'))}"
    elif r.get("answered"):
        head += "　（你已作答）"
    return head


def _request_changes(old: dict[str, Any], new: dict[str, Any], namer: Namer, *, is_mod: bool) -> list[str]:
    changes: list[str] = []
    if is_mod:
        if list(old.get("answered_by") or []) != list(new.get("answered_by") or []):
            changes.append(f"已作答：{namer.whos(new.get('answered_by'))}")
    else:
        if bool(old.get("answered")) != bool(new.get("answered")):
            changes.append("你已作答" if new.get("answered") else "你的答案被撤销")
        if (old.get("my_answer") or {}).get("value") != (new.get("my_answer") or {}).get("value") and new.get("my_answer"):
            changes.append(f"你的答案：{new['my_answer'].get('value')}")
    if new.get("share_answers") and (old.get("answers") or []) != (new.get("answers") or []):
        changes.append("当前答案：" + shared_answers_text(new.get("answers") or [], namer))
    return changes


def diff_board(old: dict[str, Any], new: dict[str, Any], namer: Namer) -> list[str]:
    """Lines describing what changed from ``old`` to ``new`` (empty when nothing actionable changed)."""
    lines: list[str] = []
    me_old, me_new = old.get("me") or {}, new.get("me") or {}
    is_mod = bool(me_new.get("is_moderator"))

    if old.get("status") != new.get("status"):
        lines.append(
            f"对局状态：{STATUS_ZH.get(str(old.get('status')), old.get('status'))} → "
            f"{STATUS_ZH.get(str(new.get('status')), new.get('status'))}"
        )
    if not is_mod:
        if list(me_old.get("tags") or []) != list(me_new.get("tags") or []):
            lines.append(f"你的标签：{_ids(me_old.get('tags'))} → {_ids(me_new.get('tags'))}")
        if (me_old.get("attributes") or {}) != (me_new.get("attributes") or {}):
            lines.append("你的属性：" + json.dumps(me_new.get("attributes") or {}, ensure_ascii=False))
        if me_old.get("brief") != me_new.get("brief") and me_new.get("brief"):
            lines.append(f"你的身份说明更新为：{me_new['brief']}")

    # ------------------------------------------------------------------------ channels
    old_channels, new_channels = _by_id(old.get("channels")), _by_id(new.get("channels"))
    for channel_id, channel in new_channels.items():
        previous = old_channels.get(channel_id)
        if previous is None:
            members = channel.get("members") or []
            shown = _ids(members) if len(members) <= 14 else f"{len(members)} 人"
            lines.append(
                f"- 新频道 {channel_id}「{channel.get('name')}」类型 {channel.get('type')}，"
                f"模式 {channel.get('mode')}；成员：{shown}"
            )
            lines.extend(_render_floor(channel))
            continue
        changes = _channel_changes(previous, channel)
        floor_changed = _floor_signature(previous) != _floor_signature(channel)
        if changes or floor_changed:
            lines.append(f"- {channel_id}「{channel.get('name')}」" + (("：" + "；".join(changes)) if changes else "："))
            lines.extend(_render_floor(channel))
    for channel_id in old_channels:
        if channel_id not in new_channels:
            lines.append(f"- 你已不在频道 {channel_id} 中")

    # ------------------------------------------------------------------------ requests
    old_requests, new_requests = _by_id(old.get("requests")), _by_id(new.get("requests"))
    for request_id, request in new_requests.items():
        previous = old_requests.get(request_id)
        if previous is None:
            lines.append(_request_line(request, namer, is_mod=is_mod))
            if request.get("answers"):
                lines.append("    当前答案：" + shared_answers_text(request["answers"], namer))
            continue
        changes = _request_changes(previous, request, namer, is_mod=is_mod)
        if changes:
            lines.append(f"- 请求 {request_id}：" + "；".join(changes))
    for request_id in old_requests:
        if request_id not in new_requests:
            lines.append(f"- 请求 {request_id} 已结束，不再待处理")

    # ------------------------------------------------------------------------ abilities
    old_abilities, new_abilities = _by_id(old.get("abilities")), _by_id(new.get("abilities"))
    for ability_id, ability in new_abilities.items():
        previous = old_abilities.get(ability_id)
        signature = (ability.get("enabled"), ability.get("uses_left"), json.dumps(ability.get("allowed"), sort_keys=True))
        if previous is None or signature != (
            previous.get("enabled"),
            previous.get("uses_left"),
            json.dumps(previous.get("allowed"), sort_keys=True),
        ):
            state = "已启用" if ability.get("enabled") else "未启用"
            extra = f"，你还可使用 {ability.get('uses_left')} 次" if ability.get("uses_left") is not None and not is_mod else ""
            if is_mod and ability.get("allowed") is not None:
                extra += f"，限定持有者 {_ids(ability.get('allowed'))}"
            lines.append(f"- 能力 {ability_id}：{state}{extra}")
    for ability_id in old_abilities:
        if ability_id not in new_abilities:
            lines.append(f"- 你不再持有能力 {ability_id}")

    # ------------------------------------------------------------------------ tickets / dashboard
    old_tickets, new_tickets = _by_id(old.get("tickets")), _by_id(new.get("tickets"))
    for ticket_id, ticket in new_tickets.items():
        previous = old_tickets.get(ticket_id)
        if previous is None:
            lines.append(f"- 新质询工单 {ticket_id} 来自 {namer.who(ticket.get('player_id'))}（{ticket.get('status')}）{_safe(str(ticket.get('preview') or ''))}")
        elif previous.get("status") != ticket.get("status"):
            lines.append(f"- 质询工单 {ticket_id}：{previous.get('status')} → {ticket.get('status')}")
    if is_mod and (old.get("dashboard") or {}) != (new.get("dashboard") or {}):
        dashboard = new.get("dashboard") or {}
        if dashboard.get("factions") or dashboard.get("tag_stats"):
            lines.append("对局面板：" + json.dumps(dashboard, ensure_ascii=False))
    return lines
