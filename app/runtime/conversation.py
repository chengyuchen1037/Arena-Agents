"""Persistent, append-only conversations (v0.12.1, docs/v0.12-contracts.md section 10.2).

Every agent keeps ONE conversation for the whole game: the system prompt and the tool schemas are fixed
when it is created, afterwards only messages are appended (user deltas, assistant turns, tool results).
That is what makes a provider's *prefix cache* hit: request ``n + 1`` starts with exactly the bytes of
request ``n``.  The only operations that break the prefix are explicit, rare and counted in
:attr:`Conversation.epoch`: a rebuild (``compact_context``) and the emergency trim.

The :class:`ConversationBook` is owned by the orchestrator, one per game.  It survives pause -> resume
(conversations and ``sleep_until`` are kept) and is reset for a new game, an import or a restore (the save
file holds the ledger, not the conversations: they are rebuilt once from the ledger).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Conversation", "ConversationBook", "message_chars", "round_spans", "tools_signature"]


def tools_signature(tools: list[dict[str, Any]]) -> str:
    """A short, order-sensitive digest of the tool schemas (equal digests = byte-identical request prefix)."""
    blob = json.dumps(tools, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def message_chars(message: dict[str, Any]) -> int:
    """Approximate size of one message in characters (JSON form); used to apportion token counts."""
    try:
        return len(json.dumps(message, ensure_ascii=False, default=str))
    except (TypeError, ValueError):  # pragma: no cover - messages are plain JSON
        return len(str(message))


def round_spans(messages: list[dict[str, Any]], start: int = 0) -> list[tuple[int, int]]:
    """Half-open index ranges of the *complete rounds* of ``messages[start:]``.

    A round is the run of user messages that opened it, the assistant message that answered, and every
    tool result of that assistant message: ``[user, user, assistant(tool_calls), tool, tool]``.  A trailing
    run of user messages the model has not answered yet is not a round (it is never dropped).  Dropping
    whole rounds keeps every tool call paired with its result.
    """
    spans: list[tuple[int, int]] = []
    n = len(messages)
    i = start
    while i < n:
        begin = i
        while i < n and messages[i].get("role") not in ("assistant", "tool"):
            i += 1
        if i >= n:
            break  # unanswered tail
        if messages[i].get("role") == "assistant":
            i += 1
        while i < n and messages[i].get("role") == "tool":
            i += 1
        spans.append((begin, i))
    return spans


@dataclass
class Conversation:
    """One agent's conversation: ``[system] + messages`` is what every model call sends."""

    agent_id: str
    system: str
    tools: list[dict[str, Any]]
    tools_sig: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    seen_seq: int = 0  # the last ledger seq the conversation has been told about
    sleep_until: float | None = None  # game-clock deadline of a timed sleep (frozen while paused)
    last_input_tokens: int = 0  # input size of the latest model call (prompt + cache read + cache write)
    epoch: int = 1  # bumped whenever the request prefix is rewritten (rebuild, trim)
    # -- runtime bookkeeping -------------------------------------------------------------------
    compact_nudged: bool = False  # the "please compact" reminder has been queued for this conversation
    rebuilt: bool = False  # this conversation is the product of ``compact_context`` (a fresh FULL message)
    base_tokens: int = 0  # input size of the first model call of this conversation (= its FULL opening)
    slept_s: float | None = None  # the duration the agent asked for with ``sleep_until``
    notes: list[str] = field(default_factory=list)  # system remarks waiting for the next user message

    # ----------------------------------------------------------------------------- reading --
    def request_messages(self) -> list[dict[str, Any]]:
        """The messages of the next model call (a new list; the message dicts themselves are shared)."""
        return [{"role": "system", "content": self.system}, *self.messages]

    def __len__(self) -> int:
        return len(self.messages)

    @property
    def tail_role(self) -> str:
        """Role of the last message (``""`` for an empty conversation)."""
        return str(self.messages[-1].get("role", "")) if self.messages else ""

    def static_chars(self) -> int:
        """Size of what is sent besides the messages: the system prompt and the tool schemas."""
        return len(self.system) + len(json.dumps(self.tools, ensure_ascii=False, default=str))

    def total_chars(self) -> int:
        return self.static_chars() + sum(message_chars(m) for m in self.messages)

    # ----------------------------------------------------------------------------- writing --
    def take_notes(self) -> list[str]:
        """Hand out (and forget) the system remarks that wait for the next user message."""
        notes, self.notes = self.notes, []
        return notes

    def drop_oldest_rounds(self, max_chars: int, *, keep_first: bool = True) -> tuple[int, int]:
        """Remove complete rounds from the *front* until at least ``max_chars`` characters are gone.

        The first message (the FULL opening: identity, board, history) is pinned when ``keep_first``, and the
        newest round is never touched.  Returns ``(rounds dropped, characters dropped)`` and bumps
        :attr:`epoch` when something was removed (the request prefix changed).
        """
        spans = round_spans(self.messages, 1 if keep_first else 0)
        droppable = spans[:-1]  # the newest round always stays
        dropped_chars = 0
        cut = 0
        rounds = 0
        base = 1 if keep_first else 0
        for begin, end in droppable:
            if dropped_chars >= max_chars:
                break
            dropped_chars += sum(message_chars(m) for m in self.messages[begin:end])
            cut = end
            rounds += 1
        if rounds:
            del self.messages[base:cut]
            self.epoch += 1
        return rounds, dropped_chars


class ConversationBook:
    """The conversations of one game, by agent id."""

    def __init__(self) -> None:
        self._items: dict[str, Conversation] = {}

    def get(self, agent_id: str) -> Conversation | None:
        return self._items.get(agent_id)

    def put(self, conversation: Conversation) -> Conversation:
        """Store (or replace) ``conversation`` under its own ``agent_id``."""
        self._items[conversation.agent_id] = conversation
        return conversation

    def reset(self, agent_id: str | None = None) -> None:
        """Forget one agent's conversation, or every conversation (new game / import / restore)."""
        if agent_id is None:
            self._items.clear()
        else:
            self._items.pop(agent_id, None)

    def agents(self) -> list[str]:
        return list(self._items)

    def __contains__(self, agent_id: object) -> bool:
        return agent_id in self._items

    def __iter__(self) -> Iterator[Conversation]:
        return iter(list(self._items.values()))

    def __len__(self) -> int:
        return len(self._items)
