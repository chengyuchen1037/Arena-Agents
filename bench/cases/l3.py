"""Suite L3 (核心矛盾): nine situations, each with ONE decisive fact buried in an ordinary-looking game, and a vote the bot must cast.

What is measured (plan §6.5 L3): does the bot find the fact that decides the question - an impossible claim, a claim that a later public fact refutes, something
only a wolf could know - instead of voting on how the speeches sound?  The score is objective (the vote is right or wrong: ``truth.answer``); the judge then
reads the bot's thinking and says what the decision rested on (rubric ``L3``).  Every speech here is plain in style (a few sentences): style is the business
of L1.  The decisive fact is *derivable from what the bot sees* - a test looks for every string of ``truth.evidence`` in its board and history.
"""

from __future__ import annotations

from typing import Any

from bench.authoring import dump
from bench.cases import families as F
from bench.cases.common import Case, make_header
from bench.cases.layouts import LAYOUTS as LAY

SUITE = "L3"


def _case(case_id: str, title: str, tests: str, build: Any) -> Case:
    def make() -> tuple[str, dict[str, Any]]:
        built: F.Built = build()
        t = built.table
        truth: dict[str, Any] = {
            "request": "@vote", "answer": built.answer, "kind": built.kind, "decisive": built.decisive, "evidence": built.evidence, "trap": built.trap, "roles": built.roles,
        }
        if built.notes:
            truth["notes"] = built.notes
        data = t.situation(
            sid=case_id, title=title, tags=[SUITE, built.kind], decision={"seat": t.bot, "expect": ["respond"]}, truth=truth,
            brief=f"{title}。被测座位是 {t.bot}（{t.role_of(t.bot)}）。{built.decisive}", judge=["L3"], meta={"family": built.kind},
        )
        return make_header(SUITE, title, tests, built), data

    return Case(case_id, SUITE, f"situations/{case_id}.yaml", make)


def cases() -> list[Case]:
    return [
        _case("l3-impossible-check", "两个预言家，一个验了已经出局的人",
              "能不能发现一个“查验记录”在规则上不可能：第 2 夜的查验名单里没有第 1 夜已经出局的人。",
              lambda: F.f1_impossible_check(LAY["v1"], x="N2", dead="N5", exiled="N1", safe="N6", seed=3101)),
        _case("l3-gold-water-exploded", "金水自爆",
              "能不能把“某人报的金水”和后来的公开事实（白狼王自爆亮明身份）对上：真预言家不会把狼人阵营的人报成好人。",
              lambda: F.f2_gold_water_exploded(LAY["v4"], x="N5", dead="N12", taken="N4", safe="N7", seed=3102)),
        _case("l3-false-hunter", "竞选演说里的猎人",
              "能不能把竞选演说里的身份声明和之后的公告对上：场上只有一个猎人，他夜里被刀、开枪时公告写明了。",
              lambda: F.f3_false_hunter(LAY["v5"], x="N4", y="N6", shot="N8", seed=3103)),
        _case("l3-checked-hunter", "被查杀的猎人",
              "能不能把“查杀”和之后的公开事实对上：被放逐后开枪的人是猎人，预言家不会把猎人查成狼人阵营。",
              lambda: F.f4_checked_hunter(LAY["v6"], x="N5", dead="N12", shot="N3", safe="N6", seed=3104)),
        _case("l3-dead-voter", "一个死人的票",
              "能不能发现一句关于投票的指控与公告矛盾：被指控“投了票”的人那时已经出局，票型里没有他。",
              lambda: F.f5_dead_voter(LAY["v3"], x="N2", y="N7", dead="N1", exiled="N11", seed=3105)),
        _case("l3-declared-vs-ballot", "说投的和投的不一样",
              "能不能把一条用模板声明的投票意向（硬事实台里的“声明”）和公布的票型对上：他说投 A，票型里他弃票。这条最软：它是行为上的矛盾，不是逻辑上的不可能。",
              lambda: F.f6_declared_vs_ballot(LAY["v8"], x="N4", y="N9", dead="N2", exiled="N3", seed=3106)),
        _case("l3-offside-slip-witch", "公布之前就知道的死讯（女巫视角）",
              "被测座位是女巫：能不能发现“死讯公布之前，有人已经把昨晚的死者说成‘没了’”——只有狼人和女巫能在公布前知道刀口，而女巫就是自己。",
              lambda: F.p1_offside_slip(LAY["witch"], x="N2", victim="N5", y="N6", seed=3107)),
        _case("l3-seer-private", "和自己的查验结果相反的查杀（预言家视角）",
              "被测座位是预言家：自己验过的人，别人报了相反的结果——能不能用自己手里的私下信息判断谁在撒谎，而不是被“已经有人跟票”带着走。",
              lambda: F.p2_seer_private(LAY["seer"], x="N2", dead="N5", target="N8", seed=3108)),
        _case("l3-same-guard-same-save", "同守同救（守卫视角）",
              "被测座位是守卫：自己守了 A，昨夜平安；有人自称女巫、说用解药救了 A——规则上“同守同救”A 会出局，所以他在撒谎。考规则（M3）和事实（L3）合用。",
              lambda: F.p3_same_guard_same_save(LAY["guard"], x="N6", target="N5", seed=3109)),
    ]


__all__ = ["cases", "dump"]
