"""OpenAI Chat Completions adapter (also DeepSeek and most compatible gateways)."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from openai import AsyncOpenAI

from app.llm.base import ProtocolAdapter, sdk_endpoint_kwargs
from app.llm.common import (
    StreamAccumulator,
    StreamProgress,
    estimate_completion_tokens,
    openai_usage_parts,
)
from app.llm.messages import strip_private
from app.llm.types import LLMResponse, StreamCallback, Target, ToolCallData

logger = logging.getLogger(__name__)


def is_deepseek_host(base_url: str) -> bool:
    host = (urlsplit(base_url or "").hostname or "").lower()
    return host == "deepseek.com" or host.endswith(".deepseek.com")


class OpenAIChatAdapter(ProtocolAdapter):
    protocol = "openai_chat"
    optional_params = ("reasoning_effort", "extra_body", "stream_options")
    rejection_markers = ("reasoning_effort", "extra_body", "stream_options", "thinking")

    def create_client(self, target: Target, timeout: float) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=target.api_key or "missing-api-key",
            timeout=timeout,
            **sdk_endpoint_kwargs(target, target.base_url),
            # Retries (with backoff / Retry-After) are handled by LLMClient;
            # the SDK's own retry would multiply the attempts.
            max_retries=0,
        )

    def build_request(
        self,
        target: Target,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        max_tokens: int,
        temperature: float | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": target.model_id,
            "messages": strip_private(messages),
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if target.thinking_enabled:
            if target.reasoning_depth:
                # Sent once, as the top-level parameter only.
                kwargs["reasoning_effort"] = target.reasoning_depth
            extra_body = {k: v for k, v in target.extra_body.items() if k != "reasoning_effort"}
            if extra_body:
                kwargs["extra_body"] = extra_body
        return kwargs

    async def consume(
        self,
        client: Any,
        request: dict[str, Any],
        on_chunk: StreamCallback | None,
        progress: StreamProgress | None,
    ) -> LLMResponse:
        acc = StreamAccumulator()
        tool_calls_by_index: dict[int, ToolCallData] = {}
        tool_arg_parts: dict[int, list[str]] = {}
        usage_prompt_tokens = 0
        usage_cache_read = 0
        usage_completion_tokens = 0
        last_finish_reason = ""

        stream = await client.chat.completions.create(**request)
        async for chunk in stream:
            u = getattr(chunk, "usage", None)
            if u is not None:
                usage_prompt_tokens, usage_cache_read = openai_usage_parts(u)
                usage_completion_tokens = int(getattr(u, "completion_tokens", 0) or 0)
            if not chunk.choices:
                continue
            choice0 = chunk.choices[0]
            if getattr(choice0, "finish_reason", None):
                last_finish_reason = str(choice0.finish_reason)
            delta = choice0.delta

            # Extract reasoning_content if present
            r_chunk = getattr(delta, "reasoning_content", None)
            if not r_chunk and hasattr(delta, "model_extra") and delta.model_extra:
                r_chunk = delta.model_extra.get("reasoning_content")
            if r_chunk:
                acc.add_thought(str(r_chunk))
                if progress is not None:
                    progress.emitted = True

            if delta.content:
                acc.add_content(delta.content)
                if progress is not None:
                    progress.emitted = True

            if delta.tool_calls:
                if progress is not None:
                    progress.emitted = True
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_by_index:
                        tool_calls_by_index[idx] = ToolCallData(
                            id=tc_delta.id or f"call_{idx}",
                            name=(
                                tc_delta.function.name
                                if tc_delta.function and tc_delta.function.name
                                else ""
                            ),
                            arguments_json="",
                        )
                        tool_arg_parts[idx] = []
                    entry = tool_calls_by_index[idx]
                    if tc_delta.id:
                        entry.id = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry.name = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_arg_parts[idx].append(tc_delta.function.arguments)

            if on_chunk:
                if progress is not None:
                    progress.emitted = True
                cur_thought, cur_content = acc.snapshot()
                await on_chunk(cur_thought, cur_content)

        for idx, parts in tool_arg_parts.items():
            tool_calls_by_index[idx].arguments_json = "".join(parts)

        final_thought, final_content, dsml_tools = acc.final()
        ordered_tools = [tool_calls_by_index[k] for k in sorted(tool_calls_by_index)]
        if not ordered_tools and dsml_tools:
            ordered_tools = dsml_tools

        resp = LLMResponse(
            thought=final_thought,
            content=final_content,
            tool_calls=ordered_tools,
            prompt_tokens=usage_prompt_tokens,
            completion_tokens=usage_completion_tokens,
            cache_read_tokens=usage_cache_read,
            finish_reason=last_finish_reason,
        )
        if resp.prompt_tokens <= 0 and resp.cache_read_tokens <= 0:
            resp.prompt_tokens = self.estimate_prompt_tokens(request)
        if resp.completion_tokens <= 0:
            resp.completion_tokens = estimate_completion_tokens(resp)
        return resp
