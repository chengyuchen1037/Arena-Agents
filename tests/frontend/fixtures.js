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
    schema_version: 5,
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

// ---- v0.13: the seat a person plays (docs/v0.12-contracts.md §11.7) ----------------------------------------

/** The server's state of one channel for the played seat; `over` replaces fields. */
function seatChannel(over = {}) {
  return { can_write: true, why_not: null, code: null, max_chars: 300, floor: null, can_pass: false, ...over };
}

/** The seat's token of a queue / turn floor (it holds the floor unless `mine` is false). */
const seatFloor = (over = {}) => ({
  kind: "queue",
  mine: true,
  paused: false,
  messages_left: 2,
  chars_left: 560,
  seconds_left: 87.5,
  deadline_clock: 100,
  ...over,
});

/** The seat's share of a free discussion. */
const seatFree = (over = {}) => ({
  kind: "free",
  mine: false,
  paused: false,
  messages_left: 4,
  chars_left: null,
  seconds_left: null,
  deadline_clock: null,
  ...over,
});

/** One open request of the played seat (its answer part). */
const seatRequest = (id, over = {}) => ({
  id,
  kind: "choice",
  max_chars: null,
  answered: false,
  my_answer: null,
  seconds_left: 90,
  deadline_clock: 102.5,
  channel_id: "public",
  ...over,
});

/** The white wolf king's ability as the server lists it for the seat that holds it (params from the scenario). */
const explodeAbility = (over = {}) => ({
  id: "white_wolf_explode",
  description: "白狼王自爆：只在主持人启用期间可用。公开宣布自爆并带走一名存活的其他玩家。",
  quota_left: 1,
  params: [
    { name: "target", type: "player_id", optional: false, candidates: PLAYER_IDS },
    { name: "declaration", type: "text", optional: false, max_chars: 120 },
  ],
  ...over,
});

/**
 * `PublicSession.takeover` of a game in which `seat` is played.  Every channel of the seat is in its default
 * state - the free public chat open (4 messages left), the private chat open (600 characters), the closed group
 * channels muted - unless `over.channels` replaces fields of one; `over` sets the rest (reshuffle_available,
 * seq, requests, abilities, attention).
 */
function makeTakeover(seat, over = {}) {
  const { channels: channelOver = {}, ...rest } = over;
  const channels = {};
  for (const ch of makeChannels()) {
    if (!ch.members.includes(seat)) continue;
    let base;
    if (ch.mode === "free") {
      base = seatChannel({ max_chars: ch.system ? 600 : 300, floor: ch.system ? null : seatFree() });
    } else {
      base = seatChannel({
        can_write: false,
        code: "CHANNEL_CLOSED",
        why_not: `频道「${ch.name}」当前已关闭，只有主持人可以发言。`,
        max_chars: null,
      });
    }
    channels[ch.id] = { ...base, ...(channelOver[ch.id] || {}) };
  }
  return { seat, reshuffle_available: false, seq: 40, channels, requests: [], abilities: [], attention: [], ...rest };
}

/** A session in which `seat` is played (its card says "human"); `over` replaces top-level fields. */
function makePlayedSession(seat, over = {}, takeoverOver = {}) {
  return makeSession({
    status: "paused",
    participants: makeParticipants({ [seat]: { state: "human" } }),
    takeover: makeTakeover(seat, takeoverOver),
    ...over,
  });
}

/** A `message.committed` event payload (flat MessageView). */
function messageEvent(id, seq, channelId, from, content, kind = "speech", visible = EVERYONE, extra = {}) {
  return { id, type: "message.committed", data: msg(seq, channelId, from, content, kind, visible, extra) };
}

// ---- v0.13.1: the error log and the thinking record (docs/v0.12-contracts.md §12.3 / §12.4) ----------------------------

/** One line of the error log (CallEntry): a successful turn of N3 (Carol) unless `over` says otherwise. */
function makeCall(id, over = {}) {
  return {
    id,
    ts: `2026-11-18T16:00:${String(id % 60).padStart(2, "0")}.000Z`,
    gt: 10 + id,
    kind: "call",
    status: "ok",
    actor_id: "N3",
    actor_name: "Carol",
    purpose: "turn",
    provider_id: "deepseek",
    model_id: "deepseek-chat",
    attempt: 1,
    elapsed_ms: 1234,
    prompt_tokens: 1000,
    completion_tokens: 200,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    finish_reason: "stop",
    http_status: null,
    error_class: null,
    message: null,
    ...over,
  };
}

/** A model request that went wrong (`status` "error" unless `over` says "timeout" / "retry"): HTTP 429, no numbers. */
function makeFailedCall(id, over = {}) {
  return makeCall(id, {
    status: "error",
    elapsed_ms: 900,
    prompt_tokens: null,
    completion_tokens: null,
    cache_read_tokens: null,
    cache_write_tokens: null,
    finish_reason: null,
    http_status: 429,
    error_class: "RateLimitError",
    message: "Rate limit reached for requests",
    ...over,
  });
}

/** One item of the thinking record (ThoughtItem); `n` makes the id (the position in the log) and the text. */
function makeThought(n, over = {}) {
  return {
    id: `1:${n * 1000}`,
    ts: `2026-11-18T16:00:${String(n % 60).padStart(2, "0")}.000Z`,
    gt: 20 + n,
    gen: 1,
    round: 1,
    kind: "thought",
    text: `第 ${n} 条思考`,
    model: "deepseek-reasoner",
    ...over,
  };
}

module.exports = {
  makeCall,
  makeFailedCall,
  makeThought,
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
  seatChannel,
  seatFloor,
  seatFree,
  seatRequest,
  explodeAbility,
  makeTakeover,
  makePlayedSession,
  floorNone,
  channel,
  msg,
  messageEvent,
};
