/* ==========================================================================
   bench/label/label.js - the labelling page of the calibration set (docs/v0.14-plan.md H48)

   A person labels a few dozen items with the SAME schema a judge fills in (bench/judges/<rubric>.md); `python -m bench calibrate` then compares the two.
   The page is offline: it makes no network request and has no dependency.  It reads set.jsonl (written by `python -m bench calibration-set`: rows of
   "arena-bench-label-item", each with the blind view of an item - never the bot, the run, the situation, the truth or a judge's verdict), shows one item
   at a time with a form generated from the item's schema, keeps every change in localStorage (the key holds a fingerprint of the file, so another file never
   mixes with it) and exports labels.jsonl (rows of "arena-bench-label").

   Everything that decides something is a pure function (node loads this file: `module.exports`; the browser gets `window.ArenaBenchLabel`):
     parseSet, fingerprintOf, storageKey          the file and the key of its progress
     formModel, finalLabel, validateLabel         the form of an item, the label it makes, the check of that label against the schema
     displayModel, guideHtml, renderHtml          what is shown (every dynamic string is escaped)
     exportRows                                   labels.jsonl
     keyAction                                    keyboard -> action
     createController                             the state machine of the page (no DOM; `view()` is everything the page shows)
   `mount(document, window)` is the thin layer that connects the controller to the DOM.
   ========================================================================== */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module && module.exports) {
    module.exports = api;
  } else {
    root.ArenaBenchLabel = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const ITEM_FORMAT = "arena-bench-label-item";
  const LABEL_FORMAT = "arena-bench-label";
  const STORAGE_PREFIX = "arena-bench-label/v1/";
  const PREFS_KEY = "arena-bench-label/prefs";
  const DEFAULT_LABELER = "user";
  const MAX_LABELER = 40;
  const MAX_TEXT = 500;
  /**
   * Keys the page never shows, whatever the set file holds: who spoke, which run, the truth, a judge's answer.  (`situation` is not one of them: in an E2 item it
   * is the description of the scene.  The id of a situation is checked where the set is made: `calibration-set` drops an item whose view names one.)
   */
  const DENY_KEYS = new Set(["item_id", "bot", "bot_label", "brain", "run", "sample", "truth", "judge", "judgment", "verdict", "label", "meta", "attempts"]);

  const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const str = (value) => (value === undefined || value === null ? "" : String(value));
  const clip = (value, max) => {
    const s = str(value);
    return s.length > max ? s.slice(0, max) + "…" : s;
  };

  function escapeHtml(value) {
    return str(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ----------------------------------------------------------------------------------------------------------------------- the set file
  /** Fields a form can be made of: a choice (enum), yes / no, a short text, a number. */
  function fieldType(sub) {
    if (!isObject(sub)) return "unsupported";
    if (Array.isArray(sub.enum) && sub.enum.length) return "enum";
    if (sub.type === "boolean") return "boolean";
    if (sub.type === "string") return "text";
    if (sub.type === "integer" || sub.type === "number") return "number";
    return "unsupported";
  }

  function parseJsonl(input) {
    const rows = [];
    const problems = [];
    const lines = str(input).replace(/^\uFEFF/, "").split(/\r?\n/);
    lines.forEach((line, index) => {
      if (!line.trim()) return;
      let value;
      try {
        value = JSON.parse(line);
      } catch (err) {
        problems.push(`第 ${index + 1} 行不是合法的 JSON，已跳过`);
        return;
      }
      if (!isObject(value)) {
        problems.push(`第 ${index + 1} 行不是一个 JSON 对象，已跳过`);
        return;
      }
      rows.push({ line: index + 1, value });
    });
    return { rows, problems };
  }

  /** What is wrong with one row of the set ("" = nothing).  The message never repeats the row's content (a wrong file may be a judge_items.jsonl). */
  function checkItemRow(row) {
    if (row.format !== ITEM_FORMAT) return `format 是 ${JSON.stringify(clip(row.format, 40))}，不是 ${ITEM_FORMAT}（要选 calibration-set 写出的 set.jsonl）`;
    if (typeof row.blind_id !== "string" || !row.blind_id) return "缺 blind_id";
    if (typeof row.rubric !== "string" || !row.rubric) return "缺 rubric";
    if (!Number.isInteger(row.rubric_version) || row.rubric_version < 1) return "rubric_version 要是正整数";
    const schema = row.schema;
    if (!isObject(schema) || schema.type !== "object" || !isObject(schema.properties) || !Object.keys(schema.properties).length) return "schema 要是带 properties 的 object";
    for (const name of Object.keys(schema.properties)) {
      if (fieldType(schema.properties[name]) === "unsupported") return `字段 ${name} 的类型这个页面不支持`;
    }
    if (typeof row.primary !== "string" || !has(schema.properties, row.primary)) return "primary 要是 schema 里的一个字段";
    if (!isObject(row.view)) return "缺 view";
    return "";
  }

  function parseSet(input) {
    const parsed = parseJsonl(input);
    const items = [];
    const problems = parsed.problems.slice();
    const seen = new Set();
    for (const { line, value } of parsed.rows) {
      const bad = checkItemRow(value);
      if (bad) {
        problems.push(`第 ${line} 行：${bad}，已跳过`);
        continue;
      }
      if (seen.has(value.blind_id)) {
        problems.push(`第 ${line} 行：blind_id 重复，已跳过`);
        continue;
      }
      seen.add(value.blind_id);
      items.push(value);
    }
    return { items, problems };
  }

  /** cyrb53: a small, fast, well mixed 53-bit hash (not a secret: only the name of a file's progress). */
  function hash53(input, seed) {
    let h1 = 0xdeadbeef ^ seed;
    let h2 = 0x41c6ce57 ^ seed;
    for (let i = 0; i < input.length; i++) {
      const code = input.charCodeAt(i);
      h1 = Math.imul(h1 ^ code, 2654435761);
      h2 = Math.imul(h2 ^ code, 1597334677);
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    return 4294967296 * (2097151 & h2) + (h1 >>> 0);
  }

  /** The fingerprint of a set: of the content of its (valid) rows, so another set - or the same items under another rubric version - has another one. */
  function fingerprintOf(items) {
    const joined = items.map((item) => JSON.stringify(item)).join("\n");
    const hex = (n) => n.toString(16).padStart(14, "0");
    return hex(hash53(joined, 0)) + hex(hash53(joined, 1));
  }

  function storageKey(fingerprint) {
    return STORAGE_PREFIX + fingerprint;
  }

  // ----------------------------------------------------------------------------------------------------------------------- the schema
  const sameValue = (a, b) => a === b || ((isObject(a) || Array.isArray(a)) && JSON.stringify(a) === JSON.stringify(b));

  /**
   * What is wrong with `value` as an answer to `schema` (the subset the rubrics use: type / properties / required / additionalProperties / items / enum /
   * const / oneOf) - the same checks as `bench.judging.validate_verdict`; an empty list = it fits.
   */
  function validateLabel(schema, value, path) {
    const where = path || "label";
    const problems = [];
    if (!isObject(schema)) return problems;
    if (has(schema, "const") && !sameValue(value, schema.const)) problems.push(`${where}：只能是 ${JSON.stringify(schema.const)}`);
    if (Array.isArray(schema.enum) && !schema.enum.some((option) => sameValue(option, value))) problems.push(`${where}：${JSON.stringify(value)} 不在 ${JSON.stringify(schema.enum)} 里`);
    if (Array.isArray(schema.oneOf)) {
      const fits = schema.oneOf.filter((sub) => validateLabel(sub, value, where).length === 0).length;
      if (fits !== 1) problems.push(`${where}：要正好符合 oneOf 里的一项`);
    }
    const kind = schema.type;
    if (kind === "object") {
      if (!isObject(value)) return problems.concat([`${where}：要是一个对象`]);
      const properties = isObject(schema.properties) ? schema.properties : {};
      for (const name of schema.required || []) {
        if (!has(value, name)) problems.push(`${where}：缺 ${name}`);
      }
      if (schema.additionalProperties === false) {
        for (const name of Object.keys(value)) if (!has(properties, name)) problems.push(`${where}：多出 ${name}`);
      }
      for (const name of Object.keys(properties)) {
        if (has(value, name)) problems.push(...validateLabel(properties[name], value[name], `${where}.${name}`));
      }
    } else if (kind === "array") {
      if (!Array.isArray(value)) return problems.concat([`${where}：要是一个数组`]);
      if (schema.items) value.forEach((entry, index) => problems.push(...validateLabel(schema.items, entry, `${where}[${index}]`)));
    } else if (kind === "string" && typeof value !== "string") {
      problems.push(`${where}：要是字符串`);
    } else if (kind === "boolean" && typeof value !== "boolean") {
      problems.push(`${where}：要是 true / false`);
    } else if (kind === "integer" && !(typeof value === "number" && Number.isInteger(value))) {
      problems.push(`${where}：要是整数`);
    } else if (kind === "number" && !(typeof value === "number" && Number.isFinite(value))) {
      problems.push(`${where}：要是数字`);
    }
    return problems;
  }

  // ----------------------------------------------------------------------------------------------------------------------- the form of an item
  const CAPTIONS = {
    basis: "主要依据",
    cites_decisive: "有没有明确提到决定性事实",
    reason: "理由",
    match: "哪一张风格卡",
    runner_up: "第二可能的卡",
    confidence: "把握有多大",
    cues: "风格线索",
    closer: "哪一段更像这位真人",
    recall: "有没有答对",
  };
  const hintCache = new WeakMap();

  function plainText(value) {
    return str(value).replace(/\*\*/g, "").replace(/`/g, "");
  }

  /** The one-line definitions of a rubric's choices, found in its guide ("- `trivia`：主要依据是……"): {value: sentence}. */
  function enumHints(guide) {
    const hints = Object.create(null);
    for (const line of str(guide).split(/\r?\n/)) {
      const match = /^\s*[-*]\s*`([^`]+)`\s*[：:]\s*(.+?)\s*$/.exec(line);
      if (!match || match[1] in hints) continue;
      const sentence = plainText(match[2]);
      const stop = sentence.indexOf("。");
      hints[match[1]] = stop >= 8 && stop < 90 ? sentence.slice(0, stop) : clip(sentence, 80);
    }
    return hints;
  }

  function hintsOf(item) {
    if (!isObject(item)) return enumHints("");
    if (!hintCache.has(item)) hintCache.set(item, enumHints(item.guide));
    return hintCache.get(item);
  }

  function isAnswered(type, value) {
    if (type === "text") return typeof value === "string" && value.trim() !== "";
    if (type === "number") return typeof value === "number" && Number.isFinite(value);
    return value !== undefined;
  }

  /**
   * The form of one item: a field per property of its schema.
   *   enum    -> a button group, the first nine options on the keys 1-9;   boolean -> 是 / 否 on Y / N;
   *   text    -> a one-line box: optional, except the rubric's primary field;   number -> a box (optional unless required).
   * `fields` = what is answered so far; `focus` = the field the keys act on (null: the first one not answered yet).
   * Returns {fields: [...], active, missing: [names], complete}.
   */
  function formModel(item, fields, focus) {
    const schema = item.schema;
    const properties = schema.properties;
    const required = new Set(schema.required || []);
    const hints = hintsOf(item);
    const list = [];
    for (const name of Object.keys(properties)) {
      const sub = properties[name];
      const type = fieldType(sub);
      const value = has(fields, name) ? fields[name] : undefined;
      const mustFill = type === "text" ? name === item.primary : required.has(name);
      const field = {
        name,
        type,
        caption: CAPTIONS[name] || name,
        mustFill,
        value,
        answered: isAnswered(type, value),
        options: null,
      };
      if (type === "enum") {
        field.options = sub.enum.map((option, index) => ({
          index,
          value: option,
          label: str(option),
          key: index < 9 ? String(index + 1) : "",
          hint: hints[str(option)] || "",
          selected: value !== undefined && sameValue(value, option),
        }));
      } else if (type === "boolean") {
        field.options = [
          { index: 0, value: true, label: "是", key: "Y", hint: "", selected: value === true },
          { index: 1, value: false, label: "否", key: "N", hint: "", selected: value === false },
        ];
      }
      list.push(field);
    }
    const choices = list.filter((field) => field.options);
    let active = null;
    if (focus && choices.some((field) => field.name === focus)) {
      active = focus;
    } else {
      const open = choices.find((field) => !field.answered);
      active = open ? open.name : choices.length ? choices[choices.length - 1].name : null;
    }
    for (const field of list) field.active = field.name === active;
    const missing = list.filter((field) => field.mustFill && !field.answered).map((field) => field.name);
    return { fields: list, active, missing, complete: missing.length === 0 };
  }

  /** The label an answered form makes: the schema's fields in the schema's order; a required text that was left empty is "" (the schema wants the key). */
  function finalLabel(item, fields) {
    const required = new Set(item.schema.required || []);
    const label = {};
    for (const name of Object.keys(item.schema.properties)) {
      const type = fieldType(item.schema.properties[name]);
      const value = has(fields, name) ? fields[name] : undefined;
      if (type === "text") {
        const written = typeof value === "string" ? value.trim() : "";
        if (written !== "" || required.has(name)) label[name] = written;
      } else if (value !== undefined) {
        label[name] = value;
      }
    }
    return label;
  }

  function cleanLabeler(name) {
    const cleaned = str(name).replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim().slice(0, MAX_LABELER);
    return cleaned || DEFAULT_LABELER;
  }

  /**
   * labels.jsonl: a row for every item whose form is complete AND whose label fits its schema; the others are not exported (`rejected` lists the ones that
   * are complete but do not fit).  `answers` = {blind_id: {fields, ts}}.
   */
  function exportRows(items, answers, options) {
    const labeler = cleanLabeler(options && options.labeler);
    const rows = [];
    const rejected = [];
    for (const item of items) {
      const entry = has(answers, item.blind_id) ? answers[item.blind_id] : null;
      if (!entry) continue;
      const fields = isObject(entry.fields) ? entry.fields : {};
      if (!formModel(item, fields, null).complete) continue;
      const label = finalLabel(item, fields);
      const problems = validateLabel(item.schema, label);
      if (problems.length) {
        rejected.push({ blind_id: item.blind_id, rubric: item.rubric, problems });
        continue;
      }
      rows.push({
        format: LABEL_FORMAT,
        version: 1,
        blind_id: item.blind_id,
        rubric: item.rubric,
        rubric_version: item.rubric_version,
        label,
        labeler,
        ts: str(entry.ts),
      });
    }
    return { rows, rejected, text: rows.map((row) => JSON.stringify(row)).join("\n") + (rows.length ? "\n" : "") };
  }

  // ----------------------------------------------------------------------------------------------------------------------- what is shown of an item
  const LABELS = {
    decisive: "决定性事实",
    trap: "容易被带偏的地方",
    question: "问题",
    expected: "标准答案",
    situation: "局面",
    reference: "这位真人的参考发言",
    cards: "风格卡",
    text: "发言",
    value: "选择",
    reason: "理由",
    thought: "思考",
    x: "候选 X",
    y: "候选 Y",
  };
  const CONTEXT_ORDER = ["decisive", "trap", "question", "expected", "situation", "reference"];
  const SUBJECT_ORDER = ["text", "value", "reason", "thought"];

  function cleanValue(value) {
    if (Array.isArray(value)) return value.map(cleanValue);
    if (isObject(value)) {
      const out = {};
      for (const key of Object.keys(value)) if (!DENY_KEYS.has(key)) out[key] = cleanValue(value[key]);
      return out;
    }
    return value;
  }

  function blockOf(zone, key, raw) {
    const value = cleanValue(raw);
    if (value === null || value === undefined || value === "") return null;
    const base = { zone, key, label: LABELS[key] || key };
    if (key === "cards" && Array.isArray(value)) {
      return { ...base, type: "cards", cards: value.filter(isObject).map((card) => ({ id: str(card.id), text: str(card.text) })) };
    }
    if (Array.isArray(value)) {
      return value.every((entry) => typeof entry === "string") ? { ...base, type: "list", items: value } : { ...base, type: "json", text: JSON.stringify(value, null, 2) };
    }
    if (isObject(value)) return { ...base, type: "json", text: JSON.stringify(value, null, 2) };
    return { ...base, type: "text", text: str(value) };
  }

  /**
   * What the page shows of an item's blind view: {title, brief, meta, blocks}.  The background (`context`) comes first, then the thing that is judged
   * (`subject`: a speech, a choice and its reason - or the two candidates X / Y, which are drawn side by side), then the style cards of S2 (to compare with the
   * speech).  Keys that name a bot, a run, the truth or a verdict are never shown, whatever the file holds.
   */
  function displayModel(item) {
    const view = isObject(item.view) ? item.view : {};
    const context = isObject(view.context) ? view.context : {};
    const subject = isObject(view.subject) ? view.subject : {};
    const blocks = [];
    const contextKeys = CONTEXT_ORDER.filter((key) => has(context, key)).concat(Object.keys(context).filter((key) => !CONTEXT_ORDER.includes(key) && key !== "cards"));
    for (const key of contextKeys) {
      if (DENY_KEYS.has(key)) continue;
      const block = blockOf("context", key, context[key]);
      if (block) blocks.push(block);
    }
    const pair = subject.kind === "pair" || has(subject, "x") || has(subject, "y");
    const subjectKeys = pair
      ? ["x", "y"]
      : SUBJECT_ORDER.concat(Object.keys(subject).filter((key) => !SUBJECT_ORDER.includes(key) && key !== "kind" && key !== "tool"));
    for (const key of subjectKeys) {
      if (DENY_KEYS.has(key) || !has(subject, key)) continue;
      const block = blockOf("subject", key, subject[key]);
      if (!block) continue;
      if (pair) block.side = key.toUpperCase();
      blocks.push(block);
    }
    if (has(context, "cards")) {
      const block = blockOf("context", "cards", context.cards);
      if (block) blocks.push(block);
    }
    const meta = [];
    if (subject.kind) meta.push({ label: "类型", value: str(subject.kind) });
    if (subject.tool) meta.push({ label: "工具", value: str(subject.tool) });
    return { title: str(view.title), brief: str(view.brief), meta, blocks };
  }

  function inlineHtml(value) {
    return escapeHtml(value).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");
  }

  /** The rubric's guide (a little markdown: headings, bullets, **bold**, `code`) as HTML: everything is escaped first. */
  function guideHtml(input) {
    const out = [];
    let inList = false;
    const closeList = () => {
      if (inList) out.push("</ul>");
      inList = false;
    };
    for (const raw of str(input).split(/\r?\n/)) {
      const line = raw.replace(/\s+$/, "");
      if (!line.trim()) {
        closeList();
        continue;
      }
      let match = /^(#{1,4})\s+(.*)$/.exec(line);
      if (match) {
        closeList();
        out.push(`<h4>${inlineHtml(match[2])}</h4>`);
        continue;
      }
      match = /^\s*[-*]\s+(.*)$/.exec(line);
      if (match) {
        if (!inList) out.push("<ul>");
        inList = true;
        out.push(`<li>${inlineHtml(match[1])}</li>`);
        continue;
      }
      closeList();
      out.push(`<p>${inlineHtml(line)}</p>`);
    }
    closeList();
    return out.join("");
  }

  // ----------------------------------------------------------------------------------------------------------------------- the keyboard
  /**
   * A key press -> an action (or null).  `ev` = {key, repeat, composing, ctrlKey, metaKey, altKey, targetTag, inField}: `inField` = the target is one of the
   * item's text boxes.  Typing in a box only knows Enter (save and next) and Escape; a key that is held down is taken once; the browser's own shortcuts are
   * left alone - and so is every key while an input method is composing (`composing`): the Enter that confirms a Chinese candidate belongs to the input
   * method, it must not save the item and take the page to the next one (nor Escape, which cancels the composition).
   */
  function keyAction(ev) {
    if (!ev || ev.repeat || ev.composing || ev.ctrlKey || ev.metaKey || ev.altKey) return null;
    const key = ev.key;
    const tag = str(ev.targetTag).toUpperCase();
    if (tag === "SELECT") return null;
    if (tag === "INPUT" || tag === "TEXTAREA") {
      if (key === "Escape") return { type: "blur" };
      if (key === "Enter") return ev.inField ? { type: "saveNext" } : { type: "blur" };
      return null;
    }
    if (key === "ArrowLeft") return { type: "prev" };
    if (key === "ArrowRight") return { type: "next" };
    if (key === "Enter") return tag === "BUTTON" || tag === "A" || tag === "SUMMARY" ? null : { type: "saveNext" };
    if (key === "s" || key === "S") return { type: "skip" };
    if (key === "y" || key === "Y") return { type: "chooseKey", kind: "boolean", value: true };
    if (key === "n" || key === "N") return { type: "chooseKey", kind: "boolean", value: false };
    if (typeof key === "string" && /^[1-9]$/.test(key)) return { type: "chooseKey", kind: "enum", number: Number(key) };
    return null;
  }

  // ----------------------------------------------------------------------------------------------------------------------- the state machine
  const THEMES = ["auto", "light", "dark"];
  const THEME_LABELS = { auto: "跟随系统", light: "浅色", dark: "深色" };
  const STATUS_LABELS = { done: "✔ 已标", open: "○ 没标", skipped: "↷ 跳过了" };

  /** A saved answer kept only if the item still exists and the value still fits its field. */
  function sanitizeSaved(items, saved) {
    const result = { answers: {}, skipped: {}, cursor: 0 };
    if (!isObject(saved)) return result;
    const byId = new Map(items.map((item) => [item.blind_id, item]));
    if (isObject(saved.answers)) {
      for (const id of Object.keys(saved.answers)) {
        const item = byId.get(id);
        const entry = saved.answers[id];
        if (!item || !isObject(entry) || !isObject(entry.fields)) continue;
        const fields = {};
        for (const name of Object.keys(item.schema.properties)) {
          if (has(entry.fields, name) && validateLabel(item.schema.properties[name], entry.fields[name], name).length === 0) fields[name] = entry.fields[name];
        }
        result.answers[id] = { fields, ts: typeof entry.ts === "string" ? entry.ts : "" };
      }
    }
    if (isObject(saved.skipped)) {
      for (const id of Object.keys(saved.skipped)) if (byId.has(id) && saved.skipped[id] === true) result.skipped[id] = true;
    }
    if (Number.isInteger(saved.cursor) && saved.cursor >= 0 && saved.cursor < items.length) result.cursor = saved.cursor;
    return result;
  }

  /**
   * The page without a DOM.  options: {storage (getItem / setItem / removeItem, or null), now (a function: ISO time)}.
   * `load(text, fileName)` opens a set; `dispatch(action)` changes something and says what the page has to do ({rerender, download?, blur?, scrollTop?});
   * `view()` is everything the page shows.
   */
  function createController(options) {
    const opts = options || {};
    const storage = opts.storage || null;
    const now = opts.now || (() => new Date().toISOString());
    const state = {
      items: [],
      problems: [],
      fileName: "",
      fingerprint: "",
      cursor: 0,
      answers: {},
      skipped: {},
      focusField: null,
      notice: null,
      labeler: DEFAULT_LABELER,
      theme: "auto",
      guideOpen: false,
      storageOk: Boolean(storage),
    };

    function readJson(key) {
      if (!storage) return { ok: true, value: null };
      try {
        const raw = storage.getItem(key);
        return { ok: true, value: raw ? JSON.parse(raw) : null };
      } catch (err) {
        return { ok: false, value: null };
      }
    }

    function writeJson(key, value) {
      if (!storage) return false;
      try {
        storage.setItem(key, JSON.stringify(value));
        return true;
      } catch (err) {
        return false;
      }
    }

    function savePrefs() {
      writeJson(PREFS_KEY, { theme: state.theme, labeler: state.labeler, guideOpen: state.guideOpen });
    }

    function persist() {
      if (!state.fingerprint) return;
      const ok = writeJson(storageKey(state.fingerprint), { version: 1, fileName: state.fileName, cursor: state.cursor, answers: state.answers, skipped: state.skipped });
      if (!ok) {
        state.storageOk = false;
        state.notice = { kind: "warn", text: "本地存储写不进去（隐私模式，或者空间满了）：关掉页面就会丢进度，请及时点“导出”。" };
      } else {
        state.storageOk = true;
      }
    }

    const prefs = readJson(PREFS_KEY).value;
    if (isObject(prefs)) {
      if (THEMES.includes(prefs.theme)) state.theme = prefs.theme;
      if (typeof prefs.labeler === "string") state.labeler = cleanLabeler(prefs.labeler);
      if (typeof prefs.guideOpen === "boolean") state.guideOpen = prefs.guideOpen;
    }

    const total = () => state.items.length;
    const current = () => state.items[state.cursor] || null;
    const entryOf = (item) => (has(state.answers, item.blind_id) ? state.answers[item.blind_id] : null);
    const fieldsOf = (item) => {
      const entry = entryOf(item);
      return entry ? entry.fields : {};
    };
    const isDone = (item) => {
      const entry = entryOf(item);
      return Boolean(entry) && formModel(item, entry.fields, null).complete;
    };
    const statusOf = (item) => (isDone(item) ? "done" : state.skipped[item.blind_id] ? "skipped" : "open");
    const countDone = () => state.items.filter(isDone).length;

    function touch(item) {
      if (!entryOf(item)) state.answers[item.blind_id] = { fields: {}, ts: now() };
      return state.answers[item.blind_id];
    }

    function setField(item, name, value) {
      const entry = touch(item);
      if (value === undefined) delete entry.fields[name];
      else entry.fields[name] = value;
      entry.ts = now();
      delete state.skipped[item.blind_id];
    }

    function go(index) {
      state.cursor = Math.max(0, Math.min(total() - 1, index));
      state.focusField = null;
      persist();
    }

    function missingText(item, form) {
      return "还差：" + form.missing.map((name) => (form.fields.find((field) => field.name === name) || { caption: name }).caption).join("、") + "（选好之后才能往后；不想标就按 S 跳过）";
    }

    function choose(item, form, field, option) {
      setField(item, field.name, option.value);
      const after = formModel(item, fieldsOf(item), null);
      const open = after.fields.find((candidate) => candidate.options && !candidate.answered);
      state.focusField = open ? open.name : field.name;
      persist();
    }

    function finishItem(item) {
      const form = formModel(item, fieldsOf(item), state.focusField);
      if (!form.complete) {
        state.notice = { kind: "warn", text: missingText(item, form) };
        state.focusField = form.missing.find((name) => form.fields.find((field) => field.name === name && field.options)) || state.focusField;
        return false;
      }
      touch(item);
      delete state.skipped[item.blind_id];
      return true;
    }

    function advance(saved) {
      const done = countDone();
      if (state.cursor < total() - 1) {
        go(state.cursor + 1);
        if (saved) state.notice = { kind: "ok", text: `已保存。已标 ${done} / 共 ${total()}` };
      } else {
        persist();
        state.notice =
          done === total()
            ? { kind: "ok", text: "全部标完了：点右上角的“导出 labels.jsonl”。" }
            : { kind: "info", text: `这是最后一条${saved ? "，已保存" : ""}；还有 ${total() - done} 条没标（“下一条没标的”可以找到它们）。` };
      }
    }

    function load(input, fileName) {
      const parsed = parseSet(input);
      if (!parsed.items.length) {
        state.notice = { kind: "error", text: parsed.problems.length ? "这个文件里没有可以标注的条目：" + parsed.problems.slice(0, 3).join("；") : "这个文件是空的。" };
        return { ok: false, problems: parsed.problems };
      }
      state.items = parsed.items;
      state.problems = parsed.problems;
      state.fileName = str(fileName);
      state.fingerprint = fingerprintOf(parsed.items);
      const read = readJson(storageKey(state.fingerprint));
      const saved = sanitizeSaved(parsed.items, read.value);
      state.answers = saved.answers;
      state.skipped = saved.skipped;
      state.cursor = saved.cursor;
      state.focusField = null;
      const done = countDone();
      if (!read.ok) state.notice = { kind: "warn", text: "这个文件上次的进度读不出来，已经从头开始。" };
      else if (done || Object.keys(saved.answers).length) state.notice = { kind: "info", text: `已恢复上次的进度：已标 ${done} / 共 ${total()}。` };
      else state.notice = parsed.problems.length ? { kind: "warn", text: `有 ${parsed.problems.length} 行不能用，已跳过：${parsed.problems.slice(0, 2).join("；")}` } : null;
      return { ok: true, problems: parsed.problems, restored: done }; // (nothing is written until the first change: opening a file leaves no trace)
    }

    function dispatch(action) {
      const effects = { rerender: true };
      if (!action || typeof action.type !== "string") return effects;
      const quiet = action.type === "text" || action.type === "labeler" || action.type === "guide";
      if (!quiet) state.notice = null;
      const item = current();
      const cursorBefore = state.cursor;
      switch (action.type) {
        case "choose": {
          if (!item) break;
          const form = formModel(item, fieldsOf(item), state.focusField);
          const field = form.fields.find((candidate) => candidate.name === action.field && candidate.options);
          const option = field && field.options[Number(action.option)];
          if (option) choose(item, form, field, option);
          break;
        }
        case "chooseKey": {
          if (!item) break;
          const form = formModel(item, fieldsOf(item), state.focusField);
          const same = form.fields.filter((field) => field.type === action.kind);
          if (!same.length) {
            state.notice = { kind: "info", text: action.kind === "boolean" ? "这一条没有是 / 否的题。" : "这一条没有选择题。" };
            break;
          }
          const field = same.find((candidate) => candidate.name === form.active) || same.find((candidate) => !candidate.answered) || same[same.length - 1];
          const option = action.kind === "boolean" ? field.options[action.value ? 0 : 1] : field.options[action.number - 1];
          if (!option) {
            state.notice = { kind: "info", text: `“${field.caption}”只有 ${field.options.length} 个选项，没有第 ${action.number} 项。` };
            break;
          }
          choose(item, form, field, option);
          break;
        }
        case "focusField":
          if (item && formModel(item, fieldsOf(item), null).fields.some((field) => field.name === action.field && field.options)) state.focusField = action.field;
          break;
        case "text": {
          effects.rerender = false;
          if (!item || typeof action.value !== "string") break;
          const field = formModel(item, fieldsOf(item), null).fields.find((candidate) => candidate.name === action.field);
          if (!field || (field.type !== "text" && field.type !== "number")) break;
          if (field.type === "text") {
            setField(item, field.name, action.value.slice(0, MAX_TEXT));
          } else {
            const number = action.value.trim() === "" ? NaN : Number(action.value);
            const sub = item.schema.properties[field.name];
            setField(item, field.name, Number.isFinite(number) && (sub.type !== "integer" || Number.isInteger(number)) ? number : undefined);
          }
          persist();
          break;
        }
        case "saveNext":
          if (item && finishItem(item)) advance(true);
          break;
        case "next":
          if (!item) break;
          if (statusOf(item) !== "open") advance(false);
          else if (finishItem(item)) advance(true);
          break;
        case "prev":
          if (state.cursor > 0) go(state.cursor - 1);
          else state.notice = { kind: "info", text: "这是第一条。" };
          break;
        case "skip":
          if (!item) break;
          if (!isDone(item)) state.skipped[item.blind_id] = true;
          if (state.cursor < total() - 1) go(state.cursor + 1);
          else {
            persist();
            state.notice = { kind: "info", text: "这是最后一条。" };
          }
          break;
        case "jump":
          if (Number.isInteger(Number(action.index))) go(Number(action.index));
          break;
        case "nextOpen": {
          const order = state.items.map((_, index) => (state.cursor + 1 + index) % Math.max(1, total()));
          const open = order.find((index) => statusOf(state.items[index]) === "open");
          const skipped = order.find((index) => statusOf(state.items[index]) === "skipped");
          const target = open !== undefined ? open : skipped;
          if (target === undefined) state.notice = { kind: "ok", text: "每一条都标完了：可以导出了。" };
          else go(target);
          break;
        }
        case "labeler":
          effects.rerender = false;
          state.labeler = cleanLabeler(action.value);
          savePrefs();
          break;
        case "theme":
          state.theme = THEMES[(THEMES.indexOf(state.theme) + 1) % THEMES.length];
          savePrefs();
          break;
        case "guide":
          effects.rerender = false;
          state.guideOpen = Boolean(action.open);
          savePrefs();
          break;
        case "clear":
          if (state.fingerprint && storage) {
            try {
              storage.removeItem(storageKey(state.fingerprint));
            } catch (err) {
              /* nothing to remove */
            }
          }
          state.answers = {};
          state.skipped = {};
          state.cursor = 0;
          state.focusField = null;
          state.notice = { kind: "ok", text: "已清除这个文件的本地进度。" };
          break;
        case "export": {
          const result = exportRows(state.items, state.answers, { labeler: state.labeler });
          if (!result.rows.length) {
            state.notice = { kind: "warn", text: "还没有可以导出的标注：先把条目标完（必填的选项都选了）。" };
            break;
          }
          const left = total() - result.rows.length;
          let text = `已导出 ${result.rows.length} 条` + (left ? `（还有 ${left} 条没标完或跳过了，没有导出）` : "") + "。";
          let kind = "ok";
          if (result.rejected.length) {
            kind = "warn";
            text += `有 ${result.rejected.length} 条的标注不符合评分标准的格式，没有导出：` + result.rejected.slice(0, 3).map((entry) => `${entry.rubric} ${entry.blind_id}`).join("、") + "。";
          }
          state.notice = { kind, text };
          effects.download = { name: "labels.jsonl", text: result.text, count: result.rows.length };
          break;
        }
        case "blur":
          effects.blur = true;
          effects.rerender = false;
          break;
        default:
          effects.rerender = false;
      }
      if (state.cursor !== cursorBefore) effects.scrollTop = true; // (a new item: the page goes back to its top)
      return effects;
    }

    function view() {
      const base = { loaded: false, labeler: state.labeler, theme: state.theme, themeLabel: THEME_LABELS[state.theme], storageOk: state.storageOk, notice: state.notice, problems: state.problems, guideOpen: state.guideOpen };
      if (!state.items.length) return base;
      const item = current();
      const done = countDone();
      const skipped = state.items.filter((entry) => statusOf(entry) === "skipped").length;
      const byRubric = [];
      for (const entry of state.items) {
        let tally = byRubric.find((candidate) => candidate.rubric === entry.rubric);
        if (!tally) byRubric.push((tally = { rubric: entry.rubric, done: 0, total: 0 }));
        tally.total += 1;
        if (isDone(entry)) tally.done += 1;
      }
      const model = displayModel(item);
      return {
        ...base,
        loaded: true,
        fileName: state.fileName,
        total: total(),
        done,
        skipped,
        percent: Math.round((done / total()) * 100),
        byRubric,
        exportCount: done,
        current: {
          index: state.cursor,
          number: state.cursor + 1,
          blindId: item.blind_id,
          rubric: item.rubric,
          rubricVersion: item.rubric_version,
          rubricTitle: str(item.title),
          kind: str(item.kind),
          status: statusOf(item),
          title: model.title,
          brief: model.brief,
          meta: model.meta,
          blocks: model.blocks,
          guide: str(item.guide),
          form: formModel(item, fieldsOf(item), state.focusField),
        },
        nav: { canPrev: state.cursor > 0, isLast: state.cursor === total() - 1 },
        jump: state.items.map((entry, index) => {
          const status = statusOf(entry);
          return { index, selected: index === state.cursor, status, text: `${index + 1} · ${entry.rubric} · ${STATUS_LABELS[status]}` };
        }),
      };
    }

    return { state, load, dispatch, view, keyAction, confirmClearText: "清除这个文件的全部本地标注进度？已经导出的 labels.jsonl 不受影响，但还没导出的标注会丢。" };
  }

  // ----------------------------------------------------------------------------------------------------------------------- the HTML
  const esc = escapeHtml;
  const KEY_HELP = [
    ["← / →", "上一条 / 下一条（这一条没标完，→ 不会往后：先标，或者按 S 跳过）"],
    ["1 – 9", "选当前高亮的那一题的第几个选项（选完自动跳到下一题）"],
    ["Y / N", "是 / 否"],
    ["Enter", "保存并下一条（文本框里也可以）"],
    ["S", "跳过这一条（以后可以回来标）"],
    ["Esc", "离开文本框"],
  ];

  function renderBlock(block) {
    const head = `<h3>${esc(block.label)}</h3>`;
    const cls = `block ${block.zone}${block.side ? " side" : ""}`;
    let body;
    if (block.type === "list") body = `<ol class="body">${block.items.map((entry) => `<li>${esc(entry)}</li>`).join("")}</ol>`;
    else if (block.type === "json") body = `<pre class="body">${esc(block.text)}</pre>`;
    else if (block.type === "cards") body = `<div class="cards">${block.cards.map((card) => `<div class="scard"><div class="scard-id">${esc(card.id)}</div><div class="scard-text">${esc(card.text)}</div></div>`).join("")}</div>`;
    else body = `<div class="body">${esc(block.text)}</div>`;
    return `<section class="${cls}" data-key="${esc(block.key)}">${head}${body}</section>`;
  }

  function renderBlocks(blocks) {
    const out = [];
    let side = [];
    const flush = () => {
      if (side.length) out.push(`<div class="pair">${side.join("")}</div>`);
      side = [];
    };
    for (const block of blocks) {
      if (block.side) {
        side.push(renderBlock(block));
      } else {
        flush();
        out.push(renderBlock(block));
      }
    }
    flush();
    return out.join("");
  }

  function renderField(field) {
    const head = `<div class="field-head"${field.options ? ` data-action="focusField" data-field="${esc(field.name)}"` : ""}><span class="field-caption">${esc(field.caption)}</span><code>${esc(field.name)}</code>` +
      `<span class="req${field.mustFill ? " must" : ""}">${field.mustFill ? "必填" : "可不填"}</span>${field.answered ? '<span class="tick">✔</span>' : ""}</div>`;
    let body;
    if (field.options) {
      const wide = field.options.some((option) => option.hint);
      body = `<div class="opts${wide ? " wide" : ""}" role="group" aria-label="${esc(field.caption)}">${field.options
        .map(
          (option) =>
            `<button type="button" class="opt${option.selected ? " selected" : ""}" data-action="choose" data-field="${esc(field.name)}" data-option="${option.index}" aria-pressed="${option.selected ? "true" : "false"}">` +
            `${option.key ? `<span class="key">${esc(option.key)}</span>` : ""}<span class="opt-text"><span class="opt-main">${esc(option.label)}</span>${option.hint ? `<span class="opt-hint">${esc(option.hint)}</span>` : ""}</span></button>`
        )
        .join("")}</div>`;
    } else if (field.type === "number") {
      body = `<input type="text" inputmode="decimal" class="text-input" data-text="${esc(field.name)}" value="${esc(field.value === undefined ? "" : field.value)}" aria-label="${esc(field.caption)}" autocomplete="off">`;
    } else {
      body = `<input type="text" class="text-input" data-text="${esc(field.name)}" value="${esc(field.value === undefined ? "" : field.value)}" maxlength="${MAX_TEXT}" placeholder="${field.mustFill ? "必填" : "一句话，可不填"}" aria-label="${esc(field.caption)}" autocomplete="off">`;
    }
    return `<div class="field${field.active ? " active" : ""}${field.answered ? " answered" : ""}" data-name="${esc(field.name)}">${head}${body}</div>`;
  }

  function renderBar(view) {
    const exportText = view.loaded ? `导出 labels.jsonl（${view.exportCount} 条）` : "导出 labels.jsonl";
    return (
      `<header class="bar"><div class="bar-title"><h1>标注页 · 校准集</h1>${view.loaded ? `<span class="muted file">${esc(view.fileName)}</span>` : ""}</div>` +
      `<div class="bar-actions"><label class="btn" for="file-input">${view.loaded ? "换一个文件" : "选择 set.jsonl"}</label>` +
      `<label class="who">标注者 <input type="text" class="text-input" data-labeler value="${esc(view.labeler)}" maxlength="${MAX_LABELER}" autocomplete="off"></label>` +
      `<button type="button" class="btn" data-action="theme" title="明暗主题">主题：${esc(view.themeLabel)}</button>` +
      `<button type="button" class="btn primary" data-action="export"${view.loaded ? "" : " disabled"}>${esc(exportText)}</button>` +
      `<button type="button" class="btn danger" data-action="clear"${view.loaded ? "" : " disabled"}>清除本地进度</button></div></header>`
    );
  }

  function renderProgress(view) {
    if (!view.loaded) return "";
    const tallies = view.byRubric.map((entry) => `<span class="tally">${esc(entry.rubric)} ${entry.done}/${entry.total}</span>`).join("");
    return (
      `<section class="progress"><div class="progress-text"><strong>已标 ${view.done} / 共 ${view.total}</strong>${view.skipped ? `<span class="muted">（跳过 ${view.skipped}）</span>` : ""}${tallies}` +
      `${view.storageOk ? "" : '<span class="warn-text">⚠ 进度没有保存到浏览器里</span>'}</div>` +
      `<div class="progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="${view.total}" aria-valuenow="${view.done}"><div class="progress-fill" style="width:${view.percent}%"></div></div></section>`
    );
  }

  function renderNotice(view) {
    return view.notice ? `<div class="notice notice-${esc(view.notice.kind)}" role="status">${esc(view.notice.text)}</div>` : "";
  }

  function renderWelcome(view) {
    return (
      `<section class="card welcome"><h2>把校准集拖到这里</h2>` +
      `<p>这个页面让你给裁判的样本人工标注：你填的是和裁判<strong>同一个格式</strong>的判定，之后 <code>python -m bench calibrate</code> 会算你们的一致性（κ）。</p>` +
      `<ol><li>先在命令行抽样：<code>python -m bench calibration-set --items judge_items.jsonl --judgments judgments.jsonl --n 30 --out set.jsonl</code></li>` +
      `<li>把 <code>set.jsonl</code> 拖到这个页面，或者点上面的“选择 set.jsonl”。</li>` +
      `<li>一条一条标：内容是盲的（看不到是哪个 bot、哪一局，也看不到裁判怎么判）。每改一下都存在这个浏览器里，关掉页面也能接着标。</li>` +
      `<li>标完点“导出 labels.jsonl”，再运行 <code>python -m bench calibrate --labels labels.jsonl --judgments judgments.jsonl --out calibration.json</code>。</li></ol>` +
      `<label class="drop" for="file-input">拖放文件到这里，或点这里选择 set.jsonl</label>` +
      `<p class="muted">页面完全离线：不联网，不上传任何东西。</p></section>`
    );
  }

  function renderItem(view) {
    const cur = view.current;
    const statusClass = `status-${cur.status}`;
    const meta = cur.meta.length ? `<p class="meta muted">${cur.meta.map((entry) => `${esc(entry.label)}：${esc(entry.value)}`).join(" · ")}</p>` : "";
    return (
      `<article class="card item" data-blind="${esc(cur.blindId)}">` +
      `<div class="card-head"><span class="chip">${esc(cur.rubric)}</span><span class="card-title">${esc(cur.rubricTitle)}</span>` +
      `<span class="pos ${statusClass}">第 ${cur.number} / ${view.total} 条 · ${esc(STATUS_LABELS[cur.status])}</span></div>` +
      `${cur.title || cur.brief ? `<section class="situation">${cur.title ? `<h2>${esc(cur.title)}</h2>` : ""}${cur.brief ? `<p>${esc(cur.brief)}</p>` : ""}</section>` : ""}` +
      `${meta}<div class="blocks">${renderBlocks(cur.blocks)}</div>` +
      `<section class="form"><h3>你的判定</h3>${cur.form.fields.map(renderField).join("")}` +
      `<p class="form-state${cur.form.complete ? " ok" : ""}">${cur.form.complete ? "✔ 必填的都选好了" : `还差 ${cur.form.missing.length} 项必填`}</p></section>` +
      `<nav class="nav"><button type="button" class="btn" data-action="prev"${view.nav.canPrev ? "" : " disabled"}>← 上一条</button>` +
      `<button type="button" class="btn" data-action="skip">跳过（S）</button>` +
      `<button type="button" class="btn primary" data-action="saveNext">${view.nav.isLast ? "保存（Enter）" : "保存并下一条（Enter）"}</button>` +
      `<button type="button" class="btn" data-action="nextOpen">下一条没标的</button>` +
      `<label class="jump">跳到 <select data-action="jump" aria-label="跳到第几条">${view.jump.map((entry) => `<option value="${entry.index}"${entry.selected ? " selected" : ""}>${esc(entry.text)}</option>`).join("")}</select></label></nav>` +
      `<details class="guide" data-guide${view.guideOpen ? " open" : ""}><summary>评分标准：这个 rubric 怎么判（和裁判看到的一样）</summary><div class="guide-body">${guideHtml(cur.guide)}</div></details>` +
      `</article>`
    );
  }

  function renderHelp() {
    return `<details class="help"><summary>键盘快捷键</summary><ul>${KEY_HELP.map(([key, text]) => `<li><kbd>${esc(key)}</kbd> ${esc(text)}</li>`).join("")}</ul></details>`;
  }

  /** The whole page (inside #app) for a `view()`. */
  function renderHtml(view) {
    return `<div class="app">${renderBar(view)}${renderProgress(view)}${renderNotice(view)}<main>${view.loaded ? renderItem(view) : renderWelcome(view)}</main>${renderHelp()}</div>`;
  }

  // ----------------------------------------------------------------------------------------------------------------------- the DOM
  function closestOf(target, selector) {
    let node = target;
    if (node && typeof node.closest !== "function" && node.parentElement) node = node.parentElement;
    return node && typeof node.closest === "function" ? node.closest(selector) : null;
  }

  function pickStorage(win) {
    try {
      const store = win.localStorage;
      if (!store) return null;
      store.setItem("arena-bench-label/probe", "1");
      store.removeItem("arena-bench-label/probe");
      return store;
    } catch (err) {
      return null;
    }
  }

  /** Connects the controller to the page: `#app` is redrawn from `view()` after every action; the events are delegated from the document. */
  function mount(doc, win, options) {
    const opts = options || {};
    const host = doc.getElementById("app");
    if (!host) return null;
    const controller = createController({ storage: opts.storage !== undefined ? opts.storage : pickStorage(win), now: opts.now });
    const fileInput = doc.getElementById("file-input");

    function applyTheme() {
      const theme = controller.state.theme;
      if (theme === "auto") doc.documentElement.removeAttribute("data-theme");
      else doc.documentElement.setAttribute("data-theme", theme);
    }

    function render() {
      host.innerHTML = renderHtml(controller.view());
      applyTheme();
    }

    function download(file) {
      const blob = new win.Blob([file.text], { type: "application/x-ndjson" });
      const url = win.URL.createObjectURL(blob);
      const link = doc.createElement("a");
      link.href = url;
      link.download = file.name;
      link.style.display = "none";
      doc.body.appendChild(link);
      link.click();
      doc.body.removeChild(link);
      win.setTimeout(() => win.URL.revokeObjectURL(url), 1000);
    }

    function run(action) {
      const effects = controller.dispatch(action);
      if (effects.download) download(effects.download);
      if (effects.blur && doc.activeElement && typeof doc.activeElement.blur === "function") doc.activeElement.blur();
      if (effects.rerender) render();
      if (effects.scrollTop && typeof win.scrollTo === "function") win.scrollTo(0, 0);
    }

    function openText(file, input) {
      controller.load(input, file.name);
      if (fileInput) fileInput.value = "";
      render();
    }

    function openFile(file) {
      if (!file) return;
      const fail = () => {
        controller.state.notice = { kind: "error", text: "读不了这个文件。" };
        render();
      };
      if (typeof file.text === "function") {
        file.text().then((input) => openText(file, input), fail);
      } else {
        const reader = new win.FileReader();
        reader.onload = () => openText(file, String(reader.result));
        reader.onerror = fail;
        reader.readAsText(file, "utf-8");
      }
    }

    doc.addEventListener("click", (event) => {
      const node = closestOf(event.target, "[data-action]");
      if (!node) return;
      const name = node.getAttribute("data-action");
      if (name === "jump") return; // (a <select>: handled on change)
      if (name === "clear") {
        if (win.confirm(controller.confirmClearText)) run({ type: "clear" });
        return;
      }
      run({ type: name, field: node.getAttribute("data-field"), option: node.getAttribute("data-option") });
    });
    doc.addEventListener("change", (event) => {
      const target = event.target;
      if (target && target.id === "file-input") {
        openFile(target.files && target.files[0]);
        return;
      }
      const node = closestOf(target, "[data-action]");
      if (node && node.getAttribute("data-action") === "jump") run({ type: "jump", index: Number(node.value) });
    });
    doc.addEventListener("input", (event) => {
      const target = event.target;
      if (!target || typeof target.getAttribute !== "function") return;
      if (target.getAttribute("data-text") !== null) run({ type: "text", field: target.getAttribute("data-text"), value: target.value });
      else if (target.getAttribute("data-labeler") !== null) run({ type: "labeler", value: target.value });
    });
    doc.addEventListener("keydown", (event) => {
      const target = event.target || {};
      const action = keyAction({
        key: event.key,
        repeat: event.repeat,
        composing: Boolean(event.isComposing) || event.keyCode === 229, // (Safari reports the Enter that ends a composition as keyCode 229 after compositionend)
        ctrlKey: event.ctrlKey,
        metaKey: event.metaKey,
        altKey: event.altKey,
        targetTag: target.tagName,
        inField: typeof target.getAttribute === "function" && target.getAttribute("data-text") !== null,
      });
      if (!action || (action.type !== "blur" && !controller.state.items.length)) return;
      event.preventDefault();
      run(action);
    });
    doc.addEventListener(
      "toggle",
      (event) => {
        const target = event.target;
        if (target && typeof target.getAttribute === "function" && target.getAttribute("data-guide") !== null) run({ type: "guide", open: target.open });
      },
      true
    );
    win.addEventListener("dragover", (event) => event.preventDefault());
    win.addEventListener("drop", (event) => {
      event.preventDefault();
      const files = event.dataTransfer && event.dataTransfer.files;
      if (files && files[0]) openFile(files[0]);
    });
    render();
    return { controller, render, run, openText };
  }

  return {
    ITEM_FORMAT,
    LABEL_FORMAT,
    STORAGE_PREFIX,
    PREFS_KEY,
    DEFAULT_LABELER,
    DENY_KEYS,
    escapeHtml,
    parseJsonl,
    parseSet,
    fingerprintOf,
    storageKey,
    validateLabel,
    fieldType,
    enumHints,
    formModel,
    finalLabel,
    cleanLabeler,
    exportRows,
    displayModel,
    guideHtml,
    keyAction,
    createController,
    renderHtml,
    mount,
  };
});
