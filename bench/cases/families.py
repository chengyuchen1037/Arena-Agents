"""The *families* of decisive facts the L suites are made of.  Each family is a small story on the real rules, ending at a vote the bot must cast; the story
holds ONE fact that decides who the wolf is, and that fact can be found in the bot's own view (the hard-fact table, the visible history, its private messages).

======  =====================  ==================================================================================================================
family  name                   the decisive fact
======  =====================  ==================================================================================================================
F1      impossible check       a "seer" reports a check, on night 2, of a player who died on night 1 (and was announced) - the seer's night-2
                               options only list the living
F2      gold water, exploded   a "seer" reports the white wolf as good (金水); the white wolf then blows himself up and shows who he is
F3      false hunter           a candidate claims to be the hunter; the real hunter dies in the night and shoots - the shot names him
F4      checked hunter         a "seer" reports the (later exiled) hunter as a wolf (查杀); the hunter's shot names him a hunter - a good player
F5      dead voter             a player says "N9 voted N1 yesterday"; N9 had been announced out before that vote, and the tally has no N9
F6      declared vs ballot     a player *declares* a vote for X (template, a claim in the table); the tally (a fact) shows he abstained
F9      changed story          a "seer" names two different targets for the same night, a day apart
P1      offside slip           (the bot is the witch) before the death is announced, a candidate speaks of the night's victim as gone
P2      seer, privately        (the bot is the seer) a rival reports a check of a player the bot checked, with the opposite result
P3      same guard, same save  (the bot is the guard) someone claims to have saved with the antidote the very player the bot guarded - on a quiet night
======  =====================  ==================================================================================================================

Every function returns a :class:`Built`: the table (steps), the decisive sentence, the evidence strings (which the tests look for in what the bot sees), the
truth (who is right to vote for) and the trap (what pulls the other way).  Speeches are written in three styles (``bench.cases.common.styled``): the same
facts, said in a few words, in a few sentences, or at length - the L1 pairs swap them between the wolf and the good player.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from bench.authoring import AuthoringError, Table, by_seat, check_claim, seat_number, vote_claim, vote_summary
from bench.cases.common import BLUNT, FLUENT, PLAIN, Layout, styled


@dataclass
class Built:
    table: Table
    decisive: str  # the fact, in words (for the judge and the header)
    evidence: list[str]  # strings the bot can read it from
    answer: list[str]  # who to vote for
    trap: str = ""
    kind: str = ""
    x: str = ""  # the wolf the question is about
    y: str = ""  # the good player he is set against (the real seer, the accused, the other candidate)
    sides: dict[str, str] = field(default_factory=dict)  # {"fluent": seat, "blunt": seat} for the L1 pairs (filled by the pair builder)
    roles: dict[str, str] = field(default_factory=dict)  # who is who among the players the question is about (for the judge)
    notes: str = ""


def wolves_of(layout: Layout) -> list[str]:
    return by_seat([*layout.wolves, layout.ww])


def ballots(alive: Sequence[str], votes: Mapping[str, str]) -> dict[str, str]:
    """Every living voter's ballot: ``votes`` says what each one voted, the rest abstain."""
    unknown = set(votes) - set(alive)
    if unknown:
        raise AuthoringError(f"不在场的人投了票：{sorted(unknown)}")
    return {seat: votes.get(seat, "abstain") for seat in by_seat(list(alive))}


def pick(layout: Layout, count: int, *, exclude: Sequence[str]) -> list[str]:
    """The first ``count`` good players (in seat order, never the seer) that are not in ``exclude`` - the bystanders of a story."""
    found = [s for s in by_seat(layout.good()) if s not in exclude and s != layout.seer]
    if len(found) < count:
        raise AuthoringError(f"好人不够用：要 {count} 个，只有 {found}。")
    return found[:count]


def _s(extra: str, style: str, *, fluent: str, plain: str, blunt: str) -> str:
    """``styled`` plus an ``extra`` sentence (the trivia of an L2 question: a habit, a wording, the order of speaking) that changes nothing about the facts."""
    return styled(style, fluent=fluent, plain=plain, blunt=blunt) + (f" {extra}" if extra else "")


def _by(bystander: tuple[str, str] | None) -> dict[str, str]:
    """The turn of a bystander (a player with no stake who remarks on the trivia of the two accounts - an L2 question's red herring)."""
    return {bystander[0]: bystander[1]} if bystander else {}


def expect_dead(t: Table, *seats: str) -> None:
    """The night's settlement the author wanted (a knife that the guard guarded, or a save, would leave nobody out and break the story)."""
    if sorted(t.pending) != sorted(seats):
        raise AuthoringError(f"这一夜出局的应该是 {sorted(seats)}，结算出来的是 {sorted(t.pending)}（守卫 / 解药把刀挡掉了？）。")


def _require_before(a: str, b: str, why: str) -> None:
    if seat_number(a) >= seat_number(b):
        raise AuthoringError(f"{a} 要比 {b} 先发言（{why}），所以座位号要更小。")


# ---------------------------------------------------------------------------------------------------------------------- F1
def f1_impossible_check(L: Layout, *, x: str, dead: str, exiled: str, safe: str, style_x: str = PLAIN, style_y: str = PLAIN, extra_x: str = "", extra_y: str = "", seed: int, n2: str = "peaceful", bystander: tuple[str, str] | None = None) -> Built:
    """The fake seer ``x`` reports a night-2 check of ``dead`` (out since night 1).  ``exiled`` goes out on day 1; ``safe`` is the player both claimers report
    as good.  ``x`` speaks first."""
    y = L.seer
    _require_before(x, y, "他的发言不提对面，真预言家的发言才回应他")
    t = Table(L.role_list, bot=L.bot, seed=seed)
    t.styled_seats = {x, y}
    wolves = wolves_of(L)
    t.open()
    t.night(1, guard=L.guard, wolf=dead, seer=exiled, witch="pass")
    expect_dead(t, dead)
    t.election(candidates=[])
    t.dawn(1)
    t.last_words(1, {dead: "我是平民，没有信息给大家，今天好好投，别因为我走了就乱。"})
    t.talk(1, {})
    helpers = pick(L, 1, exclude=[dead, exiled, L.bot, safe])
    t.day_vote(1, ballots(t.alive, {**{w: exiled for w in wolves}, **{h: exiled for h in helpers}, exiled: safe}), exile=exiled, last="我是好人，没有身份可报，被票出去了，你们自己当心。")
    t.night(2, guard=safe if n2 == "peaceful" else None, wolf=safe if n2 == "peaceful" else pick(L, 1, exclude=[dead, exiled, L.bot, safe, *helpers])[0], seer=safe, witch="pass")
    t.dawn(2)
    claim = f"第 2 夜验了 {dead}，好人阵营"
    t.talk(2, {**_by(bystander),
        x: _s(extra_x, style_x, blunt=f"我是预言家。第 1 夜验了 {safe}，好人阵营；{claim}。没查杀。",
                  plain=f"我是预言家。第 1 夜验了 {safe}，好人阵营；{claim}。没有查杀。有人对跳的话，请大家对照两边的记录再投票。",
                  fluent=f"我把我的身份和验人记录完整说一遍。我是预言家：第 1 夜验了 {safe}，好人阵营；{claim}。这就是我手里全部的信息，没有查杀，也没有隐瞒。"
                         "今天场上如果出现和我对跳的人，请大家把两个人的记录放在一起，核对谁的信息经得起公开事实的检验。投票时请按各自的判断来，我愿意接受任何核对。"),
        y: _s(extra_y, style_y, blunt=f"我才是预言家。第 1 夜验了 {exiled}，好人阵营；第 2 夜验了 {safe}，好人阵营。没查杀。",
                  plain=f"我也是预言家。第 1 夜验了 {exiled}，好人阵营；第 2 夜验了 {safe}，好人阵营。没有查杀。我们都报了 {safe} 是好人，请大家对照两边的记录再投。",
                  fluent=f"对面已经跳了预言家，那我也把我的完整记录报出来：第 1 夜验了 {exiled}，好人阵营；第 2 夜验了 {safe}，好人阵营。我没有查杀可报，也不会为了抢话语权现编一个。"
                         f"我们两个人都报了 {safe} 是好人，这一项没有分歧。其余的，请大家拿两个人的记录去对照公开信息，自己判断；投票前我会再说明我的看法。"),
    })
    helpers2 = pick(L, 1, exclude=[L.bot, *t.out, safe])
    t.decision_vote(2, ballots(t.alive, {**{w: y for w in wolves if w in t.alive}, **{h: x for h in helpers2}}))
    return Built(
        t, f"{x} 报告第 2 夜验了 {dead}，但 {dead} 在第 1 夜就出局了（第 1 天公布），第 2 夜的查验名单里没有他。",
        [f"【出局公告】{dead}", claim], [x],
        trap=f"{x} 和 {y} 都以预言家自居，记录都报了 {safe} 是好人；两人的发言都有条理。", kind="impossible_check", x=x, y=y, roles={x: "werewolf", y: "seer"},
    )


# ---------------------------------------------------------------------------------------------------------------------- F2
def f2_gold_water_exploded(L: Layout, *, x: str, dead: str, taken: str, safe: str, style_x: str = PLAIN, style_y: str = PLAIN, extra_x: str = "", extra_y: str = "", seed: int) -> Built:
    """The fake seer ``x`` calls the white wolf good on day 1; the white wolf explodes in his turn (taking ``taken``).  Order of turns: x < seer < white wolf."""
    y, ww = L.seer, L.ww
    _require_before(x, y, "他先报，真预言家后报")
    _require_before(y, ww, "白狼王最后才自爆")
    t = Table(L.role_list, bot=L.bot, seed=seed)
    t.styled_seats = {x, y}
    wolves = wolves_of(L)
    t.open()
    t.night(1, guard=L.guard, wolf=dead, seer=safe, witch="pass")
    expect_dead(t, dead)
    t.election(candidates=[])
    t.dawn(1)
    t.last_words(1, {dead: "我是平民，没有信息给大家。今天别乱投，听清楚再动手。"})
    claim = f"第 1 夜验了 {ww}，好人阵营"
    order = t.talk(1, {
        x: _s(extra_x, style_x, blunt=f"我是预言家。{claim}。今天先别急着出人。",
                  plain=f"我是预言家。{claim}。今天先别急着出人，听听对面怎么说，{ww} 是好人，可以放心。",
                  fluent=f"我是预言家。这是我的第 1 夜验人记录：{claim}。昨晚出了一个死者，信息很少，我作为预言家的责任是把我手里确定的东西亮出来，而不是猜。"
                         f"我的建议分三点：第一，今天先不要急着出人，看看有没有人和我对跳；第二，{ww} 是我验出来的好人，大家可以放心把他当成自己人；第三，投票前我还会补充。我愿意接受任何核对。"),
        y: _s(extra_y, style_y, blunt=f"我是预言家，和 {x} 对跳。第 1 夜验了 {safe}，好人阵营。没查杀。{x} 报的 {ww} 我没信息。",
                  plain=f"我才是预言家。第 1 夜验了 {safe}，好人阵营。没有查杀。{x} 报的 {ww} 我没有信息，先看后面。",
                  fluent=f"{x} 和我对跳，我把我的记录完整报出来：第 1 夜验了 {safe}，好人阵营。我没有查杀，也不想编一个。{x} 报的 {ww}，我这边没有任何信息，不评价。"
                         "我的建议是：今天先别急着出人，看接下来的发言和行动；信息多起来之后再比较两个预言家的说法，我会在投票前说明我的看法。"),
    }, until=ww)
    t.explode(ww, taken, "这一把我先走一步，你们接着演。")
    t.night(2, guard=L.hunter, wolf=L.hunter, seer=x, witch="pass")
    t.dawn(2)
    t.talk(2, {})
    helpers = pick(L, 1, exclude=[dead, taken, L.bot, safe])
    t.decision_vote(2, ballots(t.alive, {**{w: y for w in wolves if w in t.alive}, **{h: x for h in helpers}}))
    del order
    return Built(
        t, f"{x} 报告第 1 夜验了 {ww}，好人阵营；随后 {ww} 自爆，公告写明他是白狼王（狼人阵营）——真预言家不会把狼人阵营的人报成好人。",
        [claim, f"【自爆】{ww}"], [x], trap=f"{y} 的发言和 {x} 一样有条理；{ww} 自爆带走了 {taken}，场面很乱。", kind="gold_water_exploded", x=x, y=y, roles={x: "werewolf", y: "seer", ww: "white_wolf"},
    )


# ---------------------------------------------------------------------------------------------------------------------- F3
def f3_false_hunter(L: Layout, *, x: str, y: str, shot: str, style_x: str = PLAIN, style_y: str = PLAIN, extra_x: str = "", extra_y: str = "", seed: int) -> Built:
    """Day 1.  Candidate ``x`` (a wolf) claims the hunter in his election speech; candidate ``y`` (a villager) does not claim.  The real hunter (``L.hunter``)
    is killed in the night and shoots ``shot``.  ``y`` wins the badge."""
    hunter = L.hunter
    if x not in L.wolves or y not in L.villagers or L.bot in (x, y) or L.bot == hunter:
        raise AuthoringError("x 要是狼人，y 要是村民，被测座位不能是他们或猎人。")
    t = Table(L.role_list, bot=L.bot, seed=seed)
    t.styled_seats = {x, y}
    wolves = wolves_of(L)
    t.open()
    t.night(1, guard=L.guard, wolf=hunter, seer=shot, witch="pass")
    expect_dead(t, hunter)
    claim = "我是猎人，手里有一发子弹"
    order = by_seat([x, y])
    speeches = {
        x: _s(extra_x, style_x, blunt=f"{x} 上警。{claim}。投我。",
                  plain=f"我是 {x}，{claim}，上警是想带队。投我，我会把发言顺序和归票理由说清楚。",
                  fluent=f"各位好，我是 {x}，今天上警，我想把我的身份和打算说清楚。{claim}，所以我愿意站在前面带队：第一，我会认真听每个人的发言，不凭感觉点人；"
                         "第二，遇到有人对我的身份有疑问，我欢迎大家用后面的信息来检验；第三，如果我当选警长，我会把发言顺序和归票理由都公开说明。请大家把警徽投给能负责任的人。"),
        y: _s(extra_y, style_y, blunt=f"{y} 上警。平民，没技能。投我，我归票。",
                  plain=f"我是 {y}，我是平民，没有技能。上警是想帮大家记发言和归票。",
                  fluent=f"我是 {y}，我不跳身份，但我愿意上警：我是个普通玩家，没有技能，我上警是因为想把场上的发言记录整理出来，帮大家记住谁说过什么、归票时把票型说清楚。"
                         "如果你们需要一个愿意干活、不抢风头的警长，请考虑我。"),
    }
    below = [s for s in t.alive if s not in (x, y)]
    wolf_votes = [w for w in wolves if w != x and w in below]
    t.election(candidates=[x, y], turns=speeches, ballots={s: (x if s in wolf_votes else y) for s in below})
    t.dawn(1)
    t.hunter_shoots(hunter, shot)
    t.last_words(1, {hunter: f"我是猎人，开枪带走 {shot}。其他的我不多说，大家自己判断。"})
    t.talk(1, {}, direction="警左")
    t.decision_vote(1, ballots(t.alive, {**{w: y for w in wolves if w in t.alive}}))
    del order
    return Built(
        t, f"{x} 在竞选演说里说“我是猎人”；之后真正的猎人 {hunter} 夜里被刀，开枪时公告写明他是猎人——场上只有一个猎人。",
        [claim, f"【猎人开枪】{hunter}"], [x], trap=f"{x} 的竞选演说有条理、有担当，像个好警长；{y} 只是个平民。", kind="false_hunter", x=x, y=y, roles={x: "werewolf", y: "villager", hunter: "hunter"},
    )


# ---------------------------------------------------------------------------------------------------------------------- F4
def f4_checked_hunter(L: Layout, *, x: str, dead: str, shot: str, safe: str, style_x: str = PLAIN, style_y: str = PLAIN, extra_x: str = "", extra_y: str = "", seed: int, bystander: tuple[str, str] | None = None) -> Built:
    """Day 1: the fake seer ``x`` reports the hunter as a wolf (查杀) and the table exiles him; he shoots ``shot``.  Day 2: the bot votes.  ``x`` < the seer."""
    y, hunter = L.seer, L.hunter
    _require_before(x, y, "他先报，真预言家后报")
    t = Table(L.role_list, bot=L.bot, seed=seed)
    t.styled_seats = {x, y}
    wolves = wolves_of(L)
    t.open()
    t.night(1, guard=L.guard, wolf=dead, seer=safe, witch="pass")
    expect_dead(t, dead)
    t.election(candidates=[])
    t.dawn(1)
    t.last_words(1, {dead: "我是平民，没有信息给大家。今天听清楚再投，别被带着走。"})
    claim = f"第 1 夜验了 {hunter}，狼人阵营"
    t.talk(1, {
        x: _s(extra_x, style_x, blunt=f"我是预言家。{claim}。出他。",
                  plain=f"我是预言家。{claim}，这是查杀。今天出 {hunter}，对跳的我后面再核对。",
                  fluent=f"我是预言家，我把这一夜的验人结果完整报出来：{claim}。这是一个查杀。我知道在没有后续信息的时候指认一个人很冒险，但预言家的价值就在于把确定的信息交给好人。"
                         f"所以我的建议是：今天把票集中在 {hunter} 身上，让他在遗言里给大家一个解释；如果有人对跳，我会逐条核对，但眼下优先处理查杀。请好人阵营的朋友相信查验的结果。"),
        y: _s(extra_y, style_y, blunt=f"我是预言家，和 {x} 对跳。第 1 夜验了 {safe}，好人阵营。没查杀。{hunter} 我没信息。",
                  plain=f"我才是预言家。第 1 夜验了 {safe}，好人阵营。没有查杀。{x} 报的 {hunter} 我没信息，今天别急着出人。",
                  fluent=f"{x} 和我对跳，我先报我自己的记录：第 1 夜验了 {safe}，好人阵营。我没有查杀。{x} 报的 {hunter}，我这边没有任何信息，不能验证，也不想跟着起哄。"
                         "我建议今天谨慎一点：一个只有一面之词的查杀，不足以让我们放逐一个人；请大家把两个预言家各自的说法记下来，后续的信息会说明谁在说真话。"),
    })
    helpers = pick(L, 1, exclude=[dead, L.bot, safe])
    t.day_vote(1, ballots(t.alive, {**{w: hunter for w in wolves}, **{h: hunter for h in helpers}, y: x}), exile=hunter)
    t.hunter_shoots(hunter, shot)
    t.last_words(1, {hunter: f"我是猎人，开枪带走 {shot}。别的我不多说了。"})
    t.night(2, guard=safe, wolf=safe, seer=x, witch="pass")
    t.dawn(2)
    t.talk(2, {**_by(bystander),})
    helpers2 = pick(L, 1, exclude=[L.bot, *t.out, safe])
    t.decision_vote(2, ballots(t.alive, {**{w: y for w in wolves if w in t.alive}, **{h: x for h in helpers2}}))
    return Built(
        t, f"{x} 报告 {hunter} 是狼人阵营（查杀）；{hunter} 被放逐后开枪，公告写明他是猎人——猎人是好人，预言家不会把他查成狼人阵营。",
        [claim, f"【猎人开枪】{hunter}"], [x], trap=f"{x} 的查杀说得斩钉截铁，投票也跟着走了；{y} 对不能验证的事保持克制，听上去像在躲。", kind="checked_hunter", x=x, y=y,
        roles={x: "werewolf", y: "seer", hunter: "hunter"},
    )


# ---------------------------------------------------------------------------------------------------------------------- F5
def f5_dead_voter(L: Layout, *, x: str, y: str, dead: str, exiled: str, style_x: str = PLAIN, style_y: str = PLAIN, extra_x: str = "", extra_y: str = "", seed: int, n2: str = "death", bystander: tuple[str, str] | None = None) -> Built:
    """Day 2: the wolf ``x`` says the dead ``dead`` and ``y`` both voted ``exiled`` on day 1 - ``dead`` was out before that vote.  ``x`` speaks before ``y``."""
    _require_before(x, y, "他先指控，被指控的人后辩解")
    t = Table(L.role_list, bot=L.bot, seed=seed)
    t.styled_seats = {x, y}
    wolves = wolves_of(L)
    t.open()
    t.night(1, guard=L.guard, wolf=dead, seer=exiled, witch="pass")
    expect_dead(t, dead)
    t.election(candidates=[])
    t.dawn(1)
    t.last_words(1, {dead: "我是平民，没有信息给大家。今天大家投票小心一点。"})
    t.talk(1, {})
    vote1 = ballots(t.alive, {**{w: exiled for w in wolves}, y: exiled, exiled: y})
    t.day_vote(1, vote1, exile=exiled, last="我是好人，没有身份可报。你们自己多留心。")
    t.night(2, guard=y if n2 == "death" else L.witch, wolf=L.witch, seer=y if y != L.seer else None, witch="pass")
    t.dawn(2)
    claim = f"昨天 {dead} 投了 {exiled}"
    t.talk(2, {**_by(bystander),
        x: _s(extra_x, style_x, blunt=f"{claim}，{y} 也投了 {exiled}。一个票型。{y} 解释。",
                  plain=f"昨天的票我记得：{dead} 投了 {exiled}，{y} 也投了 {exiled}，一个票型。{y} 得解释一下，今天听他怎么说。",
                  fluent=f"我想把昨天的投票情况理一理，因为这关系到今天该投谁。昨天的放逐票里，我记得很清楚：{claim}，{y} 也投了 {exiled}，这两个人的票型是一致的。"
                         f"一个人在信息不足时跟着别人投，这不一定是坏事，但有一个问题需要说明：{y} 为什么跟 {dead} 站在同一边？我觉得 {y} 需要解释，今天请大家重点听他的发言，再决定要不要把票投给他。"),
        y: _s(extra_y, style_y, blunt=f"我昨天投 {exiled}，理由说过了。别拿我的票绑别人。",
                  plain=f"我昨天投 {exiled}，理由我当时说了：他发言没东西。今天拿我的票跟别人放一起说事，我不接受。",
                  fluent=f"我来回应一下被点到的这件事。昨天我投的是 {exiled}，理由我当时就讲过：他的发言里没有任何可以核对的内容，我判断他在划水。这是我自己的判断。"
                         "今天有人把我的票和别人的票放在一起说事，我不认为这有什么问题，请大家根据我昨天的发言和今天的表现来评价，而不是根据票型里我站在谁旁边。"),
    })
    helpers = pick(L, 1, exclude=[L.bot, *t.out, y])
    t.decision_vote(2, ballots(t.alive, {**{w: y for w in wolves if w in t.alive}, **{h: x for h in helpers}}))
    return Built(
        t, f"{x} 说 {dead} 昨天投了 {exiled}；但 {dead} 在第 1 天公布死讯之后才投票，已经出局，昨天的票型里没有他。",
        [f"【出局公告】{dead}", f"{dead} 投了 {exiled}", vote_summary(vote1).split("，")[0]], [x],
        trap=f"{x} 的指控看上去有票型做依据，而且 {y} 确实投了 {exiled}；{y} 的辩解简短而不接茬。", kind="dead_voter", x=x, y=y, roles={x: "werewolf", y: "villager"},
    )


# ---------------------------------------------------------------------------------------------------------------------- F6
def f6_declared_vs_ballot(L: Layout, *, x: str, y: str, dead: str, exiled: str, style_x: str = PLAIN, style_y: str = PLAIN, extra_x: str = "", extra_y: str = "", seed: int, bystander: tuple[str, str] | None = None) -> Built:
    """Day 1: ``x`` declares "我投 exiled" with the template (a claim in the hard-fact table) and abstains.  Day 2: he pushes ``y``.  ``x`` speaks first."""
    _require_before(x, y, "他先带票，被点名的人后辩解")
    t = Table(L.role_list, bot=L.bot, seed=seed)
    t.styled_seats = {x, y}
    wolves = wolves_of(L)
    t.open()
    t.night(1, guard=L.guard, wolf=dead, seer=exiled, witch="pass")
    expect_dead(t, dead)
    t.election(candidates=[])
    t.dawn(1)
    t.last_words(1, {dead: "我是平民，没有信息给大家。别乱投。"})
    t.talk(1, {x: vote_claim(exiled, "他发言没东西")})
    helpers = pick(L, 2, exclude=[dead, exiled, L.bot, y])
    others = [w for w in wolves if w != x]
    t.day_vote(1, ballots(t.alive, {**{w: exiled for w in others}, **{h: exiled for h in helpers}, exiled: y}), exile=exiled, last="我是好人，没有身份可报。你们自己多留心。")
    assert x in t.alive
    t.night(2, guard=y, wolf=L.witch, seer=y if y != L.seer else None, witch="pass")
    t.dawn(2)
    t.talk(2, {**_by(bystander),
        x: _s(extra_x, style_x, blunt=f"今天投 {y}。昨天他没信息，只会跟票。",
                  plain=f"今天投 {y}。他昨天发言没信息，投票也只是跟多数，像在躲。{y} 给个解释，不然就出他。",
                  fluent=f"昨天的放逐我们出了 {exiled}，今天要接着找狼。我的判断是 {y} 的嫌疑最大：他昨天的发言没有任何可以核对的信息，投票时又一直跟着多数走，"
                         f"这种“只跟不带”的打法，是狼人最常用的保护色。所以我的建议是今天把票集中到 {y}：如果他是好人，他有机会在发言里给出让大家信服的理由；如果给不出，就请大家相信这个判断。"),
        y: _s(extra_y, style_y, blunt="我是平民。昨天没信息就是没信息。票我自己投的。",
                  plain="我是好人，昨天没信息就是没信息，票是我自己的判断。不带节奏不是理由，大家自己回看票型。",
                  fluent="被点名我回应一下。昨天我发言里确实没有给出新的信息，因为那时手里没有；投票我是按我的判断投的，不是跟谁。今天要我自证，我只能说：我是好人，没有技能，"
                         "所有判断都在公开的发言和票型里，请大家自己回看。我不认为“不带节奏”能成为被放逐的理由。"),
    })
    helpers2 = pick(L, 1, exclude=[L.bot, *t.out, y])
    t.decision_vote(2, ballots(t.alive, {**{w: y for w in wolves if w in t.alive}, **{h: x for h in helpers2}}))
    return Built(
        t, f"{x} 用模板声明“我投 {exiled}”，但公布的票型里 {x} 是弃票——他对公众说的投票和他实际的投票相反。",
        [f"我投 {exiled}", f"{x} 弃票"], [x], trap=f"{y} 昨天的发言确实没有信息，投票也跟了多数；{x} 今天的指控像是有道理的。", kind="declared_vs_ballot", x=x, y=y, roles={x: "werewolf", y: "villager"},
        notes="三条里最软的一条：它是行为上的矛盾，不是逻辑上的不可能。",
    )


# ---------------------------------------------------------------------------------------------------------------------- F9
def f9_changed_story(L: Layout, *, x: str, dead: str, first: str, second: str, safe: str, style_x: str = PLAIN, style_y: str = PLAIN, extra_x: str = "", extra_y: str = "", seed: int, bystander: tuple[str, str] | None = None) -> Built:
    """The fake seer ``x`` names ``first`` as his night-1 check on day 1 and ``second`` on day 2.  ``x`` speaks before the seer on both days."""
    y = L.seer
    _require_before(x, y, "他先报")
    t = Table(L.role_list, bot=L.bot, seed=seed)
    t.styled_seats = {x, y}
    wolves = wolves_of(L)
    t.open()
    t.night(1, guard=L.guard, wolf=dead, seer=safe, witch="pass")
    expect_dead(t, dead)
    t.election(candidates=[])
    t.dawn(1)
    t.last_words(1, {dead: "我是平民，没有信息给大家。今天小心投票。"})
    claim1 = f"第 1 夜验了 {first}，好人阵营"
    t.talk(1, {
        x: _s(extra_x, style_x, blunt=f"我是预言家。{claim1}。没查杀。", plain=f"我是预言家。{claim1}。没有查杀，今天先听大家的。",
                  fluent=f"我先亮明身份：我是预言家。我的第 1 夜验人记录是：{claim1}。我没有查杀，也不想为了制造声势现编一个。今天先听一轮，信息多起来再谈放逐。"),
        y: _s(extra_y, style_y, blunt=f"我才是预言家。第 1 夜验了 {safe}，好人阵营。没查杀。", plain=f"我也是预言家。第 1 夜验了 {safe}，好人阵营。没有查杀，大家对照两边的说法。",
                  fluent=f"{x} 对跳，我把我的记录完整报一遍：第 1 夜验了 {safe}，好人阵营。没有查杀。两个预言家的说法各记一笔，后面的信息会说明谁在说真话，请大家先别急着下结论。"),
    })
    t.day_vote(1, ballots(t.alive, {}), exile=None)
    t.night(2, guard=safe, wolf=safe, seer=x, witch="pass")
    t.dawn(2)
    claim2 = f"第 1 夜验了 {second}，好人阵营"
    t.talk(2, {**_by(bystander),
        x: _s(extra_x, style_x, blunt=f"我是预言家。{claim2}，昨晚验了 {safe}。对面在跟着我补信息。",
                  plain=f"我是预言家，我再报一遍：{claim2}。昨晚又验了 {safe}。今天请大家投对面那个。",
                  fluent=f"我再把我的记录完整说一遍，免得有人说我含糊：我是预言家，{claim2}，第 2 夜验了 {safe}。对面的说法我已经听过了，我的判断是他在跟着我的节奏补信息，"
                         "请大家把两天的发言对照着看，谁的说法前后一致，一目了然。"),
        y: _s(extra_y, style_y, blunt=f"我是预言家。第 1 夜验了 {safe}，好人阵营。第 2 夜验了 {x}，没有变化。",
                  plain=f"我还是那句话：我是预言家，第 1 夜验了 {safe}，好人阵营。昨天和今天我的说法没有变化，请大家对照。",
                  fluent=f"我的记录没有变化，我再复述一遍，方便大家对照：我是预言家，第 1 夜验了 {safe}，好人阵营，昨天说的就是这个，今天说的还是这个。"
                         "两个预言家的说法请大家各自记录，前后是否一致，是判断谁真谁假最朴素的办法。"),
    })
    helpers = pick(L, 1, exclude=[L.bot, *t.out, safe])
    t.decision_vote(2, ballots(t.alive, {**{w: y for w in wolves if w in t.alive}, **{h: x for h in helpers}}))
    return Built(
        t, f"{x} 第 1 天说第 1 夜验的是 {first}，第 2 天说第 1 夜验的是 {second}——同一个晚上的查验，说了两个不同的目标。", [claim1, claim2], [x],
        trap=f"{x} 的两次发言都很自信；{y} 的发言每天都一样，显得平淡。", kind="changed_story", x=x, y=y, roles={x: "werewolf", y: "seer"},
    )


# ---------------------------------------------------------------------------------------------------------------------- private cases
def p1_offside_slip(L: Layout, *, x: str, victim: str, y: str, seed: int) -> Built:
    """The bot is the witch.  Night 1 she is told ``victim`` is attacked and does not save.  In the election, BEFORE the death is announced, the candidate
    ``x`` (a wolf) speaks of ``victim`` as gone."""
    if L.bot != L.witch or x not in L.wolves:
        raise AuthoringError("被测座位要是女巫，x 要是狼人。")
    t = Table(L.role_list, bot=L.bot, seed=seed)
    wolves = wolves_of(L)
    t.open()
    t.night(1, guard=L.guard, wolf=victim, seer=y, witch="pass")
    slip = f"昨晚没了的人我们都该记着，比如 {victim}"
    speeches = {
        x: f"我是 {x}，上警是想把这一局带好。{slip}，他其实什么都没做错，我们得对得起他。警徽给我，我会把每一轮的票型都讲清楚。",
        y: f"我是 {y}，我是平民，上警想帮大家记发言和归票。没有别的，投谁都行。",
    }
    below = [s for s in t.alive if s not in (x, y)]
    t.election(candidates=[x, y], turns=speeches, ballots={s: (x if s in wolves else y) for s in below})
    t.dawn(1)
    t.last_words(1, {victim: "我是平民，没有信息给大家。小心投票，别被带节奏。"})
    t.talk(1, {}, direction="警左")
    t.decision_vote(1, ballots(t.alive, {**{w: y for w in wolves if w in t.alive}}))
    return Built(
        t, f"{x} 在公布死讯之前就说“昨晚没了的人……比如 {victim}”——死讯是在他发言之后才公布的。只有狼人（刀口）和女巫（被告知刀口）能在公布前知道；你就是女巫，没有告诉过任何人。",
        [slip, f"【出局公告】{victim}", f"今晚被狼人袭击的是 {victim}"], [x], trap=f"{x} 的演说有担当、有情感，像个好警长；{y} 的话平平。", kind="offside_slip", x=x, y=y, roles={x: "werewolf", y: "villager"},
    )


def p2_seer_private(L: Layout, *, x: str, dead: str, target: str, seed: int) -> Built:
    """The bot is the seer: it checked ``target`` (a good player).  The fake seer ``x`` (a wolf) reports ``target`` as a wolf.  The bot passed its turn."""
    if L.bot != L.seer or x not in L.wolves:
        raise AuthoringError("被测座位要是预言家，x 要是狼人。")
    t = Table(L.role_list, bot=L.bot, seed=seed)
    wolves = wolves_of(L)
    _require_before(x, L.bot, "他先跳，被测座位的发言权在他之后")
    t.open()
    told = t.night(1, guard=L.guard, wolf=dead, seer=target, witch="pass")
    t.election(candidates=[])
    t.dawn(1)
    t.last_words(1, {dead: "我是平民，没有信息给大家。今天别乱投。"})
    claim = f"第 1 夜验了 {target}，狼人阵营"
    t.talk(1, {
        x: f"我是预言家。{claim}，这是查杀。今天出 {target}，大家别犹豫。",
        L.bot: None,
    })
    helpers = pick(L, 1, exclude=[dead, L.bot, target])
    t.decision_vote(1, ballots(t.alive, {**{w: target for w in wolves if w in t.alive}, **{h: target for h in helpers}}))
    return Built(
        t, f"你自己第 1 夜验了 {target}，主持人私聊告诉你他是好人阵营；{x} 却报告 {target} 是狼人阵营——两个人里必有一个在撒谎，而你知道自己没有。",
        [f"【查验结果】{target}", claim], [x], trap=f"{x} 说得斩钉截铁并且已经有人跟票；你选择了沉默（没有发言）。", kind="seer_private", x=x, y=L.bot, roles={x: "werewolf", target: "good"},
    )


def p3_same_guard_same_save(L: Layout, *, x: str, target: str, seed: int) -> Built:
    """The bot is the guard.  Night 1 it guards ``target``; the wolves attack ``target``; the witch does not save.  Peaceful night.  ``x`` (a wolf) claims
    to be the witch and to have saved ``target``."""
    if L.bot != L.guard or x not in L.wolves:
        raise AuthoringError("被测座位要是守卫，x 要是狼人。")
    t = Table(L.role_list, bot=L.bot, seed=seed)
    wolves = wolves_of(L)
    t.open()
    t.night(1, guard=target, wolf=target, seer=pick(L, 1, exclude=[target, L.bot])[0], witch="pass")
    t.election(candidates=[])
    dead = t.dawn(1)
    assert not dead
    claim = f"我是女巫，昨晚我用解药救了 {target}"
    t.talk(1, {x: f"{claim}。所以昨夜才是平安夜，这是我的功劳。大家放心，解药我已经用了，毒药还在，今天我听大家的，谁有问题我晚上处理。"})
    helpers = pick(L, 1, exclude=[L.bot, target])
    t.decision_vote(1, ballots(t.alive, {**{w: helpers[0] for w in wolves}}))
    return Built(
        t, f"你（守卫）昨晚守护了 {target}，昨夜平安；{x} 自称女巫、说自己用解药救了 {target}。守护加解药 = “同守同救”，{target} 会出局；可昨夜平安——所以他没有救。",
        [claim, "【昨夜平安】"], [x], trap=f"{x} 的说法听上去很合理，还把平安夜记成自己的功劳；真正的女巫 {L.witch} 一言不发。", kind="same_guard_same_save", x=x, y=L.witch, roles={x: "werewolf", L.witch: "witch"},
    )


__all__ = [
    "BLUNT", "Built", "FLUENT", "PLAIN", "ballots", "f1_impossible_check", "f2_gold_water_exploded", "f3_false_hunter", "f4_checked_hunter", "f5_dead_voter",
    "f6_declared_vs_ballot", "f9_changed_story", "p1_offside_slip", "p2_seer_private", "p3_same_guard_same_save", "pick", "wolves_of",
]
