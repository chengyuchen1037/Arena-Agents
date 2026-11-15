"""Generic "LLM + tools" multi-round turn for a participant, plus an isolated batch runner.

Every participant interaction (speech, concurrent speech, yes/no inquiry, planning,
voting, interruption, structured choice) used to be a hand-copied loop.  They all
share the same skeleton:

    for each round:  stream an LLM answer -> record the thought -> either handle
                     "no tool call" (finish or nudge) or execute each tool call
                     (special tools first, shared knowledge/note tools otherwise)

:class:`ParticipantTurn` owns that skeleton; concrete turns only override a few hooks.

Guarantees provided here (engine stability contract):

* The turn captures the ``GameSession`` it started in and only ever writes to it, so
  a straggler can never write into a session that was replaced by *new game* / *import*.
* :func:`run_batch` runs turns concurrently with per-participant isolation: one failing
  participant is degraded (or the batch aborted, per scenario policy) instead of silently
  leaving orphan tasks behind, and on cancellation every child is cancelled and awaited.
* Public records produced by batch turns are buffered and published together, in
  participant order, only after the whole batch succeeded.  A forced pause in the middle of
  a batch therefore leaves no half-published votes behind, and resuming re-runs the batch
  cleanly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from app.engine.llm import LLMResponse
from app.engine.state import GameSession
from app.engine.texts import participant_error_policy
from app.engine.toolargs import args_error_result, parse_tool_args
from app.engine.tools import execute_shared_tool
from app.llm.types import PROVIDER_PRIVATE_KEY

logger = logging.getLogger(__name__)

_DEPTH_INDEX = {"low": 0, "medium": 1, "high": 2, "xhigh": 3}
_BUDGETS: dict[str, tuple[int, int, int, int]] = {
    "speak": (2, 4, 6, 10),
    "decision": (2, 3, 5, 8),
    "plan": (2, 4, 6, 8),
}


class SessionReplacedError(RuntimeError):
    """The game this work belonged to was replaced while the work was in flight."""


def turn_budget(session: GameSession, kind: str) -> int:
    """Maximum LLM rounds for a turn of *kind* at the session's execution depth."""
    depth = getattr(session, "execution_depth", "medium")
    return _BUDGETS[kind][_DEPTH_INDEX.get(depth, 1)]


def assistant_tool_message(resp: LLMResponse) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": resp.content or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json or "{}"},
            }
            for call in resp.tool_calls
        ],
    }
    # Provider-private blocks (e.g. signed thinking) let the *same* provider resume the turn;
    # adapters ignore them for any other provider.
    private = getattr(resp, "provider_private", None)
    if private:
        message[PROVIDER_PRIVATE_KEY] = private
    return message


def log_participant_tool(
    session: GameSession,
    player_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_result: str,
    channel_id: str | None,
    *,
    action_id: str | None = None,
    action_type: str | None = None,
) -> None:
    session.add_record(
        stream="channel",
        channel_id=channel_id,
        kind="tool_call",
        actor_id=player_id,
        tool_name=tool_name,
        tool_args=tool_args,
        tool_result=tool_result,
        content=f"{tool_name}({json.dumps(tool_args, ensure_ascii=False)}) -> {tool_result}",
        action_id=action_id or session.active_action_id,
        action_type=action_type or session.active_action_type,
    )


def tool_json(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


class ParticipantTurn:
    """One participant's multi-round tool-using turn.  Subclass and override the hooks."""

    # --- static description of the turn (override in subclasses) -----------------
    budget_kind = "decision"
    action_tag = "turn"
    action_type = "turn"
    round_start_event: str | None = None  # broadcast at the start of every round
    tool_events = False  # broadcast before/after every tool call
    done_event: str | None = None  # broadcast when the turn ends (success or not)
    stage_thought: str | None = "思维链思考中"
    stage_content: str | None = None
    status_prefix = False  # mirror the stage into ``active_status_text``
    broadcast_thoughts = True
    last_round_without_tools = False
    claim_active_action = False  # set session.active_action_id/type while running

    def __init__(
        self,
        orch: Any,
        player_id: str,
        channel_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        rebuild: Any = None,
        defer_records: bool = False,
        budget: int | None = None,
    ) -> None:
        self.orch = orch
        # Captured once: this turn never writes into a replaced session.
        self.session: GameSession = orch.session
        self.pid = player_id
        self.channel_id = channel_id
        self.messages: list[dict[str, Any]] = list(messages)
        self.tools = tools
        self._rebuild = rebuild
        self.defer_records = defer_records
        self.budget = budget if budget is not None else turn_budget(self.session, self.budget_kind)
        participant = self.session.get_participant(player_id)
        self.name = participant.name if participant else player_id
        self.action_id = f"act_{self.action_tag}_{player_id}_{uuid.uuid4().hex[:8]}"
        self.finished = False
        self.failure: BaseException | None = None
        self.pending_records: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ hooks ---
    async def handle_tool(self, name: str, args: dict[str, Any]) -> str | None:
        """Handle a turn-specific tool; return its JSON result, or None for shared tools."""
        return None

    async def on_no_tool_calls(
        self, resp: LLMResponse, round_idx: int, is_last: bool
    ) -> tuple[str, str] | None:
        """Called when a round produced no tool call.

        Return None to end the turn, or ``(assistant_text, user_nudge)`` to continue.
        """
        return None

    async def finalize(self) -> None:
        """Produce results / records after the last round (success path)."""

    def degrade(self, exc: BaseException) -> None:
        """Fill in a safe result when this turn failed inside a batch."""

    # --------------------------------------------------------------- plumbing ---
    def emit_record(self, **fields: Any) -> None:
        fields.setdefault("action_id", self.action_id)
        fields.setdefault("action_type", self.action_type)
        if self.defer_records:
            self.pending_records.append(fields)
        else:
            self.session.add_record(**fields)

    async def _broadcast(self, event: str) -> None:
        await self.orch.broadcast_state(event)

    def _draft(self, thought: str = "", content: str = "") -> None:
        self.session.update_streaming_draft(
            actor_id=self.pid,
            actor_name=self.name,
            channel_id=self.channel_id,
            thought_text=thought,
            content_text=content,
            action_id=self.action_id,
            action_type=self.action_type,
        )

    async def _on_chunk(self, thought: str, content: str) -> None:
        self._draft(thought, content)
        stage = None
        if content and self.stage_content:
            stage = self.stage_content
        elif not content and thought and self.stage_thought:
            stage = self.stage_thought
        if stage:
            self.session.set_participant_stage(self.pid, True, stage)
            if self.status_prefix:
                self.session.active_status_text = f"{self.name} {stage}"
        await self.orch._throttled_stream_broadcast()

    async def run(self) -> None:
        session = self.session
        if self.claim_active_action:
            session.active_action_id = self.action_id
            session.active_action_type = self.action_type
        try:
            await self._run_rounds()
            await self.finalize()
        finally:
            session.clear_streaming_draft(self.pid)
            if self.claim_active_action:
                session.active_action_id = None
                session.active_action_type = None
            session.set_participant_stage(self.pid, False, "")
            if self.done_event:
                await self._broadcast(self.done_event)

    async def _run_rounds(self) -> None:
        session = self.session
        for round_idx in range(self.budget):
            is_last = round_idx == self.budget - 1
            self._draft()
            if self.round_start_event:
                await self._broadcast(self.round_start_event)
            tools = None if (self.last_round_without_tools and is_last) else self.tools
            resp = await self.orch.llm.chat_stream(
                self.messages,
                is_moderator=False,
                tools=tools,
                on_chunk=self._on_chunk,
                binding=session.get_model_binding(self.pid),
            )
            session.record_response_usage(self.pid, resp)
            session.clear_streaming_draft(self.pid)

            thought = resp.thought.strip()
            if thought:
                session.add_record(
                    stream="channel",
                    channel_id=self.channel_id,
                    kind="thought",
                    actor_id=self.pid,
                    content=thought,
                    action_id=self.action_id,
                    action_type=self.action_type,
                )
                if self.broadcast_thoughts:
                    await self._broadcast("thought_added")

            if not resp.tool_calls:
                nudge = await self.on_no_tool_calls(resp, round_idx, is_last)
                if nudge is None:
                    return
                self.messages.append({"role": "assistant", "content": nudge[0]})
                self.messages.append({"role": "user", "content": nudge[1]})
                continue

            self.messages.append(assistant_tool_message(resp))
            for call in resp.tool_calls:
                await self._run_tool(call)
            if self.finished:
                return

    async def _run_tool(self, call: Any) -> None:
        session = self.session
        session.set_participant_stage(self.pid, True, f"调用工具: {call.name}")
        if self.tool_events:
            session.active_status_text = f"{self.name} 调用工具 {call.name}"
            await self._broadcast("player_tool_start")

        args, error = parse_tool_args(call)
        compacted = False
        if error:
            result = args_error_result(call.name, error)
        else:
            handled = await self.handle_tool(call.name, args)
            if handled is not None:
                result = handled
            else:
                result = self._run_shared_tool(call.name, args)
                compacted = call.name == "compact_context"
        log_participant_tool(
            session,
            self.pid,
            call.name,
            args,
            result,
            self.channel_id,
            action_id=self.action_id,
            action_type=self.action_type,
        )
        self.messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        if compacted and self._rebuild is not None and len(self.messages) >= 2:
            self.messages[1]["content"] = self._rebuild()[1]["content"]
        if self.tool_events:
            await self._broadcast("player_tool_done")

    def _run_shared_tool(self, name: str, args: dict[str, Any]) -> str:
        try:
            result = execute_shared_tool(self.pid, name, args, self.session, self.orch.cfg)
        except Exception as exc:  # noqa: BLE001 - a broken tool call must not kill the turn
            logger.exception("Shared tool %s failed for %s", name, self.pid)
            return tool_json(ok=False, error=f"工具 {name} 执行失败：{type(exc).__name__}: {exc}")
        if result is None:
            return tool_json(ok=False, error=f"未知工具 {name}")
        return result


async def run_batch(
    orch: Any,
    turns: list[ParticipantTurn],
    *,
    label: str,
) -> list[ParticipantTurn]:
    """Run *turns* concurrently with per-participant failure isolation.

    * Cancellation of the caller cancels **and awaits** every child task.
    * A failed participant is degraded (``turn.degrade``) and noted in the engine
      stream when at least one other participant succeeded; if everyone failed (a
      systemic problem such as a missing API key) or the scenario sets
      ``limits.on_participant_error: abort``, the first error is raised so the game
      pauses with a clear reason.
    * Buffered public records are published afterwards, in participant order.
    """
    if not turns:
        return turns
    session = turns[0].session
    tasks = [asyncio.ensure_future(turn.run()) for turn in turns]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    for result in results:
        if isinstance(result, (KeyboardInterrupt, SystemExit)):
            raise result
    failures = [
        (turn, result) for turn, result in zip(turns, results) if isinstance(result, BaseException)
    ]
    if failures:
        first_turn, first_error = failures[0]
        if isinstance(first_error, asyncio.CancelledError):
            first_error = RuntimeError(f"参与者 {first_turn.pid} 的{label}任务被意外取消")
        if participant_error_policy(session) == "abort" or len(failures) == len(turns):
            raise first_error
        for turn, error in failures:
            logger.error(
                "Participant %s failed during %s; degrading", turn.pid, label, exc_info=error
            )
            turn.failure = error
            turn.degrade(error)
            session.add_record(
                stream="engine",
                kind="note",
                actor_id="moderator",
                content=(
                    f"[引擎] {turn.name}({turn.pid}) 的{label}执行失败，已按降级结果继续："
                    f"{type(error).__name__}: {error}"
                ),
            )

    if session is not orch.session:
        # The game was replaced (new game / import) while we ran: publish nothing and make
        # the caller stop instead of letting it write results into the new game.
        raise SessionReplacedError(f"对局已被替换，{label}结果已丢弃")
    for turn in turns:
        for fields in turn.pending_records:
            session.add_record(**fields)
        turn.pending_records.clear()
    return turns
