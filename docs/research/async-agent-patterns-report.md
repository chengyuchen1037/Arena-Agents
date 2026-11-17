# 异步 LLM Agent（前台 Actor + 后台 Worker）相关开源项目与设计调研报告

> 调研目标：为 FastAPI + asyncio + SSE 的多 LLM Agent 游戏竞技场（12 名玩家 + 1 名 LLM 主持人，规则以 Markdown 场景描述、引擎与游戏无关）寻找可借鉴的“前台 actor 永远可收发 + 后台 worker 负责 LLM 思考/工具调用”的设计。所有网页内容仅作为数据参考；标注“（据公开资料/经验）”的条目未在本次检索中逐行核实源码。

## 1. Actor 模型框架

### 1.1 Microsoft AutoGen v0.4+（autogen-core）
- **模式**：典型的 actor 模型。Agent 之间“消息是唯一通信手段”，消息是纯数据（Pydantic/dataclass），由 runtime 调用 `on_message`；`RoutedAgent` 用 `@message_handler` 按消息类型（及 `match` 谓词）路由（[Message and Communication](https://microsoft.github.io/autogen/stable/_sources/user-guide/core-user-guide/framework/message-and-communication.ipynb.txt)）。
- **两种投递**：直接消息（request/response，可 await 返回值，异常回传给发送者）与广播（pub/sub，单向，返回值丢弃，发布者不会收到自己发布的消息以防循环）。
- **Topic/Subscription**：Topic = (type, source)，订阅把 topic 映射到 agent ID；`TypeSubscription` 把 topic type 映射到 agent type，source 作为 agent key，实现多租户隔离（[Topic and Subscription](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/core-concepts/topic-and-subscription.html)）。
- **中断/取消**：`MessageContext` 带 `CancellationToken`，可用于取消进行中的模型调用（据公开资料）；Group Chat 的“选下一个发言者”由 manager agent 集中调度。
- **对我们可借鉴**：①“频道 = topic、可见性 = 订阅”的抽象非常契合我们的 channel 设计（公共频道、狼人夜聊、玩家↔主持人私聊各为一个 topic）；②直接消息 vs 广播的语义区分（私下质询主持人用 direct request/response，发言用广播）。**不建议依赖**：引入整个 runtime 只为得到一个 pub/sub 过重；且其 handler 的并发语义与我们“actor 永不阻塞”需求需要自己再包一层。

### 1.2 Akka / Orleans / Ray / thespian / pykka（简述，据公开资料）
- **Akka**：每个 actor 一个 mailbox，单线程顺序处理消息；监督树（supervisor 决定 restart/stop/escalate）。核心启示：**actor 内部状态只被自身协程修改**，长任务交给子 actor/Future，结果以消息形式回送（pipe-to-self）。
- **Orleans**：虚拟 actor（grain），默认 turn-based 并发（一次只处理一个请求，await 期间默认不重入，可选 reentrant）。启示：如果 actor 在 await LLM 时不可重入，就会“被思考阻塞”——这正是我们要拆出 worker 的原因。
- **Ray**：async actor + `max_concurrency`，适合分布式；单进程场景无必要。
- **pykka**：线程/gevent actor，`tell`（fire-and-forget）与 `ask`（返回 future）。**thespian**：多进程 actor system。二者都不是 asyncio 原生，对我们价值主要在概念层。

## 2. LLM Agent 框架

### 2.1 LangGraph
- **模式**：图状态机 + checkpoint。`interrupt()` 在节点内暂停并持久化状态，外部用 `Command(resume=...)` 恢复，本质是“人类在环”的协作式暂停，而非抢占式取消（[interrupt API](https://reference.langchain.com/python/langgraph/types/interrupt)，[Interrupts 文档](https://langchain-5e9cc07a.mintlify.app/oss/javascript/langgraph/interrupts)）。
- **“Double texting”策略**（LangSmith Agent Server 提供，开源框架本身不提供）：run 进行中又来新输入时四选一——**Reject**（拒绝新输入）、**Enqueue**（默认，排队顺序执行）、**Interrupt**（停止当前 run、保留进度、插入新输入后继续，需处理半截工具调用）、**Rollback**（回滚当前 run 连同其输入，用新输入重跑）（[Double texting](https://raw.githubusercontent.com/langchain-ai/docs/04d4d4a45cb57537c3d6e977f25333f2c5a4881b/src/langsmith/double-texting.mdx)）。
- **可借鉴**：这四种策略正好是我们 worker 取消策略的完整分类表，可直接作为每个 agent/每类事件的可配置 policy。

### 2.2 AgentScope 1.0 / 2.0
- **MsgHub**：async context manager，hub 内参与者的发言自动广播给其他参与者，可带 `announcement`（[Conversation 教程](https://doc.agentscope.io/tutorial/workflow_conversation.html)）。狼人杀示例用 MsgHub 组织白天讨论/夜间狼人频道，配合 sequential/fanout pipeline 做轮流发言与并行投票（据公开示例）。
- **异步与中断**：1.0 论文强调“系统化异步设计”与实时干预（[AgentScope 1.0, arXiv:2508.16279](https://arxiv.org/abs/2508.16279v1)）。2.0 文档明确：**中断建立在 `asyncio.CancelledError` 之上**，对运行中的 agent 直接 `task.cancel()`，在模型推理或工具执行任意阶段停止，且保证上下文处于一致状态，可立刻用新消息继续；对暂停中的 agent 发送 `UserInterruptEvent`，为每个未完成工具调用合成“被用户中断”的 ToolResult，再发 `ReplyEndEvent`（[Interrupt Agent](https://docs.agentscope.io/versions/2.0.5/en/building-blocks/agent/interrupt-agent)）。
- **可借鉴**：①“取消后补齐结构”——被取消的工具调用要合成占位结果，保证消息历史合法（否则下一次 LLM 请求会因孤立 tool_call 报错）；②MsgHub ≈ 我们的 channel，但它是同步回合式编排，**没有 floor（发言权）概念**。

### 2.3 MetaGPT
- **模式**：`Environment` 持有角色；`publish_message` 遍历 `member_addrs`，按消息路由（`send_to`/`cause_by`）调用 `role.put_message()` 放入角色私有消息缓冲；`Environment.run()` 每轮 `asyncio.gather` 所有非 idle 角色的 `run()`（[base_env.py](https://raw.githubusercontent.com/FoundationAgents/MetaGPT/main/metagpt/environment/base_env.py)）。角色通过 `_watch` 声明关注的 Action 类型，即基于“因果类型”的订阅。
- **可借鉴**：“每个角色私有 inbox + 环境负责路由”的结构与我们一致；但其 run 是**轮次同步（barrier）**的，一个慢角色拖住整轮，不满足“随时发言”。

### 2.4 CAMEL / ChatArena / Sotopia（据公开资料）
- **CAMEL**：角色扮演双 agent 轮流对话（RolePlaying），后续 Workforce 引入任务分派；本质是同步回合。
- **ChatArena**：`Arena` + `Environment` + `Player`，环境（含 Moderated Conversation，由 moderator 判定终止/下一位）决定 `get_next_player()`，严格 step 式回合——与我们“规则与引擎解耦”的思想接近（环境描述规则），但无异步。
- **Sotopia**：社交智能评测环境，多个 episode 用 asyncio 并发，但单个 episode 内仍是轮流行动（[sotopia server.py](https://github.com/sotopia-lab/sotopia/blob/5f342a96d1991f85fc6bdd3fb566c77e0dd43afb/sotopia/server.py)）。
- **结论**：这几个框架的“并发”大多在 episode 之间，而非 agent 之间的实时异步。

### 2.5 OpenAI Agents SDK — Realtime Agents
- **模式**：长连接 `RealtimeSession`，随时 `send_message()`；事件流含 `audio_interrupted`、`tool_start/end`、`handoff`、`history_updated` 等；用户打断时会话发出中断事件并**截断历史到用户实际听到的位置**（`RealtimePlaybackTracker`）；可关闭自动 turn detection，改为应用自己决定何时 `response.create`（[Realtime agents guide](https://raw.githubusercontent.com/openai/openai-agents-python/1705dd62509ebfb3e85e26baedbe43e9f183580f/docs/realtime/guide.md)）。
- **关键细节（强烈推荐借鉴）**：文档要求自定义 transport 的 `send_event_if()` “必须在实际的事件提交边界重新检查条件，或把条件检查与事件提交串行化”，否则检查与提交之间可能有新 response 插入——这就是我们需要的 **commit-time validation（提交时校验）**。输出 guardrail 采用**按文本长度 debounce** 的检查；guardrail 触发时只取消“对应的那个 response”（response-scoped cancel），不误伤之后开始的 response。
- **可借鉴**：①“只有已被听到/已提交的才算历史”→ 我们的 worker 草稿在提交前不进入任何频道历史；②取消要带作用域 ID（generation_id），防止迟到的取消误杀新任务；③“手动 turn control”≈ 主持人控制 floor。

### 2.6 Letta / MemGPT
- **Sleep-time agents**：主 agent 负责即时响应，另一个后台 agent 在空闲时异步整理记忆块（memory blocks），不在关键路径上，可用更强更慢的模型；风险是“静默漂移”，建议对块做版本化与 diff（[Letta 记忆块与 sleep-time 计算解读](https://raw.githubusercontent.com/rohitg00/ai-engineering-from-scratch/be7e637b7ce54c47ea080cc163c28ac2614fd457/phases/14-agent-engineering/08-memory-blocks-sleep-time-compute/docs/en.md)）。Letta 也提供 agent 间异步消息工具（据公开资料）。
- **可借鉴**：把“局势总结/嫌疑表更新”放进后台 worker 的低优先级任务，前台发言只读最新已提交的摘要版本；摘要带 version，提交时检查是否基于过期版本。

## 3. LLM 狼人杀 / Mafia 专项项目

| 项目 | 发言调度 | 异步性 | 对我们的启示 |
|---|---|---|---|
| [Werewolf Arena (Google, 2024)](https://arxiv.org/html/2407.13943) | 每轮所有 agent **出价 0–4**（4=被点名必须回应），最高者发言，平局时上一轮被提及者优先；规则型 GM 维护进度并写入记忆；辩论上限 8 轮 | 回合制（并行出价，串行发言） | 出价机制可作为“floor 申请”的结构化信号；“被点名”优先级很实用 |
| AgentScope 狼人杀示例 | MsgHub + 顺序/并行 pipeline | async 但回合式 | 频道隔离（狼人夜聊）的组织方式 |
| [Time to Talk / LLMafia (EMNLP 2025 Findings)](https://aclanthology.org/2025.findings-emnlp.608/) | **scheduler（决定何时说，输出 `<wait>/<send>`）+ generator（决定说什么）** 两阶段；按自己发言率与 1/n 比较动态切换“多说/多听”提示；按每秒一词模拟打字延迟 | **真正异步**，与人类混合对局 | 见下 |
| Avalon 系列（如 ReCon 等） | 固定顺序发言 | 同步 | 递归思考/视角推理可放进 worker |

**Time to Talk 的关键设计细节**（[ar5iv 全文](https://ar5iv.labs.arxiv.org/html/2506.05309)，代码 [niveck/LLMafia](https://github.com/niveck/LLMafia)）：agent 循环“采样决定是否发言”；**处理过程中到达的新消息不改变当前上下文，也不影响本次决定和生成**——即它采用“不取消、不校验”的最简策略，作者类比人类“边打字边有人插话”。论文还发现“最话痨者最容易被投出”，并在未来工作中提出“先生成候选再由 scheduler 决定是否发送”。其灵感来自 SAUCE 框架（外部 host 编排、允许参与者选择不发言）。
- **对我们的启示**：①“何时说”与“说什么”拆成两个模型调用/两个阶段，便宜的 scheduler 可高频运行；②它缺少的正是我们要补上的——**提交前的陈旧性检查**（draft 基于的事件序号落后太多、或 floor 已关闭则丢弃/重写）；③发言率自适应可作为反刷屏手段。

## 4. “可中断 LLM Worker”模式对比

| 策略 | 做法 | 优点 | 缺点 | 适用 |
|---|---|---|---|---|
| Cancel-in-flight | 新输入到达即 `task.cancel()` 当前生成，带新上下文重启 | 响应最新局势 | 浪费 token；高频消息下可能“永远说不出话”（饥饿） | 高优先级事件：被点名、主持人直接提问、floor 关闭 |
| Queue（Enqueue） | 当前生成完成后再处理新输入 | 简单、确定 | 输出可能已过时 | 夜间行动、投票等单次决策 |
| Merge/Coalesce | 生成期间新消息累积在 inbox，结束后一次性合并为下一轮上下文 | 吞吐高，不浪费 | 仍可能发出过时话语 | 公共讨论背景消息 |
| Debounce | 新消息到达后等待安静窗口（如 300–800ms）再启动思考 | 减少无效调用 | 增加延迟 | 群聊爆发期 |
| Commit-time validation | 生成完成后、写入频道前，在引擎侧原子地检查 floor/phase/state_version；失败则丢弃或“轻量重写” | 保证一致性，配合任何上述策略 | 需引擎提供版本号与原子提交 | **必选** |
| Rollback | 撤销本轮并重来 | 状态干净 | 实现复杂 | 一般不需要 |

要点：cancel 并非“免费”——AgentScope 强调取消后需合成工具结果以保持历史一致；OpenAI Realtime 强调取消要按 response ID 作用域、校验要与提交串行化。

## 5. 推荐方案（纯 asyncio 单进程）

### 5.1 总体结构
1. **Engine（唯一真相源，单协程或带 `asyncio.Lock` 的串行提交器）**：持有 `state_version`（单调递增）、`phase`、每个频道的 `floor` 状态（open/closed/指定发言人）、序列化事件日志。所有写操作走 `engine.submit(action)`，在锁内做规则/权限/floor/版本校验并追加事件，返回 `Accepted(seq)` 或 `Rejected(reason="no_floor"|"stale"|...)`。规则来自 Markdown 场景编译出的策略对象，引擎只执行通用校验原语。
2. **EventBus + Channel 订阅（自研，~100 行）**：频道即 topic（借鉴 AutoGen 的 topic/subscription），订阅表决定可见性；分发时对每个订阅者 `inbox.put_nowait(event)`，SSE 也是一个订阅者。玩家可订阅多个频道；“私下质询主持人”使用 `dm:{player}:moderator` 频道 + request/response（带 correlation_id 的 Future）。
3. **AgentActor（前台）**：每个 agent 一个协程，循环 `await inbox.get()`，**永不 await LLM**。职责：维护本地观察缓冲；按 policy 决定对 worker 发 `start/cancel/merge`；接收 worker 的 `DraftReady(draft, based_on_seq, gen_id)` 并调用 `engine.submit`；处理 `Rejected(no_floor)` 通知。它也可以随时发出不需 LLM 的即时输出（如“我要求发言”的 floor 申请、ack、模板化回应）。
4. **Worker（后台）**：每个 agent 至多一个活动生成任务 `asyncio.Task`，带 `gen_id`；可选第二个低优先级“sleep-time”任务做局势摘要。结果通过 actor 的 inbox 回送（pipe-to-self），worker 不直接写引擎。
5. **Supervisor**：用 `asyncio.TaskGroup`（3.11+）管理 actor/worker；worker 异常 → 记录并按策略重启（指数退避），actor 崩溃 → 重建并从事件日志重放其可见事件；整局结束时统一取消。全局 `asyncio.Semaphore` 限制并发 LLM 请求数（13 个 agent 同时思考会打爆速率限制），并对每个调用 `asyncio.timeout()`。

### 5.2 取消与提交策略（建议默认值）
- 事件分级：`URGENT`（被点名、主持人提问、floor 关闭/转给自己、阶段切换）→ cancel-in-flight 并重启；`NORMAL`（他人公共发言）→ merge + debounce（如 500ms）；`LOW`（无关频道）→ 仅入缓冲。
- **防饥饿**：同一 worker 被连续取消 N 次（如 2 次）后，下一次不再因 NORMAL 事件取消，改为 merge，依靠提交时校验兜底。
- **提交时校验**：draft 携带 `based_on_seq`；引擎检查 ① phase 匹配 ② floor 允许该发言人在该频道发言 ③ `current_seq - based_on_seq` 内是否出现“关键事件”（定向提问、死亡、身份公开等，由规则标记 `invalidates_drafts=True`）。若仅有普通消息则接受（像 Time to Talk 那样容忍轻微过时），若有关键事件则返回 `stale`，actor 选择丢弃或触发“短重写”（把新事件和旧草稿一起给模型，要求最小修改）。
- **取消安全**：捕获 `CancelledError` 后清理并重新抛出；被取消的工具调用合成占位结果（借鉴 AgentScope）；迟到的 `DraftReady` 若 `gen_id` 不是当前值直接丢弃（借鉴 OpenAI Realtime 的 response-scoped cancel）。
- **floor 关闭**：主持人发出 `CloseFloor(channel)` 事件，作为 URGENT 广播；玩家 actor 可立即取消 worker（省 token），但正确性依赖引擎拒绝——即使竞态下草稿已到达，`submit` 仍返回 `Rejected(no_floor)` 并私信“当前无发言权”。
- **发言节奏**：可选借鉴 Werewolf Arena 的出价（0–4，被点名=4）作为 floor 申请优先级，以及 Time to Talk 的发言率自适应与打字延迟，避免 12 个 agent 同时抢话。

### 5.3 依赖 vs 自研
- **自研（推荐）**：mailbox（`asyncio.Queue`，建议有界 + 对低优先级事件做合并/丢弃）、EventBus/频道订阅、supervisor、commit 校验、取消策略。这些都是游戏语义强相关、代码量小（合计数百行），外部框架反而会限制 floor/版本校验的位置。
- **可依赖**：Python 3.11+ 标准库（`TaskGroup`、`asyncio.timeout`）；`anyio`（可选，结构化并发与取消作用域更清晰）；`sse-starlette`（SSE 推送）；`pydantic`（消息/事件 schema）；LLM SDK 本身（OpenAI/Anthropic 异步客户端均支持任务取消时关闭流）；`tenacity`（重试）。
- **只借鉴、不依赖**：AutoGen-core（topic/subscription 语义）、AgentScope（取消一致性处理、MsgHub）、LangGraph（四种 double-texting 策略分类、checkpoint 思想）、OpenAI Agents SDK Realtime（提交边界校验、按 ID 作用域取消、debounce guardrail）、Letta（后台记忆整理）、Time to Talk（scheduler/generator 拆分）。Akka/Orleans/pykka/thespian/Ray 仅作概念参考，单进程 asyncio 下引入它们得不偿失。

**一句话结论**：采用“Engine 串行提交 + 频道 pub/sub + 每 agent 一个 `asyncio.Queue` 收件箱的前台 actor + 带 gen_id 的可取消后台 worker”的结构；取消策略按事件优先级分级（urgent 取消、normal 合并+防抖、带防饥饿上限），并以**引擎侧原子的提交时校验**（floor/phase/基于版本的陈旧检测）作为最终正确性保障——取消只是省 token 的优化，校验才是正确性的来源。
