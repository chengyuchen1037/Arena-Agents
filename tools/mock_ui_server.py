#!/usr/bin/env python
"""Mock UI server for the v0.14 front end (docs/v0.12-contracts.md §8 + §10.3 / §10.6 + §13:
SSE v2 events, PublicSession v6, bots / contract / rules / games / budget / price-source APIs).

v0.14: a player is a **bot** (``tools/mock_ui_bots.py``: name, avatar, L0, what it learned, an initialization record; the twelve of them are
seated by the line-up) - ``/api/roster/**`` is gone.  ``tools/mock_ui_hub.py`` ties the bots to the game (payloads, the start check ``NOT_READY``,
the status label, initialization jobs, reviews) and serves ``/api/bots/**``, ``/api/contract``, ``/api/rules/**`` and the new ``/api/games/**``
routes.  The identities are dealt when the game starts (``session.dealt``); before that no card has any.  See the notes of ``create_app`` for the
``/api/mock/*`` switches (``bots``, ``files``, ``learning``) that put the mock into the situations the page has to show.

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

v0.13 archive (contract section 11.9): ``/api/archive/*`` export and import jobs with ``archive.progress`` events, and
``/api/state.legacy_data`` (start with ``--legacy`` to see the "old v0.12.1 data found" notice).  To try the import in
the browser pick a file whose NAME says what should happen: ``demo`` / ``keys`` (password ``Correct-Horse-9``) /
``mismatch`` (keys that do not match the Provider addresses) / ``fail`` / ``newer`` / ``corrupt`` / ``huge`` (see
``MockArchive``).

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
import unicodedata
import uuid
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:  # the sibling modules of the mock (the tests load this file by its path)
    sys.path.insert(0, str(TOOLS_DIR))
import mock_ui_bots as mb  # noqa: E402  (the bot store, the contract, the rules)
import mock_ui_hub as hubmod  # noqa: E402  (the bots at the table: payloads, jobs, the API)

STATIC_DIR = ROOT / "static"
PARTS_REAL = STATIC_DIR / "avatar" / "parts.json"
PARTS_FIXTURE = ROOT / "tests" / "frontend" / "fixtures" / "parts.min.json"
DEFAULT_PORT = 8731
APP_VERSION = "v0.14.0-mock"
PACE = 1.0  # plot seconds -> game seconds at speed=1 (the whole script is about 60 s)
MODERATOR = "moderator"
ENGINE = "engine"
DEMO_BUDGET_TOKENS = 150_000  # the demo budget: warns at 80 % (120K), pauses at 100 % (see the notes above)
MOCK_EPOCH = 1_763_600_000  # "now" of a game made alone (the clock of the bots' files stands still there: replays are deterministic)

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

# a long speech that fits the per-message limit (300): the feed shows it whole.  (v0.14, H16: a speech over the limit is not cut any more - it is sent back
# with TOO_LONG and the player writes it again.)
LONG_SPEECH = (
    "我是猎人。昨晚没有任何信息，但我想先把场上的发言逻辑理一遍：N1 的发言太圆滑，几乎没有任何立场；"
    "N2 只是附和，没有独立判断；N9 一直在转移话题。从票型和发言顺序看，N10 的站边最不自然，"
    "我怀疑他是在冲锋之后收手。我愿意把枪口对准 N10，也请预言家尽快报出查验结果，给好人一个明确的方向。"
    "如果今天投不出狼，晚上我们就会被动。请大家认真对待这一轮的归票，不要被带节奏，也不要随意弃票。"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------------------------------
# the sprite library the avatar descriptions are drawn from
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


# --------------------------------------------------------------------------------------------------
# the avatars of the bots (v0.14: a bot owns its avatar; the bot store asks the sprite library for descriptions)
# --------------------------------------------------------------------------------------------------


class MockAvatars(mb.AvatarPicker):
    """Random avatar descriptions from the library (a new one differs from every other in at least three fields when the library is big enough)."""

    def __init__(self, rng: random.Random, parts: dict[str, Any] | None) -> None:
        self.rng = rng
        self.parts = parts

    def one(self, taken: Any) -> dict[str, Any] | None:
        best = None
        for _ in range(300):
            cand = random_avatar(self.rng, self.parts)
            if cand is None:
                return None
            if all(_differences(cand, o) >= 3 for o in taken):
                return cand
            best = best or cand
        return best

    def batch(self, n: int, taken: Any) -> list[dict[str, Any] | None]:
        out: list[dict[str, Any] | None] = []
        for _ in range(n):
            out.append(self.one([*taken, *[o for o in out if o]]))
        return out

    def problems(self, desc: Any) -> list[str]:
        """Why a description is refused (``[]``: fine): the shape of §10.6 (``v`` = 1, the eight fields are texts)."""
        if desc is None:
            return []
        if not isinstance(desc, dict):
            return ["头像描述必须是对象"]
        problems = [] if desc.get("v") == 1 else ["v 必须是 1"]
        problems += [f"{f} 缺少或不是文字" for f in AVATAR_FIELDS if not isinstance(desc.get(f), str)]
        return problems



# --------------------------------------------------------------------------------------------------
# v0.13 archive: export / import jobs with progress events (docs/archive.md; contract sections 11.5 / 11.9)
# --------------------------------------------------------------------------------------------------

#: What the mock "has" on its fictional disk (never the checkout's path or the user name: the dialogs show them)
MOCK_DATA_DIR = "D:\\Arena-Agents\\data"
MOCK_LEGACY_WORKSPACE = "C:\\Users\\you\\Arena-Agents\\Default"
MOCK_PASSWORD = "Correct-Horse-9"  # opens every mock archive that holds API keys (a file name with "keys")
MOCK_MAX_ARCHIVE_BYTES = 2 * 1024**3
MOCK_APP_VERSION = "v0.14.0"
V1_ARCHIVE_WARNING = "这是 v0.13 的旧格式存档：名册里的 {n} 个座位会在导入时迁移成 bot（性格设定变成底层设定，记忆会整理成印象）。"
ARCHIVE_FORMAT_VERSION = 2  # v0.14 (§13.9): the total archive is format 2 (the bots, the contract, the rules ...); a format 1 archive is still read
MAX_PASSWORD_ATTEMPTS = 10
EXPORT_NAME_RE = re.compile(r"^arena-[0-9]{8}-[0-9]{6}\.arenaagents$")
WRONG_PASSWORD = "密码错误或 API Key 部分已损坏"
GAME_RUNNING_TEXT = "对局正在运行：请先暂停对局，再导入存档。"
BUSY_TEXT = "已有一个导出或导入任务正在进行，请等它结束。"
MISMATCH_TEXT = (
    "存档里 Provider「DeepSeek」的协议或地址与加密 API Key 时不一致：存档在导出之后被改动过。为了安全，不导入 API Key；"
    "可以选择「跳过 API Key」导入其余内容，再在 Provider 里重新填写 Key。"
)
ARCHIVE_SCENARIOS = ("demo（正常）/ v1（v0.13 的旧格式：名册在导入时迁移成 bot）/ keys（含 API Key，密码 " + MOCK_PASSWORD
                     + "）/ mismatch（Key 与地址不一致）/ fail（导入中途失败）/ newer（版本太新）/ corrupt（校验失败）/ huge（太大）")


def mock_password_problem(password: Any) -> str | None:
    """The password rule of app/archive/kdf.py (tests/archive/password_cases.json is the table both are tested against)."""
    if not isinstance(password, str):
        return "请输入密码。"
    text = unicodedata.normalize("NFC", password)
    if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in text):
        return "密码不能包含控制字符。"
    if len(text) > 256:
        return "密码最长 256 位。"
    if len(text) < 12:
        return f"密码至少需要 12 位（现在 {len(text)} 位）。"
    classes = {"upper" if "A" <= ch <= "Z" else "lower" if "a" <= ch <= "z" else "digit" if "0" <= ch <= "9" else "symbol" for ch in text}
    if len(classes) < 3:
        return f"密码需要包含大写字母、小写字母、数字、符号中的至少 3 类（现在 {len(classes)} 类）。"
    return None


def archive_error(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"detail": message, "code": code}


class MockArchive:
    """The archive endpoints of the mock: jobs that really take a moment (so the progress bar moves), a pending
    import and the "old data found" notice.  Nothing touches the disk.

    Hand-testing the import in the browser: the NAME of the file picked decides what the mock makes of it ("demo"
    = a normal archive, "keys" = holds API keys, password ``MOCK_PASSWORD``, "mismatch" = keys whose Provider address was
    changed after they were sealed, "fail" = the import breaks half way, "newer" = a newer format, "corrupt" =
    checksum mismatch, "huge" = over the size limit); any other file is
    read as a real archive (its ``manifest.json``) and refused when it is none."""

    def __init__(self, bus: "EventBus", game: Any, *, speed: float = 1.0) -> None:
        self.bus = bus
        self.game = game  # () -> MockGame
        self.speed = max(0.01, float(speed))
        self.tick = 0.12  # seconds (at speed 1) between two progress events
        self.jobs: dict[str, dict[str, Any]] = {}
        self.busy: str | None = None
        self.pending: dict[str, Any] | None = None
        self.exports: dict[str, bytes] = {}
        self.legacy: dict[str, bool] = {"found": False, "dismissed": False}
        self.max_bytes = MOCK_MAX_ARCHIVE_BYTES
        self.tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------------------------ the notice
    def legacy_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {"found": bool(self.legacy["found"]), "dismissed": bool(self.legacy["dismissed"])}
        if state["found"]:
            state["path"] = MOCK_LEGACY_WORKSPACE
        return state

    # ------------------------------------------------------------------------------------- queries
    def game_running(self) -> bool:
        return self.game().session["status"] == "running"

    def public_pending(self) -> dict[str, Any] | None:
        p = self.pending
        if p is None:
            return None
        return {"import_id": p["import_id"], "name": p["name"], "size_bytes": p["size_bytes"], "summary": copy.deepcopy(p["summary"]), "expires_in": 7200}

    def snapshot(self, job: dict[str, Any], *, local: bool = True) -> dict[str, Any]:
        data = self.event_data(job)
        if job["status"] == "done" and job.get("result") is not None:
            data["result"] = dict(job["result"]) if local else {k: v for k, v in job["result"].items() if k != "path"}
        return data

    def status(self) -> dict[str, Any]:
        active = next((j for j in reversed(list(self.jobs.values())) if j["status"] == "running"), None)
        return {"busy": self.busy is not None, "job": self.snapshot(active) if active else None, "import": self.public_pending(), "game_active": self.game_running()}

    def get_job(self, job_id: str) -> tuple[int, dict[str, Any]]:
        job = self.jobs.get(job_id)
        if job is None:
            return archive_error(404, "not_found", "找不到这个任务（可能已经过期，或服务重启过）。")
        return 200, self.snapshot(job)

    # ----------------------------------------------------------------------------------- publishing
    def event_data(self, job: dict[str, Any]) -> dict[str, Any]:
        data: dict[str, Any] = {k: job[k] for k in ("job_id", "kind", "phase", "done", "total", "status")}
        if job.get("message"):
            data["message"] = job["message"]
        if job.get("code"):
            data["code"] = job["code"]
        if job["status"] == "done" and job.get("result") is not None:
            data["result"] = {k: v for k, v in job["result"].items() if k != "path"}  # no server path on the stream
        return data

    def publish(self, job: dict[str, Any]) -> None:
        self.bus.publish("archive.progress", self.event_data(job))

    def new_job(self, kind: str) -> dict[str, Any]:
        job = {"job_id": uuid.uuid4().hex[:16], "kind": kind, "phase": "flush" if kind == "export" else "verify", "done": 0, "total": 1, "status": "running", "message": "", "code": "", "result": None}
        self.jobs[job["job_id"]] = job
        self.busy = job["job_id"]
        return job

    def spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def walk(self, job: dict[str, Any], plan: list[tuple[str, int, int]], *, stop_after: str | None = None) -> bool:
        """Publish the phases of ``plan`` = [(phase, steps, total)] one step at a time; ``False`` when stopped early."""
        for phase, steps, total in plan:
            for i in range(steps + 1):
                job["phase"], job["done"], job["total"] = phase, round(total * i / steps), total
                self.publish(job)
                if i < steps:
                    await asyncio.sleep(self.tick / self.speed)
            if phase == stop_after:
                return False
        return True

    def finish(self, job: dict[str, Any], *, result: dict[str, Any] | None = None, error: tuple[str, str] | None = None) -> None:
        if error is not None:
            job["status"], job["code"], job["message"] = "error", error[0], error[1]
        else:
            job["status"], job["result"] = "done", result
        self.busy = None
        self.publish(job)

    # ---------------------------------------------------------------------------------------- export
    def start_export(self, include_keys: bool, password: Any, include_logs: bool) -> tuple[int, dict[str, Any]]:
        if include_keys:
            problem = mock_password_problem(password)
            if problem:
                return archive_error(422, "weak_password", problem)
        if self.busy is not None:
            return archive_error(409, "busy", BUSY_TEXT)
        job = self.new_job("export")
        self.publish(job)
        self.spawn(self.run_export(job, include_keys, include_logs))
        return 202, {"job_id": job["job_id"], "kind": "export"}

    async def run_export(self, job: dict[str, Any], include_keys: bool, include_logs: bool = True) -> None:
        include_logs = True  # v0.14 (H36): the logs are always part of the archive; ``include_logs`` of the request is ignored
        pack_bytes = 48_000_000 if include_logs else 1_400_000
        plan = [("flush", 1, 1), ("collect", 1, 1)] + ([("encrypt", 1, 1)] if include_keys else []) + [("pack", 12, pack_bytes), ("finalize", 1, 1)]
        try:
            await self.walk(job, plan)
            now = datetime.now().astimezone().replace(microsecond=0)
            name = f"arena-{now:%Y%m%d-%H%M%S}.arenaagents"
            while name in self.exports:  # two exports in the same second
                now = now.replace(second=(now.second + 1) % 60)
                name = f"arena-{now:%Y%m%d-%H%M%S}.arenaagents"
            contents = {"games": 3, "providers": 2, "bots": 12, "contract": True, "rules": 1, "has_secrets": include_keys, "has_logs": include_logs}
            result = {
                "name": name,
                # (H42) the name the user's browser saves it under; ``name`` is the fixed internal name in data/exports/ (the download URL keeps it)
                "download_name": f"arena-agents-{now:%Y%m%d-%H%M%S}.arenaagents",
                "path": f"{MOCK_DATA_DIR}\\exports\\{name}",
                "display_path": f"data/exports/{name}",
                "size_bytes": 9_800_000 if include_logs else 310_000,
                "archive_id": uuid.uuid4().hex,
                "created_at": now.isoformat(timespec="seconds"),
                "app_version": MOCK_APP_VERSION,
                "contents": contents,
                "file_count": 31 if include_logs else 17,
                "uncompressed_bytes": pack_bytes,
                "warnings": [],
            }
            self.exports[name] = self.tiny_archive(result)
            self.finish(job, result=result)
        except Exception:  # noqa: BLE001 - a mock job never leaves the slot taken
            self.finish(job, error=("internal", "导出失败：发生了内部错误（模拟）。"))

    @staticmethod
    def tiny_archive(result: dict[str, Any]) -> bytes:
        """A small valid zip with a manifest (the mock's idea of the downloaded file)."""
        manifest = {
            "format": "arenaagents", "format_version": ARCHIVE_FORMAT_VERSION, "app_version": result["app_version"], "archive_id": result["archive_id"],
            "created_at": result["created_at"], "contents": result["contents"], "files": [], "files_sha256": "0" * 64,
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        return buf.getvalue()

    def export_file(self, name: str) -> bytes | None:
        return self.exports.get(name) if EXPORT_NAME_RE.fullmatch(name or "") else None

    @staticmethod
    def download_name_of(name: str) -> str:
        """The name a browser saves an export under: ``arena-20251119-121530.arenaagents`` -> ``arena-agents-20251119-121530.arenaagents`` (H42)."""
        return name.replace("arena-", "arena-agents-", 1) if name.startswith("arena-") and not name.startswith("arena-agents-") else name

    # ----------------------------------------------------------------------------------------- import
    def demo_summary(self, size: int, *, has_secrets: bool, created_at: str | None = None, v1: bool = False) -> dict[str, Any]:
        """What the import shows of a file: v2 counts the bots, the contract and the rules; a v1 archive (v0.13) still has a roster of seats, which
        the import turns into bots (H45)."""
        games = [
            {"session_id": "g3demo000003", "title": "Arena-Agents", "scenario_title": "狼人杀 12 人局", "status": "finished", "winner": "好人阵营", "created_at": "2026-11-17T09:00:00+00:00", "human_involved": False, "players": 12},
            {"session_id": "g2demo000002", "title": "Arena-Agents", "scenario_title": "狼人杀 12 人局", "status": "paused", "winner": None, "created_at": "2026-11-16T09:00:00+00:00", "human_involved": True, "players": 12},
            {"session_id": "g1demo000001", "title": "Arena-Agents", "scenario_title": "狼人杀 12 人局", "status": "finished", "winner": "狼人阵营", "created_at": "2026-11-15T09:00:00+00:00", "human_involved": False, "players": 12},
        ]
        out = {
            "archive_id": uuid.uuid4().hex, "format_version": 1 if v1 else ARCHIVE_FORMAT_VERSION, "app_version": MOCK_APP_VERSION,
            "created_at": created_at or "2026-11-18T00:42:07-08:00", "size_bytes": size, "uncompressed_bytes": size * 6, "file_count": 31,
            "games": 3, "providers": 2, "has_secrets": has_secrets, "has_logs": True,
            "provider_names": ["DeepSeek", "Claude"], "game_list": games, "games_listed": 3, "warnings": [],
        }
        if v1:
            out.update(roster_seats=12, warnings=[V1_ARCHIVE_WARNING.format(n=12)])
        else:
            out.update(bots=12, contract=True, rules=1)
        return out

    def read_manifest(self, data: bytes) -> dict[str, Any] | None:
        """The summary of a REAL archive (what its manifest says); ``None`` when ``data`` is none."""
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                manifest = json.loads(z.read("manifest.json").decode("utf-8"))
        except (zipfile.BadZipFile, KeyError, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(manifest, dict) or manifest.get("format") != "arenaagents" or not isinstance(manifest.get("contents"), dict):
            return None
        c = manifest["contents"]
        v1 = manifest.get("format_version") == 1
        summary = self.demo_summary(len(data), has_secrets=bool(c.get("has_secrets")), created_at=str(manifest.get("created_at") or "2026-11-18T00:00:00+00:00"), v1=v1)
        summary.update(
            archive_id=str(manifest.get("archive_id") or summary["archive_id"]), app_version=str(manifest.get("app_version") or "?"),
            games=int(c.get("games") or 0), providers=int(c.get("providers") or 0),
            has_logs=bool(c.get("has_logs")), file_count=len(manifest.get("files") or []),
        )
        if v1:
            summary["roster_seats"] = int(c.get("roster_seats") or 0)
            summary["warnings"] = [V1_ARCHIVE_WARNING.format(n=summary["roster_seats"])]
        else:
            summary.update(bots=int(c.get("bots") or 0), contract=bool(c.get("contract")), rules=int(c.get("rules") or 0))
        summary["game_list"] = summary["game_list"][: summary["games"]]
        summary["games_listed"] = len(summary["game_list"])
        return summary

    def receive(self, data: bytes, name: str) -> tuple[int, dict[str, Any]]:
        if self.game_running():
            return archive_error(409, "game_running", GAME_RUNNING_TEXT)
        if self.busy is not None:
            return archive_error(409, "busy", BUSY_TEXT)
        if len(data) > self.max_bytes or "huge" in name.lower():
            return archive_error(413, "too_large", "存档文件超过了大小上限（2.0 GB）。可以用环境变量 ARENA_MAX_ARCHIVE_BYTES 调大。")
        if not data:
            return archive_error(422, "invalid_archive", "上传的文件是空的。")
        lowered = name.lower()
        if "newer" in lowered:
            return archive_error(422, "unsupported_version", f"这份存档使用更新的格式（v{ARCHIVE_FORMAT_VERSION + 1}），当前程序只支持到 v{ARCHIVE_FORMAT_VERSION}。请先把 Arena-Agents 升级到较新的版本再导入。")
        if "corrupt" in lowered:
            return archive_error(422, "invalid_archive", "games/g2demo000002/game.json 的 SHA-256 校验值与清单不符：存档已损坏或被改动。")
        if any(word in lowered for word in ("demo", "keys", "fail", "mismatch", "v1")):
            summary: dict[str, Any] | None = self.demo_summary(len(data), has_secrets="keys" in lowered or "mismatch" in lowered, v1="v1" in lowered)
        else:
            summary = self.read_manifest(data)
        if summary is None:
            return archive_error(422, "invalid_archive", "这不是有效的存档文件（不是 zip，或者文件已损坏）。")
        self.pending = {"import_id": uuid.uuid4().hex[:16], "name": name, "size_bytes": len(data), "summary": summary, "attempts": 0, "fail": "fail" in lowered, "mismatch": "mismatch" in lowered}
        return 200, self.public_pending() or {}

    def discard(self, import_id: str) -> bool:
        if self.pending is not None and self.pending["import_id"] == import_id:
            self.pending = None
            return True
        return False

    def start_apply(self, import_id: str, password: Any, skip_keys: bool) -> tuple[int, dict[str, Any]]:
        pending = self.pending
        if pending is None or pending["import_id"] != import_id:
            return archive_error(404, "not_found", "这次导入已经过期或被取消，请重新选择存档文件。")
        if self.game_running():
            return archive_error(409, "game_running", GAME_RUNNING_TEXT)
        if self.busy is not None:
            return archive_error(409, "busy", BUSY_TEXT)
        keys = bool(pending["summary"]["has_secrets"]) and not skip_keys
        if keys:
            if not password:
                return archive_error(422, "wrong_password", "这份存档含有 API Key：请输入密码，或者选择跳过 API Key。")
            if pending["attempts"] >= MAX_PASSWORD_ATTEMPTS:
                self.pending = None
                return archive_error(429, "too_many_attempts", "密码错误次数过多，请重新选择存档文件再试。")
            if password != MOCK_PASSWORD:
                pending["attempts"] += 1
                return archive_error(422, "wrong_password", WRONG_PASSWORD)
            if pending["mismatch"]:  # the right password, but providers.json was changed after the keys were sealed
                return archive_error(422, "keys_mismatch", MISMATCH_TEXT)
        job = self.new_job("import")
        self.publish(job)
        self.spawn(self.run_import(job, pending, keys))
        return 202, {"job_id": job["job_id"], "kind": "import"}

    async def run_import(self, job: dict[str, Any], pending: dict[str, Any], keys: bool) -> None:
        summary = pending["summary"]
        plan = [("verify", 8, pending["size_bytes"]), ("validate", 1, 1), ("backup", 1, 1), ("replace", 4, 4)] + ([("keys", 1, 1)] if keys else []) + [("reload", 1, 1), ("finalize", 1, 1)]
        try:
            if not await self.walk(job, plan, stop_after="replace" if pending["fail"] else None):
                self.finish(job, error=("io_error", "无法移动数据目录里的「games」（可能被其他程序占用）。已恢复原来的数据。"))
                return
            await self.game().reset()  # the real server reloads the data and sends every client a fresh snapshot
            now = datetime.now()
            names = summary["provider_names"]
            result = {
                "backup": f"{now:%Y%m%d-%H%M%S}", "games": summary["games"], "providers": summary["providers"],
                "keys_imported": len(names) if keys else 0, "keys_kept": 0 if keys else 1,
                "providers_without_key": [] if keys else names[1:], "warnings": [],
            }
            self.pending = None
            self.finish(job, result=result)
        except Exception:  # noqa: BLE001
            self.finish(job, error=("internal", "导入失败：发生了内部错误（模拟）。"))


# --------------------------------------------------------------------------------------------------
# contract shapes (§8.1 / §10.6) and a validator for the §8.2 events
# --------------------------------------------------------------------------------------------------

MSG_KINDS = {"speech", "announce", "template", "challenge", "reply", "ability", "system"}
#: v0.13: "human" = a person plays the seat (F6); v0.13.1: "held" = woken up, waiting in front of the soft-pause gate (§12.2)
AGENT_STATES = {"thinking", "acting", "sleeping", "human", "held"}
SOFT_PAUSE_STATES = {"draining", "held"}  # v0.13.1 (§12.2): PublicSession.soft_pause = null | {"state", "in_flight"}
GAME_STATUSES = {"idle", "running", "paused", "finished"}
PAUSE_REASONS = {"user", "hold", "error", "stall", "budget", ""}
FLOOR_KINDS = {"turn", "queue", "free", "none"}
CHANNEL_MODES = {"closed", "turn", "queue", "free", "listen"}
REQUEST_KINDS = {"choice", "text"}
AUTO_CLOSE = {"all_answered", "unanimous", "deadline"}
CLOSE_REASONS = {"all_answered", "unanimous", "deadline", "cancelled"}
BUDGET_STATES = {"ok", "warning", "reached"}
REVIEW_STATES = hubmod.REVIEW_STATES  # v0.14 (§13.8): queued / running / done / error / timeout
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
    "system_status",  # v0.14 (§13.8): {"code", "text", "reason"}
    "dealt",  # v0.14 (§13.3 / §13.8): have the identities been dealt (the game has started)?
    "stall_notice",
    "clock",  # the real backend adds the game clock (seconds) to every session.updated (contracts §10.8)
    "soft_pause",  # v0.13.1 (§12.2): null | {"state": "draining" | "held", "in_flight": n} - in every session.updated
    "clock_running",  # v0.13.1 (§12.2): does the game clock run? (status stays "running" in a soft pause)
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
    # v0.13.1 (§12.2): `untimed` = there are active tokens and none of them has a deadline (a person holds a public
    # floor); then there is no countdown to show.  Optional for an older server, a bool whenever it is there.
    if "untimed" in floor:
        if not isinstance(floor["untimed"], bool):
            errs.append(f"{where}.untimed")
        elif floor["untimed"] and floor.get("seconds_left") is not None:
            errs.append(f"{where}.untimed: an untimed floor has no seconds_left")
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
    """``PublicSession.review`` (v0.14, §13.8): ``{"available", "done", "running", "supported", "deadline_s": 60, "seconds_left"}``."""
    return hubmod.validate_review_view(r, where)


def validate_avatar(a: Any, where: str = "avatar") -> list[str]:
    if a is None:
        return []
    if not (isinstance(a, dict) and a.get("v") == 1 and all(isinstance(a.get(f), str) for f in AVATAR_FIELDS)):
        return [where]
    return []


CALL_KINDS = {"call", "event"}
CALL_STATUSES = {"ok", "retry", "timeout", "error", "cancelled"}
CALL_PURPOSES = {"turn", "review", "refine", "runtime", "init"}  # v0.14 (H24): "init" = a bot's initialization (actor_id = the bot id)
CALL_INT_FIELDS = ("attempt", "elapsed_ms", "prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens", "http_status")
CALL_STR_FIELDS = ("provider_id", "model_id", "finish_reason", "error_class", "message")
CALL_KEYS = {"id", "ts", "gt", "kind", "status", "actor_id", "actor_name", "purpose", *CALL_INT_FIELDS, *CALL_STR_FIELDS}
CALL_RING = 2000  # the error log keeps the last 2000 lines of the game (contract §12.3)


def validate_call(c: Any, where: str = "call") -> list[str]:
    """One ``CallEntry`` of the error log (contract §12.3).  It never carries a prompt, an answer, a header or a key."""
    errs: list[str] = []
    if not isinstance(c, dict):
        return [f"{where}: not an object"]
    if set(c) - CALL_KEYS:
        errs.append(f"{where}: keys {sorted(set(c) - CALL_KEYS)}")  # (a prompt / answer / header / key has no field here)
    if not _is_int(c.get("id")) or c["id"] <= 0:
        errs.append(f"{where}.id")
    if not isinstance(c.get("ts"), str) or not c["ts"]:
        errs.append(f"{where}.ts")
    if not _opt_num(c.get("gt")):
        errs.append(f"{where}.gt")
    if c.get("kind") not in CALL_KINDS:
        errs.append(f"{where}.kind")
    if c.get("status") not in CALL_STATUSES:
        errs.append(f"{where}.status")
    for k in ("actor_id", "actor_name"):
        if not isinstance(c.get(k), str) or not c[k]:
            errs.append(f"{where}.{k}")
    if c.get("purpose") not in CALL_PURPOSES:
        errs.append(f"{where}.purpose")
    for k in CALL_INT_FIELDS:
        if c.get(k) is not None and not _is_int(c[k]):
            errs.append(f"{where}.{k}")
    for k in CALL_STR_FIELDS:
        if not _opt_str(c.get(k)):
            errs.append(f"{where}.{k}")
    if isinstance(c.get("message"), str) and len(c["message"]) > 500:
        errs.append(f"{where}.message: more than 500 characters")
    if c.get("kind") == "event" and (c.get("attempt") is not None or c.get("purpose") != "runtime"):
        errs.append(f"{where}: an event has no attempt and the purpose runtime")
    return errs


def validate_review_changes(ch: Any, where: str = "changes") -> list[str]:
    """``review.progress.changes`` of a finished review (v0.14, §13.8): ``{"files": [{"file", "before", "after"}], "proposals": n}`` - what the review
    wrote into the memory (``file`` is one of the memory parts) and how many L0 proposals it made."""
    if not (isinstance(ch, dict) and set(ch) == {"files", "proposals"} and _is_int(ch["proposals"]) and ch["proposals"] >= 0 and isinstance(ch["files"], list)):
        return [where]
    errs: list[str] = []
    for i, f in enumerate(ch["files"]):
        if not (isinstance(f, dict) and set(f) == {"file", "before", "after"} and f["file"] in mb.MEMORY_PARTS and isinstance(f["before"], str) and isinstance(f["after"], str)):
            errs.append(f"{where}.files[{i}]")
    return errs


def validate_takeover(t: Any, where: str = "takeover") -> list[str]:
    """``PublicSession.takeover`` / the ``takeover.updated`` payload (contract §11.7; v0.14 removed ``reshuffle_available``, F5)."""
    errs: list[str] = []
    if not isinstance(t, dict):
        return [f"{where}: not an object"]
    if not _opt_str(t.get("seat")):
        errs.append(f"{where}.seat")
    if "reshuffle_available" in t:
        errs.append(f"{where}.reshuffle_available: removed in v0.14 (the identities are dealt when the game starts)")
    if t.get("seat") is None:
        return errs
    if not _is_int(t.get("seq")):
        errs.append(f"{where}.seq")
    channels = t.get("channels")
    if not isinstance(channels, dict):
        errs.append(f"{where}.channels")
    else:
        for cid, c in channels.items():
            if not (isinstance(c, dict) and isinstance(c.get("can_write"), bool) and _opt_str(c.get("why_not")) and _opt_str(c.get("code"))
                    and (c.get("max_chars") is None or _is_int(c["max_chars"])) and isinstance(c.get("can_pass"), bool)):
                errs.append(f"{where}.channels.{cid}")
                continue
            floor = c.get("floor")
            if floor is not None and not (
                isinstance(floor, dict) and floor.get("kind") in FLOOR_KINDS and isinstance(floor.get("mine"), bool)
                and isinstance(floor.get("paused"), bool) and all(_opt_num(floor.get(k)) for k in ("messages_left", "chars_left", "seconds_left", "deadline_clock"))
            ):
                errs.append(f"{where}.channels.{cid}.floor")
            elif floor is not None and "untimed" in floor and (
                # v0.13.1 (§12.2): an untimed token has neither a countdown nor a deadline (both null)
                not isinstance(floor["untimed"], bool) or (floor["untimed"] and (floor.get("seconds_left") is not None or floor.get("deadline_clock") is not None))
            ):
                errs.append(f"{where}.channels.{cid}.floor.untimed")
    reqs = t.get("requests")
    if not isinstance(reqs, list):
        errs.append(f"{where}.requests")
    else:
        for i, r in enumerate(reqs):
            if not (isinstance(r, dict) and isinstance(r.get("id"), str) and r.get("kind") in REQUEST_KINDS and isinstance(r.get("answered"), bool)
                    and _opt_num(r.get("seconds_left")) and _opt_num(r.get("deadline_clock")) and _opt_str(r.get("channel_id"))
                    and (r.get("my_answer") is None or (isinstance(r["my_answer"], dict) and "value" in r["my_answer"]))):
                errs.append(f"{where}.requests[{i}]")
    abilities = t.get("abilities")
    if not isinstance(abilities, list):
        errs.append(f"{where}.abilities")
    else:
        for i, a in enumerate(abilities):
            if not (isinstance(a, dict) and isinstance(a.get("id"), str) and _is_int(a.get("quota_left")) and isinstance(a.get("params"), list)
                    and all(isinstance(p, dict) and isinstance(p.get("name"), str) and p.get("type") in {"player_id", "player_ids", "enum", "int", "text"}
                            for p in a["params"])):
                errs.append(f"{where}.abilities[{i}]")
    if not _str_list(t.get("attention")):
        errs.append(f"{where}.attention")
    return errs


INIT_STATUSES = {"uninitialized", "initializing", "ready", "stale", "failed"}


def validate_session(s: Any) -> list[str]:
    """PublicSession (§10.6, v0.14: schema 6 - ``dealt``, ``system_status``, ``bots``, ``participants[i].bot_id`` / ``init_status``, §13.8)."""
    errs: list[str] = []
    if not isinstance(s, dict):
        return ["session: not an object"]
    if "takeover" in s:
        errs += validate_takeover(s["takeover"], "session.takeover")
    if s.get("schema_version") != 6:
        errs.append("session.schema_version: 6")
    if not isinstance(s.get("dealt"), bool):
        errs.append("session.dealt")
    elif not s["dealt"] and any(isinstance(p, dict) and p.get("tags") for p in s.get("participants") or []):
        errs.append("session.dealt: before the deal no seat has a tag (an identity) - not even the public ones")
    errs += hubmod.validate_system_status(s.get("system_status"), "session.system_status")
    errs += hubmod.validate_bots_payload(s.get("bots"), "session.bots")
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
                    and p.get("state") in AGENT_STATES and _opt_num(p.get("sleep_until"))
                    and isinstance(p.get("bot_id"), str) and (p.get("init_status") is None or p["init_status"] in INIT_STATUSES)):
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
    # v0.13.1 (§12.2): the soft pause and whether the game clock runs.  Optional (an older server sends neither); when they
    # are there they agree with the status: a soft pause only exists while the game "runs" - with the clock stopped.
    if "soft_pause" in s:
        errs += validate_soft_pause(s["soft_pause"], "session.soft_pause")
    if "clock_running" in s:
        if not isinstance(s["clock_running"], bool):
            errs.append("session.clock_running")
        elif s["clock_running"] and (s.get("status") != "running" or s.get("soft_pause")):
            errs.append("session.clock_running: true only while the game runs and nothing holds the clock")
        elif not s["clock_running"] and s.get("status") == "running" and not s.get("soft_pause") and "soft_pause" in s:
            errs.append("session.clock_running: a running game without a soft pause has a running clock")
    if s.get("soft_pause") and s.get("status") != "running":
        errs.append("session.soft_pause: only a running game can be soft-paused")
    return errs


def validate_soft_pause(sp: Any, where: str = "soft_pause") -> list[str]:
    """``PublicSession.soft_pause`` (contract §12.2): null, or {"state": "draining" | "held", "in_flight": n} - held has none."""
    if sp is None:
        return []
    if not (isinstance(sp, dict) and set(sp) == {"state", "in_flight"} and sp.get("state") in SOFT_PAUSE_STATES
            and _is_int(sp.get("in_flight")) and sp["in_flight"] >= 0):
        return [where]
    if sp["state"] == "held" and sp["in_flight"] != 0:
        return [f"{where}: held has nothing in flight"]
    return []


ARCHIVE_EVENT_KEYS = {"job_id", "kind", "phase", "done", "total", "status", "message", "code", "result"}
ARCHIVE_PHASES = {
    "export": {"flush", "collect", "encrypt", "pack", "finalize"},
    "import": {"verify", "validate", "backup", "replace", "keys", "reload", "finalize"},
}


def validate_archive_event(data: dict[str, Any], w: str = "archive.progress") -> list[str]:
    """Problems of one ``archive.progress`` payload (contract section 11.4 / 11.9); the result never carries a server path."""
    errs: list[str] = []
    if set(data) - ARCHIVE_EVENT_KEYS:
        errs.append(f"{w}: keys {sorted(set(data) - ARCHIVE_EVENT_KEYS)}")
    if not (isinstance(data.get("job_id"), str) and data["job_id"]):
        errs.append(f"{w}.job_id")
    kind = data.get("kind")
    if kind not in ARCHIVE_PHASES:
        errs.append(f"{w}.kind")
    elif data.get("phase") not in ARCHIVE_PHASES[kind]:
        errs.append(f"{w}.phase")
    for key in ("done", "total"):
        if not _is_int(data.get(key)) or data[key] < 0:
            errs.append(f"{w}.{key}")
    status = data.get("status")
    if status not in ("running", "done", "error"):
        errs.append(f"{w}.status")
    if status == "error" and not (isinstance(data.get("message"), str) and data["message"] and isinstance(data.get("code"), str) and data["code"]):
        errs.append(f"{w}: an error needs message and code")
    if status == "done":
        result = data.get("result")
        if not isinstance(result, dict):
            errs.append(f"{w}.result")
        elif "path" in result:
            errs.append(f"{w}.result.path: the stream never carries a server path")
    elif "result" in data:
        errs.append(f"{w}.result: only a finished job has one")
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
        # v0.14 (§13.8): {"id": seat, "bot_id", "name", "state", "message", "seconds_left", "deadline_s": 60, "changes": null | {"files", "proposals"}}
        errs += hubmod.validate_review_progress(data, w)
    elif etype == "bots.updated":
        # v0.14 (§13.8): {"bots": BotsPayload - the live part, no limits / defaults}
        if set(data) != {"bots"}:
            errs.append(f"{w}: keys {sorted(set(data) ^ {'bots'})}")
        errs += hubmod.validate_bots_payload(data.get("bots"), f"{w}.bots")
    elif etype == "bot.init.progress":
        errs += hubmod.validate_init_progress(data, w)
    elif etype == "llm.call":
        # v0.13.1 (G35, contract §12.3): one line of the error log; an ordinary event that only carries the summary
        errs += validate_call(data.get("call"), f"{w}.call")
        if not _is_int(data.get("errors")) or data["errors"] < 0:
            errs.append(f"{w}.errors")
        if set(data) - {"call", "errors"}:
            errs.append(f"{w}: keys {sorted(set(data) - {'call', 'errors'})}")
    elif etype == "session.updated":
        if not data or set(data) - SESSION_UPDATED_KEYS:
            errs.append(f"{w}: keys {sorted(set(data) - SESSION_UPDATED_KEYS)}")
        if "budget" in data:
            errs += validate_budget(data["budget"], f"{w}.budget")
        if "review" in data:
            errs += validate_review(data["review"], f"{w}.review")
        if "system_status" in data:
            errs += hubmod.validate_system_status(data["system_status"], f"{w}.system_status")
        if "dealt" in data and not isinstance(data["dealt"], bool):
            errs.append(f"{w}.dealt")
        # v0.13.1 (§12.2): both travel in every session.updated (the soft pause freezes the clock: it comes along)
        if "soft_pause" in data:
            errs += validate_soft_pause(data["soft_pause"], f"{w}.soft_pause")
        if "clock_running" in data and not isinstance(data["clock_running"], bool):
            errs.append(f"{w}.clock_running")
        if ("soft_pause" in data) != ("clock_running" in data):
            errs.append(f"{w}: soft_pause and clock_running travel together")
    elif etype == "takeover.updated":
        errs += validate_takeover(data.get("takeover"), f"{w}.takeover")
    elif etype == "archive.progress":
        errs += validate_archive_event(data, w)
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
    return {"kind": "none", "active": None, "paused": False, "seconds_left": None, "untimed": False, "queue": [], "free": None}


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


def build_session(session_id: str, bindings: dict[str, Any], hub: "hubmod.MockHub", budget: dict[str, Any], *, demo: bool = False) -> dict[str, Any]:
    """A fresh (idle) game (v0.14): a system DM per player (D23), the bots of the line-up in the seats - and NO identities: they are dealt when the
    game starts (H52, ``dealt``), so before that no seat has a tag, not even a public one.  ``bots``, ``system_status`` and every participant's
    ``bot_id`` / ``init_status`` are filled in by ``hub.sync_session`` (the caller does it once the session is the game's)."""
    participants = [
        {
            "id": pid,
            "seat": i + 1,
            "name": hub.seat_name(pid),
            "avatar": hub.seat_avatar(pid),
            "bot_id": hub.bots.bot_at(pid) or "",
            "init_status": None,
            "tags": [],
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
        "schema_version": 6,
        "version": MOCK_APP_VERSION,
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
            "avatar": copy.deepcopy(hub.bots.moderator.get("avatar")),
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
        "memory_enabled": bool(hub.bots.memory_enabled),
        # v0.14 (§13.8): who reviews what, and whether it can (the mock's games all have bots: ``supported``)
        "review": {"available": False, "done": [], "running": False, "supported": True, "deadline_s": mb.REVIEW_TIMEOUT_S, "seconds_left": None},
        "dealt": False,  # (H52) the identities are dealt when the game starts
        "bots": {},  # (hub.sync_session fills in BotsPayload's live part and the status label)
        "system_status": {"code": "before_game", "text": "对局前", "reason": ""},
        "stall_notice": "",
        "demo": demo,
        # v0.13 (F2, F10): nobody plays a seat yet (v0.14: no re-dealing any more - F5 is gone)
        "takeover": {"seat": None},
        # v0.13.1 (§12.2): no soft pause; the game clock does not run before the start
        "soft_pause": None,
        "clock_running": False,
    }


# the abilities of the mock's scenario (white wolf king) as the seat state lists them (contract §11.7)
EXPLODE = "white_wolf_explode"
EXPLODE_TEXT = "白狼王自爆：只在主持人启用期间（白天非投票的正式发言环节）可用。公开宣布自爆并带走一名存活的其他玩家，公共频道会被冻结。"
FREE_MAX_CHARS = 300  # the mock scenario's per-message limits (defaults.free / defaults.dm)
DM_MAX_CHARS = 600
TOKEN_MAX_MESSAGES = 2
TOKEN_MAX_CHARS = 600
FREE_MAX_MESSAGES = 6
TEXT_ANSWER_MAX_CHARS = 300


def receipt(ok: bool, message: str = "", *, code: str | None = None, seq: int | None = None, truncated: dict[str, int] | None = None,
            **data: Any) -> dict[str, Any]:
    """The slim receipt of the seat endpoints (contract §11.7)."""
    out: dict[str, Any] = {"accepted": ok, "seq": seq, "code": None if ok else code, "message": message}
    if truncated:
        out["truncated"] = truncated
    if data:
        out["data"] = data
    return out


class TakeoverError(Exception):
    """A takeover / seat request the server refuses: ``status`` is the HTTP status the endpoint answers with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class MockGame:
    """Holds one PublicSession (god view), mutates it as the script plays and publishes the SSE v2
    events that describe every change (so a client that applies the events ends up with the same
    session as :attr:`session`; ``tests/frontend/test_mock_server.py`` checks exactly that)."""

    def __init__(self, bus: EventBus, *, speed: float = 1.0, seed: int = 7, hub: "hubmod.MockHub | None" = None,
                 budget: dict[str, Any] | None = None, demo: bool = False) -> None:
        self.bus = bus
        self.speed = max(0.01, float(speed))
        self.seed = seed
        self.rng = random.Random(seed)
        self.demo = demo
        # v0.14: the bots (names, avatars, memory, initialization) and what ties them to the game live in the hub; it outlives a game
        # (a new game keeps the bots).  A game made alone (a test) gets a hub of its own, the way the roster used to be.
        # (its clock stands still: the replays of a game alone are the same, event for event, every time - ``core.updated_at`` and the like included)
        self.hub = hub if hub is not None else hubmod.MockHub(
            hubmod.Stores(seed + 101, avatars=MockAvatars(random.Random(seed + 101), load_parts()), clock=lambda: MOCK_EPOCH), providers_fn=lambda: PROVIDERS
        )
        self.hub.attach(self)
        if not self.hub.bot_bindings:  # (the model bindings follow the bots, H4: kept by bot id, the seats are the view)
            self.hub.load_seat_bindings(default_bindings())
        self.budget_limits = copy.deepcopy(budget if budget is not None else default_budget())
        self.bindings = self.hub.seat_bindings()
        self.session = build_session(uuid.uuid4().hex[:12], self.bindings, self.hub, self.budget_limits, demo=demo)
        self.hub.sync_session(self.session)
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
        self._reset_human()
        self._reset_observe()
        self._reset_soft()

    def _reset_soft(self) -> None:
        """v0.13.1: the soft pause (G1-G9, contract §12.2) is a volatile flag of ONE running game: a new game has none.
        (Its dice are its own: the script of the game itself is exactly what it was.)"""
        self.soft: dict[str, Any] | None = None  # None | {"state": "draining" | "held", "in_flight": n}
        self.soft_task: asyncio.Task[None] | None = None  # the calls in flight finishing (real time: the clock is frozen)
        self.soft_rng = random.Random(self.seed + 31337)

    def _reset_observe(self) -> None:
        """v0.13.1: the error log (G32-G36) and the thinking record (G21-G26) belong to ONE game: a new game starts empty.
        (Their dice are their own, so the script of the game itself is exactly what it was.)"""
        self.calls: list[dict[str, Any]] = []  # CallEntry, oldest first, at most CALL_RING
        self.call_seq = 0
        self.call_rng = random.Random(self.seed + 4242)
        self.thoughts: dict[str, list[dict[str, Any]]] = {}  # agent id -> ThoughtItem, oldest first
        self.thought_gen: dict[str, int] = {}
        self.thought_pos = 0
        self.thought_rng = random.Random(self.seed + 777)

    def _reset_human(self) -> None:
        """v0.13: a person may play one seat (F2).  The script waits for them where their seat would act."""
        self.takeover_seat: str | None = None
        self.human_seats: set[str] = set()  # every seat a person played in this game (the review offers to learn their style, H34)
        self.started = False  # a game.status "running" was recorded (the identities are dealt then, H52)
        self.invalidating_seq = 0  # the seq of the last phase change: older drafts are STALE (F11)
        self.token: dict[str, Any] | None = None  # the played seat's active token in a queue
        self.ability_open = False  # the moderator enabled the ability for the played seat
        self.ability_result: dict[str, Any] | None = None  # {"target", "declaration"} once it was used
        self.free_spoken = False  # the played seat spoke in the free discussion
        self._takeover_flush_scheduled = False

    # ----------------------------------------------------------------------------- plumbing
    #: events that cannot change what the played seat may do (no ``takeover.updated`` after them)
    _SEAT_QUIET = frozenset({"agent.status", "agent.metrics", "budget.updated", "budget.warning", "budget.reached", "takeover.updated", "llm.call"})

    def emit(self, etype: str, **data: Any) -> None:
        ev = self.bus.publish(etype, data)
        self.session["event_id"] = ev["id"]
        if self.takeover_seat is not None and etype not in self._SEAT_QUIET:
            self.schedule_takeover_flush()

    def snapshot_payload(self) -> dict[str, Any]:
        self.session["event_id"] = self.bus.latest_id
        for r in self.session["requests"]:
            if r["status"] == "open" and r["id"] in self.deadlines:
                r["seconds_left"] = round(max(0.0, self.deadlines[r["id"]] - self.session["clock"]), 2)
        self.session["takeover"] = self.compute_takeover()
        # v0.13.1 (§12.2): a snapshot always says whether a soft pause holds the game and whether the clock runs
        self.session["soft_pause"] = None if self.soft is None else dict(self.soft)
        self.session["clock_running"] = self.clock_running()
        self.hub.sync_session(self.session)  # v0.14: the bots, the status label, the review - as they are now
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
        return self.hub.seat_name(pid)  # (the bot that sits in the seat)

    def ref(self, pid: str) -> str:
        return self.hub.seat_ref(pid)

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
        self.hub.refresh_status(s)  # v0.14: the status label, the review (``supported`` / ``seconds_left``) and the memory switch follow the bots
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
            "system_status": copy.deepcopy(s["system_status"]),
            "dealt": s["dealt"],
            "stall_notice": s["stall_notice"],
            # v0.13.1 (§12.2): like the real backend, every session.updated carries the game clock (frozen the moment a
            # soft pause begins), the soft pause itself and whether the clock runs - the page's countdown follows them
            "clock": round(self.clock, 3),
            "soft_pause": None if self.soft is None else dict(self.soft),
            "clock_running": self.clock_running(),
        }
        s["soft_pause"] = None if self.soft is None else dict(self.soft)  # (the session the snapshots hold says the same)
        s["clock_running"] = data["clock_running"]
        self.emit("session.updated", **data)

    def status_text(self, text: str) -> None:
        self.session["active_status_text"] = text
        self.publish_session_updated()

    def deal_identities(self) -> None:
        """H52: the identities are dealt when the game starts - before that no card has any, in any view (``dealt`` is false and no seat has a
        tag).  One ``player.updated`` per seat (what the engine's ledger shows); ``dealt`` travels in the next ``session.updated``."""
        s = self.session
        if s["dealt"]:
            return
        s["dealt"] = True
        for pid in PLAYER_IDS:
            p = self.player(pid)
            p["tags"] = [ROLES[pid], "存活"]
            self.emit("player.updated", player_id=pid, tags=list(p["tags"]), out=bool(p["out"]))

    def set_status(self, status: str, *, reason: str = "", pause_reason: str = "", hold_reason: str = "") -> None:
        s = self.session
        if status == "running" and reason == "started":
            self.deal_identities()  # (the start check has passed: ``start`` raised NotReady otherwise)
        s["status"] = status
        s["pause_reason"] = pause_reason
        s["hold_reason"] = hold_reason
        if status == "running":
            s["winner"] = None
            s["summary"] = ""
            self.started = True  # a game.status "running" was recorded
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
        """thinking / acting / sleeping (``until``: game-clock second a timed sleep ends); "held" is set by the soft pause."""
        until = until if state == "sleeping" else None
        if aid == self.takeover_seat:
            return  # a person plays this seat: it has no agent that thinks, acts or sleeps (F6)
        node = self.session["moderator"] if aid == MODERATOR else self.player(aid)
        self.intent[aid] = (state, until)
        if self.session["status"] == "paused":
            return  # a paused game keeps everybody asleep; the intent is restored on resume
        if self.soft is not None and state in ("thinking", "acting"):
            # v0.13.1 (G3): in a soft pause an agent that is woken up waits in front of the gate - "held" - and goes
            # on (with this intent) when the pause ends
            state, until = "held", None
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
        pool = [a for a in [MODERATOR, *self.alive()] if a not in exclude and a != self.takeover_seat]
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

    # ----------------------------------------------------------------------------- v0.13.1: the soft pause (G1-G9)
    # The real backend (app/game/softpause_runtime.py) lets the model calls in flight finish, then holds every agent in
    # front of the gate and stops the game clock; the mock's script is not an agent runtime, so it plays the same
    # picture: at the click the clock and the script stop (``wait()`` blocks on ``gate``), the agents that were
    # *thinking* reach the gate after a second or three each, in REAL time ("收尾中" with a falling count), the ones that
    # were acting hold at once, and when all have stopped the pause is "held".  The person's own actions (``seat_*``)
    # stay possible: they are plain method calls.  Continuing lets the script run on.
    def clock_running(self) -> bool:
        """Does the game clock run?  ``status`` stays "running" during a soft pause; the clock does not (G2)."""
        return self.session["status"] == "running" and self.soft is None

    def _hold(self, aid: str) -> None:
        """One agent has reached the gate: its card says "held" (it keeps what it was doing in ``intent``)."""
        node = self.session["moderator"] if aid == MODERATOR else self.player(aid)
        if node["state"] != "held":
            node["state"] = "held"
            node["sleep_until"] = None
            self.emit("agent.status", id=aid, state="held", until=None)

    def soft_pause(self) -> None:
        """「⏸ 暂停 agent」: only while the game runs and a seat is played (409 otherwise, a sentence with its full stop,
        like the real backend); already soft-paused: nothing happens."""
        s = self.session
        if s["status"] != "running":
            raise TakeoverError(409, "对局没有在运行。")
        if self.takeover_seat is None:
            raise TakeoverError(409, "只有接管座位时才能软暂停。")
        if self.soft is not None:
            return
        agents = [a for a in [MODERATOR, *PLAYER_IDS] if a != self.takeover_seat]
        state_of = lambda a: (s["moderator"] if a == MODERATOR else self.player(a))["state"]  # noqa: E731
        in_flight = [a for a in agents if state_of(a) == "thinking"]  # their model call is still on its way
        acting = [a for a in agents if state_of(a) == "acting"]  # tools run synchronously: already at the gate
        self.gate.clear()  # the script and the game clock stop at the click
        self.soft = {"state": "draining" if in_flight else "held", "in_flight": len(in_flight)}
        for aid in acting:
            self._hold(aid)
        self.publish_session_updated()  # carries the frozen clock
        if in_flight:
            self.soft_task = asyncio.get_running_loop().create_task(self._drain(in_flight))

    async def _drain(self, pending: list[str]) -> None:
        """The calls that were in flight come back (real time - the game clock is frozen).  Like real model calls they run
        in parallel and each takes a second or three of its own, so the count falls one by one (a person looking at the
        page can see "收尾中（还有 N 个回答）" before "agent 已暂停")."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        due = sorted((self.soft_rng.uniform(1.0, 3.5) / self.speed, aid) for aid in pending)
        for when, aid in due:
            await asyncio.sleep(max(0.0, start + when - loop.time()))
            if self.soft is None:
                return
            self._hold(aid)
            left = self.soft["in_flight"] - 1
            self.soft = {"state": "draining", "in_flight": left} if left > 0 else {"state": "held", "in_flight": 0}
            self.publish_session_updated()

    def _clear_soft(self) -> None:
        """Forget the soft pause (a hard pause, the end of the game, a new game ...: G6)."""
        task, self.soft_task = self.soft_task, None
        if task is not None and not task.done():
            task.cancel()
        self.soft = None

    def soft_resume(self) -> None:
        """「▶ 继续」: the agents go on from where they were (also cancels a pause that is still draining)."""
        if self.soft is None:
            raise TakeoverError(409, "没有处于软暂停。")
        self._clear_soft()
        for aid in [MODERATOR, *PLAYER_IDS]:
            if aid == self.takeover_seat:
                continue
            node = self.session["moderator"] if aid == MODERATOR else self.player(aid)
            if node["state"] == "held":  # (the ones that were asleep never moved)
                state, until = self.intent.get(aid, ("sleeping", None))
                node["state"] = state
                node["sleep_until"] = until if state == "sleeping" else None
                self.emit("agent.status", id=aid, state=state, until=node["sleep_until"])
        self.gate.set()
        self.publish_session_updated()

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
        rounds = think if think is not None else r.randint(1, 2)
        m["thinking_rounds"] += rounds
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
        self.record_turn(aid, model, miss, cache_read, cache_write, ctok, rounds)  # v0.13.1: the error log + the thinking record
        self.emit("agent.metrics", id=aid, **{k: m[k] for k in METRIC_FIELDS})
        self.refresh_budget()

    # ----------------------------------------------------------------------------- v0.13.1: the error log (G32-G36)
    def call_actor_name(self, actor_id: str, purpose: str = "turn") -> str:
        if actor_id == MODERATOR:
            return "主持人"
        if actor_id == "roster":
            return "名册"
        if actor_id == ENGINE:
            return "系统"
        if purpose == "init":  # (H24) an initialization belongs to no game: the actor is the bot (its id), not a seat
            return self.hub.bots.bots[actor_id]["name"] if actor_id in self.hub.bots.bots else actor_id
        return self.hub.seat_name(actor_id)

    def call_counts(self) -> tuple[int, int]:
        """(errors, retries) of the log: the red badge counts ``timeout`` + ``error``."""
        errors = sum(1 for c in self.calls if c["status"] in ("timeout", "error"))
        return errors, sum(1 for c in self.calls if c["status"] == "retry")

    def log_call(self, actor_id: str, *, kind: str = "call", status: str = "ok", purpose: str = "turn", attempt: int | None = 1,
                 **fields: Any) -> dict[str, Any]:
        """Append one line to the error log and publish ``llm.call`` (an ordinary, droppable event)."""
        self.call_seq += 1
        binding = self.session["model_bindings"].get(MODERATOR if actor_id == "roster" else actor_id, {})  # ("完善" uses the moderator's model)
        entry: dict[str, Any] = {
            "id": self.call_seq,
            "ts": now_iso().replace("+00:00", "Z"),
            "gt": round(self.clock, 3),
            "kind": kind,
            "status": status,
            "actor_id": actor_id,
            "actor_name": self.call_actor_name(actor_id, purpose),
            "purpose": "runtime" if kind == "event" else purpose,
            "provider_id": binding.get("provider_id") if kind == "call" else None,
            "model_id": binding.get("model_id") if kind == "call" else None,
            "attempt": attempt if kind == "call" else None,
            "elapsed_ms": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "finish_reason": None,
            "http_status": None,
            "error_class": None,
            "message": None,
        }
        entry.update({k: v for k, v in fields.items() if k in entry})
        if isinstance(entry["message"], str):
            entry["message"] = entry["message"][:500]
        self.calls.append(entry)
        if len(self.calls) > CALL_RING:
            del self.calls[0 : len(self.calls) - CALL_RING]
        self.emit("llm.call", call=copy.deepcopy(entry), errors=self.call_counts()[0])
        return entry

    def calls_view(self, *, after: int = 0, limit: int = 500, errors_only: bool = False) -> dict[str, Any]:
        """``GET /api/llm/calls``: the lines with an id above ``after`` (the NEWEST ``limit`` of them), oldest first."""
        rows = [c for c in self.calls if c["id"] > after and (not errors_only or c["status"] in ("retry", "timeout", "error"))]
        if len(rows) > limit:
            rows = rows[-limit:]
        errors, retries = self.call_counts()
        return {"calls": copy.deepcopy(rows), "last_id": self.call_seq, "total": len(self.calls), "errors": errors, "retries": retries}

    def rebuild_calls(self, *, drop_every: int = 4, jitter_ms: int = 3) -> dict[str, Any]:
        """What the real backend does after a restart, a load or an import (contract §12.3): the log of the game is rebuilt from its
        trace log, so its lines are numbered from 1 AGAIN.  The lines a trace does not hold are gone (here: every ``drop_every``-th
        successful turn - a call an agent threw away; 0 keeps them all) and a failed line carries the time of its trace record,
        ``jitter_ms`` off the time of the live line.  Nothing is announced: a client learns of it when it reads the log, or from the
        next line - which now has a LOW id."""
        kept = [c for c in self.calls if not (drop_every > 0 and c["status"] == "ok" and c["purpose"] == "turn" and c["id"] % drop_every == 0)]
        for line in kept:
            if line["status"] != "ok" and jitter_ms:
                moved = datetime.fromisoformat(line["ts"]) + timedelta(milliseconds=jitter_ms)
                line["ts"] = moved.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        for number, line in enumerate(kept, start=1):
            line["id"] = number
        self.calls = kept
        self.call_seq = len(kept)
        errors, retries = self.call_counts()
        return {"ok": True, "total": len(kept), "last_id": self.call_seq, "errors": errors, "retries": retries}

    def inject_failure(self, actor_id: str = "N3", *, status: str = "error", retries: int = 2, http_status: int | None = 429,
                       error_class: str = "RateLimitError", message: str | None = None, give_up: bool = True,
                       purpose: str = "turn") -> list[dict[str, Any]]:
        """A model request that fails: ``retries`` attempts the client retries, then the request ends as ``status``
        (``error`` / ``timeout`` / ``cancelled``).  With ``give_up`` the agent also gives its action up: a runtime line
        and the ``error`` event (the "重试用尽" toast, which stays)."""
        text = message or ("调用超过 120 秒仍未返回，已放弃。" if status == "timeout" else "Rate limit reached for requests（模拟的限流错误）")
        out = [
            self.log_call(actor_id, status="retry", purpose=purpose, attempt=i + 1, elapsed_ms=800 + 450 * i, http_status=http_status,
                          error_class=error_class, message=text)
            for i in range(max(0, retries))
        ]
        out.append(self.log_call(actor_id, status=status, purpose=purpose, attempt=max(0, retries) + 1, elapsed_ms=120000 if status == "timeout" else 900,
                                 http_status=None if status in ("timeout", "cancelled") else http_status,
                                 error_class="TimeoutError" if status == "timeout" else None if status == "cancelled" else error_class,
                                 message=None if status == "cancelled" else text))
        if give_up and status != "cancelled":
            out.append(self.log_call(actor_id, kind="event", status="error", error_class=error_class, http_status=http_status,
                                     message=f"{self.call_actor_name(actor_id)} 的这次行动失败，已放弃：{text}"))
            self.emit("error", message=f"{self.call_actor_name(actor_id)} 的行动失败，已放弃（{text}）", agent_id=actor_id)
        return out

    def record_turn(self, aid: str, model: str, miss: int, cache_read: int, cache_write: int, ctok: int, rounds: int) -> None:
        """One scripted model call of ``aid``: its line in the log (sometimes after a retry) and its thinking bubbles."""
        dice = self.call_rng
        if dice.random() < 0.08:  # a rate limit that the client retries: the log shows the retry, the call succeeds
            self.log_call(aid, status="retry", attempt=1, elapsed_ms=dice.randint(300, 1500), http_status=429, error_class="RateLimitError",
                          message="Rate limit reached for requests（模拟的限流错误，客户端稍后重试）")
            attempt = 2
        else:
            attempt = 1
        self.log_call(aid, status="ok", attempt=attempt, elapsed_ms=dice.randint(700, 9000), prompt_tokens=miss, completion_tokens=ctok,
                      cache_read_tokens=cache_read, cache_write_tokens=cache_write, finish_reason=dice.choice(["stop", "stop", "stop", "tool_calls"]))
        self.note_thoughts(aid, model, rounds)

    # ----------------------------------------------------------------------------- v0.13.1: the thinking record (G21-G26)
    THOUGHT_BANK = (
        "现在是「{phase}」。先把场上的信息理一遍：谁发过言、谁还没表态，再决定这一轮要做什么。",
        "{a} 刚才的说法和上一轮不太一致，先记下来，等投票时再看他的票型。",
        "不急着表态。{a} 和 {b} 的立场有冲突，听完后面的发言再站边更稳妥。",
        "如果我现在发言，应该先给结论，再用一两句话解释理由；别说得太满，给自己留余地。",
        "这一轮的信息不多，先观察 {a} 的反应；没有把握的事不写进发言里。",
    )
    SELF_TALK_BANK = ("先等一等，看看其他人怎么说。", "（没有需要立刻做的事，继续观察。）", "这条信息和我之前的判断一致，保持不变。")

    def note_thoughts(self, aid: str, model: str, rounds: int) -> None:
        """What the model thought in one activation (G23): ``rounds`` bubbles of thinking, sometimes a line of self-talk.  Kept
        in memory only (the real backend reads them from the trace log): they never travel in an event or in the session."""
        dice = self.thought_rng
        gen = self.thought_gen.get(aid, 0) + dice.choice([1, 1, 1, 2])  # (not always consecutive: a cancelled run counts too)
        self.thought_gen[aid] = gen
        others = [self.name(p) for p in self.alive() if p != aid] or ["对方"]
        a, b = dice.choice(others), dice.choice(others)
        items = self.thoughts.setdefault(aid, [])
        kinds = ["thought"] * max(1, rounds) + (["content"] if dice.random() < 0.35 else [])
        for round_no, kind in enumerate(kinds, start=1):
            text = (dice.choice(self.THOUGHT_BANK).format(phase=self.session["phase_text"] or "未开始", a=a, b=b)
                    if kind == "thought" else dice.choice(self.SELF_TALK_BANK))
            self.thought_pos += 120 + len(text) * 3
            items.append({
                "id": f"1:{self.thought_pos}",
                "ts": now_iso().replace("+00:00", "Z"),
                "gt": round(self.clock, 3),
                "gen": gen,
                "round": min(round_no, rounds + 1),
                "kind": kind,
                "text": text,
                "model": model,
            })
        del items[0 : max(0, len(items) - 400)]

    def thought_page(self, agent_id: str, before: str | None, limit: int) -> dict[str, Any]:
        """``GET /api/agents/{id}/thoughts``: the newest page first; ``before`` = the cursor of an earlier answer."""
        if agent_id != MODERATOR and agent_id not in PLAYER_IDS:
            raise TakeoverError(404, "没有这个玩家。")
        if (self.takeover_seat is not None and self.session["status"] != "finished" and agent_id != self.takeover_seat):
            raise TakeoverError(409, "接管期间不能查看其他玩家的思考记录，对局结束后开放。")
        rows = self.thoughts.get(agent_id, [])
        if before is not None:
            m = re.fullmatch(r"(\d+):(\d+)", before)
            if not m:
                raise TakeoverError(400, "游标无效。")
            limit_key = (int(m.group(1)), int(m.group(2)))
            rows = [t for t in rows if tuple(int(x) for x in t["id"].split(":")) < limit_key]
        page = rows[-limit:]
        has_more = len(rows) > len(page)
        return {
            "agent_id": agent_id,
            "name": self.name(agent_id),
            "items": copy.deepcopy(page),
            "next_before": page[0]["id"] if page and has_more else None,
            "has_more": bool(page) and has_more,
        }

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

    def create_channel(self, cid: str, name: str, members: list[str], mode: str = "closed", ctype: str = "group") -> None:
        ch = make_channel(cid, name, members, mode=mode, ctype=ctype)
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
        if self.takeover_seat is not None:  # a ledger entry that makes the drafts written before it STALE (F11)
            self.session["seq"] += 1
            self.invalidating_seq = self.session["seq"]
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
    def queue_floor(self, ch: dict[str, Any], active: str | None, queue: list[dict[str, Any]], seconds: float = 90.0) -> dict[str, Any]:
        """The FloorView of a speaking queue.  The holder of the token has ``seconds`` (90 s) - except the person who plays a
        seat in a *public* channel (v0.13.1, G10, §12.2): that turn has no deadline, ``untimed`` is true and there is no
        countdown."""
        untimed = active is not None and active == self.takeover_seat and ch["type"] == "public"
        return {"kind": "queue", "active": active, "paused": False, "seconds_left": None if untimed else seconds,
                "untimed": untimed, "queue": queue, "free": None}

    async def run_queue(self, cid: str, order: list[str], speeches: dict[str, tuple[Any, ...]], *, pass_delay: float = 0.35,
                        token_seconds: float = 90.0) -> None:
        """``set_floor_queue``: one active token at a time; ``floor.changed`` advances after each speaker."""
        ch = self.channel(cid)
        total = len(order)
        queue = [{"holder": h, "state": "pending", "messages_used": 0, "max_messages": 2} for h in order]
        queue[0]["state"] = "active"
        ch["mode"] = "queue"
        self.emit("channel.updated", channel=copy.deepcopy(ch))
        self.set_floor(cid, self.queue_floor(ch, order[0], queue, token_seconds))
        for i, holder in enumerate(order):
            if holder == self.takeover_seat:
                await self.human_turn(cid, queue, i, seconds=token_seconds)  # a person plays this seat: wait for their words (F7)
            elif holder in speeches:
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
            floor = self.queue_floor(ch, order[i + 1] if i + 1 < total else None, copy.deepcopy(queue), token_seconds)
            if i + 1 < total:
                self.set_floor(cid, floor)
        # every token is over: the engine closes the channel and wakes the moderator
        self.set_floor(cid, floor_none())
        ch["mode"] = "closed"
        self.emit("channel.updated", channel=copy.deepcopy(ch))

    #: the id of the ordinary channel the mock helper ``timed_turn`` uses (a group channel: its turns are timed)
    ROUNDTABLE = "roundtable"

    def timed_turn_allowed(self) -> None:
        """Mock helper (v0.13.1): the played seat gets a TIMED speaking turn in an ordinary channel - to see a countdown
        next to the untimed turn of the public channel.  Needs a played seat and a running game; one turn at a time."""
        if self.takeover_seat is None:
            raise TakeoverError(409, "请先接管一个座位。")
        if self.session["status"] != "running":
            raise TakeoverError(409, "对局没有在运行。")
        if self.token is not None:
            raise TakeoverError(409, "你现在已经持有一个发言权。")

    async def timed_turn(self, seconds: float = 60.0) -> None:
        """One speaking turn of ``seconds`` game seconds for the played seat in the round-table channel (an ordinary group
        channel, so the turn is timed: ``untimed`` false, ``seconds_left`` / ``deadline_clock`` count down on the game
        clock - and stand still in a soft pause).  The channel is created on first use and closed again afterwards."""
        seat = self.takeover_seat
        if seat is None:
            return
        if self.ROUNDTABLE not in {c["id"] for c in self.session["channels"]}:
            self.create_channel(self.ROUNDTABLE, "圆桌（计时发言）", EVERYONE)
        self.announce(self.ROUNDTABLE, "timed_turn", f"【圆桌】请 {self.ref(seat)} 在 {int(seconds)} 秒内发言（计时的发言权，用来对照公共频道的“不限时”）。")
        await self.run_queue(self.ROUNDTABLE, [seat], {}, token_seconds=float(seconds))

    def open_free(self, cid: str, *, max_chars: int = 2000, duration: float = 300.0) -> None:
        ch = self.channel(cid)
        ch["mode"] = "free"
        free = {"chars_used": 0, "max_chars_total": max_chars, "elapsed_s": 0.0, "duration_s": duration, "silent_s": 0.0, "message_counts": {}}
        ch["floor"] = {"kind": "free", "active": None, "paused": False, "seconds_left": None, "untimed": False, "queue": [], "free": free}
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
        human = self.takeover_seat if self.takeover_seat in to else None  # a person votes for this seat (F17)
        await asyncio.gather(*(one(pid, t) for pid, t in zip(to, thinks) if pid != human))
        if human is not None:
            await self.wait_human(lambda: self._request(rid)["status"] != "open" or self.takeover_seat != human, 90.0)
            if self._request(rid)["status"] == "open":
                if self.takeover_seat != human:  # given back to its agent meanwhile: the script votes for it
                    await one(human, 0.8)
                else:  # nobody answered in time: the request closes at its deadline (F7)
                    self.close_request(rid, "deadline")
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
        self.create_channel("public", "公共频道", EVERYONE, ctype="public")  # (v0.13.1: a person's turn here has no deadline)
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

        human_wolf = self.takeover_seat if self.takeover_seat in WOLVES else None  # a person plays one of the wolves
        plan = [
            ("N3", "N5", 1.0, None),
            ("N6", "N4", 1.6, ("N5", 2.6)),
            ("N10", "N5", 2.0, None),
            ("N12", "N8", 2.4, ("N5", 3.4)),
        ]
        await asyncio.gather(*(wolf(pid, target, think, change) for pid, target, think, change in plan if pid != human_wolf))
        if human_wolf is not None:
            # the others agree on one target; the request closes when the person answers the same (or at its deadline)
            await self.wait_human(lambda: self._request(rid)["status"] != "open" or self.takeover_seat != human_wolf, 120.0)
            if self._request(rid)["status"] == "open":
                if self.takeover_seat != human_wolf:
                    await wolf(human_wolf, "N5", 0.8)
                else:
                    self.close_request(rid, "deadline")
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
        speeches = {
            "N4": (f"我是预言家。昨晚查验了 {ref('N3')}，他是狼人阵营。请大家今天出掉 N3。", 1.1),
            "N6": ("N4 的发言有点急，我是好人，想先听听其他人的意见。", 0.9),
            "N7": (LONG_SPEECH, 1.2),  # (a long speech that fits: one over the limit is sent back - TOO_LONG - and the player writes it again, never cut, H16)
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
        last_words = "我真的是好人，大家别被预言家牵着走。"
        if self.takeover_seat == "N10":  # a person plays the voted-out seat: they write their own last words
            await self.wait_human(lambda: self._request(lid)["status"] != "open" or self.takeover_seat != "N10", 60.0)
            written = next((a["value"] for a in self._request(lid)["answers"] if a["player_id"] == "N10"), None)
            if written is not None:
                last_words = written
            elif self._request(lid)["status"] == "open":
                if self.takeover_seat == "N10":
                    self.close_request(lid, "deadline")
        if last_words == "我真的是好人，大家别被预言家牵着走。" and self._request(lid)["status"] == "open":
            self.set_agent("N10", "thinking")
            await self.wait(1.4)
            self.set_agent("N10", "acting")
            self.meter("N10", tools=1, think=1)
            self.request_answer(lid, "N10", last_words)
            self.sleep("N10")
        await self.moderator_step(0.8, "主持人：转述遗言")
        self.announce("public", "last_words_relay", f"【遗言】{self.name('N10')}（N10）：{last_words}")
        self.moderator_idle()
        await self.wait(0.6)

        # 6. challenge tickets (D22): one rejected, one upheld
        self.set_phase("第 2 天 · 质询与复核")
        challenges = []
        if self.takeover_seat != "N3":  # (a played seat challenges only when its person does: in the private chat)
            challenges.append(
                self.challenge(
                    "N3",
                    "主持人，刚才的计票是不是漏了一票？我记得我只被投了 5 票。",
                    "rejected",
                    "经逐一核对，计票无误：N10 得 6 票。质询驳回。",
                    think=1.2,
                    resolve_after=2.4,
                )
            )
        if self.takeover_seat != "N8":
            challenges.append(
                self._delayed(
                    1.6,
                    self.challenge(
                        "N8",
                        "主持人，N7 在公共频道里把自己的身份和准备开枪的对象都说了出来，这样会泄露信息，请裁决要不要提醒他。",
                        "upheld",
                        "质询成立：已提醒 N7，白天发言请不要透露开枪对象，大家请注意这一点。",
                        think=1.0,
                        resolve_after=2.6,
                    ),
                )
            )
        await asyncio.gather(*challenges)
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
        chatter = [c for c in chatter if c[0] != self.takeover_seat]  # (a played seat speaks for itself)
        tasks = [asyncio.ensure_future(self._chatter("public", pid, text, t)) for pid, text, t in chatter]
        pub = self.channel("public")
        target = "N7"  # whom the explosion takes along
        scripted = True  # N12's agent explodes (the script's way); a person plays it differently
        exploded = True
        try:
            if self.takeover_seat == "N12":
                # the moderator enabled the explosion for the person who plays the white wolf king: it is their call
                scripted = False
                self.ability_open = True
                self.commit("dm-N12", MODERATOR, "白天发言开始，你的“自爆”技能现已启用（是否使用由你决定，不用就无需回复）。", "speech", visible=[MODERATOR, "N12"])
                self.schedule_takeover_flush()
                # Several speakers wait concurrently and each advances the mock's shared game clock: 30 game
                # seconds would be only a couple of wall-clock seconds in a real browser.  Give the person
                # enough time to fill the form and read the second confirmation (the real engine is unchanged).
                await self.wait_human(lambda: self.ability_result is not None or self.takeover_seat != "N12", 300.0)
                self.ability_open = False
                if self.ability_result is not None:
                    target = self.ability_result["target"]
                elif self.takeover_seat == "N12":
                    exploded = False  # they did not use it
                else:
                    scripted = True  # given back to its agent meanwhile: it explodes as scripted
            elif self.takeover_seat is not None:
                # a person plays another seat: leave them time to say something before the explosion freezes the channel
                await self.wait_human(lambda: self.free_spoken or self.takeover_seat is None, 25.0)
            if scripted:
                self.set_agent("N12", "thinking")
                await self.wait(2.7)  # N12 decides to explode while the others are still typing
                self.set_agent("N12", "acting")
                self.meter("N12", tools=2)
                self.emit("ability.used", ability_id="white_wolf_explode", actor_id="N12")
                self.commit("public", "N12", f"我是白狼王，自爆！带走猎人 {ref('N7')}。", "ability")  # the ability is the permission: no floor needed
                pub["frozen"] = True
                self.emit("channel.updated", channel=copy.deepcopy(pub))
            if exploded:
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
        if exploded:
            await self.moderator_step(1.6, "主持人：裁决白狼王自爆")
            self.player_out("N12")
            self.player_out(target)
            self.update_dashboard()
            self.announce("public", "death_notice", f"【出局公告】{ref('N12')} 自爆出局，并带走 {ref(target)}。")
            pub["frozen"] = False
            self.emit("channel.updated", channel=copy.deepcopy(pub))
        self.moderator_idle()
        await self.wait(1.0)

        # 8. the last wolves are voted out, the moderator ends the game (D8)
        await self.moderator_step(1.0, "主持人：第 3 天 · 放逐并判定胜负")
        self.close_floor("public")
        self.set_phase("第 3 天 · 终局")
        for pid in ("N3", "N6"):
            if not self.player(pid)["out"]:  # (a person's explosion may have taken one of them already)
                self.player_out(pid)
        self.update_dashboard()
        self.announce("public", "death_notice", f"【出局公告】{ref('N3')}、{ref('N6')} 出局。")
        await self.wait(0.6)
        await self.drain_bg()
        self.finish("好人阵营", "狼人阵营全部出局，好人阵营获胜（模拟对局）。")

    def finish(self, winner: str, summary: str) -> None:
        self._clear_soft()  # (v0.13.1, G6) the end of the game clears a soft pause
        s = self.session
        s["winner"] = winner
        s["summary"] = summary
        s["status"] = "finished"
        s["pause_reason"] = ""
        s["hold_reason"] = ""
        s["active_status_text"] = "对局已结束"
        self.emit("game.status", status="finished", reason="end_game", winner=winner, summary=summary)
        self.publish_session_updated()  # (``review.available`` = the game is over: the hub derives it, E21 / H31)
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
        await asyncio.gather(*(self.speak("public", pid, text, think=t) for pid, text, t in lines if pid != self.takeover_seat))
        await self.moderator_step(0.8, "主持人：再次宣布终局")
        self.close_floor("public")
        await self.drain_bg()
        self.finish("好人阵营", "终局后继续：幸存者感言结束，好人阵营获胜（模拟对局）。")

    # ----------------------------------------------------------------------------- review (H31-H34)
    async def review(self, ids: list[str], learn_style: dict[str, bool] | None = None) -> None:
        """The review after the verdict (v0.14): every bot of ``ids`` goes through the game on its own for at most 60 s (the mock plays that in a few
        seconds), writes what it learned into its memory at once (the user's edit and a review's write are the same thing, C9) and may PROPOSE a change
        of its L0 (``learn_style``: the style of the person who played the seat).  ``review.progress`` counts the seconds down and, at the end, says
        what changed; the hub does the work (``MockHub.run_review``)."""
        await self.hub.run_review(self, ids, learn_style)

    def start_review(self, ids: list[str] | None, learn_style: dict[str, bool] | None = None) -> list[str]:
        if self.session["status"] != "finished":
            raise ValueError("对局结束后才能复盘")
        if self.review_task is not None and not self.review_task.done():
            raise ValueError("复盘正在进行中")
        targets = [p for p in (ids or PLAYER_IDS) if p in PLAYER_IDS and not self.reviewed.get(p)]
        if not targets:
            raise ValueError("所选玩家本局都已复盘")
        self.review_task = asyncio.get_running_loop().create_task(self.review(targets, learn_style))
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
        problems = self.hub.check_ready()  # (H25) the start check: every L0, every initialization, the contract, the scenario, every model
        if problems:
            raise hubmod.NotReady(problems)  # nothing is dealt, nothing starts
        self.set_status("running", reason="started")
        self.gate.set()
        self._spawn(self.play())

    def pause(self, *, reason: str = "user", hold_reason: str = "") -> None:
        if self.session["status"] != "running":
            return
        self._clear_soft()  # (v0.13.1, G6/G7) a hard pause - also the top bar's 暂停 in a soft pause - ends the soft pause
        self.gate.clear()
        self.set_status("paused", reason=("hold:" + hold_reason) if reason == "hold" else reason, pause_reason=reason, hold_reason=hold_reason)
        for aid in [MODERATOR, *PLAYER_IDS]:  # a paused game: everybody sleeps (E2)
            if aid == self.takeover_seat:
                continue  # (the played seat stays "human")
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
        self._clear_soft()  # (v0.13.1) the calls a soft pause was waiting for are cancelled with everything else
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
        self._reset_human()
        self._reset_observe()  # v0.13.1: a new game has an empty error log and no thinking record
        self._reset_soft()  # ... and no soft pause
        self.bindings = self.hub.seat_bindings()  # (the bindings follow the bots, H4: a new game takes them as they are now)
        self.session = build_session(uuid.uuid4().hex[:12], self.bindings, self.hub, self.budget_limits, demo=self.demo)
        self.hub.sync_session(self.session)
        self.session["event_id"] = self.bus.latest_id + 1  # the id the snapshot event is about to get
        self.bus.publish("snapshot", {"session": copy.deepcopy(self.session)})

    async def jump_to_end(self) -> None:
        """Mock helper: cancel the script and finish right away."""
        await self._cancel_task()
        if self.session["status"] != "finished":
            self.finish("好人阵营", "（模拟）直接跳到终局。")
        self.gate.clear()

    # (v0.14: the table changes - names, avatars, the line-up - go through ``self.hub.bots_changed``: ``roster.updated`` + ``bots.updated``)

    # ----------------------------------------------------------------------------- v0.13: a person plays a seat
    # The real backend computes this with app/world/seat.py (``seat_status``); the mock re-implements the same
    # rules for its scripted world (contract §11.7), so the front end can be developed against it.
    def schedule_takeover_flush(self) -> None:
        """Publish ``takeover.updated`` once, after the current batch of events (like the orchestrator does)."""
        if self._takeover_flush_scheduled:
            return
        self._takeover_flush_scheduled = True
        try:
            asyncio.get_running_loop().call_soon(self.flush_takeover)
        except RuntimeError:  # no loop (a unit test): publish right away
            self.flush_takeover()

    def flush_takeover(self, *, force: bool = False) -> None:
        self._takeover_flush_scheduled = False
        if self.takeover_seat is None and not force:
            return
        payload = self.compute_takeover()
        if not force and payload == self.session.get("takeover"):
            return
        self.session["takeover"] = payload
        ev = self.bus.publish("takeover.updated", {"takeover": copy.deepcopy(payload)})
        self.session["event_id"] = ev["id"]

    def request_channel(self, request: dict[str, Any], seat: str) -> str | None:
        """The channel whose feed shows the request card (the smallest channel of the seat that holds every recipient)."""
        mine = [c for c in self.session["channels"] if seat in c["members"]]
        fits = [c for c in mine if request["to"] and all(t in c["members"] for t in request["to"])]
        fits.sort(key=lambda c: len(c["members"]))
        if fits:
            return fits[0]["id"]
        groups = [c for c in mine if not c["system"] and c["type"] != "dm"]
        return groups[0]["id"] if groups else None

    def seat_channel_state(self, ch: dict[str, Any], seat: str) -> dict[str, Any]:
        mode = ch["mode"]
        floor = ch["floor"] or floor_none()
        out: dict[str, Any] = {"can_write": False, "why_not": None, "code": None, "max_chars": None, "floor": None, "can_pass": False}
        mine = next((q for q in floor["queue"] if q["holder"] == seat and q["state"] == "active"), None) if mode in ("queue", "turn") else None
        if mode in ("queue", "turn"):
            tok = self.token if self.token and self.token["cid"] == ch["id"] else None
            used_chars = tok["chars_used"] if tok else 0
            out["floor"] = {
                "kind": mode, "mine": mine is not None, "paused": bool(floor["paused"]),
                "messages_left": None, "chars_left": None, "seconds_left": None, "deadline_clock": None, "untimed": False,
            }
            if mine is not None:
                # v0.13.1 (G10, §12.2): the person's token in a public channel has no deadline: no countdown, no deadline
                untimed = ch["type"] == "public" and seat == self.takeover_seat
                deadline = tok["deadline"] if tok and tok["deadline"] is not None else round(self.clock + 90.0, 3)
                out["floor"].update(
                    messages_left=max(0, mine["max_messages"] - mine["messages_used"]),
                    chars_left=max(0, TOKEN_MAX_CHARS - used_chars),
                    seconds_left=None if untimed else round(max(0.0, deadline - self.clock), 1),
                    deadline_clock=None if untimed else deadline,
                    untimed=untimed,
                )
        elif mode == "free" and not ch["system"]:
            counts = (floor.get("free") or {}).get("message_counts", {})
            out["floor"] = {
                "kind": "free", "mine": False, "paused": False,
                "messages_left": max(0, FREE_MAX_MESSAGES - counts.get(seat, 0)),
                "chars_left": None, "seconds_left": None, "deadline_clock": None, "untimed": False,
            }
        if mode in ("closed", "listen"):
            out["code"] = "CHANNEL_CLOSED"
            out["why_not"] = "你与主持人的私聊已被关闭，暂时无法在其中发言。" if ch["system"] else (
                f"频道「{ch['name']}」是只听频道，成员不能在其中发言。" if mode == "listen" else f"频道「{ch['name']}」当前已关闭，只有主持人可以发言。"
            )
        elif ch["frozen"]:
            out.update(code="NO_FLOOR", why_not="频道已被冻结，等待主持人裁决。")
        elif mode in ("queue", "turn"):
            if mine is not None and floor["paused"]:
                out.update(code="NO_FLOOR", why_not="发言权已暂停，请等待主持人恢复。")
            elif mine is not None:
                left = out["floor"]["chars_left"]
                out.update(can_write=True, max_chars=min(FREE_MAX_CHARS, left), can_pass=True)
            else:
                entry = next((q for q in floor["queue"] if q["holder"] == seat), None)
                active = [q["holder"] for q in floor["queue"] if q["state"] == "active"]
                if entry is not None and entry["state"] == "pending":
                    text = "还没轮到你发言"
                elif entry is not None:
                    text = "你在本轮的发言权已经结束"
                else:
                    text = "你没有本轮发言权"
                if active:
                    text += "，当前可以发言的是：" + "、".join(self.name(h) for h in active)
                out.update(code="NO_FLOOR", why_not=text + "。")
        elif mode == "free":
            counts = (floor.get("free") or {}).get("message_counts", {})
            if not ch["system"] and counts.get(seat, 0) >= FREE_MAX_MESSAGES:
                out.update(code="QUOTA_EXCEEDED", why_not=f"你在本次自由讨论中的发言条数已用完（上限 {FREE_MAX_MESSAGES} 条）。")
            else:
                out.update(can_write=True, max_chars=DM_MAX_CHARS if ch["system"] else FREE_MAX_CHARS)
        else:
            out.update(code="CHANNEL_CLOSED", why_not=f"频道「{ch['name']}」当前不可发言。")
        return out

    def compute_takeover(self) -> dict[str, Any]:
        s = self.session
        seat = self.takeover_seat
        out: dict[str, Any] = {"seat": seat}  # (v0.14: no "reshuffle_available": the identities are dealt when the game starts, F5 is gone)
        if seat is None:
            return out
        now = self.clock
        channels = {c["id"]: self.seat_channel_state(c, seat) for c in s["channels"] if seat in c["members"]}
        requests = []
        for r in s["requests"]:
            if r["status"] != "open" or seat not in r["to"]:
                continue
            mine = next((a for a in r["answers"] if a["player_id"] == seat), None)
            deadline = self.deadlines.get(r["id"])
            requests.append(
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "max_chars": TEXT_ANSWER_MAX_CHARS if r["kind"] == "text" else None,
                    "answered": mine is not None,
                    "my_answer": None if mine is None else {"value": mine["value"], "label": mine["label"], "reason": mine["reason"]},
                    "seconds_left": None if deadline is None else round(max(0.0, deadline - now), 1),
                    "deadline_clock": deadline,
                    "channel_id": self.request_channel(r, seat),
                }
            )
        abilities = []
        if self.ability_open and seat == "N12" and self.ability_result is None:
            abilities.append(
                {
                    "id": EXPLODE,
                    "description": EXPLODE_TEXT,
                    "quota_left": 1,
                    "params": [
                        {"name": "target", "type": "player_id", "optional": False, "candidates": self.alive()},
                        {"name": "declaration", "type": "text", "optional": False, "max_chars": 120},
                    ],
                }
            )
        attention = [
            cid
            for cid, st in channels.items()
            if (st["can_write"] and (st["floor"] or {}).get("mine")) or any(not r["answered"] and r["channel_id"] == cid for r in requests)
        ]
        out.update(seq=s["seq"], channels=channels, requests=requests, abilities=abilities, attention=attention)
        return out

    async def wait_human(self, done: Any, seconds: float) -> bool:
        """Game-time wait until ``done()`` (the person acted) or ``seconds`` passed (a paused game waits as well)."""
        end = self.clock + seconds
        while not done():
            if self.clock >= end:
                return False
            await self.wait(0.1)
        return True

    async def human_turn(self, cid: str, queue: list[dict[str, Any]], index: int, *, seconds: float = 90.0) -> None:
        """The played seat holds the token of a queue: wait for its speech / pass / the deadline (F7).  In a public channel
        (v0.13.1, G10) there is no deadline: the game waits for the person as long as it takes (the soft pause is for
        thinking about votes; here the turn is theirs) - only handing the seat back (or the end of their turn) goes on."""
        untimed = self.channel(cid)["type"] == "public"
        queue[index]["max_messages"] = TOKEN_MAX_MESSAGES
        self.token = {"cid": cid, "index": index, "queue": queue, "deadline": None if untimed else round(self.clock + seconds, 3),
                      "used": 0, "chars_used": 0, "done": False}
        tok = self.token
        self.schedule_takeover_flush()
        await self.wait_human(lambda: tok["done"] or self.takeover_seat is None, float("inf") if untimed else seconds)
        self.token = None

    # --- the actions of the person (the endpoints call these; every refusal is a receipt, like the engine's)
    def _played_seat(self) -> str:
        if self.takeover_seat is None:
            raise TakeoverError(409, "请先接管一个座位。")
        status = self.session["status"]
        if status != "running":
            why = {"idle": "对局还没有开始", "paused": "对局已暂停", "finished": "对局已结束"}.get(status, "对局没有在运行")
            raise TakeoverError(409, f"{why}，不能操作；请先点「开始」或「继续」。")
        return self.takeover_seat

    def seat_speak(self, channel_id: str, content: str, based_on_seq: int | None) -> dict[str, Any]:
        seat = self._played_seat()
        ch = next((c for c in self.session["channels"] if c["id"] == channel_id), None)
        if ch is None or seat not in ch["members"]:
            return receipt(False, f"频道 {channel_id!r} 不存在或你不是成员。", code="UNKNOWN_CHANNEL")
        text = str(content).strip()
        if not text:
            return receipt(False, "发言内容不能为空。", code="INVALID_PARAMS")
        if based_on_seq is not None and based_on_seq < self.invalidating_seq:
            return receipt(False, "你思考期间世界发生了重要变化，请先阅读最新内容后再决定是否发言。", code="STALE", latest_seq=self.session["seq"])
        state = self.seat_channel_state(ch, seat)
        if not state["can_write"]:
            return receipt(False, state["why_not"] or "现在不能发言。", code=state["code"] or "NO_FLOOR")
        cap = int(state["max_chars"])
        chars = len(text)
        if chars > cap:
            # v0.14 (H16): a speech over the limit is sent back WHOLE - never cut: nothing is written, nothing is booked, the floor stays where it was
            # (the checks of the floor come first, like the engine's gate); the receipt says how long it was and what the limit is
            return receipt(
                False, f"这条发言 {chars} 个字，超过了单条上限 {cap} 字，没有发出，发言权还在。请删减后重发。", code="TOO_LONG", chars=chars, limit=cap
            )
        msg = self.commit(channel_id, seat, text, "speech")
        if ch["mode"] in ("queue", "turn") and self.token and self.token["cid"] == channel_id:
            tok = self.token
            tok["used"] += 1
            tok["chars_used"] += chars
            for q in (ch["floor"]["queue"], tok["queue"]):
                q[tok["index"]]["messages_used"] = tok["used"]
            if tok["used"] >= TOKEN_MAX_MESSAGES or tok["chars_used"] >= TOKEN_MAX_CHARS:
                tok["done"] = True
            self.emit("floor.changed", channel_id=channel_id, floor=copy.deepcopy(ch["floor"]))
        elif ch["mode"] == "free" and ch["floor"]["free"] is not None:
            free = ch["floor"]["free"]
            free["chars_used"] += chars
            free["message_counts"][seat] = free["message_counts"].get(seat, 0) + 1
            self.emit("floor.changed", channel_id=channel_id, floor=copy.deepcopy(ch["floor"]))
        if channel_id == "public" and ch["mode"] == "free":
            self.free_spoken = True
        if channel_id in ("public", "wolves"):
            self.stir({seat}, 3)  # everybody who can read it wakes up (E1)
        return receipt(True, "已发送。", seq=msg["seq"])

    def seat_pass(self, channel_id: str) -> dict[str, Any]:
        seat = self._played_seat()
        ch = next((c for c in self.session["channels"] if c["id"] == channel_id), None)
        if ch is None or seat not in ch["members"]:
            return receipt(False, f"频道 {channel_id!r} 不存在或你不是成员。", code="UNKNOWN_CHANNEL")
        state = self.seat_channel_state(ch, seat)
        if not state["can_pass"] or not self.token or self.token["cid"] != channel_id:
            return receipt(False, "你当前没有可以放弃的发言权。", code="NO_FLOOR")
        self.token["done"] = True
        return receipt(True, "已放弃发言权。", seq=self.session["seq"])

    def seat_respond(self, request_id: str, value: str, reason: str = "") -> dict[str, Any]:
        seat = self._played_seat()
        req = next((r for r in self.session["requests"] if r["id"] == request_id), None)
        if req is None:
            return receipt(False, f"请求 {request_id} 不存在。", code="UNKNOWN_REQUEST")
        if req["status"] != "open":
            return receipt(False, f"请求 {request_id} 已经关闭，不能再作答。", code="REQUEST_CLOSED")
        if seat not in req["to"]:
            return receipt(False, f"请求 {request_id} 不是发给你的。", code="FORBIDDEN")
        text = str(value).strip()
        truncated = None
        if req["kind"] == "choice":
            valid = [o["id"] for o in req["options"]]
            if text not in valid:
                return receipt(False, f"「{value}」不是有效的选项。合法的选项 id：{'、'.join(valid)}。", code="INVALID_TARGET")
        else:
            if not text:
                return receipt(False, "回答内容不能为空。", code="INVALID_PARAMS")
            if len(text) > TEXT_ANSWER_MAX_CHARS:
                truncated = {"dropped_chars": len(text) - TEXT_ANSWER_MAX_CHARS, "kept_chars": TEXT_ANSWER_MAX_CHARS}
                text = text[:TEXT_ANSWER_MAX_CHARS]
        replaced = seat in req["answered_by"]  # (a person's answers cost no tokens: nothing is metered)
        self.request_answer(request_id, seat, text, reason=str(reason or ""))
        closed = req["status"] != "open"
        return receipt(True, f"已记录你对 {request_id} 的回答。" + ("" if closed else "在请求关闭前你可以再次作答来修改。"),
                       seq=self.session["seq"], truncated=truncated, request_id=request_id, value=text, replaced=replaced, closed=closed)

    def seat_ability(self, ability_id: str, params: dict[str, Any]) -> dict[str, Any]:
        seat = self._played_seat()
        if ability_id != EXPLODE:
            return receipt(False, f"没有名为 {ability_id} 的能力。已知能力：{EXPLODE}", code="UNKNOWN_ABILITY")
        if seat != "N12":
            return receipt(False, f"你不是能力 {ability_id} 的持有者。", code="FORBIDDEN")
        if not self.ability_open or self.ability_result is not None:
            return receipt(False, f"能力 {ability_id} 当前不可用（未启用）。", code="ABILITY_DISABLED")
        target = (params or {}).get("target")
        declaration = str((params or {}).get("declaration") or "").strip()
        if not isinstance(target, str) or target not in PLAYER_IDS:
            return receipt(False, f"参数 target：{target!r} 不是有效的玩家 id", code="INVALID_TARGET")
        if target == seat or self.player(target)["out"]:
            return receipt(False, "目标必须是存活的其他玩家。", code="INVALID_TARGET")
        if not declaration:
            return receipt(False, "缺少参数：declaration", code="INVALID_PARAMS")
        declaration = declaration[:120]
        self.ability_result = {"target": target, "declaration": declaration}
        self.ability_open = False
        self.emit("ability.used", ability_id=EXPLODE, actor_id=seat)
        msg = self.commit("public", seat, f"【自爆】{self.ref(seat)} 亮明白狼王身份并自爆，带走 {self.ref(target)}。{declaration}", "ability")
        pub = self.channel("public")
        pub["frozen"] = True
        self.emit("channel.updated", channel=copy.deepcopy(pub))
        return receipt(True, f"已发动能力 {ability_id}（剩余 0 次）。后续裁决由主持人完成。", seq=msg["seq"], ability_id=ability_id, uses_left=0)

    # --- takeover / release (v0.14: no re-deal - the identities are dealt when the game starts, H52)
    def take_over(self, seat: Any) -> None:
        if not isinstance(seat, str) or not seat:
            raise TakeoverError(422, "请选择要接管的座位。")
        if seat == MODERATOR:
            raise TakeoverError(422, "只能接管玩家座位，不能接管主持人。")
        if seat not in PLAYER_IDS:
            raise TakeoverError(404, "座位不存在。")
        if self.takeover_seat == seat:
            return
        if self.takeover_seat is not None:
            raise TakeoverError(409, "请先退出当前接管。")
        status = self.session["status"]
        if status == "running":
            raise TakeoverError(409, "对局进行中不能接管：请先暂停。")
        if status == "finished":
            raise TakeoverError(409, "对局已结束，不能接管。")
        self.takeover_seat = seat
        self.human_seats.add(seat)  # (the review of the game offers to learn this person's style, H34; ``human_involved`` in the lists)
        node = self.player(seat)
        node["state"] = "human"
        node["sleep_until"] = None
        self.flush_takeover(force=True)

    def release_seat(self) -> None:
        if self.takeover_seat is None:
            return
        if self.session["status"] == "running":
            raise TakeoverError(409, "对局进行中不能退出接管：请先暂停。")
        seat = self.takeover_seat
        self.takeover_seat = None
        node = self.player(seat)
        node["state"] = "sleeping"
        node["sleep_until"] = None
        self.ability_open = False
        if self.token is not None:
            self.token["done"] = True
        payload = self.compute_takeover()
        self.session["takeover"] = payload
        ev = self.bus.publish("takeover.updated", {"takeover": copy.deepcopy(payload)})
        self.session["event_id"] = ev["id"]


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
# v0.13 (F34-F36): the history of games.  A replay of a saved game holds what a LEDGER holds - the spectator
# events (messages, channels, floors, requests, tags, phases, ...) with the game-clock second they happened at -
# and nothing else: no agent states, no token counters, no review progress (contract section 11.8).
# --------------------------------------------------------------------------------------------------

#: the event types a saved game's replay consists of (what app/game/replay.py projects from a ledger)
REPLAY_EVENT_TYPES = frozenset(
    {
        "message.committed", "channel.created", "channel.updated", "floor.changed",
        "request.opened", "request.progress", "request.answer", "request.closed", "request.updated",
        "player.updated", "phase.changed", "dashboard.updated", "ability.used",
        "challenge.opened", "challenge.resolved", "game.status",
    }
)


async def _history_replay(seed: int) -> dict[str, Any]:
    """The scripted game, played offline (virtual time), as a replay: ``initial`` / ``events`` (each with its game
    clock ``gt``; the id of the event doubles as its ``seq``, so every event is a step of its own) / ``final``."""
    bus = EventBus(ring=100000)
    game = MockGame(bus, speed=1.0, seed=seed, budget=default_budget(None, False))
    clocks: dict[int, float] = {}
    publish = bus.publish

    def stamped(etype: str, data: dict[str, Any]) -> dict[str, Any]:
        ev = publish(etype, data)
        clocks[ev["id"]] = round(float(game.session["clock"]), 3)
        return ev

    bus.publish = stamped  # type: ignore[method-assign]
    initial = game.snapshot_payload()
    game.gate.set()
    game.set_status("running", reason="started")
    await game.play()
    final = game.snapshot_payload()
    events = [
        {"seq": ev["id"], "gt": clocks.get(ev["id"], 0.0), "type": ev["type"], "data": ev["data"]}
        for ev in bus.ring
        if ev["type"] in REPLAY_EVENT_TYPES
    ]
    return {"initial": initial, "events": events, "final": final, "duration_gt": events[-1]["gt"] if events else 0.0}


def history_replay(seed: int = 7) -> dict[str, Any]:
    """:func:`_history_replay` on a loop of its own (it may be called from a worker thread of the server)."""
    return asyncio.run(_history_replay(seed), loop_factory=VirtualLoop)


def mock_saved_games() -> list[dict[str, Any]]:
    """The older games of the history list and of "对局管理" (the summaries of ``GameStore`` with the v0.14 fields, §13.7): v6 and v5 games, one that is
    paused with a person in it, one that cannot be read; the sample lives in ``mock_ui_hub.MockGames``."""
    return hubmod.MockGames().sample()


# --------------------------------------------------------------------------------------------------
# the FastAPI app
# --------------------------------------------------------------------------------------------------


def create_app(*, speed: float = 1.0, seed: int = 7, budget_tokens: int | None = DEMO_BUDGET_TOKENS, demo: bool = False, legacy: bool = False,
               bots: str = "ready") -> Any:
    """``bots``: the state the twelve bots start in - ``ready`` (every one ✔, the way the default library comes), ``fresh`` (L0 only: nobody initialized),
    ``mixed`` (✔ ⚠ ✖ and a bot without an L0), ``empty`` (no L0 at all); ``POST /api/mock/bots`` changes it later (idle game only)."""
    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
    from fastapi.staticfiles import StaticFiles

    bus = EventBus()
    holder: dict[str, Any] = {}
    parts = load_parts()
    store: dict[str, Any] = {
        "providers": copy.deepcopy(PROVIDERS),
        "templates": [
            {"slot": 1, "name": "旗舰", "provider_id": "mock-claude", "model_id": "claude-sonnet", "reasoning_depth": "high", "valid": True},
        ],
        "sessions": [],
        "pricing": {"url": DEFAULT_PRICING_URL, "default_url": DEFAULT_PRICING_URL, "fetched_at": now_iso(), "entries": 2345, "error": None},
    }
    # v0.14: the bots, the contract, the rules and the older games (they outlive a game); the hub ties them to the game
    stores = hubmod.Stores(seed + 101, avatars=MockAvatars(random.Random(seed + 101), parts), preset=bots)
    games_files = hubmod.MockGames([stores.bots.name_of(i) for i in stores.bots.order])
    hub = hubmod.MockHub(stores, games=games_files, game_fn=lambda: game(), providers_fn=lambda: store["providers"])
    initial_budget = default_budget(budget_tokens if budget_tokens else None, True)

    app = FastAPI(title="Arena-Agents UI mock", version=APP_VERSION)
    app.state.mock_holder = holder  # tests reach the MockGame (created on the first request) through this
    app.state.mock_hub = hub
    app.state.mock_stores = stores

    def game() -> MockGame:
        if "game" not in holder:  # created lazily: the asyncio primitives need a running loop
            holder["game"] = MockGame(bus, speed=speed, seed=seed, hub=hub, budget=initial_budget, demo=demo)
        return holder["game"]

    archive = MockArchive(bus, game, speed=speed)  # v0.13: export / import jobs and the "old data found" notice
    archive.legacy["found"] = bool(legacy)
    games_files.busy_hook = lambda: archive.busy is not None  # (an archive export / import running: no game import)
    app.state.mock_archive = archive

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
            "legacy_data": archive.legacy_state(),
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
    async def control_start() -> Any:
        try:
            game().start()
        except hubmod.NotReady as exc:  # (H25) 409 NOT_READY with the structured reasons; nothing was dealt
            return JSONResponse(status_code=409, content={"detail": str(exc), "code": "NOT_READY", "problems": exc.problems})
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

    # ---- v0.13: a person plays a seat (contract §11.7)
    def refuse(exc: TakeoverError) -> HTTPException:
        return HTTPException(status_code=exc.status, detail=exc.detail)

    # ---- v0.13.1: the soft pause (contract §12.2): 409 with a sentence (full stop) when it is not the time for it
    @app.post("/api/control/soft-pause")
    async def control_soft_pause() -> dict[str, Any]:
        try:
            game().soft_pause()
        except TakeoverError as exc:
            raise refuse(exc) from exc
        return {"ok": True, "session": session_payload()}

    @app.post("/api/control/soft-resume")
    async def control_soft_resume() -> dict[str, Any]:
        try:
            game().soft_resume()
        except TakeoverError as exc:
            raise refuse(exc) from exc
        return {"ok": True, "session": session_payload()}

    # (v0.14: POST /api/control/shuffle-identities is gone - the identities are dealt when the game starts, H52)

    @app.post("/api/takeover")
    async def takeover(body: dict[str, Any]) -> dict[str, Any]:
        try:
            game().take_over(body.get("seat"))
        except TakeoverError as exc:
            raise refuse(exc) from exc
        return {"ok": True, "session": session_payload()}

    @app.post("/api/takeover/release")
    async def takeover_release() -> dict[str, Any]:
        try:
            game().release_seat()
        except TakeoverError as exc:
            raise refuse(exc) from exc
        return {"ok": True, "session": session_payload()}

    def seat_action(call: Any) -> dict[str, Any]:
        try:
            return call()
        except TakeoverError as exc:
            raise refuse(exc) from exc

    def based_on(body: dict[str, Any]) -> int | None:
        value = body.get("based_on_seq")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(status_code=422, detail="based_on_seq 必须是整数")
        return value

    @app.post("/api/seat/speak")
    async def seat_speak(body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body.get("channel_id"), str) or not isinstance(body.get("content"), str):
            raise HTTPException(status_code=422, detail="需要 channel_id 与 content")
        return seat_action(lambda: game().seat_speak(body["channel_id"], body["content"], based_on(body)))

    @app.post("/api/seat/pass")
    async def seat_pass(body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body.get("channel_id"), str):
            raise HTTPException(status_code=422, detail="需要 channel_id")
        return seat_action(lambda: game().seat_pass(body["channel_id"]))

    @app.post("/api/seat/respond")
    async def seat_respond(body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body.get("request_id"), str) or not isinstance(body.get("value"), str):
            raise HTTPException(status_code=422, detail="需要 request_id 与 value")
        return seat_action(lambda: game().seat_respond(body["request_id"], body["value"], str(body.get("reason") or "")))

    @app.post("/api/seat/ability")
    async def seat_ability(body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body.get("ability_id"), str) or not isinstance(body.get("params", {}), dict):
            raise HTTPException(status_code=422, detail="需要 ability_id 与 params")
        return seat_action(lambda: game().seat_ability(body["ability_id"], body.get("params") or {}))

    # ---- v0.13 (F34-F36) + v0.14 (H40): the history of games - list, delete, continue, replay (contract sections 11.3, 11.5, 11.8, 13.7).  What the
    # "对局管理" of the settings adds (the detail of a game, the .arenagame export / import) is in ``mock_ui_hub.register_routes``.
    games_files = hub.games

    def games_checked(game_id: str) -> str:
        try:
            return games_files.checked(game_id)
        except mb.Refusal as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    @app.get("/api/games")
    async def list_games() -> dict[str, Any]:
        return games_files.listing(game())

    @app.delete("/api/games/{session_id}")
    async def delete_game(session_id: str) -> Any:
        """Deletes the save and the logs of a game and says what it does NOT delete (``note``: what the bots learned stays).  The current game
        can be deleted too unless it runs: a new empty game takes its place (``new_game`` / ``new_session_id``; a snapshot follows)."""
        try:
            out = games_files.delete(game(), session_id)
            in_use = bool(out.pop("in_use", False))
            if out.get("new_game"):
                await game().reset()  # (publishes the new game's snapshot)
                out["new_session_id"] = game().session["session_id"]
            if in_use:
                # the current game's file could not be removed for the moment: the empty new game exists all the same
                raise mb.Refusal(409, "这局的文件现在被占用，暂时删不掉；已经新建了一局空白对局，旧的这局稍后可以再删。", "in_use", new_game=True, new_session_id=out["new_session_id"])
            return out
        except mb.Refusal as exc:
            return JSONResponse(status_code=exc.status, content=exc.body)

    @app.post("/api/games/{session_id}/load")
    async def load_game(session_id: str) -> dict[str, Any]:
        games_checked(session_id)
        g = game()
        if session_id == g.session["session_id"]:
            raise HTTPException(status_code=409, detail="这就是当前对局，无需载入。")
        found = games_files.find(session_id)
        if found is None:
            raise HTTPException(status_code=404, detail="对局不存在。")
        if not found.get("readable", True):
            raise HTTPException(status_code=400, detail=hubmod.UNREADABLE_LOAD)
        if g.session["status"] == "running":
            raise HTTPException(status_code=409, detail="对局进行中，不能载入其他对局：请先暂停。")
        # (the mock cannot rebuild a stored game: it deals a fresh one that carries the requested id)
        await g.reset()
        g.session["session_id"] = session_id
        g.bus.publish("snapshot", {"session": g.snapshot_payload()})
        return {"ok": True, "session": session_payload()}

    @app.get("/api/games/{session_id}/replay")
    async def replay_of_game(session_id: str) -> dict[str, Any]:
        games_checked(session_id)
        g = game()
        known = games_files.find(session_id)
        if session_id == g.session["session_id"]:
            known = games_files.live_item(g)
            if g.session["status"] == "running" or g.takeover_seat is not None:
                raise HTTPException(status_code=409, detail="对局进行中或正在接管一个座位，不能重放当前对局：请先暂停并退出接管。")
        if known is None:
            raise HTTPException(status_code=404, detail="对局不存在。")
        if not known.get("readable", True):
            raise HTTPException(status_code=422, detail=hubmod.UNREADABLE_SAVE)
        if "history_replay" not in holder:  # the script, played once offline in a worker thread (own event loop)
            holder["history_replay"] = await asyncio.to_thread(history_replay, seed)
        data = holder["history_replay"]
        initial = copy.deepcopy(data["initial"])
        initial["session_id"] = session_id
        return {
            "version": 1,
            "game": {
                "session_id": session_id, "title": known["title"], "scenario_title": known["scenario_title"], "status": "finished",
                "winner": data["final"]["winner"], "created_at": known.get("created_at"), "finished_at": known.get("finished_at"),
                "human_involved": bool(known.get("human_involved")),
            },
            "initial": initial,
            "events": copy.deepcopy(data["events"]),
            "duration_gt": data["duration_gt"],
        }

    # ---- v0.14: the bots (names, avatars, L0, memory, initialization, the line-up), the official contract, the rules, the new games routes
    # (/api/bots/**, /api/moderator, /api/contract, /api/rules/**, /api/games/{id}/detail | export, /api/games/import and the /api/mock/{bots,files,learning}
    # switches); /api/roster/** is gone (H55)
    hubmod.register_routes(app, hub, game)

    # ---- v0.13.1: the error log (G32-G36, contract §12.3) and the thinking record (G21-G26, §12.4)
    @app.get("/api/llm/calls")
    async def get_llm_calls(after: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=2000), errors_only: bool = False) -> dict[str, Any]:
        """Every model request and runtime error of the game: the lines with an id above ``after`` (the newest ``limit`` of
        them), oldest first; ``errors_only`` keeps ``retry`` / ``timeout`` / ``error``.  Also while a seat is played."""
        return game().calls_view(after=after, limit=limit, errors_only=errors_only)

    @app.get("/api/agents/{agent_id}/thoughts")
    async def get_agent_thoughts(agent_id: str, before: str | None = Query(None, max_length=64), limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
        """What an agent thought (never its tool calls), newest page first; 409 while ANOTHER seat is played and the game is
        not over (G25), 404 for an id that is no agent, 400 for a cursor that is none."""
        try:
            return game().thought_page(agent_id, before, limit)
        except TakeoverError as exc:
            raise refuse(exc) from exc

    # ---- game review (E21, v0.14 H31-H34): {"ids"?: ["N1", ...], "learn_style"?: {"N1": true}} -> {"started": [seat ids], "deadline_s": 60}
    @app.post("/api/game/review")
    async def game_review(body: dict[str, Any] | None = None) -> Any:
        body = body if isinstance(body, dict) else {}
        ids, learn = body.get("ids"), body.get("learn_style")
        if ids is not None and (not isinstance(ids, list) or any(not isinstance(i, str) for i in ids)):
            raise HTTPException(status_code=422, detail="ids 必须是座位 id 的列表。")
        if learn is not None and (not isinstance(learn, dict) or any(not isinstance(v, bool) for v in learn.values())):
            raise HTTPException(status_code=422, detail="learn_style 必须是 {座位 id: true / false} 的对象。")
        try:
            started = game().start_review(ids, learn)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"started": started, "deadline_s": mb.REVIEW_TIMEOUT_S}

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

    # ---- v0.13 (F24, F26-F32): archive export / import and the "old data found" notice (contract sections 11.5 / 11.9)
    def archive_reply(result: tuple[int, dict[str, Any]]) -> Any:
        return JSONResponse(status_code=result[0], content=result[1])

    @app.get("/api/archive/status")
    async def archive_status() -> dict[str, Any]:
        return archive.status()

    @app.post("/api/archive/export")
    async def archive_export(body: dict[str, Any] | None = None) -> Any:
        body = body or {}
        # (v0.14, H36: ``include_logs`` is accepted - a v0.13 page still sends it - and ignored: the logs are always in the archive)
        return archive_reply(archive.start_export(bool(body.get("include_keys")), body.get("password"), True))

    @app.get("/api/archive/jobs/{job_id}")
    async def archive_job(job_id: str) -> Any:
        return archive_reply(archive.get_job(job_id))

    @app.get("/api/archive/exports/{name}")
    async def archive_download(name: str) -> Any:
        data = archive.export_file(name)
        if data is None:
            return archive_reply(archive_error(404, "not_found", "找不到这个导出文件。"))
        return Response(data, media_type="application/octet-stream", headers={"Content-Disposition": hubmod.content_disposition(archive.download_name_of(name))})

    @app.post("/api/archive/import")
    async def archive_upload(request: Request, name: str = "") -> Any:
        data = await request.body()  # (the real server streams it to a temporary file instead)
        return archive_reply(archive.receive(data, re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)[:120] or "archive.arenaagents"))

    @app.post("/api/archive/import/{import_id}/apply")
    async def archive_apply(import_id: str, body: dict[str, Any] | None = None) -> Any:
        body = body or {}
        return archive_reply(archive.start_apply(import_id, body.get("password"), bool(body.get("skip_keys"))))

    @app.delete("/api/archive/import/{import_id}")
    async def archive_cancel(import_id: str) -> dict[str, Any]:
        return {"ok": True, "discarded": archive.discard(import_id)}

    @app.post("/api/notices/legacy-data:dismiss")
    async def dismiss_legacy_data() -> dict[str, Any]:
        archive.legacy["dismissed"] = True
        return {"ok": True}

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
        # (v0.14, H4: the API still speaks in seats - N1..N12 and moderator - and the hub keeps the bindings by bot id: they follow the bots)
        hub.load_seat_bindings(body.get("bindings") or {})
        g.bindings = hub.seat_bindings()
        g.session["model_bindings"] = copy.deepcopy(g.bindings)
        hub.publish_bots_updated()  # (the model of every bot, the start check, ``session.updated`` with the bindings)
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
        hub.update_seat_bindings(updated)  # (kept by bot id, H4)
        g.bindings = hub.seat_bindings()
        g.session["model_bindings"] = copy.deepcopy(g.bindings)
        hub.publish_bots_updated()
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

    @app.post("/api/mock/timed-turn")
    async def mock_timed_turn(body: dict[str, Any] | None = None) -> dict[str, Any]:
        """v0.13.1: the played seat gets a TIMED speaking turn ({"seconds": 60}) in an ordinary channel (the round table) - to
        see a countdown next to the public channel's "不限时" turn, and a soft pause stopping it.  409 without a played seat
        or a running game."""
        g = game()
        try:
            seconds = float((body or {}).get("seconds", 60.0))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="seconds must be a number") from exc
        if not 5.0 <= seconds <= 600.0:
            raise HTTPException(status_code=422, detail="seconds must be between 5 and 600")
        try:
            g.timed_turn_allowed()
        except TakeoverError as exc:
            raise refuse(exc) from exc
        g.spawn_bg(g.timed_turn(seconds))
        return {"ok": True, "channel_id": g.ROUNDTABLE, "seconds": seconds}

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

    @app.post("/api/mock/archive")
    async def mock_archive_setup(body: dict[str, Any]) -> dict[str, Any]:
        """Configure the archive mock: {"legacy": bool} shows / hides the "old data found" notice (and un-dismisses it),
        {"max_bytes": n} is the upload limit, {"tick": seconds} the time between two progress events."""
        if "legacy" in body:
            archive.legacy.update(found=bool(body["legacy"]), dismissed=False)
        if _is_int(body.get("max_bytes")) and body["max_bytes"] > 0:
            archive.max_bytes = int(body["max_bytes"])
        if _is_num(body.get("tick")) and body["tick"] >= 0:
            archive.tick = float(body["tick"])
        return {"ok": True, "legacy": archive.legacy_state(), "max_bytes": archive.max_bytes, "tick": archive.tick}

    @app.post("/api/mock/llm-fail")
    async def mock_llm_fail(body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Inject ONE failed model request (v0.13.1): ``retries`` attempts that the client retries (the ``retry`` lines),
        then the request ends as ``status`` (``error`` / ``timeout`` / ``cancelled``); with ``give_up`` (the default) the agent
        gives its action up as well: a runtime line and the ``error`` event (the "重试用尽" toast).  Body (all optional):
        ``{"actor_id": "N3", "status": "error", "retries": 2, "http_status": 429, "error_class": "RateLimitError",
        "message": "...", "give_up": true, "purpose": "turn"}``."""
        b = body or {}
        actor = str(b.get("actor_id") or "N3")
        if actor not in (MODERATOR, "roster", ENGINE, *PLAYER_IDS):
            raise HTTPException(status_code=404, detail="没有这个玩家。")
        status = b.get("status", "error")
        if status not in ("error", "timeout", "cancelled"):
            raise HTTPException(status_code=422, detail="status 必须是 error / timeout / cancelled")
        retries = b.get("retries", 2)
        http_status = b.get("http_status", 429)
        if not _is_int(retries) or not 0 <= retries <= 5 or (http_status is not None and not _is_int(http_status)):
            raise HTTPException(status_code=422, detail="retries 必须是 0–5 的整数，http_status 必须是整数或 null")
        purpose = b.get("purpose", "refine" if actor == "roster" else "turn")
        if purpose not in ("turn", "review", "refine"):
            raise HTTPException(status_code=422, detail="purpose 必须是 turn / review / refine")
        g = game()
        lines = g.inject_failure(actor, status=status, retries=retries, http_status=http_status, error_class=str(b.get("error_class") or "RateLimitError"),
                                 message=str(b["message"]) if b.get("message") else None, give_up=bool(b.get("give_up", True)), purpose=purpose)
        return {"ok": True, "ids": [c["id"] for c in lines], "errors": g.call_counts()[0]}

    @app.post("/api/mock/llm-rebuild")
    async def mock_llm_rebuild(body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Rebuild the error log the way the real backend does after a restart, a load or an import (v0.13.1, contract §12.3):
        numbered from 1 again, some lines gone, the failed ones a few ms off, nothing announced.  Body (optional):
        ``{"drop_every": 4, "jitter_ms": 3}``.  To see why the page re-bases its "read" mark: press 全部标为已读 (the badge goes),
        call this, reload the page (or just wait for the next reconnect), then ``POST /api/mock/llm-fail`` - the new error has a
        LOW id and the badge must show it."""
        b = body or {}
        drop = b.get("drop_every", 4)
        jitter = b.get("jitter_ms", 3)
        if not _is_int(drop) or not 0 <= drop <= 1000 or not _is_int(jitter) or not 0 <= jitter <= 10_000:
            raise HTTPException(status_code=422, detail="drop_every 必须是 0–1000 的整数，jitter_ms 必须是 0–10000 的整数")
        return game().rebuild_calls(drop_every=int(drop), jitter_ms=int(jitter))

    @app.post("/api/mock/emit")
    async def mock_emit(body: dict[str, Any]) -> dict[str, Any]:
        """Publish an arbitrary event (e.g. {"type": "archive.progress", "data": {...}}); validated first."""
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
                "ring": len(bus.ring), "clock": g.session["clock"], "budget": g.session["budget"], "calls": len(g.calls)}

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------

REQUIRED_EVENTS = {
    "message.committed", "channel.created", "channel.updated", "floor.changed", "player.updated", "request.opened",
    "request.progress", "request.answer", "request.closed", "challenge.opened", "challenge.resolved", "ability.used",
    "phase.changed", "dashboard.updated", "game.status", "agent.status", "agent.metrics", "session.updated",
    "budget.updated", "budget.warning", "budget.reached", "review.progress", "llm.call",
    "bots.updated",  # v0.14: a review changes what the bots remember (an initialization is not part of the scripted game: see tests/frontend/test_mock_bots.py)
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
    parser = argparse.ArgumentParser(description="Arena-Agents v0.14 UI mock server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--bots", default="ready", choices=["ready", "fresh", "mixed", "empty"],
                        help="the state the twelve bots start in: ready = every one initialized (✔); fresh = L0 only, nobody initialized; "
                             "mixed = ✔ ⚠ ✖ and a bot without an L0; empty = no L0 at all (POST /api/mock/bots changes it later)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier (default 1.0 ≈ 60 s per game)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--budget-tokens", type=int, default=DEMO_BUDGET_TOKENS,
                        help="demo token limit with 'force stop' (the replay warns at 80 %% and pauses at 100 %%); 0 = no limit")
    parser.add_argument("--demo", action="store_true", help='set session.demo (the page shows the "演示模式" badge)')
    parser.add_argument("--legacy", action="store_true", help='show the "old v0.12.1 data found" notice (the notice bar of the settings)')
    parser.add_argument("--selfcheck", action="store_true", help="replay the script offline and validate every event")
    args = parser.parse_args(argv)
    if args.selfcheck:
        return selfcheck()
    import uvicorn

    print(f"Mock UI server: http://{args.host}:{args.port}/  (speed x{args.speed}, seed {args.seed}, "
          f"demo budget {args.budget_tokens or 'off'} tokens)")
    uvicorn.run(create_app(speed=args.speed, seed=args.seed, budget_tokens=args.budget_tokens, demo=args.demo, legacy=args.legacy, bots=args.bots),
                host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
