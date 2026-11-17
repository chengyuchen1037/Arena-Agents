"use strict";
// Determinism / uniqueness of the minimal player glyphs (static/js/avatar.js).
// Run: node tests/frontend/test_avatar.js
const assert = require("node:assert/strict");
const path = require("node:path");
const Avatar = require(path.join(__dirname, "..", "..", "static", "js", "avatar.js"));

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("hashString is FNV-1a (stable test vectors)", () => {
  assert.equal(Avatar.hashString(""), 0x811c9dc5);
  assert.equal(Avatar.hashString("a"), 0xe40c292c);
  assert.equal(Avatar.hashString("foobar"), 0xbf9cf968);
  assert.equal(Avatar.hashString(null), Avatar.hashString(""));
});

test("glyphSpec is deterministic: same inputs, same output, every time", () => {
  for (const [id, seat] of [["p01", 1], ["p07", 7], ["p12", 12], ["custom", undefined], ["x", "3"]]) {
    const a = Avatar.glyphSpec(id, seat);
    const b = Avatar.glyphSpec(id, seat);
    assert.deepEqual(a, b);
    assert.ok(Avatar.SHAPES.includes(a.shape));
    assert.ok(Avatar.TEXTURES.includes(a.texture));
    assert.ok(Avatar.HUES.includes(a.hue));
  }
  // snapshot of the actual mapping: it must never change silently between releases
  assert.equal(Avatar.glyphSpec("p01", 1).key, "circle-212-solid");
  assert.equal(Avatar.glyphSpec("p02", 2).key, "square-4-solid");
  assert.equal(Avatar.glyphSpec("p07", 7).key, "circle-84-stripes");
  assert.equal(Avatar.glyphSpec("p12", 12).key, "cross-54-stripes");
});

test("the first 12 seats (and the first 24) are pairwise distinct combinations", () => {
  const twelve = new Set();
  for (let seat = 1; seat <= 12; seat++) twelve.add(Avatar.glyphSpec(`p${String(seat).padStart(2, "0")}`, seat).key);
  assert.equal(twelve.size, 12);
  const twentyFour = new Set();
  for (let seat = 1; seat <= 24; seat++) twentyFour.add(Avatar.glyphSpec("p" + seat, seat).key);
  assert.equal(twentyFour.size, 24);
});

test("neighbouring seats differ in colour AND shape so they never look alike", () => {
  for (let seat = 1; seat < 12; seat++) {
    const a = Avatar.glyphSpec("p" + seat, seat);
    const b = Avatar.glyphSpec("p" + (seat + 1), seat + 1);
    assert.notEqual(a.hue, b.hue, `seat ${seat} vs ${seat + 1}: hue`);
    assert.notEqual(a.shape, b.shape, `seat ${seat} vs ${seat + 1}: shape`);
  }
});

test("the seat number wins; otherwise the number inside the id; otherwise a hash", () => {
  assert.equal(Avatar.seatIndex("p03", 3), 2);
  assert.equal(Avatar.seatIndex("p03", undefined), 2); // same glyph without a seat lookup
  assert.deepEqual(Avatar.glyphSpec("p03"), Avatar.glyphSpec("p03", 3));
  assert.equal(Avatar.seatIndex("anything", "5"), 4); // numeric strings are fine
  const h1 = Avatar.seatIndex("alice", undefined);
  const h2 = Avatar.seatIndex("alice", undefined);
  assert.equal(h1, h2);
  assert.ok(h1 >= 0);
  assert.notEqual(Avatar.seatIndex("alice"), Avatar.seatIndex("bob"));
  assert.equal(Avatar.seatIndex("p0", 0), Avatar.seatIndex("p0")); // seat 0 is not a seat
});

test("the moderator is a star in neutral ink; the engine a gear; both differ from every player", () => {
  const mod = Avatar.glyphSpec("moderator", 99);
  const eng = Avatar.glyphSpec("engine");
  assert.equal(mod.shape, "star");
  assert.equal(mod.moderator, true);
  assert.equal(eng.shape, "gear");
  assert.equal(eng.system, true);
  const players = new Set();
  for (let seat = 1; seat <= 24; seat++) players.add(Avatar.glyphSpec("p" + seat, seat).key);
  assert.ok(!players.has(mod.key));
  assert.ok(!players.has(eng.key));
  assert.notEqual(mod.key, eng.key);
});

test("glyphHtml only emits whitelisted classes and numbers (no id echo, no injection)", () => {
  const html = Avatar.glyphHtml('"><script>alert(1)</script>', undefined);
  assert.ok(!html.includes("<script"));
  assert.ok(!html.includes("alert"));
  assert.match(html, /^<span class="glyph glyph-s-[a-z]+ glyph-t-[a-z]+" style="--gh:\d+" data-glyph="[a-z]+-\d+-[a-z]+" aria-hidden="true"><\/span>$/);

  const p = Avatar.glyphHtml("p05", 5);
  assert.match(p, /glyph-s-hexagon|glyph-s-/);
  assert.ok(p.includes('data-glyph="'));
  assert.ok(!p.includes("p05")); // the id never appears in the glyph markup

  const mod = Avatar.glyphHtml("moderator");
  assert.match(mod, /glyph-s-star/);
  assert.match(mod, /glyph-moderator/);

  // size is optional and clamped to a sane pixel range
  assert.ok(Avatar.glyphHtml("p01", 1, { size: 24 }).includes("--gs:24px"));
  assert.ok(!Avatar.glyphHtml("p01", 1, { size: 9999 }).includes("--gs"));
  assert.ok(!Avatar.glyphHtml("p01", 1, { size: "x\"y" }).includes("--gs"));
});

test("textures advance every six seats", () => {
  assert.equal(Avatar.glyphSpec("p01", 1).texture, "solid");
  assert.equal(Avatar.glyphSpec("p06", 6).texture, "solid");
  assert.equal(Avatar.glyphSpec("p07", 7).texture, "stripes");
  assert.equal(Avatar.glyphSpec("p12", 12).texture, "stripes");
  assert.equal(Avatar.glyphSpec("p13", 13).texture, "split");
  assert.equal(Avatar.glyphSpec("p19", 19).texture, "dots");
});

// ---- runner -------------------------------------------------------------------
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
