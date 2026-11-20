"""Situation scripts: a YAML file that builds a world with the REAL engine and stops at a decision point (docs/v0.14-plan.md §6.5, H47).

A situation is *played*, never described to a model: ``bench.play`` creates a world (``World.new_game`` with a fixed seed, the identities
dealt either at creation or - like the v0.14 orchestrator - when the game "starts"), submits the steps below as intents (the moderator's
tool calls and the players' scripted speeches / answers, exactly what a model's tool call would ask for) and leaves the world where one seat
has something to decide: the floor is its, a request waits for it.  ``bench.runner`` then runs the real ``Agent`` / worker loop for that seat
until the bot calls ``speak`` / ``pass`` / ``respond`` ... by itself (H18).

The file::

    format: arena-situation
    version: 1
    id: day-vote-1                 # [a-z0-9_-]+, unique
    title: 一句话说明
    tags: [L3, vote]               # suites (L1..L4, S2 ...) and free tags
    scenario: minimal_test         # scenarios/<id>.md
    seed: 7                        # the world's seed (default 1)
    deal: start                    # start (default: deal_identities() when the game starts) | creation (new_game(deal=True))
    clock: 0                       # the game clock the world starts at
    players: {count: 4, names: [Alice, Bob, Carol, David]}   # ids are N1..Nn; default: the size of the scenario's identity pool
    roles: {N1: red, N2: red, N3: blue, N4: blue}            # an explicit deal (every seat) - or a list in seat order; default: dealt by the seed
    steps:                         # in order (see below)
      - {as: moderator, tool: create_channel, args: {channel_id: public, name: 公共频道, members: [N1, N2, N3, N4], type: public}}
      - {say: {N1: "..", N2: ".."}, channel: public}
      - {as: moderator, tool: open_request, label: vote, args: {to: [N3], kind: choice, question: 投谁？, options: [{id: N1}, {id: N2}]}}
    decision: {seat: N3, wake: redeliver, expect: [respond]}   # or seats: [N3, N4]: the situation is played once for each of them
    brief: |                       # for the judges (never shown to a bot): what the situation is about, what is true
    score: [{kind: choice, request: "@vote", correct: [N1]}]   # objective scoring of a ``respond`` decision
    judge: {L2: {...}, L3: {...}}  # per rubric (bench/judges/<id>.md): the facts the judge may use; copied into the judge items
    meta: {...}                    # anything else (pair id, expected side ...): copied into every result row

Steps (a list; every item is ONE of)::

    {as: <moderator|seat>, tool: <tool name>, args: {...}, at: <game seconds>?, label: <name>?, meta: {...}?}
    {say: {<seat>: <text>, ...}, channel: <id>}              # one ``speak`` per seat, in the order written
    {answer: <request id | @label>, values: {<seat>: <value>, ...}, reasons: {<seat>: <text>}?}   # one ``respond`` per seat
    {advance: <seconds>}                                     # the game clock moves on (timers fire)
    {tick: true}                                             # fire the timers that are due
    {running: false|true, reason: <text>?}                   # pause / resume the game (the commit gate closes / opens)
    {untimed: [<seat>, ...]}                                 # the seats a person plays: their floor in a public channel has no deadline

``tool`` is the name a model uses (moderator: ``announce``, ``send_message``, ``create_channel``, ``open_floor``, ``set_floor_queue``,
``open_request`` ...; players: ``speak``, ``speak_template``, ``pass``, ``respond``, ``use_ability``, ``challenge_moderator``); ``args`` are
its parameters.  ``at`` sets the game clock (never backwards) and fires the timers that are due *before* the step.  ``label`` names the
receipt: ``"@label"`` anywhere in later ``args`` (or as ``answer``) stands for its ``request_id``.  Refer to players by seat id in texts, not by
name: the seat under test takes the name of the bot that plays it.

``decision.wake`` says what the seat is woken by: ``redeliver`` (default: what a resumed game tells a seat again - the floor it holds, the
requests it has not answered), ``{since: <label>}`` (every envelope addressed to the seat from that step on), or a list of explicit
envelopes ``[{event: message.new, channel: talk, sender: N2}]``.  ``decision.expect`` lists the tools whose acceptance ends the sample
(default: every decision tool); a refused call (``FORMAT`` / ``TOO_LONG``) never does - the bot rewrites it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.agents.schemas import MODERATOR_TOOL_INTENTS, PLAYER_TOOL_INTENTS

SITUATION_FORMAT = "arena-situation"
SITUATION_VERSION = 1
MODERATOR = "moderator"
#: The names the seats get when a script gives none (the default bot library's names).
DEFAULT_NAMES = ("Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo")
#: What ends a sample by default: a player's tool that makes a move (``sleep`` is the lack of one, notes are no move).
DECISION_TOOLS = ("speak", "speak_template", "pass", "respond", "use_ability")
DEALS = ("start", "creation")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SEAT_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
#: ``@label`` / ``@label.key`` - a whole string of that shape is a reference to the receipt of an earlier step (anything else that
#: starts with ``@``, a mention in a speech, is plain text).
REF_RE = re.compile(r"^@([A-Za-z][A-Za-z0-9_-]{0,31})(?:\.([A-Za-z_][A-Za-z0-9_]*))?$")
_TOP_KEYS = {
    "format", "version", "id", "title", "tags", "scenario", "seed", "deal", "clock", "players", "roles", "steps", "decision", "brief", "score",
    "judge", "meta", "notes",
}
_STEP_KEYS = {"as", "tool", "args", "at", "label", "meta", "note"}
_MACRO_KEYS = {
    "say": {"say", "channel", "at", "label", "meta", "note"},
    "answer": {"answer", "values", "reasons", "at", "label", "meta", "note"},
    "advance": {"advance", "meta", "note"},
    "tick": {"tick", "at", "meta", "note"},
    "running": {"running", "reason", "at", "meta", "note"},
    "untimed": {"untimed", "at", "meta", "note"},
}
SCORE_KINDS = ("choice",)
WAKE_KEYS = {"event", "channel", "sender", "kind", "priority", "payload", "seq"}


class SituationError(ValueError):
    """A situation file that cannot be used; the message says where (``file: steps[3]: ...``) and what is wrong, in Chinese."""


@dataclass(frozen=True)
class Step:
    """One thing the world is made to do.  ``kind``: ``tool`` (an intent of ``actor``), ``advance``, ``tick`` or ``running``."""

    index: int
    kind: str
    actor: str = ""
    tool: str = ""
    args: Mapping[str, Any] = field(default_factory=dict)
    at: float | None = None
    label: str = ""
    seconds: float = 0.0  # advance
    running: bool = True  # running
    reason: str = ""  # running
    seats: tuple[str, ...] = ()  # untimed: the seats whose floor in a public channel has no deadline (the seat a person plays)
    meta: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""  # where it came from in the file (for error messages)

    def describe(self) -> str:
        if self.kind == "tool":
            return f"{self.source}（{self.actor} 的 {self.tool}）"
        return f"{self.source}（{self.kind}）"

    @property
    def is_action(self) -> bool:
        """The step is something a player or the moderator did (an intent), not a move of the clock or of the gate."""
        return self.kind == "tool"


@dataclass(frozen=True)
class WakeSpec:
    """What wakes the seat under test.  ``mode``: ``redeliver`` | ``since`` (``label``) | ``explicit`` (``envelopes``)."""

    mode: str = "redeliver"
    label: str = ""
    envelopes: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class Decision:
    """Whose decision is asked for (``seats``: the situation is played once for every one of them), what wakes it, and which tools end the
    sample."""

    seats: tuple[str, ...]
    wake: WakeSpec = field(default_factory=WakeSpec)
    expect: tuple[str, ...] = DECISION_TOOLS

    @property
    def seat(self) -> str:
        return self.seats[0]


@dataclass(frozen=True)
class Situation:
    id: str
    scenario: str
    steps: tuple[Step, ...]
    decision: Decision
    title: str = ""
    tags: tuple[str, ...] = ()
    seed: int = 1
    deal: str = "start"
    clock: float = 0.0
    player_count: int | None = None
    names: tuple[str, ...] = ()
    roles: Mapping[str, str] | tuple[str, ...] | None = None
    brief: str = ""
    score: tuple[Mapping[str, Any], ...] = ()
    judge: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)
    path: str = ""  # the file it was read from ("" = made in code)

    @property
    def is_example(self) -> bool:
        return self.id.startswith("example") or Path(self.path).name.startswith("_")

    def labels(self) -> list[str]:
        return [step.label for step in self.steps if step.label]


# ---------------------------------------------------------------------------------------------------------------------- parsing
def _fail(where: str, message: str) -> SituationError:
    return SituationError(f"{where}：{message}" if where else message)


def _mapping(value: Any, where: str, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(where, f"{what} 必须是一个字典（现在是 {type(value).__name__}）。")
    return {str(k): v for k, v in value.items()}


def _strings(value: Any, where: str, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise _fail(where, f"{what} 必须是字符串列表。")
    out = []
    for item in value:
        if not isinstance(item, (str, int)) or isinstance(item, bool):
            raise _fail(where, f"{what} 里只能是字符串（现在有 {item!r}）。")
        out.append(str(item))
    return tuple(out)


def _number(value: Any, where: str, what: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(where, f"{what} 必须是数字（现在是 {value!r}）。")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise _fail(where, f"{what} 必须是有限的数字。")
    if minimum is not None and number < minimum:
        raise _fail(where, f"{what} 不能小于 {minimum:g}（现在是 {number:g}）。")
    return number


def _check_tool(actor: str, tool: str, where: str) -> None:
    if actor == MODERATOR:
        if tool not in MODERATOR_TOOL_INTENTS:
            raise _fail(where, f"主持人没有工具 {tool!r}。可用的工具：{'、'.join(MODERATOR_TOOL_INTENTS)}。")
    elif tool not in PLAYER_TOOL_INTENTS:
        raise _fail(where, f"玩家没有工具 {tool!r}。可用的工具：{'、'.join(PLAYER_TOOL_INTENTS)}。")


def _check_actor(actor: Any, where: str) -> str:
    if not isinstance(actor, (str, int)) or isinstance(actor, bool) or not SEAT_RE.match(str(actor)):
        raise _fail(where, f"as 必须是 moderator 或一个座位号（现在是 {actor!r}）。")
    return str(actor)


def _check_keys(item: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise _fail(where, f"不认识的键：{'、'.join(unknown)}。这一项只能有：{'、'.join(sorted(allowed))}。")


def _common(item: Mapping[str, Any], where: str) -> tuple[float | None, str, dict[str, Any]]:
    at = None if item.get("at") is None else _number(item["at"], where, "at", minimum=0.0)
    label = str(item.get("label") or "")
    if label and not LABEL_RE.match(label):
        raise _fail(where, f"label {label!r} 只能用字母、数字、连字符和下划线，并以字母开头。")
    meta = _mapping(item.get("meta") or {}, where, "meta")
    return at, label, meta


def expand_steps(raw: Any, *, where: str = "steps") -> tuple[Step, ...]:
    """The raw ``steps`` list -> ``Step`` objects with the macros (``say``, ``answer``) expanded, validated as far as the file alone allows
    (the engine says the rest when the step is played)."""
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise _fail(where, "steps 必须是一个列表。")
    steps: list[Step] = []
    labels: set[str] = set()

    def add(**fields: Any) -> None:
        steps.append(Step(index=len(steps), **fields))

    for position, item in enumerate(raw):
        here = f"{where}[{position}]"
        data = _mapping(item, here, "每一步")
        macro = next((name for name in _MACRO_KEYS if name in data), "")
        if macro:
            _check_keys(data, _MACRO_KEYS[macro], here)
            at, label, meta = _common(data, here) if macro != "advance" else (None, "", _mapping(data.get("meta") or {}, here, "meta"))
            if macro == "say":
                channel = data.get("channel")
                if not isinstance(channel, str) or not channel:
                    raise _fail(here, "say 需要 channel（频道 id）。")
                speeches = _mapping(data["say"], here, "say")
                if not speeches:
                    raise _fail(here, "say 里至少要有一个座位。")
                for turn, (seat, text) in enumerate(speeches.items()):
                    seat = _check_actor(seat, f"{here}.say")
                    if not isinstance(text, str) or not text.strip():
                        raise _fail(f"{here}.say[{seat}]", "发言内容必须是非空的字符串。")
                    add(kind="tool", actor=seat, tool="speak", args={"channel_id": channel, "content": text}, at=at if turn == 0 else None,
                        label=label if turn == len(speeches) - 1 else "", meta=meta, source=f"{here}.say[{seat}]")
            elif macro == "answer":
                request = data["answer"]
                if not isinstance(request, str) or not request or (request.startswith("@") and not REF_RE.match(request)):
                    raise _fail(here, "answer 必须是请求 id（r1）或 @label。")
                _check_label_ref(request, labels, here)
                values = _mapping(data.get("values"), here, "values")
                if not values:
                    raise _fail(here, "answer 需要 values：{座位: 选项或文字}。")
                reasons = _mapping(data.get("reasons") or {}, here, "reasons")
                for turn, (seat, value) in enumerate(values.items()):
                    seat = _check_actor(seat, f"{here}.values")
                    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value):
                        raise _fail(f"{here}.values[{seat}]", "作答必须是选项 id 或文字（字符串）。")
                    args: dict[str, Any] = {"request_id": request, "value": str(value)}
                    if reasons.get(seat):
                        args["reason"] = str(reasons[seat])
                    add(kind="tool", actor=seat, tool="respond", args=args, at=at if turn == 0 else None,
                        label=label if turn == len(values) - 1 else "", meta=meta, source=f"{here}.answer[{seat}]")
            elif macro == "advance":
                add(kind="advance", seconds=_number(data["advance"], here, "advance", minimum=0.0), meta=meta, source=here)
            elif macro == "tick":
                add(kind="tick", at=at, meta=meta, source=here)
            elif macro == "untimed":
                add(kind="untimed", seats=tuple(_check_actor(s, f"{here}.untimed") for s in _strings(data["untimed"], here, "untimed")), at=at, meta=meta, source=here)
            else:  # running
                if not isinstance(data["running"], bool):
                    raise _fail(here, "running 必须是 true 或 false。")
                add(kind="running", running=data["running"], reason=str(data.get("reason") or ""), at=at, meta=meta, source=here)
            if label:
                labels.add(label)
            continue
        _check_keys(data, _STEP_KEYS, here)
        actor = _check_actor(data.get("as"), here) if "as" in data else ""
        tool = data.get("tool")
        if not actor or not isinstance(tool, str) or not tool:
            raise _fail(here, "每一步要么是 say / answer / advance / tick / running，要么要有 as 和 tool。")
        _check_tool(actor, tool, here)
        args = _mapping(data.get("args") or {}, here, "args")
        _check_label_refs(args, labels, here)
        at, label, meta = _common(data, here)
        if label in labels:
            raise _fail(here, f"label {label!r} 重复了。")
        add(kind="tool", actor=actor, tool=tool, args=args, at=at, label=label, meta=meta, source=here)
        if label:
            labels.add(label)
    return tuple(steps)


def _check_label_ref(value: str, labels: set[str], where: str) -> None:
    match = REF_RE.match(value)
    if match and match.group(1) not in labels:
        raise _fail(where, f"{value} 指向的 label 还没有出现（label 必须写在引用它的那一步之前）。")


def _check_label_refs(value: Any, labels: set[str], where: str) -> None:
    if isinstance(value, str):
        _check_label_ref(value, labels, where)
    elif isinstance(value, Mapping):
        for item in value.values():
            _check_label_refs(item, labels, where)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_label_refs(item, labels, where)


def _parse_wake(raw: Any, labels: set[str], where: str) -> WakeSpec:
    if raw is None or raw == "redeliver":
        return WakeSpec()
    if isinstance(raw, Mapping):
        _check_keys(raw, {"since"}, where)
        label = str(raw.get("since") or "")
        if label not in labels:
            raise _fail(where, f"wake.since 指向的 label {label!r} 不存在。")
        return WakeSpec(mode="since", label=label)
    if isinstance(raw, Sequence) and not isinstance(raw, str):
        envelopes = []
        for index, item in enumerate(raw):
            here = f"{where}[{index}]"
            body = _mapping(item, here, "wake 里的每一项")
            _check_keys(body, WAKE_KEYS, here)
            if not body.get("event"):
                raise _fail(here, "需要 event（例如 message.new）。")
            envelopes.append(body)
        if not envelopes:
            raise _fail(where, "wake 列表不能是空的。")
        return WakeSpec(mode="explicit", envelopes=tuple(envelopes))
    raise _fail(where, "wake 只能是 redeliver、{since: <label>} 或一个信封列表。")


def _parse_score(raw: Any, labels: set[str], where: str) -> tuple[Mapping[str, Any], ...]:
    if raw is None:
        return ()
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise _fail(where, "score 必须是一个列表。")
    out = []
    for index, item in enumerate(raw):
        here = f"{where}[{index}]"
        body = _mapping(item, here, "score 的每一项")
        if body.get("kind") not in SCORE_KINDS:
            raise _fail(here, f"score.kind 只能是 {'、'.join(SCORE_KINDS)}。")
        _check_keys(body, {"kind", "request", "correct", "wrong"}, here)
        request = body.get("request")
        if not isinstance(request, str) or not request or (request.startswith("@") and not REF_RE.match(request)):
            raise _fail(here, "choice 需要 request（请求 id 或 @label）。")
        _check_label_ref(request, labels, here)
        correct = _strings(body.get("correct"), here, "correct")
        if not correct:
            raise _fail(here, "choice 需要 correct（正确的选项 id 列表）。")
        out.append({"kind": "choice", "request": request, "correct": list(correct), "wrong": list(_strings(body.get("wrong"), here, "wrong"))})
    return tuple(out)


def _parse_judge(raw: Any, where: str) -> dict[str, Mapping[str, Any]]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, Sequence) and not isinstance(raw, (str, Mapping)):
        raw = {str(name): {} for name in raw}
    body = _mapping(raw, where, "judge")
    out: dict[str, Mapping[str, Any]] = {}
    for rubric, facts in body.items():
        if not re.match(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$", rubric):
            raise _fail(where, f"rubric 名 {rubric!r} 不合法。")
        out[rubric] = _mapping(facts or {}, f"{where}.{rubric}", "每个 rubric 的内容")
    return out


def parse_situation(data: Any, *, source: str = "") -> Situation:
    """A situation from the loaded YAML (``source`` = the file name, for the messages).  Raises :class:`SituationError`."""
    where = source
    body = _mapping(data, where, "文件的顶层")
    _check_keys(body, _TOP_KEYS, where)
    if body.get("format") != SITUATION_FORMAT:
        raise _fail(where, f"format 必须是 {SITUATION_FORMAT}（现在是 {body.get('format')!r}）。")
    if body.get("version") != SITUATION_VERSION:
        raise _fail(where, f"version 必须是 {SITUATION_VERSION}（现在是 {body.get('version')!r}）。")
    sid = body.get("id")
    if not isinstance(sid, str) or not ID_RE.match(sid):
        raise _fail(where, "id 必须是小写字母、数字、连字符和下划线，最长 64 个字符。")
    scenario = body.get("scenario")
    if not isinstance(scenario, str) or not re.match(r"^[A-Za-z0-9_-]{1,64}$", scenario):
        raise _fail(where, "scenario 必须是剧本的 id（scenarios/<id>.md）。")
    seed = body.get("seed", 1)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed <= 0:
        raise _fail(where, "seed 必须是正整数。")
    deal = body.get("deal", "start")
    if deal not in DEALS:
        raise _fail(where, f"deal 只能是 {'、'.join(DEALS)}。")
    clock = _number(body.get("clock", 0.0), where, "clock", minimum=0.0)
    count: int | None = None
    names: tuple[str, ...] = ()
    if body.get("players") is not None:
        players = _mapping(body["players"], f"{where}.players", "players")
        _check_keys(players, {"count", "names"}, f"{where}.players")
        if players.get("count") is not None:
            count = players["count"]
            if isinstance(count, bool) or not isinstance(count, int) or not 2 <= count <= 64:
                raise _fail(f"{where}.players", "count 必须是 2 到 64 之间的整数。")
        names = _strings(players.get("names"), f"{where}.players", "names")
        if len(set(n.casefold() for n in names)) != len(names):
            raise _fail(f"{where}.players", "names 里有重名（不分大小写）。")
        if count is not None and len(names) > count:
            raise _fail(f"{where}.players", "names 比 count 多。")
    roles: Mapping[str, str] | tuple[str, ...] | None = None
    raw_roles = body.get("roles")
    if raw_roles is not None:
        if isinstance(raw_roles, Mapping):
            roles = {str(k): str(v) for k, v in raw_roles.items()}
        else:
            roles = _strings(raw_roles, f"{where}.roles", "roles")
        if not roles:
            raise _fail(where, "roles 不能是空的。")
    steps = expand_steps(body.get("steps"), where=f"{where}: steps" if where else "steps")
    labels = {step.label for step in steps if step.label}
    raw_decision = _mapping(body.get("decision"), f"{where}: decision", "decision")
    _check_keys(raw_decision, {"seat", "seats", "wake", "expect"}, f"{where}: decision")
    if ("seat" in raw_decision) == ("seats" in raw_decision):
        raise _fail(f"{where}: decision", "要写 seat（一个座位）或 seats（座位列表），二选一。")
    raw_seats = _strings(raw_decision.get("seats", raw_decision.get("seat")), f"{where}: decision", "seats")
    if not raw_seats or len(set(raw_seats)) != len(raw_seats):
        raise _fail(f"{where}: decision", "seats 不能是空的，也不能有重复的座位。")
    seats = tuple(_check_actor(s, f"{where}: decision.seats") for s in raw_seats)
    expect = _strings(raw_decision.get("expect"), f"{where}: decision", "expect") or DECISION_TOOLS
    for tool in expect:
        if tool not in PLAYER_TOOL_INTENTS:
            raise _fail(f"{where}: decision", f"expect 里的 {tool!r} 不是玩家的工具。")
    decision = Decision(seats=seats, wake=_parse_wake(raw_decision.get("wake"), labels, f"{where}: decision.wake"), expect=tuple(expect))
    for step in steps:
        if step.kind == "tool" and step.actor == MODERATOR and step.tool in ("announce", "send_message") and not step.args.get("channel_id"):
            raise _fail(step.describe(), "需要 channel_id。")
    return Situation(
        id=sid,
        scenario=scenario,
        steps=steps,
        decision=decision,
        title=str(body.get("title") or ""),
        tags=_strings(body.get("tags"), where, "tags"),
        seed=seed,
        deal=deal,
        clock=clock,
        player_count=count,
        names=names,
        roles=roles,
        brief=str(body.get("brief") or "").strip(),
        score=_parse_score(body.get("score"), labels, f"{where}: score"),
        judge=_parse_judge(body.get("judge"), f"{where}: judge"),
        meta=_mapping(body.get("meta") or {}, f"{where}: meta", "meta"),
    )


def load_situation(path: str | Path) -> Situation:
    """One situation file.  Raises :class:`SituationError` (a damaged file, a wrong shape)."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SituationError(f"{target.name}：读不了这个文件（{exc}）。") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SituationError(f"{target.name}：不是合法的 YAML（{str(exc).splitlines()[0] if str(exc) else exc}）。") from exc
    situation = parse_situation(data, source=target.name)
    return Situation(**{**situation.__dict__, "path": str(target)})


def situation_files(where: str | Path | Iterable[str | Path], *, include_examples: bool = False) -> list[Path]:
    """The files a ``--situations`` argument stands for: a file, a directory (``*.yaml`` / ``*.yml`` in it, ``_``-prefixed ones only with
    ``include_examples``) or a list of those."""
    items = [where] if isinstance(where, (str, Path)) else list(where)
    files: list[Path] = []
    for item in items:
        path = Path(item)
        if path.is_dir():
            found = sorted([*path.glob("*.yaml"), *path.glob("*.yml")])
            files.extend(p for p in found if include_examples or not p.name.startswith("_"))
        elif path.is_file():
            files.append(path)
        else:
            raise SituationError(f"{path}：不存在。")
    return files


def load_situations(
    where: str | Path | Iterable[str | Path],
    *,
    include_examples: bool = False,
    ids: Sequence[str] = (),
    tags: Sequence[str] = (),
) -> list[Situation]:
    """Every situation below ``where`` (see :func:`situation_files`), optionally only those with an id in ``ids`` and / or a tag in ``tags``.
    Two situations with the same id are an error."""
    found: list[Situation] = []
    seen: dict[str, str] = {}
    for path in situation_files(where, include_examples=include_examples):
        situation = load_situation(path)
        if situation.id in seen:
            raise SituationError(f"{path.name}：id {situation.id!r} 与 {seen[situation.id]} 重复。")
        seen[situation.id] = path.name
        found.append(situation)
    if ids:
        wanted = set(ids)
        missing = sorted(wanted - {s.id for s in found})
        if missing:
            raise SituationError(f"找不到这些题目：{'、'.join(missing)}。")
        found = [s for s in found if s.id in wanted]
    if tags:
        found = [s for s in found if set(tags) & set(s.tags)]
    return found


__all__ = [
    "DEALS",
    "DECISION_TOOLS",
    "DEFAULT_NAMES",
    "MODERATOR",
    "REF_RE",
    "SITUATION_FORMAT",
    "SITUATION_VERSION",
    "Decision",
    "Situation",
    "SituationError",
    "Step",
    "WakeSpec",
    "expand_steps",
    "load_situation",
    "load_situations",
    "parse_situation",
    "situation_files",
]
