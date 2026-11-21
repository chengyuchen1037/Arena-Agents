"""Reading a ``.arenaagents``: checking it, unpacking it safely and swapping it in (docs/archive.md "导入").

Three steps, each usable on its own:

``inspect(path)``
    Everything that can be known without a password and without touching the data directory: the zip structure, the
    member names (allow-list), the manifest, the file list against the real members, every checksum and every file's
    content (each game has to be a valid save-format-v5 game).  Raises :class:`ArchiveFormatError` with the reason.

``prepare(path, ...)``
    Decrypts the keys (when asked to), unpacks every member into ``<data>/tmp/import-<id>/stage`` *verifying it again*,
    validates the unpacked data and returns a :class:`PreparedImport`.  The data directory is not touched.

``commit(prepared, ...)``
    The only step that changes the data directory: the current data is moved to ``<data>/backups/<time>/``, the unpacked
    data is moved in, the keys are written, the caches are cleared and the application reloads.  Any failure puts the old
    data back.  ``apply`` is ``prepare`` + ``commit``.

Everything here is blocking: run it in a worker thread.  No password, key or file content is ever logged or put into an
exception message.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.archive.errors import (
    ArchiveError,
    ArchiveFormatError,
    ArchiveIOError,
    ArchiveKeysMismatchError,
    ArchiveLimitError,
    ArchivePasswordError,
    safe_text,
)
from app.archive.format import (
    DEFAULT_LIMITS,
    JSON_KINDS,
    KIND_BOT_FILE,
    KIND_SECRETS,
    SECRETS_NAME,
    TEXT_KINDS,
    ArchiveLimits,
    Manifest,
    classify,
    parse_manifest,
)
from app.archive.kdf import open_secrets_bound
from app.archive.ports import ProgressFn, SecretsPort, default_secrets, no_progress
from app.archive.validate import MAX_TEXT_BYTES, Inventory, check_member, endpoint_of, providers_of
from app.archive.zipcheck import OpenedZip, human_bytes, restore_mtime, stream_member

logger = logging.getLogger(__name__)

PHASE_VERIFY = "verify"
PHASE_VALIDATE = "validate"
PHASE_BACKUP = "backup"
PHASE_REPLACE = "replace"
PHASE_KEYS = "keys"
PHASE_RELOAD = "reload"
PHASE_FINALIZE = "finalize"
IMPORT_PHASES = (PHASE_VERIFY, PHASE_VALIDATE, PHASE_BACKUP, PHASE_REPLACE, PHASE_KEYS, PHASE_RELOAD, PHASE_FINALIZE)

#: Top-level entries of the data directory an import never moves: the process lock (held open, cannot be renamed on
#: Windows), the machine's master key, and the folders the archive tooling itself lives in.
KEEP_IN_PLACE = frozenset({".lock", "master.v1.dpapi", "backups", "exports", "tmp"})
KEEP_BACKUPS = 3
_BACKUP_NAME = re.compile(r"^[0-9]{8}-[0-9]{6}(?:-[0-9]+)?$")
_MAX_GAMES_LISTED = 100


# ------------------------------------------------------------------------------------------------ summary


@dataclass
class ArchiveSummary:
    """What the user is shown before an import (and what the API returns): facts of the archive, never its content.

    A format 1 archive (v0.13) has ``roster_seats``; a format 2 archive has ``bots``, ``contract`` (is the official contract in it) and
    ``rules`` (the number of scenarios); the fields of the other format are ``None`` and are left out of ``to_dict()``."""

    archive_id: str
    format_version: int
    app_version: str
    created_at: str
    size_bytes: int
    uncompressed_bytes: int
    file_count: int
    games: int
    providers: int
    roster_seats: int | None
    has_secrets: bool
    has_logs: bool
    provider_names: list[str] = field(default_factory=list)
    game_list: list[dict[str, Any]] = field(default_factory=list)
    games_listed: int = 0
    warnings: list[str] = field(default_factory=list)
    bots: int | None = None
    contract: bool | None = None
    rules: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "archive_id": self.archive_id,
            "format_version": self.format_version,
            "app_version": self.app_version,
            "created_at": self.created_at,
            "size_bytes": self.size_bytes,
            "uncompressed_bytes": self.uncompressed_bytes,
            "file_count": self.file_count,
            "games": self.games,
            "providers": self.providers,
        }
        if self.format_version >= 2:
            data.update(bots=self.bots or 0, contract=bool(self.contract), rules=self.rules or 0)
        else:
            data["roster_seats"] = self.roster_seats or 0
        data.update(
            has_secrets=self.has_secrets,
            has_logs=self.has_logs,
            provider_names=list(self.provider_names),
            game_list=[dict(item) for item in self.game_list],
            games_listed=self.games_listed,
            warnings=list(self.warnings),
        )
        return data


class _Opened(OpenedZip):
    """An archive of the total format whose structure, member names and manifest have been checked (no member has been read yet)."""

    def __init__(self, path: Path, limits: ArchiveLimits) -> None:
        super().__init__(path, limits, classify=classify, parse_manifest=parse_manifest)


def _walk(
    opened: _Opened,
    *,
    stage: Path | None,
    progress: ProgressFn,
    skip_secrets: bool = False,
) -> Inventory:
    """Verify (and, with ``stage``, unpack) every member; check each member's content; return what was found."""
    assert opened.zf is not None
    limits = opened.limits
    entries = [entry for entry in opened.manifest.files if not (skip_secrets and entry.path == SECRETS_NAME)]
    total = max(sum(entry.size for entry in entries), 1)
    done = 0
    inventory = Inventory()

    def on_bytes(count: int) -> None:
        nonlocal done
        done += count
        progress(PHASE_VERIFY, min(done, total), total)

    progress(PHASE_VERIFY, 0, total)
    for entry in entries:
        kind, game_id = classify(entry.path)
        info = opened.infos[entry.path]
        small_text = kind in TEXT_KINDS or kind == KIND_BOT_FILE
        keep = kind in JSON_KINDS or small_text or kind == KIND_SECRETS
        if keep:
            cap = limits.max_secrets_bytes if kind == KIND_SECRETS else (MAX_TEXT_BYTES if small_text else limits.max_json_bytes)
            if entry.size > cap:
                raise ArchiveLimitError(f"{safe_text(entry.path)} 太大，无法检查。")
        if stage is not None and kind != KIND_SECRETS:
            target = stage.joinpath(*entry.path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                target.parent.chmod(0o700)
            with open(target, "xb") as sink:
                data = stream_member(opened.zf, info, entry, keep=keep, sink=sink, on_bytes=on_bytes)
            if os.name != "nt":
                target.chmod(0o600)
            restore_mtime(target, info)
        else:
            data = stream_member(opened.zf, info, entry, keep=keep, sink=None, on_bytes=on_bytes)
        check_member(kind, game_id, entry.path, data if keep else None, inventory)
    progress(PHASE_VERIFY, total, total)
    return inventory


def _crosscheck(manifest: Manifest, inventory: Inventory) -> None:
    contents = manifest.contents
    if contents.games != len(inventory.games) or contents.providers != len(inventory.provider_ids):
        raise ArchiveFormatError("清单的内容统计（对局数、Provider 数）与实际内容不符。")
    if manifest.format_version >= 2:
        if (
            contents.bots != len(inventory.bot_ids)
            or contents.rules != len(inventory.scenario_ids)
            or contents.contract != inventory.has_contract
        ):
            raise ArchiveFormatError("清单的内容统计（bot 数、剧本数、是否含契约）与实际内容不符。")
    elif contents.roster_seats != len(inventory.seat_ids):
        raise ArchiveFormatError("清单的内容统计（名册座位数）与实际内容不符。")
    for game_id in inventory.prev_games - set(inventory.games):
        raise ArchiveFormatError(f"对局 {safe_text(game_id)} 只有备份副本，没有 game.json。")


#: What a summary says of a v0.13 archive: its roster is turned into bots when it is imported (H45).
LEGACY_ROSTER_NOTE = "这是 v0.13 的旧格式存档：名册里的 {seats} 个座位会在导入时迁移成 bot（性格设定变成底层设定，记忆会整理成印象）。"


def _summary(opened: _Opened, inventory: Inventory) -> ArchiveSummary:
    manifest = opened.manifest
    games = sorted(inventory.games.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)
    legacy = manifest.format_version < 2
    warnings: list[str] = []
    if legacy and inventory.seat_ids:
        warnings.append(LEGACY_ROSTER_NOTE.format(seats=len(inventory.seat_ids)))
    return ArchiveSummary(
        archive_id=manifest.archive_id,
        format_version=manifest.format_version,
        app_version=manifest.app_version,
        created_at=manifest.created_at,
        size_bytes=opened.size_bytes,
        uncompressed_bytes=opened.uncompressed_bytes,
        file_count=len(manifest.files),
        games=len(inventory.games),
        providers=len(inventory.provider_ids),
        roster_seats=len(inventory.seat_ids) if legacy else None,
        has_secrets=manifest.contents.has_secrets,
        has_logs=manifest.contents.has_logs,
        provider_names=list(inventory.provider_names),
        game_list=games[:_MAX_GAMES_LISTED],
        games_listed=min(len(games), _MAX_GAMES_LISTED),
        warnings=warnings,
        bots=None if legacy else len(inventory.bot_ids),
        contract=None if legacy else inventory.has_contract,
        rules=None if legacy else len(inventory.scenario_ids),
    )


# ------------------------------------------------------------------------------------------------ inspect


def inspect(path: Path, *, limits: ArchiveLimits = DEFAULT_LIMITS, progress: ProgressFn = no_progress) -> ArchiveSummary:
    """Validate an archive completely (no password needed) and describe it; nothing is written anywhere."""
    with _Opened(path, limits) as opened:
        inventory = _walk(opened, stage=None, progress=progress)
        _crosscheck(opened.manifest, inventory)
        return _summary(opened, inventory)


def _member_bytes(opened: "_Opened", name: str, limit: int) -> bytes | None:
    """One member read into memory with its size and SHA-256 checked (``None`` when the archive has no such member)."""
    entry = next((item for item in opened.manifest.files if item.path == name), None)
    if entry is None:
        return None
    if entry.size > limit:
        raise ArchiveLimitError(f"{safe_text(name)} 太大，无法检查。")
    assert opened.zf is not None
    return stream_member(opened.zf, opened.infos[name], entry, keep=True, sink=None, on_bytes=lambda _n: None)


def _open_bound_keys(opened: "_Opened", password: str) -> dict[str, str]:
    """Decrypt the keys of an opened archive and make sure they are still bound to the addresses they were made for:
    ``providers.json`` is not encrypted, so a changed ``base_url`` would send the owner's key somewhere else."""
    blob = _member_bytes(opened, SECRETS_NAME, opened.limits.max_secrets_bytes)
    if blob is None:
        raise ArchiveFormatError("这份存档不含 API Key。")
    sealed = open_secrets_bound(blob, password, opened.manifest.archive_id)
    raw = _member_bytes(opened, "providers.json", opened.limits.max_json_bytes)
    try:
        items = providers_of(json.loads(raw.decode("utf-8")), "providers.json") if raw is not None else []
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ArchiveFormatError("providers.json 不是有效的 JSON。") from None
    current = {str(item["id"]): item for item in items}
    for provider_id in sorted(sealed.keys):
        item = current.get(provider_id)
        # a key whose provider is not in the archive is harmless (it is dropped on import); one whose address changed is not
        if item is not None and sealed.endpoints.get(provider_id) != endpoint_of(item):
            raise ArchiveKeysMismatchError(
                f"存档里 Provider「{safe_text(item.get('name') or provider_id, 40)}」的协议或地址与加密 API Key 时不一致："
                "存档在导出之后被改动过。为了安全，不导入 API Key；可以选择「跳过 API Key」导入其余内容，再在 Provider 里重新填写 Key。"
            )
    return sealed.keys


def decrypt_archive_keys(path: Path, password: str, *, limits: ArchiveLimits = DEFAULT_LIMITS) -> dict[str, str]:
    """The API keys of an archive, decrypted with ``password`` (:class:`ArchivePasswordError` when it is wrong,
    :class:`ArchiveKeysMismatchError` when ``providers.json`` no longer names the addresses they were sealed for).

    Reads only the manifest, ``secrets.enc`` and ``providers.json`` (whose checksums are verified first), so it answers
    in about a second even for a huge archive: the UI asks for the password before it starts the import job."""
    with _Opened(path, limits) as opened:
        return _open_bound_keys(opened, password)


# ----------------------------------------------------------------------------------------------- prepare


@dataclass
class PreparedImport:
    """An archive that has been unpacked and verified into a staging directory, ready to be swapped in."""

    summary: ArchiveSummary
    stage_dir: Path
    work_dir: Path
    keys: dict[str, str] | None = field(default=None, repr=False)  # decrypted API keys (None: none to import)
    provider_ids: list[str] = field(default_factory=list)
    provider_names: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _fix_last_session(stage: Path, game_ids: set[str], warnings: list[str]) -> None:
    """``settings.json`` must not point at a game the archive does not hold: the application would start in its
    "could not restore the last game" protection mode (no autosave)."""
    path = stage / "settings.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    last = data.get("last_session_id") if isinstance(data, dict) else None
    if not last or last in game_ids:
        return
    data["last_session_id"] = None
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    warnings.append("存档里没有「上次对局」所指的对局，已清除这条记录（导入后会从一局新的空白对局开始）。")


SEATS = 12  # the table of the application (``BotStore`` fills a table of this size)


def _prepare_bots(stage: Path, version: int, warnings: list[str], *, seats: int) -> None:
    """The bots of the unpacked data (``stage``), before they are swapped in.

    * format 1 (v0.13): the roster - names, avatars, personas, memories, the records - becomes the bots of the table, like the first start
      of v0.14 on such a data directory does (:meth:`app.storage.bots.BotStore.import_legacy_roster`, H45, H59; no model is called);
      the default bindings saved by seat move to the bots.  An archive without a roster gets the default bots when the application
      reloads.
    * format 2: ``bots/`` has to be a table that works - ``lineup.json`` there, naming ``seats`` bots that all exist, no name twice
      (``BotStore.check_integrity``).  A bot whose *texts* are over a limit (somebody edited a file by hand) is no reason to refuse the
      archive: the application tolerates it and the start check says what to fix, so it is only mentioned in ``warnings``.  An archive with
      no bots at all (no line-up, no bot folder) is fine, too: the default bots are made when the application reloads."""
    from app.storage.bots import BotStore

    store = BotStore(stage, seats=seats)
    try:
        if version < 2:
            roster = stage / "roster"
            if not roster.is_dir():
                return
            store.import_legacy_roster(roster)
            problems = store.check_integrity(content=False)
        else:
            bots = stage / "bots"
            if not (bots / "lineup.json").is_file() and not any(entry.is_dir() for entry in (bots.iterdir() if bots.is_dir() else ())):
                return
            problems = store.check_integrity(content=False)
            if not problems:
                warnings.extend(store.check_integrity())  # (the table works: what is left to say is about the limits of the texts)
    except ArchiveError:
        raise
    except Exception as exc:  # noqa: BLE001 - a storage / validation failure of the bots is a problem of the archive's content
        logger.warning("The bots of an archive could not be prepared: %s", type(exc).__name__, exc_info=True)
        raise ArchiveFormatError("存档里的名册或 bot 数据无法使用（详情见服务端日志）。") from None
    if problems:
        shown = " ".join(problems[:4]) + (f"（另有 {len(problems) - 4} 项）" if len(problems) > 4 else "")
        raise ArchiveFormatError(f"存档里的玩家（bot）数据有问题：{shown}")


def _check_settings(stage: Path) -> None:
    """The real settings reader must accept the unpacked ``settings.json`` (it is stricter than a JSON check)."""
    if not (stage / "settings.json").is_file():
        return
    from app.storage.errors import StorageError
    from app.storage.settings import SettingsStore

    try:
        SettingsStore(stage).get_settings()
    except StorageError:
        raise ArchiveFormatError("settings.json 没有通过校验：它的内容不是本程序能读取的设置。") from None
    except (ValueError, OSError):
        raise ArchiveFormatError("settings.json 没有通过校验。") from None


def prepare(
    path: Path,
    data_dir: Path,
    *,
    password: str | None = None,
    skip_keys: bool = False,
    keys: dict[str, str] | None = None,
    limits: ArchiveLimits = DEFAULT_LIMITS,
    progress: ProgressFn = no_progress,
    work_dir: Path | None = None,
    seats: int = SEATS,
) -> PreparedImport:
    """Unpack and verify an archive into ``<data>/tmp/import-<id>/stage``; the data directory itself is not changed.

    ``keys`` are already decrypted keys (the API checks the password before it starts the job); otherwise ``password``
    decrypts them.  With ``skip_keys`` (or an archive without keys) none are imported.
    """
    data_dir = Path(data_dir).resolve()
    work = Path(work_dir) if work_dir is not None else data_dir / "tmp" / f"import-{uuid.uuid4().hex[:12]}"
    stage = work / "stage"
    try:
        with _Opened(path, limits) as opened:
            manifest = opened.manifest
            import_keys: dict[str, str] | None = None
            if manifest.contents.has_secrets and not skip_keys:
                if keys is not None:
                    import_keys = dict(keys)
                elif password is not None:
                    import_keys = _open_bound_keys(opened, password)
                else:
                    raise ArchivePasswordError("这份存档含有 API Key：请输入密码，或者选择跳过 API Key。")
            work.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(work).free
            need = opened.uncompressed_bytes + limits.min_free_margin_bytes
            if free < need:
                raise ArchiveIOError(f"磁盘空间不足：解压需要约 {human_bytes(opened.uncompressed_bytes)}，可用 {human_bytes(free)}。")
            stage.mkdir(parents=True, exist_ok=True)
            inventory = _walk(opened, stage=stage, progress=progress, skip_secrets=True)
            _crosscheck(manifest, inventory)
            progress(PHASE_VALIDATE, 0, 1)
            warnings: list[str] = []
            _fix_last_session(stage, set(inventory.games), warnings)
            _check_settings(stage)
            _prepare_bots(stage, manifest.format_version, warnings, seats=seats)
            progress(PHASE_VALIDATE, 1, 1)
            summary = _summary(opened, inventory)
            if import_keys is not None:
                import_keys = {pid: key for pid, key in import_keys.items() if pid in set(inventory.provider_ids)}
            return PreparedImport(
                summary=summary,
                stage_dir=stage,
                work_dir=work,
                keys=import_keys,
                provider_ids=list(inventory.provider_ids),
                provider_names=dict(zip(inventory.provider_ids, inventory.provider_names)),
                warnings=warnings,
            )
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


# ------------------------------------------------------------------------------------------------ commit


def _move(source: Path, target: Path) -> None:
    """Rename ``source`` to ``target`` (a file or a whole directory); retried briefly because on Windows an indexer or
    a virus scanner may hold a folder for a moment."""
    last: OSError | None = None
    for attempt in range(4):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(0.15 * (attempt + 1))
        except OSError as exc:
            if exc.errno == errno.EXDEV:  # the data directory spans file systems: fall back to copy + delete
                shutil.move(str(source), str(target))
                return
            last = exc
            break
    logger.warning("Could not move %s (%s)", safe_text(source.name), type(last).__name__)
    raise ArchiveIOError(f"无法移动数据目录里的「{safe_text(source.name)}」（可能被其他程序占用）。")


def _listing(path: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(path) as handle:
            return sorted(handle, key=lambda entry: entry.name)
    except OSError:
        return []


def _fresh_dir(parent: Path, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%d-%H%M%S")
    for index in range(100):
        candidate = parent / (stamp if index == 0 else f"{stamp}-{index}")
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise ArchiveIOError("备份目录里同一时刻的文件夹太多。")


def prune_backups(data_dir: Path, keep: int = KEEP_BACKUPS) -> list[str]:
    """Delete the oldest ``backups/<time>/`` folders so that ``keep`` remain; returns the names removed."""
    backups = Path(data_dir) / "backups"
    names = sorted(entry.name for entry in _listing(backups) if entry.is_dir(follow_symlinks=False) and _BACKUP_NAME.match(entry.name))
    removed: list[str] = []
    for name in names[: max(0, len(names) - keep)]:
        try:
            shutil.rmtree(backups / name)
            removed.append(name)
        except OSError:
            logger.warning("Could not delete the old backup %s", name)
    return removed


@dataclass
class ImportResult:
    backup: str | None  # name of the folder under ``backups/`` that holds the previous data (None: there was none)
    games: int
    providers: int
    keys_imported: int
    keys_kept: int  # local keys kept for providers with the same id (the archive's keys were skipped / missing)
    providers_without_key: list[str]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backup": self.backup,
            "games": self.games,
            "providers": self.providers,
            "keys_imported": self.keys_imported,
            "keys_kept": self.keys_kept,
            "providers_without_key": list(self.providers_without_key),
            "warnings": list(self.warnings),
        }


class ImportTransaction:
    """Swap the prepared data into the data directory; undo it completely on any failure."""

    def __init__(
        self,
        data_dir: Path,
        prepared: PreparedImport,
        *,
        secrets: SecretsPort | None = None,
        now: datetime | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.prepared = prepared
        self._secrets = secrets
        self._now = now or datetime.now()
        self.backup_dir: Path | None = None
        self.changed = False  # the data directory has been touched (a rollback has something to undo)
        self._moved: list[str] = []  # names moved from the data directory into the backup
        self._placed: list[str] = []  # names moved from the stage into the data directory
        self._keys_written = False
        self.keys_imported = 0
        self.keys_kept = 0
        self.providers_without_key: list[str] = []

    @property
    def secrets(self) -> SecretsPort:
        if self._secrets is None:
            self._secrets = default_secrets(self.data_dir)
        return self._secrets

    # ------------------------------------------------------------------------------------------------
    def _old_keys(self) -> dict[str, str]:
        """The keys of this machine, read *before* anything moves (F32: they stay for providers with the same id)."""
        try:
            return {pid: key for pid, key in self.secrets.read_all().items() if isinstance(key, str) and key}
        except Exception as exc:  # noqa: BLE001 - unreadable keys (no master key, damaged file) must not block an import
            logger.warning("The existing API keys could not be read (%s); none will be kept", type(exc).__name__)
            return {}

    def swap(self, progress: ProgressFn = no_progress) -> None:
        """Back the current data up, move the new data in, write the keys.  Raises on failure (call :meth:`rollback`)."""
        prepared = self.prepared
        provider_ids = set(prepared.provider_ids)
        keep_local = {pid: key for pid, key in self._old_keys().items() if pid in provider_ids}
        final = dict(keep_local)
        imported = {pid: key for pid, key in (prepared.keys or {}).items() if pid in provider_ids}
        final.update(imported)

        progress(PHASE_BACKUP, 0, 1)
        current = [entry for entry in _listing(self.data_dir) if entry.name not in KEEP_IN_PLACE]
        self.changed = True
        if current:
            self.backup_dir = _fresh_dir(self.data_dir / "backups", self._now)
            for entry in current:
                _move(Path(entry.path), self.backup_dir / entry.name)
                self._moved.append(entry.name)
            wrapper = self.data_dir / "master.v1.dpapi"
            if wrapper.is_file():
                shutil.copy2(wrapper, self.backup_dir / "master.v1.dpapi")
        progress(PHASE_BACKUP, 1, 1)

        progress(PHASE_REPLACE, 0, 1)
        staged = _listing(prepared.stage_dir)
        for index, entry in enumerate(staged, start=1):
            _move(Path(entry.path), self.data_dir / entry.name)
            self._placed.append(entry.name)
            progress(PHASE_REPLACE, index, max(len(staged), 1))
        progress(PHASE_REPLACE, max(len(staged), 1), max(len(staged), 1))

        progress(PHASE_KEYS, 0, 1)
        if final:
            self._keys_written = True  # even a half-finished write must be undone
            self.secrets.write_all(final)
        self.keys_imported = len(imported)
        self.keys_kept = len([pid for pid in keep_local if pid not in imported])
        self.providers_without_key = [prepared.provider_names.get(pid, pid) for pid in prepared.provider_ids if pid not in final]
        progress(PHASE_KEYS, 1, 1)

    def rollback(self) -> None:
        """Put the previous data back exactly as it was (best effort per entry; problems are logged, not raised)."""
        trash = self.prepared.work_dir / "rolled-back"
        try:
            trash.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        evict = list(self._placed) + (["secrets.v1.enc"] if self._keys_written else [])
        for name in reversed(evict):
            target = self.data_dir / name
            if not (target.exists() or target.is_symlink()):
                continue
            try:
                _move(target, trash / name)
            except ArchiveError:
                try:
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                except OSError:
                    logger.error("Rollback could not remove %s", safe_text(name))
        for name in reversed(self._moved):
            assert self.backup_dir is not None
            try:
                _move(self.backup_dir / name, self.data_dir / name)
            except ArchiveError:
                logger.error("Rollback could not restore %s; the previous data is in %s", safe_text(name), safe_text(self.backup_dir.name))
        if self.backup_dir is not None:
            try:
                (self.backup_dir / "master.v1.dpapi").unlink(missing_ok=True)
                self.backup_dir.rmdir()  # only succeeds when everything was restored
            except OSError:
                pass
        self._moved.clear()
        self._placed.clear()
        self._keys_written = False

    def finish(self) -> None:
        shutil.rmtree(self.prepared.work_dir, ignore_errors=True)
        prune_backups(self.data_dir)


def commit(
    prepared: PreparedImport,
    data_dir: Path,
    *,
    secrets: SecretsPort | None = None,
    reload: Callable[[], Any] | None = None,
    ensure_idle: Callable[[], None] | None = None,
    progress: ProgressFn = no_progress,
    now: datetime | None = None,
    clear_caches: Callable[[], None] | None = None,
) -> ImportResult:
    """Swap ``prepared`` into ``data_dir`` (backup first) and reload the application; any failure restores the old data
    and reloads that.  ``reload`` is called after the swap (it must raise when the new data cannot be used)."""
    if clear_caches is None:
        from app.storage import clear_store_caches as clear_caches
    transaction = ImportTransaction(data_dir, prepared, secrets=secrets, now=now)
    try:
        if ensure_idle is not None:
            ensure_idle()
        transaction.swap(progress)
        clear_caches()
        progress(PHASE_RELOAD, 0, 1)
        if reload is not None:
            reload()
        progress(PHASE_RELOAD, 1, 1)
    except BaseException:
        transaction.rollback()
        if transaction.changed:  # nothing to bring back when the failure came before the first move
            try:
                clear_caches()
                if reload is not None:
                    reload()  # bring the in-memory state back to the restored data
            except Exception:  # noqa: BLE001 - the original error is what the user needs to see
                logger.exception("Reloading the restored data after a failed import failed")
        shutil.rmtree(prepared.work_dir, ignore_errors=True)
        raise
    progress(PHASE_FINALIZE, 0, 1)
    transaction.finish()
    progress(PHASE_FINALIZE, 1, 1)
    return ImportResult(
        backup=transaction.backup_dir.name if transaction.backup_dir is not None else None,
        games=prepared.summary.games,
        providers=prepared.summary.providers,
        keys_imported=transaction.keys_imported,
        keys_kept=transaction.keys_kept,
        providers_without_key=transaction.providers_without_key,
        warnings=list(prepared.warnings),
    )


def apply(
    path: Path,
    password: str | None = None,
    skip_keys: bool = False,
    *,
    data_dir: Path | None = None,
    secrets: SecretsPort | None = None,
    reload: Callable[[], Any] | None = None,
    ensure_idle: Callable[[], None] | None = None,
    keys: dict[str, str] | None = None,
    limits: ArchiveLimits = DEFAULT_LIMITS,
    progress: ProgressFn = no_progress,
    now: datetime | None = None,
    clear_caches: Callable[[], None] | None = None,
) -> ImportResult:
    """``prepare`` + ``commit``: replace the whole data directory with the archive's content.  The previous data is moved
    to ``backups/<time>/`` first and put back if anything fails."""
    if data_dir is None:
        from app.storage.paths import get_data_dir

        data_dir = get_data_dir()
    prepared = prepare(path, data_dir, password=password, skip_keys=skip_keys, keys=keys, limits=limits, progress=progress)
    return commit(
        prepared,
        data_dir,
        secrets=secrets,
        reload=reload,
        ensure_idle=ensure_idle,
        progress=progress,
        now=now,
        clear_caches=clear_caches,
    )


__all__ = [
    "IMPORT_PHASES",
    "KEEP_BACKUPS",
    "KEEP_IN_PLACE",
    "ArchiveSummary",
    "ImportResult",
    "ImportTransaction",
    "PreparedImport",
    "apply",
    "commit",
    "decrypt_archive_keys",
    "human_bytes",
    "inspect",
    "prepare",
    "prune_backups",
]
