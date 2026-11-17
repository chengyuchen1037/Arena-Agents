"""Demo mode: scripted agents that play the bundled 12-player scenario without any language model.

``ARENA_DEMO=1`` installs :class:`DemoLlm` as the orchestrator's LLM port.  It is a stand-in for the
model: it reads the *world* (like an omniscient bot) and answers with real tool calls, so the whole
stack — commit gate, floors, requests, templates, abilities, challenges, runtime, brain, API, UI —
runs for real, just without a network and without cost.

The moderator bot follows the scenario's moderator manual in a simplified form (guard, wolves, seer,
witch, dawn, speeches, sealed vote, hunter, white-wolf self-destruct, win check).  It is *not*
engine code: it knows the scenario's channel / template / ability ids.  Game state lives in the
bot, so a demo game cannot be resumed after a process restart (the bot then holds the game).
"""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from collections.abc import Callable, Iterator
from typing import Any

from app.llm.types import LLMResponse, ToolCallData
from app.world.types import MODERATOR_ID

Call = tuple[str, dict[str, Any]]
Step = tuple[str, Any]

OUTCOME_PENDING = "未分胜负"


def do(*calls: Call) -> Step:
    return ("do", [c for c in calls if c is not None])


def until(predicate: Callable[[Any], bool]) -> Step:
    return ("until", predicate)


# ------------------------------------------------------------------------------- world helpers


def _alive(w: Any) -> list[str]:
    return [p.id for p in w.state.players if not w.is_out(p)]


def _role_ids(w: Any, role: str, *, alive_only: bool = True) -> list[str]:
    return [p.id for p in w.state.players if p.role == role and (not alive_only or not w.is_out(p))]


def _faction_ids(w: Any, faction: str, *, alive_only: bool = True) -> list[str]:
    return [p.id for p in w.state.players if p.faction == faction and (not alive_only or not w.is_out(p))]


def _entries(w: Any, type_: str, after: int, channel: str | None = None) -> list[Any]:
    return [
        e
        for e in w.entries_visible(MODERATOR_ID, after_seq=after)
        if e.type == type_ and (channel is None or e.channel_id == channel)
    ]


def _closed(w: Any, after: int, tag: str) -> Any | None:
    """The ``request.closed`` entry of the request whose question carries ``tag`` (opened after ``after``)."""
    ids = {
        e.data.get("request_id")
        for e in _entries(w, "request.opened", after)
        if tag in str(e.data.get("question", ""))
    }
    for entry in _entries(w, "request.closed", after):
        if entry.data.get("request_id") in ids:
            return entry
    return None


def _answers(entry: Any) -> dict[str, str]:
    return {a["player_id"]: a["value"] for a in (entry.data.get("answers") or [])}


def _dashboard(w: Any) -> Call:
    return ("update_dashboard", w.to_plain(w.eval_expr(w.spec.checks["counts"])))


def _elapsed(w: Any, since: float, seconds: float) -> bool:
    """Game-clock time check (a free discussion's "time is up" notice is an envelope, not a ledger entry)."""
    return w.clock.now() - since >= seconds


def _outcome(w: Any) -> str:
    return str(w.eval_expr(w.spec.checks["outcome"]))


# ---------------------------------------------------------------------------- the moderator bot


def moderator_script(w: Any, rng: random.Random) -> Iterator[Step]:
    players = [p.id for p in w.state.players]
    wolves = _faction_ids(w, "wolves", alive_only=False)
    state = {"last_guard": None, "antidote": True, "explode_day": 2}

    def deaths(victims: list[str], *, shot_allowed: bool, label: str) -> Iterator[Step]:
        """Mark players out, handle a hunter's shot, mute the dead, announce and refresh the board."""
        queue = [v for v in dict.fromkeys(victims) if v in _alive(w)]
        if not queue:
            return
        yield do(*[("update_player", {"player_id": v, "add_tags": ["出局"], "remove_tags": ["存活"]}) for v in queue])
        announced = list(queue)
        for v in list(queue):
            if w.player(v).role == "hunter" and shot_allowed:
                mark = w.seq
                options = [{"id": "none", "label": "不开枪"}] + [{"id": i, "label": i} for i in _alive(w)]
                yield do(("open_request", {"to": [v], "kind": "choice", "question": "【猎人】你可以开枪带走一名存活玩家（或选择不开枪）。",
                                           "options": options, "duration_s": 25, "default": "none"}))
                yield until(lambda world, m=mark: _closed(world, m, "【猎人】") is not None)
                target = _answers(_closed(w, mark, "【猎人】")).get(v, "none")
                if target in _alive(w):
                    yield do(("announce", {"channel_id": "public", "template_id": "hunter_shot", "params": {"hunter": v, "target": target}}),
                             ("update_player", {"player_id": target, "add_tags": ["出局"], "remove_tags": ["存活"]}))
                    announced.append(target)
        out = [p.id for p in w.state.players if w.is_out(p)]
        yield do(
            ("announce", {"channel_id": "public", "template_id": "death_notice", "params": {"players": announced}}),
            ("update_channel", {"channel_id": "public", "readonly_members": out}),
            ("update_channel", {"channel_id": "wolf-team", "readonly_members": [i for i in out if i in wolves]}),
            _dashboard(w),
        )

    def finish() -> Iterator[Step]:
        outcome = _outcome(w)
        summary = "好人阵营找出了所有狼人。" if outcome == "好人阵营" else "狼人阵营完成屠边。"
        yield do(("announce", {"channel_id": "public", "template_id": "game_over", "params": {"winner": outcome, "summary": summary}}),
                 ("end_game", {"winner": outcome, "summary": summary}))
        yield until(lambda world: False)

    # ---- setup -------------------------------------------------------------------------------
    yield do(
        ("create_channel", {"channel_id": "public", "name": "公共频道", "members": players, "type": "public"}),
        ("create_channel", {"channel_id": "wolf-team", "name": "狼人频道", "members": wolves, "type": "group"}),
        ("set_phase", {"text": "准备阶段 · 建台"}),
        _dashboard(w),
        ("send_message", {"channel_id": "wolf-team", "content": f"你们是狼人阵营，队友：{'、'.join(wolves)}。夜间在这里商议刀口。"}),
        ("send_message", {"channel_id": "public", "content": "对局开始。本局为暗牌局，出局公告不公布身份与死因。"}),
        ("evaluate", {"check_id": "candidates"}),
    )

    day = 0
    while True:
        day += 1
        # ---- night ---------------------------------------------------------------------------
        yield do(("set_phase", {"text": f"第 {day} 天 · 黑夜"}))
        guard_target = None
        guards = _role_ids(w, "guard")
        if guards:
            mark = w.seq
            options = [{"id": "none", "label": "空守"}] + [{"id": i, "label": i} for i in _alive(w) if i != state["last_guard"]]
            yield do(("open_request", {"to": guards, "kind": "choice", "question": f"【守卫】第 {day} 夜请选择守护对象。",
                                       "options": options, "duration_s": 25, "default": "none"}))
            yield until(lambda world, m=mark: _closed(world, m, "【守卫】") is not None)
            pick = _answers(_closed(w, mark, "【守卫】")).get(guards[0], "none")
            guard_target = pick if pick != "none" else None
            state["last_guard"] = guard_target

        mark = w.seq
        t0 = w.clock.now()
        alive_wolves = _faction_ids(w, "wolves")
        yield do(
            ("open_floor", {"channel_id": "wolf-team", "mode": "free", "holders": [],
                            "free_limits": {"duration_s": 6, "max_messages_per_member": 1, "max_chars_total": 600}}),
            ("send_message", {"channel_id": "wolf-team", "content": "狼人们请商议今晚的刀口，每人一句。"}),
        )
        yield until(lambda world, m=mark, t=t0: _elapsed(world, t, 6.0) or
                    len({e.actor_id for e in _entries(world, "message", m, "wolf-team") if e.actor_id in alive_wolves}) >= len(alive_wolves))
        yield do(("close_floor", {"channel_id": "wolf-team"}))

        mark = w.seq
        killer = alive_wolves[0]
        options = [{"id": "none", "label": "空刀"}] + [{"id": i, "label": i} for i in _alive(w)]
        yield do(("open_request", {"to": [killer], "kind": "choice", "question": f"【狼人刀口】第 {day} 夜请代表狼队提交刀口。",
                                   "options": options, "duration_s": 25, "default": "none"}))
        yield until(lambda world, m=mark: _closed(world, m, "【狼人刀口】") is not None)
        kill = _answers(_closed(w, mark, "【狼人刀口】")).get(killer, "none")

        seers = _role_ids(w, "seer")
        if seers:
            mark = w.seq
            yield do(("open_request", {"to": seers, "kind": "choice", "question": f"【预言家】第 {day} 夜请选择查验对象。",
                                       "options": [{"id": i, "label": i} for i in _alive(w) if i != seers[0]], "duration_s": 25,
                                       "default": [i for i in _alive(w) if i != seers[0]][0]}))
            yield until(lambda world, m=mark: _closed(world, m, "【预言家】") is not None)
            checked = _answers(_closed(w, mark, "【预言家】")).get(seers[0])
            if checked and w.player(checked):
                side = "狼人阵营" if w.player(checked).faction == "wolves" else "好人阵营"
                yield do(("announce", {"channel_id": f"dm-{seers[0]}", "template_id": "seer_result", "params": {"target": checked, "side": side}}))

        saved = False
        witches = _role_ids(w, "witch")
        if witches and state["antidote"] and kill != "none":
            mark = w.seq
            yield do(
                ("send_message", {"channel_id": f"dm-{witches[0]}", "content": f"今晚被狼人袭击的是 {kill}。你可以用解药救他。"}),
                ("open_request", {"to": witches, "kind": "choice", "question": f"【女巫】第 {day} 夜是否使用解药？",
                                  "options": [{"id": "save", "label": "使用解药"}, {"id": "pass", "label": "不用药"}],
                                  "duration_s": 25, "default": "pass"}),
            )
            yield until(lambda world, m=mark: _closed(world, m, "【女巫】") is not None)
            if _answers(_closed(w, mark, "【女巫】")).get(witches[0]) == "save":
                saved = True
                state["antidote"] = False
                yield do(("update_player", {"player_id": witches[0], "set_attributes": {"antidote": 0}}))

        # ---- dawn ----------------------------------------------------------------------------
        victims: list[str] = []
        if kill != "none":
            guarded = kill == guard_target
            if guarded == saved:  # neither, or both ("同守同救"): the victim dies
                victims.append(kill)
        yield do(("set_phase", {"text": f"第 {day} 天 · 天亮"}))
        if victims:
            yield from deaths(victims, shot_allowed=True, label="night")
        else:
            yield do(("announce", {"channel_id": "public", "template_id": "peaceful_night", "params": {}}), _dashboard(w))
        if _outcome(w) != OUTCOME_PENDING:
            yield from finish()
            return

        # ---- day: speeches ---------------------------------------------------------------------
        order = w.to_plain(w.eval_expr(w.spec.checks["speech_order"], {"start": "", "direction": "left"}))
        enable = day == state["explode_day"] and bool(_role_ids(w, "white_wolf"))
        mark = w.seq
        yield do(
            ("set_phase", {"text": f"第 {day} 天 · 白天 · 轮流发言"}),
            ("enable_ability", {"ability_id": "white_wolf_explode"}) if enable else None,
            ("evaluate", {"check_id": "speech_order", "args": {"start": "", "direction": "left"}}),
            ("set_floor_queue", {"channel_id": "public", "order": order, "limits": {"max_messages": 1, "duration_s": 30}}),
        )
        yield until(lambda world, m=mark: bool(_entries(world, "floor.completed", m, "public") or _entries(world, "ability.used", m)))

        exploded = _entries(w, "ability.used", mark)
        if exploded:
            who = exploded[-1].data["actor_id"]
            target = (exploded[-1].data.get("params") or {}).get("target")
            yield do(("disable_ability", {"ability_id": "white_wolf_explode"}), ("close_floor", {"channel_id": "public"}),
                     ("send_message", {"channel_id": "public", "content": "白狼王自爆，白天结束，直接进入黑夜。"}))
            yield from deaths([who, target], shot_allowed=False, label="explode")
            if _outcome(w) != OUTCOME_PENDING:
                yield from finish()
                return
            continue
        if enable:
            yield do(("disable_ability", {"ability_id": "white_wolf_explode"}))

        # ---- day: free talk, then the sealed exile vote ------------------------------------------
        mark = w.seq
        t0 = w.clock.now()
        yield do(
            ("set_phase", {"text": f"第 {day} 天 · 白天 · 自由讨论"}),
            ("open_floor", {"channel_id": "public", "mode": "free", "holders": [],
                            "free_limits": {"duration_s": 7, "max_messages_per_member": 1, "max_chars_total": 1500}}),
            ("send_message", {"channel_id": "public", "content": "正式发言结束，进入简短的自由讨论。"}),
        )
        yield until(lambda world, m=mark, t=t0: _elapsed(world, t, 7.0)
                    or len({e.actor_id for e in _entries(world, "message", m, "public") if e.actor_id in _alive(world)}) >= len(_alive(world)))
        yield do(("send_message", {"channel_id": "public", "content": "讨论到此为止，请大家投票。"}),
                 ("close_floor", {"channel_id": "public"}))

        mark = w.seq
        voters = _alive(w)
        options = [{"id": i, "label": i} for i in voters] + [{"id": "abstain", "label": "弃票"}]
        yield do(("set_phase", {"text": f"第 {day} 天 · 白天 · 放逐投票"}),
                 ("open_request", {"to": voters, "kind": "choice", "question": "【放逐投票】请选择要放逐的玩家（可弃票）。",
                                   "options": options, "sealed": True, "duration_s": 30, "default": "abstain"}))
        yield until(lambda world, m=mark: _closed(world, m, "【放逐投票】") is not None)
        closed = _closed(w, mark, "【放逐投票】")
        answers = _answers(closed)
        tally = {k: v for k, v in (closed.data.get("tally") or {}).items() if k != "abstain"}
        top = max(tally.values(), default=0)
        leaders = [k for k, v in tally.items() if v == top] if top else []
        summary = "；".join(f"{p}→{answers.get(p, '弃票')}" for p in voters)
        yield do(("announce", {"channel_id": "public", "template_id": "vote_result", "params": {"summary": f"{summary}。"[:400]}}))
        if len(leaders) == 1:
            yield from deaths([leaders[0]], shot_allowed=True, label="exile")
        else:
            yield do(("announce", {"channel_id": "public", "template_id": "no_exile", "params": {}}), _dashboard(w))
        if _outcome(w) != OUTCOME_PENDING:
            yield from finish()
            return


class ScriptedModerator:
    """Runs :func:`moderator_script` as an LLM policy; open challenge tickets are always handled first."""

    def __init__(self, world: Any, rng: random.Random) -> None:
        self.world = world
        self.script = moderator_script(world, rng)
        self.pending: Step | None = None
        self.resolved: set[str] = set()
        # A script that starts on a game whose set-up already happened (restored from a save) cannot continue.
        self.fresh = not any(e.type == "channel.created" and e.channel_id == "public" for e in world.entries_visible(MODERATOR_ID))

    def __call__(self, world: Any, me: str) -> list[Call]:
        if not self.fresh:
            return [("hold_game", {"reason": "演示局由脚本机器人驱动，不支持从存档继续；请新建对局。"})]
        for ticket in world.board(me).get("tickets") or []:
            if ticket.get("status") == "open" and ticket["id"] not in self.resolved:
                self.resolved.add(ticket["id"])
                return [("resolve_challenge", {"ticket_id": ticket["id"], "verdict": "rejected",
                                               "reply": "已复核本轮的流程与统计，未发现错误，维持原判。感谢提醒。"})]
        while True:
            if self.pending is None:
                try:
                    self.pending = next(self.script)
                except StopIteration:
                    return []
            kind, arg = self.pending
            if kind == "do":
                self.pending = None
                return arg
            if arg(world):
                self.pending = None
                continue
            return []


# -------------------------------------------------------------------------------- player bots

_LINES = [
    "我先听听大家的发言，目前信息还比较少。",
    "我的立场很简单：看票型和发言的一致性。",
    "{other} 刚才的发言有点含糊，我会重点关注他。",
    "我觉得今天不要轻易弃票，{other} 值得聊聊。",
    "身份我暂时不报，先看看场上的反应。",
    "{other} 的逻辑说得通，但我还想再看一轮。",
]


def _pick(rng: random.Random, options: list[str], default: str | None = None) -> str:
    return rng.choice(options) if options else (default or "")


class ScriptedPlayer:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.challenged = False

    def __call__(self, w: Any, me: str) -> list[Call]:
        board = w.board(me)
        mine = w.player(me)
        wolves = _faction_ids(w, "wolves")
        for request in board.get("requests") or []:
            if request.get("answered"):
                continue
            ids = [o["id"] for o in request.get("options") or []]
            text = str(request.get("question", ""))
            others = [i for i in ids if i not in (me, "none", "abstain", "save", "pass")]
            if "【放逐投票】" in text:
                pool = [i for i in others if i not in wolves] if mine.faction == "wolves" else others
                if mine.faction != "wolves" and self.rng.random() < 0.55:
                    pool = [i for i in others if i in wolves] or others
                value = _pick(self.rng, pool, "abstain") if self.rng.random() > 0.08 else "abstain"
            elif "【狼人刀口】" in text:
                value = _pick(self.rng, [i for i in others if i not in wolves], "none")
            elif "【女巫】" in text:
                value = "save" if self.rng.random() < 0.6 else "pass"
            elif "【猎人】" in text:
                value = _pick(self.rng, [i for i in others if i not in _role_ids(w, "hunter")], "none")
            else:  # guard / seer: anyone but myself
                value = _pick(self.rng, others or ids, ids[0] if ids else "none")
            return [("respond", {"request_id": request["id"], "value": value, "reason": "凭感觉" if "投票" in text else ""})]
        for ability in board.get("abilities") or []:
            if ability.get("enabled") and (ability.get("uses_left") or 0) > 0 and self.rng.random() < 0.7:
                targets = [i for i in _alive(w) if i != me]
                return [("use_ability", {"ability_id": ability["id"], "params": {"target": _pick(self.rng, targets),
                                                                               "declaration": "我是白狼王，今天不陪你们玩了！"}})]
        writable = [c for c in board.get("channels") or [] if c.get("can_write") and not c.get("system")]
        if writable:
            others = [i for i in _alive(w) if i != me]
            if not self.challenged and "第 2 天" in str(board.get("phase", "")) and mine.faction != "wolves" and self.rng.random() < 0.5:
                self.challenged = True
                return [("challenge_moderator", {"content": "我认为昨晚的结算流程可能有疏漏，请主持人复核一下。"})]
            line = self.rng.choice(_LINES).format(other=_pick(self.rng, others, "某位玩家"))
            if writable[0]["id"] == "wolf-team":
                line = f"今晚刀 {_pick(self.rng, [i for i in others if i not in wolves], '空刀')} 吧，大家同意吗？"
            return [("speak", {"channel_id": writable[0]["id"], "content": line})]
        return []


# ------------------------------------------------------------------------------------- the LLM port


class DemoLlm:
    """:class:`app.runtime.ports.LlmPort` backed by scripted policies (no network, no cost)."""

    def __init__(self, world: Any, *, seed: int | None = None, delay: float = 1.0) -> None:
        self.world = world
        self.rng = random.Random(seed)
        self.delay = delay
        moderator = ScriptedModerator(world, random.Random(self.rng.random()))
        self.policies: dict[str, Callable[[Any, str], list[Call]]] = {MODERATOR_ID: moderator}
        for player in world.state.players:
            self.policies[player.id] = ScriptedPlayer(random.Random(self.rng.random()))
        self.calls = 0

    async def complete(
        self, agent_id: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, *, max_tokens: int | None = None
    ) -> LLMResponse:
        self.calls += 1
        if self.delay:  # make the game watchable: "thinking" takes a moment
            top = 1.1 if agent_id == MODERATOR_ID else 0.8
            await asyncio.sleep(self.rng.uniform(0.15, top) * self.delay)
        policy = self.policies.get(agent_id)
        calls = policy(self.world, agent_id) if policy else []
        calls = calls or [("wait", {})]
        return LLMResponse(
            thought="（演示脚本）按当前状态决定下一步。",
            content="",
            tool_calls=[
                ToolCallData(id=f"call_{uuid.uuid4().hex[:10]}", name=name, arguments_json=json.dumps(args, ensure_ascii=False))
                for name, args in calls
            ],
            prompt_tokens=self.rng.randint(900, 3200),
            completion_tokens=self.rng.randint(40, 260),
            finish_reason="tool_calls",
            provider_id="demo",
            model_id="scripted-bot",
        )


def install_demo(orchestrator: Any, *, delay: float = 1.0) -> None:
    """Switch ``orchestrator`` to scripted agents and relax the model-binding checks."""
    cache: dict[int, DemoLlm] = {}

    def factory(orch: Any) -> DemoLlm:
        key = id(orch.world)  # the bot's script state survives pause / resume of the same game
        if key not in cache:
            cache.clear()
            cache[key] = DemoLlm(orch.world, delay=delay)
        return cache[key]

    orchestrator.demo_mode = True
    orchestrator.llm_port_factory = factory
    orchestrator.ensure_bindings_ready = lambda: None
    orchestrator.refresh_binding_issues = lambda: []
    orchestrator.session.binding_issues = []

    import app.api.providers as providers_api
    import app.api.routes as routes_api

    routes_api._require_complete_bindings = lambda: None  # type: ignore[assignment]
    providers_api.binding_issues = lambda: []  # type: ignore[assignment]
