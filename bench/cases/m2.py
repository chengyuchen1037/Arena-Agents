"""Suite M2 (对局记忆召回): eleven questions asked at the end of a long game about what happened at the beginning.

A four-day game is scripted on the real rules (night 1: a villager is killed; day 1: a hunter wins the badge against a wolf, the seer calls a wolf out, the
wolf is exiled; night 2: the hunter-sheriff is killed, shoots a wolf and passes the badge; day 2: the seer calls the white wolf, who is exiled; night 3: a god
is killed; day 3: nobody is exiled; night 4: another villager is killed) - about 370 ledger entries and 12 000 characters of history: as much as a bot sees.  The
suite runs its bots with ``@history=6000`` (``bench.bots``: the same narrowing for ``legacy`` and for ``bot:``), so the first night, the election and the first
day have scrolled out of what they see - the way a long game pushes them out.  Then, in the middle of day 4, the moderator asks one question about an
early event: a ``choice`` request with four options, or a ``text`` request answered in words.  The answer is scored by the machine (``truth.answer``; for
text: the answer must say it and must not say the plausible wrong ones).

What is measured (plan §6.5 M2): is what happened early still there at the end?  Every answer is in the hard-fact table (announcements, tallies, claims, the
seer's own results are facts) - which keeps what the history has lost - so a bot that reads its table answers them, and one that answers from the visible
history alone does not.  The wrong options are the *other* events of the same game (the night-2 victim for a question about night 1, the successor of the
badge for a question about the election, the target everybody else voted for for a question about one voter), so "somebody died" or "the majority did X"
is not enough.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from bench.authoring import Table, by_seat, check_claim
from bench.cases.common import Case, Layout, make_header
from bench.cases.families import ballots, expect_dead
from bench.cases.layouts import LAYOUTS as LAY

SUITE = "M2"
SIDE = "狼人阵营"


@dataclass
class Chronicle:
    table: Table
    L: Layout
    k1: str  # night-1 victim, announced on day 1
    candidates: list[str]
    sheriff: str  # the hunter, elected on day 1
    wolf1: str  # the wolf the seer called out on day 1 and the table exiled
    wolf2: str  # the wolf who ran against the hunter and was shot by him
    wolf3: str
    scapegoat: str  # the villager the wolves voted for on day 1 (voted wolf2 himself)
    shy: str  # the villager who abstained on day 1 and voted the real wolf on day 2
    votes1: dict[str, str]
    votes2: dict[str, str]
    k2: str  # night-2 victim (the sheriff)
    shot: str  # the hunter's target
    badge_to: str
    white_wolf: str  # exiled on day 2
    k3: str  # night-3 victim
    k4: str  # night-4 victim
    claims: dict[int, tuple[str, str] | None]  # day -> (target, side) the seer reported (None: the seer is the seat under test)
    checked: dict[int, tuple[str, str]]  # night -> (target, side) the seer found


def long_game(L: Layout, seed: int) -> Chronicle:
    """The scripted four-day game of the module docstring on layout ``L``; the table is left in the middle of day 4 with every speech spoken."""
    t = Table(L.role_list, bot=L.bot, seed=seed)
    t.long_lines = True
    bot, seer, witch, hunter, guard, ww = L.bot, L.seer, L.witch, L.hunter, L.guard, L.ww
    w1, w2, w3 = by_seat(L.wolves)
    villagers = [v for v in by_seat(L.villagers) if v != bot]
    k1, scapegoat, shy = villagers[0], villagers[1], villagers[2]
    # ---- night 1, election, day 1
    t.open()
    t.night(1, guard=scapegoat, wolf=k1, seer=w1, witch="pass")
    expect_dead(t, k1)
    cands = [hunter, w2]
    below = [s for s in by_seat(t.alive) if s not in cands]
    t.election(
        candidates=cands, ballots={s: (w2 if s in (w1, w3, ww) else hunter) for s in below},
        turns={
            hunter: "我是好人，上警是想帮大家把每天的票型和发言记清楚，归票的时候有据可查。",
            w2: "我也是好人，上警想带大家把票型理清楚。大家要是不放心，把警徽投给别人也行，我没有意见。",
        },
    )
    t.dawn(1)
    t.last_words(1, {k1: "我是好人，没有信息可以给大家。希望大家别被带节奏，好好听、好好投。"})
    turns: dict[str, Any] = {}
    claims: dict[int, tuple[str, str] | None] = {1: None, 2: None}
    if seer != bot:
        turns[seer] = check_claim(w1, SIDE)
        turns[w1] = f"我是好人，{seer} 报我是狼人阵营，这个查验我不认，请大家听清楚再投。"
        claims[1] = (w1, SIDE)
    t.talk(1, turns, direction="警右")
    votes1 = {s: w1 for s in t.alive}
    votes1.update({w2: scapegoat, w3: scapegoat, ww: scapegoat, w1: seer, scapegoat: w2, shy: "abstain"})
    t.day_vote(1, ballots(t.alive, votes1), exile=w1, last="我是好人，这个结果我不认。后面的人自己看，我不多说了。")
    # ---- night 2, day 2
    t.night(2, guard=seer, wolf=hunter, seer=ww, witch="pass")
    expect_dead(t, hunter)
    t.dawn(2)
    t.hunter_shoots(hunter, w2)
    badge_to = seer if seer != bot else witch
    t.badge(hunter, badge_to)
    turns = {}
    if seer != bot:
        turns[seer] = check_claim(ww, SIDE)
        turns[ww] = f"我是好人。{seer} 又来报我，这个查验我不认，我不接受这样被点名。"
        claims[2] = (ww, SIDE)
    t.talk(2, turns, direction="警左")
    votes2 = {s: ww for s in t.alive}
    votes2.update({w3: shy, ww: shy, shy: w3})
    t.day_vote(2, ballots(t.alive, votes2), exile=ww, last="好吧，你们投吧。我是好人，这一票出去我也认了，剩下的你们自己想。")
    # ---- night 3, day 3 (nobody is exiled)
    k3 = guard if guard != bot else witch
    t.night(3, guard=None, wolf=k3, seer=w3, witch="pass")
    expect_dead(t, k3)
    t.dawn(3)
    t.talk(3, {}, direction="警右")
    t.day_vote(3, ballots(t.alive, {}), exile=None)
    # ---- night 4, day 4 (the question is asked once everybody has spoken)
    k4 = scapegoat
    t.night(4, guard=None, wolf=k4, seer=w3, witch="pass")
    expect_dead(t, k4)
    t.dawn(4)
    t.talk(4, {}, direction="警左")
    return Chronicle(
        t, L, k1, cands, hunter, w1, w2, w3, scapegoat, shy, votes1, votes2, hunter, w2, badge_to, ww, k3, k4, claims,
        {1: (w1, SIDE), 2: (ww, SIDE), 3: (w3, SIDE), 4: (w3, SIDE)},
    )


# ---------------------------------------------------------------------------------------------------------------------- the questions
@dataclass(frozen=True)
class Question:
    key: str
    title: str
    question: str
    answer: str  # a seat id, "abstain", or (text) the word the answer must say
    preferred: tuple[str, ...]  # the other events of the game that the wrong options are taken from
    evidence: tuple[str, ...]  # what in the hard-fact table carries the answer
    kind: str = "choice"
    wrong: tuple[str, ...] = ()  # (text) words the answer must not say
    note: str = ""


#: Which seat layout (and so which seat under test) each question is asked on: villagers' views, and the seer who knows her own results.
LAYOUT_OF = {
    "night1-victim": "v1", "day1-exile": "v2", "day1-ballot": "v3", "day2-ballot": "v4", "sheriff": "v5", "badge": "v6", "hunter-shot": "v7", "day2-exile": "v8",
    "seer-report": "v1", "dead-count": "v2", "own-check": "seer",
}
KEYS = tuple(LAYOUT_OF)


def question(c: Chronicle, key: str) -> Question:
    """The question ``key`` about the game ``c``."""
    seer, guard, witch = c.L.seer, c.L.guard, c.L.witch
    if key == "night1-victim":
        return Question(key, "第 1 夜死的是谁", "第 1 天公布死讯时，出局的是谁？", c.k1, (c.wolf1, c.k2, c.k3, c.shot), (f"【出局公告】{c.k1}",),
                        note="之后每一天、每一夜都有人出局：第 1 天的那一个是谁。")
    if key == "day1-exile":
        return Question(key, "第 1 天放逐了谁", "第 1 天的放逐投票，谁被放逐了？", c.wolf1, (c.k1, c.k2, c.white_wolf, c.scapegoat), (f"【出局公告】{c.wolf1}", "【投票结果】"),
                        note="第 2 天放逐的是另一个人。")
    if key == "day1-ballot":
        target = c.votes1[c.scapegoat]
        return Question(key, "第 1 天某个人投了谁", f"第 1 天的放逐投票里，{c.scapegoat} 投给了谁？", target, (c.wolf1, "abstain", seer, c.shy), (f"{c.scapegoat}→{target}",),
                        note=f"{c.scapegoat} 没有跟多数投被放逐的人；多数人投的是 {c.wolf1}。")
    if key == "day2-ballot":
        target = c.votes2[c.shy]
        return Question(key, "第 2 天某个人投了谁", f"第 2 天的放逐投票里，{c.shy} 投给了谁？", target, (c.white_wolf, "abstain", c.scapegoat, seer), (f"{c.shy}→{target}",),
                        note=f"{c.shy} 没有跟多数投被放逐的人；多数人投的是 {c.white_wolf}。")
    if key == "sheriff":
        return Question(key, "谁当选了警长", "第 1 天的警长竞选，谁当选了警长？", c.sheriff, (c.candidates[1], c.badge_to, guard), (f"【警长】{c.sheriff}",),
                        note="警徽后来移交给了别人：别的人也“当过警长”。")
    if key == "badge":
        return Question(key, "警徽后来给了谁", "警长出局之后，警徽移交给了谁？", c.badge_to, (c.sheriff, c.candidates[1], guard, witch), (f"【警长】{c.badge_to}",),
                        note="第 1 天当选的是另一个人。")
    if key == "hunter-shot":
        return Question(key, "猎人带走了谁", "猎人出局的时候开枪带走了谁？", c.shot, (c.wolf1, c.white_wolf, c.k1, c.k3), (f"【猎人开枪】{c.sheriff}",))
    if key == "day2-exile":
        return Question(key, "第 2 天放逐了谁", "第 2 天的放逐投票，谁被放逐了？", c.white_wolf, (c.wolf1, c.shot, c.k2, c.k3), (f"【出局公告】{c.white_wolf}",),
                        note="第 1 天放逐的是另一个人。")
    if key == "seer-report":
        target = (c.claims[1] or ("", ""))[0]
        return Question(key, "预言家第 1 天报的是谁（文字作答）", "第 1 天，预言家公开报了谁是狼人阵营？请只回答一个座位号。", target, (), (f"我报查验：{target} ",), "text",
                        wrong=(c.white_wolf, c.shot, c.badge_to), note="第 2 天他又报了另一个人：只说第 1 天的那个。")
    if key == "dead-count":
        count = len(c.table.out)
        return Question(key, "一共出局了几个人（文字作答）", "到现在为止，一共有几个人出局了（含被放逐、被刀、被带走的）？请只回答一个数字。", str(count), (), (f"【出局公告】{c.k4}",), "text",
                        wrong=(str(count - 1), str(count + 1)), note="要把每一天、每一夜的出局数起来，不是只数最近的。")
    if key == "own-check":
        target, _ = c.checked[1]
        return Question(key, "预言家自己第 1 夜验了谁", "你（预言家）第 1 夜验的是谁？", target, (c.checked[2][0], c.checked[3][0], c.k1, c.shot), (f"【查验结果】{target}",),
                        note="第 2、3、4 夜你验的是另外的人（都是狼人阵营）。")
    raise KeyError(key)


def _options(answer: str, preferred: tuple[str, ...], rng: random.Random, universe: list[str]) -> list[dict[str, str]]:
    ids = [answer]
    for seat in [*preferred, *rng.sample(universe, len(universe))]:
        if seat not in ids:
            ids.append(seat)
        if len(ids) == 4:
            break
    rng.shuffle(ids)
    return [{"id": i, "label": "弃票" if i == "abstain" else i} for i in ids]


def build(index: int, key: str) -> tuple[str, dict[str, Any]]:
    c = long_game(LAY[LAYOUT_OF[key]], 3500 + index)
    q = question(c, key)
    t = c.table
    rng = random.Random(3500 + index)
    universe = [s for s in by_seat([f"N{i}" for i in range(1, 13)]) if s != t.bot]
    t.mod("set_phase", {"text": "回忆测验"}, dt=3.0)
    args: dict[str, Any] = {"to": [t.bot], "kind": q.kind, "question": f"回忆测验：{q.question}", "duration_s": 90}
    if q.kind == "choice":
        args["options"] = _options(q.answer, q.preferred, rng, universe)
    t.mod("open_request", args, label="recall", dt=2.0)
    decisive = f"正确答案是 {q.answer}。出处：硬事实台（公告 / 票型 / 声明 / 自己的查验结果，可见历史被挤出去之后仍然在）。" + (q.note or "")
    trap = q.note or "同一局里别的事件的主角是干扰项。"
    truth: dict[str, Any] = {"request": "@recall", "answer": [q.answer], "kind": "recall", "decisive": decisive, "evidence": list(q.evidence), "trap": trap}
    if q.kind == "text":
        truth.update({"match": "text", "wrong": list(dict.fromkeys(q.wrong))})
    built = type("Built", (), {"table": t, "decisive": decisive, "evidence": list(q.evidence), "answer": [q.answer], "trap": trap})()
    header = make_header(
        SUITE, q.title, f"对局后段问前期的事实（{'文字作答' if q.kind == 'text' else '四选一'}）：{q.question}", built,
        extra=["答案在【硬事实台】里。M2 套件用 @history=6000 跑 bot：可见历史只剩最近 6000 字，前几个夜晚、警长竞选和第 1 天已经被挤出去（不加这个选项时上限是 12000 字，整局刚好放得下）。"],
    )
    data = t.situation(
        sid=f"m2-{key}", title=f"回忆：{q.title}", tags=[SUITE, q.kind], decision={"seat": t.bot, "expect": ["respond"]}, truth=truth,
        brief=f"四天的长局之后的回忆测验：{q.question}", meta={"kind": q.kind, "asked_about": key},
    )
    return header, data


def cases() -> list[Case]:
    return [Case(f"m2-{key}", SUITE, f"situations/m2-{key}.yaml", (lambda i=index, k=key: build(i, k))) for index, key in enumerate(KEYS)]


__all__ = ["Chronicle", "KEYS", "build", "cases", "long_game", "question"]
