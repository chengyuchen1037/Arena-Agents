"""Suite S2 (风格可辨识): nine speaking turns, each a moment in a real game when the seat under test must say something - and nothing in the situation says *what*.

The twelve default bots each take the same seat in the same situation; a judge (rubric ``S2``) is given one speech and the twelve style cards (three lines of each bot's
``voice.md``, under the letters A–L, in an order of their own) and picks the card whose owner said it.  Random is 1/12; the gate asks for half.  S1 (the form of a
speech) and S3 (how much of it twelve bots share) are numbers over the same speeches: the suites ``S1`` and ``S3`` run these situations.

What makes a situation useful here: it must let a character show.  A turn that has one right thing to say (a vote, a rule) would make all twelve say it alike; these
have only a *kind* of thing to say - being asked, being accused, standing for sheriff, saying goodbye - and every bot says it its own way, or none does.  The nine:

====================  ==============================================================================================================================
situation             the moment
====================  ==============================================================================================================================
day1-first-words      the first words of the game: a quiet night, two players have spoken, nothing has happened
day1-after-death      a player was killed in the night and spoke his last words; three players have reacted
accused               the player before names the seat under test: "you voted N6 yesterday and now you have no side"
seer-standoff         two players each say they are the seer; the seat under test speaks after both of them
campaign              the sheriff election: the seat under test stands, and the other candidate has just spoken
last-words            the seat under test has just been exiled by a table that was wrong: its last words
hunter-aftermath      the hunter was killed in the night and shot a player: the table reacts, and the seat under test speaks next
quiet-day2            the second day of a game in which nothing has happened: nobody was exiled, nobody died; seven players have spoken
pk-speech             the exile vote tied between the seat under test and another player: the tie-break speech
====================  ==============================================================================================================================

Every other player speaks in plain, short lines (the bank's neutral lines): the only style in the situation is the one the bot brings.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bench.authoring import Table, check_claim
from bench.cases.common import Case
from bench.cases.families import ballots, expect_dead
from bench.cases.layouts import LAYOUTS as LAY

SUITE = "S2"


def _new(layout: str, seed: int) -> Table:
    L = LAY[layout]
    t = Table(L.role_list, bot=L.bot, seed=seed)
    t.open()
    return t


# Each scene builds the table up to the moment the seat under test must speak and returns (table, brief, what it tests, where the trap is).
Scene = Callable[[dict[str, Any]], tuple[Table, str, str, str]]


def day1_first_words(opt: dict[str, Any]) -> tuple[Table, str, str, str]:
    t = _new("v1", 3601)
    t.night(1, guard="N5", wolf="N5", seer="N6", witch="pass")
    expect_dead(t)  # (the guard guarded who the wolves attacked: a quiet night)
    t.election(candidates=[])
    t.dawn(1)
    t.talk(1, {}, until=t.bot, limits=opt.get("limits"))
    return (t, "第 1 天白天发言，昨夜平安，前两位玩家说了几句没有信息的话，轮到被测座位。",
            "游戏的第一句话：没有任何事可以回应，说什么、说多长都是自己的选择。", "没有要回应的事，所以最容易写出千篇一律的开场白；12 个 bot 的开场如果读起来一样，风格就没有被带到发言里。")


def day1_after_death(opt: dict[str, Any]) -> tuple[Table, str, str, str]:
    t = _new("v2", 3602)
    t.night(1, guard="N7", wolf="N3", seer="N8", witch="pass")
    expect_dead(t, "N3")
    t.election(candidates=[])
    t.dawn(1)
    t.last_words(1, {"N3": "我是平民，没有信息给大家。走得有点早，你们好好投，别被带节奏。"})
    t.talk(1, {
        "N1": "昨晚走的是 N3，我有点意外，他昨天一直很安静，不像是会被先清的人。",
        "N2": "N3 走了，我们的信息又少了一份。没有死因，没有身份，只能看发言和票。",
        "N4": "我也觉得意外。先听大家怎么看，我这边没有想法。",
    }, until=t.bot, limits=opt.get("limits"))
    return (t, "第 1 天白天发言：昨夜 N3 出局，说了遗言；三位玩家回应了这件事，轮到被测座位。",
            "有人走了、有人在回应，同一件事上每个 bot 怎么接话：是复述、追问、还是转到别的事上。", "三位玩家的话已经把“惊讶”的口气定好了，容易被它带成同一种口气。")


def accused(opt: dict[str, Any]) -> tuple[Table, str, str, str]:
    t = _new("v1", 3603)
    t.night(1, guard="N4", wolf="N5", seer="N6", witch="pass")
    expect_dead(t, "N5")
    t.election(candidates=[])
    t.dawn(1)
    t.last_words(1, {"N5": "我是好人，没有信息。希望大家别乱投，听清楚再动手。"})
    t.talk(1, {})
    votes = {s: "N6" for s in t.alive}
    votes["N6"] = "N1"
    t.day_vote(1, ballots(t.alive, votes), exile="N6", last="我是好人，这一票出去我认了，但你们投错了。后面的人自己看。")
    t.night(2, guard="N1", wolf="N1", seer="N2", witch="pass")
    expect_dead(t)
    t.dawn(2)
    t.talk(2, {"N1": "今天没人走，我先不说怀疑谁。", "N2": "N3 昨天投了 N6，今天又一直没有立场，我觉得 N3 在划水。N3 你给个说法。"}, until=t.bot, limits=opt.get("limits"))
    return (t, "第 2 天白天发言：上一位玩家点名被测座位，说它昨天投了被放逐的 N6、今天又没有立场，要它给个说法。",
            "被直接点名质疑时怎么回：辩解、反问、还是顺势转移——最容易露出个性的时刻。", "对方的措辞是现成的（“划水”“给个说法”），容易被拿来顺着说，而不是用自己的话回。")


def seer_standoff(opt: dict[str, Any]) -> tuple[Table, str, str, str]:
    t = _new("v7", 3604)
    t.night(1, guard="N3", wolf="N12", seer="N4", witch="pass")
    expect_dead(t, "N12")
    t.election(candidates=[])
    t.dawn(1)
    t.last_words(1, {"N12": "我是平民，没有信息给大家。你们别因为我走了就乱。"})
    t.talk(1, {
        "N5": check_claim("N1", "好人阵营"),
        "N6": check_claim("N4", "好人阵营"),
        "N1": "两个人都报了查验，我先记着，听完再说。",
    }, until=t.bot, limits=opt.get("limits"))
    return (t, "第 1 天白天发言：N5 和 N6 先后公开报了自己的查验结果（各说各的），其余玩家说了几句，轮到被测座位。",
            "面对两个都说自己是预言家的人：信谁、怎么说理由、语气是谨慎还是果断。", "局面诱导每个 bot 都去比较两份记录，容易说成同一种“逐条对账”的腔调。")


def campaign(opt: dict[str, Any]) -> tuple[Table, str, str, str]:
    t = _new("v3", 3605)
    t.night(1, guard="N12", wolf="N1", seer="N8", witch="pass")
    expect_dead(t, "N1")
    t.election(candidates=["N2", "N4"], turns={"N2": "我是 N2，上警是想把每天的票型和发言记清楚，归票的时候有据可查。大家觉得合适的话，把警徽给我。"}, until=t.bot, limits=opt.get("limits"))
    return (t, "第 1 天警长竞选：被测座位上警，另一位候选人 N2 刚发完言，轮到被测座位发表竞选演说。",
            "自己拉票：怎么介绍自己、承诺什么、对对手是回避还是回应。", "“上警想把票型记清楚”这类现成的竞选腔已经被对手说在前面，容易被复述。")


def last_words(opt: dict[str, Any]) -> tuple[Table, str, str, str]:
    t = _new("v4", 3606)
    t.night(1, guard="N3", wolf="N4", seer="N8", witch="pass")
    expect_dead(t, "N4")
    t.election(candidates=[])
    t.dawn(1)
    t.last_words(1, {"N4": "我是平民，没有信息给大家。你们好好投。"})
    t.talk(1, {})
    votes = {s: "N10" for s in t.alive}
    votes["N10"] = "N7"
    t.day_vote(1, ballots(t.alive, votes), exile="N10")
    t.mod("set_phase", {"text": "第 1 天 · 遗言"}, dt=2.0)
    t.queue(["N10"], {}, until=t.bot, limits=opt.get("limits"))
    return (t, "第 1 天：被测座位被全桌投出局（它是好人），现在是它的遗言。",
            "告别的话：愤怒、平静、交代还是叮嘱——一个人在最后一次发言里最像自己。", "遗言有现成的套路（“我是好人，你们投错了”），容易所有 bot 都写成同一句。")


def hunter_aftermath(opt: dict[str, Any]) -> tuple[Table, str, str, str]:
    t = _new("v5", 3607)
    t.night(1, guard="N9", wolf="N2", seer="N10", witch="pass")
    expect_dead(t, "N2")
    t.election(candidates=[])
    t.dawn(1)
    t.hunter_shoots("N2", "N8")
    t.last_words(1, {"N2": "我是猎人，开枪带走 N8。信不信由你们，枪是不会骗人的。"})
    t.talk(1, {
        "N1": "猎人开了枪，带走 N8。他凭什么确定 N8 有问题？这一枪有点冒险。",
        "N3": "猎人的枪我倾向于信，N8 先放一放，我们看后面的发言。",
        "N4": "我没有判断，先听。",
    }, until=t.bot, limits=opt.get("limits"))
    return (t, "第 1 天白天发言：昨夜猎人 N2 被刀，开枪带走了 N8；三位玩家表了态，轮到被测座位。",
            "一件有后果的公开事件之后，各人的第一反应：站边、质疑、还是观望。", "有人质疑、有人相信，容易所有 bot 都说“两边都有道理，先观望”。")


def quiet_day2(opt: dict[str, Any]) -> tuple[Table, str, str, str]:
    t = _new("v6", 3608)
    t.night(1, guard="N6", wolf="N6", seer="N11", witch="pass")
    expect_dead(t)
    t.election(candidates=[])
    t.dawn(1)
    t.talk(1, {})
    t.day_vote(1, ballots(t.alive, {}), exile=None)
    t.night(2, guard="N12", wolf="N12", seer="N5", witch="pass")
    expect_dead(t)
    t.dawn(2)
    t.talk(2, {}, until=t.bot, limits=opt.get("limits"))
    return (t, "第 2 天白天发言：两夜平安、第 1 天没有人被放逐，前面七位玩家都说了没有信息的话，轮到被测座位。",
            "一个什么都没发生的局面：有话可说的人怎么把话说出来，没话说的人怎么说没话。", "前面七个人的话几乎一样，容易被它带成第八个一样的。")


def pk_speech(opt: dict[str, Any]) -> tuple[Table, str, str, str]:
    t = _new("v8", 3609)
    t.night(1, guard="N10", wolf="N3", seer="N12", witch="pass")
    expect_dead(t, "N3")
    t.election(candidates=[])
    t.dawn(1)
    t.last_words(1, {"N3": "我是平民，没有信息给大家。"})
    t.talk(1, {})
    votes = {"N4": "N7", "N6": "N7", "N8": "N7", "N11": "N7", "N12": "N7", "N1": "N9", "N2": "N9", "N5": "N9", "N10": "N9", "N7": "N9", "N9": "abstain"}
    t.vote_open(1, label="vote1")
    t.vote_cast(ballots(t.alive, votes), label="vote1")
    tied = t.vote_close(ballots(t.alive, votes))
    assert tied == ["N7", "N9"], tied
    t.tie_pk(1, tied, {}, until=t.bot)
    return (t, "第 1 天放逐投票：被测座位和 N9 并列最高票（各 5 票），进入平票 PK，被测座位先发言。",
            "为自己的命辩护：被逼到墙角时的口气。", "“我是好人，别投我”是现成的辩护，容易所有 bot 都从它开始。")


SCENES: dict[str, Scene] = {
    "day1-first-words": day1_first_words, "day1-after-death": day1_after_death, "accused": accused, "seer-standoff": seer_standoff, "campaign": campaign,
    "last-words": last_words, "hunter-aftermath": hunter_aftermath, "quiet-day2": quiet_day2, "pk-speech": pk_speech,
}


def header(suite: str, key: str, table: Table, brief: str, tests: str, trap: str, *, extra: list[str] = ()) -> str:  # type: ignore[assignment]
    lines = [
        f"{suite} · {key}",
        f"在测什么：{tests}",
        f"被测座位：{table.bot}（{table.role_of(table.bot)}）；12 人暗牌局（werewolf_12p 2.2.0），局面是用主持人手册的步骤在真实引擎上打出来的。",
        f"局面：{brief}",
        f"陷阱：{trap}",
        *extra,
        "本文件由 bench/cases 里的代码生成，不要手改：改代码后运行 `python -m bench fixtures` 重写。",
    ]
    return "\n".join(lines)


def build(key: str) -> tuple[str, dict[str, Any]]:
    t, brief, tests, trap = SCENES[key]({})
    judge_note = "S2 的裁判拿 12 张风格卡（每个 bot 的 voice.md 三行）去匹配这段发言；随机水平 1/12，门槛 ≥ 50%。S1 / S3 用同一批发言算形态和同质化。"
    head = header("S2", key, t, brief, f"风格识别：{tests}", trap, extra=[judge_note])
    data = t.situation(
        sid=f"s2-{key}", title=f"风格：{key}", tags=[SUITE, "speech"], decision={"seat": t.bot, "expect": ["speak"]}, brief=brief, judge=["S2"], meta={"moment": key},
    )
    return head, data


def cases() -> list[Case]:
    return [Case(f"s2-{key}", SUITE, f"situations/s2-{key}.yaml", (lambda k=key: build(k))) for key in SCENES]


__all__ = ["SCENES", "build", "cases", "header"]
