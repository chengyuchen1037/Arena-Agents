"use strict";
// A small CSS engine for the layout checks that need no browser: it reads static/css/style.css, builds the tree of a page from its HTML (index.html with
// the fragments the scripts drew into it) and says what the cascade makes of a property for an element - specificity, order, !important, inheritance, custom
// properties (`var(--tap)`), `@media` / `@supports` for a given window, the shorthands that matter (flex, overflow, padding, margin, gap) and lengths
// (`px`, `rem`, `vw`, `calc()`, `min()`, `max()`, `clamp()`).
//
// What it is NOT: a layout engine.  It knows nothing of text width, flex shrinking or line breaks.  It answers the questions of a style sheet - "is this
// control at least 40 px tall on a touch screen?", "does this row of buttons wrap?", "is a fixed width wider than the window?" - which is where a phone layout
// usually goes wrong.  Used by test_phone_layout.js; tested by test_cssprobe.js.

// ==================================================================================================================================
// the HTML tree
// ==================================================================================================================================
const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"]);
const RAW = new Set(["script", "style"]);

function parseAttrs(src) {
  const attrs = {};
  const re = /([^\s=/>"']+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;
  let m;
  while ((m = re.exec(src))) {
    attrs[m[1].toLowerCase()] = m[2] !== undefined ? m[2] : m[3] !== undefined ? m[3] : m[4] !== undefined ? m[4] : "";
  }
  return attrs;
}

/** HTML -> the list of its top-level nodes.  A node is {tag, attrs, children, parent}; a text node has tag "#text" and `text`. */
function parseHtml(html) {
  const root = { tag: "#root", attrs: {}, children: [], parent: null };
  const stack = [root];
  const text = String(html || "").replace(/<!doctype[^>]*>/gi, "");
  const lower = text.toLowerCase();
  const re = /<!--[\s\S]*?-->|<(\/?)([A-Za-z][A-Za-z0-9-]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>|[^<]+|</g;
  let m;
  while ((m = re.exec(text))) {
    const whole = m[0];
    if (whole.startsWith("<!--")) continue;
    const top = stack[stack.length - 1];
    if (m[2] === undefined) {
      if (whole.trim() && whole !== "<") top.children.push({ tag: "#text", text: whole, attrs: {}, children: [], parent: top });
      continue;
    }
    const tag = m[2].toLowerCase();
    if (m[1] === "/") {
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].tag === tag) {
          stack.length = i;
          break;
        }
      }
      continue;
    }
    const selfClosed = /\/\s*$/.test(m[3]);
    const el = { tag, attrs: parseAttrs(m[3].replace(/\/\s*$/, "")), children: [], parent: top };
    top.children.push(el);
    if (RAW.has(tag)) {
      const close = lower.indexOf(`</${tag}`, re.lastIndex);
      const end = close >= 0 ? text.indexOf(">", close) : -1;
      re.lastIndex = end >= 0 ? end + 1 : text.length;
      continue;
    }
    if (!VOID.has(tag) && !selfClosed) stack.push(el);
  }
  for (const node of root.children) node.parent = null;
  return root.children;
}

const isText = (n) => n.tag === "#text";
const classesOf = (el) => (el.attrs && el.attrs.class ? el.attrs.class.split(/\s+/).filter(Boolean) : []);
const elementChildren = (el) => el.children.filter((c) => !isText(c));

function* walk(node) {
  yield node;
  for (const c of node.children) yield* walk(c);
}

function findById(root, id) {
  for (const n of walk(root)) if (n.attrs && n.attrs.id === id) return n;
  return null;
}

/** All the text under a node, in one string (blanks folded). */
function textOf(node) {
  if (isText(node)) return node.text.replace(/\s+/g, " ");
  return node.children.map(textOf).join("");
}

/** Put the children of `node` (parsed from `html`) in place of the old ones. */
function setInnerHtml(node, html) {
  node.children = parseHtml(html);
  for (const c of node.children) c.parent = node;
}

// ==================================================================================================================================
// selectors
// ==================================================================================================================================
/** `text` cut at every `sep` that is not inside brackets or a string; the pieces are trimmed, the empty ones dropped.  `sep` "ws": any run of blanks. */
function splitTop(text, sep) {
  const out = [];
  let depth = 0;
  let quote = "";
  let last = 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quote) {
      if (ch === "\\") i++;
      else if (ch === quote) quote = "";
    } else if (ch === '"' || ch === "'") quote = ch;
    else if (ch === "(" || ch === "[") depth++;
    else if (ch === ")" || ch === "]") depth--;
    else if (depth === 0 && (sep === "ws" ? /\s/.test(ch) : ch === sep)) {
      out.push(text.slice(last, i));
      last = i + 1;
    }
  }
  out.push(text.slice(last));
  return out.map((s) => s.trim()).filter(Boolean);
}

const IDENT = /^(?:\\.|[A-Za-z0-9_\u00a0-\uffff-])+/;
const PSEUDO_ELEMENTS = new Set(["before", "after", "first-line", "first-letter", "placeholder", "selection", "marker", "backdrop"]);

function parseCompound(src) {
  const c = { tag: null, ids: [], classes: [], attrs: [], pseudos: [], pseudoElement: false };
  let i = 0;
  const ident = () => {
    const m = IDENT.exec(src.slice(i));
    if (!m) return "";
    i += m[0].length;
    return m[0].replace(/\\(.)/g, "$1");
  };
  const balanced = (open, close) => {
    // src[i] === open: the text up to its closing bracket
    let depth = 0;
    let quote = "";
    const start = i;
    for (; i < src.length; i++) {
      const ch = src[i];
      if (quote) {
        if (ch === "\\") i++;
        else if (ch === quote) quote = "";
      } else if (ch === '"' || ch === "'") quote = ch;
      else if (ch === open) depth++;
      else if (ch === close && --depth === 0) {
        i++;
        return src.slice(start + 1, i - 1);
      }
    }
    return src.slice(start + 1);
  };
  while (i < src.length) {
    const ch = src[i];
    if (ch === "*") {
      c.tag = "*";
      i++;
    } else if (ch === ".") {
      i++;
      c.classes.push(ident());
    } else if (ch === "#") {
      i++;
      c.ids.push(ident());
    } else if (ch === "[") {
      const body = balanced("[", "]");
      const m = /^\s*([^\s~|^$*=\]]+)\s*(?:([~|^$*]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\s\]]+))\s*(i)?)?\s*$/.exec(body);
      if (m) c.attrs.push({ name: m[1].toLowerCase(), op: m[2] || "", value: m[3] !== undefined ? m[3] : m[4] !== undefined ? m[4] : m[5] || "" });
    } else if (ch === ":") {
      if (src[i + 1] === ":") {
        i += 2;
        ident();
        if (src[i] === "(") balanced("(", ")");
        c.pseudoElement = true;
      } else {
        i++;
        const name = ident().toLowerCase();
        const arg = src[i] === "(" ? balanced("(", ")") : null;
        if (PSEUDO_ELEMENTS.has(name)) c.pseudoElement = true;
        else c.pseudos.push({ name, arg });
      }
    } else {
      const name = ident();
      if (!name) {
        i++; // something this engine does not know: skip it rather than loop
      } else c.tag = name.toLowerCase();
    }
  }
  return c;
}

/** "a > b c" -> [{comb: "", compound}, {comb: ">", compound}, {comb: " ", compound}] */
function parseComplex(text) {
  const parts = [];
  let depth = 0;
  let quote = "";
  let cur = "";
  let comb = "";
  const flush = () => {
    if (cur) {
      parts.push({ comb, compound: parseCompound(cur) });
      cur = "";
      comb = "";
    }
  };
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quote) {
      cur += ch;
      if (ch === "\\") cur += text[++i] || "";
      else if (ch === quote) quote = "";
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      cur += ch;
    } else if (ch === "(" || ch === "[") {
      depth++;
      cur += ch;
    } else if (ch === ")" || ch === "]") {
      depth--;
      cur += ch;
    } else if (depth === 0 && /\s/.test(ch)) {
      flush();
      if (parts.length && !comb) comb = " ";
    } else if (depth === 0 && (ch === ">" || ch === "+" || ch === "~")) {
      flush();
      comb = ch;
    } else cur += ch;
  }
  flush();
  return parts;
}

function specificityOf(parts) {
  let a = 0;
  let b = 0;
  let c = 0;
  const addCompound = (cp) => {
    a += cp.ids.length;
    b += cp.classes.length + cp.attrs.length;
    if (cp.tag && cp.tag !== "*") c++;
    if (cp.pseudoElement) c++;
    for (const p of cp.pseudos) {
      if (p.name === "where") continue;
      if (p.name === "not" || p.name === "is" || p.name === "matches" || p.name === "has") {
        // the most specific of the list
        let best = [0, 0, 0];
        for (const sel of splitTop(p.arg || "", ",")) {
          const s = specificityOf(parseComplex(sel));
          if (s[0] > best[0] || (s[0] === best[0] && (s[1] > best[1] || (s[1] === best[1] && s[2] > best[2])))) best = s;
        }
        a += best[0];
        b += best[1];
        c += best[2];
      } else b++;
    }
  };
  for (const part of parts) addCompound(part.compound);
  return [a, b, c];
}

function parseSelector(text) {
  const parts = parseComplex(text);
  return { text, parts, spec: specificityOf(parts), pseudoElement: parts.length > 0 && parts[parts.length - 1].compound.pseudoElement };
}

const STATE_PSEUDOS = new Set(["hover", "focus", "active", "focus-visible", "focus-within", "visited", "link", "target", "checked-unfocused", "fullscreen", "placeholder-shown", "invalid", "valid", "required"]);

function siblingsOf(el) {
  return el.parent ? el.parent.children.filter((c) => !isText(c)) : [el];
}

function matchNth(expr, index) {
  // index: 1-based position
  const e = String(expr).trim().toLowerCase();
  if (e === "odd") return index % 2 === 1;
  if (e === "even") return index % 2 === 0;
  const m = /^([+-]?\d*)n\s*(?:([+-])\s*(\d+))?$/.exec(e.replace(/\s+/g, ""));
  if (m) {
    const a = m[1] === "" || m[1] === "+" ? 1 : m[1] === "-" ? -1 : Number(m[1]);
    const b = m[3] ? Number(m[3]) * (m[2] === "-" ? -1 : 1) : 0;
    if (a === 0) return index === b;
    const n = (index - b) / a;
    return Number.isInteger(n) && n >= 0;
  }
  return Number(e) === index;
}

function matchCompound(el, c) {
  if (c.pseudoElement) return false; // the rule styles a part of the element, not the element
  if (el.tag === "#text" || el.tag === "#root") return false;
  if (c.tag && c.tag !== "*" && el.tag !== c.tag) return false;
  if (c.ids.length && !c.ids.every((id) => el.attrs.id === id)) return false;
  if (c.classes.length) {
    const have = classesOf(el);
    if (!c.classes.every((k) => have.includes(k))) return false;
  }
  for (const a of c.attrs) {
    const has = Object.prototype.hasOwnProperty.call(el.attrs, a.name);
    const v = has ? String(el.attrs[a.name]) : "";
    if (!has) return false;
    if (a.op === "=" && v !== a.value) return false;
    if (a.op === "~=" && !v.split(/\s+/).includes(a.value)) return false;
    if (a.op === "|=" && v !== a.value && !v.startsWith(`${a.value}-`)) return false;
    if (a.op === "^=" && !v.startsWith(a.value)) return false;
    if (a.op === "$=" && !v.endsWith(a.value)) return false;
    if (a.op === "*=" && !v.includes(a.value)) return false;
  }
  for (const p of c.pseudos) {
    const name = p.name;
    if (STATE_PSEUDOS.has(name)) return false; // the resting state is what is checked
    if (name === "not" || name === "is" || name === "matches" || name === "where") {
      const any = splitTop(p.arg || "", ",").some((sel) => {
        const parts = parseComplex(sel);
        return matchComplex(el, parts, parts.length - 1);
      });
      if (name === "not" ? any : !any) return false;
    } else if (name === "disabled") {
      if (!("disabled" in el.attrs)) return false;
    } else if (name === "enabled") {
      if ("disabled" in el.attrs) return false;
    } else if (name === "checked") {
      if (!("checked" in el.attrs)) return false;
    } else if (name === "empty") {
      if (el.children.length) return false;
    } else if (name === "root") {
      if (el.tag !== "html") return false;
    } else if (name === "first-child") {
      if (siblingsOf(el)[0] !== el) return false;
    } else if (name === "last-child") {
      const s = siblingsOf(el);
      if (s[s.length - 1] !== el) return false;
    } else if (name === "only-child") {
      if (siblingsOf(el).length !== 1) return false;
    } else if (name === "first-of-type") {
      if (siblingsOf(el).find((s) => s.tag === el.tag) !== el) return false;
    } else if (name === "last-of-type") {
      const same = siblingsOf(el).filter((s) => s.tag === el.tag);
      if (same[same.length - 1] !== el) return false;
    } else if (name === "nth-child") {
      if (!matchNth(p.arg, siblingsOf(el).indexOf(el) + 1)) return false;
    } else if (name === "nth-of-type") {
      if (!matchNth(p.arg, siblingsOf(el).filter((s) => s.tag === el.tag).indexOf(el) + 1)) return false;
    } else return false; // an unknown pseudo-class: never matches (conservative)
  }
  return true;
}

function matchComplex(el, parts, idx) {
  if (idx < 0 || !parts.length) return false;
  if (!matchCompound(el, parts[idx].compound)) return false;
  if (idx === 0) return true;
  const comb = parts[idx].comb;
  if (comb === ">") return Boolean(el.parent) && matchComplex(el.parent, parts, idx - 1);
  if (comb === " ") {
    for (let p = el.parent; p; p = p.parent) if (matchComplex(p, parts, idx - 1)) return true;
    return false;
  }
  const sib = siblingsOf(el);
  const at = sib.indexOf(el);
  if (comb === "+") return at > 0 && matchComplex(sib[at - 1], parts, idx - 1);
  if (comb === "~") return sib.slice(0, at).some((s) => matchComplex(s, parts, idx - 1));
  return false;
}

// ==================================================================================================================================
// the style sheet
// ==================================================================================================================================
function stripComments(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

function skipString(css, i) {
  const q = css[i];
  for (i++; i < css.length; i++) {
    if (css[i] === "\\") i++;
    else if (css[i] === q) return i;
  }
  return css.length - 1;
}

function parseDecls(body) {
  const out = [];
  let depth = 0;
  let last = 0;
  const push = (end) => {
    const chunk = body.slice(last, end).trim();
    last = end + 1;
    const at = chunk.indexOf(":");
    if (at <= 0) return;
    let value = chunk.slice(at + 1).trim();
    let important = false;
    const imp = /\s*!important\s*$/i.exec(value);
    if (imp) {
      important = true;
      value = value.slice(0, imp.index).trim();
    }
    out.push({ prop: chunk.slice(0, at).trim().toLowerCase(), value, important });
  };
  for (let i = 0; i < body.length; i++) {
    const ch = body[i];
    if (ch === '"' || ch === "'") i = skipString(body, i);
    else if (ch === "(") depth++;
    else if (ch === ")") depth--;
    else if (ch === ";" && depth === 0) push(i);
  }
  push(body.length);
  return out;
}

const SIDES = ["top", "right", "bottom", "left"];

/** The shorthands the checks care about, as longhands (the others are kept as written). */
function expandDecl(d) {
  const { prop, value, important } = d;
  const mk = (p, v) => ({ prop: p, value: v, important });
  const box = (name, v) => {
    const [a, b = a, c = a, e = b] = splitTop(v, "ws");
    return [mk(`${name}-top`, a), mk(`${name}-right`, b), mk(`${name}-bottom`, c), mk(`${name}-left`, e)];
  };
  if (prop === "padding" || prop === "margin") return box(prop, value);
  if (prop === "overflow") {
    const [x, y = x] = splitTop(value, "ws");
    return [mk("overflow-x", x), mk("overflow-y", y)];
  }
  if (prop === "gap") {
    const [r, c = r] = splitTop(value, "ws");
    return [mk("row-gap", r), mk("column-gap", c)];
  }
  if (prop === "flex") {
    const v = value.trim();
    if (v === "none") return [mk("flex-grow", "0"), mk("flex-shrink", "0"), mk("flex-basis", "auto")];
    if (v === "auto") return [mk("flex-grow", "1"), mk("flex-shrink", "1"), mk("flex-basis", "auto")];
    const t = splitTop(v, "ws");
    if (t.length === 1) return /^[\d.]+$/.test(t[0]) ? [mk("flex-grow", t[0]), mk("flex-shrink", "1"), mk("flex-basis", "0px")] : [mk("flex-grow", "1"), mk("flex-shrink", "1"), mk("flex-basis", t[0])];
    if (t.length === 2) return /^[\d.]+$/.test(t[1]) ? [mk("flex-grow", t[0]), mk("flex-shrink", t[1]), mk("flex-basis", "0px")] : [mk("flex-grow", t[0]), mk("flex-shrink", "1"), mk("flex-basis", t[1])];
    return [mk("flex-grow", t[0]), mk("flex-shrink", t[1]), mk("flex-basis", t[2])];
  }
  if (prop === "flex-flow") {
    const out = [];
    for (const w of splitTop(value, "ws")) {
      if (/^(row|column)(-reverse)?$/.test(w)) out.push(mk("flex-direction", w));
      else if (/^(nowrap|wrap|wrap-reverse)$/.test(w)) out.push(mk("flex-wrap", w));
    }
    return out;
  }
  return [d];
}

function parseRules(css, media, counter) {
  const rules = [];
  let i = 0;
  const n = css.length;
  while (i < n) {
    while (i < n && /\s/.test(css[i])) i++;
    if (i >= n) break;
    let j = i;
    let depth = 0;
    while (j < n) {
      const ch = css[j];
      if (ch === '"' || ch === "'") j = skipString(css, j);
      else if (ch === "(" || ch === "[") depth++;
      else if (ch === ")" || ch === "]") depth--;
      else if ((ch === "{" || ch === ";") && depth === 0) break;
      j++;
    }
    if (css[j] === ";" || j >= n) {
      i = j + 1;
      continue;
    }
    const prelude = css.slice(i, j).trim();
    let k = j + 1;
    let d = 1;
    while (k < n && d > 0) {
      const ch = css[k];
      if (ch === '"' || ch === "'") k = skipString(css, k);
      else if (ch === "{") d++;
      else if (ch === "}") d--;
      k++;
    }
    const inner = css.slice(j + 1, k - 1);
    i = k;
    if (/^@media\b/i.test(prelude)) rules.push(...parseRules(inner, [...media, prelude.slice(6).trim()], counter));
    else if (/^@supports\b/i.test(prelude)) rules.push(...parseRules(inner, media, counter)); // (everything it asks for is supported: dvh ...)
    else if (prelude.startsWith("@")) continue; // keyframes, font-face ...
    else {
      const decls = parseDecls(inner).flatMap(expandDecl);
      const selectors = splitTop(prelude, ",").map(parseSelector);
      rules.push({ selectors, decls, media, order: counter.n++ });
    }
  }
  return rules;
}

function mediaMatches(list, vp) {
  // list: the @media preludes this rule sits in (all of them must hold)
  return list.every((query) =>
    splitTop(query, ",").some((q) =>
      q.split(/\s+and\s+/i).every((cond) => {
        const c = cond.trim().toLowerCase();
        if (c === "screen" || c === "all") return true;
        if (c === "print") return false;
        const m = /^\(\s*([a-z-]+)\s*(?::\s*([^)]+?))?\s*\)$/.exec(c);
        if (!m) return false;
        const [, feature, value] = m;
        const px = (v) => parseFloat(v);
        if (feature === "max-width") return vp.width <= px(value);
        if (feature === "min-width") return vp.width >= px(value);
        if (feature === "max-height") return vp.height <= px(value);
        if (feature === "min-height") return vp.height >= px(value);
        if (feature === "prefers-reduced-motion") return value === (vp.reducedMotion ? "reduce" : "no-preference");
        if (feature === "hover") return value === (vp.hover ? "hover" : "none");
        if (feature === "pointer") return value === (vp.hover ? "fine" : "coarse");
        if (feature === "orientation") return value === (vp.height >= vp.width ? "portrait" : "landscape");
        return false;
      })
    )
  );
}

// ==================================================================================================================================
// values
// ==================================================================================================================================
const INHERITED = new Set(["color", "font-size", "font-weight", "font-family", "line-height", "white-space", "visibility", "cursor", "text-align", "word-break", "overflow-wrap", "pointer-events", "user-select"]);

const UA_DISPLAY = {
  div: "block", section: "block", article: "block", aside: "block", header: "block", footer: "block", main: "block", nav: "block", form: "block", fieldset: "block",
  p: "block", h1: "block", h2: "block", h3: "block", h4: "block", h5: "block", h6: "block", pre: "block", ul: "block", ol: "block", dl: "block", dt: "block", dd: "block",
  blockquote: "block", details: "block", summary: "block", hr: "block", li: "list-item", table: "table", thead: "table-header-group", tbody: "table-row-group",
  tr: "table-row", td: "table-cell", th: "table-cell", caption: "table-caption", button: "inline-block", input: "inline-block", select: "inline-block",
  textarea: "inline-block", img: "inline", span: "inline", a: "inline", code: "inline", strong: "inline", em: "inline", b: "inline", i: "inline", label: "inline",
  small: "inline", head: "none", script: "none", style: "none", meta: "none", link: "none", title: "none", template: "none", html: "block", body: "block",
};

/** A tiny evaluator for lengths: `12px`, `1.5rem`, `100vw`, `calc(100vw - 16px)`, `min(340px, calc(100vw - 16px))`, `clamp(a, b, c)`.  NaN when it cannot say (`%`, `auto` ...). */
function evalLength(text, ctx) {
  const src = String(text)
    .replace(/env\([^()]*(?:\([^()]*\)[^()]*)*\)/gi, "0px") // the safe areas of a phone: nothing at rest
    .trim();
  const tokens = [];
  const re = /\s*(?:([a-z-]+)\(|(-?\d*\.?\d+)([a-z%]*)|([+\-*/(),]))/gi;
  let last = 0;
  let m;
  while ((m = re.exec(src))) {
    if (m.index !== last) return NaN;
    last = re.lastIndex;
    if (m[1]) tokens.push({ t: "fn", v: m[1].toLowerCase() });
    else if (m[2] !== undefined) tokens.push({ t: "num", n: Number(m[2]), u: (m[3] || "").toLowerCase() });
    else tokens.push({ t: "op", v: m[4] });
  }
  if (last !== src.length || !tokens.length) return NaN;
  let pos = 0;
  const unit = (n, u) => {
    switch (u) {
      case "":
        return n;
      case "px":
        return n;
      case "rem":
      case "em":
        return n * (ctx.fontSize || 16);
      case "vw":
        return (n * ctx.width) / 100;
      case "vh":
      case "dvh":
      case "svh":
      case "lvh":
        return (n * ctx.height) / 100;
      case "vmin":
        return (n * Math.min(ctx.width, ctx.height)) / 100;
      case "vmax":
        return (n * Math.max(ctx.width, ctx.height)) / 100;
      default:
        return NaN; // % ...
    }
  };
  function sum() {
    let v = prod();
    while (pos < tokens.length && tokens[pos].t === "op" && (tokens[pos].v === "+" || tokens[pos].v === "-")) {
      const op = tokens[pos++].v;
      const r = prod();
      v = op === "+" ? v + r : v - r;
    }
    return v;
  }
  function prod() {
    let v = atom();
    while (pos < tokens.length && tokens[pos].t === "op" && (tokens[pos].v === "*" || tokens[pos].v === "/")) {
      const op = tokens[pos++].v;
      const r = atom();
      v = op === "*" ? v * r : v / r;
    }
    return v;
  }
  function atom() {
    const t = tokens[pos++];
    if (!t) return NaN;
    if (t.t === "num") return unit(t.n, t.u);
    if (t.t === "op" && t.v === "(") {
      const v = sum();
      pos++; // )
      return v;
    }
    if (t.t === "fn") {
      const args = [];
      if (!(tokens[pos] && tokens[pos].t === "op" && tokens[pos].v === ")")) {
        args.push(sum());
        while (tokens[pos] && tokens[pos].t === "op" && tokens[pos].v === ",") {
          pos++;
          args.push(sum());
        }
      }
      pos++; // )
      if (t.v === "calc") return args[0];
      if (t.v === "min") return Math.min(...args);
      if (t.v === "max") return Math.max(...args);
      if (t.v === "clamp") return Math.min(Math.max(args[0], args[1]), args[2]);
      if (t.v === "env") return 0;
      return NaN;
    }
    return NaN;
  }
  const v = sum();
  return pos === tokens.length ? v : NaN;
}

// ==================================================================================================================================
// the engine
// ==================================================================================================================================
class Engine {
  /**
   * `css`: the text of the style sheet.  `viewport`: {width, height, hover (false: a touch screen), reducedMotion}.
   */
  constructor(css, viewport) {
    this.vp = { width: 390, height: 844, hover: false, reducedMotion: false, ...(viewport || {}) };
    const all = parseRules(stripComments(css), [], { n: 0 });
    this.rules = all.filter((r) => mediaMatches(r.media, this.vp));
    this.droppedByMedia = all.length - this.rules.length;
    this._matched = new WeakMap();
    this._computed = new WeakMap();
  }

  /** Every declaration that applies to `el`: [{prop, value, important, spec, order}] (unsorted). */
  matched(el) {
    let list = this._matched.get(el);
    if (list) return list;
    list = [];
    for (const rule of this.rules) {
      let best = null;
      for (const sel of rule.selectors) {
        if (sel.pseudoElement || !sel.parts.length) continue;
        if (matchComplex(el, sel.parts, sel.parts.length - 1)) {
          if (!best || cmpSpec(sel.spec, best) > 0) best = sel.spec;
        }
      }
      if (best) for (const d of rule.decls) list.push({ ...d, spec: best, order: rule.order });
    }
    // the style attribute beats every selector
    if (el.attrs && el.attrs.style) {
      for (const d of parseDecls(el.attrs.style).flatMap(expandDecl)) list.push({ ...d, spec: [1, 0, 0, 0], order: Infinity });
    }
    this._matched.set(el, list);
    return list;
  }

  /** The winning declaration for `prop` at `el` (no inheritance, no defaults): {value, ...} or null. */
  cascaded(el, prop) {
    let win = null;
    for (const d of this.matched(el)) {
      if (d.prop !== prop) continue;
      if (!win || beats(d, win)) win = d;
    }
    return win;
  }

  /** The value of a property for an element, with `var()` resolved and inheritance / the defaults of the browser applied; "" when there is none. */
  computed(el, prop) {
    let cache = this._computed.get(el);
    if (!cache) this._computed.set(el, (cache = new Map()));
    if (cache.has(prop)) return cache.get(prop);
    cache.set(prop, ""); // (a cycle of var() ends here)
    let value = "";
    const win = this.cascaded(el, prop);
    if (win && !/^(inherit|initial|unset|revert)$/i.test(win.value)) value = this.resolveVars(win.value, el);
    else if (prop.startsWith("--") || INHERITED.has(prop) || (win && /^inherit$/i.test(win.value))) value = el.parent && el.parent.tag !== "#root" ? this.computed(el.parent, prop) : "";
    else value = this.initial(el, prop);
    cache.set(prop, value);
    return value;
  }

  /** What a property is without any rule about it (the browser's own style sheet, where it matters here). */
  initial(el, prop) {
    if (prop === "display") return el.attrs && "hidden" in el.attrs ? "none" : UA_DISPLAY[el.tag] || "inline";
    if (prop === "flex-direction") return "row";
    if (prop === "flex-wrap") return "nowrap";
    if (prop === "flex-grow") return "0";
    if (prop === "flex-shrink") return "1";
    if (prop === "flex-basis") return "auto";
    if (prop === "position") return "static";
    if (prop === "overflow-x" || prop === "overflow-y") return "visible";
    if (prop === "white-space") return "normal";
    return "";
  }

  resolveVars(value, el) {
    let out = String(value);
    for (let guard = 0; guard < 8 && /var\(/.test(out); guard++) {
      out = out.replace(/var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\)[^()]*)*))?\)/g, (all, name, fallback) => {
        const v = this.computed(el, name);
        return v !== "" ? v : fallback !== undefined ? fallback.trim() : "";
      });
    }
    return out;
  }

  /** `display` after the author's rules AND the `hidden` attribute (an author rule with a display beats the attribute, as in a browser). */
  display(el) {
    const win = this.cascaded(el, "display");
    if (win) return this.resolveVars(win.value, el).trim().toLowerCase();
    return this.initial(el, "display");
  }

  /** Is the element drawn at all: no `display: none` on it or on an ancestor. */
  rendered(el) {
    for (let n = el; n && n.tag !== "#root"; n = n.parent) {
      if (n.tag === "#text") continue;
      if (this.display(n) === "none") return false;
    }
    return true;
  }

  /** A length of the element in px (NaN when it is `auto`, a percentage, or missing). */
  px(el, prop) {
    const v = this.computed(el, prop);
    if (v === "") return NaN;
    return evalLength(v, { width: this.vp.width, height: this.vp.height, fontSize: this.fontSize(el) });
  }

  fontSize(el) {
    const v = this.computed(el, "font-size");
    const n = v ? evalLength(v, { width: this.vp.width, height: this.vp.height, fontSize: 16 }) : NaN;
    return Number.isFinite(n) ? n : 16;
  }
}

function cmpSpec(a, b) {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const d = (a[i] || 0) - (b[i] || 0);
    if (d) return d;
  }
  return 0;
}

/** Does declaration `a` win against `b`?  (important, then specificity, then the later one) */
function beats(a, b) {
  if (a.important !== b.important) return a.important;
  const s = cmpSpec(a.spec, b.spec);
  if (s) return s > 0;
  return a.order >= b.order;
}

// ==================================================================================================================================
// a page
// ==================================================================================================================================
/**
 * The page of index.html as a tree, in a given context (`htmlAttrs` replace the attributes of <html>: data-layout, data-input ...), with the markup the scripts of a
 * fake-DOM run (`env.els`, harness.js) have drawn into their elements put in, and the state they gave their elements (`hidden`, `style.display`, `disabled`).
 */
function buildPage(indexHtml, env, htmlAttrs) {
  const top = parseHtml(indexHtml);
  const html = top.find((n) => n.tag === "html") || top[0];
  html.attrs = { ...html.attrs, ...(htmlAttrs || {}) };
  const page = { html, root: { tag: "#root", attrs: {}, children: [html], parent: null } };
  html.parent = page.root;
  if (env && env.els) {
    const pending = new Map([...env.els].filter(([, el]) => el));
    for (let pass = 0; pass < 8 && pending.size; pass++) {
      let progress = false;
      for (const [id, fake] of [...pending]) {
        const node = findById(html, id);
        if (!node) continue;
        pending.delete(id);
        progress = true;
        if (fake._html) setInnerHtml(node, fake._html);
        // (the fake element starts as the page's markup says - harness.js - and the scripts move it from there)
        if (fake.hidden === true) node.attrs.hidden = "";
        else if (fake.hidden === false) delete node.attrs.hidden;
        if (fake.style && fake.style.display) node.attrs.style = `display: ${fake.style.display}`;
        if (fake.disabled === true) node.attrs.disabled = "";
        if (fake.className && typeof fake.className === "string" && fake.className.trim()) node.attrs.class = fake.className;
        // what the scripts gave it with setAttribute (data-phone-tab, aria-expanded ...) and with classList (`open`, `active` ...)
        for (const [k, v] of Object.entries(fake.attrs || {})) node.attrs[k] = String(v);
        if (fake._classes && fake._classes.size) node.attrs.class = [...new Set([...(node.attrs.class || "").split(/\s+/).filter(Boolean), ...fake._classes])].join(" ");
      }
      if (!progress) break;
    }
  }
  return page;
}

module.exports = {
  parseHtml,
  parseCss: (css) => parseRules(stripComments(css), [], { n: 0 }),
  parseSelector,
  matchComplex,
  mediaMatches,
  evalLength,
  splitTop,
  Engine,
  buildPage,
  findById,
  walk,
  textOf,
  classesOf,
  elementChildren,
  isText,
  setInnerHtml,
};
