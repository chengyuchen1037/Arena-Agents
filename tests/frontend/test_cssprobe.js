"use strict";
// The CSS engine of tests/frontend/cssprobe.js (what test_phone_layout.js asks the real style sheet): its cascade must be the browser's, or the layout checks
// say nothing.  Run: node tests/frontend/test_cssprobe.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const H = require("./harness.js");
const P = require("./cssprobe.js");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

/** One element of a page made of `html` (the first one in it), with the engine of `css` for `viewport`. */
function probe(css, html, viewport, pick) {
  const engine = new P.Engine(css, viewport);
  const nodes = P.parseHtml(html);
  const root = { tag: "#root", attrs: {}, children: nodes, parent: null };
  for (const n of nodes) n.parent = root;
  const all = [...P.walk(root)].filter((n) => n.tag !== "#root" && !P.isText(n));
  const el = pick ? all.find(pick) : all[all.length - 1];
  assert.ok(el, "the element is there");
  return { engine, el, root, all };
}

const PAGE = (attrs, inner) => `<html ${attrs}><body>${inner}</body></html>`;

test("the HTML tree: attributes, void elements, text, comments, raw text, nesting", () => {
  const nodes = P.parseHtml(`<!DOCTYPE html><!-- c --><div id="a" class="x  y" data-n='1' hidden><span>one</span> two<br><input type="radio" checked/><script>if (a < b) { x }</script><b>b</b></div>`);
  assert.equal(nodes.length, 1);
  const div = nodes[0];
  assert.deepEqual(div.attrs, { id: "a", class: "x  y", "data-n": "1", hidden: "" });
  assert.deepEqual(P.classesOf(div), ["x", "y"]);
  assert.deepEqual(div.children.map((c) => c.tag), ["span", "#text", "br", "input", "script", "b"], "the script's text is not part of the tree");
  assert.equal(div.children[3].attrs.checked, "");
  assert.equal(P.textOf(div), "one two" + "b");
  assert.equal(div.children[0].parent, div);
});

test("selectors: specificity, combinators, attributes, :not, :first-child, nth-child, pseudo-elements and states", () => {
  const spec = (s) => P.parseSelector(s).spec;
  assert.deepEqual(spec("*"), [0, 0, 0]);
  assert.deepEqual(spec("button"), [0, 0, 1]);
  assert.deepEqual(spec(".a.b"), [0, 2, 0]);
  assert.deepEqual(spec("#x .a"), [1, 1, 0]);
  assert.deepEqual(spec('html[data-layout="phone"] .btn'), [0, 2, 1]);
  assert.deepEqual(spec(".btn:hover:not(:disabled)"), [0, 3, 0], ":not takes its argument's specificity");
  assert.deepEqual(spec(":where(.a, #b)"), [0, 0, 0]);
  assert.deepEqual(spec("a::before"), [0, 0, 2]);
  const { all } = probe("", `<ul class="l"><li class="a first">1</li><li class="a" data-k="x-y">2</li><li class="b" disabled>3</li></ul><p class="a">p</p>`);
  const [ul, li1, li2, li3, p] = all;
  const m = (el, sel) => {
    const s = P.parseSelector(sel);
    return P.matchComplex(el, s.parts, s.parts.length - 1);
  };
  assert.ok(m(li1, ".l > .a") && m(li1, "ul .a") && m(li1, "ul li.first") && m(li1, "li:first-child") && !m(li2, "li:first-child"));
  assert.ok(m(li3, "li:last-child") && m(li2, "li:nth-child(2)") && m(li3, "li:nth-child(odd)") && !m(li2, "li:nth-child(odd)") && m(li3, "li:nth-child(2n+1)"));
  assert.ok(m(li2, "li.a + li") && m(li3, "li.a ~ li.b") && !m(li1, "li + li"));
  assert.ok(m(li2, '[data-k="x-y"]') && m(li2, "[data-k^=x]") && m(li2, "[data-k$=y]") && m(li2, "[data-k*='-']") && m(li2, "[data-k]") && !m(li1, "[data-k]"));
  assert.ok(m(li3, "li:disabled") && !m(li2, "li:disabled") && m(li2, "li:not(:disabled)") && m(li2, "li:not(.b, .c)") && !m(li3, "li:not(.b, .c)"));
  assert.ok(!m(li1, "li:hover") && !m(li1, "li::before") && !m(li1, "li:focus-visible"), "states and pseudo-elements are not the element at rest");
  assert.ok(m(ul, ".l:not(.m)") && !m(p, ".l p") && m(p, "p.a"));
  assert.ok(!m(li1, ":unknown-pseudo"), "an unknown pseudo-class never matches");
});

test("the cascade: specificity beats order, the later wins a tie, !important beats both, the style attribute beats the selectors", () => {
  const css = `.a { color: red } .a { color: blue } #i { color: green } div.a { color: pink } .a { width: 1px !important } .a.a2 { width: 2px } `;
  const { engine, el } = probe(css, `<div id="i" class="a a2" style="height: 7px; width: 3px"></div>`);
  assert.equal(engine.computed(el, "color"), "green", "an id beats a class");
  assert.equal(engine.computed(el, "width"), "1px", "!important beats a more specific rule and the style attribute");
  assert.equal(engine.px(el, "height"), 7, "the style attribute");
  const second = probe(`.a { color: red } .a { color: blue } div.a { background: pink } .a { background: black }`, `<div class="a"></div>`);
  assert.equal(second.engine.computed(second.el, "color"), "blue", "the later of two equal rules");
  assert.equal(second.engine.computed(second.el, "background"), "pink", "the more specific, although earlier");
});

test("inheritance and custom properties: --x goes down the tree, var() has fallbacks, a rule on a deeper element wins, html[data-*] overrides :root", () => {
  const css = `:root { --tap: 34px; --gap: 4px } html[data-input="touch"] { --tap: 40px } .btn { min-height: var(--tap); padding: var(--missing, 3px) var(--gap) } .deep { --tap: 10px } body { color: red; font-size: 20px }`;
  const page = (attrs) => probe(css, PAGE(attrs, `<div><button class="btn">x</button><div class="deep"><button class="btn">y</button></div></div>`), undefined, (n) => n.tag === "button");
  const mouse = page('data-input="mouse"');
  const touch = page('data-input="touch"');
  assert.equal(mouse.engine.px(mouse.el, "min-height"), 34);
  assert.equal(touch.engine.px(touch.el, "min-height"), 40);
  assert.equal(touch.engine.computed(touch.el, "padding-top"), "3px", "the fallback of a missing variable");
  assert.equal(touch.engine.computed(touch.el, "padding-right"), "4px");
  assert.equal(touch.engine.computed(touch.el, "color"), "red", "color is inherited");
  assert.equal(touch.engine.px(touch.el, "font-size"), 20);
  const deeper = probe(css, PAGE('data-input="touch"', `<div class="deep"><button class="btn">y</button></div>`), undefined, (n) => n.tag === "button");
  assert.equal(deeper.engine.px(deeper.el, "min-height"), 10, "a custom property redefined on an ancestor");
  assert.equal(touch.engine.computed(touch.el, "width"), "", "no rule, not inherited: nothing");
});

test("@media and @supports: the rules of the window that is asked about; reduced motion and hover are windows too", () => {
  const css = `.a { width: 100px } @media (max-width: 767px) { .a { width: 200px } } @media (min-width: 768px) { .a { width: 300px } } @media (max-width: 500px) and (min-width: 400px) { .a { height: 5px } }
    @media (hover: hover) { .a { color: red } } @media (prefers-reduced-motion: reduce) { .a { color: blue } } @supports (height: 100dvh) { .a { top: 1px } } @keyframes k { from { width: 1px } to { width: 2px } } @media print { .a { width: 1px } }`;
  const at = (vp) => probe(css, `<div class="a"></div>`, vp);
  assert.equal(at({ width: 390 }).engine.px(at({ width: 390 }).el, "width"), 200);
  const wide = at({ width: 1024 });
  assert.equal(wide.engine.px(wide.el, "width"), 300);
  const mid = at({ width: 430 });
  assert.equal(mid.engine.px(mid.el, "height"), 5, "an `and` of two conditions");
  assert.ok(Number.isNaN(wide.engine.px(wide.el, "height")));
  assert.equal(at({ width: 390, hover: true }).engine.computed(at({ width: 390, hover: true }).el, "color"), "red");
  const touch = at({ width: 390, hover: false });
  assert.equal(touch.engine.computed(touch.el, "color"), "", "no hover: that rule is not there");
  const reduced = at({ width: 390, reducedMotion: true });
  assert.equal(reduced.engine.computed(reduced.el, "color"), "blue");
  assert.equal(touch.engine.px(touch.el, "top"), 1, "@supports is taken as supported");
});

test("shorthands: padding / margin / gap / overflow / flex are read as their longhands", () => {
  const css = `.a { padding: 1px 2px; margin: 4px 5px 6px 7px; gap: 3px 9px; overflow: hidden auto; flex: 1 1 220px } .b { flex: 0 0 auto; padding: 0 } .c { flex: none } .d { flex: 2 } .e { flex: 30px } .f { flex-flow: column wrap }`;
  const one = (cls) => probe(css, `<div class="${cls}"></div>`);
  const a = one("a");
  const g = (p) => a.engine.computed(a.el, p);
  assert.deepEqual([g("padding-top"), g("padding-right"), g("padding-bottom"), g("padding-left")], ["1px", "2px", "1px", "2px"]);
  assert.deepEqual([g("margin-top"), g("margin-right"), g("margin-bottom"), g("margin-left")], ["4px", "5px", "6px", "7px"]);
  assert.deepEqual([g("row-gap"), g("column-gap"), g("overflow-x"), g("overflow-y")], ["3px", "9px", "hidden", "auto"]);
  assert.deepEqual([g("flex-grow"), g("flex-shrink"), g("flex-basis")], ["1", "1", "220px"]);
  const flex = (cls) => {
    const o = one(cls);
    return [o.engine.computed(o.el, "flex-grow"), o.engine.computed(o.el, "flex-shrink"), o.engine.computed(o.el, "flex-basis")];
  };
  assert.deepEqual(flex("b"), ["0", "0", "auto"]);
  assert.deepEqual(flex("c"), ["0", "0", "auto"]);
  assert.deepEqual(flex("d"), ["2", "1", "0px"]);
  assert.deepEqual(flex("e"), ["1", "1", "30px"]);
  const f = one("f");
  assert.deepEqual([f.engine.computed(f.el, "flex-direction"), f.engine.computed(f.el, "flex-wrap")], ["column", "wrap"]);
});

test("lengths: px, rem, vw, calc, min, max, clamp; what cannot be said (%, auto) is NaN", () => {
  const ctx = { width: 390, height: 844, fontSize: 16 };
  const len = (s) => P.evalLength(s, ctx);
  assert.equal(len("40px"), 40);
  assert.equal(len("0"), 0);
  assert.equal(len("1.5rem"), 24);
  assert.equal(len("50vw"), 195);
  assert.equal(len("100dvh"), 844);
  assert.equal(len("calc(100vw - 16px)"), 374);
  assert.equal(len("min(340px, calc(100vw - 16px))"), 340);
  assert.equal(len("min(500px, calc(100vw - 16px))"), 374);
  assert.equal(len("max(10px, 3vw)"), 11.7);
  assert.equal(len("clamp(100px, 50vw, 300px)"), 195);
  assert.equal(len("calc(2 * (10px + 5px))"), 30);
  assert.equal(len("calc(env(safe-area-inset-bottom) + 12px)"), 12);
  assert.ok(Number.isNaN(len("100%")) && Number.isNaN(len("auto")) && Number.isNaN(len("")) && Number.isNaN(len("calc(100% - 10px)")) && Number.isNaN(len("12px 8px")));
});

test("display: the browser's own defaults, the hidden attribute (which an author's display beats), none on an ancestor", () => {
  const css = `.flex { display: flex } .flex[hidden] { display: none }`;
  const { engine, all } = probe(css, `<div id="a"><div class="flex" id="b"></div><div class="flex" id="c" hidden><span id="d"></span></div><span id="e"></span><div id="f" hidden></div><ul><li id="g"></li></ul></div>`);
  const by = (id) => all.find((n) => n.attrs.id === id);
  assert.equal(engine.display(by("a")), "block");
  assert.equal(engine.display(by("b")), "flex");
  assert.equal(engine.display(by("c")), "none");
  assert.equal(engine.display(by("e")), "inline");
  assert.equal(engine.display(by("f")), "none", "the hidden attribute alone");
  assert.equal(engine.display(by("g")), "list-item");
  assert.ok(engine.rendered(by("b")) && !engine.rendered(by("c")) && !engine.rendered(by("d")), "a child of a hidden element is not drawn");
  const bare = probe(`.flex { display: flex }`, `<div class="flex" hidden></div>`);
  assert.equal(bare.engine.display(bare.el), "flex", "the classic: an author's display beats the hidden attribute - as in a browser");
});

test("the real style sheet parses: rules, media, no rule lost to a brace, the variables of the touch screen", () => {
  const css = fs.readFileSync(path.join(H.ROOT, "static", "css", "style.css"), "utf8");
  const rules = P.parseCss(css);
  assert.ok(rules.length > 1000, `${rules.length} rules`);
  const braces = css.replace(/\/\*[\s\S]*?\*\//g, "");
  assert.equal((braces.match(/\{/g) || []).length, (braces.match(/\}/g) || []).length, "the style sheet is balanced");
  const selectors = rules.flatMap((r) => r.selectors);
  assert.ok(selectors.every((s) => s.parts.length > 0), "every selector was understood");
  // a button of a phone with a finger: the hit area of the style sheet
  const page = P.buildPage(H.HTML_SRC, null, { "data-platform": "phone", "data-layout": "phone", "data-input": "touch", "data-theme": "light" });
  const engine = new P.Engine(css, { width: 390, height: 844, hover: false });
  const probeEl = { tag: "button", attrs: { class: "btn btn-xs" }, children: [], parent: P.findById(page.html, "settings-body") };
  assert.equal(engine.px(probeEl, "min-height"), 40, "touch: --tap is 40px, .btn-xs asks for 40px");
  const desktop = P.buildPage(H.HTML_SRC, null, { "data-platform": "desktop", "data-layout": "wide", "data-input": "mouse", "data-theme": "light" });
  const wide = new P.Engine(css, { width: 1440, height: 900, hover: true });
  const el2 = { tag: "button", attrs: { class: "btn btn-xs" }, children: [], parent: P.findById(desktop.html, "settings-body") };
  assert.ok(!(wide.px(el2, "min-height") >= 40), "a mouse needs no 40 px");
});

test("a page is built from index.html and what the scripts drew: ids found, fragments put in, hidden / display / disabled follow the fake elements", () => {
  const env = {
    els: new Map([
      ["settings-body", { _html: `<section id="inner"><button class="btn">x</button></section>`, hidden: false }],
      ["inner", { _html: "<b>deep</b>", hidden: false }], // an id that only exists inside a fragment: found on the second pass
      ["settings-modal", { _html: "", hidden: false, style: { display: "flex" } }],
      ["btn-init-all", { _html: "", hidden: false, disabled: true }],
      ["no-such-id-in-the-page", { _html: "<i></i>" }],
    ]),
  };
  const page = P.buildPage(H.HTML_SRC, env, { "data-layout": "phone" });
  assert.equal(page.html.attrs["data-layout"], "phone");
  const body = P.findById(page.html, "settings-body");
  assert.equal(body.children[0].attrs.id, "inner");
  assert.equal(P.textOf(P.findById(page.html, "inner")), "deep");
  assert.equal(P.findById(page.html, "settings-modal").attrs.style, "display: flex");
  assert.equal("disabled" in P.findById(page.html, "btn-init-all").attrs, true);
  assert.equal(P.findById(page.html, "no-such-id-in-the-page"), null);
});

H.runTests(tests);
