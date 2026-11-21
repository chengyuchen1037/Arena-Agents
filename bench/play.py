"""Play a situation with the REAL engine: build the world, submit the steps as intents, stop at the decision point (H47).

Nothing here is a model: the moderator's operations and the scripted players' speeches / answers go through the commit gate
(``World.submit``) exactly as a tool call of an agent does, so the world is whatever the engine makes of them - floors, requests, the
state board, the facts, the visible history.  :class:`StepRunner` is the one place that applies steps (``bench.replay`` uses it to play
back the ledger of a saved game); :func:`build_scene` builds a whole situation and works out what wakes the seat under test.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import app.config as app_config
from app.agents.schemas import MODERATOR_TOOL_INTENTS, PLAYER_TOOL_INTENTS
from app.runtime.clock import ManualClock
from app.world.loader import load_scenario_v2
from app.world.spec import PoolItem, ScenarioSpec
from app.world.types import Envelope, Intent, LedgerEntry, Receipt
from app.world.world import World
from bench.situation import DEFAULT_NAMES, MODERATOR, REF_RE, Decision, Situation, SituationError, Step, WakeSpec

#: The envelope ``kind`` of an explicit wake (``decision.wake: [{event: ...}]``), by the event's prefix.
_KIND_BY_PREFIX = {"message": "message", "floor": "floor", "request": "request", "challenge": "challenge", "ability": "ability"}


class PlayError(RuntimeError):
    """A step the engine refused (the script is wrong, or the engine changed): says which step and what the commit gate answered."""

    def __init__(self, message: str, *, step: Step | None = None, receipt: Receipt | None = None) -> None:
        super().__init__(message)
        self.step = step
        self.receipt = receipt


@dataclass(frozen=True)
class StepRecord:
    """What one step did: the receipt of its intent (``None`` for a clock / gate step) and the ledger range it wrote."""

    step: Step
    receipt: Receipt | None
    seq_before: int
    seq_after: int

    @property
    def ok(self) -> bool:
        return self.receipt is None or self.receipt.ok


def scenario_path(scenario_id: str) -> Path:
    """``scenarios/<id>.md`` below the program directory (``app.config.BASE_DIR``: the repository, or the copy a test points it at)."""
    return Path(app_config.BASE_DIR) / "scenarios" / f"{scenario_id}.md"


def load_spec(scenario_id: str) -> ScenarioSpec:
    path = scenario_path(scenario_id)
    if not path.is_file():
        raise SituationError(f"找不到剧本 {scenario_id}（{path}）。")
    return load_scenario_v2(path)


def with_explicit_roles(spec: ScenarioSpec, roles: Sequence[str]) -> ScenarioSpec:
    """A copy of ``spec`` whose identity pool is exactly ``roles`` (in seat order) and is not shuffled: the engine's own dealing then gives
    seat *i* the role *i* (the rules, templates and abilities are untouched).  That is how a situation fixes who is who."""
    unknown = [role for role in dict.fromkeys(roles) if role not in spec.setup.roles]
    if unknown:
        raise SituationError(f"剧本 {spec.id} 里没有这些身份：{'、'.join(unknown)}。可用的：{'、'.join(spec.setup.roles)}。")
    setup = spec.setup.model_copy(update={"shuffle": False, "identity_pool": [PoolItem(role=role, count=1) for role in roles]})
    return spec.model_copy(update={"setup": setup})


# ---------------------------------------------------------------------------------------------------------------------- players
def seat_ids(count: int) -> list[str]:
    return [f"N{index}" for index in range(1, count + 1)]


def _player_count(situation: Situation, spec: ScenarioSpec) -> int:
    if situation.roles is not None:
        return len(situation.roles)
    if situation.player_count is not None:
        return situation.player_count
    return sum(item.count for item in spec.setup.identity_pool) or 4


def resolve_roles(situation: Situation, ids: Sequence[str]) -> list[str] | None:
    """The explicit roles in seat order (``None`` = dealt by the seed)."""
    roles = situation.roles
    if roles is None:
        return None
    if isinstance(roles, Mapping):
        missing = [seat for seat in ids if seat not in roles]
        extra = [seat for seat in roles if seat not in ids]
        if missing or extra:
            raise SituationError(
                f"{situation.id}：roles 必须正好覆盖每个座位（缺 {'、'.join(missing) or '无'}，多出 {'、'.join(extra) or '无'}）。"
            )
        return [roles[seat] for seat in ids]
    return list(roles)


def resolve_players(situation: Situation, spec: ScenarioSpec, *, seat: str | None = None, seat_name: str | None = None) -> list[dict[str, str]]:
    """``[{"id", "seat", "name"}]`` in seat order: the script's names first, then the default names that are still free; the seat under test
    gets ``seat_name`` (the bot that plays it), and whoever else had that name is renamed."""
    count = _player_count(situation, spec)
    ids = seat_ids(count)
    taken: set[str] = set()
    names: list[str] = []
    for name in situation.names:
        names.append(name)
        taken.add(name.casefold())
    spare = [n for n in DEFAULT_NAMES if n.casefold() not in taken]
    while len(names) < count:
        names.append(spare.pop(0) if spare else f"P{len(names) + 1}")
    names = names[:count]
    if seat is not None and seat not in ids:
        raise SituationError(f"{situation.id}：座位 {seat} 不在这局的 {count} 个座位里（{ids[0]}–{ids[-1]}）。")
    if seat is not None and seat_name:
        index = ids.index(seat)
        for other, name in enumerate(names):
            if other != index and name.casefold() == seat_name.casefold():
                used = {n.casefold() for n in names} | {seat_name.casefold()}
                names[other] = next((n for n in DEFAULT_NAMES if n.casefold() not in used), f"P{other + 1}")
        names[index] = seat_name
    return [{"id": pid, "seat": str(index), "name": name} for index, (pid, name) in enumerate(zip(ids, names), start=1)]


# ---------------------------------------------------------------------------------------------------------------------- steps
def resolve_refs(value: Any, labels: Mapping[str, Mapping[str, Any]], step: Step | None = None) -> Any:
    """``args`` with every ``"@label"`` (a whole string) replaced by the ``request_id`` of that step's receipt and ``"@label.key"`` by
    ``data[key]``; the rest is copied."""
    if isinstance(value, str):
        match = REF_RE.match(value)
        if not match:
            return value
        label, key = match.group(1), match.group(2) or "request_id"
        data = labels.get(label)
        if data is None or key not in data:
            known = "、".join(sorted(labels)) or "（还没有）"
            raise PlayError(f"{step.describe() if step else ''}：{value} 没有对应的值（已记下的 label：{known}；{label!r} 的回执里有 {sorted(data or {})}）。", step=step)
        return copy.deepcopy(data[key])
    if isinstance(value, Mapping):
        return {k: resolve_refs(v, labels, step) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [resolve_refs(v, labels, step) for v in value]
    return copy.deepcopy(value)


def intent_type(actor: str, tool: str) -> str:
    return (MODERATOR_TOOL_INTENTS if actor == MODERATOR else PLAYER_TOOL_INTENTS)[tool]


class StepRunner:
    """Applies :class:`~bench.situation.Step`s to a world, in order: moves the game clock (``at`` / ``advance``) and fires the timers that
    are due, submits intents with the actor's current view (``based_on_seq`` = the world's seq: scripted players never write stale drafts),
    remembers the receipts of labelled steps and every envelope the world queued (who was told what, by which step)."""

    def __init__(self, world: World, clock: ManualClock) -> None:
        self.world = world
        self.clock = clock
        self.labels: dict[str, dict[str, Any]] = {}
        self.label_step: dict[str, int] = {}
        self.records: list[StepRecord] = []
        #: ``(step index, target, envelope)`` of everything the world queued while steps were applied
        self.deliveries: list[tuple[int, str, Envelope]] = []

    def move_to(self, at: float, *, step: Step | None = None, strict: bool = True) -> None:
        """Set the game clock to ``at`` and fire the timers that are due (``strict``: going backwards is an error, else it is ignored)."""
        now = float(self.clock.now())
        if at < now - 1e-9:
            if strict:
                raise PlayError(f"{step.describe() if step else 'at'}：at={at:g} 早于现在的游戏时间 {now:g}（时间不能倒流）。", step=step)
            return
        self.clock.set(at)
        self.world.tick()
        self._collect(step.index if step is not None else -1)

    def _collect(self, index: int) -> None:
        for delivery in self.world.drain_outbox():
            self.deliveries.append((index, delivery.target, delivery.envelope))
        self.world.drain_events()  # (the spectators' events: nobody listens)

    def run(self, step: Step, *, strict: bool = True) -> StepRecord:
        world = self.world
        if step.at is not None:
            self.move_to(step.at, step=step, strict=strict)
        before = world.seq
        receipt: Receipt | None = None
        if step.kind == "advance":
            self.clock.advance(step.seconds)
            world.tick()
        elif step.kind == "tick":
            world.tick()
        elif step.kind == "running":
            world.set_running(step.running, step.reason or ("resumed" if step.running else "user"))
        elif step.kind == "untimed":
            world.set_untimed_holders(step.seats)
        elif step.kind == "tool":
            args = resolve_refs(dict(step.args), self.labels, step)
            receipt = world.submit(
                Intent(intent_id=f"bench-{step.index}", actor_id=step.actor, based_on_seq=world.seq, type=intent_type(step.actor, step.tool), payload=args)
            )
            if receipt.ok:
                if step.label:
                    self.labels[step.label] = {**receipt.data, "seq": receipt.seq}
                    self.label_step[step.label] = step.index
            elif strict:
                self._collect(step.index)
                raise PlayError(
                    f"{step.describe()} 被引擎拒绝：{receipt.code}：{receipt.message} "
                    f"（参数：{json.dumps(args, ensure_ascii=False)[:200]}）",
                    step=step,
                    receipt=receipt,
                )
        else:  # pragma: no cover - the parser only makes these four
            raise PlayError(f"{step.describe()}：不认识的步骤类型 {step.kind!r}。", step=step)
        self._collect(step.index)
        record = StepRecord(step, receipt, before, world.seq)
        self.records.append(record)
        return record

    def run_all(self, steps: Sequence[Step], *, strict: bool = True) -> list[StepRecord]:
        return [self.run(step, strict=strict) for step in steps]


# ---------------------------------------------------------------------------------------------------------------------- the scene
def ledger_fingerprint(ledger: Sequence[LedgerEntry]) -> str:
    """A hash of the whole ledger without the wall-clock stamps: two plays of one situation (one seed) have the same."""
    rows = [[e.seq, e.gt, e.type, e.actor_id, e.channel_id, list(e.visible_to_ids), e.data] for e in ledger]
    text = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Scene:
    """A situation played up to its decision point."""

    situation: Situation
    spec: ScenarioSpec
    world: World
    clock: ManualClock
    seat: str
    seat_name: str
    names: dict[str, str]
    wake: list[Envelope]
    runner: StepRunner = field(repr=False)
    #: A hash of the ledger at the decision point (before the bot did anything): two plays of one situation with one bot have the same.
    fingerprint: str = ""
    #: How many ledger entries the world had at the decision point: what the bot does is ``world.ledger[seq0:]``.
    seq0: int = 0

    @property
    def records(self) -> list[StepRecord]:
        return self.runner.records

    def after(self) -> list[dict[str, Any]]:
        """The ledger entries written since the decision point (as saved-game dicts): everything the bot's moves made the engine write."""
        return [entry.model_dump(mode="json") for entry in self.world.ledger[self.seq0 :]]


def envelope_of(raw: Mapping[str, Any], world: World, seat: str) -> Envelope:
    """An explicit wake entry (``{event, channel, sender, kind, priority, payload, seq}``) as the envelope the world would have queued."""
    event = str(raw["event"])
    kind = str(raw.get("kind") or _KIND_BY_PREFIX.get(event.split(".", 1)[0], "notice"))
    channel = raw.get("channel")
    payload = {"event": event, **({"channel_id": channel} if channel else {}), **dict(raw.get("payload") or {})}
    return Envelope(
        seq=int(raw.get("seq") or world.seq),
        kind=kind,  # type: ignore[arg-type]
        priority=str(raw.get("priority") or ("urgent" if kind in ("floor", "request") else "normal")),  # type: ignore[arg-type]
        channel_id=str(channel) if channel else None,
        sender=str(raw["sender"]) if raw.get("sender") else None,
        payload=payload,
    )


def compute_wake(world: World, runner: StepRunner, decision: Decision) -> list[Envelope]:
    """What wakes the seat of ``decision`` (see ``bench.situation``)."""
    wake: WakeSpec = decision.wake
    seat = decision.seat
    if wake.mode == "explicit":
        return [envelope_of(raw, world, seat) for raw in wake.envelopes]
    if wake.mode == "since":
        first = runner.label_step.get(wake.label)
        if first is None:
            raise PlayError(f"decision.wake.since：label {wake.label!r} 的那一步没有成功（或不存在）。")
        return [env for index, target, env in runner.deliveries if index >= first and target == seat]
    world.redeliver()  # what a resumed game tells every seat again: the floor it holds, the requests it has not answered
    queued = world.drain_outbox()
    world.drain_events()
    return [d.envelope for d in queued if d.target == seat]


def build_scene(situation: Situation, *, seat: str | None = None, seat_name: str | None = None, spec: ScenarioSpec | None = None) -> Scene:
    """Create the world of ``situation`` and play its steps.  ``seat`` overrides ``decision.seat`` (the same situation can be played for
    every good seat), ``seat_name`` is the name the seat gets (the bot that plays it).  Raises :class:`~bench.situation.SituationError` for
    a file the engine cannot use and :class:`PlayError` for a step it refuses or a decision point where the seat has nothing to do."""
    spec = spec or load_spec(situation.scenario)
    chosen_seat = seat or situation.decision.seat
    players = resolve_players(situation, spec, seat=chosen_seat, seat_name=seat_name)
    ids = [p["id"] for p in players]
    roles = resolve_roles(situation, ids)
    if roles is not None:
        spec = with_explicit_roles(spec, roles)
    for step in situation.steps:
        if step.kind == "tool" and step.actor != MODERATOR and step.actor not in ids:
            raise SituationError(f"{step.describe()}：座位 {step.actor} 不在这局里（{ids[0]}–{ids[-1]}）。")
    clock = ManualClock(situation.clock)
    try:
        world = World.new_game(spec, players, clock, seed=situation.seed, deal=situation.deal == "creation")
    except ValueError as exc:
        raise SituationError(f"{situation.id}：建不了这局（{exc}）。") from exc
    if situation.deal == "start":
        world.deal_identities()  # (what the v0.14 orchestrator does when the game starts)
    world.set_running(True, "started")
    runner = StepRunner(world, clock)
    runner.run_all(situation.steps)
    decision = Decision((chosen_seat,), situation.decision.wake, situation.decision.expect)
    wake = compute_wake(world, runner, decision)
    if not wake:
        raise PlayError(
            f"{situation.id}：座位 {chosen_seat} 在决策点没有任何待处理的事（没有发言权、没有未答的请求），没有东西能叫醒它。"
            "检查最后几步，或者用 decision.wake 指明（例如 {since: <label>} 或一个信封列表）。"
        )
    return Scene(
        situation=situation,
        spec=spec,
        world=world,
        clock=clock,
        seat=chosen_seat,
        seat_name=next(p["name"] for p in players if p["id"] == chosen_seat),
        names={p["id"]: p["name"] for p in players},
        wake=wake,
        runner=runner,
        fingerprint=ledger_fingerprint(world.ledger),
        seq0=len(world.ledger),
    )


__all__ = [
    "PlayError",
    "Scene",
    "StepRecord",
    "StepRunner",
    "build_scene",
    "compute_wake",
    "envelope_of",
    "intent_type",
    "ledger_fingerprint",
    "load_spec",
    "resolve_players",
    "resolve_refs",
    "resolve_roles",
    "scenario_path",
    "seat_ids",
    "with_explicit_roles",
]
