from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.storage._atomic import atomic_write, ensure_private_directory
from app.storage.paths import _get_app_dir

if os.name == "nt":
    import msvcrt
else:
    import fcntl

WORKSPACE_MARKER = ".arena-workspace.json"
WORKSPACE_DATA_DIRNAME = ".arena"
WORKSPACE_SCHEMA_VERSION = 1
_APP_STATE_FILE = "app-state.json"
_LOCK_FILE = "workspace.lock"
_RECENT_LIMIT = 10
_TRUE_VALUES = {"1", "true", "yes", "on"}
_ID_PATTERN = re.compile(r"^[^\x00-\x1f/\\]{1,128}$")
_RUNTIME_FILES = (
    "master.v1.dpapi",
    "providers.v1.enc",
    "providers.v1.backup.enc",
    "settings.json",
)
_COPY_DIRECTORIES = ("sessions",)


class WorkspaceError(ValueError):
    """A workspace is unsafe, invalid, locked, or could not be activated."""


@dataclass(frozen=True)
class WorkspaceInfo:
    root: Path
    data_dir: Path
    id: str
    name: str
    created_at: str


_workspace_mutex = threading.RLock()
_active_workspace: WorkspaceInfo | None = None
_active_lock_fd: int | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def workspace_enabled() -> bool:
    return os.environ.get("ARENA_DISABLE_WORKSPACE", "").strip().lower() not in _TRUE_VALUES


def get_app_dir() -> Path:
    return _get_app_dir()


def get_default_workspace_path() -> Path:
    override = os.environ.get("ARENA_WORKSPACE_DIR")
    if override and override.strip():
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise WorkspaceError("ARENA_WORKSPACE_DIR must be an absolute path")
        return path.resolve()
    if platform.system() in ("Windows", "Darwin"):
        return (Path.home() / "Arena-Agents" / "Default").resolve()
    return (get_app_dir() / "workspace" / "default").resolve()


def _reject_forbidden_root(root: Path) -> None:
    if root.anchor and root == Path(root.anchor):
        raise WorkspaceError("Workspace cannot be a filesystem root")
    home = Path.home().resolve()
    if root == home:
        raise WorkspaceError("Workspace cannot be the user home directory itself")

    import app.config

    project_root = Path(app.config.BASE_DIR).resolve()
    if root == project_root or project_root in root.parents:
        raise WorkspaceError("Workspace cannot be the project directory or its child")

    if os.name == "nt":
        windows_dir = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        if windows_dir:
            system_root = Path(windows_dir).resolve()
            if root == system_root or system_root in root.parents:
                raise WorkspaceError("Workspace cannot be inside the Windows directory")


def _resolve_workspace_path(path: Path, *, create: bool) -> Path:
    try:
        raw = Path(path)
    except (TypeError, ValueError) as exc:
        raise WorkspaceError("Invalid workspace path") from exc
    if not raw.is_absolute():
        raise WorkspaceError("Workspace path must be absolute")
    if raw.is_symlink():
        raise WorkspaceError("Workspace root cannot be a symbolic link")
    try:
        root = raw.resolve()
    except OSError as exc:
        raise WorkspaceError("Unable to resolve workspace path") from exc
    _reject_forbidden_root(root)
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise WorkspaceError("Workspace root must be a real directory")
    elif create:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError("Unable to create workspace directory") from exc
    else:
        raise WorkspaceError("Workspace directory does not exist")
    if root.is_symlink():
        raise WorkspaceError("Workspace root cannot be a symbolic link")
    return root


def _read_marker(root: Path) -> WorkspaceInfo:
    marker = root / WORKSPACE_MARKER
    if marker.is_symlink():
        raise WorkspaceError("Workspace marker cannot be a symbolic link")
    data_dir = root / WORKSPACE_DATA_DIRNAME
    if data_dir.is_symlink():
        raise WorkspaceError("Workspace data directory cannot be a symbolic link")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != WORKSPACE_SCHEMA_VERSION
            or not isinstance(payload.get("workspace_id"), str)
            or not isinstance(payload.get("name"), str)
            or not isinstance(payload.get("created_at"), str)
        ):
            raise WorkspaceError("Invalid workspace marker")
        workspace_id = payload["workspace_id"]
        name = payload["name"]
        created_at = payload["created_at"]
        if (
            not _ID_PATTERN.fullmatch(workspace_id)
            or not name.strip()
            or not created_at.strip()
        ):
            raise WorkspaceError("Invalid workspace marker fields")
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except WorkspaceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise WorkspaceError("Unable to read workspace marker") from exc
    return WorkspaceInfo(root, data_dir, workspace_id, name, created_at)


def get_active_workspace() -> WorkspaceInfo | None:
    with _workspace_mutex:
        return _active_workspace


def get_active_workspace_data_dir() -> Path | None:
    active = get_active_workspace()
    return active.data_dir if active is not None else None


def workspace_payload(info: WorkspaceInfo, *, include_path=True) -> dict:
    payload = {
        "id": info.id,
        "name": info.name,
        "created_at": info.created_at,
    }
    if include_path:
        payload["path"] = str(info.root)
    return payload


def get_active_workspace_payload(*, include_path: bool) -> dict:
    active = get_active_workspace()
    return {
        "enabled": workspace_enabled(),
        "current": (
            workspace_payload(active, include_path=include_path)
            if active is not None
            else None
        ),
    }


def open_workspace(path: Path) -> WorkspaceInfo:
    """Validate a workspace marker without activating or changing the workspace."""
    root = _resolve_workspace_path(path, create=False)
    return _read_marker(root)


def _prepare_lock_file(root: Path) -> int:
    data_dir = root / WORKSPACE_DATA_DIRNAME
    ensure_private_directory(data_dir)
    lock_path = data_dir / _LOCK_FILE
    if lock_path.is_symlink():
        raise WorkspaceError("Workspace lock cannot be a symbolic link")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise WorkspaceError("Workspace lock must be a regular file")
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            if os.name != "nt":
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return fd
        except Exception:
            os.close(fd)
            raise
    except WorkspaceError:
        raise
    except OSError as exc:
        raise WorkspaceError("Workspace is locked by another process") from exc


def _release_lock(fd: int) -> None:
    try:
        if os.name != "nt":
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _write_marker(info: WorkspaceInfo) -> None:
    payload = {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "workspace_id": info.id,
        "name": info.name,
        "created_at": info.created_at,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    atomic_write(info.root / WORKSPACE_MARKER, encoded, secure_directory=False)


def create_workspace(
    path: Path, *, name=None, initialize_existing=False
) -> WorkspaceInfo:
    root = _resolve_workspace_path(path, create=True)
    marker = root / WORKSPACE_MARKER
    if marker.exists():
        return _read_marker(root)
    try:
        nonempty = any(root.iterdir())
    except OSError as exc:
        raise WorkspaceError("Unable to inspect workspace directory") from exc
    if nonempty and not initialize_existing:
        raise WorkspaceError(
            "A non-empty directory requires initialize_existing=True"
        )
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise WorkspaceError("Workspace name must not be empty")

    data_dir = root / WORKSPACE_DATA_DIRNAME
    ensure_private_directory(data_dir)
    lock_fd = _prepare_lock_file(root)
    try:
        if marker.exists():
            return _read_marker(root)
        info = WorkspaceInfo(
            root=root,
            data_dir=data_dir,
            id=uuid.uuid4().hex,
            name=(name.strip() if isinstance(name, str) else root.name or "Workspace"),
            created_at=_utc_now(),
        )
        _write_marker(info)
        ensure_private_directory(data_dir / "logs")
        ensure_private_directory(data_dir / "exports")
        verified = _read_marker(root)
        if verified != info:
            raise WorkspaceError("Workspace marker verification failed")
        return verified
    except WorkspaceError:
        raise
    except Exception as exc:
        raise WorkspaceError("Unable to initialize workspace") from exc
    finally:
        _release_lock(lock_fd)


def _default_app_state() -> dict[str, Any]:
    return {"last_workspace": None, "recent": []}


def _read_app_state() -> dict[str, Any]:
    path = get_app_dir() / _APP_STATE_FILE
    if not path.exists():
        return _default_app_state()
    if path.is_symlink():
        raise WorkspaceError("Application state cannot be a symbolic link")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("last_workspace"), (str, type(None)))
            or not isinstance(payload.get("recent"), list)
        ):
            raise WorkspaceError("Invalid application workspace state")
        recent = []
        for item in payload["recent"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("path"), str)
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("last_opened_at"), str)
            ):
                raise WorkspaceError("Invalid recent workspace entry")
            recent.append(
                {
                    "path": item["path"],
                    "name": item["name"],
                    "last_opened_at": item["last_opened_at"],
                }
            )
        return {"last_workspace": payload["last_workspace"], "recent": recent}
    except WorkspaceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("Unable to read application workspace state") from exc


def _record_activation(info: WorkspaceInfo) -> None:
    state = _read_app_state()
    opened_at = _utc_now()
    root_key = os.path.normcase(str(info.root))
    recent = [
        item
        for item in state["recent"]
        if os.path.normcase(item["path"]) != root_key
    ]
    recent.insert(
        0,
        {
            "path": str(info.root),
            "name": info.name,
            "last_opened_at": opened_at,
        },
    )
    payload = {
        "last_workspace": str(info.root),
        "recent": recent[:_RECENT_LIMIT],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    atomic_write(get_app_dir() / _APP_STATE_FILE, encoded)


def activate_workspace(info: WorkspaceInfo) -> None:
    """Activate atomically: a failure retains the previous workspace and lock."""
    global _active_workspace, _active_lock_fd
    verified = open_workspace(info.root)
    with _workspace_mutex:
        if _active_workspace and _active_workspace.root == verified.root:
            if _active_workspace.id != verified.id:
                raise WorkspaceError("Workspace identity changed while active")
            _record_activation(verified)
            return
        new_lock_fd = _prepare_lock_file(verified.root)
        try:
            _record_activation(verified)
        except Exception:
            _release_lock(new_lock_fd)
            raise
        old_lock_fd = _active_lock_fd
        _active_workspace = verified
        _active_lock_fd = new_lock_fd
        if old_lock_fd is not None:
            _release_lock(old_lock_fd)


def deactivate_workspace() -> None:
    global _active_workspace, _active_lock_fd
    with _workspace_mutex:
        old_lock_fd = _active_lock_fd
        _active_workspace = None
        _active_lock_fd = None
        if old_lock_fd is not None:
            _release_lock(old_lock_fd)


def list_recent_workspaces() -> list[dict]:
    with _workspace_mutex:
        return [dict(item) for item in _read_app_state()["recent"]]


def _has_runtime_data(data_dir: Path) -> bool:
    if any((data_dir / name).exists() for name in _RUNTIME_FILES):
        return True
    for dirname in _COPY_DIRECTORIES:
        directory = data_dir / dirname
        if directory.is_symlink():
            return True
        if directory.exists() and any(
            path.is_file() for path in directory.rglob("*")
        ):
            return True
    return False


def _runtime_snapshot(data_dir: Path) -> dict:
    from app.providers.store import ProviderStore
    from app.storage.sessions import SessionStore
    from app.storage.settings import SettingsStore

    providers = ProviderStore(data_dir).list_providers()
    settings = SettingsStore(data_dir).get_settings()
    session_store = SessionStore(data_dir)
    sessions = {
        item["session_id"]: session_store.load(item["session_id"])
        for item in session_store.list_sessions()
    }
    return {
        "providers": [provider.model_dump(mode="json") for provider in providers],
        "settings": settings,
        "sessions": sessions,
    }


def _copy_file(source: Path, destination: Path, created: list[Path]) -> None:
    if source.is_symlink() or not source.is_file():
        raise WorkspaceError("Legacy data contains an unsafe file")
    if destination.exists() or destination.is_symlink():
        raise WorkspaceError("Refusing to overwrite existing workspace data")
    atomic_write(destination, source.read_bytes())
    created.append(destination)


def _copy_directory(source: Path, destination: Path, created: list[Path]) -> None:
    if source.is_symlink() or not source.is_dir():
        raise WorkspaceError("Legacy data directory is unsafe")
    for current_root, directories, files in os.walk(source, followlinks=False):
        current = Path(current_root)
        for dirname in directories:
            if (current / dirname).is_symlink():
                raise WorkspaceError("Legacy data contains a symbolic-link directory")
        relative_root = current.relative_to(source)
        target_root = destination / relative_root
        ensure_private_directory(target_root)
        for filename in files:
            source_file = current / filename
            target_file = target_root / filename
            _copy_file(source_file, target_file, created)


def _copy_v10_runtime_data(source: Path, target: Path) -> int:
    # Validate before writing anything to the new workspace.
    source_snapshot = _runtime_snapshot(source)
    created: list[Path] = []
    try:
        for filename in _RUNTIME_FILES:
            source_file = source / filename
            if source_file.exists():
                _copy_file(source_file, target / filename, created)
        for dirname in _COPY_DIRECTORIES:
            source_dir = source / dirname
            if source_dir.exists():
                _copy_directory(source_dir, target / dirname, created)
        target_snapshot = _runtime_snapshot(target)
        if target_snapshot != source_snapshot:
            raise WorkspaceError("Copied v0.10 data verification failed")
        return len(created)
    except Exception:
        # Only files created by this attempted copy are removed. The v0.10
        # source is never renamed, truncated, or deleted.
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def ensure_initial_workspace() -> tuple[WorkspaceInfo, dict]:
    if not workspace_enabled():
        raise WorkspaceError("Workspace support is disabled")
    report: dict[str, Any] = {
        "status": "ok",
        "error": None,
        "used_default": False,
        "migration": {"status": "not-needed", "copied_files": 0},
        "errors": [],
    }
    active = get_active_workspace()
    if active is not None:
        return active, report

    info: WorkspaceInfo | None = None
    try:
        state = _read_app_state()
        last_workspace = state["last_workspace"]
        if last_workspace:
            try:
                info = open_workspace(Path(last_workspace))
            except Exception as exc:
                report["status"] = "degraded"
                report["error"] = str(exc)
                report["errors"].append(f"Previous workspace unavailable: {exc}")
    except Exception as exc:
        report["status"] = "degraded"
        report["error"] = str(exc)
        report["errors"].append(f"Application state unavailable: {exc}")

    if info is None:
        default_path = get_default_workspace_path()
        report["used_default"] = True
        try:
            info = create_workspace(default_path, name="Default")
        except Exception as exc:
            raise WorkspaceError("Unable to initialize the default workspace") from exc

    activate_workspace(info)
    app_dir = get_app_dir()
    target = info.data_dir
    try:
        if app_dir == target:
            report["migration"]["status"] = "skipped"
        elif _has_runtime_data(target):
            _runtime_snapshot(target)
            report["migration"]["status"] = "existing"
        elif _has_runtime_data(app_dir):
            copied = _copy_v10_runtime_data(app_dir, target)
            report["migration"].update(
                {"status": "copied", "copied_files": copied}
            )
        else:
            report["migration"]["status"] = "empty"
    except Exception as exc:
        report["status"] = "degraded"
        report["error"] = str(exc)
        report["migration"]["status"] = "failed"
        report["errors"].append(f"Workspace data validation/copy failed: {exc}")
    return info, report


def pick_native_folder() -> Path:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            selected = filedialog.askdirectory()
        finally:
            root.destroy()
    except Exception as exc:
        raise WorkspaceError("Native folder picker is unavailable") from exc
    if not selected:
        raise WorkspaceError("No workspace folder was selected")
    return Path(selected).resolve()


def reset_workspace_for_tests() -> None:
    deactivate_workspace()


__all__ = [
    "WORKSPACE_DATA_DIRNAME",
    "WORKSPACE_MARKER",
    "WORKSPACE_SCHEMA_VERSION",
    "WorkspaceError",
    "WorkspaceInfo",
    "activate_workspace",
    "create_workspace",
    "deactivate_workspace",
    "ensure_initial_workspace",
    "get_active_workspace",
    "get_active_workspace_data_dir",
    "get_active_workspace_payload",
    "get_app_dir",
    "get_default_workspace_path",
    "list_recent_workspaces",
    "open_workspace",
    "pick_native_folder",
    "reset_workspace_for_tests",
    "workspace_enabled",
    "workspace_payload",
]
