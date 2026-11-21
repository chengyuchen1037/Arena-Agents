"use strict";
/* The labelling page of the calibration set (bench/label/label.js + index.html), run by node - no browser, no network.
   Run directly (`node tests/bench/test_label_page.js`) or through tests/bench/test_label_page.py. */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..", "..");
const L = require(path.join(ROOT, "bench", "label", "label.js"));
const HTML = fs.readFileSync(path.join(ROOT, "bench", "label", "index.html"), "utf8");
const SOURCE = fs.readFileSync(path.join(ROOT, "bench", "label", "label.js"), "utf8");

const tests = [];
const test = (name, fn) => tests.push({ name, fn });

// ---------------------------------------------------------------------------------------------------------------------- builders
const L3_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["basis", "cites_decisive", "reason"],
  properties: {
    basis: { type: "string", enum: ["decisive_fact", "trivia", "expression", "intuition", "unclear"] },
    cites_decisive: { type: "boolean" },
    reason: { type: "string" },
  },
};
const L3_GUIDE = [
  "你在评一个狼人杀 bot 的**一次决定**。",
  "",
  "## 怎么判",
  "",
  "- `decisive_fact`：主要依据就是决定性事实，或从公开记录里直接推出的同类矛盾。bot 要说得出是哪一条记录。",
  "- `trivia`：主要依据是一个**不影响谁是狼**的细节：首夜验了谁、警徽流的先后、用词。",
  "- `expression`：主要依据是**说话的方式**：条理清不清楚、说得长不长。",
  "- `intuition`：主要依据是感觉、“像狼 / 像好人”、跟着多数。",
  "- `unclear`：没有写理由，或者理由里看不出任何一种依据。",
  "",
  "`cites_decisive`：bot 的理由里**有没有明确提到**决定性事实。",
].join("\n");
const S2_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["match", "runner_up", "confidence", "cues"],
  properties: {
    match: { type: "string", enum: ["A", "B", "C"] },
    runner_up: { type: "string", enum: ["A", "B", "C", "none"] },
    confidence: { type: "string", enum: ["high", "medium", "low"] },
    cues: { type: "string" },
  },
};
const E2_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["closer", "confidence", "reason"],
  properties: {
    closer: { type: "string", enum: ["X", "Y", "tie"] },
    confidence: { type: "string", enum: ["high", "medium", "low"] },
    reason: { type: "string" },
  },
};

function blindId(n) {
  return `b${String(n).padStart(11, "0")}`;
}

function l3(n, over) {
  return {
    format: "arena-bench-label-item",
    version: 1,
    blind_id: blindId(n),
    rubric: "L3",
    rubric_version: 1,
    title: "核心矛盾：这个决定主要依据什么",
    kind: "basis",
    primary: "basis",
    schema: L3_SCHEMA,
    guide: L3_GUIDE,
    view: {
      blind_id: blindId(n),
      rubric: "L3",
      title: "一道题",
      brief: "被测座位是 N5。",
      context: { decisive: "N4 自称猎人，公告里开枪的猎人是 N2。", trap: "N4 说得很有条理。" },
      subject: { kind: "choice", tool: "respond", text: null, value: "N4", reason: `N4 自称猎人，公告对不上（${n}）。`, thought: "" },
    },
    ...over,
  };
}

function s2(n) {
  return {
    format: "arena-bench-label-item",
    version: 1,
    blind_id: blindId(n),
    rubric: "S2",
    rubric_version: 1,
    title: "风格识别：这段发言是哪张风格卡说的",
    kind: "match",
    primary: "match",
    schema: S2_SCHEMA,
    guide: "只看**说话的风格**，不看说了什么立场。",
    view: {
      blind_id: blindId(n),
      rubric: "S2",
      title: "第 2 天白天发言",
      brief: "没有新的死讯。",
      context: {
        cards: [
          { id: "A", text: "你说话短，爱反问。\n篇幅：40–110 字" },
          { id: "B", text: "你说话热络，爱用“咱们”。\n篇幅：150–260 字" },
          { id: "C", text: "你说话慢，爱用比喻。\n篇幅：60–140 字" },
        ],
      },
      subject: { kind: "speech", text: "你急什么？这票你自己信吗？" },
    },
  };
}

function e2(n) {
  return {
    format: "arena-bench-label-item",
    version: 1,
    blind_id: blindId(n),
    rubric: "E2",
    rubric_version: 1,
    title: "向人类学风格：哪一段更像这位真人",
    kind: "pairwise",
    primary: "closer",
    schema: E2_SCHEMA,
    guide: "只比**风格**。",
    view: {
      blind_id: blindId(n),
      rubric: "E2",
      title: "",
      brief: "",
      context: { reference: ["我昨天就说了，N5 不对。", "又在绕。投谁，说一句。"], situation: "第 2 天，白天发言，没有新的信息。" },
      subject: { kind: "pair", x: "我先把今天的情况分三点说明：第一……", y: "N5 还是不对。我今天还投他。" },
    },
  };
}

const jsonl = (rows) => rows.map((row) => JSON.stringify(row)).join("\n") + "\n";
const setOf = (count) => jsonl(Array.from({ length: count }, (_, i) => l3(i + 1)));

function memory() {
  const data = new Map();
  return {
    data,
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => void data.set(key, String(value)),
    removeItem: (key) => void data.delete(key),
  };
}

function clock() {
  let tick = 0;
  return () => `2026-11-20T10:00:${String(tick++).padStart(2, "0")}.000Z`;
}

function controllerOf(text, storage, extra) {
  const controller = L.createController({ storage: storage === undefined ? memory() : storage, now: clock() });
  if (text !== null) assert.equal(controller.load(text, "set.jsonl").ok, true);
  return controller;
}

/** Label item 0 of a loaded L3 set the way a person would: the 2nd option, yes. */
function labelCurrent(controller, number) {
  controller.dispatch({ type: "chooseKey", kind: "enum", number: number || 2 });
  controller.dispatch({ type: "chooseKey", kind: "boolean", value: true });
}

// ---------------------------------------------------------------------------------------------------------------------- the file
test("parseSet: good rows in file order - CRLF, a BOM and blank lines are fine", () => {
  const text = "\uFEFF" + [JSON.stringify(l3(1)), "", JSON.stringify(s2(2)), JSON.stringify(e2(3))].join("\r\n") + "\r\n\r\n";
  const parsed = L.parseSet(text);
  assert.deepEqual(parsed.problems, []);
  assert.deepEqual(parsed.items.map((item) => item.rubric), ["L3", "S2", "E2"]);
});

test("parseSet: a bad line costs the line, not the file - and the problem names the line", () => {
  const rows = [
    JSON.stringify(l3(1)),
    "{this is not json",
    "[1, 2]",
    JSON.stringify({ ...l3(2), blind_id: "" }),
    JSON.stringify({ ...l3(3), schema: { type: "object", properties: {} } }),
    JSON.stringify({ ...l3(4), primary: "nothing" }),
    JSON.stringify({ ...l3(5), rubric_version: 0 }),
    JSON.stringify({ ...l3(6), view: "x" }),
    JSON.stringify({ ...l3(7), schema: { type: "object", properties: { x: { type: "array" } } }, primary: "x" }),
    JSON.stringify(l3(1)),
    JSON.stringify(l3(8)),
  ];
  const parsed = L.parseSet(rows.join("\n"));
  assert.deepEqual(parsed.items.map((item) => item.blind_id), [blindId(1), blindId(8)]);
  assert.equal(parsed.problems.length, 9);
  assert.match(parsed.problems[0], /^第 2 行不是合法的 JSON/);
  assert.match(parsed.problems[1], /^第 3 行不是一个 JSON 对象/);
  assert.match(parsed.problems[2], /^第 4 行：缺 blind_id/);
  assert.match(parsed.problems[3], /^第 5 行：schema 要是带 properties/);
  assert.match(parsed.problems[4], /^第 6 行：primary/);
  assert.match(parsed.problems[5], /^第 7 行：rubric_version/);
  assert.match(parsed.problems[6], /^第 8 行：缺 view/);
  assert.match(parsed.problems[7], /^第 9 行：字段 x 的类型这个页面不支持/);
  assert.match(parsed.problems[8], /^第 10 行：blind_id 重复/);
});

test("parseSet: a judge_items.jsonl chosen by mistake is refused and its bot names never reach a message", () => {
  const judgeItem = { format: "arena-judge-item", version: 1, blind_id: "abc", item_id: "20261120/sit/N5/bot:Alice@secret/0/L3", bot: "bot:Alice@secret", rubric: "L3", context: {}, subject: {} };
  const parsed = L.parseSet(JSON.stringify(judgeItem) + "\n");
  assert.equal(parsed.items.length, 0);
  assert.match(parsed.problems[0], /不是 arena-bench-label-item（要选 calibration-set 写出的 set\.jsonl）/);
  assert.ok(!JSON.stringify(parsed.problems).includes("Alice") && !JSON.stringify(parsed.problems).includes("item_id"));
  const controller = controllerOf(null);
  const result = controller.load(JSON.stringify(judgeItem), "judge_items.jsonl");
  assert.equal(result.ok, false);
  assert.equal(controller.view().loaded, false);
  assert.equal(controller.view().notice.kind, "error");
  assert.ok(!JSON.stringify(controller.view()).includes("Alice"));
  assert.equal(controller.load("", "empty.jsonl").ok, false);
});

test("the fingerprint follows the content of the rows - and nothing else", () => {
  const a = L.parseSet(setOf(3)).items;
  const same = L.parseSet(setOf(3).replace(/\n/g, "\r\n")).items;
  assert.equal(L.fingerprintOf(a), L.fingerprintOf(same), "line endings do not matter");
  assert.equal(L.fingerprintOf(a), L.fingerprintOf(L.parseSet(setOf(3) + "garbage\n").items), "a line that is skipped does not matter");
  assert.notEqual(L.fingerprintOf(a), L.fingerprintOf(L.parseSet(setOf(4)).items));
  assert.notEqual(L.fingerprintOf(a), L.fingerprintOf(L.parseSet(jsonl([l3(1), l3(2), l3(3, { rubric_version: 2 })])).items), "another rubric version is another set");
  assert.notEqual(L.fingerprintOf(a), L.fingerprintOf(L.parseSet(jsonl([l3(2), l3(1), l3(3)])).items), "another order is another set");
  assert.match(L.fingerprintOf(a), /^[0-9a-f]{28}$/);
  assert.equal(L.storageKey("abc"), L.STORAGE_PREFIX + "abc");
  assert.ok(L.storageKey(L.fingerprintOf(a)).includes(L.fingerprintOf(a)));
});

// ---------------------------------------------------------------------------------------------------------------------- the schema
test("validateLabel: the checks of bench.judging.validate_verdict", () => {
  const ok = { basis: "trivia", cites_decisive: false, reason: "" };
  assert.deepEqual(L.validateLabel(L3_SCHEMA, ok), []);
  assert.match(L.validateLabel(L3_SCHEMA, { basis: "trivia" }).join("|"), /缺 cites_decisive.*缺 reason/);
  assert.match(L.validateLabel(L3_SCHEMA, { ...ok, extra: 1 }).join("|"), /多出 extra/);
  assert.match(L.validateLabel(L3_SCHEMA, { ...ok, basis: "vibes" }).join("|"), /label\.basis：.*不在/);
  assert.match(L.validateLabel(L3_SCHEMA, { ...ok, cites_decisive: "yes" }).join("|"), /要是 true \/ false/);
  assert.match(L.validateLabel(L3_SCHEMA, { ...ok, reason: 5 }).join("|"), /要是字符串/);
  assert.match(L.validateLabel(L3_SCHEMA, "x").join("|"), /要是一个对象/);
  assert.match(L.validateLabel({ type: "integer" }, 1.5).join("|"), /要是整数/);
  assert.match(L.validateLabel({ type: "integer" }, true).join("|"), /要是整数/);
  assert.deepEqual(L.validateLabel({ type: "number" }, 1.5), []);
  assert.match(L.validateLabel({ type: "array", items: { type: "string" } }, ["a", 1]).join("|"), /\[1\]：要是字符串/);
  assert.match(L.validateLabel({ const: 3 }, 4).join("|"), /只能是 3/);
  const oneOf = { oneOf: [{ type: "string" }, { type: "boolean" }] };
  assert.deepEqual(L.validateLabel(oneOf, "x"), []);
  assert.match(L.validateLabel(oneOf, 3).join("|"), /oneOf/);
  assert.deepEqual(L.validateLabel({ type: "object", properties: {}, additionalProperties: false }, { constructor: 1 }).length, 1, "an inherited name is not a property");
});

// ---------------------------------------------------------------------------------------------------------------------- the form
test("formModel: an enum is a button group on the keys 1-9, a boolean is 是 / 否 on Y / N, a text is optional", () => {
  const item = L.parseSet(setOf(1)).items[0];
  const form = L.formModel(item, {}, null);
  assert.deepEqual(form.fields.map((f) => [f.name, f.type, f.mustFill]), [["basis", "enum", true], ["cites_decisive", "boolean", true], ["reason", "text", false]]);
  const basis = form.fields[0];
  assert.deepEqual(basis.options.map((o) => [o.key, o.label]), [["1", "decisive_fact"], ["2", "trivia"], ["3", "expression"], ["4", "intuition"], ["5", "unclear"]]);
  assert.match(basis.options[0].hint, /主要依据就是决定性事实/, "the definition from the guide stands next to the button");
  assert.ok(!basis.options[1].hint.includes("**"), "markdown is stripped from a hint");
  assert.deepEqual(form.fields[1].options.map((o) => [o.key, o.label, o.value]), [["Y", "是", true], ["N", "否", false]]);
  assert.equal(form.fields[2].options, null);
  assert.equal(form.fields[0].caption, "主要依据");
  assert.deepEqual(form.missing, ["basis", "cites_decisive"]);
  assert.equal(form.complete, false);
});

test("formModel: required fields - false is an answer, an empty reason is fine, nothing selected is not", () => {
  const item = L.parseSet(setOf(1)).items[0];
  assert.equal(L.formModel(item, { basis: "trivia" }, null).complete, false);
  assert.deepEqual(L.formModel(item, { basis: "trivia" }, null).missing, ["cites_decisive"]);
  assert.equal(L.formModel(item, { basis: "trivia", cites_decisive: false }, null).complete, true, "false is an answer");
  assert.equal(L.formModel(item, { basis: "trivia", cites_decisive: false, reason: "" }, null).complete, true, "the reason may stay empty");
  assert.equal(L.formModel(item, { cites_decisive: true }, null).complete, false);
  const form = L.formModel(item, { basis: "trivia", cites_decisive: false, reason: "  " }, null);
  assert.equal(form.fields[2].answered, false, "blank is not an answer");
});

test("formModel: the active field is the first one not answered, then the last; a focus wins", () => {
  const item = L.parseSet(setOf(1)).items[0];
  assert.equal(L.formModel(item, {}, null).active, "basis");
  assert.equal(L.formModel(item, { basis: "trivia" }, null).active, "cites_decisive");
  assert.equal(L.formModel(item, { basis: "trivia", cites_decisive: true }, null).active, "cites_decisive", "all answered: the last choice stays");
  assert.equal(L.formModel(item, { basis: "trivia" }, "basis").active, "basis", "an explicit focus");
  assert.equal(L.formModel(item, {}, "reason").active, "basis", "a text box is not a choice: the focus falls back");
  assert.deepEqual(L.formModel(item, {}, null).fields.map((f) => f.active), [true, false, false]);
});

test("formModel: S2 offers its card ids; more than nine options keep the first nine keys; a primary text must be filled", () => {
  const card = L.parseSet(jsonl([s2(1)])).items[0];
  const form = L.formModel(card, {}, null);
  assert.deepEqual(form.fields.map((f) => [f.name, f.type]), [["match", "enum"], ["runner_up", "enum"], ["confidence", "enum"], ["cues", "text"]]);
  assert.deepEqual(form.fields[1].options.map((o) => o.label), ["A", "B", "C", "none"]);
  assert.deepEqual(form.missing, ["match", "runner_up", "confidence"]);
  const twelve = { ...s2(2), schema: { ...S2_SCHEMA, properties: { ...S2_SCHEMA.properties, match: { type: "string", enum: "ABCDEFGHIJKL".split("") } } } };
  const options = L.formModel(L.parseSet(jsonl([twelve])).items[0], {}, null).fields[0].options;
  assert.deepEqual(options.map((o) => o.key), ["1", "2", "3", "4", "5", "6", "7", "8", "9", "", "", ""]);
  const plain = { ...s2(3), schema: { ...S2_SCHEMA, properties: { ...S2_SCHEMA.properties, match: { type: "string" } } } };
  const text = L.formModel(L.parseSet(jsonl([plain])).items[0], {}, null);
  assert.equal(text.fields[0].type, "text");
  assert.equal(text.fields[0].mustFill, true, "the primary field has to be filled even when it is a text");
  assert.ok(text.missing.includes("match"));
  assert.equal(L.formModel(L.parseSet(jsonl([plain])).items[0], { match: "C", runner_up: "A", confidence: "low" }, null).complete, true);
});

test("enumHints: the one-line definitions of the guide, cut at the first full stop", () => {
  const hints = L.enumHints(L3_GUIDE);
  assert.deepEqual(Object.keys(hints), ["decisive_fact", "trivia", "expression", "intuition", "unclear"]);
  assert.equal(hints.decisive_fact, "主要依据就是决定性事实，或从公开记录里直接推出的同类矛盾");
  assert.ok(!hints.trivia.includes("`") && !hints.trivia.includes("**"));
  assert.deepEqual(Object.keys(L.enumHints("没有列表\n- 没有反引号：不算")), []);
  assert.ok(L.enumHints("- `x`：" + "字".repeat(300)).x.length <= 81);
  assert.equal(L.enumHints("- `constructor`：x").constructor, "x", "a name from Object.prototype is just a name");
});

test("finalLabel: the schema's fields in the schema's order; a required text left empty is the empty string", () => {
  const item = L.parseSet(setOf(1)).items[0];
  assert.deepEqual(L.finalLabel(item, { reason: " 因为…  ", cites_decisive: false, basis: "intuition" }), { basis: "intuition", cites_decisive: false, reason: "因为…" });
  assert.deepEqual(Object.keys(L.finalLabel(item, { cites_decisive: false, basis: "intuition" })), ["basis", "cites_decisive", "reason"]);
  assert.equal(L.finalLabel(item, { basis: "unclear", cites_decisive: true }).reason, "");
  const optional = { ...l3(2), schema: { ...L3_SCHEMA, required: ["basis", "cites_decisive"] } };
  assert.deepEqual(Object.keys(L.finalLabel(L.parseSet(jsonl([optional])).items[0], { basis: "unclear", cites_decisive: true })), ["basis", "cites_decisive"], "an optional text is left out");
});

// ---------------------------------------------------------------------------------------------------------------------- export
test("exportRows: a row for every finished item, in the documented format - and only those", () => {
  const items = L.parseSet(jsonl([l3(1), s2(2), e2(3), l3(4)])).items;
  const answers = {
    [blindId(1)]: { fields: { basis: "trivia", cites_decisive: false, reason: "细节" }, ts: "2026-11-20T10:00:01.000Z" },
    [blindId(2)]: { fields: { match: "C", runner_up: "none", confidence: "low" }, ts: "2026-11-20T10:00:02.000Z" },
    [blindId(3)]: { fields: { closer: "Y" }, ts: "2026-11-20T10:00:03.000Z" }, // (confidence is missing: not finished)
    // item 4: never touched
  };
  const result = L.exportRows(items, answers, { labeler: "  小明  " });
  assert.deepEqual(result.rejected, []);
  assert.deepEqual(result.rows.map((row) => row.blind_id), [blindId(1), blindId(2)]);
  assert.deepEqual(Object.keys(result.rows[0]), ["format", "version", "blind_id", "rubric", "rubric_version", "label", "labeler", "ts"]);
  assert.deepEqual(result.rows[0], {
    format: "arena-bench-label", version: 1, blind_id: blindId(1), rubric: "L3", rubric_version: 1,
    label: { basis: "trivia", cites_decisive: false, reason: "细节" }, labeler: "小明", ts: "2026-11-20T10:00:01.000Z",
  });
  assert.deepEqual(result.rows[1].label, { match: "C", runner_up: "none", confidence: "low", cues: "" }, "the schema asks for cues: the empty string");
  const lines = result.text.split("\n");
  assert.equal(lines.length, 3);
  assert.equal(lines[2], "");
  assert.deepEqual(JSON.parse(lines[0]), result.rows[0]);
  assert.equal(L.exportRows(items, {}, {}).text, "", "nothing finished: an empty file");
  assert.equal(L.exportRows(items, answers, {}).rows[0].labeler, "user", "the default labeler");
});

test("exportRows: a label that does not fit its schema is not exported - it is reported", () => {
  const items = L.parseSet(setOf(2)).items;
  const answers = {
    [blindId(1)]: { fields: { basis: "vibes", cites_decisive: true }, ts: "t" }, // not one of the options
    [blindId(2)]: { fields: { basis: "trivia", cites_decisive: true }, ts: "t" },
  };
  const result = L.exportRows(items, answers, {});
  assert.deepEqual(result.rows.map((row) => row.blind_id), [blindId(2)]);
  assert.equal(result.rejected.length, 1);
  assert.equal(result.rejected[0].blind_id, blindId(1));
  assert.match(result.rejected[0].problems.join("|"), /basis/);
});

test("cleanLabeler: trimmed, one line, at most 40 characters, never empty", () => {
  assert.equal(L.cleanLabeler(""), "user");
  assert.equal(L.cleanLabeler("   "), "user");
  assert.equal(L.cleanLabeler(undefined), "user");
  assert.equal(L.cleanLabeler("  a\n\tb  "), "a b");
  assert.equal(L.cleanLabeler("字".repeat(60)).length, 40);
});

// ---------------------------------------------------------------------------------------------------------------------- the keyboard
test("keyAction: the keys of the page", () => {
  const key = (k, extra) => L.keyAction({ key: k, targetTag: "BODY", ...extra });
  assert.deepEqual(key("ArrowLeft"), { type: "prev" });
  assert.deepEqual(key("ArrowRight"), { type: "next" });
  assert.deepEqual(key("Enter"), { type: "saveNext" });
  assert.deepEqual(key("s"), { type: "skip" });
  assert.deepEqual(key("S"), { type: "skip" });
  assert.deepEqual(key("y"), { type: "chooseKey", kind: "boolean", value: true });
  assert.deepEqual(key("Y"), { type: "chooseKey", kind: "boolean", value: true });
  assert.deepEqual(key("n"), { type: "chooseKey", kind: "boolean", value: false });
  assert.deepEqual(key("N"), { type: "chooseKey", kind: "boolean", value: false });
  for (let d = 1; d <= 9; d++) assert.deepEqual(key(String(d)), { type: "chooseKey", kind: "enum", number: d });
  for (const other of ["0", "a", "x", "Tab", "Escape", "ArrowUp", " ", "F5", "10"]) assert.equal(key(other), null, other);
  assert.equal(L.keyAction(null), null);
  assert.equal(L.keyAction({}), null);
});

test("keyAction: the browser's shortcuts, a held key and the controls that own the key are left alone", () => {
  const base = { targetTag: "BODY" };
  for (const mod of ["ctrlKey", "metaKey", "altKey"]) {
    assert.equal(L.keyAction({ ...base, key: "ArrowLeft", [mod]: true }), null, mod);
    assert.equal(L.keyAction({ ...base, key: "s", [mod]: true }), null, mod);
  }
  assert.equal(L.keyAction({ ...base, key: "Enter", repeat: true }), null, "a held key is taken once");
  assert.equal(L.keyAction({ key: "ArrowRight", targetTag: "SELECT" }), null, "a <select> moves with the arrows itself");
  assert.equal(L.keyAction({ key: "Enter", targetTag: "BUTTON" }), null, "Enter on a focused button presses it");
  assert.equal(L.keyAction({ key: "Enter", targetTag: "SUMMARY" }), null);
  assert.deepEqual(L.keyAction({ key: "ArrowRight", targetTag: "BUTTON" }), { type: "next" }, "the arrows still work with a button focused");
});

test("keyAction: the Enter that confirms an input-method candidate is the input method's, not 'save and next'", () => {
  for (const key of ["Enter", "Escape", "1", "ArrowRight", "y"]) {
    assert.equal(L.keyAction({ key, targetTag: "INPUT", inField: true, composing: true }), null, `${key} while composing`);
    assert.equal(L.keyAction({ key, targetTag: "BODY", composing: true }), null, `${key} while composing, outside a box`);
  }
  assert.deepEqual(L.keyAction({ key: "Enter", targetTag: "INPUT", inField: true, composing: false }), { type: "saveNext" }, "and when it is over, Enter saves again");
});

test("keyAction: typing in a text box only knows Enter and Escape", () => {
  for (const tag of ["INPUT", "TEXTAREA", "input"]) {
    for (const k of ["1", "y", "n", "s", "ArrowLeft", "ArrowRight", "a"]) assert.equal(L.keyAction({ key: k, targetTag: tag, inField: true }), null, `${tag} ${k}`);
  }
  assert.deepEqual(L.keyAction({ key: "Enter", targetTag: "INPUT", inField: true }), { type: "saveNext" }, "Enter in the reason box saves and goes on");
  assert.deepEqual(L.keyAction({ key: "Enter", targetTag: "INPUT", inField: false }), { type: "blur" }, "Enter in the name box only leaves it");
  assert.deepEqual(L.keyAction({ key: "Escape", targetTag: "INPUT", inField: true }), { type: "blur" });
});

// ---------------------------------------------------------------------------------------------------------------------- the controller
test("controller: opening a set shows the first item, nothing is done, nothing is written", () => {
  const storage = memory();
  const controller = controllerOf(setOf(3), storage);
  const view = controller.view();
  assert.equal(view.loaded, true);
  assert.deepEqual([view.total, view.done, view.skipped, view.percent, view.exportCount], [3, 0, 0, 0, 0]);
  assert.deepEqual([view.current.number, view.current.rubric, view.current.status, view.current.blindId], [1, "L3", "open", blindId(1)]);
  assert.equal(view.nav.canPrev, false);
  assert.equal(view.jump.length, 3);
  assert.equal(storage.data.size, 0, "opening a file leaves no trace in the browser");
  assert.equal(L.createController({ storage: memory() }).view().loaded, false);
});

test("controller: the keys choose, the active field moves on, Enter saves and goes to the next item", () => {
  const controller = controllerOf(setOf(3));
  controller.dispatch({ type: "chooseKey", kind: "enum", number: 3 });
  let form = controller.view().current.form;
  assert.deepEqual(form.fields.map((f) => f.value), ["expression", undefined, undefined]);
  assert.equal(form.active, "cites_decisive", "after an answer the next question is highlighted");
  assert.equal(form.complete, false);
  controller.dispatch({ type: "chooseKey", kind: "boolean", value: false });
  form = controller.view().current.form;
  assert.equal(form.complete, true);
  assert.equal(controller.view().done, 1, "an item counts as soon as its required fields are answered");
  const effects = controller.dispatch({ type: "saveNext" });
  assert.equal(effects.rerender, true);
  assert.equal(effects.scrollTop, true, "a new item: back to the top");
  const view = controller.view();
  assert.equal(view.current.number, 2);
  assert.equal(view.notice.kind, "ok");
  assert.match(view.notice.text, /已保存.*已标 1 \/ 共 3/);
  assert.equal(view.jump[0].status, "done");
});

test("controller: a click on an option picks it (also by index), a click on the selected one keeps it", () => {
  const controller = controllerOf(setOf(1));
  controller.dispatch({ type: "choose", field: "basis", option: "1" });
  assert.equal(controller.state.answers[blindId(1)].fields.basis, "trivia");
  controller.dispatch({ type: "choose", field: "basis", option: 1 });
  assert.equal(controller.state.answers[blindId(1)].fields.basis, "trivia");
  controller.dispatch({ type: "choose", field: "cites_decisive", option: "1" });
  assert.equal(controller.state.answers[blindId(1)].fields.cites_decisive, false, "option 1 of a boolean is 否");
  controller.dispatch({ type: "choose", field: "basis", option: "9" });
  controller.dispatch({ type: "choose", field: "nothing", option: "0" });
  controller.dispatch({ type: "choose", field: "reason", option: "0" });
  assert.deepEqual(controller.state.answers[blindId(1)].fields, { basis: "trivia", cites_decisive: false }, "an option that does not exist changes nothing");
});

test("controller: the keys change the answer of an answered form too (the active field is the last one touched)", () => {
  const controller = controllerOf(setOf(1));
  labelCurrent(controller, 2);
  controller.dispatch({ type: "chooseKey", kind: "enum", number: 5 });
  assert.equal(controller.state.answers[blindId(1)].fields.basis, "unclear", "with everything answered a digit changes the (only) enum field");
  controller.dispatch({ type: "chooseKey", kind: "boolean", value: false });
  assert.equal(controller.state.answers[blindId(1)].fields.cites_decisive, false);
});

test("controller: more than one enum field - the digits go to the highlighted one, a click on a field moves the highlight", () => {
  const controller = controllerOf(jsonl([s2(1)]));
  controller.dispatch({ type: "chooseKey", kind: "enum", number: 3 });
  controller.dispatch({ type: "chooseKey", kind: "enum", number: 1 });
  controller.dispatch({ type: "chooseKey", kind: "enum", number: 2 });
  assert.deepEqual(controller.state.answers[blindId(1)].fields, { match: "C", runner_up: "A", confidence: "medium" });
  assert.equal(controller.view().current.form.complete, true);
  controller.dispatch({ type: "focusField", field: "match" });
  assert.equal(controller.view().current.form.active, "match");
  controller.dispatch({ type: "chooseKey", kind: "enum", number: 2 });
  assert.equal(controller.state.answers[blindId(1)].fields.match, "B");
  assert.equal(controller.view().current.form.active, "match", "everything is answered: the highlight stays");
});

test("controller: a key that has no target says so and changes nothing", () => {
  const controller = controllerOf(jsonl([e2(1)]));
  controller.dispatch({ type: "chooseKey", kind: "boolean", value: true });
  assert.match(controller.view().notice.text, /没有是 \/ 否的题/);
  controller.dispatch({ type: "chooseKey", kind: "enum", number: 7 });
  assert.match(controller.view().notice.text, /只有 3 个选项，没有第 7 项/);
  assert.equal(controller.state.answers[blindId(1)], undefined, "nothing was answered");
  const plain = controllerOf(setOf(1));
  plain.dispatch({ type: "chooseKey", kind: "enum", number: 2 });
  plain.dispatch({ type: "chooseKey", kind: "enum", number: 9 });
  assert.equal(plain.state.answers[blindId(1)].fields.basis, "trivia");
});

test("controller: Enter and → do not go on while a required field is missing - they say what is missing", () => {
  const controller = controllerOf(setOf(3));
  controller.dispatch({ type: "saveNext" });
  assert.equal(controller.view().current.number, 1);
  assert.equal(controller.view().notice.kind, "warn");
  assert.match(controller.view().notice.text, /还差：主要依据、有没有明确提到决定性事实/);
  assert.match(controller.view().notice.text, /按 S 跳过/);
  assert.equal(controller.view().current.form.active, "basis");
  controller.dispatch({ type: "chooseKey", kind: "enum", number: 1 });
  controller.dispatch({ type: "next" });
  assert.equal(controller.view().current.number, 1, "→ is gated the same way");
  assert.match(controller.view().notice.text, /还差：有没有明确提到决定性事实/);
  controller.dispatch({ type: "chooseKey", kind: "boolean", value: true });
  controller.dispatch({ type: "next" });
  assert.equal(controller.view().current.number, 2, "complete: → saves and goes on");
});

test("controller: → moves freely over items that are done or skipped; ← always goes back", () => {
  const controller = controllerOf(setOf(3));
  labelCurrent(controller);
  controller.dispatch({ type: "saveNext" });
  controller.dispatch({ type: "skip" });
  assert.equal(controller.view().current.number, 3);
  controller.dispatch({ type: "prev" });
  controller.dispatch({ type: "prev" });
  assert.equal(controller.view().current.number, 1);
  controller.dispatch({ type: "next" });
  assert.equal(controller.view().current.number, 2, "a done item: → just goes on");
  controller.dispatch({ type: "next" });
  assert.equal(controller.view().current.number, 3, "a skipped item: → just goes on");
  controller.dispatch({ type: "next" });
  assert.equal(controller.view().current.number, 3, "item 3 is open and empty: → is gated");
  controller.dispatch({ type: "prev" });
  controller.dispatch({ type: "prev" });
  controller.dispatch({ type: "prev" });
  assert.equal(controller.view().current.number, 1);
  assert.match(controller.view().notice.text, /第一条/);
});

test("controller: S skips - the item is listed as skipped, comes back as the next one without a label, and a done item is not un-done by S", () => {
  const controller = controllerOf(setOf(3));
  controller.dispatch({ type: "skip" });
  let view = controller.view();
  assert.equal(view.current.number, 2);
  assert.deepEqual([view.done, view.skipped], [0, 1]);
  assert.equal(view.jump[0].status, "skipped");
  assert.match(view.jump[0].text, /1 · L3 · ↷ 跳过了/);
  labelCurrent(controller);
  controller.dispatch({ type: "prev" });
  controller.dispatch({ type: "next" });
  controller.dispatch({ type: "skip" }); // item 2 is done: S does not mark it
  view = controller.view();
  assert.equal(view.jump[1].status, "done");
  assert.equal(view.current.number, 3);
  controller.dispatch({ type: "skip" });
  assert.match(controller.view().notice.text, /最后一条/);
  assert.equal(controller.view().jump[2].status, "skipped");
  controller.dispatch({ type: "nextOpen" });
  assert.equal(controller.view().current.number, 1, "the skipped ones are found again");
  labelCurrent(controller, 1);
  assert.equal(controller.view().jump[0].status, "done", "labelling a skipped item makes it done");
  assert.equal(controller.state.skipped[blindId(1)], undefined);
});

test("controller: 下一条没标的 goes round to the next item without a label - open ones before skipped ones - and says when everything is done", () => {
  const controller = controllerOf(setOf(4));
  labelCurrent(controller);
  controller.dispatch({ type: "saveNext" }); // 1 done, now at 2
  controller.dispatch({ type: "skip" }); // 2 skipped, now at 3
  controller.dispatch({ type: "nextOpen" });
  assert.equal(controller.view().current.number, 4, "item 4 is open: it comes before the skipped item 2");
  controller.dispatch({ type: "nextOpen" });
  assert.equal(controller.view().current.number, 3, "round the end: item 3 is open");
  labelCurrent(controller);
  controller.dispatch({ type: "jump", index: 3 });
  labelCurrent(controller);
  controller.dispatch({ type: "nextOpen" });
  assert.equal(controller.view().current.number, 2, "only the skipped one is left");
  labelCurrent(controller);
  controller.dispatch({ type: "nextOpen" });
  assert.equal(controller.view().notice.kind, "ok");
  assert.match(controller.view().notice.text, /每一条都标完了/);
});

test("controller: the last item - saved, with a word about what is left", () => {
  const controller = controllerOf(setOf(2));
  controller.dispatch({ type: "jump", index: 1 });
  labelCurrent(controller);
  controller.dispatch({ type: "saveNext" });
  assert.equal(controller.view().current.number, 2);
  assert.match(controller.view().notice.text, /这是最后一条，已保存；还有 1 条没标/);
  controller.dispatch({ type: "jump", index: 0 });
  labelCurrent(controller);
  controller.dispatch({ type: "jump", index: 1 });
  controller.dispatch({ type: "saveNext" });
  assert.match(controller.view().notice.text, /全部标完了：点右上角的“导出 labels\.jsonl”/);
  assert.equal(controller.view().percent, 100);
  controller.dispatch({ type: "jump", index: 99 });
  assert.equal(controller.view().current.number, 2, "a jump is clamped to the set");
  controller.dispatch({ type: "jump", index: -4 });
  assert.equal(controller.view().current.number, 1);
});

test("controller: the reason box - typed text is kept without a redraw (the cursor must stay), empty is fine", () => {
  const storage = memory();
  const controller = controllerOf(setOf(1), storage);
  const effects = controller.dispatch({ type: "text", field: "reason", value: "理由：公告对不上" });
  assert.equal(effects.rerender, false);
  assert.equal(controller.state.answers[blindId(1)].fields.reason, "理由：公告对不上");
  assert.equal(controller.view().done, 0, "a reason alone does not finish an item");
  labelCurrent(controller);
  assert.equal(controller.view().done, 1);
  assert.equal(L.exportRows(controller.state.items, controller.state.answers, {}).rows[0].label.reason, "理由：公告对不上");
  controller.dispatch({ type: "text", field: "reason", value: "" });
  assert.equal(controller.view().done, 1, "an empty reason is fine");
  assert.equal(controller.dispatch({ type: "text", field: "basis", value: "x" }).rerender, false);
  assert.equal(controller.state.answers[blindId(1)].fields.basis, "trivia", "a text cannot answer a choice");
  controller.dispatch({ type: "text", field: "reason", value: "字".repeat(900) });
  assert.equal(controller.state.answers[blindId(1)].fields.reason.length, 500);
});

// ---------------------------------------------------------------------------------------------------------------------- localStorage
test("localStorage: every change is saved, and a new page opens the same file where it was left", () => {
  const storage = memory();
  const first = controllerOf(setOf(4), storage);
  labelCurrent(first, 1);
  first.dispatch({ type: "saveNext" });
  first.dispatch({ type: "skip" });
  first.dispatch({ type: "chooseKey", kind: "enum", number: 4 });
  first.dispatch({ type: "text", field: "reason", value: "半截" });
  const key = L.storageKey(L.fingerprintOf(first.state.items));
  assert.deepEqual([...storage.data.keys()].filter((k) => k.startsWith(L.STORAGE_PREFIX)), [key], "one key, with the fingerprint of the file in it");
  const saved = JSON.parse(storage.data.get(key));
  assert.equal(saved.version, 1);
  assert.equal(saved.cursor, 2);

  const second = L.createController({ storage, now: clock() });
  const result = second.load(setOf(4), "set.jsonl");
  assert.equal(result.restored, 1);
  const view = second.view();
  assert.equal(view.current.number, 3, "the item the person was on");
  assert.deepEqual([view.done, view.skipped], [1, 1]);
  assert.equal(view.notice.kind, "info");
  assert.match(view.notice.text, /已恢复上次的进度：已标 1 \/ 共 4/);
  assert.equal(second.state.answers[blindId(3)].fields.basis, "intuition", "a half answer is back too");
  assert.equal(second.state.answers[blindId(3)].fields.reason, "半截");
  assert.equal(second.state.answers[blindId(1)].ts, first.state.answers[blindId(1)].ts, "the time of an answer is kept");
  assert.equal(second.exportCount, undefined);
  assert.equal(L.exportRows(second.state.items, second.state.answers, {}).rows.length, 1);
});

test("localStorage: another file never mixes with this one - not even the same items under another rubric version", () => {
  const storage = memory();
  const a = controllerOf(setOf(3), storage);
  labelCurrent(a, 1);
  const b = L.createController({ storage, now: clock() });
  b.load(jsonl([l3(1), l3(2), l3(3, { rubric_version: 2 })]), "other.jsonl");
  assert.equal(b.view().done, 0, "the first rows are the same, the file is not");
  assert.equal(b.view().notice, null);
  labelCurrent(b, 3);
  const c = L.createController({ storage, now: clock() });
  c.load(jsonl([l3(11), l3(12)]), "third.jsonl");
  assert.equal(c.view().done, 0);
  const keys = [...storage.data.keys()].filter((k) => k.startsWith(L.STORAGE_PREFIX));
  assert.equal(keys.length, 2, "two files with progress, two keys");
  const again = L.createController({ storage, now: clock() });
  again.load(setOf(3), "set.jsonl");
  assert.equal(again.state.answers[blindId(1)].fields.basis, "decisive_fact", "the first file still has its own answers");
  const back = L.createController({ storage, now: clock() });
  back.load(jsonl([l3(1), l3(2), l3(3, { rubric_version: 2 })]), "other.jsonl");
  assert.equal(back.state.answers[blindId(1)].fields.basis, "expression", "and the second its own");
});

test("localStorage: what cannot be trusted is dropped - a corrupt entry, an item that is gone, a value that is no option", () => {
  const storage = memory();
  const items = L.parseSet(setOf(3)).items;
  const key = L.storageKey(L.fingerprintOf(items));
  storage.setItem(key, "{not json");
  const corrupt = L.createController({ storage });
  corrupt.load(setOf(3), "set.jsonl");
  assert.equal(corrupt.view().done, 0);
  assert.equal(corrupt.view().notice.kind, "warn");
  assert.match(corrupt.view().notice.text, /上次的进度读不出来/);

  storage.setItem(key, JSON.stringify({
    version: 1, cursor: 99,
    answers: {
      [blindId(1)]: { fields: { basis: "trivia", cites_decisive: true, reason: "ok", extra: 1 }, ts: "t1" },
      [blindId(2)]: { fields: { basis: "vibes", cites_decisive: "yes" }, ts: "t2" },
      ghost: { fields: { basis: "trivia", cites_decisive: true }, ts: "t3" },
      [blindId(3)]: "nonsense",
    },
    skipped: { [blindId(2)]: true, ghost: true, [blindId(3)]: "yes" },
  }));
  const messy = L.createController({ storage });
  messy.load(setOf(3), "set.jsonl");
  assert.deepEqual(Object.keys(messy.state.answers).sort(), [blindId(1), blindId(2)]);
  assert.deepEqual(messy.state.answers[blindId(1)].fields, { basis: "trivia", cites_decisive: true, reason: "ok" });
  assert.deepEqual(messy.state.answers[blindId(2)].fields, {}, "values that are no option are dropped");
  assert.deepEqual(messy.state.skipped, { [blindId(2)]: true });
  assert.equal(messy.state.cursor, 0, "a cursor outside the set starts at the top");
  assert.equal(messy.view().done, 1);
});

test("localStorage: a browser that refuses to store says so - and the page keeps working", () => {
  const storage = memory();
  storage.setItem = () => {
    throw new Error("QuotaExceededError");
  };
  const controller = controllerOf(setOf(2), storage);
  assert.equal(controller.view().storageOk, true, "nothing was tried yet");
  labelCurrent(controller);
  assert.equal(controller.view().storageOk, false);
  assert.equal(controller.view().notice.kind, "warn");
  assert.match(controller.view().notice.text, /本地存储写不进去/);
  assert.equal(controller.view().done, 1, "the answers are in memory");
  assert.equal(controller.dispatch({ type: "export" }).download.count, 1, "and can still be exported");
  assert.match(L.renderHtml(controller.view()), /进度没有保存到浏览器里/);
  const none = controllerOf(setOf(1), null);
  assert.equal(none.view().storageOk, false, "no localStorage at all");
  labelCurrent(none);
  assert.equal(none.view().done, 1);
});

test("localStorage: 清除本地进度 removes the progress of THIS file only", () => {
  const storage = memory();
  const a = controllerOf(setOf(3), storage);
  labelCurrent(a);
  const other = L.createController({ storage });
  other.load(jsonl([l3(21), l3(22)]), "other.jsonl");
  labelCurrent(other);
  assert.equal([...storage.data.keys()].filter((k) => k.startsWith(L.STORAGE_PREFIX)).length, 2);
  assert.match(a.confirmClearText, /清除这个文件的全部本地标注进度/);
  a.dispatch({ type: "clear" });
  assert.equal(a.view().done, 0);
  assert.equal(a.view().current.number, 1);
  assert.equal(a.view().notice.kind, "ok");
  assert.deepEqual([...storage.data.keys()].filter((k) => k.startsWith(L.STORAGE_PREFIX)), [L.storageKey(L.fingerprintOf(other.state.items))]);
  const reopened = L.createController({ storage });
  reopened.load(setOf(3), "set.jsonl");
  assert.equal(reopened.view().done, 0, "gone after a reload too");
  assert.equal(a.dispatch({ type: "export" }).download, undefined);
  assert.match(a.view().notice.text, /还没有可以导出的标注/);
});

test("preferences: the name of the labeler, the theme and the guide are kept for the next page", () => {
  const storage = memory();
  const controller = controllerOf(setOf(1), storage);
  assert.deepEqual([controller.view().labeler, controller.view().theme, controller.view().guideOpen], ["user", "auto", false]);
  assert.equal(controller.dispatch({ type: "labeler", value: "  小红 " }).rerender, false);
  assert.equal(controller.view().labeler, "小红");
  controller.dispatch({ type: "labeler", value: "" });
  assert.equal(controller.view().labeler, "user", "an empty name is the default");
  controller.dispatch({ type: "labeler", value: "小红" });
  assert.deepEqual([1, 2, 3, 4].map(() => controller.dispatch({ type: "theme" }) && controller.view().theme), ["light", "dark", "auto", "light"]);
  assert.equal(controller.dispatch({ type: "guide", open: true }).rerender, false);
  const next = L.createController({ storage });
  assert.deepEqual([next.view().labeler, next.view().theme, next.view().guideOpen], ["小红", "light", true]);
  assert.equal(next.view().themeLabel, "浅色");
  storage.setItem(L.PREFS_KEY, JSON.stringify({ theme: "neon", labeler: 5 }));
  assert.deepEqual([L.createController({ storage }).view().theme, L.createController({ storage }).view().labeler], ["auto", "user"]);
});

test("export: the button gives labels.jsonl with the finished items and the name of the labeler", () => {
  const controller = controllerOf(setOf(3));
  controller.dispatch({ type: "labeler", value: "小红" });
  assert.equal(controller.dispatch({ type: "export" }).download, undefined);
  assert.equal(controller.view().notice.kind, "warn");
  labelCurrent(controller, 4);
  controller.dispatch({ type: "saveNext" });
  controller.dispatch({ type: "chooseKey", kind: "enum", number: 1 }); // (item 2 stays unfinished)
  const effects = controller.dispatch({ type: "export" });
  assert.deepEqual([effects.download.name, effects.download.count], ["labels.jsonl", 1]);
  const rows = effects.download.text.trim().split("\n").map((line) => JSON.parse(line));
  assert.equal(rows.length, 1);
  assert.deepEqual(rows[0].label, { basis: "intuition", cites_decisive: true, reason: "" });
  assert.equal(rows[0].labeler, "小红");
  assert.match(rows[0].ts, /^2026-11-20T10:00:\d\d\.000Z$/);
  assert.equal(controller.view().notice.kind, "ok");
  assert.match(controller.view().notice.text, /已导出 1 条（还有 2 条没标完或跳过了，没有导出）/);
});

// ---------------------------------------------------------------------------------------------------------------------- what is shown
test("display: an S2 item shows the speech and the style cards - and no kind, no tool, no id", () => {
  const model = L.displayModel(L.parseSet(jsonl([s2(1)])).items[0]);
  assert.deepEqual(model.blocks.map((b) => [b.zone, b.key, b.type]), [["subject", "text", "text"], ["context", "cards", "cards"]]);
  assert.equal(model.blocks[0].label, "发言");
  assert.equal(model.blocks[0].text, "你急什么？这票你自己信吗？");
  assert.deepEqual(model.blocks[1].cards.map((c) => c.id), ["A", "B", "C"]);
  assert.match(model.blocks[1].cards[1].text, /咱们/);
  assert.deepEqual([model.title, model.brief], ["第 2 天白天发言", "没有新的死讯。"]);
  assert.deepEqual(model.meta, [{ label: "类型", value: "speech" }]);
});

test("display: an E2 item shows the reference speeches and the two candidates side by side", () => {
  const model = L.displayModel(L.parseSet(jsonl([e2(1)])).items[0]);
  assert.deepEqual(model.blocks.map((b) => [b.key, b.type, b.side || ""]), [["situation", "text", ""], ["reference", "list", ""], ["x", "text", "X"], ["y", "text", "Y"]]);
  assert.equal(model.blocks[1].label, "这位真人的参考发言");
  assert.deepEqual(model.blocks[1].items, ["我昨天就说了，N5 不对。", "又在绕。投谁，说一句。"]);
  assert.deepEqual(model.blocks.slice(2).map((b) => b.label), ["候选 X", "候选 Y"]);
  const html = L.renderHtml(controllerOf(jsonl([e2(1)])).view());
  assert.match(html, /<div class="pair"><section class="block subject side" data-key="x">.*<section class="block subject side" data-key="y">/);
  assert.ok(html.includes("分三点说明") && html.includes("N5 还是不对"));
});

test("display: an L3 item shows the facts before the decision and the reason of the bot", () => {
  const model = L.displayModel(L.parseSet(setOf(1)).items[0]);
  assert.deepEqual(model.blocks.map((b) => b.key), ["decisive", "trap", "value", "reason"]);
  assert.deepEqual(model.blocks.map((b) => b.label), ["决定性事实", "容易被带偏的地方", "选择", "理由"]);
  assert.deepEqual(model.meta, [{ label: "类型", value: "choice" }, { label: "工具", value: "respond" }]);
  assert.ok(!model.blocks.some((b) => b.key === "thought"), "an empty thought is not shown");
});

test("display: whatever the file holds, a bot, a run, the truth or a verdict is never shown", () => {
  const leaky = l3(1);
  leaky.item_id = "20261120-aaaa/sit-1/N5/bot:Alice@secret/0/L3";
  leaky.bot = "bot:Alice@secret";
  leaky.truth = { answer: ["N4"] };
  leaky.view.bot = "bot:Alice@secret";
  leaky.view.context = { ...leaky.view.context, bot: "bot:Alice@secret", truth: { answer: ["N4"], wrong: ["N9"] }, run: "20261120-aaaa", nested: { sample: 3, brain: "BotBrain", fine: "可以显示" } };
  leaky.view.subject = { ...leaky.view.subject, attempts: [{ secret: "bot:Alice@secret" }], bot: "bot:Alice@secret", verdict: { basis: "trivia" } };
  const controller = controllerOf(jsonl([leaky]));
  const html = L.renderHtml(controller.view());
  const everything = html + JSON.stringify(controller.view());
  for (const secret of ["Alice", "secret", "20261120-aaaa", "BotBrain", "N9", '"truth"', '"answer"', '"attempts"', '"item_id"', '"verdict"', '"bot"', '"run"', "sit-1"]) {
    assert.ok(!everything.includes(secret), secret);
  }
  assert.ok(html.includes("可以显示"), "the rest of a nested value is shown");
  for (const key of ["item_id", "bot", "bot_label", "brain", "truth", "run", "sample", "attempts", "verdict", "judge"]) assert.ok(L.DENY_KEYS.has(key), key);
  assert.ok(!L.DENY_KEYS.has("situation"), "an E2 item describes its scene under that name");
});

test("display: everything dynamic is escaped - a speech cannot put markup in the page", () => {
  const attack = l3(1);
  attack.view.subject.reason = "<script>alert('x')</script><img src=x onerror=alert(1)> & \"quote\"";
  attack.view.title = "<b>标题</b>";
  attack.title = "<i>rubric</i>";
  attack.guide = "- `<script>boom()</script>`：说明 <u>下划线</u>\n\n**<img src=y onerror=z()>**";
  const controller = controllerOf(jsonl([attack]));
  controller.dispatch({ type: "labeler", value: "<script>名字</script>" });
  controller.state.fileName = "<script>f</script>.jsonl";
  const html = L.renderHtml(controller.view());
  assert.ok(!/<script/i.test(html) && !/<img/i.test(html) && !/<b>|<i>|<u>/i.test(html), "no markup of the content survives");
  assert.ok(html.includes("&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;"));
  assert.ok(html.includes("&amp; &quot;quote&quot;"));
  assert.ok(html.includes("&lt;b&gt;标题&lt;/b&gt;"));
  assert.ok(html.includes("&lt;script&gt;名字&lt;/script&gt;"), "the name of the labeler in its input");
  assert.equal(L.escapeHtml(undefined), "");
  assert.equal(L.escapeHtml(0), "0");
});

test("guideHtml: headings, bullets, bold and code - after everything was escaped", () => {
  const html = L.guideHtml("## 怎么判\n\n- `a`：**重点** <b>x</b>\n- 第二条\n\n一段话 & `code`\n# 大标题");
  assert.equal(html, "<h4>怎么判</h4><ul><li><code>a</code>：<strong>重点</strong> &lt;b&gt;x&lt;/b&gt;</li><li>第二条</li></ul><p>一段话 &amp; <code>code</code></p><h4>大标题</h4>");
  assert.equal(L.guideHtml(""), "");
  assert.equal(L.guideHtml(undefined), "");
});

test("renderHtml: the welcome page, then an item with its form, progress, jump list and the guide", () => {
  const welcome = L.renderHtml(L.createController({ storage: memory() }).view());
  assert.match(welcome, /把校准集拖到这里/);
  assert.match(welcome, /python -m bench calibration-set/);
  assert.match(welcome, /python -m bench calibrate/);
  assert.match(welcome, /data-action="export" disabled/, "nothing to export yet");
  assert.match(welcome, /<label class="btn" for="file-input">选择 set\.jsonl<\/label>/);

  const controller = controllerOf(setOf(3));
  labelCurrent(controller, 2);
  controller.dispatch({ type: "saveNext" });
  const html = L.renderHtml(controller.view());
  assert.match(html, /已标 1 \/ 共 3/);
  assert.match(html, /<span class="tally">L3 1\/3<\/span>/);
  assert.match(html, /role="progressbar" aria-valuemin="0" aria-valuemax="3" aria-valuenow="1"><div class="progress-fill" style="width:33%">/);
  assert.match(html, /第 2 \/ 3 条 · ○ 没标/);
  assert.match(html, /<button type="button" class="opt" data-action="choose" data-field="basis" data-option="0" aria-pressed="false"><span class="key">1<\/span>/);
  assert.match(html, /data-action="choose" data-field="cites_decisive" data-option="0"[^>]*><span class="key">Y<\/span><span class="opt-text"><span class="opt-main">是<\/span>/);
  assert.match(html, /<input type="text" class="text-input" data-text="reason" value=""/);
  assert.equal((html.match(/<option /g) || []).length, 3);
  assert.match(html, /<option value="1" selected>2 · L3 · ○ 没标<\/option>/);
  assert.match(html, /<option value="0">1 · L3 · ✔ 已标<\/option>/);
  assert.match(html, /data-action="saveNext">保存并下一条（Enter）/);
  assert.match(html, /<details class="guide" data-guide>/);
  assert.match(html, /<h4>怎么判<\/h4><ul><li><code>decisive_fact<\/code>/);
  assert.match(html, /还差 2 项必填/);
  assert.match(html, /<kbd>Y \/ N<\/kbd>/);
  const picked = L.renderHtml(controller.view()).includes('aria-pressed="true"');
  assert.equal(picked, false, "item 2 has no answer yet");
  controller.dispatch({ type: "prev" });
  const back = L.renderHtml(controller.view());
  assert.match(back, /class="opt selected" data-action="choose" data-field="basis" data-option="1" aria-pressed="true"/);
  assert.match(back, /✔ 必填的都选好了/);
  assert.match(back, /<span class="tick">✔<\/span>/);
  assert.match(back, /第 1 \/ 3 条 · ✔ 已标/);
  assert.match(back, /data-action="prev" disabled/);
});

test("every button the page draws names an action the controller handles - and the events the page listens to name nothing else", () => {
  const controller = controllerOf(setOf(3));
  labelCurrent(controller);
  const pages = [L.renderHtml(L.createController({ storage: null }).view()), L.renderHtml(controller.view()), L.renderHtml(controllerOf(jsonl([s2(1), e2(2)])).view())];
  const named = new Set(pages.flatMap((html) => [...html.matchAll(/data-action="([^"]+)"/g)].map((m) => m[1])));
  assert.deepEqual([...named].sort(), ["choose", "clear", "export", "focusField", "jump", "nextOpen", "prev", "saveNext", "skip", "theme"]);
  for (const name of named) {
    if (name === "jump") continue; // (a <select>: the page sends {type: "jump", index} when it changes)
    const effects = controller.dispatch({ type: name, field: "basis", option: "0", index: 0 });
    assert.equal(effects.rerender, true, `${name} is handled (an unknown action would not redraw)`);
  }
  assert.equal(controller.dispatch({ type: "jump", index: 1 }).rerender, true);
  assert.equal(controller.dispatch({ type: "no such action" }).rerender, false);
  assert.equal(controller.dispatch(null).rerender, true, "nothing to do is not an error");
  // what the page's listeners can send: the keyboard's actions are handled as well
  for (const action of [{ type: "prev" }, { type: "next" }, { type: "saveNext" }, { type: "skip" }, { type: "blur" }, { type: "chooseKey", kind: "enum", number: 1 }, { type: "chooseKey", kind: "boolean", value: true }]) {
    assert.notEqual(controller.dispatch(action), undefined, action.type);
  }
  assert.equal(controller.dispatch({ type: "blur" }).blur, true);
});

// ---------------------------------------------------------------------------------------------------------------------- the page as a file
test("the page is offline: no URL, no request, no import, no stylesheet or script from anywhere but label.js", () => {
  for (const [name, text] of [["index.html", HTML], ["label.js", SOURCE]]) {
    for (const forbidden of ["http://", "https://", "//cdn", "fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "importScripts", "import(", "@import", "url(", "<link", "<iframe", "navigator.serviceWorker"]) {
      assert.ok(!text.includes(forbidden), `${name} must not contain ${forbidden}`);
    }
  }
  assert.match(HTML, /<input type="file" id="file-input" hidden>/, "no accept=: iOS greys out files whose extension (.jsonl) it does not know");
  const scripts = [...HTML.matchAll(/<script\b[^>]*>/g)].map((m) => m[0]);
  assert.deepEqual(scripts, ['<script src="label.js">', "<script>"]);
  assert.ok(HTML.includes("ArenaBenchLabel.mount(document, window);"));
  assert.ok(!/\bsrc=["'](?!label\.js)/.test(HTML), "nothing is loaded from anywhere else");
  assert.ok(!/\bhref=/.test(HTML), "no link at all");
});

test("the page works on a phone and in both themes - what the stylesheet promises", () => {
  assert.match(HTML, /<meta name="viewport" content="width=device-width, initial-scale=1">/);
  assert.match(HTML, /<html lang="zh-CN">/);
  for (const selector of [".btn", ".opt", ".text-input,\\s*select"]) {
    const rule = new RegExp(`${selector}\\s*\\{[^}]*min-height:\\s*(\\d+)px`).exec(HTML);
    assert.ok(rule && Number(rule[1]) >= 40, `${selector} is at least 40px high`);
  }
  assert.match(HTML, /min-width: 44px;\s*min-height: 44px/, "a button is a 44px target");
  assert.match(HTML, /summary\s*\{[^}]*min-height: 44px/);
  assert.match(HTML, /body\s*\{[^}]*overflow-x: hidden/);
  assert.match(HTML, /box-sizing: border-box/);
  assert.match(HTML, /overflow-wrap: anywhere/);
  assert.match(HTML, /white-space: pre-wrap/);
  assert.match(HTML, /@media \(max-width: 640px\)[\s\S]*\.pair\s*\{\s*grid-template-columns: 1fr;/, "the two candidates stack on a phone");
  assert.match(HTML, /@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme="light"\]\)/, "dark follows the system unless light is chosen");
  assert.match(HTML, /:root\[data-theme="dark"\]\s*\{\s*color-scheme: dark;/, "dark can be chosen by hand");
  assert.match(HTML, /:root\[data-theme="light"\]\s*\{\s*color-scheme: light;/);
  const dark = /\[data-theme="dark"\]\s*\{([^}]*)\}/.exec(HTML)[1];
  const media = /:root:not\(\[data-theme="light"\]\)\s*\{([^}]*)\}/.exec(HTML)[1];
  const names = (block) => [...block.matchAll(/--[\w-]+(?=:)/g)].map((m) => m[0]).sort();
  assert.deepEqual(names(dark).filter((n) => n !== "color-scheme"), names(media), "the two dark palettes define the same colours");
  assert.match(HTML, /prefers-reduced-motion/);
  for (const klass of ["opt", "selected", "field", "active", "pair", "cards", "scard", "notice", "progress-fill", "drop", "guide", "pos"]) {
    assert.ok(HTML.includes(`.${klass}`), `the stylesheet styles .${klass}`);
  }
});

test("the script is a plain file node can load and the browser can run: no module syntax, one global", () => {
  assert.ok(!/^\s*(import|export)\s/m.test(SOURCE), "no module syntax");
  assert.match(SOURCE, /root\.ArenaBenchLabel = api/);
  assert.match(SOURCE, /module\.exports = api/);
  assert.equal(typeof L.mount, "function");
  assert.equal(typeof L.createController, "function");
  assert.equal(L.mount({ getElementById: () => null }, {}), null, "no #app, no page");
});

// ---------------------------------------------------------------------------------------------------------------------- the DOM wiring (a small fake DOM)
function fakeEl(attrs, extra) {
  return {
    attrs: attrs || {},
    tagName: "DIV",
    value: "",
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
    },
    closest(selector) {
      const match = /^\[([\w-]+)\]$/.exec(selector);
      return match && this.getAttribute(match[1]) !== null ? this : null;
    },
    ...extra,
  };
}

function fakeBrowser(options) {
  const opts = options || {};
  const handlers = { doc: {}, win: {} };
  const downloads = [];
  const calls = { scroll: 0, confirms: [], blurred: 0, revoked: 0 };
  const host = { innerHTML: "" };
  const input = { value: "chosen.jsonl" };
  const storage = opts.storage || memory();
  const doc = {
    getElementById: (id) => (id === "app" ? host : id === "file-input" ? input : null),
    addEventListener: (type, fn) => (handlers.doc[type] = handlers.doc[type] || []).push(fn),
    documentElement: { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } },
    activeElement: { blur: () => void calls.blurred++ },
    body: { appendChild() {}, removeChild() {} },
    createElement: () => ({ style: {}, click() { downloads.push({ href: this.href, name: this.download }); } }),
  };
  const win = {
    localStorage: storage,
    confirm: (text) => {
      calls.confirms.push(text);
      return opts.confirm !== false;
    },
    scrollTo: () => void calls.scroll++,
    addEventListener: (type, fn) => (handlers.win[type] = handlers.win[type] || []).push(fn),
    Blob: class { constructor(parts, options2) { this.parts = parts; this.type = options2 && options2.type; } },
    URL: { createObjectURL: (blob) => { downloads.push({ blob }); return "blob:test"; }, revokeObjectURL: () => void calls.revoked++ },
    setTimeout: (fn) => fn(),
  };
  const fire = (target, type, event) => {
    const prevented = { value: false };
    for (const fn of handlers[target][type] || []) fn({ preventDefault: () => (prevented.value = true), ...event });
    return prevented.value;
  };
  return { doc, win, host, input, storage, downloads, calls, handlers, fire: (type, event) => fire("doc", type, event), fireWin: (type, event) => fire("win", type, event) };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const body = { tagName: "BODY", getAttribute: () => null };
const press = (browser, key, extra) => browser.fire("keydown", { key, target: body, ...extra });
const click = (browser, attrs) => browser.fire("click", { target: fakeEl(attrs) });
const fileOf = (text, name) => ({ name: name || "set.jsonl", text: () => Promise.resolve(text) });

async function openIn(browser, text) {
  browser.fire("change", { target: { id: "file-input", files: [fileOf(text)] } });
  await tick();
}

test("mount: the page starts on the welcome screen and a file chosen in the file input opens", async () => {
  const browser = fakeBrowser();
  const page = L.mount(browser.doc, browser.win);
  assert.match(browser.host.innerHTML, /把校准集拖到这里/);
  assert.equal(browser.doc.documentElement.attrs["data-theme"], undefined);
  await openIn(browser, setOf(3));
  assert.match(browser.host.innerHTML, /第 1 \/ 3 条/);
  assert.equal(browser.input.value, "", "the same file can be chosen again");
  assert.equal(page.controller.state.fileName, "set.jsonl");
  assert.match(browser.host.innerHTML, /<span class="muted file">set\.jsonl<\/span>/);
});

test("mount: a file dropped on the window opens too; a file that cannot be read says so", async () => {
  const browser = fakeBrowser();
  L.mount(browser.doc, browser.win);
  assert.equal(browser.fireWin("dragover", {}), true, "dragover is allowed (the drop needs it)");
  assert.equal(browser.fireWin("drop", { dataTransfer: { files: [fileOf(setOf(2), "dropped.jsonl")] } }), true);
  await tick();
  assert.match(browser.host.innerHTML, /第 1 \/ 2 条/);
  browser.fireWin("drop", { dataTransfer: { files: [] } });
  browser.fireWin("drop", {});
  const broken = { name: "x.jsonl", text: () => Promise.reject(new Error("denied")) };
  browser.fireWin("drop", { dataTransfer: { files: [broken] } });
  await tick();
  assert.match(browser.host.innerHTML, /读不了这个文件/);
});

test("mount: keys and clicks label an item, the page is drawn again, the progress is stored", async () => {
  const browser = fakeBrowser();
  const page = L.mount(browser.doc, browser.win);
  await openIn(browser, setOf(3));
  assert.equal(press(browser, "2"), true, "a handled key does not also do what the browser would do");
  assert.equal(press(browser, "y"), true);
  assert.match(browser.host.innerHTML, /✔ 必填的都选好了/);
  assert.match(browser.host.innerHTML, /class="opt selected" data-action="choose" data-field="basis" data-option="1"/);
  press(browser, "Enter");
  assert.match(browser.host.innerHTML, /第 2 \/ 3 条/);
  assert.equal(browser.calls.scroll, 1, "the page goes back to the top for a new item");
  click(browser, { "data-action": "choose", "data-field": "basis", "data-option": "4" });
  click(browser, { "data-action": "choose", "data-field": "cites_decisive", "data-option": "1" });
  assert.deepEqual(page.controller.state.answers[blindId(2)].fields, { basis: "unclear", cites_decisive: false });
  click(browser, { "data-action": "focusField", "data-field": "basis" });
  assert.match(browser.host.innerHTML, /<div class="field active answered" data-name="basis">/);
  click(browser, { "data-action": "prev" });
  assert.match(browser.host.innerHTML, /第 1 \/ 3 条/);
  press(browser, "ArrowRight");
  press(browser, "ArrowRight");
  assert.match(browser.host.innerHTML, /第 3 \/ 3 条/);
  press(browser, "s");
  press(browser, "ArrowLeft");
  click(browser, { "data-action": "nextOpen" });
  assert.match(browser.host.innerHTML, /第 3 \/ 3 条/);
  assert.equal([...browser.storage.data.keys()].filter((k) => k.startsWith(L.STORAGE_PREFIX)).length, 1);
});

test("mount: typing in the reason box is stored without redrawing; Enter in it saves and goes on; keys typed there are just text", async () => {
  const browser = fakeBrowser();
  const page = L.mount(browser.doc, browser.win);
  await openIn(browser, setOf(2));
  press(browser, "1");
  press(browser, "n");
  const before = browser.host.innerHTML;
  const box = fakeEl({ "data-text": "reason" }, { tagName: "INPUT", value: "要按 1 和 y" });
  browser.fire("input", { target: box });
  assert.equal(browser.host.innerHTML, before, "no redraw: the cursor stays where it is");
  assert.equal(page.controller.state.answers[blindId(1)].fields.reason, "要按 1 和 y");
  assert.equal(browser.fire("keydown", { key: "1", target: box }), false, "a digit typed in the box is left to the box");
  assert.equal(browser.fire("keydown", { key: "ArrowLeft", target: box }), false);
  assert.equal(page.controller.state.answers[blindId(1)].fields.basis, "decisive_fact");
  assert.equal(browser.fire("keydown", { key: "Enter", target: box }), true);
  assert.match(browser.host.innerHTML, /第 2 \/ 2 条/);
  const name = fakeEl({ "data-labeler": "" }, { tagName: "INPUT", value: "小红" });
  browser.fire("input", { target: name });
  assert.equal(page.controller.state.labeler, "小红");
  assert.equal(browser.fire("keydown", { key: "Enter", target: name }), true);
  assert.equal(browser.calls.blurred, 1, "Enter in the name box only leaves it");
  assert.match(browser.host.innerHTML, /第 2 \/ 2 条/, "and does not save or move on");
});

test("mount: typing a reason with a Chinese input method - the Enter that confirms the candidate neither saves nor moves on", async () => {
  const browser = fakeBrowser();
  const page = L.mount(browser.doc, browser.win);
  await openIn(browser, setOf(2));
  press(browser, "1");
  press(browser, "y");
  const box = fakeEl({ "data-text": "reason" }, { tagName: "INPUT", value: "她说" });
  browser.fire("input", { target: box });
  const before = browser.host.innerHTML;
  assert.equal(browser.fire("keydown", { key: "Enter", isComposing: true, target: box }), false, "Chrome / Firefox: isComposing");
  assert.equal(browser.fire("keydown", { key: "Enter", keyCode: 229, target: box }), false, "Safari: the Enter after compositionend has keyCode 229");
  assert.equal(browser.fire("keydown", { key: "Process", keyCode: 229, isComposing: true, target: box }), false);
  assert.equal(browser.host.innerHTML, before, "nothing was redrawn, nobody moved on");
  assert.equal(page.controller.view().current.number, 1);
  assert.equal(page.controller.state.answers[blindId(1)].fields.reason, "她说");
  assert.equal(browser.fire("keydown", { key: "Enter", isComposing: false, keyCode: 13, target: box }), true, "the next Enter is a real one");
  assert.equal(page.controller.view().current.number, 2);
});

test("mount: Enter on a focused button is the button's, the keys do nothing before a file is open", async () => {
  const browser = fakeBrowser();
  L.mount(browser.doc, browser.win);
  assert.equal(press(browser, "ArrowRight"), false);
  assert.equal(press(browser, "1"), false);
  assert.equal(press(browser, "Enter"), false);
  assert.equal(browser.fire("keydown", { key: "Escape", target: { tagName: "INPUT", getAttribute: () => null } }), true, "Escape leaves a box even now");
  await openIn(browser, setOf(2));
  assert.equal(browser.fire("keydown", { key: "Enter", target: { tagName: "BUTTON", getAttribute: () => null } }), false);
  assert.equal(browser.fire("keydown", { key: "r", target: body }), false);
  assert.equal(browser.fire("keydown", { key: "s", ctrlKey: true, target: body }), false);
});

test("mount: the jump list, the theme button, the guide, the labeler", async () => {
  const browser = fakeBrowser();
  const page = L.mount(browser.doc, browser.win);
  await openIn(browser, setOf(4));
  browser.fire("change", { target: fakeEl({ "data-action": "jump" }, { value: "2" }) });
  assert.match(browser.host.innerHTML, /第 3 \/ 4 条/);
  assert.equal(click(browser, { "data-action": "jump" }), false, "a click on the select itself does nothing");
  assert.match(browser.host.innerHTML, /第 3 \/ 4 条/);
  click(browser, { "data-action": "theme" });
  assert.equal(browser.doc.documentElement.attrs["data-theme"], "light");
  click(browser, { "data-action": "theme" });
  assert.equal(browser.doc.documentElement.attrs["data-theme"], "dark");
  assert.match(browser.host.innerHTML, /主题：深色/);
  click(browser, { "data-action": "theme" });
  assert.equal(browser.doc.documentElement.attrs["data-theme"], undefined, "back to the system's");
  const before = browser.host.innerHTML;
  browser.fire("toggle", { target: fakeEl({ "data-guide": "" }, { open: true }) });
  assert.equal(page.controller.state.guideOpen, true);
  assert.equal(browser.host.innerHTML, before, "opening the guide does not redraw (it would close it again)");
  browser.fire("toggle", { target: fakeEl({}, { open: true }) });
  click(browser, { "data-action": "choose", "data-field": "basis", "data-option": "0" });
  assert.match(browser.host.innerHTML, /<details class="guide" data-guide open>/, "the guide stays open from item to item");
  browser.fire("input", { target: fakeEl({ "data-other": "" }, { value: "x" }) });
  browser.fire("input", {});
});

test("mount: export downloads labels.jsonl; clear asks first", async () => {
  const browser = fakeBrowser({ confirm: false });
  const page = L.mount(browser.doc, browser.win);
  await openIn(browser, setOf(2));
  click(browser, { "data-action": "export" });
  assert.match(browser.host.innerHTML, /还没有可以导出的标注/);
  assert.equal(browser.downloads.length, 0);
  press(browser, "3");
  press(browser, "y");
  click(browser, { "data-action": "export" });
  const blob = browser.downloads.find((d) => d.blob).blob;
  assert.equal(blob.type, "application/x-ndjson");
  const rows = blob.parts[0].trim().split("\n").map((line) => JSON.parse(line));
  assert.deepEqual(rows.map((r) => [r.format, r.rubric, r.label.basis]), [["arena-bench-label", "L3", "expression"]]);
  assert.deepEqual(browser.downloads.find((d) => d.name), { href: "blob:test", name: "labels.jsonl" });
  assert.equal(browser.calls.revoked, 1, "the object URL is let go");
  click(browser, { "data-action": "clear" });
  assert.equal(browser.calls.confirms.length, 1);
  assert.match(browser.calls.confirms[0], /清除这个文件的全部本地标注进度/);
  assert.equal(page.controller.view().done, 1, "not confirmed: nothing is cleared");
  const sure = fakeBrowser({ confirm: true });
  const page2 = L.mount(sure.doc, sure.win);
  await openIn(sure, setOf(2));
  press(sure, "3");
  press(sure, "y");
  click(sure, { "data-action": "clear" });
  assert.equal(page2.controller.view().done, 0);
  assert.equal([...sure.storage.data.keys()].filter((k) => k.startsWith(L.STORAGE_PREFIX)).length, 0);
});

test("mount: a browser without localStorage still works - the page says the progress is not kept", async () => {
  const browser = fakeBrowser();
  Object.defineProperty(browser.win, "localStorage", { get() { throw new Error("SecurityError"); } });
  L.mount(browser.doc, browser.win);
  await openIn(browser, setOf(2));
  press(browser, "1");
  press(browser, "y");
  assert.match(browser.host.innerHTML, /进度没有保存到浏览器里/);
  assert.match(browser.host.innerHTML, /已标 1 \/ 共 2/);
});

test("mount: a file without File.text() is read with a FileReader", async () => {
  const browser = fakeBrowser();
  browser.win.FileReader = class {
    readAsText(file) {
      this.result = file.content;
      this.onload();
    }
  };
  L.mount(browser.doc, browser.win);
  browser.fire("change", { target: { id: "file-input", files: [{ name: "old.jsonl", content: setOf(2) }] } });
  assert.match(browser.host.innerHTML, /第 1 \/ 2 条/);
});

// ---------------------------------------------------------------------------------------------------------------------- run
(async () => {
  let failed = 0;
  for (const { name, fn } of tests) {
    try {
      await fn();
      console.log(`ok   - ${name}`);
    } catch (err) {
      failed += 1;
      console.log(`FAIL - ${name}\n${err && err.stack ? err.stack : err}`);
    }
  }
  console.log(`${tests.length - failed}/${tests.length} passed`);
  process.exit(failed ? 1 : 0);
})();
