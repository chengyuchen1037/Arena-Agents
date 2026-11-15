"""Protocol name -> adapter."""

from __future__ import annotations

from app.llm.anthropic_messages import AnthropicMessagesAdapter
from app.llm.base import ProtocolAdapter
from app.llm.openai_chat import OpenAIChatAdapter
from app.llm.openai_responses import OpenAIResponsesAdapter

_ADAPTERS: dict[str, ProtocolAdapter] = {
    adapter.protocol: adapter
    for adapter in (OpenAIChatAdapter(), OpenAIResponsesAdapter(), AnthropicMessagesAdapter())
}


def get_adapter(protocol: str) -> ProtocolAdapter:
    try:
        return _ADAPTERS[protocol]
    except KeyError:
        raise ValueError(f"不支持的协议类型：{protocol!r}") from None


def supported_protocols() -> tuple[str, ...]:
    return tuple(_ADAPTERS)
