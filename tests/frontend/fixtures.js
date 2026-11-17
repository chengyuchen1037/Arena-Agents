"use strict";
// Shared PublicSession v2 fixtures for the front-end tests (this file is not a test).
// Shapes mirror app/world/projection.py + app/world/chat.py (docs/v0.12-contracts.md §8).

const NAMES = ["Alice 1", "Bob 2", "Carol 3", "David 4", "Eve 5", "Frank 6", "Grace 7", "Henry 8", "Ivy 9", "Jack 10", "Kate 11", "Leo 12"];
const ROLE_BY_ID = {
  p01: "平民",
  p02: "平民",
  p03: "狼人",
  p04: "预言家",
  p05: "女巫",
  p06: "狼人",
  p07: "猎人",
  p08: "守卫",
  p09: "平民",
  p10: "狼人",
  p11: "平民",
  p12: "白狼王",
};

const pid = (n) => `p${String(n).padStart(2, "0")}`;
const PLAYER_IDS = Array.from({ length: 12 }, (_, i) => pid(i + 1));
const WOLVES = ["p03", "p06", "p10", "p12"];
const EVERYONE = ["moderator", ...PLAYER_IDS];

function makeParticipants(over = {}) {
  return PLAYER_IDS.map((id, i) => ({
    id,
    seat: i + 1,
    name: NAMES[i],
    tags: [ROLE_BY_ID[id], "存活"],
    out: false,
    state: "idle",
    ...(over[id] || {}),
  }));
}

function floorNone() {
  return { kind: "none", active: null, paused: false, seconds_left: null, queue: [], free: null };
}

function channel(id, name, members, extra = {}) {
  return {
    id,
    name,
    type: "group",
    mode: "closed",
    members,
    readonly: [],
    frozen: false,
    system: false,
    owner: null,
    floor: floorNone(),
    ...extra,
  };
}

function makeChannels() {
  return [
    channel("public", "公共频道", EVERYONE, { mode: "free" }),
    channel("wolves", "狼人阵营", ["moderator", ...WOLVES]),
    ...PLAYER_IDS.map((id) =>
      channel(`dm-${id}`, `${id} 私聊`, ["moderator", id], { type: "dm", mode: "free", system: true, owner: id })
    ),
  ];
}

function msg(seq, channelId, from, content, kind = "speech", visible = EVERYONE, extra = {}) {
  const idx = PLAYER_IDS.indexOf(from);
  return {
    seq,
    ts: "2026-11-17T08:00:%02d.000Z".replace("%02d", String(seq % 60).padStart(2, "0")),
    channel_id: channelId,
    from,
    from_name: from === "moderator" ? "主持人" : idx >= 0 ? NAMES[idx] : from,
    content,
    kind,
    template_id: null,
    truncated: null,
    ticket_id: null,
    visible_to_ids: visible,
    ...extra,
  };
}

function makeMessages() {
  return [
    msg(3, "public", "moderator", "【对局开始】天黑请闭眼。", "announce"),
    msg(5, "wolves", "moderator", "狼人队友为 p03、p06、p10、p12。", "speech", ["moderator", ...WOLVES]),
    msg(8, "public", "p01", "我是好人，大家好。"),
    msg(9, "wolves", "p03", "今晚刀 p04。", "speech", ["moderator", ...WOLVES]),
    msg(11, "dm-p03", "p03", "主持人，刚才的计票是不是漏了一票？", "challenge", ["moderator", "p03"], { ticket_id: "t1" }),
    msg(12, "public", "p02", "我同意 p01 的看法。"),
  ];
}

function makeSession(over = {}) {
  const session = {
    session_id: "s-test",
    schema_version: 3,
    version: "v0.12",
    title: "Arena-Agents",
    scenario_name: "werewolf_12p",
    scenario_title: "狼人杀 12 人局",
    status: "running",
    pause_reason: "",
    hold_reason: "",
    winner: null,
    summary: "",
    phase_text: "第 1 天 · 白天发言",
    active_status_text: "对局进行中 · 公共频道发言",
    seq: 40,
    event_id: 100,
    clock: 12.5,
    participants: makeParticipants(),
    moderator: { id: "moderator", name: "主持人", state: "idle" },
    channels: makeChannels(),
    messages: makeMessages(),
    requests: [],
    tickets: [{ id: "t1", player_id: "p03", status: "open", verdict: null, preview: "主持人，刚才的计票是不是漏了一票？", opened_seq: 11 }],
    dashboard: {
      factions: [
        { name: "狼人阵营", alive: 4, total: 4, variant: "danger" },
        { name: "好人阵营", alive: 8, total: 8, variant: "success" },
      ],
      tag_stats: [{ name: "存活", count: 12, variant: "success" }],
    },
    public_tags: ["存活", "出局", "警长"],
    tag_styles: { 狼人: { variant: "danger" }, 预言家: { variant: "purple" }, 出局: { variant: "muted" } },
    ui: { out_tags: ["出局"], status_tags: ["存活", "出局"], highlight_tags: ["警长"] },
    usage_stats: {
      prompt_tokens: 1000,
      completion_tokens: 200,
      total_tokens: 1200,
      total_cost_usd: 0.01,
      by_model: {},
      by_actor: {
        p01: { calls: 3, prompt_tokens: 600, completion_tokens: 100, cost_usd: 0.006, tool_calls: 5, thinking_rounds: 2, rejections: 1 },
        moderator: { calls: 2, prompt_tokens: 400, completion_tokens: 100, cost_usd: 0.004, tool_calls: 9, thinking_rounds: 2, rejections: 0 },
      },
    },
    model_bindings: {
      moderator: { provider_id: "p1", model_id: "model-x", reasoning_depth: "high" },
    },
    binding_issues: [],
    restored_from_save: false,
    restore_notice: "",
    autosave_error: "",
    execution_depth: "medium",
  };
  return { ...session, ...over };
}

/** A `message.committed` event payload (flat MessageView). */
function messageEvent(id, seq, channelId, from, content, kind = "speech", visible = EVERYONE, extra = {}) {
  return { id, type: "message.committed", data: msg(seq, channelId, from, content, kind, visible, extra) };
}

module.exports = {
  NAMES,
  ROLE_BY_ID,
  PLAYER_IDS,
  WOLVES,
  EVERYONE,
  pid,
  makeParticipants,
  makeChannels,
  makeMessages,
  makeSession,
  floorNone,
  channel,
  msg,
  messageEvent,
};
