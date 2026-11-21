"""Writing situations for ``werewolf_12p`` the way the moderator's manual plays the game (scenarios/werewolf_12p.md, "主持人手册").

The fixtures of the benchmark are *states of a real game*: every step below is a tool call the moderator (or a player) could make on the real engine, in the
order and with the arguments the manual prescribes - the same ``set_phase`` texts, the same templates (``death_notice``, ``vote_result`` ...), the same
requests (sealed votes with an ``abstain`` option, the hunter's, the sheriff's) - so what the bot under test finds on its board and in its history is what a
v0.14 game would have put there.  This module is the *author's* helper: it keeps the bookkeeping (who is alive, who is out, the sheriff, the potions, the
clock) that makes a long script consistent, and turns it into the plain dict of a situation file (``bench.situation`` format).  Nothing here runs a model;
the engine checks every step when the situation is built (``python -m bench situations --check``).

Conventions (so that a situation stays true when the seat under test takes the name of a bot): texts name players by **seat id** (``N5``), option labels
are ids; the engine's own renderings (``【出局公告】N9 Ivy 出局``) use the current names.  Texts are the author's own - no word of a real game.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

from bench.situation import DEFAULT_NAMES

SEATS = tuple(f"N{i}" for i in range(1, 13))
NAMES = DEFAULT_NAMES[:12]
SCENARIO = "werewolf_12p"
WOLF_ROLES = ("werewolf", "white_wolf")
GOD_ROLES = ("seer", "witch", "hunter", "guard")
#: The pool of the scenario: the 12 seats get a permutation of this list.
POOL = ("werewolf", "werewolf", "werewolf", "white_wolf", "villager", "villager", "villager", "villager", "seer", "witch", "hunter", "guard")
FACTION_NAME = {True: "狼人阵营", False: "好人阵营"}
GOOD = "好人阵营"
WOLF = "狼人阵营"


class AuthoringError(ValueError):
    """A script that the manual (or the table's own bookkeeping) does not allow."""


def seat_number(seat: str) -> int:
    return int(seat[1:])


def by_seat(seats: Sequence[str]) -> list[str]:
    return sorted(seats, key=seat_number)


def speaking_order(alive: Sequence[str], sheriff: str | None = None, direction: str = "警左") -> list[str]:
    """``evaluate(speech_order)`` of the manual: without a sheriff, ascending; with one, starting next to him (警左 = ascending from the next seat, 警右 =
    descending from the previous one) and ending with him."""
    seats = by_seat(list(alive))
    if not sheriff or sheriff not in seats:
        return seats
    i = seats.index(sheriff)
    if direction == "警左":
        return seats[i + 1:] + seats[:i] + [sheriff]
    return list(reversed(seats[:i])) + list(reversed(seats[i + 1:])) + [sheriff]


def vote_summary(ballots: Mapping[str, str], sheriff: str | None = None) -> str:
    """The text of the ``vote_result`` announcement: every ballot in seat order, then the tally (the sheriff's ballot counts 1.5, an abstention nothing)."""
    pattern, tally = [], {}
    for voter in by_seat(list(ballots)):
        target = ballots[voter]
        if target == "abstain":
            pattern.append(f"{voter} 弃票")
            continue
        pattern.append(f"{voter}→{target}")
        tally[target] = tally.get(target, 0.0) + (1.5 if voter == sheriff else 1.0)
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1], seat_number(kv[0])))
    counts = "，".join(f"{t} {v:g} 票" for t, v in ranked)
    return f"票型：{'，'.join(pattern)}；得票：{counts or '无有效票'}"


def leaders(ballots: Mapping[str, str], sheriff: str | None = None) -> list[str]:
    """The options with the most votes (abstentions are not votes), in seat order."""
    tally: dict[str, float] = {}
    for voter, target in ballots.items():
        if target != "abstain":
            tally[target] = tally.get(target, 0.0) + (1.5 if voter == sheriff else 1.0)
    if not tally:
        return []
    top = max(tally.values())
    return by_seat([t for t, v in tally.items() if v == top])


def check_claim(target: str, side: str) -> dict[str, Any]:
    """A turn at the floor spent on the ``check_report`` template (a *claim* in the hard-fact table)."""
    return {"template": "check_report", "params": {"target": target, "side": side}}


def vote_claim(target: str, reason: str = "") -> dict[str, Any]:
    """A turn at the floor spent on the ``vote_declare`` template (a *claim* in the hard-fact table)."""
    params: dict[str, Any] = {"target": target}
    if reason:
        params["reason"] = reason
    return {"template": "vote_declare", "params": params}


class Table:
    """A ``werewolf_12p`` game being written down, step by step (see the module docstring).

    ``roles``: the 12 roles in seat order (a permutation of :data:`POOL`) or ``{seat: role}``.  ``bot``: the seat under test.  The methods append the steps
    of the manual to :attr:`steps`; :meth:`situation` turns them into the dict of a situation file.
    """

    def __init__(self, roles: Sequence[str] | Mapping[str, str], *, bot: str, seed: int, start: float = 3.0) -> None:
        if isinstance(roles, Mapping):
            roles = [roles[s] for s in SEATS]
        if sorted(roles) != sorted(POOL):
            raise AuthoringError(f"身份要正好是这 12 张牌：{'、'.join(POOL)}。")
        self.roles: dict[str, str] = dict(zip(SEATS, roles))
        if bot not in self.roles:
            raise AuthoringError(f"被测座位 {bot} 不在 N1–N12 里。")
        self.bot = bot
        self.seed = seed
        self.rng = random.Random(seed)
        self.alive: list[str] = list(SEATS)
        self.out: list[str] = []
        self.pending: list[str] = []  # killed in the night, not announced yet (day 1: the election comes first)
        self.sheriff: str | None = None
        self.steps: list[dict[str, Any]] = []
        self.t = start
        self._at: float | None = None
        self._labels: set[str] = set()
        self.day = 0
        self.facts: list[str] = []  # what the hard-fact table will show (announcements), in order
        self.last_guard = ""
        self.antidote = 1
        self.poison = 1
        self.attacked: str | None = None  # the night's wolf target (None = no knife)
        self.last_night_dead: list[str] = []
        self.used_neutral: set[str] = set()
        self.long_lines = False  # the neutral lines of the table are the ~100-character FILLER (a long history, suite M2) instead of the short ones
        self.styled_seats: set[str] = set()  # the seats whose free speeches carry the style of an L1 / L2 question (marked ``meta: {styled: true}``)

    # ------------------------------------------------------------------------------------------------------ the cast
    def role_of(self, seat: str) -> str:
        return self.roles[seat]

    def seat_of(self, role: str) -> str:
        found = [s for s, r in self.roles.items() if r == role]
        if len(found) != 1:
            raise AuthoringError(f"身份 {role} 有 {len(found)} 个座位，不是唯一的。")
        return found[0]

    def seats_of(self, *roles: str) -> list[str]:
        return [s for s in SEATS if self.roles[s] in roles]

    @property
    def wolves(self) -> list[str]:
        return self.seats_of(*WOLF_ROLES)

    def alive_wolves(self) -> list[str]:
        return [s for s in self.wolves if s in self.alive]

    def is_alive(self, seat: str) -> bool:
        return seat in self.alive

    def side_of(self, seat: str) -> str:
        return FACTION_NAME[self.roles[seat] in WOLF_ROLES]

    # ------------------------------------------------------------------------------------------------------ the clock
    def _stamp(self, step: dict[str, Any]) -> dict[str, Any]:
        if self._at != round(self.t, 1):
            self._at = round(self.t, 1)
            step["at"] = self._at
        return step

    def _label(self, label: str | None) -> None:
        if label:
            if label in self._labels:
                raise AuthoringError(f"label {label!r} 用了两次。")
            self._labels.add(label)

    # ------------------------------------------------------------------------------------------------------ primitives
    def mod(self, tool: str, args: Mapping[str, Any] | None = None, *, label: str | None = None, dt: float = 0.0) -> None:
        """One call of the moderator."""
        self.t += dt
        self._label(label)
        step: dict[str, Any] = {"as": "moderator", "tool": tool, "args": dict(args or {})}
        if label:
            step["label"] = label
        self.steps.append(self._stamp(step))

    def player(self, seat: str, tool: str, args: Mapping[str, Any] | None = None, *, dt: float = 0.0) -> None:
        """One call of a player."""
        self.t += dt
        self.steps.append(self._stamp({"as": seat, "tool": tool, "args": dict(args or {})}))

    def announce(self, channel: str, template: str, params: Mapping[str, Any] | None = None, *, dt: float = 1.5, fact: str | None = None) -> None:
        """An announcement by template (what the hard-fact table records as a fact)."""
        self.mod("announce", {"channel_id": channel, "template_id": template, "params": dict(params or {})}, dt=dt)
        if fact and channel == "public":
            self.facts.append(fact)

    def turn(self, seat: str, what: str | Mapping[str, Any] | None, *, channel: str = "public", gap: tuple[float, float] = (7.0, 21.0), filler: bool = False) -> None:
        """One player's turn at the floor: a speech, a template (a ``check_report`` / ``vote_declare`` claim), or a pass (``None`` / empty)."""
        self.t += self.rng.uniform(*gap)
        if not what:
            self.steps.append(self._stamp({"as": seat, "tool": "pass", "args": {"channel_id": channel}}))
        elif isinstance(what, str):
            step: dict[str, Any] = {"say": {seat: what}, "channel": channel}
            if seat in self.styled_seats and not filler:  # (a neutral line is not in the style of anybody: it is the same in both halves of a pair)
                step["meta"] = {"styled": True}
            self.steps.append(self._stamp(step))
        else:
            self.steps.append(self._stamp({"as": seat, "tool": "speak_template", "args": {"channel_id": channel, "template_id": what["template"], "params": dict(what["params"])}}))

    def queue(
        self, order: Sequence[str], turns: Mapping[str, str | Mapping[str, Any] | None], *, channel: str = "public", fill: bool = True, dt: float = 0.3, until: str | None = None,
        limits: Mapping[str, Any] | None = None,
    ) -> None:
        """``set_floor_queue`` and the turns in that order.  A seat without an entry in ``turns`` says a neutral line (``fill``) or passes.  ``until``: the
        turns stop *before* this seat (the floor is then its - a speech to be written by the bot - or the queue is cut short by what happens next)."""
        self.mod("set_floor_queue", {"channel_id": channel, "order": list(order), "limits": {"max_messages": 1, **(limits or {})}}, dt=dt)
        for seat in order:
            if seat == until:
                break
            if seat in turns:
                self.turn(seat, turns[seat], channel=channel)
            elif fill and seat != self.bot:
                self.turn(seat, self.neutral(), channel=channel, filler=True)
            else:
                self.turn(seat, None, channel=channel)  # (the seat under test never "says" a line the author wrote: its own turns before the decision are passes)

    def neutral(self) -> str:
        """A speech with no information in it, not one that was used already."""
        from bench.cases.common import FILLER, NEUTRAL

        lines = FILLER if self.long_lines else NEUTRAL
        picked: list[str] = []
        for _ in range(3 if self.long_lines else 1):  # (a long table talks in three sentences: as long as a floor allows - the limit is 300 characters)
            pool = [line for line in lines if line not in self.used_neutral and line not in picked] or [line for line in lines if line not in picked]
            fits = [line for line in pool if len(" ".join([*picked, line])) <= 290]
            if not fits:
                break
            line = self.rng.choice(fits)
            self.used_neutral.add(line)
            picked.append(line)
        return " ".join(picked)

    # ------------------------------------------------------------------------------------------------------ manual: opening
    def open(self) -> None:
        """一、开局：阶段、公共频道、狼人频道、开局公告。"""
        self.mod("set_phase", {"text": "准备阶段 · 建立频道"})
        self.mod("create_channel", {"channel_id": "public", "name": "公共频道", "type": "public", "mode": "closed", "members": list(SEATS)})
        self.mod("create_channel", {"channel_id": "wolf-team", "name": "狼人阵营", "type": "group", "mode": "closed", "members": self.wolves}, dt=0.4)
        self.mod("send_message", {"channel_id": "wolf-team", "content": f"你们是狼人阵营的队友：{'、'.join(self.wolves)}。夜间会在这里商议，并用“刀口”请求拍刀；允许自刀或空刀。"}, dt=1.5)
        self.mod("send_message", {"channel_id": "public", "content": "对局开始。本局是 12 人暗牌局：出局不公布身份和死因。夜间行动由我私下询问，请留意你的待处理请求。"}, dt=1.0)

    # ------------------------------------------------------------------------------------------------------ manual: the night
    def _ask(self, seat: str, question: str, options: Sequence[tuple[str, str]], answer: str, *, label: str, default: str | None = None) -> None:
        """A private request to one player and his answer (the way the moderator asks a role to act at night)."""
        args: dict[str, Any] = {"to": [seat], "kind": "choice", "question": question, "options": [{"id": i, "label": t} for i, t in options]}
        if default is not None:
            args["default"] = default
        self.mod("open_request", args, label=label, dt=2.0)
        self.player(seat, "respond", {"request_id": f"@{label}", "value": answer}, dt=self.rng.uniform(3.0, 9.0))

    def _alive_options(self, *, none: tuple[str, str] | None = None, exclude: Sequence[str] = ()) -> list[tuple[str, str]]:
        options = [none] if none else []
        return options + [(s, s) for s in self.alive if s not in exclude]

    def night(
        self,
        n: int,
        *,
        guard: str | None = None,
        wolf: str | None = None,
        seer: str | None = None,
        witch: str = "pass",
    ) -> dict[str, Any]:
        """二、黑夜 N1–N5 in the manual's order: phase and nightfall, the guard, the wolves' knife, the seer, the witch, the settlement.

        ``guard``: the seat guarded (None = no guard);  ``wolf``: the knife's target (None = no knife);  ``seer``: the seat checked (None = no check);
        ``witch``: ``pass`` | ``save`` | ``poison:Nx``.  A role that is out is skipped (nothing is asked).  The night's dead are collected (:attr:`pending`) and
        NOT announced (day 1 has its election first).  Returns what the seer was told (``{"target", "side"}``) or ``{}``.
        """
        self.day = n
        self.mod("set_phase", {"text": f"第 {n} 夜"}, dt=4.0)
        self.mod("send_message", {"channel_id": "public", "content": f"第 {n} 夜，天黑请闭眼。"}, dt=0.1)
        told: dict[str, Any] = {}
        guard_seat = self.seat_of("guard") if self.seat_of("guard") in self.alive else None
        seer_seat = self.seat_of("seer") if self.seat_of("seer") in self.alive else None
        witch_seat = self.seat_of("witch") if self.seat_of("witch") in self.alive else None
        # N1 守卫
        if guard_seat:
            options = self._alive_options(none=("none", "空守"), exclude=[self.last_guard] if self.last_guard else [])
            if guard is not None and guard not in [o for o, _ in options]:
                raise AuthoringError(f"守卫不能守 {guard}（上一晚守的是 {self.last_guard or '空守'}，或已出局）。")
            self._ask(guard_seat, f"第 {n} 夜：请选择今晚守护的玩家（可以守自己，也可以空守；不能连续两晚守护同一人）。", options, guard or "none", label=f"guard{n}", default="none")
            self.mod("update_player", {"player_id": guard_seat, "set_attributes": {"last_guard": guard or ""}}, dt=1.0)
            self.last_guard = guard or ""
        # N2 狼人商议与拍刀
        self._wolves_act(n, wolf)
        # N3 预言家
        if seer_seat:
            options = self._alive_options(none=("skip", "不查验"))
            if seer is not None and seer not in [o for o, _ in options]:
                raise AuthoringError(f"预言家不能查验 {seer}。")
            self._ask(seer_seat, f"第 {n} 夜：请选择要查验的玩家。", options, seer or "skip", label=f"seer{n}", default="skip")
            if seer is not None:
                told = {"target": seer, "side": self.side_of(seer)}
                self.announce(f"dm-{seer_seat}", "seer_result", told, dt=2.0)
        # N4 女巫
        saved = False
        poisoned: str | None = None
        if witch_seat:
            save_ok = self.antidote == 1 and wolf is not None
            poison_ok = self.poison == 1
            if save_ok or poison_ok:
                if save_ok:
                    self.mod("send_message", {"channel_id": f"dm-{witch_seat}", "content": f"今晚被狼人袭击的是 {wolf}。"}, dt=2.0)
                options = [("pass", "本晚不用药")]
                if save_ok:
                    options.append(("save", "使用解药（救今晚被袭击的人）"))
                if poison_ok:
                    options.extend((f"poison:{s}", f"毒杀 {s}") for s in self.alive)
                if witch not in [o for o, _ in options]:
                    raise AuthoringError(f"女巫此刻不能选 {witch}。")
                self._ask(witch_seat, f"第 {n} 夜：请选择本晚唯一的用药动作（同一晚只能用一瓶药，也可以不用）。", options, witch, label=f"witch{n}", default="pass")
                if witch == "save":
                    self.antidote, saved = 0, True
                    self.mod("update_player", {"player_id": witch_seat, "set_attributes": {"antidote": 0}}, dt=1.0)
                elif witch.startswith("poison:"):
                    self.poison, poisoned = 0, witch.split(":", 1)[1]
                    self.mod("update_player", {"player_id": witch_seat, "set_attributes": {"poison": 0}}, dt=1.0)
            elif witch != "pass":
                raise AuthoringError("女巫这一夜没有可用的药。")
        # N5 结算（只在作者的账上算）
        dead: list[str] = []
        if wolf is not None and (guard == wolf) == saved:  # both or neither -> out (同守同救)
            dead.append(wolf)
        if poisoned and poisoned not in dead:
            dead.append(poisoned)
        for seat in dead:
            if seat not in self.alive:
                raise AuthoringError(f"{seat} 已经出局了。")
            self.alive.remove(seat)
            self.pending.append(seat)
        self.attacked = wolf
        self.last_night_dead = by_seat(dead)
        return told

    def _wolves_act(self, n: int, target: str | None) -> None:
        """N2: the wolves talk in their channel, set the knife with a shared request, and the moderator announces it in the wolf channel."""
        wolves = self.alive_wolves()
        if not wolves:
            return
        options = [{"id": "none", "label": "空刀"}] + [{"id": s, "label": s} for s in self.alive]
        self.mod("open_floor", {"channel_id": "wolf-team", "mode": "free", "holders": wolves,
                                "limits": {"duration_s": 100, "max_messages_per_member": 6, "max_chars_per_message": 300, "max_chars_total": 3000, "silence_s": 40}}, dt=2.0)
        self.mod("open_request", {"to": wolves, "kind": "choice", "question": f"第 {n} 夜：请和队友商量后选定今晚的刀口（可以自刀，也可以空刀）。你可以随时改选，队友的选择实时可见；大家选得一致，刀口立刻确定。",
                                  "options": options, "share_answers": True, "auto_close": "unanimous", "duration_s": 150}, label=f"wolf{n}", dt=1.0)
        self.mod("send_message", {"channel_id": "wolf-team", "content": f"第 {n} 夜：请商议今晚的刀口，并用你的“刀口”请求拍刀；想改就再选一次，队友的选择大家都看得到。大家选得一致，刀口立刻确定。"}, dt=0.5)
        self.t += self.rng.uniform(4.0, 9.0)
        self.steps.append(self._stamp({"answer": f"@wolf{n}", "values": {w: (target or "none") for w in wolves}}))
        self.mod("close_floor", {"channel_id": "wolf-team"}, dt=2.0)
        if target:
            self.announce("wolf-team", "wolf_target", {"target": target}, dt=1.5)
        else:
            self.announce("wolf-team", "wolf_empty", {}, dt=1.5)

    # ------------------------------------------------------------------------------------------------------ manual: the day
    def _out(self, seat: str) -> None:
        if seat in self.alive:
            self.alive.remove(seat)
        if seat in self.pending:
            self.pending.remove(seat)
        self.out.append(seat)
        self.mod("update_player", {"player_id": seat, "add_tags": ["出局"], "remove_tags": ["存活"]}, dt=0.3)

    def election(
        self,
        *,
        candidates: Sequence[str] = (),
        turns: Mapping[str, str | Mapping[str, Any] | None] | None = None,
        quit: Sequence[str] = (),
        ballots: Mapping[str, str] | None = None,
        until: str | None = None,
        limits: Mapping[str, Any] | None = None,
        decide: str | None = None,
        say_quit: bool = True,
    ) -> str | None:
        """D1 首日警长竞选（第 1 夜出局的人不参加）。``candidates`` 为空 = 无人上警；``quit``：退水的人；仍在竞选的多于一人时 ``ballots`` 是警下玩家的票。
        ``decide``：被测座位——它是警下的投票人，``ballots`` 里没有它的票，请求给它留着（决策点就是它的警长投票）。``say_quit=False``：不在公共频道多说一句“退水：…”
        （手册没有这一步；真实存档里也没有）。返回当选的人（没有则 None）。"""
        voters = list(self.alive)  # (the night's dead are not in ``alive`` any more)
        cands = by_seat(list(candidates))
        self.mod("set_phase", {"text": "第 1 天 · 警长竞选"}, dt=3.0)
        self.mod("open_request", {"to": voters, "kind": "choice", "question": "是否上警竞选警长？本选择保密。",
                                  "options": [{"id": "yes", "label": "上警"}, {"id": "no", "label": "不上警"}], "sealed": True, "default": "no"}, label="run", dt=2.0)
        self.t += self.rng.uniform(3.0, 8.0)
        self.steps.append(self._stamp({"answer": "@run", "values": {s: ("yes" if s in cands else "no") for s in voters}}))
        if not cands:
            self.announce("public", "no_sheriff", {}, dt=2.5, fact="【警长】本局不设警长（警徽作废）。")
            return None
        self.announce("public", "sheriff_candidates", {"players": cands}, dt=2.5, fact=f"【警长竞选】候选人：{'、'.join(cands)}。")
        self.mod("set_phase", {"text": "第 1 天 · 警长竞选演说"}, dt=1.5)
        self.queue(cands, dict(turns or {}), until=until, limits=limits)
        if until:
            return None  # (the decision is the turn of ``until`` in the campaign: nothing after it is written)
        self.mod("open_request", {"to": cands, "kind": "choice", "question": "是否退水？退水后你仍然没有本轮警长投票权。",
                                  "options": [{"id": "stay", "label": "不退水"}, {"id": "quit", "label": "退水"}], "sealed": True, "default": "stay"}, label="quit", dt=2.0)
        self.t += self.rng.uniform(3.0, 8.0)
        self.steps.append(self._stamp({"answer": "@quit", "values": {s: ("quit" if s in quit else "stay") for s in cands}}))
        remaining = [s for s in cands if s not in quit]
        if say_quit:
            self.mod("send_message", {"channel_id": "public", "content": f"退水：{'、'.join(by_seat(list(quit))) or '无'}；仍在竞选：{'、'.join(remaining) or '无'}。"}, dt=1.5)
        below = [s for s in voters if s not in cands]
        if not remaining or not below:
            self.announce("public", "no_sheriff", {}, dt=2.0, fact="【警长】本局不设警长（警徽作废）。")
            return None
        if len(remaining) == 1:
            winner = remaining[0]
        else:
            self.announce("public", "sheriff_candidates", {"players": remaining}, dt=2.0, fact=f"【警长竞选】候选人：{'、'.join(remaining)}。")
            self.mod("open_request", {"to": below, "kind": "choice", "question": "警长投票：请在候选人中选一名。", "options": [{"id": c, "label": c} for c in remaining], "sealed": True},
                     label="badge", dt=2.0)
            expected = [s for s in below if s != decide]
            if decide is not None and decide not in below:
                raise AuthoringError(f"被测座位 {decide} 不是警下的投票人（{below}）。")
            if not ballots or set(ballots) != set(expected) or not set(ballots.values()) <= set(remaining):
                raise AuthoringError(f"ballots 要是警下玩家 {expected} 每人一票，投给 {remaining} 之一。")
            self.t += self.rng.uniform(4.0, 10.0)
            self.steps.append(self._stamp({"answer": "@badge", "values": {s: ballots[s] for s in by_seat(list(ballots))}}))
            if decide is not None:
                return None  # (the decision: the seat under test has not voted yet)
            summary = vote_summary(ballots)
            self.announce("public", "vote_result", {"summary": summary}, dt=3.0, fact=f"【投票结果】{summary}")
            top = leaders(ballots)
            if len(top) != 1:
                raise AuthoringError("这个脚本只写一次投票就决出警长。")
            winner = top[0]
        self.mod("update_player", {"player_id": winner, "add_tags": ["警长"]}, dt=1.0)
        self.announce("public", "sheriff", {"player": winner}, dt=0.5, fact=f"【警长】{winner} 当选/继任警长，放逐投票中拥有 1.5 票，并决定发言方向、最后发言归票。")
        self.sheriff = winner
        return winner

    def dawn(self, n: int) -> list[str]:
        """D2 公布死讯：阶段、公告（死讯或平安夜）、出局标签。返回今天出局的人。"""
        self.day = n
        dead = by_seat(self.pending)
        self.mod("set_phase", {"text": f"第 {n} 天 · 公布死讯"}, dt=3.0)
        if dead:
            self.announce("public", "death_notice", {"players": dead}, dt=1.5, fact=f"【出局公告】{'、'.join(dead)} 出局。")
        else:
            self.announce("public", "peaceful_night", {}, dt=1.5, fact="【昨夜平安】昨夜无人出局。")
        for seat in dead:
            self._out(seat)
        return dead

    def last_words(self, n: int, speeches: Mapping[str, str | Mapping[str, Any]]) -> None:
        """E.c 遗言：阶段 + 发言队列 + 发言（只有第 1 夜被刀 / 被毒出局的人和被放逐的人有）。"""
        if not speeches:
            return
        self.mod("set_phase", {"text": f"第 {n} 天 · 遗言"}, dt=2.0)
        order = by_seat(list(speeches))
        self.queue(order, dict(speeches), fill=False)

    def hunter_shoots(self, hunter: str, target: str | None) -> None:
        """E.a 猎人：请求 + 作答 + 开枪公告 + 目标出局（目标没有遗言）。"""
        options = [("none", "不开枪")] + [(s, s) for s in self.alive]
        self.mod("open_request", {"to": [hunter], "kind": "choice", "question": "你出局了，可以亮出猎人身份开枪。请选择开枪目标，或选择不开枪。",
                                  "options": [{"id": i, "label": t} for i, t in options], "default": "none"}, label="shot", dt=2.0)
        self.player(hunter, "respond", {"request_id": "@shot", "value": target or "none"}, dt=self.rng.uniform(3.0, 8.0))
        if target:
            self.announce("public", "hunter_shot", {"hunter": hunter, "target": target}, dt=2.0, fact=f"【猎人开枪】{hunter} 亮明猎人身份并开枪，带走 {target}。")
            self.announce("public", "death_notice", {"players": [target]}, dt=1.0, fact=f"【出局公告】{target} 出局。")
            self._out(target)

    def badge(self, old: str, new: str | None) -> None:
        """E.b 警徽：出局的警长把警徽移交（``new``）或撕毁（None）。"""
        options = [("destroy", "撕毁警徽")] + [(s, s) for s in self.alive]
        self.mod("open_request", {"to": [old], "kind": "choice", "question": "你是警长且已出局：请选择把警徽移交给谁，或撕毁警徽。",
                                  "options": [{"id": i, "label": t} for i, t in options], "default": "destroy"}, label=f"badge_{old}", dt=2.0)
        self.player(old, "respond", {"request_id": f"@badge_{old}", "value": new or "destroy"}, dt=self.rng.uniform(3.0, 8.0))
        self.mod("update_player", {"player_id": old, "remove_tags": ["警长"]}, dt=1.0)
        if new:
            self.mod("update_player", {"player_id": new, "add_tags": ["警长"]}, dt=0.3)
            self.announce("public", "sheriff", {"player": new}, dt=0.5, fact=f"【警长】{new} 当选/继任警长，放逐投票中拥有 1.5 票，并决定发言方向、最后发言归票。")
            self.sheriff = new
        else:
            self.announce("public", "no_sheriff", {}, dt=0.5, fact="【警长】本局不设警长（警徽作废）。")
            self.sheriff = None

    def talk(
        self, n: int, turns: Mapping[str, str | Mapping[str, Any] | None], *, direction: str = "警左", fill: bool = True, until: str | None = None,
        limits: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """D3 + D4 白天发言：（有警长时）发言方向的请求与公告，阶段，发言队列，每人一次。没有写在 ``turns`` 里的座位说一句没有信息的话。返回发言顺序。
        ``until``：到这个座位之前为止（它的发言权就是决策点，或者接下来发生的事打断了队列：自爆）。"""
        if self.sheriff and self.sheriff in self.alive:
            self.mod("open_request", {"to": [self.sheriff], "kind": "choice", "question": "请决定今日发言方向：警左（从你的下一个座位号开始递增）还是警右（从你的上一个座位号开始递减）？你本人最后发言归票。",
                                      "options": [{"id": "left", "label": "警左"}, {"id": "right", "label": "警右"}], "default": "right"}, label=f"dir{n}", dt=2.0)
            self.player(self.sheriff, "respond", {"request_id": f"@dir{n}", "value": "left" if direction == "警左" else "right"}, dt=4.0)
            self.announce("public", "speech_direction", {"direction": direction}, dt=2.0, fact=f"【发言顺序】警长决定：{direction}发言，警长最后发言归票。")
        order = speaking_order(self.alive, self.sheriff, direction)
        unknown = set(turns) - set(order)
        if unknown:
            raise AuthoringError(f"不在发言名单里的座位：{sorted(unknown)}（存活的是 {by_seat(self.alive)}）。")
        self.mod("set_phase", {"text": f"第 {n} 天 · 白天发言"}, dt=2.0)
        self.queue(order, dict(turns), fill=fill, until=until, limits=limits)
        return order

    def vote_open(self, n: int, *, label: str) -> None:
        """D5 放逐投票：阶段 + 全体存活玩家的密封请求（可以弃票）。"""
        self.mod("set_phase", {"text": f"第 {n} 天 · 放逐投票"}, dt=3.0)
        options = [{"id": "abstain", "label": "弃票"}] + [{"id": s, "label": s} for s in self.alive]
        self.mod("open_request", {"to": list(self.alive), "kind": "choice", "question": f"第 {n} 天放逐投票：请选择要放逐的玩家，或弃票。", "options": options,
                                  "sealed": True, "default": "abstain"}, label=label, dt=2.0)

    def vote_cast(self, ballots: Mapping[str, str], *, label: str) -> None:
        """Scripted ballots."""
        self.t += self.rng.uniform(4.0, 12.0)
        self.steps.append(self._stamp({"answer": f"@{label}", "values": {s: ballots[s] for s in by_seat(list(ballots))}}))

    def vote_close(self, ballots: Mapping[str, str]) -> list[str]:
        """The moderator's side of a closed vote: the tally announcement.  Returns the leaders."""
        sheriff = self.sheriff if self.sheriff in ballots else None
        summary = vote_summary(ballots, sheriff)
        self.announce("public", "vote_result", {"summary": summary}, dt=3.0, fact=f"【投票结果】{summary}")
        return leaders(ballots, sheriff)

    def exile(self, n: int, who: str, *, last: str | Mapping[str, Any] | None = None) -> None:
        """D7 放逐之后：公告 + 出局 + 遗言（被放逐的人有遗言）。"""
        self.announce("public", "death_notice", {"players": [who]}, dt=1.5, fact=f"【出局公告】{who} 出局。")
        self._out(who)
        if last:
            self.last_words(n, {who: last})

    def no_exile(self) -> None:
        self.announce("public", "no_exile", {}, dt=1.5, fact="【放逐结果】本轮无人被放逐。")

    def day_vote(self, n: int, ballots: Mapping[str, str], *, exile: str | None = None, last: str | Mapping[str, Any] | None = None) -> None:
        """A whole day-vote that closes: open, every ballot, the tally, the exile (or none) - what the story needs before the decision."""
        label = f"vote{n}"
        self.vote_open(n, label=label)
        self.vote_cast(ballots, label=label)
        top = self.vote_close(ballots)
        if exile is None:
            self.no_exile()
        else:
            if top != [exile]:
                raise AuthoringError(f"票型 {dict(ballots)} 放逐的是 {top}，不是 {exile}。")
            self.exile(n, exile, last=last)

    def explode(self, wolf: str, target: str, declaration: str, *, during: str = "talk", cancel: Sequence[str] = ()) -> None:
        """五、白狼王自爆（在一轮发言的当中）：启用能力、宣言、关能力、关公共频道、取消未关闭的请求、出局公告（白狼王 + 目标 + 待公布死亡）、吞警徽。"""
        if self.roles[wolf] != "white_wolf" or wolf not in self.alive:
            raise AuthoringError(f"{wolf} 不是存活的白狼王。")
        if target == wolf or target not in self.alive:
            raise AuthoringError(f"自爆带走的 {target} 要是别的存活的人。")
        self.mod("enable_ability", {"ability_id": "white_wolf_explode"}, dt=1.0)
        self.player(wolf, "use_ability", {"ability_id": "white_wolf_explode", "params": {"target": target, "declaration": declaration}}, dt=self.rng.uniform(5.0, 15.0))
        self.facts.append(f"【自爆】{wolf} 亮明白狼王身份并自爆，带走 {target}。{declaration}")
        self.mod("disable_ability", {"ability_id": "white_wolf_explode"}, dt=1.5)
        self.mod("close_floor", {"channel_id": "public"}, dt=0.3)
        for request in cancel:
            self.mod("cancel_request", {"request_id": request}, dt=0.2)
        gone = by_seat([wolf, target, *self.pending])
        self.announce("public", "death_notice", {"players": gone}, dt=2.0, fact=f"【出局公告】{'、'.join(gone)} 出局。")
        for seat in gone:
            self._out(seat)
        if during == "election":
            self.announce("public", "no_sheriff", {}, dt=1.5, fact="【警长】本局不设警长（警徽作废）。")
            self.sheriff = None

    def tie_pk(self, n: int, tied: Sequence[str], turns: Mapping[str, str | Mapping[str, Any] | None], *, until: str | None = None) -> None:
        """D6 平票 PK：公告、阶段、并列者按座位顺序依次发言（``until``：到这个座位之前为止——轮到它就是决策点）。重投由 :meth:`vote_open` 等接着写。"""
        order = by_seat(list(tied))
        self.announce("public", "tie_pk", {"players": order}, dt=2.0, fact=f"【平票】{'、'.join(order)} 并列最高票，进入 PK 发言。")
        self.mod("set_phase", {"text": f"第 {n} 天 · 平票 PK"}, dt=1.5)
        self.queue(order, dict(turns), fill=False, until=until)

    # ------------------------------------------------------------------------------------------------------ the decision
    def decision_vote(self, n: int, ballots: Mapping[str, str], *, label: str = "vote") -> None:
        """The day-vote the bot is to cast: every other ballot is in, the bot's own request stays open."""
        self.vote_open(n, label=label)
        self.vote_cast({s: b for s, b in ballots.items() if s != self.bot}, label=label)

    # ------------------------------------------------------------------------------------------------------ the file
    def situation(
        self,
        *,
        sid: str,
        title: str,
        tags: Sequence[str],
        decision: Mapping[str, Any],
        truth: Mapping[str, Any] | None = None,
        brief: str = "",
        judge: Mapping[str, Any] | Sequence[str] | None = None,
        meta: Mapping[str, Any] | None = None,
        seed: int | None = None,
        names: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """The situation dict (``bench.situation`` format) of this table, ready for :func:`dump`."""
        data: dict[str, Any] = {
            "format": "arena-situation", "version": 1, "id": sid, "title": title, "tags": list(tags), "scenario": SCENARIO,
            "seed": seed if seed is not None else self.seed, "deal": "start", "players": {"count": 12, "names": list(names or NAMES)},
            "roles": [self.roles[s] for s in SEATS], "steps": self.steps, "decision": dict(decision),
        }
        if brief:
            data["brief"] = brief
        if truth:
            data["truth"] = dict(truth)
        if judge:
            data["judge"] = list(judge) if not isinstance(judge, Mapping) else dict(judge)
        if meta:
            data["meta"] = dict(meta)
        return data


# ---------------------------------------------------------------------------------------------------------------------- writing
class _Dumper(yaml.SafeDumper):
    """YAML without aliases (a situation file is read by people)."""

    def ignore_aliases(self, data: Any) -> bool:  # noqa: D401 - PyYAML hook
        return True


def _str_presenter(dumper: yaml.SafeDumper, value: str) -> Any:
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_Dumper.add_representer(str, _str_presenter)


def _flow(value: Any) -> str:
    """One value as a single-line flow YAML (``{as: moderator, tool: set_phase, args: {text: 第 1 夜}}``)."""
    return yaml.dump(value, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=100000, default_flow_style=True).strip().removesuffix("...").strip()


def dump(data: Mapping[str, Any], *, header: str = "") -> str:
    """The text of a situation file: the header as ``#`` lines, the top-level keys in the order given, the steps as one flow mapping per line, texts whole."""
    parts = ["".join(f"# {line}".rstrip() + "\n" for line in header.strip("\n").splitlines())]

    def block(key: str, value: Any, flow: bool | None) -> str:
        return yaml.dump({key: value}, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=100000, default_flow_style=flow)

    for key, value in data.items():
        if key == "steps":
            parts.append("\nsteps:\n" + "".join(f"  - {_flow(step)}\n" for step in value) + "\n")
        elif isinstance(value, (list, tuple)) and all(not isinstance(v, (list, tuple, Mapping)) for v in value):
            parts.append(f"{key}: {_flow(list(value))}\n")  # (tags, roles, expect: one line)
        elif isinstance(value, Mapping) and key in ("truth", "judge", "meta"):
            parts.append(block(key, value, False))
        elif isinstance(value, Mapping):
            parts.append(block(key, value, None))
        else:
            parts.append(block(key, value, False))
    return "".join(parts)


def texts_of(value: Any) -> list[str]:
    """Every string in a nested value (the speeches, announcements and private messages of a script)."""
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            out.extend(texts_of(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            out.extend(texts_of(item))
    return out


__all__ = [
    "AuthoringError", "GOOD", "NAMES", "POOL", "SCENARIO", "SEATS", "Table", "WOLF", "by_seat", "check_claim", "dump", "leaders", "seat_number", "speaking_order",
    "texts_of", "vote_claim", "vote_summary",
]
