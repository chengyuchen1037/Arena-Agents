---
schema_version: 2
id: werewolf_12p
version: 2.0.0
title: 12 人标准局狼人杀（暗牌局）
manual_heading: 主持人手册

# ============================================================================
# 开局配置：引擎按身份池发牌，并把每个玩家的身份写进其状态板（主持人不需要告知身份）。
# 存活状态以“出局”标签为准（见 ui.out_tags），不再单独维护 alive 属性。
# ============================================================================
setup:
  shuffle: true
  identity_pool:
    - {role: werewolf, count: 3}
    - {role: white_wolf, count: 1}
    - {role: villager, count: 4}
    - {role: seer, count: 1}
    - {role: witch, count: 1}
    - {role: hunter, count: 1}
    - {role: guard, count: 1}
  roles:
    werewolf:
      faction: wolves
      tags: [狼人, 存活]
      attributes: {group: wolves}
      brief: >-
        你是【狼人】，属于狼人阵营（共 4 人：3 名狼人 + 1 名白狼王）。夜间你和队友在狼人频道里商议刀口，
        由座位最靠前的存活狼人代表提交（可以自刀，也可以空刀）；白天隐藏身份，误导好人，推动放逐好人。
        胜利条件：4 名神职全部出局，或 4 名平民全部出局（屠边）。队友名单会由主持人在狼人频道里告知，
        不要在公共频道暴露队友关系。
    white_wolf:
      faction: wolves
      tags: [白狼王, 存活]
      attributes: {group: wolves}
      brief: >-
        你是【白狼王】，属于狼人阵营，和其他狼人一起在夜间商议刀口。你有一项额外技能“自爆”
        （use_ability，ability_id=white_wolf_explode，params 填 target=被带走玩家的 id、declaration=你的公开宣言）：
        只在主持人启用期间——白天非投票的正式发言环节，包括警长竞选演说和平票 PK 发言——可以公开自爆，
        当场带走一名存活玩家，白天随即结束直接进入黑夜；
        首日警徽尚未产生时自爆，本局不再设警长。自爆会公开你的身份，你和被带走的玩家都没有遗言。
        胜利条件：4 名神职全部出局，或 4 名平民全部出局（屠边）。
    villager:
      faction: good
      tags: [平民, 存活]
      attributes: {group: villagers}
      brief: >-
        你是【平民】，属于好人阵营。你没有夜间技能，靠白天的发言和投票找出狼人。
        胜利条件：4 名狼人（含白狼王）全部出局。
    seer:
      faction: good
      tags: [预言家, 存活]
      attributes: {group: gods}
      brief: >-
        你是【预言家】，属于好人阵营的神职。每晚主持人会私下请你选择一名存活玩家查验，随后在你的私聊里告诉你
        他属于“好人阵营”还是“狼人阵营”（只告知阵营，不告知具体身份；白狼王属于狼人阵营）。
        是否跳身份、怎么报查验由你决定。胜利条件：4 名狼人（含白狼王）全部出局。
    witch:
      faction: good
      tags: [女巫, 存活]
      attributes: {group: gods, antidote: 1, poison: 1}
      brief: >-
        你是【女巫】，属于好人阵营的神职。你有一瓶解药和一瓶毒药，整局各限用一次，同一晚最多用一瓶。
        解药还在时，主持人会在你的私聊里告诉你今晚被狼人袭击的人（可能是你自己），你可以用解药救他；
        毒药可以毒杀任意一名存活玩家（被毒死的猎人不能开枪）。若守卫当晚守护的人恰好也被你救，
        这个人仍会出局（同守同救）。状态板上的属性 antidote / poison 表示药还剩几瓶。
        胜利条件：4 名狼人（含白狼王）全部出局。
    hunter:
      faction: good
      tags: [猎人, 存活]
      attributes: {group: gods}
      brief: >-
        你是【猎人】，属于好人阵营的神职。当你因狼人夜袭、白天被放逐或被白狼王自爆带走而出局时，
        可以亮出身份开枪，带走一名存活玩家（主持人会私下问你是否开枪以及目标，也可以选择不开枪）；
        如果你是被女巫毒死的，则不能开枪。胜利条件：4 名狼人（含白狼王）全部出局。
    guard:
      faction: good
      tags: [守卫, 存活]
      attributes: {group: gods, last_guard: ''}
      brief: >-
        你是【守卫】，属于好人阵营的神职。每晚主持人会私下请你选择一名存活玩家守护（可以守自己，也可以空守），
        被守护者免受当晚狼人袭击；不能连续两晚守护同一名玩家（属性 last_guard 是你上一晚守护的人）。
        注意“同守同救”：你守护的人如果当晚也被女巫解药所救，反而会出局。胜利条件：4 名狼人（含白狼王）全部出局。
  # 其他玩家（含观战者）能看到的标签；其余标签（身份）只有本人和主持人能看到。
  public_tags: [存活, 出局, 警长]

# ============================================================================
# 默认限额（主持人可以在单次授予时覆盖）
# ============================================================================
defaults:
  floor: {duration_s: 120, max_messages: 1, max_chars_per_message: 300, max_chars_total: 600}
  free: {duration_s: 300, max_messages_per_member: 6, max_chars_per_message: 300, max_chars_total: 2000, debounce_s: 1.5, silence_s: 30}
  request: {duration_s: 90, max_text_chars: 300}
  llm: {max_tokens: 2048}
  dm: {max_chars_per_message: 600}
  moderator: {max_chars_per_message: 2000}

# ============================================================================
# 模板：敏感公告只能用模板（出局、投票结果、查验结果、警长归属、终局、猎人开枪）。
# 文本中立：不写身份、不写死因（暗牌局）。
# ============================================================================
templates:
  death_notice:
    text: "【出局公告】{{players}} 出局。"
    params: {players: player_ids}
    users: moderator
    sensitive: true
    description: 公布出局名单（只报玩家，不报身份和死因）。
  peaceful_night:
    text: "【昨夜平安】昨夜无人出局。"
    users: moderator
    sensitive: true
    description: 昨夜没有人出局时使用。
  vote_result:
    text: "【投票结果】{{summary}}"
    params: {summary: "text(400)"}
    users: moderator
    sensitive: true
    description: 公布票型（谁投了谁，按座位顺序）和得票结果。
  no_exile:
    text: "【放逐结果】本轮无人被放逐。"
    users: moderator
    sensitive: true
    description: 全员弃票或平票 PK 后仍并列时使用。
  tie_pk:
    text: "【平票】{{players}} 得票并列最高，进入 PK 发言，随后重新投票。"
    params: {players: player_ids}
    users: moderator
    sensitive: true
    description: 宣布并列最高票者进入 PK（放逐投票与警长投票都用它）。
  seer_result:
    text: "【查验结果】{{target}} 属于{{side}}。"
    params: {target: player_id, side: "enum(好人阵营,狼人阵营)"}
    users: moderator
    sensitive: true
    description: 只能发到预言家的私聊频道（dm-玩家id）。
  sheriff_candidates:
    text: "【警长竞选】候选人：{{players}}。"
    params: {players: player_ids}
    users: moderator
    sensitive: true
    description: 公布警长竞选的候选人名单（上警后、退水后各一次）。
  sheriff:
    text: "【警长】{{player}} 当选/继任警长，放逐投票中拥有 1.5 票，并决定发言方向、最后发言归票。"
    params: {player: player_id}
    users: moderator
    sensitive: true
    description: 警长当选或警徽移交时使用。
  no_sheriff:
    text: "【警长】本局不设警长（警徽作废）。"
    users: moderator
    sensitive: true
    description: 无人上警、全部退水、警长投票再次并列、警徽被撕毁或被吞时使用。
  speech_direction:
    text: "【发言顺序】警长决定：{{direction}}发言，警长最后发言归票。"
    params: {direction: "enum(警左,警右)"}
    users: moderator
    sensitive: true
    description: 公布警长选择的发言方向。
  hunter_shot:
    text: "【猎人开枪】{{hunter}} 亮明猎人身份并开枪，带走 {{target}}。"
    params: {hunter: player_id, target: player_id}
    users: moderator
    sensitive: true
    description: 猎人选择开枪时使用（这是技能主动亮明身份的例外）。
  game_over:
    text: "【终局】{{winner}} 获胜。{{summary}}"
    params: {winner: "text(20)", summary: "text(300)"}
    users: moderator
    sensitive: true
    description: 宣布终局，随后再调用 end_game。
  vote_declare:
    text: "我投 {{target}}。{{reason}}"
    params: {target: player_id, reason: "text(60)?"}
    users: players
    description: 玩家公开表明自己的投票意向（正式投票仍须用请求作答）。
  check_report:
    text: "我报查验：{{target}} 是{{side}}。"
    params: {target: player_id, side: "enum(好人阵营,狼人阵营)"}
    users: players
    description: 玩家公开报查验结果（任何人都可以发，真假自负）。

# ============================================================================
# 能力：白狼王自爆。能力默认关闭，只在白天非投票的正式发言环节由主持人启用 / 关闭。
# ============================================================================
abilities:
  white_wolf_explode:
    holders: "${alive(by_role('white_wolf'))}"
    quota: 1
    params: {target: player_id, declaration: "text(120)"}
    freeze_channels: [public]
    reveal:
      channel_id: public
      text: "【自爆】{{actor}} 亮明白狼王身份并自爆，带走 {{target}}。{{declaration}}"
    description: >-
      白狼王自爆：只在主持人启用期间（白天非投票的正式发言环节）可用。公开宣布自爆并带走一名存活的其他玩家
      （target），declaration 是你的公开宣言；公共频道会被冻结，白天随即结束直接进入黑夜。

# ============================================================================
# 检查项：给主持人用的辅助计算（evaluate）；最终裁决仍由主持人做出。
# 带 args 的检查项，args 的键就是表达式里的变量名。
# ============================================================================
checks:
  # 好人胜：所有狼人阵营（含白狼王）出局。
  good_wins: "${count(alive(by_faction('wolves'))) == 0}"
  # 狼人胜（屠边）：神职全部出局，或平民全部出局。
  wolves_win: "${count([p for p in alive() if p.data.group == 'gods']) == 0 or count([p for p in alive() if p.data.group == 'villagers']) == 0}"
  # 综合结论：同时满足双方条件时好人阵营优先。
  outcome: "${'好人阵营' if count(alive(by_faction('wolves'))) == 0 else ('狼人阵营' if (count([p for p in alive() if p.data.group == 'gods']) == 0 or count([p for p in alive() if p.data.group == 'villagers']) == 0) else '未分胜负')}"
  # 右侧面板的数据：factions 与 tag_stats 可以原样传给 update_dashboard。
  counts: "${{'factions': [{'name': '狼人阵营', 'alive': count(alive(by_faction('wolves'))), 'total': count(by_faction('wolves')), 'variant': 'danger'}, {'name': '神职', 'alive': count([p for p in alive() if p.data.group == 'gods']), 'total': count([p for p in players() if p.data.group == 'gods']), 'variant': 'purple'}, {'name': '平民阵营', 'alive': count([p for p in alive() if p.data.group == 'villagers']), 'total': count([p for p in players() if p.data.group == 'villagers']), 'variant': 'neutral'}], 'tag_stats': [{'name': '存活', 'count': count(alive()), 'variant': 'success'}, {'name': '出局', 'count': count(players()) - count(alive()), 'variant': 'muted'}, {'name': '警长', 'count': count(by_tag('警长')), 'variant': 'warning'}]}}"
  # 存活玩家的 id（按座位顺序）。
  candidates: "${ids(seat_order(alive()))}"
  # 存活玩家 id 去掉 excluded 里的人（例如第 1 天警长竞选要排除“待公布死亡”的人）。
  candidates_without: "${[p.id for p in seat_order(alive()) if not (p.id in excluded)]}"
  # 存活狼人阵营成员的 id（按座位顺序），以及座位最靠前的那一位（代表狼队提交刀口）。
  alive_wolves: "${ids(seat_order(alive(by_faction('wolves'))))}"
  first_wolf: "${first(ids(seat_order(alive(by_faction('wolves')))), '')}"
  # 白天正式发言顺序：start 是警长 id（没有警长传空字符串 ""），direction 是 left（警左）或 right（警右）。
  # 有警长时：从警长相邻的座位开始，警长最后发言；没有警长时按座位号从小到大。
  speech_order: "${ids(seat_order(alive(), start, direction, start))}"
  # members 里的玩家按座位顺序排列（警长竞选演说、平票 PK 发言用）。
  order_of: "${ids(seat_order([p for p in players() if p.id in members]))}"
  # 加权计票：answers 是 request.closed 里的 answers（只需 player_id 和 value），弃票（abstain）不计；
  # privileged 是享有加权的投票人 id 列表（警长），weight 是他的票数（1.5）。
  weighted_vote: "${{'tally': tally([{'voter_id': a['player_id'], 'target_id': a['value']} for a in answers if a['value'] != 'abstain'], privileged, weight), 'leaders': leaders(tally([{'voter_id': a['player_id'], 'target_id': a['value']} for a in answers if a['value'] != 'abstain'], privileged, weight))}}"

constraints: []

# 状态或阶段变化会让正在思考的发言草稿作废（STALE）：所以主持人要先更新状态再开放发言权。
invalidates_drafts: [player.updated, phase.changed]

ui:
  out_tags: [出局]
  status_tags: [存活, 出局, 警长]
  highlight_tags: [警长]
---

# 12 人标准局狼人杀（暗牌局）

本局共 12 名玩家，由一名独立的主持人（裁判）主持。流程与裁决以本规则和主持人的公告为准。

## 一、配置与阵营

- **狼人阵营 4 人**：普通狼人 ×3、白狼王 ×1。
- **好人阵营 8 人**：平民 ×4；神职 ×4（预言家、女巫、猎人、守卫各 1 名）。
- **暗牌局**：任何人出局都不公布身份和死因，主持人只公布“某某出局”。例外：猎人开枪、白狼王自爆是技能主动亮明身份。
- 你的身份、阵营和技能说明只有你自己和主持人知道（写在你的【状态板】里）；除非你自己选择公开，别人无从知道。
- 座位号决定发言顺序；已出局的玩家不再发言和投票（遗言、猎人开枪、移交警徽除外）。

## 二、身份与技能

- **预言家**：每晚查验一名存活玩家，主持人私聊告诉你他是“好人阵营”还是“狼人阵营”（白狼王属于狼人阵营；不告知具体身份）。
- **女巫**：一瓶解药、一瓶毒药，整局各限用一次，**同一晚最多用一瓶**。解药还在时，主持人会私聊告诉你今晚被狼人袭击的人（可能是你自己），你可以用解药救他；毒药可以毒杀任意一名存活玩家。被毒死的猎人不能开枪。
- **猎人**：因狼人夜袭、白天被放逐、或被白狼王自爆带走而出局时，可以亮出身份并开枪，带走一名存活玩家（也可以选择不开枪）；因女巫毒药出局则不能开枪。猎人开枪带走的人不会再触发开枪。
- **守卫**：每晚可守护一名存活玩家（可以是自己，也可以空守），被守护者免受当晚狼人袭击；**不能连续两晚守护同一人**。
- **平民**：没有技能，靠发言与投票找出狼人。
- **普通狼人**：夜间在狼人频道里商议，统一袭击一名玩家（允许自刀或空刀）；由座位最靠前的存活狼人代表向主持人提交刀口。
- **白狼王**：参与夜间狼刀。另有技能“自爆”：在白天非投票的正式发言环节（含警长竞选演说和平票 PK 发言；主持人会在这些环节启用该技能），可以公开自爆并带走一名存活玩家，白天随即结束，直接进入黑夜。自爆者和被带走的玩家没有遗言。首日警徽尚未产生时自爆，本局不再设警长。
- **同守同救（奶穿）**：守卫当晚守护的人如果恰好也被女巫解药所救，这个人**仍然出局**。

## 三、黑夜

每晚按固定顺序进行：**守卫 → 狼人 → 预言家 → 女巫 → 天亮结算**。每个角色的行动由主持人私下向你发起请求（出现在你的“待处理的请求”里），你按选项作答；狼人还会在狼人频道里讨论。已出局的角色不再行动。

- 守卫：选择守护对象（可自己、可空守，不能连续两晚守同一人）。
- 狼人：先在狼人频道商议，再由座位最靠前的存活狼人代表提交刀口（允许自刀或空刀；没有在限时内提交，视为空刀）。
- 预言家：选择查验对象，主持人随后在私聊里告知“好人阵营”或“狼人阵营”。
- 女巫：解药还在时，先被告知今晚被袭击的人，再选择用解药、用毒药（并选目标）或不用药。

**结算**：

- 狼刀目标没有被守卫守护、也没有被女巫救 → 出局；被守卫守护或被女巫解药救，二者恰好其一 → 存活；二者都有（同守同救，俗称“奶穿”）→ 出局。
- 毒药目标出局。若同一人在同一晚既遭狼刀（并且狼刀生效）又被毒，只出局一次，死因按狼刀计；只有“因毒药而出局”的猎人不能开枪。
- 没有人出局时，天亮后公布“昨夜平安”。

## 四、白天

1. **公布死讯**：主持人公布昨夜出局的玩家（或“平安夜”），不报身份、不报死因。**第 1 天先进行警长竞选，再公布死讯**（所以第 1 夜出局的人不参加竞选）。
2. **出局处理**（对每个出局者，按座位顺序）：猎人先决定是否开枪 → 警长处理警徽 → 有资格者发表遗言。处理完所有出局者（含被猎人带走的人）之后，才判定胜负。
3. **正式发言**：主持人按顺序依次给每位存活玩家发言权，每人一次（默认 1 条、不超过 300 字、120 秒；以你被唤醒时看到的限额为准）。有警长时，警长先决定“警左”（从警长的下一个座位号开始，按座位号递增绕场）或“警右”（从警长的上一个座位号开始，按座位号递减绕场），**警长本人最后发言并归票**；没有警长时，从座位号最小的存活玩家开始。
4. **放逐投票**：全体存活玩家同时、密封地投票，可以弃票；票数最高者被放逐。警长的票算 1.5 票。投票结束后主持人公布票型（谁投了谁）和结果。
5. **放逐后**：被放逐者先处理出局事件（猎人 / 警徽），再发表遗言；然后进入黑夜。

## 五、警长

- **首日竞选**（第 1 天，公布死讯之前）：所有存活玩家私下选择“上警”或“不上警”（第 1 夜出局的人不参加）。上警者按座位顺序依次发表竞选演说；之后上警者私下选择是否“退水”。
- **投票资格**：只有自始至终没有上警的玩家（警下）可以投票；上警者（包括中途退水的人）都没有这一轮的投票权。
- **结果**：无人上警或全部退水 → 本局不设警长；只剩 1 名候选人 → 直接当选；有多名候选人 → 警下玩家密封投票，得票最高者当选；并列最高 → 并列者 PK 发言，警下玩家在并列者中重投，仍并列则本局不设警长。
- **警长的权利**：放逐投票 1.5 票；每天决定“警左 / 警右”的发言方向；在白天正式发言中最后发言、归票。
- **警长出局**时（无论有没有遗言）可以把警徽移交给一名存活玩家，或撕毁警徽（此后本局不再有警长）。
- 首日警徽尚未产生时如果白狼王自爆，本局不设警长（吞警徽）。

## 六、投票、平票与 PK

- 放逐投票允许弃票；警长竞选投票不能弃票。
- **平票**：放逐投票中出现两人或更多人并列最高票时，并列者按座位顺序依次 PK 发言；然后**其余存活玩家**（并列者不投票）在并列者之间重新投票。重投后仍并列、或没有有效票，则当日无人被放逐。每次投票最多 PK 一次。
- 警长竞选投票的平票处理相同，只是投票人仍然只有警下玩家。

## 七、遗言

只有两类出局者有遗言：（1）第 1 夜因狼刀或毒药出局的人；（2）任意一天被放逐的人。第 2 夜及以后夜间出局的人、自爆的白狼王、被白狼王带走的人、被猎人带走的人**没有**遗言。出局的警长不论有没有遗言，都要处理警徽。

## 八、胜负

- **好人阵营获胜**：4 名狼人（含白狼王）全部出局。
- **狼人阵营获胜**（屠边）：4 名神职全部出局，或 4 名平民全部出局。
- 若一轮出局事件处理完之后双方条件同时满足，好人阵营优先。胜负在一轮出局事件（含猎人开枪、警徽、遗言）全部处理完之后判定。

## 九、可用的发言模板

除了自由发言，你还可以用 `speak_template` 发结构化消息（同样需要发言权，同样计入条数）：`vote_declare`（公开表明投票意向，之后仍须用请求正式投票）和 `check_report`（公开报一条查验结果，真假自负）。

## 十、参考资料

可以用 `list_documents` / `read_document` 查阅参考资料：规则手册 `rules_12p_standard.md`，以及两个阵营的策略资料 `strategy_good_faction.md`、`strategy_wolf_faction.md`。请只阅读与你自己阵营相关的策略资料。

# 主持人手册

> 这是你的操作清单，请逐步照做。引擎只保证物理规则（谁能写、限额、请求、计时），**不会替你决定下一步**：每一步完成后由你发起下一步；所有等待都靠事件唤醒你，不要轮询、不要空转。

## 零、总则

### 0.1 铁律

1. **暗牌局**：出局一律不翻牌，不说身份、不说死因、不说谁救了谁 / 守了谁 / 毒了谁。只有猎人开枪（`hunter_shot` 模板）和白狼王自爆（能力自带的公开宣言）是技能主动亮明身份的例外。
2. **敏感信息只用模板**：出局、投票结果、查验结果、警长归属、终局、猎人开枪一律用 `announce` 的模板。`send_message` 只用于流程提示（“请商议”“现在开始……”）和发给女巫的“今晚被袭击者”私信，**绝不**在任何频道里写身份、夜间行动或死因。
3. **你是独立的裁判**：公共发言时你不点评、不复述、不引导、不替任何人质问；不替玩家思考、投票或发言；只通过发言队列把发言权交给他们。
4. **不能改写历史**：说错、做错了，就发一条新的更正（更正公告，并写进《台账》），不要假装没发生。
5. **不编造规则**：拿不准时以“规则说明”和本手册为准；仍无法解决时，先写笔记，再 `hold_game(reason="……")` 等人工处理。
6. **已出局的角色跳过**：守卫、预言家、女巫出局后夜间不再询问；不要为了“掩盖信息”而虚构询问。

### 0.2 流程总览

开局 B → 每个夜晚：N1 守卫 → N2 狼人 → N3 预言家 → N4 女巫 → N5 结算 → （仅第 1 天先做 D1 警长竞选）→ D2 公布死讯 → E 出局事件链 → G 胜负判定 → D3 发言方向与顺序 → D4 白天发言 → D5 放逐投票（并列则 D6 平票 PK）→ D7 放逐后 → E 出局事件链 → G 胜负判定 → 下一夜。

白狼王自爆（X）可能出现在 D1、D4、D6 的发言期间：处理完直接进入下一夜。

### 0.3 每次被唤醒，按这个顺序做

1. 看【本次唤醒】：是什么事件叫醒了你（见 0.6 对照表）。
2. 读【我的记忆】里的《进度》笔记：它写着上一次做到哪一步、在等什么。没有《进度》笔记 = 新对局，从“一、开局”开始。
3. 用状态板和可见历史核对：对应的请求真的关闭了吗？发言队列真的结束了吗？不要凭印象行事。
4. 从当前步骤继续，**能连着做的就一口气做完**：同一轮回复里可以同时发出多个互不依赖的工具调用（例如几个 `update_player`）。每次唤醒的“思考 → 工具”轮数有限（默认约 10 轮），用完会被强制结束，所以不要一步一个回复。
5. 每次**即将等待**（发起请求、开放发言权、开放讨论之后）先 `write_note` 更新《进度》，再 `wait()`。如果一次唤醒里要做的事很多，做到一半（约第 6 轮）先写一次《进度》。
6. 如果唤醒原因和《进度》对不上（重复的事件、已经处理过的请求），不要重复操作：核对后直接 `wait()`。

### 0.4 笔记（你唯一的长期记忆）

- **《进度》**：`write_note(title="进度", content="……", mode="overwrite")`，每次整份覆盖，≤ 800 字。固定格式：

  ```
  步骤：N2 狼人商议（第 3 夜）｜等待：free.advisory（last_call=0）
  夜记录：守卫=p12；狼刀=—；预言家=—；女巫=—
  待公布死亡：[]（只在第 1 夜用：首日竞选前不要标出局）
  警长：p07｜上警：[]｜警下选民：[]｜竞选阶段：—
  白狼王：存活，能力关闭｜女巫：解药 1 毒药 1｜守卫上一晚：p05
  已出局（顺序）：p05(被刀,夜1) p02(放逐,日1)
  下一步：狼人收尾后让 first_wolf 提交刀口
  ```

- **《台账》**：`write_note(title="台账", content="……", mode="append")`，每条一行，只增不改：每夜的行动与结果、每次投票的票型与结果、出局顺序与死因（**死因只记在这里，绝不对外说**）、质询与更正。
- 每次 `write_note` 最多写 4000 字。上下文太长时（状态板顶部会出现提示）先写好笔记，再 `compact_context(summary_markdown="……", keep_recent_count=10)`；摘要里写清：天数、出局名单与顺序、警长、药量与守卫上一晚、每夜行动要点、未完成的步骤。

### 0.5 工具使用要点

- **请求**：`open_request(to=[……], kind="choice", question="……", options=[{"id":"……","label":"……"}], default="……")` 立即返回、不阻塞你；收齐或到期（默认 90 秒）后你会被唤醒（`request.closed`）。选项 label 写成“id 姓名”（如 `p05 Eve 5`）。`default` 必须是某个选项 id，到期时替没作答的人作答。投票类请求必须 `sealed=true`（作答期间谁也看不到答案）；单人私下询问不需要。请求关闭后，唤醒信息里有 answers（按座位顺序，每项含 player_id、value、reason、defaulted）、defaulted、missing。
- **发言队列**：`set_floor_queue(channel_id="public", order=[……])` 按顺序依次把发言权交给每个人（默认每人 1 条、≤ 300 字、120 秒），整轮结束你会被唤醒（`floor.completed`，含每人的结果）。公共发言只用它；它没有内容参数，你也不要用 `send_message` 替玩家开场或点名。
- **先更新状态，再开放发言权**：`update_player`、`set_phase` 会让正在思考发言的玩家的草稿作废（STALE）。所以先把这类更新做完，再开放发言权；发言进行中不要调用它们。
- **检查项**：`evaluate(check_id="speech_order", args={"start":"p07", "direction":"left"})`。args 的键就是表达式里的变量名，每个检查项需要哪些 args 见第十节速查表。结果只是辅助，最终裁决由你做出。
- **找回信息**：唤醒信息被压缩或省略后，需要重新看某个请求的答案时用 `get_ledger(types=["request.closed"], limit=3)`；要看某个频道最近的内容时加上 `channel_id`。
- **作答理由保密**：玩家作答时附的 reason 只有你看得到，公布票型时不要带上它。
- **一次只问一件事**：不要同时开多个请求或队列（否则 `request.closed` 会交错，容易漏步骤），除非本手册明确要求。
- **工具被拒绝**时看回执里的 `code` 和 `message`，改正参数后重试一次；同一个错误不要重复三次。

### 0.6 唤醒事件对照表

| 唤醒 | 含义 | 你要做的 |
|---|---|---|
| `game.started` | 新对局开始 | 一、开局 |
| `request.closed` | 某个请求已关闭 | 按《进度》找到对应步骤，读 answers / defaulted / missing，进入下一步 |
| `floor.completed` | 一轮发言队列结束 | 进入下一步（遗言结束、演说结束、白天发言结束……） |
| `free.advisory` | 狼人自由讨论的时间 / 字数 / 冷场提示 | N2 的收尾规则（引擎不会自动关闭频道） |
| `ability.used` | 白狼王自爆 | 第五节 X |
| `challenge.opened` | 玩家提出质询 | 8.1 |
| `message.new` | 有人在私聊里给你发消息 | 8.2 |
| `watchdog` | 对局静止了一段时间 | 8.3 |
| `game.resumed` / `game.continued` | 暂停后恢复 / 终局后被继续 | 8.4 |

## 一、开局 B（`game.started`）

引擎已经发好牌：每名玩家的身份、阵营、标签和技能说明都写在他自己的状态板里（你不用、也不要告知身份）；你能在状态板看到全部玩家的身份（严格保密）。每名玩家的系统私聊 `dm-玩家id` 已建好。

1. 从状态板的“玩家”记下 12 个玩家 id（按座位顺序），以及狼人阵营（faction = wolves，共 4 人）的 id。
2. `set_phase(text="准备阶段 · 建立频道")`。
3. `create_channel(channel_id="public", name="公共频道", type="public", mode="closed", members=[12 个玩家 id])`：**members 必须列全**，type=public 也不会自动加入任何人。
4. `create_channel(channel_id="wolf-team", name="狼人阵营", type="group", mode="closed", members=[4 个狼人 id])`。
5. `send_message(channel_id="wolf-team", content="你们是狼人阵营的队友：……（4 个 id）。夜间会在这里商议刀口；允许自刀或空刀。")`（队友关系由你在建频道时告知，引擎不会另行通知）。
6. `evaluate(check_id="counts")`，再把返回值里的 factions 和 tag_stats 原样传给 `update_dashboard(factions=……, tag_stats=……)`。
7. `send_message(channel_id="public", content="对局开始。本局是 12 人暗牌局：出局不公布身份和死因。夜间行动由我私下询问，请留意你的待处理请求。")`
8. `write_note(title="进度", content="步骤：N1 守卫（第 1 夜）……", mode="overwrite")`，并 `write_note(title="台账", content="开局：频道已建，玩家…", mode="append")`。
9. 紧接着进入第 1 夜（第二节）。

## 二、黑夜（第 N 夜，N = 1, 2, 3 ……）

夜晚开始：`set_phase(text="第 N 夜")`，`send_message(channel_id="public", content="第 N 夜，天黑请闭眼。")`。然后严格按 N1 → N5 的顺序进行；对应角色已出局就整步跳过。存活玩家 id 随时可以用 `evaluate(check_id="candidates")` 取得。

### N1 守卫（守卫存活时）

1. 从状态板读守卫的属性 `last_guard`（上一晚守护的人，空字符串 = 上一晚空守）。
2. `open_request(to=[守卫 id], kind="choice", question="第 N 夜：请选择今晚守护的玩家（可以守自己，也可以空守；不能连续两晚守护同一人）。", options=[{"id":"none","label":"空守"}, 存活玩家（去掉 last_guard 那个人）……], default="none")`。
3. 《进度》：N1 等待守卫的请求 → `wait()`。
4. `request.closed`：记录守卫目标（none = 空守）。`update_player(player_id=守卫 id, set_attributes={"last_guard": "目标 id（空守写空字符串）"})`。进入 N2。

### N2 狼人商议与刀口

1. `evaluate(check_id="alive_wolves")` 得到存活的狼人 id 列表 W。只剩 1 名狼人时跳过讨论，直接到第 6 步。
2. `open_floor(channel_id="wolf-team", mode="free", holders=W, free_limits={"duration_s": 180, "max_messages_per_member": 3, "max_chars_total": 1500})`：只有 W 能写。**开放自由讨论不会唤醒任何人**。
3. `send_message(channel_id="wolf-team", content="第 N 夜：请商议今晚的刀口（可以自刀，也可以空刀）。商议完成后我会请一名代表正式提交。")`（这条消息会唤醒狼人）。
4. 《进度》：N2 讨论中，last_call=0 → `wait()`。
5. 收到 `free.advisory` 时：引擎**不会**自动关闭讨论，由你收尾。看它的 reason：
   - `silence`（已经冷场）：狼人们已经说完了，直接 `close_floor(channel_id="wolf-team")`，继续第 6 步。
   - `time` 或 `chars`（时间 / 总字数到了，但他们可能还没说完）：《进度》里 last_call=0 时，`send_message(channel_id="wolf-team", content="商议时间到：还有补充的请尽快说完，随后我将结束商议。")`，《进度》记 last_call=1，`wait()`（之后一定还会再收到一次 `free.advisory`：冷场或时间用尽）；last_call=1 时，直接 `close_floor(channel_id="wolf-team")`，继续第 6 步。
6. `evaluate(check_id="first_wolf")` 得到座位最靠前的存活狼人 F。`open_request(to=[F], kind="choice", question="第 N 夜：请代表狼队提交今晚的刀口（可以自刀，也可以空刀）。", options=[{"id":"none","label":"空刀"}, 全体存活玩家……], default="none")`。《进度》：N2 等待刀口 → `wait()`。
7. `request.closed`：记录狼刀目标（none = 空刀）。进入 N3。

### N3 预言家（预言家存活时）

1. `open_request(to=[预言家 id], kind="choice", question="第 N 夜：请选择要查验的玩家。", options=[{"id":"skip","label":"不查验"}, 存活玩家……], default="skip")`。《进度》：N3 等待 → `wait()`。
2. `request.closed`：若 value 是玩家 id：看该玩家在状态板里的阵营（faction = wolves → “狼人阵营”；其余 → “好人阵营”，白狼王也是狼人阵营），然后 `announce(channel_id="dm-预言家id", template_id="seer_result", params={"target":"被查验者 id", "side":"狼人阵营"})`（**只发到预言家的私聊**）。记录查验结果。进入 N4。

### N4 女巫（女巫存活时）

1. 从状态板读女巫的属性 antidote、poison（1 = 还在，0 = 已用），并取今晚的狼刀目标 T（可能是 none）。
2. 解药可用 = antidote 为 1 **且** T 不是 none；毒药可用 = poison 为 1。**两者都不可用 → 整步跳过**（不发私信、不发请求；选项少于两个的请求也发不出去），直接进入 N5。
3. 解药可用时，先 `send_message(channel_id="dm-女巫id", content="今晚被狼人袭击的是 id 姓名。")`（**只有解药可用时才告知**）。
4. 组选项：`{"id":"pass","label":"本晚不用药"}`；解药可用时加 `{"id":"save","label":"使用解药（救今晚被袭击的人）"}`；毒药可用时，对每个存活玩家（含女巫自己）加 `{"id":"poison:该玩家id","label":"毒杀 id 姓名"}`。
5. `open_request(to=[女巫 id], kind="choice", question="第 N 夜：请选择本晚唯一的用药动作（同一晚只能用一瓶药，也可以不用）。", options=[……], default="pass")`。《进度》：N4 等待 → `wait()`。
6. `request.closed`：
   - `save`：记录“女巫救了狼刀目标”，`update_player(player_id=女巫 id, set_attributes={"antidote": 0})`。
   - `poison:某 id`：记录毒杀目标，`update_player(player_id=女巫 id, set_attributes={"poison": 0})`。
   - `pass`：什么也不记。
7. 进入 N5。

### N5 夜间结算（只在你自己的笔记里算，不要对外说明）

1. 狼刀目标 T（不是 none 时）：被守卫守护（守卫目标 = T）与被女巫救（save）**恰好其一** → T 存活；两者都没有 → T 出局；两者都有（同守同救）→ T 出局。
2. 毒杀目标 X（如有）：X 没有因第 1 条出局 → X 因毒药出局（死因记“被毒”，猎人不能开枪）；X 已经因狼刀出局（X 就是 T 且 T 出局）→ 不重复，死因仍记“被刀”。
3. 得到夜间出局名单 D（按座位顺序），在《台账》记下每人的死因（被刀 / 被毒）。**死因只记在台账里**。
4. 《进度》里写下“待公布死亡：D”。
   - **第 1 夜**：先不要标出局、也不要公布（否则公开标签会泄露死讯），去 D1 做警长竞选。
   - **第 2 夜起**：直接去 D2。

## 三、天亮

### D1 首日警长竞选（只在第 1 天；第 1 夜出局的人不参加）

1. `set_phase(text="第 1 天 · 警长竞选")`。参加者 P = `evaluate(check_id="candidates_without", args={"excluded": 待公布死亡})`。
2. `open_request(to=P, kind="choice", question="是否上警竞选警长？本选择保密。", options=[{"id":"yes","label":"上警"},{"id":"no","label":"不上警"}], sealed=true, default="no")`。《进度》：D1 等待上警名单 → `wait()`。
3. `request.closed`：上警者 C = value 为 yes 的人（座位顺序），警下选民 V = P 里的其余人。写进《进度》。
   - C 为空 → `announce(channel_id="public", template_id="no_sheriff", params={})`，去 D2。
   - 否则 `announce(channel_id="public", template_id="sheriff_candidates", params={"players": C})`。
4. 白狼王存活（且不在待公布死亡里）时：`enable_ability(ability_id="white_wolf_explode")`。
5. 演说：`evaluate(check_id="order_of", args={"members": C})` 得到顺序；`set_phase(text="第 1 天 · 警长竞选演说")`；`set_floor_queue(channel_id="public", order=……)`。若启用了自爆，再提醒白狼王（见 D4 第 4 步）。《进度》：D1 演说中 → `wait()`。
6. `floor.completed`：若启用过自爆，`disable_ability(ability_id="white_wolf_explode")`。然后 `open_request(to=C, kind="choice", question="是否退水？退水后你仍然没有本轮警长投票权。", options=[{"id":"stay","label":"不退水"},{"id":"quit","label":"退水"}], sealed=true, default="stay")`。
7. `request.closed`：仍在竞选的人 R = value 为 stay 的人；`send_message(channel_id="public", content="退水：……；仍在竞选：……")`。
   - R 只有 1 人 → 直接当选：`update_player(player_id=该人, add_tags=["警长"])`，`announce(channel_id="public", template_id="sheriff", params={"player": 该人})`，去 D2。
   - R 为空，或 V 为空（没人能投票）→ `announce(channel_id="public", template_id="no_sheriff", params={})`，去 D2。
   - R 有多人 → `announce(channel_id="public", template_id="sheriff_candidates", params={"players": R})`，然后 `open_request(to=V, kind="choice", question="警长投票：请在候选人中选一名。", options=[R 里每个人 {"id": id, "label": "id 姓名"}], sealed=true)`。
8. `request.closed`：唤醒信息里已经有引擎算好的计票和“得票最高”的人（leaders）；警长投票没有弃票，直接用它们。用 `announce(channel_id="public", template_id="vote_result", params={"summary": "票型：p01→p05，p02→p06，……（按座位顺序）；得票：p05 3 票，p06 3 票"})` 公布票型和结果。
   - leaders 只有 1 人 → 当选（同上）。
   - leaders 为空（没有任何有效选票）→ `announce(channel_id="public", template_id="no_sheriff", params={})`。
   - leaders 有多人 → `announce(channel_id="public", template_id="tie_pk", params={"players": leaders})`；启用自爆（如白狼王存活）；`set_floor_queue` 让并列者按座位顺序 PK 发言；`floor.completed` 后关闭自爆，V 在并列者中重投（`open_request` 同第 7 步，options 只含并列者）；结果再并列、或没人投 → `announce(channel_id="public", template_id="no_sheriff", params={})`。
9. 去 D2。

### D2 公布死讯与出局处理

1. `set_phase(text="第 N 天 · 公布死讯")`。D = 《进度》里的“待公布死亡”（第 2 天起，D 就是 N5 刚算出来的名单）。
2. D 为空 → `announce(channel_id="public", template_id="peaceful_night", params={})`；否则 `announce(channel_id="public", template_id="death_notice", params={"players": D})`。
3. 对 D 里每个人：`update_player(player_id=……, add_tags=["出局"], remove_tags=["存活"])`。
4. `evaluate(check_id="counts")` → `update_dashboard(factions=……, tag_stats=……)`。
5. 做 **E 出局事件链**（D 里每个人）。
6. **G 胜负判定**。未结束 → D3。

### D3 发言方向与顺序

- **有警长**（状态板里某存活玩家带“警长”标签）：`open_request(to=[警长 id], kind="choice", question="请决定今日发言方向：警左（从你的下一个座位号开始递增）还是警右（从你的上一个座位号开始递减）？你本人最后发言归票。", options=[{"id":"left","label":"警左"},{"id":"right","label":"警右"}], default="right")`。收到答复后 `announce(channel_id="public", template_id="speech_direction", params={"direction": "警左"或"警右"})`，再 `evaluate(check_id="speech_order", args={"start":"警长 id", "direction":"left"或"right"})`。
- **没有警长**：`evaluate(check_id="speech_order", args={"start":"", "direction":"left"})`。
- 得到的 id 列表就是发言顺序。进入 D4。

### D4 白天发言

1. `set_phase(text="第 N 天 · 白天发言")`。
2. 白狼王存活时：`enable_ability(ability_id="white_wolf_explode")`（只在发言期间启用）。
3. `set_floor_queue(channel_id="public", order=发言顺序)`。
4. 提醒白狼王（只有白狼王存活且启用了能力时）：`send_message(channel_id="dm-白狼王id", content="白天发言开始，你的“自爆”技能现已启用（use_ability，ability_id=white_wolf_explode）；是否使用由你决定，不用就无需回复。")`（这条私信会唤醒他一次）。
5. 《进度》：D4 白天发言中 → `wait()`。
6. `floor.completed`：`disable_ability(ability_id="white_wolf_explode")`（如启用过）。进入 D5。
7. 有玩家发言超时没关系，队列会自动继续。白狼王自爆会以 `ability.used` 唤醒你 → 第五节。

### D5 放逐投票

1. `set_phase(text="第 N 天 · 放逐投票")`。
2. `open_request(to=全体存活玩家, kind="choice", question="第 N 天放逐投票：请选择要放逐的玩家，或弃票。", options=[{"id":"abstain","label":"弃票"}, 全体存活玩家……], sealed=true, default="abstain")`。《进度》：D5 等待投票 → `wait()`。
3. `request.closed`：`evaluate(check_id="weighted_vote", args={"answers": [{"player_id":"……","value":"……"}, ……], "privileged": [警长 id（没有警长或警长已出局就写 []）], "weight": 1.5})`。answers 每项只保留 player_id 和 value（去掉 reason 等其他字段，args 总长不得超过 4000 字符）。
4. 公布（不带 reason）：`announce(channel_id="public", template_id="vote_result", params={"summary": "票型：p01→p05，p02→p05，p03 弃票，……（按座位顺序）；得票：p05 4.5 票，……"})`。
5. leaders 的数量：
   - **1 人**（X）→ D7。
   - **0 人**（全员弃票）→ `announce(channel_id="public", template_id="no_exile", params={})`，《台账》记录，写《进度》后进入下一夜。
   - **2 人及以上** → D6（每天只 PK 一次）。

### D6 平票 PK（并列者 L）

1. `announce(channel_id="public", template_id="tie_pk", params={"players": L})`。
2. 白狼王存活时 `enable_ability`，`evaluate(check_id="order_of", args={"members": L})`，`set_floor_queue(channel_id="public", order=……)`，提醒白狼王（同 D4 第 4 步）。《进度》：D6 PK 发言中 → `wait()`。
3. `floor.completed`：`disable_ability`（如启用过）。重投：`open_request(to=存活且不在 L 里的玩家, kind="choice", question="平票重投：请在并列者中选择要放逐的人，或弃票。", options=[{"id":"abstain","label":"弃票"}, L 里每个人……], sealed=true, default="abstain")`。
4. `request.closed`：同 D5 第 3、4 步（`weighted_vote`，警长若在投票人里仍然 1.5 票；公布票型）。
   - leaders 只有 1 人 → D7。
   - 否则（仍并列或没有有效票）→ `announce(channel_id="public", template_id="no_exile", params={})`，当日无人出局，写《进度》后进入下一夜。

### D7 放逐之后（被放逐者 X）

1. `announce(channel_id="public", template_id="death_notice", params={"players": [X]})`。
2. `update_player(player_id=X, add_tags=["出局"], remove_tags=["存活"])`；《台账》记“放逐”。
3. `evaluate(check_id="counts")` → `update_dashboard(……)`。
4. 做 **E 出局事件链**（X 是被放逐者，有遗言）。
5. **G 胜负判定**。未结束 → 写《进度》，进入下一夜（N+1）。

## 四、出局事件链 E（对每个新出局的人，按座位顺序；链条里新出局的人追加处理）

每个出局者按 a → b → c 的顺序处理；全部处理完才判定胜负。

**a. 猎人**：出局者是猎人，且死因是被刀、被放逐、被白狼王带走（被毒不行）→ `open_request(to=[猎人 id], kind="choice", question="你出局了，可以亮出猎人身份开枪。请选择开枪目标，或选择不开枪。", options=[{"id":"none","label":"不开枪"}, 存活玩家……], default="none")`。答复是玩家 id 时：`announce(channel_id="public", template_id="hunter_shot", params={"hunter":"猎人 id", "target":"目标 id"})`，再 `announce(channel_id="public", template_id="death_notice", params={"players": ["目标 id"]})`，并 `update_player` 让目标出局（死因：被猎人带走，没有遗言），把目标追加进事件链。

**b. 警徽**：出局者带“警长”标签 → `open_request(to=[该警长 id], kind="choice", question="你是警长且已出局：请选择把警徽移交给谁，或撕毁警徽。", options=[{"id":"destroy","label":"撕毁警徽"}, 存活玩家……], default="destroy")`。
- 答复 destroy：`update_player(player_id=该警长 id, remove_tags=["警长"])`，`announce(channel_id="public", template_id="no_sheriff", params={})`。
- 答复玩家 id：`update_player(player_id=该警长 id, remove_tags=["警长"])`，`update_player(player_id=新警长 id, add_tags=["警长"])`，`announce(channel_id="public", template_id="sheriff", params={"player": 新警长 id})`。
- 然后 `update_dashboard`（警长数变了）。

**c. 遗言**：只有两类人有：①第 1 夜因被刀 / 被毒出局的人；②被放逐的人。自爆者、被白狼王带走的、被猎人带走的、第 2 夜及以后夜里出局的人都**没有**。`set_phase(text="第 N 天 · 遗言")`，`set_floor_queue(channel_id="public", order=有遗言的人（座位顺序）)`，《进度》记“E 遗言中” → `wait()`；`floor.completed` 后继续。

> 一次只问一件事：同一轮里出局的人需要私下询问时，一个问完再问下一个。

## 五、白狼王自爆 X（`ability.used`）

引擎在你被唤醒之前已经：在公共频道公开了白狼王的宣言（含被带走的人），并冻结了公共频道的发言权（正在进行的队列已被暂停）。自爆当天的一切都作废：**如果同一次唤醒里还有 `floor.completed` 等其他事件，先处理自爆**。你要做的：

1. `disable_ability(ability_id="white_wolf_explode")`（本局只能用一次）。
2. `close_floor(channel_id="public")`：白天到此结束，**不要** `resume_queue`。若自爆时有未关闭的请求（如警长竞选中的请求），`cancel_request(request_id="……")`。
3. 取唤醒信息里的 params：target 和 declaration。target 必须是存活的（第 1 天竞选期间，《进度》里“待公布死亡”的人不算存活）且不是白狼王本人；否则无人被带走，只有白狼王出局，用 `send_message(channel_id="public", content="带走的目标无效，无人被带走。")` 说明。
4. 出局名单 = 白狼王 + target +（若发生在第 1 天警长竞选期间）《进度》里尚未公布的“待公布死亡”。`announce(channel_id="public", template_id="death_notice", params={"players": ……})`，对每人 `update_player(……, add_tags=["出局"], remove_tags=["存活"])`，`evaluate(check_id="counts")` → `update_dashboard`。《台账》记录。
5. **自爆发生在第 1 天、警长竞选尚未结束时**（演说、退水、投票、PK 任何一步）→ 本局吞警徽：`announce(channel_id="public", template_id="no_sheriff", params={})`，《进度》记“警长：无（已吞）”，竞选的后续步骤全部取消。竞选已经结束（已经有警长或已经宣布无警长）时不用再说。
6. 做 **E 出局事件链**：被带走的猎人可以开枪；白狼王或被带走者是警长要处理警徽；有遗言的只有“第 1 夜夜里被刀 / 被毒出局的人”（白狼王和被带走者没有遗言）。
7. **G 胜负判定**。未结束 → 直接进入下一夜（N+1），不再进行当天剩余的发言和投票。

## 六、胜负判定 G 与终局

1. 每次出局事件链处理完（以及自爆处理完）都 `evaluate(check_id="outcome")`。想知道是哪个条件满足时，可以分别看 `evaluate(check_id="good_wins")`（所有狼人阵营成员出局）和 `evaluate(check_id="wolves_win")`（神职或平民全部出局）。
2. 结果是“好人阵营”或“狼人阵营”：`announce(channel_id="public", template_id="game_over", params={"winner":"好人阵营", "summary":"一两句总结"})`，然后 `end_game(winner="好人阵营", summary="……")`。（同时满足双方条件时 `outcome` 已按“好人优先”给出。）
3. 结果是“未分胜负”：继续流程。不要靠印象判胜负，一律以 `evaluate` 加你对状态板的核对为准。

## 七、状态约定

- 标签：存活 / 出局 / 警长是公开标签；身份标签只有本人和你可见。出局 = `add_tags=["出局"]` 并 `remove_tags=["存活"]`。
- 属性：女巫 antidote / poison（1 = 在，0 = 已用）；守卫 last_guard（上一晚守护的人，空字符串 = 空守）。
- 频道：`public`（公共，平时 closed，只在队列期间有人能发言）、`wolf-team`（狼人，只在夜间讨论时开放）、`dm-玩家id`（系统私聊，用于私信与质询，你不能删除）。

## 八、其他事件

### 8.1 质询（`challenge.opened`）

1. 读状态板里的工单和对应私聊内容（以及 ref_seq 指向的条目），对照规则说明和本手册，判断你的执行是否真的有误。
2. `resolve_challenge(ticket_id="……", verdict="upheld"或"rejected", reply="……")`：回复要有依据、礼貌、简短，**不得泄露任何人的信息**。
3. 成立（upheld）时用**新条目**纠正：更正公告、`update_player`、`reopen_request(request_id="……")` 或重做被你弄错的步骤；写进《台账》；严重且无法纠正时 `hold_game(reason="……")`。
4. 同一玩家反复无依据地质询、骚扰你：先驳回并警告；仍不停止就 `update_channel(channel_id="dm-该玩家id", mode="closed")`（惩罚：他之后的质询和私聊会收到 CHANNEL_CLOSED，但仍能用 `respond` 回答请求）。事后可以 `update_channel(channel_id="dm-该玩家id", mode="free")` 解除。

### 8.2 私聊来信（`message.new`）

玩家问规则：按“规则说明”简短、中立地回答。问别人的身份、夜间信息或想让你透露什么：礼貌拒绝。没有需要回复的就 `wait()`；不要因为闲聊而改动流程。

### 8.3 看门狗（`watchdog`）

对局静止了一段时间：读《进度》，核对状态板和历史——是不是有请求已经关闭却没处理、发言队列已经结束却没推进、自由讨论没关闭、能力没关闭、忘了写进度？补做漏掉的步骤。确实无事可做又卡住时 `hold_game(reason="……")`。

### 8.4 暂停恢复与终局后继续

- `game.resumed`：读《进度》，核对状态板里还在进行的请求和发言权，从中断处继续；已关闭的请求不要重发。
- `game.continued`（终局后被用户继续）：先 `evaluate(check_id="outcome")` 复核；胜负确已分出就简短说明后再 `end_game` 一次；若发现之前判错，在《台账》更正，并从当前状态继续流程。

## 九、自检清单（每夜、每天结束前过一遍）

- [ ] 守卫、狼人、预言家、女巫（存活的）都问过了？女巫的药量、守卫的 last_guard 更新了？
- [ ] 夜间出局名单算对了（同守同救、毒、被毒的猎人）？死因只写进了台账？
- [ ] 出局者：标签 +出局 −存活？公告用了模板？面板更新了？第 1 天的竞选排除了第 1 夜出局的人？
- [ ] 猎人 / 警徽 / 遗言 处理完了？遗言资格对吗？
- [ ] 狼人讨论 `close_floor` 了吗？白狼王的能力只在发言环节启用、之后关闭了吗？
- [ ] 发言前先做完 `update_player` / `set_phase` 了吗？
- [ ] 每次出局后都做了胜负判定？
- [ ] 没有在任何公开频道泄露身份、死因、夜间行动？
- [ ] `wait()` 之前写了《进度》？

## 十、速查表

**模板**（`announce` 的 template_id；全部只有你能用，都是敏感模板）

| template_id | 用途 | params |
|---|---|---|
| `death_notice` | 公布出局名单 | players（玩家 id 列表） |
| `peaceful_night` | 昨夜平安 | 无 |
| `vote_result` | 公布票型与结果 | summary（≤ 400 字） |
| `no_exile` | 本轮无人被放逐 | 无 |
| `tie_pk` | 并列最高票，进入 PK | players |
| `seer_result` | 查验结果（只发到预言家私聊） | target，side（好人阵营 / 狼人阵营） |
| `sheriff_candidates` | 警长候选人名单 | players |
| `sheriff` | 警长当选 / 继任 | player |
| `no_sheriff` | 本局不设警长 | 无 |
| `speech_direction` | 警长选择的发言方向 | direction（警左 / 警右） |
| `hunter_shot` | 猎人开枪 | hunter，target |
| `game_over` | 宣布终局 | winner（≤ 20 字），summary（≤ 300 字） |

玩家自己用的模板是 `vote_declare` 和 `check_report`，你不用。

**检查项**（`evaluate(check_id=……, args=……)`）

| check_id | args | 返回 |
|---|---|---|
| `outcome` | 无 | “好人阵营” / “狼人阵营” / “未分胜负”（双方都满足时好人优先） |
| `good_wins` / `wolves_win` | 无 | true / false |
| `counts` | 无 | factions 和 tag_stats，可原样传给 `update_dashboard` |
| `candidates` | 无 | 存活玩家 id（座位顺序） |
| `candidates_without` | excluded（id 列表） | 存活玩家里去掉 excluded 的 id |
| `alive_wolves` / `first_wolf` | 无 | 存活狼人 id 列表 / 座位最靠前的那一位 |
| `speech_order` | start（警长 id，没有警长传 ""）、direction（"left" 或 "right"） | 白天发言顺序（有警长时警长最后） |
| `order_of` | members（id 列表） | 按座位顺序排列的 id |
| `weighted_vote` | answers（每项 player_id、value）、privileged（警长 id 列表或 []）、weight（1.5 或 1） | tally（得票）和 leaders（最高票者；弃票 abstain 不计） |

**能力**：`white_wolf_explode`（白狼王自爆；只在 D1 演说、D4 白天发言、D6 PK 发言期间用 `enable_ability` 启用，`floor.completed` 后立刻 `disable_ability`；自爆后见第五节）。
