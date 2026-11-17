"""Provider registry, model catalogue, model bindings and stored-session endpoints (v0.10).

Security rules carried over from v0.9: every route sits behind
``verify_api_access`` (token + Origin), every write additionally requires
``require_trusted_client`` (loopback unless a token is configured), errors are
plain ``{"detail": "..."}`` strings, and API keys are never returned or logged.
"""

from __future__ import annotations

import inspect
import logging
import math
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.security import (
    redact,
    require_trusted_client,
    validate_display_text,
    validate_provider_base_url,
    validate_secret,
    verify_api_access,
)
from app.game.orchestrator import orchestrator
from app.game.session import SESSION_ID_PATTERN
from app.llm.discovery import DiscoveryError, discover_models
from app.llm.endpoints import EndpointError, build_endpoints, endpoint_preview
from app.providers.models import (
    REASONING_DEPTHS,
    ModelBinding,
    ModelNotFound,
    ProviderError,
    ProviderNotFound,
)
from app.providers.store import get_provider_store
from app.storage.errors import StorageError
from app.storage.sessions import get_session_store
from app.storage.settings import get_default_bindings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])

ProtocolName = Literal["openai_chat", "openai_responses", "anthropic_messages"]
ACTOR_ID_PATTERN_TEXT = r"^[A-Za-z0-9_-]{1,64}$"
MAX_BINDINGS = 64
MAX_PRICE_PER_M = 1_000_000.0
TEMPLATE_SLOTS = 3
MAX_TEMPLATE_NAME_CHARS = 40


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------
class ProviderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    protocol: ProtocolName
    base_url: str
    api_key: str
    auto_append_path: bool = True


class ProviderUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    protocol: ProtocolName | None = None
    base_url: str | None = None
    api_key: str | None = None
    auto_append_path: bool | None = None


class EndpointPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: ProtocolName
    base_url: str
    auto_append_path: bool = True


class TemplateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: int = Field(ge=1, le=TEMPLATE_SLOTS)
    name: str
    provider_id: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=200)
    reasoning_depth: Literal["low", "medium", "high", "xhigh"] = "high"


class TemplatesUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    templates: list[TemplateItem] = Field(max_length=TEMPLATE_SLOTS)


class TemplateApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_ids: list[str] = Field(min_length=1, max_length=MAX_BINDINGS)


class ModelCreateRequest(BaseModel):
    # Both prices are required: there is deliberately no default price.
    model_id: str
    input_price_per_m: float = Field(ge=0, le=MAX_PRICE_PER_M, allow_inf_nan=False)
    output_price_per_m: float = Field(ge=0, le=MAX_PRICE_PER_M, allow_inf_nan=False)
    context_window_tokens: int | None = Field(default=None, ge=2000, le=100_000_000)


class ModelUpdateRequest(BaseModel):
    model_id: str
    input_price_per_m: float | None = Field(default=None, ge=0, le=MAX_PRICE_PER_M, allow_inf_nan=False)
    output_price_per_m: float | None = Field(default=None, ge=0, le=MAX_PRICE_PER_M, allow_inf_nan=False)
    context_window_tokens: int | None = Field(default=None, ge=2000, le=100_000_000)


class ModelDeleteRequest(BaseModel):
    model_id: str


class BindingsUpdateRequest(BaseModel):
    bindings: dict[str, ModelBinding]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _game_running() -> bool:
    return getattr(orchestrator.session, "status", "") == "running"


def serialize_model(model: Any) -> dict[str, Any]:
    return {
        "id": model.id,
        "input_price_per_m": model.input_price_per_m,
        "output_price_per_m": model.output_price_per_m,
        "context_window_tokens": model.context_window_tokens,
    }


def serialize_provider(provider: Any) -> dict[str, Any]:
    """Whitelist serialisation. The api_key is deliberately never copied."""

    return {
        "id": provider.id,
        "name": provider.name,
        "protocol": provider.protocol,
        "base_url": provider.base_url,
        "models": [serialize_model(m) for m in provider.models],
        "auto_append_path": bool(getattr(provider, "auto_append_path", True)),
        "read_only": bool(provider.read_only),
        "api_key_set": bool(provider.api_key),
    }


def serialize_binding(binding: Any) -> dict[str, Any]:
    if isinstance(binding, dict):
        data = binding
    else:
        data = binding.model_dump()
    return {
        "provider_id": data.get("provider_id"),
        "model_id": data.get("model_id"),
        "reasoning_depth": data.get("reasoning_depth"),
        "context_window_tokens": data.get("context_window_tokens"),
    }


def _binding_field(binding: Any, name: str) -> Any:
    return binding.get(name) if isinstance(binding, dict) else getattr(binding, name, None)


def _http_error(exc: Exception) -> HTTPException:
    """Map store/domain errors to HTTP errors without leaking secrets."""

    if isinstance(exc, (ProviderNotFound, ModelNotFound)):
        return HTTPException(status_code=404, detail=str(exc) or "未找到。")
    if isinstance(exc, StorageError):
        logger.error("Storage failure: %s", type(exc).__name__)
        return HTTPException(status_code=503, detail=str(exc) or "存储不可用。")
    if isinstance(exc, ProviderError):
        return HTTPException(status_code=400, detail=str(exc) or "无效的 Provider 设置。")
    raise exc


def _get_provider(provider_id: str) -> Any:
    try:
        return get_provider_store().get_provider(provider_id)
    except (ProviderError, StorageError) as exc:
        raise _http_error(exc) from exc


def _ensure_writable(provider: Any) -> None:
    if provider.read_only:
        raise HTTPException(
            status_code=403,
            detail="该 Provider 由环境变量/Secret 托管（只读），不能在界面修改或删除。",
        )


def session_bindings() -> dict[str, Any]:
    return dict(getattr(orchestrator.session, "model_bindings", None) or {})


def default_bindings() -> dict[str, Any]:
    try:
        return dict(get_default_bindings() or {})
    except Exception:  # noqa: BLE001 - settings problems must not break reads
        logger.warning("Could not read default model bindings", exc_info=True)
        return {}


def _participant_ids() -> list[str]:
    return [p.id for p in getattr(orchestrator.session, "participants", [])]


def actor_ids() -> list[str]:
    return ["moderator", *_participant_ids()]


def effective_binding(actor_id: str) -> Any | None:
    getter = getattr(orchestrator, "get_model_binding", None)
    if getter is not None:
        try:
            return getter(actor_id)
        except Exception:  # noqa: BLE001 - treated as "not bound"
            return None
    return session_bindings().get(actor_id) or default_bindings().get(actor_id)


def binding_issues() -> list[dict[str, str]]:
    """Actors whose binding is missing or no longer resolves ('需要重新绑定')."""

    issues: list[dict[str, str]] = []
    store = get_provider_store()
    for actor in actor_ids():
        binding = effective_binding(actor)
        if binding is None:
            issues.append({"actor_id": actor, "message": "尚未绑定模型"})
            continue
        try:
            store.resolve(
                binding if isinstance(binding, ModelBinding) else ModelBinding.model_validate(binding)
            )
        except (ProviderError, ValueError):
            issues.append({"actor_id": actor, "message": "绑定的 Provider 或模型已不存在，需要重新绑定"})
    return issues


def _settings_store() -> Any:
    from app.storage import settings as settings_module

    return settings_module.get_settings_store()


def _template_dict(item: Any) -> dict[str, Any]:
    data = item if isinstance(item, dict) else item.model_dump(mode="json")
    return {
        "slot": data.get("slot"),
        "name": data.get("name"),
        "provider_id": data.get("provider_id"),
        "model_id": data.get("model_id"),
        "reasoning_depth": data.get("reasoning_depth", "high"),
    }


def get_model_templates() -> list[dict[str, Any]]:
    """The saved templates as plain dicts, ordered by slot ([] when the store has none).

    Storage failures propagate (StorageError -> 503): callers that guard deletions must not
    treat "unreadable" as "unreferenced".
    """

    getter = getattr(_settings_store(), "get_model_templates", None)
    if getter is None:
        return []
    return sorted((_template_dict(t) for t in (getter() or [])), key=lambda t: t["slot"] or 0)


def save_model_templates(templates: list[dict[str, Any]]) -> None:
    saver = getattr(_settings_store(), "save_model_templates", None)
    if saver is None:
        raise HTTPException(status_code=501, detail="存储层尚不支持模型模板。")
    saver(templates)


def _references(provider_id: str, model_id: str | None = None, *, session_only: bool = False) -> list[str]:
    """Actor ids (current session) / default keys / template slots referencing a provider or model."""

    sources: list[tuple[str, dict[str, Any]]] = [("", session_bindings())]
    if not session_only:
        sources.append(("默认·", default_bindings()))
    found: list[str] = []
    for prefix, mapping in sources:
        for actor, binding in mapping.items():
            if _binding_field(binding, "provider_id") != provider_id:
                continue
            if model_id is not None and _binding_field(binding, "model_id") != model_id:
                continue
            found.append(f"{prefix}{actor}")
    if not session_only:
        for template in get_model_templates():
            if template["provider_id"] != provider_id:
                continue
            if model_id is not None and template["model_id"] != model_id:
                continue
            found.append(f"模板{template['slot']}")
    return found


def _in_use_error(what: str, refs: list[str]) -> HTTPException:
    shown = "、".join(refs[:6]) + ("…" if len(refs) > 6 else "")
    return HTTPException(
        status_code=409,
        detail=f"{what}仍被模型绑定或模板引用（{shown}），请先重新绑定/修改模板后再删除。",
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------
@router.get("/providers")
async def list_providers() -> dict[str, Any]:
    try:
        providers = get_provider_store().list_providers()
    except (ProviderError, StorageError) as exc:
        raise _http_error(exc) from exc
    return {"providers": [serialize_provider(p) for p in providers]}


def _validate_endpoint(protocol: str, base_url: str, auto_append_path: bool) -> str:
    """Security-validate the address, then check it builds a valid endpoint plan (400 on failure)."""

    url = validate_provider_base_url(base_url, allow_query=not auto_append_path)
    try:
        build_endpoints(protocol, url, auto_append_path)
    except EndpointError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Base URL 无效。") from exc
    return url


@router.post("/providers/endpoint-preview")
async def preview_endpoint(req: EndpointPreviewRequest) -> dict[str, Any]:
    """The final message / model-discovery URLs for a form that is being filled in.

    Pure computation (no network, nothing stored); it applies the same checks as saving, so
    a blocked address is flagged while typing.
    """

    url = validate_provider_base_url(req.base_url, allow_query=not req.auto_append_path)
    try:
        preview = endpoint_preview(req.protocol, url, req.auto_append_path)
    except EndpointError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Base URL 无效。") from exc
    return {
        "auto_append_path": bool(preview.get("auto_append_path", req.auto_append_path)),
        "request_url": preview.get("request_url"),
        "models_url": preview.get("models_url"),
        "warnings": list(preview.get("warnings") or []),
    }


@router.post("/providers", dependencies=[Depends(require_trusted_client)], status_code=201)
async def create_provider(req: ProviderCreateRequest) -> dict[str, Any]:
    name = validate_display_text("名称", req.name)
    base_url = _validate_endpoint(req.protocol, req.base_url, req.auto_append_path)
    api_key = validate_secret("API Key", req.api_key)
    if not api_key:
        raise HTTPException(status_code=400, detail="API Key 不能为空。")
    try:
        provider = get_provider_store().create_provider(
            name, req.protocol, base_url, api_key, auto_append_path=req.auto_append_path
        )
    except (ProviderError, StorageError) as exc:
        raise _http_error(exc) from exc
    return serialize_provider(provider)


@router.patch("/providers/{provider_id}", dependencies=[Depends(require_trusted_client)])
async def update_provider(provider_id: str, req: ProviderUpdateRequest) -> dict[str, Any]:
    provider = _get_provider(provider_id)
    _ensure_writable(provider)

    changes: dict[str, Any] = {}
    if req.name is not None:
        changes["name"] = validate_display_text("名称", req.name)
    current_auto = bool(getattr(provider, "auto_append_path", True))
    new_protocol = req.protocol if req.protocol is not None else provider.protocol
    new_auto = req.auto_append_path if req.auto_append_path is not None else current_auto
    if new_protocol != provider.protocol:
        changes["protocol"] = new_protocol
    if new_auto != current_auto:
        changes["auto_append_path"] = new_auto
    new_url = provider.base_url
    if req.base_url is not None:
        new_url = req.base_url
    endpoint_touched = req.base_url is not None or "protocol" in changes or "auto_append_path" in changes
    if endpoint_touched:
        # Validate the *resulting* combination (e.g. switching auto-append on for a URL that
        # already ends in /chat/completions must fail now, not at the first request).
        new_url = _validate_endpoint(new_protocol, new_url, new_auto)
        if new_url != provider.base_url:
            changes["base_url"] = new_url
    api_key = validate_secret("API Key", req.api_key) if req.api_key is not None else ""

    # Any endpoint change (protocol / base_url / auto_append_path) would send the stored key
    # to a different place, so the key must be typed again.
    redirecting = any(k in changes for k in ("protocol", "base_url", "auto_append_path"))
    if redirecting and not api_key:
        raise HTTPException(
            status_code=400,
            detail="修改协议、Base URL 或“自动拼接路径”时必须同时重新输入 API Key（防止已保存的密钥被转发到新地址）。",
        )
    if redirecting and _game_running() and _references(provider_id, session_only=True):
        raise HTTPException(
            status_code=409,
            detail="对局进行中，正在使用的 Provider 不能修改协议、Base URL 或自动拼接路径；可单独轮换 API Key，或先暂停对局。",
        )
    if not changes and not api_key:
        return serialize_provider(provider)

    kwargs: dict[str, Any] = dict(changes)
    if api_key:
        kwargs["api_key"] = api_key
    try:
        updated = get_provider_store().update_provider(provider_id, **kwargs)
    except (ProviderError, StorageError) as exc:
        raise _http_error(exc) from exc
    return serialize_provider(updated)


@router.delete("/providers/{provider_id}", dependencies=[Depends(require_trusted_client)])
async def delete_provider(provider_id: str) -> dict[str, Any]:
    provider = _get_provider(provider_id)
    _ensure_writable(provider)
    refs = _references(provider_id)
    if refs:
        raise _in_use_error("该 Provider ", refs)
    try:
        get_provider_store().delete_provider(provider_id)
    except (ProviderError, StorageError) as exc:
        raise _http_error(exc) from exc
    return {"ok": True}


@router.post("/providers/{provider_id}/discover-models", dependencies=[Depends(require_trusted_client)])
async def discover_provider_models(provider_id: str) -> dict[str, Any]:
    provider = _get_provider(provider_id)
    if not bool(getattr(provider, "auto_append_path", True)):
        # Manual-endpoint provider: there is no models URL to ask. Refuse before any network use.
        raise HTTPException(
            status_code=400,
            detail="该 Provider 已关闭“自动拼接路径”（Base URL 是完整的消息地址），无法自动获取模型；请手动填写模型 ID。",
        )
    try:
        models = await discover_models(provider)
    except DiscoveryError as exc:
        # Policy problems (bad/blocked address, manual endpoint) are the caller's fault; upstream
        # failures (auth, status, timeout, ...) are a bad gateway.
        status = (
            400
            if exc.kind in ("invalid_url", "blocked", "manual_endpoint")
            else 504
            if exc.kind == "timeout"
            else 502
        )
        raise HTTPException(
            status_code=status, detail=redact(str(exc), [provider.api_key]) or "获取模型失败。"
        ) from exc
    except ProviderError as exc:
        raise HTTPException(
            status_code=400, detail=redact(str(exc), [provider.api_key]) or "获取模型失败。"
        ) from exc
    except (ValueError, RuntimeError, OSError) as exc:
        logger.warning("Model discovery failed for provider %s: %s", provider.id, type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail=redact(f"获取模型失败：{exc}", [provider.api_key]),
        ) from exc
    names = [str(m) for m in models][:1000]
    existing = {m.id for m in provider.models}
    return {"models": names, "existing_models": [m for m in names if m in existing]}


# --------------------------------------------------------------------------
# Models (one at a time, price required)
# --------------------------------------------------------------------------
@router.post("/providers/{provider_id}/models", dependencies=[Depends(require_trusted_client)], status_code=201)
async def add_provider_model(provider_id: str, req: ModelCreateRequest) -> dict[str, Any]:
    model_id = validate_display_text("模型 ID", req.model_id, 200)
    for label, value in (("输入价格", req.input_price_per_m), ("输出价格", req.output_price_per_m)):
        if not math.isfinite(value) or value < 0:
            raise HTTPException(status_code=400, detail=f"{label}必须是有限的非负数。")
    provider = _get_provider(provider_id)
    _ensure_writable(provider)
    if any(m.id == model_id for m in provider.models):
        raise HTTPException(status_code=409, detail=f"模型 {model_id} 已存在，请直接编辑它的价格。")
    kwargs: dict[str, Any] = {}
    if req.context_window_tokens is not None:
        kwargs["context_window_tokens"] = req.context_window_tokens
    try:
        entry = get_provider_store().add_model(
            provider_id, model_id, req.input_price_per_m, req.output_price_per_m, **kwargs
        )
    except (ProviderError, StorageError) as exc:
        raise _http_error(exc) from exc
    return serialize_model(entry)


@router.patch("/providers/{provider_id}/models", dependencies=[Depends(require_trusted_client)])
async def update_provider_model(provider_id: str, req: ModelUpdateRequest) -> dict[str, Any]:
    model_id = validate_display_text("模型 ID", req.model_id, 200)
    changes = {
        key: value
        for key, value in (
            ("input_price_per_m", req.input_price_per_m),
            ("output_price_per_m", req.output_price_per_m),
            ("context_window_tokens", req.context_window_tokens),
        )
        if value is not None
    }
    if not changes:
        raise HTTPException(status_code=400, detail="没有可修改的字段（价格或上下文窗口）。")
    provider = _get_provider(provider_id)
    _ensure_writable(provider)
    if not any(m.id == model_id for m in provider.models):
        raise HTTPException(status_code=404, detail=f"模型 {model_id} 不存在。")
    # Price edits and context-window edits are allowed while a game runs: every
    # LLM call carries its own price snapshot and bindings keep their own window.
    try:
        entry = get_provider_store().update_model(provider_id, model_id, **changes)
    except (ProviderError, StorageError) as exc:
        raise _http_error(exc) from exc
    return serialize_model(entry)


@router.delete("/providers/{provider_id}/models", dependencies=[Depends(require_trusted_client)])
async def remove_provider_model(provider_id: str, req: ModelDeleteRequest) -> dict[str, Any]:
    model_id = validate_display_text("模型 ID", req.model_id, 200)
    provider = _get_provider(provider_id)
    _ensure_writable(provider)
    if not any(m.id == model_id for m in provider.models):
        raise HTTPException(status_code=404, detail=f"模型 {model_id} 不存在。")
    refs = _references(provider_id, model_id)
    if refs:
        raise _in_use_error(f"模型 {model_id} ", refs)
    try:
        get_provider_store().remove_model(provider_id, model_id)
    except (ProviderError, StorageError) as exc:
        raise _http_error(exc) from exc
    return {"ok": True}


# --------------------------------------------------------------------------
# Bindings
# --------------------------------------------------------------------------
def bindings_payload() -> dict[str, Any]:
    current = session_bindings()
    defaults = default_bindings()
    effective: dict[str, Any] = {}
    for actor in actor_ids():
        binding = current.get(actor) or defaults.get(actor)
        if binding is not None:
            effective[actor] = serialize_binding(binding)
    try:
        providers = [serialize_provider(p) for p in get_provider_store().list_providers()]
    except (ProviderError, StorageError):
        providers = []
    return {
        "bindings": effective,
        "default_bindings": {actor: serialize_binding(b) for actor, b in defaults.items()},
        "providers": providers,
        "actors": actor_ids(),
        "reasoning_depths": list(REASONING_DEPTHS),
        "locked": _game_running(),
        "issues": binding_issues(),
    }


@router.get("/settings/bindings")
async def get_bindings() -> dict[str, Any]:
    try:
        return bindings_payload()
    except (ProviderError, StorageError) as exc:
        raise _http_error(exc) from exc


@router.put("/settings/bindings", dependencies=[Depends(require_trusted_client)])
async def put_bindings(req: BindingsUpdateRequest) -> dict[str, Any]:
    if _game_running():
        raise HTTPException(
            status_code=409,
            detail="对局进行中，模型与思考深度绑定已锁定；请先暂停对局。",
        )
    if not req.bindings:
        raise HTTPException(status_code=400, detail="bindings 不能为空。")
    if len(req.bindings) > MAX_BINDINGS:
        raise HTTPException(status_code=400, detail=f"一次最多设置 {MAX_BINDINGS} 个绑定。")
    known = set(actor_ids())
    store = get_provider_store()
    for actor, binding in req.bindings.items():
        if actor not in known:
            raise HTTPException(status_code=400, detail=f"未知的角色 ID：{actor}")
        if binding.reasoning_depth not in REASONING_DEPTHS:
            raise HTTPException(status_code=400, detail=f"无效的思考深度：{binding.reasoning_depth}")
        if not 2000 <= binding.context_window_tokens <= 100_000_000:
            raise HTTPException(status_code=400, detail=f"{actor} 的上下文窗口必须在 2000 到 100000000 之间。")
        try:
            store.resolve(binding)
        except (ProviderError, StorageError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"{actor} 的绑定无效：Provider 或模型不存在。"
                if isinstance(exc, (ProviderNotFound, ModelNotFound))
                else f"{actor} 的绑定无效：{exc}",
            ) from exc
    updater = getattr(orchestrator, "update_model_bindings", None)
    if updater is None:
        raise HTTPException(status_code=501, detail="引擎尚不支持模型绑定。")
    try:
        await _maybe_await(updater(dict(req.bindings)))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc) or "当前状态不允许修改绑定。") from exc
    except (ProviderError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return bindings_payload()
    except (ProviderError, StorageError) as exc:
        raise _http_error(exc) from exc


# --------------------------------------------------------------------------
# Model templates (3 fixed slots: provider + model + reasoning depth, no prices)
# --------------------------------------------------------------------------
def _template_binding(template: dict[str, Any]) -> tuple[ModelBinding, Any]:
    """Resolve a template against the provider store -> (binding, model entry).

    Raises ProviderError subclasses when the provider/model no longer exists.
    """

    binding = ModelBinding(
        provider_id=template["provider_id"],
        model_id=template["model_id"],
        reasoning_depth=template.get("reasoning_depth") or "high",
    )
    _, entry = get_provider_store().resolve(binding)
    return binding.model_copy(update={"context_window_tokens": entry.context_window_tokens}), entry


def _template_valid(template: dict[str, Any]) -> bool:
    try:
        _template_binding(template)
    except (ProviderError, ValueError):
        return False
    return True


def templates_payload() -> dict[str, Any]:
    templates = []
    for template in get_model_templates():
        item = dict(template)
        item["valid"] = _template_valid(template)
        templates.append(item)
    return {"templates": templates, "slots": TEMPLATE_SLOTS, "locked": _game_running()}


@router.get("/settings/templates")
async def get_templates() -> dict[str, Any]:
    try:
        return templates_payload()
    except (ProviderError, StorageError) as exc:
        raise _http_error(exc) from exc


@router.put("/settings/templates", dependencies=[Depends(require_trusted_client)])
async def put_templates(req: TemplatesUpdateRequest) -> dict[str, Any]:
    """Replace the saved templates with exactly these slots (omitted slots are cleared).

    Allowed while a game runs: editing a template never changes a live binding.
    """

    slots = [t.slot for t in req.templates]
    if len(set(slots)) != len(slots):
        raise HTTPException(status_code=400, detail="模板槽位不能重复。")
    cleaned: list[dict[str, Any]] = []
    for item in sorted(req.templates, key=lambda t: t.slot):
        data = {
            "slot": item.slot,
            "name": validate_display_text(f"模板{item.slot}的名称", item.name, MAX_TEMPLATE_NAME_CHARS),
            "provider_id": item.provider_id.strip(),
            "model_id": item.model_id,
            "reasoning_depth": item.reasoning_depth,
        }
        try:
            _template_binding(data)
        except (ProviderError, StorageError) as exc:
            if isinstance(exc, StorageError):
                raise _http_error(exc) from exc
            raise HTTPException(
                status_code=400,
                detail=f"模板{item.slot}引用的 Provider 或模型不存在，请重新选择。",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"模板{item.slot}无效。") from exc
        cleaned.append(data)
    try:
        save_model_templates(cleaned)
    except (ProviderError, StorageError) as exc:
        raise _http_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="模板无效，未保存。") from exc
    return templates_payload()


@router.post("/settings/templates/{slot}/apply", dependencies=[Depends(require_trusted_client)])
async def apply_template(slot: int, req: TemplateApplyRequest) -> dict[str, Any]:
    if not 1 <= slot <= TEMPLATE_SLOTS:
        raise HTTPException(status_code=404, detail=f"模板槽位必须是 1 到 {TEMPLATE_SLOTS}。")
    if _game_running():
        raise HTTPException(
            status_code=409,
            detail="对局进行中，模型与思考深度绑定已锁定，不能应用模板；请先暂停对局。",
        )
    known = set(actor_ids())
    actors = list(dict.fromkeys(a for a in req.actor_ids))  # de-duplicate, keep order
    for actor in actors:
        if actor not in known:
            raise HTTPException(status_code=400, detail=f"未知的角色 ID：{actor}")
    template = next((t for t in get_model_templates() if t["slot"] == slot), None)
    if template is None:
        raise HTTPException(status_code=404, detail=f"模板{slot}还没有保存。")
    try:
        binding, _ = _template_binding(template)
    except StorageError as exc:
        raise _http_error(exc) from exc
    except (ProviderError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"模板{slot}引用的 Provider 或模型已不存在，请先修改模板。",
        ) from exc
    updater = getattr(orchestrator, "update_model_bindings", None)
    if updater is None:
        raise HTTPException(status_code=501, detail="引擎尚不支持模型绑定。")
    try:
        await _maybe_await(updater({actor: binding for actor in actors}))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc) or "当前状态不允许修改绑定。") from exc
    except (ProviderError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return bindings_payload()
    except (ProviderError, StorageError) as exc:
        raise _http_error(exc) from exc


# --------------------------------------------------------------------------
# Stored sessions
# --------------------------------------------------------------------------
def _clean_session_meta(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"session_id": str(item)}
    # Metadata only; never expose filesystem locations.
    return {
        key: value
        for key, value in item.items()
        if "path" not in str(key).lower() and "dir" not in str(key).lower()
    }


@router.get("/sessions")
async def list_sessions() -> dict[str, Any]:
    try:
        items = get_session_store().list_sessions()
    except StorageError as exc:
        raise _http_error(exc) from exc
    current_id = str(getattr(orchestrator.session, "session_id", ""))
    sessions = []
    for item in items:
        meta = _clean_session_meta(item)
        meta["current"] = str(meta.get("session_id", "")) == current_id
        sessions.append(meta)
    return {"sessions": sessions}


def check_session_id(session_id: str) -> str:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="无效的 session_id。")
    return session_id


@router.delete("/sessions/{session_id}", dependencies=[Depends(require_trusted_client)])
async def delete_session(session_id: str) -> dict[str, Any]:
    check_session_id(session_id)
    if session_id == str(getattr(orchestrator.session, "session_id", "")):
        raise HTTPException(
            status_code=409,
            detail="这是当前对局，不能直接删除；请先新建或加载其他对局。",
        )
    store = get_session_store()
    try:
        if not store.exists(session_id):
            raise HTTPException(status_code=404, detail="存档不存在。")
        store.delete(session_id)
    except StorageError as exc:
        raise _http_error(exc) from exc
    except ValueError as exc:  # reserved names such as CON/NUL
        raise HTTPException(status_code=400, detail="无效的 session_id（保留名称或非法字符）。") from exc
    return {"ok": True}
