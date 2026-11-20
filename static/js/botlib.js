/* ==========================================================================
   Arena-Agents v0.14 — the pure helpers of the bot UI (docs/v0.12-contracts.md §13.2 / §13.7, decisions H6–H9, H19–H23, H30, H37–H41)
   Loaded before bots.js / settings.js.  Exposes window.ArenaBotLib (and module.exports): no DOM, no network - the tests run it under node,
   and tests/frontend/test_bots_drift.py compares the text rules below with app/storage/botfiles.py (the limits, the words of the refusals,
   the merged L0 file, the playbook and the impressions) on a table of inputs: if the server's rules change, that test says so.

     limits            normalizeLimits(raw)              the numbers GET /api/bots sends (BotStore.limits_payload()), the contract's for the rest
     L0 (底层设定)     coreModel / coreProblems / coreStats / renderCoreMarkdown / parseCoreMarkdown
     memory parts      partProblems / parsePlaybook / parsePeople / countPart
     initialization    INIT_SYMBOLS / initView / initTargets / problemsOf / initProgressView
     proposals         stagingLabel / lineDiff
     misc              textLength / formatUnix / formatDuration / botNameProblem
   ========================================================================== */

(function (root) {
  "use strict";

  const DEFAULT_LIMITS = Object.freeze({
    name_chars: 20,
    core_limits: Object.freeze({ identity: 300, voice: 600, mind: 400 }),
    core_extra_max: 3,
    core_extra_chars: 300,
    extra_title_chars: 20,
    arch_notes_chars: 800,
    digest_chars: 800,
    questions_chars: 400,
    playbook_max: 20,
    playbook_entry_chars: 80,
    people_max: 30,
    impression_chars: 80,
    contract_must_read_chars: 1200,
    init_timeout_s: 120,
    review_timeout_s: 60,
    jot_max: 10,
    jot_chars: 120,
    memory_parts: Object.freeze(["arch_notes", "digest", "playbook", "questions", "people"]),
    part_labels: Object.freeze({ arch_notes: "操作笔记", digest: "规则要点", playbook: "打法", questions: "待解问题", people: "印象" }),
  });

  const CORE_KEYS = Object.freeze(["identity", "voice", "mind"]);
  const CORE_HEADINGS = Object.freeze({ identity: "身份", voice: "说话方式", mind: "分析方式" });
  const CORE_EXTRA_PREFIX = "附加：";
  /** A sanity cap on a merged L0 file (botfiles.MAX_CORE_MARKDOWN_CHARS). */
  const MAX_CORE_MARKDOWN_CHARS = 20000;
  const MEMORY_PARTS = DEFAULT_LIMITS.memory_parts;
  /** The parts that belong to one scenario (the others - the notes and the impressions - are the bot's own, whatever the scenario). */
  const SCENARIO_PARTS = Object.freeze(["digest", "playbook", "questions"]);
  const SOURCE_LABELS = Object.freeze({ core: "底层设定", contract: "官方契约", scenario: "剧本", materials: "学习材料" });

  /** Characters the way the server counts them (Python's len: an emoji or a Chinese character is one). */
  const textLength = (s) => Array.from(String(s === undefined || s === null ? "" : s)).length;
  /** A text the way the server reads it before it counts it: a byte-order mark in front and every kind of line end -> "\n" (botfiles.normalize_newlines). */
  const norm = (s) => String(s === undefined || s === null ? "" : s).replace(/^\ufeff+/, "").replace(/\r\n?/g, "\n");
  const trimLength = (s) => textLength(norm(s).trim());
  const oneLine = (s) => String(s === undefined || s === null ? "" : s).replace(/\s+/g, " ").trim();
  const shown = (v, limit = 30) => (textLength(v) <= limit ? v : `${Array.from(v).slice(0, limit).join("")}…`);

  /** The limits the server sent; the contract's numbers for whatever is missing or broken. */
  function normalizeLimits(raw) {
    const out = { ...DEFAULT_LIMITS, core_limits: { ...DEFAULT_LIMITS.core_limits }, memory_parts: DEFAULT_LIMITS.memory_parts.slice(), part_labels: { ...DEFAULT_LIMITS.part_labels } };
    if (!raw || typeof raw !== "object") return out;
    const pos = (v) => {
      const n = Number(v);
      return Number.isFinite(n) && n > 0 ? Math.floor(n) : null;
    };
    for (const k of Object.keys(DEFAULT_LIMITS)) {
      if (k === "core_limits" || k === "memory_parts" || k === "part_labels") continue;
      const n = pos(raw[k]);
      if (n !== null) out[k] = n;
    }
    if (raw.core_limits && typeof raw.core_limits === "object") {
      for (const k of CORE_KEYS) {
        const n = pos(raw.core_limits[k]);
        if (n !== null) out.core_limits[k] = n;
      }
    }
    if (Array.isArray(raw.memory_parts) && raw.memory_parts.every((p) => typeof p === "string") && raw.memory_parts.length) out.memory_parts = raw.memory_parts.slice();
    if (raw.part_labels && typeof raw.part_labels === "object") {
      for (const [k, v] of Object.entries(raw.part_labels)) if (typeof v === "string" && v) out.part_labels[k] = v;
    }
    return out;
  }

  // ---------------------------------------------------------------------------------------------------------------------
  // names (app/storage/roster.py clean_name): 1-20 characters, no control character, quote, backslash, angle bracket or colon
  // ---------------------------------------------------------------------------------------------------------------------

  /** The reason a NEW name is refused ("" = fine; the server checks again and is the authority).  `others`: the names of the other bots. */
  function botNameProblem(raw, others, limits) {
    const L = limits || DEFAULT_LIMITS;
    const t = String(raw === undefined || raw === null ? "" : raw).trim();
    if (!t) return "名字不能为空。";
    if (textLength(t) > L.name_chars) return `名字最多 ${L.name_chars} 个字符。`;
    // eslint-disable-next-line no-control-regex
    if (/[\x00-\x1f\x7f<>"'`\\]/.test(t)) return "名字不能包含控制字符、引号、反斜杠或尖括号。";
    if (/[:\uff1a]/.test(t)) return "名字不能包含冒号（: 或 ：）。";
    const clash = (others || []).find((o) => String(o).toLowerCase() === t.toLowerCase());
    if (clash !== undefined) return `名字「${t}」已被其他玩家使用，名字不能重复。`;
    return "";
  }

  // ---------------------------------------------------------------------------------------------------------------------
  // L0: the editor model, its numbers, the merged file (botfiles.validate_core / render_core_markdown / parse_core_markdown)
  // ---------------------------------------------------------------------------------------------------------------------

  /** `BotDetail.core` (or a BotSummary's, without the texts) -> what the editors hold: {identity, voice, mind, extras: [{title, text, slug}]}. */
  function coreModel(core) {
    const c = core && typeof core === "object" ? core : {};
    const sections = c.sections && typeof c.sections === "object" ? c.sections : {};
    return {
      identity: String(sections.identity || ""),
      voice: String(sections.voice || ""),
      mind: String(sections.mind || ""),
      extras: (Array.isArray(c.extras) ? c.extras : [])
        .filter((e) => e && typeof e === "object")
        .map((e) => ({ title: String(e.title || ""), text: String(e.text || ""), slug: String(e.slug || "") })),
    };
  }

  const cloneCore = (m) => ({ identity: m.identity, voice: m.voice, mind: m.mind, extras: m.extras.map((e) => ({ ...e })) });

  /** Is the typed L0 different from the saved one (what the editors compare: the texts as the server stores them, stripped)? */
  function coreDirty(model, saved) {
    const t = (s) => norm(s).trim();
    for (const k of CORE_KEYS) if (t(model[k]) !== t(saved[k])) return true;
    if (model.extras.length !== saved.extras.length) return true;
    return model.extras.some((e, i) => t(e.title) !== t(saved.extras[i].title) || t(e.text) !== t(saved.extras[i].text));
  }

  const HEADING_LINE_RE = /^ {0,3}#(?!#)(?:[ \t\u3000]+(.*?))?[ \t\u3000]*$/;
  const hasHeadingLine = (text) => String(text).split("\n").some((line) => HEADING_LINE_RE.test(line));
  const over = (label, limit, now) => `「${label}」最多 ${limit} 字（现在 ${now} 字）。`;
  const headingNote = (label) => `「${label}」里不能有以“# ”开头的行（会被当成分区标题），请去掉 # 或改用 ##。`;

  /** Every fault of an L0, as the server says them (botfiles.validate_core); `requireComplete`: an empty required section is a fault too. */
  function coreProblems(model, limits, opts) {
    const L = limits || DEFAULT_LIMITS;
    const requireComplete = Boolean(opts && opts.requireComplete);
    const problems = [];
    for (const k of CORE_KEYS) {
      const body = norm(model[k]).trim();
      const label = CORE_HEADINGS[k];
      const n = textLength(body);
      if (!body) {
        if (requireComplete) problems.push(`「${label}」不能为空。`);
      } else if (n > L.core_limits[k]) problems.push(over(label, L.core_limits[k], n));
      else if (hasHeadingLine(body)) problems.push(headingNote(label));
    }
    const extras = model.extras || [];
    if (extras.length > L.core_extra_max) problems.push(`附加分区最多 ${L.core_extra_max} 份（现在 ${extras.length} 份）。`);
    const seen = new Set();
    for (const e of extras) {
      const name = String(e.title || "").trim();
      const label = `${CORE_EXTRA_PREFIX}${name}`;
      if (!name) problems.push("附加分区需要标题。");
      else if (textLength(name) > L.extra_title_chars) problems.push(`附加分区的标题最多 ${L.extra_title_chars} 字（现在 ${textLength(name)} 字）：${shown(name)}`);
      else if (name.includes("\n")) problems.push("附加分区的标题只能有一行。");
      else if (seen.has(name.toLowerCase())) problems.push(`附加分区「${name}」重复了。`);
      seen.add(name.toLowerCase());
      const body = norm(e.text).trim();
      const n = textLength(body);
      if (!body) problems.push(`「${label}」不能为空。`);
      else if (n > L.core_extra_chars) problems.push(over(label, L.core_extra_chars, n));
      else if (hasHeadingLine(body)) problems.push(headingNote(label));
    }
    return problems;
  }

  /** What the L0 editor shows: a counter per section, the faults, what is missing, whether it could be saved as it is. */
  function coreStats(model, limits) {
    const L = limits || DEFAULT_LIMITS;
    const sections = {};
    for (const k of CORE_KEYS) {
      const n = trimLength(model[k]);
      sections[k] = { n, limit: L.core_limits[k], over: n > L.core_limits[k], empty: n === 0 };
    }
    const extras = (model.extras || []).map((e) => {
      const n = trimLength(e.text);
      const titleN = trimLength(e.title);
      return { n, limit: L.core_extra_chars, over: n > L.core_extra_chars, empty: n === 0, titleN, titleLimit: L.extra_title_chars, titleOver: titleN > L.extra_title_chars, titleEmpty: titleN === 0 };
    });
    const missing = CORE_KEYS.filter((k) => sections[k].empty);
    const problems = coreProblems(model, L, { requireComplete: false });
    return { sections, extras, missing, problems, complete: !missing.length && !problems.length, canSave: !problems.length };
  }

  /** A value of the file header: an inline comment (`#` at the start or after a space) is cut off, matching quotes dropped (botfiles._header_value). */
  function headerValue(raw) {
    let s = String(raw === undefined || raw === null ? "" : raw);
    const m = /(?:^|\s)#/.exec(s);
    if (m) s = s.slice(0, m.index);
    let v = s.trim();
    if (v.length >= 2 && v[0] === v[v.length - 1] && (v[0] === '"' || v[0] === "'")) v = v.slice(1, -1).trim();
    return v;
  }

  /** The name as it is written in the file header: one line, without anything that would be read as a comment (botfiles.name_hint). */
  const nameHint = (name) => headerValue(oneLine(name));

  /** The canonical merged L0 file (botfiles.render_core_markdown): header, 身份, 说话方式, 分析方式, the extras; one blank line between. */
  function renderCoreMarkdown(model, name) {
    const head = ["---", "format: arena-core", "version: 1"];
    const hint = nameHint(name);
    if (hint) head.push(`name: ${hint}`);
    head.push("---");
    const blocks = [];
    for (const k of CORE_KEYS) {
      const body = String(model[k] || "").trim();
      blocks.push(body ? `# ${CORE_HEADINGS[k]}\n${body}` : `# ${CORE_HEADINGS[k]}`);
    }
    for (const e of model.extras || []) {
      const body = String(e.text || "").trim();
      const heading = `# ${CORE_EXTRA_PREFIX}${String(e.title || "").trim()}`;
      blocks.push(body ? `${heading}\n${body}` : heading);
    }
    return `${head.join("\n")}\n${blocks.join("\n\n")}\n`;
  }

  /**
   * A merged L0 file -> {ok, core: {identity, voice, mind, extras: [{title, text}]}, name, problems}.  The structure is checked the way the server does (header,
   * section titles, duplicates, missing sections); the limits are NOT (a draft over a limit is shown to be fixed: see coreProblems).
   */
  function parseCoreMarkdown(text) {
    const raw = norm(text);
    if (raw.includes("\u0000")) return { ok: false, core: null, name: null, problems: ["文件不能包含空字符。"] };
    if (textLength(raw) > MAX_CORE_MARKDOWN_CHARS) {
      return { ok: false, core: null, name: null, problems: [`文件太大了（最多 ${MAX_CORE_MARKDOWN_CHARS} 字，现在 ${textLength(raw)} 字）。`] };
    }
    const lines = raw.split("\n");
    let start = 0;
    while (start < lines.length && !lines[start].trim()) start++;
    if (start >= lines.length || lines[start].trim() !== "---") {
      return { ok: false, core: null, name: null, problems: ["缺少文件头：文件开头要有用 --- 包住的 format: arena-core 和 version: 1。"] };
    }
    let end = start + 1;
    while (end < lines.length && lines[end].trim() !== "---") end++;
    if (end >= lines.length) return { ok: false, core: null, name: null, problems: ["文件头没有结束：缺少第二行 ---。"] };
    const fields = {};
    for (const line of lines.slice(start + 1, end)) {
      if (!line.trim() || line.trim().startsWith("#")) continue;
      const m = /^\s*([^:：]+?)\s*[:：]\s*(.*?)\s*$/.exec(line);
      if (!m) continue;
      const key = m[1].normalize("NFKC").trim().toLowerCase();
      if (!(key in fields)) fields[key] = m[2];
    }
    const problems = [];
    const format = headerValue(fields.format || "").normalize("NFKC").trim();
    if (!format) problems.push("文件头缺少 format: arena-core。");
    else if (format.toLowerCase() !== "arena-core") problems.push(`文件头的 format 必须是 arena-core（现在是「${shown(format)}」）。`);
    const version = headerValue(fields.version || "").normalize("NFKC").trim();
    if (!version) problems.push("文件头缺少 version: 1。");
    else if (!/^1(?:\.0+)?$/.test(version)) problems.push(`文件头的 version 目前只能是 1（现在是「${shown(version)}」）。`);
    const sections = [];
    const preamble = [];
    for (const line of lines.slice(end + 1)) {
      const m = HEADING_LINE_RE.exec(line);
      if (m) sections.push({ heading: (m[1] || "").trim(), body: [] });
      else if (sections.length) sections[sections.length - 1].body.push(line);
      else preamble.push(line);
    }
    if (preamble.some((line) => line.trim())) problems.push("第一个分区标题之前有多余的内容：分区要用一级标题开头，如「# 身份」。");
    const byKey = {};
    const counts = {};
    const extras = [];
    const extraCounts = {};
    const reverse = { 身份: "identity", 说话方式: "voice", 分析方式: "mind" };
    for (const s of sections) {
      const body = s.body.join("\n").trim();
      const key = reverse[s.heading.replace(/\s+/g, "")];
      if (key) {
        counts[key] = (counts[key] || 0) + 1;
        if (!(key in byKey)) byKey[key] = body;
        continue;
      }
      const ex = /^附加\s*[:：]\s*(.*)$/.exec(s.heading);
      if (!ex) {
        problems.push(`不认识的分区标题「${shown(s.heading) || "（空）"}」：只能是 ${CORE_KEYS.map((k) => `「${CORE_HEADINGS[k]}」`).join("、")} 或「${CORE_EXTRA_PREFIX}<标题>」。`);
        continue;
      }
      const title = ex[1].trim();
      if (!title) {
        problems.push(`附加分区需要标题，写成「# ${CORE_EXTRA_PREFIX}<标题>」。`);
        continue;
      }
      const folded = title.toLowerCase();
      extraCounts[folded] = (extraCounts[folded] || 0) + 1;
      if (extraCounts[folded] === 1) extras.push({ title, text: body });
    }
    for (const k of CORE_KEYS) if (counts[k] > 1) problems.push(`「${CORE_HEADINGS[k]}」分区出现了 ${counts[k]} 次，只能有 1 个。`);
    for (const e of extras) if (extraCounts[e.title.toLowerCase()] > 1) problems.push(`「${CORE_EXTRA_PREFIX}${e.title}」分区出现了 ${extraCounts[e.title.toLowerCase()]} 次，只能有 1 个。`);
    for (const k of CORE_KEYS) {
      if (!(k in byKey)) problems.push(`缺少「${CORE_HEADINGS[k]}」分区。`);
      else if (!byKey[k]) problems.push(`「${CORE_HEADINGS[k]}」不能为空。`);
    }
    const core = { identity: byKey.identity || "", voice: byKey.voice || "", mind: byKey.mind || "", extras };
    return { ok: problems.length === 0, core, name: headerValue(fields.name || "") || null, problems };
  }

  // ---------------------------------------------------------------------------------------------------------------------
  // the memory parts (botfiles.normalize_part): free text under a limit, the playbook, the impressions
  // ---------------------------------------------------------------------------------------------------------------------

  const BULLET_RE = /^\s*(?:[-*•·]+|\d{1,2}[.)、．])\s*/;
  const TAG_BAD_RE = /[\s|[\]［］#＃]/g;
  const CONFIDENCE_WORDS = { low: "低", mid: "中", medium: "中", high: "高" };

  function confidenceOf(value) {
    const t = String(value).trim();
    for (const ch of Array.from(t).slice(0, 3)) if (ch === "低" || ch === "中" || ch === "高") return ch;
    return CONFIDENCE_WORDS[t.toLowerCase()] || "低";
  }

  const countOf = (value) => {
    const m = /^\s*(\d+)/.exec(String(value));
    return m ? Number(m[1]) : 0;
  };

  /** ``playbook.md`` -> {entries: [{text, confidence, support, counter, source, tags}], ignored} (botfiles.parse_playbook_ex). */
  function parsePlaybook(text) {
    const entries = [];
    let ignored = 0;
    let section = "";
    for (const raw of norm(text).split("\n")) {
      let line = raw.trim();
      if (!line) continue;
      const heading = line.startsWith("#") ? /^ {0,3}#{1,6}[ \t\u3000]+(.*?)[ \t\u3000]*#*[ \t\u3000]*$/.exec(line) : null;
      if (heading) {
        section = heading[1].trim();
        continue;
      }
      if (section !== "" && section !== "假设") continue;
      const bullet = BULLET_RE.exec(line);
      if (bullet) line = line.slice(bullet[0].length);
      else if (!/^[[［]/.test(line)) {
        ignored++;
        continue;
      }
      const head = /^[[［]([^\]］]*)[\]］]\s*(.*)$/.exec(line);
      const fields = { tags: [] };
      let body = line;
      if (head) {
        body = head[2];
        for (let piece of head[1].replace(/｜/g, "|").replace(/：/g, ":").replace(/＃/g, "#").split("|")) {
          piece = piece.trim();
          if (!piece) continue;
          if (piece.startsWith("#")) {
            const tag = Array.from(piece.slice(1).replace(TAG_BAD_RE, "")).slice(0, 20).join("");
            if (tag && !fields.tags.includes(tag)) fields.tags.push(tag);
            continue;
          }
          const at = piece.indexOf(":");
          if (at < 0) {
            if (piece === "低" || piece === "中" || piece === "高") fields.confidence = piece;
            continue;
          }
          const key = piece.slice(0, at).trim();
          const value = piece.slice(at + 1).trim();
          if (key === "把握") fields.confidence = confidenceOf(value);
          else if (key === "支持") fields.support = countOf(value);
          else if (key === "反例") fields.counter = countOf(value);
          else if (key === "来源") fields.source = value;
        }
      }
      body = oneLine(body);
      if (!body) {
        ignored++;
        continue;
      }
      const source = Array.from(oneLine(fields.source || "").replace(/\|/g, "/").replace(/[[［]/g, "(").replace(/[\]］]/g, ")")).slice(0, 40).join("").trim();
      entries.push({ text: body, confidence: fields.confidence || "低", support: fields.support || 0, counter: fields.counter || 0, source, tags: fields.tags });
    }
    return { entries, ignored };
  }

  const NO_IMPRESSION = "（暂无）";

  /** One impression as the server stores it: one line; the placeholder means "none yet". */
  function cleanImpression(value) {
    const t = oneLine(value);
    return t === NO_IMPRESSION || t === "(暂无)" || t === "暂无" ? "" : t;
  }

  /** ``people.md`` -> {people: {name: impression}, ignored} (botfiles.parse_people_ex). */
  function parsePeople(text) {
    const found = {};
    const folded = {};
    let ignored = 0;
    for (const raw of norm(text).split("\n")) {
      let line = raw.trim();
      if (!line || line.startsWith("#")) continue;
      const bullet = BULLET_RE.exec(line);
      if (bullet) line = line.slice(bullet[0].length);
      const m = /^([^:：]+?)\s*[:：]\s*(.*)$/.exec(line);
      if (!m) {
        ignored++;
        continue;
      }
      const name = m[1].trim();
      const impression = cleanImpression(m[2]);
      if (!name || !impression) {
        if (!impression && name) continue;
        ignored++;
        continue;
      }
      const key = name.toLowerCase();
      if (key in folded) found[folded[key]] = impression;
      else {
        folded[key] = name;
        found[name] = impression;
      }
    }
    return { people: found, ignored };
  }

  /** What a memory editor counts: free text -> characters; the playbook -> entries; the impressions -> people. {n, limit, unit, ignored}. */
  function countPart(part, text, limits) {
    const L = limits || DEFAULT_LIMITS;
    if (part === "playbook") return { n: parsePlaybook(text).entries.length, limit: L.playbook_max, unit: "条", ignored: parsePlaybook(text).ignored };
    if (part === "people") return { n: Object.keys(parsePeople(text).people).length, limit: L.people_max, unit: "人", ignored: parsePeople(text).ignored };
    const limit = part === "arch_notes" ? L.arch_notes_chars : part === "digest" ? L.digest_chars : L.questions_chars;
    return { n: trimLength(text), limit, unit: "字", ignored: 0 };
  }

  /** Every fault of a text meant for `part`, in the server's words (botfiles.normalize_part) - empty: it can be saved. */
  function partProblems(part, text, limits) {
    const L = limits || DEFAULT_LIMITS;
    const label = L.part_labels[part] || part;
    const raw = norm(text);
    if (!MEMORY_PARTS.includes(part)) return [`不认识的记忆部分「${part}」，只能是 ${MEMORY_PARTS.join("、")}。`];
    if (raw.includes("\u0000")) return [`${label}不能包含空字符。`];
    if (part === "playbook") {
      const { entries } = parsePlaybook(raw);
      if (raw.trim() && !entries.length) return ["没有读出任何一条打法：每条写成一行「- [把握:中|支持:0|反例:0|来源:复盘|#标签] 一句话」。"];
      const problems = [];
      if (entries.length > L.playbook_max) problems.push(`打法最多 ${L.playbook_max} 条（现在 ${entries.length} 条）。`);
      entries.forEach((e, i) => {
        if (textLength(e.text) > L.playbook_entry_chars) problems.push(`打法第 ${i + 1} 条最多 ${L.playbook_entry_chars} 字（现在 ${textLength(e.text)} 字）：${shown(e.text, 16)}`);
      });
      return problems;
    }
    if (part === "people") {
      const { people, ignored } = parsePeople(raw);
      if (raw.trim() && !Object.keys(people).length && ignored) return ["没有读出任何一条印象：每条写成一行「- 名字：一句话印象」。"];
      const problems = [];
      const names = Object.keys(people);
      if (names.length > L.people_max) problems.push(`印象最多 ${L.people_max} 人（现在 ${names.length} 人）。`);
      for (const n of names) {
        const bad = botNameProblem(n, [], L);
        if (bad) {
          problems.push(`印象里的名字「${shown(n, 12)}」不合法：${bad}`);
          continue;
        }
        if (textLength(people[n]) > L.impression_chars) problems.push(`对「${n}」的印象最多 ${L.impression_chars} 字（现在 ${textLength(people[n])} 字）。`);
      }
      return problems;
    }
    const n = trimLength(raw);
    const limit = part === "arch_notes" ? L.arch_notes_chars : part === "digest" ? L.digest_chars : L.questions_chars;
    return n > limit ? [over(label, limit, n)] : [];
  }

  // ---------------------------------------------------------------------------------------------------------------------
  // initialization (H19, H22): the symbols, the words, who is a target, what a job is doing
  // ---------------------------------------------------------------------------------------------------------------------

  /** The mark next to a name on the card: ✔ ready, ⚠ needs a new initialization, ✖ failed, ⏳ running; ○ for a bot that never was initialized. */
  const INIT_SYMBOLS = Object.freeze({ ready: "✔", stale: "⚠", failed: "✖", initializing: "⏳", uninitialized: "○" });
  const INIT_LABELS = Object.freeze({ ready: "已初始化", stale: "需重新初始化", failed: "初始化失败", initializing: "初始化中", uninitialized: "未初始化" });
  /** The states a bot can be started from (the start check wants every one of them ✔). */
  const INIT_TARGET_STATES = Object.freeze(["uninitialized", "stale", "failed"]);

  const pad2 = (n) => String(n).padStart(2, "0");

  /** "2026-11-19 20:15" in the viewer's time ("" for nothing); `ts` in unix seconds. */
  function formatUnix(ts) {
    const n = Number(ts);
    if (!Number.isFinite(n) || n <= 0) return "";
    const d = new Date(n * 1000);
    if (Number.isNaN(d.getTime())) return "";
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  }

  /** 74.2 -> "1 分 14 秒"; 40 -> "40 秒". */
  function formatDuration(seconds) {
    const n = Math.round(Number(seconds));
    if (!Number.isFinite(n) || n < 0) return "";
    return n < 60 ? `${n} 秒` : `${Math.floor(n / 60)} 分 ${pad2(n % 60)} 秒`;
  }

  /**
   * What the page says about the initialization of a bot (a BotSummary / BotDetail): {status, symbol, label, detail, tone}.  `detail` is one
   * more line (why it is stale, why it failed, how long it took); `tone`: ok / warn / bad / busy / none (the colour class).
   */
  function initView(bot) {
    const init = (bot && bot.init) || {};
    const status = INIT_SYMBOLS[init.status] ? init.status : "uninitialized";
    const out = { status, symbol: INIT_SYMBOLS[status], label: INIT_LABELS[status], detail: "", tone: { ready: "ok", stale: "warn", failed: "bad", initializing: "busy", uninitialized: "none" }[status] };
    if (status === "ready") {
      const bits = [init.model ? `模型 ${init.model}` : "", Number.isFinite(Number(init.duration_s)) && init.duration_s !== null ? `用时 ${formatDuration(init.duration_s)}` : "", formatUnix(init.at)];
      out.detail = bits.filter(Boolean).join(" · ");
    } else if (status === "stale") {
      const what = (Array.isArray(init.stale) ? init.stale : []).map((k) => SOURCE_LABELS[k] || k);
      out.detail = "记忆保留；重新初始化会修订它们，不会清空。";
      if (what.length) out.label = `需重新初始化（${what.join("、")}变了）`;
    } else if (status === "failed") {
      out.detail = init.error ? `原因：${init.error}` : "原因不明。原来的文件没有改动，可以重试。";
    }
    return out;
  }

  /** The bots a global initialization takes: the ones that are not ready (default) or all of them ("全部重新初始化"); never one that is running. */
  function initTargets(bots, all) {
    return (Array.isArray(bots) ? bots : [])
      .filter((b) => b && b.init && b.init.status !== "initializing" && (all || INIT_TARGET_STATES.includes(b.init.status)))
      .map((b) => b.id);
  }

  /** `session.init_progress[botId]` (the newest bot.init.progress) -> {state, running, line, percent}; null without one. */
  function initProgressView(ev, deadline) {
    if (!ev) return null;
    const total = Number(deadline) > 0 ? Number(deadline) : Number(ev.deadline_s) > 0 ? Number(ev.deadline_s) : 120;
    const left = Number.isFinite(Number(ev.seconds_left)) && ev.seconds_left !== null ? Math.max(0, Number(ev.seconds_left)) : null;
    const files = Array.isArray(ev.files_written) ? ev.files_written : [];
    const labels = { arch_notes: "操作笔记", digest: "规则要点", playbook: "打法", questions: "待解问题" };
    const written = files.map((f) => labels[f] || f);
    const running = ev.state === "queued" || ev.state === "running";
    let line;
    if (ev.state === "queued") line = "排队中…";
    else if (ev.state === "running") line = `初始化中，还剩 ${left === null ? "?" : left} 秒${written.length ? `；已写：${written.join("、")}` : ""}`;
    else if (ev.state === "done") line = `已完成${written.length ? `：写了 ${written.join("、")}` : ""}`;
    else if (ev.state === "timeout") line = `超过 ${total} 秒，已经停止${ev.error ? `：${ev.error}` : ""}`;
    else if (ev.state === "failed") line = `失败${ev.error ? `：${ev.error}` : ""}`;
    else line = "已取消";
    const percent = ev.state === "done" ? 100 : left === null ? 0 : Math.max(0, Math.min(100, Math.round((100 * (total - left)) / total)));
    return { state: ev.state, running, line, percent, left, written };
  }

  /** The start-check problems of one bot (a `Problem` of `BotsPayload.ready` with that bot id), or null. */
  function problemsOf(ready, botId) {
    const list = ready && Array.isArray(ready.problems) ? ready.problems : [];
    return list.find((p) => p && p.scope === "bot" && p.bot_id === botId) || null;
  }

  // ---------------------------------------------------------------------------------------------------------------------
  // proposals of a review (staging): which section, and what changed
  // ---------------------------------------------------------------------------------------------------------------------

  /** `section` of a proposal ("voice", "extra:x1") -> "说话方式" / "附加：口头禅" (`extras`: the bot's extras, to find the title of a slug). */
  function stagingLabel(section, extras) {
    const s = String(section || "");
    if (CORE_HEADINGS[s]) return CORE_HEADINGS[s];
    if (s.startsWith("extra:")) {
      const ident = s.slice(6);
      const found = (extras || []).find((e) => e && (e.slug === ident || String(e.title || "").toLowerCase() === ident.toLowerCase()));
      return `${CORE_EXTRA_PREFIX}${found ? found.title : ident}`;
    }
    return s;
  }

  /** A line diff of two texts: [{kind: "same" | "del" | "add", text}] (the longest common subsequence of the lines; the texts are short). */
  function lineDiff(before, after) {
    const a = String(before || "").replace(/\r\n?/g, "\n").split("\n");
    const b = String(after || "").replace(/\r\n?/g, "\n").split("\n");
    const n = a.length;
    const m = b.length;
    const table = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    const out = [];
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) {
        out.push({ kind: "same", text: a[i] });
        i++;
        j++;
      } else if (table[i + 1][j] >= table[i][j + 1]) out.push({ kind: "del", text: a[i++] });
      else out.push({ kind: "add", text: b[j++] });
    }
    while (i < n) out.push({ kind: "del", text: a[i++] });
    while (j < m) out.push({ kind: "add", text: b[j++] });
    return out;
  }

  /** A fingerprint of what the panel shows of a bot: when it changes the detail is read again (the line-up / status / memory / L0 / proposals). */
  function fingerprint(bot) {
    if (!bot) return "";
    const init = bot.init || {};
    return JSON.stringify([bot.id, bot.name, bot.frozen, init.status, init.at, init.error, init.stale, bot.core && bot.core.updated_at, bot.core && bot.core.chars, bot.memory_stats, bot.staging, bot.avatar_code]);
  }

  const api = {
    DEFAULT_LIMITS,
    CORE_KEYS,
    CORE_HEADINGS,
    CORE_EXTRA_PREFIX,
    MAX_CORE_MARKDOWN_CHARS,
    MEMORY_PARTS,
    SCENARIO_PARTS,
    SOURCE_LABELS,
    INIT_SYMBOLS,
    INIT_LABELS,
    INIT_TARGET_STATES,
    NO_IMPRESSION,
    normalizeLimits,
    textLength,
    trimLength,
    norm,
    oneLine,
    botNameProblem,
    coreModel,
    cloneCore,
    coreDirty,
    coreProblems,
    coreStats,
    nameHint,
    headerValue,
    renderCoreMarkdown,
    parseCoreMarkdown,
    parsePlaybook,
    parsePeople,
    cleanImpression,
    countPart,
    partProblems,
    formatUnix,
    formatDuration,
    initView,
    initTargets,
    initProgressView,
    problemsOf,
    stagingLabel,
    lineDiff,
    fingerprint,
  };
  root.ArenaBotLib = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
