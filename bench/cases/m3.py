"""Suite M3 (规则理解): a quiz on the official rules of ``werewolf_12p`` 2.2.0, asked the way the engine asks anything: a ``choice`` request to the seat under
test (the moderator's ``open_request``), answered with ``respond``.  The answer is one option id; ``truth.answer`` says which.

What is measured: whether the bot has the rules it was given (they are in its prompt: "【这局游戏的官方规则】") and whether what it remembered of them in its own
words (the L2 digest, the playbook) helps or gets in the way.  Gate M3: accuracy >= 90%.  The questions are the rules that decide games and that bots get wrong:
same-guard-same-save, who has last words, the 1.5 votes of the sheriff, the PK, the poisoned hunter, the swallowed badge on the first day ...

Each question has ONE correct option from the text of the rules (``truth.rule`` quotes where), and two or three wrong ones that are the usual mistakes (a rule of
another version of the game, the opposite, a half of the rule).  The correct option's position goes round a, b, c, d so that position tells nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bench.authoring import Table
from bench.cases.common import Case
from bench.cases.layouts import LAYOUTS as LAY

SUITE = "M3"
LETTERS = "abcd"


@dataclass(frozen=True)
class Quiz:
    key: str
    question: str
    right: str
    wrong: tuple[str, ...]
    rule: str  # where the rules say it
    topic: str


QUIZZES: tuple[Quiz, ...] = (
    Quiz("same-guard-same-save", "守卫当晚守护了 A，女巫当晚用解药救了 A，狼人当晚刀的也是 A。A 的结果是什么？", "出局（同守同救）",
         ("存活（被守卫守住）", "存活（被解药救下）", "要看狼人刀口是哪一个"), "二、身份与技能「同守同救」；三、结算", "night"),
    Quiz("guard-consecutive", "守卫第 1 夜守护了 N5。第 2 夜他还想守 N5，可以吗？", "不可以：不能连续两晚守护同一人",
         ("可以", "只有守自己才可以连续", "只有 N5 被刀过才可以"), "二、身份与技能「守卫」", "night"),
    Quiz("guard-self-empty", "守卫每晚的守护对象有哪些选择？", "可以守任何一名存活玩家，包括自己，也可以空守",
         ("只能守别人，不能守自己", "必须守一个人，不能空守", "只能守自己或空守"), "二、身份与技能「守卫」", "night"),
    Quiz("witch-one-potion", "女巫同一晚最多能用几瓶药？", "一瓶（解药和毒药同一晚只能用一瓶）", ("两瓶（解药和毒药都用）", "不限，只要药还在", "解药不限，毒药一瓶"),
         "二、身份与技能「女巫」", "night"),
    Quiz("witch-told-target", "女巫的解药还在时，主持人夜里会私下告诉她什么？", "今晚被狼人袭击的人（可能是她自己）", ("今晚所有出局者的身份", "预言家查验了谁", "守卫守了谁"),
         "二、身份与技能「女巫」；三、黑夜", "night"),
    Quiz("poisoned-hunter", "猎人因为被女巫毒死而出局，他可以开枪吗？", "不可以", ("可以，和被刀一样", "只有白天才可以", "可以，但只能带走女巫"), "二、身份与技能「猎人」", "hunter"),
    Quiz("exiled-hunter", "猎人在白天被放逐，他可以开枪吗？", "可以（也可以选择不开枪）", ("不可以，只有夜里出局才能开枪", "只有他是警长才可以", "不可以，放逐后没有任何技能"), "二、身份与技能「猎人」", "hunter"),
    Quiz("hunter-taken-by-explosion", "猎人被白狼王自爆带走，他可以开枪吗？", "可以", ("不可以，自爆当天一切作废", "只有他事先亮明身份才可以", "不可以，只有被刀才可以"), "二、身份与技能「猎人」", "hunter"),
    Quiz("hunter-victim-last-words", "被猎人开枪带走的玩家有遗言吗？", "没有", ("有", "只有他是好人才有", "只有第 1 天被带走才有"), "七、遗言", "last-words"),
    Quiz("last-words-night1", "第 1 夜被狼人刀死的玩家有遗言吗？", "有", ("没有，夜里出局的人都没有遗言", "只有他是神职才有", "只有有警长的时候才有"), "七、遗言", "last-words"),
    Quiz("last-words-night2", "第 2 夜被狼人刀死的玩家有遗言吗？", "没有", ("有，夜里出局的人都有遗言", "只有他是警长才有", "只有他是神职才有"), "七、遗言", "last-words"),
    Quiz("last-words-exiled", "被放逐的玩家有遗言吗？", "有（任意一天被放逐的人都有）", ("没有", "只有第 1 天被放逐的人有", "只有被放逐的是好人才有"), "七、遗言", "last-words"),
    Quiz("last-words-explosion", "白狼王自爆之后，自爆者和被他带走的人有遗言吗？", "都没有", ("都有", "只有被带走的人有", "只有自爆者有"), "七、遗言；二、身份与技能「白狼王」", "last-words"),
    Quiz("sheriff-weight", "警长在放逐投票里算几票？", "1.5 票", ("1 票，和别人一样", "2 票", "0.5 票，只在平票时才算"), "五、警长「警长的权利」", "sheriff"),
    Quiz("sheriff-voters", "首日警长竞选的投票，谁有投票权？", "只有自始至终没有上警的玩家（警下）", ("所有存活玩家", "只有上警的玩家", "只有神职"), "五、警长「投票资格」", "sheriff"),
    Quiz("quit-voters", "上警之后中途退水的玩家，这一轮警长投票有投票权吗？", "没有（上警者包括中途退水的都没有这一轮的投票权）", ("有", "只有退水之后才有", "只有他是好人才有"), "五、警长「投票资格」", "sheriff"),
    Quiz("sheriff-tie", "首日警长竞选投票出现并列最高票，怎么处理？", "并列者 PK 发言，警下玩家在并列者中重投；仍并列则本局不设警长", ("并列者共同担任警长", "随机抽一个当警长", "直接不设警长，不再重投"), "五、警长「结果」；六、平票", "sheriff"),
    Quiz("sheriff-badge-out", "警长出局时，警徽怎么处理？", "可以移交给一名存活玩家，或者撕毁（此后本局不再有警长）", ("自动交给下一个座位号", "自动作废，不能移交", "只有警长被放逐时才能移交"), "五、警长「警长出局」", "sheriff"),
    Quiz("swallowed-badge", "首日警徽还没产生时，白狼王自爆，会怎样？", "本局不设警长（吞警徽）", ("竞选照常继续", "警徽自动给白狼王", "警徽给被带走的人"), "五、警长；二、身份与技能「白狼王」", "sheriff"),
    Quiz("exile-tie", "放逐投票里有两个人并列最高票，怎么处理？", "并列者按座位顺序 PK 发言，其余存活玩家在并列者之间重投；仍并列则当日无人被放逐", ("两个人都被放逐", "警长决定放逐哪一个", "并列者再投一次"), "六、投票、平票与 PK", "vote"),
    Quiz("revote-voters", "平票 PK 之后的重投，谁来投票？", "其余存活玩家（并列者不投票）", ("所有存活玩家", "只有警长", "只有并列者"), "六、投票、平票与 PK", "vote"),
    Quiz("abstain", "放逐投票可以弃票吗？警长竞选的投票呢？", "放逐投票可以弃票；警长竞选投票不能弃票", ("两种都可以弃票", "两种都不能弃票", "放逐投票不能弃票，警长竞选可以"), "六、投票、平票与 PK", "vote"),
    Quiz("good-wins", "好人阵营在什么条件下获胜？", "4 名狼人（含白狼王）全部出局", ("4 名神职全部存活", "狼人的人数少于好人", "放逐过一名狼人"), "八、胜负", "win"),
    Quiz("wolves-win", "狼人阵营（屠边）在什么条件下获胜？", "4 名神职全部出局，或 4 名平民全部出局", ("好人的人数少于狼人", "预言家出局", "神职和平民各出局一半"), "八、胜负", "win"),
    Quiz("both-win", "一轮出局事件处理完之后，好人和狼人的胜利条件同时满足，谁获胜？", "好人阵营优先", ("狼人阵营优先", "平局", "最后出局的一方获胜"), "八、胜负", "win"),
    Quiz("seer-white-wolf", "预言家查验白狼王，主持人会告诉他什么？", "狼人阵营", ("好人阵营", "白狼王的具体身份", "查验失败，没有结果"), "二、身份与技能「预言家」", "night"),
    Quiz("seer-info", "预言家查验一名玩家，得到的信息是什么？", "他属于“好人阵营”还是“狼人阵营”，不告知具体身份", ("他的具体身份", "他昨晚做了什么", "他是不是神职"), "二、身份与技能「预言家」", "night"),
    Quiz("dark-game", "在这一局里，出局的玩家会被公布身份吗？", "不会：只公布“某某出局”；例外是猎人开枪和白狼王自爆", ("会，出局时翻牌", "只公布死因，不公布身份", "只有好人的身份会公布"), "一、配置与阵营「暗牌局」", "overview"),
    Quiz("wolf-knife-tie", "狼人的刀口到最后期限仍然不一致，怎么确定？", "按多数；票数相同时，以座位最靠前的狼人的选择为准", ("视为空刀", "随机选一个", "由白狼王决定"), "二、身份与技能「普通狼人」", "night"),
    Quiz("explosion-timing", "白狼王可以在什么时候自爆？", "白天非投票的正式发言环节（含警长竞选演说和平票 PK 发言），主持人启用期间", ("任何时候，包括夜里", "只有放逐投票期间", "只有被放逐之后"), "二、身份与技能「白狼王」", "explosion"),
    Quiz("day1-order", "第 1 天，“警长竞选”和“公布死讯”哪个先进行？", "先警长竞选，再公布死讯（所以第 1 夜出局的人不参加竞选）", ("先公布死讯，再警长竞选", "两件事同时进行", "第 1 天不竞选警长"), "四、白天", "sheriff"),
    Quiz("sheriff-direction", "警长每天决定什么？谁在白天正式发言里最后发言？", "决定“警左”或“警右”的发言方向；警长本人最后发言并归票", ("决定谁被放逐；最后发言的是座位号最大的人", "决定谁先发言；警长第一个发言", "决定发不发言；最后发言的是被放逐者"), "五、警长「警长的权利」；四、白天", "sheriff"),
    Quiz("knife-and-poison", "猎人同一晚既被狼刀（刀生效）又被女巫毒，他可以开枪吗？", "可以（同一人既被刀又被毒，死因按狼刀计；只有因毒药而出局的猎人不能开枪）", ("不可以，被毒就不能开枪", "可以，而且还能再开一枪", "要看女巫的选择"), "三、结算", "hunter"),
)


def _options(quiz: Quiz, index: int) -> tuple[list[dict[str, str]], str]:
    """The options of question ``index``: the right one at position ``index mod n`` (so the answer letter goes round), the wrong ones in the order given."""
    texts = list(quiz.wrong)
    position = index % (len(texts) + 1)
    texts.insert(position, quiz.right)
    return [{"id": LETTERS[i], "label": text} for i, text in enumerate(texts)], LETTERS[position]


def build(index: int, quiz: Quiz) -> tuple[str, dict[str, Any]]:
    options, answer = _options(quiz, index)
    t = Table(LAY["v1"].role_list, bot=LAY["v1"].bot, seed=3300 + index)
    t.open()
    t.mod("set_phase", {"text": "规则测验"}, dt=3.0)
    t.mod("open_request", {"to": [t.bot], "kind": "choice", "question": f"规则测验：{quiz.question}", "options": options, "duration_s": 90}, label="quiz", dt=2.0)
    sid = f"m3-{quiz.key}"
    right = next(o["label"] for o in options if o["id"] == answer)
    header = "\n".join([
        f"M3 · 规则测验：{quiz.question}",
        f"在测什么：规则理解（{quiz.topic}）——“{quiz.rule}”。bot 的提示里有完整的官方规则；这题看它有没有读对、记对。",
        f"被测座位：{t.bot}（{t.role_of(t.bot)}）；局面只有开局的频道和一条主持人发来的测验请求。",
        f"正确答案：{answer}（{right}）。错误选项是常见的误解：别的版本的规则、相反的说法、只对一半的说法。",
        "本文件由 bench/cases/m3.py 生成，不要手改：改代码后运行 `python -m bench fixtures` 重写。",
    ])
    truth = {"request": "@quiz", "answer": [answer], "kind": "rules", "rule": quiz.rule, "decisive": f"官方规则：{quiz.rule}。正确的是“{right}”。", "notes": quiz.topic}
    data = t.situation(sid=sid, title=f"规则测验：{quiz.key}", tags=[SUITE, quiz.topic], decision={"seat": t.bot, "expect": ["respond"]}, truth=truth,
                       brief=f"规则测验（{quiz.topic}）：{quiz.question} 正确答案 {answer}：{right}。", meta={"quiz": quiz.key, "topic": quiz.topic})
    return header, data


def cases() -> list[Case]:
    out = []
    for index, quiz in enumerate(QUIZZES):
        out.append(Case(f"m3-{quiz.key}", SUITE, f"situations/m3-{quiz.key}.yaml", (lambda i=index, q=quiz: build(i, q))))
    return out


__all__ = ["QUIZZES", "Quiz", "build", "cases"]
