"use strict";
// v0.13.1 (G37-G44, contract §12.5): the memory is the impressions of the other seats + a few lessons (no recaps appended any
// more), the persona is a fixed format under a limit; the detail panel edits both, and shows what a finished review changed.
//   pure helpers   the counters use the SERVER's formula (memory_chars), the messages are the server's words
//   persona        template as the placeholder, "x / 500", refused above the limit, an old over-long one is flagged
//   memory         11 rows (avatar + name + counter), lessons box, whole-file counter, only the changed PARTS are saved
//   old memory     the "旧记忆（待整理）" section, tidied by the moderator's model or deleted
//   refine         the notes box, a draft first (structured), adopting replaces the impressions and the lessons
//   review         "旧 → 新" for every impression the review changed
// Run: node tests/frontend/test_memory_ui.js
const assert = require("node:assert/strict");
const path = require("node:path");
const H = require("./harness.js");
const Roster = require(path.join(__dirname, "..", "..", "static", "js", "roster.js"));

const { F, target, settle, bootLive } = H;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

async function live(over = {}, overrides = {}, envOpts = {}) {
  const session = F.makeSession({ event_id: 500, ...over });
  const ctx = await bootLive({ session, ...overrides }, envOpts);
  ctx.env.sse("snapshot", { session }, 500);
  return ctx;
}

const flush = async (env, ms = 30) => {
  await settle(ms);
  env.flushRaf();
};
const html = (env, id) => env.el(id).innerHTML;
const cardClick = (env, id) => env.el("participants-grid").dispatch("click", { target: target({ ".pcard": { "data-actor": id } }) });
const modClick = (env) => env.el("topbar-status").dispatch("click", { target: target({ "#mod-chip": { "data-actor": "moderator" } }) });
const panelClick = (env, action, kind) => env.el("detail-panel").dispatch("click", { target: target({ "[data-detail-action]": { "data-detail-action": action, "data-kind": kind || null } }) });
const lastToast = (env) => (env.toasts.length ? env.toasts[env.toasts.length - 1].textContent : "");
const unesc = (s) => s.replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
const textOf = (s) => unesc(s.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim());
// typing: the persona box, one impression, the lessons box, the notes box
const typePersona = (env, id, text) => {
  env.el("ta-detail-persona").value = text;
  env.el("detail-panel").dispatch("input", { target: { id: "ta-detail-persona", value: text, getAttribute: (k) => (k === "data-draft-key" ? `${id}:persona` : null) } });
};
const typeImp = (env, otherId, text) => env.el("detail-panel").dispatch("input", { target: { id: "", value: text, getAttribute: (k) => (k === "data-imp-id" ? otherId : null) } });
const typeBox = (env, boxId, text) => {
  env.el(boxId).value = text;
  env.el("detail-panel").dispatch("input", { target: { id: boxId, value: text, getAttribute: () => null } });
};
const typeExp = (env, text) => typeBox(env, "ta-detail-memory-exp", text);
const typeNotes = (env, text) => typeBox(env, "ta-detail-memory-notes", text);
/** The text of the impression box of `otherId` as the panel was DRAWN (a <textarea>: its content), or null when there is no such box. */
const impText = (env, otherId) => {
  const m = html(env, "detail-editors").match(new RegExp(`data-imp-id="${otherId}"[^>]*>([^<]*)</textarea>`));
  return m ? unesc(m[1]) : null;
};
const puts = (backend, key) => (backend.rosterPuts || []).filter((p) => key in p);
/** Open N3's panel with both editors expanded. */
async function openEditors(env, id = "N3") {
  cardClick(env, id);
  panelClick(env, "toggle-editor", "persona");
  panelClick(env, "toggle-editor", "memory");
}
const seatOf = (backend, id) => backend.roster.seats.find((s) => s.id === id);
const OTHERS_OF_N3 = F.PLAYER_IDS.filter((id) => id !== "N3").map((id) => ({ id, name: F.NAMES[F.PLAYER_IDS.indexOf(id)] }));

// =====================================================================================================
// pure helpers
// =====================================================================================================
test("the limits are the server's numbers, the contract's for whatever is missing or broken", () => {
  assert.deepEqual(Roster.DEFAULT_LIMITS, {
    impression_chars: 80, experience_items: 3, experience_chars: 300, memory_chars: 1500, legacy_chars: 600, persona_chars: 500, persona_item_chars: 40,
  });
  assert.deepEqual(Roster.normalizeLimits(undefined), Roster.DEFAULT_LIMITS);
  assert.deepEqual(Roster.normalizeLimits("x"), Roster.DEFAULT_LIMITS);
  const got = Roster.normalizeLimits({ impression_chars: 60, memory_chars: "1200", persona_chars: -5, experience_items: 0, legacy_chars: "abc", unknown: 7 });
  assert.equal(got.impression_chars, 60);
  assert.equal(got.memory_chars, 1200, "a number as text is a number");
  assert.equal(got.persona_chars, 500, "a negative limit is nonsense: the default stays");
  assert.equal(got.experience_items, 3);
  assert.equal(got.legacy_chars, 600);
  assert.ok(!("unknown" in got));
  assert.ok(Object.isFrozen(Roster.DEFAULT_LIMITS), "(and the defaults cannot be changed by accident)");
});

test("characters are counted the way the server counts them (code points; a persona without the blank lines around it)", () => {
  assert.equal(Roster.textLength("abc"), 3);
  assert.equal(Roster.textLength("你好"), 2);
  assert.equal(Roster.textLength("a😀b"), 3, "an emoji is one character, as in Python's len()");
  assert.equal(Roster.textLength(null), 0);
  assert.equal(Roster.personaText("\n\n  hi\r\nthere \n"), "  hi\nthere ");
  assert.equal(Roster.personaLength("\n\n  hi\r\nthere \n"), 11, "CRLF is one character, the blank lines around the text are not counted, spaces inside are");
  assert.equal(Roster.personaLength("   \n \t\n"), 0, "white space only is empty");
  assert.equal(Roster.personaLength(""), 0);
  assert.equal(Roster.personaLength(undefined), 0);
  // the refusal for a persona that is too long: the server's words (tests/frontend/test_roster_drift.py compares them with the backend)
  assert.equal(Roster.personaProblem("字".repeat(500), Roster.DEFAULT_LIMITS), "");
  assert.equal(Roster.personaProblem("\n" + "字".repeat(500) + "\n\n", Roster.DEFAULT_LIMITS), "", "the blank lines around it are not counted");
  assert.equal(Roster.personaProblem("字".repeat(501), Roster.DEFAULT_LIMITS), "性格设定最多 500 个字符（现在 501 个），请精简。");
  assert.equal(Roster.personaProblem("x".repeat(51), { ...Roster.DEFAULT_LIMITS, persona_chars: 50 }), "性格设定最多 50 个字符（现在 51 个），请精简。");
  assert.equal(Roster.personaProblem("x".repeat(501)), "性格设定最多 500 个字符（现在 501 个），请精简。", "no limits given: the defaults");
});

test("an impression is one line, '（暂无）' means none; lessons are one per line with the bullet marks dropped", () => {
  assert.equal(Roster.cleanImpression("  发言短，\n站边快  "), "发言短， 站边快");
  assert.equal(Roster.cleanImpression("（暂无）"), "");
  assert.equal(Roster.cleanImpression("(暂无)"), "");
  assert.equal(Roster.cleanImpression("暂无"), "");
  assert.equal(Roster.cleanImpression("暂无消息"), "暂无消息", "only the exact placeholders count");
  assert.equal(Roster.cleanImpression(null), "");
  assert.deepEqual(Roster.parseExperience("- a\n2. b\n（3）c\n\n• d\n（暂无）\n  e  \n* f"), ["a", "b", "c", "d", "e", "f"]);
  assert.deepEqual(Roster.parseExperience("一\r\n二\r三"), ["一", "二", "三"], "every kind of line break");
  assert.deepEqual(Roster.parseExperience(""), []);
  assert.deepEqual(Roster.parseExperience(undefined), []);
});

test("the whole-file counter is the server's formula: '## 印象' + a line per other seat + '## 经验' + the lessons (no record, no old text)", () => {
  const others = [{ id: "N1", name: "Alice" }, { id: "N2", name: "Bob" }];
  const text = Roster.memoryBodyText(others, { N1: "发言短" }, ["先听完"]);
  assert.equal(text, "## 印象\n- N1 Alice：发言短\n- N2 Bob：（暂无）\n\n## 经验\n- 先听完");
  assert.equal(Roster.memoryChars(others, { N1: "发言短" }, ["先听完"]), 47);
  // no lessons: the heading only, no trailing newline
  assert.equal(Roster.memoryBodyText(others, {}, []), "## 印象\n- N1 Alice：（暂无）\n- N2 Bob：（暂无）\n\n## 经验");
  // a seat whose name is empty is labelled by its id alone
  assert.equal(Roster.memoryBodyText([{ id: "N7", name: "" }], { N7: "x" }, []), "## 印象\n- N7：x\n\n## 经验");
  // a full file: eleven impressions of 80 characters and three lessons of 100 are well inside 1500 with usual names
  const eleven = F.PLAYER_IDS.slice(1).map((id, i) => ({ id, name: F.NAMES[i + 1] }));
  const imps = Object.fromEntries(eleven.map((o) => [o.id, "印".repeat(80)]));
  const total = Roster.memoryChars(eleven, imps, ["经".repeat(100), "经".repeat(100), "经".repeat(100)]);
  assert.ok(total > 1100 && total < 1500, String(total));
  // 11 empty impressions: 6 + 11 lines + 10 separators + 7 (the arithmetic done by hand, not by the formula)
  const empty = Roster.memoryChars(eleven, {}, []);
  let by = 6 + 7 + 10;
  for (const o of eleven) by += 2 + (o.id.length + 1 + o.name.length) + 1 + 4;
  assert.equal(empty, by);
});

test("memoryProblem names the first thing that is over a limit in the server's words", () => {
  const others = [{ id: "N2", name: "Bob" }, { id: "N5", name: "Eve" }];
  const L = Roster.DEFAULT_LIMITS;
  assert.equal(Roster.memoryProblem(others, { N5: "x".repeat(80) }, [], L), "");
  assert.equal(Roster.memoryProblem(others, { N2: "ok", N5: "x".repeat(81) }, [], L), "N5 Eve 的印象最多 80 字（现在 81 字），请精简。");
  assert.equal(Roster.memoryProblem(others, { N2: "y".repeat(90), N5: "x".repeat(81) }, [], L), "N2 Bob 的印象最多 80 字（现在 90 字），请精简。", "the first one found, in seat order");
  assert.equal(Roster.memoryProblem(others, {}, ["a", "b", "c", "d"], L), "经验最多 3 条（现在 4 条），请精简。");
  assert.equal(Roster.memoryProblem(others, {}, ["x".repeat(150), "y".repeat(151)], L), "经验合计最多 300 字（现在 301 字），请精简。");
  assert.equal(Roster.memoryProblem(others, {}, ["x".repeat(150), "y".repeat(150)], L), "");
  const small = { ...L, memory_chars: 60 };
  const msg = Roster.memoryProblem(others, { N2: "x".repeat(40) }, [], small);
  assert.match(msg, /^整份记忆（印象和经验两段，含序号与符号）最多 60 字（现在 \d+ 字），请精简。$/);
  assert.equal(Roster.memoryProblem(others, undefined, undefined, undefined), "", "tolerant of missing arguments");
});

test("memory_parts of a seat: the server's structure; a server without it gives the text as the old memory", () => {
  const p = Roster.normalizeParts({ record: "场次 1", impressions: { N1: "x", N2: 5 }, experience: ["a", 7], legacy: "old", extra: 1 }, "ignored");
  assert.deepEqual(p, { record: "场次 1", impressions: { N1: "x", N2: "" }, experience: ["a", "7"], legacy: "old" });
  assert.deepEqual(Roster.normalizeParts(undefined, "## 对局记忆\n旧文字"), { record: "", impressions: {}, experience: [], legacy: "## 对局记忆\n旧文字", derived: true });
  assert.deepEqual(Roster.normalizeParts([], ""), { record: "", impressions: {}, experience: [], legacy: "", derived: true });
});

// =====================================================================================================
// persona (G43, G44)
// =====================================================================================================
test("an empty persona shows the fixed format as its placeholder and an 'x / 500' counter; the hint and Save start out off", async () => {
  const { env } = await live({ status: "idle" });
  cardClick(env, "N3");
  const editors = html(env, "detail-editors");
  const ph = unesc((editors.match(/id="ta-detail-persona"[^>]*placeholder="([^"]*)"/) || [])[1] || "");
  assert.equal(ph, Roster.PERSONA_TEMPLATE);
  for (const head of ["【定位】", "【说话风格】", "【思考方式】", "【决策偏好】", "【压力下】", "【口头禅】"]) assert.ok(ph.includes(head), head);
  assert.ok(Roster.personaLength(Roster.PERSONA_TEMPLATE) < 500, "the example itself fits");
  assert.match(editors, /id="count-detail-persona">0 \/ 500</);
  assert.match(editors, /id="hint-detail-persona"[^>]* hidden/);
  assert.match(editors, /id="btn-detail-save-persona"[^>]* disabled/);
  assert.match(editors, /固定格式的性格与行为指引/, "the hint says what the persona is now");
  assert.doesNotMatch(editors, /默认为空。.*默认为空。/);
  assert.match(editors, /detail-editor-meta">空<\/span>/);
});

test("typing updates the counter, the over-the-limit hint and Save without repainting (the caret must stay)", async () => {
  const { env, backend } = await live({ status: "idle" });
  await openEditors(env);
  const painted = html(env, "detail-editors");
  typePersona(env, "N3", "冷静的逻辑派");
  assert.equal(env.el("count-detail-persona").textContent, "6 / 500");
  assert.equal(env.el("btn-detail-save-persona").disabled, false);
  assert.equal(html(env, "detail-editors"), painted, "no repaint while typing");
  // trailing blank lines and CRLF are counted like the server counts them
  typePersona(env, "N3", "ab\r\ncd\n\n");
  assert.equal(env.el("count-detail-persona").textContent, "5 / 500");
  // exactly the limit is fine
  typePersona(env, "N3", "字".repeat(500));
  assert.equal(env.el("count-detail-persona").textContent, "500 / 500");
  assert.equal(env.el("count-detail-persona").className, "detail-count");
  assert.equal(env.el("hint-detail-persona").hidden, true);
  assert.equal(env.el("btn-detail-save-persona").disabled, false);
  // one more: red, the hint, Save off
  typePersona(env, "N3", "字".repeat(501));
  assert.equal(env.el("count-detail-persona").textContent, "501 / 500");
  assert.match(env.el("count-detail-persona").className, /over/);
  assert.equal(env.el("hint-detail-persona").hidden, false);
  assert.equal(env.el("btn-detail-save-persona").disabled, true);
  panelClick(env, "save-text", "persona"); // (a stale click on the disabled button)
  await flush(env);
  assert.match(lastToast(env), /性格设定最多 500 个字符（现在 501 个），请精简。/);
  assert.equal((backend.rosterPuts || []).length, 0, "nothing was sent");
  // back to the limit and saved
  typePersona(env, "N3", "字".repeat(500));
  panelClick(env, "save-text", "persona");
  await flush(env);
  assert.deepEqual(puts(backend, "persona"), [{ id: "N3", persona: "字".repeat(500) }]);
  assert.match(lastToast(env), /性格设定已保存/);
});

test("the persona limit is the one the server sends (limits.persona_chars)", async () => {
  const { env, backend } = await live({ status: "idle" });
  backend.roster.limits.persona_chars = 50;
  await env.app().roster().load();
  await openEditors(env);
  assert.match(html(env, "detail-editors"), /id="count-detail-persona">0 \/ 50</);
  typePersona(env, "N3", "字".repeat(51));
  assert.equal(env.el("count-detail-persona").textContent, "51 / 50");
  assert.equal(env.el("btn-detail-save-persona").disabled, true);
});

test("an old over-long persona is flagged '超出上限，请精简或用主持人模型整理' and cannot be saved as it is", async () => {
  const { env, backend } = await live({ status: "idle" });
  seatOf(backend, "N3").persona = "老".repeat(600);
  await env.app().roster().load();
  cardClick(env, "N3");
  const editors = html(env, "detail-editors");
  assert.match(editors, /detail-editor-meta">⚠ 超出上限 600 \/ 500<\/span>/);
  assert.match(editors, /id="count-detail-persona">600 \/ 500</);
  assert.match(editors, /id="hint-detail-persona"[^>]*>超出上限，请精简或用主持人模型整理<\/span>/);
  assert.doesNotMatch(editors, /id="hint-detail-persona"[^>]* hidden/, "the warning is shown right away");
  assert.match(editors, /class="detail-count over"/);
  assert.match(editors, /id="btn-detail-save-persona"[^>]* disabled/);
  // shortening it makes Save usable
  panelClick(env, "toggle-editor", "persona");
  typePersona(env, "N3", "老".repeat(400));
  assert.equal(env.el("btn-detail-save-persona").disabled, false);
  assert.equal(env.el("hint-detail-persona").hidden, true);
  panelClick(env, "save-text", "persona");
  await flush(env);
  assert.deepEqual(puts(backend, "persona"), [{ id: "N3", persona: "老".repeat(400) }]);
  assert.match(html(env, "detail-editors"), /detail-editor-meta">已设置 · 400 字<\/span>/);
});

test("a persona draft above the limit is not saved by '采用并保存' (the server would refuse it as well); the draft stays", async () => {
  const { env, backend } = await live({ status: "idle" }, { refineDraft: "稿".repeat(501) });
  await openEditors(env);
  typePersona(env, "N3", "爱反问");
  panelClick(env, "refine", "persona");
  await flush(env);
  assert.match(html(env, "detail-editors"), /<div class="detail-draft">/);
  panelClick(env, "accept-draft", "persona");
  await flush(env);
  assert.match(lastToast(env), /性格设定最多 500 个字符（现在 501 个）/);
  assert.equal((backend.rosterPuts || []).length, 0);
  assert.match(html(env, "detail-editors"), /<div class="detail-draft">/, "still there to be read or thrown away");
  // a draft in the fixed format that fits is saved as it is
  backend.refineDraft = "【定位】冷静的逻辑派\n【说话风格】\n- 句子短，先结论后理由。\n【口头禅】我们看票。";
  panelClick(env, "refine", "persona");
  await flush(env);
  panelClick(env, "accept-draft", "persona");
  await flush(env);
  assert.deepEqual(puts(backend, "persona"), [{ id: "N3", persona: backend.refineDraft }]);
});

// =====================================================================================================
// memory: the structured editor (G42)
// =====================================================================================================
test("the memory editor has a row per OTHER seat in seat order (avatar, '号 名字', input, 'x / 80'), a lessons box and the whole-file counter", async () => {
  const { env } = await live({ status: "idle" });
  cardClick(env, "N3");
  panelClick(env, "toggle-editor", "memory");
  const editors = html(env, "detail-editors");
  const rows = [...editors.matchAll(/data-imp-row="(N\d+)"/g)].map((m) => m[1]);
  assert.deepEqual(rows, F.PLAYER_IDS.filter((id) => id !== "N3"), "eleven rows, never one for oneself, in seat order");
  assert.ok(!editors.includes('data-imp-id="N3"'));
  assert.equal((editors.match(/<textarea class="form-input mem-input"/g) || []).length, 11, "a box per seat - a <textarea>: 80 characters do not fit one line of a 428 px panel");
  assert.equal((editors.match(/placeholder="（暂无）"/g) || []).length, 11, "an empty impression says so");
  assert.ok(Array.from(rows).every((id) => impText(env, id) === ""), "all empty");
  // two lines per seat (who + counter, then the box): the counter is in the head line, before the box
  const row5 = editors.split('data-imp-row="N5"')[1].split("</li>")[0];
  assert.ok(row5.indexOf('id="count-imp-N5"') < row5.indexOf("<textarea"), "the head line (name, counter) comes first, the box below it");
  assert.match(row5, /<div class="mem-head">/);
  // ... and the style gives the box the whole width of the panel (a 181 px box beside the name showed ~13 of 80 characters)
  const css = require("node:fs").readFileSync(path.join(H.ROOT, "static", "css", "style.css"), "utf8");
  assert.match(css, /\.mem-row \{\s*display: flex;\s*flex-direction: column;/);
  assert.match(css, /\.mem-input \{[^}]*width: 100%;/);
  assert.equal((editors.match(/class="avatar-img mem-avatar"/g) || []).length, 11, "every row has the other player's avatar");
  assert.match(editors, /<span class="mem-name">1 号 Alice<\/span>/);
  assert.match(editors, /<span class="mem-name">4 号 David<\/span>/);
  assert.match(editors, /id="count-imp-N12">0 \/ 80</);
  assert.match(editors, /aria-label="对 Alice 的印象（最多 80 字）"/);
  assert.match(editors, /不写“他是狼 \/ 预言家”（身份每局重新分配）/);
  assert.match(editors, /最多 3 条，合计 300 字/);
  assert.match(editors, /id="count-detail-exp"[^>]*>0 \/ 3 条 · 0 \/ 300 字</);
  const empty = Roster.memoryChars(OTHERS_OF_N3, {}, []);
  assert.match(editors, new RegExp(`id="count-detail-total"[^>]*>整份 ${empty} / 1500 字<`));
  assert.match(editors, /id="btn-detail-save-memory"[^>]* disabled/);
  assert.match(editors, /detail-editor-meta">空<\/span>/);
  // the avatar row is drawn from the LIVE avatar of that player
  assert.ok(editors.includes(H.Avatar.imgHtml("N1", 1, F.avatarOf(1), { size: 20, className: "mem-avatar" })));
  // the moderator has neither a persona nor a memory
  modClick(env);
  assert.equal(html(env, "detail-editors"), "");
});

test("an impression is ONE line: Enter in its box starts no second line (and a pasted line break is folded the way the server folds it)", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N3");
  panelClick(env, "toggle-editor", "memory");
  let prevented = 0;
  const enter = (attrs) => env.el("detail-panel").dispatch("keydown", { key: "Enter", preventDefault: () => prevented++, target: { id: "", getAttribute: (k) => attrs[k] || null } });
  enter({ "data-imp-id": "N5" });
  assert.equal(prevented, 1, "Enter in an impression box is swallowed");
  enter({}); // some other box (the lessons: one per line): left alone
  enter({ "data-draft-key": "N3:persona" }); // the persona: free text, several lines
  assert.equal(prevented, 1);
  // a line break that got in by pasting counts as one space - exactly what the server stores - and is sent that way
  typeImp(env, "N5", "发言短，\n站边快");
  assert.equal(env.el("count-imp-N5").textContent, "8 / 80", "the line break is one character (a space), like the server's _one_line");
  panelClick(env, "save-text", "memory");
  await flush(env);
  assert.deepEqual(puts(backend, "memory_parts"), [{ id: "N3", memory_parts: { impressions: { N5: "发言短， 站边快" } } }]);
});

test("typing in an impression updates its counter, the whole-file counter and Save live, without a repaint", async () => {
  const { env } = await live({ status: "idle" });
  cardClick(env, "N3");
  panelClick(env, "toggle-editor", "memory");
  const painted = html(env, "detail-editors");
  typeImp(env, "N5", "发言短");
  assert.equal(env.el("count-imp-N5").textContent, "3 / 80");
  assert.equal(env.el("count-detail-total").textContent, `整份 ${Roster.memoryChars(OTHERS_OF_N3, { N5: "发言短" }, [])} / 1500 字`);
  assert.equal(env.el("btn-detail-save-memory").disabled, false);
  assert.equal(html(env, "detail-editors"), painted, "the input is the same node: the caret stays");
  // white space only is no impression: nothing changed
  typeImp(env, "N5", "   ");
  assert.equal(env.el("count-imp-N5").textContent, "0 / 80");
  assert.equal(env.el("btn-detail-save-memory").disabled, true);
  // the lessons: bullets and blank lines do not count, the counters show lessons and characters
  typeExp(env, "- 先听完再站边\n\n2. 女巫第一晚救人");
  assert.equal(env.el("count-detail-exp").textContent, "2 / 3 条 · 13 / 300 字");
  assert.equal(env.el("btn-detail-save-memory").disabled, false);
  assert.equal(env.el("count-detail-total").textContent, `整份 ${Roster.memoryChars(OTHERS_OF_N3, {}, ["先听完再站边", "女巫第一晚救人"])} / 1500 字`);
});

test("above a limit the counter turns red, the hint says what and how much, and Save is off (the server's wording)", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N3");
  panelClick(env, "toggle-editor", "memory");
  typeImp(env, "N5", "x".repeat(81));
  assert.match(env.el("count-imp-N5").className, /over/);
  assert.equal(env.el("count-imp-N5").textContent, "81 / 80");
  assert.equal(env.el("hint-detail-memory").hidden, false);
  assert.equal(env.el("hint-detail-memory").textContent, "N5 Eve 的印象最多 80 字（现在 81 字），请精简。");
  assert.equal(env.el("btn-detail-save-memory").disabled, true);
  panelClick(env, "save-text", "memory");
  await flush(env);
  assert.match(lastToast(env), /N5 Eve 的印象最多 80 字/);
  assert.equal((backend.rosterPuts || []).length, 0);
  typeImp(env, "N5", "x".repeat(80));
  assert.equal(env.el("hint-detail-memory").hidden, true);
  assert.equal(env.el("btn-detail-save-memory").disabled, false);
  // lessons: too many, then too long together
  typeExp(env, "a\nb\nc\nd");
  assert.equal(env.el("hint-detail-memory").textContent, "经验最多 3 条（现在 4 条），请精简。");
  assert.match(env.el("count-detail-exp").className, /over/);
  typeExp(env, `${"x".repeat(150)}\n${"y".repeat(151)}`);
  assert.equal(env.el("hint-detail-memory").textContent, "经验合计最多 300 字（现在 301 字），请精简。");
  typeExp(env, "x".repeat(150));
  assert.equal(env.el("hint-detail-memory").hidden, true);
});

test("the whole-file limit is the server's limits.memory_chars ('整份记忆…最多 N 字')", async () => {
  const { env, backend } = await live({ status: "idle" });
  backend.roster.limits.memory_chars = 200;
  await env.app().roster().load();
  cardClick(env, "N3");
  panelClick(env, "toggle-editor", "memory");
  const empty = Roster.memoryChars(OTHERS_OF_N3, {}, []);
  assert.ok(empty < 200 && empty > 150, `the empty file is ${empty} characters`);
  assert.match(html(env, "detail-editors"), new RegExp(`整份 ${empty} / 200 字`), "the limit comes from the server");
  typeImp(env, "N5", "x".repeat(30));
  const now = Roster.memoryChars(OTHERS_OF_N3, { N5: "x".repeat(30) }, []);
  assert.ok(now > 200);
  assert.equal(env.el("count-detail-total").textContent, `整份 ${now} / 200 字`);
  assert.match(env.el("count-detail-total").className, /over/);
  assert.equal(env.el("hint-detail-memory").textContent, `整份记忆（印象和经验两段，含序号与符号）最多 200 字（现在 ${now} 字），请精简。`);
  assert.equal(env.el("btn-detail-save-memory").disabled, true);
  // back under the limit: fine again
  typeImp(env, "N5", "x".repeat(10));
  assert.equal(env.el("hint-detail-memory").hidden, true);
  assert.equal(env.el("btn-detail-save-memory").disabled, false);
});

test("Save sends ONLY the parts that changed as memory_parts (never the whole text); the editor then shows what the server kept", async () => {
  const { env, backend } = await live({ status: "idle" });
  await openEditors(env);
  typeImp(env, "N2", "总是附和别人");
  typeImp(env, "N5", "  爱悍跳，\n票型和发言常对不上 ");
  typeExp(env, "- 先听完再站边\n- 女巫第一晚救人");
  panelClick(env, "save-text", "memory");
  await flush(env);
  assert.deepEqual(puts(backend, "memory_parts"), [
    { id: "N3", memory_parts: { impressions: { N2: "总是附和别人", N5: "爱悍跳， 票型和发言常对不上" }, experience: ["先听完再站边", "女巫第一晚救人"] } },
  ]);
  assert.equal(puts(backend, "memory").length, 0);
  assert.match(lastToast(env), /记忆已保存/);
  const kept = seatOf(backend, "N3").memory_parts;
  assert.equal(kept.impressions.N5, "爱悍跳， 票型和发言常对不上");
  // the editor is drawn from the server's answer: cleaned values, the counters of what is saved
  const editors = html(env, "detail-editors");
  assert.equal(impText(env, "N5"), "爱悍跳， 票型和发言常对不上");
  assert.match(editors, />先听完再站边\n女巫第一晚救人<\/textarea>/);
  assert.match(editors, /detail-editor-meta">印象 2\/11 · \d+ \/ 1500 字<\/span>/);
  assert.match(editors, /id="btn-detail-save-memory"[^>]* disabled/, "nothing left to save");
  // a second save changes one impression and leaves the lessons alone
  typeImp(env, "N2", "话少");
  panelClick(env, "save-text", "memory");
  await flush(env);
  assert.deepEqual(puts(backend, "memory_parts").pop(), { id: "N3", memory_parts: { impressions: { N2: "话少" } } });
  // clearing an impression sends "" for that seat (and nothing else)
  typeImp(env, "N5", "");
  panelClick(env, "save-text", "memory");
  await flush(env);
  assert.deepEqual(puts(backend, "memory_parts").pop(), { id: "N3", memory_parts: { impressions: { N5: "" } } });
  assert.equal(seatOf(backend, "N3").memory_parts.impressions.N5, "");
  // removing the lessons sends an empty list
  typeExp(env, "");
  panelClick(env, "save-text", "memory");
  await flush(env);
  assert.deepEqual(puts(backend, "memory_parts").pop(), { id: "N3", memory_parts: { experience: [] } });
});

test("a refusal by the server shows its detail and keeps what was typed; Save is usable again", async () => {
  const { env, backend } = await live({ status: "idle" });
  await openEditors(env);
  typeImp(env, "N5", "不会超限的印象");
  backend.failures["PUT /api/roster/N3"] = [400, "N5 Eve 的印象最多 80 字（现在 81 字），请精简。"];
  panelClick(env, "save-text", "memory");
  await flush(env);
  assert.match(lastToast(env), /N5 Eve 的印象最多 80 字（现在 81 字），请精简。/);
  assert.equal(impText(env, "N5"), "不会超限的印象", "the text is still in the box");
  assert.equal(seatOf(backend, "N3").memory_parts.impressions.N5, "");
  assert.doesNotMatch(html(env, "detail-editors"), /id="btn-detail-save-memory"[^>]* disabled/);
  delete backend.failures["PUT /api/roster/N3"];
  panelClick(env, "save-text", "memory");
  await flush(env);
  assert.equal(seatOf(backend, "N3").memory_parts.impressions.N5, "不会超限的印象");
});

test("unsaved impressions survive repaints of the editors and are kept per player", async () => {
  const { env } = await live({ status: "idle" });
  await openEditors(env, "N3");
  typeImp(env, "N5", "还没保存的印象");
  typeExp(env, "还没保存的经验");
  panelClick(env, "toggle-editor", "persona"); // repaints the editors
  assert.equal(impText(env, "N5"), "还没保存的印象");
  assert.match(html(env, "detail-editors"), />还没保存的经验<\/textarea>/);
  env.sse("agent.status", { id: "N3", state: "thinking" }); // live updates repaint the head and the state, not the typed text
  env.sse("agent.metrics", { id: "N3", calls: 3, prompt_tokens: 10, completion_tokens: 5 });
  assert.equal(impText(env, "N5"), "还没保存的印象");
  // another player has his own; coming back finds N3's text where it was left
  cardClick(env, "N3"); // closes
  cardClick(env, "N4");
  panelClick(env, "toggle-editor", "memory");
  assert.doesNotMatch(html(env, "detail-editors"), /还没保存的印象/);
  cardClick(env, "N4");
  cardClick(env, "N3");
  assert.equal(impText(env, "N5"), "还没保存的印象");
});

test("the record (战绩) is shown and cannot be edited here; the meta line counts what is remembered", async () => {
  const { env, backend } = await live({ status: "idle" });
  const seat = seatOf(backend, "N3");
  seat.memory_parts = { record: "场次 7 · 胜 4 · 胜率 57%", impressions: { ...seat.memory_parts.impressions, N1: "发言短", N2: "话少" }, experience: ["先听完再站边"], legacy: "" };
  await env.app().roster().load();
  cardClick(env, "N3");
  const editors = html(env, "detail-editors");
  assert.match(editors, /<div class="mem-record"[^>]*><span class="mem-record-label">战绩<\/span><span class="mem-record-text">场次 7 · 胜 4 · 胜率 57%<\/span><\/div>/);
  assert.match(editors, /detail-editor-meta">印象 2\/11 · \d+ \/ 1500 字<\/span>/);
  assert.equal((editors.match(/data-imp-id=/g) || []).length, 11, "a box per other seat and none for the record");
  assert.equal((editors.match(/<input/g) || []).length, 0, "(the editors hold no single-line input at all any more)");
  assert.equal(impText(env, "N1"), "发言短");
  assert.equal(impText(env, "N2"), "话少");
  // saving an impression leaves the record to the engine: it is never sent
  panelClick(env, "toggle-editor", "memory");
  typeImp(env, "N2", "话更少");
  panelClick(env, "save-text", "memory");
  await flush(env);
  assert.ok(!JSON.stringify(backend.rosterPuts).includes("record"));
  assert.equal(seat.memory_parts.record, "场次 7 · 胜 4 · 胜率 57%");
});

// =====================================================================================================
// the old memory (G41): the section, tidied by the model or deleted
// =====================================================================================================
const withLegacy = async (text = "## 对局记忆\n- 第 1 局：我是狼人，悍跳失败。\n\n## 经验与印象\n- Bob 总是附和别人。") => {
  const ctx = await live({ status: "idle" });
  seatOf(ctx.backend, "N3").memory_parts.legacy = text;
  seatOf(ctx.backend, "N3").memory = "(rendered)";
  await ctx.env.app().roster().load();
  return ctx;
};

test("an old memory (v0.13.0) shows as a section '旧记忆（待整理）' with its size, a way to tidy it up and a way to delete it", async () => {
  const { env } = await withLegacy();
  cardClick(env, "N3");
  let editors = html(env, "detail-editors");
  const memorySection = () => (html(env, "detail-editors").match(/<section class="detail-editor[^"]*" data-kind="memory">[^]*?<\/section>/) || [""])[0];
  assert.match(editors, /detail-editor-meta">印象 0\/11 · \d+ \/ 1500 字 · 有旧记忆待整理<\/span>/);
  assert.doesNotMatch(memorySection(), /detail-editor-meta">空/, "it is not empty: there is something to tidy up");
  panelClick(env, "toggle-editor", "memory");
  editors = html(env, "detail-editors");
  assert.match(editors, /<div class="mem-legacy" id="mem-legacy">/);
  assert.match(editors, /⚠ 旧记忆（待整理）<span class="form-hint">\d+ \/ 600 字<\/span>/);
  assert.match(editors, /<div class="mem-legacy-text">## 对局记忆\n- 第 1 局：我是狼人，悍跳失败。/);
  assert.match(editors, /下次复盘时，该玩家会把它整理进印象和经验，之后这一段会被删除/);
  assert.match(editors, /id="btn-detail-tidy-legacy" data-detail-action="tidy-legacy"[^>]*>✨ 用主持人模型整理<\/button>/);
  assert.match(editors, /id="btn-detail-drop-legacy" data-detail-action="drop-legacy"[^>]*>删除旧记忆<\/button>/);
  // other players have none of it
  cardClick(env, "N3");
  cardClick(env, "N4");
  panelClick(env, "toggle-editor", "memory");
  assert.doesNotMatch(html(env, "detail-editors"), /mem-legacy/);
});

test("'用主持人模型整理': the OLD text goes to refine as the notes, the draft is read first, adopting it saves {impressions, experience, legacy: ''}", async () => {
  const { env, backend } = await withLegacy("Bob 总是附和别人\n我常因为过早表态被针对");
  await openEditors(env);
  panelClick(env, "tidy-legacy");
  await flush(env);
  assert.deepEqual(backend.refineCalls, [{ id: "N3", kind: "memory", text: "Bob 总是附和别人\n我常因为过早表态被针对" }]);
  let editors = html(env, "detail-editors");
  assert.match(editors, /<div class="detail-draft">/);
  assert.match(editors, /采用后，上面的印象和经验会换成这份草稿（还没保存的修改也会被覆盖）。旧记忆段也会一并删除/);
  assert.equal((backend.rosterPuts || []).length, 0, "the draft is only a proposal");
  assert.equal(seatOf(backend, "N3").memory_parts.legacy, "Bob 总是附和别人\n我常因为过早表态被针对", "and the old text is still on the server");
  panelClick(env, "accept-draft", "memory");
  await flush(env);
  const sent = backend.rosterPuts;
  assert.equal(sent.length, 1);
  assert.deepEqual(Object.keys(sent[0].memory_parts).sort(), ["experience", "impressions", "legacy"], "never `record`");
  assert.equal(sent[0].memory_parts.legacy, "");
  assert.deepEqual(Object.keys(sent[0].memory_parts.impressions), OTHERS_OF_N3.map((o) => o.id), "the draft carries every other seat");
  assert.deepEqual(sent[0].memory_parts.experience, ["Bob 总是附和别人"]);
  editors = html(env, "detail-editors");
  assert.doesNotMatch(editors, /mem-legacy|detail-draft/, "the old section is gone");
  assert.match(editors, />Bob 总是附和别人<\/textarea>/, "the tidied lesson is in the lessons box");
  assert.equal(seatOf(backend, "N3").memory_parts.legacy, "");
});

test("a tidy-up draft can be thrown away: nothing is saved and the old text stays; '删除旧记忆' asks first, then removes only that section", async () => {
  const { env, backend } = await withLegacy("旧的文字");
  const seat = seatOf(backend, "N3");
  seat.memory_parts.impressions.N5 = "爱悍跳";
  seat.memory_parts.experience = ["先听完"];
  await env.app().roster().load();
  await openEditors(env);
  panelClick(env, "tidy-legacy");
  await flush(env);
  panelClick(env, "discard-draft", "memory");
  assert.doesNotMatch(html(env, "detail-editors"), /detail-draft/);
  assert.match(html(env, "detail-editors"), /mem-legacy/);
  assert.equal((backend.rosterPuts || []).length, 0);
  // delete: a confirmation first (cancel does nothing) ...
  panelClick(env, "drop-legacy");
  await flush(env, 10);
  assert.equal(env.el("dialog-modal").style.display, "flex");
  assert.match(textOf(html(env, "dialog-body")), /将删除 3 号 Carol 的「旧记忆（待整理）」。战绩、印象和经验不受影响。/);
  assert.match(env.el("btn-dialog-ok").className, /btn-danger-outline/);
  env.el("btn-dialog-cancel").dispatch("click");
  await flush(env, 10);
  assert.equal((backend.rosterPuts || []).length, 0);
  // ... then confirmed: only `legacy` is sent, the rest of the memory is untouched
  panelClick(env, "drop-legacy");
  await flush(env, 10);
  env.el("btn-dialog-ok").dispatch("click");
  await flush(env, 40);
  assert.deepEqual(backend.rosterPuts, [{ id: "N3", memory_parts: { legacy: "" } }]);
  assert.equal(seat.memory_parts.legacy, "");
  assert.equal(seat.memory_parts.impressions.N5, "爱悍跳");
  assert.deepEqual(seat.memory_parts.experience, ["先听完"]);
  assert.doesNotMatch(html(env, "detail-editors"), /mem-legacy/);
});

test("a server without memory_parts (older backend): the text is the old memory and can still be tidied up or deleted", async () => {
  const { env, backend } = await live({ status: "idle" });
  for (const s of backend.roster.seats) delete s.memory_parts;
  backend.roster.seats[2].memory = "## 对局记忆\n- 第 1 局：我是狼人。";
  delete backend.roster.limits;
  await env.app().roster().load();
  cardClick(env, "N3");
  panelClick(env, "toggle-editor", "memory");
  const editors = html(env, "detail-editors");
  assert.match(editors, /<div class="mem-legacy-text">## 对局记忆\n- 第 1 局：我是狼人。<\/div>/);
  assert.match(editors, /\/ 80</, "the contract's limits are used when the server sends none");
  assert.equal((editors.match(/data-imp-row=/g) || []).length, 11);
});

// =====================================================================================================
// refine the memory with notes (G42)
// =====================================================================================================
test("'用主持人模型完善' on the memory: the notes box goes to refine, the draft is shown first, adopting replaces the impressions and lessons", async () => {
  const { env, backend } = await live({ status: "idle" });
  const seat = seatOf(backend, "N3");
  seat.memory_parts.impressions.N1 = "旧印象";
  await env.app().roster().load();
  await openEditors(env);
  typeNotes(env, "N5 爱悍跳\n第一天别急着站边");
  panelClick(env, "refine", "memory");
  assert.match(html(env, "detail-editors"), /id="btn-detail-refine-memory"[^>]* disabled[^>]*>完善中…<\/button>/, "busy while the model works");
  panelClick(env, "refine", "memory"); // a second click while it works
  await flush(env);
  assert.equal(backend.refineCalls.length, 1, "only one request");
  assert.deepEqual(backend.refineCalls[0], { id: "N3", kind: "memory", text: "N5 爱悍跳\n第一天别急着站边" });
  const editors = html(env, "detail-editors");
  assert.match(editors, /<div class="detail-draft">/);
  assert.match(editors, /尚未保存，请先看一遍/);
  assert.match(editors, /## 印象\n- N1 Alice：旧印象/);
  assert.match(editors, /采用后，上面的印象和经验会换成这份草稿（还没保存的修改也会被覆盖）。<\/div>/);
  assert.doesNotMatch(editors, /旧记忆段也会一并删除/, "no old text to delete when it is the notes box");
  assert.equal((backend.rosterPuts || []).length, 0);
  assert.match(editors, /id="btn-detail-refine-memory"(?![^>]*disabled)/, "usable again");
  panelClick(env, "accept-draft", "memory");
  await flush(env);
  assert.equal(backend.rosterPuts.length, 1);
  const sent = backend.rosterPuts[0].memory_parts;
  assert.deepEqual(Object.keys(sent).sort(), ["experience", "impressions"], "no `legacy` here: an old text, if any, is not touched");
  assert.equal(sent.impressions.N1, "旧印象");
  assert.deepEqual(sent.experience, ["N5 爱悍跳"]);
  assert.doesNotMatch(html(env, "detail-editors"), /detail-draft/);
  // the notes are in the memory now: the box is empty (they must not be merged a second time)
  assert.match(html(env, "detail-editors"), /id="ta-detail-memory-notes"[^>]*><\/textarea>/);
});

test("a memory draft the model returned without its structure cannot be adopted; the server's error is shown; a draft can be discarded", async () => {
  const { env, backend } = await live({ status: "idle" }, { refineMemory: { draft: "## 印象\n- N1 Alice：只有文字" } });
  await openEditors(env);
  typeNotes(env, "随便写写");
  panelClick(env, "refine", "memory");
  await flush(env);
  assert.match(html(env, "detail-editors"), /只有文字/);
  panelClick(env, "accept-draft", "memory");
  await flush(env);
  assert.match(lastToast(env), /这份草稿没有带结构化的印象和经验，不能直接保存/);
  assert.equal((backend.rosterPuts || []).length, 0);
  panelClick(env, "discard-draft", "memory");
  assert.doesNotMatch(html(env, "detail-editors"), /detail-draft/);
  assert.match(html(env, "detail-editors"), />随便写写<\/textarea>/, "the notes are still there");
  // the server says no (a model that did not answer in the format): 400 with its words
  backend.refineMemory = null;
  backend.refineError = "模型没有按要求返回记忆（需要 JSON：impressions 和 experience），请稍后重试。";
  panelClick(env, "refine", "memory");
  await flush(env);
  assert.match(lastToast(env), /完善失败：模型没有按要求返回记忆（需要 JSON：impressions 和 experience），请稍后重试。/);
  assert.doesNotMatch(html(env, "detail-editors"), /detail-draft/);
});

// =====================================================================================================
// what a finished review changed (G40)
// =====================================================================================================
const finished = (over = {}) => ({ status: "finished", winner: "好人阵营", summary: "狼人全部出局", review: { available: true, done: [], running: false }, ...over });
const CHANGES = {
  impressions: [
    { id: "N5", before: "", after: "爱悍跳，票型和发言常对不上" },
    { id: "N2", before: "话少", after: "" },
    { id: "N1", before: "发言短", after: "<b>发言短</b> & 站边快" },
  ],
  experience: { before: [], after: ["先听完再站边", "女巫第一晚救人"] },
};

test("after a review the panel lists what changed: the old impression and the new one of every seat that changed, and the lessons", async () => {
  const { env } = await live(finished());
  cardClick(env, "N4");
  env.sse("review.progress", { id: "N4", state: "running", message: "正在复盘…" });
  env.sse("review.progress", { id: "N4", state: "done", message: "已更新印象与战绩", changes: CHANGES });
  await flush(env);
  const live_ = html(env, "detail-live");
  assert.match(live_, /✓ 已复盘/);
  assert.match(live_, /战绩已计入，对其他玩家的印象已按本局更新。/);
  assert.match(live_, /本次改动：3 条印象，经验已更新（旧 → 新）/);
  const items = live_.split('<li class="rc-item">').slice(1);
  assert.equal(items.length, 4, "three impressions and the lessons");
  // an impression that was empty before says so; one that was cleared says so
  assert.match(items[0], /<div class="rc-who">5 号 Eve<\/div>/);
  assert.match(items[0], /rc-before"><span class="rc-tag">旧<\/span><span>（暂无）<\/span>/);
  assert.match(items[0], /rc-after"><span class="rc-tag new">新<\/span><span>爱悍跳，票型和发言常对不上<\/span>/);
  assert.match(items[1], /2 号 Bob/);
  assert.match(items[1], /<span>话少<\/span>/);
  assert.match(items[1], /<span>（已清空）<\/span>/);
  assert.match(items[2], /1 号 Alice/);
  assert.match(items[2], /&lt;b&gt;发言短&lt;\/b&gt; &amp; 站边快/, "model text is escaped");
  assert.ok(!live_.includes("<b>发言短</b>"));
  assert.match(items[3], /我的经验/);
  assert.match(items[3], /rc-before"><span class="rc-tag">旧<\/span><span>（暂无）<\/span>/);
  assert.match(items[3], /<span>先听完再站边；女巫第一晚救人<\/span>/);
  // it is that player's: another player's panel shows none of it
  cardClick(env, "N4");
  cardClick(env, "N6");
  assert.doesNotMatch(html(env, "detail-live"), /review-changes/);
  cardClick(env, "N6");
  cardClick(env, "N4");
  assert.match(html(env, "detail-live"), /本次改动：3 条印象/);
});

test("the list of changes can be folded; it survives other updates of the session; no changes says so; an older server sends none", async () => {
  const { env } = await live(finished());
  cardClick(env, "N4");
  env.sse("review.progress", { id: "N4", state: "done", message: "ok", changes: CHANGES });
  assert.match(html(env, "detail-live"), /aria-expanded="true"/);
  panelClick(env, "toggle-changes");
  let live_ = html(env, "detail-live");
  assert.doesNotMatch(live_, /review-changes-list/, "folded");
  assert.match(live_, /本次改动：3 条印象，经验已更新（旧 → 新）/, "the summary stays");
  assert.match(live_, /aria-expanded="false"/);
  panelClick(env, "toggle-changes");
  assert.match(html(env, "detail-live"), /review-changes-list/);
  // other session updates (the review summary, a budget event ...) do not lose them
  env.sse("session.updated", { review: { available: true, done: ["N4"], running: false } });
  env.sse("agent.metrics", { id: "N4", calls: 9, prompt_tokens: 1, completion_tokens: 1 });
  assert.match(html(env, "detail-live"), /本次改动：3 条印象/);
  // a review that changed nothing
  env.sse("review.progress", { id: "N5", state: "done", message: "ok", changes: { impressions: [], experience: null } });
  cardClick(env, "N4");
  cardClick(env, "N5");
  assert.match(html(env, "detail-live"), /本次复盘没有改动印象和经验（战绩已计入）。/);
  assert.doesNotMatch(html(env, "detail-live"), /review-changes-toggle/);
  // impressions only / lessons only
  env.sse("review.progress", { id: "N6", state: "done", message: "ok", changes: { impressions: [], experience: { before: ["旧"], after: ["新"] } } });
  cardClick(env, "N5");
  cardClick(env, "N6");
  assert.match(html(env, "detail-live"), /本次改动：印象没有变化，经验已更新（旧 → 新）/);
  // an older server: "已复盘" without any list
  env.sse("review.progress", { id: "N7", state: "done", message: "已写入" });
  cardClick(env, "N6");
  cardClick(env, "N7");
  assert.match(html(env, "detail-live"), /✓ 已复盘/);
  assert.doesNotMatch(html(env, "detail-live"), /review-changes/);
});

test("when a review overwrites the memory what was typed over the old one is dropped and the editor shows the new impressions", async () => {
  const { env, backend } = await live(finished());
  await openEditors(env, "N4");
  typeImp(env, "N5", "我手写的、还没保存的印象");
  typeNotes(env, "还没用的要点");
  assert.match(html(env, "detail-editors"), /data-imp-id="N5"/);
  const seat = seatOf(backend, "N4");
  seat.memory_parts = { record: "场次 1 · 胜 1 · 胜率 100%", impressions: { ...seat.memory_parts.impressions, N5: "复盘后的新印象" }, experience: [], legacy: "" };
  env.sse("review.progress", { id: "N4", state: "done", message: "ok", changes: { impressions: [{ id: "N5", before: "", after: "复盘后的新印象" }], experience: null } });
  await flush(env);
  const editors = html(env, "detail-editors");
  assert.equal(impText(env, "N5"), "复盘后的新印象");
  assert.doesNotMatch(editors, /我手写的/);
  assert.match(editors, /id="ta-detail-memory-notes"[^>]*><\/textarea>/);
});

test("the review wording follows the new memory: 更新对其他玩家的印象 (no more 写入记忆 / 对局记忆); clearing says what goes", async () => {
  const { env, backend } = await live(finished());
  cardClick(env, "N4");
  assert.match(html(env, "detail-live"), /让该玩家用自己的模型复盘本局，更新对其他玩家的印象。/);
  assert.doesNotMatch(html(env, "detail-live"), /写入记忆|对局记忆/);
  env.el("btn-roster-menu").dispatch("click");
  env.el("roster-menu").dispatch("click", { target: target({ "[data-roster-action]": { "data-roster-action": "clear" } }) });
  await flush(env, 10);
  const body = textOf(html(env, "dialog-body"));
  assert.match(body, /将清空 全部 12 名玩家 的记忆文件（战绩、对其他玩家的印象、经验和待整理的旧记忆）/);
  assert.doesNotMatch(body, /对局记忆|经验与印象/);
  env.el("btn-dialog-cancel").dispatch("click");
  await flush(env, 10);
  assert.equal(backend.calls.filter((c) => /memory:clear/.test(c.url)).length, 0);
  // the hints of the editors speak the new language too
  const editorsHtml = (id) => {
    cardClick(env, "N4");
    cardClick(env, id);
    return html(env, "detail-editors");
  };
  const hints = editorsHtml("N3");
  assert.match(hints, /对其他玩家的印象（每人一条）和自己的经验。每局结束后，玩家在复盘时会按本局表现更新印象/);
  assert.doesNotMatch(hints, /常输|常赢|胜率高/);
});

test("'清除全部记忆' empties the editors too (typed text, notes and drafts of every seat are dropped)", async () => {
  const { env, backend } = await live({ status: "idle" });
  seatOf(backend, "N3").memory_parts.impressions.N1 = "旧印象";
  await env.app().roster().load();
  await openEditors(env);
  typeImp(env, "N5", "没保存的");
  typeNotes(env, "没用的要点");
  env.el("btn-roster-menu").dispatch("click");
  env.el("roster-menu").dispatch("click", { target: target({ "[data-roster-action]": { "data-roster-action": "clear" } }) });
  await flush(env, 10);
  env.el("btn-dialog-ok").dispatch("click");
  await flush(env, 40);
  const editors = html(env, "detail-editors");
  assert.doesNotMatch(editors, /旧印象|没保存的|没用的要点/);
  assert.match(editors, /detail-editor-meta">空<\/span>/);
  assert.equal(seatOf(backend, "N3").memory_parts.impressions.N1, "");
});

// =====================================================================================================
// requests that are on their way (v0.13.1): an answer that comes too late, an edit made while a save is out, answers that cross
//   (backend.hold parks an answer - computed when the request is made - until the test lets it go)
// =====================================================================================================
const refineGate = (backend) => backend.hold((key) => /\/refine$/.test(key));
const putGate = (backend, id = "N3") => backend.hold((key) => key === `PUT /api/roster/${id}`);
const firstReadGate = (backend) => {
  let n = 0;
  return backend.hold((key) => key === "GET /api/roster" && n++ === 0); // (only the first read of the roster is held)
};
const reviewDone = (env, id, changes = { impressions: [], experience: null }) => env.sse("review.progress", { id, state: "done", message: "ok", changes });
const editorsOf = (env) => html(env, "detail-editors");
const busy = (env) => /完善中…/.test(editorsOf(env));
const hasDraft = (env) => /<div class="detail-draft">/.test(editorsOf(env));
const saveOff = (env, kind) => new RegExp(`id="btn-detail-save-${kind}"[^>]* disabled`).test(editorsOf(env));
const cardName = (env, id) => ((env.el("participants-grid").innerHTML.split(/(?=<div class="pcard[ "])/).find((p) => p.includes(`data-actor="${id}"`)) || "").match(/<span class="pcard-name">([^<]*)<\/span>/) || [])[1];
const typeName = (env, text) => {
  env.el("input-detail-name").value = text;
  env.el("detail-panel").dispatch("input", { target: { id: "input-detail-name", value: text, getAttribute: () => null } });
};

test("a refine that is on its way when a REVIEW overwrites the memory brings no draft back - adopting it would have overwritten the reviewed memory", async () => {
  const { env, backend } = await live(finished());
  await openEditors(env, "N4");
  typeNotes(env, "旧的要点");
  const gate = refineGate(backend);
  panelClick(env, "refine", "memory");
  await flush(env);
  assert.equal(gate.parked.length, 1);
  assert.equal(busy(env), true);
  // the review finishes: the memory file is rewritten, what the refine was made from is gone
  const seat = seatOf(backend, "N4");
  seat.memory_parts = { record: "", impressions: { ...seat.memory_parts.impressions, N5: "复盘后的新印象" }, experience: ["复盘后的经验"], legacy: "" };
  reviewDone(env, "N4", { impressions: [{ id: "N5", before: "", after: "复盘后的新印象" }], experience: { before: [], after: ["复盘后的经验"] } });
  await flush(env);
  assert.equal(busy(env), false, "the busy mark goes with the review");
  gate.release(0);
  await flush(env);
  assert.equal(hasDraft(env), false, "the old answer brings no draft back");
  assert.equal(busy(env), false);
  assert.equal(impText(env, "N5"), "复盘后的新印象");
  panelClick(env, "accept-draft", "memory"); // (a click on a draft that is not there)
  await flush(env);
  assert.equal((backend.rosterPuts || []).length, 0, "nothing was saved over the reviewed memory");
  assert.equal(seatOf(backend, "N4").memory_parts.impressions.N5, "复盘后的新印象");
  // a review of ANOTHER player does not touch this refine
  const second = await live(finished());
  await openEditors(second.env, "N4");
  typeNotes(second.env, "要点");
  const gate2 = refineGate(second.backend);
  panelClick(second.env, "refine", "memory");
  await flush(second.env);
  reviewDone(second.env, "N7");
  await flush(second.env);
  assert.equal(busy(second.env), true, "(the review of N7 did not make the refine of N4 stale)");
  gate2.release(0);
  await flush(second.env);
  assert.equal(hasDraft(second.env), true);
});

test("the failure of an OLD refine neither toasts nor erases the 'busy' mark of the request that began after it", async () => {
  const { env, backend } = await live(finished());
  await openEditors(env, "N4");
  typeNotes(env, "第一轮要点");
  const gate = refineGate(backend);
  backend.refineError = "模型没有按要求返回记忆"; // the first request will fail (its answer is computed when it is made)
  panelClick(env, "refine", "memory");
  await flush(env);
  delete backend.refineError;
  reviewDone(env, "N4"); // ... and the review makes it stale
  await flush(env);
  assert.equal(busy(env), false);
  typeNotes(env, "第二轮要点");
  panelClick(env, "refine", "memory");
  await flush(env);
  assert.equal(gate.parked.length, 2);
  assert.equal(busy(env), true);
  const said = env.toasts.length;
  gate.release(0); // the OLD answer: a failure
  await flush(env);
  assert.equal(busy(env), true, "the second request is still working");
  assert.equal(env.toasts.length, said, "and nothing is said about the failure of the first");
  gate.release(0); // the second one: a draft
  await flush(env);
  assert.equal(hasDraft(env), true);
  assert.equal(busy(env), false);
  assert.equal(backend.refineCalls.length, 2);
  // (a failure that is NOT stale still says so)
  panelClick(env, "discard-draft", "memory");
  backend.refineError = "模型没有按要求返回记忆";
  panelClick(env, "refine", "memory");
  await flush(env);
  gate.release(0);
  await flush(env);
  assert.match(lastToast(env), /完善失败：模型没有按要求返回记忆/);
  assert.equal(busy(env), false);
});

test("a refine that is on its way when a NEW GAME starts, or the memory is cleared, brings no draft back", async () => {
  for (const how of ["new game", "clear"]) {
    const { env, backend } = await live({ status: "idle", session_id: "game-A" });
    await openEditors(env);
    typeNotes(env, "要点");
    const gate = refineGate(backend);
    panelClick(env, "refine", "memory");
    await flush(env);
    assert.equal(busy(env), true, how);
    if (how === "new game") {
      env.sse("snapshot", { session: F.makeSession({ session_id: "game-B", status: "idle", event_id: 900 }) }, 900);
    } else {
      env.el("btn-roster-menu").dispatch("click");
      env.el("roster-menu").dispatch("click", { target: target({ "[data-roster-action]": { "data-roster-action": "clear" } }) });
      await flush(env, 10);
      env.el("btn-dialog-ok").dispatch("click");
    }
    await flush(env, 40);
    assert.equal(busy(env), false, how);
    gate.release(0);
    await flush(env);
    assert.equal(hasDraft(env), false, `${how}: the answer of the game before is not a draft of this one`);
    assert.equal(busy(env), false, how);
  }
});

test("saving the text while a refine of it is on its way: the answer is stale - it was made from text that is not the text any more", async () => {
  const { env, backend } = await live({ status: "idle" });
  await openEditors(env);
  typePersona(env, "N3", "要点");
  const gate = refineGate(backend);
  panelClick(env, "refine", "persona");
  await flush(env);
  assert.equal(busy(env), true);
  typePersona(env, "N3", "我自己写好的性格");
  panelClick(env, "save-text", "persona");
  await flush(env);
  assert.equal(seatOf(backend, "N3").persona, "我自己写好的性格");
  assert.equal(busy(env), false, "the save made the refine moot");
  gate.release(0);
  await flush(env);
  assert.equal(hasDraft(env), false, "no draft comes back over the text that was saved");
  assert.match(editorsOf(env), />我自己写好的性格<\/textarea>/);
  assert.equal(seatOf(backend, "N3").persona, "我自己写好的性格");
});

test("a failed 'adopt and save' gives the draft back - unless a review made it stale while the save was on its way", async () => {
  const { env, backend } = await live(finished());
  await openEditors(env, "N4");
  typeNotes(env, "要点");
  panelClick(env, "refine", "memory");
  await flush(env);
  assert.equal(hasDraft(env), true);
  backend.failures["PUT /api/roster/N4"] = [400, "整份记忆最多 1500 字（现在 1600 字），请精简。"];
  panelClick(env, "accept-draft", "memory");
  await flush(env);
  assert.match(lastToast(env), /整份记忆最多 1500 字/);
  assert.equal(hasDraft(env), true, "the plain failure keeps the draft");
  // the same failure, with a review that finishes while the save is on its way
  const gate = putGate(backend, "N4");
  panelClick(env, "accept-draft", "memory");
  await flush(env);
  assert.equal(gate.parked.length, 1);
  reviewDone(env, "N4");
  await flush(env);
  gate.release(0);
  await flush(env);
  assert.equal(hasDraft(env), false, "the draft was made from the memory the review overwrote: it does not come back");
  assert.equal(busy(env), false);
});

test("adopting a memory draft empties the notes that went into it - notes typed while the save was on its way are new and stay", async () => {
  const { env, backend } = await live({ status: "idle" });
  await openEditors(env);
  typeNotes(env, "旧要点");
  panelClick(env, "refine", "memory");
  await flush(env);
  assert.equal(hasDraft(env), true);
  const gate = putGate(backend);
  panelClick(env, "accept-draft", "memory");
  await flush(env);
  assert.equal(gate.parked.length, 1);
  typeNotes(env, "新要点"); // typed while the save is out
  gate.release(0);
  await flush(env);
  assert.equal(hasDraft(env), false);
  assert.match(editorsOf(env), /id="ta-detail-memory-notes"[^>]*>新要点<\/textarea>/, "the notes of the draft are in the memory now; these are new");
  assert.deepEqual(seatOf(backend, "N3").memory_parts.experience, ["旧要点"]);
});

// ---- an edit made while a save is on its way -----------------------------------------------------------------------------
test("persona: Save, then type more before the answer comes - the later edit stays (unsaved) and the next Save sends it", async () => {
  const { env, backend } = await live({ status: "idle" });
  await openEditors(env);
  typePersona(env, "N3", "第一版");
  const gate = putGate(backend);
  panelClick(env, "save-text", "persona");
  await flush(env);
  assert.equal(gate.parked.length, 1);
  assert.match(editorsOf(env), /id="btn-detail-save-persona"[^>]* disabled[^>]*>保存中…<\/button>/);
  typePersona(env, "N3", "第一版，再加一句"); // typed while the request is out
  gate.release(0);
  await flush(env);
  assert.equal(seatOf(backend, "N3").persona, "第一版", "the server has what was sent");
  assert.match(editorsOf(env), />第一版，再加一句<\/textarea>/, "the later edit is still in the box");
  assert.equal(saveOff(env, "persona"), false, "and it is an unsaved difference: Save is on");
  assert.match(lastToast(env), /性格设定已保存/);
  gate.off();
  panelClick(env, "save-text", "persona");
  await flush(env);
  assert.deepEqual(puts(backend, "persona").map((p) => p.persona), ["第一版", "第一版，再加一句"]);
  assert.equal(seatOf(backend, "N3").persona, "第一版，再加一句");
  assert.equal(saveOff(env, "persona"), true, "nothing left to save");
  // no later edit: the draft goes, as it always did
  typePersona(env, "N3", "第三版");
  panelClick(env, "save-text", "persona");
  await flush(env);
  assert.match(editorsOf(env), />第三版<\/textarea>/);
  assert.equal(saveOff(env, "persona"), true);
});

test("memory: Save, then edit more before the answer comes - what was sent is saved, what was typed since stays and is the next Save's delta", async () => {
  const { env, backend } = await live({ status: "idle" });
  await openEditors(env);
  typeImp(env, "N2", "话少");
  typeImp(env, "N5", "爱悍跳");
  typeExp(env, "先听再站边");
  const gate = putGate(backend);
  panelClick(env, "save-text", "memory");
  await flush(env);
  assert.equal(gate.parked.length, 1);
  assert.deepEqual(gate.parked[0].key, "PUT /api/roster/N3");
  // typed while the request is out: the same impression further, another one, the lessons
  typeImp(env, "N5", "爱悍跳，常对不上票");
  typeImp(env, "N6", "发言稳");
  typeExp(env, "先听再站边\n女巫别急");
  gate.release(0);
  await flush(env);
  const kept = seatOf(backend, "N3").memory_parts;
  assert.equal(kept.impressions.N2, "话少");
  assert.equal(kept.impressions.N5, "爱悍跳", "the server has what was sent");
  assert.equal(kept.impressions.N6, "");
  assert.deepEqual(kept.experience, ["先听再站边"]);
  assert.equal(impText(env, "N2"), "话少", "(sent and not touched since: it is the saved text)");
  assert.equal(impText(env, "N5"), "爱悍跳，常对不上票", "the later edits are still in the boxes");
  assert.equal(impText(env, "N6"), "发言稳");
  assert.match(editorsOf(env), />先听再站边\n女巫别急<\/textarea>/);
  assert.equal(saveOff(env, "memory"), false);
  gate.off();
  panelClick(env, "save-text", "memory");
  await flush(env);
  assert.deepEqual(puts(backend, "memory_parts").pop(), {
    id: "N3",
    memory_parts: { impressions: { N5: "爱悍跳，常对不上票", N6: "发言稳" }, experience: ["先听再站边", "女巫别急"] },
  });
  assert.equal(saveOff(env, "memory"), true);
  assert.equal(seatOf(backend, "N3").memory_parts.impressions.N6, "发言稳");
});

test("the name: Save, then type another name before the answer comes - the later one stays in the box", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N3");
  typeName(env, "Caroline");
  const gate = putGate(backend);
  panelClick(env, "save-name");
  await flush(env);
  assert.equal(gate.parked.length, 1);
  typeName(env, "Caroline2");
  gate.release(0);
  await flush(env);
  assert.equal(cardName(env, "N3"), "Caroline");
  assert.match(html(env, "detail-head"), /id="input-detail-name" value="Caroline2"/, "the later edit is still in the box");
  gate.off();
  panelClick(env, "save-name");
  await flush(env);
  assert.equal(cardName(env, "N3"), "Caroline2");
});

test("an answer that comes while the user is typing in the editors repaints nothing - no caret is lost - and the counters and Save follow", async () => {
  const { env, backend } = await live({ status: "idle" });
  await openEditors(env);
  typeImp(env, "N2", "话少");
  const gate = putGate(backend);
  panelClick(env, "save-text", "memory");
  await flush(env);
  // the focus is in a box of the editors from now on (the test DOM has no focus: this is what the browser tells hasFocus())
  const box = env.el("ta-detail-memory-exp");
  env.document.activeElement = box;
  env.el("detail-editors").contains = (node) => node === box;
  typeImp(env, "N5", "还在打字");
  const drawn = editorsOf(env);
  gate.release(0);
  await flush(env);
  assert.equal(editorsOf(env), drawn, "not repainted: the box the user types in would lose its caret");
  assert.equal(env.el("btn-detail-save-memory").disabled, false, "Save follows the typed text without a repaint ...");
  assert.equal(env.el("btn-detail-save-memory").textContent, "保存", "... and is no longer 'saving'");
  assert.equal(seatOf(backend, "N3").memory_parts.impressions.N2, "话少");
  // the user leaves the box: the next repaint shows the saved text and what was typed since
  env.document.activeElement = null;
  env.el("detail-editors").contains = undefined;
  panelClick(env, "toggle-editor", "memory");
  panelClick(env, "toggle-editor", "memory");
  assert.equal(impText(env, "N2"), "话少");
  assert.equal(impText(env, "N5"), "还在打字");
});

test("the user opens another player's panel while a save is on its way: the answer repaints nothing of the other player's editors, and the saved player is right when opened again", async () => {
  const { env, backend } = await live({ status: "idle" });
  await openEditors(env, "N3");
  typeImp(env, "N5", "爱悍跳");
  const gate = putGate(backend);
  panelClick(env, "save-text", "memory");
  await flush(env);
  cardClick(env, "N3"); // (a second click on the same card closes the panel)
  cardClick(env, "N6"); // ... and the panel of another player opens
  const box = env.el("ta-detail-memory-exp");
  env.document.activeElement = box; // (the user is typing in the other player's editor)
  env.el("detail-editors").contains = (node) => node === box;
  typeImp(env, "N2", "N6 自己写的，还没保存的一句比较长的印象"); // N6's own unsaved typing: its counters and Save follow live
  const drawn = editorsOf(env);
  const counter = env.el("count-detail-total").textContent;
  assert.match(counter, /^整份 \d+ \/ 1500 字$/);
  assert.equal(env.el("btn-detail-save-memory").disabled, false, "N6's Save is on");
  gate.release(0);
  await flush(env);
  assert.equal(editorsOf(env), drawn, "N6's editors were not repainted by the answer of N3's save");
  assert.equal(env.el("count-detail-total").textContent, counter, "N3's numbers were not written into the counters of N6's panel");
  assert.equal(env.el("btn-detail-save-memory").disabled, false, "and N3's answer did not turn N6's Save off");
  env.document.activeElement = null;
  env.el("detail-editors").contains = undefined;
  cardClick(env, "N6"); // (closes it)
  cardClick(env, "N3");
  assert.equal(impText(env, "N5"), "爱悍跳", "N3 shows what it saved");
  assert.equal(seatOf(backend, "N3").memory_parts.impressions.N5, "爱悍跳");
});

// ---- answers that cross --------------------------------------------------------------------------------------------------
test("a read of the roster that was sent BEFORE a save and answered after it does not roll the saved seat back - the seats nobody wrote since are taken", async () => {
  const { env, backend } = await live({ status: "idle" });
  await openEditors(env);
  seatOf(backend, "N1").persona = "别处改的性格"; // somebody else changed N1: the older read carries it
  const gate = firstReadGate(backend);
  const older = env.app().roster().load();
  await flush(env);
  assert.equal(gate.parked.length, 1);
  typePersona(env, "N3", "新性格");
  panelClick(env, "save-text", "persona");
  await flush(env);
  assert.equal(seatOf(backend, "N3").persona, "新性格");
  assert.match(editorsOf(env), />新性格<\/textarea>/);
  gate.release(0);
  await older;
  await flush(env);
  assert.match(editorsOf(env), />新性格<\/textarea>/, "the older read does not roll N3 back to the persona it had");
  assert.equal(saveOff(env, "persona"), true, "(and the editor is not 'dirty' either)");
  cardClick(env, "N1");
  assert.match(editorsOf(env), />别处改的性格<\/textarea>/, "what the older read says of a seat that nobody wrote since is taken");
});

test("two saves of one player whose answers come in the wrong order: the view keeps what the LATER request wrote", async () => {
  const { env, backend } = await live({ status: "idle" });
  await openEditors(env);
  const gate = putGate(backend);
  typePersona(env, "N3", "A");
  panelClick(env, "save-text", "persona"); // request 1: the answer says {name: Carol, persona: A}
  await flush(env);
  typeName(env, "Caroline");
  panelClick(env, "save-name"); // request 2: the answer says {name: Caroline, persona: A}
  await flush(env);
  assert.equal(gate.parked.length, 2);
  gate.release(1); // the later one first ...
  await flush(env);
  assert.equal(cardName(env, "N3"), "Caroline");
  gate.release(0); // ... then the older one: it must not turn the name back
  await flush(env);
  assert.equal(cardName(env, "N3"), "Caroline", "the card");
  assert.match(html(env, "select-viewpoint"), /<option value="N3">3 · Caroline<\/option>/, "the viewpoint list");
  assert.match(editorsOf(env), />A<\/textarea>/);
  assert.equal(env.app().state.session.participants.find((p) => p.id === "N3").name, "Caroline", "the session");
});

test("the limits of an older read of the roster do not come back either", async () => {
  const { env, backend } = await live({ status: "idle" });
  await openEditors(env);
  assert.match(editorsOf(env), /id="count-detail-persona">0 \/ 500</);
  const gate = firstReadGate(backend);
  const older = env.app().roster().load(); // limits.persona_chars is 500 in this answer
  await flush(env);
  backend.roster.limits = { ...backend.roster.limits, persona_chars: 300 };
  await env.app().roster().load(); // ... and 300 in the newer one, which comes first
  assert.match(editorsOf(env), /id="count-detail-persona">0 \/ 300</);
  gate.release(0);
  await older;
  await flush(env);
  assert.match(editorsOf(env), /id="count-detail-persona">0 \/ 300</, "the older answer does not bring the old limit back");
});

H.runTests(tests);
