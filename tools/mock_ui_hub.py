"""The v0.14 bots at the table, behind the HTTP API (docs/v0.12-contracts.md §13.3, §13.7, §13.8) - the part of the UI mock that needs the game.

``tools/mock_ui_bots.py`` holds the model (the bot store, the contract, the rules).  This module is what the orchestrator
(``app/game/bots_runtime.py``, ``learning_runtime.py``) and the API layers do with it:

* ``MockHub`` - ``BotsPayload`` / ``BotDetail`` (seats, model bindings, readiness), the start check (``NOT_READY``), ``system_status``,
  the initialization jobs (``bot.init.progress``), the review of a finished game (``review.progress``), ``bots.updated`` / ``roster.updated``;
* ``MockGames`` - the games of the "对局管理" (list with sizes and review counts, detail, delete with its note, ``.arenagame`` export / import);
* ``register_routes`` - every ``/api/bots/**``, ``/api/moderator``, ``/api/contract``, ``/api/rules/**`` and ``/api/games/**`` (the new ones) route,
  and the ``/api/mock/*`` switches that put the mock into the situations a page has to show (a bot that failed, a missing contract ...).

Time: an initialization takes "120 seconds" at most, a review 60; the mock plays them in a few seconds (``sim_rate`` simulated seconds per
tick, a tick is a second at speed 1), so a countdown is visible but nobody waits two minutes.
"""

# (no ``from __future__ import annotations`` here: FastAPI reads the annotations of the route functions, and ``Request`` is imported inside
# ``register_routes`` - the annotations have to be the objects themselves, not strings that would be looked up in this module's globals)

import asyncio
import copy
import io
import json
import re
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable
from urllib.parse import quote

import mock_ui_bots as mb
from mock_ui_bots import FileProblems, Refusal

MODERATOR = "moderator"
GAME_NOTE = "bot 已经通过复盘形成的记忆不会被删除，也无法通过删除对局撤销。"
#: What the real server says about a save it cannot open (``GET /api/games/{id}/detail`` and ``/replay``: 422; ``StorageError`` of the game store).
UNREADABLE_SAVE = "无法读取这局的存档：存档文件已损坏，或者格式太旧（v0.12.1 及更早）。"
#: ``POST /api/games/{id}/load`` of such a save (400, ``SessionLoadError``): the old-format notice.
UNREADABLE_LOAD = (
    "这局来自旧版本引擎（v0.12.1 及更早），v0.14 不兼容，不能载入；原文件没有被改动。请用 tools/migrate_v0121_to_archive.py 转换后导入。"
)
#: how a pause is explained in the status label (``system_status.reason``; app/game/bots_runtime.py)
PAUSE_TEXTS = {
    "user": "手动暂停",
    "hold": "主持人暂停了对局",
    "stall": "对局长时间没有进展，已暂停",
    "error": "运行出错，已暂停",
    "budget": "已达到预算上限，已暂停",
}
INIT_STATES = {"queued", "running", "done", "failed", "timeout", "cancelled"}
REVIEW_STATES = {"queued", "running", "done", "error", "timeout"}
IMPRESSION_BANK = [
    "发言短，站边快，可信度中。",
    "逻辑细，抓细节准，值得多听他的。",
    "爱带节奏，票型和发言常对不上，要留意。",
    "话少，关键时才开口，看不透。",
    "容易被带着走，附和多于判断。",
    "喜欢强攻，风险偏好高，拿到好牌时更爱悍跳。",
]
REVIEW_QUESTIONS = ["被两个人同时指认的时候，该先信谁？", "第一天警上发言，到底该不该站边？", "平票 PK 的发言怎样才算有说服力？"]


def now_unix() -> int:
    return int(datetime.now(timezone.utc).timestamp())


# --------------------------------------------------------------------------------------------------
# the start check
# --------------------------------------------------------------------------------------------------


def not_ready_text(problems: list[dict[str, Any]]) -> str:
    """One Chinese sentence for ``NOT_READY`` (app/game/bots_runtime.py)."""
    parts: list[str] = []
    for problem in problems[:3]:
        who = problem.get("name") or {"contract": "官方契约", "scenario": "剧本", "binding": "模型绑定", "game": "对局"}.get(str(problem.get("scope")), "")
        items = problem.get("items") or []
        first = str(items[0].get("message") if items else "").strip().rstrip("。")
        parts.append(f"{who}：{first}" if who else first)
    more = f"（共 {len(problems)} 处）" if len(problems) > 3 else ""
    return "还没有准备好，不能开局：" + "；".join(p for p in parts if p) + more + "。"


class NotReady(Exception):
    """The game cannot start yet (H25): ``problems`` are the ``Problem`` objects of §13.7.  The API answers 409 ``NOT_READY``."""

    def __init__(self, problems: list[dict[str, Any]]) -> None:
        self.problems = problems
        super().__init__(not_ready_text(problems))


def problem(scope: str, code: str, message: str, fix: str, **who: Any) -> dict[str, Any]:
    return {"scope": scope, "bot_id": who.get("bot_id"), "seat": who.get("seat"), "name": who.get("name"), "items": [{"code": code, "message": message, "fix": fix}]}


# --------------------------------------------------------------------------------------------------
# the stores
# --------------------------------------------------------------------------------------------------


class Stores:
    """The bot store, the contract and the rules (they outlive a game: a new game keeps them)."""

    def __init__(self, seed: int = 7, *, avatars: mb.AvatarPicker | None = None, clock: Callable[[], float] | None = None, preset: str = "ready") -> None:
        self.contract = mb.MockContract(clock)
        self.rules = mb.MockRules(clock)
        self.bots = mb.MockBots(seed, sources=self.sources, avatars=avatars, clock=clock, preset=preset)

    def scenario_id(self) -> str:
        ids = self.rules.ids()
        return mb.DEFAULT_SCENARIO_ID if mb.DEFAULT_SCENARIO_ID in ids or not ids else ids[0]

    def sources(self) -> dict[str, str]:
        """The hashes an initialization depends on (apart from the L0): the contract, the scenario and its study materials."""
        sid = self.scenario_id()
        contract = "" if self.contract.broken == "missing" else self.contract.sha256
        scenario = "" if self.rules.broken.get(sid) == "missing" or sid not in self.rules.scenarios else self.rules.scenario_hash(sid)
        return {"contract": contract, "scenario": scenario, "materials": self.rules.materials_hash(sid), "scenario_id": sid}


# --------------------------------------------------------------------------------------------------
# games (the "对局管理" of the settings)
# --------------------------------------------------------------------------------------------------

ROLE_OF_SEAT = {
    "N1": "平民", "N2": "平民", "N3": "狼人", "N4": "预言家", "N5": "女巫", "N6": "狼人", "N7": "猎人", "N8": "守卫", "N9": "平民", "N10": "狼人", "N11": "平民",
    "N12": "白狼王",
}
WOLF_ROLES = {"狼人", "白狼王"}


def faction_of(role: str) -> str:
    return "狼人阵营" if role in WOLF_ROLES else "好人阵营"


class MockGames:
    """The older games of the history (summaries with the v0.14 fields) and what is done to them: detail, delete, ``.arenagame``."""

    ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

    def __init__(self, names: list[str] | None = None) -> None:
        self.names = names or list(mb.DEFAULT_NAMES)
        self.items: list[dict[str, Any]] = self.sample()
        #: ids whose files "cannot be deleted for the moment" (in use): ``DELETE`` answers 409 ``in_use`` (``POST /api/mock/games {"locked": [...]}``)
        self.locked: set[str] = set()
        #: ``() -> bool``: the archive export / import runs (the server wires it): ``POST /api/games/import`` answers 409 ``busy``
        self.busy_hook: Callable[[], bool] = lambda: False
        self.importing = False

    def sample(self) -> list[dict[str, Any]]:
        def when(days: float) -> str:
            return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        base = {"title": "Arena-Agents", "scenario_name": mb.DEFAULT_SCENARIO_ID, "scenario_title": "狼人杀 12 人局（模拟数据）", "players": 12, "readable": True}
        brief = list(self.names)
        return [
            {**base, "session_id": "mock-old-1", "status": "finished", "winner": "好人阵营", "created_at": when(1.0), "started_at": when(1.0), "finished_at": when(0.98),
             "updated_at": when(0.98), "duration_gt": 1523.0, "tokens": 1250000, "cost_usd": 0.52, "human_involved": False, "schema_version": 6, "size_bytes": 91000,
             "log_bytes": 2_400_000, "review": {"done": 5, "total": 12}, "participants_brief": brief},
            {**base, "session_id": "mock-old-2", "status": "paused", "winner": None, "created_at": when(2.0), "started_at": when(2.0), "finished_at": None,
             "updated_at": when(1.9), "duration_gt": 600.0, "tokens": 320000, "cost_usd": 0.11, "human_involved": True, "schema_version": 6, "size_bytes": 40000,
             "log_bytes": 900_000, "review": {"done": 0, "total": None}, "participants_brief": brief},
            {**base, "session_id": "mock-old-3", "status": "finished", "winner": "狼人阵营", "created_at": when(5.0), "started_at": when(5.0), "finished_at": when(4.95),
             "updated_at": when(4.95), "duration_gt": 2210.0, "tokens": 2410000, "cost_usd": 0.97, "human_involved": False, "schema_version": 5, "size_bytes": 120000,
             "log_bytes": 3_100_000, "review": {"done": 0, "total": None}, "participants_brief": brief},
            {**base, "session_id": "mock-old-4", "status": "finished", "winner": "好人阵营", "created_at": when(7.0), "started_at": when(7.0), "finished_at": when(6.96),
             "updated_at": when(6.9), "duration_gt": 1810.0, "tokens": 1900000, "cost_usd": 0.74, "human_involved": True, "schema_version": 6, "size_bytes": 101000,
             "log_bytes": 2_800_000, "review": {"done": 12, "total": 12}, "participants_brief": brief},
            {"session_id": "mock-broken-5", "title": "", "scenario_name": "", "scenario_title": "", "status": "unreadable", "winner": None, "created_at": None, "started_at": None,
             "finished_at": None, "updated_at": when(9.0), "duration_gt": 0.0, "players": 0, "tokens": 0, "cost_usd": 0.0, "human_involved": False, "schema_version": None,
             "size_bytes": 312, "readable": False, "log_bytes": 0, "review": {"done": 0, "total": None}, "participants_brief": []},
        ]

    # ------------------------------------------------------------------------------------ queries
    @staticmethod
    def checked(game_id: str) -> str:
        if not MockGames.ID_RE.fullmatch(game_id or ""):
            raise Refusal(400, "无效的对局 id。")
        return game_id

    def find(self, session_id: str) -> dict[str, Any] | None:
        return next((g for g in self.items if g["session_id"] == session_id), None)

    def live_item(self, game: Any) -> dict[str, Any]:
        s = game.session
        finished = s["status"] == "finished"
        done = list(s["review"]["done"])
        return {
            "session_id": s["session_id"], "title": s["title"], "scenario_name": s["scenario_name"], "scenario_title": s["scenario_title"], "status": s["status"],
            "winner": s["winner"], "created_at": datetime.now(timezone.utc).isoformat(), "started_at": None, "finished_at": None,
            "updated_at": datetime.now(timezone.utc).isoformat(), "duration_gt": round(float(s["clock"]), 1), "players": len(s["participants"]),
            "tokens": int((s.get("usage_stats") or {}).get("total_tokens") or 0), "cost_usd": float((s.get("usage_stats") or {}).get("total_cost_usd") or 0.0),
            "human_involved": bool(game.takeover_seat is not None or getattr(game, "human_seats", None)), "schema_version": 6,
            "size_bytes": 12_000 + 90 * len(s["messages"]), "log_bytes": 40_000 * (1 + len(s["messages"]) // 20), "readable": True,
            "review": {"done": len(done), "total": len(s["participants"]) if finished else None}, "participants_brief": [p["name"] for p in s["participants"]],
        }

    def listing(self, game: Any) -> dict[str, Any]:
        live = self.live_item(game)
        saved = [g for g in self.items if g["session_id"] != live["session_id"]]
        return {"games": [{**live, "current": True}, *({**g, "current": False} for g in saved)], "current": live["session_id"]}

    def detail(self, game: Any, session_id: str) -> dict[str, Any]:
        """``GET /api/games/{id}/detail``: who sat where (with the identity once the game is over), the files, the stretches a person played."""
        self.checked(session_id)
        live = game.session
        if session_id == live["session_id"]:
            item = self.live_item(game)
            people = [
                {"seat": p["id"], "bot_id": p.get("bot_id") or "", "name": p["name"], "avatar": copy.deepcopy(p["avatar"])} for p in live["participants"]
            ]
            done = set(live["review"]["done"])
            human = sorted(getattr(game, "human_seats", set()))
        else:
            item = self.find(session_id)
            if item is None:
                raise Refusal(404, "对局不存在。", "not_found")
            if not item.get("readable", True):
                raise Refusal(422, UNREADABLE_SAVE)  # (the real server: StorageError -> 422, no code)
            people = [
                {"seat": f"N{i + 1}", "bot_id": "" if item["schema_version"] == 5 else "b-" + format(abs(hash((session_id, name))) % 16**8, "08x"), "name": name, "avatar": None}
                for i, name in enumerate(item["participants_brief"])
            ]
            reviewed = int(item["review"]["done"])
            done = {f"N{i + 1}" for i in range(reviewed)}
            human = ["N3"] if item["human_involved"] else []
        finished = item["status"] == "finished"
        supported = item["schema_version"] != 5
        rows = []
        for person in people:
            role = ROLE_OF_SEAT.get(person["seat"]) if finished else None
            # (the real server: ``done`` when it is written, ``na`` for a game that cannot be reviewed - a v5 game, a seat without a bot -, else ``pending``:
            # also while the game is not over)
            review = "done" if person["seat"] in done else ("na" if not (supported and person.get("bot_id")) else "pending")
            rows.append({**person, "role": role, "faction": faction_of(role) if role else None, "review": review})
        return {
            "participants": rows,
            "files": {"game_bytes": item["size_bytes"], "prev_bytes": 0 if item["status"] == "finished" else item["size_bytes"] // 2, "log_bytes": item["log_bytes"]},
            "human_spans": [{"seat": seat, "from_seq": 40, "to_seq": 218} for seat in human],
        }

    def is_current(self, game: Any, session_id: str) -> bool:
        return session_id == game.session["session_id"]

    def delete(self, game: Any, session_id: str) -> dict[str, Any]:
        """Delete a game of the list: ``{"ok", "session_id", "note", "new_game", "new_session_id"}``.  The CURRENT game is one too (v0.14, lead's decision):
        everything but a running game can be deleted, and then a new empty game is made (``new_game``: the ROUTE does that and fills ``new_session_id``); a
        running one answers 409.  A game whose files are in use (``locked``) answers 409 ``in_use`` (for the current game the new empty game exists
        already: ``new_game`` says so - the route raises that after it made it, see ``in_use``)."""
        self.checked(session_id)
        if self.is_current(game, session_id):
            if game.session["status"] == "running":
                raise Refusal(409, "进行中的对局不能删除，请先暂停。")
            return {"ok": True, "session_id": session_id, "note": GAME_NOTE, "new_game": True, "new_session_id": None, "in_use": session_id in self.locked}
        if self.find(session_id) is None:
            raise Refusal(404, "对局不存在。", "not_found")
        if session_id in self.locked:
            raise Refusal(409, "这局的文件现在被占用，暂时删不掉：请稍后再试。", "in_use", new_game=False, new_session_id=None)
        self.items = [g for g in self.items if g["session_id"] != session_id]
        return {"ok": True, "session_id": session_id, "note": GAME_NOTE, "new_game": False, "new_session_id": None, "in_use": False}

    # ------------------------------------------------------------------------------------ .arenagame
    def export_name(self, session_id: str) -> str:
        return f"game-{session_id}-{datetime.now().strftime('%Y%m%d')}.arenagame"

    def export_zip(self, game: Any, session_id: str) -> bytes:
        self.checked(session_id)
        current = session_id == game.session["session_id"]
        item = self.live_item(game) if current else self.find(session_id)
        if item is None:
            raise Refusal(404, "对局不存在。", "not_found")
        if current and (game.session["status"] == "running" or game.takeover_seat is not None):
            # (the file holds every identity: the same rule as the replay of the current game)
            raise Refusal(409, "对局进行中或正在接管一个座位，不能导出当前对局：请先暂停并退出接管。")
        if not item.get("readable", True):
            raise Refusal(422, "这局的存档无法读取（格式太旧或已经损坏），不能导出。", "invalid_archive")
        game_json = json.dumps({"session_id": session_id, "schema_version": item["schema_version"], "summary": item}, ensure_ascii=False, indent=1)
        trace = json.dumps({"event": "mock", "session_id": session_id}, ensure_ascii=False) + "\n"
        files = {f"games/{session_id}/game.json": game_json, f"logs/{session_id}/trace-0001.jsonl": trace}
        manifest = {
            "format": "arenagame", "format_version": 1, "session_id": session_id, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "app_version": "v0.14.0-mock", "files": [{"path": p, "size": len(t.encode("utf-8")), "sha256": mb.sha256_text(t)} for p, t in files.items()],
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
            for path, text in files.items():
                z.writestr(path, text)
        return buf.getvalue()

    def import_zip(self, game: Any, data: bytes, name: str, on_conflict: str) -> dict[str, Any]:
        """``POST /api/games/import``: ``{"session_id", "status", "replaced", "copied"}``; 409 ``exists`` when the id is taken and the answer is ``ask``."""
        if on_conflict not in ("ask", "overwrite", "copy"):
            raise Refusal(400, "on_conflict 必须是 ask / overwrite / copy 之一。", "bad_request")
        if self.importing or self.busy_hook():
            raise Refusal(409, "另一个导入 / 导出任务正在进行：请等它结束再导入对局。", "busy")
        bad = Refusal(422, "这不是有效的 .arenagame 文件（不是 zip，或者文件已损坏）。", "invalid_archive")
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise bad from None
        with z:
            try:
                manifest = json.loads(z.read("manifest.json").decode("utf-8"))
            except (KeyError, ValueError, UnicodeDecodeError):
                raise bad from None
            if not isinstance(manifest, dict) or manifest.get("format") != "arenagame":
                raise bad
            session_id = str(manifest.get("session_id") or "")
            if not self.ID_RE.fullmatch(session_id):
                raise Refusal(422, "文件里的对局 id 不合法。", "invalid_archive")
            try:
                payload = json.loads(z.read(f"games/{session_id}/game.json").decode("utf-8"))
            except (KeyError, ValueError, UnicodeDecodeError):
                raise Refusal(422, f"缺少 games/{session_id}/game.json。", "invalid_archive") from None
            for entry in manifest.get("files") or []:
                try:
                    text = z.read(str(entry.get("path"))).decode("utf-8")
                except (KeyError, UnicodeDecodeError):
                    raise Refusal(422, f"清单里的 {str(entry.get('path'))[:40]} 在文件里读不出来。", "invalid_archive") from None
                if mb.sha256_text(text) != entry.get("sha256"):
                    raise Refusal(422, f"{str(entry.get('path'))[:40]} 的 SHA-256 校验值与清单不符：文件已损坏或被改动。", "invalid_archive")
        schema = payload.get("schema_version") if isinstance(payload, dict) else None
        if schema not in (5, 6):
            raise Refusal(422, f"只能导入存档格式 5 或 6 的对局（这个是 {schema}）。", "invalid_archive")
        summary = copy.deepcopy(payload.get("summary") or {})
        exists = session_id == game.session["session_id"] or self.find(session_id) is not None
        replaced = copied = False
        if exists:
            if on_conflict == "ask":
                raise Refusal(409, f"已经有一局 id 相同的对局（{session_id}）：可以覆盖它，或者作为副本导入。", "exists", session_id=session_id)
            if on_conflict == "copy":
                session_id = f"{session_id}-copy" + uuid.uuid4().hex[:4]
                copied = True
            else:
                if session_id == game.session["session_id"]:
                    raise Refusal(409, "当前对局不能被覆盖：请先新建或载入另一局，或者作为副本导入。", "current")
                self.items = [g for g in self.items if g["session_id"] != session_id]
                replaced = True
        summary.update(session_id=session_id, readable=True, schema_version=schema)
        summary.setdefault("status", "finished")
        for key, default in (("title", "Arena-Agents"), ("scenario_name", mb.DEFAULT_SCENARIO_ID), ("scenario_title", "导入的对局"), ("players", 12), ("size_bytes", len(data)),
                             ("log_bytes", 0), ("tokens", 0), ("cost_usd", 0.0), ("duration_gt", 0.0), ("human_involved", False), ("winner", None),
                             ("participants_brief", list(self.names)), ("review", {"done": 0, "total": None})):
            summary.setdefault(key, default)
        summary["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.items.insert(0, summary)
        return {"session_id": session_id, "status": summary["status"], "replaced": replaced, "copied": copied}


# --------------------------------------------------------------------------------------------------
# the hub
# --------------------------------------------------------------------------------------------------


class MockHub:
    """What the orchestrator does with the bots: the payloads of the API, the start check, the status label, the initialization jobs and the
    reviews, and keeping the session and the clients (``roster.updated`` / ``bots.updated`` / ``session.updated``) in step.

    ``game_fn()`` makes / returns the ``MockGame`` (it attaches itself); ``providers_fn()`` the provider list of the mock (a binding's label)."""

    INIT_CONCURRENCY = 4
    REVIEW_CONCURRENCY = 6

    def __init__(self, stores: Stores | None = None, *, games: MockGames | None = None, game_fn: Callable[[], Any] | None = None,
                 providers_fn: Callable[[], list[dict[str, Any]]] | None = None) -> None:
        self.stores = stores or Stores()
        self.games = games or MockGames([self.stores.bots.name_of(i) for i in self.stores.bots.order])
        self.game_fn = game_fn
        self.providers_fn = providers_fn or (lambda: [])
        self._game: Any = None
        self.bot_bindings: dict[str, dict[str, Any]] = {}
        self.init_job: dict[str, Any] | None = None
        self.init_task: asyncio.Task[None] | None = None
        self.init_plan: dict[str, str] = {}  # bot id (or seat / name) -> "failed" | "timeout" | "slow" | "invalid"
        self.review_plan: dict[str, str] = {}  # seat -> "error" | "timeout" | "slow"
        self.sim_rate = 12.0  # simulated seconds per tick (an initialization of 60 s takes 5 ticks)
        self.tick = 1.0  # real seconds per tick at speed 1
        self.review_running: dict[str, dict[str, Any]] = {}  # seat -> {"seconds_left"}
        self.refine_failure: str | None = None  # None | "unbound" | "model" | "format": why "用主持人模型完善" cannot work (a switch for the page's error paths)
        self.tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------------------------ plumbing
    @property
    def bots(self) -> mb.MockBots:
        return self.stores.bots

    @property
    def contract(self) -> mb.MockContract:
        return self.stores.contract

    @property
    def rules(self) -> mb.MockRules:
        return self.stores.rules

    def attach(self, game: Any) -> None:
        self._game = game

    def game(self) -> Any:
        if self._game is None and self.game_fn is not None:
            self.game_fn()
        if self._game is None:
            raise RuntimeError("the hub has no game yet")
        return self._game

    def speed(self) -> float:
        return max(0.01, float(getattr(self._game, "speed", 1.0) or 1.0))

    def sources(self) -> dict[str, str]:
        return self.stores.sources()

    def seat_ids(self) -> list[str]:
        return [f"N{i + 1}" for i in range(len(self.bots.order))]

    def spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    # ------------------------------------------------------------------------------------ the seats of the game
    def seat_name(self, pid: str) -> str:
        bot_id = self.bots.bot_at(pid)
        return self.bots.bots[bot_id]["name"] if bot_id else pid

    def seat_ref(self, pid: str) -> str:
        """What the models (and the moderator's messages) call a player: ``N3 Carol``."""
        bot_id = self.bots.bot_at(pid)
        return f"{pid} {self.bots.bots[bot_id]['name']}" if bot_id else pid

    def seat_avatar(self, pid: str) -> dict[str, Any] | None:
        bot_id = self.bots.bot_at(pid)
        return copy.deepcopy(self.bots.bots[bot_id]["avatar"]) if bot_id else None

    # ------------------------------------------------------------------------------------ model bindings (they follow the bots, H4)
    def load_seat_bindings(self, bindings: dict[str, Any]) -> None:
        """Take the seat-keyed bindings the mock starts with (or a ``PUT /api/settings/bindings`` brings) and keep them by bot id."""
        self.bot_bindings = {}
        for key, binding in (bindings or {}).items():
            if key == MODERATOR:
                self.bot_bindings[MODERATOR] = copy.deepcopy(binding)
            else:
                bot_id = self.bots.bot_at(key)
                if bot_id and isinstance(binding, dict):
                    self.bot_bindings[bot_id] = copy.deepcopy(binding)

    def update_seat_bindings(self, updated: dict[str, Any]) -> None:
        """Some seats (or the moderator) got a binding (a template was applied): the bots in those seats keep it."""
        for key, binding in (updated or {}).items():
            bot_id = MODERATOR if key == MODERATOR else self.bots.bot_at(key)
            if bot_id and isinstance(binding, dict):
                self.bot_bindings[bot_id] = copy.deepcopy(binding)

    def rename_binding(self, old_id: str, new_id: str) -> None:
        """A frozen bot that was renamed has a new id: its default binding goes with it (H4)."""
        if old_id in self.bot_bindings:
            self.bot_bindings[new_id] = self.bot_bindings.pop(old_id)

    def seat_bindings(self) -> dict[str, Any]:
        """``session.model_bindings``: the moderator's and every seat's binding (the bots' defaults, in the seat they sit in now)."""
        out: dict[str, Any] = {}
        if MODERATOR in self.bot_bindings:
            out[MODERATOR] = copy.deepcopy(self.bot_bindings[MODERATOR])
        for pid in self.seat_ids():
            bot_id = self.bots.bot_at(pid)
            if bot_id and bot_id in self.bot_bindings:
                out[pid] = copy.deepcopy(self.bot_bindings[bot_id])
        return out

    def binding_view(self, key: str) -> dict[str, Any]:
        """``BotSummary.binding``: ``{"ok", "label"}`` of the model a bot (or ``moderator``) is bound to."""
        binding = self.bot_bindings.get(key)
        if binding is None:
            return {"ok": False, "label": "未绑定模型"}
        pid, model = binding.get("provider_id", ""), binding.get("model_id", "")
        provider = next((p for p in self.providers_fn() if p.get("id") == pid), None)
        if provider is None:
            return {"ok": False, "label": f"{pid} / {model}（已不存在）"}
        label = f"{provider.get('name') or pid} / {model}"
        if not provider.get("api_key_set"):
            return {"ok": False, "label": f"{label}（未设置 API Key）"}
        return {"ok": True, "label": label}

    # ------------------------------------------------------------------------------------ what is running
    def init_running_ids(self) -> set[str]:
        job = self.init_job
        if not job:
            return set()
        return {i for i, b in job["bots"].items() if b["state"] in ("queued", "running")}

    def review_running_ids(self) -> set[str]:
        return set(self.review_running)

    def init_job_view(self) -> dict[str, Any] | None:
        job = self.init_job
        if not job:
            return None
        return {"job_id": job["job_id"], "state": "running", "ids": list(job["ids"]), "started_at": job["started_at"], "deadline_s": mb.INIT_TIMEOUT_S}

    def review_seconds_left(self) -> int | None:
        if not self.review_running:
            return None
        return int(max(int(v["seconds_left"]) for v in self.review_running.values()))

    # ------------------------------------------------------------------------------------ the start check (H25)
    def check_ready(self) -> list[dict[str, Any]]:
        """What keeps the game from starting: ``[Problem]`` (§13.7); empty when it can.  The contract, the scenario, every player, every model."""
        problems: list[dict[str, Any]] = []
        c = self.contract
        if c.broken == "missing":
            problems.append(problem("contract", "contract_missing", "官方契约是空的，请在设置里导入契约或恢复默认。", "import_contract"))
        elif c.broken == "invalid":
            problems.append(problem("contract", "contract_invalid", "官方契约有问题：契约没有提到这些工具：jot、read_contract。每个工具的名字都要出现在契约里。", "import_contract"))
        else:
            issues = mb.validate_contract(c.markdown)
            if issues:
                problems.append(problem("contract", "contract_invalid", "官方契约有问题：" + " ".join(issues), "import_contract"))
        sid = self.stores.scenario_id()
        broken = self.rules.broken.get(sid)
        if sid not in self.rules.scenarios or broken == "missing":
            problems.append(problem("scenario", "scenario_missing", f"读不到剧本 {sid}：这份剧本不见了。", "import_rules"))
        elif broken == "invalid":
            problems.append(problem("scenario", "scenario_invalid", "剧本有问题：身份池共有 11 个身份，但对局有 12 名玩家，无法发牌。", "import_rules"))
        else:
            try:
                mb.parse_scenario(self.rules.scenario_text(sid))
            except FileProblems as exc:
                problems.append(problem("scenario", "scenario_invalid", str(exc), "import_rules"))
        sources = self.sources()
        running = self.init_running_ids()
        for pid in self.seat_ids():
            bot_id = self.bots.bot_at(pid)
            entry = self.bots.problem_entry(bot_id, sources=sources, running=bot_id in running) if bot_id else None
            if entry:
                entry["seat"] = pid
                problems.append(entry)
        for pid in self.seat_ids():
            bot_id = self.bots.bot_at(pid)
            if bot_id and not self.binding_view(bot_id)["ok"]:
                problems.append(problem("binding", "no_binding", f"{pid} 还没有绑定可用的模型。", "bind_model", bot_id=bot_id, seat=pid, name=self.bots.name_of(bot_id)))
        if not self.binding_view(MODERATOR)["ok"]:
            problems.append(problem("binding", "no_binding", "主持人还没有绑定可用的模型。", "bind_model", name="主持人"))
        return problems

    # ------------------------------------------------------------------------------------ BotsPayload / BotDetail (§13.7)
    def bot_summaries(self) -> list[dict[str, Any]]:
        sources = self.sources()
        running = self.init_running_ids()
        out = []
        for index, bot_id in enumerate(self.bots.order):
            summary = self.bots.summary(bot_id, sources=sources, running=bot_id in running)
            summary["seat"] = f"N{index + 1}"
            summary["binding"] = self.binding_view(bot_id)
            out.append(summary)
        return out

    def bots_payload(self, *, full: bool = False) -> dict[str, Any]:
        problems = self.check_ready()
        payload: dict[str, Any] = {
            "lineup": list(self.bots.order), "bots": self.bot_summaries(), "memory_enabled": bool(self.bots.memory_enabled), "init_job": self.init_job_view(),
            "ready": {"ok": not problems, "problems": problems}, "learning_usage": copy.deepcopy(self.bots.usage),
        }
        if full:
            payload["limits"] = mb.limits_payload()
            payload["defaults"] = self.bots.library()
        return payload

    def bot_detail(self, bot_id: str, scenario_id: str | None = None) -> dict[str, Any]:
        data = self.bots.detail(bot_id, scenario_id or "", sources=self.sources(), running=bot_id in self.init_running_ids())
        data["seat"] = self.bots.seat_of(bot_id)
        data["binding"] = self.binding_view(bot_id)
        return data

    # ------------------------------------------------------------------------------------ PublicSession pieces (§13.8)
    def init_statuses(self) -> dict[str, str]:
        sources = self.sources()
        running = self.init_running_ids()
        return {i: self.bots.summary(i, sources=sources, running=i in running)["init"]["status"] for i in self.bots.order}

    def system_status(self, session: dict[str, Any] | None = None) -> dict[str, Any]:
        """``PublicSession.system_status`` (H26): initializing / reviewing, then in game / paused / finished, then uninitialized, then before the game."""
        s = session if session is not None else self.game().session

        def status(code: str, text: str, reason: str = "") -> dict[str, Any]:
            return {"code": code, "text": text, "reason": reason}

        if self.init_running_ids():
            return status("initializing", "初始化中")
        if self.review_running_ids():
            return status("reviewing", "复盘中")
        if s["status"] == "running":
            return status("in_game", "对局中 · agent 已暂停" if s.get("soft_pause") else "对局中")
        if s["status"] == "paused":
            reason = PAUSE_TEXTS.get(s["pause_reason"], "已暂停")
            if s.get("hold_reason"):
                reason = f"{reason}：{s['hold_reason']}"
            return status("paused", "已暂停", reason)
        if s["status"] == "finished":
            return status("finished", "已结束")
        if any(v != "ready" for v in self.init_statuses().values()):
            return status("uninitialized", "未初始化")
        return status("before_game", "对局前")

    def review_view(self, session: dict[str, Any]) -> dict[str, Any]:
        """``PublicSession.review``: ``{"available", "done", "running", "supported", "deadline_s", "seconds_left"}``."""
        running = bool(self.review_running)
        return {
            "available": session["status"] == "finished", "done": list(session["review"]["done"]), "running": running, "supported": True,
            "deadline_s": mb.REVIEW_TIMEOUT_S, "seconds_left": self.review_seconds_left() if running else None,
        }

    def refresh_status(self, session: dict[str, Any]) -> None:
        """The cheap part of :meth:`sync_session` (every ``session.updated`` carries it): the status label, the review, the memory switch."""
        session["system_status"] = self.system_status(session)
        session["review"] = {**session["review"], **self.review_view(session)}
        session["memory_enabled"] = bool(self.bots.memory_enabled)

    def sync_session(self, session: dict[str, Any]) -> None:
        """Bring the parts of the session that come from the bots up to date: ``bots``, ``system_status``, ``review`` and every participant's
        ``bot_id`` / ``init_status`` (what a snapshot carries and ``bots.updated`` / ``session.updated`` bring)."""
        payload = self.bots_payload()
        session["bots"] = payload
        by_seat = {b["seat"]: b for b in payload["bots"]}
        for p in session["participants"]:
            b = by_seat.get(p["id"])
            p["bot_id"] = b["id"] if b else ""
            p["init_status"] = b["init"]["status"] if b else None
        self.refresh_status(session)

    # ------------------------------------------------------------------------------------ telling the clients
    def publish_bots_updated(self) -> None:
        """``bots.updated`` (§13.8) + the session fields that follow from the bots (``session.updated``: the status label, the memory switch)."""
        game = self.game()
        self.sync_session(game.session)
        game.emit("bots.updated", bots=copy.deepcopy(game.session["bots"]))
        game.publish_session_updated()  # (the status label, the memory switch, the model bindings)

    def apply_table(self) -> None:
        """Take the table as it is now into the game that has not started: names, avatars, bots of the seats, the moderator's avatar, the bindings."""
        game = self.game()
        s = game.session
        for pid in self.seat_ids():
            p = game.player(pid)
            p["name"] = self.seat_name(pid)
            p["avatar"] = self.seat_avatar(pid)
        s["moderator"]["avatar"] = copy.deepcopy(self.bots.moderator.get("avatar"))
        game.bindings = self.seat_bindings()
        s["model_bindings"] = copy.deepcopy(game.bindings)

    def bots_changed(self, *, roster: bool = False) -> None:
        """Something about the bots changed (every write of the API calls this): an idle game takes the new table (``roster``: names, avatars, seats
        or the moderator's avatar changed -> ``roster.updated``), the clients get ``bots.updated``."""
        game = self.game()
        if roster and game.session["status"] == "idle":
            self.apply_table()
            data: dict[str, Any] = {"participants": [{"id": p["id"], "name": p["name"], "avatar": copy.deepcopy(p["avatar"])} for p in game.session["participants"]]}
            data["moderator"] = {"id": MODERATOR, "avatar": copy.deepcopy(game.session["moderator"]["avatar"])}
            game.emit("roster.updated", **data)
        self.publish_bots_updated()

    def sources_changed(self) -> None:
        """The contract or the scenario (or its materials) changed: every initialization may have become ``stale``."""
        self.publish_bots_updated()

    def check_busy(self, ids: list[str] | None) -> None:
        """A bot that is being initialized must not be changed under the job's feet (``None``: any bot)."""
        busy = self.init_running_ids()
        if busy and (ids is None or busy & set(ids)):
            raise Refusal(409, "正在初始化，请等它结束（或先取消）后再修改。", "init_busy")

    def require_idle(self, what: str) -> None:
        if self.game().session["status"] != "idle":
            raise Refusal(409, f"{what}只能在对局开始之前修改；请先新建对局。", "game_active")

    # ------------------------------------------------------------------------------------ initialization (H19-H24)
    def default_init_targets(self) -> list[str]:
        sources = self.sources()
        return [i for i in self.bots.order if self.bots.summary(i, sources=sources)["init"]["status"] in ("uninitialized", "stale", "failed")]

    def start_init(self, ids: Any = None, rebuild: bool = False) -> dict[str, Any]:
        """``POST /api/bots/init`` -> ``{"job_id", "ids", "deadline_s"}``; 409 ``game_active`` / ``init_busy`` / ``no_binding``."""
        s = self.game().session
        if s["status"] not in ("idle", "finished"):
            raise Refusal(409, "对局正在进行（或已暂停），现在不能初始化：请先新建对局。", "game_active")
        if self.init_job is not None:
            raise Refusal(409, "已经有一个初始化任务在运行：请等它结束，或者先取消。", "init_busy")
        if ids is not None:
            if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                raise Refusal(422, "ids 必须是玩家 id 的列表。")
            targets = self.bots._pick(ids)
        elif rebuild:
            targets = list(self.bots.order)
        else:
            targets = self.default_init_targets()
        if not targets:
            raise Refusal(400, "没有需要初始化的玩家。")
        unbound = [i for i in targets if not self.binding_view(i)["ok"]]
        if unbound:
            problems = [
                problem("binding", "no_binding", "还没有给这个玩家绑定可用的模型。", "bind_model", bot_id=i, seat=self.bots.seat_of(i), name=self.bots.name_of(i)) for i in unbound
            ]
            raise Refusal(409, "有玩家还没有绑定模型，不能初始化：" + "、".join(self.bots.name_of(i) for i in unbound) + "。", "no_binding", problems=problems)
        job_id = uuid.uuid4().hex[:12]
        job: dict[str, Any] = {"job_id": job_id, "ids": targets, "started_at": now_unix(), "bots": {}}
        for i in targets:
            job["bots"][i] = {"state": "queued", "elapsed": 0.0, "total": self.plan_total(i), "files": [], "error": None, "plan": self.plan_of(i)}
        self.init_job = job
        self.publish_bots_updated()
        for i in targets:
            self.emit_init(job, i)
        self.init_task = self.spawn(self.run_init(job))
        return {"job_id": job_id, "ids": list(targets), "deadline_s": mb.INIT_TIMEOUT_S}

    def plan_of(self, bot_id: str) -> str:
        """What the mock makes of this bot's initialization: ``done`` unless ``init_plan`` says otherwise (by bot id, seat or name)."""
        keys = [bot_id, self.bots.seat_of(bot_id) or "", self.bots.name_of(bot_id)]
        for key in keys:
            if key in self.init_plan:
                return self.init_plan[key]
        core = self.bots.bots[bot_id]["core"]
        if not (core["identity"] and core["voice"] and core["mind"]):
            return "nocore"
        return "done"

    def plan_total(self, bot_id: str) -> float:
        """Simulated seconds an initialization takes (40-100 s, a bot's own); ``slow`` takes 110 s, ``timeout`` never finishes."""
        plan = self.plan_of(bot_id)
        if plan == "timeout":
            return 10_000.0
        if plan == "slow":
            return 110.0
        return 40.0 + (sum(ord(c) for c in bot_id) % 60)

    @staticmethod
    def seconds_left(state: str, elapsed: float, deadline: float) -> int | None:
        """What is left of the 120 s (the 60 s of a review): nothing is counted while queued or after a cancel; a timeout has none left."""
        if state in ("queued", "cancelled"):
            return None
        return 0 if state == "timeout" else int(max(0.0, deadline - elapsed))

    def emit_init(self, job: dict[str, Any], bot_id: str) -> None:
        b = job["bots"][bot_id]
        seconds_left = self.seconds_left(b["state"], b["elapsed"], float(mb.INIT_TIMEOUT_S))
        self.game().emit(
            "bot.init.progress", job_id=job["job_id"], bot_id=bot_id, name=self.bots.name_of(bot_id) if bot_id in self.bots.bots else "", state=b["state"],
            seconds_left=seconds_left, deadline_s=mb.INIT_TIMEOUT_S, files_written=list(b["files"]), error=b["error"],
        )

    def init_files(self, bot_id: str) -> dict[str, str]:
        """What the bot 'wrote' (the mock's own words); an old memory is revised, not thrown away."""
        bot = self.bots.bots[bot_id]
        sid = self.sources()["scenario_id"]
        old = self.bots._rules(bot, sid)
        old_entries = mb.parse_playbook(old["playbook"])
        fresh = [mb.make_entry(t, c, sup, k, "初始化", tg) for (t, c, sup, k, _src, tg) in mb.DEFAULT_PLAYBOOK]
        known = {e["text"] for e in old_entries}
        merged = old_entries + [e for e in fresh if e["text"] not in known]
        note = "（根据新的契约和剧本修订过。）"
        notes = bot["mem"]["arch_notes"]
        return {
            "arch_notes": (notes if notes.endswith(note) else (notes + note)[: mb.ARCH_NOTES_CHARS]) if notes else mb.DEFAULT_ARCH_NOTES,
            "digest": old["digest"] or mb.DEFAULT_DIGEST,
            "playbook": mb.render_playbook(merged[: mb.PLAYBOOK_MAX]).strip(),
            "questions": old["questions"] or mb.DEFAULT_QUESTIONS,
        }

    async def run_init(self, job: dict[str, Any]) -> None:
        """Play the job: bots run in the order of the line-up, ``INIT_CONCURRENCY`` at a time; one ``bot.init.progress`` per bot per tick."""
        deadline = float(mb.INIT_TIMEOUT_S)
        try:
            while True:
                running = [i for i in job["ids"] if job["bots"][i]["state"] == "running"]
                queued = [i for i in job["ids"] if job["bots"][i]["state"] == "queued"]
                for i in queued[: max(0, self.INIT_CONCURRENCY - len(running))]:
                    job["bots"][i]["state"] = "running"
                    running.append(i)
                    self.emit_init(job, i)
                if not running and not queued:
                    break
                await asyncio.sleep(self.tick / self.speed())
                changed = False
                for i in running:
                    b = job["bots"][i]
                    b["elapsed"] += self.sim_rate
                    share = min(1.0, b["elapsed"] / b["total"]) if b["total"] else 1.0
                    b["files"] = [name for name, at in (("arch_notes", 0.3), ("digest", 0.55), ("playbook", 0.8), ("questions", 0.92)) if share >= at and b["plan"] != "nocore"]
                    if b["plan"] == "nocore":
                        b.update(state="failed", error="底层设定不完整：缺少「身份」「说话方式」「分析方式」，请先补全底层设定（原来的文件没有改动）。", files=[])
                        self.bots.fail_init(i, b["error"], model=self.model_of(i), duration_s=b["elapsed"])
                    elif b["plan"] == "failed" and share >= 0.5:
                        b.update(state="failed", error="模型调用失败：HTTP 429 Too Many Requests（模拟）；原来的文件没有改动。", files=[])
                        self.bots.fail_init(i, b["error"], model=self.model_of(i), duration_s=b["elapsed"])
                    elif b["plan"] == "invalid" and share >= 0.9:
                        b.update(state="failed", error="缺少必需的记忆文件：打法（没有写 playbook）；原来的文件没有改动。", files=["arch_notes", "digest"])
                        self.bots.fail_init(i, b["error"], model=self.model_of(i), duration_s=b["elapsed"])
                    elif b["elapsed"] >= deadline and b["elapsed"] < b["total"]:
                        b.update(state="timeout", error=f"超过 {int(deadline)} 秒还没有完成，已经停止；原来的文件没有改动。", files=[])
                        self.bots.fail_init(i, b["error"], model=self.model_of(i), duration_s=deadline)
                    elif b["elapsed"] >= b["total"]:
                        b.update(state="done", files=["arch_notes", "digest", "playbook", "questions"])
                        self.bots.commit_init(i, self.sources()["scenario_id"], self.init_files(i), model=self.model_of(i), duration_s=b["elapsed"], sources=self.sources())
                        self.account_init(i, b["elapsed"])
                    if b["state"] != "running":
                        changed = True
                    self.emit_init(job, i)
                if changed:
                    self.publish_bots_updated()
        except asyncio.CancelledError:
            for i in job["ids"]:
                b = job["bots"][i]
                if b["state"] in ("queued", "running"):
                    b.update(state="cancelled", error=None, files=[])
                    self.emit_init(job, i)
            raise
        finally:
            if self.init_job is job:
                self.init_job = None
                self.init_task = None
                self.publish_bots_updated()

    def model_of(self, bot_id: str) -> str:
        return (self.bot_bindings.get(bot_id) or {}).get("model_id") or "demo"

    def account_init(self, bot_id: str, seconds: float) -> None:
        """An initialization costs a few calls (H24): the learning use of the bots, a line of the error log (``purpose = init``)."""
        calls = 3 + (sum(ord(c) for c in bot_id) % 4)
        tokens = 5200 * calls
        self.bots.usage["calls"] += calls
        self.bots.usage["tokens"] += tokens
        self.bots.usage["cost_usd"] = round(self.bots.usage["cost_usd"] + tokens * 4e-7, 6)
        game = self.game()
        binding = self.bot_bindings.get(bot_id) or {}
        game.log_call(
            bot_id, purpose="init", status="ok", attempt=1, elapsed_ms=int(seconds * 90), prompt_tokens=tokens - 900, completion_tokens=900, cache_read_tokens=0,
            cache_write_tokens=0, finish_reason="stop", provider_id=binding.get("provider_id"), model_id=binding.get("model_id"),
        )

    async def cancel_init(self) -> dict[str, Any]:
        task = self.init_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif self.init_job is not None:
            self.init_job = None
            self.publish_bots_updated()
        return {"ok": True}

    # ------------------------------------------------------------------------------------ the review (H31-H34)
    def review_total(self, seat: str) -> float:
        plan = self.review_plan.get(seat)
        if plan == "timeout":
            return 10_000.0
        if plan == "slow":
            return 55.0
        return 18.0 + (int(seat[1:]) * 7) % 30

    def review_changes(self, game: Any, seat: str, *, partial: bool, learn_style: bool) -> dict[str, Any]:
        """What a review writes into the memory of the bot in ``seat`` (directly: the user's edit and a review's write are the same thing) and the
        proposals it makes; ``changes`` = ``{"files": [{"file", "before", "after"}], "proposals": n}``."""
        s = game.session
        bot_id = self.bots.bot_at(seat)
        bot = self.bots.bots[bot_id]
        sid = self.sources()["scenario_id"]
        files: list[dict[str, str]] = []

        def write(part: str, text: str) -> None:
            before = self.bots.read_part(bot_id, part, sid)
            ok, _msg = self.bots.write_part(bot_id, part, text, sid)
            after = self.bots.read_part(bot_id, part, sid)
            if ok and before != after:
                files.append({"file": part, "before": before, "after": after})

        # the impressions of the others at the table (names are the keys; no identity conclusions: the identities are dealt anew every game)
        people = mb.parse_people(bot["mem"]["people"])
        for other_pid in self.seat_ids():
            if other_pid == seat:
                continue
            name = self.seat_name(other_pid)
            keep = bool(people.get(name)) and (int(other_pid[1:]) + int(seat[1:])) % 3 == 0
            if not keep:
                people[name] = IMPRESSION_BANK[(int(other_pid[1:]) * 7 + int(seat[1:]) * 3) % len(IMPRESSION_BANK)]
        write("people", mb.render_people(people))
        proposals = 0
        if not partial:
            entries = mb.parse_playbook(self.bots.read_part(bot_id, "playbook", sid))
            stamp = datetime.now().strftime("%Y-%m-%d")
            if len(entries) < mb.PLAYBOOK_MAX:
                entries.append(mb.make_entry("投票前先对一遍谁改过口，改口的人比说得漂亮的人更值得追问。", "低", 1, 0, f"复盘 {stamp}", ("投票",)))
            write("playbook", mb.render_playbook(entries))
            write("questions", REVIEW_QUESTIONS[int(seat[1:]) % len(REVIEW_QUESTIONS)])
            role = ROLE_OF_SEAT.get(seat, "平民")
            won = (s["winner"] or "好人阵营") == faction_of(role)
            self.bots.add_record(bot_id, role, won)
            if learn_style and seat in getattr(game, "human_seats", set()):
                voice = bot["core"]["voice"]
                new_voice = voice + "\n「先说结论，再补一句理由。」（从接管者的发言里学来的说法）"
                if len(new_voice) <= mb.CORE_LIMITS["voice"]:
                    self.bots.stage_core(bot_id, "voice", new_voice, "接管者说话短、先说结论；可以试试。", f"复盘 {s['session_id']}")
                    proposals = 1
        return {"files": files, "proposals": proposals}

    async def run_review(self, game: Any, seats: list[str], learn_style: dict[str, bool] | None = None) -> None:
        """Review these seats in parallel (``REVIEW_CONCURRENCY`` at a time): each gets 60 s; at the deadline what was written stays."""
        s = game.session
        learn_style = learn_style or {}
        deadline = float(mb.REVIEW_TIMEOUT_S)
        state = {seat: {"state": "queued", "elapsed": 0.0, "total": self.review_total(seat)} for seat in seats}
        self.review_running = {seat: {"seconds_left": int(deadline)} for seat in seats}
        s["review"]["running"] = True

        def emit(seat: str, message: str, changes: dict[str, Any] | None = None, *, with_changes: bool = False) -> None:
            st = state[seat]
            data: dict[str, Any] = {
                "id": seat, "bot_id": self.bots.bot_at(seat) or "", "name": self.seat_name(seat), "state": st["state"], "message": message,
                "seconds_left": self.seconds_left(st["state"], st["elapsed"], deadline), "deadline_s": mb.REVIEW_TIMEOUT_S, "changes": changes if with_changes else None,
            }
            game.emit("review.progress", **data)

        self.sync_session(s)
        game.publish_session_updated()
        for seat in seats:
            emit(seat, "排队中…")
        try:
            while any(v["state"] in ("queued", "running") for v in state.values()):
                running = [x for x in seats if state[x]["state"] == "running"]
                for seat in [x for x in seats if state[x]["state"] == "queued"][: max(0, self.REVIEW_CONCURRENCY - len(running))]:
                    state[seat]["state"] = "running"
                    running.append(seat)
                    emit(seat, "正在复盘…")
                await asyncio.sleep(self.tick / self.speed())
                for seat in running:
                    st = state[seat]
                    st["elapsed"] += self.sim_rate
                    plan = self.review_plan.get(seat)
                    if plan == "error" and st["elapsed"] >= st["total"] * 0.5:
                        st["state"] = "error"
                        emit(seat, "模型调用失败：HTTP 500（模拟）；没有写入任何内容。")
                    elif st["elapsed"] >= deadline and st["elapsed"] < st["total"]:
                        st["state"] = "timeout"
                        changes = self.review_changes(game, seat, partial=True, learn_style=bool(learn_style.get(seat)))
                        self.mark_reviewed(game, seat, timeout=True)
                        emit(seat, f"到了 {int(deadline)} 秒，已经停止；已经写入的部分保留。", changes, with_changes=True)
                    elif st["elapsed"] >= st["total"]:
                        st["state"] = "done"
                        changes = self.review_changes(game, seat, partial=False, learn_style=bool(learn_style.get(seat)))
                        self.mark_reviewed(game, seat)
                        emit(seat, "已更新印象、打法和战绩", changes, with_changes=True)
                    else:
                        emit(seat, "正在复盘…")
                    if st["state"] != "running":
                        self.review_running.pop(seat, None)
                    else:
                        self.review_running[seat] = {"seconds_left": int(max(0, deadline - st["elapsed"]))}
                    s["review"]["seconds_left"] = self.review_seconds_left()
        finally:
            self.review_running = {}
            s["review"]["running"] = False
            s["review"]["seconds_left"] = None
            self.publish_bots_updated()

    def mark_reviewed(self, game: Any, seat: str, *, timeout: bool = False) -> None:
        """Everybody is reviewed once per game (``review.done``); the call is a line of the error log (``purpose = review``)."""
        s = game.session
        game.reviewed[seat] = True
        s["review"]["done"] = [p for p in self.seat_ids() if game.reviewed.get(p)]
        binding = s["model_bindings"].get(seat, {})
        game.log_call(
            seat, purpose="review", status="timeout" if timeout else "ok", attempt=1, elapsed_ms=60000 if timeout else 4200, prompt_tokens=2600, completion_tokens=420,
            cache_read_tokens=0, cache_write_tokens=0, finish_reason=None if timeout else "stop", provider_id=binding.get("provider_id"), model_id=binding.get("model_id"),
        )


# --------------------------------------------------------------------------------------------------
# validators of the new events (docs/v0.12-contracts.md §13.8); mock_ui_server.validate_event calls them
# --------------------------------------------------------------------------------------------------


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_summary(b: Any, where: str) -> list[str]:
    """One ``BotSummary`` (§13.7 + §13.13)."""
    errs: list[str] = []
    if not isinstance(b, dict):
        return [f"{where}: not an object"]
    for k in ("id", "name", "origin"):
        if not isinstance(b.get(k), str) or not b[k]:
            errs.append(f"{where}.{k}")
    if b.get("avatar") is not None and not isinstance(b["avatar"], dict):
        errs.append(f"{where}.avatar")
    if b.get("avatar_code") is not None and not isinstance(b["avatar_code"], str):
        errs.append(f"{where}.avatar_code")
    if b.get("seat") is not None and not isinstance(b["seat"], str):
        errs.append(f"{where}.seat")
    core = b.get("core")
    if not (isinstance(core, dict) and isinstance(core.get("complete"), bool) and isinstance(core.get("missing"), list) and isinstance(core.get("chars"), dict)
            and isinstance(core.get("extras"), list) and (core.get("updated_at") is None or _is_int(core["updated_at"]))):
        errs.append(f"{where}.core")
    init = b.get("init")
    if not (isinstance(init, dict) and init.get("status") in ("uninitialized", "initializing", "ready", "stale", "failed") and isinstance(init.get("stale"), list)
            and set(init) >= {"at", "model", "duration_s", "error", "stale"}):
        errs.append(f"{where}.init")
    if not isinstance(b.get("frozen"), bool):
        errs.append(f"{where}.frozen")
    stats = b.get("memory_stats")
    if not (isinstance(stats, dict) and all(_is_int(stats.get(k)) for k in ("arch_notes", "digest", "playbook", "questions", "people")) and isinstance(stats.get("record"), str)):
        errs.append(f"{where}.memory_stats")
    if not _is_int(b.get("staging")):
        errs.append(f"{where}.staging")
    binding = b.get("binding")
    if not (isinstance(binding, dict) and isinstance(binding.get("ok"), bool) and isinstance(binding.get("label"), str)):
        errs.append(f"{where}.binding")
    return errs


def validate_problem(p: Any, where: str) -> list[str]:
    errs: list[str] = []
    if not isinstance(p, dict):
        return [f"{where}: not an object"]
    if p.get("scope") not in ("bot", "contract", "scenario", "binding", "game"):
        errs.append(f"{where}.scope")
    for k in ("bot_id", "seat", "name"):
        if p.get(k) is not None and not isinstance(p[k], str):
            errs.append(f"{where}.{k}")
    items = p.get("items")
    if not (isinstance(items, list) and items):
        errs.append(f"{where}.items")
    else:
        for i, it in enumerate(items):
            codes = {"core_missing", "core_incomplete", "not_initialized", "stale", "init_failed", "init_running", "no_binding", "contract_missing", "contract_invalid",
                     "scenario_missing", "scenario_invalid"}
            fixes = {"edit_core", "import_core", "init", "bind_model", "import_contract", "import_rules", ""}
            if not (isinstance(it, dict) and it.get("code") in codes and isinstance(it.get("message"), str) and it.get("fix") in fixes):
                errs.append(f"{where}.items[{i}]")
    return errs


def validate_bots_payload(d: Any, where: str = "bots", *, full: bool = False) -> list[str]:
    """``BotsPayload`` (the live part; ``full`` also checks ``limits`` and ``defaults``)."""
    errs: list[str] = []
    if not isinstance(d, dict):
        return [f"{where}: not an object"]
    lineup = d.get("lineup")
    if not (isinstance(lineup, list) and all(isinstance(i, str) for i in lineup)):
        errs.append(f"{where}.lineup")
    bots = d.get("bots")
    if not isinstance(bots, list):
        errs.append(f"{where}.bots")
    else:
        for i, b in enumerate(bots):
            errs += validate_summary(b, f"{where}.bots[{i}]")
        if isinstance(lineup, list) and [b.get("id") for b in bots if isinstance(b, dict)] != lineup:
            errs.append(f"{where}.bots: not in the order of the lineup")
    if not isinstance(d.get("memory_enabled"), bool):
        errs.append(f"{where}.memory_enabled")
    job = d.get("init_job")
    if job is not None and not (isinstance(job, dict) and job.get("state") == "running" and isinstance(job.get("job_id"), str) and isinstance(job.get("ids"), list)
                                and _is_int(job.get("started_at")) and _is_int(job.get("deadline_s"))):
        errs.append(f"{where}.init_job")
    ready = d.get("ready")
    if not (isinstance(ready, dict) and isinstance(ready.get("ok"), bool) and isinstance(ready.get("problems"), list)):
        errs.append(f"{where}.ready")
    else:
        for i, p in enumerate(ready["problems"]):
            errs += validate_problem(p, f"{where}.ready.problems[{i}]")
        if ready["ok"] == bool(ready["problems"]):
            errs.append(f"{where}.ready: ok must be true exactly when there are no problems")
    usage = d.get("learning_usage")
    if not (isinstance(usage, dict) and _is_int(usage.get("calls")) and _is_int(usage.get("tokens")) and isinstance(usage.get("cost_usd"), (int, float))):
        errs.append(f"{where}.learning_usage")
    for k in ("limits", "defaults"):
        if (k in d) != full:
            errs.append(f"{where}.{k}: only the full payload (GET /api/bots) has it")
    return errs


def validate_init_progress(d: dict[str, Any], w: str = "bot.init.progress") -> list[str]:
    errs: list[str] = []
    keys = {"job_id", "bot_id", "name", "state", "seconds_left", "deadline_s", "files_written", "error"}
    if set(d) != keys:
        errs.append(f"{w}: keys {sorted(set(d) ^ keys)}")
    for k in ("job_id", "bot_id", "name"):
        if not isinstance(d.get(k), str):
            errs.append(f"{w}.{k}")
    if d.get("state") not in INIT_STATES:
        errs.append(f"{w}.state")
    if d.get("seconds_left") is not None and not _is_int(d["seconds_left"]):
        errs.append(f"{w}.seconds_left")
    if d.get("deadline_s") != mb.INIT_TIMEOUT_S:
        errs.append(f"{w}.deadline_s")
    if not (isinstance(d.get("files_written"), list) and all(f in mb.MEMORY_PARTS for f in d["files_written"])):
        errs.append(f"{w}.files_written")
    if d.get("error") is not None and not isinstance(d["error"], str):
        errs.append(f"{w}.error")
    if d.get("state") in ("failed", "timeout") and not d.get("error"):
        errs.append(f"{w}: a failure says why")
    return errs


def validate_review_progress(d: dict[str, Any], w: str = "review.progress") -> list[str]:
    errs: list[str] = []
    keys = {"id", "bot_id", "name", "state", "message", "seconds_left", "deadline_s", "changes"}
    if set(d) != keys:
        errs.append(f"{w}: keys {sorted(set(d) ^ keys)}")
    for k in ("id", "bot_id", "name", "message"):
        if not isinstance(d.get(k), str):
            errs.append(f"{w}.{k}")
    if d.get("state") not in REVIEW_STATES:
        errs.append(f"{w}.state")
    if d.get("seconds_left") is not None and not _is_int(d["seconds_left"]):
        errs.append(f"{w}.seconds_left")
    if d.get("deadline_s") != mb.REVIEW_TIMEOUT_S:
        errs.append(f"{w}.deadline_s")
    ch = d.get("changes")
    if d.get("state") in ("done", "timeout"):
        if not (isinstance(ch, dict) and set(ch) == {"files", "proposals"} and _is_int(ch["proposals"]) and isinstance(ch["files"], list)
                and all(isinstance(f, dict) and set(f) == {"file", "before", "after"} and f["file"] in mb.MEMORY_PARTS and isinstance(f["before"], str) and isinstance(f["after"], str)
                        for f in ch["files"])):
            errs.append(f"{w}.changes")
    elif ch is not None:
        errs.append(f"{w}.changes: only a finished review has them")
    return errs


def validate_system_status(v: Any, where: str = "system_status") -> list[str]:
    codes = {"uninitialized": "未初始化", "initializing": "初始化中", "before_game": "对局前", "in_game": "对局中", "paused": "已暂停", "finished": "已结束", "reviewing": "复盘中"}
    if not (isinstance(v, dict) and set(v) == {"code", "text", "reason"} and v.get("code") in codes and isinstance(v.get("text"), str) and isinstance(v.get("reason"), str)):
        return [where]
    if v["code"] == "in_game":
        return [] if v["text"] in ("对局中", "对局中 · agent 已暂停") else [f"{where}.text"]
    return [] if v["text"] == codes[v["code"]] else [f"{where}.text"]


def validate_review_view(r: Any, where: str = "review") -> list[str]:
    """``PublicSession.review`` (§13.8)."""
    if not (isinstance(r, dict) and isinstance(r.get("available"), bool) and isinstance(r.get("running"), bool) and isinstance(r.get("done"), list)
            and all(isinstance(i, str) for i in r["done"]) and isinstance(r.get("supported"), bool) and r.get("deadline_s") == mb.REVIEW_TIMEOUT_S
            and (r.get("seconds_left") is None or _is_int(r["seconds_left"]))):
        return [where]
    return []


# --------------------------------------------------------------------------------------------------
# the HTTP API
# --------------------------------------------------------------------------------------------------


def content_disposition(filename: str) -> str:
    """``attachment`` with the file name in ASCII (old clients) and in RFC 5987 form (Chinese names, H42)."""
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", filename).replace('"', "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def register_routes(app: Any, hub: MockHub, ensure_game: Callable[[], Any]) -> None:
    """Every route of the bots / contract / rules / games parts of §13.7 (and ``/api/mock/*`` switches for them).  ``ensure_game()`` makes the mock's game
    (created lazily: it needs a running loop)."""
    from fastapi import Request
    from fastapi.responses import JSONResponse, Response

    bots, contract, rules, games = hub.bots, hub.contract, hub.rules, hub.games

    def guarded(fn: Any) -> Any:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            ensure_game()
            try:
                return await fn(*args, **kwargs)
            except Refusal as exc:
                return JSONResponse(status_code=exc.status, content=exc.body)
            except FileProblems as exc:
                return JSONResponse(status_code=400, content={"detail": str(exc), "problems": exc.problems})

        return wrapper

    def body_of(body: Any, allowed: set[str], *, required: set[str] = frozenset()) -> dict[str, Any]:
        """A request body: an object with only the fields ``allowed`` (422 otherwise, like the real server's strict models)."""
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise Refusal(422, "请求内容必须是一个对象。")
        extra = set(body) - allowed
        if extra:
            raise Refusal(422, "不认识的字段：" + "、".join(sorted(extra)))
        for key in required:
            if key not in body:
                raise Refusal(422, f"缺少字段：{key}")
        return body

    def scenario_of(explicit: Any, default: str | None = None) -> str:
        sid = str(explicit or "").strip() or (default if default is not None else hub.sources()["scenario_id"])
        if not mb.SCENARIO_ID_RE.match(sid):
            raise Refusal(400, "剧本 id 只能由字母、数字、下划线和连字符组成。")
        return sid

    async def read_body(request: Request, limit: int, what: str, shown: str = "20 MiB") -> bytes:
        """The body of an upload, at most ``limit`` bytes; ``shown`` is the limit as the real server words it (a test may lower ``limit``)."""
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise Refusal(413, f"{what}超过了 {shown} 的上限。", "too_large")
        data = await request.body()
        if len(data) > limit:
            raise Refusal(413, f"{what}超过了 {shown} 的上限。", "too_large")
        if not data:
            raise Refusal(400, "没有收到文件内容。")
        return data

    def clean_ids(body: dict[str, Any]) -> list[str] | None:
        ids = body.get("ids")
        if ids is None:
            return None
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise Refusal(422, "ids 必须是玩家 id 的列表。")
        return ids

    # ---------------------------------------------------------------------------------------- the line-up and the whole table
    @app.get("/api/bots")
    @guarded
    async def get_bots() -> Any:
        return hub.bots_payload(full=True)

    @app.put("/api/bots/lineup")
    @guarded
    async def put_lineup(body: dict[str, Any]) -> Any:
        body_of(body, {"order"}, required={"order"})
        hub.require_idle("阵容")
        bots.set_lineup(body["order"])
        hub.bots_changed(roster=True)
        return hub.bots_payload()

    @app.post("/api/bots/init")
    @guarded
    async def post_init(body: dict[str, Any] | None = None) -> Any:
        body = body_of(body, {"ids", "rebuild"})
        if "rebuild" in body and not isinstance(body["rebuild"], bool):
            raise Refusal(422, "rebuild 必须是 true 或 false。")
        result = hub.start_init(clean_ids(body), bool(body.get("rebuild")))
        return JSONResponse(status_code=202, content=result)

    @app.post("/api/bots/init:cancel")
    @guarded
    async def post_init_cancel() -> Any:
        return await hub.cancel_init()

    @app.post("/api/bots/core:clear")
    @guarded
    async def post_core_clear(body: dict[str, Any] | None = None) -> Any:
        ids = clean_ids(body_of(body, {"ids"}))
        hub.check_busy(ids)
        bots.clear_core(ids)
        hub.bots_changed(roster=False)
        return hub.bots_payload()

    @app.post("/api/bots/memory:clear")
    @guarded
    async def post_memory_clear(body: dict[str, Any] | None = None) -> Any:
        ids = clean_ids(body_of(body, {"ids"}))
        hub.check_busy(ids)
        bots.clear_memory(ids)
        hub.bots_changed(roster=False)
        return hub.bots_payload()

    @app.put("/api/bots/memory-enabled")
    @guarded
    async def put_memory_enabled(body: dict[str, Any]) -> Any:
        body_of(body, {"enabled"}, required={"enabled"})
        if not isinstance(body["enabled"], bool):
            raise Refusal(422, "enabled 必须是 true 或 false。")
        bots.memory_enabled = body["enabled"]
        hub.bots_changed(roster=False)
        return hub.bots_payload()

    @app.post("/api/bots/avatars:regenerate")
    @guarded
    async def post_avatars_regenerate() -> Any:
        hub.require_idle("头像")
        hub.check_busy(None)
        _changed, skipped = bots.regenerate_avatars()
        hub.bots_changed(roster=True)
        return {"bots": hub.bots_payload(), "skipped": skipped}

    # ---------------------------------------------------------------------------------------- one bot
    @app.get("/api/bots/{bot_id}")
    @guarded
    async def get_bot(bot_id: str, scenario: str | None = None) -> Any:
        if scenario:
            scenario_of(scenario)
        bots.require(bot_id)
        return hub.bot_detail(bot_id, scenario or None)

    @app.put("/api/bots/{bot_id}")
    @guarded
    async def put_bot(bot_id: str, body: dict[str, Any]) -> Any:
        body = body_of(body, {"name", "avatar", "confirm_reset"})
        bots.require(bot_id)
        if "name" in body and not isinstance(body["name"], str):
            raise Refusal(422, "name 必须是文字。")
        if "avatar" in body and body["avatar"] is not None and not isinstance(body["avatar"], dict):
            raise Refusal(422, "avatar 必须是头像描述（对象）。")
        if "confirm_reset" in body and not isinstance(body["confirm_reset"], bool):
            raise Refusal(422, "confirm_reset 必须是 true 或 false。")
        if body.get("name") is not None or body.get("avatar") is not None:
            hub.require_idle("名字和头像")
        hub.check_busy([bot_id])
        new_id = bots.update_bot(bot_id, name=body.get("name"), avatar=body.get("avatar"), confirm_reset=bool(body.get("confirm_reset")))
        if new_id != bot_id:
            hub.rename_binding(bot_id, new_id)
        hub.bots_changed(roster=True)
        return hub.bot_detail(new_id)

    @app.post("/api/bots/{bot_id}/avatar:regenerate")
    @guarded
    async def post_avatar_regenerate(bot_id: str, body: dict[str, Any] | None = None) -> Any:
        body = body_of(body, {"confirm_reset"})
        bots.require(bot_id)
        hub.require_idle("头像")
        hub.check_busy([bot_id])
        new_id = bots.regenerate_avatar(bot_id, confirm_reset=bool(body.get("confirm_reset")))
        if new_id != bot_id:
            hub.rename_binding(bot_id, new_id)
        hub.bots_changed(roster=True)
        return hub.bot_detail(new_id)

    # ---------------------------------------------------------------------------------------- L0
    @app.get("/api/bots/{bot_id}/core")
    @guarded
    async def get_core(bot_id: str, download: bool = False) -> Any:
        bots.require(bot_id)
        if download:
            markdown = bots.core_markdown(bot_id)
            filename = f"{mb.safe_filename(bots.name_of(bot_id), 'bot')}.core.md"
            return Response(content=markdown.encode("utf-8"), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": content_disposition(filename)})
        return hub.bot_detail(bot_id)["core"]

    @app.put("/api/bots/{bot_id}/core")
    @guarded
    async def put_core(bot_id: str, body: dict[str, Any]) -> Any:
        body_of(body, {"markdown"}, required={"markdown"})
        if not isinstance(body["markdown"], str):
            raise Refusal(422, "markdown 必须是文字。")
        bots.require(bot_id)
        hub.check_busy([bot_id])
        try:
            hint = bots.set_core_markdown(bot_id, body["markdown"])
        except FileProblems as exc:
            return JSONResponse(status_code=400, content={"detail": mb.problems_detail("底层设定没有保存：", exc), "problems": exc.problems})
        hub.bots_changed(roster=False)
        detail = hub.bot_detail(bot_id)
        if hint and hint.casefold() != detail["name"].casefold():
            detail["notices"] = [f"文件里的名字是「{hint}」，只作提示，没有改名（这个玩家仍叫「{detail['name']}」）。"]
        return detail

    @app.delete("/api/bots/{bot_id}/core")
    @guarded
    async def delete_core(bot_id: str) -> Any:
        bots.require(bot_id)
        hub.check_busy([bot_id])
        bots.clear_core([bot_id])
        hub.bots_changed(roster=False)
        return hub.bot_detail(bot_id)

    @app.post("/api/bots/{bot_id}/core:refine")
    @guarded
    async def post_core_refine(bot_id: str, body: dict[str, Any]) -> Any:
        body_of(body, {"text"}, required={"text"})
        if not isinstance(body["text"], str):
            raise Refusal(422, "text 必须是文字。")
        bot = bots.require(bot_id)
        notes = body["text"].strip()
        if not notes:
            raise Refusal(400, "请先写下一些要点，再让主持人模型完善。")
        if len(notes) > 6000:
            raise Refusal(400, "要点太长了（上限 6000 字）。")
        if hub.refine_failure == "unbound":
            raise Refusal(400, "主持人还没有绑定模型，无法完善；请先在“模型绑定”里给主持人选一个模型。")
        if hub.refine_failure == "model":
            return JSONResponse(status_code=502, content={"detail": "调用主持人模型失败，请检查模型绑定和网络后重试。", "code": "model_failed"})
        if hub.refine_failure == "format":
            return JSONResponse(status_code=400, content={"detail": "模型没有按固定格式返回底层设定，请稍后重试。缺少「身份」分区。", "problems": ["缺少「身份」分区。"], "code": "bad_draft"})
        core, _hint, _w = mb.parse_core_markdown(mb.render_core_markdown(bots._core_dict(bot), bot["name"]), require_complete=False, lenient_limits=True)
        lines = [mb.one_line(ln) for ln in notes.splitlines() if mb.one_line(ln)]
        draft = dict(core)
        draft["identity"] = (core["identity"] + "\n" if core["identity"] else "") + (lines[0] if lines else "")
        draft["voice"] = (core["voice"] + "\n" if core["voice"] else "") + "\n".join(lines[1:3]) if len(lines) > 1 else (core["voice"] or "你说话简短直接，先结论后理由。\n「我的意见是……」\n篇幅：50–120 字")
        draft["mind"] = core["mind"] or "你先看事实再听说法，被结果打脸就改。"
        for key in mb.CORE_SECTIONS:
            draft[key] = draft[key].strip()
        game = hub.game()
        game.log_call("roster", purpose="refine", status="ok", attempt=1, elapsed_ms=game.call_rng.randint(1200, 6000), prompt_tokens=game.call_rng.randint(900, 2400),
                      completion_tokens=game.call_rng.randint(120, 500), cache_read_tokens=0, cache_write_tokens=0, finish_reason="stop")
        result: dict[str, Any] = {"draft": mb.render_core_markdown(draft, bot["name"])}
        warnings = mb.validate_core(draft)
        warnings += [f"「{mb.CORE_HEADINGS[k]}」还是空的。" for k in mb.CORE_SECTIONS if not draft[k]]
        if warnings:
            result["problems"] = warnings
        return result

    # ---------------------------------------------------------------------------------------- memory
    @app.put("/api/bots/{bot_id}/memory/{part}")
    @guarded
    async def put_memory(bot_id: str, part: str, body: dict[str, Any]) -> Any:
        body_of(body, {"content", "scenario_id"}, required={"content"})
        if not isinstance(body["content"], str):
            raise Refusal(422, "content 必须是文字。")
        bots.require(bot_id)
        if part not in mb.MEMORY_PARTS:
            raise Refusal(404, f"没有这个记忆部分：{part[:24]}", "not_found")
        sid = scenario_of(body.get("scenario_id")) if part in mb.SCENARIO_PARTS else str(body.get("scenario_id") or "")
        hub.check_busy([bot_id])
        try:
            mb.normalize_part(part, body["content"])
        except FileProblems as exc:
            return JSONResponse(status_code=400, content={"detail": mb.problems_detail("没有保存：", exc), "problems": exc.problems})
        ok, message = bots.write_part(bot_id, part, body["content"], sid)
        if not ok:
            raise Refusal(400, message)
        hub.bots_changed(roster=False)
        return hub.bot_detail(bot_id, sid or None)

    @app.delete("/api/bots/{bot_id}/memory")
    @guarded
    async def delete_memory(bot_id: str, part: str | None = None, scenario: str | None = None) -> Any:
        bots.require(bot_id)
        hub.check_busy([bot_id])
        if part is None:
            bots.clear_all_parts(bot_id)
            sid = scenario or None
        else:
            if part not in mb.MEMORY_PARTS:
                raise Refusal(400, "part 必须是 " + "、".join(mb.MEMORY_PARTS) + " 之一。")
            sid = scenario_of(scenario) if part in mb.SCENARIO_PARTS else (scenario or "")
            ok, message = bots.write_part(bot_id, part, "", sid)
            if not ok:
                raise Refusal(400, message)
        if sid:
            scenario_of(sid)
        hub.bots_changed(roster=False)
        return hub.bot_detail(bot_id, sid or None)

    # ---------------------------------------------------------------------------------------- proposals
    @app.post("/api/bots/{bot_id}/staging/{pid_action}")
    @guarded
    async def post_staging(bot_id: str, pid_action: str) -> Any:
        pid, _, action = pid_action.rpartition(":")
        if action not in ("accept", "reject") or not pid:
            raise Refusal(404, "没有这个接口。", "not_found")
        bots.require(bot_id)
        if action == "accept":
            hub.check_busy([bot_id])
            try:
                bots.accept_staging(bot_id, pid)
            except FileProblems as exc:
                return JSONResponse(status_code=400, content={"detail": mb.problems_detail("没有采纳：", exc), "problems": exc.problems})
        else:
            bots.reject_staging(bot_id, pid)
        hub.bots_changed(roster=False)
        return hub.bot_detail(bot_id)

    # ---------------------------------------------------------------------------------------- export / import
    @app.get("/api/bots/{bot_id}/export")
    @guarded
    async def get_export(bot_id: str) -> Any:
        bots.require(bot_id)
        data = bots.export_zip(bot_id)
        filename = f"{mb.safe_filename(bots.name_of(bot_id), 'bot')}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.arenabot"
        return Response(content=data, media_type="application/octet-stream", headers={"Content-Disposition": content_disposition(filename)})

    @app.post("/api/bots/{bot_id}/import")
    @guarded
    async def post_import(bot_id: str, request: Request, name: str = "") -> Any:
        hub.require_idle("导入（会换掉名字和头像）")
        hub.check_busy([bot_id])
        bots.require(bot_id)
        data = await read_body(request, mb.MAX_ARENABOT_BYTES, ".arenabot 文件")
        try:
            bots.import_zip(bot_id, data)
        except FileProblems as exc:
            return JSONResponse(status_code=422, content={"detail": mb.problems_detail("这个 bot 文件不能导入：", exc), "problems": exc.problems})
        hub.bots_changed(roster=True)
        return hub.bot_detail(bot_id)

    @app.post("/api/bots/{bot_id}/import-default")
    @guarded
    async def post_import_default(bot_id: str, body: dict[str, Any]) -> Any:
        body_of(body, {"name"}, required={"name"})
        if not isinstance(body["name"], str):
            raise Refusal(422, "name 必须是文字。")
        hub.require_idle("导入（会换掉名字和头像）")
        hub.check_busy([bot_id])
        bots.import_default(bot_id, body["name"])
        hub.bots_changed(roster=True)
        return hub.bot_detail(bot_id)

    # ---------------------------------------------------------------------------------------- the moderator
    @app.put("/api/moderator")
    @guarded
    async def put_moderator(body: dict[str, Any]) -> Any:
        if not isinstance(body, dict) or set(body) - {"avatar"}:
            raise Refusal(400, "主持人只能更换头像，名字、性格设定和记忆都是固定的。")
        if body.get("avatar") is None:
            raise Refusal(400, "请给出头像描述（avatar）。")
        hub.require_idle("头像")
        out = bots.set_moderator_avatar(body["avatar"])
        hub.bots_changed(roster=True)
        return out

    @app.post("/api/moderator/avatar:regenerate")
    @guarded
    async def post_moderator_regenerate() -> Any:
        hub.require_idle("头像")
        out = bots.regenerate_moderator_avatar()
        hub.bots_changed(roster=True)
        return out

    # ---------------------------------------------------------------------------------------- the contract (③)
    @app.get("/api/contract")
    @guarded
    async def get_contract(download: bool = False) -> Any:
        if download:
            return Response(content=contract.markdown.encode("utf-8"), media_type="text/markdown; charset=utf-8",
                            headers={"Content-Disposition": content_disposition(contract.download_name())})
        return contract.payload()

    @app.put("/api/contract")
    @guarded
    async def put_contract(body: dict[str, Any]) -> Any:
        body_of(body, {"markdown"}, required={"markdown"})
        if not isinstance(body["markdown"], str):
            raise Refusal(422, "markdown 必须是文字。")
        try:
            contract.set(body["markdown"])
        except FileProblems as exc:
            return JSONResponse(status_code=400, content={"detail": mb.problems_detail("契约没有保存：", exc), "code": "invalid", "problems": exc.problems})
        hub.sources_changed()
        return contract.payload()

    @app.post("/api/contract:reset")
    @guarded
    async def post_contract_reset() -> Any:
        contract.reset()
        hub.sources_changed()
        return contract.payload()

    # ---------------------------------------------------------------------------------------- the rules (④)
    @app.get("/api/rules")
    @guarded
    async def get_rules() -> Any:
        return rules.payload()

    @app.get("/api/rules/{sid}")
    @guarded
    async def get_rule(sid: str, download: bool = False) -> Any:
        rules.require(sid)
        if download:
            return Response(content=rules.scenario_text(sid).encode("utf-8"), media_type="text/markdown; charset=utf-8",
                            headers={"Content-Disposition": content_disposition(rules.download_name(sid))})
        return {**rules.item(sid), "markdown": rules.scenario_text(sid)}

    @app.put("/api/rules/{sid}")
    @guarded
    async def put_rule(sid: str, body: dict[str, Any]) -> Any:
        body_of(body, {"markdown"}, required={"markdown"})
        if not isinstance(body["markdown"], str):
            raise Refusal(422, "markdown 必须是文字。")
        rules.require(sid)
        try:
            rules.set_scenario(sid, body["markdown"])
        except FileProblems as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc), "code": "invalid", "problems": exc.problems})
        hub.sources_changed()
        return rules.item(sid)

    @app.post("/api/rules/{sid_action}")
    @guarded
    async def post_rule_action(sid_action: str) -> Any:
        sid, _, action = sid_action.rpartition(":")
        if action != "reset" or not sid:
            raise Refusal(404, "没有这个接口。", "not_found")
        rules.reset(sid)
        hub.sources_changed()
        return rules.item(sid)

    @app.get("/api/rules/{sid}/materials/{slug}")
    @guarded
    async def get_material(sid: str, slug: str, download: bool = False) -> Any:
        material = rules.material(sid, slug)
        if download:
            filename = f"{sid}-{slug}.material.md"
            return Response(content=material["markdown"].encode("utf-8"), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": content_disposition(filename)})
        return {"slug": slug, "title": material["title"], "markdown": material["markdown"], "chars": len(material["markdown"]), "sha256": mb.sha256_text(material["markdown"])}

    @app.put("/api/rules/{sid}/materials/{slug}")
    @guarded
    async def put_material(sid: str, slug: str, body: dict[str, Any]) -> Any:
        body_of(body, {"markdown", "title"}, required={"markdown"})
        if not isinstance(body["markdown"], str) or ("title" in body and not isinstance(body["title"], str)):
            raise Refusal(422, "markdown 和 title 必须是文字。")
        rules.set_material(sid, slug, body["markdown"], body.get("title"))
        hub.sources_changed()
        return rules.item(sid)

    @app.delete("/api/rules/{sid}/materials/{slug}")
    @guarded
    async def delete_material(sid: str, slug: str) -> Any:
        rules.delete_material(sid, slug)
        hub.sources_changed()
        return rules.item(sid)

    # ---------------------------------------------------------------------------------------- games (⑤): the detail and the files
    @app.get("/api/games/{session_id}/detail")
    @guarded
    async def get_game_detail(session_id: str) -> Any:
        return games.detail(hub.game(), session_id)

    @app.get("/api/games/{session_id}/export")
    @guarded
    async def get_game_export(session_id: str) -> Any:
        data = games.export_zip(hub.game(), session_id)
        return Response(content=data, media_type="application/octet-stream", headers={"Content-Disposition": content_disposition(games.export_name(session_id))})

    @app.post("/api/games/import")
    @guarded
    async def post_game_import(request: Request, name: str = "", on_conflict: str = "ask") -> Any:
        data = await read_body(request, 2 * 1024**3, ".arenagame 文件", "2 GiB")
        return games.import_zip(hub.game(), data, name, on_conflict)

    # ---------------------------------------------------------------------------------------- mock-only switches
    @app.post("/api/mock/games")
    @guarded
    async def mock_games(body: dict[str, Any] | None = None) -> Any:
        """``{"locked": [ids]}``: the files of these games are "in use" - ``DELETE`` answers 409 ``in_use`` (what the real server does when a file cannot be
        removed for the moment).  ``{"importing": true}``: another import is running (``POST /api/games/import`` answers 409 ``busy``)."""
        body = body or {}
        if "locked" in body:
            ids = body["locked"]
            if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                raise Refusal(422, "locked 必须是对局 id 的列表。")
            games.locked = set(ids)
        if "importing" in body:
            games.importing = bool(body["importing"])
        return {"locked": sorted(games.locked), "importing": games.importing}

    @app.post("/api/mock/bots")
    @guarded
    async def mock_bots(body: dict[str, Any] | None = None) -> Any:
        """Put the bots into a situation: ``{"preset": "ready" | "fresh" | "mixed" | "empty"}`` (every bot ✔ / nobody initialized / a bit of everything /
        no L0 at all), ``{"memory_enabled": bool}``.  Only before the game starts."""
        body = body or {}
        hub.require_idle("重置 bot 的状态")
        if "preset" in body:
            bots.apply_preset(str(body["preset"]))
            hub.bot_bindings = {k: v for k, v in hub.bot_bindings.items() if k == MODERATOR}
            hub.load_seat_bindings(hub.game().bindings)
            hub.bots_changed(roster=True)
        if "memory_enabled" in body:
            bots.memory_enabled = bool(body["memory_enabled"])
            hub.bots_changed(roster=False)
        return hub.bots_payload()

    @app.post("/api/mock/files")
    @guarded
    async def mock_files(body: dict[str, Any] | None = None) -> Any:
        """Break the official contract / the scenario for the start check: ``{"contract": "ok" | "missing" | "invalid", "scenario": "ok" | "missing" | "invalid"}``."""
        body = body or {}
        for key, store in (("contract", contract), ("scenario", rules)):
            if key not in body:
                continue
            value = body[key]
            if value not in ("ok", "missing", "invalid"):
                raise Refusal(422, f"{key} 必须是 ok / missing / invalid 之一。")
            if key == "contract":
                contract.broken = None if value == "ok" else value
            else:
                sid = hub.stores.scenario_id()
                if value == "ok":
                    rules.broken.pop(sid, None)
                else:
                    rules.broken[sid] = value
        hub.sources_changed()
        return {"ok": True, "problems": hub.check_ready()}

    @app.post("/api/mock/learning")
    @guarded
    async def mock_learning(body: dict[str, Any] | None = None) -> Any:
        """How the next initializations / reviews go: ``{"init": {"<bot id | seat | name>": "failed" | "timeout" | "slow" | "invalid"}, "review": {"N3": "error" |
        "timeout" | "slow"}, "sim_rate": 12.0, "refine": null | "unbound" | "model" | "format"}`` (``init`` / ``review`` replace the plan; ``{}`` clears it)."""
        body = body or {}
        if "init" in body:
            plan = body["init"] or {}
            if not isinstance(plan, dict) or any(v not in ("failed", "timeout", "slow", "invalid", "done") for v in plan.values()):
                raise Refusal(422, "init 必须是 {bot: failed | timeout | slow | invalid} 的对象。")
            hub.init_plan = dict(plan)
        if "review" in body:
            plan = body["review"] or {}
            if not isinstance(plan, dict) or any(v not in ("error", "timeout", "slow", "done") for v in plan.values()):
                raise Refusal(422, "review 必须是 {座位: error | timeout | slow} 的对象。")
            hub.review_plan = dict(plan)
        if "sim_rate" in body:
            if not isinstance(body["sim_rate"], (int, float)) or isinstance(body["sim_rate"], bool) or body["sim_rate"] <= 0:
                raise Refusal(422, "sim_rate 必须是正数。")
            hub.sim_rate = float(body["sim_rate"])
        if "refine" in body:
            if body["refine"] not in (None, "unbound", "model", "format"):
                raise Refusal(422, "refine 必须是 null / unbound / model / format 之一。")
            hub.refine_failure = body["refine"]
        return {"ok": True, "init": hub.init_plan, "review": hub.review_plan, "sim_rate": hub.sim_rate, "refine": hub.refine_failure}
