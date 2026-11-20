"""Suite L1 (表达不变性): seven *pairs* of situations.  The decisive fact is the same in A and B - the same board, the same hard-fact table, the same votes - and
only the way two players talk is swapped.

In A the wolf speaks at length and in an organised way (first, second, so) and the good player in a few blunt words; in B the wolf is the blunt one and the
good player the fluent one.  A bot that follows the facts votes for the same player in both; a bot that follows how well somebody talks votes for the
fluent one in both, and is right in only one of them.  ``expression bias = P(votes for the fluent side) - 0.5`` (gate L1: at most half of the baseline).

Each pair is one ``fixtures/pairs/<id>.yaml`` pointing at ``situations/<id>-a.yaml`` and ``-b.yaml``; the two situation files are generated from the same
code with the same seed, so everything but the styled speeches (marked ``meta: {styled: true}``) is identical - a test checks that.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bench.cases import families as F
from bench.cases.common import BLUNT, FLUENT, Case, make_header
from bench.cases.layouts import LAYOUTS as LAY

SUITE = "L1"
STYLE = {"A": (FLUENT, BLUNT), "B": (BLUNT, FLUENT)}  # (the wolf's style, the good player's style)


def _pair(pair_id: str, title: str, tests: str, build: Callable[..., F.Built], **params: Any) -> list[Case]:
    cases: list[Case] = []

    def variant(letter: str) -> Callable[[], tuple[str, dict[str, Any]]]:
        def make() -> tuple[str, dict[str, Any]]:
            style_x, style_y = STYLE[letter]
            built = build(style_x=style_x, style_y=style_y, **params)
            x, y = built.x, built.y
            fluent, blunt = (x, y) if letter == "A" else (y, x)
            t = built.table
            truth: dict[str, Any] = {
                "request": "@vote", "answer": built.answer, "kind": built.kind, "decisive": built.decisive, "evidence": built.evidence, "trap": built.trap,
                "sides": {"fluent": fluent, "blunt": blunt}, "roles": built.roles,
            }
            sid = f"{pair_id}-{letter.lower()}"
            data = t.situation(
                sid=sid, title=f"{title}（{letter}）", tags=[SUITE, built.kind, f"pair:{pair_id}", letter], decision={"seat": t.bot, "expect": ["respond"]}, truth=truth,
                brief=f"{title}。{built.decisive} 这一版里 {fluent} 说得长而有条理，{blunt} 说得短而直。",
                judge={"L1": {"fluent_side": [fluent], "blunt_side": [blunt]}}, meta={"pair": pair_id, "variant": letter},
            )
            extra = [f"这是成对题 {pair_id} 的 {letter} 版：{x}（狼）{'说得长而有条理' if letter == 'A' else '说得短而直'}，{y}（好人）{'说得短而直' if letter == 'A' else '说得长而有条理'}。另一版把两边对调，事实完全相同。"]
            return make_header(SUITE, f"{title}（{letter}）", tests, built, extra=extra), data

        return make

    for letter in ("A", "B"):
        cases.append(Case(f"{pair_id}-{letter.lower()}", SUITE, f"situations/{pair_id}-{letter.lower()}.yaml", variant(letter), pair=pair_id))
    return cases


def cases() -> list[Case]:
    out: list[Case] = []
    out += _pair("l1-impossible-check", "两个预言家，一个验了已经出局的人", "同一个决定性事实（预言家的第 2 夜查验名单里不可能有第 1 夜出局的人），说得好听的是狼，还是说得简短的是狼。",
                 F.f1_impossible_check, L=LAY["v2"], x="N1", dead="N5", exiled="N3", safe="N6", seed=3201)
    out += _pair("l1-gold-water-exploded", "金水自爆", "同一个决定性事实（被报成金水的人自爆亮明白狼王），报金水的人说得好听还是简短。",
                 F.f2_gold_water_exploded, L=LAY["v3"], x="N2", dead="N11", taken="N7", safe="N1", seed=3202)
    out += _pair("l1-false-hunter", "竞选演说里的猎人", "同一个决定性事实（竞选演说里自称猎人的人，不是夜里开枪的那个猎人），演说有担当的是狼还是平民。",
                 F.f3_false_hunter, L=LAY["v7"], x="N5", y="N4", shot="N2", seed=3203)
    out += _pair("l1-checked-hunter", "被查杀的猎人", "同一个决定性事实（被查杀放逐的人开枪时是猎人），斩钉截铁的查杀者是狼还是真预言家。",
                 F.f4_checked_hunter, L=LAY["v1"], x="N2", dead="N5", shot="N10", safe="N6", seed=3204)
    out += _pair("l1-dead-voter", "一个死人的票", "同一个决定性事实（被指“投了票”的人那时已经出局），有票型当依据的指控者是狼还是被指控的好人说得更好。",
                 F.f5_dead_voter, L=LAY["v5"], x="N1", y="N6", dead="N11", exiled="N3", seed=3205)
    out += _pair("l1-declared-vs-ballot", "说投的和投的不一样", "同一个决定性事实（模板声明的投票意向和公布的票型相反），带票的人是狼还是被点名的人更会说。",
                 F.f6_declared_vs_ballot, L=LAY["v6"], x="N3", y="N6", dead="N2", exiled="N12", seed=3206)
    out += _pair("l1-changed-story", "同一夜两个说法", "同一个决定性事实（同一晚的查验，两天里说了两个不同的目标），说法多变的人是狼，还是每天说一样的话的人说得更平淡。",
                 F.f9_changed_story, L=LAY["v8"], x="N4", dead="N3", first="N1", second="N2", safe="N9", seed=3207)
    return out


__all__ = ["STYLE", "cases"]
