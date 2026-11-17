"use strict";
// Shared PublicSession v3 fixtures for the front-end tests (this file is not a test).
// Shapes mirror docs/v0.12-contracts.md §8 + §10.6: ids N1..N12, nicknames without numbers, three agent
// states, avatars as descriptions, budget / review / stall_notice, requests with answers.

const NAMES = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"];
const ROLE_BY_ID = {
  N1: "平民",
  N2: "平民",
  N3: "狼人",
  N4: "预言家",
  N5: "女巫",
  N6: "狼人",
  N7: "猎人",
  N8: "守卫",
  N9: "平民",
  N10: "狼人",
  N11: "平民",
  N12: "白狼王",
};

const pid = (n) => `N${n}`;
const PLAYER_IDS = Array.from({ length: 12 }, (_, i) => pid(i + 1));
const WOLVES = ["N3", "N6", "N10", "N12"];
const EVERYONE = ["moderator", ...PLAYER_IDS];

/** The avatar description of a seat in the minimal fixture library (tests/frontend/fixtures/parts.min.json). */
function avatarOf(seat) {
  // a different (hash based) pick per field, so neighbouring seats do not look alike
  const hash = (k) => {
    let h = (Math.imul(seat + 1, 2654435761) ^ Math.imul(k + 7, 40503)) >>> 0;
    h = Math.imul(h ^ (h >>> 15), 2246822519) >>> 0;
    h = Math.imul(h ^ (h >>> 13), 3266489917) >>> 0;
    return (h ^ (h >>> 16)) >>> 0;
  };
  const pick = (list, k) => list[hash(k) % list.length];
  return {
    v: 1,
    face: pick(["f00", "f01", "f02"], 1),
    skin: pick(["s00", "s01", "s02"], 2),
    hair: pick(["h01", "h02"], 3), // always a visible hairstyle: the pictures differ more
    hair_color: pick(["c00", "c01", "c02"], 4),
    eyes: pick(["e00", "e01", "e02"], 5),
    mouth: pick(["m00", "m01", "m02"], 6),
    accessory: pick(["a00", "a01", "a02"], 7),
    accessory_color: pick(["k00", "k01"], 8),
  };
}

function makeParticipants(over = {}) {
  return PLAYER_IDS.map((id, i) => ({
    id,
    seat: String(i + 1), // the contract sends the seat as a string
    name: NAMES[i],
    avatar: avatarOf(i + 1),
    tags: [ROLE_BY_ID[id], "存活"],
    out: false,
    state: "sleeping",
    sleep_until: null,
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
    msg(5, "wolves", "moderator", "狼人队友为 N3 Carol、N6 Frank、N10 Jack、N12 Leo。", "speech", ["moderator", ...WOLVES]),
    msg(8, "public", "N1", "我是好人，大家好。"),
    msg(9, "wolves", "N3", "今晚刀 N4。", "speech", ["moderator", ...WOLVES]),
    msg(11, "dm-N3", "N3", "主持人，刚才的计票是不是漏了一票？", "challenge", ["moderator", "N3"], { ticket_id: "t1" }),
    msg(12, "public", "N2", "我同意 N1 的看法。"),
  ];
}

/** A RequestView (contract §10.6 + opened_seq). */
function makeRequest(over = {}) {
  return {
    id: "r1",
    kind: "choice",
    question: "今晚的刀口是谁？",
    options: [
      { id: "N1", label: "1号 Alice" },
      { id: "N4", label: "4号 David" },
      { id: "N5", label: "5号 Eve" },
    ],
    to: WOLVES,
    status: "open",
    sealed: false,
    share_answers: true,
    auto_close: "unanimous",
    seconds_left: 90,
    opened_seq: 6,
    answered_by: [],
    answers: [],
    result: null,
    ...over,
  };
}

const answer = (player_id, value, label, extra = {}) => ({ player_id, value, label: label || value, reason: "", defaulted: false, ...extra });

function makeSession(over = {}) {
  const session = {
    session_id: "s-test",
    schema_version: 4,
    version: "v0.12.1",
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
    moderator: { id: "moderator", name: "主持人", state: "sleeping", sleep_until: null },
    channels: makeChannels(),
    messages: makeMessages(),
    requests: [],
    tickets: [{ id: "t1", player_id: "N3", status: "open", verdict: null, preview: "主持人，刚才的计票是不是漏了一票？", opened_seq: 11 }],
    dashboard: {
      factions: [
        { name: "狼人阵营", alive: 4, total: 4, variant: "danger" },
        { name: "神职", alive: 4, total: 4, variant: "purple" },
        { name: "平民阵营", alive: 4, total: 4, variant: "neutral" },
      ],
      tag_stats: [{ name: "存活", count: 12, variant: "success" }],
    },
    public_tags: ["存活", "出局", "警长"],
    tag_styles: { 狼人: { variant: "danger" }, 预言家: { variant: "purple" }, 出局: { variant: "muted" }, 警长: { variant: "warning" } },
    ui: { out_tags: ["出局"], status_tags: ["存活", "出局", "警长"], highlight_tags: ["警长"] },
    usage_stats: {
      prompt_tokens: 600,
      cache_read_tokens: 300,
      cache_write_tokens: 50,
      completion_tokens: 200,
      total_tokens: 1150,
      total_cost_usd: 0.01,
      cache_hit_rate: 0.3158,
      by_model: {},
      by_actor: {
        N1: { activations: 4, calls: 3, prompt_tokens: 600, cache_read_tokens: 300, cache_write_tokens: 50, completion_tokens: 100, cost_usd: 0.006, tool_calls: 5, thinking_rounds: 2, rejections: 1, cache_hit_rate: 0.3158 },
        moderator: { activations: 2, calls: 2, prompt_tokens: 400, cache_read_tokens: 0, cache_write_tokens: 0, completion_tokens: 100, cost_usd: 0.004, tool_calls: 9, thinking_rounds: 2, rejections: 0, cache_hit_rate: 0 },
      },
    },
    model_bindings: {
      moderator: { provider_id: "p1", model_id: "model-x", reasoning_depth: "high" },
    },
    binding_issues: [],
    restored_from_save: false,
    restore_notice: "",
    autosave_error: "",
    budget: { max_tokens: null, max_cost_usd: null, force_stop: false, used_tokens: 1150, used_cost_usd: 0.01, state: "ok" },
    memory_enabled: true,
    review: { available: false, done: [], running: false },
    stall_notice: "",
    demo: false,
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
  avatarOf,
  makeParticipants,
  makeChannels,
  makeMessages,
  makeRequest,
  answer,
  makeSession,
  floorNone,
  channel,
  msg,
  messageEvent,
};
