#!/usr/bin/env python
"""Mock UI server for the v0.12 front end (docs/v0.12-contracts.md §8: SSE v2 + PublicSession v2).

It serves ``static/`` and replays one scripted 12-player game (about 80 s) over the SAME wire format
the real backend uses, so the front end can be developed, screenshotted and tested without any
Provider, LLM or game engine:

    PYTHONUTF8=1 .venv/Scripts/python.exe tools/mock_ui_server.py            # http://127.0.0.1:8731/
    PYTHONUTF8=1 .venv/Scripts/python.exe tools/mock_ui_server.py --speed 3  # 3x faster
    PYTHONUTF8=1 .venv/Scripts/python.exe tools/mock_ui_server.py --selfcheck

Click "开始" in the page (or ``POST /api/control/start``) and the script plays: channels are created,
the moderator announces, a queue of players speaks in turn (``floor.changed`` advances), a free
discussion, a sealed vote with progress, challenge tickets, an ability with a public declaration and a
frozen channel, players going out, ``agent.status`` / ``agent.metrics`` changes and finally
``game.status = finished``.  Pause / resume stop and continue the replay (the clock stops, D18); after
the verdict "继续" replays an epilogue (D8).

Wire format (contract §8.2): ``GET /api/events`` is ``text/event-stream``; every message is
``id: <event_id>`` + ``data: {"id": N, "type": "...", "data": {...}}`` (no ``event:`` field); the first
message is a ``snapshot``; a reconnect with ``Last-Event-ID`` (or ``?last_event_id=``) is resumed from a
ring buffer when possible, otherwise a ``snapshot`` is sent first; a ``: heartbeat`` comment every 15 s.

Besides the contract endpoints there are a few mock-only helpers under ``/api/mock/*`` (hold the game,
jump to the end, drop all SSE connections, ...); see ``create_app``.

``validate_event`` checks an event against the §8.2 shapes: tests (and the lead, when wiring the real
backend) can reuse it.
"""

import argparse
import asyncio
import copy
import json
import random
import sys
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
DEFAULT_PORT = 8731
APP_VERSION = "v0.12.0-mock"
PACE = 1.6  # a little slower than a real LLM so a human can follow the replay (speed=1 => ~80 s)
MODERATOR = "moderator"
ENGINE = "engine"

# --------------------------------------------------------------------------------------------------
# the cast
# --------------------------------------------------------------------------------------------------

NAMES = ["Alice 1", "Bob 2", "Carol 3", "David 4", "Eve 5", "Frank 6", "Grace 7", "Henry 8", "Ivy 9", "Jack 10", "Kate 11", "Leo 12"]
PLAYER_IDS = [f"p{i:02d}" for i in range(1, 13)]
NAME_OF = dict(zip(PLAYER_IDS, NAMES))
ROLES = {
    "p01": "平民",
    "p02": "平民",
    "p03": "狼人",
    "p04": "预言家",
    "p05": "女巫",
    "p06": "狼人",
    "p07": "猎人",
    "p08": "守卫",
    "p09": "平民",
    "p10": "狼人",
    "p11": "平民",
    "p12": "白狼王",
}
WOLVES = ["p03", "p06", "p10", "p12"]
GODS = ["p04", "p05", "p07", "p08"]
VILLAGERS = ["p01", "p02", "p09", "p11"]
EVERYONE = [MODERATOR, *PLAYER_IDS]

PUBLIC_TAGS = ["存活", "出局", "警长"]
UI = {"out_tags": ["出局"], "status_tags": ["存活", "出局"], "highlight_tags": ["警长"]}
TAG_STYLES = {
    "狼人": {"variant": "danger"},
    "白狼王": {"variant": "danger"},
    "预言家": {"variant": "purple"},
    "女巫": {"variant": "purple"},
    "猎人": {"variant": "warning"},
    "守卫": {"variant": "success"},
    "平民": {"variant": "neutral"},
    "存活": {"variant": "success"},
    "出局": {"variant": "muted"},
    "警长": {"variant": "warning"},
}

PROVIDERS = [
    {
        "id": "mock-deepseek",
        "name": "DeepSeek（模拟）",
        "protocol": "openai_chat",
        "base_url": "https://api.deepseek.example.com",
        "api_key_set": True,
        "read_only": False,
        "auto_append_path": True,
        "models": [
            {"id": "deepseek-chat", "input_price_per_m": 0.27, "output_price_per_m": 1.1, "context_window_tokens": 128000},
            {"id": "deepseek-flash", "input_price_per_m": 0.1, "output_price_per_m": 0.4, "context_window_tokens": 128000},
        ],
    },
    {
        "id": "mock-claude",
        "name": "Claude（模拟）",
        "protocol": "anthropic_messages",
        "base_url": "https://api.anthropic.example.com",
        "api_key_set": True,
        "read_only": False,
        "auto_append_path": True,
        "models": [
            {"id": "claude-sonnet", "input_price_per_m": 3.0, "output_price_per_m": 15.0, "context_window_tokens": 200000},
        ],
    },
]
PRICE = {m["id"]: (m["input_price_per_m"], m["output_price_per_m"]) for p in PROVIDERS for m in p["models"]}

# a long speech: the engine cuts it at the per-message limit (D19) and the feed shows a trailing "…"
LONG_SPEECH = (
    "我是猎人。昨晚没有任何信息，但我想先把场上的发言逻辑理一遍：p01 的发言太圆滑，几乎没有任何立场；"
    "p02 只是附和，没有独立判断；p09 一直在转移话题。从票型和发言顺序看，p10 的站边最不自然，"
    "我怀疑他是在冲锋之后收手。我愿意把枪口对准 p10，也请预言家尽快报出查验结果，给好人一个明确的方向。"
    "如果今天投不出狼，晚上我们就会被动。请大家认真对待这一轮的归票，不要被带节奏，也不要随意弃票。"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------------------------------
# contract shapes (§8.1) and a validator for the §8.2 events
# --------------------------------------------------------------------------------------------------

MSG_KINDS = {"speech", "announce", "template", "challenge", "reply", "ability", "system"}
AGENT_STATES = {"idle", "thinking", "acting", "stopped"}
GAME_STATUSES = {"idle", "running", "paused", "finished"}
FLOOR_KINDS = {"turn", "queue", "free", "none"}
CHANNEL_MODES = {"closed", "turn", "queue", "free", "listen"}
SESSION_UPDATED_KEYS = {
    "status",
    "pause_reason",
    "hold_reason",
    "winner",
    "summary",
    "active_status_text",
    "model_bindings",
    "binding_issues",
    "restored_from_save",
    "restore_notice",
    "autosave_error",
    "execution_depth",
    "title",
}
METRIC_FIELDS = ("calls", "prompt_tokens", "completion_tokens", "cost_usd", "tool_calls", "thinking_rounds", "rejections")


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _opt_str(v: Any) -> bool:
    return v is None or isinstance(v, str)


def _str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def validate_floor(floor: Any, where: str = "floor") -> list[str]:
    errs: list[str] = []
    if not isinstance(floor, dict):
        return [f"{where}: not an object"]
    if floor.get("kind") not in FLOOR_KINDS:
        errs.append(f"{where}.kind: {floor.get('kind')!r}")
    if not _opt_str(floor.get("active")):
        errs.append(f"{where}.active")
    if not isinstance(floor.get("paused"), bool):
        errs.append(f"{where}.paused")
    if floor.get("seconds_left") is not None and not _is_num(floor.get("seconds_left")):
        errs.append(f"{where}.seconds_left")
    queue = floor.get("queue")
    if not isinstance(queue, list):
        errs.append(f"{where}.queue")
    else:
        for i, q in enumerate(queue):
            if not (isinstance(q, dict) and isinstance(q.get("holder"), str) and q.get("state") in {"pending", "active", "done"}
                    and _is_int(q.get("messages_used")) and _is_int(q.get("max_messages"))):
                errs.append(f"{where}.queue[{i}]")
    free = floor.get("free")
    if free is not None:
        if not isinstance(free, dict):
            errs.append(f"{where}.free")
        else:
            for k in ("chars_used", "max_chars_total", "elapsed_s", "duration_s", "silent_s"):
                if not _is_num(free.get(k)):
                    errs.append(f"{where}.free.{k}")
            if not isinstance(free.get("message_counts"), dict):
                errs.append(f"{where}.free.message_counts")
    return errs


def validate_channel(ch: Any, where: str = "channel") -> list[str]:
    errs: list[str] = []
    if not isinstance(ch, dict):
        return [f"{where}: not an object"]
    for k in ("id", "name", "type"):
        if not isinstance(ch.get(k), str) or not ch.get(k):
            errs.append(f"{where}.{k}")
    if ch.get("mode") not in CHANNEL_MODES:
        errs.append(f"{where}.mode: {ch.get('mode')!r}")
    for k in ("members", "readonly"):
        if not _str_list(ch.get(k)):
            errs.append(f"{where}.{k}")
    for k in ("frozen", "system"):
        if not isinstance(ch.get(k), bool):
            errs.append(f"{where}.{k}")
    if not _opt_str(ch.get("owner")):
        errs.append(f"{where}.owner")
    errs += validate_floor(ch.get("floor"), f"{where}.floor")
    return errs


def validate_message(m: Any, where: str = "message") -> list[str]:
    errs: list[str] = []
    if not isinstance(m, dict):
        return [f"{where}: not an object"]
    if not _is_int(m.get("seq")) or m["seq"] <= 0:
        errs.append(f"{where}.seq")
    if not isinstance(m.get("ts"), str) or not m.get("ts"):
        errs.append(f"{where}.ts")
    if not isinstance(m.get("channel_id"), str):
        errs.append(f"{where}.channel_id")
    for k in ("from", "from_name", "content"):
        if not isinstance(m.get(k), str):
            errs.append(f"{where}.{k}")
    if m.get("kind") not in MSG_KINDS:
        errs.append(f"{where}.kind: {m.get('kind')!r}")
    for k in ("template_id", "ticket_id"):
        if not _opt_str(m.get(k)):
            errs.append(f"{where}.{k}")
    if m.get("truncated") is not None and not (
        isinstance(m["truncated"], dict) and _is_int(m["truncated"].get("dropped_chars"))
    ):
        errs.append(f"{where}.truncated")
    if not _str_list(m.get("visible_to_ids")):
        errs.append(f"{where}.visible_to_ids")
    # the contract forbids any chain-of-thought / tool detail on the wire
    for forbidden in ("thought", "thoughts", "reasoning", "tool_calls", "tool_name", "tool_args", "tool_result"):
        if forbidden in m:
            errs.append(f"{where}.{forbidden}: forbidden field")
    return errs


def validate_request(r: Any, where: str = "request") -> list[str]:
    errs: list[str] = []
    if not isinstance(r, dict):
        return [f"{where}: not an object"]
    if not isinstance(r.get("id"), str):
        errs.append(f"{where}.id")
    if r.get("kind") not in {"choice", "text"}:
        errs.append(f"{where}.kind")
    if not isinstance(r.get("question"), str):
        errs.append(f"{where}.question")
    opts = r.get("options")
    if not (isinstance(opts, list) and all(isinstance(o, dict) and isinstance(o.get("id"), str) and isinstance(o.get("label"), str) for o in opts)):
        errs.append(f"{where}.options")
    if not _str_list(r.get("to")):
        errs.append(f"{where}.to")
    if not _str_list(r.get("answered_by")):
        errs.append(f"{where}.answered_by")
    if r.get("status") not in {"open", "closed", "cancelled"}:
        errs.append(f"{where}.status")
    if not isinstance(r.get("sealed"), bool):
        errs.append(f"{where}.sealed")
    if r.get("seconds_left") is not None and not _is_num(r.get("seconds_left")):
        errs.append(f"{where}.seconds_left")
    return errs


def validate_ticket(t: Any, where: str = "ticket") -> list[str]:
    errs: list[str] = []
    if not isinstance(t, dict):
        return [f"{where}: not an object"]
    for k in ("id", "player_id", "preview"):
        if not isinstance(t.get(k), str):
            errs.append(f"{where}.{k}")
    if t.get("status") not in {"open", "resolved"}:
        errs.append(f"{where}.status")
    if t.get("verdict") not in {None, "upheld", "rejected"}:
        errs.append(f"{where}.verdict")
    if not _is_int(t.get("opened_seq")):
        errs.append(f"{where}.opened_seq")
    return errs


def validate_session(s: Any) -> list[str]:
    """PublicSession v2 (§8.1)."""
    errs: list[str] = []
    if not isinstance(s, dict):
        return ["session: not an object"]
    for k in ("session_id", "scenario_name", "phase_text", "active_status_text", "summary", "hold_reason", "pause_reason"):
        if not isinstance(s.get(k), str):
            errs.append(f"session.{k}")
    if s.get("status") not in GAME_STATUSES:
        errs.append("session.status")
    if s.get("pause_reason") not in {"user", "hold", "error", "stall", ""}:
        errs.append("session.pause_reason")
    for k in ("seq", "event_id"):
        if not _is_int(s.get(k)):
            errs.append(f"session.{k}")
    if not isinstance(s.get("participants"), list):
        errs.append("session.participants")
    else:
        for i, p in enumerate(s["participants"]):
            if not (isinstance(p, dict) and isinstance(p.get("id"), str) and _is_int(p.get("seat")) and isinstance(p.get("name"), str)
                    and _str_list(p.get("tags")) and isinstance(p.get("out"), bool) and p.get("state") in AGENT_STATES):
                errs.append(f"session.participants[{i}]")
    mod = s.get("moderator")
    if not (isinstance(mod, dict) and mod.get("id") == MODERATOR and mod.get("state") in AGENT_STATES):
        errs.append("session.moderator")
    for i, c in enumerate(s.get("channels") or []):
        errs += validate_channel(c, f"session.channels[{i}]")
    for i, m in enumerate(s.get("messages") or []):
        errs += validate_message(m, f"session.messages[{i}]")
    for i, r in enumerate(s.get("requests") or []):
        errs += validate_request(r, f"session.requests[{i}]")
    for i, t in enumerate(s.get("tickets") or []):
        errs += validate_ticket(t, f"session.tickets[{i}]")
    dash = s.get("dashboard")
    if not (isinstance(dash, dict) and isinstance(dash.get("factions"), list) and isinstance(dash.get("tag_stats"), list)):
        errs.append("session.dashboard")
    for k, kind in (("public_tags", list), ("tag_styles", dict), ("ui", dict), ("usage_stats", dict), ("model_bindings", dict), ("binding_issues", list)):
        if not isinstance(s.get(k), kind):
            errs.append(f"session.{k}")
    usage = s.get("usage_stats") or {}
    for k in ("prompt_tokens", "completion_tokens", "total_tokens", "total_cost_usd"):
        if not _is_num(usage.get(k)):
            errs.append(f"session.usage_stats.{k}")
    for aid, m in (usage.get("by_actor") or {}).items():
        if not (isinstance(m, dict) and all(_is_num(m.get(k)) for k in METRIC_FIELDS)):
            errs.append(f"session.usage_stats.by_actor.{aid}")
    return errs


def validate_event(ev: Any) -> list[str]:
    """Problems of one SSE v2 payload ``{"id", "type", "data"}`` against contract §8.2 (empty = valid)."""
    if not isinstance(ev, dict):
        return ["event: not an object"]
    errs: list[str] = []
    if not _is_int(ev.get("id")) or ev["id"] < 0:
        errs.append("event.id")
    etype = ev.get("type")
    data = ev.get("data")
    if not isinstance(etype, str):
        return errs + ["event.type"]
    if not isinstance(data, dict):
        return errs + ["event.data"]
    w = etype
    if etype == "snapshot":
        errs += validate_session(data.get("session"))
    elif etype == "message.committed":
        errs += validate_message(data, w)
    elif etype in ("channel.created", "channel.updated"):
        errs += validate_channel(data.get("channel"), f"{w}.channel")
    elif etype == "floor.changed":
        if not isinstance(data.get("channel_id"), str):
            errs.append(f"{w}.channel_id")
        errs += validate_floor(data.get("floor"), f"{w}.floor")
    elif etype == "player.updated":
        if not (isinstance(data.get("player_id"), str) and _str_list(data.get("tags")) and isinstance(data.get("out"), bool)):
            errs.append(w)
    elif etype == "request.opened":
        errs += validate_request(data.get("request"), f"{w}.request")
    elif etype == "request.progress":
        if not (isinstance(data.get("request_id"), str) and _str_list(data.get("answered_by")) and _is_int(data.get("total"))):
            errs.append(w)
        if set(data) - {"request_id", "answered_by", "total"}:
            errs.append(f"{w}: carries extra keys (answers must not be sent)")
    elif etype == "request.closed":
        if not (isinstance(data.get("request_id"), str) and data.get("reason") in {"all_answered", "deadline", "cancelled"}
                and _str_list(data.get("answered_by")) and _str_list(data.get("defaulted"))):
            errs.append(w)
    elif etype == "challenge.opened":
        errs += validate_ticket(data.get("ticket"), f"{w}.ticket")
    elif etype == "challenge.resolved":
        if not (isinstance(data.get("ticket_id"), str) and data.get("verdict") in {"upheld", "rejected"}):
            errs.append(w)
    elif etype == "ability.used":
        if not (isinstance(data.get("ability_id"), str) and isinstance(data.get("actor_id"), str)):
            errs.append(w)
    elif etype == "phase.changed":
        if not isinstance(data.get("text"), str):
            errs.append(w)
    elif etype == "dashboard.updated":
        d = data.get("dashboard")
        if not (isinstance(d, dict) and isinstance(d.get("factions"), list) and isinstance(d.get("tag_stats"), list)):
            errs.append(w)
    elif etype == "game.status":
        if data.get("status") not in GAME_STATUSES or "reason" not in data or "winner" not in data:
            errs.append(w)
    elif etype == "agent.status":
        if not (isinstance(data.get("id"), str) and data.get("state") in AGENT_STATES):
            errs.append(w)
    elif etype == "agent.metrics":
        if not isinstance(data.get("id"), str) or not all(_is_num(data.get(k)) for k in METRIC_FIELDS):
            errs.append(w)
    elif etype == "session.updated":
        if not data or set(data) - SESSION_UPDATED_KEYS:
            errs.append(f"{w}: keys {sorted(set(data) - SESSION_UPDATED_KEYS)}")
    elif etype == "workspace.changed":
        if data != {}:
            errs.append(w)
    elif etype == "error":
        if not isinstance(data.get("message"), str):
            errs.append(w)
    else:
        errs.append(f"event.type: unknown {etype!r}")
    return errs


# --------------------------------------------------------------------------------------------------
# event bus (ring buffer + resume), same behaviour as app/runtime/bus.py is specified to have
# --------------------------------------------------------------------------------------------------


class EventBus:
    def __init__(self, ring: int = 2000) -> None:
        self.ring: deque[dict[str, Any]] = deque(maxlen=ring)
        self.latest_id = 0
        self.subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()

    def publish(self, etype: str, data: dict[str, Any]) -> dict[str, Any]:
        self.latest_id += 1
        ev = {"id": self.latest_id, "type": etype, "data": data}
        self.ring.append(ev)
        for q in list(self.subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:  # a stuck client: drop it, it will reconnect and get a snapshot
                self.subscribers.discard(q)
        return ev

    def subscribe(self, last_event_id: int | None) -> tuple[list[dict[str, Any]] | None, asyncio.Queue[dict[str, Any] | None]]:
        """(replay, queue). ``replay`` is None when the caller needs a snapshot first."""
        replay: list[dict[str, Any]] | None = None
        if last_event_id is not None and 0 <= last_event_id <= self.latest_id:
            if last_event_id == self.latest_id:
                replay = []
            elif self.ring and self.ring[0]["id"] <= last_event_id + 1:
                replay = [e for e in self.ring if e["id"] > last_event_id]
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=5000)
        self.subscribers.add(q)
        return replay, q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any] | None]) -> None:
        self.subscribers.discard(q)

    def drop_all(self) -> int:
        """Close every stream (a ``None`` sentinel); browsers reconnect with Last-Event-ID."""
        n = len(self.subscribers)
        for q in list(self.subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        return n


# --------------------------------------------------------------------------------------------------
# the scripted game
# --------------------------------------------------------------------------------------------------


def floor_none() -> dict[str, Any]:
    return {"kind": "none", "active": None, "paused": False, "seconds_left": None, "queue": [], "free": None}


def make_channel(cid: str, name: str, members: list[str], *, mode: str = "closed", system: bool = False, owner: str | None = None, ctype: str = "group") -> dict[str, Any]:
    return {
        "id": cid,
        "name": name,
        "type": "dm" if system else ctype,
        "mode": mode,
        "members": list(members),
        "readonly": [],
        "frozen": False,
        "system": system,
        "owner": owner,
        "floor": floor_none(),
    }


def default_bindings() -> dict[str, Any]:
    models = ["deepseek-flash"] * 8 + ["claude-sonnet"] * 2 + ["deepseek-chat"] * 2
    providers = {"deepseek-flash": "mock-deepseek", "deepseek-chat": "mock-deepseek", "claude-sonnet": "mock-claude"}
    ctx = {"deepseek-flash": 128000, "deepseek-chat": 128000, "claude-sonnet": 200000}
    out: dict[str, Any] = {
        MODERATOR: {"provider_id": "mock-deepseek", "model_id": "deepseek-chat", "reasoning_depth": "high", "context_window_tokens": 128000}
    }
    for pid, model in zip(PLAYER_IDS, models):
        out[pid] = {"provider_id": providers[model], "model_id": model, "reasoning_depth": "medium", "context_window_tokens": ctx[model]}
    return out


def build_session(session_id: str, bindings: dict[str, Any]) -> dict[str, Any]:
    """A fresh (idle) game: players dealt by the engine (D9/D24) and a system DM per player (D23)."""
    participants = [
        {"id": pid, "seat": i + 1, "name": NAMES[i], "tags": [ROLES[pid], "存活"], "out": False, "state": "idle"}
        for i, pid in enumerate(PLAYER_IDS)
    ]
    channels = [make_channel(f"dm-{pid}", f"{pid} 私聊", [MODERATOR, pid], mode="free", system=True, owner=pid) for pid in PLAYER_IDS]
    return {
        "session_id": session_id,
        "schema_version": 3,
        "version": "v0.12",
        "title": "Arena-Agents",
        "scenario_name": "werewolf_12p",
        "scenario_title": "狼人杀 12 人局（模拟数据）",
        "status": "idle",
        "pause_reason": "",
        "hold_reason": "",
        "winner": None,
        "summary": "",
        "phase_text": "未开始 · 等待主持人初始化",
        "active_status_text": "未开始 · 点击「开始」由主持人初始化对局",
        "seq": 24,
        "event_id": 0,
        "clock": 0.0,
        "participants": participants,
        "moderator": {"id": MODERATOR, "name": "主持人", "state": "idle"},
        "channels": channels,
        "messages": [],
        "requests": [],
        "tickets": [],
        "dashboard": {"factions": [], "tag_stats": []},
        "public_tags": list(PUBLIC_TAGS),
        "tag_styles": copy.deepcopy(TAG_STYLES),
        "ui": copy.deepcopy(UI),
        "usage_stats": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "total_cost_usd": 0.0, "by_model": {}, "by_actor": {}},
        "model_bindings": copy.deepcopy(bindings),
        "binding_issues": [],
        "restored_from_save": False,
        "restore_notice": "",
        "autosave_error": "",
        "execution_depth": "medium",
    }


class MockGame:
    """Holds one PublicSession (god view), mutates it as the script plays and publishes the SSE v2
    events that describe every change (so a client that applies the events ends up with the same
    session as :attr:`session`; ``tests/frontend/test_mock_server.py`` checks exactly that)."""

    def __init__(self, bus: EventBus, *, speed: float = 1.0, seed: int = 7) -> None:
        self.bus = bus
        self.speed = max(0.01, float(speed))
        self.seed = seed
        self.rng = random.Random(seed)
        self.bindings = default_bindings()
        self.session = build_session(uuid.uuid4().hex[:12], self.bindings)
        self.gate = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.epilogues = 0
        self._ticket_n = 0
        self._request_n = 0

    # ----------------------------------------------------------------------------- plumbing
    def emit(self, etype: str, **data: Any) -> None:
        ev = self.bus.publish(etype, data)
        self.session["event_id"] = ev["id"]

    def snapshot_payload(self) -> dict[str, Any]:
        self.session["event_id"] = self.bus.latest_id
        return copy.deepcopy(self.session)

    async def wait(self, seconds: float) -> None:
        """Sleep ``seconds`` of GAME time: the clock stops while paused (D18)."""
        remaining = max(0.0, seconds) * PACE / self.speed
        while True:
            await self.gate.wait()
            if remaining <= 0:
                return
            step = min(0.05, remaining)
            await asyncio.sleep(step)
            self.session["clock"] = round(self.session["clock"] + step * self.speed, 2)
            remaining -= step

    def player(self, pid: str) -> dict[str, Any]:
        return next(p for p in self.session["participants"] if p["id"] == pid)

    def channel(self, cid: str) -> dict[str, Any]:
        return next(c for c in self.session["channels"] if c["id"] == cid)

    def alive(self) -> list[str]:
        return [p["id"] for p in self.session["participants"] if not p["out"]]

    # ----------------------------------------------------------------------------- session-level
    def publish_session_updated(self) -> None:
        s = self.session
        self.emit(
            "session.updated",
            status=s["status"],
            pause_reason=s["pause_reason"],
            hold_reason=s["hold_reason"],
            winner=s["winner"],
            summary=s["summary"],
            active_status_text=s["active_status_text"],
            model_bindings=copy.deepcopy(s["model_bindings"]),
            binding_issues=copy.deepcopy(s["binding_issues"]),
            restored_from_save=s["restored_from_save"],
            restore_notice=s["restore_notice"],
            autosave_error=s["autosave_error"],
            execution_depth=s["execution_depth"],
            title=s["title"],
        )

    def status_text(self, text: str) -> None:
        self.session["active_status_text"] = text
        self.publish_session_updated()

    def set_status(self, status: str, *, reason: str = "", pause_reason: str = "", hold_reason: str = "") -> None:
        s = self.session
        s["status"] = status
        s["pause_reason"] = pause_reason
        s["hold_reason"] = hold_reason
        if status == "running":
            s["winner"] = None
            s["summary"] = ""
        self.emit("game.status", status=status, reason=reason, winner=s["winner"], summary=s["summary"])
        text = {
            "running": "对局进行中",
            "paused": f"已暂停 · 主持人：{hold_reason}" if pause_reason == "hold" and hold_reason else "已暂停",
            "finished": "对局已结束",
            "idle": "未开始 · 点击「开始」由主持人初始化对局",
        }[status]
        s["active_status_text"] = text
        self.publish_session_updated()

    def set_agent(self, aid: str, state: str) -> None:
        node = self.session["moderator"] if aid == MODERATOR else self.player(aid)
        if node["state"] == state:
            return
        node["state"] = state
        self.emit("agent.status", id=aid, state=state)

    def set_all_agents(self, state: str) -> None:
        for aid in [MODERATOR, *PLAYER_IDS]:
            self.set_agent(aid, state)

    def meter(self, aid: str, *, tools: int | None = None, think: int | None = None, reject: float = 0.05) -> None:
        """Account one LLM call for ``aid`` and publish its (absolute) metrics."""
        usage = self.session["usage_stats"]
        m = usage["by_actor"].setdefault(
            aid, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "tool_calls": 0, "thinking_rounds": 0, "rejections": 0}
        )
        r = self.rng
        big = aid == MODERATOR
        ptok = r.randint(2200, 4200) if big else r.randint(900, 2400)
        ctok = r.randint(260, 700) if big else r.randint(80, 420)
        model = self.session["model_bindings"].get(aid, {}).get("model_id", "deepseek-flash")
        pin, pout = PRICE.get(model, (0.1, 0.4))
        cost = (ptok * pin + ctok * pout) / 1_000_000
        m["calls"] += 1
        m["prompt_tokens"] += ptok
        m["completion_tokens"] += ctok
        m["cost_usd"] = round(m["cost_usd"] + cost, 6)
        m["tool_calls"] += tools if tools is not None else r.randint(1, 3 if not big else 5)
        m["thinking_rounds"] += think if think is not None else r.randint(1, 2)
        if r.random() < reject:
            m["rejections"] += 1
        usage["prompt_tokens"] += ptok
        usage["completion_tokens"] += ctok
        usage["total_tokens"] += ptok + ctok
        usage["total_cost_usd"] = round(usage["total_cost_usd"] + cost, 6)
        bm = usage["by_model"].setdefault(model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0})
        bm["calls"] += 1
        bm["prompt_tokens"] += ptok
        bm["completion_tokens"] += ctok
        bm["cost_usd"] = round(bm["cost_usd"] + cost, 6)
        self.emit("agent.metrics", id=aid, **{k: m[k] for k in METRIC_FIELDS})

    # ----------------------------------------------------------------------------- world actions
    def commit(self, cid: str, sender: str, content: str, kind: str = "speech", *, template_id: str | None = None,
               truncated: dict[str, int] | None = None, ticket_id: str | None = None, visible: list[str] | None = None) -> dict[str, Any]:
        s = self.session
        ch = self.channel(cid)
        s["seq"] += self.rng.randint(1, 3)  # other ledger entries (floor.*, request.*, ...) take seqs too
        name = "主持人" if sender == MODERATOR else "系统" if sender == ENGINE else NAME_OF.get(sender, sender)
        msg = {
            "seq": s["seq"],
            "ts": now_iso(),
            "channel_id": cid,
            "from": sender,
            "from_name": name,
            "content": content,
            "kind": kind,
            "template_id": template_id,
            "truncated": truncated,
            "ticket_id": ticket_id,
            "visible_to_ids": list(ch["members"] if visible is None else visible),
        }
        s["messages"].append(msg)
        if len(s["messages"]) > 2000:
            del s["messages"][0 : len(s["messages"]) - 2000]
        self.emit("message.committed", **copy.deepcopy(msg))
        return msg

    def announce(self, cid: str, template_id: str, text: str) -> None:
        self.commit(cid, MODERATOR, text, "announce", template_id=template_id)

    def create_channel(self, cid: str, name: str, members: list[str], mode: str = "closed") -> None:
        ch = make_channel(cid, name, members, mode=mode)
        self.session["channels"].append(ch)
        self.emit("channel.created", channel=copy.deepcopy(ch))

    def update_channel(self, cid: str, **changes: Any) -> None:
        ch = self.channel(cid)
        ch.update(changes)
        self.emit("channel.updated", channel=copy.deepcopy(ch))

    def set_floor(self, cid: str, floor: dict[str, Any]) -> None:
        self.channel(cid)["floor"] = floor
        self.emit("floor.changed", channel_id=cid, floor=copy.deepcopy(floor))

    def set_phase(self, text: str) -> None:
        self.session["phase_text"] = text
        self.emit("phase.changed", text=text)

    def update_dashboard(self) -> None:
        alive = set(self.alive())

        def count(ids: list[str]) -> int:
            return len([i for i in ids if i in alive])

        sheriffs = len([p for p in self.session["participants"] if "警长" in p["tags"]])
        dash = {
            "factions": [
                {"name": "狼人阵营", "alive": count(WOLVES), "total": 4, "variant": "danger"},
                {"name": "神职", "alive": count(GODS), "total": 4, "variant": "purple"},
                {"name": "平民阵营", "alive": count(VILLAGERS), "total": 4, "variant": "neutral"},
            ],
            "tag_stats": [
                {"name": "存活", "count": len(alive), "variant": "success"},
                {"name": "出局", "count": 12 - len(alive), "variant": "neutral"},
                {"name": "警长", "count": sheriffs, "variant": "warning"},
            ],
        }
        self.session["dashboard"] = dash
        self.emit("dashboard.updated", dashboard=copy.deepcopy(dash))

    def player_out(self, pid: str) -> None:
        p = self.player(pid)
        p["tags"] = [t for t in p["tags"] if t not in ("存活", "警长")] + ["出局"]
        p["out"] = True
        self.emit("player.updated", player_id=pid, tags=list(p["tags"]), out=True)

    def add_tag(self, pid: str, tag: str) -> None:
        p = self.player(pid)
        if tag not in p["tags"]:
            p["tags"].append(tag)
        self.emit("player.updated", player_id=pid, tags=list(p["tags"]), out=p["out"])

    # --- floors
    async def run_queue(self, cid: str, order: list[str], speeches: dict[str, tuple[Any, ...]], *, pass_delay: float = 0.35) -> None:
        """``set_floor_queue``: one active token at a time; ``floor.changed`` advances after each speaker."""
        ch = self.channel(cid)
        total = len(order)
        queue = [{"holder": h, "state": "pending", "messages_used": 0, "max_messages": 2} for h in order]
        queue[0]["state"] = "active"
        ch["mode"] = "queue"
        self.emit("channel.updated", channel=copy.deepcopy(ch))
        self.set_floor(cid, {"kind": "queue", "active": order[0], "paused": False, "seconds_left": 90.0, "queue": queue, "free": None})
        for i, holder in enumerate(order):
            if holder in speeches:
                spec = speeches[holder]
                truncated = spec[2] if len(spec) > 2 else None
                await self.speak(cid, holder, spec[0], think=spec[1], truncated=truncated)
                queue[i]["messages_used"] = 1
            else:
                await self.wait(pass_delay)  # `pass`: the token ends without a message
            queue[i]["state"] = "done"
            if i + 1 < total:
                queue[i + 1]["state"] = "active"
            floor = {
                "kind": "queue",
                "active": order[i + 1] if i + 1 < total else None,
                "paused": False,
                "seconds_left": 90.0,
                "queue": copy.deepcopy(queue),
                "free": None,
            }
            if i + 1 < total:
                self.set_floor(cid, floor)
        # every token is over: the engine closes the channel and wakes the moderator
        self.set_floor(cid, floor_none())
        ch["mode"] = "closed"
        self.emit("channel.updated", channel=copy.deepcopy(ch))

    def open_free(self, cid: str, *, max_chars: int = 2000, duration: float = 300.0) -> None:
        ch = self.channel(cid)
        ch["mode"] = "free"
        free = {"chars_used": 0, "max_chars_total": max_chars, "elapsed_s": 0.0, "duration_s": duration, "silent_s": 0.0, "message_counts": {}}
        ch["floor"] = {"kind": "free", "active": None, "paused": False, "seconds_left": None, "queue": [], "free": free}
        self.emit("channel.updated", channel=copy.deepcopy(ch))
        self.emit("floor.changed", channel_id=cid, floor=copy.deepcopy(ch["floor"]))

    def close_floor(self, cid: str) -> None:
        ch = self.channel(cid)
        ch["floor"] = floor_none()
        ch["mode"] = "closed"
        ch["frozen"] = False
        self.emit("floor.changed", channel_id=cid, floor=copy.deepcopy(ch["floor"]))
        self.emit("channel.updated", channel=copy.deepcopy(ch))

    async def speak(self, cid: str, pid: str, text: str, *, think: float = 1.0, truncated: dict[str, int] | None = None) -> None:
        """A player: thinking -> acting -> a whole message (never partial) -> idle."""
        self.set_agent(pid, "thinking")
        await self.wait(think)
        self.set_agent(pid, "acting")
        await self.wait(0.25)
        self.meter(pid)
        self.commit(cid, pid, text, "speech", truncated=truncated)
        ch = self.channel(cid)
        free = (ch["floor"] or {}).get("free")
        if ch["mode"] == "free" and free is not None:
            free["chars_used"] += len(text)
            free["message_counts"][pid] = free["message_counts"].get(pid, 0) + 1
            free["elapsed_s"] = round(free["elapsed_s"] + think + 0.25, 1)
            self.emit("floor.changed", channel_id=cid, floor=copy.deepcopy(ch["floor"]))
        self.set_agent(pid, "idle")

    async def moderator_step(self, think: float = 0.8, status: str | None = None) -> None:
        if status:
            self.status_text(status)
        self.set_agent(MODERATOR, "thinking")
        await self.wait(think)
        self.set_agent(MODERATOR, "acting")
        self.meter(MODERATOR)
        await self.wait(0.3)

    def moderator_idle(self) -> None:
        self.set_agent(MODERATOR, "idle")

    # --- requests
    def open_request(self, question: str, options: list[str], to: list[str], *, sealed: bool = True, kind: str = "choice") -> str:
        self._request_n += 1
        rid = f"r{self._request_n}"
        req = {
            "id": rid,
            "kind": kind,
            "question": question,
            "options": [{"id": o, "label": f"{o} · {NAME_OF[o]}"} for o in options],
            "to": list(to),
            "answered_by": [],
            "status": "open",
            "sealed": sealed,
            "seconds_left": 90.0,
        }
        self.session["requests"].append(req)
        self.emit("request.opened", request=copy.deepcopy(req))
        return rid

    def request_progress(self, rid: str, pid: str) -> None:
        req = next(r for r in self.session["requests"] if r["id"] == rid)
        if pid not in req["answered_by"]:
            req["answered_by"].append(pid)
        req["answered_by"].sort(key=PLAYER_IDS.index)  # seat order; no answers (sealed votes stay secret)
        self.emit("request.progress", request_id=rid, answered_by=list(req["answered_by"]), total=len(req["to"]))

    def close_request(self, rid: str, reason: str = "all_answered") -> None:
        req = next(r for r in self.session["requests"] if r["id"] == rid)
        req["status"] = "closed"
        req["seconds_left"] = None
        self.emit("request.closed", request_id=rid, reason=reason, answered_by=list(req["answered_by"]), defaulted=[])

    async def vote(self, to: list[str], question: str, options: list[str]) -> str:
        """All voters think at once; answers trickle in (``request.progress``) in a random order."""
        rid = self.open_request(question, options, to)
        for pid in to:
            self.set_agent(pid, "thinking")

        async def one(pid: str, think: float) -> None:
            await self.wait(think)
            self.set_agent(pid, "acting")
            await self.wait(0.15)
            self.meter(pid, tools=1, think=1)
            self.request_progress(rid, pid)
            self.set_agent(pid, "idle")

        thinks = [0.4 + self.rng.random() * 2.6 for _ in to]
        await asyncio.gather(*(one(pid, t) for pid, t in zip(to, thinks)))
        self.close_request(rid)
        return rid

    # --- challenge tickets
    def open_ticket(self, pid: str, content: str) -> str:
        self._ticket_n += 1
        tid = f"t{self._ticket_n}"
        msg = self.commit(f"dm-{pid}", pid, content, "challenge", ticket_id=tid, visible=[MODERATOR, pid])
        ticket = {"id": tid, "player_id": pid, "status": "open", "verdict": None, "preview": content[:60], "opened_seq": msg["seq"]}
        self.session["tickets"].append(ticket)
        self.emit("challenge.opened", ticket=copy.deepcopy(ticket))
        return tid

    def resolve_ticket(self, tid: str, verdict: str, reply: str) -> None:
        ticket = next(t for t in self.session["tickets"] if t["id"] == tid)
        self.commit(f"dm-{ticket['player_id']}", MODERATOR, reply, "reply", ticket_id=tid, visible=[MODERATOR, ticket["player_id"]])
        ticket["status"] = "resolved"
        ticket["verdict"] = verdict
        self.emit("challenge.resolved", ticket_id=tid, verdict=verdict)

    async def challenge(self, pid: str, content: str, verdict: str, reply: str, *, think: float = 1.2, resolve_after: float = 2.4) -> str:
        self.set_agent(pid, "thinking")
        await self.wait(think)
        self.set_agent(pid, "acting")
        self.meter(pid, tools=1)
        tid = self.open_ticket(pid, content)
        self.set_agent(pid, "idle")
        await self.wait(resolve_after)
        await self.moderator_step(1.0, f"主持人：处理质询 {tid}")
        self.resolve_ticket(tid, verdict, reply)
        self.moderator_idle()
        return tid

    # ----------------------------------------------------------------------------- the game
    async def _chatter(self, cid: str, pid: str, text: str, think: float) -> None:
        try:
            await self.speak(cid, pid, text, think=think)
        except asyncio.CancelledError:
            self.set_agent(pid, "idle")  # D25: the floor was taken back: the engine cancelled this player's thinking
            raise

    async def _delayed(self, seconds: float, coro: Any) -> Any:
        await self.wait(seconds)
        return await coro

    async def play(self) -> None:
        # 1. set-up: channels, dashboard, announcements
        await self.moderator_step(1.0, "主持人：读取规则，建立频道")
        self.create_channel("public", "公共频道", EVERYONE)
        await self.wait(0.5)
        self.create_channel("wolves", "狼人阵营", [MODERATOR, *WOLVES])
        await self.wait(0.4)
        self.set_phase("准备阶段 · 发牌与建台")
        self.update_dashboard()
        self.commit("public", ENGINE, "对局开始：12 名玩家与主持人已就绪，计时器启动。", "system")
        await self.wait(0.6)
        self.announce("public", "game_start", "【对局开始】本局为 12 人暗牌局：3 狼人 + 白狼王、4 平民、预言家、女巫、猎人、守卫。出局公告不公布底牌。")
        self.commit("wolves", MODERATOR, "狼人队友为 p03、p06、p10、p12；夜间请在此频道商议刀口，允许自刀或空刀。")
        self.moderator_idle()
        await self.wait(1.0)

        # 2. night 1: the wolves talk (free mode), the seer and the witch get private messages
        await self.moderator_step(0.8, "主持人：第 1 夜 · 狼人商议")
        self.set_phase("第 1 夜 · 黑夜")
        self.announce("public", "night_start", "【天黑请闭眼】第 1 夜开始。")
        self.open_free("wolves", max_chars=800, duration=60.0)
        self.moderator_idle()
        for pid, text, think in [
            ("p03", "今晚刀 p05，她很可能是女巫，先下手为强。", 1.0),
            ("p06", "同意，女巫不留。p12 的白狼王身份先别暴露。", 1.0),
            ("p10", "我没意见，明天白天我来带节奏。", 0.9),
        ]:
            await self.speak("wolves", pid, text, think=think)
        self.close_floor("wolves")
        await self.moderator_step(0.7, "主持人：回复预言家与女巫")
        self.commit("dm-p04", MODERATOR, "【查验结果】p03 · Carol 3 是狼人阵营。", "speech", visible=[MODERATOR, "p04"])
        self.commit("dm-p05", MODERATOR, "【女巫】昨夜 p05（你）被刀，是否使用解药？请直接回复。", "speech", visible=[MODERATOR, "p05"])
        self.moderator_idle()
        await self.wait(0.8)

        # 3. dawn: a peaceful night (the witch saved herself), the sheriff
        await self.moderator_step(1.0, "主持人：宣布昨夜结果")
        self.set_phase("第 2 天 · 白天发言")
        self.announce("public", "peaceful_night", "【平安夜】昨夜无人出局。")
        await self.wait(0.5)
        self.add_tag("p04", "警长")
        self.update_dashboard()
        self.announce("public", "sheriff", "【警长】David 4（p04）当选警长，拥有 1.5 票。")
        self.moderator_idle()
        await self.wait(0.6)

        # 4. day speeches: set_floor_queue (floor.changed advances); some players simply pass
        await self.moderator_step(0.9, "主持人：宣布发言顺序（队列）")
        self.announce("public", "speech_order", "【发言顺序】从警长 p04 开始，按座位顺序依次发言，每人一次。")
        self.moderator_idle()
        order = ["p04", "p05", "p06", "p07", "p08", "p09", "p10", "p11", "p12", "p01", "p02", "p03"]
        long_cut = {"dropped_chars": len(LONG_SPEECH) - 300, "kept_chars": 300}  # the engine cut it (D19)
        speeches = {
            "p04": ("我是预言家。昨晚查验了 p03，他是狼人阵营。请大家今天出掉 p03。", 1.3),
            "p06": ("p04 的发言有点急，我是好人，想先听听其他人的意见。", 1.1),
            "p07": (LONG_SPEECH[:300], 1.4, long_cut),
            "p08": ("我昨晚守了 p04，没有被击中。预言家的身份我倾向于相信。", 1.0),
            "p09": ("我是平民，没有信息，跟随预言家的归票。", 0.9),
            "p10": ("p04 的跳身份太巧了，我反对盲从。p03 可能是被陷害的。", 1.1),
            "p11": ("我是平民。p10 的发言和 p03 一样在帮狼说话，我怀疑他。", 1.0),
        }
        self.status_text("主持人：公共频道按队列发言")
        await self.run_queue("public", order, speeches)
        await self.wait(0.4)

        # 5. a sealed vote with progress (request.opened / progress / closed), then the last words
        self.set_phase("第 2 天 · 放逐投票")
        await self.moderator_step(0.8, "主持人：发起放逐投票")
        self.moderator_idle()
        voters = self.alive()
        self.status_text("主持人：等待玩家投票")
        await self.vote(voters, "请投票放逐一名玩家（密封投票）", list(voters))
        await self.moderator_step(1.2, "主持人：统计票数")
        self.announce("public", "vote_result", "【投票结果】p10 · Jack 10 得 6 票，被放逐。")
        self.player_out("p10")
        self.update_dashboard()
        rid = self.open_request("请留下遗言", [], ["p10"], sealed=False, kind="text")
        self.moderator_idle()
        self.set_agent("p10", "thinking")
        await self.wait(1.4)
        self.set_agent("p10", "acting")
        self.meter("p10", tools=1, think=1)
        self.request_progress(rid, "p10")
        self.set_agent("p10", "idle")
        self.close_request(rid)
        await self.moderator_step(0.8, "主持人：转述遗言")
        self.announce("public", "last_words_relay", "【遗言】Jack 10（p10）：我真的是好人，大家别被预言家牵着走。")
        self.moderator_idle()
        await self.wait(0.6)

        # 6. challenge tickets (D22): one rejected, one upheld
        self.set_phase("第 2 天 · 质询与复核")
        await asyncio.gather(
            self.challenge(
                "p03",
                "主持人，刚才的计票是不是漏了一票？我记得我只被投了 5 票。",
                "rejected",
                "经逐一核对，计票无误：p10 得 6 票。质询驳回。",
                think=1.2,
                resolve_after=2.6,
            ),
            self._delayed(
                1.6,
                self.challenge(
                    "p08",
                    "主持人，p07 的发言被截断了，最后一句没有说完，请允许他补充。",
                    "upheld",
                    "质询成立：已记录 p07 的发言被截断，大家请注意这一点。",
                    think=1.0,
                    resolve_after=2.8,
                ),
            ),
        )
        await self.wait(0.6)

        # 7. a free discussion; the white wolf king explodes: public declaration + frozen channel (D25)
        self.set_phase("第 3 天 · 自由讨论")
        await self.moderator_step(0.8, "主持人：开放自由讨论")
        self.announce("public", "free_discussion", "【自由讨论】现在开始自由讨论，请抓紧时间交换意见。")
        self.open_free("public", max_chars=2000, duration=300.0)
        self.moderator_idle()
        chatter = [
            ("p01", "我支持 p04 的查验，p03 昨晚没有任何辩解。", 0.9),
            ("p02", "同意 p01。p10 已经出局，他的遗言也很可疑。", 1.6),
            ("p03", "我是被冤枉的！p04 是悍跳的狼，请大家看清。", 1.9),
            ("p08", "我守了 p04，他要是狼我昨晚不会守他。", 2.4),
            ("p04", "再次强调：p03 是狼。请好人认真对待归票。", 3.3),
            ("p07", "各位注意，我是猎人，今天谁敢动我我就开枪。", 3.8),
            ("p09", "我还是跟随预言家，今天投 p03。", 4.4),
        ]
        tasks = [asyncio.ensure_future(self._chatter("public", pid, text, t)) for pid, text, t in chatter]
        try:
            self.set_agent("p12", "thinking")
            await self.wait(2.9)  # p12 decides to explode while the others are still typing
            self.set_agent("p12", "acting")
            self.meter("p12", tools=2)
            self.emit("ability.used", ability_id="white_wolf_explode", actor_id="p12")
            self.commit("public", "p12", "我是白狼王，自爆！带走猎人 p07。", "ability")  # the ability is the permission: no floor needed
            pub = self.channel("public")
            pub["frozen"] = True
            self.emit("channel.updated", channel=copy.deepcopy(pub))
            for t in tasks:  # the revoked floor cancels the others' in-flight thinking
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
        self.set_agent("p12", "idle")
        await self.wait(0.8)
        await self.moderator_step(1.6, "主持人：裁决白狼王自爆")
        self.player_out("p12")
        self.player_out("p07")
        self.update_dashboard()
        self.announce("public", "death_notice", "【出局公告】Leo 12（p12）自爆出局，并带走 Grace 7（p07）。")
        pub["frozen"] = False
        self.emit("channel.updated", channel=copy.deepcopy(pub))
        self.moderator_idle()
        await self.wait(1.0)

        # 8. the last wolves are voted out, the moderator ends the game (D8)
        await self.moderator_step(1.0, "主持人：第 3 天 · 放逐并判定胜负")
        self.close_floor("public")
        self.set_phase("第 3 天 · 终局")
        self.player_out("p03")
        self.player_out("p06")
        self.update_dashboard()
        self.announce("public", "death_notice", "【出局公告】Carol 3（p03）、Frank 6（p06）出局。")
        await self.wait(0.6)
        self.finish("好人阵营", "狼人阵营全部出局，好人阵营获胜（模拟对局）。")
        self.moderator_idle()

    def finish(self, winner: str, summary: str) -> None:
        s = self.session
        s["winner"] = winner
        s["summary"] = summary
        s["status"] = "finished"
        s["pause_reason"] = ""
        s["hold_reason"] = ""
        s["active_status_text"] = "对局已结束"
        self.emit("game.status", status="finished", reason="end_game", winner=winner, summary=summary)
        self.publish_session_updated()
        self.set_all_agents("stopped")  # D8: after end_game every agent stops

    async def epilogue(self) -> None:
        """What "继续" replays after the verdict (D8): last words, then the verdict again."""
        self.epilogues += 1
        await self.moderator_step(1.0, "主持人：终局后继续 · 请幸存者发表感言")
        self.announce("public", "last_words", f"【终局后继续】第 {self.epilogues} 次：请幸存者留下最后的感言。")
        self.open_free("public", max_chars=600, duration=60.0)
        self.moderator_idle()
        lines = [
            ("p01", "谢谢主持人，这局打得很精彩，预言家带得很稳。", 1.0),
            ("p04", "能活到最后很开心，感谢守卫 p08 的那一刀。", 1.8),
            ("p08", "守卫的职责就是守住好人，今天算是圆满了。", 2.6),
        ]
        await asyncio.gather(*(self.speak("public", pid, text, think=t) for pid, text, t in lines))
        await self.moderator_step(0.8, "主持人：再次宣布终局")
        self.close_floor("public")
        self.finish("好人阵营", "终局后继续：幸存者感言结束，好人阵营获胜（模拟对局）。")
        self.moderator_idle()

    # ----------------------------------------------------------------------------- lifecycle
    def running(self) -> bool:
        return self.gate.is_set()

    def _spawn(self, coro: Any) -> None:
        async def runner() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - a bug in the script must be visible
                print(f"mock script crashed: {exc!r}", file=sys.stderr)
                self.emit("error", message=f"模拟脚本出错：{exc}")

        self.task = asyncio.get_running_loop().create_task(runner())

    def start(self) -> None:
        """idle -> run the script; paused -> resume; finished -> replay the epilogue (D8)."""
        s = self.session
        if s["status"] == "running":
            return
        if s["status"] == "paused" and self.task is not None and not self.task.done():
            self.set_status("running", reason="resumed")
            self.set_all_agents("idle")
            self.gate.set()
            return
        if s["status"] == "finished":
            self.set_status("running", reason="continued")
            self.set_all_agents("idle")
            self.gate.set()
            self._spawn(self.epilogue())
            return
        self.set_status("running", reason="started")
        self.set_all_agents("idle")
        self.gate.set()
        self._spawn(self.play())

    def pause(self, *, reason: str = "user", hold_reason: str = "") -> None:
        if self.session["status"] != "running":
            return
        self.gate.clear()
        self.set_status("paused", reason=("hold:" + hold_reason) if reason == "hold" else reason, pause_reason=reason, hold_reason=hold_reason)
        self.set_all_agents("stopped")

    async def _cancel_task(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.task = None

    async def reset(self) -> None:
        """``new``: stop everything and deal a fresh game (a new session id); every client gets a snapshot."""
        await self._cancel_task()
        self.gate.clear()
        self.rng = random.Random(self.seed)
        self.epilogues = 0
        self._ticket_n = 0
        self._request_n = 0
        self.session = build_session(uuid.uuid4().hex[:12], self.bindings)
        self.session["event_id"] = self.bus.latest_id + 1  # the id the snapshot event is about to get
        self.bus.publish("snapshot", {"session": copy.deepcopy(self.session)})

    async def jump_to_end(self) -> None:
        """Mock helper: cancel the script and finish right away."""
        await self._cancel_task()
        if self.session["status"] != "finished":
            self.finish("好人阵营", "（模拟）直接跳到终局。")
        self.gate.clear()


# --------------------------------------------------------------------------------------------------
# fast offline replay (tests / --selfcheck)
# --------------------------------------------------------------------------------------------------


async def _replay_fast(speed: float = 1_000_000.0, *, epilogue: bool = True, seed: int = 7) -> dict[str, Any]:
    """Run the whole script without delays; returns the initial snapshot, every event, the final session."""
    bus = EventBus(ring=100000)
    game = MockGame(bus, speed=speed, seed=seed)
    initial = game.snapshot_payload()
    game.gate.set()
    game.set_status("running", reason="started")
    game.set_all_agents("idle")
    await game.play()
    if epilogue:
        game.set_status("running", reason="continued")
        game.set_all_agents("idle")
        await game.epilogue()
    final = game.snapshot_payload()
    return {"initial": initial, "events": list(bus.ring), "final": final}


def replay_fast(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(_replay_fast(**kwargs))


# --------------------------------------------------------------------------------------------------
# the FastAPI app
# --------------------------------------------------------------------------------------------------


def create_app(*, speed: float = 1.0, seed: int = 7) -> Any:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles

    bus = EventBus()
    holder: dict[str, Any] = {}
    store: dict[str, Any] = {
        "providers": copy.deepcopy(PROVIDERS),
        "templates": [
            {"slot": 1, "name": "旗舰", "provider_id": "mock-claude", "model_id": "claude-sonnet", "reasoning_depth": "high", "valid": True},
        ],
        "sessions": [],
        "workspace": {"id": "w-mock", "name": "mock-workspace", "path": str(ROOT / ".mock-workspace"), "status": "ok"},
    }

    app = FastAPI(title="Arena-Agents UI mock", version=APP_VERSION)
    app.state.mock_holder = holder  # tests reach the MockGame (created on the first request) through this

    def game() -> MockGame:
        if "game" not in holder:  # created lazily: the asyncio primitives need a running loop
            holder["game"] = MockGame(bus, speed=speed, seed=seed)
        return holder["game"]

    @app.middleware("http")
    async def no_cache(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    # ---- pages
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": APP_VERSION}

    # ---- state / events
    def session_payload() -> dict[str, Any]:
        return game().snapshot_payload()

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        return {
            "app_version": APP_VERSION,
            "llm_configured": True,
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.example.com",
            "reasoning_effort": "high",
            "restored": False,
            "binding_issues": [],
            "workspace": {"enabled": True, "current": store["workspace"]},
            "session": session_payload(),
        }

    def sse(ev: dict[str, Any]) -> str:
        return f"id: {ev['id']}\ndata: {json.dumps(ev, ensure_ascii=False, separators=(',', ':'))}\n\n"

    @app.get("/api/events")
    async def events(request: Request, last_event_id: int | None = None) -> StreamingResponse:
        header = request.headers.get("last-event-id")
        last = last_event_id
        if last is None and header not in (None, ""):
            try:
                last = int(header)  # type: ignore[arg-type]
            except ValueError:
                last = None
        g = game()
        replay, queue = bus.subscribe(last)

        async def stream():  # type: ignore[no-untyped-def]
            try:
                if replay is None:
                    yield sse({"id": bus.latest_id, "type": "snapshot", "data": {"session": g.snapshot_payload()}})
                else:
                    for ev in replay:
                        yield sse(ev)
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    if ev is None:  # /api/mock/disconnect
                        break
                    yield sse(ev)
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # ---- controls
    @app.post("/api/control/new")
    async def control_new() -> dict[str, Any]:
        await game().reset()
        return {"ok": True, "session": session_payload()}

    @app.post("/api/control/start")
    async def control_start() -> dict[str, Any]:
        game().start()
        return {"ok": True, "session": session_payload()}

    @app.post("/api/control/pause")
    async def control_pause() -> dict[str, Any]:
        game().pause()
        return {"ok": True, "session": session_payload()}

    @app.post("/api/control/force-pause")
    async def control_force_pause() -> dict[str, Any]:
        game().pause()  # contract §6: pause and force-pause are equivalent now
        return {"ok": True, "session": session_payload()}

    @app.post("/api/session/execution-depth")
    async def set_execution_depth(body: dict[str, Any]) -> dict[str, Any]:
        depth = str(body.get("execution_depth") or body.get("depth") or "").lower()
        if depth not in ("low", "medium", "high", "xhigh"):
            raise HTTPException(status_code=400, detail="Invalid execution depth. Must be low, medium, high, or xhigh.")
        g = game()
        g.session["execution_depth"] = depth
        g.publish_session_updated()
        return {"ok": True, "execution_depth": depth}

    @app.post("/api/session/save")
    async def save_session() -> dict[str, Any]:
        s = session_payload()
        return {"ok": True, "filename": f"arena_session_mock_{s['session_id']}.json", "session": s}

    @app.post("/api/session/import")
    async def import_session(request: Request) -> dict[str, Any]:
        await request.body()
        return {"ok": True, "session": session_payload()}

    @app.get("/api/sessions")
    async def list_sessions() -> dict[str, Any]:
        s = game().session
        current = {"session_id": s["session_id"], "title": s["title"], "status": s["status"], "current": True, "record_count": len(s["messages"])}
        return {"sessions": [current, *store["sessions"]]}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        store["sessions"] = [x for x in store["sessions"] if x["session_id"] != session_id]
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/load")
    async def load_session(session_id: str) -> dict[str, Any]:
        if session_id == game().session["session_id"]:
            raise HTTPException(status_code=409, detail="该存档就是当前对局，无需加载。")
        await game().reset()
        return {"ok": True, "session": session_payload()}

    # ---- workspaces
    @app.get("/api/workspaces")
    async def workspaces() -> dict[str, Any]:
        cur = store["workspace"]
        return {
            "enabled": True,
            "current": cur,
            "recent": [cur, {"id": "w-other", "name": "other-workspace", "path": str(ROOT / ".other-workspace")}],
            "default_path": str(ROOT / ".mock-workspace"),
            "native_picker": False,
        }

    @app.post("/api/workspaces/pick-native-folder")
    async def pick_folder() -> Any:
        return JSONResponse(status_code=400, content={"detail": "native folder picker is unavailable in the mock"})

    @app.post("/api/workspaces/select")
    async def select_workspace(body: dict[str, Any]) -> dict[str, Any]:
        if game().session["status"] == "running":
            raise HTTPException(status_code=409, detail="对局运行中，请先暂停。")
        path = str(body.get("path") or "")
        store["workspace"] = {"id": uuid.uuid4().hex[:8], "name": Path(path).name or "workspace", "path": path, "status": "ok"}
        bus.publish("workspace.changed", {})  # every open tab reloads (§8.2)
        return {"ok": True, "workspace": store["workspace"], "session": session_payload()}

    # ---- providers / bindings / templates (in memory, just enough for the dialogs)
    def find_provider(pid: str) -> dict[str, Any]:
        for p in store["providers"]:
            if p["id"] == pid:
                return p
        raise HTTPException(status_code=404, detail="Provider 不存在")

    @app.get("/api/providers")
    async def get_providers() -> dict[str, Any]:
        return {"providers": store["providers"]}

    @app.post("/api/providers/endpoint-preview")
    async def endpoint_preview(body: dict[str, Any]) -> dict[str, Any]:
        base = str(body.get("base_url") or "").rstrip("/")
        auto = body.get("auto_append_path") is not False
        return {
            "auto_append_path": auto,
            "request_url": f"{base}/v1/chat/completions" if auto else base,
            "models_url": f"{base}/v1/models" if auto else None,
            "warnings": [] if base.startswith("https://") else ["使用的是 http:// 而不是 https://"],
        }

    @app.post("/api/providers", status_code=201)
    async def create_provider(body: dict[str, Any]) -> dict[str, Any]:
        p = {"id": uuid.uuid4().hex[:8], "name": body.get("name", "新 Provider"), "protocol": body.get("protocol", "openai_chat"),
             "base_url": body.get("base_url", ""), "api_key_set": bool(body.get("api_key")), "read_only": False,
             "auto_append_path": body.get("auto_append_path", True), "models": []}
        store["providers"].append(p)
        return p

    @app.patch("/api/providers/{provider_id}")
    async def patch_provider(provider_id: str, body: dict[str, Any]) -> dict[str, Any]:
        p = find_provider(provider_id)
        for k in ("name", "protocol", "base_url", "auto_append_path"):
            if k in body:
                p[k] = body[k]
        if body.get("api_key"):
            p["api_key_set"] = True
        return p

    @app.delete("/api/providers/{provider_id}")
    async def delete_provider(provider_id: str) -> dict[str, Any]:
        store["providers"] = [p for p in store["providers"] if p["id"] != provider_id]
        return {"ok": True}

    @app.post("/api/providers/{provider_id}/discover-models")
    async def discover_models(provider_id: str) -> dict[str, Any]:
        p = find_provider(provider_id)
        return {"models": [m["id"] for m in p["models"]] + ["mock-new-model"], "existing_models": [m["id"] for m in p["models"]]}

    @app.post("/api/providers/{provider_id}/models", status_code=201)
    async def add_model(provider_id: str, body: dict[str, Any]) -> dict[str, Any]:
        p = find_provider(provider_id)
        m = {"id": body["model_id"], "input_price_per_m": body["input_price_per_m"], "output_price_per_m": body["output_price_per_m"],
             "context_window_tokens": body.get("context_window_tokens", 128000)}
        p["models"].append(m)
        return m

    @app.patch("/api/providers/{provider_id}/models")
    async def patch_model(provider_id: str, body: dict[str, Any]) -> dict[str, Any]:
        p = find_provider(provider_id)
        for m in p["models"]:
            if m["id"] == body.get("model_id"):
                m.update({k: v for k, v in body.items() if k != "model_id"})
        return {"ok": True}

    @app.delete("/api/providers/{provider_id}/models")
    async def delete_model(provider_id: str, body: dict[str, Any]) -> dict[str, Any]:
        p = find_provider(provider_id)
        p["models"] = [m for m in p["models"] if m["id"] != body.get("model_id")]
        return {"ok": True}

    @app.get("/api/settings/bindings")
    async def get_bindings() -> dict[str, Any]:
        g = game()
        return {"bindings": g.bindings, "providers": store["providers"], "locked": g.session["status"] == "running", "issues": []}

    @app.put("/api/settings/bindings")
    async def put_bindings(body: dict[str, Any]) -> dict[str, Any]:
        g = game()
        if g.session["status"] == "running":
            raise HTTPException(status_code=409, detail="对局运行中，不能修改模型绑定")
        g.bindings = body.get("bindings") or {}
        g.session["model_bindings"] = copy.deepcopy(g.bindings)
        g.publish_session_updated()
        return {"bindings": g.bindings, "providers": store["providers"]}

    @app.get("/api/settings/templates")
    async def get_templates() -> dict[str, Any]:
        return {"templates": store["templates"], "slots": 3, "locked": game().session["status"] == "running"}

    @app.put("/api/settings/templates")
    async def put_templates(body: dict[str, Any]) -> dict[str, Any]:
        store["templates"] = [{**t, "valid": True} for t in body.get("templates", [])]
        return {"templates": store["templates"], "slots": 3, "locked": False}

    @app.post("/api/settings/templates/{slot}/apply")
    async def apply_template(slot: int, body: dict[str, Any]) -> dict[str, Any]:
        tpl = next((t for t in store["templates"] if t["slot"] == slot), None)
        if tpl is None:
            raise HTTPException(status_code=404, detail="模板不存在")
        g = game()
        updated = {
            a: {"provider_id": tpl["provider_id"], "model_id": tpl["model_id"], "reasoning_depth": tpl["reasoning_depth"]}
            for a in body.get("actor_ids", [])
        }
        g.bindings.update(updated)
        g.session["model_bindings"] = copy.deepcopy(g.bindings)
        g.publish_session_updated()
        return {"bindings": updated, "providers": store["providers"], "locked": False, "issues": []}

    # ---- mock-only helpers ----------------------------------------------------------------------
    @app.post("/api/mock/hold")
    async def mock_hold(body: dict[str, Any] | None = None) -> dict[str, Any]:
        """The moderator calls hold_game: paused with pause_reason=hold and a reason to show."""
        reason = str((body or {}).get("reason") or "请确认警长归属后再继续")
        game().pause(reason="hold", hold_reason=reason)
        return {"ok": True, "session": session_payload()}

    @app.post("/api/mock/error")
    async def mock_error(body: dict[str, Any] | None = None) -> dict[str, Any]:
        reason = str((body or {}).get("reason") or "error")
        g = game()
        g.pause(reason="stall" if reason == "stall" else "error")
        return {"ok": True, "session": session_payload()}

    @app.post("/api/mock/finish")
    async def mock_finish() -> dict[str, Any]:
        await game().jump_to_end()
        return {"ok": True, "session": session_payload()}

    @app.post("/api/mock/speed")
    async def mock_speed(body: dict[str, Any]) -> dict[str, Any]:
        """Change the replay speed on the fly (applies to the next wait): slow down to catch a moment."""
        g = game()
        try:
            g.speed = max(0.01, float(body.get("speed", 1.0)))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="speed must be a number") from exc
        return {"ok": True, "speed": g.speed}

    @app.post("/api/mock/disconnect")
    async def mock_disconnect() -> dict[str, Any]:
        """Close every SSE stream: browsers reconnect (with last_event_id) and resume from the ring buffer."""
        return {"ok": True, "dropped": bus.drop_all()}

    @app.post("/api/mock/emit")
    async def mock_emit(body: dict[str, Any]) -> dict[str, Any]:
        """Publish an arbitrary event (e.g. {"type": "workspace.changed", "data": {}}); validated first."""
        ev = {"id": bus.latest_id + 1, "type": body.get("type"), "data": body.get("data") or {}}
        problems = validate_event(ev)
        if problems:
            raise HTTPException(status_code=422, detail="; ".join(problems))
        game().emit(str(ev["type"]), **ev["data"])
        return {"ok": True, "id": bus.latest_id}

    @app.get("/api/mock/info")
    async def mock_info() -> dict[str, Any]:
        g = game()
        return {"speed": g.speed, "latest_event_id": bus.latest_id, "subscribers": len(bus.subscribers), "status": g.session["status"],
                "ring": len(bus.ring)}

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------


def selfcheck() -> int:
    run = replay_fast()
    problems: list[str] = []
    types: dict[str, int] = {}
    for ev in run["events"]:
        types[ev["type"]] = types.get(ev["type"], 0) + 1
        for p in validate_event(ev):
            problems.append(f"#{ev['id']} {ev['type']}: {p}")
    problems += [f"final: {p}" for p in validate_session(run["final"])]
    problems += [f"initial: {p}" for p in validate_session(run["initial"])]
    print(f"{len(run['events'])} events; {len(run['final']['messages'])} messages")
    for t in sorted(types):
        print(f"  {t:20s} {types[t]}")
    required = {
        "message.committed", "channel.created", "channel.updated", "floor.changed", "player.updated", "request.opened",
        "request.progress", "request.closed", "challenge.opened", "challenge.resolved", "ability.used", "phase.changed",
        "dashboard.updated", "game.status", "agent.status", "agent.metrics", "session.updated",
    }
    missing = required - set(types)
    if missing:
        problems.append(f"script does not cover: {sorted(missing)}")
    if problems:
        print("PROBLEMS:")
        for p in problems[:40]:
            print("  -", p)
        return 1
    print("selfcheck ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Arena-Agents v0.12 UI mock server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier (default 1.0 ≈ 80 s per game)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--selfcheck", action="store_true", help="replay the script offline and validate every event")
    args = parser.parse_args(argv)
    if args.selfcheck:
        return selfcheck()
    import uvicorn

    print(f"Mock UI server: http://{args.host}:{args.port}/  (speed x{args.speed}, seed {args.seed})")
    uvicorn.run(create_app(speed=args.speed, seed=args.seed), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
