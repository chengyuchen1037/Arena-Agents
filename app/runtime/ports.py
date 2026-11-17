"""Frozen v0.12 contract between the async runtime (``app.runtime``: actors, workers, supervisor)
and the agent layer (``app.agents``: prompts, tools, context building).

The runtime knows nothing about prompts or tools; the agent layer knows nothing about asyncio
scheduling.  They meet in these protocols.  Both are tested against fakes of the other side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.llm.types import LLMResponse
from app.world.types import Envelope, Receipt, UiEvent


@dataclass
class WakeContext:
    """Why a worker run starts, or what arrived while it runs."""

    agent_id: str
    kind: Literal["start", "inject"]
    gen_id: int
    reasons: list[Envelope]  # urgent / normal envelopes that caused the start, or arrived mid-run
    ambient: list[Envelope] = field(default_factory=list)  # buffered ambient envelopes (informational)
    after_seq: int = 0  # inject: the last ledger seq this run has already seen


@dataclass
class ToolOutcome:
    """Result of executing one tool call."""

    text: str  # JSON / text shown to the model as the tool result
    receipt: Receipt | None = None  # present when the tool went through the commit gate
    ends_run: bool = False  # e.g. ``wait``: stop this run now
    rebuild: bool = False  # e.g. ``compact_context``: rebuild the conversation from scratch
    shared: bool = False  # True for notes / documents / compaction (no world effect)


class Brain(Protocol):
    """Everything agent-specific the worker needs.  Synchronous and fast (no network I/O)."""

    def system_prompt(self, agent_id: str) -> str: ...

    def tool_schemas(self, agent_id: str) -> list[dict[str, Any]]:
        """OpenAI-style ``[{"type":"function","function":{name,description,parameters}}]``."""

    def start_message(self, agent_id: str, ctx: WakeContext) -> tuple[str, int]:
        """The first user message of a run: state board + memory + visible history + wake reasons.
        Returns ``(text, seen_seq)`` where ``seen_seq`` is the last ledger seq reflected in it."""

    def inject_message(self, agent_id: str, ctx: WakeContext) -> tuple[str, int] | None:
        """A user message with what arrived during the run (new visible entries + reasons), or
        ``None`` when there is nothing worth telling.  Returns ``(text, new_seen_seq)``."""

    def no_tool_nudge(self, agent_id: str, content: str, round_idx: int) -> str | None:
        """Called when the model answered without any tool call.  Return a user message to nudge it
        (e.g. "your plain text is not sent anywhere; call speak or wait"), or ``None`` to end the run."""

    def execute_tool(
        self, agent_id: str, name: str, args: dict[str, Any], *, gen_id: int, seen_seq: int
    ) -> ToolOutcome:
        """Run a tool call.  World-affecting tools must build an ``Intent`` with
        ``based_on_seq=seen_seq`` and call ``world.submit``; the receipt becomes the tool result."""


class LlmPort(Protocol):
    """The runtime's view of a language model (production: ``GatedEngineLlm`` over ``LLMClient``)."""

    async def complete(
        self,
        agent_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...


class UsageSink(Protocol):
    """Receives per-agent accounting.  Production: the orchestrator's session usage book."""

    def record_llm(self, agent_id: str, resp: LLMResponse) -> None:
        """One LLM call finished (tokens, price snapshot, thought present, ...)."""

    def count(self, agent_id: str, what: Literal["tool_calls", "thinking_rounds", "rejections"], n: int = 1) -> None: ...


class TraceSink(Protocol):
    """Plaintext JSONL trace of thoughts, tool calls and receipts (D5, D11).  Never sent to the UI."""

    def write(self, event: str, agent_id: str, **fields: Any) -> None: ...


class EventSink(Protocol):
    """Publishes spectator events (world + runtime) to the SSE bus."""

    def publish(self, event: UiEvent) -> None: ...
