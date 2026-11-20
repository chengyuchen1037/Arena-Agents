# bench：v0.14 的基准与验收

v0.14 的目标是两件事：**各玩家的发言风格不同**（不再像写工作报告），**抓住核心逻辑**（依据硬事实判断，不被“讲得有条理”的发言带走）。
这里的东西用来**量化**它们：同一套度量，先在 v0.13.1 的真实对局上测出基线，再在 v0.14 上看变化，验收门槛写在 [gates.yml](gates.yml)。

## 现在有什么

| 部分 | 文件 | 说明 |
|---|---|---|
| 读存档 / 日志 | [loaders.py](loaders.py) | `game.json` 的账本、发言（标出人类接管时段的发言）、`trace-*.jsonl` |
| 度量 R / S1 / S3 | [metrics.py](metrics.py) | 纯函数：截断、被退回的次数与重写成功率、发言权超时、发言耗时、token；编号 / 行内编号 / markdown / 自报座位、长度（对照 `篇幅：a–b 字`）；跨座位共用的片段与套话 |
| 局面脚本 | [situation.py](situation.py)、[fixtures/situations/](fixtures/situations/) | YAML：用真实引擎搭一个世界，停在一个决策点。格式见下面的 [局面脚本](#局面脚本situation)，示例 `_example*.yaml` |
| 题库 | [fixtures/](fixtures/)、[cases/](cases/)、[authoring.py](authoring.py) | 93 个静态 YAML 题目（R / S2 / L1 / L2 / L3 / L4 / M2 / M3）；`cases/` 是写它们的代码（作者工具），测试保证两者一致。清单见 [题库清单](#题库清单) |
| 套件 | [suite.py](suite.py)、[suites/](suites/) | 14 个套件的定义（题目的 glob、bot、每档的采样数、裁判、门槛）；`bench suites` 列出，`bench run --suite L1 --tier quick` 运行 |
| 搭世界 | [play.py](play.py) | 把脚本的每一步当作 intent 提交给 `World`（模型的工具调用走的同一条路），找出叫醒被测座位的是什么 |
| bot | [bots.py](bots.py) | 要比的 bot：v0.13.1 的玩家（`LegacyBrain`）、v0.14 的 bot（`BotBrain`，来自默认库 / 一个目录 / 题目里的快照）、逐层消融；`@history=6000` 把两种 bot 的可见历史收窄到这么多字（M2 用） |
| runner | [runner.py](runner.py) | bot × 题 × 采样 → 真实的 `Agent` 循环，直到 bot 自己行动 → `results.jsonl`、`judge_items.jsonl`。见 [runner](#runner) |
| 模型 | [fakes.py](fakes.py)、[realllm.py](realllm.py)、[safety.py](safety.py) | 假模型（CI，默认）；真实模型（默认关，要 `--real` 和 `--max-calls`）；真实 `data/` 和 Key 的保护 |
| 汇总 | [summary.py](summary.py) | `results.jsonl` 的 R / S1 / S3 / 正确率，按 bot 和整体 |
| 存档 → 脚本 | [replay.py](replay.py) | 把存档的账本转回脚本，在真实引擎上重放并比对；M0 的可行性验证，结论见 [下面](#从存档做局面脚本m0-可行性验证) |
| 命令行 | [\_\_main\_\_.py](__main__.py)、[cli.py](cli.py) | `python -m bench audit` / `baseline` / `situations` / `run` / `summarize` / `replay` / `suites` / `fixtures` |
| 基线 | [reports/baseline-v0.13.1.json](reports/baseline-v0.13.1.json) | v0.13.x 的 8 局真实对局（deepseek-flash）的汇总数字，**不含对局本身** |
| 门槛 | [gates.yml](gates.yml) | 每条门槛：度量、比较、数值、基线、状态（provisional / pending_baseline） |

其余套件（S2 风格可辨识、L1–L4 核心逻辑题、M1–M3 记忆层、E1–E2 学习、G 联赛）的**题目、裁判与标注页**在 `fixtures/`、`judges/`、`label/`，
见计划 [§6.5](../docs/v0.14-plan.md) 和契约 [§13.11](../docs/v0.12-contracts.md)；它们的裁判是 DSH 子 agent（不是玩家模型），评分标准在 `judges/`，人工标注页在 `label/`。
runner 为它们产出 `judge_items.jsonl`，格式见 [裁判条目](#judge_itemsjsonl裁判条目)。

## 怎么跑

```bash
# 一局存档的数字（trace 可选）
python -m bench audit --game data/games/<id>/game.json --trace data/logs/<id>/trace-0001.jsonl

# 一个数据目录下所有存档的汇总（可以给多个目录），写成报告
python -m bench baseline --data-dir data --data-dir .demo-data/real --out bench/reports/baseline-v0.13.1.json

# 局面脚本：列出 / 检查（建世界，看是什么叫醒被测座位）。以 _ 开头的是示例，默认不跑：加 --include-examples
python -m bench situations --include-examples --check

# 冒烟：假模型（默认），0 次模型调用。结果写到 .pytest_tmp/bench-runs/<run id>/（--out 可以改）
python -m bench run --include-examples --bots legacy,bot:Alice --samples 2 --fake mixed

# 真实模型：必须显式 --real 并给 --max-calls；Key 放在环境变量里，不要写在命令行
export ARENA_BENCH_API_KEY=...
python -m bench run --situations bench/fixtures/situations --bots default12 --samples 3 \
    --real --max-calls 400 --model <模型 id> --base-url https://<服务地址>/v1 --out <结果目录>
python -m bench run ... --out <同一个目录> --resume          # 中断后续跑：已经做完的样本不重做

# 一个结果文件的数字；一局存档能不能转成脚本
python -m bench summarize --results <结果目录>/results.jsonl
python -m bench replay --data-dir data                          # 只读：data/games 下每一局能否逐条重放
python -m bench replay --game data/games/<id>/game.json --list-decisions
python -m bench replay --game data/games/<id>/game.json --upto <序号> --seat <座位> --id <题目 id> --emit <文件.yaml>

# 套件：列出（题数、每档的样本数和预计调用数）/ 按定义运行一档（--real 的规则不变）
python -m bench suites --check
python -m bench run --suite L3 --tier smoke          # smoke | quick | full；结果写到 .pytest_tmp/bench-runs/<run id>/
# 题库：题目文件是静态的；改了 bench/cases 里的代码之后重写，--check 只比对
python -m bench fixtures [--check]
# 脱敏检查：题库里没有存档里任何玩家的原话（只读；对拍本机存档）
python -m bench.cases.leaks --games data

# 校准裁判：先从运行里抽每个 rubric 30 条（盲化）给人标，再算人工 ↔ 裁判的 κ（细节见 judges/calibration/README.md）
python -m bench calibration-set --items judge_items.jsonl [--judgments judgments.jsonl] --n 30 --out set.jsonl
#   标注页：浏览器直接打开 bench/label/index.html，选择 set.jsonl；标完导出 labels.jsonl
python -m bench calibrate --labels labels.jsonl --judgments judgments.jsonl --out calibration.json

# 出报告：数字 + 门槛（见下面的“出报告与门槛”）
python -m bench report --results <运行目录>... --calibration calibration.json --out bench/reports/<版本>/

# 测试（不调任何模型）
python -m pytest tests/bench
```

成本分三档：**冒烟**（CI 里跑，0 次模型调用，即上面的测试）、**快速**（约 300 次调用，改提示词 / 记忆之后自查）、**完整**（数千次调用 + 5–10 局联赛，发版用）。

**哪些在假模型上就能跑，哪些要真模型和裁判**：R / S1 / S3 的**计算**、runner 的全部流程（退回 → 重写、预算、续跑）、`judge_items` 的生成都在假模型上测过；
但假模型的发言**不是任何东西的度量**（行里写着 `"mode": "fake", "model": "fake"`）。S2、L1–L4、M1–M3、E1–E2、G 的**数字**要真实模型的行加上裁判的判断才有意义。

## 题库清单

题库在 [fixtures/situations/](fixtures/situations/)（93 个局面）和 [fixtures/pairs/](fixtures/pairs/)（L1 的 7 个成对文件）。**题目文件是静态的、可读的**：每个文件开头的注释写着它在测什么、局面是什么、陷阱在哪里、正确答案是什么；
`bench/cases/` 里的代码是写它们的**作者工具**（按主持人手册在真实引擎上“打”一局，见 [authoring.py](authoring.py)），`python -m bench fixtures` 重写、`--check` 比对，`tests/bench/test_bank.py` 保证“生成器写出的 == 已提交的文件”。
所以改题要改 `bench/cases/` 然后重写，不要手改 YAML（L4 的文字是手写的重写稿，也在 `cases/l4.py` 里）。全部题目都是 12 人暗牌局（`werewolf_12p` 2.2.0），默认名 Alice…Leo，座位号指人；每一步都被真实引擎接受过（`situations --check`）。

| 套件 | 题数 | 在测什么 | 度量 | 快速档（样本 / 调用） |
|---|---|---|---|---|
| **R** 运行健康 | 5 | 发言权每条只许 60–120 字（平时 300）：bot 第一稿写长了被退回（`TOO_LONG`），能不能在同一个发言权里重写成功、会不会被截断 | `runtime.speak.rewrite_success_rate`（R2）、`form.truncated_rate`（R1） | 10 / 16 |
| **S2** 风格可辨识 | 9 | 九个“轮到我发言”的时刻（开场第一句、有人刚走、被点名质疑、两个预言家对跳、警长竞选演说、被冤枉出局的遗言、猎人开枪之后、无事发生的第二天、平票 PK 辩护）：12 个 bot 坐同一座位各说一段，裁判拿 12 张风格卡（`voice.md` 三行，字母代替名字）去匹配，随机 1/12 | `style.top1_accuracy`（S2）；S1 / S3 用同一批发言算形态 / 同质化 | 60 / 75（12 个 bot） |
| **S1 / S3** | 借用 S2 | 发言形态（不分点、不用 markdown、不自报座位、篇幅）/ 同质化（跨 bot 共用 6 字片段、套话） | `form.*`、`homogeneity.*`、`cliches.*` | 不另算 |
| **L1** 表达不变性 | 7 对 = 14 | 同一个决定性事实，A 版狼说得长而有条理、好人说得短而直，B 版对调；看 bot 跟事实投还是跟“谁说得好听”投。七个事实家族：不可能的查验 / 金水自爆 / 竞选里自称猎人 / 被查杀的猎人 / 死人的票 / 声明与票型相反 / 同一夜两个说法 | `expression_bias` = P(信说得好的一方) − 0.5（L1）、`accuracy` | 28 / 31 |
| **L2** 琐碎陷阱 | 7 | 同样的决定性事实，但两边在首夜验谁、警徽流先后、措辞、字数上全不一样，旁观的好人还特意点出这个差别（红鲱鱼）；看 bot 会不会纠缠在无关的细节里 | `trivia_hit_rate`（裁判 L2，L2）、`accuracy` | 14 / 15 |
| **L3** 核心矛盾 | 9 | 决定性事实埋在局面里：不可能的查验、金水自爆、竞选里的假猎人、被放逐后开枪的“被查杀者”、死人的票、声明与票型相反，加三个私有视角（女巫发现公布前就知道的死讯 / 预言家的查验被人说反 / 守卫看穿同守同救） | `accuracy`、`decisive_hit_rate`（裁判 L3，L3） | 18 / 20 |
| **L4** 真实局面 | 5 | 从本机存档切的第 1 天决策点，**每个字都重写过**：三次警长票（短而直的真预言家 vs 长而有条理的悍跳狼 / 报查杀 vs 发金水 / 同一人被报金水又被报查杀）、两次放逐票（十个人跟着狼警长 / 真预言家被整桌盯着）；truth 取存档终局 | `side_accuracy`（L4：比基线高 20 点） | 10 / 11 |
| **M1** 分层消融 | 引用别的套件 | 同一批题，带不带某一层（L0 / L2 规则理解 / L2 打法 / L1 笔记 / L2 印象），配对比较 | `ablation.p_value`（M1） | 56 / 73 |
| **M2** 对局记忆召回 | 11 | 四天的长局（约 370 条账本）之后，一道关于**早先事实**的题：第 1 夜谁死、谁被放逐、某人投了谁、谁当选警长、警徽后来给了谁、猎人带走了谁、预言家报了谁（文字作答）、一共出局几个人（文字作答）、预言家自己第 1 夜验了谁。干扰项是同局别的事件的主角 | `recall.accuracy` | 22 / 22 |
| **M3** 规则理解 | 33 | 规则测验：同守同救、遗言资格、警长 1.5 票、平票 PK、被毒的猎人不能开枪、首日自爆吞警徽、胜负判定……四选一，答案位置 a–d 轮流 | `rules_quiz.accuracy`（M3：≥ 90%） | 34 / 34 |
| **E1 / E2 / G** | 不由 `run` 运行 | 学习曲线 / 向人类学风格 / 联赛：要整局对局和人的语料，做法写在各自的 `suites/*.yml` 的 `manual` 里 | | — |

**快速档合计约 297 次调用**（R 16 + S2 75 + L1 31 + L2 15 + L3 20 + L4 11 + M1 73 + M2 22 + M3 34；S1 / S3 用 S2 的发言，不另算；`bench suites` 里的“合计”重复算了它们）。
这是**每个 bot 一次采样**的数；套件文件里 `quick` 默认只带 `legacy` 和 `bot:Alice` 两个 bot。完整档每题 5 次采样、更多 bot，约 3500 次。预计调用数 = 样本数 × 每个样本的调用数（决策一次，被退回后重写再加一点；每个套件的 `calls_per_sample` 是估计）。

### 题目怎么写、怎么保证它是真的

- 每道题 = **一个真实引擎能走到的局面** + 一个**决定性事实**，事实必须**在被测座位自己的视野里能读到**（硬事实台、可见历史、私聊）：`truth.evidence` 里的原话，测试会在被测座位的板面上找。
- `truth.answer` 必须是那个请求的选项（选择题），L1 / L2 / L3 的答案必须是一个**还活着的狼**；成对题的两个文件只在带 `meta: {styled: true}` 的发言上不同；被测座位在脚本里从不发言（它的夜间行动和过去的投票是脚本写的，发言不是）。
- 干扰项（M3 的错误选项、M2 的别的事件的主角、L2 的琐碎差别）是**常见的误解**，不是随机凑的；M3 的答案位置轮流，位置不说明任何事。
- **脱敏**（L4，以及所有题目）：题目里没有存档里任何玩家的原话，也没有非默认名。检查函数在 [cases/leaks.py](cases/leaks.py)：玩家写的话（发言、作答的理由、自爆宣言、文字答复）和题目文件里作者 / 玩家的全部文字比较，**切自存档的题 10 字、其余 14 字的连续片段**不许重合（去掉标点、空格、数字；规则原文的片段不算）。
  测试里有一个内置的“泄漏样例”证明检查函数抓得到抄袭、放得过转述；`ARENA_BENCH_SAVED_GAMES=1 python -m pytest tests/bench/test_bank.py` 才拿本机存档对拍（默认跳过：存档是用户自己的数据，只读），并且把 L4 每题的板面（阶段、谁活着、请求和选项、硬事实台）和真实对局重放到那条记录处的板面逐项比对。
- **做一道 L4**：`python -m bench replay --game data/games/<id>/game.json --list-decisions` 找决策点；`--upto <序号> --seat <座位> --id <id> --emit <临时文件>` 写出**原始**局面（带存档原话，**不要提交**）；照着它的结构（谁跳了预言家、报了什么、谁退水、票型）在 `cases/l4.py` 里重写每一句发言，`python -m bench fixtures` 写出文件，`python -m bench.cases.leaks` 检查。

### 最弱处（哪里最容易被质疑）

1. **题目少、家族少**：L1 和 L2 用同一批七个决定性事实的家族，L3 用其中六个再加三个私有视角，只是“装扮”不同（说得好 / 琐碎差别 / 什么都没有）——不是互相独立的证据；L3 只有 9 题、L4 只有 5 题，快速档每题一次采样，准确率的置信区间会很宽（±30 点左右），要看 `full` 档和 bootstrap 区间，别拿单次数字下结论。bot（或它的记忆）如果见过这批题（比如把它们写进打法），数字就作废：**不要把题库里的局面当范例喂给 bot**。
2. **L1 / L2 的“说得好”“琐碎差别”是作者定义的**：说得长 / 有条理 vs 短 / 直，只改了被标记的发言；bot 可能在对“长度”而不是“流畅”起反应。L2 的数字只靠裁判（`trivia_hit_rate`），要 κ ≥ 0.6 才进验收；客观分（投对没有）和 L3 的能力混在一起。
3. **L3 的 `declared-vs-ballot` 最软**：它是行为上的矛盾（说投 A、票型里弃票），不是逻辑上的不可能；三个私有视角题（女巫 / 预言家 / 守卫）要 bot 真的在用自己的私有信息。
4. **L4 不是“真实语言”**：保留的是结构（真身份、谁跳了、报了什么、真实票型），语言是作者重写的——所以它量的是“真实局面的结构”，不是“真实发言的口气”。五题都在第 1 天、三局存档、被测座位都是平民；答案来自**终局**，不是视野里能推出的事实（真实的桌子也没推出来），所以门槛是“比基线高 20 点”，不是绝对值。
   **题很难，别美化**：五个局面里**真实的桌子全都站错了边**（狼拿了三次警徽，真预言家被投出去一次，好人被放逐一次）——选它们是因为这样的局面才有区分度，同时也意味着它们是“最难的真实局面”，不是“典型的真实局面”：
   跟着多数、跟着警长、跟着“说得有条理的人”投，在这五题上恰恰是错的，所以基线很可能接近甚至低于随机；bot 在这里的高低，要看和基线的差，不是看绝对的正确率，也不能说成“bot 能识破真实对局里的悍跳”。
5. **M2 靠 `@history=6000` 才有意义**：不收窄时整局（约 12000 字）刚好放得下，M2 退化成长提示的阅读理解。收窄之后，答案只在硬事实台里——`legacy` 没有它，**“提升”是设定里就有的**，M2 量的是 H10 硬事实台有没有用，不是一般的“记忆”；没有“bot 自己的笔记”的变体（脚本写不了 bot 的 `write_note`）。
6. **M3 是照规则文本写的**：规则也在 bot 的提示里，所以它量的是“读没读对”，不是“真懂”；四选一，随机 25%；错误选项是作者想到的常见误解，不是从 bot 的真实错误里挖的；33 题、一次采样，90% 的门槛区间约 ±10 点。
7. **S2 靠 voice.md 三行**：其他玩家的发言都是中性短句，所以 bot 的“个性”只能来自它自己的 `voice`；两个 bot 的 voice 相近（都“短”）就会被裁判混淆；随机水平 1/12（快速档若只带 3 个 bot 是 1/3，report 要写明）。S2 的数字要 κ ≥ 0.6 的裁判才进验收。
8. **R3（发言超时率）在这里量不出**：世界在决策点上冻住，bot 想多久都不会超时；整局的超时率要用 `bench audit` 看真实对局的账本和 trace。

## 局面脚本（situation）

一个局面是**被玩出来的**，不是描述给模型看的：用真实引擎建世界（`World.new_game`，固定 seed；`deal: start` 像 v0.14 的编排器那样在开局时发牌，
`deal: creation` 是旧路径），把 `steps` 一步步提交给它——主持人的步骤是 `mod.*` 意图，别的玩家的发言 / 作答以**该玩家的身份**提交，
和模型的工具调用走同一条路（同一个提交闸门、同一份回执）。世界停在**决策点**：被测的座位醒着，发言权在它手里，或者有一个请求在等它。
别的座位和主持人**不运行**——只有脚本里写好的动作。

完整的格式写在 [situation.py](situation.py) 的开头，带注释的示例在 [fixtures/situations/_example.yaml](fixtures/situations/_example.yaml)（投票，`respond`）、
[\_example\_speak.yaml](fixtures/situations/_example_speak.yaml)（轮到发言）、[\_example\_pinned.yaml](fixtures/situations/_example_pinned.yaml)（题目自带 bot）。骨架：

```yaml
format: arena-situation
version: 1
id: day-vote-1                 # [a-z0-9_-]+，不重复
title: 一句话说明
tags: [L3, vote]               # 套件（L1..L4、S2 ...）和自由标签
scenario: minimal_test         # scenarios/<id>.md
seed: 7                        # 世界的种子
deal: start                    # start（默认）| creation
players: {count: 4, names: [Alice, Bob, Carol, David]}   # 座位号 N1..Nn；被测座位会换成被测 bot 的名字
roles: {N1: red, N2: red, N3: blue, N4: blue}            # 明确指定每个座位的身份（或按座位顺序的列表）；不写就按 seed 发牌
steps:
  - {as: moderator, tool: create_channel, args: {channel_id: public, name: 公共频道, type: public, members: [N1, N2, N3, N4]}}
  - {say: {N1: "…", N2: "…"}, channel: public}
  - {as: moderator, tool: open_request, label: vote, args: {to: [N3], kind: choice, question: 投谁？, options: [{id: N1}, {id: N2}]}}
decision: {seat: N3, wake: redeliver, expect: [respond]}
bots: {N3: "bot:Alice@no-people"}  # 可选：这个座位由哪个 bot 来答（或一份内嵌的快照，见下）
brief: 给裁判看的说明（不给 bot 看）
score: [{kind: choice, request: "@vote", correct: [N1]}]   # 客观评分：respond 的值在 correct 里就算对（kind: text：写出来的答案要说出 contains、不能说 absent）
truth:                         # 什么是真的（不给 bot 看）：request + answer 就是客观评分，不用再写 score
  request: "@vote"             #   这个决定回答的是哪个请求
  answer: [N1]                 #   正确的选项（必须是那个请求的选项：测试会查）；match: text 时是答案里必须出现的词
  wrong: [N2]                  #   已知是错的选项 / 答案里不能出现的词
  decisive: "N7 称第 2 夜验了 N9，但 N9 第 1 夜已经出局。"   # 决定性事实（给裁判看）；evidence：被测座位能读到它的原话（测试会在它的视野里找）
  trap: "…"                    #   往错误方向拉的东西（L2 的琐碎差别、L1 的“说得好听”）；sides: {fluent: N7, blunt: N5}：L1 里谁说得好、谁说得直
judge: {L2: {…}}               # 每个 rubric 允许裁判用的事实；会原样放进裁判条目
meta: {pair: p1}               # 其他任何东西：成对题的编号、期望的一边……会原样放进每一行结果
```

| 步骤的写法 | 意思 |
|---|---|
| `{as: moderator｜N3, tool: …, args: {…}, at?, label?, meta?}` | 一次工具调用，`tool` 是模型用的名字（主持人：`announce` `send_message` `create_channel` `open_floor` `set_floor_queue` `open_request` ……；玩家：`speak` `speak_template` `pass` `respond` `use_ability` `challenge_moderator`） |
| `{say: {N1: 文字, …}, channel: 频道}` | 每个座位一次 `speak`，按写的顺序 |
| `{answer: r1｜@label, values: {N1: 值, …}, reasons?: {…}}` | 每个座位一次 `respond` |
| `{advance: 秒}` · `{tick: true}` | 游戏时钟往后走 / 触发到期的计时器 |
| `{running: false｜true, reason?}` | 暂停 / 继续对局（提交闸门关上 / 打开） |
| `{untimed: [座位]}` | 这些座位是真人在玩：它们的发言权没有倒计时 |

- `at`：把游戏时钟设到这个时刻（不能倒退），并先触发到期的计时器，再做这一步。`label`：给这一步的回执起名，之后 `"@label"` 代表它的 `request_id`（`@label.seq` 取回执里别的字段）。
- 文字里用座位号指人，不要用名字：被测座位用被测 bot 的名字，别的座位的名字来自 `players.names`（和被测 bot 重名的会被改名）。
- `decision.seat` 写一个座位；`decision.seats` 写几个：同一个局面对每个座位各跑一遍。`wake`：叫醒它的是什么——`redeliver`（默认：恢复对局时会再告诉座位一遍的东西：手里的发言权、没答的请求）、`{since: <label>}`（从那一步起发给它的所有信封）、
  或一个信封列表 `[{event: message.new, channel: talk, sender: N2}]`。决策点上没有任何东西能叫醒它，是错误（题目写错了），不是空跑。
  `expect`：接受了其中任何一个工具才算它做了决定；被退回的调用不算。
- **题目自带 bot**（`bots:`）：只有被测的座位需要 bot（别的座位不运行）。写成 spec 字符串（和 `--bots` 一样：`bot:Alice@no-people`、`legacy:Zed`、`bot:./minds/zed` 指题目文件旁边的 bot 目录），
  或者写成一份**快照**：`name`、`core`（L0：`identity` / `voice` / `mind`）、`notes`（L1）、`digest` / `playbook` / `questions`（这个剧本的 L2）、`people`、`record`——就是一局开始时 bot 带着的东西。
  内嵌的快照让题目不随 `bots/` 的变化而变化（M1 / M3 这类题需要）。有 `bots:` 的座位只由那个 bot 来答，不管 `--bots` 给了什么。
- 每一条错误都指明文件和哪一步（`example.yaml：steps[3]（N1 的 speak） 被引擎拒绝：NO_FLOOR：…`）；引擎拒绝了脚本里的一步，题目就停在那里，不会带着一个不完整的世界去测 bot。

## runner

`python -m bench run`（[runner.py](runner.py)）对每个 **(bot × 题 × 座位 × k 次采样)**：

1. 建世界，走到决策点（[play.py](play.py)）；
2. 给被测座位装上这个 bot 的 Brain：`BrainFactory = Callable[[seat, World, ScenarioSpec], Brain]`——基线 `LegacyBrain`（v0.13.1 的玩家），被测 `BotBrain`；
3. 启动**真实的 `app.runtime.agent.Agent`**（对局里同一个类、同一个 Worker 循环、同样的完整开场 / 增量消息 / 事件提示 / 回执），用决策点上叫醒它的信封唤醒它；
4. 它自己调用工具，直到：接受了 `decision.expect` 里的一个工具（`speak` / `pass` / `respond` …，**结束**）；被退回的（`FORMAT` / `TOO_LONG` / `NO_FLOOR` …）不算——bot 自己重写，重写也在记录里；
   或者它睡了 / 只回了文字 / 超过 `--max-rounds`（每个样本的模型调用数，默认 12）/ `--timeout`（每个样本的秒数，默认 300）/ 预算；
5. 记录：全部工具调用与回执、被退回的尝试、思考、独白、每次调用的耗时与 token、挂载清单与事件提示、提示的哈希，然后写一行 `results.jsonl`。

**H18：基准里的发言必须由 bot 自己产生。** 这里的保证是这样落实的：

- `bench/` 里只有 `StepRunner.run`（[play.py](play.py)）向世界提交意图，而且只用于**脚本里写好的动作**；runner、bots、假模型、汇总、命令、真实模型端口里没有 `submit(` 也没有 `Intent(`——`tests/bench/test_runner.py` 用源码检查守着；
- 模型只看到 brain 自己的消息（完整开场、增量、提示、工具回执、没有工具调用时的提醒）——没有“请写一段发言”这种额外的提示（测试检查）；
- 每个样本记 `integrity.speech_verbatim`：进入账本的发言 = 模型这次 `speak` 调用里的文字（提交闸门只去掉首尾空白，别的什么都不做）；
- 被退回的发言和 bot 自己的重写都完整留在行里（`attempts`）。

| `status` | `stop` | 意思 |
|---|---|---|
| `ok` | `decision` | bot 做了决定：`decision` 里是它的第一个被接受的动作 |
| `no_decision` | `slept` / `no_tool` / `nothing` / `stale` | 它睡了 / 只回了文字（独白）/ 没有要做的事 / 局面变了：没有决定，也没有发言 |
| `incomplete` | `max_rounds` / `timeout` / `budget` | 限额到了，样本没做完（`--resume` 会重做） |
| `error` | `error` / `scene` / `bot` | 模型一直出错（agent 重试后放弃）/ 题目建不出世界 / 这个 bot 造不出来：行里写着原因，别的样本照跑 |

**限制**（有意的简化，写在这里免得被当成整局的数字）：

- 世界在决策点是**冻住的**：游戏时钟不走（发言权不会在 bot 思考时超时；`elapsed_ms` 如实记下每次调用和整个样本的用时，可以据此判断“来不来得及”），别的座位不说话（思考时没有新消息插进来）。
  整局的运行健康（发言权超时率、整局的 token）要用整局的联赛（G）或真实对局的存档（`audit` / `baseline`）来测。
- 一个样本只到 bot 的**第一个被接受的动作**为止。多步的决策（先发言、再投票）拆成两道题；一个决策点上同时有几个待答的请求时，用 `expect` 和 `score` 指明要看哪一个。
- 采样之间**不共享任何状态**：每个样本重新建世界、重新造 brain，bot 在一个样本里记的笔记不会带到下一个。

### 两种模型

- **假模型**（默认，CI）：[fakes.py](fakes.py)。`--fake prose|listy|prefixed|long|sleepy|mixed`：读**世界**（不读提示）决定调哪个工具——`listy` 先写一段分条的再写口语的、`long` 先写一段超长的，
  让 `FORMAT` / `TOO_LONG` 退回和重写这条路径被走到；`sleepy` 从不行动。**真实的 Agent 循环、brain、提交闸门、回执照常运行，只有模型的话是预先写好的。**
  行里写着 `"mode": "fake", "model": "fake"`：这样的行**什么都没有测**，只说明流水线通。
- **真实模型**（默认关）：只有 `--real`，并且同时给 `--max-calls N`（整个运行的模型调用上限，大于 0）、`--model`、`--base-url`（或环境变量 `ARENA_BENCH_MODEL` / `ARENA_BENCH_BASE_URL`），
  Key 放在一个环境变量里（默认 `ARENA_BENCH_API_KEY`，`--api-key-env` 改名；**这个参数只收变量名，不收 Key**）。其余：`--protocol`、`--reasoning`、`--context-window`、`--call-timeout`、`--attempts`、`--price-in` / `--price-out`（给了就在 `run.json` 里估一个费用上限）。
  想沿用 app 里已经配好的 provider：`--providers-file <providers.json> --provider <id>`（`--model` 不写就用它的第一个模型）——地址、协议、模型的上下文窗口和价格从这个**你指定的文件**里**只读**地取，
  命令行上写的参数优先；**Key 仍然只从环境变量来，文件里的 `api_key`（app 自己也忽略）从不使用**；app 的 Provider 存储（数据目录里加密的 Key）仍然不碰。

  真实模式的保护（[realllm.py](realllm.py)、[safety.py](safety.py)，`tests/bench/test_realllm.py`、`test_safety.py`、`test_cli.py`）：
  - **不打开 app 的 Provider 存储**（它在数据目录里，Key 是加密的）：用引擎自己的 `LLMClient`（协议适配、流式、重试、思考深度都和真实对局一样），给它一个只有这一个 provider 的内存 store；provider 的值来自命令行 / 环境变量，或来自你用 `--providers-file` 明确指定的文件（只读，不含 Key）；
  - **不碰真实的 `data/`**：运行期间 `ARENA_DATA_DIR` 指向输出目录里的一个临时子目录（结束后删除并还原）；`--out` 是（或包含）`data/`、`.demo-data/`、启动时 `ARENA_DATA_DIR` 指的目录，会被拒绝；
  - **不合格的参数在创建任何目录、做任何连接之前就被拒绝**（缺 `--max-calls`、缺 Key、地址不是 http(s)://、把 Key 当变量名……）；
  - **预算**：每次模型调用之前先过 `Budget`；一个样本要先预留好第一次调用才会开始，所以同时跑的样本不会都在最后几次调用上开始、再卡在闸门上；用完就停。没有计数的调用不存在——客户端对 429 / 5xx 的重发不计（最多 `--attempts` 次 HTTP 请求，没有返回内容的请求不计费）；
  - **Key 不出现在任何结果、日志和报错里**（`scrub`）；Base URL 只记主机、端口和路径（去掉 `user:pass@` 和查询串）；
  - 测试里**从不调真实模型**：真实模式只在“替换了传输层”的桩上测。

### 输出

`--out <目录>`（默认 `.pytest_tmp/bench-runs/<run id>/`，git 忽略）里有：

| 文件 | 内容 |
|---|---|
| `results.jsonl` | 每个 (bot, 题, 座位, 采样) 一行，边做边写（中途死掉也留着已经做完的） |
| `judge_items.jsonl` | 每个需要裁判的 (行 × rubric) 一条 |
| `run.json` | 这次运行：模式和模型（不含 Key）、配置、题目文件的哈希、用到的 bot（只有大小和哈希，没有记忆的文字）、状态计数、模型调用数、token、用时 |
| `summary.json` | `summarize` 的数字（按 bot 和整体） |
| `prompts/*.md` | `--dump-prompts` 时，每个样本的整段对话 |

### `results.jsonl`（一行）

`format: "arena-bench-result"`, `version: 1`。

| 字段 | 内容 |
|---|---|
| `run` `bot` `brain` `situation` `seat` `sample` `scenario` `seed` `tags` `meta` | 这是哪一次、谁、哪道题、哪个座位、第几次采样；`bot` 是 spec 字符串（结果里 bot 的名字），`brain` 是 `LegacyBrain` / `BotBrain`；`meta` 是题目的 `meta` |
| `mode` `model` | `fake` / `real` 和模型名（假模型是 `fake`） |
| `status` `stop` | 见上表 |
| `decision` | 第一个被接受的动作：`tool`、`i`（第几次调用）、`round`、`seq`（账本序号）、`channel_id`、`request_id`、`text`（**进账本的发言**）、`value` / `reason`（作答）、`ability_id`、`template_id` / `params`、`attempt`（它之前被退回了几次）、`rewrote`（之前被 `FORMAT` / `TOO_LONG` 退回过）；没有则 `null` |
| `correct` `score` | 题目有 `score` 且这是 `respond`：`correct`（对 / 错）和 `score`（`kind` `request` `value` `correct` `wrong`）；否则 `null` |
| `attempts` | bot 的每一次**行动尝试**，按顺序，被退回的也在：`tool` `ok` `code` `message`，发言带 `channel_id` `text` `chars`，作答带 `request_id` `value` `reason`，被 `FORMAT` 退回的带 `rules`（哪几条） |
| `tool_calls` | 全部工具调用（笔记、`read_contract` 也在）：`i` `round` `name` `args` `ok` `code` `receipt`（走过提交闸门）`result`（回执的前 600 字） |
| `refusals` | 被退回的次数，按回执码：`{"FORMAT": 1}` |
| `speech` | 决定里的发言的 S1 数字：`chars` `problems`（闸门的哪几条：`list` `markdown` `prefix` …）`inline_enum` `paragraphs` `band`（它的 `voice` 里写的 `篇幅：a–b 字`）`in_band`；不是发言则 `null` |
| `integrity` | `speech_verbatim`：账本里的发言 = 模型写的（H18 的检查）；没有发言则 `null` |
| `runtime` | 这个样本的 R 数字（`bench.metrics.runtime_health`）：从账本数出的 `agent_speeches` `truncated_speeches`（应为 0）、`floor_ends`，从 trace 数出的 `tool_calls` `refusals` `speak`（`attempts` `accepted` `too_long` `format` `rewrite_success_rate`）`speak_turn_ms` `tokens` |
| `usage` `rounds` `elapsed_ms` | 模型调用数和 token（`llm_calls` `prompt` `cache_read` `completion`）、轮数、用时 |
| `thoughts` `monologues` `errors` | 模型的思考（可能被截到 6000 字）、没调工具时的文字、出错记录（已去掉 Key） |
| `mounts` `hints` | BotBrain 的挂载清单（`id` `layer` `chars` `sha`）和决策时的事件提示（`tags` `entries`）；LegacyBrain 没有，是 `[]` |
| `prompt` | `system_sha` `system_chars` `first_sha` `first_chars` `tools` `tools_sha`：提示的哈希和大小（不存全文；要全文用 `--dump-prompts`） |
| `wake` `scene` `name` | 叫醒它的事件、决策点上世界账本的指纹（同一题同一 bot 每次相同）、被测座位用的名字 |

### `judge_items.jsonl`（裁判条目）

`format: "arena-judge-item"`, `version: 1`。每个 `status: ok` 且题目有 `judge:` 的行，对 `judge:` 里的每个 rubric 出一条。

| 字段 | 内容 |
|---|---|
| `item_id` `blind_id` | `item_id` = `<run>/<题>/<座位>/<bot>/<采样>/<rubric>`（**带着 bot 的名字**）；`blind_id` = 它的哈希前 12 位，给裁判 / 标注页用来指这一条 |
| `rubric` | 评分标准的名字（`S2` `L1` `L2` `L3` `M3` …，`judges/<rubric>.md`） |
| `run` `situation` `seat` `bot` `sample` `meta` | **真值和出处**：谁做的、哪次运行、题目的 `meta`（成对题的编号和边）。**不要给裁判看** |
| `title` `brief` `tags` | 题目的标题、给裁判的说明（什么是对的、局面是什么）、标签 |
| `context` | 裁判可以知道的：题目 `truth:` 里的 `decisive`（决定性事实）和 `trap`（容易被带偏的地方），加上题目为这个 rubric 写的事实（`judge: {L1: {…}}` 的内容原样放进来）。**不含答案、不含谁是谁**（否则裁判会因为“这个决定是对的”而偏向）。`S2` 还有 `cards`：这次运行里**有 voice 的 bot** 的风格卡，`[{id: "A", text: voice.md 的三行}, …]`——字母代替名字，每条的顺序各自随机（用 `blind_id` 做种子），卡里不带 bot 名 |
| `truth` | **真值**（不给裁判看）：`answer`（题目的正确选项）、`correct`（这条决定对不对）；`S2` 还有 `bot`（是谁说的）和 `cards`（`{字母: bot 名}`）。bot 没有 voice（`legacy`）的 S2 行**不出条目**——没有风格可认 |
| `subject` | 被评的东西：`kind`（`speech` / `choice` / `pass` …）、`tool`、`text`、`value` `reason`、`thought`（它的思考）、`attempts`（被退回的尝试：能看出是哪种 brain） |

**盲评**：`blind_view(item)`（[runner.py](runner.py)）给出裁判可以看的部分——`blind_id`、`rubric`、`title`、`brief`、`context`、选定的 `subject` 字段（默认不含 `attempts`）——
不含 bot、运行、采样、题目 id、`truth`。`subject` 里 bot 自己写的话（`text` `reason` `thought`）已经把它的名字和 label 换成了座位号（“我是 Alice” → “我是 N3”）。S2 用 `blind_view(item, subject=("kind", "text"))`：风格只从文字判断。
裁判的判断用 `blind_id` 接回条目，再对 `truth` 打分（S2：`truth.cards[判定的字母] == truth.bot`）。

### 裁判（judges）

裁判是 **DSH 的子 agent**（不是被测的模型），评分标准是 [judges/](judges/) 里的 `<rubric>.md`（front matter：版本号、判定 schema、主指标；正文：怎么判、示例判例）。流程：

```bash
python -m bench judge-check                                   # 检查 rubric 文件：schema 在 DSH workflow 工具允许的子集内、每份至少 3 个示例且示例是合法判定
python -m bench judge-pack  --items <运行目录>/judge_items.jsonl --out packs/    # 每个 rubric 一批 ≤ 40 条：每条是一整段盲评提示（评分标准 + blind_view，没有 bot / 运行 / 题目 id / 真值）
#  在 DSH 里对 packs/ 里的每个文件各跑一次 workflow 工具：
#    script = judges/judge.workflow.js 的内容，meta = {"name": "bench-judge", "description": "...", "phases": [{"title": "judge"}]}，
#    args   = {"pack": <该文件的内容>, "concurrency": 2}      → 返回值存成 verdicts-<rubric>-<批>.json
python -m bench judge-merge --items <运行目录>/judge_items.jsonl --verdicts verdicts-*.json --out judgments.jsonl   # 每条判定先按 rubric 的 schema 校验，再用 blind_id 接回条目
```

- `judge.workflow.js` 对每条开一个子 agent（失败重试一次，仍没有判定的列在 `errors`，从不编造）；**子 agent 的数量受 DSH 的“活动子 agent 上限”约束（默认 3，与会话里的其他 agent——包括队友——共用）**，所以脚本把条目分给 `concurrency` 条“车道”（1–3，默认 2）顺序处理，
  超出上限时报 `subagent limit reached`：等别的 agent 结束再跑，或传 `concurrency: 1`；已有判定的条目在返回值里，没有的在 `errors` / `notes`，重跑那一批即可。
- 盲评靠提示词里的要求（“不要使用任何工具、不要读文件、不要猜出处”）和提示里**本来就没有**这些信息；子 agent 在用户的 DSH 会话里运行，理论上能读到仓库——所以请在**干净的会话**里跑裁判，并且不要把 `judge_items.jsonl` / `results.jsonl` 放在它能读到的位置（`packs/` 里只有盲评提示）。
- rubric 的版本变了，旧的判定就不能再合并（`judge-merge` 会要求重新打包）。校准（人工标注 ↔ 裁判的一致性 κ）见 [judges/calibration/README.md](judges/calibration/README.md)（`python -m bench calibration-set` 抽 30 条、标注页 [label/index.html](label/index.html)、`python -m bench calibrate` 算 κ）；报告怎么用校准的结果见 [出报告与门槛](#出报告与门槛)。

### 数字

`python -m bench summarize --results <结果>/results.jsonl`（也是 `run` 结束时打印的表）：按 bot——有决定的比例、一次过的比例、被 `FORMAT` / `TOO_LONG` 退回的次数、重写成功率、编号 / markdown / 自报座位 / 在篇幅内的比例、选择题的正确率、模型调用数、发言逐字一致；
整体——跨 bot 共用的 6 字片段比例（S3）、套话命中。门槛对照 [gates.yml](gates.yml)。

## 出报告与门槛

**你要做的事**（假模型上能做的 CI 里都在跑；下面这些要你自己的模型 Key、预算和你的判断）：

1. 用真实模型跑要验收的套件，每个套件一个结果目录：`python -m bench run --suite L3 --tier quick --real --max-calls 400 --model … --base-url … --out runs/l3`（`quick` 默认带 `legacy` 和 `bot:Alice`：基线和候选在同一批题上，相对基线的门槛才算得出；S2 的 `quick` 本来就带 12 个默认 bot）。
2. 把每个目录的 `judge_items.jsonl` 交给裁判：`judge-pack` → 在**干净的** DSH 会话里跑 `judge.workflow.js` → `judge-merge --out runs/l3/judgments.jsonl`（见 [裁判](#裁判judges)）。
3. **标注 30 条 × 每个要计入验收的 rubric（S2、L1、L2、L3）**：`calibration-set` 抽样 → 浏览器打开标注页标 → `calibrate` 算 κ（见 [judges/calibration/README.md](judges/calibration/README.md)）。κ ≥ 0.6 且至少 20 对的 rubric，裁判的数字才进验收，否则只作参考。
4. 出报告（下面）。

```bash
python -m bench report --results runs/s2 runs/l1 runs/l3 runs/m3 ... [--calibration calibration.json] [--gates bench/gates.yml] [--baseline legacy] \
    --out bench/reports/v0.14.0/ [--force] [--strict] [--seed N] [--resamples N]
```

读：每个运行目录里的 `results.jsonl`、`judge_items.jsonl`、`judgments.jsonl`（`judge-merge` 之后才有），可选的 `calibration.json`。写：`report.md`（中文：门槛、每个套件的数字、裁判）、`summary.json`（所有数字）、`gates.json`（每条门槛、每个 bot）、
`raw/`（做数字用的原始文件的**逐字拷贝**：H51）。输出目录已存在要 `--force`。同样的数据和 `--seed` 得到同样的报告（文件里没有时钟，只有 `report.md` 里的日期会变）。

**怎么读**（完整的说明在 [report.py](report.py) 的开头）：

- **每个数字带 95% 区间和样本数**：两级 bootstrap（先按题、再按题里的样本重抽，种子固定）；全对 / 全错时区间退化成一个点，报告改用 Wilson 区间并注明。**样本少的套件区间很宽**（L3 / L4 快速档每题一次采样，±30 点左右）：先看区间，再看点估计。
- **门槛的状态**：`通过` / `未通过`（按点估计；⚠ 表示区间跨过门槛线，`--strict` 把这种“通过”算作未通过）；`待基线`（相对基线的门槛，结果里没有基线 bot：把 `legacy` 放进同一批题的运行里）；
  `待校准` / `待裁判` / `仅供参考`（数字来自裁判，而这个 rubric 还没校准、没有判定、或 κ < 0.6；`acceptance: false` 的套件——M2、G——也是仅供参考）；`未跑`；`待数据`（E1 / E2 / G 不由 `run` 运行）。
- 门槛对**每个候选 bot** 各查一次，取最差的；`scope: pooled` 的门槛（S2、S3）在所有候选合在一起查。L1 比的是偏差的**绝对值**（偏向“说得差的一方”也是偏差）。
- 相对基线的门槛（L1–L4）的基线，是**同一批题上 `legacy` 的数**；R / S1 / S3 门槛旁的 `baseline` 是 v0.13.1 真实对局测出的数字（[reports/baseline-v0.13.1.json](reports/baseline-v0.13.1.json)）。
- 门槛的数值是 [gates.yml](gates.yml) 里的 `provisional`（初拟）：等有了 v0.14 真实的数据，再收紧。

## 从存档做局面脚本（M0 可行性验证）

计划里的风险（[§8](../docs/v0.14-plan.md)）：把 ledger 转成可重放的脚本可能有缺口。验证在这个仓库里做了：[replay.py](replay.py) 把存档账本里的**动作**转回工具调用，
在当前的真实引擎上重放，再把重放写出的账本逐条和存档比。

**结论：可行。** 这台机器上能读到的 v0.13.x 存档一共 12 局（`data/` 3 局、`.demo-data/real` 9 局；3 局 v0.13.0、9 局 v0.13.1；其中 4 局只有 24 条建局记录、没有开始，其余 8 局 325–495 条，含人类接管的局），
**每一局都能按动作逐条重放：账本逐条一致，终局的世界状态（玩家、频道、请求、能力、阶段、计数）完全一致，引擎拒绝 0 步，转不回的条目 0 个**——
只差一处已知的、不是缺口的差别（见下 ①）。同样的检查可以随时用 `python -m bench replay --data-dir data`（只读）打印，或者
`ARENA_BENCH_SAVED_GAMES=1 python -m pytest tests/bench/test_replay.py`（默认**不读**用户的存档，所以是显式开启的；没有存档就跳过）。

**0fa7（v0.13.0，468 条）的第 1 天警长竞选**，决策点都逐条一致：

| 决策点 | 序号 | 谁在等 |
|---|---|---|
| 是否上警 | #66 | 12 人的 `request` |
| 竞选演说的发言权 | #84 · #88 · #91 | N1 · N2 · N6 |
| 是否退水 | #96 | N1 N2 N6 |
| 警长投票 | #103 | N3 N4 N5 N7–N12（9 人） |

`python -m bench replay --game data/games/<id>/game.json --upto 103 --seat N4 --id <题目 id> --emit <文件.yaml>` 写出一份 77 步（约 21 KB）的脚本；`situations --check` 能建出它（账本 103 条，叫醒 N4 的是 `request.opened` r7）；
假模型下 `legacy` 和 `bot:Alice` 都在它上面跑完了。决策点的状态板里有硬事实台（`board["facts"]`：两条“警长竞选候选人”），因为重放用的是**现在**的剧本——v0.13.0 当时的状态板没有。

**方法**（[replay.py](replay.py)）：`convert_ledger` 把账本里是**动作**的条目转成 `Step`（`message` → `speak` / `announce` / `send_message`、`channel.created` → `create_channel`、`floor.opened` → `set_floor_queue` / `open_floor`、
`request.opened` → `open_request`、`request.answered` → `respond`、`ability.*`、`player.updated` → `update_player`、`phase.changed` → `set_phase`、`random.drawn` → `random`、`game.status` …）；
引擎自己写的条目（发牌、系统私聊、`floor.granted` / `token_ended` / `completed`、`request.closed` …）**不转**，它们由重放自己产生。每一步带着原来的游戏时间（`at`），在引擎的计时器触发处补 `tick`；
人类接管的座位用 `untimed`。`replay_game` 用存档的 seed、玩家、发牌（用 seed 发牌，发出来的和账本里的 `deal` 记录不同就改用记录的身份）和剧本（存档里的 `scenario_spec`，或现在的 `scenarios/<id>.md`）重放，比对：逐条（类型、作者、频道、可见范围、内容；游戏时间容差 0.02 秒）和终局状态。

**已知的差别和限制**（都不挡着做题；写在这里是为了不被当成“逐字复刻”）：

1. **v0.13 的截断留下的尾巴**。v0.13 把超长发言截到上限，存档里的文字尾部可能带着截断留下的空白；现在的提交闸门会先去掉首尾空白，所以这样的发言和它所在发言权的 `chars_used` 在重放里差几个字符（全是尾部空白）。
   这种差别单独计为“仅截断空白”（12 局里 7 局有，共 32 条；最多的一局 8 条），不算不一致；真的不同的文字照常报告。
2. `request.opened` 里没有 `default`（到期无人作答时用的值）。它从存档的**世界状态**里取（`state.requests[*].default`）——所以只有账本、没有状态的存档只能恢复“实际触发过的默认值”。`game.json` 两者都有。
3. **发牌**靠 seed 复现（`rng.seed`）；如果这个 seed 发出来的和账本里的 `deal` 记录不一样，改用记录的身份，之后的 `random.*` 抽签就对不上了（引擎不洗牌，就不用随机数发生器）。这 12 局都不需要（报告里“发牌方式”都是 seed）。
4. **账本里没有的东西不会被重放**：agent 的私有笔记、对话、思考、记忆快照、UI 事件。所以从存档切出来的题目是“世界在决策点时的样子”，被测的 bot 带着的是**这次基准给它的**设定和记忆，不是当时那个 agent 的。
5. 重放用**现在的引擎**。一局在旧版本里存下来，现在的剧本的模板文字或规则改了，重放就不再逐字一致——这正是检查要发现的。题目里写的 `scenario: werewolf_12p` 运行时读的是**现在**的 `scenarios/werewolf_12p.md`（2.2.0：`situations`、带 `record` 的模板），
   所以决策点的状态板比当时多了硬事实台。
6. “动作”的单位是一次工具调用：`--upto N` 停在写出第 N 条的那个动作的末尾，同一个动作里引擎接着写的几条（比如发言之后的发言权结束、下一个发言权）也一起写了、一起比对（`overshoot`）。决策点（`request.opened`、`floor.granted`）本来就是动作的末尾。
7. **存档是用户自己的数据**（名字、人类的发言）。`--emit` 写出的脚本带着存档里的名字和全部发言：提交到 `fixtures/` 之前必须检查、脱敏（脚本头部的注释也这么写）。`data/`、`.demo-data/` 本身不进仓库。

## 规则

- 只统计 agent 的发言；人类接管座位期间的发言被排除并单独计数（`human_speeches`）。
- **基准里的发言必须由 bot 自己产生**（H18）：用局面脚本建好世界，跑真实的 Agent / Worker 循环，直到 bot 自己调用 `speak` / `pass` / `respond`；不允许用单独的“请写一段发言”提示去直接生成（见 [runner](#runner) 里的保证和检查）。
- **`fixtures/` 不在任何学习工具的读取范围内**（H50）：初始化 / 复盘的 `read_source` / `read_record` 读不到这里，`app/` 里没有任何代码读取或提到 `bench/`（有测试守着）。
- **基准从不读写真实的 `data/`**：运行时用输出目录里的临时数据目录；输出目录默认是 `.pytest_tmp/bench-runs/<run id>/`；测试只用 pytest 的 `tmp_path`，不在系统临时目录（有的沙箱里它就是仓库根）留任何东西。
- **真实模型默认关**：要 `--real` 和 `--max-calls`；Key 只从环境变量来。假模型的行写着 `"mode": "fake"`。
- 报告只放数字和少量共用的短语，不放整局对局；用户自己的对局（`data/`、`.demo-data/`）不进仓库。

## 测试

`tests/bench/`（不调任何模型，全部走 `tmp_path`）：`test_metrics.py`（度量）、`test_guards.py`（H50 守卫）、`test_situation.py`（格式与错误）、`test_play.py`（真实引擎上的脚本）、`test_bots.py`（bot 与消融）、
`test_fakes.py`（假模型）、`test_runner.py`（真实 Agent 循环：退回 → 重写、预算、超时、续跑、裁判条目、H18 的检查）、`test_realllm.py` / `test_safety.py`（真实模式的保护，用桩替换传输层）、
`test_summary.py`（数字）、`test_replay.py`（账本 → 脚本；用测试里在真实引擎上打出来的一局；读用户存档的那一条默认跳过，`ARENA_BENCH_SAVED_GAMES=1` 打开）、`test_cli.py`（命令）。
题库与套件：`test_bank.py`（题库 == 生成器写出的；每道题在真实引擎上走到决策点、truth ∈ 选项、证据在被测座位的视野里、被测座位在脚本里从不发言；成对题只差带标记的发言；各家族的数量下限；L4 的结构；**脱敏检查**：内置泄漏样例证明检查函数有效，`ARENA_BENCH_SAVED_GAMES=1` 才拿本机存档对拍，并比对 L4 的板面和存档重放的板面）、`test_suite.py`（套件 yml 的校验与档位计划、`run --suite`、`truth:` 与文字作答的评分、`@history=N`）。
