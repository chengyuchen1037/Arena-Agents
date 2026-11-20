"use strict";
// A well-formedness check for the HTML the page builds with innerHTML.
//
// The fake DOM of harness.js stores markup as a string and never parses it, so a stray </div>, a tag that is never closed, a <div> inside a <p> (a browser
// closes the <p> on its own and the rest lands somewhere else) or an id that occurs twice would pass every test and break (or silently change) the page in a real
// browser.  `markupProblems(html)` finds these in one fragment; `envProblems(env, staticHtml)` looks at everything the page has drawn so far and at the ids
// against the ids of index.html.  Used by test_settings_ui.js, test_bots_ui.js and test_botedit_ui.js (the markup of v0.14).

const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"]);
/** Elements a <p> cannot contain: a browser ends the paragraph before them. */
const NOT_IN_P = new Set(["address", "article", "aside", "blockquote", "details", "div", "dl", "fieldset", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main", "nav", "ol", "p", "pre", "section", "table", "ul"]);
/** Interactive content a <button> cannot contain. */
const NOT_IN_BUTTON = new Set(["a", "button", "input", "select", "textarea", "label"]);

const TAG = /<!--[\s\S]*?-->|<(\/?)([A-Za-z][A-Za-z0-9-]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>/g;

/**
 * The problems of one HTML fragment, as short sentences ([] when it is fine).  `container`: the tag of the element the fragment is the innerHTML of (a
 * `<ul>` that gets its `<li>`s patched in: they are fine there, and nowhere else).
 */
function markupProblems(html, { container = "" } = {}) {
  const problems = [];
  const stack = container ? [container] : [];
  const text = String(html || "");
  let m;
  TAG.lastIndex = 0;
  while ((m = TAG.exec(text))) {
    if (m[0].startsWith("<!--")) continue;
    const closing = m[1] === "/";
    const name = m[2].toLowerCase();
    const selfClosed = /\/\s*$/.test(m[3]);
    if (closing) {
      const at = stack.lastIndexOf(name);
      if (at < 0 || (container && at === 0)) {
        problems.push(`</${name}> closes nothing (near "${text.slice(Math.max(0, m.index - 30), m.index + m[0].length)}")`);
        continue;
      }
      if (at !== stack.length - 1) problems.push(`<${stack[stack.length - 1]}> is still open at </${name}>`);
      stack.length = at;
      continue;
    }
    const top = stack[stack.length - 1];
    if (top === "p" && NOT_IN_P.has(name)) problems.push(`<${name}> inside a <p>: the browser ends the paragraph there`);
    if (stack.includes("button") && NOT_IN_BUTTON.has(name)) problems.push(`<${name}> inside a <button> is not allowed`);
    if (name === "li" && !(top === "ul" || top === "ol")) problems.push("<li> outside of a <ul> / <ol>");
    if (!VOID.has(name) && !selfClosed) stack.push(name);
  }
  for (const name of container ? stack.slice(1) : stack) problems.push(`<${name}> is never closed`);
  // a quote or a bracket in the text that was meant to be escaped
  const bare = text.replace(TAG, "");
  if (/[<>]/.test(bare)) problems.push(`a raw < or > in the text: "${bare.match(/.{0,20}[<>].{0,20}/)[0]}"`);
  return problems;
}

/** The ids an HTML fragment defines (in order, repeats included), with the tag that carries each: [[id, tag], ...]. */
function idsWithTags(html) {
  const out = [];
  const re = /<([A-Za-z][A-Za-z0-9-]*)(?:"[^"]*"|'[^']*'|[^>"'])*?\sid="([^"]*)"/g;
  let m;
  while ((m = re.exec(String(html || "")))) out.push([m[2], m[1].toLowerCase()]);
  return out;
}

/** The ids an HTML fragment defines (in order, repeats included). */
function idsOf(html) {
  return idsWithTags(html).map(([id]) => id);
}

/**
 * Everything the page has drawn into the fake elements of `env` so far: the problems of each fragment ("<element id>: ...") and the ids that occur twice in
 * the page as a whole (the static `staticHtml` of index.html together with every fragment).  `only(id)` limits the fragments that are CHECKED (the ids of all of
 * them still count for the duplicates).
 */
function envProblems(env, staticHtml, only) {
  const problems = [];
  const seen = new Map();
  const tagOf = new Map(); // id -> the tag that carries it (the fragment of an element is the innerHTML of that tag)
  const note = (id, tag, where) => {
    seen.set(id, (seen.get(id) || []).concat([where]));
    if (!tagOf.has(id)) tagOf.set(id, tag);
  };
  for (const [id, tag] of idsWithTags(staticHtml)) note(id, tag, "index.html");
  for (const [elementId, el] of env.els) {
    const html = el && el._html;
    if (html) for (const [id, tag] of idsWithTags(html)) note(id, tag, elementId);
  }
  for (const [elementId, el] of env.els) {
    const html = el && el._html;
    if (!html || (only && !only(elementId))) continue;
    for (const p of markupProblems(html, { container: tagOf.get(elementId) || "" })) problems.push(`${elementId}: ${p}`);
  }
  for (const [id, where] of seen) if (where.length > 1) problems.push(`id "${id}" occurs ${where.length} times (${where.join(", ")})`);
  return problems;
}

module.exports = { markupProblems, idsOf, idsWithTags, envProblems };
