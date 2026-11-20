"""Builders for the tests of the v0.14 bot (Composer, BotBrain, learning tools): a ``BotMind``, a scenario that declares situation tags,
boards with the things v0.14 adds (the hard facts, the cap of one message), wake envelopes.  No game vocabulary: the words are made up."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.agents.contract import load_default_contract
from app.bots.types import BotMind, ContractDoc, CoreFiles, PlaybookEntry, RulesMemory
from app.runtime.ports import WakeContext
from app.world.spec import ScenarioSpec
from app.world.types import MODERATOR_ID, Envelope

from .fakes import FakeWorld, make_spec

IDENTITY = "一个打了很多年线下局的老玩家，话不多，但一开口就是结论。"
VOICE = (
    "你说话短，通常两三句话就停，很少超过一百五十字。爱反问，喜欢直接点名。\n"
    "「你急什么？」「这票你自己信吗？」「我只看他投了谁。」\n"
    "篇幅：60–150 字"
)
MIND = "你先看发生了什么，再听谁说了什么。说得漂亮不等于是真的。\n你相信的东西都只是暂时的，被结果打脸就改。"
EXTRA = ("口头禅", "「行，听你的，出了再说。」")

PLAYBOOK = (
    PlaybookEntry("两个人都说自己是同一个身份时，先看谁的结论和之后的行动对得上。", "中", 3, 1, "复盘 2026-11-20", ("对跳", "发言")),
    PlaybookEntry("首夜的随手一步本来就没有道理可讲，追问它得不到信息。", "低", 0, 0, "初始化", ("发言",)),
    PlaybookEntry("先看规则给出的结果，再看人说了什么。", "高", 5, 0, "复盘 2026-11-21", ("投票", "发言")),
    PlaybookEntry("轮到自己行动时，先想清楚手里的信息是谁给的。", "中", 1, 0, "初始化", ("行动",)),
    PlaybookEntry("拿到专属身份的人才需要考虑这条。", "低", 0, 0, "初始化", ("甲类",)),
)


def make_mind(**over: Any) -> BotMind:
    """A bot with every layer filled in (``over`` replaces any field of ``BotMind``)."""
    data: dict[str, Any] = {
        "bot_id": "b-1a2b3c4d",
        "name": "Alex",
        "core": CoreFiles(identity=IDENTITY, voice=VOICE, mind=MIND, extras=(EXTRA,)),
        "arch_notes": "发言前先看状态板；被唤醒不一定要说话。",
        "rules": RulesMemory(
            scenario_id="fake",
            digest="我理解这个游戏的核心：每个人手里的信息不一样，规则产生的事实比任何人的话都硬。",
            playbook=PLAYBOOK,
            questions="还不确定平局时怎么处理。",
        ),
        "people": {"Blair": "话多，爱站边，前后常不一致", "Casey": "很少开口，一开口就有信息"},
        "record": "场次 7 · 胜 4 · 胜率 57%",
    }
    data.update(over)
    return BotMind(**data)


def rule(tag: str, **conditions: Any) -> dict[str, Any]:
    """A situation rule as the scenario declares it (``SituationRule``): every condition left out means "any"."""
    return {"tag": tag, "phase_contains": "", "channel_type": "", "channel_id": "", "events": [], "request_kind": "", **conditions}


SITUATIONS = [
    rule("发言", events=["floor.granted"]),
    rule("投票", events=["request.opened"], request_kind="choice"),
    rule("行动", events=["request.opened"], phase_contains="夜"),
    rule("警上", phase_contains="竞选", events=["floor.granted"]),
    rule("私聊", channel_type="dm"),
]


class SpecWithSituations:
    """A scenario that also declares ``situations`` (what ``ScenarioSpec`` gets from the engine stream): every attribute the agent
    layer reads comes from the real spec, ``situations`` from here."""

    def __init__(self, spec: ScenarioSpec, situations: list[Any] | None = None, **over: Any) -> None:
        self._spec = spec
        self.situations = SITUATIONS if situations is None else situations
        for key, value in over.items():
            setattr(self, key, value)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._spec, name)


def make_scenario(situations: list[Any] | None = None, **over: Any) -> SpecWithSituations:
    return SpecWithSituations(make_spec(), situations, **over)


def contract() -> ContractDoc:
    return load_default_contract()


# ---------------------------------------------------------------------------------------------------------- boards
FLOOR_ONE = {  # a queue token of ONE message: 300 characters, 600 in all (the old board showed both numbers)
    "id": "pub", "name": "公共频道", "type": "public", "mode": "queue", "members": [MODERATOR_ID, "p1", "p2"], "readonly": [],
    "frozen": False, "system": False, "can_write": True, "why_not": None, "max_chars": 300,
    "floor": {
        "kind": "queue", "active": "p1", "paused": False, "seconds_left": 40.0,
        "queue": [{"holder": "p1", "state": "active", "messages_used": 0, "max_messages": 1},
                  {"holder": "p2", "state": "pending", "messages_used": 0, "max_messages": 1}],
        "mine": {"messages_left": 1, "chars_per_message": 300, "chars_left": 600, "seconds_left": 40.0},
    },
}
FLOOR_THREE = {
    **FLOOR_ONE,
    "floor": {
        **FLOOR_ONE["floor"],
        "queue": [{"holder": "p1", "state": "active", "messages_used": 0, "max_messages": 3}],
        "mine": {"messages_left": 3, "chars_per_message": 300, "chars_left": 600, "seconds_left": 40.0},
    },
}
FREE = {
    "id": "talk", "name": "自由讨论", "type": "group", "mode": "free", "members": [MODERATOR_ID, "p1", "p2"], "readonly": [],
    "frozen": False, "system": False, "can_write": True, "why_not": None, "max_chars": 300,
    "floor": {"kind": "free", "mine": None,
              "free": {"elapsed_s": 5, "duration_s": 60, "chars_used": 10, "max_chars_total": 600, "silent_s": 2, "my_messages_left": 3,
                       "max_messages_per_member": 3, "max_chars_per_message": 300, "writers": None}},
}
CLOSED = {
    "id": "pub", "name": "公共频道", "type": "public", "mode": "closed", "members": [MODERATOR_ID, "p1", "p2"], "readonly": [],
    "frozen": False, "system": False, "can_write": False, "why_not": "CHANNEL_CLOSED：该频道当前已关闭，不能发言。", "max_chars": None,
    "floor": {"kind": "none", "active": None, "paused": False, "seconds_left": None, "queue": [], "mine": None, "free": None},
}
VOTE = {
    "id": "r7", "kind": "choice", "question": "投谁？", "options": [{"id": "p1", "label": "Alex"}, {"id": "p2", "label": "Blair"}],
    "seconds_left": 80.0, "sealed": True, "answered": False, "to": ["p1", "p2"], "share_answers": False, "auto_close": "all_answered",
}
ACTION = {**VOTE, "id": "r8", "question": "今晚选谁？", "to": ["p1"]}
ASK = {**VOTE, "id": "r9", "kind": "text", "question": "说说你的想法", "options": []}

FACTS = [
    {"seq": 3, "phase": "第 1 夜", "kind": "fact", "actor_id": None, "text": "今晚没有人出局。", "template_id": "calm"},
    {"seq": 9, "phase": "第 1 天", "kind": "claim", "actor_id": "p2", "text": "我投 p1。理由：他太安静", "template_id": "say_vote"},
    {"seq": 11, "phase": "第 1 天", "kind": "fact", "actor_id": None, "text": "投票结果：p1 一票，p2 零票。", "template_id": "tally"},
]


def put_board(world: FakeWorld, agent: str = "p1", **changes: Any) -> dict[str, Any]:
    """Give ``agent`` a board that differs from the fake world's default by ``changes``."""
    board = dict(world.board(agent))
    board.update(changes)
    world.boards[agent] = board
    return board


def floor_granted(channel_id: str = "pub", *, seq: int = 3, max_messages: int = 1, **payload: Any) -> Envelope:
    return Envelope(
        seq=seq, kind="floor", priority="urgent", channel_id=channel_id,
        payload={"event": "floor.granted", "max_messages": max_messages, "max_chars_per_message": 300, "max_chars_total": 600,
                 "seconds": 120, **payload},
    )


def request_opened(request: dict[str, Any], *, seq: int = 5) -> Envelope:
    return Envelope(
        seq=seq, kind="request", priority="urgent",
        payload={"event": "request.opened", "request_id": request["id"], "kind": request["kind"], "question": request["question"],
                 "options": request["options"], "seconds": 80},
    )


def message_new(sender: str = "p2", channel_id: str = "pub", *, seq: int = 0) -> Envelope:
    return Envelope(seq=seq, kind="message", priority="normal", channel_id=channel_id, sender=sender, payload={"event": "message.new"})


def wake(agent: str, *envs: Envelope, kind: str = "start", after: int = 0) -> WakeContext:
    return WakeContext(agent_id=agent, kind=kind, gen_id=3, reasons=list(envs), after_seq=after)  # type: ignore[arg-type]


def simple_namespace(**fields: Any) -> SimpleNamespace:
    return SimpleNamespace(**fields)
