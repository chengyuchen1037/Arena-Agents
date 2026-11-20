# bench：v0.14 的基准与验收

v0.14 的目标是两件事：**各玩家的发言风格不同**（不再像写工作报告），**抓住核心逻辑**（依据硬事实判断，不被“讲得有条理”的发言带走）。
这里的东西用来**量化**它们：同一套度量，先在 v0.13.1 的真实对局上测出基线，再在 v0.14 上看变化，验收门槛写在 [gates.yml](gates.yml)。

## 现在有什么

| 部分 | 文件 | 说明 |
|---|---|---|
| 读存档 / 日志 | [loaders.py](loaders.py) | `game.json` 的账本、发言（标出人类接管时段的发言）、`trace-*.jsonl` |
| 度量 R / S1 / S3 | [metrics.py](metrics.py) | 纯函数：截断、被退回的次数与重写成功率、发言权超时、发言耗时、token；编号 / 行内编号 / markdown / 自报座位、长度（对照 `篇幅：a–b 字`）；跨座位共用的片段与套话 |
| 命令行 | [\_\_main\_\_.py](__main__.py) | `python -m bench audit` / `baseline` |
| 基线 | [reports/baseline-v0.13.1.json](reports/baseline-v0.13.1.json) | v0.13.x 的 8 局真实对局（deepseek-flash）的汇总数字，**不含对局本身** |
| 门槛 | [gates.yml](gates.yml) | 每条门槛：度量、比较、数值、基线、状态（provisional / pending_baseline） |

其余套件（S2 风格可辨识、L1–L4 核心逻辑题、M1–M3 记忆层、E1–E2 学习、G 联赛）由 runner 与题目库提供，见计划 [§6.5](../docs/v0.14-plan.md)
和契约 [§13.11](../docs/v0.12-contracts.md)；它们的裁判是 DSH 子 agent（不是玩家模型），评分标准在 `judges/`，人工标注页在 `label/`。

## 怎么跑

```bash
# 一局存档的数字（trace 可选）
python -m bench audit --game data/games/<id>/game.json --trace data/logs/<id>/trace-0001.jsonl

# 一个数据目录下所有存档的汇总（可以给多个目录），写成报告
python -m bench baseline --data-dir data --data-dir .demo-data/real --out bench/reports/baseline-v0.13.1.json

# 测试（不调任何模型）
python -m pytest tests/bench
```

成本分三档：**冒烟**（CI 里跑，0 次模型调用，即上面的测试）、**快速**（约 300 次调用，改提示词 / 记忆之后自查）、**完整**（数千次调用 + 5–10 局联赛，发版用）。

## 规则

- 只统计 agent 的发言；人类接管座位期间的发言被排除并单独计数（`human_speeches`）。
- **基准里的发言必须由 bot 自己产生**（H18）：用局面脚本建好世界，跑真实的 Agent / Worker 循环，直到 bot 自己调用 `speak` / `pass`；不允许用单独的“请写一段发言”提示去直接生成。
- **`fixtures/` 不在任何学习工具的读取范围内**（H50）：初始化 / 复盘的 `read_source` / `read_record` 读不到这里，`app/` 里没有任何代码读取 `bench/`（有测试守着）。
- 报告只放数字和少量共用的短语，不放整局对局；用户自己的对局（`data/`、`.demo-data/`）不进仓库。
