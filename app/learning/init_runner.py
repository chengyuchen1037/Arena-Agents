"""The initialization of ONE bot (v0.14, H19-H22; contract §13.6): it reads the contract and the rules and writes its own understanding.

:func:`run_init` builds the learning brain over an :class:`~app.learning.ports.InitPort`, runs it for at most ``deadline_s`` seconds of wall
clock (:func:`~app.learning.loop.run_learning`) and then decides what the end means - **all or nothing** (C16):

* the run ``finished`` and ``arch_notes``, ``digest`` and ``playbook`` are all written and valid -> the files are committed together and the
  bot becomes ``ready`` (``BotStore.commit_init``, stamped with the hashes the run *began* with: an edit made meanwhile shows as ``stale``);
* anything else - the clock ran out, the model could not be reached, a required file is missing, a text is over its limit - the bot becomes
  ``failed`` with the reason and **its files are exactly as they were**.

A bot that already has memory is *revised*: ``read_source("my_memory")`` gives the model what it has and it rewrites the whole set.  This
module knows no game: the words are the prompts', the scenario's and the model's.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.agents.composer import describe_situations
from app.agents.learning_brain import build_learning_brain
from app.bots.types import INIT_TIMEOUT_S, ContractDoc, LearningJob, SourceHashes
from app.learning.loop import run_learning
from app.learning.ports import InitPort
from app.storage.botfiles import BotFileError

logger = logging.getLogger(__name__)

__all__ = ["InitOutcome", "run_init"]


@dataclass
class InitOutcome:
    """How an initialization ended: ``done`` (committed), ``failed`` (the reason is in ``error``, the files are untouched), ``timeout`` (the
    wall clock ran out: failed as well).  ``files_written`` are the files the model wrote (staged), ``rounds`` the model calls made."""

    state: Literal["done", "failed", "timeout"]
    error: str | None = None
    files_written: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    rounds: int = 0


def _fail(store: Any, bot_id: str, reason: str, *, model: str, duration_s: float) -> None:
    """Record the failure on the bot (its files stay as they were); a bot that is gone has nothing to record."""
    try:
        store.fail_init(bot_id, reason, model=model, duration_s=duration_s)
    except Exception:  # noqa: BLE001 - the bot was removed while it was being initialized: nothing to mark
        logger.warning("Could not record the failed initialization of %s", bot_id, exc_info=True)


async def run_init(
    *,
    store: Any,
    bot_id: str,
    scenario_id: str,
    spec: Any,
    contract: ContractDoc,
    materials: dict[str, str],
    sources: SourceHashes,
    llm: Any,
    agent_id: str,
    model: str,
    deadline_s: int = INIT_TIMEOUT_S,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    on_response: Callable[[Any], None] | None = None,
    trace: Any = None,
    max_tokens: int | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> InitOutcome:
    """Initialize ``bot_id`` for ``scenario_id`` with ``llm`` (asked as ``agent_id``) and commit the result - or leave everything as it was.

    ``spec`` is the loaded scenario (its player rules and situation tags), ``contract`` the official contract, ``materials`` the study
    materials, ``sources`` the hashes of what the initialization depends on *now* (they are stamped on the result), ``model`` the name of the model
    (shown on the bot).  A cancellation of the calling task propagates and changes nothing."""
    started = clock()
    core_hash_at_start = store.core_hash_of(bot_id)
    mind = store.load_mind(bot_id, scenario_id, memory=True)
    if not mind.core.complete:
        labels = {"identity": "身份", "voice": "说话方式", "mind": "分析方式"}
        reason = f"底层设定不完整：缺少{'、'.join(f'「{labels[name]}」' for name in mind.core.missing)}，先写好再初始化。"
        _fail(store, bot_id, reason, model=model, duration_s=0.0)
        return InitOutcome("failed", reason, [], 0.0, 0)
    job = LearningJob(
        kind="init",
        mind=mind,
        scenario_id=scenario_id,
        scenario_title=str(getattr(spec, "title", "") or scenario_id),
        contract=contract,
        deadline_s=int(deadline_s),
    )
    port = InitPort(
        mind=mind,
        contract=contract,
        rules_text=str(getattr(spec, "rules_text", "") or ""),
        situations=describe_situations(spec),
        materials=materials,
        stored=lambda part: store.read_part(bot_id, part, scenario_id),
        known_tags=[str(getattr(rule, "tag", "")) for rule in (getattr(spec, "situations", None) or []) if getattr(rule, "tag", "")],
    )
    brain = build_learning_brain(job, port)
    result = await run_learning(
        brain, llm, agent_id, deadline_s=deadline_s, on_progress=on_progress, trace=trace, on_response=on_response,
        max_tokens=max_tokens, clock=clock,
    )
    duration = round(clock() - started, 1)
    written = port.written
    if result.status == "timeout":
        reason = f"时间到了（{deadline_s} 秒），初始化没有完成，原来的记忆没有改动。"
        _fail(store, bot_id, reason, model=model, duration_s=duration)
        return InitOutcome("timeout", reason, written, duration, result.rounds)
    if result.status == "error":
        reason = f"调用模型失败（{result.error}），原来的记忆没有改动，可以重试。"
        _fail(store, bot_id, reason, model=model, duration_s=duration)
        return InitOutcome("failed", reason, written, duration, result.rounds)
    missing_files = port.missing
    if missing_files:
        reason = f"没有写完必需的文件：{'、'.join(missing_files)}，原来的记忆没有改动，可以重试。"
        _fail(store, bot_id, reason, model=model, duration_s=duration)
        return InitOutcome("failed", reason, written, duration, result.rounds)
    try:
        store.commit_init(
            bot_id,
            scenario_id,
            dict(port.staged),
            model=model,
            duration_s=duration,
            sources=sources,
            core_hash_at_start=core_hash_at_start,
        )
    except BotFileError as exc:  # a text that does not pass the store's own check: all or nothing
        reason = f"写入没有通过检查：{exc}。原来的记忆没有改动，可以重试。"
        _fail(store, bot_id, reason, model=model, duration_s=duration)
        return InitOutcome("failed", reason, written, duration, result.rounds)
    except Exception as exc:  # noqa: BLE001 - the bot was removed meanwhile, the disk is full ...: nothing was written
        logger.warning("The initialization of %s could not be committed", bot_id, exc_info=True)
        reason = f"保存失败（{type(exc).__name__}），原来的记忆没有改动，可以重试。"
        _fail(store, bot_id, reason, model=model, duration_s=duration)
        return InitOutcome("failed", reason, written, duration, result.rounds)
    return InitOutcome("done", None, written, duration, result.rounds)
