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
    input_price_per_m: float
    output_price_per_m: float
    context_window_tokens: int = Field(
        default=DEFAULT_CONTEXT_WINDOW_TOKENS, gt=0
    )

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


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    protocol: ProtocolType
    base_url: str
    api_key: str = Field(repr=False)
    models: list[ModelEntry] = Field(default_factory=list)
    read_only: bool = False
    source: str = "encrypted"
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
