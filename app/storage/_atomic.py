from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app.storage.errors import StorageError


def ensure_private_directory(path: Path) -> None:
    try:
        if path.is_symlink():
            raise StorageError("Refusing to use a symbolic-link data directory")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.is_dir():
            raise StorageError("External data path is not a directory")
        if os.name != "nt":
            path.chmod(0o700)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError("Unable to create the external data directory") from exc


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY)
        os.fsync(fd)
    except OSError as exc:
        raise StorageError("Unable to sync the external data directory") from exc
    finally:
        if fd is not None:
            os.close(fd)


def atomic_write(
    path: Path, data: bytes, *, secure_directory: bool = True
) -> None:
    """Write bytes beside their destination, fsync, then atomically replace."""
    if not isinstance(data, bytes):
        raise TypeError("atomic_write expects bytes")
    if secure_directory:
        ensure_private_directory(path.parent)
    elif not path.parent.is_dir():
        raise StorageError("Atomic write destination directory does not exist")
    if path.is_symlink():
        raise StorageError("Refusing to replace a symbolic-link data file")

    fd: int | None = None
    temp_path: Path | None = None
    try:
        fd, raw_temp_path = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temp_path = Path(raw_temp_path)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        temp_path = None
        if os.name != "nt":
            path.chmod(0o600)
        _fsync_directory(path.parent)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError("Atomic external storage write failed") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
