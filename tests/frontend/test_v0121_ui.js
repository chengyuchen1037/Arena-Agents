"use strict";
// v0.12.1 features of the UI against the fake DOM (see harness.js):
//   roster      names / avatars / persona / memory editors, "用主持人模型完善" (draft first, confirm to save),
//               regenerate (one / all), the 名册 menu (clear, load-memory switch; v0.13: export / import moved to the settings),
//               review (复盘全部 / 复盘此玩家 / progress / 已复盘)
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
// roster: names
// =====================================================================================================
test("before the game starts the name is editable (1-20 characters, unique); saving PUTs /api/roster/{id} and every view follows", async () => {
  const { env, backend } = await live({ status: "idle" });
  assert.ok(backend.calls.some((c) => c.method === "GET" && c.url.endsWith("/api/roster")), "the roster was loaded at start-up");
  cardClick(env, "N3");
  const head = html(env, "detail-head");
  assert.match(head, /<input type="text" class="form-input detail-name-input" id="input-detail-name" value="Carol"/);
  assert.match(head, /data-detail-action="save-name"/);
  // (v0.13.1, G29: the re-roll lives in the small panel behind the 🎲 next to the avatar code)
  assert.match(head, /data-detail-action="toggle-avatar-pop"/);
  assert.ok(!head.includes("regen-avatar"), "the panel is closed until the 🎲 is pressed");

  env.el("input-detail-name").value = "Caroline";
  panelClick(env, "save-name");
  await flush(env, 30);
  const put = backend.rosterPuts.find((p) => p.id === "N3");
  assert.deepEqual(put, { id: "N3", name: "Caroline" });
  assert.equal(cardName(env, "N3"), "Caroline");
  assert.match(html(env, "select-viewpoint"), /<option value="N3">3 · Caroline<\/option>/);
  assert.match(lastToast(env), /名字已保存：Caroline/);
  assert.equal(env.app().state.session.participants.find((p) => p.id === "N3").name, "Caroline");
  assert.equal(backend.roster.seats[2].name, "Caroline", "the server's roster has it (it is persisted there)");
});

test("name validation happens locally first: empty, too long, a duplicate; the server's refusal is shown too", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N3");
  const attempt = async (value) => {
    env.el("input-detail-name").value = value;
    panelClick(env, "save-name");
    await flush(env, 20);
    return textOf(html(env, "detail-head"));
  };
  assert.match(await attempt("   "), /名字不能为空/);
  assert.match(await attempt("x".repeat(21)), /名字最多 20 个字符/);
  assert.match(await attempt("Bob"), /名字「Bob」已被其他玩家使用/);
  // (v0.13.1) a colon is refused as well - ASCII or full-width - in the server's own words
  assert.match(await attempt("Carol:2"), /名字不能包含冒号（: 或 ：）。/);
  assert.match(await attempt("Carol：2"), /名字不能包含冒号（: 或 ：）。/);
  assert.equal((backend.rosterPuts || []).length, 0, "nothing was sent");
  assert.equal(cardName(env, "N3"), "Carol");
  // unchanged name: no request either
  assert.doesNotMatch(await attempt("Carol"), /已被/);
  assert.equal((backend.rosterPuts || []).length, 0);
  // the server may still refuse (409 / 422): its message is shown, the card keeps its name
  backend.failures["PUT /api/roster/N3"] = [422, "名字含有非法字符"];
  const after = await attempt("Caro l");
  assert.match(after, /名字含有非法字符/);
  assert.equal(cardName(env, "N3"), "Carol");
  assert.match(lastToast(env), /名字含有非法字符/);
});

test("an OLD name with a colon (stored before the server refused them) still loads and shows; Save on it changes nothing; only a NEW nickname with a colon is refused", async () => {
  const OLD = "Carol:2";
  const COLON = /名字不能包含冒号（: 或 ：）。/;
  const participants = F.makeSession().participants.map((p) => (p.id === "N3" ? { ...p, name: OLD } : p));
  const { env, backend } = await live({ status: "idle", participants });
  backend.roster.seats[2].name = OLD; // (the server still holds it and still sends it)
  await env.app().roster().load();
  const attempt = async (value) => {
    env.el("input-detail-name").value = value;
    panelClick(env, "save-name");
    await flush(env, 20);
    return textOf(html(env, "detail-head"));
  };
  // shown as it is: the card, the viewpoint list and the name box of the panel - nothing complains about a name that is only displayed
  assert.equal(cardName(env, "N3"), OLD);
  assert.match(html(env, "select-viewpoint"), /<option value="N3">3 · Carol:2<\/option>/);
  cardClick(env, "N3");
  assert.match(html(env, "detail-head"), /id="input-detail-name" value="Carol:2"/);
  assert.doesNotMatch(textOf(html(env, "detail-head")), /冒号/);
  // Save / Enter on the name as it is (with or without white space around it): nothing to save - no request, no complaint
  assert.doesNotMatch(await attempt("Carol:2"), /冒号/);
  assert.doesNotMatch(await attempt("  Carol:2  "), /冒号/);
  assert.equal((backend.rosterPuts || []).length, 0);
  assert.equal(cardName(env, "N3"), OLD);
  assert.match(html(env, "detail-head"), /id="input-detail-name" value="Carol:2"/, "the box shows the old name again");
  // a NEW nickname with a colon - the old one changed a little, a bare colon, a full-width one - is refused here, in the server's words
  for (const bad of ["Carol:3", "Carol：2", ":", "：", "Carol:2:"]) assert.match(await attempt(bad), COLON, JSON.stringify(bad));
  assert.equal((backend.rosterPuts || []).length, 0, "nothing was sent");
  assert.equal(cardName(env, "N3"), OLD, "and the old name stays");
  // the way out: a name without one is saved (the old one is replaced), and the box no longer shows the complaint
  assert.doesNotMatch(await attempt("Caroline"), COLON);
  assert.deepEqual(backend.rosterPuts, [{ id: "N3", name: "Caroline" }]);
  assert.equal(cardName(env, "N3"), "Caroline");
  // Enter in the name box takes the same road: an unchanged old name does nothing there either
  const second = await live({ status: "idle", participants });
  cardClick(second.env, "N3");
  second.env.el("input-detail-name").value = OLD;
  second.env.el("detail-panel").dispatch("keydown", { key: "Enter", preventDefault: () => {}, target: { id: "input-detail-name", getAttribute: () => null } });
  await flush(second.env, 20);
  assert.equal((second.backend.rosterPuts || []).length, 0);
  assert.doesNotMatch(textOf(html(second.env, "detail-head")), /冒号/);
});

test("once the game has started the name is read-only text and the regenerate button is gone", async () => {
  const { env } = await live({ status: "running" });
  cardClick(env, "N3");
  const head = html(env, "detail-head");
  assert.ok(!head.includes("input-detail-name"));
  assert.ok(!head.includes("regen-avatar"));
  assert.match(head, /<div class="detail-name" title="对局未开始时才能改名">Carol<\/div>/);
  // a game that goes back to idle (new game) makes it editable again
  env.sse("snapshot", { session: F.makeSession({ session_id: "next", status: "idle", event_id: 900 }) }, 900);
  assert.match(html(env, "detail-head"), /id="input-detail-name"/);
});

test("regenerate avatar (one player): POST .../avatar:regenerate, only that card changes", async () => {
  const { env, backend } = await live({ status: "idle" });
  const before = Object.fromEntries(F.PLAYER_IDS.map((id) => [id, cardImg(env, id)]));
  cardClick(env, "N5");
  panelClick(env, "toggle-avatar-pop");
  panelClick(env, "regen-avatar");
  await flush(env, 30);
  assert.equal(posts(backend, "POST", "/api/roster/N5/avatar:regenerate").length, 1);
  assert.notEqual(cardImg(env, "N5"), before.N5);
  assert.equal(cardImg(env, "N4"), before.N4);
  assert.equal(cardImg(env, "N6"), before.N6);
  assert.match(lastToast(env), /头像已重新生成/);
  // the detail panel shows the new picture as well
  assert.match(html(env, "detail-head"), /class="avatar-img"[^>]* src="data:image\/png/);
});

test("regenerate ALL avatars: the button in the participant bar is idle-only; one POST, every card changes", async () => {
  const idle = await live({ status: "idle" });
  const { env, backend } = idle;
  assert.equal(env.el("btn-regen-all-avatars").disabled, false);
  const before = F.PLAYER_IDS.map((id) => cardImg(env, id));
  env.el("btn-regen-all-avatars").dispatch("click");
  await flush(env, 30);
  assert.equal(posts(backend, "POST", "/api/roster/avatars:regenerate").length, 1);
  const after = F.PLAYER_IDS.map((id) => cardImg(env, id));
  assert.equal(after.filter((u, i) => u !== before[i]).length, 12);
  assert.match(lastToast(env), /已重新生成全部头像/);
  // a game that has started: disabled, with the reason as its tooltip
  env.sse("game.status", { status: "running", reason: "started", winner: null });
  assert.equal(env.el("btn-regen-all-avatars").disabled, true);
  assert.match(env.el("btn-regen-all-avatars").title, /对局已开始/);
});

// ---- the moderator's own random avatar -------------------------------------------------------------------------
const modClick = (env) => env.el("topbar-status").dispatch("click", { target: target({ "#mod-chip": { "data-actor": "moderator" } }) });
const chipImg = (env) => (html(env, "mod-chip").match(/ src="([^"]+)"/) || [])[1];
const withHost = (status, avatar = F.avatarOf(13)) => ({ status, moderator: { id: "moderator", name: "主持人", avatar, state: "sleeping", sleep_until: null } });

test("the moderator's panel shows its random avatar and, before the game, a button to roll a new one: only the moderator changes", async () => {
  const { env, backend } = await live(withHost("idle"));
  const players = Object.fromEntries(F.PLAYER_IDS.map((id) => [id, cardImg(env, id)]));
  const before = chipImg(env);
  assert.notEqual(before, H.Avatar.moderatorUrl(), "a random avatar, not the top hat");
  modClick(env);
  const head = html(env, "detail-head");
  assert.match(head, /data-detail-action="toggle-avatar-pop"/);
  assert.ok(head.includes(before), "the panel shows the same picture");
  assert.ok(!head.includes("input-detail-name") && !head.includes("save-name"), "the moderator's name is fixed");

  panelClick(env, "toggle-avatar-pop");
  assert.match(html(env, "detail-head"), /data-detail-action="regen-avatar"/);
  panelClick(env, "regen-avatar");
  await flush(env, 30);
  assert.equal(posts(backend, "POST", "/api/roster/moderator/avatar:regenerate").length, 1);
  assert.equal(posts(backend, "POST", "/api/roster/N1/avatar:regenerate").length, 0);
  const after = chipImg(env);
  assert.notEqual(after, before, "the chip shows the new avatar");
  assert.deepEqual(env.app().state.session.moderator.avatar, backend.roster.moderator.avatar);
  assert.ok(html(env, "detail-head").includes(after), "and so does the open panel");
  for (const id of F.PLAYER_IDS) assert.equal(cardImg(env, id), players[id], `${id} is untouched`);
  assert.match(lastToast(env), /主持人头像已重新生成/);
});

test("the moderator's regenerate button is gone once the game has started, and a refusal is shown", async () => {
  const running = await live(withHost("running"));
  modClick(running.env);
  assert.ok(!html(running.env, "detail-head").includes("regen-avatar"));
  assert.ok(!html(running.env, "detail-head").includes("toggle-avatar-pop"), "no 🎲 once the game has started");
  const { env, backend } = await live(withHost("idle"));
  backend.session.status = "paused"; // the server says the game moved on while the panel was open
  modClick(env);
  panelClick(env, "toggle-avatar-pop");
  panelClick(env, "regen-avatar");
  await flush(env, 30);
  assert.match(lastToast(env), /对局已开始/);
  assert.equal(chipImg(env), H.Avatar.urlFor("moderator", undefined, F.avatarOf(13)), "nothing changed");
});

test("'regenerate all' re-rolls the moderator together with the twelve players", async () => {
  const { env, backend } = await live(withHost("idle"));
  const before = chipImg(env);
  env.el("btn-regen-all-avatars").dispatch("click");
  await flush(env, 30);
  assert.equal(posts(backend, "POST", "/api/roster/avatars:regenerate").length, 1);
  assert.notEqual(chipImg(env), before);
  assert.deepEqual(env.app().state.session.moderator.avatar, backend.roster.moderator.avatar);
  assert.equal(env.app().state.session.participants.length, 12, "the moderator is not added to the players");
});

test("an older backend (no moderator avatar anywhere): the top hat stays and there is nothing to roll", async () => {
  const { env } = await live({ status: "idle" });
  assert.equal(chipImg(env), H.Avatar.moderatorUrl());
  modClick(env);
  assert.ok(!html(env, "detail-head").includes("regen-avatar"), "no avatar, no endpoint, no button");
  assert.ok(!html(env, "detail-head").includes("toggle-avatar-pop") && !html(env, "detail-head").includes("avatar-code"), "and no code to show either");
});

// =====================================================================================================
// roster: persona + memory (draft first, then confirm)
// =====================================================================================================
test("persona and memory are two expandable editors, empty by default; Save PUTs only the changed text", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N3");
  const editors = html(env, "detail-editors");
  assert.match(editors, /<section class="detail-editor" data-kind="persona">/);
  assert.match(editors, /<section class="detail-editor" data-kind="memory">/);
  assert.match(editors, /性格设定/);
  assert.match(editors, /记忆/);
  // (v0.13.1) the persona is one text box; the memory is structured: a box per OTHER seat, one for the lessons and one for notes
  assert.equal((editors.match(/<textarea/g) || []).length, 1 + 11 + 2);
  assert.equal((editors.match(/class="form-input mem-input"/g) || []).length, 11, "one impression per other seat");
  assert.match(editors, /detail-editor-meta">空<\/span>/, "empty by default");
  assert.match(editors, /id="btn-detail-save-persona"[^>]* disabled/, "nothing to save yet");
  // collapsed -> expanded
  panelClick(env, "toggle-editor", "persona");
  assert.match(html(env, "detail-editors"), /<section class="detail-editor open" data-kind="persona">/);
  assert.match(html(env, "detail-editors"), /aria-expanded="true"/);

  typeInto(env, "persona", "N3", "说话简短直接，喜欢用反问");
  assert.equal(env.el("btn-detail-save-persona").disabled, false, "the save button follows the text");
  panelClick(env, "save-text", "persona");
  await flush(env, 30);
  assert.deepEqual(backend.rosterPuts.find((p) => "persona" in p), { id: "N3", persona: "说话简短直接，喜欢用反问" });
  assert.match(lastToast(env), /性格设定已保存/);
  assert.match(html(env, "detail-editors"), /已设置 · 12 字/);
  // the saved text is still there after the panel is closed and opened again, and after a reload of the roster
  cardClick(env, "N3");
  cardClick(env, "N3");
  assert.match(html(env, "detail-editors"), />说话简短直接，喜欢用反问<\/textarea>/);
  await env.app().roster().load();
  assert.equal(env.app().roster().seat("N3").persona, "说话简短直接，喜欢用反问");

  // memory is independent; what is saved are the PARTS that changed (`memory_parts`), never the whole text
  panelClick(env, "toggle-editor", "memory");
  typeImpression(env, "N2", "总是附和别人");
  assert.equal(env.el("btn-detail-save-memory").disabled, false);
  panelClick(env, "save-text", "memory");
  await flush(env, 30);
  assert.deepEqual(backend.rosterPuts.filter((p) => "memory_parts" in p).pop(), { id: "N3", memory_parts: { impressions: { N2: "总是附和别人" } } });
  assert.ok(!backend.rosterPuts.some((p) => "memory" in p), "the whole memory text is never sent by the editor");
  assert.match(lastToast(env), /记忆已保存/);
});

test("unsaved text survives collapsing / expanding and live repaints of the panel", async () => {
  const { env } = await live({ status: "idle" });
  cardClick(env, "N2");
  panelClick(env, "toggle-editor", "persona");
  typeInto(env, "persona", "N2", "还没保存的想法");
  panelClick(env, "toggle-editor", "memory"); // repaints the editors
  assert.match(html(env, "detail-editors"), />还没保存的想法<\/textarea>/);
  env.sse("agent.status", { id: "N2", state: "thinking" }); // live updates repaint the head / state, not the editors
  env.sse("agent.metrics", { id: "N2", calls: 3, prompt_tokens: 10, completion_tokens: 5 });
  assert.match(html(env, "detail-editors"), />还没保存的想法<\/textarea>/);
  assert.equal(env.el("btn-detail-save-persona").disabled, false);
});

test("'用主持人模型完善': the DRAFT is shown first and nothing is saved until the user confirms", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N3");
  panelClick(env, "toggle-editor", "persona");
  typeInto(env, "persona", "N3", "爱反问");
  panelClick(env, "refine", "persona");
  await flush(env, 30);
  assert.deepEqual(backend.lastRefine, { id: "N3", kind: "persona", text: "爱反问" });
  let editors = html(env, "detail-editors");
  assert.match(editors, /<div class="detail-draft">/);
  assert.match(editors, /尚未保存，请先看一遍/);
  assert.ok(editors.includes("【草稿】这是主持人模型完善后的设定。"));
  assert.match(editors, /data-detail-action="accept-draft"/);
  assert.match(editors, /data-detail-action="discard-draft"/);
  assert.equal((backend.rosterPuts || []).length, 0, "no PUT: the draft is only a proposal");
  assert.equal(backend.roster.seats[2].persona, "", "and the server still has the old (empty) text");
  // discard: back to the user's own text, nothing saved
  panelClick(env, "discard-draft", "persona");
  editors = html(env, "detail-editors");
  assert.ok(!editors.includes("detail-draft"));
  assert.match(editors, />爱反问<\/textarea>/);
  assert.equal((backend.rosterPuts || []).length, 0);
  // refine again and accept: ONE PUT with the draft
  panelClick(env, "refine", "persona");
  await flush(env, 30);
  panelClick(env, "accept-draft", "persona");
  await flush(env, 30);
  assert.deepEqual(backend.rosterPuts, [{ id: "N3", persona: "【草稿】这是主持人模型完善后的设定。" }]);
  editors = html(env, "detail-editors");
  assert.ok(!editors.includes("detail-draft"));
  assert.match(editors, />【草稿】这是主持人模型完善后的设定。<\/textarea>/);
  assert.match(lastToast(env), /性格设定已保存/);
});

test("refine needs some text first, and shows the server's error; memory is refined the same way", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N3");
  panelClick(env, "refine", "persona"); // empty text
  await flush(env, 10);
  assert.match(lastToast(env), /请先在「性格设定」里写几个要点/);
  assert.equal(backend.lastRefine, undefined);
  panelClick(env, "toggle-editor", "memory");
  panelClick(env, "refine", "memory"); // (v0.13.1) the notes of the memory are their own box: empty -> the same hint
  await flush(env, 10);
  assert.match(lastToast(env), /请先在「记忆」里写几个要点/);
  assert.equal(backend.lastRefine, undefined);
  typeBox(env, "ta-detail-memory-notes", "我常输");
  backend.refineError = "主持人没有绑定模型";
  panelClick(env, "refine", "memory");
  await flush(env, 30);
  assert.match(lastToast(env), /完善失败：.*主持人没有绑定模型/);
  assert.ok(!html(env, "detail-editors").includes("detail-draft"));
  assert.equal(backend.lastRefine.kind, "memory");
  assert.equal(backend.lastRefine.text, "我常输");
  assert.match(html(env, "detail-editors"), /id="btn-detail-refine-memory"(?![^>]*disabled)/, "the button is usable again");
  assert.match(html(env, "detail-editors"), />我常输<\/textarea>/, "and the notes are still there");
});

test("an older backend without the roster API: the editors are disabled with an explanation, the rest of the UI works", async () => {
  const { env } = await live({ status: "idle" }, { failures: { "GET /api/roster": [404, "Not Found"] } });
  cardClick(env, "N3");
  const editors = html(env, "detail-editors");
  assert.match(editors, /名册接口不可用/);
  assert.match(editors, /<textarea[^>]* disabled/);
  assert.match(html(env, "detail-head"), /class="detail-name"/, "no editable name either");
  assert.equal(env.el("btn-regen-all-avatars").disabled, true);
  assert.equal(env.app().state.session.participants.length, 12);
});

// =====================================================================================================
// roster menu: clear / "load memory" (v0.13: the memory pack export / import is gone, the archive covers it)
// =====================================================================================================
test("名册 menu: opens and closes; it only has clear-all-memory and the load-memory switch (the pack export / import is gone)", async () => {
  const { env, backend } = await live({ status: "idle" });
  assert.equal(env.el("roster-menu").hidden, true);
  env.el("btn-roster-menu").dispatch("click");
  assert.equal(env.el("roster-menu").hidden, false);
  assert.equal(env.el("btn-roster-menu").getAttribute("aria-expanded"), "true");
  assert.ok(!H.HTML_SRC.includes("btn-roster-export") && !H.HTML_SRC.includes("btn-roster-import") && !H.HTML_SRC.includes("file-import-memory"));
  assert.ok(!H.SRC.roster.includes("memory:export") && !H.SRC.roster.includes("memory:import"));
  // an old menu entry does nothing (and calls nothing)
  menuClick(env, "export");
  await flush(env, 10);
  assert.equal(backend.calls.filter((c) => /memory:(export|import)/.test(c.url)).length, 0);
  assert.equal(env.downloads.length, 0);
  // a click elsewhere / Esc closes it
  env.document.dispatchEvent("click", { target: target({}) });
  assert.equal(env.el("roster-menu").hidden, true);
  env.el("btn-roster-menu").dispatch("click");
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.equal(env.el("roster-menu").hidden, true);
});

test("clear all memory: a second confirmation (danger), optionally persona too; nothing happens on cancel", async () => {
  const { env, backend } = await live({ status: "idle" });
  backend.roster.seats.forEach((s) => {
    s.memory = "旧记忆";
    s.persona = "旧性格";
  });
  menuClick(env, "clear");
  await flush(env, 10);
  assert.equal(env.el("dialog-modal").style.display, "flex");
  assert.match(textOf(html(env, "dialog-body")), /将清空\s*全部 12 名玩家\s*的记忆文件/);
  assert.match(html(env, "dialog-body"), /id="chk-dialog-include-persona"/);
  assert.match(env.el("btn-dialog-ok").className, /btn-danger-outline/);
  env.el("btn-dialog-cancel").dispatch("click");
  await flush(env, 10);
  assert.equal(posts(backend, "POST", "/api/roster/memory:clear").length, 0);
  assert.equal(backend.roster.seats[0].memory, "旧记忆");

  // confirm WITHOUT the persona checkbox
  menuClick(env, "clear");
  await flush(env, 10);
  env.el("chk-dialog-include-persona").checked = false;
  env.el("btn-dialog-ok").dispatch("click");
  await flush(env, 40);
  assert.deepEqual(backend.lastClear, { include_persona: false });
  assert.equal(backend.roster.seats[0].memory, "");
  assert.equal(backend.roster.seats[0].persona, "旧性格");
  assert.match(lastToast(env), /已清除全部记忆$/);
  // confirm WITH it
  menuClick(env, "clear");
  await flush(env, 10);
  env.el("chk-dialog-include-persona").checked = true;
  env.el("btn-dialog-ok").dispatch("click");
  await flush(env, 40);
  assert.deepEqual(backend.lastClear, { include_persona: true });
  assert.equal(backend.roster.seats[0].persona, "");
  assert.match(lastToast(env), /已清除全部记忆和性格设定/);
  // an open editor shows the cleared state
  cardClick(env, "N1");
  assert.match(html(env, "detail-editors"), /detail-editor-meta">空<\/span>/);
});

test("'本局加载记忆' switch: PUT /api/roster/memory-enabled; it follows the session; idle-only (and clear too)", async () => {
  const { env, backend } = await live({ status: "idle", memory_enabled: true });
  assert.equal(env.el("chk-memory-enabled").checked, true);
  env.el("chk-memory-enabled").checked = false;
  env.el("chk-memory-enabled").dispatch("change");
  await flush(env, 30);
  assert.deepEqual(posts(backend, "PUT", "/api/roster/memory-enabled")[0].body, { enabled: false });
  assert.match(lastToast(env), /本局不加载记忆/);
  // the server publishes it back (session.updated)
  env.sse("session.updated", { memory_enabled: false });
  assert.equal(env.el("chk-memory-enabled").checked, false);
  env.sse("session.updated", { memory_enabled: true });
  assert.equal(env.el("chk-memory-enabled").checked, true);
  // a failure puts the checkbox back
  backend.failures["PUT /api/roster/memory-enabled"] = [409, "对局已开始"];
  env.el("chk-memory-enabled").checked = false;
  env.el("chk-memory-enabled").dispatch("change");
  await flush(env, 30);
  assert.equal(env.el("chk-memory-enabled").checked, true);
  assert.match(lastToast(env), /对局已开始/);
  // while a game runs the switch and clear are locked
  env.sse("game.status", { status: "running", reason: "started", winner: null });
  assert.equal(env.el("chk-memory-enabled").disabled, true);
  assert.equal(env.el("btn-roster-clear").disabled, true);
});

test("a new game reloads the roster (names / avatars / memory may have changed in between)", async () => {
  const { env, backend } = await live({ status: "idle" });
  const n = () => backend.calls.filter((c) => c.method === "GET" && c.url.endsWith("/api/roster")).length;
  const before = n();
  env.sse("snapshot", { session: F.makeSession({ session_id: "another-game", status: "idle", event_id: 900 }) }, 900);
  await flush(env, 30);
  assert.equal(n(), before + 1);
  env.sse("phase.changed", { text: "x" }, 901);
  await flush(env, 10);
  assert.equal(n(), before + 1, "ordinary events do not");
});

// =====================================================================================================
// review (E21): manual, after the game
// =====================================================================================================
const finished = (over = {}) => ({ status: "finished", winner: "好人阵营", summary: "狼人全部出局", review: { available: true, done: [], running: false }, ...over });

test("复盘全部 only exists once the game is finished; it POSTs /api/game/review and shows progress, then 已复盘", async () => {
  const { env, backend } = await live({ status: "running" });
  assert.equal(env.el("btn-review-all").hidden, true);
  env.sse("game.status", { status: "finished", reason: "end_game", winner: "好人阵营", summary: "x" });
  env.sse("session.updated", { review: { available: true, done: [], running: false } });
  assert.equal(env.el("btn-review-all").hidden, false);
  assert.equal(env.el("btn-review-all").textContent, "复盘全部");
  assert.equal(env.el("btn-review-all").disabled, false);
  env.el("btn-review-all").dispatch("click");
  await flush(env, 30);
  assert.deepEqual(backend.lastReview, {}, "no ids: everybody");
  assert.match(lastToast(env), /已开始复盘全部玩家/);
  // progress arrives per player
  env.sse("review.progress", { id: "N1", state: "running", message: "正在复盘…" });
  assert.equal(env.el("btn-review-all").textContent, "复盘中 0/12…");
  assert.equal(env.el("btn-review-all").disabled, true);
  env.sse("review.progress", { id: "N1", state: "done", message: "已写入战绩与复盘" });
  env.sse("review.progress", { id: "N2", state: "running", message: "正在复盘…" });
  assert.equal(env.el("btn-review-all").textContent, "复盘中 1/12…");
  for (const id of F.PLAYER_IDS.slice(1)) env.sse("review.progress", { id, state: "done", message: "ok" });
  assert.equal(env.el("btn-review-all").textContent, "✓ 已复盘");
  assert.equal(env.el("btn-review-all").disabled, true);
});

test("a player's panel: 复盘此玩家 → 复盘中 → 已复盘 (and the memory editor can show what was written)", async () => {
  const { env, backend } = await live(finished());
  cardClick(env, "N4");
  assert.match(html(env, "detail-live"), /data-detail-action="review-one"[^>]*>复盘此玩家<\/button>/);
  assert.ok(!html(env, "detail-live").includes("已复盘"));
  panelClick(env, "review-one");
  await flush(env, 30);
  assert.deepEqual(backend.lastReview, { ids: ["N4"] });
  env.sse("review.progress", { id: "N4", state: "running", message: "正在复盘…" });
  assert.match(html(env, "detail-live"), /复盘中…/);
  assert.match(html(env, "detail-live"), /正在复盘…/);
  assert.ok(!html(env, "detail-live").includes("review-one"), "no second review of the same game");
  // the memory file changed on the server: finishing refreshes the roster, then "已复盘" with a way to look at it
  // (v0.13.1: a review REVISES the impressions of the other seats; the record is the engine's)
  const seat4 = backend.roster.seats[3];
  seat4.memory_parts = { record: "本局：预言家，胜", impressions: { ...seat4.memory_parts.impressions, N5: "爱悍跳" }, experience: ["先听完再站边"], legacy: "" };
  seat4.memory = "## 战绩\n本局：预言家，胜";
  const gets = () => backend.calls.filter((c) => c.method === "GET" && c.url.endsWith("/api/roster")).length;
  const before = gets();
  env.sse("review.progress", { id: "N4", state: "done", message: "已更新印象", changes: { impressions: [{ id: "N5", before: "", after: "爱悍跳" }], experience: null } });
  await flush(env, 30);
  assert.equal(gets(), before + 1);
  assert.match(html(env, "detail-live"), /✓ 已复盘/);
  panelClick(env, "show-memory");
  assert.match(html(env, "detail-editors"), /<section class="detail-editor open" data-kind="memory">/);
  assert.match(html(env, "detail-editors"), /<span class="mem-record-text">本局：预言家，胜<\/span>/, "the record is shown (read-only)");
  assert.match(html(env, "detail-editors"), /data-imp-id="N5"[^>]*>爱悍跳<\/textarea>/, "and the revised impression is in its box");
  assert.match(html(env, "detail-editors"), />先听完再站边<\/textarea>/, "and the lesson in its box");
  // other players can still be reviewed; a failed one shows why and can be retried
  cardClick(env, "N4");
  cardClick(env, "N5");
  env.sse("review.progress", { id: "N5", state: "error", message: "模型不可用" });
  assert.match(html(env, "detail-live"), /上次复盘失败：模型不可用/);
  assert.match(html(env, "detail-live"), /复盘此玩家/);
  assert.ok(env.toasts.some((t) => /复盘失败（Eve）：模型不可用/.test(t.textContent)));
});

test("review errors from the server are toasted (e.g. not finished / already reviewed)", async () => {
  const { env, backend } = await live(finished());
  backend.reviewError = "所选玩家本局都已复盘";
  env.el("btn-review-all").dispatch("click");
  await flush(env, 30);
  assert.match(lastToast(env), /复盘失败：.*所选玩家本局都已复盘/);
  assert.equal(env.el("btn-review-all").disabled, false, "usable again");
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
  assert.equal(env.el("active-status-text").textContent, "因预算上限暂停");
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
  // (v0.13: the whole viewpoint bar [视角▾][切换][接管] moves, the takeover bar stays in the first row)
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
  const inMenu = ["btn-open-settings-modal", "btn-theme", "viewpoint-bar", "btn-open-budget-modal", "btn-open-calllog", "btn-new", "btn-open-history-modal", "btn-open-bindings-modal", "btn-open-provider-modal"];
  for (const id of inMenu) assert.equal(env.el(id).parentNode, menu, `${id} is in the menu`);
  assert.equal(menu.children.length, inMenu.length);
  assert.equal(env.el("takeover-bar").parentNode, homes.left, "the takeover bar stays in the first row of the phone (F3)");
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
