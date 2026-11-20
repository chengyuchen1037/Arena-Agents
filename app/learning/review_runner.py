"""The review of ONE bot after a game (v0.14, H31-H34; contract §13.6): it holds its memory against what really happened.

:func:`run_review` gives the bot the truth the game made public - its own result and identity, everybody's - plus what it noted with ``jot``
during the game and, when a person played its seat for a while, which part of the record that was (H34); runs the learning brain over a
:class:`~app.learning.ports.ReviewPort` for at most ``deadline_s`` seconds of wall clock; and counts the game in the bot's record.

* every write of the bot goes into its memory **at once and for good** (checked, atomic): when the clock runs out what was written stays (C16);
* a change of its L0 is only ever a **proposal** the user decides on (H34);
* the bot is told which part of the record a person played *only if it was its own seat* - nobody else is told who the person was (they
  learn from the true record: what the person said and was right about counts for the others as what it was);
* nothing is said on the bot's behalf: the review is the bot's own work with its own model (H18).

The pieces that assemble the :class:`~app.bots.types.LearningJob` (:func:`outcome_text`, :func:`role_text`, :func:`others_text`,
:func:`human_note`, :func:`jots_of`) are plain functions of the finished world, so they can be tested without a model.  This module knows no
game: identities and factions are named by the scenario.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.agents.composer import describe_situations
from app.agents.context import BOT, Namer, render_entry
from app.agents.learning_brain import build_learning_brain
from app.bots.types import JOT_NOTE_TITLE, REVIEW_TIMEOUT_S, ContractDoc, LearningJob
from app.game.outcome import player_won, role_label
from app.learning.loop import run_learning
from app.learning.ports import ReviewPort
from app.storage import botfiles

logger = logging.getLogger(__name__)

__all__ = [
    "ReviewOutcome",
    "build_review_job",
    "human_note",
    "jots_of",
    "others_text",
    "outcome_text",
    "role_text",
    "run_review",
]

#: At most this many of a person's actions are listed in the note to the bot whose seat they played.
HUMAN_ACTIONS_SHOWN = 12


@dataclass
class ReviewOutcome:
    """How a review ended: ``done`` (the bot finished or had nothing more to write), ``timeout`` (the clock ran out; what was written
    stays), ``error`` (the model could not be reached: nothing was counted, it can be asked again).  ``changes`` is what it changed
    (``{"files": [{"file", "before", "after"}], "proposals": n}``)."""

    state: Literal["done", "timeout", "error"]
    message: str = ""
    changes: dict[str, Any] = field(default_factory=lambda: {"files": [], "proposals": 0})
    rounds: int = 0
    duration_s: float = 0.0


# ------------------------------------------------------------------------------------------------ the truth of the game
def _faction_label(spec: Any, faction: str) -> str:
    names = getattr(getattr(spec, "setup", None), "faction_names", None) or {}
    return str(names.get(faction) or faction or "无阵营")


def _result_word(won: bool | None) -> str:
    return {True: "获胜", False: "失败", None: "结果不明"}[won]


def outcome_text(spec: Any, world: Any, seat_id: str) -> str:
    """How the game ended for this seat, as the engine says it (never as the bot would)."""
    player = world.player(seat_id)
    won = player_won(spec, getattr(player, "faction", ""), world.state.winner)
    return {
        True: "你所在的阵营获胜了。",
        False: "你所在的阵营失败了。",
        None: f"这局的结果是：{world.state.winner or '未知'}。",
    }[won]


def role_text(spec: Any, world: Any, seat_id: str) -> str:
    """The identity of this seat (public once the game is over)."""
    player = world.player(seat_id)
    faction = getattr(player, "faction", "")
    return f"{role_label(spec, getattr(player, 'role', ''))}（{_faction_label(spec, faction)}）"


def others_text(spec: Any, world: Any) -> str:
    """Who was who: every player's identity, faction and result, one line each (the identities are public once the game is over)."""
    lines = []
    for player in world.state.players:
        won = player_won(spec, player.faction, world.state.winner)
        lines.append(
            f"- {player.id} {player.name}：{role_label(spec, player.role)}（{_faction_label(spec, player.faction)}）· {_result_word(won)}"
        )
    return "\n".join(lines)


def jots_of(world: Any, seat_id: str) -> tuple[str, ...]:
    """What the bot noted with ``jot`` during the game (the lines of the world note ``JOT_NOTE_TITLE``)."""
    note = str(world.memory(seat_id).notes.get(JOT_NOTE_TITLE, "") or "")
    return tuple(line.strip() for line in note.splitlines() if line.strip())


def human_note(world: Any, spans: Sequence[Any], seat_id: str, namer: Namer) -> str:
    """What the bot is told when a person played ITS seat for a while (H34): which stretches of the record, and what they did there.
    ``""`` when nobody did - and nobody but the bot of that seat ever gets it: the others are not told who the person was."""
    mine = [span for span in spans if str(getattr(span, "seat", "")) == seat_id]
    if not mine:
        return ""
    last = int(world.seq)
    stretches: list[tuple[int, int]] = []
    for span in mine:
        begin = int(getattr(span, "from_seq", 0) or 0)
        end = getattr(span, "to_seq", None)
        stretches.append((begin, last if end is None else int(end)))
    shown: list[str] = []
    total = 0
    for entry in world.entries_visible(seat_id):
        seq = int(entry.seq)
        if not any(begin < seq <= end for begin, end in stretches):
            continue
        own = (entry.type == "message" and entry.actor_id == seat_id and entry.data.get("kind") in ("speech", "template")) or (
            entry.type == "request.answered" and entry.data.get("player_id") == seat_id
        )
        if not own:
            continue
        total += 1
        line = render_entry(entry, namer, viewer=seat_id, style=BOT)
        if line and len(shown) < HUMAN_ACTIONS_SHOWN:
            shown.append(f"- {line[:160]}")
    where = "；".join(f"#{begin} 到 #{end}" for begin, end in stretches)
    parts = [
        f"这局里，你的座位有一段时间是一个人类玩家代你出场的（记录 {where}）：他看到的是你看到的，以你的名义发出的发言和作答是他自己说的、选的，不是你想出来的。"
    ]
    if shown:
        parts.append(f"他做了 {total} 件事，其中{'这些' if total <= len(shown) else f'前 {len(shown)} 件'}是：\n" + "\n".join(shown))
    else:
        parts.append("这段时间里他没有以你的名义发过言或作答。")
    return "\n".join(parts)


def build_review_job(
    *,
    mind: Any,
    spec: Any,
    scenario_id: str,
    contract: ContractDoc,
    seat_id: str,
    world: Any,
    spans: Sequence[Any],
    learn_style: bool,
    namer: Namer,
    deadline_s: int,
) -> LearningJob:
    """The job of one bot's review, assembled from the finished game."""
    note = human_note(world, spans, seat_id, namer)
    return LearningJob(
        kind="review",
        mind=mind,
        scenario_id=scenario_id,
        scenario_title=str(getattr(spec, "title", "") or scenario_id),
        contract=contract,
        deadline_s=int(deadline_s),
        seat_id=seat_id,
        outcome_text=outcome_text(spec, world, seat_id),
        role_text=role_text(spec, world, seat_id),
        others_text=others_text(spec, world),
        human_note=note,
        learn_style=bool(learn_style and note),
        jots=jots_of(world, seat_id),
    )


def namer_of(world: Any, seat_id: str) -> Namer:
    channels = {str(c.get("id")): str(c.get("name")) for c in (world.board(seat_id).get("channels") or [])}
    return Namer(world.display_name, channels)


# ------------------------------------------------------------------------------------------------ the run
async def run_review(
    *,
    store: Any,
    bot_id: str,
    seat_id: str,
    session_id: str,
    scenario_id: str,
    world: Any,
    spec: Any,
    contract: ContractDoc,
    materials: dict[str, str],
    spans: Sequence[Any],
    learn_style: bool,
    llm: Any,
    agent_id: str | None = None,
    deadline_s: int = REVIEW_TIMEOUT_S,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    on_response: Callable[[Any], None] | None = None,
    trace: Any = None,
    max_tokens: int | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ReviewOutcome:
    """Review the finished game of ``world`` for the bot ``bot_id`` that sat at ``seat_id`` (the model is asked as ``agent_id``, default the seat).

    Writes land in the bot's memory as they are made; the game is counted in its record when the review ends - ``done`` or ``timeout``.  An
    ``error`` (the model could not be reached) counts nothing and can be repeated.  A cancellation of the calling task propagates."""
    started = clock()
    stored_people = list(botfiles.parse_people(store.read_part(bot_id, "people")))
    mind = store.load_mind(bot_id, scenario_id, memory=True, others=stored_people)
    namer = namer_of(world, seat_id)
    job = build_review_job(
        mind=mind, spec=spec, scenario_id=scenario_id, contract=contract, seat_id=seat_id, world=world, spans=spans,
        learn_style=learn_style, namer=namer, deadline_s=deadline_s,
    )
    known_tags = [str(getattr(rule, "tag", "")) for rule in (getattr(spec, "situations", None) or []) if getattr(rule, "tag", "")]
    port = ReviewPort(
        mind=mind,
        contract=contract,
        rules_text=str(getattr(spec, "rules_text", "") or ""),
        situations=describe_situations(spec),
        materials=materials,
        stored=lambda part: store.read_part(bot_id, part, scenario_id),
        write_part=lambda part, text: store.write_part(bot_id, part, text, scenario_id, known_tags=known_tags),
        stage_core=lambda section, text, reason: store.stage_core(bot_id, section, text, reason, f"复盘 {session_id}"),
        entries=lambda: world.entries_visible(seat_id),
        namer=lambda: namer,
        viewer=seat_id,
    )
    brain = build_learning_brain(job, port)
    result = await run_learning(
        brain, llm, agent_id or seat_id, deadline_s=deadline_s, on_progress=on_progress, trace=trace, on_response=on_response,
        max_tokens=max_tokens, clock=clock,
    )
    duration = round(clock() - started, 1)
    changes = port.changes()
    if result.status == "error":
        return ReviewOutcome(
            "error", f"调用模型失败（{result.error}），这次复盘没有记入，可以重试。", changes, result.rounds, duration
        )
    player = world.player(seat_id)
    try:
        store.add_record(
            bot_id, role_label(spec, getattr(player, "role", "")), player_won(spec, getattr(player, "faction", ""), world.state.winner)
        )
    except Exception:  # noqa: BLE001 - a bot that was removed meanwhile has no record to keep
        logger.warning("Could not count the game in the record of %s", bot_id, exc_info=True)
    if result.status == "timeout":
        return ReviewOutcome("timeout", f"时间到了（{deadline_s} 秒），已经写进去的保留了。", changes, result.rounds, duration)
    return ReviewOutcome("done", "复盘完成。" if (changes["files"] or changes["proposals"]) else "复盘完成，没有需要改动的。", changes, result.rounds, duration)
