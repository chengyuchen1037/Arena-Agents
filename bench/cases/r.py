"""Suite R (运行健康): five speaking turns whose floor allows only 60–120 characters.

The turns are those of S2 (a moment in a real game when the seat under test must say something) with one thing changed: the floor's limit on a message is far below
what a bot writes by default (``max_chars_per_message``: 60, 80, 100, 120; the usual 300).  A bot that first writes too long a speech has it refused (``TOO_LONG``) and
must say it again, shorter, within the turn - and what the benchmark counts is whether it does: ``runtime.speak.rewrite_success_rate`` (gate R2: at least 90% of the
refused speeches are followed by one that went through), ``form.truncated_rate`` (gate R1: no speech of a bot is ever cut by the engine: always 0, because the engine
refuses instead) and ``runtime.floor_timeout_rate`` (R3).  The world stands still while a bot thinks, so a floor never times out *here*: the R3 number of a real game comes
from ``python -m bench audit`` over its saved ledger and trace.

The limit is announced in the bot's own turn ("≤ 60 字"), so a bot that reads its turn writes short; one that writes its habitual 150 characters is refused once, and that is
what is being measured.
"""

from __future__ import annotations

from typing import Any

from bench.cases.common import Case
from bench.cases.s2 import SCENES, header

SUITE = "R"

#: (scene of S2, characters allowed in one message)
TURNS = (
    ("day1-first-words", 60),
    ("day1-after-death", 80),
    ("accused", 100),
    ("seer-standoff", 60),
    ("campaign", 120),
)


def build(key: str, chars: int) -> tuple[str, dict[str, Any]]:
    t, brief, _tests, _trap = SCENES[key]({"limits": {"max_chars_per_message": chars}})
    tests = f"运行健康：发言权只允许每条 {chars} 字（平时 300）。bot 写长了会被退回（TOO_LONG），要在同一个发言权里重写得更短；看它能不能重写成功、会不会被截断。"
    trap = f"bot 习惯的篇幅远超 {chars} 字：第一稿几乎一定被退回。真正的考点是退回之后——是照着限额重写，还是反复写长、或干脆放弃。"
    brief = f"{brief} 这次发言权每条最多 {chars} 字。"
    head = header("R", f"{key}（每条最多 {chars} 字）", t, brief, tests, trap, extra=["指标：runtime.speak.rewrite_success_rate（R2）、form.truncated_rate（R1）；整局的发言超时率（R3）要用 audit 看真实对局。"])
    data = t.situation(
        sid=f"r-{chars}-{key}", title=f"运行健康：{key}（≤ {chars} 字）", tags=[SUITE, "speech", "tight"], decision={"seat": t.bot, "expect": ["speak"]}, brief=brief,
        meta={"moment": key, "max_chars": chars},
    )
    return head, data


def cases() -> list[Case]:
    return [Case(f"r-{chars}-{key}", SUITE, f"situations/r-{chars}-{key}.yaml", (lambda k=key, n=chars: build(k, n))) for key, chars in TURNS]


__all__ = ["TURNS", "build", "cases"]
