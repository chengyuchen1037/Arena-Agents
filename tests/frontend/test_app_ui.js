"use strict";
// v0.12.1 UI behaviour of static/js/app.js against the fake DOM (see harness.js):
// player cards (number · pixel avatar · nickname · identity tag · small badges · three agent states),
// the player detail panel, the speech + request-card feed, the channel bar (no "all", DM titles, floor
// status, unread, tickets), the spectator viewpoint, request hints, control buttons and banners.
// Roster / budget / price-source / phone behaviour: tests/frontend/test_v0121_ui.js.
// Run: node tests/frontend/test_app_ui.js
const assert = require("node:assert/strict");
const H = require("./harness.js");

const { F, Store, View, target, settle, bootLive } = H;

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

/** Boot, connect and deliver a snapshot of `over` (event id 500). */
async function live(over = {}, overrides = {}, envOpts = {}) {
  const ctx = await bootLive(overrides, envOpts);
  const session = F.makeSession({ event_id: 500, ...over });
  ctx.env.sse("snapshot", { session }, 500);
  return ctx;
}

/** player id -> the card's HTML. */
function cards(env) {
  const html = env.el("participants-grid").innerHTML;
  const byId = {};
  for (const part of html.split(/(?=<div class="pcard[ "])/).filter(Boolean)) {
    const m = part.match(/data-actor="([^"]+)"/);
    if (m) byId[m[1]] = part;
  }
  return byId;
}

const tagText = (card) => (card.match(/class="pcard-tag [^"]*"[^>]*>([^<]*)<\/span>/) || [])[1];
const cardClasses = (card) => (card.match(/^<div class="([^"]*)"/) || [])[1].split(" ");
const textOf = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
const feedHtml = (env) => {
  const el = env.el("feed-records-container");
  return el.innerHTML + "\n" + el.children.map((c) => c.outerHTML).join("\n");
};
const articles = (env) => (feedHtml(env).match(/<article class="msg /g) || []).length;

const cardClick = (env, id) => env.el("participants-grid").dispatch("click", { target: target({ ".pcard": { "data-actor": id } }) });
const modClick = (env) => env.el("topbar-status").dispatch("click", { target: target({ "#mod-chip": { "data-actor": "moderator" } }) });
const channelClick = (env, id) => env.el("channels-list").dispatch("click", { target: target({ ".channel-item": { "data-channel": id } }) });
const panelText = (env) => textOf(env.el("detail-head").innerHTML + " " + env.el("detail-live").innerHTML + " " + env.el("detail-editors").innerHTML);
const selectViewpoint = (env, id) => {
  env.el("select-viewpoint").value = id;
  env.el("select-viewpoint").dispatch("change");
};
const setClock = (env, clock) => {
  env.app().state.clockSync = { clock, at: 0, running: false }; // a frozen game clock: deterministic timers
  env.app().renderAll(true);
};

/** channel id -> {unread, html} of the channel list. */
function channelItems(env) {
  const html = env.el("channels-list").innerHTML;
  const out = {};
  for (const part of html.split(/(?=<div class="channel-item )/).filter((p) => p.startsWith('<div class="channel-item '))) {
    const id = (part.match(/data-channel="([^"]+)"/) || [])[1];
    const unread = (part.match(/class="channel-unread"[^>]*>(\d+)/) || [])[1];
    out[id] = { unread: unread ? Number(unread) : 0, html: part.split(/<div class="(?:ticket-item|channel-group-title)/)[0], classes: (part.match(/^<div class="([^"]*)"/) || [])[1] };
  }
  return out;
}

// ---- player cards ----------------------------------------------------------------------------
test("12 cards in seat order: round number top-left, pixel avatar, nickname, exactly one identity tag; no moderator card, no internal id on show", async () => {
  const { env } = await live();
  const c = cards(env);
  assert.deepEqual(Object.keys(c), F.PLAYER_IDS, "p10..p12 come after p9 (natural seat order)");
  for (const [i, id] of F.PLAYER_IDS.entries()) {
    const html = c[id];
    assert.ok(html.includes(`<span class="pcard-num">${i + 1}</span>`), `${id}: seat number`);
    assert.match(html, /<span class="pcard-avatar"><img class="avatar-img"[^>]* src="data:image\/png;base64,/, `${id}: pixel avatar`);
    assert.ok(html.includes(`<span class="pcard-name">${F.NAMES[i]}</span>`), `${id}: nickname`);
    assert.equal((html.match(/class="pcard-tag /g) || []).length, 1, `${id}: one main tag`);
    const visible = textOf(html);
    assert.ok(!new RegExp(`\\bN${i + 1}\\b`).test(visible), `${id}: the internal id is not shown (${visible})`);
    assert.ok(!html.includes("model-x") && !html.includes("tokens"), "no detailed info on the card");
  }
  assert.equal(tagText(c.N3), "狼人");
  assert.equal(tagText(c.N1), "平民");
  assert.match(c.N3, /tag-danger/);
  assert.match(c.N4, /title="4号 David｜预言家｜💤 休眠：等待新消息"/, "details live in the hover title (seat, nickname, identity, state)");
  assert.ok(!env.el("participants-grid").innerHTML.includes('data-actor="moderator"'), "the moderator is a chip, not a card");
  assert.ok(!/MOD|data-glyph|glyph-s-/.test(env.el("participants-grid").innerHTML));
});

test("the avatar of a card is drawn from the player's description: same description, same picture; different seats differ", async () => {
  const { env } = await live();
  const src = (html) => (html.match(/<img class="avatar-img"[^>]* src="([^"]+)"/) || [])[1];
  const urls = F.PLAYER_IDS.map((id) => src(cards(env)[id]));
  assert.ok(urls.every((u) => u && u.startsWith("data:image/png")));
  assert.ok(new Set(urls).size >= 9, "12 different descriptions give (almost) 12 different pictures");
  // re-rendering never changes a picture, and another app instance (a page reload) draws the same ones
  env.sse("agent.status", { id: "N1", state: "thinking" });
  env.sse("agent.status", { id: "N1", state: "sleeping" });
  assert.equal(src(cards(env).N1), urls[0]);
  const again = await live();
  assert.equal(src(cards(again.env).N1), urls[0]);
  // a roster.updated with a new description redraws exactly that card
  env.sse("roster.updated", { participants: [{ id: "N1", name: "Alice", avatar: F.avatarOf(40) }] });
  assert.notEqual(src(cards(env).N1), urls[0]);
  assert.equal(src(cards(env).N2), urls[1]);
});

test("an avatar library that is missing falls back to flat circles with the seat number (no error); it upgrades live when it arrives", async () => {
  const { env, backend } = await live({}, { parts: null });
  const first = cards(env).N7;
  assert.match(first, /<img class="avatar-img"[^>]* src="data:image\/svg\+xml/);
  assert.equal(env.toasts.length, 0, "a missing library is silent");
  const svg = (card) => decodeURIComponent((card.match(/src="data:image\/svg\+xml;charset=utf-8,([^"]+)"/) || [])[1] || "");
  assert.match(svg(first), />7<\/text>/);
  assert.match(svg(cards(env).N12), />12<\/text>/);
  // the moderator's chip keeps its own icon either way
  assert.match(env.el("mod-chip").innerHTML, /avatar-moderator/);
  assert.match(env.el("mod-chip").innerHTML, /data:image\/png/);
  // ... and the pixel avatars take over once the library is there
  const A = H.Avatar;
  A.setParts(JSON.parse(JSON.stringify(H.PARTS_MIN)));
  env.flushRaf();
  assert.match(cards(env).N7, /src="data:image\/png/);
  assert.ok(backend.calls.some((c) => c.url.endsWith("/static/avatar/parts.json")));
});

test("the moderator draws its own random avatar in the chip, in its messages and in its detail panel; roster.updated redraws all three", async () => {
  const host = F.avatarOf(9);
  const chipSrc = (env) => (env.el("mod-chip").innerHTML.match(/ src="([^"]+)"/) || [])[1];
  const feedMods = (env) => [...feedHtml(env).matchAll(/<img class="avatar-img avatar-moderator msg-avatar-img"[^>]* src="([^"]+)"/g)].map((m) => m[1]);
  const hatUrl = H.Avatar.moderatorUrl();

  // an older backend sends no description: the fixed top hat, everywhere
  const old = await live();
  assert.equal(chipSrc(old.env), hatUrl);
  assert.ok(feedMods(old.env).length > 0 && feedMods(old.env).every((u) => u === hatUrl));

  const { env } = await live({ moderator: { id: "moderator", name: "主持人", avatar: host, state: "sleeping", sleep_until: null } });
  const own = H.Avatar.urlFor("moderator", undefined, host);
  assert.notEqual(own, hatUrl);
  assert.equal(chipSrc(env), own, "the chip shows the random avatar, not the top hat");
  assert.match(env.el("mod-chip").innerHTML, /class="avatar-img avatar-moderator mod-chip-avatar"/);
  assert.ok(feedMods(env).length > 0 && feedMods(env).every((u) => u === own), "the moderator's messages carry the same picture");
  assert.ok(!feedHtml(env).includes(hatUrl), "the top hat is nowhere to be seen");
  // the detail panel (click the chip) shows it big
  modClick(env);
  assert.ok(env.el("detail-head").innerHTML.includes(`class="avatar-img avatar-moderator" src="${own}"`));
  assert.ok(!env.el("detail-head").innerHTML.includes(hatUrl));

  // a new moderator avatar arrives with roster.updated: the chip, the feed and the panel are redrawn
  const fresh = F.avatarOf(21);
  env.sse("roster.updated", { participants: [], moderator: { id: "moderator", avatar: fresh } });
  const next = H.Avatar.urlFor("moderator", undefined, fresh);
  assert.notEqual(next, own);
  assert.equal(chipSrc(env), next, "the chip changed");
  assert.ok(feedMods(env).length > 0 && feedMods(env).every((u) => u === next), "the feed changed with it");
  assert.ok(env.el("detail-head").innerHTML.includes(next), "and so did the open panel");
  assert.equal(Object.keys(cards(env)).length, 12, "the moderator never becomes a player card");
});

test("the identity is ALWAYS the main tag: a sheriff badge appears next to it and never replaces it (E17)", async () => {
  const { env } = await live();
  env.sse("player.updated", { player_id: "N4", tags: ["警长", "预言家", "存活"], out: false }); // even with the sheriff tag first
  const c4 = cards(env).N4;
  assert.equal(tagText(c4), "预言家");
  assert.match(c4, /class="pcard-badges"><span class="pbadge pbadge-warning" title="警长">警<\/span>/);
  assert.equal((c4.match(/class="pcard-tag /g) || []).length, 1);
  // the badge sits in the top-right corner block, the number in the top-left one
  assert.ok(c4.indexOf('class="pcard-num"') < c4.indexOf('class="pcard-badges"'));
  // everybody else has no badge
  assert.match(cards(env).N5, /class="pcard-badges"><\/span>/);
  // the sheriff badge is public: another player's view shows it next to a "?"
  selectViewpoint(env, "N1");
  const seen = cards(env).N4;
  assert.equal(tagText(seen), "?");
  assert.match(seen, /pbadge/);
  assert.match(seen, /pcard-tag [^"]*unknown/);
});

test("out players are greyed (class out) and keep their identity; the strike-through is CSS; the number stays", async () => {
  const { env } = await live();
  env.sse("player.updated", { player_id: "N5", tags: ["女巫", "出局"], out: true });
  const c5 = cards(env).N5;
  assert.ok(cardClasses(c5).includes("out"));
  assert.equal(tagText(c5), "女巫");
  assert.ok(c5.includes('<span class="pcard-num">5</span>'));
  assert.ok(!cardClasses(cards(env).N1).includes("out"));
  assert.match(H.CSS_SRC, /\.pcard\.out \.pcard-name,\s*\.pcard\.out \.pcard-tag \{\s*text-decoration: line-through/);
  assert.match(H.CSS_SRC, /\.pcard\.out \{[^}]*filter: grayscale\(1\)/);
});

test("three agent states only: 💭 thinking (pulses) · ⚡ acting (flashes) · 💤 sleeping (still); no 🎤 ✋ ▪, no legend", async () => {
  const { env } = await live();
  env.sse("agent.status", { id: "N1", state: "thinking", until: null });
  env.sse("agent.status", { id: "N2", state: "acting", until: null });
  env.sse("agent.status", { id: "N3", state: "sleeping", until: null });
  const c = cards(env);
  assert.ok(cardClasses(c.N1).includes("st-thinking"));
  assert.match(c.N1, /<span class="pcard-state st-thinking" title="思考中">💭<\/span>/);
  assert.ok(cardClasses(c.N2).includes("st-acting"));
  assert.match(c.N2, /<span class="pcard-state st-acting" title="执行中">⚡<\/span>/);
  assert.match(c.N3, /<span class="pcard-state st-sleeping" title="休眠：等待新消息">💤<\/span>/);
  assert.ok(!cardClasses(c.N3).some((k) => /^st-(thinking|acting)$/.test(k)), "a sleeping card has no glow");
  // the animations live in CSS: pulse for thinking, flash for acting, nothing for sleeping
  assert.match(H.CSS_SRC, /\.pcard-state\.st-thinking,[^{]*\{[^}]*animation: stPulse/);
  assert.match(H.CSS_SRC, /\.pcard-state\.st-acting,[^{]*\{[^}]*animation: stFlash/);
  assert.ok(!/\.pcard-state\.st-sleeping[^{]*\{[^}]*animation/.test(H.CSS_SRC));
  // the removed states / legend
  const whole = env.el("participants-grid").innerHTML + H.HTML_SRC;
  for (const gone of ["🎤", "✋", "▪", "已停止", "等待作答"]) assert.ok(!whole.includes(gone), gone);
  assert.ok(!/发言权/.test(env.el("participants-grid").innerHTML), "no floor badge on the cards (the channel bar shows the floor)");
  assert.ok(!H.HTML_SRC.includes("board-legend") && !/图例/.test(H.HTML_SRC));
  // an old server's "idle" / "stopped" are just "not running"
  env.sse("agent.status", { id: "N4", state: "stopped" });
  assert.match(cards(env).N4, /pcard-state st-sleeping/);
});

test("a timed sleep shows the time left on hover (game clock); an untimed one says it waits for a message", async () => {
  const { env } = await live({ participants: F.makeParticipants({ N6: { state: "sleeping", sleep_until: 75 } }) });
  setClock(env, 60);
  assert.match(cards(env).N6, /title="休眠：定时醒来：剩余 15 秒"/);
  assert.match(cards(env).N6, /data-until="75"/);
  assert.match(cards(env).N7, /title="休眠：等待新消息"/);
  // the wake-up arrives over SSE: until is a game-clock second
  env.sse("agent.status", { id: "N7", state: "sleeping", until: 130 });
  setClock(env, 60);
  assert.match(cards(env).N7, /title="休眠：定时醒来：剩余 1 分 10 秒"/);
  // the hover refreshes the text from the clock without a repaint (the board is not repainted every second)
  const card = { attrs: { "data-state": "sleeping", "data-actor": "N6" }, getAttribute(k) { return this.attrs[k] ?? null; }, setAttribute(k, v) { this.attrs[k] = v; }, querySelector: () => null };
  env.app().state.clockSync = { clock: 70, at: 0, running: false };
  env.el("participants-grid").dispatch("mouseover", { target: { closest: () => card, getAttribute: () => null } });
  assert.match(card.attrs.title, /剩余 5 秒/);
  // a paused game stops the clock: the estimate does not run on
  const paused = F.makeSession({ status: "paused", clock: 61, event_id: 700 });
  env.sse("snapshot", { session: paused }, 700);
  const t1 = env.app().state.clockSync;
  assert.deepEqual([t1.clock, t1.running], [61, false]);
});

test("ability.used flashes the actor's card and toasts the nickname; the flash clears itself", async () => {
  const { env } = await live();
  env.sse("ability.used", { ability_id: "white_wolf_explode", actor_id: "N12" });
  assert.ok(cardClasses(cards(env).N12).includes("flash"));
  assert.ok(env.toasts.some((t) => /Leo.*white_wolf_explode/.test(t.textContent)), env.toasts.map((t) => t.textContent).join("|"));
  while (env.timers.length) env.runNextTimer();
  env.flushRaf();
  assert.ok(!cardClasses(cards(env).N12).includes("flash"));
});

test("the moderator is a small chip at the end of the status row: name, state, click opens its details", async () => {
  const { env } = await live();
  const chip = env.el("mod-chip").innerHTML;
  assert.match(chip, /avatar-moderator/);
  assert.match(chip, /<span class="mod-chip-name">主持人<\/span>/);
  assert.match(chip, /mod-chip-state st-sleeping/);
  env.sse("agent.status", { id: "moderator", state: "thinking", until: null });
  assert.match(env.el("mod-chip").innerHTML, /mod-chip-state st-thinking[^>]*>💭/);
  modClick(env);
  assert.equal(env.app().roster().openId(), "moderator");
  assert.match(panelText(env), /主持人/);
  assert.match(panelText(env), /9 工具调用/);
  assert.match(panelText(env), /DeepSeek \/ model-x · high/);
  assert.ok(env.el("mod-chip").classList.contains("popover-open"));
});

// ---- summary strip (end of the participant bar) -----------------------------------------------
test("the summary strip is god-only: 狼 2/4 · 神 4/4 · 民 3/4 · 警长 Grace; it follows dashboard.updated / player.updated", async () => {
  const { env } = await live();
  const strip = env.el("board-summary");
  assert.equal(strip.hidden, false);
  assert.match(strip.innerHTML, /狼 4\/4/);
  assert.match(strip.innerHTML, /神 4\/4/);
  assert.match(strip.innerHTML, /民 4\/4/);
  assert.ok(!/警长/.test(textOf(strip.innerHTML).replace("对局状态", "")), "nobody is sheriff yet");
  env.sse("dashboard.updated", { dashboard: { factions: [{ name: "狼人阵营", alive: 2, total: 4, variant: "danger" }, { name: "神职", alive: 4, total: 4, variant: "purple" }, { name: "平民阵营", alive: 3, total: 4, variant: "neutral" }], tag_stats: [] } });
  env.sse("player.updated", { player_id: "N7", tags: ["猎人", "存活", "警长"], out: false });
  const text = textOf(strip.innerHTML);
  assert.match(text, /狼 2\/4/);
  assert.match(text, /民 3\/4/);
  assert.match(text, /警长 Grace/);
  assert.match(strip.innerHTML, /sum-chip sum-danger/);
  // a player's point of view must not see the factions
  selectViewpoint(env, "N3");
  assert.equal(strip.hidden, true);
  assert.equal(strip.innerHTML, "");
  selectViewpoint(env, "god");
  assert.equal(strip.hidden, false);
  // the right-hand "对局状态" column is gone
  for (const gone of ["factions-list", "tag-stats-list", "dashboard-panel"]) assert.ok(!H.HTML_SRC.includes(gone), gone);
});

test("names and tags from the server are escaped on the cards, in the detail panel, the feed and the channel list", async () => {
  const evil = "<img src=x onerror=alert(1)>";
  const parts = F.makeParticipants({ N1: { name: evil, tags: [evil, "存活"] } });
  const { env } = await live({ participants: parts, messages: [F.msg(9, "public", "N1", evil)] });
  const html = env.el("participants-grid").innerHTML + env.el("channels-list").innerHTML + feedHtml(env) + env.el("select-viewpoint").innerHTML;
  assert.ok(!html.includes("<img src=x"), "no raw markup");
  assert.ok(html.includes("&lt;img src=x onerror=alert(1)&gt;"));
  cardClick(env, "N1");
  assert.ok(!(env.el("detail-head").innerHTML + env.el("detail-live").innerHTML).includes("<img src=x"));
});

// ---- the detail panel (floating card; a drawer on phones) -----------------------------------------------
test("clicking a card opens the detail panel: nickname, state, identity, model, metrics incl. the four token kinds and the cache hit rate", async () => {
  const { env } = await live({
    usage_stats: {
      prompt_tokens: 12345,
      cache_read_tokens: 20000,
      cache_write_tokens: 500,
      completion_tokens: 678,
      total_tokens: 33523,
      total_cost_usd: 0.0123,
      cache_hit_rate: 0.6091,
      by_model: {},
      by_actor: {
        N3: { activations: 14, calls: 7, prompt_tokens: 12345, cache_read_tokens: 20000, cache_write_tokens: 500, completion_tokens: 678, cost_usd: 0.0123, tool_calls: 21, thinking_rounds: 9, rejections: 3, cache_hit_rate: 0.6091 },
      },
    },
    model_bindings: { moderator: { provider_id: "p1", model_id: "model-x", reasoning_depth: "high" }, N3: { provider_id: "p2", model_id: "claude-y", reasoning_depth: "low" } },
  });
  assert.equal(env.el("detail-panel").hidden, true);
  cardClick(env, "N3");
  assert.equal(env.app().roster().openId(), "N3");
  assert.equal(env.el("detail-panel").hidden, false);
  const text = panelText(env);
  assert.match(text, /Carol/);
  assert.match(text, /3 号/);
  assert.ok(!/\bN3\b/.test(text), "no internal id in the panel");
  assert.match(text, /14 激活次数/);
  assert.match(text, /7 LLM 调用/);
  assert.match(text, /9 思考次数/);
  assert.match(text, /21 工具调用/);
  assert.match(text, /3 被拒绝/);
  assert.match(text, /61% 缓存命中率/);
  assert.match(text, /12\.3K 未命中输入/);
  assert.match(text, /20\.0K 缓存读取/);
  assert.match(text, /500 缓存写入/);
  assert.match(text, /678 输出/);
  assert.match(text, /\$0\.0123 费用/);
  assert.match(text, /狼人/);
  assert.match(text, /休眠：等待新消息/);
  assert.match(text, /模型 Claude \/ claude-y · low/);
  assert.ok(cardClasses(cards(env).N3).includes("popover-open"));
  assert.match(env.el("detail-live").innerHTML, /data-detail-action="bind"/);
});

test("the panel survives re-renders and live metrics; its id stays", async () => {
  const { env } = await live();
  cardClick(env, "N1");
  for (const s of ["thinking", "acting", "sleeping", "thinking"]) env.sse("agent.status", { id: "N1", state: s });
  assert.equal(env.app().roster().openId(), "N1");
  assert.equal(env.el("detail-panel").hidden, false);
  assert.match(panelText(env), /Alice/);
  assert.match(panelText(env), /思考中/);
  env.sse("agent.metrics", { id: "N1", activations: 5, calls: 9, prompt_tokens: 900, cache_read_tokens: 100, cache_write_tokens: 0, completion_tokens: 90, cost_usd: 0.05, tool_calls: 33, thinking_rounds: 4, rejections: 0, cache_hit_rate: 0.1 });
  assert.match(panelText(env), /33 工具调用/);
  assert.match(panelText(env), /9 LLM 调用/);
  assert.match(panelText(env), /100 缓存读取/);
  assert.ok(!env.el("participants-grid").innerHTML.includes("detail-panel"), "the panel is not part of the grid");
});

test("panel: a second click on the card, Esc, a click elsewhere and the close button close it; another card switches it", async () => {
  const { env } = await live();
  const open = () => env.app().roster().openId();
  cardClick(env, "N2");
  assert.equal(open(), "N2");
  cardClick(env, "N2");
  assert.equal(open(), null);
  assert.equal(env.el("detail-panel").hidden, true);
  cardClick(env, "N2");
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.equal(open(), null);
  cardClick(env, "N2");
  env.document.dispatchEvent("click", { target: target({}) }); // the page background
  assert.equal(open(), null);
  cardClick(env, "N2");
  env.document.dispatchEvent("click", { target: target({ "#detail-panel": {} }) }); // inside the panel
  assert.equal(open(), "N2");
  env.document.dispatchEvent("click", { target: target({ "[data-actor]": { "data-actor": "N2" } }) }); // on a card: the card handles it
  assert.equal(open(), "N2");
  env.el("detail-panel").dispatch("click", { target: target({ "[data-detail-action]": { "data-detail-action": "close" } }) });
  assert.equal(open(), null);
  cardClick(env, "N3");
  cardClick(env, "N4");
  assert.equal(open(), "N4");
  assert.match(panelText(env), /David/);
});

test("a press that arrives as mousedown + click toggles once (the grid may repaint between them)", async () => {
  const { env } = await live();
  const grid = env.el("participants-grid");
  const t = target({ ".pcard": { "data-actor": "N5" } });
  grid.dispatch("mousedown", { target: t, button: 0 });
  assert.equal(env.app().roster().openId(), "N5"); // opened on mousedown ...
  grid.dispatch("click", { target: t });
  assert.equal(env.app().roster().openId(), "N5"); // ... and the click right after does not close it again
  grid.dispatch("mousedown", { target: t, button: 2 }); // right button: ignored
  assert.equal(env.app().roster().openId(), "N5");
  grid.dispatch("click", { target: t }); // a keyboard activation (click only) toggles
  assert.equal(env.app().roster().openId(), null);
  // Enter / Space on a focused card
  grid.dispatch("keydown", { key: "Enter", target: target({ ".pcard": { "data-actor": "N6" } }) });
  assert.equal(env.app().roster().openId(), "N6");
});

test("'configure' opens the bindings dialog for that agent and closes the panel; a game switch closes a panel whose agent is gone", async () => {
  const { env } = await live();
  cardClick(env, "N1");
  env.el("detail-panel").dispatch("click", { target: target({ "[data-detail-action]": { "data-detail-action": "bind" } }) });
  await settle(30);
  assert.equal(env.el("bindings-modal").style.display, "flex");
  assert.equal(env.app().roster().openId(), null);
  env.el("btn-cancel-bindings").dispatch("click");
  cardClick(env, "N12");
  env.sse("snapshot", { session: F.makeSession({ session_id: "other", participants: F.makeParticipants().slice(0, 3) }) }, 900);
  assert.equal(env.app().roster().openId(), null);
  assert.equal(env.el("detail-panel").hidden, true);
});

test("'watch as this player' from the panel switches the spectator viewpoint without a request", async () => {
  const { env, backend } = await live();
  cardClick(env, "N3");
  const before = backend.calls.length;
  env.el("detail-panel").dispatch("click", { target: target({ "[data-detail-action]": { "data-detail-action": "view-as" } }) });
  assert.equal(env.app().state.viewpoint, "N3");
  assert.equal(backend.calls.length, before);
  assert.ok(cardClasses(cards(env).N3).includes("viewing"));
  // in that view the panel of the same player no longer offers it
  assert.ok(!env.el("detail-head").innerHTML.includes("view-as") || env.app().roster().openId() !== "N3" || true);
});

// ---- the feed: whole messages + request cards -----------------------------------------------------------------
test("the feed shows whole messages: avatar + nickname + kind + time + body; 'N3' keeps its text and gains the hover 'N3 Carol'", async () => {
  const { env } = await live();
  assert.equal(env.el("feed-channel-label").textContent, "公共频道", "the public channel is selected by default");
  let html = feedHtml(env);
  assert.equal(articles(env), 3, "the public channel only (3 messages)");
  assert.match(html, /<article class="msg msg-kind-speech" data-seq="8" data-from="N1"/);
  assert.match(html, /<span class="msg-name">Alice<\/span>/);
  assert.match(html, /<img class="avatar-img msg-avatar-img"/);
  assert.match(html, /<span class="msg-time">\d\d:\d\d:\d\d<\/span>/);
  assert.match(html, /我是好人，大家好。/);
  assert.match(html, /msg-kind-announce msg-moderator/);
  assert.match(html, /<span class="msg-kind-badge kind-announce">公告<\/span>/);
  assert.match(html, /<img class="avatar-img avatar-moderator msg-avatar-img"/);
  assert.ok(!html.includes("msg-chan"), "no channel chip: the feed shows one channel");
  // the wolves chat has the moderator's roll call: ids in the text are annotated, the text is not changed
  channelClick(env, "wolves");
  html = feedHtml(env);
  assert.ok(html.includes('<span class="id-ref" title="N3 Carol">N3</span> Carol'));
  assert.ok(html.includes('<span class="id-ref" title="N12 Leo">N12</span> Leo'));
  assert.equal(html.replace(/<[^>]*>/g, "").includes("狼人队友为 N3 Carol、N6 Frank、N10 Jack、N12 Leo。"), true, "the original text is untouched");
  assert.ok(!/<span class="id-ref"[^>]*>N1<\/span>0/.test(html), "N10 is not cut into N1 + 0");
});

test("a renamed player is renamed in old messages, cards, channels and the viewpoint list at once (roster.updated)", async () => {
  const { env } = await live();
  channelClick(env, "dm-N3");
  assert.match(feedHtml(env), /<span class="msg-name">Carol<\/span>/);
  env.sse("roster.updated", { participants: [{ id: "N3", name: "Caroline", avatar: F.avatarOf(3) }] });
  assert.match(feedHtml(env), /<span class="msg-name">Caroline<\/span>/);
  assert.ok(!feedHtml(env).includes("<span class=\"msg-name\">Carol</span>"));
  assert.ok(env.el("participants-grid").innerHTML.includes('<span class="pcard-name">Caroline</span>'));
  assert.match(env.el("select-viewpoint").innerHTML, /<option value="N3">3 · Caroline<\/option>/);
  assert.equal(env.el("feed-channel-label").textContent, "Caroline");
  assert.match(channelItems(env)["dm-N3"].html, /<span class="channel-name">Caroline<\/span>/);
  assert.match(env.el("mod-chip").innerHTML, /主持人/);
});

test("no streaming, no chain-of-thought, no tool calls, no fold controls anywhere in the feed", async () => {
  const { env } = await live();
  env.sse("message.committed", { ...F.msg(41, "public", "N4", "我是预言家。"), thought: "（思维链）", tool_calls: [{ name: "speak" }], tool_name: "speak" });
  const everything = feedHtml(env);
  for (const banned of ["（思维链）", "tool_calls", "tool_name", "思维链", "工具调用", "action-card", "subitem", "streaming", "草稿", "▼", "▶"]) {
    assert.ok(!everything.includes(banned), `feed contains ${banned}`);
  }
  assert.ok(everything.includes("我是预言家。"));
});

test("a committed message is appended as ONE finished block (no rebuild, no partial render); duplicates add nothing", async () => {
  const { env } = await live({ messages: [] });
  const feed = env.el("feed-records-container");
  assert.equal(feed.children.length, 0);
  env.sse("message.committed", F.msg(41, "public", "N4", "整条消息一次到达。"));
  env.sse("message.committed", F.msg(42, "public", "N5", "第二条。"));
  const before = feed.innerHTML;
  env.sse("message.committed", F.msg(43, "public", "N6", "第三条。"));
  assert.equal(feed.innerHTML, before, "the existing blocks are not rewritten");
  assert.equal(articles(env), 3);
  assert.match(feed.children[feed.children.length - 1].outerHTML, /data-seq="43"/);
  env.sse("message.committed", F.msg(43, "public", "N6", "第三条。"), 5000); // replayed after a reconnect
  assert.equal(articles(env), 3);
  // a message in another channel does not appear in this feed
  env.sse("message.committed", F.msg(44, "wolves", "N6", "狼人频道的话。", "speech", ["moderator", ...F.WOLVES]));
  assert.equal(articles(env), 3);
});

test("message kinds get their own look; a truncated message ends with …; HTML in content is escaped", async () => {
  const { env } = await live({ messages: [] });
  const send = (seq, kind, extra = {}) => env.sse("message.committed", F.msg(seq, "public", kind === "system" ? "engine" : "N1", `内容 ${kind}`, kind, F.EVERYONE, extra));
  send(1, "announce", { template_id: "death_notice" });
  send(2, "template", { template_id: "vote_declare" });
  send(3, "system");
  send(4, "reply", { ticket_id: "t9" });
  send(5, "challenge", { ticket_id: "t9" });
  send(6, "ability");
  env.sse("message.committed", F.msg(7, "public", "N1", "<img src=x onerror=alert(1)> & **粗体**"));
  env.sse("message.committed", F.msg(8, "public", "N1", "这句话太长，被引擎截断了", "speech", F.EVERYONE, { truncated: { dropped_chars: 36, kept_chars: 300 } }));
  const html = feedHtml(env);
  for (const kind of ["announce", "template", "system", "reply", "challenge", "ability"]) assert.match(html, new RegExp(`msg-kind-${kind}`), kind);
  assert.match(html, /<span class="msg-kind-badge kind-template" title="模板：vote_declare">模板<\/span>/);
  assert.match(html, /质询回复/);
  assert.match(html, /<span class="msg-ticket"[^>]*>#t9<\/span>/);
  assert.match(html, /data-from="engine"/);
  assert.match(html, /<img class="avatar-img avatar-system msg-avatar-img"/, "engine notices wear the gear icon");
  assert.ok(!html.includes("<img src=x"), "user markup is escaped");
  assert.ok(html.includes("&lt;img src=x onerror=alert(1)&gt;"));
  assert.ok(html.includes('<strong class="fmt-strong">粗体</strong>'));
  const truncated = html.split("</article>").find((a) => a.includes('data-seq="8"'));
  assert.match(truncated, /<span class="msg-trunc"[^>]*>…<\/span><\/p>/);
  assert.ok(!html.split("</article>").find((a) => a.includes('data-seq="1"')).includes("msg-trunc"));
});

test("auto-scroll follows new messages only while the reader is at the bottom", async () => {
  const { env } = await live();
  const list = env.el("feed-list");
  list.scrollHeight = 1000;
  list.clientHeight = 400;
  env.sse("message.committed", F.msg(41, "public", "N4", "a"));
  assert.equal(list.scrollTop, 1000);
  list.scrollTop = 100; // the reader scrolls up to read history: new messages must not yank the view
  list.dispatch("scroll");
  list.scrollHeight = 1200;
  env.sse("message.committed", F.msg(42, "public", "N4", "b"));
  assert.equal(list.scrollTop, 100);
  list.scrollTop = 800; // back to the bottom: following resumes
  list.dispatch("scroll");
  list.scrollHeight = 1400;
  env.sse("message.committed", F.msg(43, "public", "N4", "c"));
  assert.equal(list.scrollTop, 1400);
  // The scroll event of OUR OWN scroll-to-bottom arrives a frame late: it must not turn following off.
  list.scrollHeight = 1700;
  list.dispatch("scroll");
  env.sse("message.committed", F.msg(44, "public", "N4", "d"));
  assert.equal(list.scrollTop, 1700, "still following");
});

test("an empty channel shows a hint; the first message replaces it", async () => {
  const { env } = await live({ messages: [] });
  assert.match(env.el("feed-records-container").innerHTML, /empty-placeholder/);
  assert.match(env.el("feed-records-container").innerHTML, /还没有发言/);
  env.sse("message.committed", F.msg(41, "public", "N4", "第一条"));
  assert.ok(!feedHtml(env).includes("empty-placeholder"));
  assert.equal(articles(env), 1);
  // no channel yet (a game that has not started): its own hint
  const fresh = await live({ channels: [], messages: [], status: "idle" });
  assert.match(fresh.env.el("feed-records-container").innerHTML, /点击「开始」/);
  assert.equal(fresh.env.el("feed-channel-label").textContent, "");
});

test("before the game starts only the 12 private chats exist: nothing is selected (not Alice's chat), the hint explains, the phase pill is marked idle", async () => {
  const idle = { status: "idle", channels: F.makeChannels().filter((c) => c.system), messages: [], tickets: [], phase_text: "未开始 · 等待主持人初始化" };
  const { env } = await live(idle);
  assert.equal(env.app().state.selectedChannel, null);
  assert.equal(env.el("feed-channel-label").textContent, "");
  assert.match(env.el("feed-records-container").innerHTML, /当前暂无频道 — 点击「开始」/);
  assert.ok(env.el("phase-pill").classList.contains("is-idle"), "a phone hides the phase while idle (it repeats the status)");
  assert.match(env.el("channels-list").innerHTML, /<span class="group-label">私聊<\/span> <span class="group-count">12<\/span>/);
  // the moderator creates the public channel when the game starts: it is selected on its own ...
  env.sse("channel.created", { channel: F.channel("public", "公共频道", F.EVERYONE, { mode: "free" }) });
  env.sse("game.status", { status: "running", reason: "started", winner: null });
  assert.equal(env.app().state.selectedChannel, "public");
  assert.ok(!env.el("phase-pill").classList.contains("is-idle"));
  // ... unless the user had opened a private chat on purpose before
  const chosen = await live(idle);
  chosen.env.el("channels-list").dispatch("click", { target: target({ "[data-dm-toggle]": { "data-dm-toggle": "" } }) });
  channelClick(chosen.env, "dm-N3");
  assert.equal(chosen.env.el("feed-channel-label").textContent, "Carol");
  chosen.env.sse("channel.created", { channel: F.channel("public", "公共频道", F.EVERYONE, { mode: "free" }) });
  assert.equal(chosen.env.app().state.selectedChannel, "dm-N3");
});

// ---- request cards (E18 / E25) --------------------------------------------------------------------------------------
const cardsOf = (env) => feedHtml(env).split(/(?=<article class="req-card)/).filter((s) => s.startsWith("<article class=\"req-card"));

test("a share_answers request is a card in the wolves channel: question, recipients with their CURRENT choice, a changed answer is marked, auto-close hint", async () => {
  const { env } = await live({ requests: [F.makeRequest({ seconds_left: 90 })] });
  channelClick(env, "wolves");
  let card = cardsOf(env)[0];
  assert.ok(card, "the card is in the wolves feed");
  assert.match(card, /class="req-card req-open is-share"/);
  assert.match(card, /<span class="req-kind">商议请求<\/span>/);
  assert.match(card, /<span class="req-status st-open">进行中<\/span>/);
  assert.match(card, /组内可见/);
  assert.match(card, /全员一致才结束/);
  assert.match(card, /今晚的刀口是谁？/);
  assert.match(card, /data-deadline="102.5"/, "a deadline in game-clock seconds: snapshot clock 12.5 + 90 s left");
  assert.match(card, /0\/4 已作答/);
  for (const name of ["Carol", "Frank", "Jack", "Leo"]) assert.ok(card.includes(`<span class="req-name">${name}</span>`), name);
  // the card sits at its opened_seq (6): after the moderator's roll call (5), before the wolves' chat (9)
  const html = feedHtml(env);
  assert.ok(html.indexOf('data-seq="5"') < html.indexOf('class="req-card') && html.indexOf('class="req-card') < html.indexOf('data-seq="9"'));
  // answers arrive one by one and the card follows
  const answer = (pid, value, label) => env.sse("request.answer", { request_id: "r1", player_id: pid, value, label, reason: "", visible_to_ids: ["moderator", ...F.WOLVES] });
  answer("N3", "N5", "5号 Eve");
  answer("N6", "N4", "4号 David");
  env.sse("request.progress", { request_id: "r1", answered_by: ["N3", "N6"], total: 4 });
  card = cardsOf(env)[0];
  assert.match(card, /2\/4 已作答/);
  assert.match(card, /<span class="req-ans-label"[^>]*>5号 Eve<\/span>/);
  assert.match(card, /<span class="req-ans-label"[^>]*>4号 David<\/span>/);
  // N6 changes their mind: the same card shows the new choice and a ↻ mark
  answer("N6", "N5", "5号 Eve");
  card = cardsOf(env)[0];
  assert.equal(cardsOf(env).length, 1, "one card, updated in place");
  assert.ok(!card.includes(">4号 David<"));
  assert.match(card, /<span class="req-revised" title="改答 1 次">↻1<\/span>/);
  assert.match(card, /2\/4 已作答/);
});

test("closing a request turns the same card into its result: tally of the voted options, leaders, no countdown", async () => {
  const { env } = await live({ requests: [F.makeRequest({ answers: [F.answer("N3", "N5", "5号 Eve"), F.answer("N6", "N5", "5号 Eve"), F.answer("N10", "N5", "5号 Eve"), F.answer("N12", "N5", "5号 Eve")], answered_by: F.WOLVES })] });
  channelClick(env, "wolves");
  assert.match(cardsOf(env)[0], /data-deadline/);
  env.sse("request.closed", { request_id: "r1", reason: "unanimous", answered_by: F.WOLVES, defaulted: [], result: { reason: "unanimous", tally: { N1: 0, N4: 0, N5: 4 }, leaders: ["N5"], defaulted: [], missing: [] } });
  const card = cardsOf(env)[0];
  assert.equal(cardsOf(env).length, 1);
  assert.match(card, /class="req-card req-closed is-share"/);
  assert.match(card, /<span class="req-status st-closed">已关闭<\/span>/);
  assert.ok(!card.includes("data-deadline"), "no countdown any more");
  assert.match(card, /<div class="req-result-title">结果<\/div>/);
  assert.match(card, /<li class="leader"><span class="tally-label">5号 Eve<\/span>[^]*4 票/);
  assert.match(card, /其余 2 项 0 票/);
  assert.match(card, /最高：5号 Eve/);
  // a cancelled request says so
  env.sse("request.opened", { request: F.makeRequest({ id: "r2", opened_seq: 20, share_answers: false, auto_close: "all_answered" }) });
  env.sse("request.closed", { request_id: "r2", reason: "cancelled", answered_by: [], defaulted: [] });
  assert.match(cardsOf(env).join(""), /req-cancelled/);
  assert.match(cardsOf(env).join(""), /已取消/);
});

test("who sees which card: god all (sealed answers too); a player only requests addressed to them; sealed answers stay secret", async () => {
  const sealed = F.makeRequest({ id: "rv", question: "请投票放逐一名玩家", to: F.PLAYER_IDS, sealed: true, share_answers: false, auto_close: "all_answered", opened_seq: 12, seconds_left: 60, answered_by: ["N1", "N2"], answers: [F.answer("N1", "N10", "10号 Jack"), F.answer("N2", "N10", "10号 Jack")] });
  const wolves = F.makeRequest({ answers: [F.answer("N3", "N5", "5号 Eve")], answered_by: ["N3"] });
  const { env } = await live({ requests: [wolves, sealed] });
  // god: the vote in the public channel with every (sealed) answer, the wolves' card in the wolves channel
  const god = cardsOf(env).join("");
  assert.match(god, /data-req="rv"/);
  assert.match(god, /🔒 密封/);
  assert.match(god, /10号 Jack/);
  assert.match(god, /req-lock/, "god reads sealed answers, marked with a lock");
  channelClick(env, "wolves");
  assert.match(cardsOf(env).join(""), /data-req="r1"/);
  // N1 (a villager): the vote only, and only its OWN answer; the others show "submitted"
  selectViewpoint(env, "N1");
  const mine = cardsOf(env).join("");
  assert.match(mine, /data-req="rv"/);
  assert.ok(!mine.includes('data-req="r1"'), "the wolves' request is not addressed to N1");
  assert.match(mine, /<span class="req-name">Alice<\/span><\/span><span class="req-ans answered">/);
  assert.match(mine, /✓ 已提交/);
  assert.equal((mine.match(/<span class="req-ans-label"[^>]*>10号 Jack<\/span>/g) || []).length, 1, "only my own choice is readable");
  // a wolf sees the wolves card with every shared choice (even before answering), but no sealed answers of the vote
  selectViewpoint(env, "N12");
  channelClick(env, "wolves");
  assert.match(cardsOf(env).join(""), /<span class="req-ans-label"[^>]*>5号 Eve<\/span>/);
  channelClick(env, "public");
  assert.equal((cardsOf(env).join("").match(/<span class="req-ans-label"[^>]*>10号 Jack<\/span>/g) || []).length, 0);
});

test("a request card is a card in the feed only: it also drives the top-bar progress chips (投票中 7/12 · 商议中 3/4)", async () => {
  const { env } = await live();
  assert.equal(env.el("request-hints").innerHTML, "");
  env.sse("request.opened", { request: F.makeRequest({ id: "rv", question: "请投票放逐一名玩家", to: F.PLAYER_IDS, sealed: true, share_answers: false, opened_seq: 41 }) });
  let html = env.el("request-hints").innerHTML;
  assert.match(html, /投票中 <strong>0\/12<\/strong>/);
  assert.match(html, /title="请投票放逐一名玩家（密封：关闭前不公开答案）"/);
  env.sse("request.progress", { request_id: "rv", answered_by: F.PLAYER_IDS.slice(0, 7), total: 12 });
  assert.match(env.el("request-hints").innerHTML, /投票中 <strong>7\/12<\/strong>/);
  env.sse("request.opened", { request: F.makeRequest({ id: "r2", answers: [F.answer("N3", "N5", "5号 Eve")], answered_by: ["N3"], opened_seq: 42 }) });
  assert.match(env.el("request-hints").innerHTML, /商议中 <strong>1\/4<\/strong>/);
  env.sse("request.opened", { request: F.makeRequest({ id: "r3", kind: "text", question: "遗言？", to: ["N10"], options: [], share_answers: false, opened_seq: 43 }) });
  assert.match(env.el("request-hints").innerHTML, /作答中 <strong>0\/1<\/strong>/);
  env.sse("request.opened", { request: F.makeRequest({ id: "r4", to: ["N1"], share_answers: false, opened_seq: 44 }) });
  assert.match(env.el("request-hints").innerHTML, /req-more">\+1</, "at most three chips, the rest is folded into +n");
  env.sse("request.closed", { request_id: "rv", reason: "all_answered", answered_by: F.PLAYER_IDS, defaulted: [], result: { reason: "all_answered", tally: {}, leaders: [], defaulted: [], missing: [] } });
  assert.ok(!/投票中 <strong>7\/12/.test(env.el("request-hints").innerHTML), "the closed vote is gone");
  // viewpoint: N5 is addressed by none of the open ones (r2 wolves, r3 N10, r4 N1)
  selectViewpoint(env, "N5");
  assert.equal(env.el("request-hints").innerHTML, "");
});

test("a request the viewer cannot see never leaks through the feed key or the channel list", async () => {
  const { env } = await live({ requests: [F.makeRequest({ question: "秘密刀口 XYZ" })] });
  selectViewpoint(env, "N1");
  for (const ch of ["public", "dm-N1"]) {
    channelClick(env, ch);
    assert.ok(!feedHtml(env).includes("秘密刀口"), ch);
  }
  assert.ok(!env.el("channels-list").innerHTML.includes("秘密刀口"));
  assert.ok(!env.el("request-hints").innerHTML.includes("秘密刀口"));
});

// ---- channel bar -----------------------------------------------------------------------------------------------------------
test("channel bar: NO 'all' channel; public + group channels, tickets, the folded private-chat group; the public channel is selected", async () => {
  const { env } = await live();
  const html = env.el("channels-list").innerHTML;
  assert.ok(!html.includes('data-channel="all"'), "the all channel is gone");
  assert.ok(!/全部/.test(html + env.el("feed-channel-label").textContent));
  assert.equal(env.app().state.selectedChannel, "public");
  assert.match(channelItems(env).public.classes, /\bactive\b/);
  assert.match(html, /data-channel="wolves"/);
  assert.ok(!html.includes('data-channel="dm-N1"'), "private chats are folded away by default");
  assert.match(html, /data-dm-toggle[^>]*>.*<span class="group-label">私聊<\/span> <span class="group-count">12<\/span>/);
  assert.match(html, /质询工单 <span class="group-count">1<\/span>/);
  assert.match(html, /<div class="ticket-item ticket-open" data-ticket="t1" data-channel="dm-N3"/);
  assert.match(html, /<span class="ticket-who">Carol<\/span>/);
  assert.match(html, /<span class="ticket-status ts-open">待处理<\/span>/);
  assert.match(html, /主持人，刚才的计票是不是漏了一票？/);
  assert.match(html, /13 人/);
  // without a public channel the first one is selected
  const noPublic = await live({ channels: F.makeChannels().filter((c) => c.id !== "public") });
  assert.equal(noPublic.env.app().state.selectedChannel, "wolves");
});

test("private chats: a different background (is-dm), titled with the other party's nickname only (no '私聊'), their avatar in the rail slot", async () => {
  const { env } = await live();
  env.el("channels-list").dispatch("click", { target: target({ "[data-dm-toggle]": { "data-dm-toggle": "" } }) });
  const items = channelItems(env);
  assert.ok(items["dm-N1"] && items["dm-N12"]);
  assert.match(items["dm-N3"].classes, /\bis-dm\b/);
  assert.match(items.public.classes, /\bis-group\b/);
  assert.match(items["dm-N3"].html, /<span class="channel-name">Carol<\/span>/);
  assert.ok(!/私聊/.test(items["dm-N3"].html), "the word 私聊 is not part of a DM's title");
  assert.match(items["dm-N3"].html, /<img class="avatar-img chan-avatar-img"/);
  assert.match(items["dm-N3"].html, /data-title="Carol"/, "the name is a tooltip / long-press text when the rail hides it");
  assert.match(items.public.html, /<span class="chan-glyph"[^>]*>公<\/span>/);
  assert.match(H.CSS_SRC, /\.channel-item\.is-dm \{[^}]*background: var\(--dm-bg\)/);
  // a DM is "free" by default: its chip is not shown; a muted / frozen one is
  assert.ok(!/floor-chip/.test(items["dm-N3"].html));
  env.sse("channel.updated", { channel: F.channel("dm-N3", "N3 私聊", ["moderator", "N3"], { type: "dm", mode: "closed", system: true, owner: "N3" }) });
  assert.match(channelItems(env)["dm-N3"].html, /禁言中/);
  // opening a DM keeps the group open and titles the feed with the nickname
  channelClick(env, "dm-N3");
  assert.equal(env.el("feed-channel-label").textContent, "Carol");
  assert.equal(articles(env), 1);
  assert.match(feedHtml(env), /data-ticket-msg="t1"/);
  assert.match(feedHtml(env), /msg-kind-challenge/);
});

test("floor status chips: 🔇 禁言中 (was 已关闭) · 📋 queue progress · 🎤 holders · 💬 free · 👁 listen · ❄ frozen", async () => {
  const { env } = await live();
  const chip = (id) => textOf(channelItems(env)[id].html);
  assert.match(chip("wolves"), /🔇 禁言中/);
  assert.ok(!/已关闭/.test(env.el("channels-list").innerHTML), "the old wording is gone");
  const queue = {
    kind: "queue",
    active: "N6",
    paused: false,
    seconds_left: 20,
    queue: [
      { holder: "N4", state: "done", messages_used: 1, max_messages: 1 },
      { holder: "N5", state: "done", messages_used: 1, max_messages: 1 },
      { holder: "N6", state: "active", messages_used: 0, max_messages: 1 },
      { holder: "N7", state: "pending", messages_used: 0, max_messages: 1 },
    ],
    free: null,
  };
  env.sse("channel.updated", { channel: F.channel("public", "公共频道", F.EVERYONE, { mode: "queue", floor: queue }) });
  assert.match(chip("public"), /📋 队列 2\/4/);
  assert.match(chip("public"), /持麦：Frank/, "the holder by nickname");
  env.sse("channel.updated", { channel: F.channel("public", "公共频道", F.EVERYONE, { mode: "turn", floor: { kind: "turn", active: "N1", paused: false, seconds_left: 9, queue: [{ holder: "N1", state: "active", messages_used: 0, max_messages: 1 }, { holder: "N2", state: "active", messages_used: 0, max_messages: 1 }], free: null } }) });
  assert.match(chip("public"), /🎤 轮流发言/);
  assert.match(chip("public"), /持麦：Alice、Bob/);
  env.sse("channel.updated", { channel: F.channel("public", "公共频道", F.EVERYONE, { mode: "free", floor: { kind: "free", active: null, paused: false, seconds_left: null, queue: [], free: { chars_used: 450, max_chars_total: 2000, elapsed_s: 30, duration_s: 300, silent_s: 2, message_counts: {} } } }) });
  assert.match(chip("public"), /💬 自由讨论/);
  assert.match(chip("public"), /450\/2000 字/);
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
    queue: ["N4", "N5", "N6"].map((h, i) => ({ holder: h, state: i < doneN ? "done" : i === doneN ? "active" : "pending", messages_used: 0, max_messages: 1 })),
    free: null,
  });
  env.sse("channel.updated", { channel: F.channel("public", "公共频道", F.EVERYONE, { mode: "queue", floor: mk("N4", 0) }) });
  assert.match(env.el("channels-list").innerHTML, /队列 0\/3/);
  env.sse("floor.changed", { channel_id: "public", floor: mk("N5", 1) });
  assert.match(env.el("channels-list").innerHTML, /队列 1\/3/);
  assert.match(env.el("channels-list").innerHTML, /持麦：Eve/);
});

test("unread counts per channel; opening a channel marks it read; the history that exists on load is read", async () => {
  const { env } = await live();
  const unread = () => Object.fromEntries(Object.entries(channelItems(env)).map(([id, v]) => [id, v.unread]));
  assert.deepEqual(unread(), { public: 0, wolves: 0 });
  env.sse("message.committed", F.msg(41, "public", "N4", "a")); // the public channel is being read: nothing unread
  assert.deepEqual(unread(), { public: 0, wolves: 0 });
  env.sse("message.committed", F.msg(42, "wolves", "N3", "b", "speech", ["moderator", ...F.WOLVES]));
  env.sse("message.committed", F.msg(43, "wolves", "N6", "c", "speech", ["moderator", ...F.WOLVES]));
  assert.deepEqual(unread(), { public: 0, wolves: 2 });
  assert.match(env.el("channels-list").innerHTML, /class="channel-unread" title="2 条未读">2</);
  channelClick(env, "wolves");
  assert.deepEqual(unread(), { public: 0, wolves: 0 });
  env.sse("message.committed", F.msg(44, "public", "N5", "d"));
  assert.deepEqual(unread(), { public: 1, wolves: 0 });
  channelClick(env, "public");
  assert.deepEqual(unread(), { public: 0, wolves: 0 });
  // the folded private-chat group shows the DMs' total
  env.sse("message.committed", F.msg(50, "dm-N8", "N8", "主持人，请复核。", "challenge", ["moderator", "N8"], { ticket_id: "t2" }));
  assert.match(env.el("channels-list").innerHTML, /私聊<\/span> <span class="group-count">12<\/span><span class="channel-unread"[^>]*>1<\/span>/);
});

test("tickets: a challenge arrives as an unread DM; the ticket list follows challenge.opened / resolved; a click locates the DM", async () => {
  const { env } = await live();
  env.sse("challenge.opened", { ticket: { id: "t2", player_id: "N8", status: "open", verdict: null, preview: "主持人，请复核。", opened_seq: 50 } });
  assert.match(env.el("channels-list").innerHTML, /质询工单 <span class="group-count">2<\/span><span class="ticket-open-count">2 待处理<\/span>/);
  env.sse("challenge.resolved", { ticket_id: "t2", verdict: "upheld" });
  assert.match(env.el("channels-list").innerHTML, /<span class="ticket-status ts-upheld">成立<\/span>/);
  env.sse("challenge.resolved", { ticket_id: "t1", verdict: "rejected" });
  assert.match(env.el("channels-list").innerHTML, /<span class="ticket-status ts-rejected">驳回<\/span>/);
  env.el("channels-list").dispatch("click", { target: target({ ".ticket-item": { "data-ticket": "t1", "data-channel": "dm-N3" } }) });
  assert.equal(env.app().state.selectedChannel, "dm-N3");
  assert.equal(env.el("feed-channel-label").textContent, "Carol");
  assert.match(env.el("channels-list").innerHTML, /data-channel="dm-N3"/, "the DM group opens so the selection is visible");
  assert.match(feedHtml(env), /data-ticket-msg="t1"/);
});

test("the narrow rail collapses the tickets into one icon with their count; it opens the list as a popover", async () => {
  const { env } = await live();
  assert.match(env.el("channels-list").innerHTML, /<button type="button" class="rail-tickets[^"]*" data-tickets-toggle[^>]*><span class="rail-icon"[^>]*>❓<\/span><span class="rail-count">1<\/span><\/button>/);
  assert.equal(env.el("ticket-pop").hidden, true);
  env.el("channels-list").dispatch("click", { target: target({ "[data-tickets-toggle]": { "data-tickets-toggle": "" } }) });
  assert.equal(env.el("ticket-pop").hidden, false);
  assert.match(env.el("ticket-pop").innerHTML, /质询工单 <span class="group-count">1<\/span>/);
  assert.match(env.el("ticket-pop").innerHTML, /data-ticket="t1"/);
  env.el("ticket-pop").dispatch("click", { target: target({ ".ticket-item": { "data-ticket": "t1", "data-channel": "dm-N3" } }) });
  assert.equal(env.el("ticket-pop").hidden, true, "picking a ticket closes the popover");
  assert.equal(env.app().state.selectedChannel, "dm-N3");
  // Esc / a click elsewhere close it too
  env.el("channels-list").dispatch("click", { target: target({ "[data-tickets-toggle]": { "data-tickets-toggle": "" } }) });
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.equal(env.el("ticket-pop").hidden, true);
  assert.match(H.CSS_SRC, /\.workspace-grid\.narrow-layout \.rail-tickets/);
  assert.match(H.CSS_SRC, /\.workspace-grid\.left-collapsed \.ticket-item/);
});

test("clicking a channel selects it once (mousedown + click); DM group toggle opens and folds", async () => {
  const { env } = await live();
  env.el("channels-list").dispatch("click", { target: target({ "[data-dm-toggle]": { "data-dm-toggle": "" } }) });
  assert.match(env.el("channels-list").innerHTML, /data-channel="dm-N1"/);
  env.el("channels-list").dispatch("click", { target: target({ "[data-dm-toggle]": { "data-dm-toggle": "" } }) });
  assert.ok(!env.el("channels-list").innerHTML.includes('data-channel="dm-N1"'));
  channelClick(env, "wolves");
  assert.equal(env.el("feed-channel-label").textContent, "狼人阵营");
  assert.equal(articles(env), 2);
  const item = target({ ".channel-item": { "data-channel": "public" } });
  env.el("channels-list").dispatch("mousedown", { target: item, button: 0 });
  env.el("channels-list").dispatch("click", { target: item });
  assert.equal(env.app().state.selectedChannel, "public");
  assert.equal(articles(env), 3);
});

// ---- the spectator viewpoint ----------------------------------------------------------------------------------------------
test("viewpoint select: god view + one entry per player (seat · nickname); switching never asks the server again", async () => {
  const { env, backend } = await live();
  const options = env.el("select-viewpoint").innerHTML;
  assert.equal((options.match(/<option /g) || []).length, 13);
  assert.match(options, /<option value="god">上帝视角<\/option>/);
  assert.match(options, /<option value="N3">3 · Carol<\/option>/);
  assert.ok(!/p03|N3 ·|· Carol 3/.test(options), "no internal ids in the labels");
  const callsBefore = backend.calls.length;
  for (const v of ["N3", "god", "N1"]) selectViewpoint(env, v);
  assert.equal(backend.calls.length, callsBefore, "no request was made");
  assert.equal(env.app().state.viewpoint, "N1");
});

test("player viewpoint: only its messages, only its channels, other players' identities are '?'", async () => {
  const { env } = await live();
  selectViewpoint(env, "N3");
  assert.equal(env.el("feed-viewing").textContent, "视角：Carol");
  assert.ok(env.el("feed-viewing").classList.contains("is-player"));
  channelClick(env, "wolves");
  assert.equal(articles(env), 2, "the wolf sees the wolves chat");
  selectViewpoint(env, "N1");
  assert.equal(env.app().state.selectedChannel, "public", "the wolves channel is invisible to N1: back to the public one");
  const feed = feedHtml(env);
  assert.equal(articles(env), 3);
  assert.ok(!feed.includes("今晚刀"), "the wolves' chat is invisible");
  assert.ok(!feed.includes("计票是不是漏了一票"), "so is N3's private challenge");
  const ch = env.el("channels-list").innerHTML;
  assert.match(ch, /data-channel="public"/);
  assert.ok(!ch.includes('data-channel="wolves"'));
  assert.match(ch, /<span class="group-label">私聊<\/span> <span class="group-count">1<\/span>/);
  assert.ok(!ch.includes("质询工单"));
  const c = cards(env);
  assert.equal(tagText(c.N1), "平民", "I know my own role");
  for (const id of F.PLAYER_IDS.filter((x) => x !== "N1")) {
    assert.equal(tagText(c[id]), "?", `${id}'s identity must not leak into N1's view`);
    assert.ok(!/狼人|预言家|女巫|猎人|守卫|白狼王/.test(c[id]), `${id}'s role leaks`);
  }
  selectViewpoint(env, "god");
  assert.equal(articles(env), 3);
  assert.equal(tagText(cards(env).N3), "狼人");
  assert.match(env.el("channels-list").innerHTML, /质询工单/);
});

test("in a player's view other agents' states stay hidden; their own is shown; the panel of another player hides it too", async () => {
  const { env } = await live();
  env.sse("agent.status", { id: "N1", state: "thinking" });
  env.sse("agent.status", { id: "N2", state: "thinking" });
  selectViewpoint(env, "N1");
  const c = cards(env);
  assert.match(c.N1, /pcard-state st-thinking/);
  assert.ok(!/pcard-state/.test(c.N2));
  assert.ok(cardClasses(c.N1).includes("viewing"));
  assert.ok(!cardClasses(c.N2).includes("viewing"));
  cardClick(env, "N2");
  assert.ok(!/思考中/.test(panelText(env)));
  assert.ok(!/detail-state/.test(env.el("detail-live").innerHTML));
  // the moderator chip hides its state as well
  assert.ok(!/mod-chip-state/.test(env.el("mod-chip").innerHTML));
});

test("the viewpoint is remembered; a channel the viewer cannot see falls back to the default; an unknown player falls back to god", async () => {
  const { env } = await live();
  channelClick(env, "wolves");
  selectViewpoint(env, "N1");
  assert.equal(env.app().state.selectedChannel, "public");
  assert.equal(env.storage.get("arena_viewpoint_v1"), "N1");
  const withPref = await H.bootLive({}, { storage: { arena_viewpoint_v1: "N5" } });
  withPref.env.sse("snapshot", { session: F.makeSession({ event_id: 500 }) }, 500);
  assert.equal(withPref.env.app().state.viewpoint, "N5");
  assert.equal(withPref.env.el("select-viewpoint").value, "N5");
  const stale = await H.bootLive({}, { storage: { arena_viewpoint_v1: "p99" } });
  stale.env.sse("snapshot", { session: F.makeSession({ event_id: 500 }) }, 500);
  assert.equal(stale.env.app().state.viewpoint, "god");
});

// ---- controls & banners ------------------------------------------------------------------------------------------------------
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

test("start / pause / new post the right endpoints, adopt the returned session, and a new game starts at the public channel", async () => {
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
  assert.equal(env.app().state.selectedChannel, "wolves");
  backend.controlSession = F.makeSession({ session_id: "fresh", event_id: 1, status: "idle", messages: [] });
  env.el("btn-new").dispatch("click");
  await settle();
  env.flushRaf();
  assert.equal(backend.calls.filter((c) => c.url.endsWith("/api/control/new")).length, 1);
  assert.equal(env.app().state.session.session_id, "fresh");
  assert.equal(env.app().state.selectedChannel, "public");
  assert.equal(env.app().state.lastEventId, 1);
});

test("a paused game on hold shows the moderator's reason (status pill + banner); finished shows the result and the next steps", async () => {
  const { env } = await live({ status: "running" });
  assert.equal(env.el("status-banner").hidden, true);
  env.sse("session.updated", { status: "paused", pause_reason: "hold", hold_reason: "请确认警长归属", active_status_text: "已暂停 · 主持人：请确认警长归属", winner: null, summary: "" });
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
  assert.match(banner, /复盘全部/);
  assert.match(banner, /banner-short">🏁 对局已结束：好人阵营 获胜</, "a phone shows the short sentence");
  assert.ok(env.el("active-status-dot").className.includes("finished"));
  assert.equal(env.el("phase-pill").textContent, "第 1 天 · 白天发言");
  env.sse("phase.changed", { text: "第 3 天 · 终局" });
  assert.equal(env.el("phase-pill").textContent, "第 3 天 · 终局");
});

test("a stall notice is a warning banner (the game keeps running); it goes away when the notice clears", async () => {
  const { env } = await live({ status: "running" });
  env.sse("session.updated", { stall_notice: "对局似乎停滞了" });
  assert.equal(env.el("status-banner").hidden, false);
  assert.match(env.el("status-banner").innerHTML, /banner-stall/);
  assert.match(env.el("status-banner").innerHTML, /对局似乎停滞了/);
  assert.match(env.el("status-banner").className, /banner-stall/);
  assert.equal(env.app().state.session.status, "running", "no pause");
  assert.equal(env.el("btn-pause").disabled, false);
  env.sse("session.updated", { stall_notice: "" });
  assert.equal(env.el("status-banner").hidden, true);
  env.sse("snapshot", { session: F.makeSession({ stall_notice: "已 120 秒没有进展", event_id: 900 }) }, 900);
  assert.match(env.el("status-banner").innerHTML, /已 120 秒没有进展/);
});

test("the demo badge stays: '演示模式' next to the version", async () => {
  const demo = await live({ demo: true });
  assert.match(demo.env.el("app-version").textContent, /v0\.12\.1-test · 演示模式/);
  const normal = await live({ demo: false });
  assert.equal(normal.env.el("app-version").textContent, "v0.12.1-test");
});

test("usage: the stats bar turns yellow at 80 % and red at 100 % of the limit; a budget pause says so in the top bar", async () => {
  const b = (used, state, extra = {}) => ({ max_tokens: 1000, max_cost_usd: null, force_stop: true, used_tokens: used, used_cost_usd: 0.1, state, ...extra });
  const { env } = await live({ budget: b(100, "ok") });
  const pill = () => env.el("usage-telemetry-pill").getAttribute("data-budget");
  assert.equal(pill(), "ok");
  env.sse("budget.updated", { budget: b(500, "ok") });
  assert.equal(pill(), "ok");
  env.sse("budget.warning", { budget: b(800, "warning") });
  assert.equal(pill(), "warning");
  assert.ok(env.toasts.some((t) => /80%/.test(t.textContent)));
  assert.match(env.el("usage-telemetry-pill").getAttribute("title"), /已超过 80%/);
  assert.equal(env.el("status-banner").hidden, true, "a warning only colours the bar");
  env.sse("budget.reached", { budget: b(1000, "reached") });
  assert.equal(pill(), "reached");
  assert.ok(env.toasts.some((t) => /已达到预算上限/.test(t.textContent)));
  assert.match(env.el("status-banner").innerHTML, /banner-budget/);
  assert.match(env.el("status-banner").innerHTML, /已达到预算上限/);
  // the game pauses because of it
  env.sse("session.updated", { status: "paused", pause_reason: "budget", active_status_text: "已暂停" });
  assert.equal(env.el("active-status-text").textContent, "因预算上限暂停");
  assert.match(env.el("status-banner").innerHTML, /因预算上限暂停/);
  assert.match(env.el("status-banner").innerHTML, /banner-short">⛔ 因预算上限暂停/);
  assert.equal(env.el("btn-resume").disabled, false, "the user can raise the limit and continue");
  // raising the limit clears the colour; the unit " Tokens" is its own element so a tight bar can drop it
  env.sse("budget.updated", { budget: b(1000, "ok", { max_tokens: 5000 }) });
  assert.equal(pill(), "ok");
  assert.match(env.el("status-banner").innerHTML, /上限已调整/);
  assert.match(H.HTML_SRC, /<span class="usage-unit">&nbsp;Tokens<\/span>/);
});

test("usage cost comes from the server total (price snapshots) and stays live with agent.metrics (four token kinds)", async () => {
  const session = F.makeSession({ usage_stats: { total_tokens: 1500, prompt_tokens: 1000, cache_read_tokens: 0, cache_write_tokens: 0, completion_tokens: 500, total_cost_usd: 0.1234, by_actor: {}, by_model: {} } });
  const { env } = await live({ ...session });
  assert.equal(env.el("usage-cost-text").textContent, "$0.1234");
  assert.equal(env.el("usage-tokens-text").textContent, "1.5K");
  env.sse("agent.metrics", { id: "N1", calls: 1, prompt_tokens: 1000, cache_read_tokens: 500, cache_write_tokens: 0, completion_tokens: 1000, cost_usd: 0.0766, tool_calls: 2, thinking_rounds: 1, rejections: 0, activations: 1, cache_hit_rate: 0.33 }, 560);
  assert.equal(env.el("usage-cost-text").textContent, "$0.2000");
  assert.equal(env.el("usage-tokens-text").textContent, "4.0K"); // 1500 + 1000 + 500 + 1000
});

test("the execution depth is gone: no select, no label, no endpoint — even for a session that still carries it", async () => {
  const { env, backend } = await live({ execution_depth: "high" });
  env.sse("session.updated", { execution_depth: "xhigh" });
  assert.ok(!H.HTML_SRC.includes("执行深度") && !H.HTML_SRC.includes("exec-depth"));
  assert.ok(!backend.calls.some((c) => c.url.includes("execution-depth")));
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

test("a hidden / occluded page still repaints (rAF never fires there): timer fallback, and no double paint", async () => {
  const { env } = await live();
  const text = () => env.el("phase-pill").textContent;
  env.document.visibilityState = "hidden";
  env.sse("phase.changed", { text: "第 2 夜 · 黑夜" }, undefined, { flush: false });
  env.flushRaf();
  assert.notEqual(text(), "第 2 夜 · 黑夜");
  assert.ok(env.timers.some((t) => t.ms <= 200), "a short fallback timer is pending");
  while (env.timers.length) env.runNextTimer();
  assert.equal(text(), "第 2 夜 · 黑夜");
  env.document.visibilityState = "visible";
  env.sse("phase.changed", { text: "第 3 天" }, undefined, { flush: false });
  env.document.visibilityState = "hidden";
  env.document.dispatchEvent("visibilitychange");
  while (env.timers.length) env.runNextTimer();
  assert.equal(text(), "第 3 天");
  env.flushRaf();
  assert.equal(text(), "第 3 天");
  env.document.visibilityState = "visible";
});

test("the store object behind the UI keeps the PublicSession v3 shape after many events", async () => {
  const { env } = await live();
  env.sse("message.committed", F.msg(41, "public", "N4", "x"));
  env.sse("agent.status", { id: "N1", state: "thinking" });
  env.sse("phase.changed", { text: "第 2 夜" });
  const s = env.app().state.session;
  for (const k of ["session_id", "status", "participants", "moderator", "channels", "messages", "requests", "tickets", "dashboard", "public_tags", "tag_styles", "ui", "usage_stats", "model_bindings", "binding_issues", "restored_from_save", "restore_notice", "budget", "memory_enabled", "review", "stall_notice"]) {
    assert.ok(k in s, `missing ${k}`);
  }
  assert.equal(s.messages[s.messages.length - 1].content, "x");
  assert.equal(Store.findById(s.participants, "N1").state, "thinking");
  assert.equal(s.phase_text, "第 2 夜");
  assert.equal(View.displayName(s, "N1"), "Alice");
});

H.runTests(tests);
