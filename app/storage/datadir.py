"""The data directory: creating it and keeping two servers out of it (v0.13, decisions F20 / F21).

:func:`ensure_data_dir` creates ``<data>/`` (``0700`` on POSIX) and takes the process lock ``<data>/.lock`` for
the lifetime of the process, so two Arena-Agents servers can never share one data directory (they would
overwrite each other's autosaves).  The lock is an advisory OS lock on an open file (``msvcrt`` on Windows,
``fcntl.flock`` elsewhere): it disappears with the process, even after a crash, so there are no stale lock
files to clean up.

The process holds at most one lock.  Asking for another directory (tests change ``ARENA_DATA_DIR``) takes the
new lock first and releases the old one afterwards, so a failure leaves the old directory locked.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

from app.storage._atomic import ensure_private_directory
from app.storage.errors import StorageError
from app.storage.paths import get_data_dir

if os.name == "nt":
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)

LOCK_FILENAME = ".lock"
LOCKED_MESSAGE = "另一个 Arena-Agents 实例正在使用该数据目录。"


class DataDirLockedError(StorageError):
    """Another process holds the data directory.  The server must refuse to start (it is not a degraded state)."""


@dataclass(frozen=True)
class _HeldLock:
    path: Path
    fd: int


_mutex = threading.RLock()
_held: _HeldLock | None = None


def _open_lock_file(data_dir: Path) -> int:
    lock_path = data_dir / LOCK_FILENAME
    if lock_path.is_symlink():
        raise StorageError("数据目录里的进程锁文件不能是符号链接。")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise StorageError("无法在数据目录中创建进程锁文件。") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise StorageError("数据目录里的进程锁必须是普通文件。")
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
    except StorageError:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        raise StorageError("无法初始化数据目录里的进程锁文件。") from exc
    return fd


#: What a lock that somebody else holds fails with (``flock``: EWOULDBLOCK / EAGAIN; ``msvcrt.locking``: EACCES).
_CONTENDED = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK})


def _lock_fd(fd: int) -> bool:
    """Take the exclusive lock without waiting.

    Returns ``True`` when it is held.  Raises :class:`DataDirLockedError` when somebody else has it.  Returns ``False``
    (and warns) when the file system cannot lock at all (some network shares and bind mounts): refusing to start there
    would make the application unusable, and the lock only guards against starting a second server by mistake.
    """
    try:
        if os.name != "nt":
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        if exc.errno in _CONTENDED:
            raise DataDirLockedError(LOCKED_MESSAGE) from exc
        logger.warning(
            "The file system of the data directory does not support file locks (%s); make sure no second "
            "Arena-Agents server uses this directory",
            exc.strerror or type(exc).__name__,
        )
        return False
    return True


def _unlock_fd(fd: int) -> None:
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


def ensure_data_dir() -> Path:
    """Create the data directory and lock it for this process; returns its path.

    Idempotent: while this process already holds the lock of the directory that
    :func:`app.storage.paths.get_data_dir` names, nothing happens.  Raises :class:`DataDirLockedError` when
    another process holds it, and :class:`~app.storage.errors.StorageError` when it cannot be created.
    """
    global _held
    path = get_data_dir()
    with _mutex:
        if _held is not None and _held.path == path:
            return path
        ensure_private_directory(path)
        fd = _open_lock_file(path)
        try:
            _lock_fd(fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        previous, _held = _held, _HeldLock(path, fd)
        if previous is not None:
            _unlock_fd(previous.fd)
        return path


def release_data_dir() -> None:
    """Give the lock back (clean shutdown, tests).  The data stays where it is."""
    global _held
    with _mutex:
        previous, _held = _held, None
    if previous is not None:
        _unlock_fd(previous.fd)


def held_data_dir() -> Path | None:
    """The directory this process has locked, if any."""
    with _mutex:
        return _held.path if _held is not None else None


__all__ = [
    "DataDirLockedError",
    "LOCKED_MESSAGE",
    "LOCK_FILENAME",
    "ensure_data_dir",
    "held_data_dir",
    "release_data_dir",
]
