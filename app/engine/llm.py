"""Engine-facing LLM client: binding routing, client caching and retries.

The protocol work lives in :mod:`app.llm` (``openai_chat``, ``openai_responses``,
``anthropic_messages``). This module keeps the long-standing import surface
(``LLMClient``, ``LLMResponse``, ``ToolCallData`` ...) for the engine.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Callable

from app.llm.base import ProtocolAdapter
from app.llm.common import (
    DEFAULT_RETRY_AFTER_CAP,
    DEFAULT_RETRY_BASE_DELAY,
    DEFAULT_RETRY_JITTER,
    DEFAULT_RETRY_MAX_ATTEMPTS,
    DEFAULT_RETRY_MAX_DELAY,
    ClientCache,
    ClientHandle,
    RetryPolicy,
    StreamAccumulator,
    StreamProgress,
    as_float,
    as_int,
    is_retryable_error,
    redact_exception,
    retry_after_seconds,
    run_with_retry,
)
from app.llm.endpoints import EndpointError, build_endpoints
from app.llm.openai_chat import is_deepseek_host
from app.llm.registry import get_adapter
from app.llm.types import (
    ARGS_ERROR_KEY,
    PROVIDER_PRIVATE_KEY,
    LLMResponse,
    StreamCallback,
    Target,
    ToolCallData,
    args_error,
    estimate_text_tokens,
    extract_dsml_tool_calls,
    split_inline_think,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ARGS_ERROR_KEY",
    "PROVIDER_PRIVATE_KEY",
    "DEFAULT_BASE_URL",
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "DEFAULT_MODEL",
    "DEFAULT_RETRY_AFTER_CAP",
    "DEFAULT_RETRY_BASE_DELAY",
    "DEFAULT_RETRY_JITTER",
    "DEFAULT_RETRY_MAX_ATTEMPTS",
    "DEFAULT_RETRY_MAX_DELAY",
    "LLMClient",
    "LLMResponse",
    "StreamAccumulator",
    "StreamCallback",
    "Target",
    "ToolCallData",
    "args_error",
    "estimate_text_tokens",
    "extract_dsml_tool_calls",
    "is_retryable_error",
    "retry_after_seconds",
    "split_inline_think",
]

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
#: Keep in sync with ``config.yml`` (llm.context_window_tokens) and ``state.py``.
DEFAULT_CONTEXT_WINDOW_TOKENS = 1_000_000
#: Provider id used for the cfg-only fallback (no binding passed).
LEGACY_PROVIDER_ID = "legacy"


class LLMClient:
    """Streaming chat client for every supported protocol.

    ``chat_stream(..., binding=ModelBinding)`` resolves the binding through the
    provider store, picks the protocol adapter and keeps one SDK client per
    provider (rebuilt only when its base_url / api_key / protocol change; the old
    one is closed once idle). Without a binding the ``llm`` section of ``cfg`` is
    used (OpenAI Chat) - that path exists for legacy unit tests.
    """

    def __init__(self, cfg: dict[str, Any], *, provider_store: Any = None) -> None:
        self._cache = ClientCache()
        self._effort_user_set = False
        self._reasoning_effort = "high"
        self._random: Callable[[], float] = random.random
        #: Anything with ``resolve(binding) -> (provider, model)``; defaults to the
        #: process-wide ``ProviderStore`` (looked up lazily).
        self.provider_store = provider_store
        self._apply_config(cfg)

    # ------------------------------------------------------------------ config
    def _apply_config(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        llm_cfg = cfg.get("llm") or {}
        self.base_url: str = llm_cfg.get("base_url") or DEFAULT_BASE_URL
        self.api_key: str = llm_cfg.get("api_key") or ""
        self.default_model: str = llm_cfg.get("default_model") or DEFAULT_MODEL
        self.temperature: float = as_float(llm_cfg.get("temperature"), 0.7)
        self.max_tokens: int = as_int(llm_cfg.get("max_tokens"), 4096)
        self.timeout: float = as_float(llm_cfg.get("timeout_seconds"), 120.0)

        thinking_cfg = llm_cfg.get("thinking") or {}
        self.thinking_enabled: bool = bool(thinking_cfg.get("enabled", True))
        if not self._effort_user_set:
            self._reasoning_effort = str(thinking_cfg.get("reasoning_effort", "high"))
        self.extra_body: dict[str, Any] = dict(thinking_cfg.get("extra_body") or {})
        self.mod_override: dict[str, Any] = dict(llm_cfg.get("moderator_override") or {})

        pricing_cfg = llm_cfg.get("pricing") or {}
        self.input_price_per_m: float = as_float(pricing_cfg.get("input_price_per_m"), 0.27)
        self.output_price_per_m: float = as_float(pricing_cfg.get("output_price_per_m"), 1.10)
        self.context_window_tokens: int = as_int(
            llm_cfg.get("context_window_tokens"), DEFAULT_CONTEXT_WINDOW_TOKENS
        )

        retry_cfg = llm_cfg.get("retry") or {}
        policy = RetryPolicy.from_cfg(retry_cfg)
        self.retry_max_attempts: int = policy.max_attempts
        self.retry_base_delay: float = policy.base_delay
        self.retry_max_delay: float = policy.max_delay
        self.retry_jitter: float = policy.jitter
        self.retry_after_cap: float = policy.retry_after_cap

    def reconfigure(self, cfg: dict[str, Any]) -> None:
        """Re-read ``cfg`` in place.

        A reasoning effort set at runtime (``llm.reasoning_effort = ...``) is kept.
        SDK clients are rebuilt lazily, only if a provider's endpoint / key changed.
        """
        self._apply_config(cfg)

    @property
    def reasoning_effort(self) -> str:
        return self._reasoning_effort

    @reasoning_effort.setter
    def reasoning_effort(self, value: str) -> None:
        # An explicit (UI / session) choice wins over moderator_override.
        self._reasoning_effort = value
        self._effort_user_set = True

    def is_configured(self) -> bool:
        """Whether the cfg-level (legacy) key is usable."""
        return bool(
            self.api_key
            and self.api_key.strip()
            and self.api_key != "sk-your-deepseek-api-key-here"
        )

    @property
    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            max_attempts=self.retry_max_attempts,
            base_delay=self.retry_base_delay,
            max_delay=self.retry_max_delay,
            jitter=self.retry_jitter,
            retry_after_cap=self.retry_after_cap,
        )

    def _backoff_delay(self, failures: int, retry_after: float | None = None) -> float:
        return self.retry_policy.delay(failures, retry_after, self._random)

    # ------------------------------------------------------------ client reuse
    def _create_client(self, adapter: ProtocolAdapter, target: Target) -> Any:
        return adapter.create_client(target, self.timeout)

    async def _acquire_client(self, adapter: ProtocolAdapter, target: Target) -> ClientHandle:
        key = (*target.client_key, self.timeout)
        return await self._cache.acquire(
            target.provider_id, key, lambda: self._create_client(adapter, target)
        )

    async def aclose(self) -> None:
        """Release every shared HTTP client (new ones are created on next use)."""
        await self._cache.aclose()

    # ----------------------------------------------------------------- routing
    def _store(self) -> Any:
        if self.provider_store is not None:
            return self.provider_store
        try:
            from app.providers.store import get_provider_store
        except ImportError as exc:  # pragma: no cover - depends on deployment
            raise RuntimeError("Provider 存储不可用，无法解析模型绑定。") from exc
        return get_provider_store()

    def resolve_binding(self, binding: Any) -> tuple[Any, Any]:
        """``(ProviderConfig, ModelEntry)`` for ``binding`` (raises ProviderNotFound/ModelNotFound)."""
        return self._store().resolve(binding)

    def _moderator_temperature(self) -> float:
        return as_float(self.mod_override.get("temperature"), self.temperature)

    def _resolve_effort(self, is_moderator: bool) -> str:
        if is_moderator and not self._effort_user_set:
            override = self.mod_override.get("reasoning_effort")
            if override:
                return str(override)
        return self._reasoning_effort

    def build_target(self, binding: Any, *, is_moderator: bool = False) -> Target:
        """Resolve ``binding`` (or the cfg fallback when ``None``) into a :class:`Target`."""
        if binding is None:
            if not self.is_configured():
                raise RuntimeError(
                    "未配置有效的 DEEPSEEK_API_KEY。请在 .env 文件中设置 DEEPSEEK_API_KEY 后重试。"
                )
            model = (
                (self.mod_override.get("model") or self.default_model)
                if is_moderator
                else self.default_model
            )
            return Target(
                provider_id=LEGACY_PROVIDER_ID,
                protocol="openai_chat",
                base_url=self.base_url,
                api_key=self.api_key,
                model_id=str(model),
                input_price_per_m=self.input_price_per_m,
                output_price_per_m=self.output_price_per_m,
                context_window_tokens=self.context_window_tokens,
                reasoning_depth=self._resolve_effort(is_moderator),
                extra_body=dict(self.extra_body),
                thinking_enabled=self.thinking_enabled,
                temperature=self._moderator_temperature() if is_moderator else self.temperature,
                # v0.9 semantics for the cfg-only path: the SDK gets the URL verbatim.
                auto_append_path=True,
                request_url=self.base_url.rstrip("/") + "/chat/completions",
                models_url=self.base_url.rstrip("/") + "/models",
                sdk_base_url=self.base_url,
            )

        provider, model = self.resolve_binding(binding)
        api_key = str(getattr(provider, "api_key", "") or "")
        if not api_key.strip():
            raise RuntimeError(f"Provider「{provider.name}」尚未设置 API Key，请先在 Provider 管理中补充。")
        auto_append_path = bool(getattr(provider, "auto_append_path", True))
        try:
            plan = build_endpoints(provider.protocol, provider.base_url, auto_append_path)
        except EndpointError as exc:
            raise EndpointError(f"Provider「{provider.name}」的 Base URL 配置无效：{exc}") from None
        windows = [
            w
            for w in (
                as_int(getattr(binding, "context_window_tokens", 0), 0),
                as_int(getattr(model, "context_window_tokens", 0), 0),
            )
            if w > 0
        ]
        # Global extra_body (e.g. DeepSeek's ``thinking``) only suits DeepSeek endpoints.
        extra_body = (
            dict(self.extra_body)
            if provider.protocol == "openai_chat" and is_deepseek_host(provider.base_url)
            else {}
        )
        return Target(
            provider_id=provider.id,
            protocol=provider.protocol,
            base_url=provider.base_url,
            api_key=api_key,
            model_id=model.id,
            input_price_per_m=float(model.input_price_per_m),
            output_price_per_m=float(model.output_price_per_m),
            context_window_tokens=min(windows) if windows else DEFAULT_CONTEXT_WINDOW_TOKENS,
            reasoning_depth=str(getattr(binding, "reasoning_depth", "") or ""),
            extra_body=extra_body,
            thinking_enabled=self.thinking_enabled,
            temperature=self._moderator_temperature() if is_moderator else self.temperature,
            auto_append_path=plan.auto_append_path,
            request_url=plan.request_url,
            models_url=plan.models_url,
            sdk_base_url=plan.sdk_base_url,
        )

    # ------------------------------------------------------------------- chat
    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        is_moderator: bool = False,
        tools: list[dict[str, Any]] | None = None,
        on_chunk: StreamCallback | None = None,
        max_tokens_override: int | None = None,
        binding: Any = None,
    ) -> LLMResponse:
        target = self.build_target(binding, is_moderator=is_moderator)
        adapter = get_adapter(target.protocol)
        request = adapter.build_request(
            target,
            messages,
            tools,
            max_tokens=max_tokens_override or self.max_tokens,
            temperature=target.temperature,
        )

        handle = await self._acquire_client(adapter, target)
        try:

            async def attempt(progress: StreamProgress) -> LLMResponse:
                return await adapter.consume(handle.client, request, on_chunk, progress)

            def downgrade(exc: BaseException) -> bool:
                changed = adapter.downgrade(exc, request)
                if changed:
                    logger.warning(
                        "Upstream rejected optional parameters (%s); retrying with a standard body.",
                        target.protocol,
                    )
                return changed

            try:
                resp = await run_with_retry(
                    attempt,
                    policy=self.retry_policy,
                    downgrade=downgrade,
                    rand=lambda: self._random(),
                    label=f"LLM[{target.protocol}]",
                )
            except Exception as exc:
                redact_exception(exc, target.api_key)
                raise
        finally:
            await self._cache.release(handle)
        return self._stamp(resp, target)

    @staticmethod
    def _stamp(resp: LLMResponse, target: Target) -> LLMResponse:
        """Attach the model identity and the price / window snapshot to ``resp``."""
        resp.provider_id = target.provider_id
        resp.model_id = target.model_id
        resp.input_price_per_m = target.input_price_per_m
        resp.output_price_per_m = target.output_price_per_m
        resp.context_window_tokens = target.context_window_tokens
        if resp.provider_private:
            resp.provider_private = {
                **resp.provider_private,
                "provider_id": target.provider_id,
                "protocol": target.protocol,
                "model_id": target.model_id,
            }
        return resp
