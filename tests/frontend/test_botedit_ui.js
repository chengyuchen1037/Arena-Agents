"use strict";
// v0.14 (task-25): the editors in the lower half of the player panel (static/js/botedit.js) against the fake DOM and the fake backend:
//   底层设定   three required sections + up to three extras, counters, "✨ 用主持人模型完善" (draft first, confirm to save), the warning of a bot
//              that is initialized, .md out / in, clear
//   记忆       the five parts + the record: edit, save, revert, clear a part, clear all
//   待确认     the proposals of a review: old -> new, 采纳 / 拒绝
//   导入 / 导出  .arenabot out / in, the default library
// It replaces test_memory_ui.js (the persona / memory editors of v0.13.1, whose limits / counters / races it kept; the limits and the words are now
// held to the backend by test_bots_drift.py).
// Run: node tests/frontend/test_botedit_ui.js
const assert = require("node:assert/strict");
const H = require("./harness.js");
const M = require("./markup.js");

const { F, target, settle, bootLive, posts } = H;
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
  const ctx = await bootLive({ session, botsPreset: "fresh", ...overrides }, envOpts);
  envs.push(ctx.env);
  ctx.env.sse("snapshot", { session }, 500);
  await flushAll(ctx.env, 20);
  return ctx;
}

const flushAll = async (env, ms = 30) => {
  await settle(ms);
  env.flushRaf();
  await settle(ms);
  env.flushRaf();
};
const cardClick = (env, id) => env.el("participants-grid").dispatch("click", { target: target({ ".pcard": { "data-actor": id } }) });
const panelClick = (env, action, attrs = {}) => env.el("detail-panel").dispatch("click", { target: target({ "[data-detail-action]": { "data-detail-action": action, ...attrs } }) });
const html = (env, id) => env.el(id).innerHTML;
const textOf = (s) => s.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
const lastToast = (env) => (env.toasts.length ? env.toasts[env.toasts.length - 1].textContent : "");
const dialogOpen = (env) => env.el("dialog-modal").style.display === "flex";
const dialogText = (env) => textOf(html(env, "dialog-body"));
const dialogOk = (env) => env.el("btn-dialog-ok").dispatch("click");
const dialogCancel = (env) => env.el("btn-dialog-cancel").dispatch("click");
const unesc = (s) => s.replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
/** What a text box of the panel was DRAWN with (the text between its tags). */
const boxText = (env, id) => {
  const m = html(env, "detail-editors").match(new RegExp(`<textarea[^>]*id="${id}"[^>]*>([^<]*)</textarea>`));
  return m ? unesc(m[1]) : null;
};
const typeInto = (env, id, attrs, text) => {
  env.el(id).value = text;
  env.el("detail-panel").dispatch("input", { target: { id, value: text, getAttribute: (k) => (k in attrs ? attrs[k] : null) } });
};
const typeCore = (env, field, text) => typeInto(env, `ta-core-${field}`, { "data-core-field": field }, text);
const typeExtraTitle = (env, i, text) => typeInto(env, `input-core-extra-title-${i}`, { "data-core-extra-title": String(i) }, text);
const typeExtraText = (env, i, text) => typeInto(env, `ta-core-extra-${i}`, { "data-core-extra-text": String(i) }, text);
const typePart = (env, part, text) => typeInto(env, `ta-part-${part}`, { "data-part-text": part }, text);
const openEditor = async (env, kind) => {
  panelClick(env, "toggle-editor", { "data-kind": kind });
  await flushAll(env, 10);
};
const detailOf = (backend, seat) => backend.botsApi.detail(seat);
/** What a dialog says, with the spaces that come from the tags (<strong>) taken out in front of Chinese punctuation. */
const said = (env) => dialogText(env).replace(/\s+(?=[，。：；（）、])/g, "");
const file = (name, text, extra = {}) => ({ name, size: Buffer.byteLength(text || "x"), text: async () => text, ...extra });
const pick = (env, inputId, f) => env.el(inputId).dispatch("change", { target: { files: [f], value: "C:\\fakepath\\" + f.name } });
const CORE_MD = (identity, voice, mind, extra = "") => `---\nformat: arena-core\nversion: 1\n---\n# 身份\n${identity}\n\n# 说话方式\n${voice}\n\n# 分析方式\n${mind}\n${extra}`;

// =====================================================================================================
// 底层设定
// =====================================================================================================
test("the sections of the panel: 底层设定 / 记忆 / 待确认 / 导入 / 导出, closed at first, each with a line of what is in it", async () => {
  const { env } = await live({ status: "idle" }, { botsPreset: "ready" });
  cardClick(env, "N1");
  await flushAll(env);
  const ed = html(env, "detail-editors");
  for (const kind of ["core", "memory", "staging", "transfer"]) assert.match(ed, new RegExp(`<section class="detail-editor" data-kind="${kind}">`));
  const text = textOf(ed);
  assert.match(text, /▶ 底层设定 完整 · \d+ 字 · 附加 1 份/);
  assert.match(text, /▶ 记忆 打法 3 · 印象 2/);
  assert.match(text, /▶ 待确认 无/);
  assert.match(text, /▶ 导入 \/ 导出/);
  assert.ok(!ed.includes("<textarea"), "closed: no boxes");
  await openEditor(env, "core");
  assert.match(html(env, "detail-editors"), /<section class="detail-editor open" data-kind="core">/);
  assert.match(html(env, "detail-editors"), /aria-expanded="true"/);
});

test("the L0 editor: a box per required section with its counter (x / limit), the extras, nothing to save until something changes", async () => {
  const { env } = await live({ status: "idle" }, { botsPreset: "ready" });
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "core");
  assert.equal(boxText(env, "ta-core-identity"), "打了二十多年线下局的老玩家，话不多，一开口就是结论。");
  assert.match(html(env, "detail-editors"), /id="core-count-identity">26 \/ 300</);
  assert.match(html(env, "detail-editors"), /id="core-count-voice">47 \/ 600</);
  assert.match(html(env, "detail-editors"), /id="core-count-mind">25 \/ 400</);
  assert.match(html(env, "detail-editors"), /id="input-core-extra-title-0"[^>]*value="口头禅"/);
  assert.equal(boxText(env, "ta-core-extra-0"), "「行，听你的，出了再说。」");
  assert.match(html(env, "detail-editors"), /id="core-extra-count-0">13 \/ 300</);
  assert.match(textOf(html(env, "detail-editors")), /1 \/ 3/);
  assert.match(html(env, "detail-editors"), /id="btn-core-save"[^>]* disabled/);
  assert.match(html(env, "detail-editors"), /id="btn-core-revert"[^>]* disabled/);
  assert.match(textOf(html(env, "detail-editors")), /上限：身份 300 字、说话方式 600 字、分析方式 400 字，每份附加 300 字/);
  assert.match(textOf(html(env, "detail-editors")), /已经初始化：保存修改后它会变为「需重新初始化」，学到的记忆保留。/);
});

test("typing updates the counters, the list of faults, Save and Revert WITHOUT repainting (the caret must stay)", async () => {
  const { env } = await live({ status: "idle" });
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "core");
  const before = html(env, "detail-editors");
  typeCore(env, "voice", "说话".repeat(301)); // 602 characters: over 600
  assert.equal(html(env, "detail-editors"), before, "the editors were not drawn again");
  assert.equal(env.el("core-count-voice").textContent, "602 / 600");
  assert.match(env.el("core-count-voice").className, /over/);
  assert.equal(env.el("core-problems").hidden, false);
  assert.match(env.el("core-problems").innerHTML, /「说话方式」最多 600 字（现在 602 字）。/);
  assert.equal(env.el("btn-core-save").disabled, true, "over the limit: no Save");
  assert.equal(env.el("btn-core-revert").disabled, false);
  typeCore(env, "voice", "短一点");
  assert.equal(env.el("core-count-voice").textContent, "3 / 600");
  assert.doesNotMatch(env.el("core-count-voice").className, /over/);
  assert.equal(env.el("core-problems").hidden, true);
  assert.equal(env.el("btn-core-save").disabled, false);
  // a section emptied: the list says what is missing, and there is no Save
  typeCore(env, "mind", "   ");
  assert.match(env.el("core-problems").innerHTML, /还缺：分析方式（三个都要写才能保存）。/);
  assert.equal(env.el("btn-core-save").disabled, true);
  // back to what is saved: nothing to save, nothing to revert
  typeCore(env, "voice", F.makeCore(1).voice);
  typeCore(env, "mind", F.makeCore(1).mind);
  assert.equal(env.el("btn-core-save").disabled, true);
  assert.equal(env.el("btn-core-revert").disabled, true);
});

test("Save sends the canonical merged file (PUT /api/bots/{id}/core {markdown}); the editor shows what the server kept; the card's data follows", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "core");
  typeCore(env, "voice", "  新的说话方式  \n第二行  ");
  panelClick(env, "core-save");
  await flushAll(env);
  assert.equal(backend.corePuts.length, 1);
  const put = backend.corePuts[0];
  assert.equal(put.id, F.botId(1));
  assert.equal(put.markdown, `---\nformat: arena-core\nversion: 1\nname: Alice\n---\n# 身份\n${F.makeCore(1).identity}\n\n# 说话方式\n新的说话方式  \n第二行\n\n# 分析方式\n${F.makeCore(1).mind}\n\n# 附加：口头禅\n「行，听你的，出了再说。」\n`);
  assert.match(lastToast(env), /底层设定已保存：Alice/);
  assert.equal(detailOf(backend, "N1").core.sections.voice, "新的说话方式  \n第二行", "(the text is stripped at its ends, the way the server does; a space inside stays)");
  assert.equal(boxText(env, "ta-core-voice"), "新的说话方式  \n第二行");
  assert.match(html(env, "detail-editors"), /id="btn-core-save"[^>]* disabled/, "saved: nothing to save");
  assert.equal(env.app().state.session.bots.bots[0].core.chars.voice, 12, "the summary was read again");
});

test("a bot that is initialized: Save asks first - \"将变为需重新初始化，记忆保留\" - and it becomes ⚠ afterwards (memory kept)", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
  cardClick(env, "N2");
  await flushAll(env);
  await openEditor(env, "core");
  typeCore(env, "mind", "改过的分析方式");
  panelClick(env, "core-save");
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.equal(env.el("dialog-title").textContent, "保存底层设定");
  assert.match(dialogText(env), /将变为需重新初始化，记忆保留。/);
  dialogCancel(env);
  await flushAll(env);
  assert.equal((backend.corePuts || []).length, 0, "no: nothing was sent");
  await openEditor(env, "memory"); // (a repaint: the boxes are drawn from what was typed)
  await openEditor(env, "memory");
  assert.equal(boxText(env, "ta-core-mind"), "改过的分析方式", "and what was typed is still there");
  panelClick(env, "core-save");
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.equal(backend.corePuts.length, 1);
  assert.equal(env.app().state.session.participants.find((p) => p.id === "N2").init_status, "stale", "⚠ now");
  assert.match(textOf(html(env, "detail-head")), /⚠ 需重新初始化（底层设定变了）/);
  assert.ok(detailOf(backend, "N2").memory.arch_notes, "the memory is still there");
});

test("the server's faults of a text are listed under the editor; what was typed stays and Save is usable again", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "core");
  typeCore(env, "identity", "换一个身份");
  backend.failures[`PUT /api/bots/${F.botId(1)}/core`] = [400, "底层设定没有保存：共 2 处问题：「说话方式」最多 600 字（现在 612 字）。 缺少「分析方式」分区。", { problems: ["「说话方式」最多 600 字（现在 612 字）。", "缺少「分析方式」分区。"] }];
  panelClick(env, "core-save");
  await flushAll(env);
  assert.match(html(env, "detail-editors"), /id="core-server-problems"[^>]*>(?:<li>「说话方式」最多 600 字（现在 612 字）。<\/li><li>缺少「分析方式」分区。<\/li>)<\/ul>/);
  assert.match(lastToast(env), /底层设定没有保存/);
  assert.equal(boxText(env, "ta-core-identity"), "换一个身份");
  assert.match(html(env, "detail-editors"), /id="btn-core-save"(?![^>]*disabled)/, "usable again");
  // an error without a list: one line
  backend.failures[`PUT /api/bots/${F.botId(1)}/core`] = [409, "正在初始化，请等它结束（或先取消）后再修改。", { code: "init_busy" }];
  panelClick(env, "core-save");
  await flushAll(env);
  assert.match(html(env, "detail-editors"), /id="core-error"[^>]*>正在初始化，请等它结束/);
  assert.ok(!/core-server-problems"[^>]*><li>/.test(html(env, "detail-editors")), "the list of the older refusal is gone");
});

test("text typed while a save is on its way is a LATER edit: it stays (unsaved), what was sent is saved", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "core");
  typeCore(env, "voice", "第一版");
  const gate = backend.hold((key) => key === `PUT /api/bots/${F.botId(1)}/core`);
  panelClick(env, "core-save");
  await flushAll(env, 10);
  assert.equal(gate.parked.length, 1);
  assert.match(html(env, "detail-editors"), /id="btn-core-save"[^>]* disabled[^>]*>保存中…/);
  typeCore(env, "voice", "第二版（保存期间又改了）");
  gate.release(0);
  await flushAll(env);
  assert.equal(detailOf(backend, "N1").core.sections.voice, "第一版", "the server has what was sent");
  assert.equal(boxText(env, "ta-core-voice"), "第二版（保存期间又改了）", "the box keeps the later edit");
  assert.match(html(env, "detail-editors"), /id="btn-core-save"(?![^>]*disabled)/, "and it is an unsaved difference: Save is on");
  assert.match(html(env, "detail-editors"), /id="btn-core-revert"(?![^>]*disabled)/);
});

test("extras: add (up to 3), type a title and a text, delete; the file has them as '# 附加：<标题>'", async () => {
  const { env, backend } = await live({ status: "idle" });
  backend.botsApi.set("N1", { core: { extras: [] } });
  await env.app().bots().refreshBots();
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "core");
  assert.ok(!html(env, "detail-editors").includes("core-extra-title"));
  for (let i = 0; i < 3; i++) panelClick(env, "core-extra-add");
  assert.equal((html(env, "detail-editors").match(/class="form-input core-extra-title"/g) || []).length, 3);
  assert.match(html(env, "detail-editors"), /id="btn-core-extra-add"[^>]* disabled/, "three is the most");
  panelClick(env, "core-extra-add"); // (a stale click on the disabled button)
  assert.equal((html(env, "detail-editors").match(/class="form-input core-extra-title"/g) || []).length, 3);
  panelClick(env, "core-extra-del", { "data-index": "1" });
  assert.equal((html(env, "detail-editors").match(/class="form-input core-extra-title"/g) || []).length, 2);
  typeExtraTitle(env, 0, "口头禅");
  typeExtraText(env, 0, "好的好的");
  typeExtraTitle(env, 1, "禁忌");
  assert.match(env.el("core-problems").innerHTML, /「附加：禁忌」不能为空。/, "an empty extra is a fault");
  assert.equal(env.el("btn-core-save").disabled, true);
  typeExtraText(env, 1, "不说脏话");
  assert.equal(env.el("btn-core-save").disabled, false);
  panelClick(env, "core-save");
  await flushAll(env);
  assert.match(backend.corePuts[0].markdown, /# 附加：口头禅\n好的好的\n\n# 附加：禁忌\n不说脏话\n$/);
  assert.deepEqual(detailOf(backend, "N1").core.extras.map((e) => [e.title, e.text]), [["口头禅", "好的好的"], ["禁忌", "不说脏话"]]);
  // a title that is used twice, and one that is too long
  panelClick(env, "core-extra-add");
  typeExtraTitle(env, 2, "口头禅");
  typeExtraText(env, 2, "x");
  assert.match(env.el("core-problems").innerHTML, /附加分区「口头禅」重复了。/);
  typeExtraTitle(env, 2, "长".repeat(21));
  assert.match(env.el("core-problems").innerHTML, /附加分区的标题最多 20 字（现在 21 字）/);
});

test("'✨ 用主持人模型完善': the notes go to the moderator's model, the DRAFT is shown first and nothing is saved until the user confirms", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N3");
  await flushAll(env);
  await openEditor(env, "core");
  env.el("ta-core-notes").value = "爱反问\n先看票型";
  env.el("detail-panel").dispatch("input", { target: { id: "ta-core-notes", value: "爱反问\n先看票型", getAttribute: () => null } });
  panelClick(env, "core-refine");
  await flushAll(env);
  assert.deepEqual(backend.refineCalls, [{ id: F.botId(3), text: "爱反问\n先看票型" }]);
  const ed = html(env, "detail-editors");
  assert.match(ed, /id="core-draft"/);
  assert.match(textOf(ed), /主持人模型给出的草稿（尚未保存，请先看一遍）/);
  assert.match(textOf(ed), /身份\s*：爱反问/);
  assert.match(ed, /data-detail-action="core-draft-save"/);
  assert.match(ed, /data-detail-action="core-draft-edit"/);
  assert.match(ed, /data-detail-action="core-draft-discard"/);
  assert.equal((backend.corePuts || []).length, 0, "no PUT: the draft is only a proposal");
  assert.equal(detailOf(backend, "N3").core.sections.identity, F.makeCore(3).identity, "and the server still has the old text");
  // discard: nothing saved, the notes stay
  panelClick(env, "core-draft-discard");
  assert.ok(!html(env, "detail-editors").includes("core-draft"));
  assert.match(html(env, "detail-editors"), />爱反问\n先看票型<\/textarea>/);
  // refine again and accept: ONE PUT with the draft
  panelClick(env, "core-refine");
  await flushAll(env);
  panelClick(env, "core-draft-save");
  await flushAll(env);
  assert.equal(backend.corePuts.length, 1);
  assert.match(backend.corePuts[0].markdown, /^---\nformat: arena-core\nversion: 1\nname: Carol\n---\n# 身份\n爱反问/);
  assert.ok(!html(env, "detail-editors").includes("core-draft"), "the draft is gone after it was saved");
  assert.equal(detailOf(backend, "N3").core.sections.identity, "爱反问");
});

test("a draft can be put into the editor boxes to be corrected before it is saved; a draft with faults cannot be saved as it is", async () => {
  const { env, backend } = await live({ status: "idle" });
  backend.refineDraft = CORE_MD("草稿身份", "字".repeat(700), "草稿分析");
  cardClick(env, "N3");
  await flushAll(env);
  await openEditor(env, "core");
  env.el("ta-core-notes").value = "随便";
  env.el("detail-panel").dispatch("input", { target: { id: "ta-core-notes", value: "随便", getAttribute: () => null } });
  panelClick(env, "core-refine");
  await flushAll(env);
  assert.match(html(env, "core-draft-problems") || html(env, "detail-editors"), /「说话方式」最多 600 字（现在 700 字）。/);
  assert.match(html(env, "detail-editors"), /id="btn-core-draft-save"[^>]* disabled/, "it would be refused: no 采用并保存");
  assert.match(textOf(html(env, "detail-editors")), /草稿有上面这些问题：可以先放进编辑框，改好再保存。/);
  panelClick(env, "core-draft-edit");
  assert.ok(!html(env, "detail-editors").includes('id="core-draft"'));
  assert.equal(boxText(env, "ta-core-identity"), "草稿身份");
  assert.match(html(env, "detail-editors"), /id="core-count-voice">700 \/ 600</);
  assert.equal((backend.corePuts || []).length, 0, "still nothing saved");
  typeCore(env, "voice", "改短了");
  assert.equal(env.el("btn-core-save").disabled, false);
  panelClick(env, "core-save");
  await flushAll(env);
  assert.equal(detailOf(backend, "N3").core.sections.voice, "改短了");
  assert.equal(detailOf(backend, "N3").core.sections.identity, "草稿身份");
  // a draft that is no file at all: shown as text, cannot be adopted
  backend.refineDraft = "这不是合并格式";
  panelClick(env, "core-refine");
  await flushAll(env);
  assert.match(html(env, "detail-editors"), /id="btn-core-draft-save"[^>]* disabled/);
  assert.match(html(env, "detail-editors"), /id="btn-core-draft-edit"[^>]* disabled/);
  assert.match(textOf(html(env, "detail-editors")), /缺少文件头/);
});

test("refine without notes uses the L0 as it is; with nothing at all it asks for some notes; the server's errors are shown, the notes stay", async () => {
  const { env, backend } = await live({ status: "idle" });
  backend.botsApi.set("N6", { core: { identity: "", voice: "", mind: "", extras: [] } });
  await env.app().bots().refreshBots();
  cardClick(env, "N6");
  await flushAll(env);
  await openEditor(env, "core");
  panelClick(env, "core-refine");
  await flushAll(env, 10);
  assert.match(html(env, "detail-editors"), /id="core-refine-error"[^>]*>请先在上面写几个要点，或者先写一点底层设定/);
  assert.equal(backend.refineCalls, undefined);
  // typed L0 text counts as notes
  typeCore(env, "identity", "一个直觉派");
  panelClick(env, "core-refine");
  await flushAll(env);
  assert.match(backend.refineCalls[0].text, /^---\nformat: arena-core\nversion: 1\nname: Frank\n---\n# 身份\n一个直觉派/);
  panelClick(env, "core-draft-discard");
  // the server's refusals
  env.el("ta-core-notes").value = "我的要点";
  env.el("detail-panel").dispatch("input", { target: { id: "ta-core-notes", value: "我的要点", getAttribute: () => null } });
  backend.refineError = [400, "主持人还没有绑定模型，不能完善：请先在「模型绑定」里给主持人选一个模型。", "moderator_unbound"];
  panelClick(env, "core-refine");
  await flushAll(env);
  assert.match(html(env, "detail-editors"), /id="core-refine-error"[^>]*>主持人还没有绑定模型/);
  assert.ok(!html(env, "detail-editors").includes("core-draft"));
  assert.match(html(env, "detail-editors"), /id="btn-core-refine"(?![^>]*disabled)/, "usable again");
  assert.match(html(env, "detail-editors"), />我的要点<\/textarea>/, "the notes are still there");
  backend.refineError = [502, "主持人的模型没有回答：HTTP 500", "model_failed"];
  panelClick(env, "core-refine");
  await flushAll(env);
  assert.match(html(env, "detail-editors"), /主持人的模型没有回答/);
});

test("a refine that is on its way when the L0 is saved, or a new game starts, brings no draft back", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "core");
  env.el("ta-core-notes").value = "要点";
  env.el("detail-panel").dispatch("input", { target: { id: "ta-core-notes", value: "要点", getAttribute: () => null } });
  const gate = backend.hold((key) => key === `POST /api/bots/${F.botId(1)}/core:refine`);
  panelClick(env, "core-refine");
  await flushAll(env, 10);
  assert.match(html(env, "detail-editors"), /完善中…/);
  // a new game while it is on its way
  env.sse("snapshot", { session: F.makeSession({ session_id: "next-game", status: "idle", event_id: 900 }) }, 900);
  await flushAll(env);
  gate.release(0);
  await flushAll(env);
  assert.ok(!html(env, "detail-editors").includes("core-draft"), "the draft of the game before is not shown");
  assert.match(html(env, "detail-editors"), /id="btn-core-refine"(?![^>]*disabled)/);
});

test("清除: the question, then DELETE /api/bots/{id}/core; the editor is empty and the bot cannot start any more", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
  cardClick(env, "N2");
  await flushAll(env);
  await openEditor(env, "core");
  panelClick(env, "core-clear");
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.match(dialogText(env), /将清空 Bob 的底层设定。清除后需要重新生成，否则无法开始对局；已有的初始化会变为需重新初始化。/);
  assert.match(env.el("btn-dialog-ok").className, /btn-danger-outline/);
  dialogCancel(env);
  await flushAll(env);
  assert.equal(posts(backend, "DELETE", "/core").length, 0);
  panelClick(env, "core-clear");
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.equal(posts(backend, "DELETE", "/core").length, 1);
  assert.equal(boxText(env, "ta-core-identity"), "");
  assert.match(textOf(html(env, "detail-editors")), /▼ 底层设定 还没有设定/);
  assert.equal(env.app().state.session.participants.find((p) => p.id === "N2").init_status, "stale");
  assert.equal(env.app().state.session.bots.ready.ok, false, "the start check says no");
});

test("底层设定 as a .md file: 导出 downloads the merged file; 导入 reads a file, asks, saves it - and says what is wrong with a file that is no L0", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "core");
  panelClick(env, "core-export");
  await flushAll(env);
  assert.equal(env.downloads.length, 1);
  assert.equal(env.downloads[0].download, "Alice.core.md");
  assert.ok(backend.calls.some((c) => c.method === "GET" && c.url.endsWith(`/api/bots/${F.botId(1)}/core?download=1`)));
  // import: the click opens the picker of the page's hidden input
  let clicked = 0;
  env.el("file-core-import").click = () => clicked++;
  panelClick(env, "core-import");
  assert.equal(clicked, 1);
  pick(env, "file-core-import", file("alice.core.md", CORE_MD("导入的身份", "导入的说话", "导入的分析")));
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.match(dialogText(env), /将用文件 alice.core.md 里的内容替换 Alice 的底层设定（\d+ 字）。名字、头像和记忆不变。/);
  dialogOk(env);
  await flushAll(env);
  assert.equal(backend.corePuts.length, 1);
  assert.equal(detailOf(backend, "N1").core.sections.identity, "导入的身份");
  assert.equal(detailOf(backend, "N1").core.extras.length, 0, "the file had no extras: the old extra is gone");
  assert.equal(boxText(env, "ta-core-identity"), "导入的身份");
  // a file that is not an L0: nothing is asked or sent, the faults are listed
  panelClick(env, "core-import");
  pick(env, "file-core-import", file("junk.md", "随便写点什么"));
  await flushAll(env);
  assert.ok(!dialogOpen(env));
  assert.equal(backend.corePuts.length, 1);
  assert.match(html(env, "detail-editors"), /「junk.md」不是合法的底层设定文件。/);
  assert.match(html(env, "detail-editors"), /缺少文件头：文件开头要有用 --- 包住的 format: arena-core 和 version: 1。/);
  // the picker of an older click is not reused: a file chosen without a click is ignored
  pick(env, "file-core-import", file("late.md", CORE_MD("x", "y", "z")));
  await flushAll(env);
  assert.equal(backend.corePuts.length, 1);
});

test("what was typed in the L0 editor is kept per bot (also when its section is closed and opened, and while another player's panel is open)", async () => {
  const { env } = await live({ status: "idle" });
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "core");
  typeCore(env, "identity", "没保存的想法");
  await openEditor(env, "memory");
  await openEditor(env, "core");
  await openEditor(env, "core");
  assert.equal(boxText(env, "ta-core-identity"), "没保存的想法");
  env.sse("agent.status", { id: "N1", state: "thinking" });
  env.sse("agent.metrics", { id: "N1", calls: 3, prompt_tokens: 10, completion_tokens: 5 });
  assert.equal(boxText(env, "ta-core-identity"), "没保存的想法", "live updates repaint the head / state, not the editors");
  assert.match(html(env, "detail-editors"), /id="btn-core-save"(?![^>]*disabled)/);
  cardClick(env, "N1");
  cardClick(env, "N2");
  await flushAll(env);
  assert.ok(!html(env, "detail-editors").includes("没保存的想法"), "another player's panel has its own text");
  cardClick(env, "N2");
  cardClick(env, "N1");
  await flushAll(env);
  assert.equal(boxText(env, "ta-core-identity"), "没保存的想法", "and the first one's is as it was left");
  // a new game drops it
  env.sse("snapshot", { session: F.makeSession({ session_id: "next", status: "idle", event_id: 900 }) }, 900);
  await flushAll(env);
  assert.notEqual(boxText(env, "ta-core-identity"), "没保存的想法");
});

test("an answer that comes while the user is typing in a box repaints nothing (no caret is lost); the repaint comes when the focus leaves", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "core");
  // the user is in the identity box
  const box = env.el("ta-core-identity");
  box.tagName = "TEXTAREA";
  env.el("detail-editors").contains = (n) => n === box;
  env.document.activeElement = box;
  typeCore(env, "identity", "正在输入的字");
  const typingHtml = html(env, "detail-editors");
  backend.botsApi.set("N1", { binding: { ok: true, label: "x" } });
  env.sse("bots.updated", { bots: F.makeBots({ learning_usage: { calls: 1, tokens: 1, cost_usd: 0 } }) }); // an event arrives
  assert.equal(html(env, "detail-editors"), typingHtml, "the box the user is typing in is not replaced");
  assert.equal(boxText(env, "ta-core-identity"), F.makeCore(1).identity, "(the drawn text is the saved one; the typed text lives in the draft)");
  env.document.activeElement = null;
  env.el("detail-panel").dispatch("focusout", {});
  assert.equal(boxText(env, "ta-core-identity"), "正在输入的字", "when the focus leaves, the box is drawn from the draft");
});

test("while the bot is being initialized every write is off, with the reason", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N2");
  await flushAll(env);
  await openEditor(env, "core");
  typeCore(env, "voice", "想改");
  backend.botsApi.state.initJob = { job_id: "j", ids: [F.botId(2)], started_at: 1, states: {} };
  env.sse("bots.updated", { bots: backend.botsApi.live() });
  await flushAll(env);
  const ed = html(env, "detail-editors");
  assert.match(textOf(ed), /正在初始化这个玩家：现在不能修改它（可以先取消初始化）。/);
  assert.match(ed, /id="btn-core-save"[^>]* disabled/);
  assert.match(ed, /id="btn-core-clear"[^>]* disabled/);
  assert.match(ed, /id="btn-core-refine"[^>]* disabled/);
  assert.match(ed, /id="btn-core-import"[^>]* disabled/);
  assert.equal(boxText(env, "ta-core-voice"), "想改", "what was typed stays");
});

// =====================================================================================================
// 记忆
// =====================================================================================================
test("the memory editor: the five parts with their counters, the playbook as a list, the record (read-only), the scenario of the three that belong to one", async () => {
  const { env } = await live({ status: "idle" }, { botsPreset: "ready" });
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "memory");
  const ed = html(env, "detail-editors");
  for (const part of ["arch_notes", "digest", "playbook", "questions", "people"]) assert.match(ed, new RegExp(`id="ta-part-${part}"`), part);
  assert.match(textOf(ed), /操作笔记[^]*规则要点\s*（剧本：werewolf_12p）[^]*打法\s*（剧本：werewolf_12p）[^]*待解问题\s*（剧本：werewolf_12p）[^]*印象/);
  assert.match(ed, /id="part-count-playbook">3 \/ 20 条</);
  assert.match(ed, /id="part-count-people">2 \/ 30 人</);
  assert.match(ed, /id="part-count-arch_notes">\d+ \/ 800 字</);
  assert.match(ed, /id="mem-playbook-preview"/);
  assert.match(textOf(ed), /中 \+3 \/ −1 #发言 两个人都说自己是同一个身份时/);
  assert.match(ed, /id="mem-record">场次 7，胜 3，负 4，胜率 43%<br>平民：5 局，胜 3</);
  assert.ok(!/id="ta-part-record"/.test(ed), "the record cannot be edited here");
  assert.match(boxText(env, "ta-part-people"), /- Bob：发言短，站边快，可信度中。/);
  assert.match(textOf(ed), /你的修改\s*直接生效\s*，不影响初始化状态；复盘写进来的内容也是直接生效，没有回滚/);
});

test("editing a part: typing updates its counter, the faults and Save live; Save PUTs the part (with the scenario of the three that have one) and the editor shows what was kept", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "memory");
  assert.match(html(env, "detail-editors"), /id="btn-part-save-digest"[^>]* disabled/);
  const before = html(env, "detail-editors");
  typePart(env, "digest", "新的规则要点");
  assert.equal(html(env, "detail-editors"), before, "no repaint while typing");
  assert.equal(env.el("part-count-digest").textContent, "6 / 800 字");
  assert.equal(env.el("btn-part-save-digest").disabled, false);
  assert.equal(env.el("btn-part-revert-digest").disabled, false);
  typePart(env, "digest", "字".repeat(801));
  assert.match(env.el("part-count-digest").className, /over/);
  assert.match(env.el("part-problems-digest").innerHTML, /「规则要点」最多 800 字（现在 801 字）。/);
  assert.equal(env.el("btn-part-save-digest").disabled, true);
  typePart(env, "digest", "新的规则要点");
  panelClick(env, "part-save", { "data-part": "digest" });
  await flushAll(env);
  assert.deepEqual(backend.memoryPuts, [{ id: F.botId(1), part: "digest", content: "新的规则要点", scenario_id: "werewolf_12p" }]);
  assert.match(lastToast(env), /规则要点已保存/);
  assert.equal(boxText(env, "ta-part-digest"), "新的规则要点");
  assert.equal(detailOf(backend, "N1").memory.digest, "新的规则要点");
  assert.equal(detailOf(backend, "N1").init.status, "ready", "a user's edit does not touch the initialization (C9)");
  assert.equal(env.app().state.session.participants.find((p) => p.id === "N1").init_status, "ready");
  // the notes of the bot itself have no scenario
  typePart(env, "arch_notes", "我自己的笔记");
  panelClick(env, "part-save", { "data-part": "arch_notes" });
  await flushAll(env);
  assert.deepEqual(backend.memoryPuts[1], { id: F.botId(1), part: "arch_notes", content: "我自己的笔记" });
});

test("the playbook and the impressions are read like the server reads them: what is not understood is refused with its words, Revert brings the saved text back", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "memory");
  typePart(env, "playbook", "没有符号的话");
  assert.match(env.el("part-problems-playbook").innerHTML, /没有读出任何一条打法：每条写成一行/);
  assert.equal(env.el("btn-part-save-playbook").disabled, true);
  typePart(env, "playbook", "- [把握:高|支持:2|反例:0|来源:用户|#警上] 警上先听完再站边\n- 另一条");
  assert.equal(env.el("part-count-playbook").textContent, "2 / 20 条");
  assert.equal(env.el("btn-part-save-playbook").disabled, false);
  panelClick(env, "part-save", { "data-part": "playbook" });
  await flushAll(env);
  const saved = detailOf(backend, "N1").memory.playbook;
  assert.deepEqual(saved.map((e) => [e.text, e.confidence, e.support, e.tags]), [["警上先听完再站边", "高", 2, ["警上"]], ["另一条", "低", 0, []]]);
  assert.match(html(env, "detail-editors"), /pb-conf-高/);
  typePart(env, "people", `- ${"名".repeat(21)}：太长的名字\n- Bob：${"评".repeat(81)}`);
  assert.match(env.el("part-problems-people").innerHTML, /对「Bob」的印象最多 80 字（现在 81 字）。/);
  panelClick(env, "part-revert", { "data-part": "people" });
  assert.match(boxText(env, "ta-part-people"), /^- Bob：发言短，站边快，可信度中。/);
  assert.match(html(env, "detail-editors"), /id="btn-part-revert-people"[^>]* disabled/, "nothing to revert any more");
});

test("a refusal by the server shows its words under the part and keeps what was typed; text typed while a save is on its way stays as the next edit", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "memory");
  typePart(env, "questions", "一个问题");
  backend.failures[`PUT /api/bots/${F.botId(1)}/memory/questions`] = [400, "没有保存：「待解问题」最多 400 字（现在 401 字）。", { problems: ["「待解问题」最多 400 字（现在 401 字）。"] }];
  panelClick(env, "part-save", { "data-part": "questions" });
  await flushAll(env);
  assert.match(html(env, "detail-editors"), /id="part-error-questions"[^>]*>「待解问题」最多 400 字（现在 401 字）。</);
  assert.equal(boxText(env, "ta-part-questions"), "一个问题");
  delete backend.failures[`PUT /api/bots/${F.botId(1)}/memory/questions`];
  const gate = backend.hold((key) => key === `PUT /api/bots/${F.botId(1)}/memory/questions`);
  panelClick(env, "part-save", { "data-part": "questions" });
  await flushAll(env, 10);
  typePart(env, "questions", "一个问题，保存时又加了一句");
  gate.release(0);
  await flushAll(env);
  assert.equal(detailOf(backend, "N1").memory.questions, "一个问题");
  assert.equal(boxText(env, "ta-part-questions"), "一个问题，保存时又加了一句");
  assert.match(html(env, "detail-editors"), /id="btn-part-save-questions"(?![^>]*disabled)/);
});

test("清除 of a part: the question, then DELETE ...?part=; a file the game needs asks for more; 清除全部记忆 empties everything and the bot is uninitialized", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "memory");
  panelClick(env, "part-clear", { "data-part": "people" });
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.match(dialogText(env), /将清空 Alice 的「印象」。不能撤销。/);
  dialogCancel(env);
  await flushAll(env);
  assert.equal(backend.memoryDeletes, undefined);
  panelClick(env, "part-clear", { "data-part": "people" });
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.memoryDeletes, [{ id: F.botId(1), part: "people" }]);
  assert.equal(boxText(env, "ta-part-people"), "");
  assert.match(lastToast(env), /印象已清除/);
  // the playbook is needed to start: say so
  panelClick(env, "part-clear", { "data-part": "playbook" });
  await flushAll(env);
  assert.match(dialogText(env), /这是开局必需的文件：清除后要重新初始化才能开局。/);
  dialogCancel(env);
  await flushAll(env);
  // everything
  panelClick(env, "memory-clear-all");
  await flushAll(env);
  assert.match(dialogText(env), /将清空 Alice 学到的全部记忆：操作笔记、规则要点、打法、待解问题、印象、战绩。初始化结果一并清空（变为未初始化），底层设定保留。/);
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.memoryDeletes[1], { id: F.botId(1), part: null });
  assert.equal(env.app().state.session.participants.find((p) => p.id === "N1").init_status, "uninitialized");
  assert.equal(boxText(env, "ta-part-playbook"), "");
  assert.match(textOf(html(env, "detail-editors")), /还没有战绩。/);
  assert.match(textOf(html(env, "detail-editors")), /▶ 记忆 空|▼ 记忆 空/);
});

test("emptying a file the game needs, in its box and saving, asks first (the bot is initialized)", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
  cardClick(env, "N1");
  await flushAll(env);
  await openEditor(env, "memory");
  typePart(env, "arch_notes", "   ");
  panelClick(env, "part-save", { "data-part": "arch_notes" });
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.match(dialogText(env), /「操作笔记」是开局必需的文件：清空以后，Alice 要重新初始化才能开局。/);
  dialogCancel(env);
  await flushAll(env);
  assert.equal(backend.memoryPuts, undefined);
  panelClick(env, "part-save", { "data-part": "arch_notes" });
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.memoryPuts, [{ id: F.botId(1), part: "arch_notes", content: "   ", scenario_id: undefined }].map(({ scenario_id, ...rest }) => rest));
});

// =====================================================================================================
// 待确认 (proposals)
// =====================================================================================================
const withProposals = (backend) => {
  backend.botsApi.set("N2", {
    core: { extras: [{ title: "口头禅", slug: "x1", text: "「行。」" }] },
    staging: [
      F.makeProposal({ id: "p-0001", section: "voice", before: "你说话短。\n爱反问。", after: "你说话短。\n爱反问。\n先说结论，再补理由。", reason: "接管者说话短、先说结论" }),
      F.makeProposal({ id: "p-0002", section: "extra:x1", before: "「行。」", after: "「行，我听你的。」", reason: "口头禅" }),
    ],
  });
  backend.botsApi.initialized("N2"); // (the L0 was changed under the bot: it is initialized under that L0)
};

test("待确认: every proposal says which section, why and from where, and shows the old text against the new one", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
  withProposals(backend);
  await env.app().bots().refreshBots();
  cardClick(env, "N2");
  await flushAll(env);
  assert.match(textOf(html(env, "detail-editors")), /▶ 待确认 2 条待确认/);
  await openEditor(env, "staging");
  const ed = html(env, "detail-editors");
  assert.match(textOf(ed), /说话方式 复盘 s-test/);
  assert.match(textOf(ed), /理由：接管者说话短、先说结论/);
  assert.match(textOf(ed), /附加：口头禅 复盘 s-test/);
  assert.match(ed, /<div class="diff-line same"><span class="diff-mark"> <\/span><span class="diff-text">你说话短。<\/span><\/div>/);
  assert.match(ed, /<div class="diff-line add"><span class="diff-mark">\+<\/span><span class="diff-text">先说结论，再补理由。<\/span><\/div>/);
  assert.match(ed, /<div class="diff-line del"><span class="diff-mark">−<\/span><span class="diff-text">「行。」<\/span><\/div>/);
  assert.equal((ed.match(/data-detail-action="stage-accept"/g) || []).length, 2);
  // a bot with no proposals says so
  cardClick(env, "N2");
  cardClick(env, "N3");
  await flushAll(env);
  await openEditor(env, "staging");
  assert.match(textOf(html(env, "detail-editors")), /没有待确认的提案。/);
});

test("采纳 writes the proposal into the L0 (a bot that is initialized is asked first and becomes ⚠); 拒绝 drops it; a proposal that cannot be applied says why and stays", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
  withProposals(backend);
  await env.app().bots().refreshBots();
  cardClick(env, "N2");
  await flushAll(env);
  await openEditor(env, "staging");
  panelClick(env, "stage-reject", { "data-pid": "p-0002" });
  await flushAll(env);
  assert.deepEqual(backend.stagingCalls, [{ id: F.botId(2), pid: "p-0002", action: "reject" }]);
  assert.match(lastToast(env), /已拒绝这条提案/);
  assert.equal((html(env, "detail-editors").match(/data-proposal=/g) || []).length, 1);
  // accept: the question of an initialized bot first
  panelClick(env, "stage-accept", { "data-pid": "p-0001" });
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.equal(env.el("dialog-title").textContent, "采纳提案");
  assert.match(dialogText(env), /将变为需重新初始化，记忆保留。/);
  dialogCancel(env);
  await flushAll(env);
  assert.equal(backend.stagingCalls.length, 1);
  panelClick(env, "stage-accept", { "data-pid": "p-0001" });
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.stagingCalls[1], { id: F.botId(2), pid: "p-0001", action: "accept" });
  assert.equal(detailOf(backend, "N2").core.sections.voice, "你说话短。\n爱反问。\n先说结论，再补理由。");
  assert.match(lastToast(env), /已采纳，写进了 Bob 的底层设定/);
  assert.match(textOf(html(env, "detail-editors")), /没有待确认的提案。/);
  assert.equal(env.app().state.session.participants.find((p) => p.id === "N2").init_status, "stale");
  // a proposal the server cannot apply: its words, and the proposal stays
  backend.botsApi.set("N3", { staging: [F.makeProposal({ id: "p-0009", section: "voice", after: "字".repeat(700) })] });
  await env.app().bots().refreshBots();
  cardClick(env, "N2");
  cardClick(env, "N3");
  await flushAll(env);
  await openEditor(env, "staging");
  backend.failures[`POST /api/bots/${F.botId(3)}/staging/p-0009:accept`] = [400, "没有采纳：「说话方式」最多 600 字（现在 700 字）。", { problems: ["「说话方式」最多 600 字（现在 700 字）。"] }];
  panelClick(env, "stage-accept", { "data-pid": "p-0009" });
  await flushAll(env);
  dialogOk(env); // (N3 is initialized: the question of the L0 change first)
  await flushAll(env);
  assert.match(html(env, "detail-editors"), /class="form-error editor-problems"><li>「说话方式」最多 600 字（现在 700 字）。<\/li>/);
  assert.equal((html(env, "detail-editors").match(/data-proposal=/g) || []).length, 1, "the proposal stays");
});

// =====================================================================================================
// 导入 / 导出
// =====================================================================================================
test("导出 .arenabot: GET /api/bots/{id}/export is fetched with the access token as a HEADER (a link cannot send one) and saved under the server's file name", async () => {
  const { env, backend } = await live({ status: "idle" }, { requireToken: "tok-1" }, { storedToken: "tok-1" });
  cardClick(env, "N4");
  await flushAll(env);
  await openEditor(env, "transfer");
  panelClick(env, "bot-export");
  await flushAll(env);
  const call = backend.calls.find((c) => c.method === "GET" && c.url.endsWith(`/api/bots/${F.botId(4)}/export`));
  assert.ok(call, "requested");
  assert.equal(call.headers["X-Arena-Token"], "tok-1");
  assert.equal(env.downloads.length, 1);
  assert.equal(env.downloads[0].download, "David-20251119-120000.arenabot", "the name the server gave it (Content-Disposition)");
  assert.match(env.downloads[0].href, /^blob:/, "saved from memory: no ?token= in any URL");
  // a refusal is a toast, not a dead click
  backend.failures[`GET /api/bots/${F.botId(4)}/export`] = [501, "导出和导入 .arenabot 的功能还没有就绪。", { code: "not_implemented" }];
  panelClick(env, "bot-export");
  await flushAll(env);
  assert.match(lastToast(env), /下载失败：导出和导入 \.arenabot 的功能还没有就绪。/);
  assert.equal(env.downloads.length, 1);
});

test("导入 .arenabot: a file is chosen, the question names the file and the bot, then one POST with the file as the body; the bot is the file's now", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
  cardClick(env, "N4");
  await flushAll(env);
  await openEditor(env, "transfer");
  let clicked = 0;
  env.el("file-bot-import").click = () => clicked++;
  panelClick(env, "bot-import");
  assert.equal(clicked, 1);
  const f = file("Zed-20251119.arenabot", "PK...zip", { size: 2048, bot: { name: "Zed", core: F.makeCore(9) } });
  pick(env, "file-bot-import", f);
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.equal(env.el("dialog-title").textContent, "导入 bot");
  assert.match(said(env), /将用文件 Zed-20251119.arenabot（2 KiB）里的 bot 覆盖 David：名字、头像、底层设定、记忆都会被替换。此操作不能撤销。/);
  assert.match(env.el("btn-dialog-ok").className, /btn-danger-outline/);
  dialogCancel(env);
  await flushAll(env);
  assert.equal(backend.botImports, undefined, "no: nothing was sent");
  panelClick(env, "bot-import");
  pick(env, "file-bot-import", f);
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.botImports, [{ id: F.botId(4), name: "Zed-20251119.arenabot", size: 2048 }]);
  assert.equal(backend.calls.at(-1) === undefined, false);
  assert.equal(env.app().state.session.participants.find((p) => p.id === "N4").name, "Zed");
  assert.match(textOf(html(env, "detail-editors")), /已导入：Zed。/);
  assert.match(lastToast(env), /已导入，这个玩家被文件里的 bot 替换/);
  assert.equal(env.app().state.session.participants.find((p) => p.id === "N4").init_status, "uninitialized", "the state is judged by the hashes again");
});

test("导入 .arenabot that the server refuses: too big / name taken / a file with faults (listed) - the bot is unchanged", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N4");
  await flushAll(env);
  await openEditor(env, "transfer");
  const attempt = async (name) => {
    panelClick(env, "bot-import");
    pick(env, "file-bot-import", file(name, "zip"));
    await flushAll(env);
    dialogOk(env);
    await flushAll(env);
    return textOf(html(env, "detail-editors"));
  };
  assert.match(await attempt("big.arenabot"), /\.arenabot 文件超过了 20 MiB 的上限。/);
  assert.match(await attempt("taken.arenabot"), /名字「Bob」已被 N2 使用，名字不能重复。/);
  const bad = await attempt("bad.arenabot");
  assert.match(bad, /这个 bot 文件不能导入：/);
  assert.match(bad, /缺少 bot.json，或者它不是有效的 JSON。/);
  assert.match(bad, /core\/voice.md：「说话方式」最多 600 字（现在 612 字）。/);
  assert.equal(env.app().state.session.participants.find((p) => p.id === "N4").name, "David");
  assert.match(lastToast(env), /导入失败/);
});

test("从默认库导入: a list of the library (name + one line), the question, POST /import-default {name}; a name another bot has is refused", async () => {
  const { env, backend } = await live({ status: "idle" });
  cardClick(env, "N4");
  await flushAll(env);
  await openEditor(env, "transfer");
  const select = html(env, "detail-editors").match(/<select[^>]*id="sel-default-bot"[^>]*>([^]*?)<\/select>/)[1];
  assert.equal((select.match(/<option/g) || []).length, 12);
  assert.match(select, /<option value="Alice" selected>Alice · 打了二十多年线下局的老玩家，话不多，一开口就是结论。<\/option>|<option value="Alice" selected>Alice · 打了二十多年线下局的老玩家，话不多，一开口就是结…<\/option>/);
  // choose Leo (nobody at the table is called Leo... yes: seat 12 is) - choose a name another bot has: refused
  env.el("sel-default-bot").value = "Leo";
  env.el("detail-panel").dispatch("change", { target: { id: "sel-default-bot", value: "Leo", getAttribute: () => null } });
  panelClick(env, "bot-default");
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.match(said(env), /将用默认库里的 Leo 覆盖 David：名字、头像、底层设定、记忆都会被替换。此操作不能撤销。/);
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.defaultImports, [{ id: F.botId(4), name: "Leo" }]);
  assert.match(textOf(html(env, "detail-editors")), /名字「Leo」已被 N12 使用，名字不能重复。/);
  assert.equal(env.app().state.session.participants.find((p) => p.id === "N4").name, "David");
  // a name nobody has at the table: free the name first
  backend.botsApi.set("N12", { name: "Zed12" });
  await env.app().bots().refreshBots();
  panelClick(env, "bot-default");
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.equal(env.app().state.session.participants.find((p) => p.id === "N4").name, "Leo");
  assert.match(textOf(html(env, "detail-editors")), /已从默认库导入：Leo。/);
});

test("importing is for before the game: with a game on, the buttons are off and say why", async () => {
  const { env } = await live({ status: "running" });
  cardClick(env, "N4");
  await flushAll(env);
  await openEditor(env, "transfer");
  const ed = html(env, "detail-editors");
  assert.match(ed, /id="btn-bot-import"[^>]* disabled/);
  assert.match(ed, /id="btn-bot-default"[^>]* disabled/);
  assert.match(ed, /id="sel-default-bot"[^>]* disabled/);
  assert.match(textOf(ed), /对局开始之后不能导入：请先新建对局。/);
  assert.match(ed, /id="btn-bot-export"(?![^>]*disabled)/, "exporting is always fine");
});

H.runTests(tests);
