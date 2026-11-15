"""OpenAI Responses API adapter (stateless: ``store=False``)."""

from __future__ import annotations

import copy
import logging
from typing import Any

from openai import AsyncOpenAI

from app.llm.base import ProtocolAdapter, sdk_endpoint_kwargs
from app.llm.common import (
    StreamAccumulator,
    StreamProgress,
    UpstreamStreamError,
    estimate_completion_tokens,
)
from app.llm.messages import (
    call_parts,
    private_of,
    repair_tool_pairs,
    text_of,
    tool_function_spec,
)
from app.llm.types import LLMResponse, StreamCallback, Target, ToolCallData

logger = logging.getLogger(__name__)

_REASONING_ITEM_TYPES = {"reasoning"}
_RETRYABLE_FAILURE_CODES = {"server_error", "rate_limit_exceeded", "overloaded", "timeout"}


def to_plain(obj: Any) -> Any:
    """Recursively turn SDK models / namespaces into JSON-able dicts and lists."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return to_plain(dump(exclude_none=True))
    if hasattr(obj, "__dict__"):
        return to_plain(vars(obj))
    return str(obj)


class OpenAIResponsesAdapter(ProtocolAdapter):
    protocol = "openai_responses"
    optional_params = ("reasoning", "include", "temperature")
    rejection_markers = ("reasoning", "include", "encrypted_content", "summary", "temperature")

    def create_client(self, target: Target, timeout: float) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=target.api_key or "missing-api-key",
            timeout=timeout,
            max_retries=0,
            **sdk_endpoint_kwargs(target, target.base_url),
        )

    # ------------------------------------------------------------- conversion
    def convert_messages(
        self, target: Target, messages: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        """Canonical messages -> ``(instructions, input_items)``."""
        messages = repair_tool_pairs(messages)
        instructions: list[str] = []
        items: list[dict[str, Any]] = []
        leading = True
        for message in messages:
            role = message.get("role")
            if role == "system":
                text = text_of(message.get("content"))
                if leading:
                    if text:
                        instructions.append(text)
                elif text:
                    items.append({"role": "system", "content": text})
                continue
            leading = False
            if role == "user":
                items.append({"role": "user", "content": text_of(message.get("content"))})
            elif role == "assistant":
                items.extend(self._assistant_items(target, message))
            elif role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(message.get("tool_call_id") or ""),
                        "output": text_of(message.get("content")),
                    }
                )
        return "\n\n".join(instructions), items

    def _assistant_items(self, target: Target, message: dict[str, Any]) -> list[dict[str, Any]]:
        calls = [call_parts(c) for c in (message.get("tool_calls") or [])]
        private = private_of(message)
        if target.private_matches(private):
            replay = private.get("items")
            if (
                isinstance(replay, list)
                and replay
                and list(private.get("call_ids") or []) == [c[0] for c in calls]
            ):
                return copy.deepcopy(replay)
        items: list[dict[str, Any]] = []
        text = text_of(message.get("content"))
        if text:
            items.append({"role": "assistant", "content": text})
        for call_id, name, arguments in calls:
            items.append(
                {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}
            )
        return items

    def build_request(
        self,
        target: Target,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        max_tokens: int,
        temperature: float | None,
    ) -> dict[str, Any]:
        instructions, items = self.convert_messages(target, messages)
        request: dict[str, Any] = {
            "model": target.model_id,
            "input": items,
            "stream": True,
            "store": False,
            "max_output_tokens": max_tokens,
        }
        if instructions:
            request["instructions"] = instructions
        if tools:
            request["tools"] = [self._tool(t) for t in tools]
            request["tool_choice"] = "auto"
        if target.thinking_enabled and target.reasoning_depth:
            request["reasoning"] = {"effort": target.reasoning_depth, "summary": "auto"}
            request["include"] = ["reasoning.encrypted_content"]
        elif temperature is not None:
            request["temperature"] = temperature
        return request

    @staticmethod
    def _tool(tool: dict[str, Any]) -> dict[str, Any]:
        name, description, schema = tool_function_spec(tool)
        spec: dict[str, Any] = {"type": "function", "name": name, "parameters": schema}
        if description:
            spec["description"] = description
        return spec

    # ---------------------------------------------------------------- streaming
    async def consume(
        self,
        client: Any,
        request: dict[str, Any],
        on_chunk: StreamCallback | None,
        progress: StreamProgress | None,
    ) -> LLMResponse:
        acc = StreamAccumulator()
        calls: dict[int, ToolCallData] = {}
        arg_parts: dict[int, list[str]] = {}
        items_by_index: dict[int, dict[str, Any]] = {}
        final_response: Any = None
        incomplete_reason = ""

        def mark() -> None:
            if progress is not None:
                progress.emitted = True

        stream = await client.responses.create(**request)
        async for event in stream:
            etype = getattr(event, "type", "") or ""
            if etype == "response.output_text.delta":
                acc.add_content(str(getattr(event, "delta", "") or ""))
                mark()
                await self._emit(on_chunk, acc)
            elif etype in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                acc.add_thought(str(getattr(event, "delta", "") or ""))
                mark()
                await self._emit(on_chunk, acc)
            elif etype == "response.reasoning_summary_part.added":
                if acc.native:
                    acc.add_thought("\n\n")
            elif etype == "response.output_item.added":
                item = getattr(event, "item", None)
                if getattr(item, "type", "") == "function_call":
                    idx = int(getattr(event, "output_index", len(calls)) or 0)
                    call_id = getattr(item, "call_id", None) or getattr(item, "id", None) or f"call_{idx}"
                    calls[idx] = ToolCallData(
                        id=str(call_id), name=str(getattr(item, "name", "") or ""), arguments_json=""
                    )
                    arg_parts[idx] = []
                    mark()
            elif etype == "response.function_call_arguments.delta":
                idx = int(getattr(event, "output_index", 0) or 0)
                if idx in calls:
                    arg_parts[idx].append(str(getattr(event, "delta", "") or ""))
                    mark()
            elif etype == "response.function_call_arguments.done":
                idx = int(getattr(event, "output_index", 0) or 0)
                if idx in calls:
                    arguments = getattr(event, "arguments", None)
                    if isinstance(arguments, str):
                        arg_parts[idx] = [arguments]
                    name = getattr(event, "name", None)
                    if name:
                        calls[idx].name = str(name)
            elif etype == "response.output_item.done":
                idx = int(getattr(event, "output_index", len(items_by_index)) or 0)
                item = to_plain(getattr(event, "item", None))
                if isinstance(item, dict):
                    items_by_index[idx] = item
            elif etype in ("response.completed", "response.incomplete"):
                final_response = getattr(event, "response", None)
                if etype == "response.incomplete":
                    details = getattr(final_response, "incomplete_details", None)
                    incomplete_reason = str(getattr(details, "reason", "") or "incomplete")
            elif etype == "response.failed":
                response = getattr(event, "response", None)
                error = getattr(response, "error", None)
                code = str(getattr(error, "code", "") or "")
                raise UpstreamStreamError(
                    f"上游返回失败（{code or 'unknown'}）",
                    retryable=code in _RETRYABLE_FAILURE_CODES,
                )
            elif etype == "error":
                code = str(getattr(event, "code", "") or "")
                raise UpstreamStreamError(
                    f"上游流式错误（{code or 'unknown'}）",
                    retryable=code in _RETRYABLE_FAILURE_CODES,
                )

        output_items: list[dict[str, Any]] = [items_by_index[i] for i in sorted(items_by_index)]
        if final_response is not None:
            final_output = to_plain(getattr(final_response, "output", None))
            if isinstance(final_output, list) and final_output:
                output_items = [i for i in final_output if isinstance(i, dict)]

        # Gateways that do not stream deltas still deliver everything in the final output.
        if not acc.raw:
            for item in output_items:
                if item.get("type") == "message":
                    for part in item.get("content") or []:
                        if isinstance(part, dict) and part.get("type") == "output_text":
                            acc.add_content(str(part.get("text") or ""))
        for idx, parts in arg_parts.items():
            calls[idx].arguments_json = "".join(parts)
        if not calls:
            for idx, item in enumerate(output_items):
                if item.get("type") == "function_call":
                    calls[idx] = ToolCallData(
                        id=str(item.get("call_id") or item.get("id") or f"call_{idx}"),
                        name=str(item.get("name") or ""),
                        arguments_json=str(item.get("arguments") or ""),
                    )
        else:
            # final items carry the authoritative arguments (and fill in missing ones)
            by_call_id = {
                str(i.get("call_id") or i.get("id")): i
                for i in output_items
                if i.get("type") == "function_call"
            }
            for call in calls.values():
                final_item = by_call_id.get(call.id)
                if final_item is not None and not call.arguments_json:
                    call.arguments_json = str(final_item.get("arguments") or "")

        thought, content, _ = acc.final()
        tool_calls = [calls[k] for k in sorted(calls)]

        usage = getattr(final_response, "usage", None)
        prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        if tool_calls:
            finish = "tool_calls"
        elif incomplete_reason:
            finish = "length" if incomplete_reason == "max_output_tokens" else incomplete_reason
        else:
            finish = "stop"

        private: dict[str, Any] = {}
        if any(item.get("type") in _REASONING_ITEM_TYPES for item in output_items):
            private = {"items": output_items, "call_ids": [c.id for c in tool_calls]}

        resp = LLMResponse(
            thought=thought,
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=finish,
            provider_private=private,
        )
        if resp.prompt_tokens <= 0:
            resp.prompt_tokens = self.estimate_prompt_tokens(request)
        if resp.completion_tokens <= 0:
            resp.completion_tokens = estimate_completion_tokens(resp)
        return resp

    @staticmethod
    async def _emit(on_chunk: StreamCallback | None, acc: StreamAccumulator) -> None:
        if on_chunk:
            thought, content = acc.snapshot()
            await on_chunk(thought, content)
