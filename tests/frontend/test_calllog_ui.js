"use strict";
// v0.13.1 (G32-G36, contract §12.3): the error log (static/js/calllog.js) and the red badge of the top bar
//   the badge       unread timeout / error lines (a retry or a cancel is no error), kept per game in localStorage, on a phone also on ⋯
//   the window      one line per model call and runtime error, newest first, a line that went wrong opens to its details
//   the filters     all / errors only, by role;  "全部标为已读";  llm.call appends live (a gap in the ids, or a line that does not fit,
//                   makes the page read the log again: what it keeps of it, never only "the lines after the last id")
//   the reads       numbered: only the newest one counts; recoveries are bounded (one at once, then spaced, doubling while they find nothing)
// Run: node tests/frontend/test_calllog_ui.js
const assert = require("node:assert/strict");
const path = require("node:path");
const H = require("./harness.js");
const CallLog = require(path.join(__dirname, "..", "..", "static", "js", "calllog.js"));

const { F, Core, Store, View, target, settle, bootLive } = H;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

const IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1";

const LOG = [
  F.makeCall(1, { actor_id: "moderator", actor_name: "主持人", model_id: "deepseek-chat" }),
  F.makeCall(2, { actor_id: "N3" }),
  F.makeFailedCall(3, { status: "retry", actor_id: "N3", attempt: 1 }),
  F.makeCall(4, { actor_id: "N3", attempt: 2 }),
  F.makeFailedCall(5, { status: "error", actor_id: "N5", actor_name: "Eve", attempt: 3, message: "Rate limit <b>reached</b> for requests" }),
  F.makeFailedCall(6, { kind: "event", status: "error", actor_id: "N5", actor_name: "Eve", purpose: "runtime", attempt: null, provider_id: null, model_id: null, message: "Eve 的这次行动失败，已放弃" }),
  F.makeFailedCall(7, { status: "timeout", actor_id: "N6", actor_name: "Frank", http_status: null, error_class: "TimeoutError", message: "调用超过 120 秒仍未返回", elapsed_ms: 120000 }),
  F.makeCall(8, { actor_id: "N4", actor_name: "David", purpose: "review", elapsed_ms: 8400 }),
  F.makeCall(9, { actor_id: "roster", actor_name: "名册", purpose: "refine" }),
  F.makeCall(10, { status: "cancelled", actor_id: "N7", actor_name: "Grace", elapsed_ms: 300, finish_reason: null, prompt_tokens: null, completion_tokens: null }),
];
const clone = (v) => JSON.parse(JSON.stringify(v));

async function live(over = {}, overrides = {}, envOpts = {}) {
  const session = F.makeSession({ event_id: 500, ...over });
  const ctx = await bootLive({ session, llmCalls: clone(LOG), ...overrides }, envOpts);
  ctx.env.sse("snapshot", { session }, 500);
  await settle(20);
  ctx.env.flushRaf();
  return ctx;
}

const flush = async (env, ms = 30) => {
  await settle(ms);
  env.flushRaf();
};
const html = (env, id) => env.el(id).innerHTML;
const unesc = (s) => s.replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
const textOf = (s) => unesc(s.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim());
const ui = (env) => env.app().calllog();
const win = (env) => env.el("calllog-window");
const badge = (env) => ({ hidden: env.el("calllog-badge").hidden, text: env.el("calllog-badge").textContent, more: env.el("more-badge").textContent, moreHidden: env.el("more-badge").hidden });
const rowIds = (env) => [...html(env, "calllog-list").matchAll(/<div class="cl-row[^"]*" data-call-id="(\d+)"/g)].map((m) => Number(m[1]));
const rowOf = (env, id) => {
  const piece = html(env, "calllog-list").split('<div class="cl-row').slice(1).find((r) => r.includes(`data-call-id="${id}"`));
  return piece === undefined ? "" : `<div class="cl-row${piece}`;
};
const sub = (env) => env.el("calllog-sub").textContent; // (set as text, not as HTML)
// (arrays made inside the page's sandbox are of another realm: deepEqual wants them copied first)
const ids = (list) => Array.from(list, (c) => c.id);
const rowClick = (env, id) => env.el("calllog-list").dispatch("click", { target: target({ "[data-calllog-action]": { "data-calllog-action": "toggle", "data-call-id": String(id) } }) });
const toolbarClick = (env, action, extra = {}) => env.el("calllog-toolbar").dispatch("click", { target: target({ "[data-calllog-action]": { "data-calllog-action": action, ...extra } }) });
const pickActor = (env, value) => env.el("calllog-toolbar").dispatch("change", { target: { id: "select-calllog-actor", value } });
const open = async (env) => {
  env.el("btn-open-calllog").dispatch("click");
  await flush(env);
};
const callEvent = (env, call, errors) => env.sse("llm.call", { call, errors: errors === undefined ? 0 : errors });
const queries = (backend) => backend.llmCallRequests || [];
const session = () => F.makeSession();
// (time passes: the recoveries - "read the log again to make sure" - are spaced a few seconds apart, the first one after a quiet time is not)
const later = (env) => Object.assign(ui(env)._state, { lastFetchAt: 0, lastRecoverAt: 0, recoverStreak: 0 });
/** boot + snapshot like live(), with the answers of the requests `hold(key, url)` accepts parked: gate.parked, gate.release(i), gate.releaseAll() */
async function liveHeld(over = {}, overrides = {}, hold = (key) => key.includes("api/llm/calls")) {
  const session = F.makeSession({ event_id: 500, ...over });
  const backend = H.makeBackend({ session, llmCalls: clone(LOG), ...overrides });
  const gate = backend.hold(hold);
  const env = H.makeEnv({ fetchImpl: backend.fetch });
  env.xhrHooks.auto = backend.uploadHandler;
  env.run();
  await settle();
  env.flushRaf();
  env.runNextTimer(); // the delayed first connect
  env.sse("snapshot", { session }, session.event_id);
  await settle(20);
  env.flushRaf();
  return { env, backend, gate };
}

// =====================================================================================================
// pure helpers
// =====================================================================================================
test("what counts as an error: timeout and error (the red badge); what 'only errors' shows: retries too; a cancel is neither", () => {
  const status = (s) => ({ status: s });
  assert.deepEqual(["ok", "retry", "timeout", "error", "cancelled"].map((s) => CallLog.isError(status(s))), [false, false, true, true, false]);
  assert.deepEqual(["ok", "retry", "timeout", "error", "cancelled"].map((s) => CallLog.isFailure(status(s))), [false, true, true, true, false]);
  assert.equal(CallLog.isError(null), false);
  assert.equal(CallLog.isFailure(undefined), false);
  const calls = LOG.map(Store.normalizeCall);
  assert.equal(CallLog.unreadErrors(calls, 0), 3, "ids 5, 6 and 7");
  assert.equal(CallLog.unreadErrors(calls, 5), 2);
  assert.equal(CallLog.unreadErrors(calls, 6), 1);
  assert.equal(CallLog.unreadErrors(calls, 7), 0);
  assert.equal(CallLog.unreadErrors(calls, "x"), 3, "a broken read mark means nothing was read");
  assert.equal(CallLog.unreadErrors([], 0), 0);
  assert.equal(CallLog.unreadErrors(null, 0), 0);
  assert.deepEqual(CallLog.countCalls(calls), { total: 10, errors: 3, retries: 1 });
  assert.deepEqual(CallLog.countCalls(null), { total: 0, errors: 0, retries: 0 });
});

test("filters: all / errors only (retries included), by role, both together; the order is kept", () => {
  const calls = LOG.map(Store.normalizeCall);
  const ids = (f) => CallLog.filterCalls(calls, f).map((c) => c.id);
  assert.deepEqual(ids({}), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  assert.deepEqual(ids({ errorsOnly: true }), [3, 5, 6, 7]);
  assert.deepEqual(ids({ actor: "N3" }), [2, 3, 4]);
  assert.deepEqual(ids({ actor: "all" }), ids({}));
  assert.deepEqual(ids({ errorsOnly: true, actor: "N5" }), [5, 6]);
  assert.deepEqual(ids({ errorsOnly: true, actor: "N8" }), []);
  assert.deepEqual(CallLog.filterCalls(null, {}), []);
});

test("elapsed time and game time read naturally", () => {
  assert.equal(CallLog.formatElapsed(850), "850 ms");
  assert.equal(CallLog.formatElapsed(1234), "1.2 s");
  assert.equal(CallLog.formatElapsed(9999), "10.0 s");
  assert.equal(CallLog.formatElapsed(34000), "34 s");
  assert.equal(CallLog.formatElapsed(125000), "2 分 05 秒");
  assert.equal(CallLog.formatElapsed(null), "");
  assert.equal(CallLog.formatElapsed(-5), "");
  assert.equal(CallLog.formatGameTime(0), "0:00");
  assert.equal(CallLog.formatGameTime(125.9), "2:05");
  assert.equal(CallLog.formatGameTime(3725), "1:02:05");
  assert.equal(CallLog.formatGameTime(null), "");
  assert.match(CallLog.formatFullTime("2026-11-18T10:03:12Z"), /^2026-11-\d\d \d\d:\d\d:\d\d$/);
  assert.equal(CallLog.formatFullTime("nonsense"), "");
});

test("a line names its role the way the plan says: 'N3 Carol' (the live nickname) / 主持人 / 复盘 · … / 完善; the result chip tells a runtime error from a call", () => {
  const s = F.makeSession();
  const lbl = (c) => CallLog.roleLabel(Store.normalizeCall(c), s, View);
  assert.equal(lbl(F.makeCall(1, { actor_id: "N3", actor_name: "OldName" })), "N3 Carol", "the roster's nickname wins over the one frozen into the line");
  assert.equal(lbl(F.makeCall(1, { actor_id: "moderator", actor_name: "主持人" })), "主持人");
  assert.equal(lbl(F.makeCall(1, { actor_id: "N4", purpose: "review" })), "复盘 · N4 David");
  assert.equal(lbl(F.makeCall(1, { actor_id: "roster", purpose: "refine" })), "完善");
  assert.equal(lbl(F.makeCall(1, { actor_id: "engine", actor_name: "系统", purpose: "runtime", kind: "event" })), "系统");
  assert.equal(lbl(F.makeCall(1, { actor_id: "N99", actor_name: "N99" })), "N99", "an unknown seat: its id");
  assert.equal(lbl(F.makeCall(1, { actor_id: "N99", actor_name: "Zed" })), "N99 Zed");
  const info = (c) => CallLog.statusInfo(Store.normalizeCall(c));
  assert.deepEqual([info(F.makeCall(1)).label, info(F.makeCall(1)).cls], ["成功", "ok"]);
  assert.equal(info(F.makeCall(1, { status: "retry" })).label, "重试");
  assert.equal(info(F.makeCall(1, { status: "timeout" })).label, "超时");
  assert.equal(info(F.makeCall(1, { status: "error" })).label, "失败");
  assert.equal(info(F.makeCall(1, { status: "cancelled" })).label, "取消");
  assert.equal(info(F.makeCall(1, { kind: "event", status: "error" })).label, "运行错误", "a tool that raised is no failed request");
  assert.equal(info(F.makeCall(1, { kind: "event", status: "retry" })).label, "将重试");
  assert.equal(info({ ...F.makeCall(1), status: "mystery" }).cls, "error", "a status nobody knows must not look like a success");
});

test("the actors in the role filter: the moderator, the players by seat, then the moderator's helpers; models, tokens and the reason column", () => {
  const s = F.makeSession();
  const opts = CallLog.actorOptions([F.makeCall(1, { actor_id: "roster" }), F.makeCall(2, { actor_id: "N12" }), F.makeCall(3, { actor_id: "N3" }), F.makeCall(4, { actor_id: "moderator" }), F.makeCall(5, { actor_id: "N3" })].map(Store.normalizeCall), s, View);
  assert.deepEqual(opts.map((o) => o.id), ["moderator", "N3", "N12", "roster"]);
  assert.deepEqual(opts.map((o) => o.label), ["主持人", "N3 Carol", "N12 Leo", "名册"]);
  const c = (o) => Store.normalizeCall(F.makeCall(1, o));
  assert.equal(CallLog.modelText(c({})), "deepseek / deepseek-chat");
  assert.equal(CallLog.modelText(c({ provider_id: null })), "deepseek-chat");
  assert.equal(CallLog.modelText(c({ provider_id: null, model_id: null })), "");
  assert.equal(CallLog.inputTokens(c({ prompt_tokens: 100, cache_read_tokens: 50, cache_write_tokens: 10 })), 160, "every token that was read, cached or written");
  assert.equal(CallLog.inputTokens(c({ prompt_tokens: null, cache_read_tokens: null, cache_write_tokens: null })), null);
  assert.equal(CallLog.reasonText(c({})), "stop");
  assert.equal(CallLog.reasonText(c({ status: "error", http_status: 429 })), "HTTP 429");
  assert.equal(CallLog.reasonText(c({ status: "timeout", http_status: null, error_class: "TimeoutError" })), "TimeoutError");
  assert.equal(CallLog.reasonText(c({ status: "cancelled", http_status: null, error_class: null })), "已取消");
});

test("an opened line says error type, HTTP status, the attempt, the scrubbed message, the tokens - only what is known - and every value is escaped", () => {
  const failed = Store.normalizeCall(LOG[4]);
  const pairs = Object.fromEntries(CallLog.detailPairs(failed, session(), View, Core));
  assert.equal(pairs["类型"], "模型请求");
  assert.equal(pairs["角色"], "N5 Eve");
  assert.equal(pairs["Provider / 模型"], "deepseek / deepseek-chat");
  assert.equal(pairs["第几次尝试"], "第 3 次");
  assert.equal(pairs["HTTP 状态"], "429");
  assert.equal(pairs["错误类型"], "RateLimitError");
  assert.equal(pairs["错误信息"], "Rate limit <b>reached</b> for requests");
  assert.ok(!("token" in pairs), "a failed request has no numbers");
  assert.equal(CallLog.detailPairs(Store.normalizeCall(LOG[2]), session(), View, Core).find((p) => p[0] === "第几次尝试")[1], "第 1 次（失败，随后重试）");
  const ok = Object.fromEntries(CallLog.detailPairs(Store.normalizeCall(F.makeCall(2, { cache_read_tokens: 5000, cache_write_tokens: 0 })), session(), View, Core));
  assert.equal(ok["token"], "未命中输入 1,000 · 缓存读取 5,000 · 缓存写入 0 · 输出 200");
  assert.equal(ok["耗时"], "1.2 s");
  assert.equal(ok["结束原因"], "stop");
  assert.equal(ok["用途"], "对局");
  const event = Object.fromEntries(CallLog.detailPairs(Store.normalizeCall(LOG[5]), session(), View, Core));
  assert.equal(event["类型"], "运行错误（不是模型请求）");
  assert.equal(event["用途"], "运行");
  assert.ok(!("第几次尝试" in event) && !("Provider / 模型" in event));
  // the row: escaped, a red dot for an unread error, the details only when opened
  const row = CallLog.rowHtml(failed, { Core, View, session: session(), expanded: true, unread: true });
  assert.match(row, /^<div class="cl-row cl-error unread open" data-call-id="5">/);
  assert.match(row, /<dd class="cl-message">Rate limit &lt;b&gt;reached&lt;\/b&gt; for requests<\/dd>/);
  assert.ok(!row.includes("<b>reached</b>"));
  assert.match(row, /<div class="cl-wide"><dt>错误信息<\/dt>/);
  const closed = CallLog.rowHtml(failed, { Core, View, session: session(), expanded: false, unread: false });
  assert.doesNotMatch(closed, /cl-detail|unread|open/);
  assert.match(closed, /aria-expanded="false"/);
  assert.match(closed, /<span class="cl-status st-error">失败<\/span>/);
  assert.match(closed, /<span class="cl-reason" title="结束原因 \/ 状态">HTTP 429<\/span>/);
  assert.match(closed, /<span class="cl-num cl-elapsed" title="耗时">900 ms<\/span>/);
  assert.match(closed, /<span class="cl-num cl-tokens"[^>]*><\/span>/, "no numbers, none shown");
  assert.match(CallLog.rowHtml(Store.normalizeCall(LOG[1]), { Core, View, session: session(), expanded: false, unread: false }), /<span class="cl-num cl-tokens"[^>]*>1,000 \/ 200<\/span>/);
});

test("the read mark: per game in localStorage - the contract's map of ids, plus the witness of each id beside it; the last 50 games stay; a broken or missing storage never throws", () => {
  const mem = new Map();
  const storage = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, String(v)) };
  const WITNESS = "2026-11-18T16:00:17.000Z";
  assert.equal(CallLog.READ_KEY, "arena_calllog_read_v1");
  assert.equal(CallLog.ANCHOR_KEY, "arena_calllog_anchor_v1");
  assert.deepEqual(CallLog.readMarkOf(storage, "g1"), { id: 0, ts: "" });
  CallLog.saveReadMark(storage, "g1", { id: 17, ts: WITNESS });
  CallLog.saveReadMark(storage, "g2", { id: 4, ts: "" });
  assert.deepEqual(CallLog.readMarkOf(storage, "g1"), { id: 17, ts: WITNESS });
  assert.deepEqual(CallLog.readMarkOf(storage, "g2"), { id: 4, ts: "" }, "a mark without a witness (kept by an older page): the id alone");
  // the contract's map (§12.7) is exactly {"<session_id>": <last_read_id>}; the witnesses live in a map of their own
  assert.deepEqual(JSON.parse(mem.get("arena_calllog_read_v1")), { g1: 17, g2: 4 });
  assert.deepEqual(JSON.parse(mem.get("arena_calllog_anchor_v1")), { g1: WITNESS });
  CallLog.saveReadMark(storage, "g1", { id: 20, ts: WITNESS }); // the game used last is the newest key
  assert.deepEqual(Object.keys(JSON.parse(mem.get("arena_calllog_read_v1"))), ["g2", "g1"]);
  CallLog.saveReadMark(storage, "g1", { id: 21, ts: "" }); // a mark without a witness replaces the old witness (it was about another line)
  assert.deepEqual(JSON.parse(mem.get("arena_calllog_anchor_v1")), {});
  for (let i = 0; i < 60; i++) CallLog.saveReadMark(storage, `old-${i}`, { id: i + 1, ts: `2026-11-18T16:01:${String(i).padStart(2, "0")}.000Z` });
  const kept = JSON.parse(mem.get("arena_calllog_read_v1"));
  assert.equal(Object.keys(kept).length, CallLog.READ_KEEP);
  assert.equal(CallLog.READ_KEEP, 50);
  assert.ok(!("g1" in kept) && "old-59" in kept, "the oldest went");
  const witnesses = JSON.parse(mem.get("arena_calllog_anchor_v1"));
  assert.equal(Object.keys(witnesses).length, CallLog.READ_KEEP, "...and its witness with it");
  assert.ok(Object.keys(witnesses).every((k) => k in kept));
  // broken or nonsensical values: nothing read; the next save repairs them
  mem.set("arena_calllog_read_v1", "{not json");
  assert.deepEqual(CallLog.readMarkOf(storage, "g1"), { id: 0, ts: "" });
  CallLog.saveReadMark(storage, "g1", { id: 3, ts: WITNESS });
  assert.deepEqual(CallLog.readMarkOf(storage, "g1"), { id: 3, ts: WITNESS });
  mem.set("arena_calllog_read_v1", JSON.stringify({ g1: "abc", g2: -4, g3: 5.7, g4: 9 }));
  mem.set("arena_calllog_anchor_v1", JSON.stringify({ g3: 12345, g4: "2026-11-18T16:00:09.000Z" }));
  assert.deepEqual(CallLog.readMarkOf(storage, "g1"), { id: 0, ts: "" });
  assert.deepEqual(CallLog.readMarkOf(storage, "g2"), { id: 0, ts: "" });
  assert.deepEqual(CallLog.readMarkOf(storage, "g3"), { id: 5, ts: "" }, "a witness that is no text is no witness");
  assert.deepEqual(CallLog.readMarkOf(storage, "g4"), { id: 9, ts: "2026-11-18T16:00:09.000Z" });
  mem.set("arena_calllog_anchor_v1", "[1,2]");
  assert.deepEqual(CallLog.readMarkOf(storage, "g4"), { id: 9, ts: "" });
  assert.deepEqual(CallLog.readMarkOf(null, "g1"), { id: 0, ts: "" });
  CallLog.saveReadMark(null, "g1", { id: 3, ts: WITNESS });
  CallLog.saveReadMark(storage, "", { id: 3, ts: WITNESS });
  const throwing = { getItem: () => { throw new Error("denied"); }, setItem: () => { throw new Error("denied"); } };
  assert.deepEqual(CallLog.readMarkOf(throwing, "g1"), { id: 0, ts: "" });
  CallLog.saveReadMark(throwing, "g1", { id: 3, ts: WITNESS });
});

test("Store.normalizeCall: every field present and typed (null = not known); an unknown status is an error; no id, no line", () => {
  const n = Store.normalizeCall({ id: "7", ts: "t", gt: "12.5", kind: "event", status: "retry", actor_id: "N3", actor_name: "Carol", purpose: "runtime", attempt: 2, elapsed_ms: "800", http_status: 503, error_class: "X", message: "m", extra: "dropped" });
  assert.deepEqual(n, {
    id: 7, ts: "t", gt: 12.5, kind: "event", status: "retry", actor_id: "N3", actor_name: "Carol", purpose: "runtime", provider_id: null, model_id: null, attempt: 2,
    elapsed_ms: 800, prompt_tokens: null, completion_tokens: null, cache_read_tokens: null, cache_write_tokens: null, finish_reason: null, http_status: 503, error_class: "X", message: "m",
  });
  assert.equal(Store.normalizeCall({ id: 1, status: "mystery" }).status, "error");
  assert.equal(Store.normalizeCall({ id: 1, purpose: "x" }).purpose, "turn");
  assert.equal(Store.normalizeCall({ id: 1, kind: "x" }).kind, "call");
  assert.equal(Store.normalizeCall({ status: "ok" }), null);
  assert.equal(Store.normalizeCall({ id: 0 }), null);
  assert.equal(Store.normalizeCall(null), null);
  assert.equal(Store.normalizeCall([]), null);
  // the event: an ordinary one that carries the line and the error counter; it changes nothing in the game's session
  const s = Store.emptySession();
  const before = JSON.stringify(s);
  const res = Store.applyEvent(s, { id: 9, type: "llm.call", data: { call: LOG[4], errors: 3 } }, null);
  assert.deepEqual([res.llmCall.call.id, res.llmCall.errors], [5, 3]);
  assert.equal(JSON.stringify(res.session), before, "the game's state is untouched");
  assert.equal(Object.values(res.dirty).some(Boolean), false, "and nothing needs repainting");
  assert.equal(Store.applyEvent(s, { id: 9, type: "llm.call", data: { call: { status: "ok" } } }, null).llmCall, null, "a line without an id is dropped");
  assert.equal(Store.applyEvent(s, { id: 9, type: "llm.call", data: {} }, null).llmCall, null);
});

// =====================================================================================================
// the badge (G32)
// =====================================================================================================
test("at start-up only the failed lines are asked for (errors_only); the badge counts the unread errors - a retry or a cancel is no error", async () => {
  const { env, backend } = await live({ status: "running" });
  assert.deepEqual(queries(backend), [{ errors_only: "true", limit: "2000" }]);
  assert.deepEqual(badge(env), { hidden: false, text: "3", more: "3", moreHidden: false });
  assert.match(env.el("calllog-badge").getAttribute("title"), /3 条未读错误/);
  assert.match(env.el("btn-open-calllog").getAttribute("title"), /错误日志：全部 LLM 调用与报错（3 条未读错误）/);
  assert.equal(win(env).hidden, true, "the window waits to be opened");
  assert.ok(H.HTML_SRC.includes('id="btn-open-calllog"') && H.HTML_SRC.includes('id="calllog-badge" hidden'));
  // nothing wrong: no badge, a plain tooltip
  const calm = await live({ status: "running" }, { llmCalls: [LOG[0], LOG[1], LOG[2], LOG[9]].map(clone) });
  assert.equal(calm.env.el("calllog-badge").hidden, true);
  assert.equal(calm.env.el("more-badge").hidden, true);
  assert.equal(calm.env.el("btn-open-calllog").getAttribute("title"), "错误日志：全部 LLM 调用与报错");
  // the badge reads 99+ beyond that
  const many = await live({ status: "running" }, { llmCalls: Array.from({ length: 120 }, (_, i) => F.makeFailedCall(i + 1, { status: "error" })) });
  assert.equal(many.env.el("calllog-badge").textContent, "99+");
  assert.match(many.env.el("calllog-badge").getAttribute("title"), /120 条未读错误/);
});

test("an older server (no such endpoint) leaves the button there without a badge and without noise", async () => {
  const { env } = await live({ status: "running" }, { callsFailure: [404, "Not Found"] });
  assert.equal(env.el("calllog-badge").hidden, true);
  await open(env);
  assert.equal(win(env).hidden, false);
  assert.match(textOf(html(env, "calllog-list")), /Not Found|找不到/);
});

test("llm.call events: an error raises the badge, an ok / retry / cancel does not; the line is added to the log without a request", async () => {
  const { env, backend } = await live({ status: "running" });
  const asked = queries(backend).length;
  callEvent(env, F.makeCall(11, { actor_id: "N3" }), 3);
  callEvent(env, F.makeFailedCall(12, { status: "retry" }), 3);
  callEvent(env, F.makeCall(13, { status: "cancelled" }), 3);
  assert.equal(badge(env).text, "3");
  callEvent(env, F.makeFailedCall(14, { status: "timeout", http_status: null }), 4);
  assert.equal(badge(env).text, "4");
  callEvent(env, F.makeFailedCall(15, { kind: "event", status: "error", purpose: "runtime", attempt: null }), 5);
  assert.equal(badge(env).text, "5");
  assert.equal(queries(backend).length, asked, "consecutive lines need no request");
  assert.equal(ui(env).calls().length >= 8, true);
  // a repeated event (the stream resumed after a reconnect) is not counted twice
  callEvent(env, F.makeFailedCall(15, { kind: "event", status: "error", purpose: "runtime", attempt: null }), 5);
  assert.equal(badge(env).text, "5");
  // the event does not touch the game
  assert.equal(env.app().state.session.calls, undefined);
});

test("a gap in the ids (llm.call may be dropped) is filled by reading the log again - what the page keeps of it, not just the lines after the last id", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.llmCalls.push(F.makeCall(11), F.makeFailedCall(12, { status: "error" }), F.makeFailedCall(13, { status: "timeout", http_status: null }));
  const asked = queries(backend).length;
  callEvent(env, backend.llmCalls[12], 4); // the event of line 13: 11 and 12 never came
  await flush(env);
  // the window is closed: the page keeps the failed lines (the badge needs nothing else) - one read, of all of them, from the start
  assert.deepEqual(queries(backend).slice(asked), [{ errors_only: "true", limit: "2000" }]);
  assert.equal(badge(env).text, "5", "ids 5, 6, 7, 12 and 13");
  assert.deepEqual(ids(ui(env).calls()).filter((id) => id > 10), [12, 13]);
  assert.equal(ui(env)._state.lastId, 13);
  // with the window open the page keeps every line: the same gap is filled with a read of the whole log
  await open(env);
  later(env);
  backend.llmCalls.push(F.makeCall(14), F.makeCall(15, { status: "cancelled" }));
  const asked2 = queries(backend).length;
  callEvent(env, backend.llmCalls[14], 4); // 14 never came
  await flush(env);
  assert.deepEqual(queries(backend).slice(asked2), [{ limit: "2000" }]);
  assert.deepEqual(ids(ui(env).calls()), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]);
  assert.equal(ui(env)._state.full, true);
  // the first event of a game that came before we knew anything is simply taken
  const fresh = await live({ status: "running", session_id: "game-F" }, { llmCalls: [] });
  callEvent(fresh.env, F.makeCall(7), 0);
  assert.equal(queries(fresh.backend).length, 1, "(only the first read of the page)");
  assert.deepEqual(ids(ui(fresh.env).calls()), [7]);
});

test("the unread mark: 全部标为已读 empties the badge and is remembered per game; a new error after it counts again; the next page load starts from it", async () => {
  const { env, backend } = await live({ status: "running", session_id: "game-A" });
  await open(env);
  toolbarClick(env, "mark-read");
  assert.equal(badge(env).hidden, true);
  assert.equal(badge(env).moreHidden, true);
  // the mark is the newest FAILED line (every error is at or before it) - id 7, the timeout - and the time of that line, its witness
  assert.deepEqual(JSON.parse(env.storage.get("arena_calllog_read_v1")), { "game-A": 7 });
  assert.deepEqual(JSON.parse(env.storage.get("arena_calllog_anchor_v1")), { "game-A": LOG[6].ts });
  backend.llmCalls.push(F.makeFailedCall(11, { status: "error" })); // (the server's log gets the line, and the stream tells the page)
  callEvent(env, backend.llmCalls[10], 4);
  assert.equal(badge(env).text, "1");
  // a reload of the page: the mark is read back, so the old errors stay read
  const second = await live({ status: "running", session_id: "game-A" }, { llmCalls: clone(backend.llmCalls) }, { storage: Object.fromEntries(env.storage) });
  assert.equal(second.env.el("calllog-badge").textContent, "1", "only the error that came after the mark");
  // another game has its own mark
  const third = await live({ status: "running", session_id: "game-B" }, { llmCalls: clone(LOG) }, { storage: Object.fromEntries(env.storage) });
  assert.equal(third.env.el("calllog-badge").textContent, "3");
});

// =====================================================================================================
// a REBUILT log: after a restart / a load / an import the server rebuilds the log of a game from its trace and numbers the
// lines from 1 again (contract §12.3) - "read up to id 50" must not hide the new error that now has id 3
// =====================================================================================================
const at = (sec, ms = 0) => `2026-11-18T16:${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(sec % 60).padStart(2, "0")}.${String(ms).padStart(3, "0")}Z`;
const shift = (ts, ms) => new Date(Date.parse(ts) + ms).toISOString();
const mk = (id, sec, status = "ok", ms = 0) => (status === "ok" ? F.makeCall(id, { ts: at(sec, ms) }) : F.makeFailedCall(id, { status, ts: at(sec, ms) }));
/** The log of a game as the live server had it: 60 lines, 4 seconds apart; a retry (id 35) and two errors (ids 20 and 50). */
const OLD_LOG = () => Array.from({ length: 60 }, (_, i) => mk(i + 1, (i + 1) * 4, { 20: "error", 35: "retry", 50: "error" }[i + 1] || "ok"));
/**
 * What the server answers after it rebuilt that log from the trace: the lines it does not trace (`drop`: a call an agent threw away)
 * are gone, the failed ones carry the time of their trace record (a few ms off the live line's), the rest is numbered from 1 again;
 * `extra` are the lines that came after the restart.
 */
const rebuilt = (old, drop, extra = []) =>
  old
    .filter((c) => !drop(c))
    .map((c) => (c.status === "ok" ? c : { ...c, ts: shift(c.ts, 3) }))
    .concat(extra)
    .map((c, i) => ({ ...c, id: i + 1 }));
const marked = async (log, sessionId = "game-A") => {
  // a page that read the log and pressed 全部标为已读: what it leaves in the browser's storage
  const { env } = await live({ status: "running", session_id: sessionId }, { llmCalls: log });
  await open(env);
  toolbarClick(env, "mark-read");
  assert.equal(badge(env).hidden, true);
  return Object.fromEntries(env.storage);
};
const reload = (log, storage, sessionId = "game-A") => live({ status: "running", session_id: sessionId }, { llmCalls: log }, { storage });
const stored = (env) => ({ read: JSON.parse(env.storage.get("arena_calllog_read_v1") || "{}"), anchor: JSON.parse(env.storage.get("arena_calllog_anchor_v1") || "{}") });

test("a page reload of the SAME log keeps what was read: the mark still means the line it was taken from; a new error after it counts", async () => {
  const old = OLD_LOG();
  const storage = await marked(old);
  assert.deepEqual(JSON.parse(storage.arena_calllog_read_v1), { "game-A": 50 });
  assert.deepEqual(JSON.parse(storage.arena_calllog_anchor_v1), { "game-A": at(200) });
  const { env, backend } = await reload(old, storage);
  assert.equal(badge(env).hidden, true, "the old errors stay read after an ordinary refresh");
  assert.deepEqual(stored(env).read, { "game-A": 50 }, "nothing was re-based");
  backend.llmCalls.push(mk(61, 300, "error"));
  callEvent(env, backend.llmCalls[60], 3);
  assert.equal(badge(env).text, "1");
  assert.deepEqual(stored(env).read, { "game-A": 50 }, "an ordinary append leaves the mark alone");
});

test("after a restart the log is rebuilt and numbered from 1 again - SHORTER than the mark: the old errors stay read, the NEW error (a low id) lights the badge", async () => {
  const old = OLD_LOG();
  const storage = await marked(old);
  // 20 lines are gone (the trace does not have them): 40 are left, and the one error that happened after the restart is line 41
  const log = rebuilt(old, (c) => c.status === "ok" && c.id % 3 === 0, [mk(0, 300, "error")]);
  assert.equal(log.length, 41);
  assert.ok(log[40].id < 50, "its id is far below the id the old page had read up to");
  const { env } = await reload(log, storage);
  assert.equal(badge(env).text, "1", "the new error - not 0 (hidden behind the old mark), not 3 (the old ones lit again)");
  // what was read stays read: the retry and the errors that were there before (their rebuilt times are a few ms later than the live ones)
  assert.equal(CallLog.unreadErrors(ui(env).calls(), ui(env)._state.readId), 1);
  assert.ok(ui(env)._state.readId > 0 && ui(env)._state.readId < 41, "the mark was carried over to the new numbering");
  // ... and that is what is remembered now: the next reload of the rebuilt log is an ordinary one
  const again = await reload(log, Object.fromEntries(env.storage));
  assert.equal(badge(again.env).text, "1");
  assert.deepEqual(stored(again.env).read, stored(env).read, "no second re-base");
  // pressing 全部标为已读 reads the new error too
  await open(env);
  toolbarClick(env, "mark-read");
  assert.equal(badge(env).hidden, true);
});

test("... and a rebuilt log that is NOT shorter than the mark: a new error with a low id must not hide behind it either", async () => {
  const old = OLD_LOG();
  const storage = await marked(old);
  // 14 lines are gone; the lines after the restart: an error (id 47) and 8 more calls - the log has 55 lines, MORE than the 50 that were read
  const extra = [mk(0, 300, "error"), ...Array.from({ length: 8 }, (_, i) => mk(0, 304 + i * 4))];
  const log = rebuilt(old, (c) => c.status === "ok" && c.id % 4 === 0, extra);
  const fresh = log.find((c) => c.ts === at(300));
  assert.ok(log.length >= 50 && fresh.id <= 50, `the trap: last id ${log.length} >= 50 and the new error has id ${fresh.id} <= 50`);
  const { env } = await reload(log, storage);
  assert.equal(badge(env).text, "1", "a log that merely is not shorter than the mark is no proof that it is the same log");
  assert.equal(ui(env)._state.readId < fresh.id, true);
});

test("a log rebuilt to NOTHING (an import without logs): the first error that follows has id 1 and lights the badge", async () => {
  const storage = await marked(OLD_LOG());
  const first = await reload([], storage);
  assert.equal(badge(first.env).hidden, true);
  assert.deepEqual(stored(first.env).read, { "game-A": 0 }, "nothing is known to be read: the stale id is gone");
  first.backend.llmCalls.push(mk(1, 400, "error"));
  callEvent(first.env, first.backend.llmCalls[0], 1);
  assert.equal(badge(first.env).text, "1", "id 1 is not above the old mark 50 - and must show anyway");
  // the same, found by a page that loads after the first error of the new log happened
  const second = await reload([mk(1, 400, "error")], storage);
  assert.equal(second.env.el("calllog-badge").textContent, "1");
});

test("the page is OPEN while the server restarts: the reconnect notices the rebuilt log, re-bases the mark, and the first new error lights the badge", async () => {
  const old = OLD_LOG();
  const { env, backend } = await live({ status: "running", session_id: "game-A" }, { llmCalls: old });
  await open(env);
  toolbarClick(env, "mark-read");
  assert.equal(badge(env).hidden, true);
  assert.equal(ui(env)._state.readId, 50);
  // the server restarts: the log comes back rebuilt (shorter: 40 lines; the one error since the restart is line 41)
  backend.llmCalls = rebuilt(old, (c) => c.status === "ok" && c.id % 3 === 0, [mk(0, 300, "error")]);
  const asked = backend.llmCallRequests.length;
  later(env);
  env.sse("snapshot", { session: F.makeSession({ session_id: "game-A", status: "paused", event_id: 901 }) }, 901);
  await flush(env, 80);
  assert.equal(backend.llmCallRequests.length, asked + 1, "the reconnect read the log again (the whole of it: the window is open) - and found it was another one");
  assert.equal(ids(ui(env).calls()).length, 41, "the old numbering is gone: the log is the rebuilt one");
  assert.equal(badge(env).text, "1", "the new error is unread; the old ones stay read");
  assert.ok(ui(env)._state.readId < 41);
  assert.deepEqual(ids(ui(env).calls()).slice(-1), [41]);
  // the next error is an ordinary event of the rebuilt log
  backend.llmCalls.push(mk(42, 310, "timeout"));
  callEvent(env, backend.llmCalls[41], 2);
  assert.equal(badge(env).text, "2");
  // a rebuild to NOTHING is the same story with an empty list: the first error is id 1
  backend.llmCalls = [];
  later(env);
  env.sse("snapshot", { session: F.makeSession({ session_id: "game-A", status: "paused", event_id: 902 }) }, 902);
  await flush(env, 80);
  assert.equal(ui(env).calls().length, 0);
  assert.equal(badge(env).hidden, true);
  backend.llmCalls.push(mk(1, 500, "error"));
  callEvent(env, backend.llmCalls[0], 1);
  assert.equal(badge(env).text, "1");
});

test("an event with an id we hold for ANOTHER line is a line of a rebuilt log - never taken for a duplicate; neither is a failed line that should be here and is not", async () => {
  const old = OLD_LOG();
  const { env, backend } = await live({ status: "running", session_id: "game-A" }, { llmCalls: old });
  await open(env);
  toolbarClick(env, "mark-read");
  // the server was rebuilt without the page noticing (no snapshot): its next error is id 3 - an id the page holds for an ok line
  backend.llmCalls = [mk(1, 10), mk(2, 20), mk(3, 500, "error")];
  callEvent(env, backend.llmCalls[2], 1);
  await flush(env, 80);
  assert.equal(badge(env).text, "1", "id 3 is not above the mark 50: it showed anyway");
  assert.deepEqual(ids(ui(env).calls()), [1, 2, 3], "the old numbering is gone");
  // the same for a FAILED line the page does not hold.  Until the window is opened the page holds only the failed lines, and every failed
  // line up to the last id is among them: one that is not (id 5, below the last id 60, while the failed lines are 20, 35 and 50) is of
  // another log - the id is not "ours" even though no line of ours has it
  const storage = await marked(OLD_LOG(), "game-B");
  const second = await reload(OLD_LOG(), storage, "game-B");
  assert.equal(second.env.el("calllog-badge").hidden, true);
  assert.deepEqual(ids(ui(second.env).calls()), [20, 35, 50], "(no window yet: the failed lines of the old log, no ok lines)");
  second.backend.llmCalls = [mk(1, 10), mk(2, 20), mk(3, 30), mk(4, 40), mk(5, 600, "timeout")];
  callEvent(second.env, second.backend.llmCalls[4], 1);
  await flush(second.env, 80);
  assert.equal(second.env.el("calllog-badge").textContent, "1", "id 5 is not above the mark 50: it showed anyway");
  assert.deepEqual(ids(ui(second.env).calls()), [5], "the failed lines of the old log are gone: the log is the one the server has now");
  // an ok line below the last id that the page does not hold is nothing to worry about while only the failed lines are held
  const third = await reload(OLD_LOG(), storage, "game-B");
  const requests = third.backend.llmCallRequests.length;
  callEvent(third.env, mk(7, 28), 2); // (a late delivery of a line the failed-only read skipped)
  assert.equal(third.backend.llmCallRequests.length, requests, "no re-read for it");
  assert.deepEqual(ids(ui(third.env).calls()), [7, 20, 35, 50]);
  assert.equal(third.env.el("calllog-badge").hidden, true);
});

test("a rebuilt log of the SAME size: the next catch-up sees that the errors the server counts are not the ones the page holds", async () => {
  const old = OLD_LOG(); // 60 lines, 2 errors
  const { env, backend } = await live({ status: "running", session_id: "game-A" }, { llmCalls: old });
  await open(env);
  toolbarClick(env, "mark-read");
  // 60 lines again - but the errors are other lines (and there are 3 of them): same last id, nothing "shorter", no id is a conflict
  backend.llmCalls = Array.from({ length: 60 }, (_, i) => mk(i + 1, 300 + (i + 1) * 4, { 10: "error", 30: "error", 59: "timeout" }[i + 1] || "ok"));
  later(env);
  env.sse("snapshot", { session: F.makeSession({ session_id: "game-A", status: "paused", event_id: 901 }) }, 901);
  await flush(env, 80);
  assert.equal(backend.llmCalls.length, 60);
  assert.deepEqual(ids(ui(env).calls().filter(CallLog.isError)), [10, 30, 59], "the page holds the log it reads now");
  assert.equal(badge(env).text, "3", "all three are after the mark's time: unread");
});

test("ordinary appends never reset the mark: events in sequence, a late duplicate, a catch-up that finds nothing wrong", async () => {
  const old = OLD_LOG();
  const storage = await marked(old);
  const { env, backend } = await reload(old, storage);
  const before = stored(env);
  const asked = backend.llmCallRequests.length;
  for (const line of [mk(61, 300), mk(62, 304, "error"), mk(63, 308), mk(64, 312, "retry")]) {
    backend.llmCalls.push(line);
    callEvent(env, line, line.id === 62 ? 3 : 3);
  }
  assert.equal(badge(env).text, "1", "only the error after the mark");
  callEvent(env, backend.llmCalls[61], 3); // a late duplicate of line 62
  callEvent(env, backend.llmCalls[60], 3); // ... and of an ok line that came before it
  assert.equal(badge(env).text, "1");
  assert.deepEqual(stored(env), before, "nothing was re-written");
  assert.equal(backend.llmCallRequests.length, asked, "and nothing was read again");
  assert.equal(ui(env)._state.readId, 50);
  // a reconnect of the same log: one read that finds nothing wrong keeps the mark and the lines
  later(env);
  env.sse("snapshot", { session: F.makeSession({ session_id: "game-A", status: "running", event_id: 902 }) }, 902);
  await flush(env, 80);
  assert.equal(backend.llmCallRequests.length, asked + 1, "exactly the one request of the recovery");
  assert.deepEqual(backend.llmCallRequests.slice(-1)[0], { errors_only: "true", limit: "2000" }, "(what the page keeps with the window closed: the failed lines, all of them)");
  assert.equal(ui(env)._state.readId, 50);
  assert.deepEqual(stored(env), before);
  assert.equal(badge(env).text, "1");
});

test("a mark kept by an older page has no witness: it is trusted for a log that is not shorter, and dropped for one that is", async () => {
  const log = OLD_LOG();
  // (what the page of the first build of this feature wrote: the contract's map, and nothing beside it)
  const legacy = { arena_calllog_read_v1: JSON.stringify({ "game-A": 50 }) };
  const same = await reload(log, legacy);
  assert.equal(same.env.el("calllog-badge").hidden, true, "id 50 is inside this log: the old behaviour");
  const shorter = await reload(rebuilt(log, (c) => c.status === "ok" && c.id % 3 === 0, [mk(0, 300, "error")]), legacy);
  assert.equal(shorter.env.el("calllog-badge").textContent, "3", "the log is shorter than the mark: it is another log; everything in it is unread");
  assert.deepEqual(stored(shorter.env).read, { "game-A": 0 });
});

// =====================================================================================================
// reads that overlap: only the answer of the NEWEST read counts (an older answer may arrive late)
// =====================================================================================================
test("the badge's read (the failed lines) is still on its way when the window opens and reads the whole log: the late answer must neither replace the whole log nor undo `full`", async () => {
  const { env, backend, gate } = await liveHeld({ status: "running", session_id: "game-A" });
  assert.equal(gate.parked.length, 1, "the first read of the page is on its way");
  assert.deepEqual(queries(backend), [{ errors_only: "true", limit: "2000" }]);
  env.el("btn-open-calllog").dispatch("click");
  await flush(env);
  assert.equal(gate.parked.length, 2, "the window asks for the whole log without waiting for the other read");
  assert.deepEqual(queries(backend)[1], { limit: "2000" });
  assert.equal(ui(env)._state.loading, true);
  // the whole log comes first ...
  assert.equal(gate.release(1), true);
  await flush(env);
  assert.equal(ui(env)._state.full, true);
  assert.deepEqual(ids(ui(env).calls()), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  assert.equal(rowIds(env).length, 10);
  assert.equal(ui(env)._state.loading, false);
  // ... then the older answer of the first read (the failed lines only): stale - it must change nothing, show nothing, retry nothing
  assert.equal(gate.release(0), true);
  await flush(env);
  assert.equal(ui(env)._state.full, true, "`full` is not undone");
  assert.deepEqual(ids(ui(env).calls()), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], "the whole log, not only the failed lines");
  assert.equal(rowIds(env).length, 10);
  assert.doesNotMatch(html(env, "calllog-foot"), /只加载了报错与重试/);
  assert.equal(badge(env).text, "3");
  assert.equal(queries(backend).length, 2, "and nothing asked again");
  // the other order is the ordinary one: the failed lines first, then the whole log
  const second = await liveHeld({ status: "running", session_id: "game-B" });
  second.env.el("btn-open-calllog").dispatch("click");
  await flush(second.env);
  second.gate.release(0);
  await flush(second.env);
  assert.equal(ui(second.env)._state.full, false, "(after the failed lines only: the whole log is still on its way)");
  second.gate.release(0);
  await flush(second.env);
  assert.equal(ui(second.env)._state.full, true);
  assert.equal(rowIds(second.env).length, 10);
});

test("the window is opened again while a read of the failed lines only is on its way, after the whole log was held once: the whole log is read - the other answer would have replaced it", async () => {
  const { env, backend, gate } = await liveHeld({ status: "running", session_id: "game-A" });
  gate.release(0); // the page's first read (the failed lines) is in
  await flush(env);
  await open(env); // the window reads the whole log
  gate.release(0);
  await flush(env);
  assert.equal(ui(env)._state.full, true);
  env.el("btn-close-calllog").dispatch("click");
  assert.equal(ui(env).isOpen(), false);
  // while it is closed a recovery reads what the page keeps then: the failed lines only (parked)
  backend.llmCalls.push(F.makeCall(11), F.makeFailedCall(12, { status: "error" }));
  later(env);
  callEvent(env, backend.llmCalls[11], 4); // line 11 never came
  await flush(env);
  assert.equal(gate.parked.length, 1);
  assert.match(gate.parked[0].url, /errors_only=true/);
  assert.equal(ui(env)._state.full, true, "(still the picture of the whole log)");
  // the window is opened again: the whole log is asked for at once - not left to the failed lines
  env.el("btn-open-calllog").dispatch("click");
  await flush(env);
  assert.equal(gate.parked.length, 2);
  assert.doesNotMatch(gate.parked[1].url, /errors_only/);
  gate.release(1);
  await flush(env);
  assert.deepEqual(ids(ui(env).calls()), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
  gate.release(0); // the answer of the failed lines is stale now
  await flush(env);
  assert.equal(ui(env)._state.full, true);
  assert.equal(ids(ui(env).calls()).length, 12);
  assert.equal(rowIds(env).length, 12);
});

test("the answer of a read of a game we have left is never taken - not even when we are back in a game of that id", async () => {
  const { env, backend, gate } = await liveHeld({ status: "running", session_id: "game-A" });
  // the game changes: the server's log is the new game's (its read is parked as well) ...
  backend.llmCalls = [F.makeCall(1, { actor_id: "N4" }), F.makeFailedCall(2, { status: "error", actor_id: "N4", actor_name: "David" })];
  env.sse("snapshot", { session: F.makeSession({ session_id: "game-B", status: "idle", event_id: 900 }) }, 900);
  await flush(env);
  // ... and back to a game with the first one's id (another load): a third log
  backend.llmCalls = [F.makeFailedCall(1, { status: "timeout", http_status: null })];
  env.sse("snapshot", { session: F.makeSession({ session_id: "game-A", status: "idle", event_id: 901 }) }, 901);
  await flush(env);
  assert.equal(gate.parked.length, 3);
  // the answers come in the worst order: the newest first, then the two others
  gate.release(2);
  await flush(env);
  assert.deepEqual(ids(ui(env).calls()), [1]);
  assert.equal(badge(env).text, "1");
  gate.releaseAll();
  await flush(env);
  assert.deepEqual(ids(ui(env).calls()), [1], "what the old reads said is nowhere");
  assert.equal(ui(env).calls()[0].status, "timeout");
  assert.equal(badge(env).text, "1");
  assert.equal(queries(backend).length, 3);
  // a session without an id (nothing to read a log of): the read that was on its way for the game before it is dropped as well
  const second = await liveHeld({ status: "running", session_id: "game-C" });
  ui(second.env).onSession({});
  second.gate.releaseAll();
  await flush(second.env);
  assert.deepEqual(ids(ui(second.env).calls()), [], "nothing of game C is taken into a page that is in no game");
  assert.equal(badge(second.env).hidden, true);
});

test("a line that comes by event while a read is on its way stays; when it does not follow the answer directly the page reads once more", async () => {
  const { env, backend, gate } = await liveHeld({ status: "running", session_id: "game-A" });
  // the answer on its way ends at line 10 (it was computed when the request was made); line 11 happens now and comes by event
  backend.llmCalls.push(F.makeFailedCall(11, { status: "error" }));
  callEvent(env, backend.llmCalls[10], 4);
  assert.deepEqual(ids(ui(env).calls()), [11], "(the page knows nothing else yet)");
  gate.release(0);
  await flush(env);
  assert.deepEqual(ids(ui(env).calls()), [3, 5, 6, 7, 11], "the answer, and the line that is newer than it");
  assert.equal(badge(env).text, "4");
  assert.equal(queries(backend).length, 1, "11 follows the last line of the answer (10): nothing is missing");
  // a hole: line 12 comes by event, line 11 never did
  const second = await liveHeld({ status: "running", session_id: "game-B" });
  second.backend.llmCalls.push(F.makeCall(11), F.makeFailedCall(12, { status: "timeout", http_status: null }));
  callEvent(second.env, second.backend.llmCalls[11], 4);
  second.gate.release(0);
  await flush(second.env);
  assert.equal(second.gate.parked.length, 1, "the hole between the answer (10) and the line (12) is read again");
  second.gate.release(0);
  await flush(second.env);
  assert.deepEqual(ids(ui(second.env).calls()), [3, 5, 6, 7, 12]);
  assert.equal(badge(second.env).text, "4");
  assert.equal(queries(second.backend).length, 2);
});

// =====================================================================================================
// a rebuilt log that "after the last id" cannot tell from the old one
// =====================================================================================================
const OLD_100 = () => Array.from({ length: 100 }, (_, i) => mk(i + 1, (i + 1) * 4, i + 1 === 80 ? "error" : "ok"));
// the log after the rebuild: 120 lines numbered anew; the errors that happened AFTER the old mark have the ids 20 and 110
const REBUILT_120 = () => Array.from({ length: 120 }, (_, i) => mk(i + 1, 600 + (i + 1) * 4, i + 1 === 20 || i + 1 === 110 ? "error" : "ok"));

test("a rebuilt log with MORE lines - an error that is new at an id below our last one and one above it: the lines after our last id are not the whole story", async () => {
  for (const mode of ["closed", "open"]) {
    const { env, backend } = await live({ status: "running", session_id: `game-${mode}` }, { llmCalls: OLD_100() });
    if (mode === "open") await open(env);
    ui(env).markAllRead();
    assert.equal(badge(env).hidden, true, mode);
    assert.equal(ui(env)._state.readId, 80, mode);
    // the server was restarted: the log is another one now.  The only thing the page learns of it is the event of the error at 110
    // (its ids 101-109 never came), and "the lines after id 100" - which hold the one error at 110 - make the errors add up (2 and 2)
    backend.llmCalls = REBUILT_120();
    callEvent(env, backend.llmCalls[109], 2);
    await flush(env, 60);
    assert.deepEqual(ids(ui(env).calls().filter(CallLog.isError)), [20, 110], `${mode}: the page holds the log it reads now`);
    assert.equal(badge(env).text, "2", `${mode}: both errors are newer than what was read - the one at 20 does not hide behind the old mark`);
    assert.equal(ui(env)._state.readId, 0, mode);
    assert.equal(ui(env)._state.full, mode === "open", mode);
    assert.equal(queries(backend).slice(-1)[0].after, undefined, `${mode}: never a read "after the last id"`);
    assert.equal(ids(ui(env).calls()).length, mode === "open" ? 120 : 2, mode);
  }
  // the same when the page learns of it from the snapshot after the reconnect (no event at all)
  const { env, backend } = await live({ status: "running", session_id: "game-snap" }, { llmCalls: OLD_100() });
  ui(env).markAllRead();
  backend.llmCalls = REBUILT_120();
  later(env);
  env.sse("snapshot", { session: F.makeSession({ session_id: "game-snap", status: "running", event_id: 901 }) }, 901);
  await flush(env, 60);
  assert.equal(badge(env).text, "2");
  assert.deepEqual(ids(ui(env).calls().filter(CallLog.isError)), [20, 110]);
});

// =====================================================================================================
// duplicates, late copies, the ring
// =====================================================================================================
const RING_FAILED = { 3500: "error", 4800: "timeout" };
const ringLog = (from, to, failed = RING_FAILED) =>
  Array.from({ length: to - from + 1 }, (_, i) => (failed[from + i] ? F.makeFailedCall(from + i, { status: failed[from + i], ...(failed[from + i] === "timeout" ? { http_status: null } : {}) }) : F.makeCall(from + i)));

test("a late copy of a failure from far below the ring is ignored once the page has the picture of the log: no read, nothing cleared - however often it comes", async () => {
  // the server keeps the newest 2000 lines: ids 3001 - 5000, two of them failed
  const { env, backend } = await live({ status: "running", session_id: "game-R" }, { llmCalls: ringLog(3001, 5000) });
  assert.equal(queries(backend).length, 1);
  assert.equal(ui(env)._state.lastId, 5000);
  assert.equal(ui(env)._state.synced, true);
  assert.equal(badge(env).text, "2");
  const picture = ids(ui(env).calls());
  assert.deepEqual(picture, [3500, 4800]);
  const late = [F.makeFailedCall(1), F.makeFailedCall(1), F.makeFailedCall(2999), F.makeFailedCall(3000), F.makeCall(7), F.makeFailedCall(1, { status: "timeout", http_status: null })];
  for (const call of late) callEvent(env, call, 2);
  await flush(env);
  assert.equal(queries(backend).length, 1, "no read: the initial one only (it used to be one read per copy)");
  assert.deepEqual(ids(ui(env).calls()), picture, "nothing was cleared, nothing was added");
  assert.equal(badge(env).text, "2");
  assert.equal(ui(env)._state.recoverTimer, null);
  // the oldest line of the ring is still inside it: a failure with that id which we do not hold is a line of another log
  callEvent(env, F.makeFailedCall(3001), 3);
  await flush(env);
  assert.equal(queries(backend).length, 2, "the edge of the ring: id 3001 is inside it");
  assert.deepEqual(ids(ui(env).calls()), picture, "(the server's line 3001 is an ok one: the picture is the same)");
});

test("out of order inside the ring: line 5000 before 4999 - one read, and the late copy of 4999 is a line the read brought", async () => {
  const { env, backend } = await live({ status: "running", session_id: "game-O" }, { llmCalls: ringLog(3001, 4998, { 4000: "error" }) });
  assert.equal(badge(env).text, "1");
  backend.llmCalls.push(F.makeFailedCall(4999), F.makeFailedCall(5000, { status: "timeout", http_status: null }));
  callEvent(env, backend.llmCalls[1999 + 0], 2); // line 5000 (the server's log has 2000 lines: 3001 - 4998 and these two)
  await flush(env);
  assert.equal(queries(backend).length, 2, "the gap (4999 is missing) is one read");
  assert.deepEqual(ids(ui(env).calls()), [4000, 4999, 5000]);
  assert.equal(badge(env).text, "3");
  callEvent(env, backend.llmCalls[1998], 3); // 4999 comes at last
  await flush(env);
  assert.equal(queries(backend).length, 2, "it is a line we hold: nothing to ask");
  assert.deepEqual(ids(ui(env).calls()), [4000, 4999, 5000]);
});

test("a failure inside the ring that the server does not have, said again and again: one read at once, the rest wait their turn - and the wait doubles while the reads find nothing wrong", async () => {
  const { env, backend } = await live({ status: "running", session_id: "game-S" }); // LOG: ids 1 - 10, the failed lines are 3, 5, 6, 7
  const s = ui(env)._state;
  const stray = F.makeFailedCall(4, { status: "error", actor_id: "N9" }); // inside the ring, not held, and the server's line 4 is an ok one
  const waiting = () => env.timers.filter((t) => t.id === s.recoverTimer);
  const fire = () => {
    const [t] = env.timers.splice(env.timers.findIndex((x) => x.id === s.recoverTimer), 1);
    t.fn();
    return t;
  };
  for (let i = 0; i < 3; i++) callEvent(env, stray, 3);
  await flush(env);
  assert.equal(queries(backend).length, 2, "the first one of the three read the log at once (the first recovery after a quiet time)");
  assert.deepEqual(ids(ui(env).calls()), [3, 5, 6, 7], "the page keeps its picture while it asks - and after: the server's log is the same");
  assert.equal(badge(env).text, "3");
  assert.equal(waiting().length, 1, "the other two wait for their turn - in ONE timer");
  assert.ok(waiting()[0].ms > 2500 && waiting()[0].ms <= 3000, `the wait doubled once: ${waiting()[0].ms}`);
  assert.equal(s.recoverStreak, 1);
  for (let i = 0; i < 3; i++) callEvent(env, stray, 3);
  assert.equal(waiting().length, 1, "still one");
  assert.equal(queries(backend).length, 2);
  // its turn comes: one read - and the next wait is twice as long
  fire();
  await flush(env);
  assert.equal(queries(backend).length, 3);
  assert.equal(s.recoverStreak, 2);
  callEvent(env, stray, 3);
  assert.ok(waiting()[0].ms > 5500 && waiting()[0].ms <= 6000, `${waiting()[0].ms}`);
  fire();
  await flush(env);
  callEvent(env, stray, 3);
  assert.ok(waiting()[0].ms > 11500 && waiting()[0].ms <= 12000, `capped at 12 s: ${waiting()[0].ms}`);
  fire();
  await flush(env);
  callEvent(env, stray, 3);
  assert.ok(waiting()[0].ms > 11500 && waiting()[0].ms <= 12000, `(and no more than that) ${waiting()[0].ms}`);
  // a recovery that finds the log changed starts the doubling over
  backend.llmCalls.push(F.makeFailedCall(11, { status: "error" }));
  fire();
  await flush(env);
  assert.equal(s.recoverStreak, 0);
  assert.equal(badge(env).text, "4");
});

test("a recovery asked for while a read is on its way is folded into ONE more read behind it - and a new game forgets what was waiting", async () => {
  const { env, backend, gate } = await liveHeld({ status: "running", session_id: "game-A" });
  gate.release(0); // the first read of the page is done
  await flush(env);
  const s = ui(env)._state;
  // a gap: the recovery reads at once (parked) ...
  backend.llmCalls.push(F.makeFailedCall(11, { status: "error" }), F.makeCall(12), F.makeFailedCall(13, { status: "error" }));
  callEvent(env, backend.llmCalls[12], 5);
  await flush(env);
  assert.equal(gate.parked.length, 1);
  // ... while it is on its way more gaps and strangers come: no more requests, just the note that one more read is wanted
  callEvent(env, F.makeFailedCall(40, { status: "error" }), 5);
  callEvent(env, F.makeFailedCall(41, { status: "error" }), 5);
  callEvent(env, F.makeFailedCall(2, { status: "error", actor_id: "N9" }), 5);
  await flush(env);
  assert.equal(gate.parked.length, 1);
  assert.equal(s.again, true);
  gate.release(0);
  await flush(env);
  assert.equal(queries(backend).length, 2);
  assert.equal(s.again, false);
  assert.notEqual(s.recoverTimer, null, "the one more read waits for its turn (a recovery has just begun)");
  assert.equal(gate.parked.length, 0);
  // the game changes while it waits: the waiting read is gone, the new game's own read is the only one
  env.sse("snapshot", { session: F.makeSession({ session_id: "game-B", status: "idle", event_id: 910 }) }, 910);
  await flush(env);
  assert.equal(s.recoverTimer, null);
  assert.equal(gate.parked.length, 1, "the new game's read");
  assert.equal(s.recoverStreak, 0);
});

// ---- the pure helpers of the above ----------------------------------------------------------------------------------------
test("sameLine / lastFailure / markOf: a line of one log never changes; the mark is the newest failed line", () => {
  const a = Store.normalizeCall(mk(5, 20, "error"));
  assert.equal(CallLog.sameLine(a, Store.normalizeCall(mk(5, 20, "error"))), true);
  for (const other of [mk(5, 21, "error"), mk(5, 20, "timeout"), mk(5, 20, "error", 1), mk(5, 20, "error")]) {
    const o = Store.normalizeCall({ ...other, actor_id: other === undefined ? "x" : other.actor_id });
    assert.equal(CallLog.sameLine(a, o), other.ts === a.ts && other.status === a.status, JSON.stringify([other.ts, other.status]));
  }
  assert.equal(CallLog.sameLine(a, Store.normalizeCall({ ...mk(5, 20, "error"), actor_id: "N9" })), false);
  assert.equal(CallLog.sameLine(a, Store.normalizeCall({ ...mk(5, 20, "error"), kind: "event" })), false);
  assert.equal(CallLog.sameLine(a, null), false);
  assert.equal(CallLog.sameLine(undefined, undefined), false);
  const calls = [mk(1, 4), mk(2, 8, "error"), mk(3, 12, "retry"), mk(4, 16), mk(5, 20, "cancelled")].map(Store.normalizeCall);
  assert.equal(CallLog.lastFailure(calls).id, 3, "a retry is a failure too (it is what 只看错误 shows)");
  assert.equal(CallLog.lastFailure(calls.slice(0, 2)).id, 2);
  assert.equal(CallLog.lastFailure([calls[0], calls[3]]), null);
  assert.equal(CallLog.lastFailure(null), null);
  assert.deepEqual(CallLog.markOf(calls), { id: 3, ts: at(12) });
  assert.deepEqual(CallLog.markOf([calls[0]]), { id: 0, ts: "" }, "no failure: nothing to mark");
  assert.deepEqual(CallLog.markOf(null), { id: 0, ts: "" });
});

test("markIsValid: a mark is about ONE log - the very line at that id, or a line the ring dropped; without a witness only 'not shorter' can be said", () => {
  const calls = [mk(10, 40, "error"), mk(12, 48), mk(13, 52, "error")].map(Store.normalizeCall);
  const valid = (mark, meta, list = calls) => CallLog.markIsValid(mark, list, meta);
  assert.equal(valid({ id: 0, ts: "" }, { last_id: 13, total: 13 }), true, "nothing was read");
  assert.equal(valid({ id: 13, ts: at(52) }, { last_id: 13, total: 13 }), true, "the line is there, with the time it had");
  assert.equal(valid({ id: 13, ts: at(53) }, { last_id: 13, total: 13 }), false, "the same id, another line");
  assert.equal(valid({ id: 13, ts: at(52, 3) }, { last_id: 13, total: 13 }), false, "even 3 ms off is another line: the rebuilt ones differ a little");
  assert.equal(valid({ id: 11, ts: at(44) }, { last_id: 13, total: 13 }), false, "the log is as long as the mark but has no such line: it is another log");
  assert.equal(valid({ id: 50, ts: at(200) }, { last_id: 13, total: 13 }), false, "shorter than the mark");
  assert.equal(valid({ id: 5, ts: at(20) }, { last_id: 2500, total: 2000 }, calls), true, "gone because the ring holds only the newest 2000 (ids 501 and up): everything it has is newer");
  assert.equal(valid({ id: 600, ts: at(20) }, { last_id: 2500, total: 2000 }, calls), false, "id 600 is inside the ring: it should be here");
  assert.equal(valid({ id: 5, ts: at(20) }, { last_id: 5, total: 0 }, []), false, "an empty log holds nothing the mark could mean");
  // a mark without a witness
  assert.equal(valid({ id: 13, ts: "" }, { last_id: 13, total: 13 }), true);
  assert.equal(valid({ id: 14, ts: "" }, { last_id: 13, total: 13 }), false);
  assert.equal(valid(null, null), true);
  assert.equal(valid({ id: 3, ts: "" }, null), false);
});

test("rebaseMark: what is not later than the mark stays read (a few ms of slack for the rebuilt times); everything else, and every unknown time, is unread", () => {
  assert.equal(CallLog.REBASE_SLACK_MS, 50);
  const rows = (...specs) => specs.map(([id, sec, status, ms]) => Store.normalizeCall(mk(id, sec, status || "ok", ms || 0)));
  const mark = { id: 50, ts: at(200) };
  // the rebuilt log: the errors that were read - with a trace time a few ms later - then a new one
  const log = rows([1, 40], [2, 80, "error", 2], [3, 140, "retry", 3], [4, 190], [5, 200, "error", 3], [6, 230], [7, 300, "error"]);
  assert.deepEqual(CallLog.rebaseMark(mark, log), { id: 5, ts: at(200, 3) }, "the line of the mark, 3 ms late, is still read");
  assert.equal(CallLog.unreadErrors(log, CallLog.rebaseMark(mark, log).id), 1, "only the error of second 300");
  // 100 ms later is no jitter: a NEW line (it can only come after the restart ... but it can come close after the mark)
  const close = rows([1, 40, "error"], [2, 200, "error", 100]);
  assert.deepEqual(CallLog.rebaseMark(mark, close), { id: 1, ts: at(40) });
  // the exact same instant (several errors in one millisecond: a provider that went down): all of them count as read, and only them
  const burst = rows([1, 200, "error"], [2, 200, "error"], [3, 200, "timeout"], [4, 200, "error", 1], [5, 201, "error"]);
  assert.deepEqual(CallLog.rebaseMark(mark, burst), { id: 4, ts: at(200, 1) });
  // nothing in the log is that old: nothing is known to be read
  assert.deepEqual(CallLog.rebaseMark(mark, rows([1, 300, "error"], [2, 310])), { id: 0, ts: "" });
  assert.deepEqual(CallLog.rebaseMark(mark, []), { id: 0, ts: "" });
  assert.deepEqual(CallLog.rebaseMark(mark, null), { id: 0, ts: "" });
  // no time to go by: no mark with a witness, or no witness
  assert.deepEqual(CallLog.rebaseMark({ id: 50, ts: "" }, log), { id: 0, ts: "" });
  assert.deepEqual(CallLog.rebaseMark({ id: 50, ts: "not a time" }, log), { id: 0, ts: "" });
  assert.deepEqual(CallLog.rebaseMark(null, log), { id: 0, ts: "" });
  // a line whose time is not known (an old trace record) stops the scan: it and everything after it is unread
  const noTime = rows([1, 40, "error"], [2, 80, "error"], [3, 120, "error"]);
  noTime[1] = { ...noTime[1], ts: "" };
  assert.deepEqual(CallLog.rebaseMark(mark, noTime), { id: 1, ts: at(40) });
  const garbled = rows([1, 40, "error"], [2, 80, "error"]);
  garbled[1] = { ...garbled[1], ts: "yesterday" };
  assert.deepEqual(CallLog.rebaseMark(mark, garbled), { id: 1, ts: at(40) });
  // times that do not rise with the ids (a clock that was set back) never hide a line: the first later line ends the scan
  const odd = rows([1, 40, "error"], [2, 260, "error"], [3, 100, "error"]);
  assert.deepEqual(CallLog.rebaseMark(mark, odd), { id: 1, ts: at(40) }, "line 3 is 'old' by its time but comes after a later line: unread");
  // a read that ended at a line without failures before it
  assert.deepEqual(CallLog.rebaseMark(mark, rows([1, 40], [2, 80], [3, 300, "error"])), { id: 0, ts: "" });
});

test("on a phone the badge is on the ⋯ button as well, and the log button lives in the ⋯ menu", async () => {
  const { env } = await live({ status: "running" }, {}, { innerWidth: 390, innerHeight: 844, userAgent: IPHONE, coarse: true, maxTouchPoints: 5 });
  assert.equal(env.el("more-badge").textContent, "3");
  assert.equal(env.el("more-badge").hidden, false);
  const css = require("node:fs").readFileSync(path.join(H.ROOT, "static", "css", "style.css"), "utf8");
  assert.match(css, /\.more-badge \{[^}]*position: absolute/);
  assert.match(css, /\.more-menu-list \.calllog-badge \{[^}]*position: static/, "inside the menu the badge sits in the row, not on a corner");
  assert.ok(H.SRC.app.includes('{ id: "btn-open-calllog", slot: "menu" }'), "the button is one of those that move");
  assert.match(H.HTML_SRC, /<button class="btn btn-more" id="btn-more"[^>]*>⋯<span class="more-badge" id="more-badge" hidden><\/span><\/button>/);
});

// =====================================================================================================
// the window (G33)
// =====================================================================================================
test("opening the window reads the whole log; every line shows when, who, which model, the result, how long, the tokens and why it ended; newest first", async () => {
  const { env, backend } = await live({ status: "running" });
  await open(env);
  assert.equal(win(env).hidden, false);
  assert.deepEqual(queries(backend).pop(), { limit: "2000" }, "no errors_only: everything");
  assert.deepEqual(rowIds(env), [10, 9, 8, 7, 6, 5, 4, 3, 2, 1], "the newest on top");
  assert.match(sub(env), /当前对局 · 10 条 · 报错 3/);
  assert.match(html(env, "calllog-foot"), /共 10 条 · 报错 3 · 重试 1/);
  assert.match(html(env, "calllog-foot"), /不含提示词、模型输出和密钥/);
  const r2 = rowOf(env, 2);
  assert.match(r2, /cl-row cl-ok/);
  assert.ok(r2.includes(`>${Core.formatClockTime(LOG[1].ts)}<small>游戏 0:12</small>`), "local time and game time");
  assert.match(r2, /<span class="cl-role" title="对局">N3 Carol<\/span>/);
  assert.match(r2, /<span class="cl-model" title="Provider \/ 模型">deepseek \/ deepseek-chat<\/span>/);
  assert.match(r2, /<span class="cl-status st-ok">成功<\/span>/);
  assert.match(r2, /cl-elapsed" title="耗时">1\.2 s<\/span>/);
  assert.match(r2, /cl-tokens"[^>]*>1,000 \/ 200<\/span>/);
  assert.match(r2, /cl-reason"[^>]*>stop<\/span>/);
  assert.match(rowOf(env, 1), /<span class="cl-role"[^>]*>主持人<\/span>/);
  assert.match(rowOf(env, 8), /<span class="cl-role" title="复盘">复盘 · N4 David<\/span>/);
  assert.match(rowOf(env, 9), /<span class="cl-role" title="完善">完善<\/span>/);
  assert.match(rowOf(env, 3), /cl-row cl-retry[^>]*>[^]*<span class="cl-status st-retry">重试<\/span>/);
  assert.match(rowOf(env, 7), /cl-row cl-timeout[^]*<span class="cl-status st-timeout">超时<\/span>[^]*cl-elapsed" title="耗时">2 分 00 秒<\/span>[^]*cl-reason"[^>]*>TimeoutError<\/span>/);
  assert.match(rowOf(env, 10), /cl-row cl-cancelled[^]*<span class="cl-status st-cancelled">取消<\/span>[^]*cl-reason"[^>]*>已取消<\/span>/);
  assert.match(rowOf(env, 6), /cl-row cl-error cl-event[^]*<span class="cl-status st-error">运行错误<\/span>/);
  // the unread errors carry the red dot; the ones that are not errors do not
  assert.deepEqual([5, 6, 7].map((id) => /cl-row[^"]* unread/.test(rowOf(env, id).split('data-call-id')[0] + "\"")), [true, true, true]);
  assert.ok(!/unread/.test(rowOf(env, 3).slice(0, 60)) && !/unread/.test(rowOf(env, 2).slice(0, 60)));
});

test("a line opens and closes on click and shows the error type, the HTTP status, the attempt and the scrubbed message; several can be open", async () => {
  const { env } = await live({ status: "running" });
  await open(env);
  assert.ok(!html(env, "calllog-list").includes("cl-detail"));
  rowClick(env, 5);
  const r5 = rowOf(env, 5);
  assert.match(r5, /^<div class="cl-row cl-error[^"]* open" data-call-id="5">/);
  assert.match(r5, /aria-expanded="true"/);
  const detail = textOf(r5.slice(r5.indexOf('<dl class="cl-detail">')));
  assert.match(detail, /错误类型 RateLimitError/);
  assert.match(detail, /HTTP 状态 429/);
  assert.match(detail, /第几次尝试 第 3 次/);
  assert.match(detail, /错误信息 Rate limit <b>reached<\/b> for requests/, "shown as text, the tags are not tags");
  assert.ok(!r5.includes("<b>reached</b>"));
  rowClick(env, 7);
  assert.equal((html(env, "calllog-list").match(/<dl class="cl-detail">/g) || []).length, 2);
  assert.match(textOf(rowOf(env, 7)), /错误类型 TimeoutError/);
  assert.match(textOf(rowOf(env, 7)), /错误信息 调用超过 120 秒仍未返回/);
  rowClick(env, 5);
  assert.ok(!rowOf(env, 5).includes("cl-detail"));
  assert.ok(rowOf(env, 7).includes("cl-detail"), "the other stays open");
  // the details survive a new line arriving at the top
  callEvent(env, F.makeCall(11), 3);
  assert.ok(rowOf(env, 7).includes("cl-detail"));
  assert.deepEqual(rowIds(env)[0], 11);
});

test("filters: 只看错误 shows the failed lines (retries too) and counts them; the role select narrows further; 全部 brings everything back", async () => {
  const { env } = await live({ status: "running" });
  await open(env);
  assert.match(html(env, "calllog-toolbar"), /data-mode="all" aria-pressed="true">全部<\/button>/);
  assert.match(html(env, "calllog-toolbar"), /data-mode="errors" aria-pressed="false">只看错误 <span class="cl-seg-count">4<\/span><\/button>/, "retry + 3 errors");
  toolbarClick(env, "mode", { "data-mode": "errors" });
  assert.deepEqual(rowIds(env), [7, 6, 5, 3]);
  assert.match(html(env, "calllog-toolbar"), /data-mode="errors" aria-pressed="true"/);
  // the role select lists those who have lines
  const options = [...html(env, "calllog-toolbar").matchAll(/<option value="([^"]*)"[^>]*>([^<]*)<\/option>/g)].map((m) => [m[1], unesc(m[2])]);
  assert.deepEqual(options, [["all", "全部角色"], ["moderator", "主持人"], ["N3", "N3 Carol"], ["N4", "N4 David"], ["N5", "N5 Eve"], ["N6", "N6 Frank"], ["N7", "N7 Grace"], ["roster", "名册"]]);
  pickActor(env, "N5");
  assert.deepEqual(rowIds(env), [6, 5]);
  assert.match(html(env, "calllog-toolbar"), /<option value="N5" selected>N5 Eve<\/option>/);
  pickActor(env, "N3");
  assert.deepEqual(rowIds(env), [3], "the retry of N3 is the only failure of N3");
  toolbarClick(env, "mode", { "data-mode": "all" });
  assert.deepEqual(rowIds(env), [4, 3, 2]);
  pickActor(env, "all");
  assert.equal(rowIds(env).length, 10);
  // nothing to show: the right empty message
  toolbarClick(env, "mode", { "data-mode": "errors" });
  pickActor(env, "moderator");
  assert.match(textOf(html(env, "calllog-list")), /没有报错，也没有重试 ✓/);
  toolbarClick(env, "mode", { "data-mode": "all" });
  env.el("calllog-toolbar").dispatch("change", { target: { id: "something-else", value: "N3" } }); // another control: ignored
  assert.deepEqual(rowIds(env), [1]);
});

test("a new line arrives while the window is open: it is on top at once, the counters and the badge follow, the filters apply to it", async () => {
  const { env } = await live({ status: "running" });
  await open(env);
  callEvent(env, F.makeFailedCall(11, { status: "error", actor_id: "N8", actor_name: "Henry" }), 4);
  assert.deepEqual(rowIds(env).slice(0, 2), [11, 10]);
  assert.match(html(env, "calllog-list"), /<div class="cl-row cl-error unread" data-call-id="11">/);
  assert.equal(badge(env).text, "4");
  assert.match(textOf(html(env, "calllog-foot")), /共 11 条 · 报错 4 · 重试 1/);
  assert.match(html(env, "calllog-toolbar"), /<option value="N8">N8 Henry<\/option>/, "a role that was not there before");
  toolbarClick(env, "mode", { "data-mode": "errors" });
  callEvent(env, F.makeCall(12), 4); // not a failure: not shown in this mode
  assert.deepEqual(rowIds(env), [11, 7, 6, 5, 3]);
  toolbarClick(env, "mark-read");
  assert.equal(badge(env).hidden, true);
  assert.ok(!/unread/.test(html(env, "calllog-list")), "the dots go with the badge");
  assert.match(html(env, "calllog-toolbar"), /data-calllog-action="mark-read" disabled/);
});

test("a long log: the first 300 lines are drawn, '显示更多' adds the rest", async () => {
  const log = Array.from({ length: 700 }, (_, i) => F.makeCall(i + 1, { actor_id: i % 2 ? "N3" : "N4" }));
  const { env } = await live({ status: "running" }, { llmCalls: log });
  await open(env);
  assert.equal(rowIds(env).length, 300);
  assert.equal(rowIds(env)[0], 700);
  assert.match(html(env, "calllog-list"), /data-calllog-action="more">显示更多（还有 400 条）<\/button>/);
  toolbarClick(env, "more");
  assert.equal(rowIds(env).length, 600);
  toolbarClick(env, "more");
  assert.equal(rowIds(env).length, 700);
  assert.doesNotMatch(html(env, "calllog-list"), /显示更多/);
  assert.equal(CallLog.PAGE_ROWS, 300);
});

test("refresh reads the whole log again; a failing read keeps what is shown and says why; the next one recovers", async () => {
  const { env, backend } = await live({ status: "running" });
  await open(env);
  backend.llmCalls.push(F.makeCall(11, { actor_id: "N3" }));
  toolbarClick(env, "refresh");
  await flush(env);
  assert.equal(rowIds(env)[0], 11);
  backend.callsFailure = [500, "日志暂时不可用"];
  toolbarClick(env, "refresh");
  await flush(env);
  assert.equal(rowIds(env).length, 11, "the lines are still there");
  assert.match(html(env, "calllog-list"), /<div class="form-hint error cl-error">[^<]*日志暂时不可用[^<]*<\/div>/);
  backend.callsFailure = null;
  toolbarClick(env, "refresh");
  await flush(env);
  assert.doesNotMatch(html(env, "calllog-list"), /form-hint error cl-error/, "the complaint goes with the next good read");
});

test("an empty log says what will happen; opening and closing: the button toggles, ✕ and Esc close", async () => {
  const { env } = await live({ status: "idle" }, { llmCalls: [] });
  await open(env);
  assert.match(textOf(html(env, "calllog-list")), /还没有调用记录。对局开始后，每次模型调用都会记在这里。/);
  assert.match(sub(env), /当前对局 · 0 条 · 报错 0/);
  env.el("btn-close-calllog").dispatch("click");
  assert.equal(win(env).hidden, true);
  env.el("btn-open-calllog").dispatch("click");
  assert.equal(win(env).hidden, false);
  env.el("btn-open-calllog").dispatch("click");
  assert.equal(win(env).hidden, true, "the button toggles");
  env.el("btn-open-calllog").dispatch("click");
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.equal(win(env).hidden, true);
  assert.match(H.HTML_SRC, /<div class="vw-window calllog-window" id="calllog-window" role="dialog" aria-label="错误日志" hidden>/);
});

// =====================================================================================================
// games: a new one starts empty; a reconnect of the same one catches up; the log is the live game's
// =====================================================================================================
test("a new game starts a new log (its own read mark); the window, if open, reads it; a reconnect of the SAME game asks for what it missed", async () => {
  const { env, backend } = await live({ status: "running", session_id: "game-A" });
  await open(env);
  toolbarClick(env, "mode", { "data-mode": "errors" });
  pickActor(env, "N5");
  rowClick(env, 5);
  // a new game: the server's log is the new game's
  backend.llmCalls = [F.makeCall(1, { actor_id: "N4" }), F.makeFailedCall(2, { status: "error", actor_id: "N4", actor_name: "David" })];
  env.sse("snapshot", { session: F.makeSession({ session_id: "game-B", status: "idle", event_id: 900 }) }, 900);
  await flush(env);
  assert.equal(ui(env)._state.actor, "all", "the filter of the other game is gone");
  assert.equal(ui(env)._state.expanded.size, 0);
  assert.equal(win(env).hidden, false, "the window stays open");
  assert.equal(badge(env).text, "1", "its own unread count");
  assert.deepEqual(ids(ui(env).calls()), [1, 2]);
  assert.deepEqual(rowIds(env), [2], "(the 'only errors' mode is the user's, it stays: the one error of the new game)");
  toolbarClick(env, "mode", { "data-mode": "all" });
  assert.deepEqual(rowIds(env), [2, 1]);
  // the same game's snapshot (a reconnect) reads the log again - the whole of it, the window is open - and not again at once
  backend.llmCalls.push(F.makeFailedCall(3, { status: "timeout", http_status: null }));
  const asked = queries(backend).length;
  later(env);
  env.sse("snapshot", { session: F.makeSession({ session_id: "game-B", status: "running", event_id: 901 }) }, 901);
  await flush(env);
  assert.deepEqual(queries(backend).slice(asked), [{ limit: "2000" }]);
  assert.equal(badge(env).text, "2");
  env.sse("snapshot", { session: F.makeSession({ session_id: "game-B", status: "running", event_id: 902 }) }, 902);
  await flush(env);
  assert.equal(queries(backend).length, asked + 1, "(the next recovery waits for its turn)");
});

test("a log that is SHORTER than what we know was rebuilt (another game loaded under the same id): the page starts over", async () => {
  const { env, backend } = await live({ status: "running", session_id: "game-A" });
  await open(env);
  assert.equal(rowIds(env).length, 10);
  backend.llmCalls = [F.makeCall(1, { actor_id: "N4" }), F.makeFailedCall(2, { status: "error", actor_id: "N4" })];
  later(env);
  env.sse("snapshot", { session: F.makeSession({ session_id: "game-A", status: "running", event_id: 901 }) }, 901);
  await flush(env, 60);
  assert.deepEqual(rowIds(env), [2, 1]);
});

test("the log belongs to the LIVE game: its lines keep coming - and the badge keeps counting - while a replay is on screen", async () => {
  const { env } = await live({ status: "running", session_id: "game-A" });
  const app = env.app();
  app.state.replay = { live: app.state.session, installing: false }; // (what enterReplay sets: a saved game is shown, the live one waits)
  callEvent(env, F.makeFailedCall(11, { status: "error" }), 4);
  assert.equal(badge(env).text, "4");
  assert.ok(/getSession: \(\) => liveSession\(\)/.test(H.SRC.app.slice(H.SRC.app.indexOf("window.ArenaCallLogUI.create"))), "it reads the live session, not the replayed one");
  app.state.replay = null;
});

H.runTests(tests);
