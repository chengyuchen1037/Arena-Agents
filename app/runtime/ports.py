"""Frozen contract between the async runtime (``app.runtime``: actors, workers, supervisor)
and the agent layer (``app.agents``: prompts, tools, context building).  Revised for v0.12.1.

The runtime knows nothing about prompts or tools; the agent layer knows nothing about asyncio
scheduling.  They meet in these protocols.  Both are tested against fakes of the other side.

v0.12.1 model (docs/v0.12-contracts.md section 10): every delivered envelope wakes the agent at once;
each agent keeps ONE persistent, append-only conversation (so provider prefix caches hit):

* a conversation is created with ``Brain.start_message`` (FULL: state board + memory + history + reasons);
* every later activation, and every merge of envelopes that arrived while a call was in flight, is a
  DELTA built by ``Brain.inject_message`` (new visible entries + board changes + reasons);
* the run ends when the model calls ``sleep`` (optionally with a timer) or answers without tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.llm.types import LLMResponse
from app.world.types import Envelope, Receipt, UiEvent


@dataclass
class WakeContext:
    """Why a worker run starts, or what arrived while it runs (``start``: conversation creation, FULL
    message; ``inject``: any later activation or mid-run merge, DELTA message)."""

    agent_id: str
    kind: Literal["start", "inject"]
    gen_id: int
    reasons: list[Envelope]  # every envelope that caused this activation / arrived since the last injection
    after_seq: int = 0  # inject: the last ledger seq this conversation has already been told about


@dataclass
class ToolOutcome:
    """Result of executing one tool call."""

    text: str  # JSON / text shown to the model as the tool result
    receipt: Receipt | None = None  # present when the tool went through the commit gate
    ends_run: bool = False  # e.g. ``sleep``: stop this run now (the agent goes to sleep)
    # With ``ends_run``: also wake the agent after this many *game-clock* seconds if nothing arrives
    # earlier (the timer is frozen while the game is paused).  ``None`` = sleep until something arrives.
    sleep_s: float | None = None
    rebuild: bool = False  # e.g. ``compact_context``: discard the conversation and start a new one (FULL message)
    shared: bool = False  # True for notes / documents / compaction (no world effect)


class Brain(Protocol):
    """Everything agent-specific the worker needs.  Synchronous and fast (no network I/O)."""

    def system_prompt(self, agent_id: str) -> str: ...

    def tool_schemas(self, agent_id: str) -> list[dict[str, Any]]:
        """OpenAI-style ``[{"type":"function","function":{name,description,parameters}}]``."""

    def begin_run(self, agent_id: str) -> None:
        """An activation starts (a new run, whether or not the conversation continues): reset per-run state
        such as "already nudged" / "already reminded about unfinished business"."""

    def reset_conversation(self, agent_id: str) -> None:
        """The runtime discards this agent's conversation (compaction, restore, ``rebuild``): forget every
        per-conversation baseline (e.g. the state board the next delta is computed against)."""

    def start_message(self, agent_id: str, ctx: WakeContext) -> tuple[str, int]:
        """FULL first user message of a *new* conversation: state board + memory + visible history + wake
        reasons.  Returns ``(text, seen_seq)`` where ``seen_seq`` is the last ledger seq reflected in it.
        It also records the board baseline the following deltas are computed against."""

    def inject_message(self, agent_id: str, ctx: WakeContext) -> tuple[str, int] | None:
        """DELTA user message for an existing conversation: visible entries after ``ctx.after_seq``, what
        changed on the state board since the last message of this conversation, and the wake reasons.
        ``None`` when there is nothing to tell (no new entries, no board change, no reasons).
        Returns ``(text, new_seen_seq)``."""

    def no_tool_nudge(self, agent_id: str, content: str, round_idx: int) -> str | None:
        """Called when the model answered without any tool call.  Return a user message to nudge it
        (e.g. "your plain text is not sent anywhere; call speak or sleep"), or ``None`` to end the run."""

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

    def count(
        self, agent_id: str, what: Literal["tool_calls", "thinking_rounds", "rejections", "activations"], n: int = 1
    ) -> None: ...


class TraceSink(Protocol):
    """Plaintext JSONL trace of thoughts, tool calls and receipts (D5, D11).  Never sent to the UI."""

    def write(self, event: str, agent_id: str, **fields: Any) -> None: ...


class EventSink(Protocol):
    """Publishes spectator events (world + runtime) to the SSE bus."""

    def publish(self, event: UiEvent) -> None: ...
