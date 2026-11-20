"""``BotBrain``: the brain of one v0.14 bot (H12-H18, H53; docs/v0.12-contracts.md §13.5).

``BotBrain(world, spec, seat_id, mind, contract, ...)`` implements :class:`app.runtime.ports.Brain` for ONE seat: a game has one
instance per seat (each bot has its own memory), and every method's ``agent_id`` must be that seat.

* the **system prompt** is the Composer's (``app.agents.composer``): who the bot is, how it talks and reasons, what it understands
  of the game, what it thinks of the others, the official rules, how the platform works, and once more who it is.  It is built once
  and never changes, so the provider's prefix cache keeps hitting;
* the **opening message** of a conversation is the state board (with the limit of ONE message shown, and no "total" for a floor of a
  single message), the **hard facts** the world keeps (``board["facts"]``: what the rules produced and what players declared, by
  phase), the bot's notes, its latest actions, the visible history and why it was woken; later messages are **increments** (new
  records, what changed, why).  When a decision is due - a turn to speak, a request to answer - a short **hint** is appended
  (``Composer.event_hint``): the bot's voice, one or two of its own playbook entries found by the scenario's situation tags, and a
  question that fits the moment;
* the **tools** are the world's, ``sleep``, notes, ``jot`` and ``read_contract`` (no reference documents, H53);
* **speech is the bot's own** (H18).  This class has no way to say anything for a bot: the only thing that reaches the world is
  what the model's own tool call asks for, through :class:`~app.agents.bot_tools.BotToolExecutor`.  The one thing it does with
  a ``speak`` is *look at it* (the output gate, H15): a text that reads like a report - lines of a list, a heading, bold, a line that
  starts with the bot's own id or name - is not submitted; the tool result is a ``FORMAT`` receipt that says why, and the bot
  writes it again in its own words.  Once per turn: the same speech again goes through as it is.  Nothing is rewritten, cut or
  completed on the way, and ``speak_template`` is not looked at.

Optional hooks for the runtime's trace (never a body, only ids, layers, lengths and hashes): ``mount_manifest(agent_id)`` and
``hint_manifest(agent_id)``.
"""

from __future__ import annotations

import copy
import json
import logging
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from app.agents.bot_tools import BotToolExecutor
from app.agents.brain import DEFAULT_HISTORY_CHARS, INJECT_HISTORY_CHARS, NUDGE_TEXT, RECENT_ACTIONS_LIMIT
from app.agents.composer import Composer, ComposerBudgets, sha12
from app.agents.context import (
    BOT,
    FACT_CHARS,
    Namer,
    _secs,
    render_board,
    render_facts,
    render_history,
    render_memory,
    render_wake,
)
from app.agents.delta import diff_board
from app.agents.gates import allows_oral_lists, check_speech, format_message
from app.agents.schemas import bot_tool_schemas
from app.bots.types import BotMind, ContractDoc, MountBlock
from app.runtime.ports import ToolOutcome, WakeContext
from app.world.port import WorldPort
from app.world.spec import ScenarioSpec
from app.world.types import Code, Envelope, Receipt

logger = logging.getLogger(__name__)

HINT_HEADER = "【此刻的提醒】"
#: A choice asked of at least this many players is a collective one - a vote (``choose``) - even when not everybody who is still in
#: the game may cast it; fewer is a role acting by itself (``act``).  (Found in a real decision point: the vote for a sheriff is
#: asked of the players who did not run, so "everybody" alone took it for an action and the bot got no "what is the hardest fact".)
GROUP_CHOICE = 3


class BotBrain:
    """Implements :class:`app.runtime.ports.Brain` for one seat of a v0.14 game."""

    def __init__(
        self,
        world: WorldPort,
        spec: ScenarioSpec,
        seat_id: str,
        mind: BotMind,
        contract: ContractDoc,
        *,
        cfg: dict[str, Any] | None = None,
        memory_enabled: bool = True,
        history_chars: int | Mapping[str, int] | None = None,
        budgets: ComposerBudgets | None = None,
    ) -> None:
        self.world = world
        self.spec = spec
        self.seat_id = seat_id
        self.mind = mind
        self.contract = contract
        self.cfg = cfg or {}
        self.memory_enabled = bool(memory_enabled)
        if isinstance(history_chars, Mapping):
            history_chars = history_chars.get("player")
        self.history_chars = int(history_chars) if history_chars else DEFAULT_HISTORY_CHARS["player"]
        self.executor = BotToolExecutor(world, contract=contract)
        self.composer = Composer(
            mind,
            contract,
            spec=spec,
            memory_enabled=memory_enabled,
            seat_id=seat_id,
            seated=self._other_names(),
            budgets=budgets,
        )
        self._allow_lists = allows_oral_lists(mind.core.voice)
        self._system = self.composer.system_prompt()  # built once: never changes during a game
        self._tools = bot_tool_schemas(spec)
        self._baseline: dict[str, Any] | None = None  # the board of the last message of the conversation
        self._nudged = False
        self._recent: deque[str] = deque(maxlen=RECENT_ACTIONS_LIMIT)
        self._hint: dict[str, Any] | None = None  # the manifest of the hint the last message got (read once)
        self._l3: list[MountBlock] = []  # the blocks of the last opening message
        self._grants: dict[str, int] = {}  # channel -> the ledger seq of the floor it was granted with
        self._spoken: dict[str, int] = {}  # channel -> speeches that went through
        self._bounced: set[tuple[str, int, int]] = set()  # (channel, grant, speeches so far): the turns the FORMAT gate has used

    # ------------------------------------------------------------------------------------------------ identity
    def _other_names(self) -> list[str] | None:
        """The names of the other players (the impressions of anybody else are not mounted); ``None`` when the world cannot say."""
        try:
            board = self.world.board(self.seat_id)
            return [str(p.get("name")) for p in board.get("players") or [] if str(p.get("id")) != self.seat_id and p.get("name")]
        except Exception:  # noqa: BLE001 - a world that cannot be asked: the snapshot's impressions are taken as they are
            return None

    def _own(self, agent_id: str) -> None:
        if agent_id != self.seat_id:
            raise ValueError(f"这个 BotBrain 只服务座位 {self.seat_id}，却被问到 {agent_id}。")

    def _names(self) -> list[str]:
        names = [self.mind.name]
        try:
            names.append(str(self.world.display_name(self.seat_id)))
        except Exception:  # noqa: BLE001
            pass
        return [n for n in dict.fromkeys(names) if n and n != self.seat_id]

    # ---------------------------------------------------------------------------------------- prompts / tools
    def system_prompt(self, agent_id: str) -> str:
        self._own(agent_id)
        return self._system

    def tool_schemas(self, agent_id: str) -> list[dict[str, Any]]:
        self._own(agent_id)
        return self._tools

    # ------------------------------------------------------------------------------------------------ messages
    def _namer(self, board: dict[str, Any]) -> Namer:
        channels = {str(c.get("id")): str(c.get("name")) for c in (board.get("channels") or [])}
        return Namer(self.world.display_name, channels)

    def _recent_text(self) -> str:
        if not self._recent:
            return "【我最近的行动】\n（暂无）"
        return "【我最近的行动】\n" + "\n".join(f"- {line}" for line in self._recent)

    def begin_run(self, agent_id: str) -> None:
        self._own(agent_id)
        self._nudged = False
        self.executor.begin_run(agent_id)

    def reset_conversation(self, agent_id: str) -> None:
        self._own(agent_id)
        self._baseline = None
        self._nudged = False

    def _note_grants(self, reasons: Sequence[Envelope]) -> None:
        """Remember the floors that were granted (the gate's "once per turn" is counted per floor)."""
        for env in reasons:
            if env.payload.get("event") == "floor.granted" and env.channel_id:
                channel = str(env.channel_id)
                self._grants[channel] = int(env.seq or 0) or self._grants.get(channel, 0) + 1

    def start_message(self, agent_id: str, ctx: WakeContext) -> tuple[str, int]:
        """FULL message of a new conversation (also the baseline for the increments that follow)."""
        self._own(agent_id)
        self._nudged = False
        self.executor.begin_run(agent_id)
        self._note_grants(ctx.reasons)
        board = self.world.board(agent_id)
        self._baseline = copy.deepcopy(board)
        memory = self.world.memory(agent_id)
        entries = self.world.entries_visible(agent_id, after_seq=memory.summary_upto_seq)
        namer = self._namer(board)
        history, omitted = render_history(entries, namer, viewer=agent_id, max_chars=self.history_chars, style=BOT)
        long_history = (
            "可见历史较长，更早的内容已省略。请把关键信息写进笔记（write_note），必要时调用 compact_context 生成摘要。"
            if omitted
            else ""
        )
        facts = render_facts(board.get("facts"), namer, max_chars=FACT_CHARS)
        blocks: list[tuple[str, str]] = [("l3.board", render_board(board, namer, hint=long_history, style=BOT))]
        if facts:
            blocks.append(("l3.facts", facts))
        blocks += [
            ("l3.memory", render_memory(memory, self.world.read_notes_text(agent_id))),
            ("l3.actions", self._recent_text()),
            ("l3.history", "【可见历史】\n" + (history or "（还没有可见的历史。）")),
            ("l3.wake", render_wake(ctx.reasons, namer, agent_id=agent_id, style=BOT)),
        ]
        hint = self._hint_text(ctx.reasons, board, None)
        if hint:
            blocks.append(("l3.hint", hint))
        self._l3 = [MountBlock(block_id, "L3", len(text), sha12(text)) for block_id, text in blocks]
        return "\n\n".join(text for _id, text in blocks), self.world.seq

    def inject_message(self, agent_id: str, ctx: WakeContext) -> tuple[str, int] | None:
        """DELTA message: new visible entries + what changed on the board + why the agent was woken (+ a hint when a decision is due)."""
        self._own(agent_id)
        self._hint = None
        self._note_grants(ctx.reasons)
        entries = self.world.entries_visible(agent_id, after_seq=ctx.after_seq)
        board = self.world.board(agent_id)
        namer = self._namer(board)
        history, _ = render_history(entries, namer, viewer=agent_id, max_chars=INJECT_HISTORY_CHARS, style=BOT)
        baseline = self._baseline
        if baseline is None:
            # The conversation outlived this brain's memory of it (e.g. a resumed game): one full refresh - the facts too.
            changes = ["（状态板完整刷新）", render_board(board, namer, style=BOT)]
            facts = render_facts(board.get("facts"), namer, max_chars=FACT_CHARS)
            if facts:
                changes.append(facts)
        else:
            changes = diff_board(baseline, board, namer, style=BOT)
        self._baseline = copy.deepcopy(board)
        if not history and not changes and not ctx.reasons:
            return None
        parts = [f"【游戏时间】{_secs(board.get('now'))}", "【新动态】", history or "（没有新的可见条目。）"]
        if changes:
            parts.append("【状态变化】\n" + "\n".join(changes))
        writable = [
            f"{c.get('id')}（{c.get('mode')}）" for c in (board.get("channels") or []) if c.get("can_write")
        ]
        parts.append("你当前可以发言的频道：" + ("、".join(writable) if writable else "（无）"))
        parts.append(render_wake(ctx.reasons, namer, agent_id=agent_id, style=BOT))
        hint = self._hint_text(ctx.reasons, board, baseline)
        if hint:
            parts.append(hint)
        return "\n".join(parts), self.world.seq

    def no_tool_nudge(self, agent_id: str, content: str, round_idx: int) -> str | None:
        self._own(agent_id)
        if self._nudged:
            return None
        duties = self.executor.duties_text(agent_id)
        if not (content or "").strip() and not duties:
            return None  # nothing was said and nothing is owed: a quiet end of the run
        self._nudged = True
        return NUDGE_TEXT + (f"\n你还有没处理完的事项：{duties}。" if duties else "")

    # ---------------------------------------------------------------------------------------- the hint (H14)
    def _identity_tags(self, board: dict[str, Any]) -> list[str]:
        """The bot's own tags on the board without the ones that only say how it stands (alive, out, a position)."""
        ui = getattr(self.spec, "ui", None)
        setup = getattr(self.spec, "setup", None)
        status = {*(getattr(ui, "status_tags", None) or []), *(getattr(ui, "out_tags", None) or []), *(getattr(setup, "public_tags", None) or [])}
        return [str(t) for t in ((board.get("me") or {}).get("tags") or []) if str(t) not in status]

    @staticmethod
    def _request_kind(request: Mapping[str, Any], board: Mapping[str, Any]) -> str:
        """``choose`` = a choice made by a group of players - a vote: every player still in the game, or at least three of them (a vote
        that only some may cast, such as one without the candidates; H14 "a vote or a choice request");
        ``act`` = a choice asked of one or two players (what a role does by itself, at night);
        ``answer`` = a question to answer in words."""
        if str(request.get("kind")) == "text":
            return "answer"
        alive = {str(p.get("id")) for p in board.get("players") or [] if not p.get("out")}
        to = {str(i) for i in request.get("to") or []}
        return "choose" if (alive and alive <= to) or len(to) >= GROUP_CHOICE else "act"

    def _moments(
        self, reasons: Sequence[Envelope], board: Mapping[str, Any], baseline: Mapping[str, Any] | None
    ) -> list[tuple[str, dict[str, str]]]:
        """The decisions that are due now: ``[(kind, facts of the moment)]`` - a floor that was just granted (or a free discussion
        that just opened for the bot) and a request it has not answered."""
        channels = {str(c.get("id")): c for c in board.get("channels") or [] if isinstance(c, Mapping)}
        requests = {str(r.get("id")): r for r in board.get("requests") or [] if isinstance(r, Mapping)}
        phase = str(board.get("phase") or "")
        moments: list[tuple[str, dict[str, str]]] = []
        seen: set[tuple[str, str]] = set()

        def add(kind: str, key: str, **facts: str) -> None:
            if (kind, key) not in seen:
                seen.add((kind, key))
                moments.append((kind, {"phase": phase, **facts}))

        for env in reasons:
            event = str(env.payload.get("event") or "")
            if event == "floor.granted":
                channel_id = str(env.channel_id or "")
                channel = channels.get(channel_id)
                if channel is not None and channel.get("can_write"):
                    add("speak", channel_id, event=event, channel_id=channel_id, channel_type=str(channel.get("type") or ""))
            elif event == "request.opened":
                request_id = str(env.payload.get("request_id") or "")
                request = requests.get(request_id)
                if request is not None and not request.get("answered"):
                    add(
                        self._request_kind(request, board),
                        request_id,
                        event=event,
                        channel_id=str(env.channel_id or ""),
                        request_kind=str(request.get("kind") or env.payload.get("kind") or ""),
                    )
        if baseline is not None:
            before = {str(c.get("id")): bool(c.get("can_write")) for c in baseline.get("channels") or [] if isinstance(c, Mapping)}
            for channel_id, channel in channels.items():
                floor = channel.get("floor") or {}
                if channel.get("can_write") and not channel.get("system") and floor.get("kind") == "free" and not before.get(channel_id):
                    add("speak", channel_id, event="free.opened", channel_id=channel_id, channel_type=str(channel.get("type") or ""))
        order = {"speak": 0, "choose": 1, "act": 2, "answer": 3}
        return sorted(moments, key=lambda moment: order.get(moment[0], 9))

    def _hint_text(self, reasons: Sequence[Envelope], board: Mapping[str, Any], baseline: Mapping[str, Any] | None) -> str:
        """The hint of this message ("" = none); the manifest of it is kept for :meth:`hint_manifest`."""
        self._hint = None
        texts: list[str] = []
        kinds: list[str] = []
        tags: list[str] = []
        entries: list[str] = []
        identity = self._identity_tags(dict(board))
        for kind, facts in self._moments(reasons, board, baseline)[:2]:
            moment_tags = self.composer.situation_tags(**facts)
            text, manifest = self.composer.event_hint(kind, moment_tags, identity_tags=identity)
            if not text:
                continue
            texts.append(text if not texts else text.removeprefix(HINT_HEADER + "\n"))
            kinds.append(kind)
            tags += [t for t in manifest["tags"] if t not in tags]
            entries += [e for e in manifest["entries"] if e not in entries]
        if not texts:
            return ""
        self._hint = {"kind": kinds[0] if len(kinds) == 1 else "+".join(kinds), "tags": tags, "entries": entries}
        return "\n".join(texts)

    def hint_manifest(self, agent_id: str) -> dict[str, Any] | None:
        """What the latest message got as a hint - ``{"kind", "tags", "entries"}`` (hashes, no text) - or ``None`` when it got none.
        Read once: asking again answers ``None``, so the runtime never writes the same hint twice."""
        self._own(agent_id)
        manifest, self._hint = self._hint, None
        return manifest

    def mount_manifest(self, agent_id: str) -> list[dict[str, Any]]:
        """The blocks the conversation started with: the system prompt's (L0 / L1 / L2) and the opening message's (L3), each as
        ``{"id", "layer", "chars", "sha"}``."""
        self._own(agent_id)
        return [block.to_dict() for block in (*self.composer.mounts(), *self._l3)]

    # ------------------------------------------------------------------------------------------------ tools
    def execute_tool(
        self, agent_id: str, name: str, args: dict[str, Any], *, gen_id: int, seen_seq: int
    ) -> ToolOutcome:
        self._own(agent_id)
        if name == "speak":
            bounced = self._format_gate(args)
            if bounced is not None:
                outcome = ToolOutcome(text=bounced.tool_json(), receipt=bounced)
                self._remember(name, args, outcome)
                return outcome
        outcome = self.executor.execute(agent_id, name, args, gen_id=gen_id, seen_seq=seen_seq)
        if name == "speak" and isinstance(args, dict) and outcome.receipt is not None and outcome.receipt.ok:
            channel = str(args.get("channel_id") or "")
            self._spoken[channel] = self._spoken.get(channel, 0) + 1
        self._remember(name, args, outcome)
        return outcome

    def _format_gate(self, args: Any) -> Receipt | None:
        """The output gate (H15): a ``FORMAT`` receipt when the speech reads like a report - and only the first time in a turn.  The
        text is only read: it is never changed, cut or completed, and when the gate lets it through it is the bot's own words."""
        if not isinstance(args, dict):
            return None
        content, channel = args.get("content"), args.get("channel_id")
        if not isinstance(content, str) or not isinstance(channel, str) or not channel:
            return None  # (the world answers a speech it cannot make sense of)
        problems = check_speech(content, ids=[self.seat_id], names=self._names(), allow_lists=self._allow_lists)
        if not problems:
            return None
        turn = (channel, self._grants.get(channel, 0), self._spoken.get(channel, 0))
        if turn in self._bounced:
            return None  # sent back once in this turn already: it goes through as it is
        self._bounced.add(turn)
        return Receipt.reject(
            Code.FORMAT, format_message(problems), rule=problems[0].rule, rules=[problem.rule for problem in problems]
        )

    def _remember(self, name: str, args: dict[str, Any], outcome: ToolOutcome) -> None:
        if name in ("sleep", "wait"):
            return
        shown = json.dumps(args, ensure_ascii=False)[:100] if isinstance(args, dict) else str(args)[:100]
        receipt = outcome.receipt
        if receipt is None:
            result = "完成"
        elif receipt.ok:
            result = f"成功（序号 #{receipt.seq}）" if receipt.seq else "成功"
        else:
            result = f"被拒绝 {receipt.code}：{receipt.message}"
        self._recent.append(f"{name}({shown}) → {result}")


__all__ = ["BotBrain", "HINT_HEADER"]
