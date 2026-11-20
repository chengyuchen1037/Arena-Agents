"use strict";
// The pure helpers of the bot UI (static/js/botlib.js, v0.14).  What they say about limits and texts is held to the backend by
// tests/frontend/test_bots_drift.py; this file tests the rest: the initialization states, the progress of a job, diffs, fingerprints.
// Run: node tests/frontend/test_botlib.js
const assert = require("node:assert/strict");
const path = require("node:path");
const Lib = require(path.join(__dirname, "..", "..", "static", "js", "botlib.js"));
const F = require("./fixtures.js");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("the symbols and the words of the five states of an initialization (H22)", () => {
  assert.deepEqual(Lib.INIT_SYMBOLS, { ready: "✔", stale: "⚠", failed: "✖", initializing: "⏳", uninitialized: "○" });
  const ok = Lib.initView(F.makeBotSummary(1));
  assert.deepEqual([ok.status, ok.symbol, ok.label, ok.tone], ["ready", "✔", "已初始化", "ok"]);
  assert.match(ok.detail, /模型 deepseek-flash · 用时 1 分 14 秒 · \d{4}-\d\d-\d\d \d\d:\d\d/);
  const stale = Lib.initView(F.makeBotSummary(2, { init: { status: "stale", stale: ["core", "contract"] } }));
  assert.equal(stale.label, "需重新初始化（底层设定、官方契约变了）");
  assert.match(stale.detail, /记忆保留；重新初始化会修订它们，不会清空/);
  assert.equal(stale.tone, "warn");
  assert.equal(Lib.initView(F.makeBotSummary(2, { init: { status: "stale", stale: [] } })).label, "需重新初始化");
  const failed = Lib.initView(F.makeBotSummary(3, { init: { status: "failed", error: "模型调用失败：HTTP 429" } }));
  assert.deepEqual([failed.symbol, failed.tone, failed.detail], ["✖", "bad", "原因：模型调用失败：HTTP 429"]);
  assert.match(Lib.initView(F.makeBotSummary(3, { init: { status: "failed", error: null } })).detail, /原因不明/);
  assert.deepEqual([Lib.initView(F.makeBotSummary(4, { init: { status: "initializing" } })).symbol, Lib.initView({ init: { status: "weird" } }).status], ["⏳", "uninitialized"]);
  assert.equal(Lib.initView(null).status, "uninitialized");
});

test("a global initialization takes the bots that are not ready (or everybody); never one that is running", () => {
  const bots = [
    F.makeBotSummary(1),
    F.makeBotSummary(2, { init: { status: "stale" } }),
    F.makeBotSummary(3, { init: { status: "failed" } }),
    F.makeBotSummary(4, { init: { status: "uninitialized" } }),
    F.makeBotSummary(5, { init: { status: "initializing" } }),
  ];
  assert.deepEqual(Lib.initTargets(bots, false), [F.botId(2), F.botId(3), F.botId(4)]);
  assert.deepEqual(Lib.initTargets(bots, true), [F.botId(1), F.botId(2), F.botId(3), F.botId(4)]);
  assert.deepEqual(Lib.initTargets([], true), []);
  assert.deepEqual(Lib.initTargets(null, false), []);
});

test("the progress of one bot's initialization: the line, the countdown, the files written, the bar", () => {
  const ev = (over) => ({ job_id: "j", bot_id: "b", name: "A", state: "running", seconds_left: 90, deadline_s: 120, files_written: [], error: null, ...over });
  assert.equal(Lib.initProgressView(null), null);
  let v = Lib.initProgressView(ev({}), 120);
  assert.deepEqual([v.running, v.percent, v.left], [true, 25, 90]);
  assert.equal(v.line, "初始化中，还剩 90 秒");
  v = Lib.initProgressView(ev({ seconds_left: 30, files_written: ["arch_notes", "digest"] }), 120);
  assert.equal(v.line, "初始化中，还剩 30 秒；已写：操作笔记、规则要点");
  assert.equal(v.percent, 75);
  assert.equal(Lib.initProgressView(ev({ state: "queued", seconds_left: null }), 120).line, "排队中…");
  v = Lib.initProgressView(ev({ state: "done", seconds_left: 12, files_written: ["arch_notes", "digest", "playbook"] }), 120);
  assert.deepEqual([v.running, v.percent, v.line], [false, 100, "已完成：写了 操作笔记、规则要点、打法"]);
  assert.equal(Lib.initProgressView(ev({ state: "timeout", seconds_left: 0, error: "没写完" }), 120).line, "超过 120 秒，已经停止：没写完");
  assert.equal(Lib.initProgressView(ev({ state: "failed", error: "模型调用失败" }), 120).line, "失败：模型调用失败");
  assert.equal(Lib.initProgressView(ev({ state: "cancelled" }), 120).line, "已取消");
  assert.equal(Lib.initProgressView(ev({ seconds_left: null }), 120).line, "初始化中，还剩 ? 秒");
  assert.equal(Lib.initProgressView(ev({ seconds_left: 500 }), 120).percent, 0, "never below 0 %");
});

test("the problems of the start check of one bot", () => {
  const ready = { ok: false, problems: [F.makeProblem("contract", [{ code: "contract_missing", message: "x", fix: "import_contract" }]), F.makeProblem("bot", [{ code: "stale", message: "y", fix: "init" }], { bot_id: "b-1", seat: "N1", name: "Alice" })] };
  assert.equal(Lib.problemsOf(ready, "b-1").items[0].code, "stale");
  assert.equal(Lib.problemsOf(ready, "b-2"), null);
  assert.equal(Lib.problemsOf(null, "b-1"), null);
});

test("a proposal names its section; an extra section by its title (found by slug or by title)", () => {
  const extras = [{ title: "口头禅", slug: "x1" }, { title: "Quirks", slug: "quirks" }];
  assert.equal(Lib.stagingLabel("voice", extras), "说话方式");
  assert.equal(Lib.stagingLabel("identity", []), "身份");
  assert.equal(Lib.stagingLabel("extra:x1", extras), "附加：口头禅");
  assert.equal(Lib.stagingLabel("extra:QUIRKS", extras), "附加：Quirks");
  assert.equal(Lib.stagingLabel("extra:new-one", extras), "附加：new-one");
});

test("a line diff keeps the lines they share and marks the others", () => {
  assert.deepEqual(Lib.lineDiff("a\nb\nc", "a\nx\nc"), [
    { kind: "same", text: "a" },
    { kind: "del", text: "b" },
    { kind: "add", text: "x" },
    { kind: "same", text: "c" },
  ]);
  assert.deepEqual(Lib.lineDiff("", "新的").map((l) => l.kind), ["del", "add"], "an empty text is one empty line");
  assert.deepEqual(Lib.lineDiff("same", "same"), [{ kind: "same", text: "same" }]);
  assert.deepEqual(Lib.lineDiff("a\r\nb", "a\nb").map((l) => l.kind), ["same", "same"], "line ends do not matter");
  const long = Array.from({ length: 60 }, (_, i) => `line ${i}`).join("\n");
  assert.equal(Lib.lineDiff(long, long.replace("line 30", "LINE 30")).filter((l) => l.kind !== "same").length, 2);
});

test("the fingerprint of a bot changes with what the panel shows of it (and only that)", () => {
  const a = F.makeBotSummary(1);
  assert.equal(Lib.fingerprint(a), Lib.fingerprint(JSON.parse(JSON.stringify(a))));
  for (const change of [{ name: "Other" }, { frozen: false }, { staging: 2 }, { init: { status: "stale" } }, { memory_stats: { playbook: 9 } }, { core: { updated_at: 5 } }, { avatar_code: "x" }]) {
    assert.notEqual(Lib.fingerprint(F.makeBotSummary(1, change)), Lib.fingerprint(a), JSON.stringify(change));
  }
  assert.equal(Lib.fingerprint(F.makeBotSummary(1, { binding: { ok: false, label: "" } })), Lib.fingerprint(a), "the model binding is not part of the detail");
  assert.equal(Lib.fingerprint(null), "");
});

test("the editor model of an L0: from a BotDetail, dirty or not, copied without sharing", () => {
  const d = F.makeBotDetail(1);
  const m = Lib.coreModel(d.core);
  assert.deepEqual(Object.keys(m), ["identity", "voice", "mind", "extras"]);
  assert.equal(m.extras[0].title, "口头禅");
  assert.equal(Lib.coreDirty(m, Lib.coreModel(d.core)), false);
  const c = Lib.cloneCore(m);
  c.extras[0].text += "!";
  assert.equal(Lib.coreDirty(c, m), true);
  assert.equal(m.extras[0].text.endsWith("!"), false, "a copy shares nothing");
  assert.equal(Lib.coreDirty({ ...m, voice: `  ${m.voice}\n` }, m), false, "white space around a text is not a change");
  assert.deepEqual(Lib.coreModel(null), { identity: "", voice: "", mind: "", extras: [] });
  assert.equal(Lib.coreStats(Lib.coreModel(null)).complete, false);
  const stats = Lib.coreStats(m);
  assert.deepEqual([stats.complete, stats.canSave, stats.missing], [true, true, []]);
  assert.equal(Lib.coreStats({ ...m, voice: "字".repeat(601) }).canSave, false);
});

test("times and durations the way the page says them", () => {
  assert.equal(Lib.formatDuration(74.2), "1 分 14 秒");
  assert.equal(Lib.formatDuration(40), "40 秒");
  assert.equal(Lib.formatDuration(0), "0 秒");
  assert.equal(Lib.formatDuration(-1), "");
  assert.equal(Lib.formatDuration("x"), "");
  assert.equal(Lib.formatUnix(0), "");
  assert.equal(Lib.formatUnix(null), "");
  assert.match(Lib.formatUnix(1763600000), /^\d{4}-\d\d-\d\d \d\d:\d\d$/);
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log(`ok   - ${name}`);
    } catch (e) {
      failed++;
      console.log(`FAIL - ${name}\n${e && e.stack ? e.stack : e}`);
    }
  }
  console.log(`\n${tests.length - failed}/${tests.length} passed`);
  process.exit(failed ? 1 : 0);
})();
