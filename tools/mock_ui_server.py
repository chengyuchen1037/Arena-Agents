#!/usr/bin/env python
"""Mock UI server for the v0.12.1 front end (docs/v0.12-contracts.md §8 + §10.3 / §10.4 / §10.6:
SSE v2 events, PublicSession v3, roster / budget / price-source APIs).

It serves ``static/`` and replays one scripted 12-player game (about 60 s) over the SAME wire format
the real backend uses, so the front end can be developed, screenshotted and tested without any
Provider, LLM or game engine:

    PYTHONUTF8=1 .venv/Scripts/python.exe tools/mock_ui_server.py            # http://127.0.0.1:8731/
    PYTHONUTF8=1 .venv/Scripts/python.exe tools/mock_ui_server.py --speed 3  # 3x faster
    PYTHONUTF8=1 .venv/Scripts/python.exe tools/mock_ui_server.py --selfcheck

Click "开始" in the page (or ``POST /api/control/start``) and the script plays: channels are created,
the moderator announces; at night the wolves "拍刀" in a ``share_answers`` request (somebody changes
their mind, the request closes when everybody agrees); a queue of players speaks in turn
(``floor.changed`` advances); a sealed vote; challenge tickets; a free discussion in which the white
wolf king's ability freezes the channel; agents wake, think, act and sleep all the time
(``agent.status`` / ``agent.metrics`` with cache tokens); the token budget warns at 80 % and pauses the
game at 100 % (pause_reason "budget": raise the limit in "⛔ 上限" and press 继续); finally
``game.status = finished`` and "复盘全部" becomes available.  Pause / resume stop and continue the replay
(the game clock stops, D18); after the verdict "继续" replays an epilogue (D8).

Names are the roster's (Alice, Bob, ...; ids N1..N12); the moderator writes "N3 Carol" in its messages.
Avatars are descriptions drawn from the sprite library ``static/avatar/parts.json`` when that file
exists, otherwise from ``tests/frontend/fixtures/parts.min.json`` (which is then also served at the
library's URL, so the page renders pixel avatars either way).

Wire format (contract §8.2): ``GET /api/events`` is ``text/event-stream``; every message is
``id: <event_id>`` + ``data: {"id": N, "type": "...", "data": {...}}`` (no ``event:`` field); the first
message is a ``snapshot``; a reconnect with ``Last-Event-ID`` (or ``?last_event_id=``) is resumed from a
ring buffer when possible, otherwise a ``snapshot`` is sent first; a ``: heartbeat`` comment every 15 s.

Besides the contract endpoints there are a few mock-only helpers under ``/api/mock/*`` (hold the game,
jump to the end, drop all SSE connections, show a stall notice, ...); see ``create_app``.

``validate_event`` / ``validate_session`` check payloads against the §8 / §10.6 shapes: tests (and the
lead, when wiring the real backend) can reuse them.
"""

import argparse
import asyncio
import copy
import heapq
import io
import json
import random
import re
import sys
import uuid
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
PARTS_REAL = STATIC_DIR / "avatar" / "parts.json"
PARTS_FIXTURE = ROOT / "tests" / "frontend" / "fixtures" / "parts.min.json"
DEFAULT_PORT = 8731
APP_VERSION = "v0.12.1-mock"
PACE = 1.0  # plot seconds -> game seconds at speed=1 (the whole script is about 60 s)
MODERATOR = "moderator"
ENGINE = "engine"
DEMO_BUDGET_TOKENS = 150_000  # the demo budget: warns at 80 % (120K), pauses at 100 % (see the notes above)

# --------------------------------------------------------------------------------------------------
# the cast (docs/v0.12.1-plan.md E13: ids N1..N12, default names without numbers)
# --------------------------------------------------------------------------------------------------

DEFAULT_NAMES = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]
PLAYER_IDS = [f"N{i}" for i in range(1, 13)]
SEAT_OF = {pid: i + 1 for i, pid in enumerate(PLAYER_IDS)}
ROLES = {
    "N1": "平民",
    "N2": "平民",
    "N3": "狼人",
    "N4": "预言家",
    "N5": "女巫",
    "N6": "狼人",
    "N7": "猎人",
    "N8": "守卫",
    "N9": "平民",
    "N10": "狼人",
    "N11": "平民",
    "N12": "白狼王",
}
WOLVES = ["N3", "N6", "N10", "N12"]
GODS = ["N4", "N5", "N7", "N8"]
VILLAGERS = ["N1", "N2", "N9", "N11"]
EVERYONE = [MODERATOR, *PLAYER_IDS]

PUBLIC_TAGS = ["存活", "出局", "警长"]
# the real scenario lists the sheriff among the status tags too (scenarios/werewolf_12p.md)
UI = {"out_tags": ["出局"], "status_tags": ["存活", "出局", "警长"], "highlight_tags": ["警长"]}
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

# --------------------------------------------------------------------------------------------------
# providers, models (four prices, E11) and a tiny price table that behaves like litellm's (E12)
# --------------------------------------------------------------------------------------------------

DEFAULT_PRICING_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"

PRICE_TABLE: dict[str, dict[str, Any]] = {
    "deepseek/deepseek-chat": {
        "input_cost_per_token": 2.7e-07,
        "cache_read_input_token_cost": 7e-08,
        "output_cost_per_token": 1.1e-06,
        "max_input_tokens": 128000,
    },
    "deepseek/deepseek-reasoner": {
        "input_cost_per_token": 5.5e-07,
        "cache_read_input_token_cost": 1.4e-07,
        "output_cost_per_token": 2.19e-06,
        "max_input_tokens": 128000,
    },
    "gpt-4o-mini": {
        "input_cost_per_token": 1.5e-07,
        "cache_read_input_token_cost": 7.5e-08,
        "output_cost_per_token": 6e-07,
        "max_input_tokens": 128000,
    },
    "claude-sonnet-4-5-20250929": {
        "input_cost_per_token": 3e-06,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
        "output_cost_per_token": 1.5e-05,
        "max_input_tokens": 200000,
    },
}


def _per_m(v: float | None) -> float | None:
    return None if v is None else round(v * 1_000_000, 6)


def match_price(base_url: str, model_id: str) -> dict[str, Any] | None:
    """The four matching rules of plan §3.3 (exact key, provider/model, without a date suffix,
    case-insensitive) over :data:`PRICE_TABLE`; returns the ``suggested`` / ``match`` payload or None."""
    host = re.sub(r"^https?://", "", str(base_url)).split("/")[0].lower()
    provider = "deepseek" if "deepseek" in host else "anthropic" if "anthropic" in host else "openai" if "openai" in host else ""
    keys = {k.lower(): k for k in PRICE_TABLE}
    candidates = [model_id, f"{provider}/{model_id}" if provider else model_id]
    stripped = re.sub(r"[-_](\d{8}|\d{4}-\d{2}-\d{2})$", "", model_id)
    candidates += [stripped, f"{provider}/{stripped}" if provider else stripped]
    for cand in candidates:
        key = cand if cand in PRICE_TABLE else keys.get(cand.lower())
        if key:
            e = PRICE_TABLE[key]
            return {
                "key": key,
                "input_price_per_m": _per_m(e.get("input_cost_per_token")),
                "output_price_per_m": _per_m(e.get("output_cost_per_token")),
                "cache_read_price_per_m": _per_m(e.get("cache_read_input_token_cost")),
                "cache_write_price_per_m": _per_m(e.get("cache_creation_input_token_cost")),
                "context_window_tokens": e.get("max_input_tokens"),
            }
    return None


def _model(mid: str, pin: float, pcr: float | None, pcw: float | None, pout: float, ctx: int, source: str) -> dict[str, Any]:
    return {
        "id": mid,
        "input_price_per_m": pin,
        "cache_read_price_per_m": pcr,
        "cache_write_price_per_m": pcw,
        "output_price_per_m": pout,
        "context_window_tokens": ctx,
        "price_source": source,
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
            _model("deepseek-chat", 0.27, 0.07, None, 1.1, 128000, "litellm:deepseek/deepseek-chat"),
            _model("deepseek-flash", 0.1, 0.025, None, 0.4, 128000, "manual"),
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
            _model("claude-sonnet", 3.0, 0.3, 3.75, 15.0, 200000, "litellm:claude-sonnet-4-5-20250929"),
        ],
    },
]


def price_tuple(m: dict[str, Any]) -> tuple[float, float, float, float]:
    """(input, cache read, cache write, output): empty cache prices bill at the input price (E11)."""
    pin = float(m["input_price_per_m"])
    cr = m.get("cache_read_price_per_m")
    cw = m.get("cache_write_price_per_m")
    return pin, pin if cr is None else float(cr), pin if cw is None else float(cw), float(m["output_price_per_m"])


PRICE = {m["id"]: price_tuple(m) for p in PROVIDERS for m in p["models"]}
ANTHROPIC_MODELS = {"claude-sonnet"}  # the only protocol that bills cache writes

# a long speech: the engine cuts it at the per-message limit (D19) and the feed shows a trailing "…"
LONG_SPEECH = (
    "我是猎人。昨晚没有任何信息，但我想先把场上的发言逻辑理一遍：N1 的发言太圆滑，几乎没有任何立场；"
    "N2 只是附和，没有独立判断；N9 一直在转移话题。从票型和发言顺序看，N10 的站边最不自然，"
    "我怀疑他是在冲锋之后收手。我愿意把枪口对准 N10，也请预言家尽快报出查验结果，给好人一个明确的方向。"
    "如果今天投不出狼，晚上我们就会被动。请大家认真对待这一轮的归票，不要被带节奏，也不要随意弃票。"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------------------------------
# the roster: names, avatars, persona, memory (persisted by the real backend, in memory here)
# --------------------------------------------------------------------------------------------------


def load_parts() -> dict[str, Any] | None:
    """The sprite library: the real one when it exists, else the test fixture (None if neither does)."""
    for path in (PARTS_REAL, PARTS_FIXTURE):
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def avatar_options(parts: dict[str, Any]) -> dict[str, list[str]]:
    pal = parts.get("palettes", {})
    sprites = parts.get("parts", {})
    return {
        "face": sorted(sprites.get("face", {})),
        "skin": sorted(pal.get("skin", {})),
        "hair": sorted(sprites.get("hair", {})),
        "hair_color": sorted(pal.get("hair_color", {})),
        "eyes": sorted(sprites.get("eyes", {})),
        "mouth": sorted(sprites.get("mouth", {})),
        "accessory": sorted(sprites.get("accessory", {})) + ["none"],
        "accessory_color": sorted(pal.get("accessory_color", {})),
    }


AVATAR_FIELDS = ("face", "skin", "hair", "hair_color", "eyes", "mouth", "accessory", "accessory_color")


def random_avatar(rng: random.Random, parts: dict[str, Any] | None) -> dict[str, Any] | None:
    if not parts:
        return None
    opts = avatar_options(parts)
    if not all(opts[f] for f in AVATAR_FIELDS if f != "accessory_color"):
        return None
    desc: dict[str, Any] = {"v": 1}
    for f in AVATAR_FIELDS:
        desc[f] = rng.choice(opts[f]) if opts[f] else ""
    return desc


def _differences(a: dict[str, Any], b: dict[str, Any]) -> int:
    return sum(1 for f in AVATAR_FIELDS if a.get(f) != b.get(f))


def roster_avatars(rng: random.Random, parts: dict[str, Any] | None, n: int = 12) -> list[dict[str, Any] | None]:
    """n avatars, pairwise different in at least three fields (when the library is big enough)."""
    out: list[dict[str, Any]] = []
    for _ in range(n):
        best = None
        for _try in range(300):
            cand = random_avatar(rng, parts)
            if cand is None:
                return [None] * n
            if all(_differences(cand, o) >= 3 for o in out):
                best = cand
                break
            best = best or cand
        out.append(best)  # type: ignore[arg-type]
    return out  # type: ignore[return-value]


class MockRoster:
    """The 12 seats: nickname, avatar description, persona and memory text; plus the "load memory" switch."""

    def __init__(self, rng: random.Random, parts: dict[str, Any] | None) -> None:
        self.rng = rng
        self.parts = parts
        self.memory_enabled = True
        avatars = roster_avatars(rng, parts, len(PLAYER_IDS) + 1)  # the last one is the moderator's
        # the moderator is not a seat: it only has an avatar of its own (random like the players')
        self.moderator: dict[str, Any] = {"id": MODERATOR, "name": "主持人", "avatar": avatars[len(PLAYER_IDS)]}
        self.seats: dict[str, dict[str, Any]] = {
            pid: {
                "seat": i + 1,
                "id": pid,
                "name": DEFAULT_NAMES[i],
                "default_name": DEFAULT_NAMES[i],
                "avatar": avatars[i],
                "persona": "",
                "memory": "",
            }
            for i, pid in enumerate(PLAYER_IDS)
        }

    def name(self, pid: str) -> str:
        return self.seats[pid]["name"] if pid in self.seats else pid

    def ref(self, pid: str) -> str:
        """What the models (and the moderator's messages) call a player: "N3 Carol"."""
        return f"{pid} {self.name(pid)}" if pid in self.seats else pid

    def entries(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(self.seats[pid]) for pid in PLAYER_IDS]

    def public(self, pid: str) -> dict[str, Any]:
        s = self.seats[pid]
        return {"id": pid, "name": s["name"], "avatar": copy.deepcopy(s["avatar"])}

    def validate_name(self, pid: str, name: Any) -> str | None:
        if not isinstance(name, str):
            return "名字必须是文字"
        n = name.strip()
        if not n:
            return "名字不能为空"
        if len(n) > 20:
            return "名字最多 20 个字符"
        if any(s["name"] == n and p != pid for p, s in self.seats.items()):
            return f"名字「{n}」已被其他玩家使用"
        return None

    def regenerate(self, pid: str | None = None) -> list[str]:
        """New avatars: everybody (the twelve seats and the moderator, picked together) or one seat / the moderator."""
        if pid is None:
            avatars = roster_avatars(self.rng, self.parts, len(PLAYER_IDS) + 1)
            for p, a in zip(PLAYER_IDS, avatars):
                self.seats[p]["avatar"] = a
            self.moderator["avatar"] = avatars[len(PLAYER_IDS)]
            return [*PLAYER_IDS, MODERATOR]
        others = [s["avatar"] for p, s in self.seats.items() if p != pid and s["avatar"]]
        if pid != MODERATOR and self.moderator["avatar"]:
            others.append(self.moderator["avatar"])
        best = None
        for _ in range(300):
            cand = random_avatar(self.rng, self.parts)
            if cand is None:
                break
            if all(_differences(cand, o) >= 3 for o in others):
                best = cand
                break
            best = best or cand
        if pid == MODERATOR:
            self.moderator["avatar"] = best
        else:
            self.seats[pid]["avatar"] = best
        return [pid]

    def export_zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for pid in PLAYER_IDS:
                s = self.seats[pid]
                z.writestr(f"{pid}.persona.md", s["persona"])
                z.writestr(f"{pid}.memory.md", s["memory"])
        return buf.getvalue()

    def import_zip(self, data: bytes, *, apply: bool) -> list[dict[str, Any]]:
        """Parse a memory pack; returns the changes [{id, persona, memory}]. Raises ValueError for bad packs."""
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ValueError("不是有效的 zip 记忆包") from exc
        changes: dict[str, dict[str, Any]] = {}
        incoming: dict[str, dict[str, str]] = {}
        for info in z.infolist():
            if info.is_dir():
                continue
            m = re.fullmatch(r"(N\d{1,2})\.(persona|memory)\.md", info.filename)
            if not m or m.group(1) not in self.seats:
                raise ValueError(f"记忆包里有不在名册内的文件：{info.filename}")
            if info.file_size > 256 * 1024:
                raise ValueError(f"文件太大：{info.filename}")
            text = z.read(info).decode("utf-8", errors="replace")
            incoming.setdefault(m.group(1), {})[m.group(2)] = text
        for pid, parts in incoming.items():
            diff = {k: parts[k] != self.seats[pid][k] for k in parts}
            if any(diff.values()):
                changes[pid] = {"id": pid, "persona": bool(diff.get("persona")), "memory": bool(diff.get("memory"))}
            if apply:
                for k, v in parts.items():
                    self.seats[pid][k] = v
        return [changes[p] for p in PLAYER_IDS if p in changes]

    def clear(self, include_persona: bool) -> None:
        for s in self.seats.values():
            s["memory"] = ""
            if include_persona:
                s["persona"] = ""


# --------------------------------------------------------------------------------------------------
# contract shapes (§8.1 / §10.6) and a validator for the §8.2 events
# --------------------------------------------------------------------------------------------------

MSG_KINDS = {"speech", "announce", "template", "challenge", "reply", "ability", "system"}
AGENT_STATES = {"thinking", "acting", "sleeping"}
GAME_STATUSES = {"idle", "running", "paused", "finished"}
PAUSE_REASONS = {"user", "hold", "error", "stall", "budget", ""}
FLOOR_KINDS = {"turn", "queue", "free", "none"}
CHANNEL_MODES = {"closed", "turn", "queue", "free", "listen"}
REQUEST_KINDS = {"choice", "text"}
AUTO_CLOSE = {"all_answered", "unanimous", "deadline"}
CLOSE_REASONS = {"all_answered", "unanimous", "deadline", "cancelled"}
BUDGET_STATES = {"ok", "warning", "reached"}
REVIEW_STATES = {"running", "done", "error"}
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
    "title",
    "budget",
    "memory_enabled",
    "review",
    "stall_notice",
}
METRIC_FIELDS = (
    "activations",
    "calls",
    "prompt_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "completion_tokens",
    "cost_usd",
    "tool_calls",
    "thinking_rounds",
    "rejections",
    "cache_hit_rate",
)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _opt_str(v: Any) -> bool:
    return v is None or isinstance(v, str)


def _opt_num(v: Any) -> bool:
    return v is None or _is_num(v)


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


def validate_answer(a: Any, where: str = "answer") -> list[str]:
    if not (isinstance(a, dict) and isinstance(a.get("player_id"), str) and "value" in a
            and isinstance(a.get("label"), str) and isinstance(a.get("reason"), str) and isinstance(a.get("defaulted"), bool)):
        return [where]
    return []


def validate_result(r: Any, where: str = "result") -> list[str]:
    if r is None:
        return []
    if not (isinstance(r, dict) and isinstance(r.get("reason"), str) and _str_list(r.get("defaulted")) and _str_list(r.get("missing"))
            and (r.get("tally") is None or (isinstance(r["tally"], dict) and all(_is_int(v) for v in r["tally"].values())))
            and (r.get("leaders") is None or _str_list(r["leaders"]))):
        return [where]
    return []


def validate_request(r: Any, where: str = "request") -> list[str]:
    """RequestView (§10.6) + opened_seq (the front end places the card in the feed by it)."""
    errs: list[str] = []
    if not isinstance(r, dict):
        return [f"{where}: not an object"]
    if not isinstance(r.get("id"), str):
        errs.append(f"{where}.id")
    if r.get("kind") not in REQUEST_KINDS:
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
    if not isinstance(r.get("share_answers"), bool):
        errs.append(f"{where}.share_answers")
    if r.get("auto_close") not in AUTO_CLOSE:
        errs.append(f"{where}.auto_close")
    if r.get("seconds_left") is not None and not _is_num(r.get("seconds_left")):
        errs.append(f"{where}.seconds_left")
    if not _is_int(r.get("opened_seq")):
        errs.append(f"{where}.opened_seq")
    if not isinstance(r.get("answers"), list):
        errs.append(f"{where}.answers")
    else:
        for i, a in enumerate(r["answers"]):
            errs += validate_answer(a, f"{where}.answers[{i}]")
    errs += validate_result(r.get("result"), f"{where}.result")
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


def validate_budget(b: Any, where: str = "budget") -> list[str]:
    if not (isinstance(b, dict) and (b.get("max_tokens") is None or _is_int(b["max_tokens"]))
            and _opt_num(b.get("max_cost_usd")) and isinstance(b.get("force_stop"), bool)
            and _is_num(b.get("used_tokens")) and _is_num(b.get("used_cost_usd")) and b.get("state") in BUDGET_STATES):
        return [where]
    return []


def validate_review(r: Any, where: str = "review") -> list[str]:
    if not (isinstance(r, dict) and isinstance(r.get("available"), bool) and _str_list(r.get("done")) and isinstance(r.get("running"), bool)):
        return [where]
    return []


def validate_avatar(a: Any, where: str = "avatar") -> list[str]:
    if a is None:
        return []
    if not (isinstance(a, dict) and a.get("v") == 1 and all(isinstance(a.get(f), str) for f in AVATAR_FIELDS)):
        return [where]
    return []


def validate_session(s: Any) -> list[str]:
    """PublicSession v3 (§10.6)."""
    errs: list[str] = []
    if not isinstance(s, dict):
        return ["session: not an object"]
    for k in ("session_id", "scenario_name", "phase_text", "active_status_text", "summary", "hold_reason", "pause_reason", "stall_notice"):
        if not isinstance(s.get(k), str):
            errs.append(f"session.{k}")
    if s.get("status") not in GAME_STATUSES:
        errs.append("session.status")
    if s.get("pause_reason") not in PAUSE_REASONS:
        errs.append("session.pause_reason")
    for k in ("seq", "event_id"):
        if not _is_int(s.get(k)):
            errs.append(f"session.{k}")
    if "execution_depth" in s:
        errs.append("session.execution_depth: removed in v0.12.1")
    if not isinstance(s.get("participants"), list):
        errs.append("session.participants")
    else:
        for i, p in enumerate(s["participants"]):
            if not (isinstance(p, dict) and isinstance(p.get("id"), str) and (_is_int(p.get("seat")) or isinstance(p.get("seat"), str))
                    and isinstance(p.get("name"), str) and _str_list(p.get("tags")) and isinstance(p.get("out"), bool)
                    and p.get("state") in AGENT_STATES and _opt_num(p.get("sleep_until"))):
                errs.append(f"session.participants[{i}]")
            else:
                errs += validate_avatar(p.get("avatar"), f"session.participants[{i}].avatar")
    mod = s.get("moderator")
    if not (isinstance(mod, dict) and mod.get("id") == MODERATOR and mod.get("state") in AGENT_STATES and _opt_num(mod.get("sleep_until"))):
        errs.append("session.moderator")
    else:
        errs += validate_avatar(mod.get("avatar"), "session.moderator.avatar")
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
    for k in ("prompt_tokens", "cache_read_tokens", "cache_write_tokens", "completion_tokens", "total_tokens", "total_cost_usd", "cache_hit_rate"):
        if not _is_num(usage.get(k)):
            errs.append(f"session.usage_stats.{k}")
    for aid, m in (usage.get("by_actor") or {}).items():
        if not (isinstance(m, dict) and all(_is_num(m.get(k)) for k in METRIC_FIELDS)):
            errs.append(f"session.usage_stats.by_actor.{aid}")
    errs += validate_budget(s.get("budget"), "session.budget")
    if not isinstance(s.get("memory_enabled"), bool):
        errs.append("session.memory_enabled")
    errs += validate_review(s.get("review"), "session.review")
    return errs


def validate_event(ev: Any) -> list[str]:
    """Problems of one SSE v2 payload ``{"id", "type", "data"}`` against contract §8.2 / §10.6 (empty = valid)."""
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
            errs.append(f"{w}: carries extra keys (answers travel in request.answer)")
    elif etype == "request.answer":
        if not (isinstance(data.get("request_id"), str) and isinstance(data.get("player_id"), str) and "value" in data
                and isinstance(data.get("label"), str) and isinstance(data.get("reason"), str) and _str_list(data.get("visible_to_ids"))):
            errs.append(w)
    elif etype == "request.closed":
        if not (isinstance(data.get("request_id"), str) and data.get("reason") in CLOSE_REASONS
                and _str_list(data.get("answered_by")) and _str_list(data.get("defaulted"))):
            errs.append(w)
        errs += validate_result(data.get("result"), f"{w}.result")
        if data.get("result") is None:
            errs.append(f"{w}.result: missing")
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
        if not (isinstance(data.get("id"), str) and data.get("state") in AGENT_STATES and "until" in data and _opt_num(data.get("until"))):
            errs.append(w)
        elif data.get("until") is not None and data.get("state") != "sleeping":
            errs.append(f"{w}: only a sleeping agent has a wake-up time")
    elif etype == "agent.metrics":
        if not isinstance(data.get("id"), str) or not all(_is_num(data.get(k)) for k in METRIC_FIELDS):
            errs.append(w)
    elif etype in ("budget.updated", "budget.warning", "budget.reached"):
        errs += validate_budget(data.get("budget"), f"{w}.budget")
    elif etype == "roster.updated":
        ps = data.get("participants")
        if not (isinstance(ps, list) and all(isinstance(p, dict) and isinstance(p.get("id"), str) and isinstance(p.get("name"), str)
                                            and not validate_avatar(p.get("avatar")) for p in ps)):
            errs.append(w)
        mod = data.get("moderator")  # the moderator's avatar (it is not a participant): optional
        if mod is not None and not (isinstance(mod, dict) and mod.get("id") == MODERATOR and isinstance(mod.get("avatar"), dict)
                                    and not validate_avatar(mod.get("avatar"))):
            errs.append(f"{w}.moderator")
    elif etype == "review.progress":
        if not (isinstance(data.get("id"), str) and data.get("state") in REVIEW_STATES and isinstance(data.get("message"), str)):
            errs.append(w)
    elif etype == "session.updated":
        if not data or set(data) - SESSION_UPDATED_KEYS:
            errs.append(f"{w}: keys {sorted(set(data) - SESSION_UPDATED_KEYS)}")
        if "budget" in data:
            errs += validate_budget(data["budget"], f"{w}.budget")
        if "review" in data:
            errs += validate_review(data["review"], f"{w}.review")
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


class VirtualLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock jumps to the next timer instead of sleeping.

    Offline replays (tests, ``--selfcheck``) run the whole 60 s script in milliseconds and, because no
    real time is involved, produce exactly the same events every time."""

    def __init__(self) -> None:
        super().__init__()
        self._vtime = 0.0
        # a timer is "due" when `when < time() + resolution`: it must be positive, or a timer that fires
        # exactly at the (virtual) now is never released and the loop spins forever
        self._clock_resolution = 1e-9

    def time(self) -> float:
        return self._vtime

    def _run_once(self) -> None:  # type: ignore[override]
        if not self._ready and self._scheduled:
            live = [h._when for h in self._scheduled if not h._cancelled]  # noqa: SLF001
            if live:
                self._vtime = max(self._vtime, min(live))
        super()._run_once()


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


def empty_usage() -> dict[str, Any]:
    return {
        "prompt_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
        "cache_hit_rate": 0.0,
        "by_model": {},
        "by_actor": {},
    }


def default_budget(max_tokens: int | None = DEMO_BUDGET_TOKENS, force_stop: bool = True) -> dict[str, Any]:
    return {
        "max_tokens": max_tokens,
        "max_cost_usd": None,
        "force_stop": force_stop,
        "used_tokens": 0,
        "used_cost_usd": 0.0,
        "state": "ok",
    }


def build_session(session_id: str, bindings: dict[str, Any], roster: MockRoster, budget: dict[str, Any], *, demo: bool = False) -> dict[str, Any]:
    """A fresh (idle) game: players dealt by the engine (D9/D24) and a system DM per player (D23)."""
    participants = [
        {
            "id": pid,
            "seat": i + 1,
            "name": roster.name(pid),
            "avatar": copy.deepcopy(roster.seats[pid]["avatar"]),
            "tags": [ROLES[pid], "存活"],
            "out": False,
            "state": "sleeping",
            "sleep_until": None,
        }
        for i, pid in enumerate(PLAYER_IDS)
    ]
    channels = [make_channel(f"dm-{pid}", f"{pid} 私聊", [MODERATOR, pid], mode="free", system=True, owner=pid) for pid in PLAYER_IDS]
    b = copy.deepcopy(budget)
    b.update({"used_tokens": 0, "used_cost_usd": 0.0, "state": "ok"})
    return {
        "session_id": session_id,
        "schema_version": 4,
        "version": "v0.12.1",
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
        "moderator": {
            "id": MODERATOR,
            "name": "主持人",
            "avatar": copy.deepcopy(roster.moderator["avatar"]),
            "state": "sleeping",
            "sleep_until": None,
        },
        "channels": channels,
        "messages": [],
        "requests": [],
        "tickets": [],
        "dashboard": {"factions": [], "tag_stats": []},
        "public_tags": list(PUBLIC_TAGS),
        "tag_styles": copy.deepcopy(TAG_STYLES),
        "ui": copy.deepcopy(UI),
        "usage_stats": empty_usage(),
        "model_bindings": copy.deepcopy(bindings),
        "binding_issues": [],
        "restored_from_save": False,
        "restore_notice": "",
        "autosave_error": "",
        "budget": b,
        "memory_enabled": roster.memory_enabled,
        "review": {"available": False, "done": [], "running": False},
        "stall_notice": "",
        "demo": demo,
    }


class MockGame:
    """Holds one PublicSession (god view), mutates it as the script plays and publishes the SSE v2
    events that describe every change (so a client that applies the events ends up with the same
    session as :attr:`session`; ``tests/frontend/test_mock_server.py`` checks exactly that)."""

    def __init__(self, bus: EventBus, *, speed: float = 1.0, seed: int = 7, roster: MockRoster | None = None,
                 budget: dict[str, Any] | None = None, demo: bool = False) -> None:
        self.bus = bus
        self.speed = max(0.01, float(speed))
        self.seed = seed
        self.rng = random.Random(seed)
        self.demo = demo
        self.roster = roster if roster is not None else MockRoster(random.Random(seed + 101), load_parts())
        self.budget_limits = copy.deepcopy(budget if budget is not None else default_budget())
        self.bindings = default_bindings()
        self.session = build_session(uuid.uuid4().hex[:12], self.bindings, self.roster, self.budget_limits, demo=demo)
        self.gate = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.bg: set[asyncio.Task[None]] = set()
        self.epilogues = 0
        self.review_task: asyncio.Task[None] | None = None
        self.on_budget_pause: Any = None  # replay hook: called (sync) right after a budget pause
        self._ticket_n = 0
        self._request_n = 0
        self.intent: dict[str, tuple[str, float | None]] = {}  # what each agent is doing (restored on resume)
        self.deadlines: dict[str, float] = {}  # request id -> game-clock second it closes at
        self.reviewed: dict[str, bool] = {}

    # ----------------------------------------------------------------------------- plumbing
    def emit(self, etype: str, **data: Any) -> None:
        ev = self.bus.publish(etype, data)
        self.session["event_id"] = ev["id"]

    def snapshot_payload(self) -> dict[str, Any]:
        self.session["event_id"] = self.bus.latest_id
        for r in self.session["requests"]:
            if r["status"] == "open" and r["id"] in self.deadlines:
                r["seconds_left"] = round(max(0.0, self.deadlines[r["id"]] - self.session["clock"]), 2)
        return copy.deepcopy(self.session)

    @property
    def clock(self) -> float:
        return float(self.session["clock"])

    async def wait(self, seconds: float) -> None:
        """Sleep ``seconds`` of GAME time: the clock stops while paused (D18)."""
        remaining = max(0.0, seconds) * PACE / self.speed
        while True:
            await self.gate.wait()
            if remaining <= 0:
                return
            step = min(0.05, remaining)
            await asyncio.sleep(step)
            if not self.gate.is_set():
                continue  # paused during this step: it does not count, the game clock stands still (D18)
            self.session["clock"] = round(self.session["clock"] + step * self.speed, 3)
            remaining -= step

    def name(self, pid: str) -> str:
        if pid == MODERATOR:
            return "主持人"
        if pid == ENGINE:
            return "系统"
        return self.roster.name(pid)

    def ref(self, pid: str) -> str:
        return self.roster.ref(pid)

    def player(self, pid: str) -> dict[str, Any]:
        return next(p for p in self.session["participants"] if p["id"] == pid)

    def channel(self, cid: str) -> dict[str, Any]:
        return next(c for c in self.session["channels"] if c["id"] == cid)

    def alive(self) -> list[str]:
        return [p["id"] for p in self.session["participants"] if not p["out"]]

    def spawn_bg(self, coro: Any) -> None:
        async def runner() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - a bug in the script must be visible
                print(f"mock background task crashed: {exc!r}", file=sys.stderr)

        task = asyncio.get_running_loop().create_task(runner())
        self.bg.add(task)
        task.add_done_callback(self.bg.discard)

    async def drain_bg(self) -> None:
        while self.bg:
            await asyncio.gather(*list(self.bg), return_exceptions=True)

    # ----------------------------------------------------------------------------- session-level
    def publish_session_updated(self, *extra: str) -> None:
        s = self.session
        data = {
            "status": s["status"],
            "pause_reason": s["pause_reason"],
            "hold_reason": s["hold_reason"],
            "winner": s["winner"],
            "summary": s["summary"],
            "active_status_text": s["active_status_text"],
            "model_bindings": copy.deepcopy(s["model_bindings"]),
            "binding_issues": copy.deepcopy(s["binding_issues"]),
            "restored_from_save": s["restored_from_save"],
            "restore_notice": s["restore_notice"],
            "autosave_error": s["autosave_error"],
            "title": s["title"],
            "budget": copy.deepcopy(s["budget"]),
            "memory_enabled": s["memory_enabled"],
            "review": copy.deepcopy(s["review"]),
            "stall_notice": s["stall_notice"],
        }
        self.emit("session.updated", **data)

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
            s["review"]["available"] = False
        self.emit("game.status", status=status, reason=reason, winner=s["winner"], summary=s["summary"])
        text = {
            "running": "对局进行中",
            "paused": (
                f"已暂停 · 主持人：{hold_reason}" if pause_reason == "hold" and hold_reason
                else "已暂停 · 因预算上限暂停" if pause_reason == "budget" else "已暂停"
            ),
            "finished": "对局已结束",
            "idle": "未开始 · 点击「开始」由主持人初始化对局",
        }[status]
        s["active_status_text"] = text
        self.publish_session_updated()

    def set_agent(self, aid: str, state: str, until: float | None = None) -> None:
        """thinking / acting / sleeping (``until``: game-clock second a timed sleep ends)."""
        until = until if state == "sleeping" else None
        node = self.session["moderator"] if aid == MODERATOR else self.player(aid)
        self.intent[aid] = (state, until)
        if self.session["status"] == "paused":
            return  # a paused game keeps everybody asleep; the intent is restored on resume
        if node["state"] == state and node.get("sleep_until") == until:
            return
        node["state"] = state
        node["sleep_until"] = until
        self.emit("agent.status", id=aid, state=state, until=until)

    def sleep(self, aid: str, seconds: float | None = None) -> None:
        self.set_agent(aid, "sleeping", None if seconds is None else round(self.clock + seconds * PACE, 3))

    def set_all_agents(self, state: str) -> None:
        for aid in [MODERATOR, *PLAYER_IDS]:
            self.set_agent(aid, state)

    def stir(self, exclude: set[str], k: int = 3) -> None:
        """A new message wakes agents (E1): a few of the sleeping ones think for a moment and go back to sleep."""
        pool = [a for a in [MODERATOR, *self.alive()] if a not in exclude]
        pool = [a for a in pool if (self.session["moderator"] if a == MODERATOR else self.player(a))["state"] == "sleeping"]
        for aid in self.rng.sample(pool, min(k, len(pool))):
            self.spawn_bg(self._mini_activation(aid))

    async def _mini_activation(self, aid: str) -> None:
        try:
            self.set_agent(aid, "thinking")
            await self.wait(self.rng.uniform(0.4, 1.5))
            self.set_agent(aid, "acting")
            await self.wait(self.rng.uniform(0.15, 0.4))
            self.meter(aid, tools=self.rng.randint(1, 2), think=1)
            # most go back to waiting for the next message, some set an alarm
            self.sleep(aid, self.rng.choice([None, None, None, 8, 15]))
        except asyncio.CancelledError:
            self.set_agent(aid, "sleeping")
            raise

    # ----------------------------------------------------------------------------- tokens & budget
    def meter(self, aid: str, *, tools: int | None = None, think: int | None = None, reject: float = 0.05) -> None:
        """Account one LLM call for ``aid`` (four token kinds, E11) and publish its absolute metrics."""
        usage = self.session["usage_stats"]
        zero = {"activations": 0, "calls": 0, "prompt_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                "completion_tokens": 0, "cost_usd": 0.0, "tool_calls": 0, "thinking_rounds": 0, "rejections": 0, "cache_hit_rate": 0.0}
        m = usage["by_actor"].setdefault(aid, dict(zero))
        r = self.rng
        big = aid == MODERATOR
        model = self.session["model_bindings"].get(aid, {}).get("model_id", "deepseek-flash")
        pin, pcr, pcw, pout = PRICE.get(model, (0.1, 0.025, 0.1, 0.4))
        total_in = r.randint(2600, 4800) if big else r.randint(1100, 2800)
        first = m["calls"] == 0
        # the conversation is append-only: from the second call on most of the input is a cache hit (E5)
        hit = 0.0 if first else min(0.9, 0.4 + 0.06 * m["calls"])
        cache_read = int(total_in * hit)
        cache_write = int(total_in * 0.55) if first and model in ANTHROPIC_MODELS else 0
        miss = total_in - cache_read - cache_write
        ctok = r.randint(280, 760) if big else r.randint(90, 460)
        cost = (miss * pin + cache_read * pcr + cache_write * pcw + ctok * pout) / 1_000_000
        m["activations"] += 1
        m["calls"] += 1
        m["prompt_tokens"] += miss
        m["cache_read_tokens"] += cache_read
        m["cache_write_tokens"] += cache_write
        m["completion_tokens"] += ctok
        m["cost_usd"] = round(m["cost_usd"] + cost, 6)
        m["tool_calls"] += tools if tools is not None else r.randint(1, 3 if not big else 5)
        m["thinking_rounds"] += think if think is not None else r.randint(1, 2)
        if r.random() < reject:
            m["rejections"] += 1
        side = m["prompt_tokens"] + m["cache_read_tokens"] + m["cache_write_tokens"]
        m["cache_hit_rate"] = round(m["cache_read_tokens"] / side, 4) if side else 0.0
        usage["prompt_tokens"] += miss
        usage["cache_read_tokens"] += cache_read
        usage["cache_write_tokens"] += cache_write
        usage["completion_tokens"] += ctok
        usage["total_tokens"] += miss + cache_read + cache_write + ctok
        usage["total_cost_usd"] = round(usage["total_cost_usd"] + cost, 6)
        uside = usage["prompt_tokens"] + usage["cache_read_tokens"] + usage["cache_write_tokens"]
        usage["cache_hit_rate"] = round(usage["cache_read_tokens"] / uside, 4) if uside else 0.0
        bm = usage["by_model"].setdefault(model, {"calls": 0, "prompt_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                                                   "completion_tokens": 0, "cost_usd": 0.0})
        bm["calls"] += 1
        bm["prompt_tokens"] += miss
        bm["cache_read_tokens"] += cache_read
        bm["cache_write_tokens"] += cache_write
        bm["completion_tokens"] += ctok
        bm["cost_usd"] = round(bm["cost_usd"] + cost, 6)
        self.emit("agent.metrics", id=aid, **{k: m[k] for k in METRIC_FIELDS})
        self.refresh_budget()

    def budget_state(self) -> str:
        b = self.session["budget"]
        ratios = []
        if b["max_tokens"]:
            ratios.append(b["used_tokens"] / b["max_tokens"])
        if b["max_cost_usd"]:
            ratios.append(b["used_cost_usd"] / b["max_cost_usd"])
        r = max(ratios) if ratios else 0.0
        return "reached" if r >= 1.0 else "warning" if r >= 0.8 else "ok"

    def refresh_budget(self) -> None:
        """After every call: used numbers, 80 % warning, 100 % reached (+ pause when "force stop" is on)."""
        b = self.session["budget"]
        usage = self.session["usage_stats"]
        b["used_tokens"] = usage["total_tokens"]
        b["used_cost_usd"] = round(usage["total_cost_usd"], 6)
        prev = b["state"]
        b["state"] = self.budget_state()
        self.emit("budget.updated", budget=copy.deepcopy(b))
        if b["state"] != prev:
            if b["state"] == "warning":
                self.emit("budget.warning", budget=copy.deepcopy(b))
            elif b["state"] == "reached":
                self.emit("budget.reached", budget=copy.deepcopy(b))
                if b["force_stop"] and self.session["status"] == "running":
                    self.pause(reason="budget")
                    if self.on_budget_pause is not None:
                        self.on_budget_pause(self)

    def set_budget(self, *, max_tokens: int | None, max_cost_usd: float | None, force_stop: bool) -> dict[str, Any]:
        self.budget_limits.update({"max_tokens": max_tokens, "max_cost_usd": max_cost_usd, "force_stop": force_stop})
        b = self.session["budget"]
        b.update({"max_tokens": max_tokens, "max_cost_usd": max_cost_usd, "force_stop": force_stop})
        self.refresh_budget()
        return copy.deepcopy(b)

    # ----------------------------------------------------------------------------- world actions
    def commit(self, cid: str, sender: str, content: str, kind: str = "speech", *, template_id: str | None = None,
               truncated: dict[str, int] | None = None, ticket_id: str | None = None, visible: list[str] | None = None) -> dict[str, Any]:
        s = self.session
        ch = self.channel(cid)
        s["seq"] += self.rng.randint(1, 3)  # other ledger entries (floor.*, request.*, ...) take seqs too
        msg = {
            "seq": s["seq"],
            "ts": now_iso(),
            "channel_id": cid,
            "from": sender,
            "from_name": self.name(sender),
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
        self.stir({MODERATOR}, 3)

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
                self.set_agent(holder, "thinking")
                await self.wait(pass_delay)  # `pass`: the token ends without a message
                self.sleep(holder)
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
        """A player: thinking -> acting -> a whole message (never partial) -> asleep again."""
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
        self.sleep(pid)
        if cid in ("public", "wolves"):
            self.stir({pid}, 3)  # everybody who can read it wakes up (E1)

    async def moderator_step(self, think: float = 0.8, status: str | None = None) -> None:
        if status:
            self.status_text(status)
        self.set_agent(MODERATOR, "thinking")
        await self.wait(think)
        self.set_agent(MODERATOR, "acting")
        self.meter(MODERATOR)
        await self.wait(0.3)

    def moderator_idle(self, seconds: float | None = None) -> None:
        self.sleep(MODERATOR, seconds)

    # --- requests (E18 / E25)
    def option_label(self, pid: str) -> str:
        return f"{SEAT_OF[pid]}号 {self.name(pid)}"

    def open_request(self, question: str, options: list[str], to: list[str], *, sealed: bool = True, kind: str = "choice",
                     share_answers: bool = False, auto_close: str = "all_answered", seconds: float = 90.0) -> str:
        self._request_n += 1
        rid = f"r{self._request_n}"
        self.session["seq"] += 1
        req = {
            "id": rid,
            "kind": kind,
            "question": question,
            "options": [{"id": o, "label": self.option_label(o)} for o in options],
            "to": list(to),
            "answered_by": [],
            "status": "open",
            "sealed": sealed and not share_answers,
            "share_answers": share_answers,
            "auto_close": auto_close,
            "seconds_left": float(seconds),
            "opened_seq": self.session["seq"],
            "answers": [],
            "result": None,
        }
        self.session["requests"].append(req)
        self.deadlines[rid] = round(self.clock + seconds, 3)
        self.emit("request.opened", request=copy.deepcopy(req))
        return rid

    def _request(self, rid: str) -> dict[str, Any]:
        return next(r for r in self.session["requests"] if r["id"] == rid)

    def request_answer(self, rid: str, pid: str, value: str, *, reason: str = "") -> None:
        """One recipient answers (or changes the answer, share_answers). Sealed answers are visible to the
        moderator only; shared ones to every recipient; ``request.progress`` follows each answer."""
        req = self._request(rid)
        if req["status"] != "open":
            return
        label = value
        if req["kind"] == "choice":
            label = next((o["label"] for o in req["options"] if o["id"] == value), value)
        ans = {"player_id": pid, "value": value, "label": label, "reason": reason, "defaulted": False}
        i = next((k for k, a in enumerate(req["answers"]) if a["player_id"] == pid), -1)
        if i >= 0:
            req["answers"][i] = ans
        else:
            req["answers"].append(ans)
        req["answers"].sort(key=lambda a: PLAYER_IDS.index(a["player_id"]))
        if pid not in req["answered_by"]:
            req["answered_by"].append(pid)
        req["answered_by"].sort(key=PLAYER_IDS.index)
        visible = [MODERATOR, *req["to"]] if req["share_answers"] else [MODERATOR]
        self.emit("request.answer", request_id=rid, player_id=pid, value=value, label=label, reason=reason, visible_to_ids=visible)
        self.emit("request.progress", request_id=rid, answered_by=list(req["answered_by"]), total=len(req["to"]))
        everyone = len(req["answered_by"]) == len(req["to"])
        values = {a["value"] for a in req["answers"]}
        if req["auto_close"] == "unanimous" and everyone and len(values) == 1:
            self.close_request(rid, "unanimous")
        elif req["auto_close"] == "all_answered" and everyone:
            self.close_request(rid, "all_answered")

    def close_request(self, rid: str, reason: str = "all_answered") -> None:
        req = self._request(rid)
        if req["status"] != "open":
            return
        req["status"] = "cancelled" if reason == "cancelled" else "closed"
        req["seconds_left"] = None
        self.deadlines.pop(rid, None)
        tally = None
        leaders = None
        if req["kind"] == "choice":
            tally = {o["id"]: 0 for o in req["options"]}
            for a in req["answers"]:
                tally[a["value"]] = tally.get(a["value"], 0) + 1
            top = max(tally.values()) if tally else 0
            leaders = [k for k, v in tally.items() if v == top and top > 0]
        missing = [p for p in req["to"] if p not in req["answered_by"]]
        result = {"reason": reason, "tally": tally, "leaders": leaders, "defaulted": [], "missing": missing}
        req["result"] = result
        self.emit("request.closed", request_id=rid, reason=reason, answered_by=list(req["answered_by"]), defaulted=[], result=copy.deepcopy(result))

    async def vote(self, to: list[str], question: str, options: list[str], votes: dict[str, str]) -> str:
        """All voters think at once; their (sealed) answers trickle in in a random order."""
        rid = self.open_request(question, options, to, sealed=True)
        for pid in to:
            self.set_agent(pid, "thinking")

        async def one(pid: str, think: float) -> None:
            await self.wait(think)
            self.set_agent(pid, "acting")
            await self.wait(0.15)
            self.meter(pid, tools=1, think=1)
            self.request_answer(rid, pid, votes[pid])
            self.sleep(pid)

        thinks = [0.4 + self.rng.random() * 2.2 for _ in to]
        await asyncio.gather(*(one(pid, t) for pid, t in zip(to, thinks)))
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
        self.sleep(pid)
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
            self.sleep(pid)  # D25: the floor was taken back: the engine cancelled this player's thinking
            raise

    async def _delayed(self, seconds: float, coro: Any) -> Any:
        await self.wait(seconds)
        return await coro

    async def play(self) -> None:
        ref = self.ref
        # 1. set-up: channels, dashboard, announcements
        await self.moderator_step(1.0, "主持人：读取规则，建立频道")
        self.create_channel("public", "公共频道", EVERYONE)
        await self.wait(0.4)
        self.create_channel("wolves", "狼人阵营", [MODERATOR, *WOLVES])
        await self.wait(0.3)
        self.set_phase("准备阶段 · 发牌与建台")
        self.update_dashboard()
        self.commit("public", ENGINE, "对局开始：12 名玩家与主持人已就绪，计时器启动。", "system")
        await self.wait(0.4)
        self.announce("public", "game_start", "【对局开始】本局为 12 人暗牌局：3 狼人 + 白狼王、4 平民、预言家、女巫、猎人、守卫。出局公告不公布底牌。")
        self.commit("wolves", MODERATOR, f"狼人队友为 {'、'.join(ref(w) for w in WOLVES)}；夜间请在这里商议刀口，允许自刀或空刀。")
        self.moderator_idle()
        await self.wait(0.6)

        # 2. night 1: the wolves "拍刀" (a request whose answers they all see, changeable until unanimous)
        await self.moderator_step(0.8, "主持人：第 1 夜 · 狼人商议（拍刀）")
        self.set_phase("第 1 夜 · 黑夜")
        self.announce("public", "night_start", "【天黑请闭眼】第 1 夜开始。")
        targets = [p for p in PLAYER_IDS if p not in WOLVES]
        rid = self.open_request("今晚的刀口是谁？请在这里拍刀，随时可以改，全员一致就进入下一环节。", targets, WOLVES,
                                share_answers=True, auto_close="unanimous", seconds=120.0)
        self.commit("wolves", MODERATOR, "【拍刀】本轮商议 2 分钟：请对上面的请求作答，你们能互相看到当前的选择；全员一致就收口，否则到时按多数。")
        self.moderator_idle(20)  # a timed sleep: look again in 20 s to nudge the wolves

        async def wolf(pid: str, target: str, think: float, change: tuple[str, float] | None = None) -> None:
            self.set_agent(pid, "thinking")
            await self.wait(think)
            self.set_agent(pid, "acting")
            await self.wait(0.2)
            self.meter(pid, tools=1)
            self.request_answer(rid, pid, target)
            self.sleep(pid)
            if change:
                await self.wait(change[1])  # sees the others' choices, changes its mind
                self.set_agent(pid, "thinking")
                await self.wait(0.7)
                self.set_agent(pid, "acting")
                await self.wait(0.2)
                self.meter(pid, tools=1)
                self.request_answer(rid, pid, change[0])
                self.sleep(pid)

        await asyncio.gather(
            wolf("N3", "N5", 1.0),
            wolf("N6", "N4", 1.6, change=("N5", 2.6)),
            wolf("N10", "N5", 2.0),
            wolf("N12", "N8", 2.4, change=("N5", 3.4)),
        )
        await self.moderator_step(0.8, "主持人：宣布刀口，回复预言家与女巫")
        self.announce("wolves", "kill_target", f"今晚刀口：{ref('N5')}。")
        self.commit("dm-N4", MODERATOR, f"【查验结果】{ref('N3')} 是狼人阵营。", "speech", visible=[MODERATOR, "N4"])
        self.commit("dm-N5", MODERATOR, f"【女巫】昨夜 {ref('N5')}（你）被刀，是否使用解药？请直接回复。", "speech", visible=[MODERATOR, "N5"])
        self.moderator_idle(12)
        await self.wait(0.8)

        # 3. dawn: a peaceful night (the witch saved herself), the sheriff
        await self.moderator_step(1.0, "主持人：宣布昨夜结果")
        self.set_phase("第 2 天 · 白天发言")
        self.announce("public", "peaceful_night", "【平安夜】昨夜无人出局。")
        await self.wait(0.5)
        self.add_tag("N4", "警长")
        self.update_dashboard()
        self.announce("public", "sheriff", f"【警长】{ref('N4')} 当选警长，拥有 1.5 票。")
        self.moderator_idle()
        await self.wait(0.5)

        # 4. day speeches: set_floor_queue (floor.changed advances); some players simply pass
        await self.moderator_step(0.9, "主持人：宣布发言顺序（队列）")
        self.announce("public", "speech_order", f"【发言顺序】从警长 {ref('N4')} 开始，按座位顺序依次发言，每人一次。")
        self.moderator_idle()
        order = ["N4", "N5", "N6", "N7", "N8", "N9", "N10", "N11", "N12", "N1", "N2", "N3"]
        long_cut = {"dropped_chars": len(LONG_SPEECH) - 300, "kept_chars": 300}  # the engine cut it (D19)
        speeches = {
            "N4": (f"我是预言家。昨晚查验了 {ref('N3')}，他是狼人阵营。请大家今天出掉 N3。", 1.1),
            "N6": ("N4 的发言有点急，我是好人，想先听听其他人的意见。", 0.9),
            "N7": (LONG_SPEECH[:300], 1.2, long_cut),
            "N8": ("我昨晚守了 N4，没有被击中。预言家的身份我倾向于相信。", 0.8),
            "N9": ("我是平民，没有信息，跟随预言家的归票。", 0.7),
            "N10": ("N4 的跳身份太巧了，我反对盲从。N3 可能是被陷害的。", 0.9),
            "N11": ("我是平民。N10 的发言和 N3 一样在帮狼说话，我怀疑他。", 0.8),
        }
        self.status_text("主持人：公共频道按队列发言")
        await self.run_queue("public", order, speeches)
        await self.wait(0.4)

        # 5. a sealed vote with progress (request.opened / answer / progress / closed), then the last words
        self.set_phase("第 2 天 · 放逐投票")
        await self.moderator_step(0.8, "主持人：发起放逐投票")
        self.moderator_idle()
        voters = self.alive()
        votes = {"N1": "N10", "N2": "N10", "N3": "N4", "N4": "N10", "N5": "N10", "N6": "N4", "N7": "N6", "N8": "N10",
                 "N9": "N10", "N10": "N4", "N11": "N3", "N12": "N4"}
        self.status_text("主持人：等待玩家投票（密封）")
        vid = await self.vote(voters, "请投票放逐一名玩家（密封投票）", list(voters), votes)
        top = self._request(vid)["result"]["tally"]["N10"]
        await self.moderator_step(1.2, "主持人：统计票数")
        self.announce("public", "vote_result", f"【投票结果】{ref('N10')} 得 {top} 票，被放逐。")
        self.player_out("N10")
        self.update_dashboard()
        lid = self.open_request("请留下遗言（不超过 100 字）", [], ["N10"], sealed=False, kind="text", seconds=60.0)
        self.moderator_idle()
        self.set_agent("N10", "thinking")
        await self.wait(1.4)
        self.set_agent("N10", "acting")
        self.meter("N10", tools=1, think=1)
        self.request_answer(lid, "N10", "我真的是好人，大家别被预言家牵着走。")
        self.sleep("N10")
        await self.moderator_step(0.8, "主持人：转述遗言")
        self.announce("public", "last_words_relay", f"【遗言】{self.name('N10')}（N10）：我真的是好人，大家别被预言家牵着走。")
        self.moderator_idle()
        await self.wait(0.6)

        # 6. challenge tickets (D22): one rejected, one upheld
        self.set_phase("第 2 天 · 质询与复核")
        await asyncio.gather(
            self.challenge(
                "N3",
                "主持人，刚才的计票是不是漏了一票？我记得我只被投了 5 票。",
                "rejected",
                "经逐一核对，计票无误：N10 得 6 票。质询驳回。",
                think=1.2,
                resolve_after=2.4,
            ),
            self._delayed(
                1.6,
                self.challenge(
                    "N8",
                    "主持人，N7 的发言被截断了，最后一句没有说完，请允许他补充。",
                    "upheld",
                    "质询成立：已记录 N7 的发言被截断，大家请注意这一点。",
                    think=1.0,
                    resolve_after=2.6,
                ),
            ),
        )
        await self.wait(0.5)

        # 7. a free discussion; the white wolf king explodes: public declaration + frozen channel (D25)
        self.set_phase("第 3 天 · 自由讨论")
        await self.moderator_step(0.8, "主持人：开放自由讨论")
        self.announce("public", "free_discussion", "【自由讨论】现在开始自由讨论，请抓紧时间交换意见。")
        self.open_free("public", max_chars=2000, duration=300.0)
        self.moderator_idle(30)
        chatter = [
            ("N1", "我支持 N4 的查验，N3 昨晚没有任何辩解。", 0.8),
            ("N2", "同意 N1。N10 已经出局，他的遗言也很可疑。", 1.4),
            ("N3", "我是被冤枉的！N4 是悍跳的狼，请大家看清。", 1.7),
            ("N8", "我守了 N4，他要是狼我昨晚不会守他。", 2.2),
            ("N4", "再次强调：N3 是狼。请好人认真对待归票。", 3.0),
            ("N7", "各位注意，我是猎人，今天谁敢动我我就开枪。", 3.5),
            ("N9", "我还是跟随预言家，今天投 N3。", 4.0),
        ]
        tasks = [asyncio.ensure_future(self._chatter("public", pid, text, t)) for pid, text, t in chatter]
        try:
            self.set_agent("N12", "thinking")
            await self.wait(2.7)  # N12 decides to explode while the others are still typing
            self.set_agent("N12", "acting")
            self.meter("N12", tools=2)
            self.emit("ability.used", ability_id="white_wolf_explode", actor_id="N12")
            self.commit("public", "N12", f"我是白狼王，自爆！带走猎人 {ref('N7')}。", "ability")  # the ability is the permission: no floor needed
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
        self.sleep("N12")
        await self.wait(0.8)
        await self.moderator_step(1.6, "主持人：裁决白狼王自爆")
        self.player_out("N12")
        self.player_out("N7")
        self.update_dashboard()
        self.announce("public", "death_notice", f"【出局公告】{ref('N12')} 自爆出局，并带走 {ref('N7')}。")
        pub["frozen"] = False
        self.emit("channel.updated", channel=copy.deepcopy(pub))
        self.moderator_idle()
        await self.wait(1.0)

        # 8. the last wolves are voted out, the moderator ends the game (D8)
        await self.moderator_step(1.0, "主持人：第 3 天 · 放逐并判定胜负")
        self.close_floor("public")
        self.set_phase("第 3 天 · 终局")
        self.player_out("N3")
        self.player_out("N6")
        self.update_dashboard()
        self.announce("public", "death_notice", f"【出局公告】{ref('N3')}、{ref('N6')} 出局。")
        await self.wait(0.6)
        await self.drain_bg()
        self.finish("好人阵营", "狼人阵营全部出局，好人阵营获胜（模拟对局）。")

    def finish(self, winner: str, summary: str) -> None:
        s = self.session
        s["winner"] = winner
        s["summary"] = summary
        s["status"] = "finished"
        s["pause_reason"] = ""
        s["hold_reason"] = ""
        s["active_status_text"] = "对局已结束"
        s["review"]["available"] = True  # E21: reviewing is manual and only possible after the end
        self.emit("game.status", status="finished", reason="end_game", winner=winner, summary=summary)
        self.publish_session_updated()
        self.set_all_agents("sleeping")  # D8: after end_game every agent sleeps

    async def epilogue(self) -> None:
        """What "继续" replays after the verdict (D8): last words, then the verdict again."""
        self.epilogues += 1
        await self.moderator_step(1.0, "主持人：终局后继续 · 请幸存者发表感言")
        self.announce("public", "last_words", f"【终局后继续】第 {self.epilogues} 次：请幸存者留下最后的感言。")
        self.open_free("public", max_chars=600, duration=60.0)
        self.moderator_idle()
        lines = [
            ("N1", "谢谢主持人，这局打得很精彩，预言家带得很稳。", 1.0),
            ("N4", "能活到最后很开心，感谢守卫 N8 的那一刀。", 1.8),
            ("N8", "守卫的职责就是守住好人，今天算是圆满了。", 2.6),
        ]
        await asyncio.gather(*(self.speak("public", pid, text, think=t) for pid, text, t in lines))
        await self.moderator_step(0.8, "主持人：再次宣布终局")
        self.close_floor("public")
        await self.drain_bg()
        self.finish("好人阵营", "终局后继续：幸存者感言结束，好人阵营获胜（模拟对局）。")

    # ----------------------------------------------------------------------------- review (E21)
    async def review(self, ids: list[str]) -> None:
        """Manual review after the verdict: the stats go into the memory file, then the player's own recap."""
        s = self.session
        s["review"]["running"] = True
        self.publish_session_updated()
        winner = s["winner"] or "好人阵营"
        try:
            for pid in ids:
                self.emit("review.progress", id=pid, state="running", message="正在复盘…")
                await asyncio.sleep(0.45 / self.speed)
                role = ROLES[pid]
                wolfish = role in ("狼人", "白狼王")
                won = (winner == "好人阵营") != wolfish
                seat = self.roster.seats[pid]
                record = f"## 战绩\n- 本局：{role}，{'胜' if won else '负'}（{winner}获胜）\n- 累计：1 场 · 胜率 {100 if won else 0}%\n"
                recap = f"\n## 对局记忆\n- 第 1 局：我是{role}。{self.name('N4')} 的查验给了好人方向；{self.name('N12')} 的自爆很突然。\n"
                seat["memory"] = (seat["memory"] or "") + record + recap
                self.reviewed[pid] = True
                s["review"]["done"] = [p for p in PLAYER_IDS if self.reviewed.get(p)]
                self.emit("review.progress", id=pid, state="done", message="已写入战绩与复盘")
        finally:
            s["review"]["running"] = False
            self.publish_session_updated()

    def start_review(self, ids: list[str] | None) -> list[str]:
        if self.session["status"] != "finished":
            raise ValueError("对局结束后才能复盘")
        if self.review_task is not None and not self.review_task.done():
            raise ValueError("复盘正在进行中")
        targets = [p for p in (ids or PLAYER_IDS) if p in PLAYER_IDS and not self.reviewed.get(p)]
        if not targets:
            raise ValueError("所选玩家本局都已复盘")
        self.review_task = asyncio.get_running_loop().create_task(self.review(targets))
        return targets

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

    def _restore_agents(self) -> None:
        """After a pause every agent slept; bring back what each one was doing."""
        for aid in [MODERATOR, *PLAYER_IDS]:
            state, until = self.intent.get(aid, ("sleeping", None))
            self.set_agent(aid, state, until)

    def start(self) -> None:
        """idle -> run the script; paused -> resume; finished -> replay the epilogue (D8)."""
        s = self.session
        if s["status"] == "running":
            return
        if s["status"] == "paused" and s["pause_reason"] == "budget" and self.budget_state() == "reached" and s["budget"]["force_stop"]:
            raise ValueError("仍然超出预算上限：请先在「⛔ 上限」里提高上限，或取消「强制停止」")
        if s["status"] == "paused" and self.task is not None and not self.task.done():
            self.set_status("running", reason="resumed")
            self._restore_agents()
            self.gate.set()
            return
        if s["status"] == "finished":
            self.set_status("running", reason="continued")
            self.gate.set()
            self._spawn(self.epilogue())
            return
        self.set_status("running", reason="started")
        self.gate.set()
        self._spawn(self.play())

    def pause(self, *, reason: str = "user", hold_reason: str = "") -> None:
        if self.session["status"] != "running":
            return
        self.gate.clear()
        self.set_status("paused", reason=("hold:" + hold_reason) if reason == "hold" else reason, pause_reason=reason, hold_reason=hold_reason)
        for aid in [MODERATOR, *PLAYER_IDS]:  # a paused game: everybody sleeps (E2)
            node = self.session["moderator"] if aid == MODERATOR else self.player(aid)
            if node["state"] != "sleeping" or node.get("sleep_until") is not None:
                node["state"] = "sleeping"
                node["sleep_until"] = None
                self.emit("agent.status", id=aid, state="sleeping", until=None)

    def resume_after_budget(self) -> None:
        """Offline replays play the user: raise the limit and press 继续."""
        b = self.session["budget"]
        raised = (b["max_tokens"] or 0) * 3
        self.set_budget(max_tokens=raised or None, max_cost_usd=b["max_cost_usd"], force_stop=b["force_stop"])
        self.set_status("running", reason="resumed")
        self._restore_agents()
        self.gate.set()

    async def _cancel_task(self) -> None:
        if self.review_task is not None and not self.review_task.done():
            self.review_task.cancel()
            await asyncio.gather(self.review_task, return_exceptions=True)
        self.review_task = None
        for t in list(self.bg):
            t.cancel()
        if self.bg:
            await asyncio.gather(*list(self.bg), return_exceptions=True)
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
        self.intent = {}
        self.deadlines = {}
        self.reviewed = {}
        self.session = build_session(uuid.uuid4().hex[:12], self.bindings, self.roster, self.budget_limits, demo=self.demo)
        self.session["event_id"] = self.bus.latest_id + 1  # the id the snapshot event is about to get
        self.bus.publish("snapshot", {"session": copy.deepcopy(self.session)})

    async def jump_to_end(self) -> None:
        """Mock helper: cancel the script and finish right away."""
        await self._cancel_task()
        if self.session["status"] != "finished":
            self.finish("好人阵营", "（模拟）直接跳到终局。")
        self.gate.clear()

    # ----------------------------------------------------------------------------- roster events
    def roster_changed(self, ids: list[str]) -> None:
        """Names / avatars were edited (idle only): update the session and tell every client (roster.updated)."""
        seats = [pid for pid in ids if pid != MODERATOR]
        for pid in seats:
            p = self.player(pid)
            p["name"] = self.roster.name(pid)
            p["avatar"] = copy.deepcopy(self.roster.seats[pid]["avatar"])
        data: dict[str, Any] = {"participants": [self.roster.public(pid) for pid in seats]}
        if MODERATOR in ids:  # the moderator's avatar travels as ``moderator`` (it is not a participant)
            self.session["moderator"]["avatar"] = copy.deepcopy(self.roster.moderator["avatar"])
            data["moderator"] = {"id": MODERATOR, "avatar": copy.deepcopy(self.roster.moderator["avatar"])}
        self.emit("roster.updated", **data)


# --------------------------------------------------------------------------------------------------
# fast offline replay (tests / --selfcheck): a virtual-time loop, so it is instant and deterministic
# --------------------------------------------------------------------------------------------------


async def _replay(*, epilogue: bool, seed: int, budget_demo: bool, review: bool) -> dict[str, Any]:
    bus = EventBus(ring=100000)
    game = MockGame(bus, speed=1.0, seed=seed, budget=default_budget() if budget_demo else default_budget(None, False))
    initial = game.snapshot_payload()
    if budget_demo:
        game.on_budget_pause = lambda g: asyncio.get_running_loop().call_soon(g.resume_after_budget)
    loop = asyncio.get_running_loop()
    game.gate.set()
    game.set_status("running", reason="started")
    started = loop.time()
    await game.play()
    duration = loop.time() - started
    if epilogue:
        game.set_status("running", reason="continued")
        await game.epilogue()
    if review:
        await game.review(game.start_review(None) if False else [p for p in PLAYER_IDS if not game.reviewed.get(p)])
    final = game.snapshot_payload()
    return {"initial": initial, "events": list(bus.ring), "final": final, "duration": duration}


def replay_fast(*, epilogue: bool = True, seed: int = 7, budget_demo: bool = True, review: bool = True, **_ignored: Any) -> dict[str, Any]:
    """Run the whole script offline (virtual time): the initial snapshot, every event, the final session
    and the script's length in game seconds (``duration``)."""
    return asyncio.run(_replay(epilogue=epilogue, seed=seed, budget_demo=budget_demo, review=review), loop_factory=VirtualLoop)


# --------------------------------------------------------------------------------------------------
# the FastAPI app
# --------------------------------------------------------------------------------------------------


def create_app(*, speed: float = 1.0, seed: int = 7, budget_tokens: int | None = DEMO_BUDGET_TOKENS, demo: bool = False) -> Any:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
    from fastapi.staticfiles import StaticFiles

    bus = EventBus()
    holder: dict[str, Any] = {}
    parts = load_parts()
    roster = MockRoster(random.Random(seed + 101), parts)
    store: dict[str, Any] = {
        "providers": copy.deepcopy(PROVIDERS),
        "templates": [
            {"slot": 1, "name": "旗舰", "provider_id": "mock-claude", "model_id": "claude-sonnet", "reasoning_depth": "high", "valid": True},
        ],
        "sessions": [],
        "workspace": {"id": "w-mock", "name": "mock-workspace", "path": str(ROOT / ".mock-workspace"), "status": "ok"},
        "pricing": {"url": DEFAULT_PRICING_URL, "default_url": DEFAULT_PRICING_URL, "fetched_at": now_iso(), "entries": 2345, "error": None},
    }
    initial_budget = default_budget(budget_tokens if budget_tokens else None, True)

    app = FastAPI(title="Arena-Agents UI mock", version=APP_VERSION)
    app.state.mock_holder = holder  # tests reach the MockGame (created on the first request) through this
    app.state.mock_roster = roster

    def game() -> MockGame:
        if "game" not in holder:  # created lazily: the asyncio primitives need a running loop
            holder["game"] = MockGame(bus, speed=speed, seed=seed, roster=roster, budget=initial_budget, demo=demo)
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

    parts_bytes = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if parts else b""

    @app.get("/static/avatar/parts.json")
    async def avatar_parts() -> Response:
        """The library the roster's avatar descriptions were generated from (the real one when static/avatar/
        parts.json existed at start-up, otherwise the test fixture), so that ids and sprites always match."""
        if not parts_bytes:
            raise HTTPException(status_code=404, detail="parts.json not found")
        return Response(parts_bytes, media_type="application/json")

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
        try:
            game().start()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "session": session_payload()}

    @app.post("/api/control/pause")
    async def control_pause() -> dict[str, Any]:
        game().pause()
        return {"ok": True, "session": session_payload()}

    @app.post("/api/control/force-pause")
    async def control_force_pause() -> dict[str, Any]:
        game().pause()  # contract §6: pause and force-pause are equivalent now
        return {"ok": True, "session": session_payload()}

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

    # ---- roster (E13-E16, E21, E24): contract §10.4; {id} is the internal id ("N3")
    def seat_or_404(pid: str, *, moderator_ok: bool = False) -> str:
        if pid not in roster.seats and not (moderator_ok and pid == MODERATOR):
            raise HTTPException(status_code=404, detail="名册里没有这个座位")
        return pid

    def require_idle(what: str) -> None:
        if game().session["status"] != "idle":
            raise HTTPException(status_code=409, detail=f"对局已开始，不能{what}")

    @app.get("/api/roster")
    async def get_roster() -> dict[str, Any]:
        return {
            "memory_enabled": roster.memory_enabled,
            "editable": game().session["status"] == "idle",
            "seats": roster.entries(),
            "moderator": copy.deepcopy(roster.moderator),
        }

    @app.put("/api/roster/memory-enabled")
    async def put_memory_enabled(body: dict[str, Any]) -> dict[str, Any]:
        require_idle("修改「本局加载记忆」")
        roster.memory_enabled = bool(body.get("enabled"))
        g = game()
        g.session["memory_enabled"] = roster.memory_enabled
        g.publish_session_updated()
        return {"enabled": roster.memory_enabled}

    @app.get("/api/roster/memory:export")
    async def export_memory() -> Response:
        return Response(
            roster.export_zip(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="arena-memory.zip"'},
        )

    @app.post("/api/roster/memory:import")
    async def import_memory(request: Request, dry_run: bool = True) -> dict[str, Any]:
        data = await request.body()
        if len(data) > 4 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="记忆包太大")
        try:
            changes = roster.import_zip(data, apply=not dry_run)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"dry_run": dry_run, "changes": changes}

    @app.post("/api/roster/memory:clear")
    async def clear_memory(body: dict[str, Any] | None = None) -> dict[str, Any]:
        roster.clear(bool((body or {}).get("include_persona")))
        return {"ok": True}

    @app.post("/api/roster/avatars:regenerate")
    async def regenerate_all() -> dict[str, Any]:
        require_idle("重新生成头像")
        ids = roster.regenerate(None)
        game().roster_changed(ids)
        return {"seats": roster.entries(), "moderator": copy.deepcopy(roster.moderator)}

    @app.put("/api/roster/{pid}")
    async def put_seat(pid: str, body: dict[str, Any]) -> dict[str, Any]:
        seat_or_404(pid, moderator_ok=True)
        if pid == MODERATOR:  # the moderator only has an avatar to change
            if set(body) - {"avatar"}:
                raise HTTPException(status_code=400, detail="主持人只能更换头像，名字、性格设定和记忆都是固定的。")
            require_idle("修改头像")
            if validate_avatar(body.get("avatar")) or body.get("avatar") is None:
                raise HTTPException(status_code=422, detail="头像描述不合法")
            roster.moderator["avatar"] = body["avatar"]
            game().roster_changed([MODERATOR])
            return copy.deepcopy(roster.moderator)
        changed = False
        if "name" in body or "avatar" in body:
            require_idle("修改名字或头像")
        if "name" in body:
            err = roster.validate_name(pid, body["name"])
            if err:
                raise HTTPException(status_code=422, detail=err)
            roster.seats[pid]["name"] = body["name"].strip()
            changed = True
        if "avatar" in body:
            if validate_avatar(body["avatar"]):
                raise HTTPException(status_code=422, detail="头像描述不合法")
            roster.seats[pid]["avatar"] = body["avatar"]
            changed = True
        for kind in ("persona", "memory"):
            if kind in body:
                if not isinstance(body[kind], str) or len(body[kind]) > 20000:
                    raise HTTPException(status_code=422, detail=f"{kind} 必须是不超过 20000 字的文字")
                roster.seats[pid][kind] = body[kind]
        if changed:
            game().roster_changed([pid])
        return copy.deepcopy(roster.seats[pid])

    @app.post("/api/roster/{pid}/avatar:regenerate")
    async def regenerate_one(pid: str) -> dict[str, Any]:
        seat_or_404(pid, moderator_ok=True)
        require_idle("重新生成头像")
        ids = roster.regenerate(pid)
        game().roster_changed(ids)
        return copy.deepcopy(roster.moderator if pid == MODERATOR else roster.seats[pid])

    @app.post("/api/roster/{pid}/refine")
    async def refine(pid: str, body: dict[str, Any]) -> dict[str, Any]:
        if pid == MODERATOR:
            raise HTTPException(status_code=400, detail="主持人没有性格设定和记忆。")
        seat_or_404(pid)
        kind = body.get("kind")
        text = str(body.get("text") or "").strip()
        if kind not in ("persona", "memory"):
            raise HTTPException(status_code=422, detail="kind 必须是 persona 或 memory")
        if not text:
            raise HTTPException(status_code=422, detail="请先写几个要点")
        name = roster.name(pid)
        if kind == "persona":
            draft = (
                f"【{name} 的性格设定（模拟草稿）】\n"
                f"- 核心：{text}\n- 说话方式：先抛结论，再用一两句话解释；被质疑时不急着反驳，而是复述对方的论点再回应。\n"
                f"- 思维方式：记票型和发言顺序，把矛盾点逐条列出；信息不足时承认不确定。"
            )
        else:
            draft = (
                f"## 对局记忆（模拟草稿）\n- 对其他玩家的印象：{text}\n"
                f"- 自己的经验：前几局常因过早表态被针对，现在更倾向先听后说；胜率中等，神职牌更稳。"
            )
        return {"draft": draft}

    # ---- game review (E21)
    @app.post("/api/game/review")
    async def game_review(body: dict[str, Any] | None = None) -> dict[str, Any]:
        ids = (body or {}).get("ids")
        try:
            started = game().start_review(ids if isinstance(ids, list) else None)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"started": started}

    # ---- budget (E10 / E22)
    @app.get("/api/budget")
    async def get_budget() -> dict[str, Any]:
        return copy.deepcopy(game().session["budget"])

    @app.put("/api/budget")
    async def put_budget(body: dict[str, Any]) -> dict[str, Any]:
        mt = body.get("max_tokens")
        mc = body.get("max_cost_usd")
        if mt is not None and (not _is_int(mt) or mt <= 0):
            raise HTTPException(status_code=422, detail="max_tokens 必须是正整数或空")
        if mc is not None and (not _is_num(mc) or mc <= 0):
            raise HTTPException(status_code=422, detail="max_cost_usd 必须是正数或空")
        return game().set_budget(max_tokens=mt, max_cost_usd=float(mc) if mc is not None else None, force_stop=bool(body.get("force_stop")))

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
        existing = [m["id"] for m in p["models"]]
        extra = ["deepseek-reasoner", "mock-new-model"] if "deepseek" in p["base_url"] else ["claude-sonnet-4-5-20250929", "claude-mock-unknown"]
        ids = existing + [i for i in extra if i not in existing]
        return {"models": [{"id": i, "suggested": match_price(p["base_url"], i)} for i in ids], "existing_models": existing}

    @app.get("/api/pricing/match")
    async def pricing_match(provider_id: str = "", model_id: str = "") -> dict[str, Any]:
        p = find_provider(provider_id) if provider_id else {"base_url": ""}
        m = match_price(p["base_url"], model_id)
        if not m:
            return {"matched": False, "key": None, "input_price_per_m": None, "output_price_per_m": None,
                    "cache_read_price_per_m": None, "cache_write_price_per_m": None, "context_window_tokens": None}
        return {"matched": True, **m}

    @app.get("/api/pricing/source")
    async def get_pricing_source() -> dict[str, Any]:
        return copy.deepcopy(store["pricing"])

    @app.put("/api/pricing/source")
    async def put_pricing_source(body: dict[str, Any]) -> dict[str, Any]:
        url = str(body.get("url") or "").strip()
        if not url.startswith("https://"):
            raise HTTPException(status_code=422, detail="价格源 URL 必须以 https:// 开头")
        store["pricing"].update({"url": url, "error": None})
        return copy.deepcopy(store["pricing"])

    @app.post("/api/pricing/refresh")
    async def refresh_pricing() -> dict[str, Any]:
        pr = store["pricing"]
        if "fail" in pr["url"]:
            pr["error"] = "无法拉取价格表：连接超时（模拟的失败，URL 里含有 fail）"
        else:
            pr.update({"error": None, "fetched_at": now_iso(), "entries": 2345 + len(PRICE_TABLE)})
        return copy.deepcopy(pr)

    @app.post("/api/providers/{provider_id}/models", status_code=201)
    async def add_model(provider_id: str, body: dict[str, Any]) -> dict[str, Any]:
        p = find_provider(provider_id)
        hit = match_price(p["base_url"], body["model_id"])
        same = bool(hit) and all(
            body.get(k) == hit[k] for k in ("input_price_per_m", "output_price_per_m", "cache_read_price_per_m", "cache_write_price_per_m")
        )
        m = _model(
            body["model_id"], body["input_price_per_m"], body.get("cache_read_price_per_m"), body.get("cache_write_price_per_m"),
            body["output_price_per_m"], body.get("context_window_tokens", 128000),
            f"litellm:{hit['key']}" if same and hit else "manual",
        )
        p["models"].append(m)
        PRICE[m["id"]] = price_tuple(m)
        return m

    @app.patch("/api/providers/{provider_id}/models")
    async def patch_model(provider_id: str, body: dict[str, Any]) -> dict[str, Any]:
        p = find_provider(provider_id)
        for m in p["models"]:
            if m["id"] == body.get("model_id"):
                m.update({k: v for k, v in body.items() if k != "model_id"})
                m["price_source"] = "manual"
                PRICE[m["id"]] = price_tuple(m)
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

    @app.post("/api/mock/stall")
    async def mock_stall(body: dict[str, Any] | None = None) -> dict[str, Any]:
        """The watchdog saw nothing happen (E9): a banner, no pause. An empty notice clears it."""
        g = game()
        g.session["stall_notice"] = str((body or {}).get("notice") or "")
        g.publish_session_updated()
        return {"ok": True, "stall_notice": g.session["stall_notice"]}

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

    @app.get("/api/mock/frame")
    async def mock_frame(w: int = 390, h: int = 844, q: str = "") -> Response:
        """The page inside an iframe of w x h CSS pixels: the frame has its own viewport, so the responsive
        layout can be measured / screenshotted at any size on a desktop browser (q = the page's query string,
        e.g. ``platform=phone&input=touch``)."""
        w = max(200, min(4000, int(w)))
        h = max(200, min(4000, int(h)))
        src = "/?" + "".join(ch for ch in q if ch.isalnum() or ch in "=&_-")
        html = (
            '<!doctype html><meta charset="utf-8"><title>frame</title>'
            f'<body style="margin:0;background:#64748b"><iframe id="f" src="{src}" '
            f'style="display:block;width:{w}px;height:{h}px;border:0;background:#fff"></iframe></body>'
        )
        return Response(html, media_type="text/html")

    @app.get("/api/mock/info")
    async def mock_info() -> dict[str, Any]:
        g = game()
        return {"speed": g.speed, "latest_event_id": bus.latest_id, "subscribers": len(bus.subscribers), "status": g.session["status"],
                "ring": len(bus.ring), "clock": g.session["clock"], "budget": g.session["budget"]}

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------

REQUIRED_EVENTS = {
    "message.committed", "channel.created", "channel.updated", "floor.changed", "player.updated", "request.opened",
    "request.progress", "request.answer", "request.closed", "challenge.opened", "challenge.resolved", "ability.used",
    "phase.changed", "dashboard.updated", "game.status", "agent.status", "agent.metrics", "session.updated",
    "budget.updated", "budget.warning", "budget.reached", "review.progress",
}


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
    print(f"{len(run['events'])} events; {len(run['final']['messages'])} messages; the main script is {run['duration']:.0f} s long")
    print(f"tokens: {run['final']['usage_stats']['total_tokens']:,}  cost: ${run['final']['usage_stats']['total_cost_usd']:.4f}")
    for t in sorted(types):
        print(f"  {t:20s} {types[t]}")
    missing = REQUIRED_EVENTS - set(types)
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
    parser = argparse.ArgumentParser(description="Arena-Agents v0.12.1 UI mock server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier (default 1.0 ≈ 60 s per game)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--budget-tokens", type=int, default=DEMO_BUDGET_TOKENS,
                        help="demo token limit with 'force stop' (the replay warns at 80 %% and pauses at 100 %%); 0 = no limit")
    parser.add_argument("--demo", action="store_true", help='set session.demo (the page shows the "演示模式" badge)')
    parser.add_argument("--selfcheck", action="store_true", help="replay the script offline and validate every event")
    args = parser.parse_args(argv)
    if args.selfcheck:
        return selfcheck()
    import uvicorn

    print(f"Mock UI server: http://{args.host}:{args.port}/  (speed x{args.speed}, seed {args.seed}, "
          f"demo budget {args.budget_tokens or 'off'} tokens)")
    uvicorn.run(create_app(speed=args.speed, seed=args.seed, budget_tokens=args.budget_tokens, demo=args.demo),
                host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
