"""Handlers for the orchestration tools a moderator (or a scenario ``tool`` action) can call.

``MODERATOR_TOOL_HANDLERS`` maps a tool name to ``async handler(orch, name, args)`` which
returns ``(tool_result, interrupt_event_or_None)``.  Handlers that need to run
participants delegate to ``orch._handle_*`` / ``orch._run_participant_speak`` looked up
at call time, so they can be replaced on an instance (tests rely on this).

Error convention: a handler reports a failure with ``orch._tool_error(...)``, which also
records ``orch._last_tool_error`` so callers such as the scenario director can tell a
failed tool from a successful one.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.engine.participant_tasks import speech_tool_result
from app.engine.state import Channel, FactionStat, InterruptQuota, TagStat
from app.engine.toolargs import (
    arg_dict_list,
    arg_str,
    arg_str_list,
    strict_int,
)

Result = tuple[str, "str | None"]
Handler = Callable[[Any, str, dict[str, Any]], Awaitable[Result]]


async def _done(
    orch: Any, name: str, args: dict[str, Any], res: str, channel: str | None, event: str = "tool_executed"
) -> Result:
    orch._log_tool(name, args, res, channel)
    await orch.broadcast_state(event)
    return res, None


def _target_channel(args: dict[str, Any]) -> str | None:
    value = args.get("channel_id")
    return value if isinstance(value, str) and value else None


async def set_phase(orch: Any, name: str, args: dict[str, Any]) -> Result:
    phase_text = arg_str(args, "phase_text")
    orch.session.phase_text = phase_text
    return await _done(orch, name, args, f"阶段已更新为: {phase_text}", _target_channel(args))


async def update_player_tags(orch: Any, name: str, args: dict[str, Any]) -> Result:
    pid = arg_str(args, "player_id")
    tags = arg_str_list(args, "tags")
    participant = orch.session.get_participant(pid)
    if not participant:
        res = f"错误：未找到参与者 {pid}"
        return orch._tool_error(name, args, res, _target_channel(args))
    participant.tags = tags
    res = f"已将 {participant.name} ({pid}) 的标签更新为: {', '.join(tags)}"
    return await _done(orch, name, args, res, _target_channel(args))


async def batch_update_player_tags(orch: Any, name: str, args: dict[str, Any]) -> Result:
    updated: list[str] = []
    for item in arg_dict_list(args, "updates"):
        pid = arg_str(item, "player_id")
        tags = arg_str_list(item, "tags")
        participant = orch.session.get_participant(pid)
        if participant:
            participant.tags = tags
            updated.append(f"{participant.name}({pid}): [{', '.join(tags)}]")
    res = "已批量更新标签：" + "；".join(updated) if updated else "未匹配到有效的参与者更新项。"
    return await _done(orch, name, args, res, _target_channel(args))


async def update_dashboard(orch: Any, name: str, args: dict[str, Any]) -> Result:
    factions: list[FactionStat] | None = None
    stats: list[TagStat] | None = None
    # Parse everything first so a bad number leaves the dashboard untouched.
    if isinstance(args.get("factions"), list):
        factions = [
            FactionStat(
                name=arg_str(item, "name"),
                alive=strict_int(item.get("alive", 0), "factions[].alive"),
                total=strict_int(item.get("total", 0), "factions[].total"),
                variant=arg_str(item, "variant", "neutral"),
            )
            for item in arg_dict_list(args, "factions")
        ]
    if isinstance(args.get("tag_stats"), list):
        stats = [
            TagStat(
                name=arg_str(item, "name"),
                count=strict_int(item.get("count", 0), "tag_stats[].count"),
                variant=arg_str(item, "variant", "neutral"),
            )
            for item in arg_dict_list(args, "tag_stats")
        ]
    if factions is not None:
        orch.session.dashboard.factions = factions
    if stats is not None:
        orch.session.dashboard.tag_stats = stats
    return await _done(orch, name, args, "右侧对局状态面板已更新。", _target_channel(args))


async def create_channel(orch: Any, name: str, args: dict[str, Any]) -> Result:
    cid = arg_str(args, "channel_id")
    if not cid:
        return orch._tool_error(name, args, "错误：channel_id 不能为空。", None)
    cname = arg_str(args, "name", cid)
    ctype = arg_str(args, "type", "group")
    if ctype not in ("public", "group", "dm"):
        ctype = "group"
    members = arg_str_list(args, "members")
    if "moderator" not in members:
        members.insert(0, "moderator")
    status = arg_str(args, "status", "open")
    if status not in ("open", "closed"):
        status = "open"

    existing = orch.session.get_channel(cid)
    if existing:
        existing.name = cname
        existing.type = ctype  # type: ignore[assignment]
        existing.members = members
        existing.status = status  # type: ignore[assignment]
        verb = "已更新"
    else:
        orch.session.channels.append(
            Channel(id=cid, name=cname, type=ctype, status=status, members=members)  # type: ignore[arg-type]
        )
        verb = "已创建"
    res = f"频道 `{cid}` {verb}（成员: {', '.join(members)}，状态: {status}）。"
    return await _done(orch, name, args, res, cid)


async def update_channel(orch: Any, name: str, args: dict[str, Any]) -> Result:
    cid = arg_str(args, "channel_id")
    channel = orch.session.get_channel(cid)
    if not channel:
        return orch._tool_error(name, args, f"错误：频道 `{cid}` 不存在。", cid)
    if args.get("status") in ("open", "closed"):
        channel.status = args["status"]
    for member in arg_str_list(args, "add_members"):
        if member not in channel.members:
            channel.members.append(member)
    for member in arg_str_list(args, "remove_members"):
        if member in channel.members and member != "moderator":
            channel.members.remove(member)
    res = f"频道 `{cid}` 已更新（状态: {channel.status}，成员: {', '.join(channel.members)}）。"
    return await _done(orch, name, args, res, cid)


async def set_interrupt_quota(orch: Any, name: str, args: dict[str, Any]) -> Result:
    cid = arg_str(args, "channel_id", "public")
    pid = arg_str(args, "player_id")
    max_count = strict_int(args.get("max_count", 1), "max_count", minimum=0, maximum=1000)
    channel = orch.session.get_channel(cid)
    participant = orch.session.get_participant(pid)
    if not channel or not participant:
        return orch._tool_error(
            name, args, f"错误：未找到频道 `{cid}` 或参与者 `{pid}`。", cid
        )
    channel.interrupt_quotas[pid] = InterruptQuota(
        player_id=pid,
        player_name=participant.name,
        used=0,
        max_count=max_count,
        reason=arg_str(args, "reason"),
    )
    res = f"已在频道 `{cid}` 授予 {participant.name} ({pid}) 插话权 (0/{max_count})。"
    return await _done(orch, name, args, res, cid)


async def clear_interrupt_quota(orch: Any, name: str, args: dict[str, Any]) -> Result:
    cid = arg_str(args, "channel_id", "public")
    pid = arg_str(args, "player_id")
    channel = orch.session.get_channel(cid)
    if not channel:
        return orch._tool_error(name, args, f"错误：未找到频道 `{cid}`。", cid)
    if pid:
        channel.interrupt_quotas.pop(pid, None)
        res = f"已移除参与者 `{pid}` 在频道 `{cid}` 的插话权。"
    else:
        channel.interrupt_quotas.clear()
        res = f"已清空频道 `{cid}` 的所有插话权。"
    return await _done(orch, name, args, res, cid)


async def record_engine_note(orch: Any, name: str, args: dict[str, Any]) -> Result:
    orch.session.add_record(
        stream="engine", kind="note", actor_id="moderator", content=arg_str(args, "note")
    )
    return await _done(orch, name, args, "内部裁判笔记已记录。", None)


async def send_message(orch: Any, name: str, args: dict[str, Any]) -> Result:
    cid = arg_str(args, "channel_id", "public")
    content = arg_str(args, "content")
    if not orch.session.get_channel(cid):
        return orch._tool_error(
            name, args, f"错误：频道 `{cid}` 不存在，请先调用 create_channel 创建。", cid
        )
    orch.session.set_moderator_state(True, f"📢 向频道 {cid} 传输消息中...")
    orch.session.add_record(
        stream="channel", channel_id=cid, kind="message", actor_id="moderator", content=content
    )
    return await _done(orch, name, args, f"主持人消息已发送至 `{cid}`。", cid, "message_sent")


async def grant_speaking_turn(orch: Any, name: str, args: dict[str, Any]) -> Result:
    cid = arg_str(args, "channel_id", "public")
    pid = arg_str(args, "player_id")
    instruction = arg_str(args, "instruction")
    channel = orch.session.get_channel(cid)
    participant = orch.session.get_participant(pid)
    if not channel or not participant:
        return orch._tool_error(name, args, f"错误：未找到频道 `{cid}` 或参与者 `{pid}`。", cid)
    if channel.status != "open":
        return orch._tool_error(name, args, f"错误：频道 `{cid}` 未开放发言。", cid)
    if pid not in channel.members:
        return orch._tool_error(name, args, f"错误：参与者 `{pid}` 不是频道 `{cid}` 的成员。", cid)
    orch.session.set_moderator_state(True, f"🎙️ 移交发言权给 {participant.name} ({cid})...")
    speech, _ = await orch._run_participant_speak(participant.id, cid, instruction)
    res = speech_tool_result(participant.name, participant.id, cid, speech)
    orch._log_tool(name, {"channel_id": cid, "player_id": pid}, res, cid)
    return res, None


async def end_game(orch: Any, name: str, args: dict[str, Any]) -> Result:
    session = orch.session
    winner = arg_str(args, "winner_faction")
    summary = arg_str(args, "summary")
    session.winner_faction = winner
    session.phase_text = f"对局结束 · {winner} 获胜"
    session.status = "finished"
    session.clear_all_generating()
    session.active_status_text = f"已结束 · {winner} 获胜"
    session.set_moderator_state(False, f"🏁 对局已结束 ({winner} 获胜)")
    if session.get_channel("public"):
        session.add_record(
            stream="channel",
            channel_id="public",
            kind="message",
            actor_id="moderator",
            content=f"🏆 【对局结束 · {winner} 获胜】\n{summary}",
        )
    return await _done(orch, name, args, f"对局已宣告结束，获胜方：{winner}", "public", "game_ended")


def _delegate(method: str) -> Handler:
    async def handler(orch: Any, name: str, args: dict[str, Any]) -> Result:
        return await getattr(orch, method)(args)

    return handler


MODERATOR_TOOL_HANDLERS: dict[str, Handler] = {
    "set_phase": set_phase,
    "update_player_tags": update_player_tags,
    "batch_update_player_tags": batch_update_player_tags,
    "update_dashboard": update_dashboard,
    "create_channel": create_channel,
    "update_channel": update_channel,
    "set_interrupt_quota": set_interrupt_quota,
    "clear_interrupt_quota": clear_interrupt_quota,
    "record_engine_note": record_engine_note,
    "send_message": send_message,
    "grant_speaking_turn": grant_speaking_turn,
    "request_player_speak": grant_speaking_turn,
    "end_game": end_game,
    # Long-running participant interactions live on the orchestrator (patchable per instance).
    "request_players_strategic_planning": _delegate("_handle_request_players_strategic_planning"),
    "request_choices": _delegate("_handle_request_choices"),
    "ask_yes_or_no": _delegate("_handle_ask_yes_or_no"),
    "request_player_interrupt": _delegate("_handle_request_player_interrupt"),
    "request_players_concurrent": _delegate("_handle_request_players_concurrent"),
    "request_players_vote": _delegate("_handle_request_players_vote"),
}

__all__ = ["MODERATOR_TOOL_HANDLERS"]
