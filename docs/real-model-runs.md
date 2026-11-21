# 用真实模型跑：冒烟脚本与默认 bot 库初始化

离线测试（pytest）从不调用模型：所有模型调用都换成了假模型或演示机器人。有两件事只有真实模型才看得出来，仓库为它们各留了一个脚本：

| 脚本 | 做什么 | 什么时候跑 |
|---|---|---|
| `tools/real_smoke.py` | 用真实模型打一小局，打印工具使用、被引擎退回的回执（`TOO_LONG` / `FORMAT`）、四类 token、提示缓存命中率、各 agent 的激活数、记忆挂载量；可选地真实初始化 bot、跑真实复盘与“用主持人模型完善底层设定” | 改了引擎、提示词、契约、Composer / BotBrain 之后，想知道真实模型的表现 |
| `tools/init_default_bots.py` | 对仓库里的默认 bot 库（`bots/<名字>/`）跑一次**真实初始化**，把 bot 自己写的操作笔记、规则要点、打法、待解问题和初始化记录写回库里 | 发版前一次；契约、默认剧本或某个 bot 的底层设定改过之后（`tests/test_bots_library_fresh.py` 变红时） |

bot、初始化和复盘是什么，见 [bots.md](bots.md)。两个脚本都会花你的 API 额度，所以都有**硬预算**（`--max-calls`：超出的调用根本不会发出去；一次调用 = 引擎的一次模型请求，它内部对网络错误的自动重试不另算）；**任何测试、任何自动流程都不会运行它们**——只有你亲手在命令行里跑。`tests/test_tools_real_smoke.py`、`tests/test_tools_init_default_bots.py`、`tests/test_tools_devlib.py` 用演示机器人把两个脚本的主流程完整跑通（不调模型、不碰真实数据目录），所以脚本本身是有测试的；真实模型的表现则只能靠你跑。

## 先决条件

- 一个装好 provider 的数据目录（`--from`）：你平时用应用的那个 `data/`。里面要有 `providers.json`、`secrets.v1.enc`（保存了 API Key）和默认模型绑定（在应用的“模型绑定”里给主持人和玩家选过模型）。`--from` 里没有 Key、没有绑定时脚本会直接说明，不会去猜。
- Windows：主密钥由 `master.v1.dpapi` 保护，只有**同一个 Windows 用户**在**同一台电脑**上才解得开。其他系统：主密钥来自环境变量 `ARENA_MASTER_KEY` / `ARENA_MASTER_KEY_FILE`，和应用一样。
- 应用可以同时开着：脚本只**读** `--from` 里的这几个文件（普通的文件复制，不用任何存储对象去打开它），不会碰它的锁。

## 数据目录隔离

两个脚本都不会用你的数据目录干活：

1. 在 `.demo-data/` 下建一个私有的临时目录（`arena-real-smoke-…` / `arena-init-bots-…`），把 `--from` 里的 `providers.json`、`secrets.v1.enc` 和（Windows）`master.v1.dpapi` **复制**进去——只有这三个文件，不复制对局、日志、bot、名册；
2. 在导入任何应用代码**之前**把 `ARENA_DATA_DIR` 指向这个临时目录（结束后环境变量原样恢复），所以应用的所有存储（bot、契约、剧本、对局、日志、用量）都落在临时目录里；
3. 把 `--from` 的默认模型绑定写成临时目录里的座位绑定（v0.13 的目录按座位存、v0.14 的目录按 bot id 存，两种都能读）；
4. 结束时删除临时目录（它里面有 provider 的加密副本）；想留下来看看就加 `--keep`，脚本会打印它的位置，用完请自己删掉。

`--from` 本身不会被写入或改动；仓库里的 `data/` 更是从来不是它们的工作目录。

## `tools/real_smoke.py`：真实模型打一小局

v0.14 的开局要求每个 bot 都已初始化（否则 `NOT_READY`），所以脚本先让 bot 初始化，再开局：

1. 临时数据目录（见上）；启动应用自己的启动流程：桌上的 bot 是默认库的前 `--players` 个（Alice、Bob……），只有底层设定；
2. 初始化：`--init fixed`（默认）提交演示用的固定初始化——文件合法、**不调用模型**，只为让开局检查通过；`--init real` 跑真实的学习运行（每个 bot 各自调用自己的模型，并行，每个最多 120 秒），并逐个 bot 打印耗时、调用数、token、写了哪些文件和字数、失败原因；
3. 开局，每秒看一眼，到预算或时限就暂停；打印报告；
4. 可选：`--resume-calls` 暂停后继续再跑几次调用，看提示缓存有没有断；`--oneshots N` 跑真实的“完善底层设定”（主持人模型，只打印草稿、不保存）和对前 N 个 bot 的真实复盘（把暂停的局当作已结束；打印每个 bot 写了什么）。

桌上有 `--players` 个座位（1–12），剧本要正好是这个人数：`scenarios/minimal_test.md` 是 4 人（默认），`scenarios/werewolf_12p.md` 是 12 人。对不上时脚本会直接说清楚，退出码 2。

### 选项

| 选项 | 默认 | 说明 |
|---|---|---|
| `--from` | — | 装有 provider 的数据目录（只读，必填） |
| `--scenario` | `scenarios/minimal_test.md` | 剧本文件；相对路径先按当前目录找，再按程序目录 |
| `--players` | 4 | 座位数 1–12，要和剧本的人数一致 |
| `--init` | fixed | `fixed`：演示的固定初始化，不调模型；`real`：真实学习运行，花钱，计入 `--max-calls` |
| `--core-from-sample` | — | 把脚本里的示例性格（`SEATS`）写进各 bot 的底层设定，代替默认库的 |
| `--max-calls` | 60 | 整个运行的模型调用**硬预算**（初始化、对局、一次性调用都算）；超出的调用不会发出，对局到预算时暂停 |
| `--timeout` | 300 | 初始化和对局各自的墙钟上限（秒） |
| `--concurrency` | 0 | 并发调用数上限（0 = 不限） |
| `--price-source` | — | 下载 LiteLLM 价格表，只在临时副本里给绑定的模型定价，报告里的费用更接近 provider 实际计费 |
| `--window` | 0 | 假装每个模型只有这么多 token 的上下文窗口（比如 16000），用来在一局很短的游戏里触发压缩提醒、`compact_context` 和紧急裁剪 |
| `--resume-calls` | 0 | 到预算暂停后再继续跑 K 次调用，报告提示缓存是否跨暂停命中、对话和 system 提示词是否逐字节不变 |
| `--oneshots` | 0 | 对局后跑真实的“完善底层设定”和对前 N 个 bot 的真实复盘；预算在 `--max-calls` 之外再加 `2 + 8N` 次 |
| `--keep` | — | 保留临时目录 |

### 命令示例

```
python tools/real_smoke.py --from "C:/Users/me/Arena-Agent/data" --players 4 --max-calls 60
python tools/real_smoke.py --from "C:/Users/me/Arena-Agent/data" --init real --players 4 --max-calls 120
python tools/real_smoke.py --from "C:/Users/me/Arena-Agent/data" --scenario scenarios/werewolf_12p.md --players 12 --max-calls 400 --oneshots 2
```

退出码：0 跑完了（报告已打印）；2 模型绑定缺失 / 不可用，或剧本和 `--players` 对不上；3 对局没能开始（有 bot 没初始化，或开局检查拒绝；问题会打印出来）。

### 花费的量级

下面都是按调用数估的量级，不是报价：

- `--init fixed` 的初始化 0 次调用。4 人的 `minimal_test.md` 一局大约几十次调用，默认预算 60 够打到结束或暂停；12 人的 `werewolf_12p.md` 一整局要几百次（演示机器人的一整局约 630 次调用、约 390 万 token，其中约 320 万是命中缓存的输入；真实模型的思考轮次更多，只会更多）——所以跑 12 人局要把 `--max-calls` 放大，或者故意用小预算只看开头；
- `--init real`：每个 bot 约 4–8 次调用（读契约 / 规则 → 写文件 → 完成），12 个 bot 约 50–100 次；
- `--oneshots N`：2 次“完善底层设定”加每个 bot 几次复盘调用，预算里预留 `2 + 8N`。

每次调用的 token 数看报告里的 `tokens:` 行；`cost $` 按 provider 表里的价格算（价格没填就是 0，`--price-source` 可以补）。先用小预算（比如 `--players 4 --max-calls 30`）试一次再放大。

### 怎么审结果

报告里依次是：

- `wall time … | LLM calls … (refused over the budget: …) | call errors {…}`：调用数、超预算被拒的次数、调用抛出的错误按类统计；
- `tokens:` 四类 token（未命中缓存的输入、缓存读、缓存写、输出）、缓存命中率、费用；
- 每个 agent 一行：激活数、调用数、工具数、思考轮数、被引擎退回次数、输入 / 缓存 / 输出 token；
- `tool usage:` 各工具的使用次数；`activations … | in-flight merges …`；
- `mounted at the start of a conversation (average per agent, chars): L0 …, L1 …, L2 …, L3 …`：每个 agent 开场挂载的记忆量（底层设定、操作笔记、规则要点与打法、事实台）；`hints shown with the messages:` 事件提示出现的次数；
- `prompt cache: x/y calls after an agent's first one reported cached input`：同一个 agent 第一次调用之后的调用有多少命中缓存——应该接近 100%，明显偏低说明提示词在一局内变了（system 提示词必须逐字节不变）；
- `rejections by the engine (receipts): TOO_LONG x…, FORMAT x…`：引擎退回的发言：`TOO_LONG`（超过单条上限）和 `FORMAT`（读起来像报告）。偶尔出现是正常的（bot 会按回执重写）；某个 bot 反复被退回，说明它的底层设定或提示词需要看一看；其他回执码单独列出，并附一条样例；
- 末尾的发言权时间线（前 60 条）和最后 8 条公共发言：读一遍，看 bot 说的是不是人话、有没有自报座位号、列点。

`--init real` 的报告每个 bot 一行：状态、耗时、调用数、三类 token、写出的文件和字数；失败的有 `why:`。`--oneshots` 的输出是“完善底层设定”的各段字数和缺失段，以及每个被复盘的 bot 的改动摘要（`people` / `playbook` 前后字数和前 300 字）。

## `tools/init_default_bots.py`：给默认 bot 库跑一次真实初始化

默认库里的 12 个 bot 只有底层设定（L0）。每个安装第一次运行都把它们复制成“未初始化”，用户得先初始化才能开局。这个脚本在发版前替用户做一次：用你选的模型、仓库当前的官方契约（`prompts/contract.md`）和默认剧本（`config.yml` 里的 `scenario.path`，没有学习材料），跑应用自己的初始化（`start_init`，每个 bot 并行，最多 120 秒，全有或全无），然后——**只有加了 `--write-library`**——把成功的 bot 的 `arch/notes.md`、`rules/<剧本 id>/{digest,playbook,questions}.md` 和 `bot.json` 里的 `init` 记录（`model` 写实际的模型名、时间、四个哈希）复制回 `bots/<名字>/`。失败的 bot 一个字都不动。

步骤：

1. 临时数据目录（见上），把模型绑到 12 个座位和主持人；`--model PROVIDER/MODEL`（provider 的 id，或设置里显示的名字，不分大小写；再一个斜杠和模型 id）；不给就用 `--from` 里绑给主持人的模型；
2. 启动应用，桌子由默认库（`--library`，默认是本仓库的 `bots/`）建出来；要初始化的 bot 先清掉已有的记忆（**从零开始**，不在旧结果上修订，结果不依赖上一次）；
3. `start_init`：每个 bot 找自己的模型，进度打印成报告：状态、耗时、调用数、token、写了哪些文件和字数、失败原因，最后是总用量和费用；
4. `--write-library` 时写回，再对库做一次检查（见下），有问题退出码 1。

要初始化哪些 bot：默认是**初始化不是最新的**那些（没初始化过，或者底层设定、契约、剧本在初始化之后改过）；`--all` 是全部 12 个；`--only Alice,Bob` 是点名的（不分大小写）。某个 bot 失败了，用 `--only` 单独补跑，不必为其他 11 个再付一遍钱。

### 选项

| 选项 | 默认 | 说明 |
|---|---|---|
| `--from` | — | 装有 provider 的数据目录（只读；除 `--check` 外必填） |
| `--model` | — | `PROVIDER/MODEL`；不给就用 `--from` 里主持人的模型 |
| `--only` | — | 只初始化这些 bot（名字，逗号分隔） |
| `--all` | — | 全部 bot，包括初始化还是最新的 |
| `--write-library` | — | 把成功的 bot 的结果写进库里（`bots/<名字>/`）；不加就不改库 |
| `--library` | — | 要从中建桌子、也往里写的默认库（默认：本仓库的 `bots/`） |
| `--dry-run` | — | 只打印计划：模型和单价、要初始化的 bot、预算、契约 / 剧本的哈希；不调模型 |
| `--check` | — | 只检查库里已初始化的 bot 是否还是最新的（契约、默认剧本、底层设定都没变）；不调模型、不需要 `--from`；不是最新的退出码 1 |
| `--max-calls` | 200 | 模型调用**硬预算**（约 5–8 次 / bot）；超出的调用不会发出 |
| `--timeout` | 400 | 整个运行的墙钟上限（秒），到了就取消，没做完的 bot 不写 |
| `--concurrency` | 0 | 并发调用数上限（0 = 12 个 bot 同时） |
| `--keep` | — | 保留临时目录（里面是初始化完的 bot） |

退出码：0 要做的都做完了；1 有 bot 失败，或库不是最新的（`--check`，或写完后的自检）；2 参数不对（模型、名字）。

### 命令示例

```
python tools/init_default_bots.py --from "C:/Users/me/Arena-Agent/data" --dry-run
python tools/init_default_bots.py --from "C:/Users/me/Arena-Agent/data" --model MyProvider/my-model --max-calls 200 --write-library
python tools/init_default_bots.py --from "C:/Users/me/Arena-Agent/data" --only Kate --write-library
python tools/init_default_bots.py --check
```

### 花费的量级

12 个 bot 约 50–100 次调用（每个 4–8 次：读契约必读段 / 规则、写文件、完成），每次输入大约几千 token（契约 + 剧本里玩家能看的规则；第二次起多数命中缓存），输出几百到几千 token（推理模型的思考也算输出）。总量大致是十几万到几十万 token：便宜的模型几美分到几角，贵的模型可能几美元——**先 `--dry-run` 看单价，再用 `--max-calls` 封顶**；跑完的报告最后一行是实际用量和费用。

### 怎么审结果（写进库之前和之后）

1. 报告里 12 个 bot 都是 `ready`；`wrote:` 里有 `arch_notes`、`digest`、`playbook`（`questions` 可选）；字数都在上限内（操作笔记 ≤ 800、规则要点 ≤ 800、待解问题 ≤ 400、打法 ≤ 20 条、每条 ≤ 80 字）；
2. 加了 `--write-library` 后看 `git diff bots/`，逐个读：
   - 是 bot **自己的话**、第一人称，写“我理解 / 我打算”，不是照抄契约或剧本；
   - 打法是**假设**：把握度“低”、来源“初始化”、带情境标签（`#发言` `#投票` …），不是写死的结论；不教具体套路、不夸大；
   - 操作笔记讲的是怎么用平台（被唤醒不等于必须行动、什么时候休眠、被退回怎么办），规则要点讲的是这个剧本的规则和已知的信息边界；
   - 没有对具体某个人的印象（初始化时它还没和谁玩过），没有身份牌信息，也没有不该出现的内容；
   - `bot.json` 的 `init.model` 是实际模型名，`init.hashes` 有四个哈希，`init.scenario_id` 是默认剧本；
3. `python tools/init_default_bots.py --check` 通过（写完后脚本自己也会检查一次）；
4. 一个 bot 不满意就删掉它的写入（`git checkout`）或用 `--only 名字 --write-library` 再跑一次；
5. 提交。库里带 `ready` 的 bot 是合法的：`tests/test_bots_defaults.py` 对“带 init 的库”和“不带 init 的库”两种形状都验证过（库的文件夹是 `bot.json` + `core/`，已初始化的再加 `arch/`、`rules/`；新数据目录里带 `ready` 记录的 bot 是 ✔，没有的是未初始化），库的写法见 [bots/README.md](../bots/README.md)。

### 守卫测试：库不能带着过期的初始化发版

`tests/test_bots_library_fresh.py` 对仓库里的 `bots/` 做 `--check` 同样的检查：库里每个 `ready` 的 bot，它的 `hashes.core` / `contract` / `scenario` / `materials` 必须等于**全新安装**会算出的哈希（用新数据目录里的契约库、规则库和 bot 存储来算，所以和应用在用户电脑上算的一致），记录里承诺的记忆文件必须在。契约、默认剧本、某个 bot 的底层设定一改，这条测试就变红，提示“请重新运行 `tools/init_default_bots.py`”。库里没有 `ready` 的 bot 时它自然通过（一个没有初始化的库也是合法的）；它的红 / 绿由造出来的假库和一个用脚本同款代码做出来的库来验证。

## 出了问题

| 现象 | 原因 / 做法 |
|---|---|
| `missing providers.json …` / `missing secrets.v1.enc …` | `--from` 不是装着 provider 的数据目录，或者那些 provider 没存 Key；先在应用里配好 |
| `BINDING ISSUES: …` | 默认绑定指向的 provider / 模型在 `--from` 里不存在或没有 Key；在应用的“模型绑定”里重新绑 |
| `THE TABLE DOES NOT FIT THE SCENARIO` | `--players` 和剧本的人数对不上 |
| `BOTS THAT ARE NOT INITIALIZED` / `THE GAME DID NOT START` | `--init real` 有 bot 没初始化成功（原因在报告里：超时、模型调用失败、缺文件、超字数……），或开局检查拒绝；对局没有开始，也没有花对局的钱 |
| `BudgetExhausted … the call budget … is spent` | 预算用完了，这次调用没有发出；放大 `--max-calls` 重跑 |
| `no (single) provider … ; the providers there: …` | `--model` 里的 provider 写错了（或有重名），后面列出了可选的 provider 和它们的模型 |
| 解密失败 / 看不到 Key | 主密钥对不上：Windows 要同一个用户；其他系统检查 `ARENA_MASTER_KEY` / `ARENA_MASTER_KEY_FILE` |
