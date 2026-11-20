"""Saved game -> situation script: can a real game's decision point be rebuilt on the REAL engine? (H47: "M0 first checks whether it works")

A saved game (``game.json``) holds the world's ledger: every entry the engine committed.  The moderator's and the players' *actions* are in it
as the entries they caused - ``phase.changed``, ``channel.created``, ``floor.opened``, ``request.opened``, ``request.answered``, ``message`` ... -
and the engine's own reactions (``floor.granted``, ``floor.token_ended``, ``request.closed`` ...) are there too.  :func:`convert_ledger` turns the
*actions* back into the steps of a situation script (``bench.situation.Step``: the tool call and its arguments) and leaves the reactions out;
:func:`replay_game` plays those steps on a fresh world (the game's own seed, players, dealt identities and scenario) and **compares** what the
engine made of them with the saved ledger - entry by entry - and the final state.  If they agree, a decision point of that game (``upto_seq``) can be
rebuilt exactly, and a bot can be run on it; where they do not, the report names the gap.

What the converter does and cannot do (the findings are in bench/README.md):

* it needs the clock: every step carries the game time of the entry it comes from (``at``), and a ``tick`` is added at the time of every engine
  reaction, so deadlines (request timeouts, floor timeouts) fire where they did;
* a request's ``default`` answer is not in ``request.opened``; it is recovered from the ``defaulted`` answers of the matching ``request.closed``
  (when nobody was defaulted it never mattered);
* an agent's private notes and compactions are not in the ledger: the replayed seat starts the decision point with empty notes - as a benchmark
  bot does anyway;
* ``mod.random`` is replayed with the world's seed: the draws agree when the dealing used the same seed (``deal="seed"``), not after an explicit deal
  (a pool that is not shuffled does not use the generator, so every later draw differs);
* the unit of a replay is an action: "up to entry N" ends at the end of the action that wrote N (the entries the engine writes in the same breath
  after N are written too: ``overshoot`` in the report).  A decision point - a ``request.opened``, a ``floor.granted`` - is the end of its action.
"""

from __future__ import annotations

import copy
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from app.runtime.clock import ManualClock
from app.world.spec import ScenarioSpec
from app.world.world import World
from bench.loaders import load_game
from bench.play import PlayError, StepRunner, load_spec, with_explicit_roles
from bench.situation import MODERATOR, Decision, Situation, Step, WakeSpec, implied_score

ENGINE = "engine"
GT_TOLERANCE = 0.02  # seconds: the clock of a replay is set to the saved ``gt``, a timer fires at the same tick or one step later
#: Engine reactions that are driven by time or by a tick: a ``tick`` step at their game time lets the replay fire the same timers.
_TICK_AT = frozenset({"request.closed", "floor.token_ended", "floor.completed", "floor.granted", "floor.closed", "floor.frozen"})


class Unconvertible(Exception):
    """A ledger entry that is an action but cannot be turned back into a tool call (the report lists it as a gap)."""


@dataclass(frozen=True)
class Gap:
    seq: int
    type: str
    reason: str


@dataclass
class Conversion:
    """The steps of a (part of a) saved game, and what could not be converted."""

    steps: list[Step]
    gaps: list[Gap]
    ids: list[str]
    names: list[str]
    roles: list[str]  # seat order, from the ``deal`` entries
    seed: int
    scenario: str
    upto_seq: int
    step_seq: dict[int, int]  # step index -> ledger seq it came from
    entries: int  # entries in the converted range
    actions: int  # of which actions (converted into a tool call)
    started: bool = True  # the game had started by ``upto_seq`` (a game that was only set up has no ``started`` entry)


# ---------------------------------------------------------------------------------------------------------------------- the game
def game_ledger(game: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(e) for e in (game.get("world") or {}).get("ledger") or []]


def game_players(game: Mapping[str, Any]) -> list[dict[str, str]]:
    """``[{"id", "seat", "name", "persona"}]`` of the game (the persona is part of the world state of a v0.13 game; a bot ignores it)."""
    return [
        {"id": str(p["id"]), "seat": str(p.get("seat") or index), "name": str(p.get("name") or p["id"]), "persona": str(p.get("persona") or "")}
        for index, p in enumerate(game.get("participants") or [], start=1)
    ]


def deal_roles(ledger: Sequence[Mapping[str, Any]], ids: Sequence[str]) -> list[str]:
    """The role of every seat as the ``deal`` entries say (seat order).  Raises ``ValueError`` when a seat was not dealt."""
    by_seat = {
        str((e.get("data") or {}).get("player_id")): str((e.get("data") or {}).get("role"))
        for e in ledger
        if e.get("type") == "player.updated" and (e.get("data") or {}).get("reason") == "deal" and (e.get("data") or {}).get("role")
    }
    missing = [seat for seat in ids if seat not in by_seat]
    if missing:
        raise ValueError(f"账本里没有这些座位的发牌记录：{'、'.join(missing)}")
    return [by_seat[seat] for seat in ids]


def request_defaults(ledger: Sequence[Mapping[str, Any]], state: Mapping[str, Any] | None = None) -> dict[str, str]:
    """``{request id: default answer}``.  ``request.opened`` does not say what a request defaults to; the saved world *state* does (every request
    stays in ``state.requests`` with its definition) - a ledger alone gives only the defaults that fired (the ``defaulted`` answers of
    ``request.closed``)."""
    found: dict[str, str] = {}
    for entry in ledger:
        if entry.get("type") != "request.closed":
            continue
        data = entry.get("data") or {}
        for answer in data.get("answers") or []:
            if isinstance(answer, Mapping) and answer.get("defaulted"):
                found.setdefault(str(data.get("request_id")), str(answer.get("value")))
    for request_id, request in ((state or {}).get("requests") or {}).items():
        if isinstance(request, Mapping) and request.get("default") is not None:
            found[str(request_id)] = str(request["default"])
    return found


def _drop_empty(args: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in args.items() if v not in (None, "", [], {})}


def _members(data: Mapping[str, Any]) -> list[str]:
    return [str(m) for m in data.get("members") or [] if m != MODERATOR]


def convert_entry(entry: Mapping[str, Any], defaults: Mapping[str, str]) -> tuple[str, dict[str, Any]] | None:
    """One ledger entry -> what it was caused by: ``("tool", {actor, tool, args})``, ``("running", {running, reason})`` - or ``None`` for an
    entry that is a reaction of the engine (or of an action already converted).  Raises :class:`Unconvertible`."""
    kind, actor, data = str(entry.get("type")), str(entry.get("actor_id") or ""), dict(entry.get("data") or {})
    channel = entry.get("channel_id")

    def tool(who: str, name: str, args: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        return "tool", {"actor": who, "tool": name, "args": dict(args)}

    if kind == "message":
        message_kind = str(data.get("kind") or "speech")
        if actor == MODERATOR:
            if message_kind == "speech":
                return tool(MODERATOR, "send_message", {"channel_id": channel, "content": data.get("content")})
            if message_kind == "announce":
                return tool(MODERATOR, "announce", {"channel_id": channel, "template_id": data.get("template_id"), "params": data.get("params") or {}})
            if message_kind == "reply":
                return tool(MODERATOR, "resolve_challenge", {"ticket_id": data.get("ticket_id"), "verdict": data.get("verdict"), "reply": data.get("content")})
            raise Unconvertible(f"主持人的 {message_kind} 消息")
        if actor == ENGINE:
            return None
        if message_kind == "speech":
            return tool(actor, "speak", {"channel_id": channel, "content": data.get("content")})
        if message_kind == "template":
            return tool(actor, "speak_template", {"channel_id": channel, "template_id": data.get("template_id"), "params": data.get("params") or {}})
        if message_kind == "challenge":
            return tool(actor, "challenge_moderator", _drop_empty({"content": data.get("content"), "ref_seq": data.get("ref_seq")}))
        if message_kind == "ability":
            return None  # the reveal of an ability: ``ability.used`` is the action
        raise Unconvertible(f"玩家的 {message_kind} 消息")
    if kind == "channel.created":
        if actor != MODERATOR:
            return None  # the system DMs of the engine
        return tool(MODERATOR, "create_channel", {
            "channel_id": data.get("id"), "name": data.get("name"), "members": _members(data), "type": data.get("type") or "group",
            "mode": data.get("mode") or "closed",
        })
    if kind == "channel.updated":
        if actor != MODERATOR:
            return None
        changes = data.get("changes") or {}
        args: dict[str, Any] = {"channel_id": channel}
        if "mode" in changes:
            args["mode"] = (changes["mode"] or {}).get("to")
        if "name" in changes:
            args["name"] = changes["name"]
        if "added" in changes:
            args["add_members"] = list(changes["added"])
        if "removed" in changes:
            args["remove_members"] = list(changes["removed"])
        if "readonly" in changes:
            args["readonly_members"] = list(changes["readonly"])
        if "limits" in changes:
            args["limits"] = dict(changes["limits"])
        if len(args) == 1:
            raise Unconvertible("channel.updated 里只有 unfrozen 等无法还原成参数的变化")
        return tool(MODERATOR, "update_channel", args)
    if kind == "floor.opened":
        if actor != MODERATOR:
            return None
        mode = data.get("mode")
        if mode == "queue":
            return tool(MODERATOR, "set_floor_queue", {"channel_id": channel, "order": list(data.get("order") or []), "limits": data.get("limits") or {}})
        if mode == "turn":
            return tool(MODERATOR, "open_floor", {"channel_id": channel, "mode": "turn", "holders": list(data.get("holders") or []), "limits": data.get("limits") or {}})
        if mode == "free":
            return tool(MODERATOR, "open_floor", _drop_empty({"channel_id": channel, "mode": "free", "holders": list(data.get("holders") or []), "limits": data.get("limits") or {}}))
        raise Unconvertible(f"floor.opened 的 mode {mode!r}")
    if kind in ("floor.paused", "floor.resumed", "floor.closed"):
        if actor != MODERATOR:
            return None
        return tool(MODERATOR, {"floor.paused": "pause_queue", "floor.resumed": "resume_queue", "floor.closed": "close_floor"}[kind], {"channel_id": channel})
    if kind == "request.opened":
        if actor != MODERATOR:
            return None
        if data.get("reopens"):
            return tool(MODERATOR, "reopen_request", _drop_empty({"request_id": data["reopens"], "to": list(data.get("to") or []), "duration_s": data.get("duration_s")}))
        args = {
            "to": list(data.get("to") or []), "kind": data.get("kind") or "choice", "question": data.get("question"),
            "options": [{"id": o.get("id"), "label": o.get("label") or ""} for o in data.get("options") or []],
            "duration_s": data.get("duration_s"), "sealed": bool(data.get("sealed")), "share_answers": bool(data.get("share_answers")),
            "auto_close": data.get("auto_close") or "all_answered", "default": defaults.get(str(data.get("request_id"))),
        }
        if not args["options"]:
            args.pop("options")
        return tool(MODERATOR, "open_request", {k: v for k, v in args.items() if v is not None})
    if kind == "request.answered":
        return tool(actor, "respond", _drop_empty({"request_id": data.get("request_id"), "value": data.get("value"), "reason": data.get("reason")}))
    if kind == "request.cancelled":
        return tool(MODERATOR, "cancel_request", {"request_id": data.get("request_id")}) if actor == MODERATOR else None
    if kind in ("ability.enabled", "ability.disabled"):
        if actor != MODERATOR:
            return None
        return tool(MODERATOR, "enable_ability" if kind == "ability.enabled" else "disable_ability", _drop_empty({"ability_id": data.get("ability_id"), "holders": data.get("holders")}))
    if kind == "ability.used":
        return tool(actor, "use_ability", {"ability_id": data.get("ability_id"), "params": data.get("params") or {}})
    if kind == "player.updated":
        if actor != MODERATOR:
            return None  # the deal
        attributes = dict(data.get("set_attributes") or {})
        for key in ("role", "faction"):
            if data.get(key):
                attributes[key] = data[key]
        return tool(MODERATOR, "update_player", _drop_empty({
            "player_id": data.get("player_id"), "add_tags": list(data.get("add_tags") or []), "remove_tags": list(data.get("remove_tags") or []),
            "set_attributes": attributes,
        }))
    if kind == "phase.changed":
        return tool(MODERATOR, "set_phase", {"text": data.get("text")})
    if kind == "dashboard.updated":
        return tool(MODERATOR, "update_dashboard", {"factions": data.get("factions") or [], "tag_stats": data.get("tag_stats") or []})
    if kind == "random.drawn":
        return tool(MODERATOR, "random", {"kind": data.get("kind"), **(data.get("args") or {})})
    if kind == "game.status":
        status, reason = data.get("status"), str(data.get("reason") or "")
        if actor == MODERATOR and status == "finished":
            return tool(MODERATOR, "end_game", _drop_empty({"winner": data.get("winner"), "summary": data.get("summary")}))
        if status == "running" and reason == "started":
            return None  # the start of the game: the replay does it itself
        if status == "paused" and reason.startswith("hold"):
            return tool(MODERATOR, "hold_game", {"reason": reason.partition(":")[2]})
        if status in ("running", "paused"):
            return "running", {"running": status == "running", "reason": reason}
        return None
    if kind in ("floor.granted", "floor.token_ended", "floor.completed", "floor.frozen", "request.closed", "player.public_changed"):
        return None
    raise Unconvertible(f"不认识的账本条目类型 {kind}")


def convert_ledger(game: Mapping[str, Any], *, upto_seq: int | None = None) -> Conversion:
    """The steps of ``game`` up to the entry ``upto_seq`` (inclusive; default: the whole ledger)."""
    ledger = game_ledger(game)
    last = ledger[-1]["seq"] if ledger else 0
    upto = last if upto_seq is None else min(int(upto_seq), last)
    players = game_players(game)
    ids = [p["id"] for p in players]
    roles = deal_roles(ledger, ids)
    defaults = request_defaults(ledger, (game.get("world") or {}).get("state"))
    spans = [(str(s.get("seat")), int(s.get("from_seq") or 0), None if s.get("to_seq") is None else int(s["to_seq"])) for s in game.get("human_spans") or []]
    uses_untimed = any((e.get("data") or {}).get("untimed") for e in ledger if e.get("type") == "floor.granted")
    steps: list[Step] = []
    gaps: list[Gap] = []
    step_seq: dict[int, int] = {}
    actions = 0
    untimed: set[str] = set()
    started = any(
        e.get("type") == "game.status" and (e.get("data") or {}).get("reason") == "started" and int(e["seq"]) <= upto for e in ledger
    )

    def add(seq: int, **fields: Any) -> None:
        step_seq[len(steps)] = seq
        steps.append(Step(index=len(steps), source=f"seq {seq}", **fields))

    for entry in ledger:
        seq = int(entry["seq"])
        if seq > upto:
            break
        at = float(entry.get("gt") or 0.0)
        if uses_untimed:
            now_untimed = {seat for seat, start, end in spans if start < seq and (end is None or seq <= end)}
            if now_untimed != untimed:
                untimed = now_untimed
                add(seq, kind="untimed", at=at, seats=tuple(sorted(untimed)))
        try:
            converted = convert_entry(entry, defaults)
        except Unconvertible as exc:
            gaps.append(Gap(seq, str(entry.get("type")), str(exc)))
            continue
        if converted is None:
            if str(entry.get("actor_id")) == ENGINE and entry.get("type") in _TICK_AT:
                add(seq, kind="tick", at=at)
            continue
        actions += 1
        what, body = converted
        if what == "running":
            add(seq, kind="running", at=at, running=body["running"], reason=body["reason"])
            continue
        human = any(seat == body["actor"] and start < seq and (end is None or seq <= end) for seat, start, end in spans)
        add(seq, kind="tool", at=at, actor=body["actor"], tool=body["tool"], args=body["args"], meta={"seq": seq, **({"human": True} if human else {})})
    return Conversion(
        steps=steps, gaps=gaps, ids=ids, names=[p["name"] for p in players], roles=roles,
        seed=int((game.get("world") or {}).get("state", {}).get("rng", {}).get("seed") or 1),
        scenario=str(game.get("scenario_name") or ""), upto_seq=upto, step_seq=step_seq, entries=min(upto, last), actions=actions, started=started,
    )


# ---------------------------------------------------------------------------------------------------------------------- comparing
def _close(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(float(a), float(b), rel_tol=0, abs_tol=GT_TOLERANCE)
        except (TypeError, ValueError):
            return False
    return a == b


def diff_values(a: Any, b: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """The differences between two JSON values as ``(path, a, b)`` (floats within ``GT_TOLERANCE`` agree)."""
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        out: list[tuple[str, Any, Any]] = []
        for key in sorted(set(a) | set(b), key=str):
            if key not in a:
                out.append((f"{path}.{key}" if path else str(key), "（没有）", b[key]))
            elif key not in b:
                out.append((f"{path}.{key}" if path else str(key), a[key], "（没有）"))
            else:
                out.extend(diff_values(a[key], b[key], f"{path}.{key}" if path else str(key)))
        return out
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return [(f"{path}（长度）", len(a), len(b))]
        out = []
        for index, (x, y) in enumerate(zip(a, b)):
            out.extend(diff_values(x, y, f"{path}[{index}]"))
        return out
    return [] if _close(a, b) else [(path, a, b)]


def normalize_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """What is compared of an entry: everything but the wall-clock stamp (and ``gt``, compared by tolerance); the keys an older engine did not
    write (a speech's ``truncated`` - v0.14 never cuts one - and the live ``floor`` view inside a channel entry) are left out."""
    data = copy.deepcopy(dict(entry.get("data") or {}))
    data.pop("truncated", None)
    if entry.get("type") in ("channel.created", "channel.updated"):
        data.pop("floor", None)
    return {
        "type": entry.get("type"), "actor_id": entry.get("actor_id"), "channel_id": entry.get("channel_id"),
        "visible_to_ids": list(entry.get("visible_to_ids") or []), "data": data,
    }


@dataclass
class Mismatch:
    seq: int
    type: str
    differences: list[tuple[str, Any, Any]]

    def describe(self) -> str:
        shown = "；".join(f"{path}: {str(a)[:60]!r} ≠ {str(b)[:60]!r}" for path, a, b in self.differences[:3])
        return f"#{self.seq} {self.type}：{shown}"


#: ``chars_used`` of a floor entry may be this much smaller in a replay of a v0.13 game (see :func:`is_truncation_artifact`).
ARTIFACT_CHARS = 8


def is_truncation_artifact(entry: Mapping[str, Any], diffs: Sequence[tuple[str, Any, Any]], cut_before: bool) -> bool:
    """A difference that only comes from how v0.13 cut a long speech (and that v0.14 never does): the engine of v0.13 truncated to the limit
    *after* trimming, so the saved text can end with the whitespace of the cut; a replay submits it and the commit gate trims it again - the entry
    is a few characters shorter, and so are the ``chars_used`` of the floor entries that count it."""
    kind = entry.get("type")
    if kind == "message":
        data = entry.get("data") or {}
        return bool(data.get("truncated")) and all(
            path == "data.content" and isinstance(a, str) and isinstance(b, str) and a.strip() == b.strip() for path, a, b in diffs
        )
    if kind in ("floor.token_ended", "floor.completed") and cut_before:
        return all(
            path.endswith("chars_used") and isinstance(a, (int, float)) and isinstance(b, (int, float)) and 0 < a - b <= ARTIFACT_CHARS for path, a, b in diffs
        )
    return False


def compare_ledgers(saved: Sequence[Mapping[str, Any]], replayed: Sequence[Mapping[str, Any]], upto: int) -> dict[str, Any]:
    """Entry by entry (same position): how many are identical (by ``normalize_entry``; ``gt`` within ``GT_TOLERANCE``), the first that is not, and
    the mismatches by type.  A replay that wrote more or fewer entries is reported by the counts.  Differences that are only the trace of a v0.13
    truncation (:func:`is_truncation_artifact`) are counted as ``artifacts``, not as mismatches.

    The unit of a replay is an *action* (a tool call): the engine writes everything that follows from it in the same breath, so a replay that is to stop
    "at entry ``upto``" ends at the end of the action that wrote it.  When that action's consequences run past ``upto`` the replay has a few entries more
    (``overshoot``); they are compared with the saved entries at the same positions."""
    reach = min(len(saved), max(upto, len(replayed)))
    expected = [e for e in saved if int(e["seq"]) <= reach]
    mismatches: list[Mismatch] = []
    artifacts: list[Mismatch] = []
    gt_drift = 0
    matched = 0
    cut_before = False
    for index, entry in enumerate(expected):
        if index >= len(replayed):
            break
        other = replayed[index]
        left, right = normalize_entry(entry), normalize_entry(other)
        diffs = diff_values(left, right)
        if not _close(float(entry.get("gt") or 0.0), float(other.get("gt") or 0.0)):
            gt_drift += 1
        if not diffs:
            matched += 1
        elif is_truncation_artifact(entry, diffs, cut_before):
            artifacts.append(Mismatch(int(entry["seq"]), str(entry.get("type")), diffs))
            cut_before = cut_before or entry.get("type") == "message"
        else:
            mismatches.append(Mismatch(int(entry["seq"]), str(entry.get("type")), diffs))
    return {
        "saved": len(expected), "replayed": len(replayed), "compared": min(len(expected), len(replayed)), "matched": matched,
        "overshoot": max(0, len(replayed) - upto) if len(replayed) <= len(saved) else 0,
        "artifacts": len(artifacts), "identical": not mismatches and len(expected) == len(replayed), "gt_drift": gt_drift,
        "first_mismatch": mismatches[0].describe() if mismatches else (
            None if len(expected) == len(replayed) else f"条数不同：存档 {len(expected)} 条，重放 {len(replayed)} 条"
        ),
        "mismatch_by_type": dict(Counter(m.type for m in mismatches)),
        "mismatches": mismatches, "artifact_entries": artifacts,
    }


#: The parts of the world state a decision point depends on (the agents' private notes and the rng state are not part of the comparison).
_STATE_KEYS = ("players", "channels", "requests", "tickets", "abilities", "dashboard", "phase", "counters", "status", "winner", "summary")


def compare_state(saved_state: Mapping[str, Any], world: World) -> dict[str, Any]:
    """The world state of the replay against the saved one (``_STATE_KEYS``): ``equal`` and the first differences."""
    mine = world.state.model_dump(mode="json")
    diffs: list[tuple[str, Any, Any]] = []
    for key in _STATE_KEYS:
        diffs.extend(diff_values(saved_state.get(key), mine.get(key), key))
    return {"equal": not diffs, "differences": [f"{path}: {str(a)[:50]!r} ≠ {str(b)[:50]!r}" for path, a, b in diffs[:12]], "count": len(diffs)}


# ---------------------------------------------------------------------------------------------------------------------- the replay
@dataclass
class ReplayResult:
    conversion: Conversion
    world: World
    ledger_report: dict[str, Any]
    state_report: dict[str, Any] | None
    refused: list[str]  # steps the engine refused (the game's own action was accepted in the original)
    deal: str  # seed | explicit
    spec_source: str

    @property
    def exact(self) -> bool:
        return bool(self.ledger_report["identical"]) and not self.refused and (self.state_report is None or self.state_report["equal"])


def replay_game(game: Mapping[str, Any], *, upto_seq: int | None = None, spec_source: str = "saved", deal: str = "seed") -> ReplayResult:
    """Rebuild ``game`` up to ``upto_seq`` on a fresh world and compare.  ``spec_source``: ``saved`` (the scenario the game was played with, as
    saved) or ``current`` (``scenarios/<id>.md`` as it is now).  ``deal``: ``seed`` (deal with the game's seed - the rng stays in step - and fall
    back on the recorded roles when that deals differently) or ``explicit``."""
    conversion = convert_ledger(game, upto_seq=upto_seq)
    if spec_source == "saved" and game.get("scenario_spec"):
        spec = ScenarioSpec.model_validate(game["scenario_spec"])
    else:
        spec = load_spec(conversion.scenario or str(game.get("scenario_name")))
    players = game_players(game)
    used_deal = deal
    world: World | None = None
    clock = ManualClock(0.0)
    if deal == "seed":
        world = World.new_game(spec, players, clock, seed=conversion.seed, deal=False)
        world.deal_identities()
        dealt = [p.role for p in world.state.players]
        if dealt != conversion.roles:
            used_deal, world = "explicit", None  # the engine's shuffle differs from the game's: use the recorded roles
    if world is None:
        clock = ManualClock(0.0)
        world = World.new_game(with_explicit_roles(spec, conversion.roles), players, clock, seed=conversion.seed, deal=False)
        world.deal_identities()
    if conversion.started:
        world.set_running(True, "started")
    runner = StepRunner(world, clock)
    refused: list[str] = []
    for step in conversion.steps:
        try:
            record = runner.run(step, strict=False)
        except PlayError as exc:  # (a clock that goes backwards is ignored; anything else is a refusal)
            refused.append(str(exc))
            continue
        if not record.ok and record.receipt is not None:
            refused.append(f"seq {conversion.step_seq.get(step.index)}（{step.actor} 的 {step.tool}）：{record.receipt.code}：{record.receipt.message}")
    saved_ledger = game_ledger(game)
    ledger_report = compare_ledgers(saved_ledger, [e.model_dump(mode="json") for e in world.ledger], conversion.upto_seq)
    complete = conversion.upto_seq >= (saved_ledger[-1]["seq"] if saved_ledger else 0)
    state_report = compare_state((game.get("world") or {}).get("state") or {}, world) if complete else None
    return ReplayResult(conversion, world, ledger_report, state_report, refused, used_deal, spec_source)


def decision_points(game: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Where a seat is asked to act: every ``request.opened`` (to whom) and every ``floor.granted`` (to whom), with the phase at that time -
    the places a situation can stop."""
    phase = ""
    points: list[dict[str, Any]] = []
    for entry in game_ledger(game):
        data = entry.get("data") or {}
        kind = entry.get("type")
        if kind == "phase.changed":
            phase = str(data.get("text") or "")
        elif kind == "request.opened":
            points.append({"seq": entry["seq"], "gt": entry.get("gt"), "phase": phase, "kind": "request", "id": data.get("request_id"), "to": list(data.get("to") or []),
                           "question": str(data.get("question") or "")[:60]})
        elif kind == "floor.granted":
            points.append({"seq": entry["seq"], "gt": entry.get("gt"), "phase": phase, "kind": "floor", "channel": entry.get("channel_id"), "to": [data.get("holder")]})
    return points


# ---------------------------------------------------------------------------------------------------------------------- as a situation
def situation_from_game(
    game: Mapping[str, Any], *, upto_seq: int, seat: str, situation_id: str, title: str = "", tags: Sequence[str] = ("L4",),
    expect: Sequence[str] = ("speak", "speak_template", "pass", "respond", "use_ability"),
) -> Situation:
    """A situation script that rebuilds ``game`` up to the entry ``upto_seq`` and asks ``seat`` for its decision there.  The identities are the
    game's (explicit roles), the seed and the names are the game's.  The steps keep where they came from (``meta.seq``) and whether a person
    played the seat (``meta.human``): look at them before a fixture is committed - a saved game is the user's own data."""
    conversion = convert_ledger(game, upto_seq=upto_seq)
    if seat not in conversion.ids:
        raise ValueError(f"座位 {seat} 不在这局里（{'、'.join(conversion.ids)}）。")
    return Situation(
        id=situation_id, scenario=conversion.scenario, steps=tuple(conversion.steps),
        decision=Decision((seat,), WakeSpec(), tuple(expect)), title=title or f"{game.get('session_id')} 的第 {upto_seq} 条记录处，{seat} 的决定",
        tags=tuple(tags), seed=conversion.seed, deal="start", clock=0.0, player_count=len(conversion.ids), names=tuple(conversion.names),
        roles=dict(zip(conversion.ids, conversion.roles)), meta={"source": "saved game", "game": str(game.get("session_id") or ""), "upto_seq": upto_seq},
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def situation_to_data(situation: Situation) -> dict[str, Any]:
    """The YAML-ready dict of a situation (what :func:`bench.situation.parse_situation` reads back)."""
    steps: list[dict[str, Any]] = []
    for step in situation.steps:
        item: dict[str, Any]
        if step.kind == "tool":
            item = {"as": step.actor, "tool": step.tool, "args": _plain(step.args)}
        elif step.kind == "advance":
            item = {"advance": step.seconds}
        elif step.kind == "tick":
            item = {"tick": True}
        elif step.kind == "untimed":
            item = {"untimed": list(step.seats)}
        else:
            item = {"running": step.running, **({"reason": step.reason} if step.reason else {})}
        if step.at is not None:
            item["at"] = round(step.at, 3)
        if step.label:
            item["label"] = step.label
        if step.meta:
            item["meta"] = _plain(step.meta)
        steps.append(item)
    data: dict[str, Any] = {
        "format": "arena-situation", "version": 1, "id": situation.id, "title": situation.title, "tags": list(situation.tags),
        "scenario": situation.scenario, "seed": situation.seed, "deal": situation.deal, "clock": situation.clock,
        "players": {"count": situation.player_count, "names": list(situation.names)},
    }
    if situation.roles is not None:
        data["roles"] = _plain(situation.roles) if isinstance(situation.roles, Mapping) else list(situation.roles)
    data["steps"] = steps
    wake = situation.decision.wake
    data["decision"] = {
        "seats" if len(situation.decision.seats) > 1 else "seat": list(situation.decision.seats) if len(situation.decision.seats) > 1 else situation.decision.seat,
        "wake": "redeliver" if wake.mode == "redeliver" else ({"since": wake.label} if wake.mode == "since" else [dict(e) for e in wake.envelopes]),
        "expect": list(situation.decision.expect),
    }
    if situation.bots:
        data["bots"] = {seat: (definition["spec"] if "spec" in definition else _plain(definition)) for seat, definition in situation.bots.items()}
    if situation.brief:
        data["brief"] = situation.brief
    if situation.truth:
        data["truth"] = _plain(situation.truth)
    implied = [rule] if (rule := implied_score(situation.truth)) else []
    if situation.score and [dict(rule) for rule in situation.score] != implied:
        data["score"] = _plain(list(situation.score))  # (a score that the truth already implies is not written twice)
    if situation.judge:
        data["judge"] = _plain(situation.judge)
    if situation.meta:
        data["meta"] = _plain(situation.meta)
    return data


def situation_to_yaml(situation: Situation, *, header: str = "") -> str:
    """The situation as the text of a YAML file (``header``: comment lines to put in front)."""
    text = yaml.safe_dump(situation_to_data(situation), allow_unicode=True, sort_keys=False, width=140, default_flow_style=None)
    comments = "".join(f"# {line}\n" for line in header.splitlines())
    return comments + text


def report_text(result: ReplayResult, *, game_id: str = "") -> str:
    """The feasibility report of a replay, in Chinese, for the terminal."""
    conv, led = result.conversion, result.ledger_report
    lines = [
        f"重放 {game_id or '存档'}：账本 {led['saved']} 条（到 #{conv.upto_seq}），转成 {len(conv.steps)} 步（动作 {conv.actions} 个），"
        f"发牌方式 {result.deal}，剧本 {result.spec_source}。",
        f"  逐条比对：重放写出 {led['replayed']} 条，逐字一致 {led['matched']}/{led['compared']}"
        + (f"，另有 {led['artifacts']} 条只差 v0.13 截断留下的尾部空白（引擎现在会先去掉首尾空白，见模块说明）" if led["artifacts"] else "")
        + ("，其余全部一致。" if led["identical"] else f"，第一处不同 → {led['first_mismatch']}"),
    ]
    if led["overshoot"]:
        lines.append(f"  决策点（#{conv.upto_seq}）之后，同一个动作的后续记录又写了 {led['overshoot']} 条（引擎在一个动作里一口气写完），也一并比对了。")
    if led["gt_drift"]:
        lines.append(f"  游戏时钟与存档相差超过 {GT_TOLERANCE:g} 秒的条目：{led['gt_drift']} 条。")
    if led["mismatch_by_type"]:
        lines.append("  不同的条目按类型：" + "，".join(f"{k} {v}" for k, v in sorted(led["mismatch_by_type"].items())))
    if result.state_report is not None:
        state = result.state_report
        lines.append("  终局世界状态（玩家/频道/请求/能力/阶段/计数）：" + ("完全一致。" if state["equal"] else f"{state['count']} 处不同，如 {state['differences'][:2]}"))
    if result.refused:
        lines.append(f"  引擎拒绝了 {len(result.refused)} 步（原局里这些动作是成功的）：{result.refused[:3]}")
    if conv.gaps:
        lines.append(f"  转不回工具调用的条目 {len(conv.gaps)} 个：" + "；".join(f"#{g.seq} {g.type}（{g.reason}）" for g in conv.gaps[:5]))
    lines.append("  结论：" + ("可行——这一局到该条记录为止可以在真实引擎上逐条重放。" if result.exact else "有缺口，见上。"))
    return "\n".join(lines)


def load_and_replay(path: str, **options: Any) -> ReplayResult:
    return replay_game(load_game(path), **options)


__all__ = [
    "Conversion",
    "Gap",
    "Mismatch",
    "ReplayResult",
    "Unconvertible",
    "compare_ledgers",
    "compare_state",
    "convert_entry",
    "convert_ledger",
    "deal_roles",
    "decision_points",
    "diff_values",
    "game_ledger",
    "game_players",
    "load_and_replay",
    "normalize_entry",
    "replay_game",
    "report_text",
    "request_defaults",
    "situation_from_game",
    "situation_to_data",
    "situation_to_yaml",
]
