/* ==========================================================================
   Arena-Agents v0.12.1 — platform detection (plan §6.1)
   Exposes window.ArenaPlatform and module.exports.

   The UI adapts to the device it is opened on.  Three inputs are combined:
     - navigator.userAgentData.mobile, with the User-Agent string as the fallback
       (iPhone / Android "Mobile" = phone, iPad / Android tablets = tablet;
       an iPad that asks for the desktop site is recognised by maxTouchPoints);
     - matchMedia('(pointer: coarse)'): the *primary* pointer is a finger;
     - the viewport width (and height, for a phone held sideways).
   The result is written to <html>:
     data-platform    desktop | tablet | phone
     data-layout      wide | compact | phone   (what the stylesheet keys on:
                      compact = 6x2 player cards + avatar-only channel rail, below 1024 px
                      or on a touch tablet; phone = one pane at a time + bottom tab bar)
     data-input       touch | mouse
     data-orientation portrait | landscape
   and recomputed whenever the window is resized or rotated (synchronously, so a
   resize never paints a stale layout).  CSS owns the actual layout
   (static/css/style.css); app.js only reacts to `phone` (the "⋯" menu, the tab bar).

   A manual override is available for testing and for people who want another
   layout:  ?platform=phone | tablet | desktop  in the page URL.
   ========================================================================== */

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.ArenaPlatform = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const PLATFORMS = ["desktop", "tablet", "phone"];
  const PHONE_MAX_WIDTH = 767; // narrower than this is always the phone layout
  const TABLET_MAX_WIDTH = 1439; // touch devices up to this width get the tablet layout
  const COMPACT_MAX_WIDTH = 1023; // below 1024 px even a mouse-driven window gets the compact layout
  const PHONE_LANDSCAPE_MAX_HEIGHT = 500; // a phone held sideways is short, not narrow

  const PHONE_UA = /iPhone|iPod|Android.*Mobile|Windows Phone|webOS|BlackBerry|Opera Mini|IEMobile|Mobile.*Firefox|\bMobile\b/i;
  const TABLET_UA = /iPad|Tablet|PlayBook|Silk|Kindle|Nexus 7|Nexus 9|SM-T\d|KFAPWI/i;
  const ANDROID_UA = /Android/i;
  const IOS_UA = /iPhone|iPad|iPod/i;

  const num = (v, d = 0) => {
    const n = typeof v === "number" ? v : Number(v);
    return Number.isFinite(n) ? n : d;
  };

  /** "?platform=phone" in a query string -> "phone" (anything else -> null). */
  function parseOverride(search) {
    const m = /(?:^|[?&])platform=([A-Za-z]+)/.exec(String(search || ""));
    const v = m ? m[1].toLowerCase() : "";
    return PLATFORMS.includes(v) ? v : null;
  }

  /** The layout the stylesheet uses: "phone", "compact" (6x2 cards, avatar rail) or "wide". */
  function layoutOf(platform, width) {
    if (platform === "phone") return "phone";
    if (platform === "tablet" || num(width, 1280) <= COMPACT_MAX_WIDTH) return "compact";
    return "wide";
  }

  /**
   * Classify the device.
   * input: {userAgent, userAgentData: {mobile}, maxTouchPoints, platform (navigator.platform),
   *         coarsePointer, width, height, override}
   * -> {platform, mobileUa, phoneUa, tabletUa, touch, os, width, height, orientation, override}
   */
  function detect(input) {
    const i = input || {};
    const ua = String(i.userAgent || "");
    const width = Math.max(0, Math.round(num(i.width, 1280)));
    const height = Math.max(0, Math.round(num(i.height, 800)));
    const hintMobile = i.userAgentData && typeof i.userAgentData.mobile === "boolean" ? i.userAgentData.mobile : null;
    const touchPoints = num(i.maxTouchPoints, 0);

    // iPadOS 13+ sends a Mac UA: it is an iPad when the "Mac" has a touch screen.
    const ipadAsMac = /Macintosh|MacIntel/.test(`${ua} ${i.platform || ""}`) && touchPoints > 1 && !/iPhone/.test(ua);
    const tabletUa = TABLET_UA.test(ua) || ipadAsMac || (ANDROID_UA.test(ua) && !/Mobile/i.test(ua));
    const phoneUa = !tabletUa && (PHONE_UA.test(ua) || (hintMobile === true && !ipadAsMac));
    const mobileUa = hintMobile === true || phoneUa || tabletUa;
    const coarse = Boolean(i.coarsePointer);
    const touch = coarse || mobileUa || ipadAsMac;
    const os = IOS_UA.test(ua) || ipadAsMac ? "ios" : ANDROID_UA.test(ua) ? "android" : "other";
    const orientation = height > width ? "portrait" : "landscape";

    let platform;
    const override = PLATFORMS.includes(i.override) ? i.override : null;
    if (override) {
      platform = override;
    } else if (width <= PHONE_MAX_WIDTH || (phoneUa && Math.min(width, height) <= PHONE_LANDSCAPE_MAX_HEIGHT)) {
      platform = "phone";
    } else if ((tabletUa || coarse || mobileUa) && width <= TABLET_MAX_WIDTH) {
      platform = "tablet";
    } else {
      platform = "desktop";
    }
    const layout = layoutOf(platform, width);
    return { platform, layout, mobileUa, phoneUa, tabletUa, touch, os, width, height, orientation, override };
  }

  /** Collect the detection inputs from a browser-like `win` (window or a test double). */
  function readEnvironment(win) {
    const w = win || {};
    const nav = w.navigator || {};
    let coarse = false;
    try {
      coarse = typeof w.matchMedia === "function" && Boolean(w.matchMedia("(pointer: coarse)").matches);
    } catch {
      coarse = false;
    }
    const vv = w.visualViewport;
    return {
      userAgent: nav.userAgent || "",
      userAgentData: nav.userAgentData || null,
      maxTouchPoints: nav.maxTouchPoints || 0,
      platform: nav.platform || "",
      coarsePointer: coarse,
      // innerWidth is the layout viewport: pinch-zoom does not change it, browser zoom does (as wanted)
      width: w.innerWidth || (vv && vv.width) || 1280,
      height: w.innerHeight || (vv && vv.height) || 800,
      override: parseOverride(w.location && w.location.search),
    };
  }

  const same = (a, b) =>
    a &&
    b &&
    a.platform === b.platform &&
    a.layout === b.layout &&
    a.touch === b.touch &&
    a.orientation === b.orientation &&
    a.os === b.os;

  /** Write the result to <html> data attributes (and a --vh helper for browsers without dvh). */
  function applyToDocument(doc, info) {
    const el = doc && doc.documentElement;
    if (!el || typeof el.setAttribute !== "function") return false;
    el.setAttribute("data-platform", info.platform);
    el.setAttribute("data-layout", info.layout || layoutOf(info.platform, info.width));
    el.setAttribute("data-input", info.touch ? "touch" : "mouse");
    el.setAttribute("data-orientation", info.orientation);
    el.setAttribute("data-os", info.os);
    if (el.style && typeof el.style.setProperty === "function") {
      el.style.setProperty("--vh", `${info.height * 0.01}px`);
    }
    return true;
  }

  /**
   * Start tracking: detects now, then again on resize / rotation / pointer-type changes.
   * ctx: {window, document, onChange(info, previous)}.  Returns {info(), refresh(), stop()}.
   */
  function start(ctx) {
    const c = ctx || {};
    const win = c.window || (typeof window !== "undefined" ? window : null);
    const doc = c.document || (win && win.document) || null;
    const onChange = typeof c.onChange === "function" ? c.onChange : () => {};
    if (!win) return null;

    let current = null;
    let timer = null;
    const mqls = [];

    function refresh() {
      const next = detect(readEnvironment(win));
      const previous = current;
      current = next;
      applyToDocument(doc, next);
      // width / height changes inside the same platform matter to nobody but --vh (already written)
      if (!previous || !same(previous, next)) onChange(next, previous);
      return next;
    }

    // Recomputed synchronously: detection is a handful of comparisons, and a resize must never
    // paint one frame of the old layout (nor may a screenshot taken right after it).
    function schedule() {
      refresh();
      // some browsers report the final size of a rotation a moment later
      if (timer === null && typeof win.setTimeout === "function") {
        timer = win.setTimeout(() => {
          timer = null;
          refresh();
        }, 250);
      }
    }

    if (typeof win.addEventListener === "function") {
      win.addEventListener("resize", schedule);
      win.addEventListener("orientationchange", schedule);
    }
    try {
      if (typeof win.matchMedia === "function") {
        for (const q of ["(pointer: coarse)", "(orientation: portrait)"]) {
          const mql = win.matchMedia(q);
          if (mql && typeof mql.addEventListener === "function") {
            mql.addEventListener("change", schedule);
            mqls.push(mql);
          }
        }
      }
    } catch {
      /* older browsers: resize alone is enough */
    }

    refresh();
    return {
      info: () => current,
      refresh,
      stop() {
        if (typeof win.removeEventListener === "function") {
          win.removeEventListener("resize", schedule);
          win.removeEventListener("orientationchange", schedule);
        }
        for (const mql of mqls) {
          if (typeof mql.removeEventListener === "function") mql.removeEventListener("change", schedule);
        }
      },
    };
  }

  return {
    PLATFORMS,
    PHONE_MAX_WIDTH,
    TABLET_MAX_WIDTH,
    COMPACT_MAX_WIDTH,
    parseOverride,
    layoutOf,
    detect,
    readEnvironment,
    applyToDocument,
    start,
  };
});
