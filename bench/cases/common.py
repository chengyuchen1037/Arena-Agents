"""Shared pieces of the situation authors: the registry, the stock of neutral speeches, the seat layouts of a table, and how speeches take a style.

A ``Case`` is one file of the question bank: ``build()`` returns ``(header, data)`` - the comment that explains what the question tests and where its trap
is, and the dict of the situation (``bench.authoring.Table.situation``) - and ``path`` says where the file goes below ``bench/fixtures``.
``python -m bench fixtures`` writes them all (or checks that the committed files are what the code says).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from bench.authoring import SEATS, AuthoringError, seat_number

#: Speeches of players with nothing to say: no accusation, no claim, no advice.  The words of a player who does not know anything.
NEUTRAL = (
    "我这轮没有什么特别的信息，先听听大家的。",
    "目前没抓到明显的毛病，先观望，投票前再说。",
    "前面的发言我都听了，暂时没有倾向，等后面的人说完。",
    "我是平民，没有技能，只能靠听发言。这轮先不站边。",
    "信息不多，我先不乱表态，投票的时候再认真选。",
    "我先过，等有更多信息再说。",
    "没什么要补充的，听完再投。",
    "我这边没有额外的信息，大家继续。",
    "两边的话我都记下了，现在还下不了判断。",
    "我这轮就听，不带节奏。",
    "暂时没有怀疑对象，我再听一轮。",
    "我是好人，没有信息可报，等投票。",
    "我先不说怀疑谁，怕说错了带偏大家。",
    "这轮我保持观望，等明确的信息出来再动。",
    "我没有信息，只想多听几句再决定票型。",
    "发言我听完了，没有特别想说的。",
    "我先记着，晚点再说我的想法。",
    "目前没有谁让我特别不放心，我再看看。",
    "我不确定，先不表态，投票时我会认真看。",
    "我这边平平安安，没有消息可给，听大家的。",
    "先让信息多的人说，我跟着听。",
    "我这轮没有想法，不浪费大家时间。",
    "还没想清楚，我先听，等下再看。",
    "我只是个普通玩家，信息有限，先听着。",
)

#: Longer speeches with no information in them (about a hundred characters): the bulk of a long game's history (suite M2).
FILLER = (
    "我把前面几位的发言都过了一遍，目前大家说的都是各自的判断，没有谁给出能被核对的东西。我这轮不想硬站边，先把几个人的说法记下来，等后面有了新信息再对照。",
    "说实话今天的局面信息很少，我手里也没有任何技能带来的情报。我的做法是先听完所有人，再决定投谁，不想因为一两句话就把票押出去。",
    "我注意到有人的发言很长但没有新内容，也有人发言很短但立场很清楚。我现在不下结论，只是提醒大家：别被说得好不好听带着走，看后面的行动。",
    "这一轮我想听听每个人对昨晚结果的看法，因为那是目前唯一确定的事实。我自己没有怀疑对象，等发言轮完我再认真选一个，不会随便弃票。",
    "我是普通村民，没有夜间信息。我的原则是：谁说的话前后对得上我就多信一点，谁的说法忽左忽右我就多留意一点。现在还没看到明显的矛盾。",
    "前面有人提到要集中票型，我觉得可以，但前提是有一个站得住的理由。目前我没听到足够的理由，所以我这轮先不跟，投票时再看大家的倾向。",
    "我把今天要关注的点列了一下：谁的发言和昨天不一致、谁的投票和表态不一致、谁在回避问题。眼下三项里我都还没有明确的人选，继续听。",
    "我想提醒大家，白天的信息大多是别人说出来的，真假自负，只有公告和投票结果才是确定的。我会优先用确定的东西做判断，说法只作参考。",
    "我这边没有特别想说的，要说就是希望大家发言时尽量具体一点：哪一句话有问题、为什么有问题。泛泛地说‘像狼’，对我没有帮助。",
    "对昨天的投票结果我有点想法，但还不成熟，先不说。我更想看今天每个人怎么解释自己的票，解释得清楚的我会更放心一些。",
    "目前我的倾向偏中立，没有想重点怀疑的人。我会在投票前再看一遍所有发言，如果有人的说法在这一轮出现了变化，我会把票投给那个人。",
    "从规则上讲，今天出局的人不会翻牌，所以我们能依靠的只有发言和投票。我准备把每个人的发言简单记一笔，晚上再对照，希望大家发言也尽量留下可核对的细节。",
    "我不太擅长带节奏，只能用笨办法：把每个人说过的重点列出来，看谁前后不一致。这一轮我先听，等所有人说完我再动笔。",
    "我对今天的局面有个朴素的想法：信息少的时候不要冒险出人，宁可多留一轮观察。如果大家都同意，我这轮可以弃票，除非有人拿出确凿的东西。",
    "我看到几个人在互相确认彼此是好人，这当然可以，但确认需要依据。我暂时没有依据，所以不跟着背书，也不反对，先放着。",
    "我这轮的重点是听预言家和其他神职有没有要补充的，因为他们手里的信息最多。如果没有人站出来，我就按自己的判断投票。",
    "昨晚的结果我已经看到了，说明夜里确实有人行动。我自己没有任何夜间信息，所以只能从白天的发言里找线索。目前没有明显的线索，继续听。",
    "我想先把话说在前面：我今天的投票会基于确定的信息，而不是某个人的气势。谁的话听上去有道理，我会拿后面的事实去验证它。",
    "刚才有人说得很满，但没有给出任何细节，这让我有点在意。不过也可能只是他表达的方式如此，我先记下，等更多信息再判断。",
    "我的发言不长：目前没有重点怀疑的对象，也没有想保的人。投票时我会看谁的票型前后一致，谁的发言前后一致，按这两条来选。",
    "今天的节奏有点慢，大家都在观望，这对狼人有利，所以我觉得至少要有人把自己的怀疑说出来。我的怀疑还不成形，说不出来，只能请有想法的人先说。",
    "我复盘了一下昨天的票型，没有发现特别反常的地方。今天我会继续留意有没有人票型和说法对不上，这是我目前能用的唯一方法。",
    "我不想空谈，简单说三句：第一，我没有信息；第二，我不站边；第三，投票前我会再认真听一遍。大家继续。",
    "我对大家说的话都在听，也都在记，但眼下的判断只能是暂时的。请大家发言时不要只讲结论，把依据也说出来，我们才好比较。",
)


def neutral_line(rng: Any, used: set[str] | None = None) -> str:
    """One neutral speech, not one that was just used (so a day does not read like a copy)."""
    pool = [line for line in NEUTRAL if not used or line not in used]
    line = rng.choice(pool or list(NEUTRAL))
    if used is not None:
        used.add(line)
    return line


# ---------------------------------------------------------------------------------------------------------------------- layouts
@dataclass(frozen=True)
class Layout:
    """Who sits where: a permutation of the pool of ``werewolf_12p`` (the seat under test is ``bot``; ``villagers`` includes it when it is one)."""

    bot: str
    seer: str
    witch: str
    hunter: str
    guard: str
    ww: str  # the white wolf
    wolves: tuple[str, str, str]
    villagers: tuple[str, str, str, str]

    @property
    def roles(self) -> dict[str, str]:
        mapping: dict[str, str] = {self.seer: "seer", self.witch: "witch", self.hunter: "hunter", self.guard: "guard", self.ww: "white_wolf"}
        mapping.update({w: "werewolf" for w in self.wolves})
        mapping.update({v: "villager" for v in self.villagers})
        if set(mapping) != set(SEATS):
            raise AuthoringError(f"座位排布不对：{sorted(mapping, key=seat_number)}")
        return mapping

    @property
    def role_list(self) -> list[str]:
        roles = self.roles
        return [roles[s] for s in SEATS]

    def good(self) -> list[str]:
        return [s for s in SEATS if self.roles[s] not in ("werewolf", "white_wolf")]


def layout(*, bot: int, seer: int, witch: int, hunter: int, guard: int, ww: int, wolves: Sequence[int]) -> Layout:
    """A layout from seat numbers; the villagers are the seats left over."""
    named = {f"N{n}" for n in (seer, witch, hunter, guard, ww, *wolves)}
    if len(named) != 8:
        raise AuthoringError("神职、白狼王和三个狼人要占 8 个不同的座位。")
    villagers = tuple(s for s in SEATS if s not in named)
    return Layout(f"N{bot}", f"N{seer}", f"N{witch}", f"N{hunter}", f"N{guard}", f"N{ww}", tuple(f"N{n}" for n in wolves), villagers)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------------------------------- styles
FLUENT, PLAIN, BLUNT = "fluent", "plain", "blunt"


def styled(style: str, *, fluent: str, plain: str, blunt: str) -> str:
    """One speech in the style asked for: ``fluent`` (long, organised, "first ... second ... so"), ``plain`` (a few sentences), ``blunt`` (a few words)."""
    return {FLUENT: fluent, PLAIN: plain, BLUNT: blunt}[style]


def make_header(suite: str, title: str, tests: str, built: Any, *, extra: Sequence[str] = ()) -> str:
    """The comment at the top of a generated situation file: what the question tests, where the trap is, what decides it."""
    table = built.table
    seat = table.bot
    lines = [
        f"{suite} · {title}",
        f"在测什么：{tests}",
        f"被测座位：{seat}（{table.role_of(seat)}）；12 人暗牌局（werewolf_12p 2.2.0），局面是用主持人手册的步骤在真实引擎上打出来的。",
        f"决定性事实：{built.decisive}",
        f"证据（被测 bot 在自己的状态板 / 可见历史 / 私聊里能读到）：{'；'.join(built.evidence)}",
        f"陷阱：{built.trap}" if built.trap else "",
        *extra,
        f"正确的投票：{'、'.join(built.answer)}（truth.answer）。",
        "本文件由 bench/cases 里的代码生成，不要手改：改代码后运行 `python -m bench fixtures` 重写。",
    ]
    return "\n".join(line for line in lines if line)


@dataclass(frozen=True)
class Case:
    """One file of the question bank."""

    id: str
    suite: str
    path: str  # below bench/fixtures: situations/l1-dead-target-a.yaml
    build: Callable[[], tuple[str, dict[str, Any]]]
    pair: str = ""  # the id of the pair it belongs to (L1)

    @property
    def filename(self) -> str:
        return self.path.rsplit("/", 1)[-1]


__all__ = ["BLUNT", "Case", "FILLER", "FLUENT", "Layout", "NEUTRAL", "PLAIN", "layout", "make_header", "neutral_line", "styled"]
