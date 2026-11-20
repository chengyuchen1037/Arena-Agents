"""What keeps a benchmark run away from the user's real data and secrets.

* a run never reads or writes the real data directory (games, the provider table, the encrypted keys): :func:`isolated_data_dir` points
  ``ARENA_DATA_DIR`` at a fresh temporary directory for the whole run (and :func:`refuse_real_data_dir` is the check the real-model mode makes);
* a real model is reached only with what the user hands over *explicitly*: a base URL, a model id and the name of an environment variable that
  holds the key (``bench.realllm``) - the provider table of the app is never opened;
* a key never appears in a result, a log or an error message (:func:`scrub`).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

DATA_DIR_ENV = "ARENA_DATA_DIR"
REPO_ROOT = Path(__file__).resolve().parents[1]
#: Where ``ARENA_DATA_DIR`` pointed when this module was imported (before any run isolated it): a real installation's data.
_STARTUP_DATA_DIR = os.environ.get(DATA_DIR_ENV)


class UnsafeDataDir(RuntimeError):
    """A place a benchmark must not touch: the app's real data."""


def _resolved(path: str | Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path).expanduser().absolute()


def real_data_dirs() -> list[Path]:
    """The places the app keeps its data: ``<repository>/data`` (the development default), ``.demo-data``, ``%LOCALAPPDATA%/Arena-Agent``,
    ``/data`` (Docker) and wherever ``ARENA_DATA_DIR`` pointed when this module was imported."""
    found = [REPO_ROOT / "data", REPO_ROOT / ".demo-data", Path("/data")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found.append(Path(local) / "Arena-Agent")
    if _STARTUP_DATA_DIR:
        found.append(Path(_STARTUP_DATA_DIR))
    return [_resolved(p) for p in found]


def refuse_real_data_dir(path: str | Path) -> None:
    """Raise :class:`UnsafeDataDir` when ``path`` is one of the real data directories, inside one, or one that contains one (an output directory
    must not be the repository root, say: ``data/`` would be inside it)."""
    target = _resolved(path)
    for real in real_data_dirs():
        if target == real or real in target.parents or target in real.parents:
            raise UnsafeDataDir(f"基准不能写进真实的数据目录：{target}（它与 {real} 重叠）。")


@contextlib.contextmanager
def isolated_data_dir() -> Iterator[Path]:
    """``ARENA_DATA_DIR`` = a fresh temporary directory, removed afterwards; the old value is restored."""
    directory = Path(tempfile.mkdtemp(prefix="arena-bench-"))
    previous = os.environ.get(DATA_DIR_ENV)
    os.environ[DATA_DIR_ENV] = str(directory)
    try:
        yield directory
    finally:
        if previous is None:
            os.environ.pop(DATA_DIR_ENV, None)
        else:
            os.environ[DATA_DIR_ENV] = previous
        shutil.rmtree(directory, ignore_errors=True)


def scrub(text: object, secrets: Iterable[str] = ()) -> str:
    """``text`` with every secret (a key) replaced by ``***`` - applied to everything a run writes that came from a provider (error texts)."""
    out = str(text)
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "***")
    return out


__all__ = ["DATA_DIR_ENV", "UnsafeDataDir", "isolated_data_dir", "real_data_dirs", "refuse_real_data_dir", "scrub"]
