"use strict";
// Pixel avatars (static/js/avatar.js): determinism, layer order, fallbacks, PNG output, library loading.
// Run: node tests/frontend/test_avatar.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");
const Avatar = require(path.join(__dirname, "..", "..", "static", "js", "avatar.js"));
const PARTS = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "parts.min.json"), "utf8"));
const F = require("./fixtures.js");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const clone = (v) => JSON.parse(JSON.stringify(v));

const DESC = {
  v: 1, face: "f01", skin: "s01", hair: "h02", hair_color: "c02", eyes: "e02", mouth: "m01", accessory: "a01", accessory_color: "k00",
};
const withParts = (fn) => () => {
  Avatar.setParts(clone(PARTS));
  try {
    return fn();
  } finally {
    Avatar.setParts(null);
  }
};
const rgbaAt = (px, x, y) => Array.from(px.data.slice((y * px.width + x) * 4, (y * px.width + x) * 4 + 4));
const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));

// ---- helpers ------------------------------------------------------------------------------
test("hashString is FNV-1a (stable test vectors)", () => {
  assert.equal(Avatar.hashString(""), 0x811c9dc5);
  assert.equal(Avatar.hashString("a"), 0xe40c292c);
  assert.equal(Avatar.hashString("foobar"), 0xbf9cf968);
  assert.equal(Avatar.hashString(null), Avatar.hashString(""));
});

test("seatIndex: the seat number wins, then the number inside the id (N3 -> 2), then a hash", () => {
  assert.equal(Avatar.seatIndex("N3", 3), 2);
  assert.equal(Avatar.seatIndex("N3", undefined), 2);
  assert.equal(Avatar.seatIndex("N12", "12"), 11); // the contract sends the seat as a string
  assert.equal(Avatar.seatIndex("anything", "5"), 4);
  assert.equal(Avatar.seatIndex("alice"), Avatar.seatIndex("alice"));
  assert.notEqual(Avatar.seatIndex("alice"), Avatar.seatIndex("bob"));
  assert.equal(Avatar.seatIndex("N0", 0), Avatar.seatIndex("N0"));
});

// ---- the library --------------------------------------------------------------------------
test("validateParts accepts the fixture library and rejects broken ones; a rejected library is not installed", () => {
  assert.deepEqual(Avatar.validateParts(PARTS), { ok: true, errors: [] });
  assert.equal(Avatar.validateParts(null).ok, false);
  assert.equal(Avatar.validateParts({}).ok, false);
  const noFace = clone(PARTS);
  noFace.parts.face = {};
  assert.equal(Avatar.validateParts(noFace).ok, false);
  const noFixed = clone(PARTS);
  delete noFixed.palettes.fixed;
  assert.match(Avatar.validateParts(noFixed).errors.join(), /fixed/);
  assert.equal(Avatar.setParts(noFace), false);
  assert.equal(Avatar.hasParts(), false);
  assert.equal(Avatar.setParts(clone(PARTS)), true);
  assert.equal(Avatar.hasParts(), true);
  Avatar.setParts(null);
  assert.equal(Avatar.hasParts(), false);
});

test("loadParts installs a good file and never throws on a missing / broken one (404, bad JSON, offline)", async () => {
  const ok = (body) => async () => ({ ok: true, json: async () => body });
  assert.equal(await Avatar.loadParts(ok(clone(PARTS)), "x").then(() => Avatar.hasParts()), true);
  await Avatar.loadParts(async () => ({ ok: false, status: 404, json: async () => ({}) }), "x");
  assert.equal(Avatar.hasParts(), false, "404: the previous library is dropped, the circle fallback is used");
  await Avatar.loadParts(ok(clone(PARTS)), "x");
  await Avatar.loadParts(async () => ({ ok: true, json: async () => { throw new SyntaxError("bad json"); } }), "x");
  assert.equal(Avatar.hasParts(), false);
  await Avatar.loadParts(async () => { throw new TypeError("offline"); }, "x");
  assert.equal(Avatar.hasParts(), false);
  await Avatar.loadParts(ok({ not: "a library" }), "x");
  assert.equal(Avatar.hasParts(), false);
});

test("onChange fires when the library arrives / goes away and can be unsubscribed", () => {
  const seen = [];
  const off = Avatar.onChange((has) => seen.push(has));
  Avatar.setParts(clone(PARTS));
  Avatar.setParts(null);
  off();
  Avatar.setParts(clone(PARTS));
  Avatar.setParts(null);
  assert.deepEqual(seen, [true, false]);
  // a throwing listener never breaks loading
  const off2 = Avatar.onChange(() => {
    throw new Error("boom");
  });
  assert.doesNotThrow(() => Avatar.setParts(clone(PARTS)));
  off2();
  Avatar.setParts(null);
});

// ---- rendering -----------------------------------------------------------------------------
test("the same description renders pixel-for-pixel the same, every time (and as a data URL)", withParts(() => {
  const a = Avatar.renderPixels(DESC);
  const b = Avatar.renderPixels(clone(DESC));
  assert.equal(a.width, 20);
  assert.equal(a.height, 20);
  assert.deepEqual(Array.from(a.data), Array.from(b.data));
  assert.equal(Avatar.pixelHash(a), Avatar.pixelHash(b));
  const u1 = Avatar.dataUrl(DESC, { seat: 4 });
  const u2 = Avatar.dataUrl(clone(DESC), { seat: 9 }); // the seat only matters for the fallback
  assert.equal(u1, u2);
  assert.match(u1, /^data:image\/png;base64,/);
}));

test("different descriptions render differently: every field changes the picture", withParts(() => {
  const base = Avatar.pixelHash(Avatar.renderPixels(DESC));
  const variants = {
    face: "f02", skin: "s02", hair: "h01", hair_color: "c01", eyes: "e01", mouth: "m02", accessory: "a02", accessory_color: "k01",
  };
  for (const [field, id] of Object.entries(variants)) {
    const hash = Avatar.pixelHash(Avatar.renderPixels({ ...DESC, [field]: id }));
    assert.notEqual(hash, base, `${field} does not change the picture`);
  }
  // all 12 default seats look different from each other
  const seen = new Set();
  for (let seat = 1; seat <= 12; seat++) seen.add(Avatar.pixelHash(Avatar.renderPixels(Avatar.defaultDescription(seat))));
  assert.ok(seen.size >= 10, `only ${seen.size} different default avatars for 12 seats`);
}));

test("layers are drawn hair.back -> accessory.back -> face -> eyes -> mouth -> hair.front -> accessory.front (checked against an independent drawer)", withParts(() => {
  // independent re-implementation of docs/v0.12-contracts.md §10.4
  const P = PARTS;
  const fixed = P.palettes.fixed;
  const colorOf = (palette, ch) => hex(palette[ch]);
  const fixedChar = { o: "outline", x: "blush", w: "eye_white", p: "pupil", m: "mouth", t: "tooth" };
  const expected = Array.from({ length: 400 }, () => [0, 0, 0, 0]);
  const draw = (grid, palette) => {
    if (!grid) return;
    grid.forEach((row, y) =>
      Array.from(row).forEach((ch, x) => {
        if (ch === ".") return;
        const rgb = "bsl".includes(ch) ? colorOf(palette, ch) : hex(fixed[fixedChar[ch]]);
        expected[y * 20 + x] = [...rgb, 255];
      })
    );
  };
  for (const d of [DESC, { ...DESC, face: "f00", hair: "h01", accessory: "a02", eyes: "e00" }, { ...DESC, hair: "h00", accessory: "none" }]) {
    expected.fill(null);
    for (let i = 0; i < 400; i++) expected[i] = [0, 0, 0, 0];
    const hair = P.parts.hair[d.hair];
    const acc = d.accessory === "none" ? null : P.parts.accessory[d.accessory];
    draw(hair.back, P.palettes.hair_color[d.hair_color]);
    draw(acc && acc.back, P.palettes.accessory_color[d.accessory_color]);
    draw(P.parts.face[d.face].front, P.palettes.skin[d.skin]);
    draw(P.parts.eyes[d.eyes].front, P.palettes.skin[d.skin]);
    draw(P.parts.mouth[d.mouth].front, P.palettes.skin[d.skin]);
    draw(hair.front, P.palettes.hair_color[d.hair_color]);
    draw(acc && acc.front, P.palettes.accessory_color[d.accessory_color]);
    const got = Avatar.renderPixels(d);
    for (let i = 0; i < 400; i++) {
      assert.deepEqual(Array.from(got.data.slice(i * 4, i * 4 + 4)), expected[i], `pixel ${i} of ${JSON.stringify(d)}`);
    }
  }
}));

test("overlap order is observable with a hand-made library: later layers cover earlier ones", () => {
  const blank = Array(20).fill(".".repeat(20));
  const row = (ch) => [ch + ".".repeat(19), ...Array(19).fill(".".repeat(20))];
  const lib = {
    version: 1,
    size: 20,
    palettes: {
      skin: { s: { b: "#010101", s: "#010101", l: "#010101" } },
      hair_color: { h: { b: "#020202", s: "#020202", l: "#020202" } },
      accessory_color: { a: { b: "#030303", s: "#030303", l: "#030303" } },
      fixed: { outline: "#0a0a0a", blush: "#0b0b0b", eye_white: "#0c0c0c", pupil: "#0d0d0d", mouth: "#0e0e0e", tooth: "#0f0f0f" },
    },
    parts: {
      face: { f: { front: row("b") } },
      hair: { h: { back: row("b"), front: blank } },
      eyes: { e: { front: blank } },
      mouth: { m: { front: blank } },
      accessory: { a: { back: row("b"), front: blank } },
    },
  };
  const d = { v: 1, face: "f", skin: "s", hair: "h", hair_color: "h", eyes: "e", mouth: "m", accessory: "a", accessory_color: "a" };
  // face (skin) covers hair.back and accessory.back
  assert.deepEqual(rgbaAt(Avatar.renderPixels(d, lib), 0, 0), [1, 1, 1, 255]);
  // eyes over face, mouth over eyes, hair.front over mouth, accessory.front over hair.front
  const set = (cat, layer, ch) => {
    const l = clone(lib);
    l.parts[cat][Object.keys(l.parts[cat])[0]][layer] = row(ch);
    return l;
  };
  assert.deepEqual(rgbaAt(Avatar.renderPixels(d, set("eyes", "front", "w")), 0, 0), [12, 12, 12, 255]);
  const l2 = set("eyes", "front", "w");
  l2.parts.mouth.m.front = row("m");
  assert.deepEqual(rgbaAt(Avatar.renderPixels(d, l2), 0, 0), [14, 14, 14, 255]);
  l2.parts.hair.h.front = row("b");
  assert.deepEqual(rgbaAt(Avatar.renderPixels(d, l2), 0, 0), [2, 2, 2, 255]);
  l2.parts.accessory.a.front = row("o");
  assert.deepEqual(rgbaAt(Avatar.renderPixels(d, l2), 0, 0), [10, 10, 10, 255]);
  // transparent pixels stay transparent
  assert.deepEqual(rgbaAt(Avatar.renderPixels(d, lib), 5, 5), [0, 0, 0, 0]);
});

test("accessory \"none\" draws no accessory; a palette without s / l falls back to the base colour", withParts(() => {
  const withAcc = Avatar.pixelHash(Avatar.renderPixels({ ...DESC, accessory: "a01" }));
  const none = Avatar.pixelHash(Avatar.renderPixels({ ...DESC, accessory: "none", accessory_color: "" }));
  assert.notEqual(withAcc, none);
  const lib = clone(PARTS);
  lib.palettes.skin.s01 = { b: "#808080" }; // s / l missing
  const px = Avatar.renderPixels(DESC, lib);
  assert.ok(px, "still renders");
  const colours = new Set();
  for (let i = 0; i < 400; i++) if (px.data[i * 4 + 3]) colours.add(Array.from(px.data.slice(i * 4, i * 4 + 3)).join());
  assert.ok(colours.has("128,128,128"));
}));

test("a description that cannot be drawn renders nothing (null) instead of throwing", withParts(() => {
  assert.equal(Avatar.renderPixels(null), null);
  assert.equal(Avatar.renderPixels({ v: 1 }), null);
  assert.equal(Avatar.renderPixels({ ...DESC, face: "f99" }), null); // unknown face
  assert.equal(Avatar.renderPixels({ ...DESC, skin: "s99" }), null); // unknown skin
  // an unknown eye sprite just leaves the eyes out
  assert.ok(Avatar.renderPixels({ ...DESC, eyes: "e99" }));
}));

// ---- fallbacks ------------------------------------------------------------------------------
test("without a library every avatar is a flat circle with the seat number (no error, deterministic)", () => {
  assert.equal(Avatar.hasParts(), false);
  const a = Avatar.dataUrl(DESC, { seat: 7, label: "N7" });
  const b = Avatar.dataUrl(DESC, { seat: 7, label: "N7" });
  assert.equal(a, b);
  assert.match(a, /^data:image\/svg\+xml/);
  const svg = decodeURIComponent(a.split(",")[1]);
  assert.match(svg, /<circle /);
  assert.match(svg, />7<\/text>/);
  assert.notEqual(Avatar.dataUrl(null, { seat: 8, label: "N8" }), a); // another seat: another colour / number
  // two-digit seats and non-numeric labels
  assert.match(decodeURIComponent(Avatar.dataUrl(null, { seat: 12, label: "N12" }).split(",")[1]), />12<\/text>/);
  assert.match(decodeURIComponent(Avatar.dataUrl(null, { label: "ab" }).split(",")[1]), />ab<\/text>/);
  // markup in a label can never reach the SVG
  const evil = decodeURIComponent(Avatar.dataUrl(null, { label: '"><script>alert(1)</script>' }).split(",")[1]);
  assert.ok(!evil.includes("<script"));
});

test("a missing description falls back to the seat's default one (needs the library); seats get different defaults", withParts(() => {
  assert.ok(Avatar.defaultDescription(3)); // exists
  assert.deepEqual(Avatar.defaultDescription(3), Avatar.defaultDescription(3)); // deterministic
  const url = Avatar.dataUrl(null, { seat: 3 });
  assert.match(url, /^data:image\/png/);
  assert.equal(url, Avatar.dataUrl(Avatar.defaultDescription(3), { seat: 3 }));
  assert.notEqual(Avatar.dataUrl(null, { seat: 3 }), Avatar.dataUrl(null, { seat: 4 }));
  // an unusable description behaves like a missing one
  assert.equal(Avatar.dataUrl({ v: 1, face: "f01" }, { seat: 3 }), url);
  assert.deepEqual(Avatar.defaultDescription(3, null), Avatar.defaultDescription(3)); // uses the installed library
}));

test("changing the library clears the render cache", () => {
  Avatar.setParts(clone(PARTS));
  const before = Avatar.dataUrl(DESC, { seat: 1 });
  const lib = clone(PARTS);
  lib.palettes.skin.s01 = { b: "#ff0000", s: "#aa0000", l: "#ff8888" };
  Avatar.setParts(lib);
  const after = Avatar.dataUrl(DESC, { seat: 1 });
  assert.notEqual(before, after);
  Avatar.setParts(null);
});

// ---- fixed icons ----------------------------------------------------------------------------------
test("the moderator and the engine have their own icons, independent of the library", () => {
  assert.equal(Avatar.hasParts(), false);
  const mod = Avatar.moderatorUrl();
  const sys = Avatar.systemUrl();
  assert.match(mod, /^data:image\/png;base64,/);
  assert.match(sys, /^data:image\/png;base64,/);
  assert.notEqual(mod, sys);
  assert.equal(Avatar.urlFor("moderator", undefined, null), mod);
  assert.equal(Avatar.urlFor("engine", undefined, null), sys);
  assert.notEqual(Avatar.urlFor("N3", 3, null), mod);
});

test("the moderator draws its random avatar when it has one; the fixed top hat is only the fallback", withParts(() => {
  const hat = Avatar.moderatorUrl();
  // no description (an older backend): the fixed icon
  assert.equal(Avatar.urlFor("moderator", undefined, null), hat);
  assert.equal(Avatar.urlFor("moderator", undefined, { v: 1 }), hat); // a description that cannot describe an avatar
  // a description (v0.12.1): rendered from the library, exactly like a player with the same description
  const own = Avatar.urlFor("moderator", undefined, DESC);
  assert.notEqual(own, hat);
  assert.match(own, /^data:image\/png;base64,/);
  assert.equal(own, Avatar.dataUrl(DESC, { seat: 0, label: "moderator" }));
  assert.equal(own, Avatar.urlFor("N5", 5, DESC)); // the picture depends on the description only
  // a different description is a different picture; the same one is cached
  const other = { ...DESC, hair: "h01", eyes: "e01" };
  assert.notEqual(Avatar.urlFor("moderator", undefined, other), own);
  assert.equal(Avatar.urlFor("moderator", undefined, DESC), own);
  // the markup keeps the moderator's class (the chip's styling) and never echoes the description
  const html = Avatar.imgHtml("moderator", undefined, DESC, { size: 20, className: "mod-chip-avatar" });
  assert.match(html, /class="avatar-img avatar-moderator mod-chip-avatar"/);
  assert.ok(html.includes(own) && !html.includes("f01"));
}));

test("without the library the moderator keeps its top hat even when it has a description", () => {
  assert.equal(Avatar.hasParts(), false);
  assert.equal(Avatar.urlFor("moderator", undefined, DESC), Avatar.moderatorUrl());
});

// ---- PNG / scaling -----------------------------------------------------------------------------------
test("scalePixels enlarges by nearest neighbour", () => {
  const px = { width: 2, height: 1, data: new Uint8ClampedArray([1, 2, 3, 255, 9, 8, 7, 255]) };
  const big = Avatar.scalePixels(px, 3);
  assert.equal(big.width, 6);
  assert.equal(big.height, 3);
  assert.deepEqual(rgbaAt(big, 0, 0), [1, 2, 3, 255]);
  assert.deepEqual(rgbaAt(big, 2, 2), [1, 2, 3, 255]);
  assert.deepEqual(rgbaAt(big, 3, 0), [9, 8, 7, 255]);
  assert.deepEqual(rgbaAt(big, 5, 2), [9, 8, 7, 255]);
});

test("the PNG made without a canvas is a valid image whose pixels equal the drawing", withParts(() => {
  const px = Avatar.scalePixels(Avatar.renderPixels(DESC), 4);
  const png = Buffer.from(Avatar.encodePng(px));
  assert.deepEqual(Array.from(png.slice(0, 8)), [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  assert.equal(png.readUInt32BE(16), 80);
  assert.equal(png.readUInt32BE(20), 80);
  // chunks: IHDR, IDAT, IEND; every CRC is right
  let off = 8;
  const types = [];
  let idat = Buffer.alloc(0);
  while (off < png.length) {
    const len = png.readUInt32BE(off);
    const type = png.toString("ascii", off + 4, off + 8);
    const body = png.slice(off + 8, off + 8 + len);
    const crc = png.readUInt32BE(off + 8 + len);
    assert.equal(crc, zlib.crc32 ? zlib.crc32(png.slice(off + 4, off + 8 + len)) : crc, `crc of ${type}`);
    types.push(type);
    if (type === "IDAT") idat = Buffer.concat([idat, body]);
    off += 12 + len;
  }
  assert.deepEqual(types, ["IHDR", "IDAT", "IEND"]);
  const raw = zlib.inflateSync(idat);
  assert.equal(raw.length, 80 * (80 * 4 + 1));
  for (let y = 0; y < 80; y += 7) {
    assert.equal(raw[y * 321], 0, "filter none");
    for (let x = 0; x < 80; x += 5) {
      const got = Array.from(raw.slice(y * 321 + 1 + x * 4, y * 321 + 1 + x * 4 + 4));
      assert.deepEqual(got, rgbaAt(px, x, y), `(${x},${y})`);
    }
  }
}));

test("base64 matches Buffer's", () => {
  for (const n of [0, 1, 2, 3, 4, 5, 255]) {
    const bytes = Uint8Array.from({ length: n }, (_, i) => (i * 37 + 11) & 255);
    assert.equal(Avatar.base64(bytes), Buffer.from(bytes).toString("base64"), `${n} bytes`);
  }
});

// ---- markup ----------------------------------------------------------------------------------------------
test("imgHtml emits one <img> with a data URL, a clamped size and no echo of its inputs", withParts(() => {
  const html = Avatar.imgHtml("N3", 3, F.avatarOf(3), { size: 40, className: "x y" });
  assert.match(html, /^<img class="avatar-img x y" src="data:image\/png;base64,[A-Za-z0-9+/=]+" width="40" height="40" alt="" draggable="false" aria-hidden="true">$/);
  assert.ok(Avatar.imgHtml("N3", 3, null, { size: 5 }).includes('width="8"'));
  assert.ok(Avatar.imgHtml("N3", 3, null, { size: 9999 }).includes('width="160"'));
  assert.ok(Avatar.imgHtml("N3", 3, null, { size: "x\"y" }).includes('width="40"'));
  const evil = Avatar.imgHtml('"><script>alert(1)</script>', 1, null, { className: '"><script>' });
  assert.ok(!evil.includes("<script"));
  assert.match(Avatar.imgHtml("moderator"), /avatar-moderator/);
  assert.match(Avatar.imgHtml("engine"), /avatar-system/);
}));

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
