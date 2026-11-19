from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.storage.errors import StorageError

ProtocolType = Literal["openai_chat", "openai_responses", "anthropic_messages"]
ReasoningDepth = Literal["low", "medium", "high", "xhigh"]
REASONING_DEPTHS = ("low", "medium", "high", "xhigh")
DEFAULT_CONTEXT_WINDOW_TOKENS = 1_000_000


class ProviderError(ValueError):
    """A provider operation is invalid or is not permitted."""


class ProviderNotFound(ProviderError):
    """The requested provider does not exist."""


class ModelNotFound(ProviderError):
    """The requested model does not exist on the provider."""


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    input_price_per_m: float  # USD per million tokens that missed the provider's prompt cache
    output_price_per_m: float  # (reasoning tokens are billed as output)
    # v0.12.1: cache prices.  ``None`` = "bill it like a normal input token" (resolved by ``price_snapshot``).
    cache_read_price_per_m: float | None = None
    cache_write_price_per_m: float | None = None
    context_window_tokens: int = Field(
        default=DEFAULT_CONTEXT_WINDOW_TOKENS, gt=0
    )
    # Where the prices came from: "manual" or "litellm:<key of the price table>" (informational).
    price_source: str = "manual"

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model id must not be empty")
        return value

    @field_validator("input_price_per_m", "output_price_per_m")
    @classmethod
    def _validate_price(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("price must be finite and non-negative")
        return value

    @field_validator("cache_read_price_per_m", "cache_write_price_per_m")
    @classmethod
    def _validate_cache_price(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value) or value < 0:
            raise ValueError("price must be finite and non-negative")
        return value

    @field_validator("price_source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        value = (value or "manual").strip()
        return value[:200] or "manual"

    def price_snapshot(self) -> dict[str, float]:
        """The four prices as plain numbers (an empty cache price is billed like an input token)."""
        return {
            "input_price_per_m": float(self.input_price_per_m),
            "output_price_per_m": float(self.output_price_per_m),
            "cache_read_price_per_m": float(
                self.input_price_per_m if self.cache_read_price_per_m is None else self.cache_read_price_per_m
            ),
            "cache_write_price_per_m": float(
                self.input_price_per_m if self.cache_write_price_per_m is None else self.cache_write_price_per_m
            ),
        }


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    protocol: ProtocolType
    base_url: str
    #: The key lives in ``secrets.v1.enc``, never in ``providers.json``; ``""`` = not set (imported without keys)
    api_key: str = Field(default="", repr=False)
    models: list[ModelEntry] = Field(default_factory=list)
    read_only: bool = False
    #: a label: ``user`` (made in the app), ``environment`` (DEEPSEEK_*); ``encrypted`` / ``legacy-migration`` come
    #: from data of v0.12.1 and older and are kept as they are
    source: str = "user"
    auto_append_path: bool = True

    @field_validator("id", "name", "base_url")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value


class ModelBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model_id: str
    reasoning_depth: ReasoningDepth = "high"
    context_window_tokens: int = Field(
        default=DEFAULT_CONTEXT_WINDOW_TOKENS, gt=0
    )

    @field_validator("provider_id", "model_id")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("binding ids must not be empty")
        return value


class ModelTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: int = Field(ge=1, le=3)
    name: str
    provider_id: str
    model_id: str
    reasoning_depth: ReasoningDepth = "high"

    @field_validator("name", "provider_id", "model_id")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("template fields must not be empty")
        return value
