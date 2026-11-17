/* ==========================================================================
   Arena-Agents v0.12 — deterministic minimal glyphs for players
   (pure helpers, no DOM access; exposes window.ArenaAvatar and module.exports)

   A glyph is the combination  shape × colour × texture  derived ONLY from the
   seat (or, for ids that carry no seat number, a hash of the id):

     shape    6 simple silhouettes   (circle / square / triangle / diamond /
                                      hexagon / cross)
     colour   12 well separated hues (consecutive seats are far apart on the
                                      colour wheel)
     texture  4 fills                (solid / stripes / split / dots), switching
                                      every 6 seats so (shape, colour, texture)
                                      is unique for the first 24 seats

   The moderator always gets a star in neutral ink. The result is rendered by
   CSS only (class names + the --gh custom property): no images, no SVG ids.
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

  const SHAPES = ["circle", "square", "triangle", "diamond", "hexagon", "cross"];
  const TEXTURES = ["solid", "stripes", "split", "dots"];
  // 12 hues; the seat order was picked so neighbours never look alike.
  const HUES = [212, 4, 142, 270, 178, 330, 84, 22, 240, 196, 300, 54];
  const SEATS_PER_TEXTURE = 6;

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
   * the id ("p03" -> 2) -> hash of the id.
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
    return hashString(id) % (SHAPES.length * HUES.length * TEXTURES.length);
  }

  /**
   * glyphSpec(id, seat) -> { shape, hue, texture, key, index, moderator, system }
   * Same inputs always give the same output; `key` is a compact identity string.
   */
  function glyphSpec(id, seat) {
    const sid = String(id === undefined || id === null ? "" : id);
    if (sid === MODERATOR_ID) {
      return { shape: "star", hue: 0, texture: "solid", key: "star-ink-solid", index: -1, moderator: true, system: false };
    }
    if (sid === ENGINE_ID) {
      return { shape: "gear", hue: 0, texture: "solid", key: "gear-ink-solid", index: -2, moderator: false, system: true };
    }
    const i = seatIndex(sid, seat);
    const shape = SHAPES[i % SHAPES.length];
    const hue = HUES[i % HUES.length];
    const texture = TEXTURES[Math.floor(i / SEATS_PER_TEXTURE) % TEXTURES.length];
    return { shape, hue, texture, key: `${shape}-${hue}-${texture}`, index: i, moderator: false, system: false };
  }

  function glyphClass(spec) {
    const kind = spec.moderator ? " glyph-moderator" : spec.system ? " glyph-system" : "";
    return `glyph glyph-s-${spec.shape} glyph-t-${spec.texture}${kind}`;
  }

  /**
   * HTML for one glyph. Only numbers and whitelisted class names are emitted, so
   * the result is safe to splice into markup. opts.size is a pixel count (clamped).
   */
  function glyphHtml(id, seat, opts) {
    const spec = glyphSpec(id, seat);
    const o = opts || {};
    const size = Number(o.size);
    const sizeStyle =
      Number.isFinite(size) && size >= 8 && size <= 64 ? `;--gs:${Math.round(size)}px` : "";
    return (
      `<span class="${glyphClass(spec)}" style="--gh:${spec.hue}${sizeStyle}"` +
      ` data-glyph="${spec.key}" aria-hidden="true"></span>`
    );
  }

  return {
    MODERATOR_ID,
    ENGINE_ID,
    SHAPES,
    TEXTURES,
    HUES,
    SEATS_PER_TEXTURE,
    hashString,
    seatIndex,
    glyphSpec,
    glyphClass,
    glyphHtml,
  };
});
