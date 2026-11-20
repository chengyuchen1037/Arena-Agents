"use strict";
// v0.13.1 (G27-G31, contract §12.6): the avatar code in the detail panel and the 🎲 panel behind it
//   the code under the avatar + "复制" (the moderator has one too; it stays after the game started, the 🎲 does not),
//   the panel: random (the existing logic) or "按编号生成" (format and parts checked first, a twin is allowed with a hint),
//   PUT /api/bots/{id} {avatar} (no new write endpoint; v0.14: the moderator's is PUT /api/moderator), the server's refusal shown in the panel,
//   and (v0.14, H23) the question a bot that is initialized (frozen) asks first: a new avatar clears its initialization and memory.
// The pure encoding / decoding has its own file (test_avatar_code.js, which runs the vectors shared with Python).
// Run: node tests/frontend/test_avatar_pop_ui.js
const assert = require("node:assert/strict");
const H = require("./harness.js");
const M = require("./markup.js");

const { F, target, settle, bootLive, Avatar } = H;
const tests = [];
// Every test also checks the markup the page drew by the end of it (the fake DOM never parses markup: a stray </div>, a <div> in a <p> or an id that occurs
// twice would pass unseen and break the page in a real browser) - see markup.js.
const envs = [];
const test = (name, fn) =>
  tests.push([
    name,
    async () => {
      envs.length = 0;
      await fn();
      for (const env of envs) assert.deepEqual(M.envProblems(env, H.HTML_SRC), [], `the markup drawn in "${name}" is well formed`);
    },
  ]);

async function live(over = {}, overrides = {}, envOpts = {}) {
  const session = F.makeSession({ event_id: 500, ...over });
  // (v0.14: the bots start "fresh" - nobody initialized, nothing learned: they are not frozen, so a new avatar needs no question)
  const ctx = await bootLive({ session, botsPreset: "fresh", ...overrides }, envOpts);
  envs.push(ctx.env);
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
const panelClick = (env, action) => env.el("detail-panel").dispatch("click", { target: target({ "[data-detail-action]": { "data-detail-action": action } }) });
const cardImg = (env, id) => ((env.el("participants-grid").innerHTML.split(/(?=<div class="pcard[ "])/).find((p) => p.includes(`data-actor="${id}"`)) || "").match(/ src="([^"]+)"/) || [])[1];
const lastToast = (env) => (env.toasts.length ? env.toasts[env.toasts.length - 1].textContent : "");
const unesc = (s) => s.replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
// what the panel was DRAWN with (after a repaint) ...
const note = (env) => html(env, "detail-head").match(/<div class="avatar-pop-note([^"]*)" id="avatar-pop-note" role="status">([^<]*)<\/div>/);
const noteText = (env) => (note(env) ? unesc(note(env)[2]) : null);
const noteKind = (env) => (note(env) ? note(env)[1].trim() : null);
// ... and what typing changed in the live node without a repaint (the caret must stay where it is)
const liveText = (env) => env.el("avatar-pop-note").textContent;
const liveKind = (env) => (env.el("avatar-pop-note").className || "").replace("avatar-pop-note", "").trim();
const typeCode = (env, text) => {
  env.el("input-avatar-code").value = text;
  env.el("detail-panel").dispatch("input", { target: { id: "input-avatar-code", value: text, getAttribute: () => null } });
};
const codeOfSeat = (n) => Avatar.codeOf(F.avatarOf(n));
// a code the fixture library has parts for, written the way a person might type it
const CODE = "f01s02h02c01e01m02a01k01";
const CODE_DESC = { v: 1, face: "f01", skin: "s02", hair: "h02", hair_color: "c01", eyes: "e01", mouth: "m02", accessory: "a01", accessory_color: "k01" };

// ---- the code under the avatar (G28) -------------------------------------------------------------
test("the detail panel shows the avatar's code under the avatar with a 复制 button; clicking it copies the code", async () => {
  const { env } = await live({ status: "idle" });
  cardClick(env, "N3");
  const head = html(env, "detail-head");
  assert.ok(codeOfSeat(3).length >= 24, "a real code");
  assert.match(head, new RegExp(`<code class="avatar-code" id="avatar-code"[^>]*>${codeOfSeat(3)}</code>`));
  assert.match(head, /头像编号/);
  assert.match(head, /data-detail-action="copy-code"[^>]*>复制<\/button>/);
  assert.ok(head.indexOf("avatar-code") > head.indexOf("detail-avatar"), "it comes after the avatar");
  panelClick(env, "copy-code");
  await flush(env);
  assert.deepEqual(env.clipboard, [codeOfSeat(3)]);
  assert.match(lastToast(env), new RegExp(`头像编号已复制：${codeOfSeat(3)}`));
  // a different player has a different code
  cardClick(env, "N3");
  cardClick(env, "N4");
  assert.match(html(env, "detail-head"), new RegExp(codeOfSeat(4)));
  assert.notEqual(codeOfSeat(3), codeOfSeat(4));
});

test("the moderator has a code too (and the code row follows a changed avatar)", async () => {
  const { env } = await live({ status: "idle", moderator: { id: "moderator", name: "主持人", avatar: F.avatarOf(13), state: "sleeping", sleep_until: null } });
  modClick(env);
  assert.match(html(env, "detail-head"), new RegExp(`id="avatar-code"[^>]*>${codeOfSeat(13)}<`));
  panelClick(env, "copy-code");
  await flush(env);
  assert.deepEqual(env.clipboard, [codeOfSeat(13)]);
  // an older server that sends no moderator avatar: there is nothing to number
  const old = await live({ status: "idle" });
  modClick(old.env);
  assert.ok(!html(old.env, "detail-head").includes("avatar-code"));
});

test("copying falls back to a hidden text box where the clipboard API is missing, and says when nothing worked", async () => {
  const { env } = await live({ status: "idle" });
  cardClick(env, "N3");
  // no navigator.clipboard (an insecure page): the old execCommand way
  env.sandbox.navigator.clipboard = undefined;
  const boxes = [];
  const create = env.document.createElement;
  env.document.createElement = (tag) => {
    const el = create(tag);
    if (tag === "textarea") boxes.push(el);
    return el;
  };
  env.document.execCommand = (cmd) => cmd === "copy";
  panelClick(env, "copy-code");
  await flush(env);
  assert.equal(boxes.length, 1);
  assert.equal(boxes[0].value, codeOfSeat(3));
  assert.match(lastToast(env), /头像编号已复制/);
  // nothing works: the user is told to select it by hand (the code is `user-select: all` in the page)
  env.document.execCommand = () => false;
  panelClick(env, "copy-code");
  await flush(env);
  assert.match(lastToast(env), /复制失败：请手动选中编号再复制/);
  assert.match(H.CSS_SRC, /\.avatar-code \{[^}]*user-select: all/);
});

// ---- when the 🎲 exists (G31) --------------------------------------------------------------------
test("after the game started the code stays (and copies) but the 🎲 is gone; a new game brings it back", async () => {
  const { env } = await live({ status: "running" });
  cardClick(env, "N3");
  let head = html(env, "detail-head");
  assert.match(head, /id="avatar-code"/);
  assert.match(head, /data-detail-action="copy-code"/);
  assert.ok(!head.includes("toggle-avatar-pop") && !head.includes("avatar-pop") && !head.includes("apply-code"));
  // a new game (idle again): the open panel gets its 🎲 back
  env.sse("snapshot", { session: F.makeSession({ session_id: "next", status: "idle", event_id: 900 }) }, 900);
  head = html(env, "detail-head");
  assert.match(head, /data-detail-action="toggle-avatar-pop"/);
  panelClick(env, "toggle-avatar-pop");
  assert.match(html(env, "detail-head"), /id="avatar-pop"/);
  env.sse("game.status", { status: "running", reason: "started", winner: null });
  head = html(env, "detail-head");
  assert.ok(!head.includes("avatar-pop") && !head.includes("toggle-avatar-pop"), "started: no panel, no 🎲");
  assert.match(head, /id="avatar-code"/, "but the code is still there");
});

test("the 🎲 opens a small panel (random / by code) and a second press closes it; it is not shared between players", async () => {
  const { env } = await live({ status: "idle" });
  cardClick(env, "N3");
  assert.ok(!html(env, "detail-head").includes('id="avatar-pop"'));
  panelClick(env, "toggle-avatar-pop");
  let head = html(env, "detail-head");
  assert.match(head, /id="avatar-pop"/);
  assert.match(head, /data-detail-action="regen-avatar"[^>]*>🎲 随机生成<\/button>/);
  assert.match(head, /至少 3 项不同/, "the random pick keeps the existing rule");
  assert.match(head, /id="input-avatar-code"[^>]*placeholder="输入头像编号，如 f03s05h12c07e04m02a09k03"/);
  assert.match(head, /data-detail-action="apply-code"[^>]*>按编号生成<\/button>/);
  assert.match(head, /aria-expanded="true"/);
  panelClick(env, "toggle-avatar-pop");
  head = html(env, "detail-head");
  assert.ok(!head.includes('id="avatar-pop"'));
  assert.match(head, /aria-expanded="false"/);
  panelClick(env, "toggle-avatar-pop");
  cardClick(env, "N3");
  cardClick(env, "N4");
  assert.ok(!html(env, "detail-head").includes('id="avatar-pop"'), "another player's panel starts closed");
  cardClick(env, "N4");
  cardClick(env, "N3");
  assert.match(html(env, "detail-head"), /id="avatar-pop"/, "and N3's is as it was left");
});

// ---- 按编号生成 (G30) ----------------------------------------------------------------------------
test("按编号生成: PUT /api/bots/{id} {avatar} with the decoded description; the card and the code row follow; no other endpoint", async () => {
  const { env, backend } = await live({ status: "idle" });
  const before = cardImg(env, "N3");
  const others = Object.fromEntries(F.PLAYER_IDS.filter((id) => id !== "N3").map((id) => [id, cardImg(env, id)]));
  cardClick(env, "N3");
  panelClick(env, "toggle-avatar-pop");
  typeCode(env, " F01-s02 H02 : c01_E01  m02 a01 K01 ");
  assert.equal(liveText(env), "编号有效。", "a valid code says so while it is typed");
  assert.equal(liveKind(env), "ok");
  assert.ok(!html(env, "detail-head").includes("avatar-pop-note ok"), "(and typing did not repaint the head: the caret stays)");
  panelClick(env, "apply-code");
  await flush(env);
  assert.deepEqual(backend.botPuts, [{ id: F.botId(3), avatar: CODE_DESC }], "exactly one PUT, with exactly the description (and no confirm_reset: the bot is not frozen)");
  assert.equal(backend.calls.filter((c) => /avatar:regenerate/.test(c.url)).length, 0, "no random pick");
  assert.notEqual(cardImg(env, "N3"), before, "the card changed");
  for (const id of Object.keys(others)) assert.equal(cardImg(env, id), others[id], `${id} is untouched`);
  assert.deepEqual(env.app().state.session.participants.find((p) => p.id === "N3").avatar, CODE_DESC);
  assert.match(html(env, "detail-head"), new RegExp(`id="avatar-code"[^>]*>${CODE}<`), "the panel shows the new code");
  assert.match(lastToast(env), new RegExp(`头像已按编号生成：${CODE}`));
  assert.equal(noteText(env), "头像已按编号生成。");
  assert.match(html(env, "detail-head"), /id="input-avatar-code" value=""/, "the box is empty again for the next code");
  // the same code on the next player works too (a code is not "used up")
  cardClick(env, "N3");
  cardClick(env, "N6");
  panelClick(env, "toggle-avatar-pop");
  typeCode(env, CODE);
  panelClick(env, "apply-code");
  await flush(env);
  assert.deepEqual(backend.botPuts.pop(), { id: F.botId(6), avatar: CODE_DESC });
});

test("Enter in the code box applies it", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N2");
  panelClick(env, "toggle-avatar-pop");
  typeCode(env, CODE);
  env.el("detail-panel").dispatch("keydown", { key: "Enter", target: { id: "input-avatar-code" } });
  await flush(env);
  assert.deepEqual(backend.botPuts, [{ id: F.botId(2), avatar: CODE_DESC }]);
});

test("an invalid code says WHICH part is wrong and sends nothing", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N3");
  panelClick(env, "toggle-avatar-pop");
  const attempt = async (text) => {
    typeCode(env, text);
    panelClick(env, "apply-code");
    await flush(env, 10);
    return { text: noteText(env), kind: noteKind(env) };
  };
  let r = await attempt("   ");
  assert.match(r.text, /请输入头像编号/);
  assert.equal(r.kind, "error");
  // "blank" is decided by the decoder, by the rules the backend shares (tests/avatar_code_cases.json), not by JS's idea of white space:
  // a byte order mark or a NEL alone is blank; a lone U+1680, U+200B or U+001C is not (JS trim() would strip the first, Python's \s the last)
  for (const blank of ["\ufeff", "\u0085", " \t\r\n\u3000\u00a0 ", "\u2028"]) {
    r = await attempt(blank);
    assert.match(r.text, /请输入头像编号/, JSON.stringify(blank));
  }
  for (const stray of ["\u1680", "\u200b", "\u001c", "\u00ad"]) {
    r = await attempt(stray);
    assert.match(r.text, /头像编号格式不对：含有无法识别的字符/, JSON.stringify(stray));
  }
  // ... and they are ignored BETWEEN the segments exactly where the list says (the zero-width space is not)
  typeCode(env, "f01\ufeffs02 h02\u0085c01\u2028e01\u3000m02\u00a0a01\tk01"); // (typed only: this test sends nothing)
  assert.equal(liveKind(env), "ok", "an explicit list of what may sit between the segments");
  assert.equal(liveText(env), "编号有效。");
  r = await attempt("f01s02h02\u200bc01e01m02a01k01");
  assert.match(r.text, /含有无法识别的字符/, "U+200B is not white space for the shared rules");
  r = await attempt("f01s02h02c01e01m02a01"); // the accessory colour is missing
  assert.match(r.text, /缺少「配饰颜色」一段（以 k 开头，如 k03）/);
  r = await attempt("f01f02s02h02c01e01m02a01k01"); // the face twice
  assert.match(r.text, /「脸型」（以 f 开头，如 f03）出现了不止一次/);
  r = await attempt("f01s02h02c01e01m02a01k01!"); // a character that cannot be part of a code
  assert.match(r.text, /含有无法识别的字符 '!'/);
  r = await attempt("f0s02h02c01e01m02a01k01"); // a segment that is too short
  assert.match(r.text, /'f0' 不是部件 id/);
  r = await attempt("x01s02h02c01e01m02a01k01"); // a letter that is no field
  assert.match(r.text, /'x01' 不是部件 id/);
  r = await attempt("f99s02h02c01e01m02a01k01"); // a face the library does not have
  assert.match(r.text, /素材库里没有脸型 f99/);
  r = await attempt("f01s02h02c01e01m02a77k01"); // the first unknown part in field order
  assert.match(r.text, /素材库里没有配饰 a77/);
  assert.equal((backend.botPuts || []).length, 0, "nothing was sent, whatever was wrong");
  assert.equal(backend.calls.filter((c) => c.method === "PUT").length, 0);
  // what was typed stays in the box so that it can be corrected (the panel redraws it from its state)
  assert.match(html(env, "detail-head"), /id="input-avatar-code" value="f01s02h02c01e01m02a77k01"/);
  // a valid code afterwards clears the complaint
  typeCode(env, CODE);
  assert.equal(liveText(env), "编号有效。");
  assert.equal(liveKind(env), "ok");
  // ... and a half-typed one says nothing yet (it is not an error until the user presses the button)
  typeCode(env, "f01s02h0");
  assert.equal(liveText(env), "");
});

test("a code that looks exactly like somebody else's is allowed - the panel says so (G30); the same as one's own avatar is a no-op", async () => {
  const { env, backend } = await live({ status: "idle", moderator: { id: "moderator", name: "主持人", avatar: F.avatarOf(13), state: "sleeping", sleep_until: null } });
  cardClick(env, "N3");
  panelClick(env, "toggle-avatar-pop");
  typeCode(env, codeOfSeat(5));
  assert.equal(liveKind(env), "hint");
  assert.match(liveText(env), /和 5 号 Eve 的头像完全相同：可以生成，但看起来会是同款/);
  panelClick(env, "apply-code");
  await flush(env);
  assert.deepEqual(backend.botPuts, [{ id: F.botId(3), avatar: F.avatarOf(5) }], "it is NOT refused");
  assert.match(noteText(env), /头像已按编号生成。注意：和 5 号 Eve 的头像完全相同（同款）/);
  assert.match(lastToast(env), /和 5 号 Eve 同款/);
  // now it IS the current avatar: pressing again sends nothing
  typeCode(env, codeOfSeat(5));
  panelClick(env, "apply-code");
  await flush(env);
  assert.equal(backend.botPuts.length, 1);
  assert.match(noteText(env), /这已经是当前的头像/);
  // the moderator counts as somebody else, and so does N3 for the moderator
  modClick(env);
  panelClick(env, "toggle-avatar-pop");
  typeCode(env, codeOfSeat(5));
  assert.match(liveText(env), /和 3 号 Carol、5 号 Eve 的头像完全相同/, "every twin is named");
});

test("the moderator's panel: 按编号生成 PUTs /api/moderator {avatar}; the chip, the panel and the code follow", async () => {
  const { env, backend } = await live({ status: "idle", moderator: { id: "moderator", name: "主持人", avatar: F.avatarOf(13), state: "sleeping", sleep_until: null } });
  const chip = () => (html(env, "mod-chip").match(/ src="([^"]+)"/) || [])[1];
  const before = chip();
  modClick(env);
  panelClick(env, "toggle-avatar-pop");
  typeCode(env, CODE);
  panelClick(env, "apply-code");
  await flush(env);
  assert.deepEqual(backend.moderatorPuts, [{ avatar: CODE_DESC }]);
  assert.equal((backend.botPuts || []).length, 0, "the moderator is not a bot");
  assert.deepEqual(env.app().state.session.moderator.avatar, CODE_DESC);
  assert.notEqual(chip(), before, "the top bar chip shows the new avatar");
  assert.match(html(env, "detail-head"), new RegExp(`id="avatar-code"[^>]*>${CODE}<`));
  assert.equal(env.app().state.session.participants.length, 12, "the moderator is not added to the players");
});

test("the server's refusal is shown in the panel (409 once the game moved on, 400 for a part it does not know); the avatar is unchanged", async () => {
  const { env, backend } = await live({ status: "idle" });
  const before = cardImg(env, "N3");
  cardClick(env, "N3");
  panelClick(env, "toggle-avatar-pop");
  typeCode(env, CODE);
  backend.failures[`PUT /api/bots/${F.botId(3)}`] = [409, "名字和头像只能在对局开始之前修改；请先新建对局。", { code: "game_active" }];
  panelClick(env, "apply-code");
  await flush(env);
  assert.equal(noteKind(env), "error");
  assert.match(noteText(env), /名字和头像只能在对局开始之前修改/);
  assert.equal(cardImg(env, "N3"), before);
  backend.failures[`PUT /api/bots/${F.botId(3)}`] = [400, "头像描述的 hair（发型）含有未知的 id：'h02'"];
  panelClick(env, "apply-code");
  await flush(env);
  assert.match(noteText(env), /含有未知的 id：'h02'/, "the server's own words (it knows the library better than a stale page)");
  // the panel is usable again afterwards
  delete backend.failures[`PUT /api/bots/${F.botId(3)}`];
  panelClick(env, "apply-code");
  await flush(env);
  assert.equal(noteText(env), "头像已按编号生成。");
  assert.notEqual(cardImg(env, "N3"), before);
});

test("without the sprite library the format is still checked here; unknown parts are left to the server", async () => {
  const { env, backend } = await live({ status: "idle" }, { parts: null });
  assert.equal(Avatar.hasParts(), false);
  cardClick(env, "N3");
  panelClick(env, "toggle-avatar-pop");
  typeCode(env, "f01s02");
  panelClick(env, "apply-code");
  await flush(env, 10);
  assert.match(noteText(env), /缺少「发型」/);
  assert.equal((backend.botPuts || []).length, 0);
  typeCode(env, "f99s02h02c01e01m02a01k01");
  panelClick(env, "apply-code");
  await flush(env);
  assert.equal(backend.botPuts.length, 1, "this page cannot know f99 is missing: the server decides");
});

test("the random pick still works from the panel and keeps its own note; the old direct button is gone from the head", async () => {
  const { env, backend } = await live({ status: "idle" });
  const before = cardImg(env, "N5");
  cardClick(env, "N5");
  assert.ok(!/data-detail-action="regen-avatar"/.test(html(env, "detail-head")), "closed panel: no random button in the head");
  panelClick(env, "toggle-avatar-pop");
  panelClick(env, "regen-avatar");
  await flush(env);
  assert.equal(backend.calls.filter((c) => c.method === "POST" && new RegExp(`${F.botId(5)}/avatar:regenerate$`).test(c.url)).length, 1);
  assert.notEqual(cardImg(env, "N5"), before);
  assert.equal(noteText(env), "已随机生成新头像。");
  assert.match(html(env, "detail-head"), new RegExp(`id="avatar-code"[^>]*>${Avatar.codeOf(env.app().state.session.participants.find((p) => p.id === "N5").avatar)}<`));
});

// ---- v0.14 (H23): a bot that is initialized is frozen - a new avatar resets it, after a question ----------------------------------
const askedTexts = (env) => (env.el("dialog-body").innerHTML.match(/id="dialog-reset-text">([^<]*)</) || [])[1];
const dialogOk = (env) => env.el("btn-dialog-ok").dispatch("click");
const dialogCancel = (env) => env.el("btn-dialog-cancel").dispatch("click");

test("a frozen bot (initialized): 按编号生成 asks first - the fixed sentence - and only a yes sends confirm_reset; the bot gets a new id, the seat stays", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
  cardClick(env, "N3");
  assert.match(html(env, "detail-head"), /class="lock-badge"/, "🔒");
  panelClick(env, "toggle-avatar-pop");
  typeCode(env, CODE);
  panelClick(env, "apply-code");
  await flush(env);
  assert.equal(env.el("dialog-modal").style.display, "flex");
  assert.equal(askedTexts(env), "修改名字或头像会清空这个玩家的初始化结果和全部记忆（操作笔记、规则要点、打法、待解问题、印象、战绩），底层设定保留。修改后需要重新初始化才能开局。是否确认？");
  assert.equal((backend.botPuts || []).length, 0, "nothing is sent before the answer");
  dialogCancel(env);
  await flush(env);
  assert.equal((backend.botPuts || []).length, 0, "no: nothing was sent");
  assert.deepEqual(JSON.parse(JSON.stringify(env.app().state.session.participants.find((p) => p.id === "N3").avatar)), F.avatarOf(3));
  panelClick(env, "apply-code");
  await flush(env);
  dialogOk(env);
  await flush(env);
  assert.deepEqual(backend.botPuts, [{ id: F.botId(3), avatar: CODE_DESC, confirm_reset: true }]);
  const p3 = env.app().state.session.participants.find((p) => p.id === "N3");
  assert.deepEqual(p3.avatar, CODE_DESC);
  assert.notEqual(p3.bot_id, F.botId(3), "a reset bot has a new internal id: the seat shows the new one");
  assert.equal(p3.init_status, "uninitialized", "and it has to be initialized again");
  assert.match(html(env, "detail-head"), /data-init="uninitialized"/);
  assert.ok(!html(env, "detail-head").includes("lock-badge"), "no longer frozen");
});

test("the random pick of a frozen bot asks as well; a server that says needs_confirm (the bot got frozen behind our back) is answered with the same question", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
  cardClick(env, "N5");
  panelClick(env, "toggle-avatar-pop");
  panelClick(env, "regen-avatar");
  await flush(env);
  assert.equal(askedTexts(env).slice(0, 12), "修改名字或头像会清空这个");
  dialogCancel(env);
  await flush(env);
  assert.equal(backend.calls.filter((c) => /avatar:regenerate$/.test(c.url)).length, 0);
  // a bot the page thinks is free, which the server knows to be frozen: 409 needs_confirm carries the sentence and the page asks it
  const fresh = await live({ status: "idle" });
  const f = fresh.env;
  cardClick(f, "N2");
  panelClick(f, "toggle-avatar-pop");
  typeCode(f, CODE);
  fresh.backend.botsApi.initialized("N2"); // (the fake's N2 is initialized now; the page was not told)
  panelClick(f, "apply-code");
  await flush(f);
  assert.equal(askedTexts(f).slice(0, 12), "修改名字或头像会清空这个", "the server's own sentence");
  dialogOk(f);
  await flush(f);
  assert.deepEqual(fresh.backend.botPuts.map((x) => x.confirm_reset), [undefined, true], "the first try had no confirmation, the second has");
  assert.deepEqual(JSON.parse(JSON.stringify(env.app().state.session.participants.find((p) => p.id === "N5").avatar)), F.avatarOf(5), "(N5 of the first page: untouched)");
});

H.runTests(tests);
