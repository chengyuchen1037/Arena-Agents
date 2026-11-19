"""Saved games (v0.13; decisions F21, F22, F38; contract docs/v0.12-contracts.md section 11.3).

Every game lives in its own folder, ``<data>/games/<session_id>/``::

    game.json        the game as plain JSON (UTF-8, not indented; save format v5)
    game.prev.json   the previous good copy of ``game.json``

and ``<data>/games/index.json`` keeps one summary per game for the history list.  The index is only a cache:
it is updated by every save and rebuilt from the game files when it is missing, damaged or no longer matches
the folders (an imported archive, a hand-copied game, a crash between the two writes).

Nothing is encrypted here: a game holds no secret (the API keys live in ``secrets.v1.enc``, see
:mod:`app.storage.secrets`).  Writes are atomic (temp file, fsync, replace); the previous good copy is kept so
that a damaged ``game.json`` costs at most the last save.  Symbolic links are never followed.
"""

from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.storage._atomic import atomic_write, ensure_private_directory
from app.storage._validation import validate_session_id
from app.storage.errors import StorageError
from app.storage.paths import get_data_dir

GAMES_DIRNAME = "games"
LOGS_DIRNAME = "logs"
GAME_FILE = "game.json"
PREVIOUS_FILE = "game.prev.json"
INDEX_FILE = "index.json"
INDEX_SCHEMA_VERSION = 1
#: ``status`` of a summary whose game files cannot be read (it can still be deleted from the history list)
UNREADABLE_STATUS = "unreadable"


class GameNotFoundError(StorageError):
    """There is no such game: no folder, or a folder without a game file (the API answers 404)."""


def _encode(payload: dict[str, Any]) -> bytes:
    try:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise StorageError("对局数据不是有效的 JSON，无法保存。") from exc


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise StorageError("对局文件不是有效的 JSON。") from exc
    if not isinstance(payload, dict):
        raise StorageError("对局文件必须是 JSON 对象。")
    return payload


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


class GameStore:
    """Plain-JSON store of the games of one data directory (see the module docstring)."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = (data_dir or get_data_dir()).resolve()
        self._games_dir = self._data_dir / GAMES_DIRNAME
        self._index_path = self._games_dir / INDEX_FILE
        self._lock = threading.RLock()
        #: ``path -> (size, mtime_ns)`` of a file this store wrote and read back: it need not be parsed again
        #: when the next save rotates it into ``game.prev.json``
        self._verified: dict[Path, tuple[int, int]] = {}

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    # ------------------------------------------------------------------ paths ---
    def _game_dir(self, session_id: str) -> Path:
        validate_session_id(session_id)
        return self._games_dir / session_id

    def _check_not_symlink(self, *paths: Path) -> None:
        for path in paths:
            if path.is_symlink():
                raise StorageError("拒绝使用符号链接形式的对局目录或文件。")

    # --------------------------------------------------------------- file level ---
    def _read_file(self, path: Path) -> dict[str, Any]:
        if path.is_symlink():
            raise StorageError("拒绝读取符号链接形式的对局文件。")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise StorageError("无法读取对局文件。") from exc
        return _decode(raw)

    def _good_bytes(self, path: Path) -> bytes | None:
        """The bytes of ``path`` when it is a readable game file (a JSON object), else ``None``."""
        try:
            if path.is_symlink():
                return None
            stat = path.stat()
            raw = path.read_bytes()
        except OSError:
            return None
        if self._verified.get(path) == (stat.st_size, stat.st_mtime_ns):
            return raw
        try:
            _decode(raw)
        except StorageError:
            return None
        return raw

    def _remember(self, path: Path) -> None:
        try:
            stat = path.stat()
        except OSError:
            self._verified.pop(path, None)
            return
        self._verified[path] = (stat.st_size, stat.st_mtime_ns)

    # --------------------------------------------------------------------- save ---
    def save(self, session_id: str, payload: dict) -> None:
        """Write ``games/<id>/game.json`` atomically; the previous good copy becomes ``game.prev.json``."""
        if not isinstance(payload, dict):
            raise TypeError("game payload must be a dict")
        game_dir = self._game_dir(session_id)
        encoded = _encode(payload)
        with self._lock:
            self._check_not_symlink(self._games_dir, game_dir)
            ensure_private_directory(self._games_dir)
            ensure_private_directory(game_dir)
            current = game_dir / GAME_FILE
            existing = self._good_bytes(current)
            if existing != encoded:
                if existing is not None:
                    atomic_write(game_dir / PREVIOUS_FILE, existing)
                atomic_write(current, encoded)
                if current.read_bytes() != encoded:
                    raise StorageError("对局文件写入后校验失败。")
                self._remember(current)
            self._record_in_index(session_id, payload, current)

    # --------------------------------------------------------------------- load ---
    def load(self, session_id: str) -> dict:
        """The game; a damaged ``game.json`` falls back to ``game.prev.json``."""
        game_dir = self._game_dir(session_id)
        with self._lock:
            self._check_not_symlink(self._games_dir, game_dir)
            found = False
            for filename in (GAME_FILE, PREVIOUS_FILE):
                path = game_dir / filename
                if not path.exists() and not path.is_symlink():
                    continue
                found = True
                try:
                    return self._read_file(path)
                except StorageError:
                    continue
            if found:
                raise StorageError("对局文件和它的上一份副本都无法读取。")
            raise GameNotFoundError("对局不存在。")

    def exists(self, session_id: str) -> bool:
        game_dir = self._game_dir(session_id)
        with self._lock:
            self._check_not_symlink(self._games_dir, game_dir)
            return (game_dir / GAME_FILE).exists() or (game_dir / PREVIOUS_FILE).exists()

    # ------------------------------------------------------------------- delete ---
    def delete(self, session_id: str) -> None:
        """Remove the game, its logs (``logs/<id>/``) and its index entry.  Idempotent."""
        game_dir = self._game_dir(session_id)
        logs_dir = self._data_dir / LOGS_DIRNAME / session_id
        with self._lock:
            self._check_not_symlink(self._games_dir, game_dir, logs_dir.parent, logs_dir)
            for target in (game_dir, logs_dir):
                if target.exists():
                    try:
                        shutil.rmtree(target)
                    except OSError as exc:
                        raise StorageError("无法删除对局文件。") from exc
            for path in [p for p in self._verified if game_dir in p.parents]:
                self._verified.pop(path, None)
            self._drop_from_index(session_id)

    # ------------------------------------------------------------------- listing ---
    def list_games(self) -> list[dict]:
        """One summary per game, newest ``updated_at`` first; the index is rebuilt when it does not match the folders."""
        with self._lock:
            if self._games_dir.is_symlink():
                raise StorageError("拒绝读取符号链接形式的 games 目录。")
            if not self._games_dir.is_dir():
                return []
            found = self._scan()
            entries = self._read_index()
            if entries is None or not self._index_matches(entries, found):
                entries = {item["session_id"]: item for item in self.rebuild_index()}
            return self._sorted(entries.values())

    def rebuild_index(self) -> list[dict]:
        """Read every game file again and write ``index.json`` from scratch."""
        with self._lock:
            if self._games_dir.is_symlink():
                raise StorageError("拒绝读取符号链接形式的 games 目录。")
            if not self._games_dir.is_dir():
                return []
            entries: dict[str, dict] = {}
            for session_id, (candidate, stat) in self._scan().items():
                entries[session_id] = self._summary_of(session_id, candidate, stat)
            self._write_index(entries)
            return self._sorted(entries.values())

    @staticmethod
    def summarize(payload: dict, *, size_bytes: int = 0, updated_at: str | None = None) -> dict:
        """The list-view summary of a game payload (tolerant: a game of another version or a partial one still yields one)."""
        payload = _as_dict(payload)
        world = _as_dict(payload.get("world"))
        state = _as_dict(world.get("state"))
        usage = _as_dict(payload.get("usage_stats"))
        status = payload.get("status")
        if status == "running":  # the process ended mid-game: it comes back paused
            status = "paused"
        elif status not in ("idle", "paused", "finished"):
            status = "paused"
        tokens = usage.get("total_tokens")
        if not isinstance(tokens, (int, float)) or isinstance(tokens, bool):
            tokens = sum(
                _number(usage.get(key))
                for key in ("prompt_tokens", "cache_read_tokens", "cache_write_tokens", "completion_tokens")
            )
        participants = payload.get("participants")
        schema = payload.get("schema_version")
        return {
            "session_id": _text(payload.get("session_id")),
            "title": _text(payload.get("title")),
            "scenario_name": _text(payload.get("scenario_name")),
            "scenario_title": _text(payload.get("scenario_title")),
            "status": status,
            "winner": _optional_text(state.get("winner")),
            "created_at": _optional_text(payload.get("created_at")),
            "started_at": _optional_text(payload.get("started_at")),
            "finished_at": _optional_text(payload.get("finished_at")),
            "updated_at": updated_at,
            "duration_gt": round(_number(world.get("clock_now")), 3),
            "players": len(participants) if isinstance(participants, list) else 0,
            "tokens": int(tokens),
            "cost_usd": round(_number(usage.get("total_cost_usd")), 6),
            "human_involved": bool(payload.get("human_spans")),
            "schema_version": schema if isinstance(schema, int) and not isinstance(schema, bool) else None,
            "size_bytes": int(size_bytes),
        }

    # ------------------------------------------------------------ index internals ---
    def _scan(self) -> dict[str, tuple[Path, Any]]:
        """``{session_id: (the file the entry describes, its stat)}`` for every game folder."""
        found: dict[str, tuple[Path, Any]] = {}
        try:
            children = list(self._games_dir.iterdir())
        except OSError as exc:
            raise StorageError("无法列出对局目录。") from exc
        for game_dir in children:
            if game_dir.is_symlink() or not game_dir.is_dir():
                continue
            try:
                validate_session_id(game_dir.name)
            except ValueError:
                continue
            for filename in (GAME_FILE, PREVIOUS_FILE):
                candidate = game_dir / filename
                if candidate.is_symlink():
                    continue
                try:
                    stat = candidate.stat()
                except OSError:
                    continue
                found[game_dir.name] = (candidate, stat)
                break
        return found

    def _summary_of(self, session_id: str, candidate: Path, stat: Any) -> dict:
        """The summary of one folder: the content of the first readable of ``game.json`` / ``game.prev.json``, the
        size and time of ``candidate`` (what :meth:`_index_matches` compares)."""
        updated_at = _utc_iso(stat.st_mtime)
        payload: dict[str, Any] | None = None
        for path in dict.fromkeys((candidate, candidate.with_name(PREVIOUS_FILE))):
            try:
                payload = self._read_file(path)
                break
            except StorageError:
                continue
        if payload is None:
            summary = self.summarize({}, size_bytes=stat.st_size, updated_at=updated_at)
            summary["status"] = UNREADABLE_STATUS
        else:
            summary = self.summarize(payload, size_bytes=stat.st_size, updated_at=updated_at)
        summary["session_id"] = session_id  # the folder is the identity, whatever the file says
        return summary

    @staticmethod
    def _sorted(entries: Any) -> list[dict]:
        return sorted(entries, key=lambda item: (item.get("updated_at") or "", item["session_id"]), reverse=True)

    def _read_index(self) -> dict[str, dict] | None:
        """The index as ``{session_id: summary}``; ``None`` when it is missing or damaged."""
        try:
            if self._index_path.is_symlink() or not self._index_path.is_file():
                return None
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError):
            return None
        games = data.get("games") if isinstance(data, dict) and data.get("schema_version") == INDEX_SCHEMA_VERSION else None
        if not isinstance(games, list):
            return None
        entries: dict[str, dict] = {}
        for item in games:
            if not isinstance(item, dict) or not isinstance(item.get("session_id"), str) or item["session_id"] in entries:
                return None
            entries[item["session_id"]] = item
        return entries

    def _index_matches(self, entries: dict[str, dict], found: dict[str, tuple[Path, Any]]) -> bool:
        if set(entries) != set(found):
            return False
        for session_id, (_candidate, stat) in found.items():
            entry = entries[session_id]
            if entry.get("size_bytes") != stat.st_size or entry.get("updated_at") != _utc_iso(stat.st_mtime):
                return False
        return True

    def _write_index(self, entries: dict[str, dict]) -> None:
        document = {"schema_version": INDEX_SCHEMA_VERSION, "games": self._sorted(entries.values())}
        ensure_private_directory(self._games_dir)
        atomic_write(self._index_path, _encode(document))

    def _record_in_index(self, session_id: str, payload: dict, current: Path) -> None:
        try:
            stat = current.stat()
        except OSError:
            return
        entries = self._read_index() or {}
        summary = self.summarize(payload, size_bytes=stat.st_size, updated_at=_utc_iso(stat.st_mtime))
        summary["session_id"] = session_id
        entries[session_id] = summary
        self._write_index(entries)

    def _drop_from_index(self, session_id: str) -> None:
        entries = self._read_index()
        if entries is None or session_id not in entries:
            return
        del entries[session_id]
        self._write_index(entries)


_store_lock = threading.RLock()
_game_store: GameStore | None = None


def get_game_store() -> GameStore:
    """The store of the current data directory (a new one is made when ``ARENA_DATA_DIR`` points elsewhere)."""
    global _game_store
    with _store_lock:
        data_dir = get_data_dir()
        if _game_store is None or _game_store.data_dir != data_dir:
            _game_store = GameStore(data_dir)
        return _game_store


def reset_game_store() -> None:
    global _game_store
    with _store_lock:
        _game_store = None


def save(session_id: str, payload: dict) -> None:
    get_game_store().save(session_id, payload)


def load(session_id: str) -> dict:
    return get_game_store().load(session_id)


def exists(session_id: str) -> bool:
    return get_game_store().exists(session_id)


def list_games() -> list[dict]:
    return get_game_store().list_games()


def delete(session_id: str) -> None:
    get_game_store().delete(session_id)


__all__ = [
    "GAME_FILE",
    "INDEX_FILE",
    "PREVIOUS_FILE",
    "UNREADABLE_STATUS",
    "GameNotFoundError",
    "GameStore",
    "delete",
    "exists",
    "get_game_store",
    "list_games",
    "load",
    "reset_game_store",
    "save",
]
