# 默认 bot 库

这里是 Arena-Agents 随版本提供的 12 个默认 bot：Alice、Bob、Carol、David、Eve、Frank、Grace、Henry、Ivy、Jack、Kate、Leo。每个 bot 的底层设定（L0）互不相同；它们也可以带着**已经初始化好的记忆**（见下面的“初始化产出”）。bot 是什么、四层记忆怎么用，见 [docs/bots.md](../docs/bots.md)。

每个 bot 一个文件夹，便于 diff：

```text
bots/<名字>/
├─ bot.json              名字、头像（头像是 20×20 像素头像的部件描述，固定，不随机）；已初始化的 bot 另有 init 记录
├─ core/
│  ├─ identity.md        身份：一两句话 + 背景（≤ 300 字）
│  ├─ voice.md           说话方式：连贯的散文 + 3–5 句用「」标出的原话样本 + 一行 `篇幅：xx–yy 字`（≤ 600 字）
│  └─ mind.md            分析方式与学习态度（≤ 400 字）
├─ arch/notes.md         （只有已初始化的 bot 才有）它自己写的操作笔记
└─ rules/<剧本 id>/      （只有已初始化的 bot 才有）规则要点 digest.md、打法 playbook.md、待解问题 questions.md（可选）
```

## 怎么用

- **首次运行**（数据目录里还没有 `bots/`，也没有 v0.13 的 `roster/`）时，这 12 个文件夹被复制成默认阵容。**带 `init` 记录的 bot 复制过来就是 ✔（已初始化），没有的是“未初始化”**，要先点“⚙ 全局初始化”才能开局。✔ 的前提是记录里的哈希对得上这台电脑上的底层设定、官方契约和剧本（默认情况下对得上）；用户改过契约或剧本之后，它们会显示 ⚠“需要重新初始化”。
- **从默认库导入**（玩家详情里的按钮，`POST /api/bots/{id}/import-default`）：用这里的某个 bot **整体覆盖**当前玩家（名字、头像、底层设定，以及这个 bot 带着的记忆和初始化记录）。
- 这里的文件只是**模板**：复制之后数据目录里的 bot 有自己的内部 id，之后怎么改都不会影响这里。

## 写法约定（L0）

- 12 份底层设定互不相同：篇幅区间、语气、分析方式都不同；前提都是“想赢”。
- 说话方式写成连贯的散文，不分点；原话样本不重复。篇幅区间落在 30–280 字之内（每条发言有 300 字的上限）。
- 分析方式用各自的话表达同一种态度——先看发生了什么，再听谁说了什么；说得漂亮不等于是真的；被结果打脸就改。**不写任何具体打法，也不写具体游戏的词汇**：游戏的内容只通过剧本进来，底层设定只说“这个人是谁、怎么说话、怎么想”。
- 文件是 UTF-8、LF 换行，以换行结束（`tests/test_bots_defaults.py` 会检查）。

这些约定针对 L0（`core/` 和 `bot.json`），`tests/test_bots_defaults.py` 逐条检查它们。已初始化的记忆（`arch/`、`rules/`）写的是 bot 对**这个游戏**的理解，自然会讲到游戏，不在“没有游戏词汇”这条检查的范围里。

## 初始化产出（L1 / L2）

操作笔记、规则要点、打法等是每个 bot 自己“初始化”得到的。库里的 bot **可以带着它们**：这样一份全新的安装一启动，这些 bot 就是 ✔，用户不用再花模型费用初始化；**不带也完全合法**。带不带，以 `bot.json` 里有没有 `init` 记录为准。

带着的时候，一个 bot 的文件夹里多出：

- `bot.json` 里的 `init` 记录：`{"status": "ready", "at", "model", "duration_s", "error": null, "scenario_id", "hashes": {"core", "contract", "scenario", "materials"}}`——用哪个模型、什么时候、花了多久，以及初始化当时它的底层设定、官方契约、默认剧本、学习材料各自的指纹；
- `arch/notes.md`（操作笔记）、`rules/<剧本 id>/digest.md`（规则要点）、`rules/<剧本 id>/playbook.md`（打法）三个**必需**文件，和可选的 `rules/<剧本 id>/questions.md`（待解问题）。

除此之外**没有别的**：印象（`people.md`）、战绩（`record.json`）和待确认的提案不进库——它们是玩过之后才有的。

**怎么生成、重新生成**：用 `tools/init_default_bots.py`。它会调用真实模型、花 API 额度（有硬预算）；步骤、选项和怎么审结果见 [docs/real-model-runs.md](../docs/real-model-runs.md)。先 `--dry-run` 看计划，**加了 `--write-library` 才会把成功的 bot 的结果写进这里**；然后看 `git diff bots/`，逐个读 bot 写了什么，满意再提交。`--check` 只检查库是不是最新的，不调用模型。

**什么时候必须重新生成**：一个带 `init` 记录的 bot，只要它的底层设定、官方契约（`prompts/contract.md`）或默认剧本（`config.yml` 里 `scenario.path` 指的那个）改了，记录里的哈希就对不上了——全新安装里这个 bot 会显示 ⚠“需要重新初始化”。守卫测试 **`tests/test_bots_library_fresh.py`** 就是防这件事的：库里有 `ready` 的 bot 而哈希对不上（或者承诺的记忆文件不在），它就变红，并提示运行 `tools/init_default_bots.py`。要改某个 bot 的底层设定，要么重新生成它（`--only <名字> --write-library`），要么删掉它的 `init` 记录和 `arch/`、`rules/`（回到只有 L0）。

另一个测试 `tests/test_bots_defaults.py` 检查库的**形状**：每个文件夹是 `bot.json` + `core/`，已初始化的再加 `arch/`、`rules/`；`bot.json` 只有 `format`、`name`、`avatar` 和可选的 `init`；`init` 要么没有、要么是 `ready`，`ready` 的 bot 必须带齐三个必需文件（它们要能通过导入时的检查），记录里的底层设定哈希要和 `core/` 对得上；没有 `ready` 记录的 bot 不能带记忆文件。它对“带 init 的库”和“不带 init 的库”两种都验证过：在新数据目录里，带 `ready` 记录的 bot 是 ✔（哈希对得上），没有的是“未初始化”。
