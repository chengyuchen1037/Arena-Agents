"""Suite L2 (琐碎陷阱): seven situations in which the two sides of a dispute differ in a lot of ways that do not matter - the first-night check target, the
order of the badge flow (警徽流), the wording, the length - and in ONE way that does: a fact the public record decides (the same decisive facts as L3).
Somebody with nothing at stake then points at the trivia ("你俩的警徽流顺序正好相反，这个差别值得问一句"): a red herring made out of what players really say.

What is measured (plan §6.5 L2): does the bot get entangled in the trivia?  The vote is scored objectively (``truth.answer``); the judge (rubric ``L2``) then
reads the bot's reasoning and says what the decision rested on - ``trivia_hit_rate`` is the share that rests on the trivia (gate: at most half of the baseline).
Both sides speak in the same plain style (style is the business of L1), so the trivia are the only thing that makes the two accounts *look* different.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bench.authoring import AuthoringError, by_seat, seat_number
from bench.cases import families as F
from bench.cases.common import Case, make_header
from bench.cases.layouts import LAYOUTS as LAY

SUITE = "L2"


def _flow_texts(x: str, y: str, a: str, b: str) -> tuple[str, str, str]:
    """(x's aside, y's aside, what a bystander says about them): the badge flows are the same two players in the opposite order."""
    return (
        f"我的警徽流是 {a}、{b}。",
        f"我的警徽流是 {b}、{a}，先看 {b}。",
        f"我注意到 {x} 和 {y} 的警徽流顺序正好相反，一个先 {a} 后 {b}，一个先 {b} 后 {a}；两个人首夜验的人也不一样。这个差别我觉得值得问一句，两位怎么解释？",
    )


def _wording_texts(x: str, y: str) -> tuple[str, str, str]:
    """(x's aside, y's aside, a bystander's remark): the two talk differently - short and to the point, and long and apologetic."""
    return (
        "我说话一向简短，有话一次说完。",
        "我说话啰嗦，习惯多解释两句，请大家见谅。",
        f"我觉得 {x} 说话特别简短，{y} 就啰嗦得多，两个人的说话方式差得太远了，不太好比较，这一点两位自己怎么看？",
    )


def _direction_texts(x: str, y: str) -> tuple[str, str, str]:
    """For an election: which way each would let the table speak if elected (警左 / 警右) - a habit that tells nothing."""
    return ("当选的话，我习惯警左发言。", "当选的话，我习惯警右发言。", "")


def _pick_bystander(built: F.Built, minimum: int) -> str:
    t = built.table
    pool = [s for s in by_seat(t.alive) if s not in (built.x, built.y, t.bot) and seat_number(s) > minimum and t.role_of(s) not in ("werewolf", "white_wolf")]
    if not pool:
        raise AuthoringError(f"没有可以当旁观者的好人（座位号要大于 {minimum}，还活着）。")
    return pool[0]


def _case(
    case_id: str, title: str, tests: str, fn: Callable[..., F.Built], layout: str, params: dict[str, Any], *, kind: str = "flow", bystander: bool = True,
) -> Case:
    def make() -> tuple[str, dict[str, Any]]:
        L = LAY[layout]
        dry = fn(L, **params)  # (a first pass tells who is still alive: the trivia must name players that are in the game when they are said)
        x, y = dry.x, dry.y
        pool = [s for s in by_seat(dry.table.alive) if s not in (x, y, dry.table.bot)]
        a, b = pool[0], pool[-1]
        if kind == "flow":
            ex, ey, remark = _flow_texts(x, y, a, b)
            trivia = [f"{x} 的警徽流是 {a}、{b}，{y} 的是 {b}、{a}（顺序相反）", "两人首夜验的人不同", "两人的用词和说话长短不同"]
        elif kind == "wording":
            ex, ey, remark = _wording_texts(x, y)
            trivia = [f"{x} 说话简短，{y} 说话啰嗦（长度和措辞不同）", "两人的用词不同"]
        else:
            ex, ey, remark = _direction_texts(x, y)
            trivia = [f"{x} 习惯警左发言，{y} 习惯警右发言", "两人演说的长短、用词不同"]
        extras: dict[str, Any] = {"extra_x": ex, "extra_y": ey}
        spoken_by = ""
        if bystander and remark:
            spoken_by = _pick_bystander(dry, max(seat_number(x), seat_number(y)))
            extras["bystander"] = (spoken_by, remark)
        if not bystander:
            extras = {"extra_x": ex, "extra_y": ey}
        built = fn(L, **params, **extras)
        t = built.table
        trap = (
            f"两边在无关紧要的地方不一样：{'；'.join(trivia)}。"
            + (f"旁观的 {spoken_by} 还在发言里特意点了这个差别，把它说成一个值得追问的疑点。" if spoken_by else "")
            + f"真正的差别只有一处：{built.decisive}"
        )
        truth: dict[str, Any] = {
            "request": "@vote", "answer": built.answer, "kind": built.kind, "decisive": built.decisive, "evidence": built.evidence, "trap": trap, "roles": built.roles,
        }
        if built.notes:
            truth["notes"] = built.notes
        data = t.situation(
            sid=case_id, title=title, tags=[SUITE, built.kind, "trivia"], decision={"seat": t.bot, "expect": ["respond"]}, truth=truth,
            brief=f"{title}。被测座位是 {t.bot}（{t.role_of(t.bot)}）。{built.decisive}", judge={"L2": {"trivia": trivia}},
            meta={"family": built.kind, "trivia": kind, **({"bystander": spoken_by} if spoken_by else {})},
        )
        extra_header = [f"琐碎差别（干扰项）：{'；'.join(trivia)}。"] + ([f"旁观者：{spoken_by} 在发言里点了这个差别（红鲱鱼）。"] if spoken_by else [])
        header = make_header(SUITE, title, tests, F.Built(t, built.decisive, built.evidence, built.answer, trap, built.kind, built.x, built.y, built.sides, built.roles, built.notes),
                             extra=extra_header)
        return header, data

    return Case(case_id, SUITE, f"situations/{case_id}.yaml", make)


def cases() -> list[Case]:
    return [
        _case("l2-impossible-check", "两个预言家：警徽流顺序相反，其中一个验了已经出局的人",
              "能不能不被“警徽流顺序相反、首夜验的人不同”带走，抓住真正能判定的那一处：一个人报的第 2 夜查验对象，第 1 夜就出局了。",
              F.f1_impossible_check, "v4", dict(x="N5", dead="N7", exiled="N4", safe="N6", seed=3401)),
        _case("l2-gold-water-exploded", "两个预言家：警徽流不同，其中一个报了金水，金水自爆了",
              "能不能不被警徽流先后的差别带走，抓住真正能判定的那一处：被报成好人的人自爆亮明了白狼王。",
              F.f2_gold_water_exploded, "v7", dict(x="N5", dead="N12", taken="N1", safe="N4", seed=3402), bystander=False),
        _case("l2-false-hunter", "警长竞选：发言习惯不同，其中一个自称猎人",
              "能不能不被“当选后习惯警左还是警右”这类无关的习惯带走，抓住真正能判定的那一处：自称猎人的人，不是夜里开枪的那个猎人。",
              F.f3_false_hunter, "v2", dict(x="N4", y="N10", shot="N8", seed=3403), kind="direction", bystander=False),
        _case("l2-checked-hunter", "两个预言家：警徽流不同，其中一个查杀了后来开枪的猎人",
              "能不能不被警徽流和首夜目标的差别带走，抓住真正能判定的那一处：被查杀放逐的人开枪时是猎人。",
              F.f4_checked_hunter, "v5", dict(x="N4", dead="N6", shot="N8", safe="N3", seed=3404)),
        _case("l2-dead-voter", "一句关于票型的指控：两个人一个简短一个啰嗦",
              "能不能不被“一个说话简短、一个啰嗦”这类表达上的差别带走，抓住真正能判定的那一处：被说成“投了票”的人那时已经出局。",
              F.f5_dead_voter, "v6", dict(x="N5", y="N6", dead="N2", exiled="N12", seed=3405), kind="wording"),
        _case("l2-declared-vs-ballot", "说投的和投的不一样：一个简短一个啰嗦",
              "能不能不被两个人说话长短、措辞的差别带走，抓住真正能判定的那一处：模板声明的投票意向和公布的票型相反。这条最软：它是行为上的矛盾，不是逻辑上的不可能。",
              F.f6_declared_vs_ballot, "v3", dict(x="N2", y="N7", dead="N1", exiled="N11", seed=3406), kind="wording"),
        _case("l2-changed-story", "两个预言家：警徽流不同，其中一个同一夜说了两个目标",
              "能不能不被警徽流先后的差别带走，抓住真正能判定的那一处：同一个晚上的查验，两天里说了两个不同的目标。",
              F.f9_changed_story, "v1", dict(x="N2", dead="N6", first="N12", second="N5", safe="N1", seed=3407)),
    ]


__all__ = ["cases"]
