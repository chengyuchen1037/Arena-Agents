"use strict";
// v0.12.1 features of the UI against the fake DOM (see harness.js):
//   (roster: names / avatars / editors / 名册 menu / review moved to test_bots_ui.js and test_botedit_ui.js in v0.14: a player is a bot now)
//   budget      the "上限" dialog (GET/PUT /api/budget)
//   prices      price source bar, four model prices, suggestions that fill the form
//   platform    <html data-platform / data-layout>, the phone tab bar, the "⋯" menu, the drawer backdrop
// Run: node tests/frontend/test_v0121_ui.js
const assert = require("node:assert/strict");
const H = require("./harness.js");

const { F, Core, target, settle, bootLive, FakeEl, posts } = H;

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

async function live(over = {}, overrides = {}, envOpts = {}) {
  // the fake backend answers write requests from ITS session (e.g. "names can only change while idle"),
  // so it gets the same game the stream's snapshot describes
  const session = F.makeSession({ event_id: 500, ...over });
  const ctx = await bootLive({ session, ...overrides }, envOpts);
  ctx.env.sse("snapshot", { session }, 500);
  return ctx;
}

const IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1";
const IPAD = "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1";
const ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36";

const cardClick = (env, id) => env.el("participants-grid").dispatch("click", { target: target({ ".pcard": { "data-actor": id } }) });
const panelClick = (env, action, kind) => env.el("detail-panel").dispatch("click", { target: target({ "[data-detail-action]": { "data-detail-action": action, "data-kind": kind || null } }) });
const menuClick = (env, action) => env.el("roster-menu").dispatch("click", { target: target({ "[data-roster-action]": { "data-roster-action": action } }) });
const typeInto = (env, kind, id, text) => {
  env.el(`ta-detail-${kind}`).value = text;
  env.el("detail-panel").dispatch("input", { target: { id: `ta-detail-${kind}`, value: text, getAttribute: (k) => (k === "data-draft-key" ? `${id}:${kind}` : null) } });
};
// v0.13.1 (G42): the memory editor is structured - an input per other seat, a box for the lessons, a box for the notes of "完善"
const typeImpression = (env, otherId, text) =>
  env.el("detail-panel").dispatch("input", { target: { id: "", value: text, getAttribute: (k) => (k === "data-imp-id" ? otherId : null) } });
const typeBox = (env, boxId, text) => {
  env.el(boxId).value = text;
  env.el("detail-panel").dispatch("input", { target: { id: boxId, value: text, getAttribute: () => null } });
};
const textOf = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
const html = (env, id) => env.el(id).innerHTML;
const cardName = (env, id) => ((env.el("participants-grid").innerHTML.split(/(?=<div class="pcard[ "])/).find((p) => p.includes(`data-actor="${id}"`)) || "").match(/<span class="pcard-name">([^<]*)<\/span>/) || [])[1];
const cardImg = (env, id) => ((env.el("participants-grid").innerHTML.split(/(?=<div class="pcard[ "])/).find((p) => p.includes(`data-actor="${id}"`)) || "").match(/ src="([^"]+)"/) || [])[1];
const lastToast = (env) => (env.toasts.length ? env.toasts[env.toasts.length - 1].textContent : "");
/** Let pending requests finish, then run the animation frame the app schedules its repaint on. */
const flush = async (env, ms = 30) => {
  await settle(ms);
  env.flushRaf();
};

// =====================================================================================================
// roster -> bots (v0.14)
// The roster of v0.12.1 (names, avatars, persona / memory editors, the 名册 menu, the review of a player) is gone: a player IS a bot now and
// has its own API (docs/v0.12-contracts.md §13.7).  What this file tested of it moved, adapted, to test_bots_ui.js (names, avatars, the
// moderator's avatar, the 名册 menu, review, the initialization), test_botedit_ui.js (the editors that replace the persona / memory boxes) and
// test_bots_drift.py (the limits and the words, held to the backend).  What stays here is what does not depend on the roster.
// =====================================================================================================
const modClick = (env) => env.el("topbar-status").dispatch("click", { target: target({ "#mod-chip": { "data-actor": "moderator" } }) });
const chipImg = (env) => (html(env, "mod-chip").match(/ src="([^"]+)"/) || [])[1];

test("an older backend (no moderator avatar anywhere): the top hat stays and there is nothing to roll", async () => {
  const { env } = await live({ status: "idle" });
  assert.equal(chipImg(env), H.Avatar.moderatorUrl());
  modClick(env);
  assert.ok(!html(env, "detail-head").includes("regen-avatar"), "no avatar, no endpoint, no button");
  assert.ok(!html(env, "detail-head").includes("toggle-avatar-pop") && !html(env, "detail-head").includes("avatar-code"), "and no code to show either");
});

// =====================================================================================================
// budget dialog (E10 / E22)
// =====================================================================================================
const budgetOf = (used, over = {}) => ({ max_tokens: 1000, max_cost_usd: null, force_stop: true, used_tokens: used, used_cost_usd: 0.25, state: used >= 1000 ? "reached" : used >= 800 ? "warning" : "ok", ...over });

test("the 上限 button opens the dialog with the current limits, usage, progress bars and the four token kinds (GET /api/budget)", async () => {
  const b = budgetOf(850, { max_cost_usd: 2 });
  const { env, backend } = await live({ budget: b, usage_stats: { prompt_tokens: 400, cache_read_tokens: 300, cache_write_tokens: 50, completion_tokens: 100, total_tokens: 850, total_cost_usd: 0.25, cache_hit_rate: 0.4, by_model: {}, by_actor: {} } }, { budget: b });
  assert.notEqual(env.el("budget-modal").style.display, "flex", "closed at first");
  env.el("btn-open-budget-modal").dispatch("click");
  await flush(env, 30);
  assert.equal(env.el("budget-modal").style.display, "flex");
  assert.ok(backend.calls.some((c) => c.method === "GET" && c.url.endsWith("/api/budget")));
  assert.equal(env.el("input-budget-tokens").value, "1000");
  assert.equal(env.el("input-budget-cost").value, "2");
  assert.equal(env.el("chk-budget-force-stop").checked, true);
  const usage = html(env, "budget-usage");
  assert.match(textOf(usage), /token 850 \/ 1,000 · 85%/);
  assert.match(textOf(usage), /费用 \$0\.2500 \/ \$2\.0000 · 13%/);
  assert.match(usage, /budget-bar-fill warning/, "85 % of the token limit: the bar is yellow");
  assert.match(usage, /budget-bar-fill ok/, "12.5 % of the cost limit: green");
  assert.match(usage, /role="progressbar"/);
  assert.match(textOf(usage), /已用 80% 以上/);
  const parts = textOf(html(env, "budget-breakdown"));
  assert.match(parts, /400 未命中输入/);
  assert.match(parts, /300 缓存读取/);
  assert.match(parts, /50 缓存写入/);
  assert.match(parts, /100 输出/);
  assert.match(parts, /缓存命中率 40%/);
  // a live budget.reached event repaints the open dialog
  env.sse("budget.reached", { budget: budgetOf(1000, { max_cost_usd: 2 }) });
  assert.match(html(env, "budget-usage"), /budget-bar-fill reached/);
  assert.match(textOf(html(env, "budget-usage")), /已达到上限/);
  assert.match(html(env, "budget-usage"), /budget-pct reached/);
  // close
  env.el("btn-close-budget-modal").dispatch("click");
  assert.equal(env.el("budget-modal").style.display, "none");
});

test("saving the limits: empty = no limit, positive numbers only, force-stop flag; PUT /api/budget; the top bar follows", async () => {
  const b = budgetOf(850);
  const { env, backend } = await live({ budget: b }, { budget: b });
  assert.equal(env.el("usage-telemetry-pill").getAttribute("data-budget"), "warning");
  env.el("btn-open-budget-modal").dispatch("click");
  await flush(env, 30);
  // invalid input: shown in the dialog, nothing is sent, the dialog stays open
  env.el("input-budget-tokens").value = "abc";
  env.el("btn-save-budget").dispatch("click");
  await flush(env, 10);
  assert.equal(env.el("budget-form-error").hidden, false);
  assert.match(env.el("budget-form-error").textContent, /token 上限必须是正数/);
  assert.equal(posts(backend, "PUT", "/api/budget").length, 0);
  assert.equal(env.el("budget-modal").style.display, "flex");
  env.el("input-budget-tokens").value = "1.5";
  env.el("btn-save-budget").dispatch("click");
  await flush(env, 10);
  assert.match(env.el("budget-form-error").textContent, /必须是整数/);
  env.el("input-budget-tokens").value = "10";
  env.el("input-budget-cost").value = "-3";
  env.el("btn-save-budget").dispatch("click");
  await flush(env, 10);
  assert.match(env.el("budget-form-error").textContent, /费用上限必须是正数/);

  // valid: tokens 10,000, cost empty, force stop off
  env.el("input-budget-tokens").value = "10,000";
  env.el("input-budget-cost").value = "";
  env.el("chk-budget-force-stop").checked = false;
  env.el("btn-save-budget").dispatch("click");
  await flush(env, 30);
  const put = posts(backend, "PUT", "/api/budget");
  assert.equal(put.length, 1);
  assert.deepEqual(put[0].body, { max_tokens: 10000, max_cost_usd: null, force_stop: false });
  assert.equal(env.el("budget-modal").style.display, "none");
  assert.match(lastToast(env), /上限已保存：token 上限 10\.0K，费用不限$/);
  assert.equal(env.app().state.session.budget.max_tokens, 10000);
  assert.equal(env.el("usage-telemetry-pill").getAttribute("data-budget"), "ok", "850 of 10,000: back to normal");
});

test("clearing both limits saves nulls; the server's refusal is shown in the dialog", async () => {
  const b = budgetOf(10);
  const { env, backend } = await live({ budget: b }, { budget: b });
  env.el("btn-open-budget-modal").dispatch("click");
  await flush(env, 30);
  env.el("input-budget-tokens").value = "";
  env.el("input-budget-cost").value = "  ";
  env.el("chk-budget-force-stop").checked = true;
  env.el("btn-save-budget").dispatch("click");
  await flush(env, 30);
  assert.deepEqual(posts(backend, "PUT", "/api/budget")[0].body, { max_tokens: null, max_cost_usd: null, force_stop: true });
  assert.match(lastToast(env), /token 不限，费用不限，达到后强制停止/);
  env.el("btn-open-budget-modal").dispatch("click");
  await flush(env, 30);
  backend.failures["PUT /api/budget"] = [422, "max_tokens 必须是正整数或空"];
  env.el("input-budget-tokens").value = "5";
  env.el("btn-save-budget").dispatch("click");
  await flush(env, 30);
  assert.match(env.el("budget-form-error").textContent, /max_tokens 必须是正整数或空/);
  assert.equal(env.el("budget-modal").style.display, "flex");
  assert.equal(env.el("btn-save-budget").disabled, false);
});

test("raising the limit after a budget pause: the banner and the top bar react, 继续 works", async () => {
  const b = budgetOf(1000, { state: "reached" });
  const { env } = await live({ status: "paused", pause_reason: "budget", budget: b }, { budget: b });
  assert.equal(env.el("active-status-text").textContent, "已暂停 · 已达到预算上限", "(v0.14, H26: the status label says 已暂停 and why)");
  assert.equal(env.el("usage-telemetry-pill").getAttribute("data-budget"), "reached");
  assert.match(env.el("status-banner").innerHTML, /因预算上限暂停/);
  assert.equal(env.el("btn-resume").disabled, false);
  env.el("btn-open-budget-modal").dispatch("click");
  await flush(env, 30);
  env.el("input-budget-tokens").value = "5000";
  env.el("btn-save-budget").dispatch("click");
  await flush(env, 30);
  assert.equal(env.el("usage-telemetry-pill").getAttribute("data-budget"), "ok");
  assert.match(env.el("status-banner").innerHTML, /上限已调整/);
});

// =====================================================================================================
// Provider dialog: price source, four prices, suggestions (E11 / E12)
// =====================================================================================================
const openProviders = async (env) => {
  env.el("btn-open-provider-modal").dispatch("click");
  await flush(env, 30);
};
const SUGGEST = { key: "deepseek/m-new", input_price_per_m: 0.27, output_price_per_m: 1.1, cache_read_price_per_m: 0.07, cache_write_price_per_m: null, context_window_tokens: 128000 };

test("the price source bar: default URL, status line (count, time), save the URL, refresh the table", async () => {
  const { env, backend } = await live({ status: "idle" });
  await openProviders(env);
  assert.ok(backend.calls.some((c) => c.method === "GET" && c.url.endsWith("/api/pricing/source")));
  assert.equal(env.el("input-pricing-url").value, Core.DEFAULT_PRICING_SOURCE_URL);
  assert.match(env.el("pricing-status").textContent, /已加载 2,345 条 · 获取于 \d{4}-\d\d-\d\d \d\d:\d\d/);
  assert.match(env.el("pricing-status").className, /\bok\b/);
  // refresh
  env.el("btn-refresh-pricing").dispatch("click");
  await flush(env, 30);
  assert.equal(posts(backend, "POST", "/api/pricing/refresh").length, 1);
  assert.match(env.el("pricing-status").textContent, /已加载 2,400 条/);
  assert.match(lastToast(env), /价格表已刷新：2,400 条/);
  // a failing refresh keeps the old numbers and says why
  backend.refreshError = "无法拉取价格表：连接超时";
  env.el("btn-refresh-pricing").dispatch("click");
  await flush(env, 30);
  assert.match(env.el("pricing-status").textContent, /获取失败：无法拉取价格表：连接超时/);
  assert.match(env.el("pricing-status").textContent, /仍使用上次的 2,400 条/);
  assert.match(env.el("pricing-status").className, /\berror\b/);
  // change the URL and save
  backend.refreshError = "";
  env.el("input-pricing-url").value = "https://example.com/prices.json";
  env.el("btn-save-pricing-url").dispatch("click");
  await flush(env, 30);
  assert.deepEqual(posts(backend, "PUT", "/api/pricing/source")[0].body, { url: "https://example.com/prices.json" });
  assert.match(lastToast(env), /价格源 URL 已保存/);
});

test("price source URL: https only; refresh first saves a URL that was typed but not saved yet", async () => {
  const { env, backend } = await live({ status: "idle" });
  await openProviders(env);
  env.el("input-pricing-url").value = "http://insecure.example.com/p.json";
  env.el("btn-save-pricing-url").dispatch("click");
  await flush(env, 20);
  assert.match(lastToast(env), /价格源 URL 必须以 https:\/\/ 开头/);
  assert.equal(posts(backend, "PUT", "/api/pricing/source").length, 0);
  assert.match(env.el("pricing-status").textContent, /价格源 URL 必须以 https/);
  env.el("input-pricing-url").value = "https://example.com/new.json";
  env.el("btn-refresh-pricing").dispatch("click");
  await flush(env, 40);
  assert.equal(posts(backend, "PUT", "/api/pricing/source").length, 1, "the typed URL was saved first");
  assert.equal(posts(backend, "POST", "/api/pricing/refresh").length, 1);
  const order = backend.calls.filter((c) => /pricing\/(source|refresh)$/.test(c.url) && c.method !== "GET").map((c) => c.method);
  assert.deepEqual(order, ["PUT", "POST"]);
  // an older backend without the endpoint: the bar says so and the rest of the dialog still works
  const old = await live({ status: "idle" }, { failures: { "GET /api/pricing/source": [404, "Not Found"] } });
  await openProviders(old.env);
  assert.match(old.env.el("pricing-status").textContent, /价格源接口不可用/);
  assert.match(html(old.env, "provider-list"), /DeepSeek/);
});

test("models are listed with FOUR prices and their price source", async () => {
  const { env } = await live({ status: "idle" });
  await openProviders(env);
  const list = textOf(html(env, "provider-model-list"));
  assert.match(list, /model-x/);
  assert.match(list, /输入 \$0\.5 · 缓存读 \$0\.1 · 缓存写 — · 输出 \$2 \/ 1M · 窗口 128000/);
  assert.match(list, /来源 litellm:deepseek\/model-x/);
  // another provider: manual prices, no cache prices
  env.el("provider-list").dispatch("click", { target: target({ ".provider-item": { "data-id": "p2" } }) });
  const claude = textOf(html(env, "provider-model-list"));
  assert.match(claude, /claude-y/);
  assert.match(claude, /输入 \$3 · 缓存读 — · 缓存写 — · 输出 \$15/);
  assert.match(claude, /来源 manual/);
});

test("'获取模型' brings suggestions: picking a model fills the four prices and the context window; they stay editable", async () => {
  const { env, backend } = await live({ status: "idle" }, { suggestions: { "m-new": SUGGEST } });
  await openProviders(env);
  env.el("btn-discover-models").dispatch("click");
  await flush(env, 30);
  assert.match(env.el("discover-status").textContent, /其中 1 个在价格源里匹配到价格/);
  env.el("discover-list").dispatch("change", { target: { value: "m-new" } });
  assert.equal(env.el("input-model-id").value, "m-new");
  assert.equal(env.el("input-model-price-in").value, "0.27");
  assert.equal(env.el("input-model-price-cache-read").value, "0.07");
  assert.equal(env.el("input-model-price-cache-write").value, "", "no cache-write price in the table: empty = input price");
  assert.equal(env.el("input-model-price-out").value, "1.1");
  assert.equal(env.el("input-model-ctx").value, "128000");
  assert.match(env.el("model-price-hint").textContent, /已按价格源条目「deepseek\/m-new」自动填写四个价格和上下文窗口，可以再修改/);
  assert.match(env.el("model-price-hint").className, /\bok\b/);
  // the user edits one price, then adds the model
  env.el("input-model-price-out").value = "1.5";
  env.el("model-form").dispatch("submit");
  await flush(env, 30);
  const req = posts(backend, "POST", "/models");
  assert.equal(req.length, 1);
  assert.deepEqual(req[0].body, {
    model_id: "m-new",
    input_price_per_m: 0.27,
    cache_read_price_per_m: 0.07,
    cache_write_price_per_m: null,
    output_price_per_m: 1.5,
    context_window_tokens: 128000,
  });
  // the form is cleared for the next model
  assert.equal(env.el("input-model-id").value, "");
  assert.equal(env.el("input-model-price-cache-read").value, "");
  assert.match(textOf(html(env, "provider-model-list")), /m-new/);
});

test("a model without a match stays empty with a hint to fill it in by hand; cache prices left empty are sent as null", async () => {
  const { env, backend } = await live({ status: "idle" }, { suggestions: { "m-new": SUGGEST } });
  await openProviders(env);
  env.el("btn-discover-models").dispatch("click");
  await flush(env, 30);
  env.el("discover-list").dispatch("change", { target: { value: "other/slash" } });
  for (const id of ["input-model-price-in", "input-model-price-cache-read", "input-model-price-cache-write", "input-model-price-out", "input-model-ctx"]) {
    assert.equal(env.el(id).value, "", id);
  }
  assert.match(env.el("model-price-hint").textContent, /价格源里没有匹配到这个模型：请手动填写价格/);
  assert.match(env.el("model-price-hint").className, /\bwarn\b/);
  env.el("input-model-price-in").value = "2";
  env.el("input-model-price-out").value = "8";
  env.el("model-form").dispatch("submit");
  await flush(env, 30);
  assert.deepEqual(posts(backend, "POST", "/models")[0].body, {
    model_id: "other/slash",
    input_price_per_m: 2,
    cache_read_price_per_m: null,
    cache_write_price_per_m: null,
    output_price_per_m: 8,
  });
  // an invalid cache price blocks the save with its own message
  env.el("discover-list").dispatch("change", { target: { value: "m-new" } });
  env.el("input-model-price-cache-read").value = "-1";
  env.el("model-form").dispatch("submit");
  await flush(env, 20);
  assert.match(env.el("model-form-error").textContent, /缓存读取价格必须是非负数字/);
});

test("both discover-models shapes work (per-model objects, or a `suggested` map next to the ids)", async () => {
  const { env } = await live({ status: "idle" }, { suggestions: { "m-new": SUGGEST }, discoveredShape: "map" });
  await openProviders(env);
  env.el("btn-discover-models").dispatch("click");
  await flush(env, 30);
  env.el("discover-list").dispatch("change", { target: { value: "m-new" } });
  assert.equal(env.el("input-model-price-cache-read").value, "0.07");
  assert.equal(env.el("input-model-ctx").value, "128000");
});

test("'自动匹配价格' asks the server for a hand-typed model id (GET /api/pricing/match)", async () => {
  const { env, backend } = await live({ status: "idle" }, { suggestions: { "typed-id": SUGGEST } });
  await openProviders(env);
  env.el("input-model-id").value = "typed-id";
  env.el("btn-match-price").dispatch("click");
  await flush(env, 30);
  const call = backend.calls.find((c) => c.url.includes("/api/pricing/match"));
  assert.ok(call);
  assert.match(call.url, /provider_id=p1&model_id=typed-id$/);
  assert.equal(env.el("input-model-price-in").value, "0.27");
  assert.equal(env.el("input-model-price-cache-read").value, "0.07");
  assert.match(env.el("model-price-hint").textContent, /自动填写/);
  // no match: fields cleared, hint says so
  env.el("input-model-id").value = "unknown-model";
  env.el("btn-match-price").dispatch("click");
  await flush(env, 30);
  assert.equal(env.el("input-model-price-in").value, "");
  assert.match(env.el("model-price-hint").textContent, /没有匹配到/);
  // no id typed yet
  env.el("input-model-id").value = "";
  env.el("btn-match-price").dispatch("click");
  await flush(env, 10);
  assert.match(env.el("model-price-hint").textContent, /请先填写模型 ID/);
});

test("editing an existing model prefills all four prices; the PATCH carries them", async () => {
  const { env, backend } = await live({ status: "idle" });
  await openProviders(env);
  env.el("btn-discover-models").dispatch("click");
  await flush(env, 30);
  env.el("discover-list").dispatch("change", { target: { value: "model-x" } });
  assert.equal(env.el("btn-submit-model").textContent, "保存价格");
  assert.equal(env.el("input-model-price-in").value, "0.5");
  assert.equal(env.el("input-model-price-cache-read").value, "0.1");
  assert.equal(env.el("input-model-price-cache-write").value, "");
  assert.match(env.el("model-price-hint").textContent, /价格来源：litellm:deepseek\/model-x/);
  env.el("input-model-price-cache-write").value = "0.6";
  env.el("model-form").dispatch("submit");
  await flush(env, 30);
  assert.deepEqual(posts(backend, "PATCH", "/models")[0].body, {
    model_id: "model-x",
    input_price_per_m: 0.5,
    cache_read_price_per_m: 0.1,
    cache_write_price_per_m: 0.6,
    output_price_per_m: 2,
    context_window_tokens: 128000,
  });
});

// =====================================================================================================
// platform detection and the phone layout
// =====================================================================================================
const attr = (env, name) => env.document.documentElement.getAttribute(name);

test("data-platform is set from the user agent, (pointer: coarse) and the viewport width — before and after the first paint", async () => {
  const cases = [
    [{ innerWidth: 1440, innerHeight: 900 }, ["desktop", "wide", "mouse", "landscape"]],
    [{ innerWidth: 1024, innerHeight: 768 }, ["desktop", "wide", "mouse", "landscape"]],
    [{ innerWidth: 900, innerHeight: 700 }, ["desktop", "compact", "mouse", "landscape"]],
    [{ innerWidth: 820, innerHeight: 1180, userAgent: IPAD, coarse: true, maxTouchPoints: 5 }, ["tablet", "compact", "touch", "portrait"]],
    [{ innerWidth: 1024, innerHeight: 768, userAgent: IPAD, coarse: true, maxTouchPoints: 5 }, ["tablet", "compact", "touch", "landscape"]],
    [{ innerWidth: 390, innerHeight: 844, userAgent: IPHONE, coarse: true, maxTouchPoints: 5 }, ["phone", "phone", "touch", "portrait"]],
    [{ innerWidth: 360, innerHeight: 800, userAgent: ANDROID, coarse: true, maxTouchPoints: 5 }, ["phone", "phone", "touch", "portrait"]],
    [{ innerWidth: 1440, innerHeight: 900, search: "?platform=phone" }, ["phone", "phone", "mouse", "landscape"]],
    [{ innerWidth: 390, innerHeight: 844, search: "?input=touch" }, ["phone", "phone", "touch", "portrait"]],
  ];
  for (const [opts, expected] of cases) {
    const { env } = await bootLive({}, opts);
    assert.deepEqual([attr(env, "data-platform"), attr(env, "data-layout"), attr(env, "data-input"), attr(env, "data-orientation")], expected, JSON.stringify(opts));
    assert.equal(env.app().platform().platform, expected[0]);
  }
});

test("rotating / resizing recomputes the layout, the avatar rail follows, and nothing is requested", async () => {
  const { env, backend } = await bootLive({}, { innerWidth: 1440, innerHeight: 900 });
  const grid = env.el("workspace-grid");
  assert.equal(attr(env, "data-layout"), "wide");
  assert.ok(!grid.classList.contains("narrow-layout"));
  const calls = backend.calls.length;
  env.sandbox.innerWidth = 900;
  env.sandbox.dispatchEvent("resize");
  assert.equal(attr(env, "data-layout"), "compact");
  assert.ok(grid.classList.contains("narrow-layout"), "the channel column became the avatar rail");
  env.sandbox.innerWidth = 390;
  env.sandbox.innerHeight = 844;
  env.sandbox.dispatchEvent("resize");
  assert.equal(attr(env, "data-platform"), "phone");
  assert.ok(!grid.classList.contains("narrow-layout"), "the phone has its own panes");
  env.sandbox.innerWidth = 1440;
  env.sandbox.innerHeight = 900;
  env.sandbox.dispatchEvent("resize");
  assert.equal(attr(env, "data-layout"), "wide");
  assert.equal(backend.calls.length, calls);
  assert.equal(env.app().state.platform.layout, "wide");
});

test("phone: a bottom tab bar (信息流 / 频道 / 玩家) switches the pane; the channels tab shows the unread total", async () => {
  const { env } = await live({}, {}, { innerWidth: 390, innerHeight: 844, userAgent: IPHONE, coarse: true, maxTouchPoints: 5 });
  const tabs = () => [...html(env, "tabbar").matchAll(/data-tab="(\w+)"[^>]*>/g)].map((m) => m[1]);
  assert.deepEqual(tabs(), ["feed", "channels", "players"]);
  assert.match(html(env, "tabbar"), /class="tab-btn active" role="tab" aria-selected="true" data-tab="feed"/);
  assert.equal(env.app().state.phoneTab, "feed");
  const tab = (id) => env.el("tabbar").dispatch("click", { target: target({ "[data-tab]": { "data-tab": id } }) });
  tab("players");
  assert.equal(env.el("main-container").getAttribute("data-phone-tab"), "players");
  assert.match(html(env, "tabbar"), /class="tab-btn active" role="tab" aria-selected="true" data-tab="players"/);
  tab("channels");
  assert.equal(env.el("main-container").getAttribute("data-phone-tab"), "channels");
  // unread messages elsewhere show up as a badge on the channels tab
  env.sse("message.committed", F.msg(41, "wolves", "N3", "x", "speech", ["moderator", ...F.WOLVES]));
  env.sse("message.committed", F.msg(42, "wolves", "N6", "y", "speech", ["moderator", ...F.WOLVES]));
  assert.match(html(env, "tabbar"), /data-tab="channels"[^>]*>[^]*<span class="tab-badge">2<\/span>/);
  // picking a channel there jumps to the feed
  env.el("channels-list").dispatch("click", { target: target({ ".channel-item": { "data-channel": "wolves" } }) });
  assert.equal(env.app().state.selectedChannel, "wolves");
  assert.equal(env.el("main-container").getAttribute("data-phone-tab"), "feed");
  assert.ok(!/tab-badge/.test(html(env, "tabbar")), "the wolves channel was read");
  // unknown tabs are ignored; changing tab closes an open drawer
  tab("nonsense");
  assert.equal(env.app().state.phoneTab, "feed");
  tab("players");
  cardClick(env, "N3");
  assert.equal(env.app().roster().openId(), "N3");
  tab("feed");
  assert.equal(env.app().roster().openId(), null);
});

test("phone: the detail panel is a bottom drawer with a backdrop; tapping the backdrop closes it; desktop has no backdrop", async () => {
  const phone = await live({}, {}, { innerWidth: 390, innerHeight: 844, userAgent: IPHONE, coarse: true, maxTouchPoints: 5 });
  cardClick(phone.env, "N5");
  assert.equal(phone.env.el("detail-panel").hidden, false);
  assert.equal(phone.env.el("detail-backdrop").hidden, false);
  phone.env.el("detail-backdrop").dispatch("click");
  assert.equal(phone.env.app().roster().openId(), null);
  assert.equal(phone.env.el("detail-backdrop").hidden, true);
  assert.equal(phone.env.el("detail-panel").hidden, true);

  const desk = await live({}, {}, { innerWidth: 1440, innerHeight: 900 });
  desk.env.el("participants-grid").querySelector = () => ({ getBoundingClientRect: () => ({ left: 300, top: 120, right: 380, bottom: 220, width: 80, height: 100 }) });
  cardClick(desk.env, "N5");
  assert.equal(desk.env.el("detail-panel").hidden, false);
  assert.equal(desk.env.el("detail-backdrop").hidden, true);
  assert.match(desk.env.el("detail-panel").style.left, /px$/, "positioned next to its card");
  assert.match(desk.env.el("detail-panel").style.top, /px$/);
  // the drawer CSS: bottom sheet, rounded top, grab handle
  assert.match(H.CSS_SRC, /html\[data-layout="phone"\] \.detail-panel \{[^}]*bottom: 0[^}]*border-radius: 18px 18px 0 0/);
  assert.match(H.CSS_SRC, /html\[data-layout="phone"\] \.detail-grab \{\s*display: block/);
});

test("phone: secondary top-bar buttons move into the ⋯ menu (the real elements, listeners intact) and come back on a wide screen", async () => {
  const backend = H.makeBackend();
  const env = H.makeEnv({ fetchImpl: backend.fetch, innerWidth: 1440, innerHeight: 900 });
  // give the buttons the homes they have in index.html
  const homes = {
    left: new FakeEl("div"),
    right: new FakeEl("div"),
    status: env.el("topbar-status"),
    boardRight: new FakeEl("div"),
  };
  const place = (home, ...ids) => ids.forEach((id) => home.appendChild(env.el(id)));
  // (v0.14, H27: the viewpoint label and the takeover bar both stay in the first row of the phone)
  place(homes.left, "btn-open-settings-modal", "btn-theme", "viewpoint-bar", "takeover-bar");
  place(homes.right, "btn-open-calllog", "usage-telemetry-pill", "btn-open-budget-modal", "btn-new", "btn-start", "btn-pause", "btn-resume", "btn-more");
  place(homes.boardRight, "btn-open-history-modal", "btn-open-bindings-modal", "btn-open-provider-modal");
  homes.status.appendChild(env.el("mod-chip"));
  env.run();
  await settle();
  env.flushRaf();
  const menu = env.el("more-menu-list");
  assert.equal(menu.children.length, 0, "desktop: the menu is empty");
  assert.equal(env.el("more-menu").hidden, true);

  // -> phone
  env.sandbox.innerWidth = 390;
  env.sandbox.innerHeight = 844;
  env.sandbox.dispatchEvent("resize");
  // (v0.13.1, G32: the error log's button moves too; its badge then shows on ⋯)
  const inMenu = ["btn-open-settings-modal", "btn-theme", "btn-open-budget-modal", "btn-open-calllog", "btn-new", "btn-open-history-modal", "btn-open-bindings-modal", "btn-open-provider-modal"];
  for (const id of inMenu) assert.equal(env.el(id).parentNode, menu, `${id} is in the menu`);
  assert.equal(menu.children.length, inMenu.length);
  assert.equal(env.el("takeover-bar").parentNode, homes.left, "the takeover bar stays in the first row of the phone (F3)");
  assert.equal(env.el("viewpoint-bar").parentNode, homes.left, "the viewpoint label stays in the first row of the phone too (H27)");
  for (const id of ["btn-start", "btn-pause", "btn-resume", "btn-more"]) assert.equal(env.el(id).parentNode, homes.right, `${id} stays on the bar`);
  assert.equal(env.el("usage-telemetry-pill").parentNode, homes.status, "the usage pill moves to the status row");
  assert.ok(homes.status.children.indexOf(env.el("usage-telemetry-pill")) < homes.status.children.indexOf(env.el("mod-chip")), "... before the moderator chip");

  // the ⋯ button toggles the menu; choosing an item closes it (except the theme switch)
  env.el("btn-more").dispatch("click");
  assert.equal(env.el("more-menu").hidden, false);
  assert.equal(env.el("btn-more").getAttribute("aria-expanded"), "true");
  env.el("more-menu").dispatch("click", { target: { closest: (sel) => (sel === "button" ? { id: "btn-theme" } : null) } });
  assert.equal(env.el("more-menu").hidden, false, "toggling the theme keeps the menu open");
  env.el("more-menu").dispatch("click", { target: { closest: (sel) => (sel === "button" ? { id: "btn-new" } : null) } });
  assert.equal(env.el("more-menu").hidden, true);
  env.el("btn-more").dispatch("click");
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.equal(env.el("more-menu").hidden, true);
  env.el("btn-more").dispatch("click");
  env.document.dispatchEvent("click", { target: target({}) });
  assert.equal(env.el("more-menu").hidden, true, "a tap outside closes it");
  // the moved buttons still work: their listeners were not lost
  env.el("btn-theme").dispatch("click");
  assert.equal(attr(env, "data-theme"), "dark");

  // -> desktop again: everything goes back where it was
  env.sandbox.innerWidth = 1440;
  env.sandbox.innerHeight = 900;
  env.sandbox.dispatchEvent("resize");
  assert.equal(menu.children.length, 0);
  for (const id of ["btn-open-settings-modal", "btn-theme", "viewpoint-bar", "takeover-bar"]) assert.equal(env.el(id).parentNode, homes.left, id);
  for (const id of ["usage-telemetry-pill", "btn-open-budget-modal", "btn-new"]) assert.equal(env.el(id).parentNode, homes.right, id);
  for (const id of ["btn-open-history-modal", "btn-open-bindings-modal", "btn-open-provider-modal"]) assert.equal(env.el(id).parentNode, homes.boardRight, id);
  assert.equal(env.el("more-menu").hidden, true);
});

test("phone CSS contract: only title / state / start-pause-resume stay on the bar; modals are full screen; tap targets are >= 40 px", () => {
  const css = H.CSS_SRC;
  assert.match(css, /html\[data-layout="phone"\] \.usage-group \{\s*display: none/);
  assert.match(css, /html\[data-layout="phone"\] #btn-start:disabled,\s*html\[data-layout="phone"\] #btn-pause:disabled,\s*html\[data-layout="phone"\] #btn-resume:disabled \{\s*display: none/);
  assert.match(css, /html\[data-layout="phone"\] \.btn-more \{\s*display: inline-flex/);
  assert.match(css, /html\[data-layout="phone"\] \.modal-card,\s*html\[data-layout="phone"\] \.modal-card\.modal-wide \{[^}]*width: 100vw[^}]*height: 100dvh[^}]*border-radius: 0/);
  assert.match(css, /html\[data-layout="phone"\] \.tabbar \{\s*display: flex/);
  assert.match(css, /html\[data-layout="phone"\] \.main-container\[data-phone-tab="channels"\] > \.board-section/);
  assert.match(css, /html\[data-input="touch"\] \{\s*--tap: 40px/);
  assert.match(css, /html\[data-input="touch"\] \.btn-xs \{\s*min-height: 40px/);
  assert.match(css, /\.tab-btn \{[^}]*min-height: 56px/);
  assert.match(css, /image-rendering: pixelated/);
  // the breakpoint table of the plan: 12-column cards when wide, 6x2 when compact, 6x2 minis + tabs on a phone
  assert.match(css, /\.participants-grid \{\s*display: grid;\s*grid-template-columns: repeat\(12, minmax\(0, 1fr\)\)/);
  assert.match(css, /html\[data-layout="compact"\] \.participants-grid \{\s*grid-template-columns: repeat\(6, minmax\(0, 1fr\)\)/);
  assert.match(css, /html\[data-layout="phone"\] \.main-container\[data-phone-tab="feed"\] \.participants-grid \{\s*grid-template-columns: repeat\(6, minmax\(0, 1fr\)\)/);
  assert.match(css, /html\[data-layout="phone"\] \.main-container\[data-phone-tab="players"\] \.participants-grid \{\s*grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/);
});

// ---- runner -----------------------------------------------------------------------------------------------
H.runTests(tests);
