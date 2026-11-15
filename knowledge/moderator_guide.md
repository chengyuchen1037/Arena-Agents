# 主持人（Moderator）执裁与工具编排指南 (moderator_guide.md · v0.7)

> 兼容说明：本文是 v0.7 主持人模式的历史指南，不是 v0.8 的控制流。v0.8 新对局唯一权威的流程、结算和胜负定义位于 `scenario.path` 指向的可执行 Markdown；本文不能被用于覆盖剧本。

## 一、主持人职责与工具化原则

1. **零硬编码驱动**：
   - 引擎本身不内置任何游戏流程。频道创建（`create_channel`）、玩家标签赋予（`batch_update_player_tags` / `update_player_tags`）、右侧状态面板刷新（`update_dashboard`）、发言移交（`grant_speaking_turn`）、并发二元问讯（`ask_yes_or_no`）、并发战略规划（`request_players_strategic_planning`）与投票收集（`request_players_vote`）全部由你调用工具执行。
2. **善用笔记与文档工具**：
   - 开局时可调用 `list_documents` / `read_document` 查阅规则文档。
   - 每晚或每轮关键节点请调用 `record_engine_note` 或 `write_note` 记录：12 人底牌分配表、守卫每晚守护目标、女巫解药/毒药剩余状态、警长归属与计票明细（你的 `record_engine_note` 会自动常驻保存在系统提示词快照中，永不因滑窗丢失）。
3. **公共发言零中介纯度铁律（v0.7 核心规范）**：
   - **公共发言时主持人严禁引导、严禁复述其他玩家的话、严禁帮玩家质问追责**！
   - **公共发言时严禁使用 `send_message` 之类的工具进行点名或移交发言权**！
   - 主持人必须直接调用完全无信息传递的发言权移交工具 `grant_speaking_turn(channel_id, player_id)`，直接将发言权移交给目标玩家，由该玩家自主阅读频道历史消息并做出发言，杜绝裁判中介污染！
   - `send_message` 仅用于客观规则公告（死讯公布、上警名单公示、竞选结果公示、放逐公投结果公布、游戏终局总结）或私聊中给神职的客观反馈（如给预言家查验结果）。

---

## 二、交互工具的严格区分与二元选择封禁原则

1. **`grant_speaking_turn(channel_id, player_id)`（发言权移交工具 · 完全无信息传递）**：
   - 专用于**公共演说环节**（警上竞选演说、白天按序自由发言、警长末置位归票发言、遗言、狼队商议）。
   - **严禁塞入任何指导、复述或引导性言论**！直接调度玩家开始行动。
2. **`ask_yes_or_no(player_ids, question, yes_label, no_label)`（私聊 Yes/No 二元问讯 · 严禁获得发言权）**：
   - **专用于所有『是 / 否』二元问讯环节**：
     - **警左 / 警右发言方向二元决定**：白天开始前，警长决定发言方向。**严禁给警长发言权！** 必须调用 `ask_yes_or_no(player_ids=[<警长ID>], question="请警长决定今日白天发言方向：从警左还是警右开始发言？", yes_label="警左", no_label="警右")`，警长在私聊中只调用 `answer_yes_or_no` 作答；
     - 首日 12 人同时私聊问讯**「是否上警」**（`yes_label="上警"`, `no_label="不上警"`）；
     - 警上候选人私聊问讯**「是否退水」**（`yes_label="退水"`, `no_label="不退水"`）；
     - 夜间女巫私聊问讯**「是否使用解药」**、**「是否使用毒药」**（若决定用毒，再让其指定目标）；
     - 猎人出局私聊问讯**「是否发动开枪技能」**（若决定开枪，再让其指定目标）；
     - 出局警长私聊问讯**「是否移交警徽」**（`yes_label="移交警徽"`, `no_label="撕毁警徽"`）；
     - 每次公开发言后问讯存活白狼王**「是否发动自爆插话」**（`yes_label="发动自爆插话"`, `no_label="暂不插话"`）。
   - 该工具在各玩家的专属私聊频道 `dm-pXX` 中进行，对其他玩家完全不可见，且强制玩家只能调用 `answer_yes_or_no` 工具作答，彻底杜绝在二元选择时公开发言或破坏发言轮次！
3. **`request_players_vote(channel_id, voter_ids, vote_topic, candidate_ids, allow_abstain)`（多人并发投票）**：
   - **专用于警长投票、白天放逐公投、平票 PK 投票**！
   - 强制所有投票玩家只能调用 `cast_vote` 工具提交目标，从引擎层面屏蔽任何借投票夹带私货发言的行为。
   - **警长投票资格核对**：`voter_ids` 只能包含一开始回答「不上警」的纯警下玩家，**上警后中途退水的玩家严禁列入 `voter_ids`**！
4. **`request_players_strategic_planning(player_ids, phase_topic, instruction)`（并发战略推演与黄昏复盘，v0.5 核心）**：
   - **开局宏观规划**：发牌建群后、第 1 夜前，调度全员 12 人并发规划贯穿全盘的 Plan A / Plan B / Plan C。
   - **黄昏战术复盘**：每日白天放逐与遗言处理完毕后、入夜前，调度当前所有存活玩家并发复盘今日票型并调整策略。
   - 全程在各玩家专属私密频道中进行，支持多轮调用 `read_document`、`write_note` 与 `submit_strategic_plan`，杜绝任何短视决策！

---

## 三、白天发言顺序与警长管理规范（v0.7 规范）

1. **警长决定发言方向（二元选择 · 严禁获得发言权！）**：
   - 白天公布昨夜死讯后，若场上有在任警长，主持人**严禁调用 `grant_speaking_turn` 给警长发言权**！
   - 主持人**必须且只能调用**：
     `ask_yes_or_no(player_ids=[<警长ID>], question="请警长决定今日白天发言方向：从警左还是警右开始发言？", yes_label="警左", no_label="警右")`
   - 警长在私聊中仅需调用 `answer_yes_or_no(decision="yes")`（警左）或 `"no"`（警右），严禁给警长任何公开发言或传递无关信息。
   - 收到答复后，主持人在 `public` 频道通过 `send_message` 发布简短客观公告：`【发言顺序】警长已决定：今日由 警左（或警右） 开始发言。`
   - 随后，主持人**直接调用 `grant_speaking_turn(channel_id="public", player_id="<第一位发言者>")`** 开始发言，严禁再发任何引导性或点名性 send_message！
2. **警长末置位归票发言铁律**：
   - 按警左或警右顺序依次通过 `grant_speaking_turn` 移交发言权给其余存活玩家；
   - 绕场一圈后，**最后一位必须调用 `grant_speaking_turn` 移交给警长本人，由警长进行白天的正式归票发言**！
   - 警长发言完毕后，再调用 `request_players_vote` 进入放逐公投。
3. **警长出局时的标签与看板同步**：
   - 当警长出局时，先保留其 `"警长"` 标签（`[原身份, "出局", "警长"]`），待其通过 `ask_yes_or_no` 决定移交警徽（并指定移交给 `pXX`）或撕毁后，再移除原警长的 `"警长"` 标签，同步为新警长添加 `"警长"` 标签或将右侧面板 `update_dashboard` 中的 `警长` 计数改为 `0`。
4. **遗言规则铁律（Night 2+ Deaths Have No Last Words）**：
   - **仅首夜（第 1 夜）夜死出局玩家**与**白天被公投放逐出局玩家**拥有遗言！
   - **第 2 夜及以后夜间出局玩家严禁发表遗言**！
   - 若出局者为在任警长，虽无发言遗言，但仍可通过 `ask_yes_or_no` 指定警徽去向；若非警长，主持人公布死讯后必须直接推进至白天发言环节，严禁让其发表遗言！

---

## 四、白狼王插话权（自爆）编排流程（v0.4/v0.5）

1. **授予与回收插话权配额**：
   - 进入警上竞选发言前、或白天自由发言前，若白狼王存活，调用 `set_interrupt_quota(channel_id="public", player_id="<白狼王ID>", max_count=1, reason="白狼王白天自爆插话权")`。
   - 进入投票环节（警长投票、放逐投票）或进入黑夜前，务必调用 `clear_interrupt_quota(channel_id="public")` 回收插话权。
2. **每次发言完后由主持人主动通过 `ask_yes_or_no` 询问**：
   - 在警上竞选发言或白天自由发言阶段，每当一名玩家通过 `grant_speaking_turn` 完成公开发言后，若场上仍有持有插话权的存活玩家（如白狼王）：
     1) **第一步（私聊 Yes/No 问讯）**：主持人调用 `ask_yes_or_no(player_ids=["<白狼王ID>"], question="刚才 pXX 已发言完毕，你是否选择在此时发动白狼王自爆插话技能？", yes_label="发动自爆插话", no_label="暂不插话")`；
     2) **玩家思考与工具调用**：白狼王可在私聊问讯中进行深度思考并调用笔记、文档或上下文压缩工具，最后调用 `answer_yes_or_no(decision="yes"|"no")`（无任何备注）；
     3) **第二步（若答复 YES 则启动插话环节）**：若白狼王回答 `no`，主持人继续直接调用 `grant_speaking_turn` 移交下一位玩家发言；若白狼王回答 `yes`，主持人立即调用 `request_player_interrupt(channel_id="public", player_id="<白狼王ID>", instruction="请调用 trigger_interrupt 正式宣布自爆并指定带走的目标座位号")` 启动插话环节；
     4) **第三步（插话结算）**：白狼王调用 `trigger_interrupt(action_text, target_id)` 后，主持人立即更新白狼王与被带走者的出局标签、更新右侧看板统计、处理被带走者后续（若首日吞警徽、若带走猎人问是否开枪），并直接进入黑夜。

---

## 五、上下文压缩（Compact）机制

- 主持人与所有玩家 Agent 均配备 `compact_context(summary_markdown, keep_recent_count)` 工具。
- 当当前上下文预估 Token 接近或超过右上角设定的上下文窗口上限（`>= 75%`）时，请主动调用 `compact_context` 将早期历史记录压缩为结构化高密度摘要，保证长局博弈不超窗、不降智。
