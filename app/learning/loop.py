"""The loop of a learning run: one bot, one model, a few tools, a clock (v0.14, H20, H32, H54; contract §13.6).

A learning run - an *initialization* or the *review* after a game - is the bot studying: it reads sources, writes its memory,
says when it is done.  It is **not** an agent of the game: there is no world, no envelope, no actor, no soft pause (H54) - just the
message / tool protocol of the worker (:mod:`app.runtime.messages`, :class:`~app.runtime.ports.ToolOutcome`) over any
:class:`~app.runtime.ports.LlmPort`, and one hard limit: the **wall clock**.

* ``asyncio.timeout(deadline_s)`` wraps the whole run, a model call that is in flight included (it is cancelled when the time is up);
  waiting for a free model slot counts too - the bot has what the clock has, no more;
* there is no limit on thinking and no limit on rounds apart from a safety net of :data:`MAX_ROUNDS`;
* an answer without a tool call gets a nudge once (the brain says what to do); the second one ends the run - what only stands in an
  answer is not kept;
* every tool call of an answer is carried out, in order, and every call gets its result before the model is asked again; a call whose
  arguments are not a JSON object never reaches the brain (the model is told, as in a game).

The loop knows nothing of memory files: the *brain* (``app.agents.learning_brain``) turns tool calls into writes through its port, and
the runners (:mod:`app.learning.init_runner`, :mod:`app.learning.review_runner`) say what the end of a run means.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.runtime.messages import (
    args_error_text,
    assistant_text_message,
    assistant_tool_message,
    ensure_call_ids,
    parse_call,
    system_message,
    tool_failure_text,
    tool_message,
    user_message,
)
from app.runtime.ports import LlmPort, ToolOutcome
from app.runtime.trace_log import redact_text

logger = logging.getLogger(__name__)

__all__ = ["CUT_OFF_NUDGE", "MAX_ROUNDS", "LearningBrainLike", "LoopResult", "run_learning"]

#: The safety net: a run that has not ended after this many rounds is stopped (the wall clock is the real limit).
MAX_ROUNDS = 60
#: Said once when an answer was cut off by the length limit and called no tool.
CUT_OFF_NUDGE = "（系统提示）上一次输出因长度上限中断，没有完成任何操作。请直接用工具完成当前要做的事，写完调用 finish。"
TRUNCATION_REASONS = frozenset({"length", "max_tokens"})


class LearningBrainLike(Protocol):
    """What the loop asks of a brain (``app.agents.learning_brain.LearningBrain``)."""

    def system_prompt(self) -> str: ...

    def first_message(self) -> str: ...

    def tool_schemas(self) -> list[dict[str, Any]]: ...

    def execute_tool(self, name: str, args: dict[str, Any]) -> ToolOutcome: ...

    def no_tool_nudge(self, content: str, round_idx: int) -> str | None: ...


@dataclass
class LoopResult:
    """How a run ended: ``finished`` (the brain ended it - or the model had nothing more to say), ``timeout`` (the wall clock ran out),
    ``error`` (the model could not be asked; ``error`` says why, in Chinese).  ``rounds`` model calls were made; ``capped`` says that the
    safety net of :data:`MAX_ROUNDS` ended a run that was still going."""

    status: Literal["finished", "timeout", "error"]
    rounds: int = 0
    error: str | None = None
    capped: bool = False


def error_text(exc: BaseException) -> str:
    """What may be shown of a failed model call (on the card of a bot, in an event, in the log): its class and its own text - cut, and scrubbed of
    anything that looks like a credential (``sk-…``, ``Bearer …``, ``api_key=…``): a provider's error may echo what it was sent."""
    try:
        text = redact_text(str(exc).strip())
    except Exception:  # noqa: BLE001
        text = ""
    name = type(exc).__name__
    return f"{name}：{text[:200]}" if text else name


async def run_learning(
    brain: LearningBrainLike,
    llm: LlmPort,
    agent_id: str,
    *,
    deadline_s: float,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    trace: Any = None,
    on_response: Callable[[Any], None] | None = None,
    max_tokens: int | None = None,
    max_rounds: int = MAX_ROUNDS,
    clock: Callable[[], float] = time.monotonic,
) -> LoopResult:
    """Run ``brain`` against ``llm`` until it finishes, the model has nothing more to say, or ``deadline_s`` seconds of wall clock have passed.

    ``agent_id`` is what the port is asked for (the model binding is found by it: a bot's id for an initialization, a seat's id for a
    review).  ``on_progress({"rounds", "written", "elapsed_s"})`` is called after every round; ``on_response(resp)`` after every answer
    of the model (the runners account the tokens in it); ``trace`` (``write(event, agent_id, **fields)``) gets ``llm_call`` /
    ``tool_call`` / ``nudge`` lines - never a prompt.  Never raises for what a model or a brain does (that is :class:`LoopResult`); a
    cancellation of the calling task propagates."""
    started = clock()
    state = {"rounds": 0}
    wall_clock = asyncio.timeout(float(deadline_s))
    try:
        async with wall_clock:
            return await _rounds(
                brain, llm, agent_id, state, started, on_progress, trace, on_response, max_tokens, max_rounds, clock
            )
    except TimeoutError as exc:
        # the wall clock is the only thing that makes this a "timeout" of the run; a call that timed out by itself is a failed call
        if wall_clock.expired():
            _trace(trace, "end", agent_id, status="timeout", rounds=state["rounds"], elapsed_s=round(clock() - started, 1))
            return LoopResult("timeout", state["rounds"], "时间到了。")
        _trace(trace, "end", agent_id, status="error", rounds=state["rounds"], error=error_text(exc))
        return LoopResult("error", state["rounds"], "模型调用超时：" + error_text(exc))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed call, a bug of a port: this run failed, the others go on
        logger.warning("A learning run of %s failed", agent_id, exc_info=True)
        _trace(trace, "end", agent_id, status="error", rounds=state["rounds"], error=error_text(exc))
        return LoopResult("error", state["rounds"], error_text(exc))


def _trace(trace: Any, event: str, agent_id: str, **fields: Any) -> None:
    if trace is None:
        return
    try:
        trace.write(event, agent_id, **fields)
    except Exception:  # noqa: BLE001 - a log must never end a run
        logger.debug("could not write a learning trace line", exc_info=True)


def _progress(brain: Any, on_progress: Callable[[dict[str, Any]], None] | None, rounds: int, started: float, clock: Callable[[], float]) -> None:
    if on_progress is None:
        return
    try:
        on_progress(
            {"rounds": rounds, "written": list(getattr(brain, "written", ()) or ()), "elapsed_s": round(clock() - started, 2)}
        )
    except Exception:  # noqa: BLE001 - the progress is for the page: it must never end a run
        logger.debug("a learning progress callback failed", exc_info=True)


def _short(value: Any, limit: int = 160) -> Any:
    """``value`` for a log line: long texts are cut (a log line is no place for what a bot wrote or read in full)."""
    if isinstance(value, str):
        return value if len(value) <= limit else f"{value[:limit]}…[{len(value)} 字]"
    if isinstance(value, dict):
        return {str(k): _short(v, limit) for k, v in list(value.items())[:12]}
    if isinstance(value, (list, tuple)):
        return [_short(v, limit) for v in list(value)[:12]]
    return value


#: The arguments of a tool whose value is a name or a number - which source, which file, which section, a keyword, a page - and is logged as it is.
#: Every other text (the content of a memory file, a reason, a summary: what the bot wrote) is logged as its length only.
LOGGED_ARGS = frozenset({"source", "part", "file", "section", "query", "start", "limit"})
ARG_TEXT_CHARS = 60


def _loggable_args(value: Any) -> Any:
    """The arguments of a tool call as the log shows them: the name-like ones as they are (cut when long), every other text as ``（N 字）`` - the log
    says what was done, not what was written."""
    if not isinstance(value, dict):
        return _short(value, ARG_TEXT_CHARS)
    out: dict[str, Any] = {}
    for key, item in list(value.items())[:12]:
        if isinstance(item, str) and str(key) not in LOGGED_ARGS:
            out[str(key)] = f"（{len(item)} 字）"
        else:
            out[str(key)] = _short(item, ARG_TEXT_CHARS)
    return out


async def _rounds(
    brain: LearningBrainLike,
    llm: LlmPort,
    agent_id: str,
    state: dict[str, int],
    started: float,
    on_progress: Callable[[dict[str, Any]], None] | None,
    trace: Any,
    on_response: Callable[[Any], None] | None,
    max_tokens: int | None,
    max_rounds: int,
    clock: Callable[[], float],
) -> LoopResult:
    messages: list[dict[str, Any]] = [system_message(brain.system_prompt()), user_message(brain.first_message())]
    tools = copy.deepcopy(brain.tool_schemas())
    cut_off_retried = False
    while state["rounds"] < max_rounds:
        round_idx = state["rounds"]
        state["rounds"] = round_idx + 1
        began = clock()
        resp = await llm.complete(agent_id, messages, tools, max_tokens=max_tokens)
        if on_response is not None:
            try:
                on_response(resp)
            except Exception:  # noqa: BLE001 - accounting must never end a run
                logger.debug("a learning response callback failed", exc_info=True)
        _trace(
            trace,
            "llm_call",
            agent_id,
            round=round_idx,
            provider=getattr(resp, "provider_id", None),
            model=getattr(resp, "model_id", None),
            prompt_tokens=getattr(resp, "prompt_tokens", None),
            completion_tokens=getattr(resp, "completion_tokens", None),
            cache_read_tokens=getattr(resp, "cache_read_tokens", None),
            finish_reason=getattr(resp, "finish_reason", None),
            tool_calls=[call.name for call in getattr(resp, "tool_calls", [])],
            elapsed_ms=int((clock() - began) * 1000),
        )
        if not resp.tool_calls:
            reply = assistant_text_message(resp)
            if reply is not None:
                messages.append(reply)  # what the model said stays part of its conversation
            if str(resp.finish_reason or "").lower() in TRUNCATION_REASONS and not cut_off_retried:
                cut_off_retried = True
                nudge: str | None = CUT_OFF_NUDGE
            else:
                nudge = _brain_nudge(brain, resp.content or "", round_idx)
            _progress(brain, on_progress, state["rounds"], started, clock)
            if not nudge:
                _trace(trace, "end", agent_id, status="finished", rounds=state["rounds"], why="no_tool", elapsed_s=round(clock() - started, 1))
                return LoopResult("finished", state["rounds"])
            _trace(trace, "nudge", agent_id, round=round_idx)
            messages.append(user_message(nudge))
            continue
        ensure_call_ids(resp, prefix=f"call_{round_idx}")
        results: list[dict[str, Any]] = []
        ends = False
        for call in resp.tool_calls:
            name = str(call.name or "")
            args, error = parse_call(call)
            if error:
                text = args_error_text(name, error)
            else:
                try:
                    outcome = brain.execute_tool(name, args)
                except Exception as exc:  # noqa: BLE001 - keep the call paired with a result, as the worker does
                    logger.warning("learning tool %s failed", name, exc_info=True)
                    outcome = ToolOutcome(text=tool_failure_text(name, exc))
                if not isinstance(outcome, ToolOutcome):
                    outcome = ToolOutcome(text=str(outcome))
                text = outcome.text
                ends = ends or outcome.ends_run
            results.append(tool_message(call.id, text))
            _trace(
                trace,
                "tool_call",
                agent_id,
                round=round_idx,
                name=name,
                args=_loggable_args(args) if not error else {"_error": _short(error)},
                result=_short(text, 200),
                result_chars=len(text),
            )
        messages.append(assistant_tool_message(resp))
        messages.extend(results)
        _progress(brain, on_progress, state["rounds"], started, clock)
        if ends:
            _trace(trace, "end", agent_id, status="finished", rounds=state["rounds"], why="finish", elapsed_s=round(clock() - started, 1))
            return LoopResult("finished", state["rounds"])
    _trace(trace, "end", agent_id, status="finished", rounds=state["rounds"], why="max_rounds", elapsed_s=round(clock() - started, 1))
    return LoopResult("finished", state["rounds"], capped=True)


def _brain_nudge(brain: LearningBrainLike, content: str, round_idx: int) -> str | None:
    try:
        return brain.no_tool_nudge(content, round_idx)
    except Exception:  # noqa: BLE001 - a brain that cannot nudge simply lets the run end
        logger.warning("a learning brain failed to nudge", exc_info=True)
        return None
