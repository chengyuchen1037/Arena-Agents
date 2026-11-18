"""Per-actor model bindings: defaults and validation.

A binding names a Provider/model/reasoning depth by *id*; the secrets (key, URL) live only in the
encrypted provider store.  This module never creates or edits providers — an imported or restored
game whose bindings cannot be resolved locally is reported as "needs rebinding", and nothing
silently falls back to another provider.

v0.12: the moderator is an LLM agent like every player, so its binding is always required.
"""

from __future__ import annotations

import logging
from typing import Any

from app.game.session import MODERATOR_ID, BindingIssue, GameSession
from app.providers.models import ModelBinding

logger = logging.getLogger(__name__)


class BindingError(RuntimeError):
    """One or more actors have no usable model binding ("需要重新绑定")."""

    def __init__(self, issues: list[BindingIssue]) -> None:
        self.issues = issues
        super().__init__(format_issues(issues))


def format_issues(issues: list[BindingIssue]) -> str:
    if not issues:
        return ""
    shown = "；".join(f"{issue.actor_id}：{issue.message}" for issue in issues[:6])
    more = f"（另有 {len(issues) - 6} 项）" if len(issues) > 6 else ""
    return f"需要重新绑定模型后才能继续：{shown}{more}"


def load_default_bindings() -> dict[str, ModelBinding]:
    """The user's saved default bindings; an unreadable settings file yields none (logged)."""
    try:
        from app.storage.settings import get_default_bindings

        return dict(get_default_bindings())
    except Exception:  # noqa: BLE001 - a broken settings store must not break game creation
        logger.warning("Could not load default model bindings", exc_info=True)
        return {}


def required_actors(session: GameSession) -> list[str]:
    """Every agent calls a model: the moderator and all players."""
    return [MODERATOR_ID, *(p.id for p in session.participants)]


def actor_label(session: GameSession, actor_id: str) -> str:
    if actor_id == MODERATOR_ID:
        return "主持人"
    participant = next((p for p in session.participants if p.id == actor_id), None)
    return f"{participant.name}（{actor_id}）" if participant else actor_id


def normalized_binding(binding: ModelBinding, entry: Any) -> ModelBinding:
    """Binding whose context window never exceeds what the model actually supports."""
    window = min(int(binding.context_window_tokens), int(entry.context_window_tokens))
    return binding.model_copy(update={"context_window_tokens": max(1, window)})


def fill_missing_from_defaults(session: GameSession, defaults: dict[str, ModelBinding]) -> None:
    """Give actors that have no binding the user's default one (ids only, no provider created)."""
    for actor_id in required_actors(session):
        if actor_id not in session.model_bindings and actor_id in defaults:
            session.model_bindings[actor_id] = defaults[actor_id].model_copy(deep=True)


def compute_binding_issues(
    session: GameSession,
    provider_store: Any,
    cfg: dict[str, Any] | None = None,
) -> list[BindingIssue]:
    """Resolve every binding the game will use; return what is missing or unusable."""
    from app.providers.models import ModelNotFound, ProviderError, ProviderNotFound

    issues: list[BindingIssue] = []
    for actor_id in required_actors(session):
        label = actor_label(session, actor_id)
        binding = session.get_model_binding(actor_id)
        if binding is None:
            issues.append(BindingIssue(actor_id=actor_id, message=f"{label} 尚未选择模型"))
            continue
        try:
            provider, _entry = provider_store.resolve(binding)
        except ProviderNotFound:
            issues.append(BindingIssue(actor_id=actor_id, message=f"{label} 绑定的 Provider 在本机不存在"))
            continue
        except ModelNotFound:
            issues.append(
                BindingIssue(actor_id=actor_id, message=f"{label} 绑定的模型 {binding.model_id} 在该 Provider 中不存在")
            )
            continue
        except ProviderError as exc:
            issues.append(BindingIssue(actor_id=actor_id, message=f"{label} 的模型绑定无效：{exc}"))
            continue
        except Exception:  # noqa: BLE001 - storage problems are reported, not raised into the API
            logger.warning("Could not resolve binding for %s", actor_id, exc_info=True)
            issues.append(BindingIssue(actor_id=actor_id, message=f"{label} 的 Provider 配置无法读取"))
            continue
        if not str(getattr(provider, "api_key", "") or "").strip():
            name = str(getattr(provider, "name", "") or getattr(provider, "id", ""))
            message = f"{label} 的 Provider「{name}」未设置 API Key"
            if getattr(provider_store, "keys_error", None):
                message += "（已保存的密钥无法解密：主密钥缺失或不匹配）"
            issues.append(BindingIssue(actor_id=actor_id, message=message))
    return issues


def context_window_for(session: GameSession, actor_id: str | None) -> int:
    """Context window of the actor's binding (1M tokens without one)."""
    binding = session.get_model_binding(actor_id) if actor_id else None
    if binding is not None and binding.context_window_tokens:
        return int(binding.context_window_tokens)
    return 1_000_000
