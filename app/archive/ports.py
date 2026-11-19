"""The few things the archive code asks of the rest of the application, as small protocols.

Keeping them narrow lets the library run (and be tested) against a plain directory and fakes, and keeps the
orchestrator out of the file-level code.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.archive.errors import ArchiveIOError

logger = logging.getLogger(__name__)

#: ``(phase, done, total)``: ``done`` / ``total`` are bytes for packing / unpacking phases and a step count otherwise.
ProgressFn = Callable[[str, int, int], None]


def no_progress(phase: str, done: int, total: int) -> None:
    return None


@runtime_checkable
class SecretsPort(Protocol):
    """``app.storage.secrets.SecretStore``: the API keys of this machine, ``{provider_id: api_key}``."""

    def read_all(self) -> dict[str, str]: ...

    def write_all(self, keys: dict[str, str]) -> None: ...


def default_secrets(data_dir: Path) -> SecretsPort:
    """The key store of ``data_dir``."""
    try:
        from app.storage.secrets import SecretStore
    except ImportError:  # the storage layer of this build has no key store
        raise ArchiveIOError("当前版本没有 API Key 存储，无法导出或导入 API Key。") from None
    return SecretStore(data_dir)


class EnginePort(Protocol):
    """What the jobs need of the running application (``app.game.orchestrator.orchestrator``)."""

    @property
    def bus(self) -> Any: ...

    def is_game_active(self) -> bool: ...

    async def flush_autosave(self, reason: str) -> bool: ...

    async def reload_from_data_dir(self) -> Any: ...

    def persistence_suspended(self) -> contextlib.AbstractAsyncContextManager[Any]: ...


class OrchestratorAdapter:
    """:class:`EnginePort` over the real orchestrator (imported lazily: this package must stay importable without it)."""

    def __init__(self, orchestrator: Any | None = None) -> None:
        self._orchestrator = orchestrator

    @property
    def target(self) -> Any:
        if self._orchestrator is None:
            from app.game.orchestrator import orchestrator

            self._orchestrator = orchestrator
        return self._orchestrator

    @property
    def bus(self) -> Any:
        return self.target.bus

    def is_game_active(self) -> bool:
        """Is a game running (agents working, or the session says so)?  The orchestrator knows best."""
        target = self.target
        is_active = getattr(target, "_is_active", None)
        if callable(is_active):
            return bool(is_active())
        if getattr(target, "_supervisor", None) is not None:
            return True
        session = getattr(target, "session", None)
        return getattr(session, "status", "") == "running"

    async def flush_autosave(self, reason: str) -> bool:
        flush = getattr(self.target, "flush_autosave", None)
        if flush is None:
            return True
        return bool(await flush(reason))

    async def reload_from_data_dir(self) -> Any:
        reload = getattr(self.target, "reload_from_data_dir", None)
        if reload is None:
            raise ArchiveIOError("当前版本的编排器还不能在导入后重新载入数据目录。")
        return await reload()

    def persistence_suspended(self) -> contextlib.AbstractAsyncContextManager[Any]:
        suspend = getattr(self.target, "persistence_suspended", None)
        if suspend is None:
            return _null_async_context()
        return suspend()


@contextlib.asynccontextmanager
async def _null_async_context() -> AsyncIterator[None]:
    yield None
