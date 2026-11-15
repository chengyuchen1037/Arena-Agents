---
schema_version: 1
id: werewolf_12p
version: 1.0.0
entry: setup

identity_setup:
  pool:
    - {role: werewolf, faction: wolves, count: 3}
    - {role: white_wolf, faction: wolves, count: 1}
    - {role: villager, faction: good, count: 4}
    - {role: seer, faction: good, count: 1}
    - {role: witch, faction: good, count: 1}
    - {role: hunter, faction: good, count: 1}
    - {role: guard, faction: good, count: 1}

roles:
  werewolf:
    faction: wolves
    tags: [狼人, 存活]
    attributes: {alive: true, group: wolves}
  white_wolf:
    faction: wolves
    tags: [白狼王, 存活]
    attributes: {alive: true, group: wolves}
  villager:
    faction: good
    tags: [平民, 存活]
    attributes: {alive: true, group: villagers}
  seer:
    faction: good
    tags: [预言家, 存活]
    attributes: {alive: true, group: gods}
  witch:
    faction: good
    tags: [女巫, 存活]
    attributes: {alive: true, group: gods, antidote: 1, poison: 1}
  hunter:
    faction: good
    tags: [猎人, 存活]
    attributes: {alive: true, group: gods}
  guard:
    faction: good
    tags: [守卫, 存活]
    attributes: {alive: true, group: gods, last_guard: ''}

variables:
  initialized: false
  day: 0
  sheriff_id: ''
  sheriff_burned: false
  sheriff_entered: []
  sheriff_withdrawn: []
  sheriff_remaining: []
  speech_direction: right
  speech_order: []
  speech_index: 0
  speech_channel: public
  speech_instruction: ''
  speech_return: ''
  guard_target: none
  wolf_target: none
  wolf_actor_id: ''
  seer_target: none
  seer_target_faction: ''
  seer_result: ''
  witch_actor_id: ''
  witch_action: pass
  witch_save_target: none
  witch_poison_target: none
  interrupt_target_id: ''
  vote_kind: ''
  vote_round: 0
  vote_voters: []
  vote_original_voters: []
  vote_candidates: []
  vote_privileged: []
  vote_weight: 1
  vote_allow_abstain: true
  vote_topic: ''
  vote_tally: {}
  vote_leaders: []

elimination:
  remove_tags: [存活]
  add_tags: [出局]

# 引擎通用提示语的剧本覆盖（引擎本身不含任何具体游戏的词汇）。
texts:
  pass_speech: （过麦）
  truncated_followup: 你的发言最后一句因长度上限被截断了。请紧接上面的末尾残句，用 1~2 句话（不超过 80 字）把最后一句补完并给出明确的归票/出人结论（只输出补完的收尾文字，不要重复前文）：
  plan_nudge: 你可以调用 `read_document` 查阅阵营战术指南、调用 `write_note` 记录 Plan A/B/C 到私人笔记本中。规划完成后，请务必调用 **`submit_strategic_plan(plan_summary, current_focus)`** 提交你本阶段的核心预案与行动焦点！
  plan_fallback_summary: 基于自身身份按本阵营最高胜率既定规划行动
  plan_fallback_focus: 观察场上局势与发言破绽

workflow:
  - id: setup
    do:
      - tool: {name: set_phase, args: {phase_text: 准备阶段 · 发牌与建台}}
      - deal_identities: true
      - set:
          - {path: initialized, value: true}
      - refresh_dashboard: true
    next: setup_channels

  - id: setup_channels
    do:
      - tool:
          name: create_channel
          args:
            channel_id: public
            name: 公共频道
            type: public
            members: "${['moderator'] + ids(players())}"
            status: open
      - tool:
          name: create_channel
          args:
            channel_id: wolf-team
            name: 狼人阵营
            type: group
            members: "${['moderator'] + ids(by_faction('wolves'))}"
            status: open
      - tool:
          name: send_message
          args:
            channel_id: wolf-team
            content: 狼人队友为 {{ids(by_faction('wolves'))}}；夜间依次商议后，由当前座位顺序最靠前的存活狼人提交统一刀口。允许自刀或空刀。
    next: setup_dms

  - id: setup_dms
    do:
      - for_each:
          items: "${players()}"
          as: actor
          max_iterations: 12
          do:
            - tool:
                name: create_channel
                args:
                  channel_id: dm-{{actor.id}}
                  name: "{{actor.name}} 私聊"
                  type: dm
                  members: "${['moderator', actor.id]}"
                  status: open
    next: opening_plan

  - id: opening_plan
    do:
      - tool: {name: set_phase, args: {phase_text: 开局 · 宏观战略规划}}
      - plan:
          player_ids: "${ids(players())}"
          phase_topic: 开局宏观战略规划与推演
          instruction: 请依据自身底牌制定全局预案；不得泄露私密规划。
          save_as: opening_plans
      - tool: {name: send_message, args: {channel_id: public, content: 对局开始。本局为暗牌局，出局公告不公布底牌或具体死因。}}
    next: night_start

  - id: night_start
    do:
      - set:
          - {path: day, value: "${day + 1}"}
          - {path: guard_target, value: none}
          - {path: wolf_target, value: none}
          - {path: wolf_actor_id, value: ''}
          - {path: seer_target, value: none}
          - {path: seer_target_faction, value: ''}
          - {path: seer_result, value: ''}
          - {path: witch_actor_id, value: ''}
          - {path: witch_action, value: pass}
          - {path: witch_save_target, value: none}
          - {path: witch_poison_target, value: none}
          - {path: interrupt_target_id, value: ''}
      - tool: {name: set_phase, args: {phase_text: "第 {{day}} 天 · 黑夜"}}
      - tool: {name: update_channel, args: {channel_id: wolf-team, status: open}}
    next: night_guard

  - id: night_guard
    do:
      - for_each:
          items: "${alive(by_role('guard'))}"
          as: actor
          max_iterations: 1
          do:
            - choice:
                actors: "${[actor.id]}"
                question: 请选择今晚守护目标；可守自己或空守，但不可连续两晚守护同一名玩家。
                options: "${[{'id':'none','label':'空守'}] + [{'id':p.id,'label':p.seat+' '+p.name} for p in alive() if p.id != get(actor.data,'last_guard','')]}"
                save_as: guard_choice
            - set:
                - {path: guard_target, value: "${get(guard_choice.decisions,actor.id,'none') or 'none'}"}
            - patch_player:
                player_id: "${actor.id}"
                data: {last_guard: "${guard_target}"}
    next: night_wolves_discuss

  - id: night_wolves_discuss
    do:
      - for_each:
          items: "${seat_order(alive(by_faction('wolves')))}"
          as: actor
          max_iterations: 4
          do:
            - speak:
                player_id: "${actor.id}"
                channel_id: wolf-team
                instruction: 请与狼队商议今晚统一刀口；允许自刀或空刀，不要泄露其他频道信息。
                save_as: wolf_discussion
    next: night_wolves_decide

  - id: night_wolves_decide
    do:
      - if:
          when: "${count(alive(by_faction('wolves'))) > 0}"
          do:
            - for_each:
                items: "${[first(seat_order(alive(by_faction('wolves'))))]}"
                as: actor
                max_iterations: 1
                do:
                  - choice:
                      actors: "${[actor.id]}"
                      question: 请代表已完成商议的狼队提交今晚统一刀口；可选择任意存活玩家或空刀。
                      options: "${[{'id':'none','label':'空刀'}] + [{'id':p.id,'label':p.seat+' '+p.name} for p in alive()]}"
                      save_as: wolf_choice
                  - set:
                      - {path: wolf_target, value: "${get(wolf_choice.decisions,actor.id,'none') or 'none'}"}
                      - {path: wolf_actor_id, value: "${actor.id}"}
          else: []
    next: night_seer

  - id: night_seer
    do:
      - for_each:
          items: "${alive(by_role('seer'))}"
          as: actor
          max_iterations: 1
          do:
            - choice:
                actors: "${[actor.id]}"
                question: 请选择今晚要查验的一名存活玩家。
                options: "${[{'id':p.id,'label':p.seat+' '+p.name} for p in alive()]}"
                save_as: seer_choice
            - set:
                - {path: seer_target, value: "${get(seer_choice.decisions,actor.id,'none') or 'none'}"}
                - {path: seer_target_faction, value: ''}
            - if:
                when: "${seer_target != 'none' and count([p for p in alive() if p.id == seer_target]) == 1}"
                do:
                  - set:
                      - {path: seer_target_faction, value: "${first([p for p in players() if p.id == seer_target]).data.faction}"}
                  - if:
                      when: "${seer_target_faction == 'wolves'}"
                      do:
                        - set:
                            - {path: seer_result, value: 狼人阵营}
                      else:
                        - set:
                            - {path: seer_result, value: 好人阵营}
                  - tool:
                      name: send_message
                      args:
                        channel_id: dm-{{actor.id}}
                        content: "查验结果：{{first([p for p in players() if p.id == seer_target]).seat}} {{first([p for p in players() if p.id == seer_target]).name}} 属于 {{seer_result}}。只告知阵营，不告知具体身份。"
                else: []
    next: night_witch

  - id: night_witch
    do:
      - for_each:
          items: "${alive(by_role('witch'))}"
          as: actor
          max_iterations: 1
          do:
            - set:
                - {path: witch_actor_id, value: "${actor.id}"}
            - if:
                when: "${actor.data.antidote > 0 and wolf_target != 'none'}"
                do:
                  - tool:
                      name: send_message
                      args:
                        channel_id: dm-{{actor.id}}
                        content: "今晚被狼人袭击的是 {{first([p for p in players() if p.id == wolf_target]).seat}} {{first([p for p in players() if p.id == wolf_target]).name}}。"
                else: []
            - choice:
                actors: "${[actor.id]}"
                question: 请选择本晚唯一的用药动作；解药仅救今晚狼刀，毒药需在下一步选择目标，也可不用药。
                options: "${[o for o in [{'id':'save','label':'使用解药'},{'id':'poison','label':'使用毒药'},{'id':'pass','label':'本晚不用药'}] if o['id'] == 'pass' or (o['id'] == 'save' and actor.data.antidote > 0 and wolf_target != 'none') or (o['id'] == 'poison' and actor.data.poison > 0)]}"
                save_as: witch_choice
            - set:
                - {path: witch_action, value: "${get(witch_choice.decisions,actor.id,'pass') or 'pass'}"}
            - if:
                when: "${witch_action == 'save'}"
                do:
                  - set:
                      - {path: witch_save_target, value: "${wolf_target}"}
                  - patch_player:
                      player_id: "${actor.id}"
                      data: {antidote: "${actor.data.antidote - 1}"}
                else: []
            - if:
                when: "${witch_action == 'poison'}"
                do:
                  - choice:
                      actors: "${[actor.id]}"
                      question: 请选择毒药目标。
                      options: "${[{'id':p.id,'label':p.seat+' '+p.name} for p in alive()]}"
                      save_as: poison_choice
                  - set:
                      - {path: witch_poison_target, value: "${get(poison_choice.decisions,actor.id,'none') or 'none'}"}
                  - if:
                      when: "${witch_poison_target != 'none' and count([p for p in alive() if p.id == witch_poison_target]) == 1}"
                      do:
                        - patch_player:
                            player_id: "${actor.id}"
                            data: {poison: "${actor.data.poison - 1}"}
                      else:
                        - set:
                            - {path: witch_action, value: pass}
                else: []
    next: night_resolve

  - id: night_resolve
    do:
      - tool: {name: set_phase, args: {phase_text: "第 {{day}} 天 · 夜间统一结算"}}
      - if:
          when: "${wolf_target != 'none' and ((guard_target == wolf_target) == (witch_save_target == wolf_target))}"
          do:
            - eliminate: {player_id: "${wolf_target}", cause: wolf_attack, actor_id: "${wolf_actor_id}"}
          else: []
      - if:
          when: "${witch_poison_target != 'none' and count([p for p in alive() if p.id == witch_poison_target]) == 1}"
          do:
            - eliminate: {player_id: "${witch_poison_target}", cause: witch_poison, actor_id: "${witch_actor_id}"}
          else: []
      - refresh_dashboard: true
    next: after_night

  - id: after_night
    do:
      - if:
          when: "${day == 1 and sheriff_id == '' and not sheriff_burned}"
          do:
            - goto: sheriff_signup
          else:
            - goto: dawn_announce
    next: dawn_announce

  - id: sheriff_signup
    do:
      - tool: {name: set_phase, args: {phase_text: 第 1 天 · 警长竞选报名}}
      - choice:
          actors: "${ids(alive())}"
          question: 是否参加警长竞选（上警）？本选择私密提交；首夜已出局者不参加。
          options: [{id: 'yes', label: 上警}, {id: 'no', label: 不上警}]
          save_as: sheriff_signup_answers
      - set:
          - {path: sheriff_entered, value: "${[p.id for p in alive() if get(sheriff_signup_answers.decisions,p.id,'no') == 'yes']}"}
          - {path: sheriff_withdrawn, value: []}
          - {path: vote_kind, value: sheriff}
          - {path: vote_original_voters, value: "${[p.id for p in alive() if get(sheriff_signup_answers.decisions,p.id,'no') != 'yes']}"}
          - {path: vote_privileged, value: []}
          - {path: vote_weight, value: 1}
          - {path: vote_allow_abstain, value: false}
      - tool: {name: send_message, args: {channel_id: public, content: "上警名单：{{sheriff_entered}}。曾上警者即使退水，也无本轮警长投票权。"}}
    next: sheriff_signup_result

  - id: sheriff_signup_result
    do:
      - if:
          when: "${len(sheriff_entered) == 0}"
          do:
            - tool: {name: send_message, args: {channel_id: public, content: 无人参加警长竞选，本局不设警长。}}
            - goto: dawn_announce
          else: []
    next: sheriff_prepare_speeches

  - id: sheriff_prepare_speeches
    do:
      - set:
          - {path: speech_channel, value: public}
          - {path: speech_order, value: "${ids(seat_order([p for p in alive() if p.id in sheriff_entered]))}"}
          - {path: speech_instruction, value: 请发表警长竞选演说。}
          - {path: speech_return, value: sheriff_withdraw}
      - tool: {name: set_phase, args: {phase_text: 第 1 天 · 警长竞选发言}}
    next: speech_wave_prepare

  - id: sheriff_withdraw
    do:
      - tool: {name: set_phase, args: {phase_text: 第 1 天 · 警长竞选退水确认}}
      - choice:
          actors: "${[p.id for p in alive() if p.id in sheriff_entered]}"
          question: 是否退出本次警长竞选（退水）？退水后仍无本轮警长投票权。
          options: [{id: 'yes', label: 退水}, {id: 'no', label: 不退水}]
          save_as: sheriff_withdraw_answers
      - set:
          - {path: sheriff_withdrawn, value: "${[p.id for p in alive() if p.id in sheriff_entered and get(sheriff_withdraw_answers.decisions,p.id,'no') == 'yes']}"}
          - {path: sheriff_remaining, value: "${[p.id for p in alive() if p.id in sheriff_entered and get(sheriff_withdraw_answers.decisions,p.id,'no') != 'yes']}"}
      - tool: {name: send_message, args: {channel_id: public, content: "退水名单：{{sheriff_withdrawn}}；剩余候选人：{{sheriff_remaining}}。"}}
    next: sheriff_withdraw_branch

  - id: sheriff_withdraw_branch
    do:
      - if:
          when: "${len(sheriff_remaining) == 1}"
          do:
            - set:
                - {path: vote_leaders, value: "${sheriff_remaining}"}
            - goto: ballot_unique
          else: []
      - if:
          when: "${len(sheriff_remaining) == 0}"
          do:
            - goto: ballot_no_winner
          else: []
    next: sheriff_vote_prepare

  - id: sheriff_vote_prepare
    do:
      - set:
          - {path: vote_kind, value: sheriff}
          - {path: vote_round, value: 1}
          - {path: vote_voters, value: "${[p.id for p in alive() if p.id in vote_original_voters]}"}
          - {path: vote_candidates, value: "${sheriff_remaining}"}
          - {path: vote_privileged, value: []}
          - {path: vote_weight, value: 1}
          - {path: vote_allow_abstain, value: false}
          - {path: vote_topic, value: 警长竞选投票}
      - tool: {name: send_message, args: {channel_id: public, content: "开始警长投票。纯警下投票人：{{vote_voters}}；候选人：{{vote_candidates}}。"}}
    next: ballot_cast

  - id: dawn_announce
    do:
      - tool: {name: set_phase, args: {phase_text: "第 {{day}} 天 · 公布昨夜死讯"}}
      - dispatch_events:
          channel_id: public
          template: "【出局公告】{{player.seat}} {{player.name}} 出局。"
          empty_message: 昨夜平安夜，无人出局。
      - refresh_dashboard: true
    next: day_direction

  - id: day_direction
    do:
      - set:
          - {path: speech_direction, value: right}
      - if:
          when: "${sheriff_id != '' and count([p for p in alive() if p.id == sheriff_id]) == 1}"
          do:
            - choice:
                actors: "${[sheriff_id]}"
                question: 请警长决定今日发言方向。
                options: [{id: left, label: 警左}, {id: right, label: 警右}]
                save_as: direction_choice
            - set:
                - {path: speech_direction, value: "${get(direction_choice.decisions,sheriff_id,'right') or 'right'}"}
            - tool: {name: send_message, args: {channel_id: public, content: "警长选择发言方向：{{speech_direction}}。"}}
          else: []
    next: day_speech_order

  - id: day_speech_order
    do:
      - if:
          when: "${sheriff_id != '' and count([p for p in alive() if p.id == sheriff_id]) == 1}"
          do:
            - set:
                - {path: speech_order, value: "${ids(seat_order(alive(),sheriff_id,speech_direction,sheriff_id))}"}
          else:
            - set:
                - {path: speech_order, value: "${ids(seat_order(alive()))}"}
      - set:
          - {path: speech_channel, value: public}
          - {path: speech_instruction, value: 请进行今日白天正式发言；若有警长，警长已按规则末置位。}
          - {path: speech_return, value: exile_vote_prepare}
      - tool: {name: set_phase, args: {phase_text: "第 {{day}} 天 · 白天正式发言"}}
    next: speech_wave_prepare

  - id: exile_vote_prepare
    do:
      - set:
          - {path: vote_kind, value: exile}
          - {path: vote_round, value: 1}
          - {path: vote_voters, value: "${ids(alive())}"}
          - {path: vote_original_voters, value: "${ids(alive())}"}
          - {path: vote_candidates, value: "${ids(alive())}"}
          - {path: vote_privileged, value: "${ids(by_tag('警长'))}"}
          - {path: vote_weight, value: 1.5}
          - {path: vote_allow_abstain, value: true}
          - {path: vote_topic, value: "第 {{day}} 天放逐投票"}
      - tool: {name: set_phase, args: {phase_text: "第 {{day}} 天 · 放逐投票"}}
      - tool: {name: send_message, args: {channel_id: public, content: 开始放逐投票。警长票权为 1.5，其余存活玩家为 1；允许弃票。}}
    next: ballot_cast

  - id: ballot_cast
    do:
      - vote:
          channel_id: public
          voter_ids: "${vote_voters}"
          candidate_ids: "${vote_candidates}"
          topic: "${vote_topic}"
          allow_abstain: "${vote_allow_abstain}"
          save_as: vote_result
    next: ballot_resolve

  - id: ballot_resolve
    do:
      - set:
          - {path: vote_tally, value: "${tally([b for b in get(vote_result,'ballots',[]) if b['target_id'] in vote_candidates and b['voter_id'] in vote_voters],vote_privileged,vote_weight)}"}
          - {path: vote_leaders, value: "${leaders(tally([b for b in get(vote_result,'ballots',[]) if b['target_id'] in vote_candidates and b['voter_id'] in vote_voters],vote_privileged,vote_weight))}"}
      - tool: {name: send_message, args: {channel_id: public, content: "投票结果：{{vote_tally}}；最高票：{{vote_leaders}}。"}}
    next: ballot_decide

  - id: ballot_decide
    do:
      - if:
          when: "${len(vote_leaders) == 1}"
          do:
            - goto: ballot_unique
          else: []
      - if:
          when: "${len(vote_leaders) > 1 and vote_round == 1}"
          do:
            - goto: ballot_prepare_pk
          else: []
    next: ballot_no_winner

  - id: ballot_prepare_pk
    do:
      - set:
          - {path: vote_round, value: 2}
          - {path: vote_candidates, value: "${vote_leaders}"}
          - {path: vote_voters, value: "${[p.id for p in alive() if p.id in vote_original_voters and not (p.id in vote_leaders)]}"}
          - {path: vote_topic, value: "{{vote_topic}} · 平票 PK 重投"}
          - {path: speech_channel, value: public}
          - {path: speech_order, value: "${ids(seat_order([p for p in alive() if p.id in vote_leaders]))}"}
          - {path: speech_instruction, value: 请进行平票 PK 发言；随后由非平票的存活合资格投票人重投。}
          - {path: speech_return, value: ballot_cast}
      - tool: {name: send_message, args: {channel_id: public, content: "首次平票，进入 PK：{{vote_candidates}}。非平票存活者重投：{{vote_voters}}；再次平票则本轮无人胜出/出局。"}}
    next: speech_wave_prepare

  - id: ballot_unique
    do:
      - if:
          when: "${vote_kind == 'sheriff'}"
          do:
            - set:
                - {path: sheriff_id, value: "${first(vote_leaders)}"}
            - patch_player:
                player_id: "${first(vote_leaders)}"
                add_tags: [警长]
            - tool: {name: send_message, args: {channel_id: public, content: "{{first(vote_leaders)}} 当选警长。放逐投票中警长票权为 1.5。"}}
            - goto: dawn_announce
          else:
            - eliminate: {player_id: "${first(vote_leaders)}", cause: exile}
            - tool: {name: send_message, args: {channel_id: public, content: "{{first(vote_leaders)}} 被公投放逐。公告不翻牌。"}}
            - goto: post_exile
    next: post_exile

  - id: ballot_no_winner
    do:
      - if:
          when: "${vote_kind == 'sheriff'}"
          do:
            - tool: {name: send_message, args: {channel_id: public, content: 警长竞选无唯一胜者或再次平票，本局不设警长。}}
            - goto: dawn_announce
          else:
            - tool: {name: send_message, args: {channel_id: public, content: 再次平票或无有效最高票，当日无人出局。}}
            - goto: post_exile
    next: post_exile

  - id: post_exile
    do:
      - dispatch_events:
          channel_id: public
          template: "【出局公告】{{player.seat}} {{player.name}} 出局。"
      - refresh_dashboard: true
    next: dusk_plan

  - id: dusk_plan
    do:
      - plan:
          player_ids: "${ids(alive())}"
          phase_topic: "第 {{day}} 天 · 黄昏战术复盘与策略调整"
          instruction: 请私密复盘今日发言和票型，调整后续预案。
          save_as: dusk_plans
    next: night_start

  - id: speech_wave_prepare
    do:
      - set:
          - {path: speech_index, value: 0}
      - if:
          when: "${count(alive(by_role('white_wolf'))) > 0}"
          do:
            - for_each:
                items: "${alive(by_role('white_wolf'))}"
                as: actor
                max_iterations: 1
                do:
                  - tool:
                      name: set_interrupt_quota
                      args: {channel_id: public, player_id: "${actor.id}", max_count: 1, reason: 白狼王白天非投票正式发言环节自爆插话权}
          else: []
    next: speech_wave_loop

  - id: speech_wave_loop
    do:
      - while:
          when: "${speech_index < len(speech_order)}"
          max_iterations: 24
          do:
            - speak:
                player_id: "${speech_order[speech_index]}"
                channel_id: "${speech_channel}"
                instruction: "${speech_instruction}"
                save_as: public_speech
            - for_each:
                items: "${alive(by_role('white_wolf'))}"
                as: actor
                max_iterations: 1
                do:
                  - choice:
                      actors: "${[actor.id]}"
                      question: 刚才一名玩家已公开发言完毕。是否此时发动白狼王自爆插话并带走一名存活玩家？
                      options: [{id: 'yes', label: 发动自爆插话}, {id: 'no', label: 暂不插话}]
                      save_as: interrupt_choice
                  - if:
                      when: "${get(interrupt_choice.decisions,actor.id,'no') == 'yes'}"
                      do:
                        - interrupt:
                            channel_id: public
                            player_id: "${actor.id}"
                            candidate_ids: "${[p.id for p in alive() if p.id != actor.id]}"
                            instruction: 请正式宣布白狼王自爆，并指定一名其他存活玩家作为带走目标；空目标或非法目标不带走任何人。
                            save_as: interrupt_result
                        - set:
                            - {path: interrupt_target_id, value: "${get(interrupt_result,'target_id','')}"}
                        - if:
                            when: "${get(interrupt_result,'triggered',false)}"
                            do:
                              - eliminate: {player_id: "${actor.id}", cause: self_explode}
                              - if:
                                  when: "${interrupt_target_id != '' and interrupt_target_id != actor.id and count([p for p in alive() if p.id == interrupt_target_id]) == 1}"
                                  do:
                                    - eliminate: {player_id: "${interrupt_target_id}", cause: white_wolf, actor_id: "${actor.id}"}
                                  else: []
                              - if:
                                  when: "${day == 1 and sheriff_id == ''}"
                                  do:
                                    - set:
                                        - {path: sheriff_burned, value: true}
                                  else: []
                              - tool: {name: clear_interrupt_quota, args: {channel_id: public}}
                              - dispatch_events:
                                  channel_id: public
                                  template: "【出局公告】{{player.seat}} {{player.name}} 出局。"
                              - goto: night_start
                            else: []
                      else: []
            - set:
                - {path: speech_index, value: "${speech_index + 1}"}
    next: speech_wave_finish

  - id: speech_wave_finish
    do:
      - tool: {name: clear_interrupt_quota, args: {channel_id: public}}
    next: "${speech_return}"

termination:
  - when: "${initialized and count(alive(by_faction('wolves'))) == 0}"
    winner: 好人阵营
    summary: 所有狼人均已出局，好人阵营获胜。若同一事件队列同时满足双方条件，按本顺序优先判定好人胜利。
    channel_id: public
  - when: "${initialized and (count([p for p in alive() if p.data.group == 'gods']) == 0 or count([p for p in alive() if p.data.group == 'villagers']) == 0)}"
    winner: 狼人阵营
    summary: 四名神职全部出局或四名平民全部出局，狼人阵营屠边获胜。
    channel_id: public

hooks:
  eliminated:
    - when: "${player.data.role == 'hunter' and cause in ['wolf_attack','exile','white_wolf'] and count(alive()) > 0}"
      do:
        - choice:
            actors: "${[player.id]}"
            question: 你因可开枪的死因出局，是否亮明猎人身份并开枪？女巫毒杀不会进入本分支。
            options: [{id: 'yes', label: 发动技能开枪}, {id: 'no', label: 不开枪}]
            save_as: hunter_choice
        - if:
            when: "${get(hunter_choice.decisions,player.id,'no') == 'yes'}"
            do:
              - tool: {name: send_message, args: {channel_id: public, content: "{{player.seat}} {{player.name}} 亮明猎人身份并发动开枪技能。"}}
              - choice:
                  actors: "${[player.id]}"
                  question: 请选择一名存活玩家作为开枪目标。
                  options: "${[{'id':p.id,'label':p.seat+' '+p.name} for p in alive()]}"
                  save_as: hunter_target_choice
              - if:
                  when: "${get(hunter_target_choice.decisions,player.id,'') in ids(alive())}"
                  do:
                    - eliminate: {player_id: "${get(hunter_target_choice.decisions,player.id,'')}", cause: hunter_shot, actor_id: "${player.id}"}
                  else: []
            else: []

    - when: "${'警长' in player.tags}"
      do:
        - choice:
            actors: "${[player.id]}"
            question: 你已出局，请选择移交警徽或撕毁警徽；无遗言不影响本决定。
            options: "${[o for o in [{'id':'transfer','label':'移交警徽'},{'id':'destroy','label':'撕毁警徽'}] if o['id'] == 'destroy' or count(alive()) > 0]}"
            save_as: badge_choice
        - if:
            when: "${get(badge_choice.decisions,player.id,'destroy') == 'transfer'}"
            do:
              - choice:
                  actors: "${[player.id]}"
                  question: 请选择一名存活玩家继承警徽。
                  options: "${[{'id':p.id,'label':p.seat+' '+p.name} for p in alive()]}"
                  save_as: badge_target_choice
              - if:
                  when: "${get(badge_target_choice.decisions,player.id,'') in ids(alive())}"
                  do:
                    - set:
                        - {path: sheriff_id, value: "${get(badge_target_choice.decisions,player.id,'')}"}
                    - patch_player:
                        player_id: "${player.id}"
                        remove_tags: [警长]
                    - patch_player:
                        player_id: "${sheriff_id}"
                        add_tags: [警长]
                    - tool: {name: send_message, args: {channel_id: public, content: "警徽已移交给 {{sheriff_id}}。"}}
                  else:
                    - patch_player:
                        player_id: "${player.id}"
                        remove_tags: [警长]
                    - set:
                        - {path: sheriff_id, value: ''}
                    - tool: {name: send_message, args: {channel_id: public, content: 未提交有效警徽继承人，警徽已撕毁。}}
            else:
              - patch_player:
                  player_id: "${player.id}"
                  remove_tags: [警长]
              - set:
                  - {path: sheriff_id, value: ''}
              - tool: {name: send_message, args: {channel_id: public, content: 警徽已撕毁，本局不再设警长。}}

    - when: "${cause == 'exile' or (event_day == 1 and cause in ['wolf_attack','witch_poison'])}"
      do:
        - speak:
            player_id: "${player.id}"
            channel_id: public
            instruction: 请发表遗言。首夜出局或白天被放逐者拥有遗言；不得公布引擎未公开的底牌信息。
            save_as: last_words

dashboard:
  factions:
    - {name: 狼人阵营, alive: "${count(alive(by_faction('wolves')))}", total: "${count(by_faction('wolves'))}", variant: danger}
    - {name: 神职, alive: "${count([p for p in alive() if p.data.group == 'gods'])}", total: "${count([p for p in players() if p.data.group == 'gods'])}", variant: purple}
    - {name: 平民阵营, alive: "${count([p for p in alive() if p.data.group == 'villagers'])}", total: "${count([p for p in players() if p.data.group == 'villagers'])}", variant: neutral}
  tag_stats:
    - {name: 存活, count: "${count(alive())}", variant: success}
    - {name: 出局, count: "${count(players()) - count(alive())}", variant: muted}
    - {name: 警长, count: "${count(by_tag('警长'))}", variant: warning}
---

# 12 人标准局狼人杀（可执行剧本）

本文件的 YAML front matter 是唯一的机器规则源；正文是与它对应的规则说明。引擎不需要理解角色名、中文阶段、频道名或死因，只执行声明式动作、表达式、事件与钩子。

## 1. 配置与阵营

- 狼人阵营 4 人：普通狼人 3 人、白狼王 1 人。
- 好人阵营 8 人：平民 4 人；预言家、女巫、猎人、守卫各 1 人，四者构成神职组。
- 全局为暗牌局。通用出局公告只报座位和姓名，不报具体死因或底牌；猎人开枪、白狼王自爆属于技能主动亮明身份的例外。
- `data.alive` 是存活权威状态；“存活/出局”只是由出局原语同步的展示标签。

## 2. 开局与夜晚

开局先原子发牌、创建公共频道/狼队频道/全员私聊，再做全员私密战略规划。

每夜严格按以下顺序先收集决策，再统一结算：

1. **守卫**：可守自己或空守；动态选项已经排除上一晚守护目标，因此不能连守同一人。
2. **狼队**：存活狼人按座位顺序在狼队频道商议。随后由当前座位顺序最靠前的存活狼人提交统一刀口；允许自刀或空刀。这是对旧版“商议并拍板”未指定拍板人的明确补充。
3. **预言家**：选择一名存活玩家，只收到“好人阵营/狼人阵营”，不收到具体身份。
4. **女巫**：解药和毒药整局各一次。解药尚存时才私聊告知今晚狼刀；动态选项不出现已耗尽的资源。每次选择最多一个动作，毒药另选目标，所以同晚不可能双药。
5. **统一结算**：狼刀与毒杀都先进入效果计算，再产生出局事件。狼刀目标在“恰好一种保护”时存活；无保护时出局；守卫和解药同时作用于同一目标时“同守同救（奶穿）”，仍出局。毒药再追加一个独立出局效果，并防止对同一目标重复入队。

## 3. 首日警长

首夜结算后、公布死讯前进行首日竞选。首夜已出局者不参与报名、演说或投票；这是对本时序下资格的明确化。出局在内部立即生效，但公告前其他 Agent 的公开座位投影仍保留事件前状态，避免提前泄密。

1. 存活玩家私聊选择上警/不上警。
2. 上警者按座位顺序演说，之后私聊选择是否退水。
3. 只有从未上警的纯警下存活玩家有投票权；上警后退水者仍无投票权。
4. 只有一名剩余候选人时直接当选；多人时投票，无人时本局无警长。
5. 警长决定每日警左/警右方向，并在绕场发言后末置位归票。放逐投票中警长权重为 1.5，其余为 1。
6. 在任警长出局后，无论是否有遗言，都可选择移交给存活玩家或撕毁；标签和 `sheriff_id` 同步更新。

## 4. 投票与平票 PK

放逐投票允许弃票；警长竞选不允许弃票。所有 ballot 都由通用投票原语采集，剧本显式计算加权 tally 和最高票集合。

首次出现两人及以上并列最高票时：

1. 平票者进入 PK 发言。
2. 非平票且仍存活的合资格投票人重投。放逐 PK 从全体原存活投票人中排除平票者；警长 PK 仍只使用原纯警下投票人。
3. 第二次仍平票时，放逐为当日无人出局，警长竞选为本局不设警长。没有有效最高票时也按无人胜出处理。

## 5. 白狼王插话

- 在警长竞选、日常正式发言和平票 PK 发言期间，每位玩家发言完毕后，存活白狼王都会私聊决定是否自爆插话；投票阶段不开放该插话。
- 确认后通过结构化 `interrupt` 公布自爆宣言并指定目标。白狼王以 `self_explode` 出局，目标以 `white_wolf` 出局；空目标、自己、死者或不存在目标不会造成额外出局。
- 首日在警徽尚未归属时自爆会设置 `sheriff_burned`，本局吞警徽。
- 自爆效果、后续猎人/警徽/遗言事件全部处理完后，若仍未终局，直接进入下一夜，不继续当天投票或发言。

## 6. 出局、猎人与遗言

出局原语只更新存活状态/标签并入队。`dispatch_events` 先发布暗牌公告，再按钩子顺序处理猎人、警徽和遗言；钩子可继续产生出局事件，终局只在整个队列清空后判断。

- 猎人因狼刀、放逐或白狼王带走而出局时，可选择开枪；因女巫毒药出局不能开枪，猎人开枪造成的后续出局也不会无限触发猎人自身技能。
- 首夜因狼刀或女巫毒药出局者有遗言；任意一天被公投放逐者有遗言。
- 第二夜及以后夜死、白狼王自爆、白狼王带走、猎人开枪等死因不自动获得遗言。
- 事件处理顺序为猎人开枪、警徽去向、遗言；级联事件也按同一规则递归处理。

## 7. 胜负与看板

- 所有狼人出局：好人阵营胜。
- 神职组全部出局或平民组全部出局：狼人阵营屠边胜。
- 若同一事件队列同时满足双方条件，按 `termination` 顺序由好人阵营优先获胜；这是旧规则未明确之处的显式补充。
- 看板中的阵营存活数、总数、存活/出局/警长统计全部由表达式每步刷新，主持人不手填数字。
