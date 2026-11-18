"use strict";
// Platform detection (static/js/platform.js): UA / pointer / viewport -> <html data-platform data-layout ...>.
// Run: node tests/frontend/test_platform.js
const assert = require("node:assert/strict");
const path = require("node:path");
const P = require(path.join(__dirname, "..", "..", "static", "js", "platform.js"));

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

const UA = {
  desktop: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
  mac: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
  iphone: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
  ipad: "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
  androidPhone: "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36",
  androidTablet: "Mozilla/5.0 (Linux; Android 14; SM-T970) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
};

const d = (over) => P.detect({ userAgent: UA.desktop, width: 1440, height: 900, ...over });

// ---- the rules ------------------------------------------------------------------------------------------
test("desktop browsers: wide layout from 1024 px, compact below", () => {
  for (const [w, layout] of [[1920, "wide"], [1440, "wide"], [1280, "wide"], [1024, "wide"], [1023, "compact"], [900, "compact"], [768, "compact"]]) {
    const r = d({ width: w, height: 800 });
    assert.equal(r.platform, "desktop", `${w}`);
    assert.equal(r.layout, layout, `${w}`);
    assert.equal(r.touch, false);
  }
});

test("a narrow window is the phone layout on any device (< 768 px)", () => {
  for (const ua of [UA.desktop, UA.mac, UA.iphone, UA.androidPhone, UA.ipad]) {
    const r = d({ userAgent: ua, width: 700, height: 900 });
    assert.equal(r.platform, "phone", ua.slice(0, 30));
    assert.equal(r.layout, "phone");
  }
  assert.equal(d({ width: 767 }).platform, "phone");
  assert.equal(d({ width: 768 }).platform, "desktop");
});

test("phones by user agent: iPhone 390x844 and Android 360x800 -> phone, touch, correct OS", () => {
  const iphone = d({ userAgent: UA.iphone, width: 390, height: 844, coarsePointer: true });
  assert.deepEqual([iphone.platform, iphone.layout, iphone.touch, iphone.os, iphone.orientation], ["phone", "phone", true, "ios", "portrait"]);
  const android = d({ userAgent: UA.androidPhone, width: 360, height: 800, coarsePointer: true });
  assert.deepEqual([android.platform, android.layout, android.touch, android.os], ["phone", "phone", true, "android"]);
});

test("a phone held sideways is still a phone (short, not narrow)", () => {
  const r = d({ userAgent: UA.iphone, width: 844, height: 390, coarsePointer: true });
  assert.equal(r.platform, "phone");
  assert.equal(r.orientation, "landscape");
  // ... but a desktop window of the same size is not
  assert.equal(d({ width: 844, height: 390 }).platform, "desktop");
});

test("tablets: iPad UA, Android without 'Mobile', the iPadOS 'Mac' with a touch screen, any touch device up to 1439 px", () => {
  assert.equal(d({ userAgent: UA.ipad, width: 820, height: 1180, coarsePointer: true }).platform, "tablet");
  assert.equal(d({ userAgent: UA.androidTablet, width: 1000, height: 600, coarsePointer: true }).platform, "tablet");
  const mac = d({ userAgent: UA.mac, platform: "MacIntel", maxTouchPoints: 5, width: 820, height: 1180 });
  assert.equal(mac.platform, "tablet");
  assert.equal(mac.os, "ios");
  assert.equal(mac.touch, true);
  // a real Mac (no touch points) is a desktop
  assert.equal(d({ userAgent: UA.mac, platform: "MacIntel", maxTouchPoints: 0, width: 1440, height: 900 }).platform, "desktop");
  // pointer: coarse on an unknown UA
  assert.equal(d({ width: 1000, height: 700, coarsePointer: true }).platform, "tablet");
  // ... but a touch device with a huge viewport is a desktop-class screen
  assert.equal(d({ userAgent: UA.ipad, width: 1600, height: 1000, coarsePointer: true }).platform, "desktop");
});

test("layout of tablets: compact up to 1024 px (matrix: 1024x768 / 820x1180), wide beyond (iPad Air sideways)", () => {
  const t = (w, h) => d({ userAgent: UA.ipad, width: w, height: h, coarsePointer: true });
  assert.equal(t(820, 1180).layout, "compact");
  assert.equal(t(1024, 768).layout, "compact");
  assert.equal(t(1180, 820).layout, "wide");
  assert.equal(t(1366, 1024).layout, "wide");
  // rotating a tablet re-lays it out
  assert.notEqual(t(820, 1180).layout, t(1180, 820).layout);
});

test("navigator.userAgentData.mobile counts; a mobile hint on an unknown UA is a phone UA", () => {
  const r = d({ userAgent: "SomeNewBrowser/1.0", userAgentData: { mobile: true }, width: 800, height: 400, coarsePointer: true });
  assert.equal(r.mobileUa, true);
  assert.equal(r.phoneUa, true);
  assert.equal(r.platform, "phone"); // short landscape
  assert.equal(d({ userAgent: "SomeNewBrowser/1.0", userAgentData: { mobile: false }, width: 1440 }).mobileUa, false);
  assert.equal(d({ userAgent: UA.desktop, userAgentData: { mobile: true }, width: 800, height: 1200 }).platform, "tablet");
});

test("overrides: ?platform=… forces the platform, ?input=touch the tap sizes", () => {
  assert.equal(P.parseOverride("?platform=phone"), "phone");
  assert.equal(P.parseOverride("?x=1&platform=Tablet"), "tablet");
  assert.equal(P.parseOverride("?platform=watch"), null);
  assert.equal(P.parseOverride(""), null);
  assert.equal(P.parseOverride(undefined), null);
  assert.equal(P.parseInputOverride("?input=touch"), "touch");
  assert.equal(P.parseInputOverride("?platform=phone&input=MOUSE"), "mouse");
  assert.equal(P.parseInputOverride("?input=pen"), null);
  assert.equal(d({ width: 1440, override: "phone" }).platform, "phone");
  assert.equal(d({ width: 1440, override: "phone" }).layout, "phone");
  assert.equal(d({ width: 400, override: "desktop" }).platform, "desktop");
  assert.equal(d({ width: 1440, override: "tablet" }).layout, "wide"); // a tablet wider than 1024
  assert.equal(d({ width: 1440, inputOverride: "touch" }).touch, true);
  assert.equal(d({ userAgent: UA.iphone, width: 390, inputOverride: "mouse" }).touch, false);
});

test("layoutOf is the single source of the layout name", () => {
  assert.equal(P.layoutOf("phone", 2000), "phone");
  assert.equal(P.layoutOf("desktop", 1023), "compact");
  assert.equal(P.layoutOf("desktop", 1024), "wide");
  assert.equal(P.layoutOf("tablet", 1024), "compact");
  assert.equal(P.layoutOf("tablet", 1025), "wide");
  assert.equal(P.layoutOf("desktop", undefined), "wide");
});

// ---- environment + document ------------------------------------------------------------------------------
function fakeWindow({ ua = UA.desktop, width = 1440, height = 900, coarse = false, search = "", maxTouchPoints = 0, mobileHint = null } = {}) {
  const listeners = {};
  const mqls = [];
  const timers = [];
  const win = {
    innerWidth: width,
    innerHeight: height,
    navigator: { userAgent: ua, maxTouchPoints, platform: "", userAgentData: mobileHint === null ? undefined : { mobile: mobileHint } },
    location: { search },
    document: { documentElement: null },
    matchMedia: (q) => {
      const m = { q, matches: /coarse/.test(q) ? coarse : false, listeners: [], addEventListener: (t, fn) => m.listeners.push(fn), removeEventListener: (t, fn) => (m.listeners = m.listeners.filter((f) => f !== fn)) };
      mqls.push(m);
      return m;
    },
    addEventListener: (t, fn) => (listeners[t] = (listeners[t] || []).concat([fn])),
    removeEventListener: (t, fn) => (listeners[t] = (listeners[t] || []).filter((f) => f !== fn)),
    setTimeout: (fn, ms) => timers.push({ fn, ms }),
    fire: (t) => (listeners[t] || []).forEach((fn) => fn({ type: t })),
    listeners,
    mqls,
    timers,
  };
  const attrs = {};
  const styleProps = {};
  win.document.documentElement = {
    attrs,
    setAttribute: (k, v) => (attrs[k] = String(v)),
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    style: { setProperty: (k, v) => (styleProps[k] = v), props: styleProps },
  };
  return win;
}

test("readEnvironment collects UA, touch points, (pointer: coarse), the viewport and the URL overrides", () => {
  const win = fakeWindow({ ua: UA.ipad, width: 820, height: 1180, coarse: true, search: "?platform=phone&input=mouse", maxTouchPoints: 5, mobileHint: true });
  const env = P.readEnvironment(win);
  assert.equal(env.userAgent, UA.ipad);
  assert.equal(env.coarsePointer, true);
  assert.equal(env.maxTouchPoints, 5);
  assert.deepEqual(env.userAgentData, { mobile: true });
  assert.equal(env.width, 820);
  assert.equal(env.height, 1180);
  assert.equal(env.override, "phone");
  assert.equal(env.inputOverride, "mouse");
  // a window without matchMedia / navigator does not throw
  assert.doesNotThrow(() => P.readEnvironment({}));
  assert.equal(P.detect(P.readEnvironment({})).platform, "desktop");
  const noMq = fakeWindow();
  noMq.matchMedia = () => {
    throw new Error("no");
  };
  assert.equal(P.readEnvironment(noMq).coarsePointer, false);
});

test("applyToDocument writes data-platform / data-layout / data-input / data-orientation / data-os", () => {
  const win = fakeWindow();
  assert.equal(P.applyToDocument(win.document, P.detect({ userAgent: UA.iphone, width: 390, height: 844, coarsePointer: true })), true);
  const a = win.document.documentElement.attrs;
  assert.deepEqual(a, { "data-platform": "phone", "data-layout": "phone", "data-input": "touch", "data-orientation": "portrait", "data-os": "ios" });
  assert.equal(win.document.documentElement.style.props["--vh"], "8.44px");
  assert.equal(P.applyToDocument({}, {}), false);
  assert.equal(P.applyToDocument(null, {}), false);
});

test("start(): detects now, recomputes on resize / rotation, reports only real changes, and stops", () => {
  const win = fakeWindow({ width: 1440, height: 900 });
  const changes = [];
  const ctl = P.start({ window: win, document: win.document, onChange: (info, prev) => changes.push([prev && prev.platform, info.platform, info.layout]) });
  const attrs = win.document.documentElement.attrs;
  assert.equal(attrs["data-platform"], "desktop");
  assert.equal(attrs["data-layout"], "wide");
  assert.equal(ctl.info().platform, "desktop");
  assert.deepEqual(changes, [[null, "desktop", "wide"]]);

  // resizing inside the same layout: attributes stay, no change event
  win.innerWidth = 1300;
  win.fire("resize");
  assert.equal(changes.length, 1);

  // a narrower window: compact, then phone (immediately: no debounce before the paint)
  win.innerWidth = 900;
  win.fire("resize");
  assert.equal(attrs["data-layout"], "compact");
  win.innerWidth = 390;
  win.innerHeight = 844;
  win.fire("resize");
  assert.equal(attrs["data-platform"], "phone");
  assert.equal(attrs["data-layout"], "phone");
  assert.equal(attrs["data-orientation"], "portrait");
  assert.deepEqual(changes.map((c) => c[2]), ["wide", "compact", "phone"]);

  // rotation (the browser reports the new size, maybe a moment later: the follow-up timer repeats the check)
  win.innerWidth = 844;
  win.innerHeight = 390;
  win.fire("orientationchange");
  assert.equal(attrs["data-orientation"], "landscape");
  assert.ok(win.timers.length >= 1);
  win.timers.splice(0).forEach((t) => t.fn());

  // a pointer-type change (a keyboard dock on a tablet) is observed through matchMedia
  const coarse = win.mqls.find((m) => /coarse/.test(m.q));
  assert.ok(coarse && coarse.listeners.length === 1);

  ctl.stop();
  assert.deepEqual(win.listeners.resize, []);
  assert.deepEqual(win.listeners.orientationchange, []);
  assert.equal(coarse.listeners.length, 0);
  const before = attrs["data-platform"];
  win.innerWidth = 390;
  win.fire("resize");
  assert.equal(attrs["data-platform"], before, "stopped: nothing recomputes");
  assert.notEqual(before, "phone");
});

test("start(): no window -> null (the page keeps its default layout)", () => {
  assert.equal(P.start({ window: null, document: null }), null);
});

// ---- runner -------------------------------------------------------------------------------------------------
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
