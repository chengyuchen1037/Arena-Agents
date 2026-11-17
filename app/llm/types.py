"""Protocol-neutral LLM types shared by the engine and the protocol adapters.

``app.engine.llm`` re-exports everything public from here, so existing imports
(``from app.engine.llm import LLMResponse, ToolCallData``) keep working.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

_THINK_TAG_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_DSML_BLOCK_RE = re.compile(
    r"<\s*[｜|]+\s*DSML\s*[｜|]+\s*(?:function_)?calls\s*>.*?(?:</\s*[｜|]+\s*DSML\s*[｜|]+\s*(?:function_)?calls\s*>|$)",
    re.DOTALL,
)
_DSML_INVOKE_RE = re.compile(
    r'<\s*[｜|]+\s*DSML\s*[｜|]+\s*invoke\s+name="([^"]+)"[^>]*>(.*?)(?:</\s*[｜|]+\s*DSML\s*[｜|]+\s*invoke\s*>|$)',
    re.DOTALL,
)
_DSML_PARAM_RE = re.compile(
    r'<\s*[｜|]+\s*DSML\s*[｜|]+\s*parameter\s+name="([^"]+)"[^>]*>(.*?)(?:</\s*[｜|]+\s*DSML\s*[｜|]+\s*parameter\s*>|$)',
    re.DOTALL,
)

#: Key placed in the dict returned by :meth:`ToolCallData.parse_args` when the
#: model's tool arguments could not be turned into a JSON object.
ARGS_ERROR_KEY = "_args_error"

#: Key under which the engine stores :attr:`LLMResponse.provider_private` on an
#: assistant message so the same provider can resume the turn.
PROVIDER_PRIVATE_KEY = "_provider_private"

PROTOCOLS = ("openai_chat", "openai_responses", "anthropic_messages")
REASONING_DEPTHS = ("low", "medium", "high", "xhigh")


def args_error(args: Any) -> str | None:
    """Return the argument-parse error carried by ``parse_args()`` output, if any."""
    if isinstance(args, dict):
        marker = args.get(ARGS_ERROR_KEY)
        if marker:
            return str(marker)
    return None


@dataclass
class ToolCallData:
    id: str
    name: str
    arguments_json: str = ""

    def parse_args_checked(self) -> tuple[dict[str, Any], str | None]:
        """Parse the arguments; always returns ``(dict, error_or_None)``.

        * empty arguments -> ``({}, None)``
        * a JSON object  -> ``(obj, None)``
        * invalid JSON   -> ``({"_raw": text, "_args_error": msg}, msg)``
        * valid JSON that is not an object (list / str / number / null)
          -> ``({"_raw": text, "_args_error": msg}, msg)``
        """
        text = self.arguments_json or ""
        if not text.strip():
            return {}, None
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, RecursionError, ValueError):
            msg = f"工具 {self.name or '?'} 的参数不是合法的 JSON"
            return {"_raw": text, ARGS_ERROR_KEY: msg}, msg
        if not isinstance(parsed, dict):
            msg = (
                f"工具 {self.name or '?'} 的参数必须是 JSON 对象，"
                f"实际为 {type(parsed).__name__}"
            )
            return {"_raw": text, ARGS_ERROR_KEY: msg}, msg
        return parsed, None

    def parse_args(self) -> dict[str, Any]:
        """Always returns a ``dict``; failures carry :data:`ARGS_ERROR_KEY`."""
        return self.parse_args_checked()[0]

    @property
    def args_error(self) -> str | None:
        """Parse error message for the arguments, or ``None`` when they are fine."""
        return self.parse_args_checked()[1]


@dataclass
class LLMResponse:
    thought: str = ""
    content: str = ""
    tool_calls: list[ToolCallData] = field(default_factory=list)
    # v0.12.1: ``prompt_tokens`` is the input that missed the provider's cache; the cached part is reported
    # separately.  total input of the call = prompt + cache_read + cache_write.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    finish_reason: str = ""
    # --- v0.10: which model produced this and what it costs (a snapshot, USD per million tokens) ---
    # cost = (prompt*input + cache_read*cache_read + cache_write*cache_write + completion*output) / 1e6
    # (an empty cache price is resolved to the input price before it gets here)
    provider_id: str = ""
    model_id: str = ""
    input_price_per_m: float = 0.0
    output_price_per_m: float = 0.0
    cache_read_price_per_m: float = 0.0
    cache_write_price_per_m: float = 0.0
    context_window_tokens: int = 0
    #: Provider-specific blocks needed to resume *this* provider's turn (for
    #: example signed Anthropic thinking blocks). Always carries ``provider_id``,
    #: ``protocol`` and ``model_id`` when non-empty; ignored by other providers.
    provider_private: dict[str, Any] = field(default_factory=dict)


@dataclass
class Target:
    """Everything an adapter needs to talk to one model (resolved from a binding)."""

    provider_id: str
    protocol: str
    base_url: str
    api_key: str
    model_id: str
    input_price_per_m: float = 0.0
    output_price_per_m: float = 0.0
    context_window_tokens: int = 1_000_000
    reasoning_depth: str = "high"
    #: Extra request-body fields forwarded verbatim (OpenAI-compatible gateways only).
    extra_body: dict[str, Any] = field(default_factory=dict)
    #: Whether reasoning / thinking parameters are sent at all.
    thinking_enabled: bool = True
    temperature: float | None = None
    #: v0.11 endpoint plan (see :mod:`app.llm.endpoints`). ``request_url`` /
    #: ``sdk_base_url`` stay empty for hand-built targets; adapters then fall back
    #: to their protocol default derived from ``base_url``.
    auto_append_path: bool = True
    request_url: str = ""
    models_url: str | None = None
    sdk_base_url: str = ""

    @property
    def client_key(self) -> tuple[Any, ...]:
        return (
            self.protocol,
            self.base_url,
            self.api_key,
            self.auto_append_path,
            self.request_url,
            self.sdk_base_url,
        )

    def private_matches(self, private: Any) -> bool:
        """True when ``private`` was produced by this very provider/protocol/model."""
        return (
            isinstance(private, dict)
            and private.get("provider_id") == self.provider_id
            and private.get("protocol") == self.protocol
            and private.get("model_id") == self.model_id
        )


def extract_dsml_tool_calls(content: str) -> tuple[str, list[ToolCallData]]:
    """
    If DeepSeek emitted raw `<｜DSML｜calls>` markup inside `content`,
    parse it into structured `ToolCallData` objects and strip the raw markup
    so it never leaks into public chat channels.
    """
    if "DSML" not in content:
        return content, []

    parsed_calls: list[ToolCallData] = []
    for idx, inv_match in enumerate(_DSML_INVOKE_RE.finditer(content)):
        fn_name = inv_match.group(1).strip()
        inv_body = inv_match.group(2)
        params: dict[str, Any] = {}
        for p_match in _DSML_PARAM_RE.finditer(inv_body):
            p_name = p_match.group(1).strip()
            p_val = p_match.group(2).strip()
            params[p_name] = p_val
        parsed_calls.append(
            ToolCallData(
                id=f"dsml_call_{idx}",
                name=fn_name,
                arguments_json=json.dumps(params, ensure_ascii=False),
            )
        )

    cleaned = _DSML_BLOCK_RE.sub("", content).strip()
    return cleaned, parsed_calls


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return max(1, int(cjk * 0.65 + other * 0.28))


StreamCallback = Callable[[str, str], Awaitable[None]]


def split_inline_think(raw_content: str, native_thought: str) -> tuple[str, str]:
    """Extract <think>...</think> if native reasoning_content is empty."""
    thought = native_thought.strip()
    content = raw_content

    if "<think>" in content:
        if "</think>" in content:
            matches = _THINK_TAG_RE.findall(content)
            extracted = "\n".join(m.strip() for m in matches if m.strip())
            if extracted:
                thought = f"{thought}\n{extracted}".strip() if thought else extracted
            content = _THINK_TAG_RE.sub("", content).strip()
        else:
            # Still inside unclosed <think> during streaming
            parts = content.split("<think>", 1)
            before = parts[0].strip()
            inside = parts[1].strip()
            thought = f"{thought}\n{inside}".strip() if thought else inside
            content = before

    if "DSML" in content and ("<｜" in content or "<|" in content):
        content = _DSML_BLOCK_RE.sub("", content).strip()

    return thought, content.strip()
