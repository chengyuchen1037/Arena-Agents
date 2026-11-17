/* ==========================================================================
   Arena-Agents v0.12.1 — pixel avatars (E15)
   Exposes window.ArenaAvatar and module.exports. No DOM is required to compute
   pixels (the unit tests run under node); a <canvas> is only used, when the
   browser has one, to turn the pixels into a small PNG data URL.

   A player's avatar is a *description* (docs/v0.12-contracts.md §10.4):
     {"v":1,"face":"f03","skin":"s05","hair":"h12","hair_color":"c07","eyes":"e04",
      "mouth":"m02","accessory":"a09","accessory_color":"k03"}
   and is drawn from the sprite library static/avatar/parts.json (20x20, layered):
     hair.back -> accessory.back -> face -> eyes -> mouth -> hair.front -> accessory.front
   '.' transparent, 'o' outline, 'b/s/l' base/shade/light of the layer's palette,
   'x' blush, 'w' eye white, 'p' pupil, 'm' mouth, 't' teeth.

   The same description always gives the same pixels (and the same data URL); a
   description is rendered once per scale and cached.  When the library is missing
   or broken every call falls back to a flat coloured circle with the seat number,
   so the page never breaks.  The moderator and the engine have fixed icons that do
   not depend on the library.
   ========================================================================== */

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.ArenaAvatar = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const MODERATOR_ID = "moderator";
  const ENGINE_ID = "engine";
  const SIZE = 20;
  const DEFAULT_SCALE = 4; // 20 px * 4 = 80 px source; CSS shows it at 40 / 60 / 80 px (integer steps)
  const MIN_SCALE = 3;
  const MAX_SCALE = 6;
  const DESC_VERSION = 1;
  // category -> description field, in drawing-relevant order
  const FIELDS = ["face", "skin", "hair", "hair_color", "eyes", "mouth", "accessory", "accessory_color"];
  const REQUIRED_FIELDS = ["face", "skin", "hair", "hair_color", "eyes", "mouth"];
  const CATEGORIES = ["face", "hair", "eyes", "mouth", "accessory"];
  const PALETTE_OF = { skin: "skin", hair_color: "hair_color", accessory_color: "accessory_color" };
  // fallback circle colours: 12 well separated hues (consecutive seats are far apart)
  const HUES = [212, 4, 142, 270, 178, 330, 84, 22, 240, 196, 300, 54];

  // -------------------------------------------------------------------------
  // small helpers
  // -------------------------------------------------------------------------

  /** FNV-1a (32 bit, unsigned): stable across engines / runs. */
  function hashString(value) {
    const str = String(value === undefined || value === null ? "" : value);
    let h = 0x811c9dc5;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return h >>> 0;
  }

  /**
   * 0-based slot for a player. Priority: explicit seat number -> number inside
   * the id ("N3" -> 2) -> hash of the id.
   */
  function seatIndex(id, seat) {
    const s = Number(seat);
    if (seat !== undefined && seat !== null && seat !== "" && Number.isInteger(s) && s >= 1) {
      return s - 1;
    }
    const m = /^[A-Za-z_-]*0*(\d+)$/.exec(String(id === undefined || id === null ? "" : id));
    if (m) {
      const n = Number(m[1]);
      if (Number.isInteger(n) && n >= 1) return n - 1;
    }
    return hashString(id) % 997;
  }

  const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

  function clampInt(value, min, max, fallback) {
    const n = Math.round(Number(value));
    if (!Number.isFinite(n)) return fallback;
    return Math.min(max, Math.max(min, n));
  }

  function hexToRgb(hex) {
    const m = /^#?([0-9a-f]{6}|[0-9a-f]{3})$/i.exec(String(hex || "").trim());
    if (!m) return null;
    let h = m[1];
    if (h.length === 3) h = h.split("").map((c) => c + c).join("");
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }

  // -------------------------------------------------------------------------
  // sprite library (static/avatar/parts.json)
  // -------------------------------------------------------------------------

  let parts = null;
  const urlCache = new Map(); // "key@scale" -> data URL
  const listeners = [];

  function isGrid(grid, size) {
    return (
      Array.isArray(grid) &&
      grid.length === size &&
      grid.every((line) => typeof line === "string" && line.length === size)
    );
  }

  /** Minimal structural check; returns {ok, errors}. Individual broken sprites are skipped at draw time. */
  function validateParts(lib) {
    const errors = [];
    if (!isObject(lib)) return { ok: false, errors: ["parts: not an object"] };
    const size = Number(lib.size) || SIZE;
    if (size !== SIZE) errors.push(`parts.size: ${lib.size} (expected ${SIZE})`);
    const pal = lib.palettes;
    if (!isObject(pal)) errors.push("parts.palettes");
    else {
      for (const k of ["skin", "hair_color", "accessory_color"]) {
        if (!isObject(pal[k])) errors.push(`parts.palettes.${k}`);
      }
      if (!isObject(pal.fixed)) errors.push("parts.palettes.fixed");
    }
    if (!isObject(lib.parts)) errors.push("parts.parts");
    else {
      for (const cat of CATEGORIES) {
        if (!isObject(lib.parts[cat]) || Object.keys(lib.parts[cat]).length === 0) {
          errors.push(`parts.parts.${cat}`);
        }
      }
    }
    return { ok: errors.length === 0, errors };
  }

  /** Install (or clear, with null) the library; clears the render cache and tells the listeners. */
  function setParts(lib) {
    const ok = validateParts(lib).ok;
    parts = ok ? lib : null;
    urlCache.clear();
    for (const fn of listeners.slice()) {
      try {
        fn(parts !== null);
      } catch {
        /* a listener must never break avatar loading */
      }
    }
    return parts !== null;
  }

  const getParts = () => parts;
  const hasParts = () => parts !== null;

  /** Subscribe to "library changed" (fired by setParts); returns an unsubscribe function. */
  function onChange(fn) {
    listeners.push(fn);
    return () => {
      const i = listeners.indexOf(fn);
      if (i >= 0) listeners.splice(i, 1);
    };
  }

  /** Fetch + install the library. Never rejects: a missing / broken file simply means "fallback". */
  function loadParts(fetchImpl, url) {
    return Promise.resolve()
      .then(() => fetchImpl(url))
      .then((resp) => (resp && resp.ok ? resp.json() : null))
      .then((lib) => setParts(lib))
      .catch(() => setParts(null));
  }

  /** Ids of one category, sorted (so default descriptions do not depend on file order). */
  function idsOf(lib, category) {
    const group = category in PALETTE_OF ? lib.palettes[PALETTE_OF[category]] : lib.parts[category];
    return isObject(group) ? Object.keys(group).sort() : [];
  }

  // -------------------------------------------------------------------------
  // descriptions
  // -------------------------------------------------------------------------

  /** Canonical description, or null when it cannot describe an avatar. */
  function normalizeDescription(desc) {
    if (!isObject(desc)) return null;
    const out = { v: DESC_VERSION };
    for (const f of FIELDS) {
      const v = desc[f];
      out[f] = typeof v === "string" || typeof v === "number" ? String(v) : "";
    }
    if (!REQUIRED_FIELDS.every((f) => out[f])) return null;
    if (!out.accessory) out.accessory = "none";
    return out;
  }

  /** Stable identity string of a description (used as the cache key and in tests). */
  function descriptionKey(desc) {
    const d = normalizeDescription(desc);
    return d ? [d.v, ...FIELDS.map((f) => d[f])].join("|") : "";
  }

  /** The description a seat gets when the server sent none (deterministic, library based). */
  function defaultDescription(seat, lib) {
    const L = lib || parts;
    if (!L) return null;
    const n = Math.max(1, Math.round(Number(seat)) || 1);
    const desc = { v: DESC_VERSION };
    for (const f of FIELDS) {
      const ids = idsOf(L, f);
      if (!ids.length) {
        if (f === "accessory" || f === "accessory_color") {
          desc[f] = f === "accessory" ? "none" : "";
          continue;
        }
        return null;
      }
      // a hash per (field, seat): neighbouring seats do not share a face, a skin, a hair ... just by
      // arithmetic accident, even in a tiny library
      desc[f] = ids[hashString(`${f}:${n}`) % ids.length];
    }
    return normalizeDescription(desc);
  }

  // -------------------------------------------------------------------------
  // pixels
  // -------------------------------------------------------------------------

  function paintLayer(data, size, grid, palette, fixed) {
    if (!isGrid(grid, size)) return;
    for (let r = 0; r < size; r++) {
      const line = grid[r];
      for (let c = 0; c < size; c++) {
        const ch = line[c];
        if (ch === ".") continue;
        let rgb = null;
        if (ch === "b" || ch === "s" || ch === "l") rgb = palette ? palette[ch] : null;
        else if (ch === "o") rgb = fixed.outline;
        else if (ch === "x") rgb = fixed.blush;
        else if (ch === "w") rgb = fixed.eye_white;
        else if (ch === "p") rgb = fixed.pupil;
        else if (ch === "m") rgb = fixed.mouth;
        else if (ch === "t") rgb = fixed.tooth;
        if (!rgb) continue;
        const i = (r * size + c) * 4;
        data[i] = rgb[0];
        data[i + 1] = rgb[1];
        data[i + 2] = rgb[2];
        data[i + 3] = 255;
      }
    }
  }

  function paletteRgb(entry) {
    if (!isObject(entry)) return null;
    const out = {};
    for (const k of ["b", "s", "l"]) {
      const rgb = hexToRgb(entry[k]);
      if (rgb) out[k] = rgb;
    }
    return out.b ? { b: out.b, s: out.s || out.b, l: out.l || out.b } : null;
  }

  /**
   * Draw a description. Returns {width, height, data: Uint8ClampedArray(RGBA)} or null when the library
   * is missing or lacks the face / skin of the description. Pure and deterministic.
   */
  function renderPixels(desc, lib) {
    const L = lib || parts;
    const d = normalizeDescription(desc);
    if (!L || !d || !validateParts(L).ok) return null;
    const size = Number(L.size) || SIZE;
    const pal = L.palettes;
    const fixed = {};
    for (const k of ["outline", "blush", "eye_white", "pupil", "mouth", "tooth"]) {
      fixed[k] = hexToRgb(pal.fixed[k]);
    }
    const sprite = (cat, id) => (isObject(L.parts[cat]) && isObject(L.parts[cat][id]) ? L.parts[cat][id] : null);
    const face = sprite("face", d.face);
    const skin = paletteRgb(pal.skin[d.skin]);
    if (!face || !skin) return null;
    const hairColor = paletteRgb(pal.hair_color[d.hair_color]);
    const hair = sprite("hair", d.hair);
    const eyes = sprite("eyes", d.eyes);
    const mouth = sprite("mouth", d.mouth);
    const acc = d.accessory === "none" ? null : sprite("accessory", d.accessory);
    const accColor = acc ? paletteRgb((pal.accessory_color || {})[d.accessory_color]) : null;

    const data = new Uint8ClampedArray(size * size * 4);
    const layers = [
      [hair && hair.back, hairColor],
      [acc && acc.back, accColor],
      [face.front, skin],
      [eyes && eyes.front, skin],
      [mouth && mouth.front, skin],
      [hair && hair.front, hairColor],
      [acc && acc.front, accColor],
    ];
    for (const [grid, palette] of layers) paintLayer(data, size, grid, palette, fixed);
    return { width: size, height: size, data };
  }

  /** Nearest-neighbour enlargement (integer factor). */
  function scalePixels(px, scale) {
    const k = clampInt(scale, 1, 16, DEFAULT_SCALE);
    const w = px.width * k;
    const h = px.height * k;
    const out = new Uint8ClampedArray(w * h * 4);
    for (let y = 0; y < h; y++) {
      const sy = Math.floor(y / k);
      for (let x = 0; x < w; x++) {
        const si = (sy * px.width + Math.floor(x / k)) * 4;
        const di = (y * w + x) * 4;
        out[di] = px.data[si];
        out[di + 1] = px.data[si + 1];
        out[di + 2] = px.data[si + 2];
        out[di + 3] = px.data[si + 3];
      }
    }
    return { width: w, height: h, data: out };
  }

  /** A short hex fingerprint of a pixel buffer (tests: "same description -> identical pixels"). */
  function pixelHash(px) {
    if (!px) return "";
    let h = 0x811c9dc5;
    const d = px.data;
    for (let i = 0; i < d.length; i++) {
      h ^= d[i];
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return `${px.width}x${px.height}:${(h >>> 0).toString(16).padStart(8, "0")}`;
  }

  // -------------------------------------------------------------------------
  // PNG (no canvas needed: uncompressed deflate blocks) + base64
  // -------------------------------------------------------------------------

  let crcTable = null;
  function crc32(bytes, start, end) {
    if (!crcTable) {
      crcTable = new Uint32Array(256);
      for (let n = 0; n < 256; n++) {
        let c = n;
        for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
        crcTable[n] = c >>> 0;
      }
    }
    let c = 0xffffffff;
    for (let i = start; i < end; i++) c = crcTable[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  }

  function adler32(bytes) {
    let a = 1;
    let b = 0;
    for (let i = 0; i < bytes.length; i++) {
      a = (a + bytes[i]) % 65521;
      b = (b + a) % 65521;
    }
    return ((b << 16) | a) >>> 0;
  }

  function u32(n) {
    return [(n >>> 24) & 0xff, (n >>> 16) & 0xff, (n >>> 8) & 0xff, n & 0xff];
  }

  /** RGBA pixel buffer -> PNG bytes (stored deflate blocks: big, but dependency free and deterministic). */
  function encodePng(px) {
    const { width, height, data } = px;
    const stride = width * 4;
    const raw = new Uint8Array((stride + 1) * height);
    for (let y = 0; y < height; y++) {
      raw[y * (stride + 1)] = 0; // filter: none
      raw.set(data.subarray(y * stride, (y + 1) * stride), y * (stride + 1) + 1);
    }
    const blocks = [0x78, 0x01];
    for (let pos = 0; pos < raw.length || pos === 0; pos += 65535) {
      const len = Math.min(65535, raw.length - pos);
      const last = pos + len >= raw.length ? 1 : 0;
      blocks.push(last, len & 0xff, (len >>> 8) & 0xff, ~len & 0xff, (~len >>> 8) & 0xff);
      for (let i = 0; i < len; i++) blocks.push(raw[pos + i]);
      if (last) break;
    }
    blocks.push(...u32(adler32(raw)));
    const chunk = (type, body) => {
      const buf = new Uint8Array(12 + body.length);
      buf.set(u32(body.length), 0);
      for (let i = 0; i < 4; i++) buf[4 + i] = type.charCodeAt(i);
      buf.set(body, 8);
      buf.set(u32(crc32(buf, 4, 8 + body.length)), 8 + body.length);
      return buf;
    };
    const ihdr = new Uint8Array([...u32(width), ...u32(height), 8, 6, 0, 0, 0]);
    const parts2 = [
      new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
      chunk("IHDR", ihdr),
      chunk("IDAT", Uint8Array.from(blocks)),
      chunk("IEND", new Uint8Array(0)),
    ];
    const total = parts2.reduce((n, p) => n + p.length, 0);
    const out = new Uint8Array(total);
    let off = 0;
    for (const p of parts2) {
      out.set(p, off);
      off += p.length;
    }
    return out;
  }

  const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  function base64(bytes) {
    let out = "";
    for (let i = 0; i < bytes.length; i += 3) {
      const a = bytes[i];
      const b = i + 1 < bytes.length ? bytes[i + 1] : 0;
      const c = i + 2 < bytes.length ? bytes[i + 2] : 0;
      out += B64[a >> 2] + B64[((a & 3) << 4) | (b >> 4)];
      out += i + 1 < bytes.length ? B64[((b & 15) << 2) | (c >> 6)] : "=";
      out += i + 2 < bytes.length ? B64[c & 63] : "=";
    }
    return out;
  }

  /** A real <canvas> (compressed PNG, much smaller); null when there is none (node, odd browsers). */
  function canvasDataUrl(px) {
    try {
      if (typeof document === "undefined" || !document || typeof document.createElement !== "function") return null;
      const canvas = document.createElement("canvas");
      if (!canvas || typeof canvas.getContext !== "function") return null;
      canvas.width = px.width;
      canvas.height = px.height;
      const ctx = canvas.getContext("2d");
      if (!ctx || typeof ctx.putImageData !== "function" || typeof ctx.createImageData !== "function") return null;
      const img = ctx.createImageData(px.width, px.height);
      img.data.set(px.data);
      ctx.putImageData(img, 0, 0);
      const url = canvas.toDataURL("image/png");
      return typeof url === "string" && url.startsWith("data:image/png") ? url : null;
    } catch {
      return null;
    }
  }

  function pngDataUrl(px) {
    return `data:image/png;base64,${base64(encodePng(px))}`;
  }

  // -------------------------------------------------------------------------
  // fixed icons: moderator (top hat) and engine (gear); fallback circle
  // -------------------------------------------------------------------------

  const MOD_PALETTE = {
    o: "#2a1b3d",
    h: "#3a3a5c",
    H: "#5a5a82",
    r: "#d94a4a",
    k: "#f6cfa5",
    K: "#d9a77a",
    e: "#24243e",
    x: "#ff8fa3",
    m: "#8a2b3b",
  };
  const MOD_ROWS = [
    "....................",
    ".....oooooooooo.....",
    ".....ohhhhhhhho.....",
    ".....ohHHhhhhho.....",
    ".....ohHhhhhhho.....",
    ".....orrrrrrrro.....",
    ".....orrrrrrrro.....",
    "..oooooooooooooooo..",
    "..ohhhhhhhhhhhhhho..",
    "..oooooooooooooooo..",
    "....okkkkkkkkkko....",
    "....okkkkkkkkkko....",
    "....okeekkkkeeko....",
    "....okeekkkkeeko....",
    "....okxkkkkkkxko....",
    "....okkmkkkkmkko....",
    "....okkkmmmmkkko....",
    "....oKKKKKKKKKKo....",
    ".....oooooooooo.....",
    "....................",
  ];

  function pixelsFromRows(rowsList, palette) {
    const size = rowsList.length;
    const data = new Uint8ClampedArray(size * size * 4);
    for (let r = 0; r < size; r++) {
      for (let c = 0; c < size; c++) {
        const rgb = hexToRgb(palette[rowsList[r][c]]);
        if (!rgb) continue;
        const i = (r * size + c) * 4;
        data[i] = rgb[0];
        data[i + 1] = rgb[1];
        data[i + 2] = rgb[2];
        data[i + 3] = 255;
      }
    }
    return { width: size, height: size, data };
  }

  /** A procedural 8-tooth gear (20x20) for system notices. */
  function gearPixels() {
    const size = SIZE;
    const mask = [];
    for (let r = 0; r < size; r++) {
      mask.push([]);
      for (let c = 0; c < size; c++) {
        const dx = c - 9.5;
        const dy = r - 9.5;
        const d = Math.hypot(dx, dy);
        const ang = Math.atan2(dy, dx);
        const tooth = Math.cos(ang * 8) > 0.15;
        mask[r].push(d >= 2.7 && (d <= 6.4 || (d <= 8.6 && tooth)));
      }
    }
    const data = new Uint8ClampedArray(size * size * 4);
    const col = { o: hexToRgb("#2a1b3d"), b: hexToRgb("#a7b0c4"), s: hexToRgb("#7d879f") };
    for (let r = 0; r < size; r++) {
      for (let c = 0; c < size; c++) {
        if (!mask[r][c]) continue;
        let edge = false;
        for (const [dr, dc] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
          const rr = r + dr;
          const cc = c + dc;
          if (rr < 0 || cc < 0 || rr >= size || cc >= size || !mask[rr][cc]) edge = true;
        }
        const rgb = edge ? col.o : r > 11 ? col.s : col.b;
        const i = (r * size + c) * 4;
        data[i] = rgb[0];
        data[i + 1] = rgb[1];
        data[i + 2] = rgb[2];
        data[i + 3] = 255;
      }
    }
    return { width: size, height: size, data };
  }

  /** Flat circle + seat number (library missing / broken): an inline SVG data URL. */
  function fallbackUrl(seat, label) {
    const idx = seatIndex(label, seat);
    const hue = HUES[idx % HUES.length];
    const n = Number(seat);
    const text = Number.isInteger(n) && n > 0 ? String(n) : String(label === undefined || label === null ? "" : label).slice(0, 2);
    const safe = text.replace(/[^0-9A-Za-z\u4e00-\u9fff]/g, "");
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" width="40" height="40">` +
      `<circle cx="20" cy="20" r="18.5" fill="hsl(${hue} 62% 50%)" stroke="#2a1b3d" stroke-width="2"/>` +
      `<text x="20" y="26.5" text-anchor="middle" font-family="sans-serif" font-size="${safe.length > 1 ? 16 : 20}" ` +
      `font-weight="700" fill="#ffffff">${safe}</text></svg>`;
    return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  }

  function cachedUrl(key, make) {
    if (urlCache.has(key)) return urlCache.get(key);
    const url = make();
    urlCache.set(key, url);
    return url;
  }

  function urlOfPixels(px, scale) {
    const big = scalePixels(px, scale);
    return canvasDataUrl(big) || pngDataUrl(big);
  }

  /** Data URL of the moderator's fixed icon (does not need the library). */
  function moderatorUrl(scale) {
    const k = clampInt(scale, MIN_SCALE, MAX_SCALE, DEFAULT_SCALE);
    return cachedUrl(`mod@${k}`, () => urlOfPixels(pixelsFromRows(MOD_ROWS, MOD_PALETTE), k));
  }

  function systemUrl(scale) {
    const k = clampInt(scale, MIN_SCALE, MAX_SCALE, DEFAULT_SCALE);
    return cachedUrl(`sys@${k}`, () => urlOfPixels(gearPixels(), k));
  }

  /**
   * Data URL for a player's avatar. `opts`: {scale 3..6 (default 4), seat, label}.
   * - library missing -> flat circle + seat number;
   * - description missing -> the seat's default description (needs the library).
   */
  function dataUrl(desc, opts) {
    const o = opts || {};
    const k = clampInt(o.scale, MIN_SCALE, MAX_SCALE, DEFAULT_SCALE);
    if (!parts) return cachedUrl(`fb|${o.seat}|${o.label}`, () => fallbackUrl(o.seat, o.label));
    const d = normalizeDescription(desc) || defaultDescription(o.seat);
    if (!d) return cachedUrl(`fb|${o.seat}|${o.label}`, () => fallbackUrl(o.seat, o.label));
    return cachedUrl(`${descriptionKey(d)}@${k}`, () => {
      const px = renderPixels(d);
      return px ? urlOfPixels(px, k) : fallbackUrl(o.seat, o.label);
    });
  }

  /** The URL for any actor id (moderator / engine / player). */
  function urlFor(id, seat, desc, opts) {
    if (id === MODERATOR_ID) {
      // v0.12.1: the moderator has a random avatar of its own; the fixed top hat is only the fallback for
      // "no description yet" (older backend) or "no parts library"
      if (parts && normalizeDescription(desc)) return dataUrl(desc, { ...(opts || {}), seat: 0, label: id });
      return moderatorUrl((opts || {}).scale);
    }
    if (id === ENGINE_ID) return systemUrl((opts || {}).scale);
    return dataUrl(desc, { ...(opts || {}), seat, label: id });
  }

  /**
   * <img> markup. Only numbers and a data URL we produced are emitted, so the result is safe to
   * splice into HTML. opts: {size px (8..160), className}.
   */
  function imgHtml(id, seat, desc, opts) {
    const o = opts || {};
    const size = clampInt(o.size, 8, 160, 40);
    const cls = String(o.className || "").replace(/[^A-Za-z0-9_ -]/g, "");
    const kind = id === MODERATOR_ID ? " avatar-moderator" : id === ENGINE_ID ? " avatar-system" : "";
    return (
      `<img class="avatar-img${kind}${cls ? ` ${cls}` : ""}" src="${urlFor(id, seat, desc, o)}"` +
      ` width="${size}" height="${size}" alt="" draggable="false" aria-hidden="true">`
    );
  }

  return {
    MODERATOR_ID,
    ENGINE_ID,
    SIZE,
    DEFAULT_SCALE,
    FIELDS,
    HUES,
    hashString,
    seatIndex,
    validateParts,
    setParts,
    getParts,
    hasParts,
    onChange,
    loadParts,
    idsOf,
    normalizeDescription,
    descriptionKey,
    defaultDescription,
    renderPixels,
    scalePixels,
    pixelHash,
    encodePng,
    base64,
    dataUrl,
    moderatorUrl,
    systemUrl,
    fallbackUrl,
    urlFor,
    imgHtml,
  };
});
