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

/** The internal id of the bot that sits in seat `n` (1-based) in the fixtures: b-<8 hex>, never shown (v0.14, H1). */
const botId = (n) => `b-${(0x1000 + n).toString(16).padStart(8, "0")}`;
const BOT_IDS = Array.from({ length: 12 }, (_, i) => botId(i + 1));

function makeParticipants(over = {}) {
  return PLAYER_IDS.map((id, i) => ({
    id,
    seat: String(i + 1), // the contract sends the seat as a string
    name: NAMES[i],
    avatar: avatarOf(i + 1),
    bot_id: botId(i + 1), // v0.14 (§13.8): the bot in the seat ("" for a v5 game) ...
    init_status: "ready", // ... and its initialization status (null for a v5 game): what the ✔ ⚠ ✖ ⏳ on the card show
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

/** `PublicSession.system_status` (v0.14, H26) of a session in `status`: the label of the top bar (the bots are all initialized here). */
function systemStatusOf(status, pauseReason = "", holdReason = "") {
  const PAUSE = { user: "手动暂停", hold: "主持人暂停了对局", stall: "对局长时间没有进展，已暂停", error: "运行出错，已暂停", budget: "已达到预算上限，已暂停" };
  if (status === "running") return { code: "in_game", text: "对局中", reason: "" };
  if (status === "paused") return { code: "paused", text: "已暂停", reason: holdReason ? `${PAUSE[pauseReason] || "已暂停"}：${holdReason}` : PAUSE[pauseReason] || "已暂停" };
  if (status === "finished") return { code: "finished", text: "已结束", reason: "" };
  return { code: "before_game", text: "对局前", reason: "" };
}

function makeSession(over = {}) {
  const status = over.status || "running";
  const session = {
    session_id: "s-test",
    schema_version: 6, // v0.14: the identities are dealt when the game starts (`dealt`), bots, system_status (§13.8)
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
    // v0.14 (§13.8): {"available", "done", "running", "supported", "deadline_s": 60, "seconds_left"}
    review: { available: false, done: [], running: false, supported: true, deadline_s: 60, seconds_left: null },
    // v0.14: have the identities been dealt?  A game that has not started has none (H52): `undealt(session)` makes such a session
    dealt: status !== "idle",
    system_status: systemStatusOf(status, over.pause_reason || "", over.hold_reason || ""),
    bots: makeBots(), // the live part of BotsPayload: the twelve bots of the table, all initialized
    stall_notice: "",
    demo: false,
  };
  return { ...session, ...over };
}

/** A session before the deal (H52): `dealt` is false and no seat has a tag - not an identity, not even "存活"; the status label says 对局前. */
function undealt(session) {
  return {
    ...session,
    dealt: false,
    participants: session.participants.map((p) => ({ ...p, tags: [] })),
    dashboard: { factions: [], tag_stats: [] },
  };
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
 * channels muted - unless `over.channels` replaces fields of one; `over` sets the rest (seq, requests, abilities,
 * attention).  (v0.14: no `reshuffle_available` - the re-deal is gone, the cards are dealt when the game starts.)
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
  return { seat, seq: 40, channels, requests: [], abilities: [], attention: [], ...rest };
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

// ---- v0.14: the bots (docs/v0.12-contracts.md §13.2 / §13.7 / §13.13) ------------------------------------------------------

/** `BotsPayload.limits` (`BotStore.limits_payload()`): the numbers the UI counts against. */
const BOT_LIMITS = {
  name_chars: 20,
  core_limits: { identity: 300, voice: 600, mind: 400 },
  core_extra_max: 3,
  core_extra_chars: 300,
  extra_title_chars: 20,
  arch_notes_chars: 800,
  digest_chars: 800,
  questions_chars: 400,
  playbook_max: 20,
  playbook_entry_chars: 80,
  people_max: 30,
  impression_chars: 80,
  contract_must_read_chars: 1200,
  init_timeout_s: 120,
  review_timeout_s: 60,
  jot_max: 10,
  jot_chars: 120,
  memory_parts: ["arch_notes", "digest", "playbook", "questions", "people"],
  part_labels: { arch_notes: "操作笔记", digest: "规则要点", playbook: "打法", questions: "待解问题", people: "印象" },
};

const CORE_HEADINGS = { identity: "身份", voice: "说话方式", mind: "分析方式" };

// (identity, voice, mind, extra title, extra text): a different L0 per default bot (the library of the real program has twelve of its own)
const DEFAULT_CORES = [
  ["打了二十多年线下局的老玩家，话不多，一开口就是结论。", "你说话短，通常两三句就停。爱反问。\n「你急什么？」「我只看他投了谁。」\n篇幅：40–110 字", "你先看发生了什么，再听谁说了什么。被结果打脸就认。", "口头禅", "「行，听你的，出了再说。」"],
  ["热心的话痨，爱把大家的话串起来。", "你说话热络，爱用“咱们”，一口气说好几句。\n「咱们捋一捋啊。」\n篇幅：120–260 字", "你把每个人的发言排成一条时间线，找前后对不上的地方。", "", ""],
  ["稳健的老好人，心里有杆秤。", "你说话慢条斯理，很少下断言。\n「我先说我的顾虑，不一定对。」\n篇幅：80–180 字", "你偏向先排除而不是先指认。证据不足宁可弃票。", "", ""],
  ["凭直觉打牌，觉得谁不对劲就会直说。", "你说话直接，句子很短。\n「我感觉他在编。」\n篇幅：30–90 字", "你把直觉当成线索，再去找一条能支持或推翻它的事实。", "口头禅", "「我就问一句，你怎么解释？」"],
  ["只信票型和记录的计算派。", "你说话克制，像在念记录。\n「三号第一轮投了五号。」\n篇幅：60–140 字", "你把每一轮的投票和发言顺序做成对照表，先找矛盾。", "", ""],
  ["强势的带队者，习惯替大家做决定。", "你说话斩钉截铁，爱点名归票。\n「今天就出这个人，理由两条。」\n篇幅：100–200 字", "你先给出一个明确的怀疑对象和两条理由，被有力反驳就当场换人。", "", ""],
  ["和事佬，擅长缓和气氛。", "你说话温和，常用“大家先别急”。\n「我们先把事实摆出来。」\n篇幅：70–150 字", "你留意谁在制造对立，关键时刻会明确说出自己的倾向。", "", ""],
  ["阴阳怪气的老油条，观察得很细。", "你说话带刺，爱用反问和夸张的赞美。\n「好好好，你说得都对。」\n篇幅：50–120 字", "你用调侃逼对方多说两句，再从反应里找破绽。", "", ""],
  ["认真的新手，每一步都想弄清规则。", "你说话认真，偶尔先确认规则再发言。\n「我想确认一下，这个能力今天可以用吗？」\n篇幅：90–200 字", "你把能确认的写在前面，把猜测单独标出来。", "", ""],
  ["爱讲故事的人，喜欢用连贯的叙述串起线索。", "你说话生动，爱用比喻，句子偏长。\n「想象一下，如果他是狼，昨晚他会怎么做？」\n篇幅：140–280 字", "你为每个可疑对象写一个“如果他是狼”的故事，再拿记录去检验。", "", ""],
  ["谨慎的复盘派，每一轮都在整理已知信息。", "你说话条理清楚，常用“目前已知”。\n「这一点还不能下结论。」\n篇幅：90–190 字", "你把信息分成已确认、待确认、猜测三类，只用前两类做判断。", "口头禅", "「先把已知的列出来。」"],
  ["冲动派，爱第一个表态，节奏很快。", "你说话快，句子短，常常一连串发问。\n「谁先说？」「别拖，直接站边。」\n篇幅：30–100 字", "你先抛出一个观点逼大家回应，被打脸就立刻收回。", "", ""],
];

const charCount = (s) => Array.from(String(s || "").trim()).length;

/** The L0 of the bot in seat `n` as the store keeps it: {identity, voice, mind, extras: [{title, slug, text}]}. */
function makeCore(n, over = {}) {
  const [identity, voice, mind, extraTitle, extraText] = DEFAULT_CORES[(n - 1) % DEFAULT_CORES.length];
  return { identity, voice, mind, extras: extraTitle ? [{ title: extraTitle, slug: "x1", text: extraText }] : [], ...over };
}

/** The canonical merged L0 file (the real server renders it; `name` is only a hint): header, 身份, 说话方式, 分析方式, the extras. */
function coreMarkdown(core, name) {
  const blocks = ["identity", "voice", "mind"].map((k) => (String(core[k] || "").trim() ? `# ${CORE_HEADINGS[k]}\n${String(core[k]).trim()}` : `# ${CORE_HEADINGS[k]}`));
  for (const e of core.extras || []) blocks.push(`# 附加：${e.title}\n${String(e.text || "").trim()}`);
  return `---\nformat: arena-core\nversion: 1${name ? `\nname: ${name}` : ""}\n---\n${blocks.join("\n\n")}\n`;
}

/** The avatar code of a description (§12.6): the eight part ids joined. */
function avatarCode(desc) {
  if (!desc) return null;
  const f = (k) => (k === "accessory" && desc[k] === "none" ? "a00" : k === "accessory_color" && !desc[k] ? "k00" : desc[k]);
  const out = ["face", "skin", "hair", "hair_color", "eyes", "mouth", "accessory", "accessory_color"].map(f);
  return out.every((x) => typeof x === "string" && x) ? out.join("") : null;
}

/** A `BotSummary` (§13.7) of the bot in seat `n` (1-based): initialized, a little history; `over` replaces fields (nested `init` / `core` merge). */
function makeBotSummary(n, over = {}) {
  const core = makeCore(n);
  const { init: initOver = {}, core: coreOver = {}, memory_stats: statsOver = {}, binding: bindingOver = {}, ...rest } = over;
  const chars = { identity: charCount(core.identity), voice: charCount(core.voice), mind: charCount(core.mind) };
  return {
    id: botId(n),
    name: NAMES[n - 1],
    avatar: avatarOf(n),
    avatar_code: avatarCode(avatarOf(n)),
    seat: `N${n}`,
    origin: "default",
    core: {
      complete: true,
      missing: [],
      chars,
      extras: core.extras.map((e) => ({ title: e.title, slug: e.slug, chars: charCount(e.text) })),
      updated_at: 1763500000,
      ...coreOver,
    },
    init: { status: "ready", at: 1763600000, model: "deepseek-flash", duration_s: 74.2, error: null, stale: [], ...initOver },
    frozen: true,
    memory_stats: { arch_notes: 102, digest: 85, playbook: 3, questions: 27, people: 2, record: n <= 5 ? "场次 7 · 胜 3 · 胜率 43%" : "", ...statsOver },
    staging: 0,
    binding: { ok: true, label: "DeepSeek / model-x", ...bindingOver },
    ...rest,
  };
}

/** One `Problem` of the start check (§13.7): `items` = [{code, message, fix}]. */
function makeProblem(scope, items, who = {}) {
  return { scope, bot_id: null, seat: null, name: null, ...who, items };
}

/** `BotsPayload`, the live part (§13.7: what `PublicSession.bots` and `bots.updated` carry): the twelve bots of the table; `over` replaces top-level fields. */
function makeBots(over = {}) {
  const bots = PLAYER_IDS.map((_, i) => makeBotSummary(i + 1));
  return {
    lineup: bots.map((b) => b.id),
    bots,
    memory_enabled: true,
    init_job: null,
    ready: { ok: true, problems: [] },
    learning_usage: { calls: 0, tokens: 0, cost_usd: 0 },
    ...over,
  };
}

/** The default library (`BotsPayload.defaults`): [{name, avatar, identity}] - what "从默认库导入" offers. */
function makeDefaults() {
  return NAMES.map((name, i) => ({ name, avatar: avatarOf(i + 1), identity: DEFAULT_CORES[i][0] }));
}

/** `GET /api/bots`: the live part + `limits` and `defaults`. */
function makeBotsFull(over = {}) {
  return { ...makeBots(over), limits: JSON.parse(JSON.stringify(BOT_LIMITS)), defaults: makeDefaults() };
}

/** A `BotDetail` (§13.7 / §13.13) of the bot in seat `n`: the summary + the merged L0 file, its sections, the memory of a scenario, the pending proposals. */
function makeBotDetail(n, over = {}) {
  const summary = makeBotSummary(n, over.summary || {});
  const core = makeCore(n);
  const { summary: _summary, core: coreOver = {}, memory: memoryOver = {}, ...rest } = over;
  return {
    ...summary,
    core: {
      ...summary.core,
      markdown: coreMarkdown(core, NAMES[n - 1]),
      sections: { identity: core.identity, voice: core.voice, mind: core.mind },
      extras: core.extras.map((e) => ({ title: e.title, slug: e.slug, chars: charCount(e.text), text: e.text })),
      ...coreOver,
    },
    memory: {
      scenario_id: "werewolf_12p",
      arch_notes: "我每次被叫醒，先看新动态和唤醒原因，判断有没有轮到我的事；没有就记一笔然后睡。",
      digest: "好人要找出狼，狼要藏起来。白天先发言再投票放逐，夜里各个身份分别行动。",
      playbook: [
        { text: "两个人都说自己是同一个身份时，先看谁的结论和之后的行动对得上。", confidence: "中", support: 3, counter: 1, source: "初始化", tags: ["发言"] },
        { text: "投票前先把已经确认的事实列出来，再决定投谁。", confidence: "中", support: 2, counter: 0, source: "初始化", tags: ["投票"] },
        { text: "警上发言先听完全部再站边。", confidence: "低", support: 0, counter: 0, source: "初始化", tags: ["警上"] },
      ],
      playbook_md:
        "## 假设\n- [把握:中|支持:3|反例:1|来源:初始化|#发言] 两个人都说自己是同一个身份时，先看谁的结论和之后的行动对得上。\n" +
        "- [把握:中|支持:2|反例:0|来源:初始化|#投票] 投票前先把已经确认的事实列出来，再决定投谁。\n- [把握:低|支持:0|反例:0|来源:初始化|#警上] 警上发言先听完全部再站边。",
      questions: "平票的时候，被 PK 的两个人该怎么发言才最有说服力？",
      people: { [NAMES[n % 12]]: "发言短，站边快，可信度中。", [NAMES[(n + 1) % 12]]: "逻辑细，抓细节准。" },
      people_md: `- ${NAMES[n % 12]}：发言短，站边快，可信度中。\n- ${NAMES[(n + 1) % 12]}：逻辑细，抓细节准。`,
      record: n <= 5 ? "场次 7，胜 3，负 4，胜率 43%\n平民：5 局，胜 3" : "",
      ...memoryOver,
    },
    staging: [],
    ...rest,
  };
}

/** One pending proposal of L0 (a review made it): {id, section, before, after, reason, source, created_at}. */
function makeProposal(over = {}) {
  return {
    id: "p-1a2b3c4d",
    section: "voice",
    before: "你说话短，通常两三句就停。",
    after: "你说话短，通常两三句就停。\n「先说结论，再补一句理由。」",
    reason: "接管者说话短、先说结论",
    source: "复盘 s-test",
    created_at: 1763700000,
    ...over,
  };
}

module.exports = {
  BOT_IDS,
  BOT_LIMITS,
  DEFAULT_CORES,
  botId,
  charCount,
  makeCore,
  coreMarkdown,
  avatarCode,
  makeBotSummary,
  makeBots,
  makeBotsFull,
  makeDefaults,
  makeBotDetail,
  makeProblem,
  makeProposal,
  systemStatusOf,
  undealt,
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
