"""Bots API (v0.14; docs/v0.12-contracts.md §13.7, the "botstore" table): the lineup, a bot's name / avatar / L0 / memory /
proposals, import and export, the default library, and the moderator's avatar.

    GET    /api/bots                                  the line-up, every bot's summary, readiness (``BotsPayload``)
    PUT    /api/bots/lineup                           {order}: seat the bots (idle game only)
    POST   /api/bots/core:clear · memory:clear        {ids?}: empty L0 / what the bots learned
    PUT    /api/bots/memory-enabled                   {enabled}
    POST   /api/bots/avatars:regenerate               new avatars for every bot that is not frozen (idle only)
    GET    /api/bots/{id}                             ``BotDetail`` (``?scenario=``)
    PUT    /api/bots/{id}                             {name?, avatar?, confirm_reset?}: a frozen bot needs the confirmation
    POST   /api/bots/{id}/avatar:regenerate
    GET · PUT · DELETE /api/bots/{id}/core            L0 as the merged file (``?download=1``) / replace it / clear it
    POST   /api/bots/{id}/core:refine                 {text}: the moderator's model drafts an L0 (not saved)
    PUT    /api/bots/{id}/memory/{part}               {content, scenario_id?} (the user's edit takes effect at once)
    DELETE /api/bots/{id}/memory                      ``?part=`` (default: everything it learned)
    POST   /api/bots/{id}/staging/{pid}:accept · :reject   a proposal of L0 a review made
    GET    /api/bots/{id}/export · POST /api/bots/{id}/import · POST /api/bots/{id}/import-default
    PUT    /api/moderator · POST /api/moderator/avatar:regenerate

Everything that changes something needs a trusted client (loopback, or the API token).  Errors are ``{"detail": "<中文>"}`` plus
``"code"`` (``needs_confirm`` with ``will_clear``, ``name_taken``, ``game_active``, ``init_busy``, ``not_found`` ...) and
``"problems"`` (every fault of a text, in Chinese).  What the orchestrator knows - the seat's binding, a running initialization,
the start check - is added by ``Orchestrator.bots_payload`` / ``bot_detail`` when it has them; until then
:func:`stub_bots_payload` builds the same objects from the stores alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import tempfile
from functools import wraps
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from app.api._common import (
    LazyOrchestrator,
    attachment,
    content_disposition,
    error_response as _error,
    maybe_await,
    safe_filename as _safe_filename,
    stamp as _stamp,
)
from app.api.security import require_trusted_client, verify_api_access
from app.bots.refine import MAX_NOTES_CHARS, build_core_refine_messages, clean_answer
from app.bots.types import MEMORY_PARTS, SourceHashes, materials_hash, sha256_text
from app.storage import botfiles
from app.storage.botfiles import BotFileError
from app.storage.bots import (
    BotError,
    NameTaken,
    NeedsConfirm,
    default_scenario_id,
    default_scenario_path,
    file_sha256,
    get_bot_store,
    limits_payload,
)
from app.storage.errors import StorageError
from app.storage.paths import demo_mode_enabled, get_base_dir, get_data_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])
TRUSTED = [Depends(require_trusted_client)]

MAX_ARENABOT_BYTES = 20 * 1024 * 1024
REFINE_MAX_TOKENS = 6000
MODERATOR_ID = "moderator"


# ======================================================================================================================
# what the API needs of the orchestrator (looked up when it is needed, so that this module loads without it)
# ======================================================================================================================
orchestrator: Any = LazyOrchestrator()


def _orch_attr(name: str) -> Any:
    try:
        return getattr(orchestrator, name)
    except AttributeError:
        return None


def _game_status() -> str:
    session = _orch_attr("session")
    return str(getattr(session, "status", "idle") or "idle")


class GameActive(BotError):
    """The game is not idle: names, avatars and the line-up belong to the table before the game starts."""

    status = 409
    code = "game_active"

    def __init__(self, what: str) -> None:
        super().__init__(f"{what}只能在对局开始之前修改；请先新建对局。")


class InitBusy(BotError):
    status = 409
    code = "init_busy"

    def __init__(self) -> None:
        super().__init__("正在初始化，请等它结束（或先取消）后再修改。")


def _require_idle(what: str) -> None:
    if _game_status() != "idle":
        raise GameActive(what)


def _check_busy(bot_ids: list[str] | None) -> None:
    """A bot that is being initialized must not be changed under the job's feet (``None``: any bot)."""
    getter = _orch_attr("init_busy_ids")
    if not callable(getter):
        return
    busy = set(getter() or ())
    if busy and (bot_ids is None or busy & set(bot_ids)):
        raise InitBusy()


async def _changed(*, roster: bool) -> None:
    """Tell the orchestrator that bots changed: an idle game takes the new seats / names / avatars / bindings and the clients
    get ``bots.updated`` (and, when ``roster``, ``roster.updated``).  Nothing happens when it has no such hook."""
    hook = _orch_attr("bots_changed")
    if callable(hook):
        await maybe_await(hook(roster=roster))


# ======================================================================================================================
# the objects of the contract: the stand-in of ``Orchestrator.bots_payload`` / ``bot_detail``
# ======================================================================================================================
def _fallback_contract_hash() -> str:
    return file_sha256(get_base_dir() / "prompts" / "contract.md")


def _fallback_scenario(scenario_id: str) -> str:
    path = default_scenario_path()
    return file_sha256(path) if (path is not None and scenario_id) else ""


def current_sources() -> SourceHashes:
    """The hashes of the contract, the scenario and its study materials as they are now (the contract / rules stores when they
    exist, else the files the program ships)."""
    scenario_id = default_scenario_id()
    contract = scenario = materials = ""
    try:
        from app.storage.contract import get_contract_store

        contract = get_contract_store().current().sha256
    except Exception:  # noqa: BLE001
        contract = _fallback_contract_hash()
    try:
        from app.storage.rules import get_rules_store

        rules = get_rules_store()
        scenario = sha256_text(rules.scenario_text(scenario_id))
        materials = materials_hash(rules.materials(scenario_id))
    except Exception:  # noqa: BLE001
        scenario = _fallback_scenario(scenario_id)
    return SourceHashes(contract=contract, scenario=scenario, materials=materials, scenario_id=scenario_id)


def _binding_info(bot_id: str) -> dict[str, Any]:
    """``BotSummary.binding``: ``{"ok", "label"}`` of the model this bot's initialization (and its seat) uses."""
    if demo_mode_enabled():
        return {"ok": True, "label": "演示模式"}
    try:
        from app.storage.settings import get_settings_store

        binding = get_settings_store().get_bot_binding(bot_id)
    except StorageError:
        binding = None
    if binding is None:
        return {"ok": False, "label": "未绑定模型"}
    try:
        from app.providers.store import get_provider_store

        provider, _model = get_provider_store().resolve(binding)
    except Exception:  # noqa: BLE001 - the provider or the model is gone
        return {"ok": False, "label": f"{binding.provider_id} / {binding.model_id}（已不存在）"}
    label = f"{provider.name} / {binding.model_id}"
    if not str(getattr(provider, "api_key", "") or "").strip():
        return {"ok": False, "label": f"{label}（未设置 API Key）"}
    return {"ok": True, "label": label}


def _learning_usage() -> dict[str, Any]:
    try:
        raw = json.loads(botfiles.read_text(get_bot_store().root / "usage.json") or "{}")
    except (ValueError, StorageError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    return {"calls": int(raw.get("calls") or 0), "tokens": int(raw.get("tokens") or 0), "cost_usd": float(raw.get("cost_usd") or 0.0)}


def stub_bots_payload(*, full: bool = False) -> dict[str, Any]:
    """``BotsPayload`` (contract §13.7) built from the stores alone: the seat and the model binding are added to every summary,
    ``ready`` lists what the start check would stop at (the bots' own problems and a missing model).  ``full`` adds ``limits`` and
    ``defaults``.  What needs the orchestrator - a running initialization, the contract / scenario checks - is not in it."""
    store = get_bot_store()
    sources = current_sources()
    order = store.lineup()
    bots: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    for index, bot_id in enumerate(order, start=1):
        summary = store.summary(bot_id, sources=sources)
        binding = _binding_info(bot_id)
        summary["seat"] = f"N{index}"
        summary["binding"] = binding
        bots.append(summary)
        items = store.problems(bot_id, sources=sources)
        if not binding["ok"]:
            items.append({"code": "no_binding", "message": "还没有给这个玩家绑定可用的模型。", "fix": "bind_model"})
        if items:
            problems.append({"scope": "bot", "bot_id": bot_id, "seat": f"N{index}", "name": summary["name"], "items": items})
    payload: dict[str, Any] = {
        "lineup": order,
        "bots": bots,
        "memory_enabled": store.memory_enabled(),
        "init_job": None,
        "ready": {"ok": not problems, "problems": problems},
        "learning_usage": _learning_usage(),
    }
    if full:
        payload["limits"] = limits_payload()
        payload["defaults"] = store.defaults()
    return payload


def stub_bot_detail(bot_id: str, scenario_id: str | None = None) -> dict[str, Any]:
    """``BotDetail`` built from the stores alone (see :func:`stub_bots_payload`)."""
    store = get_bot_store()
    data = store.detail(bot_id, scenario_id or "", sources=current_sources())
    data["seat"] = store.seat_of(bot_id)
    data["binding"] = _binding_info(bot_id)
    return data


async def _payload(*, full: bool = False) -> dict[str, Any]:
    real = _orch_attr("bots_payload")
    if callable(real):
        return await maybe_await(real(full=full))
    return await asyncio.to_thread(stub_bots_payload, full=full)


async def _detail(bot_id: str, scenario: str | None = None) -> dict[str, Any]:
    real = _orch_attr("bot_detail")
    if callable(real):
        return await maybe_await(real(bot_id, scenario))
    return await asyncio.to_thread(stub_bot_detail, bot_id, scenario)


def _scenario_id(explicit: str | None = None) -> str:
    """The scenario a memory request is about: the one asked for, else the one of the game, else the program's default."""
    sid = (explicit or "").strip()
    if not sid:
        sid = str(getattr(_orch_attr("spec"), "id", "") or "")
    if not sid:
        sid = default_scenario_id()
    if not sid:
        raise BotError("没有可用的剧本。")
    try:
        return botfiles.check_scenario_id(sid)
    except BotFileError as exc:
        raise BotError(str(exc)) from None


# ======================================================================================================================
# errors and small helpers
# ======================================================================================================================
def _problems_detail(prefix: str, exc: BotFileError) -> str:
    return f"{prefix}{exc}" if len(exc.problems) == 1 else f"{prefix}共 {len(exc.problems)} 处问题：{exc}"


def _guarded(func: Any) -> Any:
    """Turn the refusals of the store (``BotError`` and its kinds, ``BotFileError``) into ``{"detail", "code", ...}`` answers with
    their own HTTP status; an error with a ``status`` / ``code`` / ``message`` of its own (the archive's) likewise."""

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except NeedsConfirm as exc:
            return _error(409, str(exc), code="needs_confirm", will_clear=exc.will_clear)
        except BotError as exc:
            return _error(exc.status, str(exc), code=exc.code or None)
        except BotFileError as exc:
            return _error(400, str(exc), problems=exc.problems)
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001 - the archive's errors carry their own status and message
            status, code, message = getattr(exc, "status", None), getattr(exc, "code", None), getattr(exc, "message", None)
            if isinstance(status, int) and isinstance(code, str) and isinstance(message, str):
                return _error(status, message, code=code)
            raise

    return wrapper


def _tmp_dir() -> Path:
    from app.storage._atomic import ensure_private_directory

    path = get_data_dir() / "tmp"
    ensure_private_directory(path)
    return path


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("could not delete %s", path)


async def _run(func: Any, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(func, *args, **kwargs)


# ======================================================================================================================
# request bodies
# ======================================================================================================================
class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LineupRequest(_Body):
    order: list[str] = Field(max_length=64)


class IdsRequest(_Body):
    ids: list[str] | None = Field(default=None, max_length=64)


class MemoryEnabledRequest(_Body):
    enabled: bool


class BotUpdate(_Body):
    name: str | None = Field(default=None, max_length=200)
    avatar: dict[str, Any] | None = None
    confirm_reset: bool = False


class ResetRequest(_Body):
    confirm_reset: bool = False


class CoreUpdate(_Body):
    markdown: str = Field(max_length=200_000)


class RefineRequest(_Body):
    text: str = Field(max_length=200_000)


class MemoryUpdate(_Body):
    content: str = Field(max_length=200_000)
    scenario_id: str | None = Field(default=None, max_length=64)


class ImportDefaultRequest(_Body):
    name: str = Field(max_length=200)


class ModeratorUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    avatar: dict[str, Any] | None = None


# ======================================================================================================================
# the line-up and the whole table
# ======================================================================================================================
@router.get("/bots")
@_guarded
async def get_bots() -> Any:
    return await _payload(full=True)


@router.put("/bots/lineup", dependencies=TRUSTED)
@_guarded
async def put_lineup(req: LineupRequest) -> Any:
    """Seat the bots in this order (a permutation of the bots there are, else 422).  The names, avatars, memory and model
    bindings follow the bots; only an idle game is changed (409 ``game_active``)."""
    _require_idle("阵容")
    await _run(get_bot_store().set_lineup, req.order)
    await _changed(roster=True)
    return await _payload()


@router.post("/bots/core:clear", dependencies=TRUSTED)
@_guarded
async def clear_cores(req: IdsRequest | None = None) -> Any:
    """Empty the L0 of these bots (all when ``ids`` is left out); an initialized bot becomes ``stale`` and cannot start."""
    ids = req.ids if req else None
    _check_busy(ids)
    await _run(get_bot_store().clear_core, ids)
    await _changed(roster=False)
    return await _payload()


@router.post("/bots/memory:clear", dependencies=TRUSTED)
@_guarded
async def clear_memories(req: IdsRequest | None = None) -> Any:
    """Forget what these bots learned (notes, digest, playbook, questions, impressions, record) - and with it the initialization;
    L0 stays."""
    ids = req.ids if req else None
    _check_busy(ids)
    await _run(get_bot_store().clear_memory, ids)
    await _changed(roster=False)
    return await _payload()


@router.put("/bots/memory-enabled", dependencies=TRUSTED)
@_guarded
async def put_memory_enabled(req: MemoryEnabledRequest) -> Any:
    await _run(get_bot_store().set_memory_enabled, req.enabled)
    await _changed(roster=False)
    return await _payload()


@router.post("/bots/avatars:regenerate", dependencies=TRUSTED)
@_guarded
async def regenerate_avatars() -> Any:
    """New avatars for every bot that is not frozen (a frozen bot would lose what it learned); ``skipped`` are the frozen ones."""
    _require_idle("头像")
    _check_busy(None)
    _changed_ids, skipped = await _run(get_bot_store().regenerate_avatars)
    await _changed(roster=True)
    return {"bots": await _payload(), "skipped": skipped}


# ======================================================================================================================
# one bot
# ======================================================================================================================
@router.get("/bots/{bot_id}")
@_guarded
async def get_bot(bot_id: str, scenario: str | None = Query(default=None, max_length=64)) -> Any:
    if scenario:
        _scenario_id(scenario)
    return await _detail(bot_id, scenario or None)


@router.put("/bots/{bot_id}", dependencies=TRUSTED)
@_guarded
async def put_bot(bot_id: str, req: BotUpdate) -> Any:
    """Rename a bot / change its avatar.  A bot that has learned something is frozen: without ``confirm_reset`` the answer is 409
    ``needs_confirm`` (with ``will_clear``); with it the bot starts a new identity (new id, L0 kept, the rest cleared)."""
    if req.name is not None or req.avatar is not None:
        _require_idle("名字和头像")
    _check_busy([bot_id])
    try:
        new_id = await _run(get_bot_store().update_bot, bot_id, name=req.name, avatar=req.avatar, confirm_reset=req.confirm_reset)
    except NameTaken as exc:
        return _error(400, str(exc), code="name_taken")
    await _changed(roster=True)
    return await _detail(new_id)


@router.post("/bots/{bot_id}/avatar:regenerate", dependencies=TRUSTED)
@_guarded
async def regenerate_avatar(bot_id: str, req: ResetRequest | None = None) -> Any:
    _require_idle("头像")
    _check_busy([bot_id])
    new_id = await _run(get_bot_store().regenerate_avatar, bot_id, confirm_reset=bool(req and req.confirm_reset))
    await _changed(roster=True)
    return await _detail(new_id)


# ---------------------------------------------------------------------------------------------------- L0
@router.get("/bots/{bot_id}/core")
@_guarded
async def get_core(bot_id: str, download: bool = Query(default=False)) -> Any:
    """The L0 of a bot: ``BotDetail.core`` (with the merged file as ``markdown``); ``?download=1`` is the file itself."""
    if download:
        store = get_bot_store()
        name = await _run(store.name_of, bot_id)
        markdown = await _run(store.core_markdown, bot_id)
        return attachment(markdown, f"{_safe_filename(name, 'bot')}.core.md")
    return (await _detail(bot_id))["core"]


@router.put("/bots/{bot_id}/core", dependencies=TRUSTED)
@_guarded
async def put_core(bot_id: str, req: CoreUpdate) -> Any:
    """Replace L0 by a merged file.  All of it is checked first (400 with ``problems``: every fault, with the section and the
    numbers); the name in the file is only a hint (``notices`` says when it differs).  An initialized bot becomes ``stale``."""
    _check_busy([bot_id])
    store = get_bot_store()
    try:
        parsed = await _run(store.set_core_markdown, bot_id, req.markdown)
    except BotFileError as exc:
        return _error(400, _problems_detail("底层设定没有保存：", exc), problems=exc.problems)
    await _changed(roster=False)
    detail = await _detail(bot_id)
    current = detail.get("name") or await _run(store.name_of, bot_id)
    if parsed.name and parsed.name.casefold() != str(current).casefold():
        detail["notices"] = [f"文件里的名字是「{parsed.name}」，只作提示，没有改名（这个玩家仍叫「{current}」）。"]
    return detail


@router.delete("/bots/{bot_id}/core", dependencies=TRUSTED)
@_guarded
async def delete_core(bot_id: str) -> Any:
    _check_busy([bot_id])
    await _run(get_bot_store().clear_core, [bot_id])
    await _changed(roster=False)
    return await _detail(bot_id)


async def _complete_oneshot(messages: list[dict[str, str]]) -> str:
    """One model call of the moderator outside the game: ``Orchestrator.complete_oneshot`` (accounted, in the call log), else the
    one-shot port the orchestrator still has from v0.13."""
    complete = _orch_attr("complete_oneshot")
    if callable(complete):
        return str(await maybe_await(complete(MODERATOR_ID, messages, max_tokens=REFINE_MAX_TOKENS, purpose="refine")) or "")
    factory = _orch_attr("_oneshot_port")
    if callable(factory):
        port = factory()
        response = await port.complete(MODERATOR_ID, messages, None, max_tokens=REFINE_MAX_TOKENS)
        record = _orch_attr("_record_oneshot")
        if callable(record):
            record("roster", response)
        return str(getattr(response, "content", "") or "")
    raise BotError("引擎还不支持让主持人模型完善设定。", status=501, code="not_implemented")


@router.post("/bots/{bot_id}/core:refine", dependencies=TRUSTED)
@_guarded
async def refine_core(bot_id: str, req: RefineRequest) -> Any:
    """The moderator's model turns the user's points into a merged L0 file: ``{"draft": "<merged md>"}`` (and ``problems`` when the
    draft is over a limit).  Nothing is saved: the user reads it first and saves it with ``PUT .../core``."""
    store = get_bot_store()
    name = await _run(store.name_of, bot_id)
    notes = req.text.strip()
    if not notes:
        raise BotError("请先写下一些要点，再让主持人模型完善。")
    if len(notes) > MAX_NOTES_CHARS:
        raise BotError(f"要点太长了（上限 {MAX_NOTES_CHARS} 字）。")
    session = _orch_attr("session")
    binder = getattr(session, "get_model_binding", None)
    if callable(binder) and binder(MODERATOR_ID) is None and not _orch_attr("demo_mode"):
        raise BotError("主持人还没有绑定模型，无法完善；请先在“模型绑定”里给主持人选一个模型。")
    core = await _run(store.core, bot_id)
    has_core = bool(core.identity.strip() or core.voice.strip() or core.mind.strip() or core.extras)
    current = await _run(store.core_markdown, bot_id) if has_core else ""  # (what the bot is now: the points are merged into it)
    messages = build_core_refine_messages(name, notes, current)
    try:
        answer = await _complete_oneshot(messages)
    except BotError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider / network problems of the model call are never echoed
        logger.warning("Refine of the core of a bot failed: %s", type(exc).__name__)
        return _error(502, "调用主持人模型失败，请检查模型绑定和网络后重试。", code="model_failed")
    text = clean_answer(answer)
    if not text:
        raise BotError("模型没有返回内容，请稍后重试。")
    if not text.lstrip().startswith("---"):
        text = f"---\nformat: arena-core\nversion: 1\nname: {botfiles.name_hint(name) or name}\n---\n{text}"
    try:
        parsed = botfiles.parse_core_markdown(text, require_complete=False, lenient_limits=True)
    except BotFileError as exc:
        logger.warning("Refine of the core of a bot: the answer is not a core file")
        return _error(400, "模型没有按固定格式返回底层设定，请稍后重试。" + str(exc), problems=exc.problems, code="bad_draft")
    warnings = list(parsed.warnings) + [f"「{botfiles.CORE_HEADINGS[key]}」还是空的。" for key in parsed.core.missing]
    result: dict[str, Any] = {"draft": botfiles.render_core_markdown(parsed.core, name)}
    if warnings:
        result["problems"] = warnings
    return result


# ---------------------------------------------------------------------------------------------------- memory
@router.put("/bots/{bot_id}/memory/{part}", dependencies=TRUSTED)
@_guarded
async def put_memory(bot_id: str, part: str, req: MemoryUpdate) -> Any:
    """The user's edit of one part of what a bot learned (``arch_notes`` / ``digest`` / ``playbook`` / ``questions`` / ``people``):
    checked against its limits (400 with ``problems``), written at once, no effect on the initialization (C9)."""
    if part not in MEMORY_PARTS:
        return _error(404, f"没有这个记忆部分：{part[:24]}", code="not_found")
    sid = _scenario_id(req.scenario_id) if part in ("digest", "playbook", "questions") else (req.scenario_id or "")
    _check_busy([bot_id])
    try:
        botfiles.normalize_part(part, req.content)
    except BotFileError as exc:
        return _error(400, _problems_detail("没有保存：", exc), problems=exc.problems)
    result = await _run(get_bot_store().write_part, bot_id, part, req.content, sid)
    if not result.ok:
        return _error(400, result.message)
    await _changed(roster=False)
    return await _detail(bot_id, sid or None)


@router.delete("/bots/{bot_id}/memory", dependencies=TRUSTED)
@_guarded
async def delete_memory(
    bot_id: str,
    part: str | None = Query(default=None, max_length=32),
    scenario: str | None = Query(default=None, max_length=64),
) -> Any:
    """Clear one part of what a bot learned (``?part=``), or - without it - everything it learned (the initialization with it)."""
    _check_busy([bot_id])
    store = get_bot_store()
    if part is None:
        await _run(store.clear_all_parts, bot_id)
        sid = scenario or None
    else:
        if part not in MEMORY_PARTS:
            raise BotError("part 必须是 " + "、".join(MEMORY_PARTS) + " 之一。")
        sid = _scenario_id(scenario) if part in ("digest", "playbook", "questions") else (scenario or "")
        result = await _run(store.clear_part, bot_id, part, sid)
        if not result.ok:
            raise BotError(result.message)
    await _changed(roster=False)
    return await _detail(bot_id, sid or None)


# ---------------------------------------------------------------------------------------------------- proposals
@router.post("/bots/{bot_id}/staging/{pid}:accept", dependencies=TRUSTED)
@_guarded
async def accept_staging(bot_id: str, pid: str) -> Any:
    """Accept a proposal of L0 a review made: it is written into L0 (an initialized bot becomes ``stale``)."""
    _check_busy([bot_id])
    try:
        await _run(get_bot_store().accept_staging, bot_id, pid)
    except BotFileError as exc:
        return _error(400, _problems_detail("没有采纳：", exc), problems=exc.problems)
    await _changed(roster=False)
    return await _detail(bot_id)


@router.post("/bots/{bot_id}/staging/{pid}:reject", dependencies=TRUSTED)
@_guarded
async def reject_staging(bot_id: str, pid: str) -> Any:
    await _run(get_bot_store().reject_staging, bot_id, pid)
    await _changed(roster=False)
    return await _detail(bot_id)


# ---------------------------------------------------------------------------------------------------- export / import
def _codec() -> Any:
    """The ``.arenabot`` zip packing of the archive package: ``pack_arenabot(target, *, name, origin, files)`` and
    ``unpack_arenabot(source, workdir) -> Path`` (the unpacked ``bot/`` directory, checked: format, SHA-256, whitelist, size)."""
    try:
        from app.archive import arenabot
    except ImportError:
        raise BotError("导出和导入 .arenabot 的功能还没有就绪。", status=501, code="not_implemented") from None
    return arenabot


@router.get("/bots/{bot_id}/export")
@_guarded
async def export_bot(bot_id: str) -> Any:
    """The whole bot as a ``.arenabot`` file (name, avatar, L0, memory, record, pending proposals): ``<名字>-YYYYmmdd-HHMMSS.arenabot``."""
    codec = _codec()
    store = get_bot_store()
    name = await _run(store.name_of, bot_id)
    files = await _run(store.bot_files, bot_id)
    origin = (await _run(store.summary, bot_id))["origin"]
    target = _tmp_dir() / f"export-{secrets.token_hex(6)}.arenabot"
    try:
        await _run(codec.pack_arenabot, target, name=name, origin=origin, files=files)
    except BaseException:
        _unlink(target)
        raise
    return FileResponse(
        target,
        media_type="application/octet-stream",
        headers={"Content-Disposition": content_disposition(f"{_safe_filename(name)}-{_stamp()}.arenabot")},
        background=BackgroundTask(_unlink, target),
    )


async def _receive_upload(request: Request) -> Path:
    """The request body, streamed to a temporary file (never held in memory), at most ``MAX_ARENABOT_BYTES``."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_ARENABOT_BYTES:
        raise BotError(".arenabot 文件超过了 20 MiB 的上限。", status=413, code="too_large")
    path = _tmp_dir() / f"import-{secrets.token_hex(6)}.upload"
    size = 0
    try:
        with open(path, "wb") as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_ARENABOT_BYTES:
                    raise BotError(".arenabot 文件超过了 20 MiB 的上限。", status=413, code="too_large")
                await asyncio.to_thread(stream.write, chunk)
    except BaseException:
        _unlink(path)
        raise
    if size == 0:
        _unlink(path)
        raise BotError("没有收到文件内容。")
    return path


@router.post("/bots/{bot_id}/import", dependencies=TRUSTED)
@_guarded
async def import_bot(bot_id: str, request: Request, name: str = Query(default="", max_length=300)) -> Any:
    """Replace this bot - name, avatar, L0, memory, record, proposals - by the ``.arenabot`` file in the request body.  The id in the
    file is ignored; a name that another bot has is 409 ``name_taken``; a file that is not acceptable is 400 / 422."""
    _require_idle("导入（会换掉名字和头像）")
    _check_busy([bot_id])
    codec = _codec()
    store = get_bot_store()
    await _run(store.name_of, bot_id)  # (404 before the file is read)
    upload = await _receive_upload(request)
    workdir = Path(tempfile.mkdtemp(prefix="import-", dir=_tmp_dir()))
    try:
        try:
            staged = await _run(codec.unpack_arenabot, upload, workdir)
            await _run(store.install_bot_dir, bot_id, staged)
        except BotFileError as exc:
            return _error(422, _problems_detail("这个 bot 文件不能导入：", exc), problems=exc.problems)
    finally:
        _unlink(upload)
        shutil.rmtree(workdir, ignore_errors=True)
    await _changed(roster=True)
    return await _detail(bot_id)


@router.post("/bots/{bot_id}/import-default", dependencies=TRUSTED)
@_guarded
async def import_default(bot_id: str, req: ImportDefaultRequest) -> Any:
    """Replace this bot by one of the default library (``{"name": "Alice"}``): name, avatar and L0 become the library's."""
    _require_idle("导入（会换掉名字和头像）")
    _check_busy([bot_id])
    try:
        await _run(get_bot_store().import_default, bot_id, req.name)
    except BotFileError as exc:
        return _error(422, _problems_detail("这个默认 bot 不能导入：", exc), problems=exc.problems)
    await _changed(roster=True)
    return await _detail(bot_id)


# ======================================================================================================================
# the moderator
# ======================================================================================================================
@router.put("/moderator", dependencies=TRUSTED)
@_guarded
async def put_moderator(req: ModeratorUpdate) -> Any:
    """The moderator is not a bot: its avatar is the only thing to choose (its name, persona and memory are fixed)."""
    if req.model_extra:
        raise BotError("主持人只能更换头像，名字、性格设定和记忆都是固定的。")
    if req.avatar is None:
        raise BotError("请给出头像描述（avatar）。")
    _require_idle("头像")
    moderator = await _run(get_bot_store().set_moderator_avatar, req.avatar)
    await _changed(roster=True)
    return moderator


@router.post("/moderator/avatar:regenerate", dependencies=TRUSTED)
@_guarded
async def regenerate_moderator_avatar() -> Any:
    _require_idle("头像")
    moderator = await _run(get_bot_store().regenerate_moderator_avatar)
    await _changed(roster=True)
    return moderator


__all__ = [
    "GameActive",
    "InitBusy",
    "current_sources",
    "router",
    "stub_bot_detail",
    "stub_bots_payload",
]
