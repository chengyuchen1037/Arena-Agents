"use strict";
// v0.13.1 (G27, contract §12.6): the avatar CODE of static/js/avatar.js - Avatar.encode / decode / codeOf / sameLook.
// The vectors are the SAME file the Python side is tested with (tests/avatar_code_cases.json, tests/test_avatar_code.py):
// the two implementations must agree on every one of them (NFKC, the fixed priority empty > bad_format >
// duplicate_field > missing_field > unknown_part, "none" -> a00, the first letter of every id is checked when encoding).
// Run: node tests/frontend/test_avatar_code.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Avatar = require(path.join(__dirname, "..", "..", "static", "js", "avatar.js"));

const CASES = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "avatar_code_cases.json"), "utf8"));
const PARTS = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "parts.min.json"), "utf8"));
const FIELDS = Avatar.FIELDS;
const CJK = /[\u4e00-\u9fff]/;
const CANONICAL = /^(?:[fshcemak][0-9]{2,3}){8}$/;
const GOOD = { v: 1, face: "f03", skin: "s05", hair: "h12", hair_color: "c07", eyes: "e04", mouth: "m02", accessory: "a09", accessory_color: "k03" };
const GOOD_CODE = "f03s05h12c07e04m02a09k03";

const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const shown = (text) => JSON.stringify(text).slice(0, 60);

// ---- the shared vector file --------------------------------------------------------------------
test("the vector file has the contract's shape (the same checks as tests/test_avatar_code.py)", () => {
  assert.ok(Array.isArray(CASES.valid) && Array.isArray(CASES.invalid) && Array.isArray(CASES.notes));
  assert.ok(CASES.valid.length >= 20 && CASES.invalid.length >= 30);
  for (const c of CASES.valid) {
    assert.deepEqual(Object.keys(c).sort(), ["code", "description", "input"]);
    assert.match(c.code, CANONICAL, "the canonical form: lower case, no separators, eight ids");
    assert.deepEqual(Object.keys(c.description), ["v", ...FIELDS]);
    assert.ok(FIELDS.every((f) => c.description[f][0] === Avatar.FIELD_LETTERS[f]));
  }
  for (const c of CASES.invalid) {
    assert.ok(Object.keys(c).every((k) => ["input", "reason", "field"].includes(k)), JSON.stringify(c));
    assert.ok(Avatar.CODE_REASONS.includes(c.reason) && c.reason !== "unknown_part", "unknown_part needs a library");
    if (c.reason === "missing_field" || c.reason === "duplicate_field") assert.ok(FIELDS.includes(c.field), JSON.stringify(c));
    else assert.ok(!("field" in c));
  }
  assert.deepEqual([...new Set(CASES.invalid.map((c) => c.reason))].sort(), ["bad_format", "duplicate_field", "empty", "missing_field"]);
  assert.deepEqual([...new Set(CASES.invalid.filter((c) => c.reason === "missing_field").map((c) => c.field))].sort(), [...FIELDS].sort());
  assert.deepEqual(Avatar.CODE_REASONS, ["empty", "bad_format", "missing_field", "duplicate_field", "unknown_part"]);
  assert.deepEqual(Avatar.FIELD_LETTERS, { face: "f", skin: "s", hair: "h", hair_color: "c", eyes: "e", mouth: "m", accessory: "a", accessory_color: "k" });
});

for (const c of CASES.valid) {
  test(`valid vector ${shown(c.input)}: decode(input), decode(code), encode(description) and the round trip agree`, () => {
    const a = Avatar.decode(c.input);
    assert.equal(a.ok, true, JSON.stringify(a));
    assert.deepEqual(a.description, c.description);
    assert.deepEqual(Object.keys(a.description), ["v", ...FIELDS], "the key order of validate_description's output");
    assert.deepEqual(Avatar.decode(c.code).description, c.description);
    assert.equal(Avatar.encode(c.description), c.code);
    assert.deepEqual(Avatar.decode(Avatar.encode(c.description)).description, c.description);
    assert.equal(Avatar.codeOf(c.description), c.code);
  });
}

for (const c of CASES.invalid) {
  test(`invalid vector ${shown(c.input)}: ${c.reason}${c.field ? ` (${c.field})` : ""}`, () => {
    for (const lib of [undefined, PARTS]) {
      const r = Avatar.decode(c.input, lib);
      assert.equal(r.ok, false, JSON.stringify(r));
      assert.equal(r.reason, c.reason, `${JSON.stringify(c.input)}: ${r.message}`);
      assert.equal(r.field, c.field, JSON.stringify(c));
      assert.equal(r.part, undefined, "parts are only checked against a library, and only after the format");
      assert.match(r.message, CJK, "the message is for the user: Chinese");
      assert.equal(r.description, undefined);
    }
  });
}

// ---- decode: the rules in words ---------------------------------------------------------------------
test("the docstring example and the contract example", () => {
  assert.deepEqual(Avatar.decode(GOOD_CODE).description, GOOD);
  assert.equal(Avatar.encode(GOOD), GOOD_CODE);
});

test("decode is lenient about case, separators and order but not about the segments", () => {
  for (const text of [
    "F03S05H12C07E04M02A09K03", "f03 s05 h12 c07 e04 m02 a09 k03", " f03-s05-h12-c07-e04-m02-a09-k03 ", "f03_s05_h12_c07_e04_m02_a09_k03",
    "f03:s05:h12:c07:e04:m02:a09:k03", "k03 a09 m02 e04 c07 h12 s05 f03",
  ]) {
    assert.deepEqual(Avatar.decode(text).description, GOOD, text);
  }
  assert.equal(Avatar.decode("f03s05h12c07e04m02a00k03").description.accessory, "a00", "a00 is simply 'no accessory' (never 'none')");
  for (const text of ["f03s05h12c07e04m02a09k03.", "f03/s05/h12/c07/e04/m02/a09/k03", "f3s05h12c07e04m02a09k03"]) {
    assert.equal(Avatar.decode(text).reason, "bad_format", text);
  }
});

test("the first problem is reported in a fixed order: format, then duplicates, then what is missing", () => {
  const cases = [
    ["f03f04s05h12c07e04m02a09k03k", "bad_format", undefined], // a stray `k` is a format error even though face repeats
    ["f03s05s06h12c07e04m02a09", "duplicate_field", "skin"], // duplicate wins over the missing accessory colour
    ["s05s06f03f04h12c07e04m02a09k03", "duplicate_field", "skin"], // the first repeat in reading order
    ["k03a09m02", "missing_field", "face"], // the first missing one in FIELDS order, not in reading order
  ];
  for (const [text, reason, field] of cases) {
    const r = Avatar.decode(text);
    assert.deepEqual([r.reason, r.field], [reason, field], text);
  }
});

test("nothing, or something that is not text", () => {
  for (const v of [undefined, null, ""]) {
    const r = Avatar.decode(v);
    assert.equal(r.reason, "empty");
    assert.match(r.message, /头像编号/);
  }
  for (const v of [5, 1.5, true, [], {}, ["f03"]]) {
    const r = Avatar.decode(v);
    assert.equal(r.reason, "bad_format", String(v));
    assert.match(r.message, CJK);
  }
});

test("the messages name the offending part in Chinese", () => {
  let r = Avatar.decode("f03s05h12c07e04m02a09");
  assert.deepEqual([r.reason, r.field], ["missing_field", "accessory_color"]);
  assert.match(r.message, /配饰颜色/);
  assert.match(r.message, /k/);
  r = Avatar.decode("f03f04s05h12c07e04m02a09k03");
  assert.deepEqual([r.reason, r.field], ["duplicate_field", "face"]);
  assert.match(r.message, /脸型/);
  assert.match(r.message, /不止一次/);
  assert.match(Avatar.decode("f03s05h12c07e04m02a09k03!").message, /'!'/, "which character is wrong");
  assert.match(Avatar.decode("x03s05h12c07e04m02a09k03").message, /x03/, "which segment is wrong");
});

test("full-width letters, digits, colons, spaces and hyphens are read like their ASCII twins (NFKC)", () => {
  const full = "\uff26\uff10\uff13\u3000\uff53\uff10\uff15\uff1a\uff48\uff11\uff12\uff3f\uff43\uff10\uff17\uff0d\uff45\uff10\uff14\u00a0m02a09k03";
  assert.deepEqual(Avatar.decode(full).description, GOOD);
});

// ---- decode against the sprite library -------------------------------------------------------------
test("unknown parts are only reported when a library is given, after the format and in FIELDS order", () => {
  const code = "f99s05h12c07e04m02a09k03";
  assert.equal(Avatar.decode(code).description.face, "f99", "format only: nothing to compare with");
  const r = Avatar.decode(code, PARTS);
  assert.deepEqual([r.ok, r.reason, r.field, r.part], [false, "unknown_part", "face", "f99"]);
  assert.match(r.message, /脸型/);
  assert.match(r.message, /f99/);
  let two = Avatar.decode("f01s01h01c01e01m01a99k99", PARTS);
  assert.deepEqual([two.field, two.part], ["accessory", "a99"], "the first unknown one in FIELDS order");
  two = Avatar.decode("f01s01h01c01e01m01a00k99", PARTS);
  assert.deepEqual([two.field, two.part], ["accessory_color", "k99"], "a colour the library does not have is unknown even without an accessory");
  assert.equal(Avatar.decode("f99s05h12c07e04m02a09", PARTS).reason, "missing_field", "structure is checked before the parts");
  // a library that is not a library does not make a code invalid
  assert.equal(Avatar.decode(GOOD_CODE, {}).ok, true);
  assert.equal(Avatar.decode(GOOD_CODE, null).ok, true);
});

test("every part of the library has a code that decodes back; the page can draw what was decoded", () => {
  for (const field of FIELDS) {
    for (const id of Avatar.idsOf(PARTS, field)) {
      const desc = { v: 1, face: "f01", skin: "s01", hair: "h01", hair_color: "c01", eyes: "e01", mouth: "m01", accessory: "a01", accessory_color: "k01", [field]: id };
      const code = Avatar.encode(desc);
      assert.deepEqual(Avatar.decode(code, PARTS).description, desc, `${field} ${id}`);
    }
  }
  const px = Avatar.renderPixels(Avatar.decode("f01 s01 h01 c01 e01 m01 a01 k01", PARTS).description, PARTS);
  assert.ok(px && px.width === 20);
  const same = Avatar.renderPixels(Avatar.decode("K01A01M01E01C01H01S01F01", PARTS).description, PARTS);
  assert.equal(Avatar.pixelHash(px), Avatar.pixelHash(same), "the same code always draws the same picture");
});

// ---- encode ---------------------------------------------------------------------------------------------
test("encode maps none to a00 and an empty accessory colour to k00", () => {
  assert.equal(Avatar.encode({ ...GOOD, accessory: "none" }), "f03s05h12c07e04m02a00k03");
  assert.equal(Avatar.encode({ ...GOOD, accessory: "none", accessory_color: "" }), "f03s05h12c07e04m02a00k00");
  assert.equal(Avatar.encode({ ...GOOD, accessory: "a00", accessory_color: "" }), "f03s05h12c07e04m02a00k00");
  const noColor = { ...GOOD };
  delete noColor.accessory_color;
  assert.equal(Avatar.encode({ ...noColor, accessory: "a00" }), "f03s05h12c07e04m02a00k00");
  assert.equal(Avatar.decode("f03s05h12c07e04m02a00k00").description.accessory_color, "k00");
  // with an accessory the colour is required
  assert.throws(() => Avatar.encode({ ...GOOD, accessory_color: "" }), (e) => e.reason === "bad_format" && e.field === "accessory_color");
  assert.throws(() => Avatar.encode(noColor), (e) => e.reason === "missing_field" && e.field === "accessory_color");
});

test("encode ignores the version and extra keys and does not touch its input", () => {
  const desc = { ...GOOD, v: 7, note: "x" };
  const before = JSON.stringify(desc);
  assert.equal(Avatar.encode(desc), GOOD_CODE);
  assert.equal(JSON.stringify(desc), before);
  const noVersion = { ...GOOD };
  delete noVersion.v;
  assert.equal(Avatar.encode(noVersion), GOOD_CODE);
});

test("encode refuses a missing field and a malformed or misplaced id (it would be read back as another field)", () => {
  for (const field of FIELDS) {
    const missing = { ...GOOD };
    delete missing[field];
    assert.throws(() => Avatar.encode(missing), (e) => e instanceof Avatar.AvatarCodeError && e.reason === "missing_field" && e.field === field && e.message.includes(field), field);
    const wrongLetter = Avatar.FIELD_LETTERS[field] === "z" ? "y05" : "z05";
    for (const bad of ["", "05", "x", wrongLetter, GOOD[field].toUpperCase(), `${GOOD[field]}\n`, `${GOOD[field][0]}0003`, 3, ["f03"]]) {
      assert.throws(() => Avatar.encode({ ...GOOD, [field]: bad }), (e) => e.reason === "bad_format" && e.field === field && CJK.test(e.message), `${field} = ${JSON.stringify(bad)}`);
    }
  }
  // `s05` as a face would be read back as the skin
  assert.throws(() => Avatar.encode({ ...GOOD, face: "s05" }), (e) => e.reason === "bad_format" && e.field === "face");
  for (const bad of [null, [], "f03", 3, undefined]) {
    assert.throws(() => Avatar.encode(bad), (e) => e.reason === "bad_format" && /对象/.test(e.message));
  }
});

test("codeOf is the quiet encode: '' for anything that has no code, and descriptions from the server work", () => {
  assert.equal(Avatar.codeOf(GOOD), GOOD_CODE);
  assert.equal(Avatar.codeOf(null), "");
  assert.equal(Avatar.codeOf({}), "");
  assert.equal(Avatar.codeOf({ ...GOOD, face: "s05" }), "", "an id of another field has no code");
  assert.equal(Avatar.codeOf({ ...GOOD, accessory: "none", accessory_color: "" }), "f03s05h12c07e04m02a00k00");
  assert.equal(Avatar.codeOf({ ...GOOD, accessory: undefined, accessory_color: undefined }), "f03s05h12c07e04m02a00k00", "no accessory at all");
});

test("sameLook: the eight parts decide, 'none' is a00, the colour of no accessory does not count", () => {
  assert.equal(Avatar.sameLook(GOOD, { ...GOOD }), true);
  assert.equal(Avatar.sameLook(GOOD, { ...GOOD, v: 1, hair: "h13" }), false);
  assert.equal(Avatar.sameLook({ ...GOOD, accessory: "none", accessory_color: "k01" }, { ...GOOD, accessory: "a00", accessory_color: "k09" }), true);
  assert.equal(Avatar.sameLook({ ...GOOD, accessory_color: "k01" }, { ...GOOD, accessory_color: "k09" }), false, "with an accessory the colour is part of the look");
  assert.equal(Avatar.sameLook(null, null), false, "two nothings are not a twin");
  assert.equal(Avatar.sameLook(GOOD, null), false);
  assert.equal(Avatar.lookKey(null), "");
});

test("the decoded description is accepted wherever a description is (normalizeDescription / descriptionKey)", () => {
  const d = Avatar.decode(GOOD_CODE).description;
  assert.deepEqual(Avatar.normalizeDescription(d), d);
  assert.equal(Avatar.descriptionKey(d), "1|f03|s05|h12|c07|e04|m02|a09|k03");
});

// ---- runner --------------------------------------------------------------------------------------------------
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
