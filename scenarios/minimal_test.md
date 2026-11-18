---
schema_version: 2
id: minimal_test
version: 1.0.0
title: 最小测试剧本
manual_heading: 主持人手册

setup:
  identity_pool:
    - {role: red, count: 2}
    - {role: blue, count: 2}
  roles:
    red:
      faction: red
      tags: [红队, 存活]
      attributes: {group: red}
      brief: 你属于红队。目标：让蓝队全部出局。
    blue:
      faction: blue
      tags: [蓝队, 存活]
      attributes: {group: blue}
      brief: 你属于蓝队。目标：让红队全部出局。
  public_tags: [存活, 出局]
  faction_names: {red: 红队, blue: 蓝队}

defaults:
  floor: {duration_s: 30, max_messages: 1, max_chars_per_message: 120, max_chars_total: 200}
  free: {duration_s: 60, max_messages_per_member: 3, max_chars_per_message: 120, max_chars_total: 600, silence_s: 20}
  request: {duration_s: 30}
  llm: {max_tokens: 4096}

templates:
  out_notice:
    text: "【出局公告】{{players}} 出局。"
    params: {players: player_ids}
    users: moderator
    sensitive: true
  say_vote:
    text: "我投 {{target}}。"
    params: {target: player_id}
    users: players

abilities:
  shout:
    holders: "${alive(by_role('red'))}"
    quota: 1
    params: {declaration: "text(60)"}
    freeze_channels: [public]
    reveal: {channel_id: public, text: "{{actor}} 宣布：{{declaration}}"}
    description: 红队的一次性公开宣言（会让公共频道暂时冻结）。

checks:
  blue_wins: "${count(alive(by_faction('red'))) == 0}"
  red_wins: "${count(alive(by_faction('blue'))) == 0}"

invalidates_drafts: [player.updated, phase.changed]

ui:
  out_tags: [出局]
  highlight_tags: [红队, 蓝队]
---

# 最小测试剧本

四名玩家分成红、蓝两队，每队两人。每轮由主持人让大家依次发言，再投票请出一人；
一队全部出局则另一队获胜。

# 主持人手册

1. 开局：创建公共频道 `public`（成员为全部玩家），`set_phase` 写明阶段。
2. 发言：用 `set_floor_queue` 按座位顺序让每人发言一次。
3. 投票：用 `open_request(sealed=true)` 收集投票，关闭后用 `announce` 发布出局公告并 `update_player` 标记出局。
4. 每次出局后用 `evaluate` 检查胜负，达成条件就 `end_game`（winner 填阵营的显示名：红队或蓝队）。
5. 你会被每一条新消息唤醒：等的事还没发生（又一条发言、又一次作答）就直接 `sleep()`，不要重复操作。
