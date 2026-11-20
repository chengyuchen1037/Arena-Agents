"""Helpers shared by the development tools (``dev_server.py``, ``real_smoke.py``, ``init_default_bots.py``).

Nothing here imports the application when this module is imported: the tools point ``ARENA_DATA_DIR`` at a scratch directory FIRST and
import ``app`` afterwards, so that no import-time singleton can ever see the user's real data directory.

* ``apply_price_source`` downloads the LiteLLM price table (the default *price source* of the app) and writes its prices into the model
  entries the given bindings use - in the data directory the process is configured with (``ARENA_DATA_DIR``), i.e. in the *scratch copy*
  the dev tools work on, never in a user's real ``data/`` directory.  It is the same thing the Provider dialog's "刷新价格表" +
  "自动填入" do, so a development instance shows the cost a provider really bills instead of whatever was typed into the registry by hand.
* The **scratch data directory** of the real-model tools (``new_scratch`` / ``copy_provider_files`` / ``adopt_master_key`` /
  ``source_bindings`` / ``write_bindings`` / ``scratch_environment`` / ``patched_config``): the provider registry, its encrypted keys and
  the master-key wrapper are COPIED from the data directory the user names (``--from``) - which is only read, never opened by a store,
  never written - and the default model bindings are written into the copy as the bindings of the seats of the table.
* The **call budget** of a real-model run (``Meter``): a hard cap on the number of model calls, counted where every real call passes
  (the engine's client, ``MeteredClient``) or - for a fake model in a test - at the model port (``MeteredPort``).  A call over the cap is
  never sent: it raises ``BudgetExhausted``.
* ``trace_rows``: the trace of a game (``logs/<session id>/trace-*.jsonl``) as parsed rows.
"""

from __future__ import annotations

import collections
import contextlib
import copy
import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

#: What the real-model tools copy of a data directory: the provider registry, its encrypted keys and (where Windows protects the master
#: key) the DPAPI wrapper next to them.  Nothing else: no games, no bots, no logs.
PROVIDER_FILES = ("providers.json", "secrets.v1.enc", "master.v1.dpapi")


# ======================================================================================================== price source
async def apply_price_source(
    bindings: Iterable[Any],
    *,
    transport: Any = None,
    resolver: Any = None,
    allow_private: bool | None = None,
) -> list[str]:
    """Refresh the price table and price every model the ``bindings`` use with it.

    Returns printable report lines.  Never raises for a network / parsing problem (the existing prices simply
    stay): a development instance must still start offline.
    """
    from app.providers.pricing import get_price_store
    from app.providers.store import get_provider_store

    lines: list[str] = []
    prices = get_price_store()
    status = await prices.refresh(transport=transport, resolver=resolver, allow_private=allow_private)
    if status["error"]:
        return [f"price table: NOT updated ({status['error']}); the configured prices stay"]
    lines.append(f"price table: {status['entries']} models from {status['url']}")

    store = get_provider_store()
    done: set[tuple[str, str]] = set()
    for binding in bindings:
        provider_id, model_id = str(binding.provider_id), str(binding.model_id)
        if (provider_id, model_id) in done:
            continue
        done.add((provider_id, model_id))
        try:
            provider = store.get_provider(provider_id)
            match = prices.match(provider, model_id)
            if match is None:
                lines.append(f"  {model_id}: no price found in the table; the configured price stays")
                continue
            store.update_model(
                provider_id,
                model_id,
                input_price_per_m=match.input_price_per_m,
                output_price_per_m=match.output_price_per_m,
                cache_read_price_per_m=match.cache_read_price_per_m,
                cache_write_price_per_m=match.cache_write_price_per_m,
                price_source=f"litellm:{match.key}",
            )
        except Exception as exc:  # noqa: BLE001 - read-only providers, a missing model ...: report, go on
            lines.append(f"  {model_id}: not updated ({type(exc).__name__})")
            continue
        lines.append(
            f"  {model_id}: in ${match.input_price_per_m}/M, cache read ${match.cache_read_price_per_m}/M, "
            f"cache write ${match.cache_write_price_per_m}/M, out ${match.output_price_per_m}/M  (litellm:{match.key})"
        )
    return lines


# ============================================================================================ the scratch data directory
def new_scratch(root: Path, prefix: str) -> Path:
    """A new private directory below ``root`` (made when it is missing)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(root))).resolve()


def copy_provider_files(source: Path, scratch: Path) -> None:
    """COPY the provider registry, the encrypted keys and the master-key wrapper from the data directory ``source`` into ``scratch``.
    ``source`` is only read (plain file copies: no store opens it, nothing in it is created or changed).  ``SystemExit`` with a plain
    message when it has no registry or no stored keys: then no model can be called."""
    source = Path(source)
    if not (source / "providers.json").is_file():
        raise SystemExit(f"missing providers.json in {source} (is this the data/ directory of an installation?)")
    if not (source / "secrets.v1.enc").is_file():
        raise SystemExit(f"missing secrets.v1.enc in {source}: the providers have no API keys stored, so no model can be called")
    for name in PROVIDER_FILES:
        src = source / name
        if src.is_file():  # (master.v1.dpapi only exists where Windows protects the master key)
            shutil.copy2(src, Path(scratch) / name)


def adopt_master_key(scratch: Path) -> None:
    """Where the master key is wrapped next to the data (Windows: ``master.v1.dpapi``), that wrapper IS the key - an ``ARENA_MASTER_KEY`` of
    the environment must not override it.  Elsewhere the master key comes from the environment (``ARENA_MASTER_KEY`` /
    ``ARENA_MASTER_KEY_FILE``), as it does for the application.  Call it inside :func:`scratch_environment` (the change is undone with it)."""
    if (Path(scratch) / "master.v1.dpapi").is_file():
        os.environ.pop("ARENA_MASTER_KEY", None)
        os.environ.pop("ARENA_MASTER_KEY_FILE", None)


@contextlib.contextmanager
def scratch_environment(scratch: Path) -> Iterator[None]:
    """Make ``scratch`` the data directory of this process (``ARENA_DATA_DIR``) for the duration of the block; every environment variable the
    tools touch is put back afterwards (a test that runs a tool in-process leaves nothing behind)."""
    names = ("ARENA_DATA_DIR", "ARENA_MASTER_KEY", "ARENA_MASTER_KEY_FILE", "PYTHONUTF8")
    saved = {name: os.environ.get(name) for name in names}
    os.environ["ARENA_DATA_DIR"] = str(scratch)
    os.environ["PYTHONUTF8"] = "1"
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def require_scratch(scratch: Path) -> None:
    """Defense in depth: ``SystemExit`` unless the data directory of this process IS ``scratch`` - a tool that went on with any other directory
    (the environment was not applied, a store was made too early) would create and change files in somebody's real data."""
    from app.storage.paths import get_data_dir

    if Path(get_data_dir()).resolve() != Path(scratch).resolve():
        raise SystemExit(f"refusing to go on: the data directory is {get_data_dir()}, not the scratch directory {scratch}")


def source_bindings(source: Path, seats: Sequence[str], *, window: int = 0) -> dict[str, Any]:
    """The default model bindings of the data directory ``source`` as the seats of a table of ``seats`` use them, and the moderator's:
    ``{"moderator": ModelBinding, "N1": ModelBinding, ...}`` (ids and numbers only, no secrets).

    ``source`` may be a v0.13 data directory (bindings saved by seat) or a v0.14 one (saved by bot id: the line-up of the directory says which
    bot sits where) - the settings store of the application reads both the same way.  A seat that has no binding gets the moderator's (else
    the first there is); ``window`` > 0 pretends every model has only that context window.  ``SystemExit`` when there is no binding at all."""
    from app.storage.errors import StorageError
    from app.storage.settings import SettingsStore, migrate_legacy_actor_ids

    try:
        found = SettingsStore(Path(source)).get_default_bindings()
    except StorageError as exc:
        raise SystemExit(f"cannot read the default model bindings of {source}: {exc}") from exc
    found = migrate_legacy_actor_ids(dict(found), {"moderator", *seats})  # (v0.12.0 saved them as p01..)
    if not found:
        raise SystemExit("no default model bindings found; bind models in the UI first")
    fallback = found.get("moderator") or next(iter(found.values()))
    wanted = {actor: found.get(actor) or fallback for actor in ["moderator", *seats]}
    if window > 0:  # a short context window on purpose
        wanted = {actor: binding.model_copy(update={"context_window_tokens": int(window)}) for actor, binding in wanted.items()}
    return wanted


def write_bindings(scratch: Path, bindings: dict[str, Any]) -> None:
    """Save ``bindings`` (keyed by seat and ``moderator``) as the default bindings of the scratch data directory - and nothing else: no
    last-session pointer, so nothing is restored.  While the directory has no table of bots they are stored as they are; the first use of the
    bot store moves each seat's binding to the bot that sits there."""
    from app.storage.settings import SettingsStore

    SettingsStore(Path(scratch)).save_default_bindings(bindings)


@contextlib.contextmanager
def patched_config(cfg: dict[str, Any]) -> Iterator[None]:
    """``config.yml`` is ``cfg`` for the orchestrator AND for the stores that read it themselves (the rules store installs the default
    scenario the configuration names into the data directory) - for the duration of the block."""
    import app.config as app_config
    import app.game.orchestrator as orchestrator_module
    import app.storage.rules as rules_module

    def fake(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return copy.deepcopy(cfg)

    originals = (app_config.load_config, orchestrator_module.load_config)
    app_config.load_config = fake
    orchestrator_module.load_config = fake
    rules_module._config_cache.clear()  # (it remembers what the file names by its bytes, not what ``cfg`` says)
    try:
        yield
    finally:
        app_config.load_config, orchestrator_module.load_config = originals
        rules_module._config_cache.clear()


# ===================================================================================================== the call budget
class BudgetExhausted(RuntimeError):
    """The call budget of the run is spent: this call was not sent."""


class Meter:
    """Counts the model calls of a run and refuses the ones over ``max_calls`` (nothing is sent for them).  ``errors`` counts what the
    calls that were sent raised, by class."""

    def __init__(self, max_calls: int) -> None:
        self.max_calls = int(max_calls)
        self.calls = 0  # calls that were let through
        self.refused = 0  # calls over the budget
        self.errors: collections.Counter[str] = collections.Counter()

    def extend(self, more: int) -> None:
        """Raise the budget by ``more`` calls (a later phase of the run has its own allowance)."""
        self.max_calls += max(0, int(more))

    def reserve(self) -> int:
        """Book one call; :class:`BudgetExhausted` when the budget is spent.  Returns the number of the call."""
        if self.calls >= self.max_calls:
            self.refused += 1
            raise BudgetExhausted(f"the call budget of {self.max_calls} model calls is spent; this call was not sent")
        self.calls += 1
        return self.calls

    def client(self, inner: Any) -> MeteredClient:
        """The engine's model client behind the budget (every real call - the game's, the learning runs', the one-shots' - passes it)."""
        return MeteredClient(inner, self)

    def port(self, inner: Any) -> MeteredPort:
        """A model port (a test's fake, the demo's scripted bots) behind the budget."""
        return MeteredPort(inner, self)


class MeteredClient:
    """``LLMClient`` behind a :class:`Meter`: ``chat_stream`` is counted and capped (one call = one ``chat_stream``: the retries the client makes
    inside it for a network error do not count again), every other attribute (``reconfigure``, ``aclose`` ...) is the real client's.  The
    keyword arguments of ``chat_stream`` pass through as they are (``is_moderator``, ``tools``, ``on_chunk``, ``max_tokens_override``,
    ``binding``, ``on_retry`` ...), and the model port above can still find out that retries can be reported."""

    def __init__(self, inner: Any, meter: Meter) -> None:
        self._inner = inner
        self._meter = meter

    async def chat_stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self._meter.reserve()
        try:
            return await self._inner.chat_stream(messages, **kwargs)
        except Exception as exc:  # noqa: BLE001 - counted, then re-raised for the caller's own handling (a cancellation is not an error)
            self._meter.errors[type(exc).__name__] += 1
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class MeteredPort:
    """A model port (``complete(agent_id, messages, tools, *, max_tokens)``) behind a :class:`Meter`."""

    def __init__(self, inner: Any, meter: Meter) -> None:
        self._inner = inner
        self._meter = meter

    async def complete(
        self, agent_id: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, *, max_tokens: int | None = None
    ) -> Any:
        self._meter.reserve()
        try:
            return await self._inner.complete(agent_id, messages, tools, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001 - counted, then re-raised for the caller's own handling (a cancellation is not an error)
            self._meter.errors[type(exc).__name__] += 1
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ============================================================================================== the initialization report
#: The parts of the memory an initialization writes (``questions`` is optional).
MEMORY_FILES = ("arch_notes", "digest", "playbook", "questions")


def init_report(orch: Any, ids: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Print how an initialization went, bot by bot - state, time, model calls and tokens, the files it wrote and their size, why it failed -
    and what it cost in all; returns the rows (``ids``: only these bots, in this order; default: the whole table).

    The model calls are read from the error log of the orchestrator's game (``purpose`` ``init``, one line per call, the bot's id as the
    actor), the cost from ``bots/usage.json`` of the data directory the orchestrator works on."""
    store = orch._bots()
    scenario_id = orch.source_hashes().scenario_id
    lines = [row for row in orch.call_log().entries() if row.get("purpose") == "init" and row.get("kind") == "call"]
    summaries = {str(s["id"]): s for s in orch.bot_summaries()}
    order = list(ids) if ids is not None else [str(b) for b in store.lineup()]
    rows: list[dict[str, Any]] = []
    for bot_id in order:
        summary = summaries.get(bot_id)
        if summary is None:
            continue
        init = summary["init"]
        mine = [row for row in lines if row.get("actor_id") == bot_id]
        sizes = {part: len(store.read_part(bot_id, part, scenario_id)) for part in MEMORY_FILES}
        row = {
            "id": bot_id,
            "name": str(summary["name"]),
            "status": str(init["status"]),
            "seconds": float(init["duration_s"]) if init.get("duration_s") is not None else None,
            "calls": len(mine),
            "failed_calls": sum(1 for line in mine if line.get("status") != "ok"),
            "tokens_in": sum(int(line.get("prompt_tokens") or 0) for line in mine),
            "tokens_cached": sum(int(line.get("cache_read_tokens") or 0) for line in mine),
            "tokens_out": sum(int(line.get("completion_tokens") or 0) for line in mine),
            "files": {part: size for part, size in sizes.items() if size},
            "error": init.get("error"),
        }
        rows.append(row)
        seconds = f"{row['seconds']:.0f}s" if row["seconds"] is not None else "-"
        failed = f" ({row['failed_calls']} failed)" if row["failed_calls"] else ""
        written = ", ".join(f"{part} {size}字" for part, size in row["files"].items()) or "nothing"
        print(
            f"  {row['name']:8s} {row['status']:13s} {seconds:>5s}  calls={row['calls']}{failed}"
            f"  in={row['tokens_in']} cached={row['tokens_cached']} out={row['tokens_out']}  wrote: {written}"
        )
        if row["error"]:
            print(f"           why: {row['error']}")
    usage = orch.learning_usage()
    print(f"  total: {usage['calls']} calls, {usage['tokens']} tokens, ${usage['cost_usd']:.4f} (bots/usage.json of the scratch directory)")
    return rows


# ============================================================================================================== traces
def trace_rows(data_dir: Path, session_id: str) -> list[dict[str, Any]]:
    """Every row of the trace of one game, oldest first (``<data>/logs/<session id>/trace-*.jsonl``; lines that are not JSON are skipped)."""
    from app.runtime.trace_log import trace_dir, trace_files

    rows: list[dict[str, Any]] = []
    for _index, path in trace_files(trace_dir(Path(data_dir) / "logs", session_id)):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows
