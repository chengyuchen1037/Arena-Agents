"use strict";
// The settings dialog (static/js/archive.js, plan F24 / F26-F32, contract section 11.9) against the fake DOM and the fake
// backend of harness.js: the export wizard, the import wizard (XHR upload), the progress events and the "old data found"
// notice bar.  The removed old entries (folder, save, single-game import, memory packs) are asserted in test_app_smoke.js.
// Run: node tests/frontend/test_archive_ui.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const H = require("./harness.js");

const { F, target, settle, bootLive } = H;
require(path.join(H.ROOT, "static", "js", "archive.js")); // (also loaded into every app sandbox by the harness)
const Archive = globalThis.ArenaArchiveUI.helpers;

const CASES = JSON.parse(fs.readFileSync(path.join(H.ROOT, "tests", "archive", "password_cases.json"), "utf8"));
const PW = "Correct-Horse-Battery-9";
const NAME = "arena-20261118-004207.arenaagents";

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// ---- helpers ----------------------------------------------------------------------------------
async function live(over = {}, overrides = {}, envOpts = {}) {
  const session = F.makeSession({ event_id: 500, status: "paused", ...over });
  const ctx = await bootLive({ session, ...overrides }, envOpts);
  ctx.env.sse("snapshot", { session }, 500);
  return ctx;
}
const body = (env) => env.el("settings-body").innerHTML;
const textOf = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
const title = (env) => env.el("settings-title").textContent;
const act = (env, action) => env.el("settings-footer").dispatch("click", { target: target({ "[data-action]": { "data-action": action } }) });
const typeInto = (env, id, value) => env.el("settings-body").dispatch("input", { target: { id, value } });
const tickLabel = (env) => env.el("archive-phase").textContent;
/** The path the result dialog shows (it is inside the rendered HTML, not a separate fake element). */
const savedPath = (env) => ((body(env).match(/id="archive-saved-path">([^<]*)</) || [])[1] || "").replace(/&amp;/g, "&");
const barWidth = (env) => env.el("archive-bar-fill").style.width;
const modalOpen = (env) => env.el("settings-modal").style.display === "flex";
async function openSettings(env) {
  env.el("btn-open-settings-modal").dispatch("click");
  await settle();
}
/** Run the timer of the dialog (the 1.5 s poll / the reload delay), not the app's own ones. */
function runDialogTimer(env) {
  const i = env.timers.findIndex((t) => t.ms === 1500);
  assert.ok(i >= 0, "the dialog scheduled a timer");
  const [timer] = env.timers.splice(i, 1);
  timer.fn();
}
const progress = (env, data, id) => env.sse("archive.progress", { done: 0, total: 1, status: "running", ...data }, id);

const SUMMARY = (over = {}) => ({
  archive_id: "0123456789abcdef0123456789abcdef",
  format_version: 1,
  app_version: "v0.13.0",
  created_at: "2026-11-18T00:42:07-08:00",
  size_bytes: 123456,
  uncompressed_bytes: 654321,
  file_count: 20,
  games: 3,
  providers: 2,
  roster_seats: 12,
  has_secrets: false,
  has_logs: true,
  provider_names: ["DeepSeek", "Claude"],
  game_list: [],
  games_listed: 0,
  warnings: [],
  ...over,
});
const EXPORT_RESULT = {
  name: NAME,
  display_path: `data/exports/${NAME}`,
  size_bytes: 9800000,
  archive_id: "0123456789abcdef0123456789abcdef",
  created_at: "2026-11-18T00:42:07-08:00",
  app_version: "v0.13.0",
  contents: { games: 3, providers: 2, roster_seats: 12, has_secrets: false, has_logs: true },
  file_count: 31,
  uncompressed_bytes: 48000000,
  warnings: [],
};

// ---- the password rule ---------------------------------------------------------------------------
test("the password rule is the one of the server: the same table (tests/archive/password_cases.json) passes both", () => {
  for (const c of CASES) assert.equal(Archive.passwordProblem(c.password) === null, c.ok, `${c.note}: ${JSON.stringify(c.password.slice(0, 30))}`);
  assert.deepEqual(Archive.passwordClasses("Abc123!!"), ["upper", "lower", "digit", "symbol"]);
  assert.deepEqual(Archive.passwordClasses("abc"), ["lower"]);
  assert.equal(Archive.passwordProblem(undefined), "请输入密码。");
  assert.match(Archive.passwordProblem("short"), /至少需要 12 位（现在 5 位）/);
  assert.match(Archive.passwordProblem("alllowercaseletters"), /至少 3 类（现在 1 类）/);
  assert.match(Archive.RULE_TEXT, /12/);
  assert.match(Archive.RULE_TEXT, /三类/);
});

test("pure helpers: sizes and the overall percentage of a job", () => {
  assert.equal(Archive.formatBytes(512), "512 B");
  assert.equal(Archive.formatBytes(1536), "1.5 KB");
  assert.equal(Archive.formatBytes(5 * 1024 * 1024), "5.0 MB");
  assert.equal(Archive.formatBytes(3 * 1024 ** 3), "3.00 GB");
  assert.equal(Archive.formatBytes(-1), "—");
  assert.equal(Archive.overallPercent("export", "pack", 6, 12), 60);
  assert.equal(Archive.overallPercent("export", "flush", 0, 1), 0);
  assert.equal(Archive.overallPercent("export", "finalize", 1, 1), 100);
  assert.equal(Archive.overallPercent("import", "verify", 1, 2), 30);
  assert.equal(Archive.overallPercent("import", "unknown", 1, 2), 0);
  assert.equal(Archive.overallPercent("import", "replace", 5, 4), 85, "never beyond the phase");
  const pct = [0, 3, 6, 12].map((d) => Archive.overallPercent("export", "pack", d, 12));
  assert.deepEqual([...pct].sort((a, b) => a - b), pct);
});

// ---- the dialog ----------------------------------------------------------------------------------
test("⚙ 设置 opens a dialog with exactly two things - export and import; ✕, Esc and the dark backdrop close it", async () => {
  const { env, backend } = await live();
  assert.notEqual(env.el("settings-modal").style.display, "flex");
  await openSettings(env);
  assert.ok(modalOpen(env));
  assert.equal(title(env), "设置");
  const html = body(env);
  assert.equal([...html.matchAll(/class="settings-section-title"/g)].length, 2);
  assert.match(textOf(html), /导出存档/);
  assert.match(textOf(html), /导入存档/);
  assert.ok(backend.calls.some((c) => c.method === "GET" && c.url.endsWith("/api/archive/status")), "it asks what the server is doing");
  env.el("btn-close-settings-modal").dispatch("click");
  assert.ok(!modalOpen(env));
  await openSettings(env);
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.ok(!modalOpen(env));
  await openSettings(env);
  env.el("settings-modal").dispatch("click"); // the click lands on the backdrop itself
  assert.ok(!modalOpen(env));
  await openSettings(env);
  env.el("settings-modal").dispatch("click", { target: target({}) }); // ... a click inside the card does not close it
  assert.ok(modalOpen(env));
  assert.ok(env.app().archive(), "the app exposes it");
});

test("while a game runs the import is disabled with a hint to pause first; the export stays; both follow the game", async () => {
  const { env } = await live({ status: "running" });
  await openSettings(env);
  assert.match(body(env), /id="btn-archive-import"[^>]*disabled/);
  assert.doesNotMatch(body(env), /id="btn-archive-export"[^>]*disabled/);
  assert.match(textOf(body(env)), /对局正在运行：请先点击「暂停」/);
  env.sse("game.status", { status: "paused", reason: "user", winner: null });
  assert.doesNotMatch(body(env), /id="btn-archive-import"[^>]*disabled/);
  assert.doesNotMatch(textOf(body(env)), /对局正在运行/);
  env.sse("game.status", { status: "running", reason: "resumed", winner: null });
  assert.match(body(env), /id="btn-archive-import"[^>]*disabled/);
});

test("a replay on screen does not hide the live game: the import follows the LIVE game's status, also while the dialog is open", async () => {
  const { env } = await live({ status: "paused" });
  await openSettings(env);
  assert.doesNotMatch(body(env), /id="btn-archive-import"[^>]*disabled/);
  // a saved game is replayed: the screen shows another session
  const initial = F.makeSession({ session_id: "old-1", status: "idle", messages: [], requests: [], channels: [], phase_text: "" });
  const events = [
    { seq: 1, gt: 0, type: "game.status", data: { status: "running", reason: "start", winner: null } },
    { seq: 2, gt: 5, type: "game.status", data: { status: "finished", reason: "end_game", winner: "好人阵营", summary: "x" } },
  ];
  env.app().replay().start({ version: 1, game: { session_id: "old-1", status: "finished" }, initial, events, duration_gt: 5 }, { session_id: "old-1", scenario_title: "狼人杀 12 人局" });
  env.flushRaf();
  assert.ok(env.app().state.session.replay, "the replay is on screen");
  assert.equal(env.app().state.session.status !== "running", true);
  // ... and the live game starts in the background (another tab): the open dialog follows it
  env.sse("game.status", { status: "running", reason: "started", winner: null });
  assert.equal(env.app().liveSession().status, "running");
  assert.match(body(env), /id="btn-archive-import"[^>]*disabled/);
  assert.match(textOf(body(env)), /对局正在运行：请先点击「暂停」/);
  env.sse("game.status", { status: "paused", reason: "user", winner: null });
  assert.doesNotMatch(body(env), /id="btn-archive-import"[^>]*disabled/);
  // a snapshot of the live stream (a reconnect) replaces the live game, never the replay: the dialog follows that too
  env.sse("snapshot", { session: { ...env.app().liveSession(), status: "running" } });
  assert.ok(env.app().state.session.replay);
  assert.match(body(env), /id="btn-archive-import"[^>]*disabled/);
});

// ---- export --------------------------------------------------------------------------------------
test("export without the API key: ask, ask about the logs (kept by default), start - no password anywhere", async () => {
  const { env, backend } = await live();
  await openSettings(env);
  act(env, "export-start");
  assert.match(title(env), /1\/3 API Key/);
  assert.match(textOf(body(env)), /要把 API Key 一起导出吗/);
  assert.match(footer(env), /btn-export-keys-no/);
  act(env, "export-keys-no");
  assert.match(title(env), /2\/2 思考日志/);
  assert.match(body(env), /id="chk-archive-logs" checked/, "the logs are included unless the user says otherwise");
  assert.doesNotMatch(body(env), /input-archive-password/);
  act(env, "export-run");
  await settle();
  assert.deepEqual(backend.exportCalls, [{ include_keys: false, include_logs: true }]);
  assert.equal(title(env), "正在导出存档");
  assert.equal(env.app().archive()._state.export.jobId, "job-export-1");
});
const footer = (env) => env.el("settings-footer").innerHTML;

test("export with the API key: the password is typed twice, the rule shows live, the next step needs a strong, matching one", async () => {
  const { env, backend } = await live();
  await openSettings(env);
  act(env, "export-start");
  act(env, "export-keys-yes");
  assert.match(title(env), /2\/3 设置密码/);
  assert.equal(env.el("btn-archive-next").disabled, true);
  const rules = () => env.el("archive-pw-rules").innerHTML;
  assert.match(rules(), /至少 12 位（现在 0 位）/);

  typeInto(env, "input-archive-password", "short");
  typeInto(env, "input-archive-password2", "short");
  assert.equal(env.el("btn-archive-next").disabled, true, "too short");
  assert.match(rules(), /class="bad">✗ 至少 12 位（现在 5 位）/);
  assert.match(rules(), /class="ok">✓ 两次输入一致/);

  typeInto(env, "input-archive-password", "alllowercaseletters");
  typeInto(env, "input-archive-password2", "alllowercaseletters");
  assert.equal(env.el("btn-archive-next").disabled, true, "one class of characters");
  assert.match(rules(), /class="ok">✓ 至少 12 位（现在 19 位）/);
  assert.match(rules(), /class="bad">✗ 大写 \/ 小写 \/ 数字 \/ 符号中至少三类：大写✗ 小写✓ 数字✗ 符号✗/);

  typeInto(env, "input-archive-password", PW);
  assert.equal(env.el("btn-archive-next").disabled, true, "the two entries differ");
  assert.match(rules(), /class="bad">✗ 两次输入一致/);
  typeInto(env, "input-archive-password2", PW);
  assert.equal(env.el("btn-archive-next").disabled, false);
  assert.doesNotMatch(rules(), /class="bad"/);
  assert.match(rules(), /大写✓ 小写✓ 数字✓ 符号✓/);

  act(env, "export-to-logs");
  assert.match(title(env), /3\/3 思考日志/);
  env.el("settings-body").dispatch("change", { target: { id: "chk-archive-logs", checked: false } });
  act(env, "export-run");
  await settle();
  assert.deepEqual(backend.exportCalls, [{ include_keys: true, include_logs: false, password: PW }]);
  // back and forth keeps what was typed
  assert.equal(env.app().archive()._state.export.password, PW);
});

test("an incomplete / weak password cannot move on, even when the button is clicked anyway", async () => {
  const { env } = await live();
  await openSettings(env);
  act(env, "export-start");
  act(env, "export-keys-yes");
  typeInto(env, "input-archive-password", "weak");
  typeInto(env, "input-archive-password2", "weak");
  act(env, "export-to-logs");
  assert.match(title(env), /2\/3 设置密码/, "still on the password step");
  typeInto(env, "input-archive-password", PW);
  act(env, "export-to-logs");
  assert.match(title(env), /2\/3 设置密码/, "the entries still differ");
});

test("the way back: logs -> password -> keys; choosing 'no key' forgets a typed password", async () => {
  const { env, backend } = await live();
  await openSettings(env);
  act(env, "export-start");
  act(env, "export-keys-yes");
  typeInto(env, "input-archive-password", PW);
  typeInto(env, "input-archive-password2", PW);
  act(env, "export-to-logs");
  act(env, "export-to-password");
  assert.match(title(env), /2\/3/);
  act(env, "export-to-keys");
  act(env, "export-keys-no");
  assert.match(title(env), /2\/2/);
  act(env, "export-run");
  await settle();
  assert.deepEqual(backend.exportCalls, [{ include_keys: false, include_logs: true }], "no password is sent when no key is exported");
});

test("progress: SSE events move the bar through the phases; the end opens the result with the saved path and a download button", async () => {
  const { env, backend } = await live();
  await openSettings(env);
  act(env, "export-start");
  act(env, "export-keys-no");
  act(env, "export-run");
  await settle();
  const job = "job-export-1";
  progress(env, { job_id: job, kind: "export", phase: "flush", done: 0, total: 1 });
  assert.equal(tickLabel(env), "正在保存当前对局…");
  assert.equal(barWidth(env), "0%");
  progress(env, { job_id: job, kind: "export", phase: "pack", done: 6000000, total: 12000000 });
  assert.equal(tickLabel(env), "正在打包…");
  assert.equal(barWidth(env), "60%");
  assert.match(env.el("archive-percent").textContent, /60% · 5\.7 MB \/ 11\.4 MB/);
  progress(env, { job_id: job, kind: "export", phase: "finalize", done: 1, total: 1 });
  assert.equal(barWidth(env), "100%");
  assert.equal(title(env), "正在导出存档");

  // the job's own answer carries the absolute path (only a client on the server's machine gets it)
  backend.archiveJobs[job] = { job_id: job, kind: "export", phase: "finalize", done: 1, total: 1, status: "done", result: { ...EXPORT_RESULT, path: `D:\\Arena-Agents\\data\\exports\\${NAME}` } };
  progress(env, { job_id: job, kind: "export", phase: "finalize", done: 1, total: 1, status: "done", result: EXPORT_RESULT });
  await settle();
  assert.equal(title(env), "导出完成");
  assert.equal(savedPath(env), `D:\\Arena-Agents\\data\\exports\\${NAME}`);
  const html = textOf(body(env));
  assert.match(html, /9\.3 MB/);
  assert.match(html, /3 局对局 · 2 个 Provider · 不含 API Key · 含思考日志/);
  assert.match(footer(env), /btn-archive-download/);
  assert.ok(env.toasts.some((t) => /存档已导出/.test(t.textContent)));
});

test("a remote browser gets no server path: the result shows data/exports/<name>", async () => {
  const { env } = await live();
  await openSettings(env);
  act(env, "export-start");
  act(env, "export-keys-no");
  act(env, "export-run");
  await settle();
  progress(env, { job_id: "job-export-1", kind: "export", phase: "finalize", done: 1, total: 1, status: "done", result: EXPORT_RESULT });
  await settle(); // (the fake server has no job record: the event's result is what there is)
  assert.equal(title(env), "导出完成");
  assert.equal(savedPath(env), `data/exports/${NAME}`);
});

test("the download button saves the file through a link; with an access token the link carries it (a link cannot send a header)", async () => {
  const { env } = await live();
  await openSettings(env);
  act(env, "export-start");
  act(env, "export-keys-no");
  act(env, "export-run");
  await settle();
  progress(env, { job_id: "job-export-1", kind: "export", phase: "finalize", done: 1, total: 1, status: "done", result: EXPORT_RESULT });
  await settle();
  act(env, "export-download");
  assert.deepEqual(env.downloads, [{ href: `/arena/api/archive/exports/${NAME}`, download: NAME }]);

  const secured = await live({}, { requireToken: "tok" }, { storedToken: "tok" });
  await openSettings(secured.env);
  act(secured.env, "export-start");
  act(secured.env, "export-keys-no");
  act(secured.env, "export-run");
  await settle();
  progress(secured.env, { job_id: "job-export-1", kind: "export", phase: "finalize", done: 1, total: 1, status: "done", result: EXPORT_RESULT });
  await settle();
  act(secured.env, "export-download");
  assert.deepEqual(secured.env.downloads, [{ href: `/arena/api/archive/exports/${NAME}?token=tok`, download: NAME }]);
});

test("the dialog can be closed while the server packs; the result pops up when it is done", async () => {
  const { env } = await live();
  await openSettings(env);
  act(env, "export-start");
  act(env, "export-keys-no");
  act(env, "export-run");
  await settle();
  progress(env, { job_id: "job-export-1", kind: "export", phase: "pack", done: 1, total: 4 });
  act(env, "close");
  assert.ok(!modalOpen(env));
  progress(env, { job_id: "job-export-1", kind: "export", phase: "pack", done: 3, total: 4 }); // painting a closed dialog is harmless
  await openSettings(env);
  assert.equal(title(env), "正在导出存档", "it comes back where it was");
  act(env, "close");
  progress(env, { job_id: "job-export-1", kind: "export", phase: "finalize", done: 1, total: 1, status: "done", result: EXPORT_RESULT });
  await settle();
  assert.ok(modalOpen(env), "the finished export opens the dialog by itself");
  assert.equal(title(env), "导出完成");
  act(env, "close");
  assert.ok(!modalOpen(env));
  await openSettings(env);
  assert.equal(title(env), "设置", "closing the result starts over");
});

// The fake DOM invents any element a script looks up by id, so a bar the markup forgot to draw would still "work" there.
// In a real browser the events only PATCH the label / bar / percentage (updateProgressDom): they exist only if the view
// was drawn with them.  So these tests read the HTML the dialog really rendered (found in a real browser: the export
// showed the phase text but no bar, because the "starting" state drew none).
const barDrawn = (env) => /id="archive-phase"/.test(body(env)) && /class="archive-bar"/.test(body(env)) && /id="archive-bar-fill"/.test(body(env)) && /id="archive-percent"/.test(body(env));

test("every progress view is drawn WITH its bar from the first paint, also while the job is still starting", async () => {
  // ---- export: the POST is still in flight, then the job id arrives, then an event
  const { env } = await live();
  await openSettings(env);
  act(env, "export-start");
  act(env, "export-keys-no");
  act(env, "export-run"); // (not settled yet: the request has not been answered)
  assert.equal(title(env), "正在导出存档");
  assert.ok(barDrawn(env), "starting: the bar is there at 0 %");
  assert.match(body(env), /id="archive-phase">正在启动…</);
  assert.match(body(env), /id="archive-bar-fill" style="width:0%"/);
  await settle();
  assert.ok(barDrawn(env), "the job id arrived");
  assert.equal(tickLabel(env), "正在保存当前对局…", "the label leaves 'starting' as soon as the answer names the job");
  progress(env, { job_id: "job-export-1", kind: "export", phase: "pack", done: 3, total: 4 });
  assert.ok(barDrawn(env), "a running event");
  assert.equal(tickLabel(env), "正在打包…");

  // ---- import: upload, server-side check, summary, apply (starting, then running)
  const imp = await live({}, { uploadManual: true });
  await openSettings(imp.env);
  act(imp.env, "import-pick");
  pickFile(imp.env, FILE);
  assert.equal(title(imp.env), "正在上传存档");
  assert.ok(barDrawn(imp.env), "uploading");
  imp.env.xhrs[0].progress(50000, 100000);
  imp.env.xhrs[0].uploaded();
  assert.equal(title(imp.env), "正在校验存档");
  assert.ok(barDrawn(imp.env), "the server is checking the file");
  imp.env.xhrs[0].respond(200, { import_id: "imp-1", name: "my backup.arenaagents", size_bytes: 100000, summary: SUMMARY(), expires_in: 7200 });
  assert.equal(title(imp.env), "导入存档 · 确认");
  act(imp.env, "import-apply-skip"); // (not settled yet)
  assert.equal(title(imp.env), "正在导入存档");
  assert.ok(barDrawn(imp.env), "import starting: the bar is there at 0 %");
  assert.match(body(imp.env), /id="archive-phase">正在启动…</);
  await settle();
  assert.ok(barDrawn(imp.env), "import job id arrived");
  assert.equal(tickLabel(imp.env), "正在校验并解包…");
  progress(imp.env, { job_id: "job-import-1", kind: "import", phase: "replace", done: 1, total: 4 });
  assert.ok(barDrawn(imp.env));
  assert.equal(tickLabel(imp.env), "正在替换数据…");
});

test("opening the dialog on a job that is already running, and the page coming back to a running one, draw the bar as well", async () => {
  const { env } = await live({}, { archiveStatus: { busy: true, job: { job_id: "j9", kind: "export", phase: "pack", done: 6, total: 12, status: "running" }, import: null, game_active: false } });
  await openSettings(env);
  assert.equal(title(env), "正在导出存档");
  assert.ok(barDrawn(env));
  assert.match(body(env), /id="archive-bar-fill" style="width:60%"/, "25 + 70 * 6/12 = 60 %");
});

test("events that arrive before the HTTP answer are kept, and only the ones of OUR job count", async () => {
  const { env } = await live();
  await openSettings(env);
  act(env, "export-start");
  act(env, "export-keys-no");
  act(env, "export-run"); // the POST is in flight ...
  progress(env, { job_id: "intruder", kind: "export", phase: "pack", done: 4, total: 4 }); // (somebody else's job)
  progress(env, { job_id: "job-export-1", kind: "export", phase: "pack", done: 3, total: 4 }); // ... and ours is already running
  await settle(); // the answer names the job
  assert.equal(tickLabel(env), "正在打包…");
  assert.equal(barWidth(env), "78%", "25 + 70 * 3/4 = 77.5: ours, not the intruder's 95 %");
});

test("the poll catches a finish the stream missed", async () => {
  const { env, backend } = await live();
  await openSettings(env);
  act(env, "export-start");
  act(env, "export-keys-no");
  act(env, "export-run");
  await settle();
  backend.archiveJobs["job-export-1"] = { job_id: "job-export-1", kind: "export", phase: "pack", done: 1, total: 4, status: "running" };
  runDialogTimer(env);
  await settle();
  assert.equal(barWidth(env), "43%", "the poll updated the bar (25 + 70 * 1/4 = 42.5)");
  backend.archiveJobs["job-export-1"] = { job_id: "job-export-1", kind: "export", phase: "finalize", done: 1, total: 1, status: "done", result: { ...EXPORT_RESULT, path: "D:\\x\\" + NAME } };
  runDialogTimer(env);
  await settle();
  assert.equal(title(env), "导出完成");
  assert.equal(savedPath(env), "D:\\x\\" + NAME);
  assert.ok(!env.timers.some((t) => t.ms === 1500), "nothing keeps polling after the end");
});

test("an export that fails ends in a message with the reason and a way to start over; a double end event shows once", async () => {
  const { env } = await live();
  await openSettings(env);
  act(env, "export-start");
  act(env, "export-keys-no");
  act(env, "export-run");
  await settle();
  const fail = { job_id: "job-export-1", kind: "export", phase: "pack", done: 1, total: 4, status: "error", code: "io_error", message: "磁盘空间不足，导出已中止。" };
  progress(env, fail);
  assert.equal(title(env), "导出失败");
  assert.match(textOf(body(env)), /磁盘空间不足，导出已中止。/);
  progress(env, fail); // (the poll reports it again)
  assert.equal(title(env), "导出失败");
  act(env, "export-start");
  assert.match(title(env), /1\/3 API Key/, "start over");
});

test("the server refusing the password (422 weak_password) sends the user back to the password step with the reason", async () => {
  const { env } = await live({}, { exportReply: [422, { detail: "密码至少需要 12 位（现在 5 位）。", code: "weak_password" }] });
  await openSettings(env);
  act(env, "export-start");
  act(env, "export-keys-yes");
  typeInto(env, "input-archive-password", PW);
  typeInto(env, "input-archive-password2", PW);
  act(env, "export-to-logs");
  act(env, "export-run");
  await settle();
  assert.match(title(env), /2\/3 设置密码/);
  assert.match(textOf(body(env)), /密码至少需要 12 位（现在 5 位）。/);
  assert.equal(env.el("btn-archive-next").disabled, false, "what was typed is still there");
});

test("another failure of the POST (a running import, no disk) is shown as an export error", async () => {
  const { env } = await live({}, { exportReply: [409, { detail: "已有一个导出或导入任务正在进行，请等它结束。", code: "busy" }] });
  await openSettings(env);
  act(env, "export-start");
  act(env, "export-keys-no");
  act(env, "export-run");
  await settle();
  assert.equal(title(env), "导出失败");
  assert.match(textOf(body(env)), /已有一个导出或导入任务正在进行/);
});

test("somebody else's job (another tab) is shown as 'busy' and disables the buttons until it ends", async () => {
  const { env } = await live();
  await openSettings(env);
  progress(env, { job_id: "other", kind: "export", phase: "pack", done: 1, total: 4 });
  assert.match(body(env), /id="btn-archive-export"[^>]*disabled/);
  assert.match(body(env), /id="archive-busy"/);
  progress(env, { job_id: "other", kind: "export", phase: "finalize", done: 1, total: 1, status: "done", result: EXPORT_RESULT });
  assert.doesNotMatch(body(env), /id="btn-archive-export"[^>]*disabled/);
  assert.doesNotMatch(body(env), /archive-busy/);
});

test("opening the dialog while the server is exporting follows that job", async () => {
  const { env } = await live({}, { archiveStatus: { busy: true, job: { job_id: "j9", kind: "export", phase: "pack", done: 6, total: 12, status: "running" }, import: null, game_active: false } });
  await openSettings(env);
  assert.equal(title(env), "正在导出存档");
  assert.equal(env.app().archive()._state.export.jobId, "j9");
  progress(env, { job_id: "j9", kind: "export", phase: "finalize", done: 1, total: 1, status: "done", result: EXPORT_RESULT });
  await settle();
  assert.equal(title(env), "导出完成");
});

// ---- import --------------------------------------------------------------------------------------
const pickFile = (env, file) => env.el("file-archive-import").dispatch("change", { target: { files: [file], value: "x" } });
const FILE = { name: "my backup.arenaagents", size: 100000 };

test("import: choose a file, the upload shows its progress (XHR), then the server's check, then the summary of what is inside", async () => {
  const { env } = await live({}, { uploadManual: true });
  await openSettings(env);
  act(env, "import-pick");
  assert.equal(title(env), "导入存档");
  assert.match(textOf(body(env)), /整体替换.*当前数据/);
  let clicked = 0;
  env.el("file-archive-import").click = () => clicked++;
  act(env, "import-choose");
  assert.equal(clicked, 1, "the hidden file input is opened");
  pickFile(env, FILE);
  assert.equal(title(env), "正在上传存档");
  const xhr = env.xhrs[0];
  assert.equal(xhr.method, "POST");
  assert.equal(xhr.url, "/arena/api/archive/import?name=my%20backup.arenaagents");
  assert.equal(xhr.headers["Content-Type"], "application/octet-stream");
  assert.equal(xhr.body, FILE, "the File itself is sent (streamed by the browser), never read into memory");
  assert.ok(!("X-Arena-Token" in xhr.headers));
  xhr.progress(25000, 100000);
  assert.equal(barWidth(env), "25%");
  assert.match(env.el("archive-percent").textContent, /25% · 24\.4 KB \/ 97\.7 KB/);
  xhr.progress(100000, 100000);
  xhr.uploaded();
  assert.equal(title(env), "正在校验存档");
  progress(env, { job_id: "imp-1", kind: "import", phase: "verify", done: 1, total: 4 });
  assert.equal(barWidth(env), "25%");
  xhr.respond(200, { import_id: "imp-1", name: "my backup.arenaagents", size_bytes: 100000, summary: SUMMARY(), expires_in: 7200 });
  assert.equal(title(env), "导入存档 · 确认");

  const facts = textOf(env.el("settings-body").innerHTML);
  assert.match(facts, /my backup\.arenaagents · 97\.7 KB/);
  assert.match(facts, /2026-11-18 \d\d:\d\d/, "the creation time");
  assert.match(facts, /v0\.13\.0/);
  assert.match(facts, /对局 3 局/);
  assert.match(facts, /Provider 2 个（DeepSeek、Claude）/);
  assert.match(facts, /名册 12 个座位/);
  assert.match(facts, /API Key 不含/);
  assert.match(facts, /思考日志 含/);
  assert.match(facts, /将\s*整体替换当前数据.*旧数据会先备份到\s*data\/backups\/.*最近 3 份/);
  assert.doesNotMatch(body(env), /input-archive-import-password/, "no keys inside: no password asked");
  assert.match(footer(env), /btn-archive-apply/);
  assert.doesNotMatch(footer(env), /btn-archive-apply-skip"/);
});

test("import: the access token goes to the upload as a header", async () => {
  const { env } = await live({}, { requireToken: "tok", uploadManual: true }, { storedToken: "tok" });
  await openSettings(env);
  act(env, "import-pick");
  pickFile(env, FILE);
  assert.equal(env.xhrs[0].headers["X-Arena-Token"], "tok");
});

test("import without keys: one button replaces everything; the job's progress and result; the page reloads by itself", async () => {
  const { env, backend } = await live();
  await openSettings(env);
  act(env, "import-pick");
  pickFile(env, FILE);
  await settle();
  assert.equal(title(env), "导入存档 · 确认");
  assert.deepEqual(backend.uploads.map((u) => [u.name, u.size]), [["my backup.arenaagents", 100000]]);
  act(env, "import-apply-skip");
  await settle();
  assert.deepEqual(backend.applyCalls, [{ import_id: "imp-1", body: { skip_keys: true } }], "no password for an archive without keys");
  assert.equal(title(env), "正在导入存档");
  assert.match(textOf(body(env)), /请不要关闭或刷新页面/);
  const job = "job-import-1";
  progress(env, { job_id: job, kind: "import", phase: "verify", done: 1, total: 2 });
  assert.equal(tickLabel(env), "正在校验并解包…");
  assert.equal(barWidth(env), "30%");
  progress(env, { job_id: job, kind: "import", phase: "backup", done: 1, total: 1 });
  assert.equal(tickLabel(env), "正在备份当前数据…");
  progress(env, { job_id: job, kind: "import", phase: "replace", done: 2, total: 4 });
  assert.equal(tickLabel(env), "正在替换数据…");
  assert.equal(barWidth(env), "78%");
  assert.equal(env.reloads.length, 0);
  progress(env, {
    job_id: job,
    kind: "import",
    phase: "finalize",
    done: 1,
    total: 1,
    status: "done",
    result: { backup: "20261118-004207", games: 3, providers: 2, keys_imported: 0, keys_kept: 1, providers_without_key: ["Claude"], warnings: ["存档里没有「上次对局」所指的对局"] },
  });
  assert.equal(title(env), "导入完成");
  const done = textOf(body(env));
  assert.match(done, /导入完成：3 局对局 · 2 个 Provider · 1 个本机 Key 已保留/);
  assert.match(done, /这些 Provider 还没有 API Key.*Claude/);
  assert.match(done, /data\/backups\/20261118-004207\//);
  assert.match(done, /存档里没有「上次对局」所指的对局/);
  assert.equal(env.reloads.length, 0, "not at once ...");
  runDialogTimer(env);
  assert.equal(env.reloads.length, 1, "... the page reloads by itself a moment later");
});

test("import: 立即刷新 reloads at once; the dialog cannot be closed while the data is being replaced", async () => {
  const { env } = await live();
  await openSettings(env);
  act(env, "import-pick");
  pickFile(env, FILE);
  await settle();
  act(env, "import-apply-skip");
  await settle();
  act(env, "close");
  assert.ok(modalOpen(env), "closing is refused while the import runs");
  assert.ok(env.toasts.some((t) => /导入进行中/.test(t.textContent)));
  env.el("btn-close-settings-modal").dispatch("click");
  assert.ok(modalOpen(env));
  progress(env, { job_id: "job-import-1", kind: "import", phase: "finalize", done: 1, total: 1, status: "done", result: { backup: null, games: 3, providers: 2, keys_imported: 0, keys_kept: 0, providers_without_key: [], warnings: [] } });
  act(env, "reload");
  assert.equal(env.reloads.length, 1);
});

const WITH_KEYS = SUMMARY({ has_secrets: true });

test("import with keys: the password field, a hint, 'skip the API keys', and the empty password is stopped before the server", async () => {
  const { env, backend } = await live({}, { importSummary: WITH_KEYS });
  await openSettings(env);
  act(env, "import-pick");
  pickFile(env, FILE);
  await settle();
  assert.match(body(env), /id="input-archive-import-password"/);
  assert.match(textOf(body(env)), /API Key 含（用密码加密）/);
  assert.match(textOf(body(env)), /跳过 API Key/);
  assert.match(footer(env), /btn-archive-apply-skip/);
  act(env, "import-apply");
  await settle();
  assert.equal((backend.applyCalls || []).length, 0, "nothing is sent without a password");
  assert.match(env.el("archive-import-password-error").textContent, /请输入存档密码，或者选择「跳过 API Key」/);
  assert.equal(env.el("archive-import-password-error").hidden, false);
  typeInto(env, "input-archive-import-password", "x");
  assert.equal(env.el("archive-import-password-error").hidden, true, "typing clears the complaint");
});

test("import with keys: a wrong password stays on the summary with the server's message and can be retried; the right one starts the job", async () => {
  const { env, backend } = await live(
    {},
    {
      importSummary: WITH_KEYS,
      applyReply: (_body, n) => (n === 1 ? [422, { detail: "密码错误或 API Key 部分已损坏", code: "wrong_password" }] : null),
    }
  );
  await openSettings(env);
  act(env, "import-pick");
  pickFile(env, FILE);
  await settle();
  typeInto(env, "input-archive-import-password", "Wrong-Horse-Battery-9");
  act(env, "import-apply");
  await settle();
  assert.deepEqual(backend.applyCalls[0].body, { skip_keys: false, password: "Wrong-Horse-Battery-9" });
  assert.equal(title(env), "导入存档 · 确认", "no job, still the summary");
  assert.match(textOf(body(env)), /密码错误或 API Key 部分已损坏/);
  assert.equal(env.el("archive-import-password-error").hidden, false);
  typeInto(env, "input-archive-import-password", PW);
  act(env, "import-apply");
  await settle();
  assert.deepEqual(backend.applyCalls[1].body, { skip_keys: false, password: PW });
  assert.equal(title(env), "正在导入存档");
  assert.equal(env.app().archive()._state.import.password, "", "the password is not kept once it was sent");
  assert.ok(!JSON.stringify(env.app().archive()._state).includes(PW));
});

test("import with keys: keys that do not match the Provider addresses are explained on the summary, and skipping them works", async () => {
  const mismatch = "存档里 Provider「DeepSeek」的协议或地址与加密 API Key 时不一致：存档在导出之后被改动过。为了安全，不导入 API Key；可以选择「跳过 API Key」导入其余内容，再在 Provider 里重新填写 Key。";
  const { env, backend } = await live({}, { importSummary: WITH_KEYS, applyReply: (_body, n) => (n === 1 ? [422, { detail: mismatch, code: "keys_mismatch" }] : null) });
  await openSettings(env);
  act(env, "import-pick");
  pickFile(env, FILE);
  await settle();
  typeInto(env, "input-archive-import-password", PW);
  act(env, "import-apply");
  await settle();
  assert.equal(title(env), "导入存档 · 确认", "no job: the upload is still there");
  assert.match(body(env), /id="archive-import-password-error" >/, "the complaint box is shown (not hidden)");
  assert.match(textOf(body(env)), /不一致.*不导入 API Key.*跳过 API Key/);
  assert.match(footer(env), /btn-archive-apply-skip/);
  act(env, "import-apply-skip");
  await settle();
  assert.deepEqual(backend.applyCalls[1].body, { skip_keys: true });
  assert.equal(title(env), "正在导入存档");
});

test("import with keys: 'skip the API keys' needs no password and says so to the server", async () => {
  const { env, backend } = await live({}, { importSummary: WITH_KEYS });
  await openSettings(env);
  act(env, "import-pick");
  pickFile(env, FILE);
  await settle();
  act(env, "import-apply-skip");
  await settle();
  assert.deepEqual(backend.applyCalls[0].body, { skip_keys: true });
});

test("import: a result with keys says how many were imported and none are missing", async () => {
  const { env } = await live({}, { importSummary: WITH_KEYS });
  await openSettings(env);
  act(env, "import-pick");
  pickFile(env, FILE);
  await settle();
  typeInto(env, "input-archive-import-password", PW);
  act(env, "import-apply");
  await settle();
  progress(env, { job_id: "job-import-1", kind: "import", phase: "keys", done: 1, total: 1 });
  assert.equal(tickLabel(env), "正在保存 API Key…");
  progress(env, { job_id: "job-import-1", kind: "import", phase: "finalize", done: 1, total: 1, status: "done", result: { backup: "b", games: 3, providers: 2, keys_imported: 2, keys_kept: 0, providers_without_key: [], warnings: [] } });
  assert.match(textOf(body(env)), /2 个 API Key 已导入/);
  assert.doesNotMatch(body(env), /archive-missing-keys/);
});

test("import errors: invalid file, a newer format, too large, a dropped connection - each says why and offers a new file", async () => {
  const cases = [
    [[422, { detail: "这不是有效的存档文件（不是 zip，或者文件已损坏）。", code: "invalid_archive" }], /不是有效的存档文件/],
    [[422, { detail: "这份存档使用更新的格式（v2），当前程序只支持到 v1。请先把 Arena-Agents 升级到较新的版本再导入。", code: "unsupported_version" }], /升级到较新的版本/],
    [[413, { detail: "存档文件超过了大小上限（2.0 GB）。可以用环境变量 ARENA_MAX_ARCHIVE_BYTES 调大。", code: "too_large" }], /ARENA_MAX_ARCHIVE_BYTES/],
    [[409, { detail: "对局正在运行：请先暂停对局，再导入存档。", code: "game_running" }], /请先暂停对局/],
  ];
  for (const [reply, pattern] of cases) {
    const { env, backend } = await live({}, { uploadReply: reply });
    await openSettings(env);
    act(env, "import-pick");
    pickFile(env, FILE);
    await settle();
    assert.equal(title(env), "导入失败");
    assert.match(textOf(body(env)), pattern);
    assert.equal((backend.applyCalls || []).length, 0);
    act(env, "import-pick");
    assert.equal(title(env), "导入存档", "pick another file");
  }
  const { env } = await live({}, { uploadManual: true });
  await openSettings(env);
  act(env, "import-pick");
  pickFile(env, FILE);
  env.xhrs[0].fail();
  assert.equal(title(env), "导入失败");
  assert.match(textOf(body(env)), /上传失败：无法连接到服务器/);
});

test("import: cancelling at the summary (or closing the dialog there) deletes the upload on the server", async () => {
  const { env, backend } = await live();
  await openSettings(env);
  act(env, "import-pick");
  pickFile(env, FILE);
  await settle();
  act(env, "import-cancel");
  await settle();
  assert.deepEqual(backend.discardCalls, ["imp-1"]);
  assert.equal(title(env), "设置");
  act(env, "import-pick");
  pickFile(env, FILE);
  await settle();
  env.el("btn-close-settings-modal").dispatch("click");
  await settle();
  assert.deepEqual(backend.discardCalls, ["imp-1", "imp-1"], "✕ at the summary discards it as well");
  assert.ok(!modalOpen(env));
});

test("import: cancelling during the upload aborts the request", async () => {
  const { env } = await live({}, { uploadManual: true });
  await openSettings(env);
  act(env, "import-pick");
  pickFile(env, FILE);
  const xhr = env.xhrs[0];
  act(env, "import-cancel");
  assert.equal(xhr.aborted, true);
  assert.equal(title(env), "设置");
  xhr.respond(200, { import_id: "late", summary: SUMMARY(), size_bytes: 1, name: "x" }); // a late answer changes nothing
  assert.equal(title(env), "设置");
});

test("import is refused while a game runs: no file dialog, no apply; a game that starts after the upload is caught too", async () => {
  const running = await live({ status: "running" });
  await openSettings(running.env);
  act(running.env, "import-pick");
  assert.match(textOf(body(running.env)), /对局正在运行：请先点击「暂停」/);
  assert.match(footer(running.env), /id="btn-archive-choose"[^>]*disabled/);
  let clicked = 0;
  running.env.el("file-archive-import").click = () => clicked++;
  act(running.env, "import-choose");
  assert.equal(clicked, 0);
  assert.ok(running.env.toasts.some((t) => /先暂停/.test(t.textContent)));

  const { env, backend } = await live({ status: "paused" });
  await openSettings(env);
  act(env, "import-pick");
  pickFile(env, FILE);
  await settle();
  env.sse("game.status", { status: "running", reason: "resumed", winner: null });
  assert.match(footer(env), /id="btn-archive-apply"[^>]*disabled/);
  assert.match(textOf(body(env)), /对局正在运行/);
  act(env, "import-apply-skip");
  await settle();
  assert.equal((backend.applyCalls || []).length, 0);
  // the server has the last word: 409 game_running puts the user back on the summary with the reason
  env.sse("game.status", { status: "paused", reason: "user", winner: null });
  backend.applyReply = [409, { detail: "对局正在运行：请先暂停对局，再导入存档。", code: "game_running" }];
  act(env, "import-apply-skip");
  await settle();
  assert.equal(title(env), "导入存档 · 确认");
  assert.ok(env.toasts.some((t) => /请先暂停对局/.test(t.textContent)));
});

test("an upload the server still holds (the page was reloaded) is offered again: continue or discard", async () => {
  const pending = { import_id: "imp-9", name: "left.arenaagents", size_bytes: 5000, summary: SUMMARY({ games: 7 }), expires_in: 100 };
  const { env, backend } = await live({}, { archiveStatus: { busy: false, job: null, import: pending, game_active: false } });
  await openSettings(env);
  assert.match(textOf(body(env)), /已上传并校验过的存档等待你决定：\s*left\.arenaagents\s*（4\.9 KB）/);
  act(env, "pending-continue");
  assert.equal(title(env), "导入存档 · 确认");
  assert.match(textOf(body(env)), /对局 7 局/);
  act(env, "import-apply-skip");
  await settle();
  assert.equal(backend.applyCalls[0].import_id, "imp-9");

  const other = await live({}, { archiveStatus: { busy: false, job: null, import: pending, game_active: false } });
  await openSettings(other.env);
  act(other.env, "pending-discard");
  await settle();
  assert.deepEqual(other.backend.discardCalls, ["imp-9"]);
  assert.doesNotMatch(body(other.env), /已上传并校验过的存档/);
});

test("a 401 on the upload asks for the access token and sends the file again with it", async () => {
  const { env, backend } = await live(
    {},
    {
      uploadReply: (xhr, size) => (xhr.headers["X-Arena-Token"] === "secret" ? [200, { import_id: "imp-1", name: "x", size_bytes: size, summary: SUMMARY(), expires_in: 1 }] : [401, { detail: "Invalid or missing API token" }]),
    }
  );
  await openSettings(env);
  act(env, "import-pick");
  pickFile(env, FILE);
  await settle();
  assert.equal(env.el("token-modal").style.display, "flex", "the token dialog is shown");
  env.el("input-api-token").value = "secret";
  env.el("token-form").dispatch("submit");
  await settle();
  assert.equal(env.xhrs.length, 2, "uploaded again");
  assert.equal(env.xhrs[1].headers["X-Arena-Token"], "secret");
  assert.equal(title(env), "导入存档 · 确认");
  assert.equal(backend.uploads.length, 2);
});

test("an import finished by ANOTHER tab reloads this page (its data is stale now)", async () => {
  const { env } = await live();
  await openSettings(env);
  progress(env, { job_id: "elsewhere", kind: "import", phase: "finalize", done: 1, total: 1, status: "done", result: { backup: "b", games: 1, providers: 1, keys_imported: 0, keys_kept: 0, providers_without_key: [], warnings: [] } });
  assert.equal(env.reloads.length, 1);
});

// ---- the "old data found" notice (F24) ------------------------------------------------------------
const LEGACY = { found: true, dismissed: false, path: "C:\\Users\\me\\Arena-Agents\\Default" };

test("old data found: the bar shows only while found and not dismissed", async () => {
  assert.equal((await live({}, { legacyData: LEGACY })).env.el("legacy-notice").hidden, false);
  assert.equal((await live({}, { legacyData: { ...LEGACY, dismissed: true } })).env.el("legacy-notice").hidden, true);
  assert.equal((await live({}, { legacyData: { found: false, dismissed: false } })).env.el("legacy-notice").hidden, true);
  assert.equal((await live()).env.el("legacy-notice").hidden, true, "nothing found: nothing shown");
  assert.match(H.HTML_SRC, /id="legacy-notice-text">检测到旧版（v0\.12\.1）的数据：请先运行转换脚本，再到「设置」里导入存档/);
});

test("old data found: 查看说明 explains the command, names the folder, and leads to the settings", async () => {
  const { env } = await live({}, { legacyData: LEGACY });
  env.el("btn-legacy-help").dispatch("click");
  await settle();
  assert.equal(env.el("dialog-modal").style.display, "flex");
  assert.equal(env.el("dialog-title").textContent, "导入旧版（v0.12.1）数据");
  const help = textOf(env.el("dialog-body").innerHTML);
  assert.match(help, /python tools\/migrate_v0121_to_archive\.py/);
  assert.match(help, /--include-keys/);
  assert.match(help, /--no-logs/);
  assert.match(help, /--workspace/);
  assert.match(help, /C:\\Users\\me\\Arena-Agents\\Default/, "the folder it found (for a client on the server's machine)");
  assert.match(help, /同一个系统用户/);
  assert.match(help, /设置.*导入存档/);
  assert.equal(env.el("btn-dialog-ok").textContent, "打开设置");
  env.el("btn-dialog-ok").dispatch("click");
  await settle();
  assert.ok(modalOpen(env), "OK opens the settings");
});

test("old data found: for a remote browser the help does not name a folder", async () => {
  const { env } = await live({}, { legacyData: { found: true, dismissed: false } });
  env.el("btn-legacy-help").dispatch("click");
  await settle();
  const help = textOf(env.el("dialog-body").innerHTML);
  assert.match(help, /migrate_v0121_to_archive/);
  assert.doesNotMatch(help, /检测到的旧工作文件夹/);
  env.el("btn-dialog-cancel").dispatch("click");
  await settle();
  assert.ok(!modalOpen(env), "closing the help does not open the settings");
});

test("old data found: 忽略 is remembered by the server and hides the bar; the next state keeps it hidden", async () => {
  const { env, backend } = await live({}, { legacyData: LEGACY });
  env.el("btn-legacy-dismiss").dispatch("click");
  await settle();
  assert.equal(backend.legacyDismissed, 1);
  assert.equal(env.el("legacy-notice").hidden, true);
  assert.equal(env.app().archive()._state.legacy.dismissed, true);
  const again = await live({}, { legacyData: { ...LEGACY, dismissed: true } });
  assert.equal(again.env.el("legacy-notice").hidden, true);
});

test("old data found: a failing dismiss keeps the bar and says so", async () => {
  const { env } = await live({}, { legacyData: LEGACY, failures: { "POST /api/notices/legacy-data:dismiss": [500, "磁盘错误"] } });
  env.el("btn-legacy-dismiss").dispatch("click");
  await settle();
  assert.equal(env.el("legacy-notice").hidden, false);
  assert.ok(env.toasts.some((t) => /磁盘错误/.test(t.textContent)));
});

H.runTests(tests);
