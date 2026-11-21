"""Suite L4 (真实局面): five decision points cut from three real games played on this machine, rebuilt with every word invented.

A real game is the best question there is and the worst thing to publish: the speeches in it are the words of the people who played (and of the bots that wrote them), and
the bank is committed.  So an L4 situation keeps the *structure* of the real moment and none of its text: who sat where and who they really were, who claimed to be the seer and
whom they said they had checked, who withdrew, how each of the others voted, what the table said it would do - and every line a player says is the author's own, in the register
the real one had (a blunt person against a fluent bot, a hurried claim against a calm one), under the default names.  The roles and the votes are the game's; the final result of the
game is the truth.  (``python -m bench replay --list-decisions`` finds the moments, ``--emit`` writes the real one as a situation - the raw file this was written from, never
committed.  ``tests/bench/test_bank.py`` checks, with ``ARENA_BENCH_SAVED_GAMES=1``, that every file still agrees with its game and holds none of its words.)

What is measured (plan §6.5 L4): *side accuracy* - does the bot, at the moment the real table got it wrong, side with the right camp?  The answer is objective and comes from the
end of the game, not from a fact the bot can find: a sheriff vote is right when it goes to the real seer; an exile vote is right when it goes to a wolf.  Nothing here is
derivable the way an L3 fact is - the real players could not derive it either - so the baseline matters: gate L4 is "baseline + 20 points".  In all five, the real table got
the side wrong (the wolves won the badge three times and the true seer was voted out once), which makes them hard on purpose.

====================================  ============  ==========================================================================================  ==========================
situation                             real game     the moment                                                                                  the real table
====================================  ============  ==========================================================================================  ==========================
l4-sheriff-blunt-seer-vs-fluent-wolf  0fa7   sheriff vote, day 1: a blunt seer and a fluent wolf both claim; the wolf has named the     8 of 9 voted for the wolf
                                                      third candidate a wolf, and he has withdrawn
l4-exile-follow-the-sheriff           0fa7   exile vote, day 1: the wolf holds the badge; ten players say they follow it                the villager was exiled
l4-sheriff-kill-check-vs-gold-check   2b91   sheriff vote, day 1: a seer who names a wolf against a wolf who hands out a gold water     5 of 9 voted for the wolf
l4-sheriff-opposite-checks-on-one     e0f2   sheriff vote, day 1: two claimers give opposite results on the same player; a third          6 of 9 voted for the wolf
                                                      candidate who says he has no role does not withdraw
l4-exile-true-seer-on-trial           e0f2   exile vote, day 1: the badge is a wolf's; the true seer is the table's target; one          the true seer was exiled
                                                      candidate said something only a wolf says
====================================  ============  ==========================================================================================  ==========================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bench.authoring import WOLF_ROLES, Table, by_seat
from bench.cases.common import Case
from bench.cases.families import expect_dead

SUITE = "L4"

#: The identities of the saved games, in seat order N1 ... N12 (the games' own: the deal of the saved game).
ROLES: dict[str, list[str]] = {
    "0fa7": ["seer", "werewolf", "guard", "witch", "white_wolf", "villager", "villager", "hunter", "werewolf", "villager", "villager", "werewolf"],
    "2b91": ["hunter", "werewolf", "villager", "werewolf", "villager", "seer", "witch", "villager", "villager", "white_wolf", "werewolf", "guard"],
    "e0f2": ["white_wolf", "villager", "werewolf", "villager", "villager", "werewolf", "villager", "werewolf", "guard", "witch", "seer", "hunter"],
}
WINNER = {"0fa7": "狼人阵营", "2b91": "狼人阵营", "e0f2": "好人阵营"}


# ---------------------------------------------------------------------------------------------------------------------- the words (all of them the author's)
#: Game A (0fa7), day 1.  N1 is the real seer, N2 a wolf who jumped on the seer, N6 a villager whom N2 called a wolf.
A_ELECTION = {
    "N1": "预言家是我。昨晚我验了 8 号，是个好人。验他，是因为上一局他跟我一个狼队，这局想先弄清他的底。至于警徽流，上台的我验那个没跟我对跳的，台下就验 12 号或者 2 号，反正都在我旁边。以后再细排。",
    "N2": "我才是预言家。第一晚我验的 N6，结果是狼。狼队这是派了人上来争警徽，好在我在这里。回头看 N1：她验 8 号说是旧识，转头又说台下验 12 或 2，自己都没想好验谁，不像有把握的真预言家。我接下来这样排：明晚先验 N1，弄清她跟 N8 是金水还是同伙，再验 N3。请把警徽投给我，今天先送 N6 出局。",
    "N6": "我是个平民，夜里没有任何信息。我上警，只是想在台上多放一个好人。N2 的查杀不用负责任：这里不亮出局者的身份，他想查谁就查谁，永远没人能对他的话。我看狼最喜欢这种悍跳，挑个上警的平民发查杀，顺便拿走警徽。我不跟他争，退水，请台下把票给 N1。N8，你手里有金水，之后得多出点力。我信 N1。",
}
A_DAY1 = {
    "N3": "先说明：警长票里只有我投了 N1。当时觉得 N6 被查杀还主动退水让票，像好人认输。看到票型我改主意了：N2 拿了八票，N1 发给 N8 的金水，N8 自己却把票投给了 N2——真预言家的金水不会投给对面，所以那是假金水，N1 是悍跳。我信 N2，今天出 N6，明天再看 N1。我夜里没有信息，只按票型判断。",
    "N4": "我跟 N2，今天出 N6。N1 的金水 N8 都没站她，金水这条不成立；N2 讲到现在没有漏洞，该给他 1.5 票。N3，我想问：你说 N6 退水像认输，可被查杀的人退水只会更显得想自保，你却把票给了报他查杀的对跳位。你一开口就转头打 N1，转得很利索，像投错了赶紧补救。当时为什么不选弃票？",
    "N5": "我是 N5，普通位，夜里没信息。警长票给 N2，原因不在嗓门：他报查杀时把后几晚的验人安排一起交了（先 N1，再 N3），对不对很快见分晓；N1 却连自己发的金水 N8 都没拉住。今天我跟着出 N6。另外昨夜平安，说明刀被守卫挡了或被女巫救了，之后谁抢着提被刀的人，要留意。",
    "N6": "全场都说 N2 的验人对得上，我看正相反：这里不公布出局者身份，我被推走没人能证明自己是平民，他将来倒下也没人替他验，所以这张查杀对他毫无代价。他先验 N1 再验 N3，验对跳的位置，结果当然是狼，等于白验一晚；真预言家会验中间位，这像背好的稿。最可疑的是 N8：拿着 N1 的金水，却投了报我查杀的人。请你正面解释。N3 也一样，投了 N1 又比谁都急着打她，是止损，还是在给 N2 做“独立”？要出我可以，但明天 N2 必须把验人交出来。",
    "N8": "回应 N6。N1 说验我是因为上一局是队友，这个理由我不接受；她台下验 12 或 2 的安排也含糊。金水本来就该自己掂量，不是拿来信的，所以哪怕握着 N1 发的金水，我还是投了 N2：我看逻辑，不看谁递给我一张好人牌。今天的票出 N6，我不反对，跟着警长。再说两件：N2，明天的验人必须拿出来，别拿“这里不公布身份”搪塞；N3 那票我不追，解释我只信一半。我更想看 N1 今晚会怎样接着编，她点过名的 2 号和 12 号别被带偏。我不替任何人挡刀。",
    "N9": "我是平民牌，夜里没信息。今天的票我跟着警长走，出 N6，原因两个：一，N1 报验人时给的理由，还有她的警徽流排法，都不像在找狼，她发的金水 N8 又投了 N2，这条金水是在拉票；二，N6 一被查杀就退水，还让台下把票集中给对跳的人，被冤枉的人不会这样做。另外，N3 一个人把票投给了 N1，请他明天说明白；N2 明天必须交出验人。",
    "N10": "我是 N10，平民，夜里没信息。警长票我给了 N2：N1 的验人理由轻率，警徽流前后矛盾，她发的金水 N8 又反过来投 N2，这条金水是拉票用的。今天出 N6 我也同意。但我不想只喊口号，补两点：N2 打算第一验 N1 是浪费，应当验 N3、N8 这样的关系位；N8 你站边很干脆，这票还得再亮一次底。N1 还没开口，等她说完再下结论。",
    "N11": "我是 N11，平民，夜里没信息，今天投 N6。我只补一条：N6 被查杀后没有硬顶，而是退水，还叫台下的人把票全给 N1，被查杀的人替对跳位拉票，这条最有说服力，也就是说 N1 在悍跳、N6 是狼。另外 N1 还没发言，如果她是狼，今晚多半沿着她点过名的 2 号、12 号继续编，看谁第一个替她接。N3，“退水像认输”这个理由站不住。",
    "N12": "我是 N12，普通位，夜里没信息，今天出 N6。N2，明天不如验 N3 吧，他的那一票至今讲不出理由，验出狼就出，验成金水他也洗不白，比白验 N1 有用。还有一句预先说明：N1 排警徽流时提到过 2 号和 12 号，我就是 12 号，她今晚要是顺势点我，请大家看我的票型，别听她一句话就定我的罪。N8 敢不认那张金水，我给他记个好人位。",
    "N1": "这是要把 2 号整个票飞啊。我刚才没留意 2 号也在台上。我就是真预言家，道理摆在这儿。你们看：4 号站 2 号，5 号也站 2 号，6 号是 2 号报的查杀，那他就是铁好人。你们动动脑子行不行？2 号根本说不出理由，只会丢一个听着很笃定的结论。想赢的好人，现在就跟我把 2 号投出去，要不然狼赢定了。真没几个人信我，可狼是抱团的，别犯糊涂。",
    "N2": "N1 这一段，等于什么都没回答：金水 N8 为什么没投她，她不解释；验人的理由，她也不提；只会喊让大家把我投出去。真预言家被查杀、被对跳，会先摊开自己的验人链，她给的只有情绪。还有一处：主持人开场就念过候选人名单，她却说没留意我也在台上，真预言家会先听完对手再开口，她连对手都没看。我最后归票给 N6。验人我听大家的：今晚验 N8，他是 N1 金水链的关键，是狼这条链当场塌，是好人明天我把警徽流交给他。",
}
A_BALLOTS_SHERIFF = {"N3": "N1", "N4": "N2", "N5": "N2", "N7": "N2", "N8": "N2", "N9": "N2", "N10": "N2", "N11": "N2", "N12": "N2"}
A_BALLOTS_EXILE = {"N1": "N2", "N2": "N6", "N3": "N6", "N4": "N6", "N5": "N6", "N6": "N2", "N7": "N6", "N8": "N6", "N9": "N6", "N10": "N6", "N11": "N6", "N12": "N6"}

#: Game B (2b91), day 1.  N6 is the real seer (his kill check on N4 is true), N2 a wolf who jumped and handed N5 a gold water, N8 a villager who withdrew.
B_ELECTION = {
    "N2": "我是预言家，昨晚验了 N5，是好人。选中段位，是因为狼多半藏在那里，先拿一张干净的牌垫底。接下来两晚打算验 N8、N3——N8 我会重点看他今晚的发言，如果左右逢源，就是我的第二验。同台的 N6、N8，要认预言家就先把首夜结果和后两晚的计划摆出来，只说感觉不算数。你报了，我就跟你逐条对；对不上的自己退。你要是村民，上警只是来试探我，现在退还来得及。警下的票请投给我：摆得出验人记录的人，比光会煽情的人靠谱。",
    "N6": "我才是预言家。第一晚我验的是 N4，结果是狼人，这是查杀。N2 是对跳的悍跳位：他给 N5 的金水没法验证，可他排警徽流时把同台另外两个人都写进去了，先替自己占住别人的位置，这是悍跳最省力的写法。我的安排：今晚验 N2，把悍跳位定下来，下一晚验 N8。万一我出局，遗言只有两句：N4 是狼，N2 是悍跳。今天我的票不分散，狼的排序，N4 在前，N2 在后。请台下把票给我——敢亮出查杀、也敢和悍跳位硬碰的人，才配拿这块警徽。",
    "N8": "我上警，本来只是想把规矩摆上桌；现在两份查验都出来了，我退出竞选，投票权交回台下。两边我暂时不下结论，只列几点可核对的：第一，N2 说选中段位是因为狼多半在那，是先有结论再找理由；他的警徽流把另外两个候选人都点了名，这种做法真预言家也会用，所以我只扣一点分，不定罪。第二，N6 报了查杀，这是今天风险最大的位置，报错自己就得走，核对起来也最容易；不过查杀本身算不上证据，N4 怎么应对才是。今天的焦点是 N4：希望他先自证：结构、对位、票向都讲清楚，光喊冤不够，抢着反咬也不好。谁在 N4 开口前就替他定性，我会留意。我的票先不锁，等 N4 说完，在 N2 和 N6 之间选。",
}
B_BALLOTS_SHERIFF = {"N1": "N6", "N3": "N6", "N4": "N2", "N5": "N2", "N7": "N6", "N9": "N2", "N10": "N2", "N11": "N2", "N12": "N6"}

#: Game C (e0f2), day 1.  N11 is the real seer, N3 a wolf who jumped on the seer and gave N6 (his teammate) a gold water, N1 the white wolf (the human of that game), who claims nothing.
C_ELECTION = {
    "N1": "我先上来看看，没身份，先观望，这边没什么信息。往后很可能是狼和预言家打起来，我再看，多半会退水。",
    "N3": "我是预言家。昨晚验的 N6，查验结果是好人，金水。选他，是想从不起眼的中段位先探个底，给后面的查验定个坐标。后面的安排是今晚验 N9、明晚验 N12，这两个座位靠后、发言晚，狼最爱躲在那里。对跳的人我只有一个要求：把两晚验谁、为什么验这个人讲清楚；真假看逻辑，不看谁嗓门大。N1 自称没身份、可能退水，这个我先放一边，不算坏证据，暂时也不打她。我拿到警徽会把票往狼身上归。请台下把票投给我，让大家有个确定的方向。",
    "N11": "我是预言家，昨晚验了 N6，结果是狼人阵营。所以 N3 在悍跳，N6 是她的队友，她替他报金水，是在洗白，这个金水我直接否掉。我的警徽流：今晚验 N9，明晚验 N2——N9 位置靠后、还没发过言，N2 排在前面，先给他们各定一个参照。判断谁真谁假，不用看谁气势足，只看结论：N3 说验了 N6 是金水，而 N6 实际是狼，她的话与我验出的结果直接对立。如果我被刀或者被自爆带走，请按 N6、N3 的顺序出，之后的查验我白天会报。请警下把票投给我，白天先把 N6 放逐。",
}
C_DAY1 = {
    "N1": "我是个带刀的好人。两个预言家我都不太满意，所以不退水；要是好人不信他俩，我可以出来带队。今天我觉得 6 和 11 都不该出，万一出到真预言家，好人亏大了。我想的是夜里让好人动刀，挑一个说得差的带走，再接着盯焦点位。",
    "N12": "三点。其一，N1 在台上讲自己没身份、会退水，最后没退；这会儿又自称带刀好人，主张夜里让好人动刀——这个视角不对劲，我最盯她。其二，对跳的两位，N3 把验人理由和评判标准讲得更全，她第二晚排到我，我无所谓；N11 排在后面，第一晚也选了 N9，和 N3 撞车，开口就是“N6、N3 必出”，太着急。所以我偏向 N3 为真，狼在 N11 这头。其三，我是平民，警长票给了 N3，今天的票先放 N11，要听过 N6 的解释再定。",
    "N11": "我就是唯一的真预言家，验了 N6，是狼，这是定论。N3 替 N6 报金水，说明两人是一伙，用金水保队友，再用这套话拿到警徽。她第一晚的警徽流点 N9，和我同一手，太巧。回 N2、N12：看预言家真假不是比理由谁漂亮，N3 明明清楚 N6 的底细，替队友找理由毫不费力；我急，是因为手里有查杀，没什么要藏的。N1 要大家别出 6 也别出 11，等于在替 N6 续命，身份又不清楚，我盯着她。今天票落 N6。N3 是狼警长，明天要出。",
    "N10": "我的警长票给了 N3：她的验人理由和评判标准更完整；N11 开口就喊“N6、N3 必出”，没有人能同时认定两个狼，她喊得太急了。但我今天不站死，关键看 N6。第一，N6 要说明：你把警长票给了给你发金水的 N3，为什么？你若是好人，就说清你谁也不认识；你若是狼，那就是抱成一团。第二，N1 比两边都可疑：她的说法前后对不上，先称没有身份、准备退水，实际留在台上，接着又冒出个带刀好人的身份，还想让 6 和 11 都留下、夜里动刀——这种和稀泥的立场，我先记在心里。第三，N4、N7、N8 投了 N11，请说清理由，别只是跟势头。我今天的票在 N1 和 N11 之间。",
    "N9": "我是平民，没有身份，警长票给了 N3：她的验人理由和评判标准讲得完整。N11 是后手，第一晚也点我，同一手撞了之后，又在 N3 发金水的那个人身上直接报了同一目标的查杀，像是对着结论硬顶，更像悍跳在抢话语权。这两位在同一个人 N6 身上给出了相反的结果，所以 N6 本人怎么说最要紧：他为什么把警长票给了给他发金水的 N3，是巧合还是抱团？N1 想把两个焦点一起糊过去，我看该被压的是她。我今天的票倾向 N11；警长要验我、要我表态，我都不躲。",
    "N8": "先回 N10：我先前投 N11，只因为她是唯一亮出查杀的人，想用一票逼她多给些信息，并不是在站边。这一轮听完我改判：偏向 N3 是真预言家。三个原因：第一，N11 开口就喊“N6、N3 必出”，没人能一口气锁定两个目标，这是狼想抢节奏时常用的写法，后面还用威胁来代替逻辑；第二，她的警徽流第一晚也是 N9，跟 N3 撞了，之后才报同一目标的查杀；第三，N3 的验人理由和评判标准是完整的。N1 提议两个都别出、夜里再动刀，我同样不接受，那不是好人视角，她今天该被压。N6 要自己解释台下的票为什么给 N3。",
    "N7": "我那张警长票给了 N11，是想逼出硬信息，不是站死边，听完这轮我松动了。两点：其一，光凭她喊 N6、N3 必出就判她是狼，证据不够；手里捏着查杀的真预言家，本来也会着急；她的警徽流也选 N9，不能算撞车——靠后又没发言的座位，谁都会挑。其二，N6 的警长票给了替他报金水的 N3，放在两种假设下都解释得通，不是决定性的东西。我今天想压的是 N1：先称自己没有身份、打算退水，却又留在台上；转头自称带刀好人，还说夜里动刀——夜里动刀的是狼，这不像好人的视角。我的票倾向 N1，N6 的解释要是垮了就落 N6。",
    "N6": "我是个普通好人，没有身份。先答台下投票的事：当时只有 N3 给了完整的验人理由和评判标准，N11 是后手，一开口就要求先出 N6 和 N3，我据此选了 N3，不是抱团，我谁也不认识。你们都漏了一点：我和 N3 要是都是狼，狼队何必先让我显眼地领一张队友发的金水，再安排 N11 来查我？N11 要是真预言家，该去验 N9（她自己说的第一晚），而不是找一个已经领了金水的人来凑成对跳。她的顺序是先有结论再找理由，还拿恐吓来替代推理。我今天的票落 N11。N1 那套把 6 和 11 都放过的说法，我也不接受，出 N11 才是止损。",
    "N5": "我是平民，没有身份，我的警长票给了 N3，今天的票落在 N11。我只拿一条硬的出来讲：N11 自述的第一晚警徽流是 N9，实际报出来的却是 N6 的查杀。真预言家的计划和结果得对得上，她是先定下结论——要把 N3 的金水否掉——然后才找理由，N6 问得对。第二，她拿“明天逐个算”这种话吓全场，是狼在抢票权的姿态。我还盯两个位置：N1 说自己是带刀好人，随后又讲夜里动刀、把 6 和 11 都放过，动刀是狼的话，明天要处理她；N7 是唯一替 N11 说话的人，说“拿着查杀的人当然着急”，这我也留着。",
    "N4": "有个大家都在引用的论点是错的，先纠正：警徽流讲的是以后打算验谁，并非已经验过谁；所以拿 N11 先前说的第一晚计划去对她报的结果，算不上矛盾，N3 的 N9、N12 也一样；拿这个打 N11，逻辑不通。第二，我把警长票给 N11，并不是跟着势头走：先手发金水、后手报查杀，狼队要是这么配合，既白送一个狼、又拿不到警徽，没有动机，所以我对 N3 也留有余地。今天的处决，我不赞成落在 N11、N3 中的任何一个身上，错出一个，就是同时丢掉预言家和警徽，这个险不能冒。我主张今天先处理 N1：她先说自己没有身份、会退水，却留在台上，后来又自称带刀好人，还讲夜里动刀，这是全场唯一不像好人会说的话。",
    "N3": "警长归票。先接 N4 的质疑：他说得对，警徽流指的不是已经验过的人，这一点我不会拿来攻击谁。N4，请你看关键所在：N11 这一整天没有一条独立的信息，她的查杀出现在我报完 N6 的金水之后，是冲着我的结论来的；真预言家的推理应当来自她自己的验人，她的却是照着我的来。第二，换作我是狼，替队友报金水只会让他更显眼、更容易被刀，狼队不会这么安排；N6 在台下投我，是认可我的逻辑完整。第三，她那句“明天逐个算”是吓唬人，并不是在抓狼。N4 自己说这个险赌不起，那就更不该为了压 N1 放过 N11。N1 的身份说不清，明天我把她排在最前面处理。我的归票对象：N11。",
}
C_BALLOTS_SHERIFF = {"N4": "N11", "N5": "N3", "N6": "N3", "N7": "N11", "N8": "N11", "N9": "N3", "N10": "N3", "N12": "N3"}
C_BALLOTS_EXILE = {"N1": "N11", "N3": "N11", "N4": "N1", "N5": "N11", "N6": "N11", "N7": "N1", "N8": "N11", "N9": "N11", "N10": "N11", "N11": "N6", "N12": "N11"}


# ---------------------------------------------------------------------------------------------------------------------- the five
@dataclass(frozen=True)
class Point:
    key: str  # the id of the situation is l4-<key>
    game: str
    seq: int  # the entry of the saved game at which the seat was asked
    seat: str
    kind: str  # sheriff | exile
    title: str
    tests: str
    moment: str  # in words
    real_choice: str  # what the seat really did
    real_result: str  # what the table really did
    decisive: str
    trap: str
    evidence: tuple[str, ...]
    answer: tuple[str, ...] = ()  # (sheriff) the right vote; (exile) the wolves that are still in the game, filled in when it is built
    wrong: tuple[str, ...] = ()
    roles: dict[str, str] = field(default_factory=dict)  # the players the question is about
    notes: str = ""


POINTS = {
    "sheriff-blunt-seer-vs-fluent-wolf": Point(
        "sheriff-blunt-seer-vs-fluent-wolf", "0fa7", 103, "N7", "sheriff", "警长票：说得短的真预言家，对说得长的悍跳狼",
        "站边准确率：两个人都说自己是预言家，一个说得又短又直、验人理由很怪、警徽流含糊，一个说得长而有条理、报了一个查杀、还把警徽流排得整整齐齐；被报查杀的第三位候选人退了水。该投谁？",
        "第 1 天警长竞选：N1、N2、N6 上警，N1 和 N2 都自称预言家（N1 报 N8 金水，N2 报 N6 查杀），N6 自称平民并退水、请警下投 N1；警下 9 位玩家投票，被测座位是其中之一。",
        "N2", "警下 9 人里 8 人投了 N2（悍跳的狼）、1 人投了真预言家 N1；N2 当选警长，终局狼人阵营获胜。",
        "N1 才是预言家（她第一晚验的 N8 是好人，属实：N8 是猎人）；N2 是悍跳的狼，他报的“N6 查杀”是编的（N6 是平民）。",
        "N2 说得长、有结构、有一条能对上的警徽流，还把对手的毛病一条条挑出来；N1 说得短、验人理由很怪（“上一局是队友”）、警徽流含糊；N6 被查杀后退水并叫警下投 N1，容易被读成“被查杀的位置替对跳位拉票”。",
        ("昨晚我验了 8 号，是个好人", "第一晚我验的 N6，结果是狼", "请台下把票给 N1"),
        answer=("N1",), wrong=("N2",), roles={"N1": "seer", "N2": "werewolf", "N6": "villager", "N8": "hunter"},
        notes="第 1 夜平安（狼刀 N4，被女巫自救）。",
    ),
    "exile-follow-the-sheriff": Point(
        "exile-follow-the-sheriff", "0fa7", 168, "N7", "exile", "放逐票：十个人跟着悍跳狼的警徽走",
        "站边准确率：狼已经拿到警徽（8 比 1），真预言家发言激动、说不出理由，被查杀的平民被全场点名；十个人说“今天出 N6”。你投谁？",
        "第 1 天放逐投票：警长 N2（狼）归票出 N6；其余多数人发言都说跟着警长出 N6，N1（真预言家）喊着要大家把 2 号投出去，N6 自辩；12 人投票，被测座位是其中之一。",
        "N6", "10.5 票（含警长 1.5 票）投给了 N6（平民），N1 和 N6 投了 N2；N6 被放逐，终局狼人阵营获胜。",
        "N2 是悍跳的狼，N1 才是真预言家；N5、N9、N12 也是狼；N6 是平民。",
        "全场几乎说同一套话（“跟警长票出 N6”）；N1 的发言又急又乱，N2 的回应冷静而有结构；N1 的金水 N8 投了 N2，这被当成“金水链是假的”的证据（N8 是真的好人，他是凭逻辑投的）。",
        ("今天出 N6，明天再看 N1", "N8 自己却把票投给了 N2", "票型：N3→N1，N4→N2"),
        roles={"N1": "seer", "N2": "werewolf", "N5": "white_wolf", "N6": "villager", "N8": "hunter", "N9": "werewolf", "N12": "werewolf"},
        notes="第 1 夜平安。第 2 夜 N4（女巫）毒了 N1（真预言家）；N2 在第 2 天被放逐；狼人阵营最终获胜。",
    ),
    "sheriff-kill-check-vs-gold-check": Point(
        "sheriff-kill-check-vs-gold-check", "2b91", 106, "N9", "sheriff", "警长票：报查杀的真预言家，对发金水的悍跳狼",
        "站边准确率：一个预言家报了一个查杀（报错了自己就走），另一个预言家给别人发了金水（无法验证），第三位候选人退了水。该投谁？",
        "第 1 天警长竞选：N2、N6、N8 上警，N2 自称预言家（验 N5，金水），N6 自称预言家（验 N4，查杀），N8 退水并列出可核对的几点、不定票；警下 9 位玩家投票，被测座位是其中之一。",
        "N2", "警下 9 人里 5 人投了 N2（悍跳的狼）、4 人投了真预言家 N6；N2 当选警长；第 1 天白狼王自爆带走了 N6；终局狼人阵营获胜。",
        "N6 才是预言家（他报的查杀 N4 属实：N4 确实是狼）；N2 是悍跳的狼，他给 N5 的金水是随手发的一张好人牌（N5 是平民）。",
        "N2 的发言更长、更像在“立规矩”，把验人的理由和警徽流的排法都摆出来了；N6 更短更硬，还指出了 N2 警徽流的“省事”之处；N8 的退水发言看上去最中立、最有条理，容易让人觉得“两边都有道理”。",
        ("昨晚验了 N5，是好人", "第一晚我验的是 N4，结果是狼人", "我退出竞选，投票权交回台下"),
        answer=("N6",), wrong=("N2",), roles={"N2": "werewolf", "N4": "werewolf", "N5": "villager", "N6": "seer", "N8": "villager"},
        notes="第 1 夜平安（狼刀 N7，被女巫自救）。N4 的“查杀”是对的，但全场没有谁能当场验证。",
    ),
    "sheriff-opposite-checks-on-one-player": Point(
        "sheriff-opposite-checks-on-one-player", "e0f2", 116, "N2", "sheriff", "警长票：两个预言家对同一个人给出相反的结果",
        "站边准确率：两个人对同一个人（N6）给出相反的结果（一个金水、一个查杀），第三位候选人说自己没有身份却没有退水。该投谁？",
        "第 1 天警长竞选：N1、N3、N11 上警，N1 自称没有身份、说大概率会退水（但没退），N3 自称预言家（验 N6，金水），N11 自称预言家（验 N6，狼人阵营），并说 N3 是悍跳、请警下把票给她；警下 9 位玩家投票，被测座位是其中之一。",
        "N3", "警下 9 人里 6 人投了 N3（悍跳的狼）、3 人投了真预言家 N11、0 人投 N1；N3 当选警长；第 1 天放逐的是 N11；终局好人阵营获胜。",
        "N11 是真预言家（验 N6，狼人阵营——N6 确实是狼）；N3 是悍跳的狼，给自己的狼队友 N6 发了金水；N1 是白狼王（没有报身份）。",
        "N3 的发言更完整、给了“判断标准”，N11 一上来就报查杀、点名要先出 N6 和 N3，节奏很急；N1 的“没身份、观望、可能退水”看上去最无害，投给他的票是零。N6 本人是狼，对同一个人给出相反结果这件事，只有 N6 自己知道谁在说谎。",
        ("昨晚验的 N6，查验结果是好人，金水", "昨晚验了 N6，结果是狼人阵营", "没身份，先观望"),
        answer=("N11",), wrong=("N1", "N3"), roles={"N1": "white_wolf", "N3": "werewolf", "N6": "werewolf", "N11": "seer"},
        notes="第 1 夜平安（狼刀 N5，被女巫救下）。N1（白狼王）在警上没有报身份。",
    ),
    "exile-true-seer-on-trial": Point(
        "exile-true-seer-on-trial", "e0f2", 181, "N2", "exile", "放逐票：真预言家被全场点名，N1 说了一句只有狼才会说的话",
        "站边准确率：警徽在悍跳狼手里，真预言家 N11 被整桌盯着；N1 在发言里说了一句只有狼队才会说的话（夜里让好人动刀）。你投谁？",
        "第 1 天放逐投票：警长 N3（狼）归票 N11；N12、N10、N9、N8、N7、N6、N5、N4 先后发言，其中 N4、N7 想先处理 N1，其余多数倾向 N11；12 人投票，被测座位是其中之一。",
        "N11", "N11 得 9.5 票（含警长 1.5 票）被放逐——他是真预言家；N4、N7 投了 N1（白狼王）；终局好人阵营获胜。",
        "N1 在发言里主张“夜里让好人动刀”——动刀是狼队才会说的话；N1 是白狼王，N3、N6、N8 也是狼；N11 是真预言家。",
        "N3 的归票有理有据地攻击 N11（“威胁代替逻辑”“顺着我的结论报查杀”）；N11 的发言带着火气，又喊“明天逐个算”，很像狼抢节奏；N1 的那句话只被两个人抓住。",
        ("夜里让好人动刀", "我的归票对象：N11", "票型：N2→N3"),
        roles={"N1": "white_wolf", "N3": "werewolf", "N6": "werewolf", "N8": "werewolf", "N11": "seer"},
        notes="N11 被放逐后留了遗言；好人最终获胜。",
    ),
}


# ---------------------------------------------------------------------------------------------------------------------- building
def _table(game: str, seat: str, seed: int) -> Table:
    t = Table(ROLES[game], bot=seat, seed=seed)
    t.open()
    return t


def _wolves(t: Table) -> list[str]:
    return [s for s in by_seat(t.alive) if t.roles[s] in WOLF_ROLES]


def build_a(point: Point) -> Table:
    t = _table("0fa7", "N7", 4101)
    t.night(1, guard="N3", wolf="N4", seer="N8", witch="save")  # the wolves attacked the witch; she saved herself: a quiet night
    expect_dead(t)
    if point.kind == "sheriff":
        t.election(
            candidates=["N1", "N2", "N6"], turns=A_ELECTION, quit=["N6"], ballots={s: v for s, v in A_BALLOTS_SHERIFF.items() if s != "N7"}, decide="N7", say_quit=False,
        )
        return t
    t.election(candidates=["N1", "N2", "N6"], turns=A_ELECTION, quit=["N6"], ballots=A_BALLOTS_SHERIFF, say_quit=False)
    t.dawn(1)
    t.talk(1, A_DAY1, direction="警左", fill=False)
    t.decision_vote(1, A_BALLOTS_EXILE)
    return t


def build_b(point: Point) -> Table:
    t = _table("2b91", "N9", 4103)
    t.night(1, guard=None, wolf="N7", seer="N4", witch="save")  # the wolves attacked the witch; she saved herself: a quiet night
    expect_dead(t)
    t.election(
        candidates=["N2", "N6", "N8"], turns=B_ELECTION, quit=["N8"], ballots={s: v for s, v in B_BALLOTS_SHERIFF.items() if s != "N9"}, decide="N9", say_quit=False,
    )
    return t


def build_c(point: Point) -> Table:
    t = _table("e0f2", "N2", 4104)
    t.night(1, guard=None, wolf="N5", seer="N6", witch="save")  # the witch saved the target of the wolves: a quiet night
    expect_dead(t)
    if point.kind == "sheriff":
        t.election(candidates=["N1", "N3", "N11"], turns={s: C_ELECTION[s] for s in ("N1", "N3", "N11")}, ballots=C_BALLOTS_SHERIFF, decide="N2", say_quit=False)
        return t
    t.election(candidates=["N1", "N3", "N11"], turns=C_ELECTION, ballots={**C_BALLOTS_SHERIFF, "N2": "N3"}, say_quit=False)
    t.dawn(1)
    t.talk(1, C_DAY1, direction="警右", fill=False)
    t.decision_vote(1, C_BALLOTS_EXILE)
    return t


BUILDERS = {"0fa7": build_a, "2b91": build_b, "e0f2": build_c}


def build(key: str) -> tuple[str, dict[str, Any]]:
    point = POINTS[key]
    t = BUILDERS[point.game](point)
    seat = point.seat
    answer, wrong = list(point.answer), list(point.wrong)
    if point.kind == "exile":
        answer = _wolves(t)
        wrong = [s for s in by_seat(t.alive) if t.roles[s] not in WOLF_ROLES]  # (every good player, the seat under test itself included)
    label = "badge" if point.kind == "sheriff" else "vote"
    result = WINNER[point.game]
    truth: dict[str, Any] = {
        "request": f"@{label}", "answer": answer, "wrong": wrong, "kind": "side", "decisive": f"存档终局揭晓的真相：{point.decisive}", "evidence": list(point.evidence),
        "trap": point.trap, "roles": dict(point.roles), "source": f"本机存档 {point.game} 的第 {point.seq} 条记录（脱敏重写）",
        "notes": f"存档里的真实走向：{point.real_result}" + (f" {point.notes}" if point.notes else ""),
    }
    meta = {"source": point.game, "seq": point.seq, "real_choice": point.real_choice, "real_ballots": dict(_real_ballots(point)), "winner": result}
    data = t.situation(
        sid=f"l4-{key}", title=point.title, tags=[SUITE, "real", point.kind, "side"], decision={"seat": seat, "expect": ["respond"]}, truth=truth,
        brief=f"{point.moment} 被测座位是 {seat}（{t.role_of(seat)}）。", meta=meta,
    )
    header = "\n".join([
        f"L4 · {point.title}",
        f"在测什么：{point.tests}",
        f"被测座位：{seat}（{t.role_of(seat)}）；12 人暗牌局（werewolf_12p 2.2.0）。局面：{point.moment}",
        f"来源：本机存档 {point.game} 的决策点 #{point.seq}。保留的是结构——座位和真实身份、谁跳了预言家、报了什么查验、谁退水、其余玩家的票型和立场；"
        "所有发言都是重写的（脱敏）：没有存档里的原话，名字用默认名。",
        f"存档终局揭晓的真相：{point.decisive}",
        f"正确的{'投票对象' if point.kind == 'exile' else '警长票'}：{'、'.join(answer)}（truth.answer）；"
        + (f"投给 {'、'.join(wrong)} 是站错了边（truth.wrong）。" if point.kind == "sheriff" else "投给好人阵营的任何一人（含被测座位自己）都是站错了边（truth.wrong 列着他们）。"),
        f"陷阱：{point.trap}",
        f"真实走向：{point.real_result}（被测座位在存档里选了 {point.real_choice}。）",
        "本文件由 bench/cases/l4.py 生成，不要手改：改代码后运行 `python -m bench fixtures` 重写。",
    ])
    return header, data


def _real_ballots(point: Point) -> dict[str, str]:
    """The ballots of the others at the decision point, as the real game cast them."""
    table = {
        ("0fa7", "sheriff"): {s: v for s, v in A_BALLOTS_SHERIFF.items() if s != "N7"}, ("0fa7", "exile"): {s: v for s, v in A_BALLOTS_EXILE.items() if s != "N7"},
        ("2b91", "sheriff"): {s: v for s, v in B_BALLOTS_SHERIFF.items() if s != "N9"},
        ("e0f2", "sheriff"): dict(C_BALLOTS_SHERIFF), ("e0f2", "exile"): dict(C_BALLOTS_EXILE),
    }
    return table[(point.game, point.kind)]


def cases() -> list[Case]:
    return [Case(f"l4-{key}", SUITE, f"situations/l4-{key}.yaml", (lambda k=key: build(k))) for key in POINTS]


__all__ = ["POINTS", "ROLES", "WINNER", "Point", "build", "cases"]
