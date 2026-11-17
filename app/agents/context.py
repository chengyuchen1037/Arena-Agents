"""Text rendering for an agent's context: state board, memory, visible history and wake reasons.

Pure and deterministic given the world state.  The FULL message of a conversation is built from
``render_board`` + memory + history; every later message is a DELTA (new history entries, board changes
from ``app.agents.delta.diff_board``, wake reasons) so the conversation stays an append-only prefix.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.agents.sanitize import neutralize_untrusted_text as _safe
from app.world.types import ENGINE_ID, MODERATOR_ID, AgentMemory, Envelope, LedgerEntry

STATUS_ZH = {"idle": "未开始", "running": "进行中", "paused": "已暂停", "finished": "已结束"}
KIND_ZH = {
    "speech": "",
    "announce": "【公告】",
    "template": "",
    "challenge": "【质询】",
    "reply": "【质询回复】",
    "ability": "【能力】",
    "system": "【系统】",
}


def _g(data: Any, key: str, default: Any = None) -> Any:
    return data.get(key, default) if isinstance(data, dict) else default


def _secs(value: Any) -> str:
    try:
        return f"{float(value):.0f} 秒"
    except (TypeError, ValueError):
        return "不限时"


def _ids(items: Any) -> str:
    return "、".join(str(i) for i in (items or [])) or "（无）"


class Namer:
    """Resolves display names for ids and channels (best effort)."""

    def __init__(self, display_name: Callable[[str], str], channels: dict[str, str] | None = None) -> None:
        self._display = display_name
        self.channels = channels or {}

    def who(self, agent_id: str | None) -> str:
        """``<id> <name>`` for a player (the id is what tools take, the name is what people say)."""
        if not agent_id:
            return "?"
        if agent_id == MODERATOR_ID:
            return "主持人"
        if agent_id == ENGINE_ID:
            return "系统"
        name = self._display(agent_id)
        return agent_id if not name or name == agent_id else f"{agent_id} {name}"

    def whos(self, ids: Any) -> str:
        return "、".join(self.who(i) for i in (ids or [])) or "（无）"

    def channel(self, channel_id: str | None) -> str:
        if not channel_id:
            return "?"
        name = self.channels.get(channel_id)
        return f"{name}[{channel_id}]" if name and name != channel_id else str(channel_id)


# --------------------------------------------------------------------------------- board


def _render_floor(channel: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    floor = channel.get("floor") or {}
    kind = floor.get("kind", "none")
    if channel.get("can_write"):
        mine = floor.get("mine")
        if mine:
            lines.append(
                f"    ✅ 你现在可以发言：还可发 {mine.get('messages_left', '?')} 条，单条 ≤ "
                f"{mine.get('chars_per_message', '?')} 字，总共还可写 {mine.get('chars_left', '?')} 字，"
                f"剩余 {_secs(mine.get('seconds_left'))}"
            )
        else:
            free = floor.get("free") or {}
            left = free.get("my_messages_left")
            extra = f"，你还可发 {left} 条" if left is not None else ""
            lines.append(f"    ✅ 你现在可以发言{extra}")
    else:
        why = channel.get("why_not")
        lines.append(f"    ⛔ 你现在不能发言{('：' + str(why)) if why else ''}")
    if kind in ("turn", "queue"):
        tokens = floor.get("queue") or []
        if tokens:
            shown = "，".join(
                f"{t.get('holder')}({t.get('state')} {t.get('messages_used', 0)}/{t.get('max_messages', '?')})"
                for t in tokens
            )
            state = "（已暂停）" if floor.get("paused") else ""
            lines.append(f"    发言组 {kind}{state}：{shown}；当前发言者：{floor.get('active') or '（无）'}")
    elif kind == "free" and floor.get("free"):  # system DMs are "free" without a discussion to report
        free = floor.get("free") or {}
        lines.append(
            f"    自由讨论：已进行 {_secs(free.get('elapsed_s'))}/{_secs(free.get('duration_s'))}，"
            f"总字数 {free.get('chars_used', 0)}/{free.get('max_chars_total', '?')}，"
            f"已冷场 {_secs(free.get('silent_s'))}"
        )
    return lines


def request_flags(r: dict[str, Any]) -> str:
    """Short flags of a request line: shared answers / how it closes early."""
    flags = ""
    if r.get("share_answers"):
        flags += "　答案互相可见（可随时改答）"
    auto = r.get("auto_close")
    if auto == "unanimous":
        flags += "　全员答案一致才会提前关闭"
    elif auto == "deadline":
        flags += "　只在截止时关闭"
    return flags


def shared_answers_text(answers: list[dict[str, Any]], namer: Namer) -> str:
    return "；".join(
        f"{namer.who(a.get('player_id'))}→{a.get('label') or a.get('value')}" for a in answers
    ) or "（还没有人作答）"


def render_board(board: dict[str, Any], namer: Namer, *, hint: str = "") -> str:
    me = board.get("me") or {}
    is_mod = bool(me.get("is_moderator"))
    lines: list[str] = ["【状态板】"]
    if hint:
        lines.append(f"⚠ {hint}")
    if is_mod:
        lines.append(f"你是主持人（{me.get('id', 'moderator')}）。你知道所有玩家的底牌，请严格保密。")
    else:
        lines.append(f"你是 {me.get('id', '')} {me.get('name', '')}（座位 {me.get('seat', '')}）。")
        if me.get("brief"):
            lines.append(f"你的身份说明：{me['brief']}")
        if me.get("tags"):
            lines.append(f"你的标签：{_ids(me.get('tags'))}")
    lines.append(
        f"当前阶段：{board.get('phase') or '（未设置）'}　对局状态：{STATUS_ZH.get(str(board.get('status')), board.get('status'))}"
        f"　游戏时间：{_secs(board.get('now'))}"
    )

    lines.append("\n玩家：")
    for p in board.get("players") or []:
        parts = [f"- {p.get('id')} {p.get('name')}（座位 {p.get('seat')}）标签：{_ids(p.get('tags'))}"]
        if p.get("out"):
            parts.append("【已出局】")
        if is_mod and (p.get("role") or p.get("faction")):
            parts.append(f"｜身份 {p.get('role') or '?'} / 阵营 {p.get('faction') or '?'}")
        if is_mod and p.get("attributes"):
            parts.append(f"｜属性 {json.dumps(p['attributes'], ensure_ascii=False)}")
        lines.append(" ".join(parts))

    lines.append("\n频道（你是成员的）：")
    for ch in board.get("channels") or []:
        flags = "，已冻结" if ch.get("frozen") else ""
        flags += "，系统私聊" if ch.get("system") else ""
        members = ch.get("members") or []
        shown = _ids(members) if len(members) <= 14 else f"{len(members)} 人"
        lines.append(
            f"- {ch.get('id')}「{ch.get('name')}」类型 {ch.get('type')}，模式 {ch.get('mode')}{flags}；成员：{shown}"
        )
        if ch.get("readonly"):
            lines.append(f"    只读成员：{_ids(ch['readonly'])}")
        lines.extend(_render_floor(ch))

    requests = board.get("requests") or []
    lines.append("\n待处理的请求：" if not is_mod else "\n进行中的请求：")
    if not requests:
        lines.append("（无）")
    for r in requests:
        options = "；".join(f"{o.get('id')}={o.get('label') or o.get('id')}" for o in (r.get("options") or []))
        head = f"- {r.get('id')}（{r.get('kind')}）{r.get('question')}"
        if options:
            head += f" 选项：{options}"
        head += f"　剩余 {_secs(r.get('seconds_left'))}"
        if r.get("sealed"):
            head += "　密封"
        head += request_flags(r)
        if is_mod:
            head += f"　发给 {_ids(r.get('to'))}，已作答 {_ids(r.get('answered_by'))}"
        else:
            head += "　（你已作答，可改答）" if r.get("answered") else "　（你还没作答）"
        lines.append(head)
        if r.get("answers"):
            lines.append("    当前答案：" + shared_answers_text(r["answers"], namer))

    abilities = board.get("abilities") or []
    if abilities:
        lines.append("\n能力：")
        for a in abilities:
            state = "已启用" if a.get("enabled") else "未启用"
            extra = f"，你还可使用 {a.get('uses_left')} 次" if a.get("uses_left") is not None and not is_mod else ""
            if is_mod and a.get("allowed") is not None:
                extra += f"，限定持有者 {_ids(a.get('allowed'))}"
            lines.append(f"- {a.get('id')}（{state}{extra}）{a.get('description') or ''}")

    tickets = board.get("tickets") or []
    if tickets:
        lines.append("\n质询工单：")
        for t in tickets:
            lines.append(f"- {t.get('id')} 来自 {t.get('player_id')}（{t.get('status')}）{_safe(str(t.get('preview') or ''))}")

    dashboard = board.get("dashboard") or {}
    if dashboard.get("factions") or dashboard.get("tag_stats"):
        lines.append("\n对局面板：" + json.dumps(dashboard, ensure_ascii=False))
    return "\n".join(lines)


# --------------------------------------------------------------------------------- history


def render_entry(entry: LedgerEntry, namer: Namer, *, viewer: str) -> str | None:
    """One history line, or ``None`` for entries not worth showing."""
    t, d = entry.type, entry.data
    ch = namer.channel(entry.channel_id) if entry.channel_id else ""
    prefix = f"#{entry.seq}"
    if t == "message":
        kind = str(d.get("kind", "speech"))
        tag = KIND_ZH.get(kind, "")
        ticket = f"#{d['ticket_id']}" if d.get("ticket_id") else ""
        verdict = {"upheld": "成立", "rejected": "驳回"}.get(str(d.get("verdict")), "")
        who = namer.who(entry.actor_id)
        if kind == "challenge":
            tag = f"【质询{ticket}】"
        elif kind == "reply":
            tag = f"【质询回复{ticket}{('·' + verdict) if verdict else ''}】"
        cut = "（已被截断）" if d.get("truncated") else ""
        content = str(d.get("content", ""))
        if entry.actor_id not in (MODERATOR_ID, ENGINE_ID):
            content = _safe(content)  # a player's words are data, never structure
        return f"{prefix} [{ch}] {tag}{who}：{content}{cut}"
    if t == "channel.created":
        return f"{prefix} 〔系统〕频道「{d.get('name', ch)}」已创建（{d.get('type', '')}，成员：{namer.whos(d.get('members'))}）"
    if t == "channel.updated":
        changes = d.get("changes") or d
        return f"{prefix} 〔系统〕频道 {ch} 已更新：{json.dumps(changes, ensure_ascii=False)[:160]}"
    if t == "floor.opened":
        holders = d.get("holders") or d.get("order") or []
        return f"{prefix} 〔发言权〕{ch} 开放（{d.get('mode')}）：{_ids(holders)}"
    if t == "floor.granted":
        return f"{prefix} 〔发言权〕{ch} 轮到 {namer.who(d.get('holder'))} 发言"
    if t == "floor.token_ended":
        reason = {"used": "已发完", "pass": "放弃", "timeout": "超时", "revoked": "被收回"}.get(str(d.get("reason")), d.get("reason"))
        return f"{prefix} 〔发言权〕{ch} {namer.who(d.get('holder'))} 的发言结束（{reason}）"
    if t in ("floor.completed", "floor.closed", "floor.paused", "floor.resumed", "floor.frozen"):
        label = {"floor.completed": "发言全部结束", "floor.closed": "已关闭", "floor.paused": "已暂停", "floor.resumed": "已恢复", "floor.frozen": "已冻结"}[t]
        reason = f"（{d['reason']}）" if d.get("reason") else ""
        return f"{prefix} 〔发言权〕{ch} {label}{reason}"
    if t == "request.opened":
        options = "；".join(f"{o.get('id')}={o.get('label') or o.get('id')}" for o in (d.get("options") or []))
        return f"{prefix} 〔请求 {d.get('request_id')}〕{d.get('question', '')}{('（选项：' + options + '）') if options else ''}"
    if t == "request.answered":
        return f"{prefix} 〔请求 {d.get('request_id')}〕{namer.who(d.get('player_id'))} 作答：{_safe(str(d.get('value', '')))}"
    if t == "request.closed":
        answers = d.get("answers")
        shown = json.dumps(answers, ensure_ascii=False) if answers is not None else ""
        return (
            f"{prefix} 〔请求 {d.get('request_id')} 已关闭（{d.get('reason')}）〕答案：{shown}"
            f"{_tally_text(d)}"
            f"{('；使用默认：' + _ids(d.get('defaulted'))) if d.get('defaulted') else ''}"
            f"{('；未作答：' + _ids(d.get('missing'))) if d.get('missing') else ''}"
        )
    if t == "request.cancelled":
        return f"{prefix} 〔请求 {d.get('request_id')}〕已取消（{d.get('reason', '')}）"
    if t == "ability.used":
        return f"{prefix} 〔能力〕{namer.who(d.get('actor_id'))} 发动 {d.get('ability_id')}：{json.dumps(d.get('params') or {}, ensure_ascii=False)}"
    if t in ("ability.enabled", "ability.disabled"):
        return f"{prefix} 〔能力〕{d.get('ability_id')} {'已启用' if t.endswith('enabled') else '已关闭'}"
    if t == "player.updated":
        bits = []
        if d.get("reason") == "deal":
            bits.append(f"发牌：身份 {d.get('role')}／阵营 {d.get('faction')}")
        if d.get("add_tags"):
            bits.append("+" + "、".join(d["add_tags"]))
        if d.get("remove_tags"):
            bits.append("-" + "、".join(d["remove_tags"]))
        if d.get("set_attributes"):
            bits.append("属性 " + json.dumps(d["set_attributes"], ensure_ascii=False))
        return f"{prefix} 〔状态〕{namer.who(d.get('player_id'))}：{'，'.join(bits) or '更新'}（现标签：{_ids(d.get('tags_after'))}）"
    if t == "player.public_changed":
        bits = []
        if d.get("added"):
            bits.append("+" + "、".join(d["added"]))
        if d.get("removed"):
            bits.append("-" + "、".join(d["removed"]))
        return f"{prefix} 〔状态〕{namer.who(d.get('player_id'))} 的公开标签变化：{'，'.join(bits)}"
    if t == "phase.changed":
        return f"{prefix} 〔阶段〕{d.get('text', '')}"
    if t == "random.drawn":
        return f"{prefix} 〔随机〕{d.get('kind')} → {json.dumps(d.get('result'), ensure_ascii=False)}"
    if t == "game.status":
        extra = f"，胜者：{d.get('winner')}；{d.get('summary', '')}" if d.get("winner") else ""
        return f"{prefix} 〔对局〕{STATUS_ZH.get(str(d.get('status')), d.get('status'))}（{d.get('reason', '')}）{extra}"
    return None  # dashboard.updated and unknown types


def render_history(
    entries: list[LedgerEntry], namer: Namer, *, viewer: str, max_chars: int
) -> tuple[str, int]:
    """Newest-first budgeted history, returned oldest-first.  ``(text, omitted_entries)``."""
    lines: list[tuple[int, str]] = []
    for entry in entries:
        line = render_entry(entry, namer, viewer=viewer)
        if line:
            lines.append((entry.seq, line))
    kept: list[str] = []
    used = 0
    omitted = 0
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index][1]
        if used + len(line) + 1 > max_chars and kept:
            omitted = index + 1
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    head = f"（更早的 {omitted} 条历史因篇幅已省略）\n" if omitted else ""
    return head + "\n".join(kept), omitted


# ----------------------------------------------------------------------- memory / wake


def render_memory(memory: AgentMemory, notes_text: str) -> str:
    lines = ["【我的记忆】"]
    if memory.summary:
        lines.append(f"📦 早期历史摘要（第 {memory.compactions} 次压缩）：\n{memory.summary}")
    lines.append(notes_text)
    return "\n".join(lines)


MAX_WAKE_LINES = 14


def render_wake(reasons: list[Envelope], namer: Namer, *, agent_id: str, ambient: list[Envelope] | None = None) -> str:
    """Why the agent was woken.  New messages are summed up per channel (a busy channel would otherwise
    fill the section with one line per message); everything else gets a line of its own."""
    lines = ["【本次唤醒】"]
    per_channel: dict[str, tuple[list[str], int]] = {}
    others: list[str] = []
    for env in [*reasons, *(ambient or [])]:
        if env.payload.get("event") == "message.new":
            who = namer.who(env.sender)
            senders, count = per_channel.setdefault(str(env.channel_id or ""), ([], 0))
            if who not in senders:
                senders.append(who)
            per_channel[str(env.channel_id or "")] = (senders, count + 1)
            continue
        text = _wake_line(env, namer)
        if text:
            others.append(text)
    for channel_id, (senders, count) in per_channel.items():
        lines.append(f"- {namer.channel(channel_id or None)} 里有 {count} 条新消息（{'、'.join(senders)}）。")
    for text in others[:MAX_WAKE_LINES]:
        lines.append("- " + text)
    if len(others) > MAX_WAKE_LINES:
        lines.append(f"- …另有 {len(others) - MAX_WAKE_LINES} 件事（见可见历史与状态变化）。")
    if len(lines) == 1:
        lines.append("- （没有明确的唤醒原因，请查看状态变化与可见历史决定是否需要行动；没事就调用 sleep。）")
    return "\n".join(lines)


def _tally_text(data: dict[str, Any]) -> str:
    """Engine-computed vote count of a closed choice request (``tally`` / ``leaders``)."""
    tally = data.get("tally")
    if not tally:
        return ""
    counts = "、".join(f"{option} {count} 票" for option, count in tally.items())
    leaders = data.get("leaders") or []
    lead = f"；得票最高：{_ids(leaders)}" if leaders else "；无人得票"
    return f"；计票：{counts}{lead}"


_REVOKE_REASONS = {
    "timeout": "已超时",
    "closed": "主持人关闭了发言",
    "replaced": "被新的发言安排取代",
    "mode_changed": "频道模式被修改",
    "removed": "你已被移出该频道",
    "paused": "队列已暂停",
}


def _revoke_reason(reason: Any) -> str:
    text = str(reason or "")
    if text.startswith("ability:"):
        return f"能力 {text.split(':', 1)[1]} 被发动，频道冻结，等待主持人裁决"
    return _REVOKE_REASONS.get(text, text)


def _wake_line(env: Envelope, namer: Namer) -> str:
    event = str(env.payload.get("event", ""))
    p = env.payload
    ch = namer.channel(env.channel_id)
    if event == "message.new":
        return f"{namer.who(env.sender)} 在 {ch} 发了新消息。"
    if event == "challenge.opened":
        return f"玩家 {namer.who(env.sender)} 提出了质询 {p.get('ticket_id') or ''}，请尽快复核并用 resolve_challenge 处理。"
    if event == "challenge.resolved":
        return f"主持人回复了你的质询 {p.get('ticket_id') or ''}（见可见历史）。"
    if event == "floor.granted":
        pos = f"（第 {p['position']}/{p['total']} 位）" if p.get("position") else ""
        many = int(p.get("max_messages") or 1) > 1
        tail = (
            "说完请调用 pass 让出发言权，否则要等到限时结束，别人才能接着发言。"
            if many
            else "发言一条后发言权会自动交给下一位。"
        )
        return (
            f"你获得了 {ch} 的发言权{pos}：最多 {p.get('max_messages', '?')} 条，单条 ≤ "
            f"{p.get('max_chars_per_message', '?')} 字，总共 ≤ {p.get('max_chars_total', '?')} 字，{_secs(p.get('seconds'))} 内有效。{tail}"
        )
    if event in ("floor.revoked", "floor.paused", "floor.frozen"):
        label = {"floor.revoked": "被收回", "floor.paused": "被暂停", "floor.frozen": "被冻结"}[event]
        return f"你在 {ch} 的发言权{label}（{_revoke_reason(p.get('reason'))}）。"
    if event == "floor.completed":
        return f"{ch} 的发言权已全部结束：{json.dumps(p.get('results'), ensure_ascii=False)[:300]}。"
    if event == "free.advisory":
        why = {"time": "时间已到", "chars": "总字数已达上限", "silence": "已经冷场"}.get(str(p.get("reason")), p.get("reason"))
        return f"{ch} 的自由讨论{why}。引擎不会自动关闭自由讨论，请按手册的流程处理（需要结束时用 close_floor）。"
    if event == "request.opened":
        options = "；".join(f"{o.get('id')}={o.get('label') or o.get('id')}" for o in (p.get("options") or []))
        shared = "各收件人的答案会实时互相可见，可随时改答。" if p.get("share_answers") else ""
        return (
            f"你收到请求 {p.get('request_id')}：{p.get('question', '')}{('（选项：' + options + '）') if options else ''}，"
            f"{_secs(p.get('seconds'))} 内作答（respond）。{shared}"
        )
    if event == "request.answer":
        return (
            f"{namer.who(p.get('player_id'))} 对请求 {p.get('request_id')} 的答案是「{p.get('label') or p.get('value')}」"
            f"{('（理由：' + _safe(str(p.get('reason'))) + '）') if p.get('reason') else ''}。请求仍然开放，答案可以修改。"
        )
    if event == "timer.elapsed":
        return f"你设定的休眠时间到了（睡了 {float(p.get('slept_s') or 0):g} 秒）。看看现在该做什么。"
    if event == "request.closed":
        return (
            f"请求 {p.get('request_id')} 已关闭（{p.get('reason')}）。答案：{json.dumps(p.get('answers'), ensure_ascii=False)}"
            f"{_tally_text(p)}；默认：{_ids(p.get('defaulted'))}；未作答：{_ids(p.get('missing'))}。"
        )
    if event == "request.cancelled":
        return f"请求 {p.get('request_id')} 已{('过期' if p.get('reason') == 'expired' else '取消')}，不必再作答。"
    if event == "ability.used":
        return f"{namer.who(p.get('actor_id'))} 发动了能力 {p.get('ability_id')}：{json.dumps(p.get('params') or {}, ensure_ascii=False)}。请裁决并推进流程。"
    if event in ("game.started", "game.resumed", "game.continued"):
        return {
            "game.started": "对局开始了。请依据规则与主持人手册开局。",
            "game.resumed": "对局从暂停中恢复。请查看状态板与可见历史，确认当前进度后继续。",
            "game.continued": "对局在终局后被用户继续。请查看状态变化，决定如何继续（例如宣布继续、调整状态）。",
        }[event]
    if event == "watchdog":
        return "对局已经静止一段时间（没有任何人在行动、也没有计时器）。请检查流程是否漏了一步并推进；确实无法推进时再 hold_game。"
    if event in ("channel.created", "channel.updated", "player.updated", "phase.changed"):
        return f"状态变化：{event}（见状态变化与可见历史）。"
    return f"{event or env.kind}：{json.dumps(p, ensure_ascii=False)[:200]}"
