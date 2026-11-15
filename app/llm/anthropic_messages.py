"""Anthropic Messages API adapter."""

from __future__ import annotations

import copy
import logging
from typing import Any

from app.llm.base import ProtocolAdapter, sdk_endpoint_kwargs
from app.llm.common import (
    StreamAccumulator,
    StreamProgress,
    UpstreamStreamError,
    estimate_completion_tokens,
)
from app.llm.messages import (
    call_parts,
    arguments_to_dict,
    private_of,
    repair_tool_pairs,
    safe_tool_id,
    text_of,
    tool_function_spec,
)
from app.llm.types import LLMResponse, StreamCallback, Target, ToolCallData

try:  # optional at import time; a clear error is raised when a client is needed
    from anthropic import AsyncAnthropic
except ImportError:  # pragma: no cover
    AsyncAnthropic = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
#: Thinking budget (tokens) per reasoning depth.
THINKING_BUDGETS = {"low": 1024, "medium": 2048, "high": 4096, "xhigh": 8192}
#: Head-room kept for the visible answer on top of the thinking budget.
ANSWER_HEADROOM = 1024
EMPTY_TEXT = "（空）"
_RETRYABLE_ERROR_TYPES = {"overloaded_error", "api_error", "rate_limit_error", "timeout_error"}
_STOP_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length"}
_THINKING_TYPES = ("thinking", "redacted_thinking")


def normalize_base_url(base_url: str) -> str:
    """Anthropic SDK base URL: no trailing slash and no ``/v1`` (the SDK adds it)."""
    url = (base_url or "").strip().rstrip("/") or DEFAULT_ANTHROPIC_BASE_URL
    if url.endswith("/v1"):
        url = url[: -len("/v1")].rstrip("/")
    return url or DEFAULT_ANTHROPIC_BASE_URL


def thinking_budget(depth: str) -> int:
    return THINKING_BUDGETS.get(depth, THINKING_BUDGETS["high"])


def _push(out: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]) -> None:
    if not blocks:
        return
    if out and out[-1]["role"] == role:
        out[-1]["content"].extend(blocks)
    else:
        out.append({"role": role, "content": list(blocks)})


def _has_real_user_content(message: dict[str, Any]) -> bool:
    return message["role"] == "user" and any(b.get("type") != "tool_result" for b in message["content"])


def thinking_resumable(messages: list[dict[str, Any]]) -> bool:
    """Can extended thinking stay on? Tool-use turns of the current loop need signed thinking."""
    start = 0
    for idx, message in enumerate(messages):
        if _has_real_user_content(message):
            start = idx
    for message in messages[start + 1 :]:
        if message["role"] != "assistant":
            continue
        blocks = message["content"]
        if any(b.get("type") == "tool_use" for b in blocks):
            if not blocks or blocks[0].get("type") not in _THINKING_TYPES:
                return False
    return True


def strip_thinking_blocks(messages: list[dict[str, Any]]) -> None:
    for message in messages:
        if message["role"] == "assistant":
            message["content"] = [
                b for b in message["content"] if b.get("type") not in _THINKING_TYPES
            ]
    messages[:] = [m for m in messages if m["content"]]


class AnthropicMessagesAdapter(ProtocolAdapter):
    protocol = "anthropic_messages"
    optional_params = ("thinking",)
    rejection_markers = ("thinking", "budget_tokens")

    def create_client(self, target: Target, timeout: float) -> Any:
        if AsyncAnthropic is None:
            raise RuntimeError("未安装 anthropic 依赖，无法使用 Anthropic Messages 协议。")
        return AsyncAnthropic(
            api_key=target.api_key or "missing-api-key",
            timeout=timeout,
            max_retries=0,
            **sdk_endpoint_kwargs(target, normalize_base_url(target.base_url)),
        )

    # ------------------------------------------------------------- conversion
    def convert_messages(
        self, target: Target, messages: list[dict[str, Any]], *, with_thinking: bool
    ) -> tuple[str, list[dict[str, Any]]]:
        """Canonical messages -> ``(system, anthropic_messages)``."""
        messages = repair_tool_pairs(messages)
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []
        leading = True
        for message in messages:
            role = message.get("role")
            if role == "system":
                text = text_of(message.get("content"))
                if leading:
                    if text:
                        system_parts.append(text)
                elif text:
                    _push(out, "user", [{"type": "text", "text": text}])
                continue
            leading = False
            if role == "user":
                _push(out, "user", [{"type": "text", "text": text_of(message.get("content")) or EMPTY_TEXT}])
            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                private = private_of(message)
                if with_thinking and target.private_matches(private):
                    for block in private.get("blocks") or []:
                        if isinstance(block, dict) and block.get("type") in _THINKING_TYPES:
                            blocks.append(copy.deepcopy(block))
                text = text_of(message.get("content"))
                if text.strip():
                    blocks.append({"type": "text", "text": text})
                for call in message.get("tool_calls") or []:
                    call_id, name, arguments = call_parts(call)
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": safe_tool_id(call_id),
                            "name": name,
                            "input": arguments_to_dict(arguments),
                        }
                    )
                _push(out, "assistant", blocks)
            elif role == "tool":
                _push(
                    out,
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": safe_tool_id(str(message.get("tool_call_id") or "")),
                            "content": text_of(message.get("content")) or EMPTY_TEXT,
                        }
                    ],
                )
        # Messages must start with a user turn and end with one (no assistant prefill).
        if not out or out[0]["role"] != "user":
            out.insert(0, {"role": "user", "content": [{"type": "text", "text": "（对局继续）"}]})
        if out[-1]["role"] != "user":
            out.append({"role": "user", "content": [{"type": "text", "text": "请继续。"}]})
        # tool_result blocks must come first inside a user turn
        for message in out:
            if message["role"] == "user":
                message["content"].sort(key=lambda b: 0 if b.get("type") == "tool_result" else 1)
        return "\n\n".join(system_parts), out

    def build_request(
        self,
        target: Target,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        max_tokens: int,
        temperature: float | None,
    ) -> dict[str, Any]:
        system, converted = self.convert_messages(target, messages, with_thinking=True)
        thinking_on = bool(target.thinking_enabled and target.reasoning_depth)
        if thinking_on and not thinking_resumable(converted):
            # e.g. the turn was started by another provider: there is no signed thinking
            # block to hand back, so thinking cannot be continued for this request.
            thinking_on = False
        if not thinking_on:
            strip_thinking_blocks(converted)
            if not converted or converted[0]["role"] != "user":
                converted.insert(0, {"role": "user", "content": [{"type": "text", "text": "（对局继续）"}]})

        request: dict[str, Any] = {
            "model": target.model_id,
            "messages": converted,
            "max_tokens": int(max_tokens),
            "stream": True,
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = [self._tool(t) for t in tools]
            request["tool_choice"] = {"type": "auto"}
        if thinking_on:
            budget = thinking_budget(target.reasoning_depth)
            request["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # max_tokens must exceed the thinking budget (and leave room for the answer)
            request["max_tokens"] = max(request["max_tokens"], budget + ANSWER_HEADROOM)
        elif temperature is not None:
            request["temperature"] = min(1.0, max(0.0, float(temperature)))
        return request

    @staticmethod
    def _tool(tool: dict[str, Any]) -> dict[str, Any]:
        name, description, schema = tool_function_spec(tool)
        spec: dict[str, Any] = {"name": name, "input_schema": schema}
        if description:
            spec["description"] = description
        return spec

    def downgrade(self, exc: BaseException, request: dict[str, Any]) -> bool:
        changed = super().downgrade(exc, request)
        if changed:
            strip_thinking_blocks(request["messages"])
            if not request["messages"] or request["messages"][0]["role"] != "user":
                request["messages"].insert(
                    0, {"role": "user", "content": [{"type": "text", "text": "（对局继续）"}]}
                )
        return changed

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
        thinking_blocks: dict[int, dict[str, Any]] = {}
        thinking_order: list[int] = []
        input_tokens = 0
        output_tokens = 0
        stop_reason = ""

        def mark() -> None:
            if progress is not None:
                progress.emitted = True

        async def emit() -> None:
            if on_chunk:
                thought, content = acc.snapshot()
                await on_chunk(thought, content)

        stream = await client.messages.create(**request)
        async for event in stream:
            etype = getattr(event, "type", "") or ""
            if etype == "message_start":
                usage = getattr(getattr(event, "message", None), "usage", None)
                input_tokens = _prompt_tokens(usage) or input_tokens
                output_tokens = int(getattr(usage, "output_tokens", 0) or 0) or output_tokens
            elif etype == "content_block_start":
                idx = int(getattr(event, "index", 0) or 0)
                block = getattr(event, "content_block", None)
                btype = getattr(block, "type", "")
                if btype == "tool_use":
                    calls[idx] = ToolCallData(
                        id=str(getattr(block, "id", "") or f"call_{idx}"),
                        name=str(getattr(block, "name", "") or ""),
                        arguments_json="",
                    )
                    arg_parts[idx] = []
                    mark()
                elif btype == "thinking":
                    if thinking_order and acc.native:
                        acc.add_thought("\n\n")
                    thinking_blocks[idx] = {"type": "thinking", "thinking": "", "signature": ""}
                    thinking_order.append(idx)
                    first = str(getattr(block, "thinking", "") or "")
                    if first:
                        thinking_blocks[idx]["thinking"] += first
                        acc.add_thought(first)
                        mark()
                        await emit()
                elif btype == "redacted_thinking":
                    thinking_blocks[idx] = {
                        "type": "redacted_thinking",
                        "data": str(getattr(block, "data", "") or ""),
                    }
                    thinking_order.append(idx)
                elif btype == "text":
                    first = str(getattr(block, "text", "") or "")
                    if first:
                        acc.add_content(first)
                        mark()
                        await emit()
            elif etype == "content_block_delta":
                idx = int(getattr(event, "index", 0) or 0)
                delta = getattr(event, "delta", None)
                dtype = getattr(delta, "type", "")
                if dtype == "text_delta":
                    acc.add_content(str(getattr(delta, "text", "") or ""))
                    mark()
                    await emit()
                elif dtype == "thinking_delta":
                    piece = str(getattr(delta, "thinking", "") or "")
                    if idx in thinking_blocks:
                        thinking_blocks[idx]["thinking"] += piece
                    acc.add_thought(piece)
                    mark()
                    await emit()
                elif dtype == "signature_delta":
                    if idx in thinking_blocks:
                        thinking_blocks[idx]["signature"] += str(getattr(delta, "signature", "") or "")
                elif dtype == "input_json_delta":
                    if idx in arg_parts:
                        arg_parts[idx].append(str(getattr(delta, "partial_json", "") or ""))
                        mark()
            elif etype == "message_delta":
                delta = getattr(event, "delta", None)
                reason = getattr(delta, "stop_reason", None)
                if reason:
                    stop_reason = str(reason)
                usage = getattr(event, "usage", None)
                out = int(getattr(usage, "output_tokens", 0) or 0)
                if out:
                    output_tokens = out
                in_tokens = _prompt_tokens(usage)
                if in_tokens:
                    input_tokens = in_tokens
            elif etype == "error":
                err = getattr(event, "error", None)
                err_type = str(getattr(err, "type", "") or "")
                raise UpstreamStreamError(
                    f"上游流式错误（{err_type or 'unknown'}）",
                    retryable=err_type in _RETRYABLE_ERROR_TYPES,
                )

        for idx, parts in arg_parts.items():
            calls[idx].arguments_json = "".join(parts)
        thought, content, _ = acc.final()
        blocks = [
            thinking_blocks[i]
            for i in thinking_order
            if thinking_blocks[i]["type"] == "redacted_thinking" or thinking_blocks[i].get("signature")
        ]
        resp = LLMResponse(
            thought=thought,
            content=content,
            tool_calls=[calls[k] for k in sorted(calls)],
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            finish_reason=_STOP_REASONS.get(stop_reason, stop_reason),
            provider_private={"blocks": blocks} if blocks else {},
        )
        if resp.prompt_tokens <= 0:
            resp.prompt_tokens = self.estimate_prompt_tokens(request)
        if resp.completion_tokens <= 0:
            resp.completion_tokens = estimate_completion_tokens(resp)
        return resp


def _prompt_tokens(usage: Any) -> int:
    """Total prompt tokens: fresh input plus cache writes and reads."""
    if usage is None:
        return 0
    total = 0
    for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = getattr(usage, name, 0)
        if isinstance(value, (int, float)):
            total += int(value)
    return total
