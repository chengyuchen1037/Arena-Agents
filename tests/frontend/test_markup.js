"use strict";
// The well-formedness check of tests/frontend/markup.js (used by test_settings_ui.js, test_bots_ui.js, test_botedit_ui.js) finds what it is meant to find - and
// leaves valid markup alone.  Run: node tests/frontend/test_markup.js
const assert = require("node:assert/strict");
const H = require("./harness.js");
const M = require("./markup.js");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("valid markup has no problems: void elements, quoted > in an attribute, comments, nesting, entities", () => {
  assert.deepEqual(M.markupProblems(""), []);
  assert.deepEqual(M.markupProblems(`<div class="a"><span>文字 &amp; &lt;b&gt;</span><img src="x" alt=""><input type="radio" checked /><br></div>`), []);
  assert.deepEqual(M.markupProblems(`<div title="a > b" data-x='1 > 0'>x</div><!-- <div> in a comment -->`), []);
  assert.deepEqual(M.markupProblems(`<ul><li><button type="button"><span>x</span></button></li></ul><p>a <strong>b</strong></p><table><thead><tr><th>x</th></tr></thead></table>`), []);
});

test("a tag that is not closed, one that closes nothing, one that closes too late", () => {
  assert.deepEqual(M.markupProblems("<div><span>x</div>"), ["<span> is still open at </div>"]);
  assert.match(M.markupProblems("<div>x</div></div>")[0], /^<\/div> closes nothing/);
  assert.deepEqual(M.markupProblems("<div><p>x"), ["<div> is never closed", "<p> is never closed"]);
  assert.deepEqual(M.markupProblems("<ul><li>x</ul>"), ["<li> is still open at </ul>"]);
});

test("what a browser would repair on its own is a problem here: a block in a <p>, an <li> outside a list, a link or a button in a button", () => {
  assert.ok(M.markupProblems("<p>a<div>b</div></p>").some((p) => /<div> inside a <p>/.test(p)));
  assert.ok(M.markupProblems("<p>a<ul><li>b</li></ul></p>").some((p) => /<ul> inside a <p>/.test(p)));
  assert.ok(M.markupProblems("<div><li>x</li></div>").some((p) => /<li> outside/.test(p)));
  assert.ok(M.markupProblems(`<button><a href="#">x</a></button>`).some((p) => /<a> inside a <button>/.test(p)));
  assert.ok(M.markupProblems(`<button><button>x</button></button>`).some((p) => /<button> inside a <button>/.test(p)));
  assert.deepEqual(M.markupProblems("<p>a <span>b</span> <code>c</code></p>"), [], "inline content in a <p> is fine");
});

test("a fragment that is the innerHTML of a <ul> may be a list of <li>s - and must not close its own container", () => {
  assert.deepEqual(M.markupProblems("<li>a</li><li>b</li>", { container: "ul" }), []);
  assert.ok(M.markupProblems("<li>a</li><li>b</li>").some((p) => /<li> outside/.test(p)), "(anywhere else it is a problem)");
  assert.ok(M.markupProblems("<li>a</li></ul>", { container: "ul" }).some((p) => /<\/ul> closes nothing/.test(p)));
  assert.deepEqual(M.markupProblems("<li><span>a</li>", { container: "ul" }), ["<span> is still open at </li>"]);
  assert.ok(M.markupProblems("<li>a", { container: "ul" }).some((p) => /<li> is never closed/.test(p)));
  // through the page: the element `box` carries a <ul> in the static page, so what the page patches into it is read in that context
  const env = { els: new Map([["box", { _html: "<li>x</li>" }], ["other", { _html: "<li>y</li>" }]]) };
  assert.deepEqual(M.envProblems(env, `<ul id="box"></ul><div id="other"></div>`), ["other: <li> outside of a <ul> / <ol>"]);
});

test("text that should have been escaped: a raw < or >", () => {
  assert.ok(M.markupProblems("<div>a < b</div>").some((p) => /raw < or >/.test(p)));
  assert.ok(M.markupProblems("<div>1 > 0</div>").some((p) => /raw < or >/.test(p)));
  assert.deepEqual(M.markupProblems("<div>a &lt; b</div>"), []);
});

test("ids: found in order, repeats included; data-id and for are not ids", () => {
  assert.deepEqual(M.idsOf(`<div id="a" class="x"><span data-id="no" id="b"></span><label for="c"></label><input id="c"></div>`), ["a", "b", "c"]);
  assert.deepEqual(M.idsOf(`<div title="x" id='single'>`), [], "(single quotes are not used by the page: only id=\"...\" counts)");
});

test("the page as a whole: a fragment that repeats an id of index.html or of another fragment is reported", () => {
  const env = { els: new Map([["box-a", { _html: `<div id="dup"></div><div id="only-a"></div>` }], ["box-b", { _html: `<span id="dup"></span><p><div></div></p>` }], ["empty", { _html: "" }]]) };
  const problems = M.envProblems(env, `<div id="static-one"></div><div id="only-a"></div>`);
  assert.ok(problems.includes(`id "dup" occurs 2 times (box-a, box-b)`));
  assert.ok(problems.includes(`id "only-a" occurs 2 times (index.html, box-a)`));
  assert.ok(problems.some((p) => /^box-b: <div> inside a <p>/.test(p)));
  assert.equal(problems.length, 3);
  assert.deepEqual(M.envProblems({ els: new Map([["box-a", { _html: "<div><span></div>" }]]) }, "", (id) => id !== "box-a"), [], "`only` limits what is checked");
  assert.deepEqual(M.envProblems({ els: new Map() }, ""), []);
});

test("index.html itself is well formed and has no id twice (the page the tests run against)", () => {
  const body = H.HTML_SRC.replace(/<script[\s\S]*?<\/script>/g, "").replace(/<style[\s\S]*?<\/style>/g, "");
  const ids = M.idsOf(body);
  const twice = ids.filter((id, i) => ids.indexOf(id) !== i);
  assert.deepEqual([...new Set(twice)], []);
  const problems = M.markupProblems(body.replace(/<!DOCTYPE[^>]*>/i, ""));
  assert.deepEqual(problems, []);
});

H.runTests(tests);
