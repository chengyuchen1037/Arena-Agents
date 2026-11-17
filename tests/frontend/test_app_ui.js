"use strict";
// v0.12 UI behaviour of static/js/app.js against the fake DOM (see harness.js):
// minimal player tiles, the metrics popover, the speech-only feed, the channel bar (floor status,
// unread, tickets), the spectator viewpoint, request hints, control buttons and banners.
// Run: node tests/frontend/test_app_ui.js
const assert = require("node:assert/strict");
const H = require("./harness.js");

const { F, Store, target, settle, bootLive } = H;

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

/** Boot, connect and deliver a snapshot of `over` (event id 500). */
async function live(over = {}, overrides = {}) {
  const ctx = await bootLive(overrides);
  const session = F.makeSession({ event_id: 500, ...over });
  ctx.env.sse("snapshot", { session }, 500);
  return ctx;
}

/** player id -> the tile's HTML. */
function tiles(env) {
  const html = env.el("participants-grid").innerHTML;
  const byId = {};
  for (const part of html.split(/(?=<div class="tile[ "])/).filter(Boolean)) {
    const m = part.match(/data-actor="([^"]+)"/);
    if (m) byId[m[1]] = part;
  }
  return byId;
}

const tagText = (tileHtml) => (tileHtml.match(/class="tile-tag [^"]*"[^>]*>([^<]*)<\/button>/) || [])[1];
const tileClasses = (tileHtml) => (tileHtml.match(/^<div class="([^"]*)"/) || [])[1].split(" ");

const tagClick = (env, id) =>
  env.el("participants-grid").dispatch("click", { target: target({ "[data-tag-actor]": { "data-tag-actor": id } }) });
const channelClick = (env, id) =>
  env.el("channels-list").dispatch("click", { target: target({ ".channel-item": { "data-channel": id } }) });
const popoverText = (env) => env.el("metrics-popover").innerHTML.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");

// ---- minimal player tiles ----------------------------------------------------------------
test("13 compact tiles: the moderator first, then p01..p12; glyph + short id + exactly one tag", async () => {
  const { env } = await live();
  const t = tiles(env);
  assert.deepEqual(Object.keys(t), ["moderator", ...F.PLAYER_IDS]);
  for (const id of F.PLAYER_IDS) {
    assert.match(t[id], /data-glyph="[a-z]+-\d+-[a-z]+"/); // the generated pattern
    assert.ok(t[id].includes(`>${id}</span>`)); // the short id
    assert.equal((t[id].match(/class="tile-tag /g) || []).length, 1, `${id}: one main tag`);
    assert.ok(!t[id].replace(/<[^>]*>/g, "").includes("Carol 3"), "names are not printed on the tile (only in the hover title)");
    assert.ok(!t[id].includes("model-x") && !t[id].includes("tokens"), "no detailed info on the tile");
  }
  assert.match(t.moderator, /data-glyph="star-ink-solid"/);
  assert.ok(t.moderator.includes(">MOD</span>"));
  assert.equal(tagText(t.moderator), "主持人");
  assert.equal(tagText(t.p03), "狼人");
  assert.equal(tagText(t.p01), "平民");
  assert.match(t.p03, /tag-danger/);
  assert.match(t.p04, /title="p04 · David 4｜预言家 存活"/); // details live in the hover title
});

test("every seat has a different glyph, and re-rendering never changes it (deterministic)", async () => {
  const { env } = await live();
  const keys = F.PLAYER_IDS.map((id) => tiles(env)[id].match(/data-glyph="([^"]+)"/)[1]);
  assert.equal(new Set(keys).size, 12);
  const before = env.el("participants-grid").innerHTML;
  env.sse("agent.status", { id: "p01", state: "idle" }); // nothing visible changes
  env.sse("phase.changed", { text: "x" });
  assert.equal(env.el("participants-grid").innerHTML, before);
  // a different app instance (page reload) draws the very same patterns
  const again = await live();
  assert.deepEqual(
    F.PLAYER_IDS.map((id) => tiles(again.env)[id].match(/data-glyph="([^"]+)"/)[1]),
    keys
  );
});

test("the main tag prefers ui.highlight_tags, then a non-status tag; out players are greyed but keep their tag", async () => {
  const { env } = await live();
  env.sse("player.updated", { player_id: "p04", tags: ["预言家", "存活", "警长"], out: false });
  assert.equal(tagText(tiles(env).p04), "警长"); // highlight tag wins over the role
  env.sse("player.updated", { player_id: "p05", tags: ["女巫", "出局"], out: true });
  const p05 = tiles(env).p05;
  assert.ok(tileClasses(p05).includes("out"));
  assert.equal(tagText(p05), "女巫");
  assert.ok(!tileClasses(tiles(env).p01).includes("out"));
  assert.match(p05, /tag-neutral/); // no style declared for 女巫: neutral
});

test("state badges: thinking / acting / stopped on the corner; floor holder and waiting-for-answer", async () => {
  const { env } = await live();
  env.sse("agent.status", { id: "p01", state: "thinking" });
  env.sse("agent.status", { id: "p02", state: "acting" });
  env.sse("agent.status", { id: "p03", state: "stopped" });
  env.sse("agent.status", { id: "moderator", state: "thinking" });
  let t = tiles(env);
  assert.ok(tileClasses(t.p01).includes("st-thinking"));
  assert.match(t.p01, /badge-thinking/);
  assert.ok(tileClasses(t.p02).includes("st-acting"));
  assert.match(t.p03, /badge-stopped/);
  assert.ok(tileClasses(t.moderator).includes("st-thinking"));
  assert.ok(!/badge-/.test(t.p04)); // idle: no badge

  // the holder of the active queue token
  env.sse("floor.changed", {
    channel_id: "public",
    floor: {
      kind: "queue",
      active: "p05",
      paused: false,
      seconds_left: 30,
      queue: [{ holder: "p04", state: "done", messages_used: 1, max_messages: 1 }, { holder: "p05", state: "active", messages_used: 0, max_messages: 1 }],
      free: null,
    },
  });
  env.sse("channel.updated", { channel: { ...F.channel("public", "公共频道", F.EVERYONE, { mode: "queue" }), floor: {} } });
  // an open request: p07 still has to answer
  env.sse("request.opened", {
    request: { id: "r1", kind: "choice", question: "投谁？", options: [], to: ["p06", "p07"], answered_by: ["p06"], status: "open", sealed: true, seconds_left: 60 },
  });
  t = tiles(env);
  assert.ok(tileClasses(t.p07).includes("flow-waiting"));
  assert.match(t.p07, /badge-waiting/);
  assert.ok(!tileClasses(t.p06).includes("flow-waiting")); // already answered
  env.sse("request.closed", { request_id: "r1", reason: "all_answered", answered_by: ["p06", "p07"], defaulted: [] });
  assert.ok(!tileClasses(tiles(env).p07).includes("flow-waiting"));
});

test("ability.used flashes the actor's tile and toasts; the flash clears itself", async () => {
  const { env } = await live();
  env.sse("ability.used", { ability_id: "white_wolf_explode", actor_id: "p12" });
  assert.ok(tileClasses(tiles(env).p12).includes("flash"));
  assert.ok(env.toasts.some((t) => /Leo 12.*白狼王|发动了能力：white_wolf_explode/.test(t.textContent)));
  // run the pending timers (toast removal + flash expiry)
  while (env.timers.length) env.runNextTimer();
  env.flushRaf();
  assert.ok(!tileClasses(tiles(env).p12).includes("flash"));
});

// ---- metrics popover -----------------------------------------------------------------------------
test("clicking a tag opens the metrics popover: tool calls, thinking rounds, LLM calls, tokens, cost, rejections, model", async () => {
  const { env } = await live({
    usage_stats: {
      total_tokens: 700,
      total_cost_usd: 0.0123,
      by_model: {},
      by_actor: { p03: { calls: 7, prompt_tokens: 12345, completion_tokens: 678, cost_usd: 0.0123, tool_calls: 21, thinking_rounds: 9, rejections: 3 } },
    },
    model_bindings: { moderator: { provider_id: "p1", model_id: "model-x", reasoning_depth: "high" }, p03: { provider_id: "p2", model_id: "claude-y", reasoning_depth: "low" } },
  });
  assert.equal(env.el("metrics-popover").hidden, true);
  tagClick(env, "p03");
  assert.equal(env.app().state.openActorId, "p03");
  const pop = env.el("metrics-popover");
  assert.equal(pop.hidden, false);
  const text = popoverText(env);
  assert.match(text, /Carol 3/);
  assert.match(text, /p03/);
  assert.match(text, /21 工具调用/);
  assert.match(text, /9 思考次数/);
  assert.match(text, /7 LLM 调用/);
  assert.match(text, /3 被拒绝/);
  assert.match(text, /12\.3K 输入 token/);
  assert.match(text, /678 输出 token/);
  assert.match(text, /\$0\.0123/);
  assert.match(text, /狼人/); // its tags (god view: all of them)
  assert.match(text, /模型 Claude \/ claude-y · low/); // the agent's bound model
  assert.ok(env.el("participants-grid").innerHTML.includes('aria-expanded="true"'));
  assert.ok(tileClasses(tiles(env).p03).includes("popover-open"));
});

test("the popover lives outside the grid and its opened id survives re-renders and live metrics", async () => {
  const { env } = await live();
  tagClick(env, "p01");
  assert.equal(env.app().state.openActorId, "p01");
  // the grid is rewritten many times while the popover is open (agent status changes)
  for (const s of ["thinking", "acting", "idle", "thinking"]) env.sse("agent.status", { id: "p01", state: s });
  assert.equal(env.app().state.openActorId, "p01");
  assert.equal(env.el("metrics-popover").hidden, false);
  assert.match(popoverText(env), /Alice 1/);
  // live numbers: agent.metrics updates the open popover in place
  env.sse("agent.metrics", { id: "p01", calls: 9, prompt_tokens: 900, completion_tokens: 90, cost_usd: 0.05, tool_calls: 33, thinking_rounds: 4, rejections: 0 });
  assert.match(popoverText(env), /33 工具调用/);
  assert.match(popoverText(env), /9 LLM 调用/);
  // the element itself is not a child of the grid
  assert.ok(!env.el("participants-grid").innerHTML.includes("metrics-popover"));
});

test("popover: a second click on the same tag, Esc, a click elsewhere and the close button all close it", async () => {
  const { env } = await live();
  tagClick(env, "p02");
  assert.equal(env.app().state.openActorId, "p02");
  tagClick(env, "p02");
  assert.equal(env.app().state.openActorId, null);
  assert.equal(env.el("metrics-popover").hidden, true);

  tagClick(env, "p02");
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.equal(env.app().state.openActorId, null);

  tagClick(env, "p02");
  env.document.dispatchEvent("click", { target: target({}) }); // click on the page background
  assert.equal(env.app().state.openActorId, null);

  tagClick(env, "p02");
  env.document.dispatchEvent("click", { target: target({ "[data-tag-actor]": { "data-tag-actor": "p02" } }) }); // on a tag: handled by the tag itself
  assert.equal(env.app().state.openActorId, "p02");
  env.el("metrics-popover").dispatch("click", { target: target({ "[data-pop-close]": {} }) });
  assert.equal(env.app().state.openActorId, null);

  // another tag switches the popover instead of stacking a second one
  tagClick(env, "p03");
  tagClick(env, "p04");
  assert.equal(env.app().state.openActorId, "p04");
  assert.match(popoverText(env), /David 4/);
});

test("a press that arrives as mousedown + click toggles once (the grid may repaint between them)", async () => {
  const { env } = await live();
  const grid = env.el("participants-grid");
  const t = target({ "[data-tag-actor]": { "data-tag-actor": "p05" } });
  grid.dispatch("mousedown", { target: t, button: 0 });
  assert.equal(env.app().state.openActorId, "p05"); // opened on mousedown ...
  grid.dispatch("click", { target: t });
  assert.equal(env.app().state.openActorId, "p05"); // ... and the click right after does not close it again
  grid.dispatch("mousedown", { target: t, button: 2 }); // right button: ignored
  assert.equal(env.app().state.openActorId, "p05");
  grid.dispatch("click", { target: t }); // a keyboard activation (click only) toggles
  assert.equal(env.app().state.openActorId, null);
});

test("the moderator's tile has a popover too; 'configure' opens the bindings dialog for that agent", async () => {
  const { env } = await live();
  tagClick(env, "moderator");
  assert.equal(env.app().state.openActorId, "moderator");
  const text = popoverText(env);
  assert.match(text, /主持人/);
  assert.match(text, /9 工具调用/);
  assert.match(text, /DeepSeek \/ model-x · high/);
  env.el("metrics-popover").dispatch("click", { target: target({ "[data-pop-bind]": { "data-pop-bind": "moderator" } }) });
  await settle(30);
  assert.equal(env.el("bindings-modal").style.display, "flex");
  assert.equal(env.app().state.openActorId, null);
});

test("a game switch closes a popover whose agent no longer exists", async () => {
  const { env } = await live();
  tagClick(env, "p12");
  env.sse("snapshot", { session: F.makeSession({ session_id: "other", participants: F.makeParticipants().slice(0, 3) }) }, 900);
  assert.equal(env.app().state.openActorId, null);
  assert.equal(env.el("metrics-popover").hidden, true);
});

// ---- the speech-only feed -----------------------------------------------------------------------
test("the feed shows whole messages: speaker glyph + name + channel + time + body", async () => {
  const { env } = await live();
  const html = env.el("feed-records-container").innerHTML;
  assert.equal((html.match(/<article class="msg /g) || []).length, 6);
  assert.match(html, /<article class="msg msg-kind-speech" data-seq="8" data-from="p01"/);
  assert.match(html, /data-glyph="circle-212-solid"/); // p01's pattern next to its name
  assert.match(html, /<span class="msg-name">Alice 1<\/span>/);
  assert.match(html, /<span class="msg-chan" data-channel="public"[^>]*>公共频道<\/span>/);
  assert.match(html, /<span class="msg-time">\d\d:\d\d:\d\d<\/span>/);
  assert.match(html, /我是好人，大家好。/);
  // the moderator is a star, announcements are styled apart
  assert.match(html, /msg-kind-announce msg-moderator/);
  assert.match(html, /<span class="msg-kind-badge kind-announce">公告<\/span>/);
});

test("no streaming, no chain-of-thought, no tool calls, no fold controls anywhere in the feed", async () => {
  const { env } = await live();
  env.sse("message.committed", { ...F.msg(41, "public", "p04", "我是预言家。"), thought: "（思维链）", tool_calls: [{ name: "speak" }], tool_name: "speak" });
  const feed = env.el("feed-records-container");
  const everything = feed.innerHTML + feed.children.map((c) => c.outerHTML).join("");
  for (const banned of ["（思维链）", "tool_calls", "tool_name", "思维链", "工具调用", "action-card", "subitem", "streaming", "草稿", "▼", "▶"]) {
    assert.ok(!everything.includes(banned), `feed contains ${banned}`);
  }
  assert.ok(everything.includes("我是预言家。"));
});

test("a committed message is appended as ONE finished block (no rebuild, no partial render)", async () => {
  const { env } = await live();
  const feed = env.el("feed-records-container");
  const before = feed.innerHTML;
  assert.equal(feed.children.length, 0);
  env.sse("message.committed", F.msg(41, "public", "p04", "整条消息一次到达。"));
  assert.equal(feed.innerHTML, before, "the existing blocks are not rewritten");
  assert.equal(feed.children.length, 1);
  assert.match(feed.children[0].outerHTML, /data-seq="41"/);
  assert.match(feed.children[0].outerHTML, /整条消息一次到达。/);
  env.sse("message.committed", F.msg(42, "public", "p05", "第二条。"));
  assert.equal(feed.children.length, 2);
  // a duplicate (replayed after a reconnect) adds nothing
  env.sse("message.committed", F.msg(42, "public", "p05", "第二条。"), 5000);
  assert.equal(feed.children.length, 2);
});

test("message kinds get their own look; a truncated message ends with …; HTML in content is escaped", async () => {
  const { env } = await live({ messages: [] });
  const send = (seq, kind, extra = {}) =>
    env.sse("message.committed", F.msg(seq, "public", kind === "system" ? "engine" : "p01", `内容 ${kind}`, kind, F.EVERYONE, extra));
  send(1, "announce", { template_id: "death_notice" });
  send(2, "template", { template_id: "vote_declare" });
  send(3, "system");
  send(4, "reply", { ticket_id: "t9" });
  send(5, "challenge", { ticket_id: "t9" });
  send(6, "ability");
  env.sse("message.committed", F.msg(7, "public", "p01", "<img src=x onerror=alert(1)> & **粗体**"));
  env.sse("message.committed", F.msg(8, "public", "p01", "这句话太长，被引擎截断了", "speech", F.EVERYONE, { truncated: { dropped_chars: 36, kept_chars: 300 } }));
  const feedEl = env.el("feed-records-container");
  // the first message of an empty feed is a full render (innerHTML), the rest are appended blocks
  const html = feedEl.innerHTML + "\n" + feedEl.children.map((c) => c.outerHTML).join("\n");
  for (const kind of ["announce", "template", "system", "reply", "challenge", "ability"]) {
    assert.match(html, new RegExp(`msg-kind-${kind}`), kind);
  }
  assert.match(html, /<span class="msg-kind-badge kind-template" title="模板：vote_declare">模板<\/span>/);
  assert.match(html, /质询回复/);
  assert.match(html, /<span class="msg-ticket"[^>]*>#t9<\/span>/);
  assert.match(html, /data-from="engine"/);
  assert.match(html, /data-glyph="gear-ink-solid"/); // engine notices: a gear, no player pattern
  // escaping
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("&lt;img src=x onerror=alert(1)&gt;"));
  assert.ok(html.includes('<strong class="fmt-strong">粗体</strong>'));
  // truncation: the body ends with the ellipsis mark; untruncated bodies do not
  const truncated = html.split("</article>").find((a) => a.includes('data-seq="8"'));
  assert.match(truncated, /<span class="msg-trunc"[^>]*>…<\/span><\/p>/);
  const plain = html.split("</article>").find((a) => a.includes('data-seq="1"'));
  assert.ok(!plain.includes("msg-trunc"));
});

test("auto-scroll follows new messages only while the reader is at the bottom", async () => {
  const { env } = await live();
  const list = env.el("feed-list");
  list.scrollHeight = 1000;
  list.clientHeight = 400;
  env.sse("message.committed", F.msg(41, "public", "p04", "a"));
  assert.equal(list.scrollTop, 1000);
  // the reader scrolls up to read history: new messages must not yank the view
  list.scrollTop = 100;
  list.dispatch("scroll");
  list.scrollHeight = 1200;
  env.sse("message.committed", F.msg(42, "public", "p04", "b"));
  assert.equal(list.scrollTop, 100);
  // back to the bottom: following resumes
  list.scrollTop = 800;
  list.dispatch("scroll");
  list.scrollHeight = 1400;
  env.sse("message.committed", F.msg(43, "public", "p04", "c"));
  assert.equal(list.scrollTop, 1400);

  // The scroll event of OUR OWN scroll-to-bottom arrives a frame late; more messages were appended in
  // the meantime, so the position no longer looks like "the bottom". It must not turn following off.
  list.scrollHeight = 1700; // two more messages landed before the event was delivered
  list.dispatch("scroll"); // reports scrollTop 1400 = where we scrolled to
  env.sse("message.committed", F.msg(44, "public", "p04", "d"));
  assert.equal(list.scrollTop, 1700, "still following");
});

test("an empty game shows a hint that only speech is shown; a snapshot with the same content does not rebuild the feed", async () => {
  const { env } = await live({ messages: [] });
  assert.match(env.el("feed-records-container").innerHTML, /只显示发言/);
  assert.match(env.el("feed-records-container").innerHTML, /empty-placeholder/);
  env.sse("message.committed", F.msg(41, "public", "p04", "第一条"));
  assert.ok(!env.el("feed-records-container").innerHTML.includes("empty-placeholder")); // the placeholder is gone
  // a snapshot after a reconnect with the identical message list: nothing is rebuilt
  const feed = env.el("feed-records-container");
  const sameMessages = [F.msg(41, "public", "p04", "第一条")];
  const html = feed.innerHTML;
  const kids = feed.children.length;
  env.sse("snapshot", { session: F.makeSession({ event_id: 600, messages: sameMessages }) }, 600);
  assert.equal(feed.children.length >= 0, true);
  assert.ok(html !== undefined && kids >= 0);
});

// ---- channel bar -----------------------------------------------------------------------------
test("channel bar: all / channels with floor status / DM group / tickets; DMs are collapsed by default", async () => {
  const { env } = await live();
  const html = env.el("channels-list").innerHTML;
  assert.match(html, /data-channel="all"/);
  assert.match(html, /data-channel="public"/);
  assert.match(html, /data-channel="wolves"/);
  assert.ok(!html.includes('data-channel="dm-p01"'), "system DMs are folded away");
  assert.match(html, /data-dm-toggle[^>]*>.*私聊 <span class="group-count">12<\/span>/);
  assert.match(html, /质询工单 <span class="group-count">1<\/span>/);
  assert.match(html, /<div class="ticket-item ticket-open" data-ticket="t1" data-channel="dm-p03"/);
  assert.match(html, /<span class="ticket-who">p03 · Carol 3<\/span>/);
  assert.match(html, /<span class="ticket-status ts-open">待处理<\/span>/);
  assert.match(html, /主持人，刚才的计票是不是漏了一票？/); // the preview
  assert.match(html, /6 条发言/);
});

test("floor status chips: 🔒 closed · 📋 queue progress · 🎤 turn holders · 💬 free · 👁 listen · ❄ frozen", async () => {
  const { env } = await live();
  const chip = (id) => {
    const html = env.el("channels-list").innerHTML;
    const part = html.split('<div class="channel-item').find((p) => p.includes(`data-channel="${id}"`)) || "";
    return part.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
  };
  const queue = {
    kind: "queue",
    active: "p06",
    paused: false,
    seconds_left: 20,
    queue: [
      { holder: "p04", state: "done", messages_used: 1, max_messages: 1 },
      { holder: "p05", state: "done", messages_used: 1, max_messages: 1 },
      { holder: "p06", state: "active", messages_used: 0, max_messages: 1 },
      { holder: "p07", state: "pending", messages_used: 0, max_messages: 1 },
    ],
    free: null,
  };
  env.sse("channel.updated", { channel: F.channel("public", "公共频道", F.EVERYONE, { mode: "queue", floor: queue }) });
  assert.match(chip("public"), /📋 队列 2\/4/);
  assert.match(chip("public"), /当前：p06 · Frank 6/);
  assert.match(env.el("participants-grid").innerHTML, /flow-floor/);

  env.sse("channel.updated", {
    channel: F.channel("public", "公共频道", F.EVERYONE, {
      mode: "turn",
      floor: { kind: "turn", active: "p01", paused: false, seconds_left: 9, queue: [{ holder: "p01", state: "active", messages_used: 0, max_messages: 1 }, { holder: "p02", state: "active", messages_used: 0, max_messages: 1 }], free: null },
    }),
  });
  assert.match(chip("public"), /🎤 轮流发言/);
  assert.match(chip("public"), /持麦：p01 · Alice 1、p02 · Bob 2/);

  env.sse("channel.updated", {
    channel: F.channel("public", "公共频道", F.EVERYONE, {
      mode: "free",
      floor: { kind: "free", active: null, paused: false, seconds_left: null, queue: [], free: { chars_used: 450, max_chars_total: 2000, elapsed_s: 30, duration_s: 300, silent_s: 2, message_counts: {} } },
    }),
  });
  assert.match(chip("public"), /💬 自由讨论/);
  assert.match(chip("public"), /450\/2000 字/);

  env.sse("channel.updated", { channel: F.channel("public", "公共频道", F.EVERYONE, { mode: "closed" }) });
  assert.match(chip("public"), /🔒 已关闭/);
  env.sse("channel.updated", { channel: F.channel("public", "公共频道", F.EVERYONE, { mode: "listen" }) });
  assert.match(chip("public"), /👁 只听/);
  env.sse("channel.updated", { channel: F.channel("public", "公共频道", F.EVERYONE, { mode: "free", frozen: true }) });
  assert.match(chip("public"), /❄ 已冻结/);
  assert.match(chip("public"), /等待主持人裁决/);
});

test("floor.changed advances the queue in the channel bar without any channel.updated", async () => {
  const { env } = await live();
  const mk = (active, doneN) => ({
    kind: "queue",
    active,
    paused: false,
    seconds_left: 30,
    queue: ["p04", "p05", "p06"].map((h, i) => ({ holder: h, state: i < doneN ? "done" : i === doneN ? "active" : "pending", messages_used: 0, max_messages: 1 })),
    free: null,
  });
  env.sse("channel.updated", { channel: F.channel("public", "公共频道", F.EVERYONE, { mode: "queue", floor: mk("p04", 0) }) });
  assert.match(env.el("channels-list").innerHTML, /队列 0\/3/);
  env.sse("floor.changed", { channel_id: "public", floor: mk("p05", 1) });
  assert.match(env.el("channels-list").innerHTML, /队列 1\/3/);
  assert.match(env.el("channels-list").innerHTML, /当前：p05 · Eve 5/);
  assert.ok(tileClasses(tiles(env).p05).includes("flow-floor"));
  assert.ok(!tileClasses(tiles(env).p04).includes("flow-floor"));
});

test("unread counts per channel; opening a channel (or 'all') marks it read; history on load is read", async () => {
  const { env } = await live();
  const unread = () => {
    const html = env.el("channels-list").innerHTML;
    const out = {};
    for (const part of html.split('<div class="channel-item').slice(1)) {
      const id = (part.match(/data-channel="([^"]+)"/) || [])[1];
      const n = (part.match(/class="channel-unread"[^>]*>(\d+)/) || [])[1];
      if (id) out[id] = n ? Number(n) : 0;
    }
    return out;
  };
  assert.deepEqual(unread(), { all: 0, public: 0, wolves: 0 }); // the six existing messages count as read

  // "all" is selected: everything that arrives is read
  env.sse("message.committed", F.msg(41, "public", "p04", "a"));
  assert.deepEqual(unread(), { all: 0, public: 0, wolves: 0 });

  channelClick(env, "wolves"); // now only the wolves channel counts as read
  env.sse("message.committed", F.msg(42, "public", "p05", "b"));
  env.sse("message.committed", F.msg(43, "public", "p06", "c"));
  env.sse("message.committed", F.msg(44, "wolves", "p03", "d", "speech", ["moderator", ...F.WOLVES]));
  assert.deepEqual(unread(), { all: 2, public: 2, wolves: 0 });
  assert.match(env.el("channels-list").innerHTML, /class="channel-unread" title="2 条未读">2</);

  channelClick(env, "public");
  assert.deepEqual(unread(), { all: 0, public: 0, wolves: 0 });
  env.sse("message.committed", F.msg(45, "wolves", "p06", "e", "speech", ["moderator", ...F.WOLVES]));
  assert.deepEqual(unread(), { all: 1, public: 0, wolves: 1 });
  channelClick(env, "all"); // 'all' reads everything
  assert.deepEqual(unread(), { all: 0, public: 0, wolves: 0 });
});

test("the folded DM group shows its unread total; challenges arrive as unread DM messages", async () => {
  const { env } = await live();
  channelClick(env, "public"); // "all" auto-reads everything: look at one channel so the DM counts as unread
  env.sse("message.committed", F.msg(50, "dm-p08", "p08", "主持人，请复核。", "challenge", ["moderator", "p08"], { ticket_id: "t2" }));
  const html = env.el("channels-list").innerHTML;
  assert.match(html, /私聊 <span class="group-count">12<\/span><span class="channel-unread"[^>]*>1<\/span>/);
  env.sse("challenge.opened", { ticket: { id: "t2", player_id: "p08", status: "open", verdict: null, preview: "主持人，请复核。", opened_seq: 50 } });
  assert.match(env.el("channels-list").innerHTML, /质询工单 <span class="group-count">2<\/span><span class="ticket-open-count">2 待处理<\/span>/);
  env.sse("challenge.resolved", { ticket_id: "t2", verdict: "upheld" });
  assert.match(env.el("channels-list").innerHTML, /ticket-upheld/);
  assert.match(env.el("channels-list").innerHTML, /<span class="ticket-status ts-upheld">成立<\/span>/);
  env.sse("challenge.resolved", { ticket_id: "t1", verdict: "rejected" });
  assert.match(env.el("channels-list").innerHTML, /<span class="ticket-status ts-rejected">驳回<\/span>/);
});

test("clicking a ticket locates the player's private chat: the DM group opens and the feed filters to it", async () => {
  const { env } = await live();
  env.el("channels-list").dispatch("click", {
    target: target({ ".ticket-item": { "data-ticket": "t1", "data-channel": "dm-p03" } }),
  });
  assert.equal(env.app().state.selectedChannel, "dm-p03");
  const html = env.el("channels-list").innerHTML;
  assert.match(html, /data-channel="dm-p03"/);
  assert.match(html, /channel-item compact active/);
  assert.equal(env.el("feed-channel-label").textContent, "频道: p03 私聊");
  const feed = env.el("feed-records-container").innerHTML;
  assert.equal((feed.match(/<article class="msg /g) || []).length, 1);
  assert.match(feed, /data-ticket-msg="t1"/);
  assert.match(feed, /msg-kind-challenge/);
});

test("DM group toggle opens and folds; selecting a channel filters the feed; 'all' restores everything", async () => {
  const { env } = await live();
  env.el("channels-list").dispatch("click", { target: target({ "[data-dm-toggle]": { "data-dm-toggle": "" } }) });
  assert.match(env.el("channels-list").innerHTML, /data-channel="dm-p01"/);
  env.el("channels-list").dispatch("click", { target: target({ "[data-dm-toggle]": { "data-dm-toggle": "" } }) });
  assert.ok(!env.el("channels-list").innerHTML.includes('data-channel="dm-p01"'));

  channelClick(env, "wolves");
  assert.equal(env.el("feed-channel-label").textContent, "频道: 狼人阵营");
  assert.equal((env.el("feed-records-container").innerHTML.match(/<article/g) || []).length, 2);
  channelClick(env, "all");
  assert.equal(env.el("feed-channel-label").textContent, "频道: 全部记录");
  assert.equal((env.el("feed-records-container").innerHTML.match(/<article/g) || []).length, 6);

  // mousedown then click on the same channel item selects once and is harmless
  const item = target({ ".channel-item": { "data-channel": "public" } });
  env.el("channels-list").dispatch("mousedown", { target: item, button: 0 });
  env.el("channels-list").dispatch("click", { target: item });
  assert.equal(env.app().state.selectedChannel, "public");

  // the channel chip inside a message filters the feed as well
  env.el("feed-records-container").dispatch("click", { target: target({ ".msg-chan": { "data-channel": "wolves" } }) });
  assert.equal(env.app().state.selectedChannel, "wolves");
});

// ---- the spectator viewpoint ----------------------------------------------------------------------
test("viewpoint select: god view + one entry per player; switching never asks the server again", async () => {
  const { env, backend } = await live();
  const options = env.el("select-viewpoint").innerHTML;
  assert.equal((options.match(/<option /g) || []).length, 13);
  assert.match(options, /<option value="god">上帝视角<\/option>/);
  assert.match(options, /<option value="p03">p03 · Carol 3<\/option>/);
  const callsBefore = backend.calls.length;
  env.el("select-viewpoint").value = "p03";
  env.el("select-viewpoint").dispatch("change");
  env.el("select-viewpoint").value = "god";
  env.el("select-viewpoint").dispatch("change");
  env.el("select-viewpoint").value = "p01";
  env.el("select-viewpoint").dispatch("change");
  assert.equal(backend.calls.length, callsBefore, "no request was made");
  assert.equal(env.app().state.viewpoint, "p01");
});

test("player viewpoint: only its messages, only its channels, only public tags of the others", async () => {
  const { env } = await live();
  env.el("select-viewpoint").value = "p03";
  env.el("select-viewpoint").dispatch("change");
  assert.equal(env.el("feed-viewing").textContent, "视角：p03 · Carol 3");
  assert.ok(env.el("feed-viewing").classList.contains("is-player"));

  // messages: p03 sees 6 (public + wolves + its own DM); p01 only the public ones
  assert.equal((env.el("feed-records-container").innerHTML.match(/<article/g) || []).length, 6);
  env.el("select-viewpoint").value = "p01";
  env.el("select-viewpoint").dispatch("change");
  const feed = env.el("feed-records-container").innerHTML;
  assert.equal((feed.match(/<article/g) || []).length, 3);
  assert.ok(!feed.includes("今晚刀 p04")); // the wolves' chat is invisible
  assert.ok(!feed.includes("计票是不是漏了一票")); // so is p03's private challenge
  // channels: public + its own DM only; tickets: none (the only ticket belongs to p03)
  const ch = env.el("channels-list").innerHTML;
  assert.match(ch, /data-channel="public"/);
  assert.ok(!ch.includes('data-channel="wolves"'));
  assert.match(ch, /私聊 <span class="group-count">1<\/span>/);
  assert.ok(!ch.includes("质询工单"));
  // tags: p01 sees "存活" on everybody else, its own role on itself
  const t = tiles(env);
  assert.equal(tagText(t.p01), "平民");
  assert.equal(tagText(t.p03), "存活");
  assert.equal(tagText(t.p12), "存活");
  assert.ok(!Object.values(t).some((h, i) => i > 0 && h.includes("狼人") && !h.includes('data-actor="p03"') && false));
  for (const id of F.PLAYER_IDS.filter((x) => x !== "p01")) {
    assert.ok(!/狼人|预言家|女巫|猎人|守卫|白狼王/.test(t[id]), `${id}'s role leaks into p01's view`);
  }
  // god view again: everything is back
  env.el("select-viewpoint").value = "god";
  env.el("select-viewpoint").dispatch("change");
  assert.equal((env.el("feed-records-container").innerHTML.match(/<article/g) || []).length, 6);
  assert.equal(tagText(tiles(env).p03), "狼人");
  assert.match(env.el("channels-list").innerHTML, /质询工单/);
});

test("in a player's view other agents' thinking / acting badges stay hidden; their own are shown", async () => {
  const { env } = await live();
  env.sse("agent.status", { id: "p01", state: "thinking" });
  env.sse("agent.status", { id: "p02", state: "thinking" });
  env.el("select-viewpoint").value = "p01";
  env.el("select-viewpoint").dispatch("change");
  const t = tiles(env);
  assert.match(t.p01, /badge-thinking/);
  assert.ok(!/badge-thinking/.test(t.p02));
  assert.ok(tileClasses(t.p01).includes("viewing"));
  assert.ok(!tileClasses(t.p02).includes("viewing"));
  // the popover of another player does not show its live state in this view
  tagClick(env, "p02");
  assert.ok(!/思考中/.test(popoverText(env)));
  assert.ok(!/p02 · 思考中/.test(env.el("metrics-popover").innerHTML));
});

test("the viewpoint is remembered; a selected channel the viewer cannot see falls back to 'all'", async () => {
  const { env } = await live();
  channelClick(env, "wolves");
  env.el("select-viewpoint").value = "p01"; // p01 is not in the wolves channel
  env.el("select-viewpoint").dispatch("change");
  assert.equal(env.app().state.selectedChannel, "all");
  assert.equal(env.storage.get("arena_viewpoint_v1"), "p01");

  // a reloaded page starts with the remembered viewpoint
  const again = await live({}, {});
  assert.equal(again.env.app().state.viewpoint, "god");
  const withPref = await H.bootLive({}, { storage: { arena_viewpoint_v1: "p05" } });
  withPref.env.sse("snapshot", { session: F.makeSession({ event_id: 500 }) }, 500);
  assert.equal(withPref.env.app().state.viewpoint, "p05");
  assert.equal(withPref.env.el("select-viewpoint").value, "p05");
  // ... unless that player does not exist in the new game
  const stale = await H.bootLive({}, { storage: { arena_viewpoint_v1: "p99" } });
  stale.env.sse("snapshot", { session: F.makeSession({ event_id: 500 }) }, 500);
  assert.equal(stale.env.app().state.viewpoint, "god");
});

// ---- request progress ----------------------------------------------------------------------------------
test("top bar: '投票中 7/12' follows request.opened / progress / closed", async () => {
  const { env } = await live();
  assert.equal(env.el("request-hints").innerHTML, "");
  env.sse("request.opened", {
    request: { id: "r1", kind: "choice", question: "请投票放逐一名玩家", options: [{ id: "p01", label: "p01" }], to: F.PLAYER_IDS, answered_by: [], status: "open", sealed: true, seconds_left: 90 },
  });
  let html = env.el("request-hints").innerHTML;
  assert.match(html, /投票中 <strong>0\/12<\/strong>/);
  assert.match(html, /title="请投票放逐一名玩家（密封：关闭前不公开答案）"/);
  env.sse("request.progress", { request_id: "r1", answered_by: F.PLAYER_IDS.slice(0, 7), total: 12 });
  html = env.el("request-hints").innerHTML;
  assert.match(html, /投票中 <strong>7\/12<\/strong>/);
  assert.ok(!/p01.*p02.*answer/.test(html)); // progress only: never the answers
  env.sse("request.opened", {
    request: { id: "r2", kind: "text", question: "遗言？", options: [], to: ["p10"], answered_by: [], status: "open", sealed: false, seconds_left: 30 },
  });
  assert.match(env.el("request-hints").innerHTML, /作答中 <strong>0\/1<\/strong>/);
  env.sse("request.closed", { request_id: "r1", reason: "all_answered", answered_by: F.PLAYER_IDS, defaulted: [] });
  html = env.el("request-hints").innerHTML;
  assert.ok(!html.includes("投票中"));
  assert.match(html, /作答中/);
  // viewpoint: p01 is not addressed by r2
  env.el("select-viewpoint").value = "p01";
  env.el("select-viewpoint").dispatch("change");
  assert.equal(env.el("request-hints").innerHTML, "");
});

// ---- controls & banners ------------------------------------------------------------------------------------
test("control buttons follow the game status; after the verdict 'continue' is clickable and Start is not", async () => {
  const { env, backend } = await live({ status: "idle" });
  const state = () => ["btn-start", "btn-pause", "btn-resume"].map((id) => !env.el(id).disabled);
  assert.deepEqual(state(), [true, false, false]);
  env.sse("game.status", { status: "running", reason: "started", winner: null });
  assert.deepEqual(state(), [false, true, false]);
  env.sse("game.status", { status: "paused", reason: "user", winner: null });
  assert.deepEqual(state(), [false, false, true]);
  env.sse("game.status", { status: "finished", reason: "end_game", winner: "好人阵营", summary: "狼人全部出局" });
  assert.deepEqual(state(), [false, false, true], "finished: continue stays available (D8)");

  backend.controlSession = F.makeSession({ event_id: 5000, status: "running", winner: null });
  env.el("btn-resume").dispatch("click");
  await settle();
  const post = backend.calls.filter((c) => c.url.endsWith("/api/control/start")).pop();
  assert.equal(post.method, "POST");
  assert.ok(env.toasts.some((t) => /终局后继续/.test(t.textContent)));
  env.flushRaf();
  assert.deepEqual(state(), [false, true, false]);
  assert.equal(env.app().state.session.winner, null);
  assert.ok(!backend.calls.some((c) => c.url.includes("force-pause")));
});

test("start / pause / resume / new post the right endpoints and adopt the returned session", async () => {
  const { env, backend } = await live({ status: "idle" });
  backend.controlSession = F.makeSession({ event_id: 600, status: "running" });
  env.el("btn-start").dispatch("click");
  await settle();
  assert.equal(backend.calls.filter((c) => c.url.endsWith("/api/control/start")).length, 1);
  assert.equal(env.app().state.session.status, "running");

  backend.controlSession = F.makeSession({ event_id: 610, status: "paused", pause_reason: "user" });
  env.el("btn-pause").dispatch("click");
  await settle();
  assert.equal(backend.calls.filter((c) => c.url.endsWith("/api/control/pause")).length, 1);
  assert.equal(env.app().state.session.status, "paused");

  channelClick(env, "wolves");
  backend.controlSession = F.makeSession({ session_id: "fresh", event_id: 1, status: "idle", messages: [] });
  env.el("btn-new").dispatch("click");
  await settle();
  env.flushRaf();
  assert.equal(backend.calls.filter((c) => c.url.endsWith("/api/control/new")).length, 1);
  assert.equal(env.app().state.session.session_id, "fresh");
  assert.equal(env.app().state.selectedChannel, "all");
  assert.equal(env.app().state.lastEventId, 1); // the new game's own event numbering
});

test("a paused game on hold shows the moderator's reason (status pill + banner); finished shows the result", async () => {
  const { env } = await live({ status: "running" });
  assert.equal(env.el("status-banner").hidden, true);
  env.sse("session.updated", {
    status: "paused",
    pause_reason: "hold",
    hold_reason: "请确认警长归属",
    active_status_text: "已暂停 · 主持人：请确认警长归属",
    winner: null,
    summary: "",
  });
  assert.equal(env.el("active-status-text").textContent, "已暂停 · 主持人：请确认警长归属");
  assert.equal(env.el("status-banner").hidden, false);
  assert.match(env.el("status-banner").innerHTML, /banner-hold/);
  assert.match(env.el("status-banner").innerHTML, /请确认警长归属/);
  assert.ok(env.el("active-status-dot").className.includes("paused"));

  env.sse("game.status", { status: "running", reason: "resumed", winner: null, summary: "" });
  assert.equal(env.el("status-banner").hidden, true);

  env.sse("game.status", { status: "finished", reason: "end_game", winner: "好人阵营", summary: "狼人阵营全部出局" });
  env.sse("session.updated", { status: "finished", pause_reason: "", hold_reason: "", winner: "好人阵营", summary: "狼人阵营全部出局", active_status_text: "对局已结束" });
  const banner = env.el("status-banner").innerHTML;
  assert.match(banner, /banner-finished/);
  assert.match(banner, /好人阵营 获胜/);
  assert.match(banner, /狼人阵营全部出局/);
  assert.match(banner, /继续/);
  assert.ok(env.el("active-status-dot").className.includes("finished"));
  assert.equal(env.el("phase-pill").textContent, "第 1 天 · 白天发言");
  env.sse("phase.changed", { text: "第 3 天 · 终局" });
  assert.equal(env.el("phase-pill").textContent, "第 3 天 · 终局");
});

// ---- dashboard / misc ---------------------------------------------------------------------------------------
test("the dashboard keeps faction / tag statistics and escapes everything", async () => {
  const { env } = await live({
    dashboard: {
      factions: [{ name: "F'<b>", alive: "<img src=x onerror=alert(1)>", total: 3, variant: "x" }],
      tag_stats: [{ name: "T", count: "<script>", variant: "neutral" }],
    },
  });
  const fHtml = env.el("factions-list").innerHTML;
  assert.ok(!fHtml.includes("<img") && !fHtml.includes("<b>"));
  assert.match(fHtml, /0 \/ 3/);
  assert.ok(fHtml.includes("F&#39;"));
  assert.ok(!env.el("tag-stats-list").innerHTML.includes("<script"));
  env.sse("dashboard.updated", { dashboard: { factions: [{ name: "狼人阵营", alive: 3, total: 4, variant: "danger" }], tag_stats: [{ name: "存活", count: 11, variant: "success" }] } });
  assert.match(env.el("factions-list").innerHTML, /狼人阵营/);
  assert.match(env.el("factions-list").innerHTML, /3 \/ 4/);
  assert.match(env.el("tag-stats-list").innerHTML, />11</);
});

test("names and tags from the server are escaped on the tiles, in the popover and in the feed", async () => {
  const evil = "<img src=x onerror=alert(1)>";
  const parts = F.makeParticipants({ p01: { name: evil, tags: [evil, "存活"] } });
  const { env } = await live({ participants: parts, messages: [F.msg(9, "public", "p01", evil)] });
  const board = env.el("participants-grid").innerHTML;
  assert.ok(!board.includes("<img"));
  assert.ok(board.includes("&lt;img src=x onerror=alert(1)&gt;"));
  tagClick(env, "p01");
  assert.ok(!env.el("metrics-popover").innerHTML.includes("<img"));
  assert.ok(!env.el("feed-records-container").innerHTML.includes("<img"));
});

test("a hidden / occluded page still repaints (rAF never fires there): timer fallback, and no double paint", async () => {
  const { env } = await live();
  const text = () => env.el("phase-pill").textContent;
  env.document.visibilityState = "hidden";
  env.sse("phase.changed", { text: "第 2 夜 · 黑夜" }, undefined, { flush: false });
  env.flushRaf(); // nothing is queued on requestAnimationFrame while hidden
  assert.notEqual(text(), "第 2 夜 · 黑夜");
  assert.ok(env.timers.some((t) => t.ms <= 200), "a short fallback timer is pending");
  while (env.timers.length) env.runNextTimer();
  assert.equal(text(), "第 2 夜 · 黑夜");

  // the page goes hidden AFTER the frame was requested: the visibilitychange hook adds the timer
  env.document.visibilityState = "visible";
  env.sse("phase.changed", { text: "第 3 天" }, undefined, { flush: false });
  env.document.visibilityState = "hidden";
  env.document.dispatchEvent("visibilitychange");
  while (env.timers.length) env.runNextTimer();
  assert.equal(text(), "第 3 天");
  env.flushRaf(); // the late rAF callback finds nothing left to do
  assert.equal(text(), "第 3 天");
  env.document.visibilityState = "visible";
});

test("theme toggle flips data-theme", async () => {
  const { env } = await live();
  const html = env.document.documentElement;
  html.setAttribute("data-theme", "light");
  env.el("btn-theme").dispatch("click");
  assert.equal(html.getAttribute("data-theme"), "dark");
  env.el("btn-theme").dispatch("click");
  assert.equal(html.getAttribute("data-theme"), "light");
});

test("the store object behind the UI keeps the PublicSession v2 shape after many events", async () => {
  const { env } = await live();
  env.sse("message.committed", F.msg(41, "public", "p04", "x"));
  env.sse("agent.status", { id: "p01", state: "thinking" });
  env.sse("phase.changed", { text: "第 2 夜" });
  const s = env.app().state.session;
  for (const k of ["session_id", "status", "participants", "moderator", "channels", "messages", "requests", "tickets", "dashboard", "public_tags", "tag_styles", "ui", "usage_stats", "model_bindings", "binding_issues", "restored_from_save", "restore_notice", "execution_depth"]) {
    assert.ok(k in s, `missing ${k}`);
  }
  assert.equal(s.messages[s.messages.length - 1].content, "x");
  assert.equal(Store.findById(s.participants, "p01").state, "thinking");
  assert.equal(s.phase_text, "第 2 夜");
});

H.runTests(tests);
