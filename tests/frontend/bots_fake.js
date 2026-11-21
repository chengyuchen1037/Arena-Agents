"use strict";
// The v0.14 bots / contract / rules / games part of the fake backend of the front-end tests (not a test).  It mirrors tools/mock_ui_bots.py +
// tools/mock_ui_hub.py (which tests/frontend/test_mock_bots.py / test_mock_files.py hold to docs/v0.12-contracts.md §13.7): the same shapes,
// the same refusals (needs_confirm, name_taken, game_active, init_busy, NOT_READY ...) and the same words - just smaller: the files are texts
// in memory, an initialization is a job whose progress a TEST delivers (`env.sse("bot.init.progress", ...)`), nothing happens by itself.
//
//   const fake = makeBotsFake({ b, resp, F });   // harness.js does this inside makeBackend(); b.bots* is what the routes read and write
//   fake.handle(key, url, body, init) -> a fetch response, or null when the route is not one of these
//   fake.upload(xhr) -> true when the XHR upload is one of these (a .arenabot / .arenagame)
//
// Tests change the world through `b.botsApi` (the `fake.api` below): set(seatOrId, patch), payload(full), detail(seatOrId), live() ...
const crypto = require("node:crypto");

const sha = (text) => crypto.createHash("sha256").update(String(text || ""), "utf8").digest("hex");
const clone = (v) => JSON.parse(JSON.stringify(v));
const len = (s) => Array.from(String(s === undefined || s === null ? "" : s)).length;
const oneLine = (s) => String(s === undefined || s === null ? "" : s).replace(/\s+/g, " ").trim();

const CORE_KEYS = ["identity", "voice", "mind"];
const HEADINGS = { identity: "身份", voice: "说话方式", mind: "分析方式" };
const CORE_LIMITS = { identity: 300, voice: 600, mind: 400 };
const PARTS = ["arch_notes", "digest", "playbook", "questions", "people"];
const LABELS = { arch_notes: "操作笔记", digest: "规则要点", playbook: "打法", questions: "待解问题", people: "印象" };
const TEXT_LIMITS = { arch_notes: 800, digest: 800, questions: 400 };
const SCENARIO_PARTS = ["digest", "playbook", "questions"];
const WILL_CLEAR = ["初始化结果", "操作笔记", "规则要点", "打法", "待解问题", "印象", "战绩"];
const RESET_CONFIRM_TEXT =
  "修改名字或头像会清空这个玩家的初始化结果和全部记忆（操作笔记、规则要点、打法、待解问题、印象、战绩），底层设定保留。修改后需要重新初始化才能开局。是否确认？";
const NOTE = "bot 已经通过复盘形成的记忆不会被删除，也无法通过删除对局撤销。";
const SOURCE_LABELS = { core: "底层设定", contract: "官方契约", scenario: "剧本", materials: "学习材料" };
const TOOLS = ["speak", "speak_template", "pass", "respond", "use_ability", "challenge_moderator", "sleep", "write_note", "read_notes", "compact_context", "jot", "read_contract"];
const CODES = [
  "NO_FLOOR", "NOT_MEMBER", "CHANNEL_CLOSED", "QUOTA_EXCEEDED", "TOO_LONG", "FORMAT", "STALE", "REQUEST_CLOSED", "ABILITY_DISABLED", "GAME_NOT_RUNNING",
  "INVALID_PARAMS", "INVALID_TARGET", "UNKNOWN_CHANNEL", "UNKNOWN_TEMPLATE", "UNKNOWN_ABILITY", "UNKNOWN_REQUEST", "UNKNOWN_PLAYER", "UNKNOWN_TICKET",
  "FORBIDDEN", "CONSTRAINT", "ALREADY_EXISTS", "INTERNAL_ERROR", "NOT_FINISHED",
];
const SCENARIO_ID = "werewolf_12p";

function makeBotsFake({ b, resp, F }) {
  const api = {};

  // ================================================================================================ texts and files
  function coreHash(core) {
    const parts = [core.identity, core.voice, core.mind].map((x) => String(x || "").trim());
    for (const e of core.extras || []) parts.push(`${String(e.title).trim()}\n${String(e.text).trim()}`);
    return sha(parts.join("\n"));
  }

  function cleanName(value) {
    const t = String(value === undefined || value === null ? "" : value).trim();
    if (!t) return { error: "名字不能为空。" };
    if (len(t) > 20) return { error: "名字最多 20 个字符。" };
    if (/[\x00-\x1f\x7f<>"'`\\]/.test(t)) return { error: "名字不能包含控制字符、引号、反斜杠或尖括号。" };
    if (/[:：]/.test(t)) return { error: "名字不能包含冒号（: 或 ：）。" };
    return { value: t };
  }

  const over = (label, limit, now) => `「${label}」最多 ${limit} 字（现在 ${now} 字）。`;
  const hasHeadingLine = (text) => String(text).split("\n").some((l) => /^ {0,3}#(?!#)(?:[ \t\u3000]+.*)?$/.test(l));

  /** Every fault of an L0 {identity, voice, mind, extras: [{title, text}]} as Chinese sentences. */
  function validateCore(core, requireComplete) {
    const problems = [];
    for (const k of CORE_KEYS) {
      const body = String(core[k] || "").trim();
      if (!body) {
        if (requireComplete) problems.push(`「${HEADINGS[k]}」不能为空。`);
      } else if (len(body) > CORE_LIMITS[k]) problems.push(over(HEADINGS[k], CORE_LIMITS[k], len(body)));
      else if (hasHeadingLine(body)) problems.push(`「${HEADINGS[k]}」里不能有以“# ”开头的行（会被当成分区标题），请去掉 # 或改用 ##。`);
    }
    const extras = core.extras || [];
    if (extras.length > 3) problems.push(`附加分区最多 3 份（现在 ${extras.length} 份）。`);
    const seen = new Set();
    for (const e of extras) {
      const name = String(e.title || "").trim();
      const label = `附加：${name}`;
      if (!name) problems.push("附加分区需要标题。");
      else if (len(name) > 20) problems.push(`附加分区的标题最多 20 字（现在 ${len(name)} 字）：${name.slice(0, 30)}`);
      else if (seen.has(name.toLowerCase())) problems.push(`附加分区「${name}」重复了。`);
      seen.add(name.toLowerCase());
      const body = String(e.text || "").trim();
      if (!body) problems.push(`「${label}」不能为空。`);
      else if (len(body) > 300) problems.push(over(label, 300, len(body)));
    }
    return problems;
  }

  /** The merged L0 file -> {core, hint, problems} (botfiles.parse_core_markdown, shorter). */
  function parseCoreMarkdown(text) {
    const lines = String(text === undefined || text === null ? "" : text).replace(/^\ufeff/, "").replace(/\r\n?/g, "\n").split("\n");
    let start = 0;
    while (start < lines.length && !lines[start].trim()) start++;
    if (start >= lines.length || lines[start].trim() !== "---") {
      return { problems: ["缺少文件头：文件开头要有用 --- 包住的 format: arena-core 和 version: 1。"] };
    }
    let end = start + 1;
    while (end < lines.length && lines[end].trim() !== "---") end++;
    if (end >= lines.length) return { problems: ["文件头没有结束：缺少第二行 ---。"] };
    const fields = {};
    for (const line of lines.slice(start + 1, end)) {
      if (!line.trim() || line.trim().startsWith("#")) continue;
      const m = line.match(/^\s*([^:：]+?)\s*[:：]\s*(.*?)\s*$/);
      if (m && !(m[1].trim().toLowerCase() in fields)) fields[m[1].trim().toLowerCase()] = m[2].replace(/(?:^|\s)#.*$/, "").trim();
    }
    const problems = [];
    if (!fields.format) problems.push("文件头缺少 format: arena-core。");
    else if (fields.format.toLowerCase() !== "arena-core") problems.push(`文件头的 format 必须是 arena-core（现在是「${fields.format}」）。`);
    if (!fields.version) problems.push("文件头缺少 version: 1。");
    else if (!/^1(?:\.0+)?$/.test(fields.version)) problems.push(`文件头的 version 目前只能是 1（现在是「${fields.version}」）。`);
    const sections = [];
    for (const line of lines.slice(end + 1)) {
      const m = line.match(/^ {0,3}#(?!#)(?:[ \t\u3000]+(.*?))?[ \t\u3000]*$/);
      if (m) sections.push({ heading: (m[1] || "").trim(), body: [] });
      else if (sections.length) sections[sections.length - 1].body.push(line);
      else if (line.trim()) problems.push("第一个分区标题之前有多余的内容：分区要用一级标题开头，如「# 身份」。");
    }
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
      const ex = s.heading.match(/^附加\s*[:：]\s*(.*)$/);
      if (!ex) {
        problems.push(`不认识的分区标题「${s.heading || "（空）"}」：只能是 「身份」、「说话方式」、「分析方式」 或「附加：<标题>」。`);
        continue;
      }
      const title = ex[1].trim();
      if (!title) {
        problems.push("附加分区需要标题，写成「# 附加：<标题>」。");
        continue;
      }
      extraCounts[title.toLowerCase()] = (extraCounts[title.toLowerCase()] || 0) + 1;
      if (extraCounts[title.toLowerCase()] === 1) extras.push({ title, text: body });
    }
    for (const k of CORE_KEYS) if (counts[k] > 1) problems.push(`「${HEADINGS[k]}」分区出现了 ${counts[k]} 次，只能有 1 个。`);
    for (const e of extras) if (extraCounts[e.title.toLowerCase()] > 1) problems.push(`「附加：${e.title}」分区出现了 ${extraCounts[e.title.toLowerCase()]} 次，只能有 1 个。`);
    for (const k of CORE_KEYS) {
      if (!(k in byKey)) problems.push(`缺少「${HEADINGS[k]}」分区。`);
      else if (!byKey[k]) problems.push(`「${HEADINGS[k]}」不能为空。`);
    }
    const core = { identity: byKey.identity || "", voice: byKey.voice || "", mind: byKey.mind || "", extras };
    for (const p of validateCore(core, false)) if (!problems.includes(p)) problems.push(p);
    // (the core comes with the problems too: a draft of the moderator's model is shown with its faults as warnings)
    return { core, hint: fields.name || null, problems };
  }

  // ---- the playbook and the impressions ------------------------------------------------------------------------------
  const BULLET = /^\s*(?:[-*•·]+|\d{1,2}[.)、．])\s*/;
  const clip = (s, n) => String(s).slice(0, n);

  function parsePlaybook(text) {
    const entries = [];
    let ignored = 0;
    let section = "";
    for (const raw of String(text || "").replace(/\r\n?/g, "\n").split("\n")) {
      let line = raw.trim();
      if (!line) continue;
      const h = line.startsWith("#") && line.match(/^ {0,3}#{1,6}[ \t\u3000]+(.*?)[ \t\u3000]*#*[ \t\u3000]*$/);
      if (h) {
        section = h[1].trim();
        continue;
      }
      if (section && section !== "假设") continue;
      const bullet = line.match(BULLET);
      if (bullet) line = line.slice(bullet[0].length);
      else if (!/^[\[［]/.test(line)) {
        ignored++;
        continue;
      }
      let fields = { tags: [] };
      let body = line;
      const m = line.match(/^[\[［]([^\]］]*)[\]］]\s*(.*)$/);
      if (m) {
        body = m[2];
        for (let piece of m[1].replace(/｜/g, "|").replace(/：/g, ":").replace(/＃/g, "#").split("|")) {
          piece = piece.trim();
          if (!piece) continue;
          if (piece.startsWith("#")) {
            const tag = piece.slice(1).replace(/[\s|[\]［］#＃]/g, "").slice(0, 20);
            if (tag && !fields.tags.includes(tag)) fields.tags.push(tag);
            continue;
          }
          const i = piece.indexOf(":");
          if (i < 0) {
            if (["低", "中", "高"].includes(piece)) fields.confidence = piece;
            continue;
          }
          const key = piece.slice(0, i).trim();
          const value = piece.slice(i + 1).trim();
          const num = () => (value.match(/^\s*(\d+)/) ? Number(value.match(/^\s*(\d+)/)[1]) : 0);
          if (key === "把握") fields.confidence = [...value.slice(0, 3)].find((c) => "低中高".includes(c)) || { low: "低", mid: "中", medium: "中", high: "高" }[value.toLowerCase()] || "低";
          else if (key === "支持") fields.support = num();
          else if (key === "反例") fields.counter = num();
          else if (key === "来源") fields.source = value;
        }
      }
      body = oneLine(body);
      if (!body) {
        ignored++;
        continue;
      }
      entries.push({ text: body, confidence: fields.confidence || "低", support: fields.support || 0, counter: fields.counter || 0, source: clip(oneLine(fields.source || "").replace(/[|[\]［］]/g, "/"), 40), tags: fields.tags });
    }
    return { entries, ignored };
  }

  const renderEntry = (e) =>
    `- [${[`把握:${e.confidence}`, `支持:${e.support}`, `反例:${e.counter}`, ...(e.source ? [`来源:${e.source}`] : []), ...e.tags.map((t) => `#${t}`)].join("|")}] ${e.text}`;
  const renderPlaybook = (entries) => (entries.length ? `## 假设\n${entries.map(renderEntry).join("\n")}\n` : "");

  function parsePeople(text) {
    const found = {};
    let ignored = 0;
    for (const raw of String(text || "").replace(/\r\n?/g, "\n").split("\n")) {
      let line = raw.trim();
      if (!line || line.startsWith("#")) continue;
      const bullet = line.match(BULLET);
      if (bullet) line = line.slice(bullet[0].length);
      const m = line.match(/^([^:：]+?)\s*[:：]\s*(.*)$/);
      if (!m) {
        ignored++;
        continue;
      }
      const impression = cleanImpression(m[2]);
      if (!m[1].trim() || !impression) {
        if (!impression && m[1].trim()) continue;
        ignored++;
        continue;
      }
      found[m[1].trim()] = impression;
    }
    return { people: found, ignored };
  }

  const cleanImpression = (v) => {
    const t = oneLine(v);
    return t === "（暂无）" || t === "(暂无)" || t === "暂无" ? "" : t;
  };
  const renderPeople = (people) => {
    const lines = Object.entries(people)
      .map(([n, v]) => [oneLine(n), cleanImpression(v)])
      .filter(([n, v]) => n && v)
      .map(([n, v]) => `- ${n}：${v}`);
    return lines.length ? `${lines.join("\n")}\n` : "";
  };

  /** A text meant for `part` -> {text} or {problems}.  (botfiles.normalize_part) */
  function normalizePart(part, content) {
    if (!PARTS.includes(part)) return { problems: [`不认识的记忆部分「${part}」，只能是 ${PARTS.join("、")}。`] };
    const label = LABELS[part];
    const raw = String(content === undefined || content === null ? "" : content).replace(/^\ufeff/, "").replace(/\r\n?/g, "\n");
    if (raw.includes("\x00")) return { problems: [`${label}不能包含空字符。`] };
    if (part === "playbook") {
      const { entries } = parsePlaybook(raw);
      if (raw.trim() && !entries.length) return { problems: ["没有读出任何一条打法：每条写成一行「- [把握:中|支持:0|反例:0|来源:复盘|#标签] 一句话」。"] };
      const problems = [];
      if (entries.length > 20) problems.push(`打法最多 20 条（现在 ${entries.length} 条）。`);
      entries.forEach((e, i) => {
        if (len(e.text) > 80) problems.push(`打法第 ${i + 1} 条最多 80 字（现在 ${len(e.text)} 字）：${e.text.slice(0, 16)}`);
      });
      return problems.length ? { problems } : { text: renderPlaybook(entries).trim() };
    }
    if (part === "people") {
      const { people, ignored } = parsePeople(raw);
      if (raw.trim() && !Object.keys(people).length && ignored) return { problems: ["没有读出任何一条印象：每条写成一行「- 名字：一句话印象」。"] };
      const problems = [];
      if (Object.keys(people).length > 30) problems.push(`印象最多 30 人（现在 ${Object.keys(people).length} 人）。`);
      for (const [n, v] of Object.entries(people)) if (len(v) > 80) problems.push(`对「${n}」的印象最多 80 字（现在 ${len(v)} 字）。`);
      return problems.length ? { problems } : { text: renderPeople(people).trim() };
    }
    const text = raw.trim();
    if (len(text) > TEXT_LIMITS[part]) return { problems: [over(label, TEXT_LIMITS[part], len(text))] };
    return { text };
  }

  // ================================================================================================ the stores
  const defaultContractMarkdown = () =>
    `---\nformat: arena-contract\nversion: v0.14.0\n---\n# 平台契约\n\n## 必读\n\n你只能通过工具影响这个世界。\`speak\` \`pass\` 是发言。\`TOO_LONG\` 和 \`FORMAT\` 会退回。\n\n## 参考\n\n工具：${TOOLS.map((t) => `\`${t}\``).join("、")}。回执码：${CODES.map((c) => `\`${c}\``).join("、")}。\n`;

  const mentions = (md, term) => new RegExp(`(?<![A-Za-z0-9_])${term}(?![A-Za-z0-9_])`).test(md || "");

  function validateContract(markdown) {
    const text = String(markdown || "");
    if (!text.trim()) return ["契约是空的。"];
    const problems = [];
    if (len(text) > 20000) problems.push(`契约最多 20000 字（现在 ${len(text)} 字）。`);
    const header = text.replace(/^\s+/, "").startsWith("---") ? text.split("---")[1] || "" : null;
    if (header === null) problems.push("缺少格式头：文件开头需要用 --- 包住的一段，其中写 format: arena-contract。");
    else if (!/^format:\s*arena-contract\s*$/m.test(header)) problems.push(`格式头里的 format 必须是 arena-contract（现在是 ${(header.match(/^format:\s*(.*)$/m) || [])[1] || "空"}）。`);
    const headings = [...text.matchAll(/^##[ \t]+(.+?)\s*$/gm)].map((m) => m[1].trim());
    for (const h of ["必读", "参考"]) {
      const n = headings.filter((x) => x === h).length;
      if (n === 0) problems.push(`缺少“## ${h}”一节。`);
      else if (n > 1) problems.push(`“## ${h}”出现了 ${n} 次，只能有一节。`);
    }
    const must = (text.split(/^##[ \t]+必读\s*$/m)[1] || "").split(/^##[ \t]+/m)[0].trim();
    if (len(must) > 1200) problems.push(`“必读”最多 1200 字（现在 ${len(must)} 字）。`);
    const tools = TOOLS.filter((t) => !mentions(text, t));
    const codes = CODES.filter((c) => !mentions(text, c));
    if (tools.length) problems.push(`契约没有提到这些工具：${tools.join("、")}。每个工具的名字都要出现在契约里。`);
    if (codes.length) problems.push(`契约没有提到这些回执码：${codes.join("、")}。每个回执码都要出现在契约里。`);
    return problems;
  }

  const contractVersion = (md) => {
    const m = String(md || "").match(/^version:\s*([A-Za-z0-9][A-Za-z0-9._-]{0,31})\s*$/m);
    return m ? m[1] : "custom";
  };
  const ymd = () => {
    const d = new Date(1763600000 * 1000);
    return `${d.getUTCFullYear()}${String(d.getUTCMonth() + 1).padStart(2, "0")}${String(d.getUTCDate()).padStart(2, "0")}`;
  };

  const scenarioMarkdown = (version = "2.2.0") =>
    `---\nschema_version: 2\nid: ${SCENARIO_ID}\nversion: ${version}\ntitle: 12 人标准局狼人杀（暗牌局，测试）\nmanual_heading: 主持人手册\n---\n# 12 人标准局\n\n## 玩家规则\n\n分成好人和狼人两个阵营。\n\n## 主持人手册\n\n按流程主持。\n`;

  function parseScenario(markdown, name = "scenario.md") {
    const text = String(markdown || "");
    if (!text.trim()) return { problems: [`剧本文件 ${name} 是空的。`] };
    if (!text.replace(/^\s+/, "").startsWith("---")) return { problems: [`剧本文件 ${name} 缺少文件头：开头需要用 --- 包住的 schema_version、id、version、title。`] };
    const header = text.split("---")[1] || "";
    const fields = {};
    for (const line of header.split("\n")) {
      const m = line.match(/^([A-Za-z_]+):\s*(.*)$/);
      if (m) fields[m[1]] = m[2].replace(/(?:^|\s)#.*$/, "").trim();
    }
    const problems = [];
    if (fields.schema_version !== "2") problems.push(`schema_version 必须是 2（现在是 ${fields.schema_version || "空"}）。`);
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(fields.id || "")) problems.push("id 只能由字母、数字、下划线和连字符组成，最多 64 个字符。");
    if (!fields.title) problems.push("缺少 title。");
    if (!fields.version) problems.push("缺少 version。");
    if (!text.includes("主持人手册")) problems.push("缺少“主持人手册”一节（manual_heading 指向的标题）。");
    return problems.length ? { problems: [`剧本文件 ${name} 有问题：${problems.join(" ")}`] } : { fields };
  }

  const state = {
    contract: { markdown: defaultContractMarkdown(), origin: "default", imported_at: null, broken: null },
    rules: { [SCENARIO_ID]: { markdown: scenarioMarkdown(), origin: "default", imported_at: null, materials: {}, broken: null } },
    initPlan: null, // a test sets {job_id?, ...}: what POST /api/bots/init answers
  };
  api.contractState = state.contract;
  api.rulesState = state.rules;

  const sourcesNow = () => {
    const sc = state.rules[SCENARIO_ID];
    const mats = Object.entries(sc.materials).sort(([a], [z]) => (a < z ? -1 : 1));
    return {
      contract: state.contract.broken === "missing" ? "" : sha(state.contract.markdown),
      scenario: sc.broken === "missing" ? "" : sha(sc.markdown),
      materials: mats.length ? sha(mats.map(([slug, m]) => `${slug}:${sha(m.markdown)}`).join("\n")) : "",
      scenario_id: SCENARIO_ID,
    };
  };

  // ================================================================================================ the bots
  let idCounter = 0x2000;
  const newId = () => `b-${(++idCounter).toString(16).padStart(8, "0")}`;

  function makeBot(n) {
    const core = F.makeCore(n);
    const bot = {
      id: F.botId(n),
      name: F.NAMES[n - 1],
      avatar: F.avatarOf(n),
      origin: "default",
      created_at: 1763000000 + n,
      core,
      core_updated_at: 1763500000,
      init: { status: "ready", at: 1763600000, model: "deepseek-flash", duration_s: 74.2, error: null, scenario_id: SCENARIO_ID, hashes: null },
      mem: {
        arch_notes: "我每次被叫醒，先看新动态和唤醒原因，判断有没有轮到我的事；没有就记一笔然后睡。",
        people: n <= 5 ? renderPeople({ [F.NAMES[n % 12]]: "发言短，站边快，可信度中。", [F.NAMES[(n + 1) % 12]]: "逻辑细，抓细节准。" }).trim() : "",
        record: n <= 5 ? { games: 7, wins: 3, by_role: { 平民: [5, 3] } } : { games: 0, wins: 0, by_role: {} },
        rules: {
          [SCENARIO_ID]: {
            digest: "好人要找出狼，狼要藏起来。白天先发言再投票放逐，夜里各个身份分别行动。",
            playbook: F.makeBotDetail(n).memory.playbook_md,
            questions: "平票的时候，被 PK 的两个人该怎么发言才最有说服力？",
          },
        },
      },
      staging: [],
      binding: { ok: true, label: "DeepSeek / model-x" },
    };
    const src = sourcesNow();
    bot.init.hashes = { core: coreHash(core), contract: src.contract, scenario: src.scenario, materials: src.materials };
    return bot;
  }

  const bots = { order: [], byId: {}, memoryEnabled: true, initJob: null, usage: { calls: 0, tokens: 0, cost_usd: 0 }, moderator: { id: "moderator", name: "主持人", avatar: F.avatarOf(13) }, moderatorBinding: { ok: true, label: "DeepSeek / model-x" } };
  F.PLAYER_IDS.forEach((_, i) => {
    const bot = makeBot(i + 1);
    bots.byId[bot.id] = bot;
    bots.order.push(bot.id);
  });
  api.state = bots;

  const botOf = (ref) => bots.byId[ref] || bots.byId[bots.order[Number(String(ref).replace(/^N/, "")) - 1]] || null;
  const seatOfBot = (id) => (bots.order.includes(id) ? `N${bots.order.indexOf(id) + 1}` : null);
  api.bot = botOf;

  const scenarioOf = (sid) => state.rules[sid || SCENARIO_ID] || { markdown: "", materials: {} };
  const rulesOf = (bot, sid) => bot.mem.rules[sid] || { digest: "", playbook: "", questions: "" };
  const isFrozen = (bot) =>
    bot.init.status === "ready" || Boolean(bot.mem.arch_notes.trim()) || Object.values(bot.mem.rules).some((r) => SCENARIO_PARTS.some((p) => String(r[p] || "").trim()));

  function recordText(r) {
    if (!r.games) return "";
    const lines = [`场次 ${r.games}，胜 ${r.wins}，负 ${r.games - r.wins}，胜率 ${Math.round((100 * r.wins) / r.games)}%`];
    for (const [role, [played, won]] of Object.entries(r.by_role || {})) lines.push(`${role}：${played} 局，胜 ${won}`);
    return lines.join("\n");
  }

  function derivedInit(bot, running) {
    const init = bot.init;
    let status = init.status;
    const stale = [];
    if (status === "ready") {
      const cur = { ...sourcesNow(), core: coreHash(bot.core) };
      for (const k of ["core", "contract", "scenario", "materials"]) if (!init.hashes || String(init.hashes[k] || "") !== String(cur[k] || "")) stale.push(k);
      if (stale.length) status = "stale";
    }
    if (running) status = "initializing";
    return { status, at: init.at, model: init.model, duration_s: init.duration_s, error: init.status === "failed" ? init.error : null, stale };
  }

  const runningIds = () => (bots.initJob ? bots.initJob.ids.filter((id) => (bots.initJob.states[id] || "running") === "running" || bots.initJob.states[id] === "queued") : []);

  function summary(bot) {
    const core = bot.core;
    const missing = CORE_KEYS.filter((k) => !String(core[k] || "").trim());
    const sid = SCENARIO_ID;
    const rules = rulesOf(bot, sid);
    return {
      id: bot.id,
      name: bot.name,
      avatar: clone(bot.avatar),
      avatar_code: F.avatarCode(bot.avatar),
      seat: seatOfBot(bot.id),
      origin: bot.origin,
      core: {
        complete: !missing.length,
        missing,
        chars: { identity: len(core.identity), voice: len(core.voice), mind: len(core.mind) },
        extras: core.extras.map((e) => ({ title: e.title, slug: e.slug, chars: len(e.text) })),
        updated_at: bot.core_updated_at,
      },
      init: derivedInit(bot, runningIds().includes(bot.id)),
      frozen: isFrozen(bot),
      memory_stats: {
        arch_notes: len(bot.mem.arch_notes),
        digest: len(rules.digest),
        playbook: parsePlaybook(rules.playbook).entries.length,
        questions: len(rules.questions),
        people: Object.keys(parsePeople(bot.mem.people).people).length,
        record: bot.mem.record.games ? `场次 ${bot.mem.record.games} · 胜 ${bot.mem.record.wins} · 胜率 ${Math.round((100 * bot.mem.record.wins) / bot.mem.record.games)}%` : "",
      },
      staging: bot.staging.length,
      binding: clone(bot.binding),
    };
  }

  function detail(bot, sid) {
    const s = summary(bot);
    const scenario = sid || SCENARIO_ID;
    const rules = rulesOf(bot, scenario);
    return {
      ...s,
      core: {
        ...s.core,
        markdown: F.coreMarkdown(bot.core, bot.name),
        sections: { identity: bot.core.identity, voice: bot.core.voice, mind: bot.core.mind },
        extras: bot.core.extras.map((e) => ({ title: e.title, slug: e.slug, chars: len(e.text), text: e.text })),
      },
      memory: {
        scenario_id: scenario,
        arch_notes: bot.mem.arch_notes,
        digest: rules.digest,
        playbook: parsePlaybook(rules.playbook).entries,
        playbook_md: rules.playbook,
        questions: rules.questions,
        people: parsePeople(bot.mem.people).people,
        people_md: bot.mem.people,
        record: recordText(bot.mem.record),
      },
      staging: clone(bot.staging),
    };
  }

  const NOT_INIT = { code: "not_initialized", message: "还没有初始化。", fix: "init" };

  function botProblems(bot) {
    const core = bot.core;
    const items = [];
    const missing = CORE_KEYS.filter((k) => !String(core[k] || "").trim());
    if (missing.length === 3 && !core.extras.length) items.push({ code: "core_missing", message: "底层设定还没有写：身份、说话方式、分析方式都是空的。", fix: "import_core" });
    else {
      const faults = validateCore(core, false);
      if (missing.length) faults.unshift(`缺少${missing.map((k) => `「${HEADINGS[k]}」`).join("、")}。`);
      if (faults.length) items.push({ code: "core_incomplete", message: `底层设定不完整：${faults.join(" ")}`, fix: "edit_core" });
    }
    const d = derivedInit(bot, runningIds().includes(bot.id));
    if (d.status === "initializing") items.push({ code: "init_running", message: "正在初始化，请等它结束。", fix: "init" });
    else if (d.status === "uninitialized") items.push({ ...NOT_INIT });
    else if (d.status === "failed") items.push({ code: "init_failed", message: `上次初始化没有成功：${d.error || "原因不明"}`, fix: "init" });
    else if (d.status === "stale") items.push({ code: "stale", message: `初始化之后${d.stale.map((k) => SOURCE_LABELS[k]).join("、")}变了，需要重新初始化。`, fix: "init" });
    if (bot.init.status === "ready") {
      const rules = rulesOf(bot, SCENARIO_ID);
      const gone = [["arch_notes", bot.mem.arch_notes], ["digest", rules.digest], ["playbook", rules.playbook]].filter(([, t]) => !String(t || "").trim()).map(([p]) => LABELS[p]);
      if (gone.length) items.push({ code: "not_initialized", message: `缺少必需的记忆文件：${gone.join("、")}，请重新初始化。`, fix: "init" });
    }
    return items;
  }

  /** What keeps the game from starting (H25): the contract, the scenario, every player, every model (the same order as the real check). */
  function checkReady() {
    const problems = [];
    const c = state.contract;
    if (c.broken === "missing") problems.push(F.makeProblem("contract", [{ code: "contract_missing", message: "官方契约是空的，请在设置里导入契约或恢复默认。", fix: "import_contract" }]));
    else if (c.broken === "invalid") problems.push(F.makeProblem("contract", [{ code: "contract_invalid", message: "官方契约有问题：契约没有提到这些工具：jot、read_contract。每个工具的名字都要出现在契约里。", fix: "import_contract" }]));
    const sc = state.rules[SCENARIO_ID];
    if (sc.broken === "missing") problems.push(F.makeProblem("scenario", [{ code: "scenario_missing", message: `读不到剧本 ${SCENARIO_ID}：这份剧本不见了。`, fix: "import_rules" }]));
    else if (sc.broken === "invalid") problems.push(F.makeProblem("scenario", [{ code: "scenario_invalid", message: "剧本有问题：身份池共有 11 个身份，但对局有 12 名玩家，无法发牌。", fix: "import_rules" }]));
    for (const id of bots.order) {
      const items = botProblems(bots.byId[id]);
      if (items.length) problems.push(F.makeProblem("bot", items, { bot_id: id, seat: seatOfBot(id), name: bots.byId[id].name }));
    }
    for (const id of bots.order) {
      const bot = bots.byId[id];
      if (!bot.binding.ok) {
        problems.push(F.makeProblem("binding", [{ code: "no_binding", message: `${seatOfBot(id)} 还没有绑定可用的模型。`, fix: "bind_model" }], { bot_id: id, seat: seatOfBot(id), name: bot.name }));
      }
    }
    if (!bots.moderatorBinding.ok) problems.push(F.makeProblem("binding", [{ code: "no_binding", message: "主持人还没有绑定可用的模型。", fix: "bind_model" }], { name: "主持人" }));
    return problems;
  }
  api.checkReady = checkReady;

  function notReadyText(problems) {
    const parts = problems.slice(0, 3).map((p) => {
      const who = p.name || { contract: "官方契约", scenario: "剧本", binding: "模型绑定", game: "对局" }[p.scope] || "";
      const first = String((p.items[0] || {}).message || "").trim().replace(/。$/, "");
      return who ? `${who}：${first}` : first;
    });
    return `还没有准备好，不能开局：${parts.filter(Boolean).join("；")}${problems.length > 3 ? `（共 ${problems.length} 处）` : ""}。`;
  }
  api.notReadyText = notReadyText;

  /** `BotsPayload`: the live part, or with `full` also `limits` and `defaults` (GET /api/bots). */
  function payload(full) {
    const problems = checkReady();
    const out = {
      lineup: bots.order.slice(),
      bots: bots.order.map((id) => summary(bots.byId[id])),
      memory_enabled: bots.memoryEnabled,
      init_job: bots.initJob ? { job_id: bots.initJob.job_id, state: "running", ids: bots.initJob.ids.slice(), started_at: bots.initJob.started_at, deadline_s: 120 } : null,
      ready: { ok: !problems.length, problems },
      learning_usage: clone(bots.usage),
    };
    if (full) {
      out.limits = clone(F.BOT_LIMITS);
      out.defaults = F.makeDefaults();
    }
    return out;
  }
  api.payload = payload;
  api.live = () => payload(false);
  api.detail = (ref, sid) => detail(botOf(ref), sid);
  api.summary = (ref) => summary(botOf(ref));
  /** Change a bot of the fake: `patch` = {name, avatar, core: {...}, init: {...}, mem: {...}, binding, staging, origin} (nested objects merge). */
  api.set = (ref, patch) => {
    const bot = botOf(ref);
    for (const [k, v] of Object.entries(patch)) {
      if (["init", "mem", "binding"].includes(k)) Object.assign(bot[k], v);
      else if (k === "core") {
        Object.assign(bot.core, v);
        bot.core_updated_at = bot.core_updated_at || 1763500000;
      } else bot[k] = v;
    }
    return bot;
  };
  /** Make the bot "initialized now" under the current sources (what a finished initialization does). */
  api.initialized = (ref) => {
    const bot = botOf(ref);
    const src = sourcesNow();
    bot.init = { status: "ready", at: 1763800000, model: "deepseek-flash", duration_s: 80.5, error: null, scenario_id: SCENARIO_ID, hashes: { core: coreHash(bot.core), contract: src.contract, scenario: src.scenario, materials: src.materials } };
    return bot;
  };
  /** Put the table into a state: "fresh" (nobody initialized), "mixed" (✔ ⚠ ✖ ⏳ and an empty L0 - the states a page has to show). */
  api.preset = (name) => {
    for (const id of bots.order) {
      const bot = bots.byId[id];
      if (name === "fresh" || name === "mixed") {
        bot.init = { status: "uninitialized", at: null, model: null, duration_s: null, error: null, scenario_id: null, hashes: null };
        bot.mem.arch_notes = "";
        bot.mem.rules = {};
      }
    }
    if (name === "mixed") {
      const ids = bots.order;
      ["N1", "N8", "N9", "N10", "N11", "N12"].forEach((s) => api.initialized(s));
      botOf("N2").core.mind += "（刚改过）";
      api.initialized("N2");
      botOf("N2").core.mind += "（又改了）";
      api.initialized("N3");
      Object.assign(botOf("N3").init, { status: "failed", error: "模型调用失败：HTTP 429（模拟）", hashes: null });
      botOf("N6").core = { identity: "", voice: "", mind: "", extras: [] };
      botOf("N6").core_updated_at = null;
      void ids;
    }
  };

  // ---- writes ---------------------------------------------------------------------------------------------------------
  const err = (status, detailText, code, extra) => resp(status, { detail: detailText, ...(code ? { code } : {}), ...(extra || {}) });
  const notFound = (what) => err(404, what || "没有这个玩家（bot）。", "not_found");
  const idle = () => b.session.status === "idle";
  const gameActive = (what) => (idle() ? null : err(409, `${what}只能在对局开始之前修改；请先新建对局。`, "game_active"));
  const busy = (ids) => {
    const run = runningIds();
    return run.length && (ids === null || ids.some((i) => run.includes(i))) ? err(409, "正在初始化，请等它结束（或先取消）后再修改。", "init_busy") : null;
  };
  const problemsAnswer = (prefix, problems, status = 400, code) =>
    resp(status, { detail: `${prefix}${problems.length === 1 ? problems[0] : `共 ${problems.length} 处问题：${problems.join(" ")}`}`, ...(code ? { code } : {}), problems });
  const changed = (roster) => {
    b.botsChanges = (b.botsChanges || []).concat([{ roster: Boolean(roster) }]);
    if (typeof b.onBotsChanged === "function") b.onBotsChanged(roster);
  };

  function setCoreOf(bot, core) {
    const taken = new Map(bot.core.extras.map((e) => [e.title.trim().toLowerCase(), e.slug]));
    const used = new Set();
    bot.core = {
      identity: core.identity.trim(),
      voice: core.voice.trim(),
      mind: core.mind.trim(),
      extras: core.extras.map((e, i) => {
        let slug = taken.get(e.title.trim().toLowerCase());
        if (!slug || used.has(slug)) {
          const base = e.title.toLowerCase().replace(/[^a-z0-9-]/g, "").replace(/^-+/, "").slice(0, 32).replace(/-+$/, "") || `x${i + 1}`;
          slug = base;
          for (let n = 2; used.has(slug); n++) slug = `${base}-${n}`;
        }
        used.add(slug);
        return { title: e.title.trim(), slug, text: e.text.trim() };
      }),
    };
    bot.core_updated_at = 1763900000;
  }

  function resetIdentity(bot, name, avatar) {
    const fresh = makeBot(1);
    const id = newId();
    Object.assign(bot, {
      id,
      name,
      avatar,
      core: bot.core,
      init: { status: "uninitialized", at: null, model: null, duration_s: null, error: null, scenario_id: null, hashes: null },
      mem: { arch_notes: "", people: "", record: { games: 0, wins: 0, by_role: {} }, rules: {} },
      staging: [],
    });
    void fresh;
    return id;
  }

  function updateBot(bot, body) {
    let name = bot.name;
    let renamed = false;
    if (body.name !== undefined) {
      const checked = cleanName(body.name);
      if (checked.error) return err(400, checked.error);
      const clash = bots.order.map((i) => bots.byId[i]).find((x) => x.id !== bot.id && x.name.toLowerCase() === checked.value.toLowerCase());
      if (clash) return err(400, `名字「${checked.value}」已被 ${seatOfBot(clash.id)} 使用，名字不能重复。`, "name_taken");
      name = checked.value;
      renamed = name !== bot.name;
    }
    let avatar = bot.avatar;
    let reAvatar = false;
    if (body.avatar !== undefined && body.avatar !== null) {
      if (typeof body.avatar !== "object" || body.avatar.v !== 1) return err(400, "头像描述不合法：v 必须是 1");
      avatar = body.avatar;
      reAvatar = JSON.stringify(avatar) !== JSON.stringify(bot.avatar);
    }
    if (!renamed && !reAvatar) return bot;
    if (!isFrozen(bot)) {
      bot.name = name;
      bot.avatar = avatar;
      return bot;
    }
    if (!body.confirm_reset) return err(409, RESET_CONFIRM_TEXT, "needs_confirm", { will_clear: WILL_CLEAR.slice() });
    const oldId = bot.id;
    const newBotId = newId();
    const at = bots.order.indexOf(oldId);
    delete bots.byId[oldId];
    Object.assign(bot, {
      id: newBotId,
      name,
      avatar,
      init: { status: "uninitialized", at: null, model: null, duration_s: null, error: null, scenario_id: null, hashes: null },
      mem: { arch_notes: "", people: "", record: { games: 0, wins: 0, by_role: {} }, rules: {} },
      staging: [],
    });
    bots.byId[newBotId] = bot;
    bots.order[at] = newBotId;
    return bot;
  }

  // ================================================================================================ the routes
  const decode = (s) => decodeURIComponent(s);

  function handle(key, url, body, init) {
    let m;
    // ---- the table
    if (key === "GET /api/bots") return resp(200, payload(true));
    if (key === "PUT /api/bots/lineup") {
      const refused = gameActive("阵容");
      if (refused) return refused;
      const order = body && body.order;
      if (!Array.isArray(order) || order.length !== bots.order.length || [...order].sort().join() !== [...bots.order].sort().join()) {
        return err(422, `阵容必须是现在这 ${bots.order.length} 个玩家的一个排列：不能多、不能少、不能重复。`, "bad_lineup");
      }
      bots.order = order.slice();
      b.lineupPuts = (b.lineupPuts || []).concat([order.slice()]);
      changed(true);
      return resp(200, payload(false));
    }
    if (key === "POST /api/bots/init") {
      b.initCalls = (b.initCalls || []).concat([body || {}]);
      if (!["idle", "finished"].includes(b.session.status)) return err(409, "对局正在进行（或已暂停），现在不能初始化：请先新建对局。", "game_active");
      if (bots.initJob) return err(409, "已经有一个初始化任务在运行：请等它结束，或者先取消。", "init_busy");
      const states = (body && body.ids) || (body && body.rebuild ? bots.order.slice() : bots.order.filter((id) => ["uninitialized", "stale", "failed"].includes(derivedInit(bots.byId[id], false).status)));
      if (!states.length) return err(400, "没有需要初始化的玩家。");
      const unbound = states.filter((id) => !bots.byId[id].binding.ok);
      if (unbound.length) {
        return err(409, `有玩家还没有绑定模型，不能初始化：${unbound.map((i) => bots.byId[i].name).join("、")}。`, "no_binding", {
          problems: unbound.map((id) => F.makeProblem("binding", [{ code: "no_binding", message: "还没有给这个玩家绑定可用的模型。", fix: "bind_model" }], { bot_id: id, seat: seatOfBot(id), name: bots.byId[id].name })),
        });
      }
      const job = { job_id: (state.initPlan && state.initPlan.job_id) || `job-init-${(b.initCalls || []).length}`, ids: states.slice(), started_at: 1763800000, states: {} };
      bots.initJob = job;
      changed(false);
      return resp(202, { job_id: job.job_id, ids: job.ids.slice(), deadline_s: 120 });
    }
    if (key === "POST /api/bots/init:cancel") {
      b.initCancels = (b.initCancels || 0) + 1;
      bots.initJob = null;
      changed(false);
      return resp(200, { ok: true });
    }
    if (key === "POST /api/bots/core:clear" || key === "POST /api/bots/memory:clear") {
      const ids = body && Array.isArray(body.ids) ? body.ids : null;
      b.clearCalls = (b.clearCalls || []).concat([{ key, ids }]);
      const refused = busy(ids);
      if (refused) return refused;
      for (const id of ids || bots.order.slice()) {
        const bot = bots.byId[id];
        if (!bot) return notFound();
        if (key.endsWith("core:clear")) {
          bot.core = { identity: "", voice: "", mind: "", extras: [] };
          bot.core_updated_at = null;
        } else {
          bot.mem = { arch_notes: "", people: "", record: { games: 0, wins: 0, by_role: {} }, rules: {} };
          bot.init = { status: "uninitialized", at: null, model: null, duration_s: null, error: null, scenario_id: null, hashes: null };
        }
      }
      changed(false);
      return resp(200, payload(false));
    }
    if (key === "PUT /api/bots/memory-enabled") {
      if (typeof (body || {}).enabled !== "boolean") return err(422, "enabled 必须是 true 或 false。");
      bots.memoryEnabled = body.enabled;
      b.session.memory_enabled = body.enabled;
      changed(false);
      return resp(200, payload(false));
    }
    if (key === "POST /api/bots/avatars:regenerate") {
      const refused = gameActive("头像") || busy(null);
      if (refused) return refused;
      const skipped = bots.order.filter((id) => isFrozen(bots.byId[id]));
      bots.order.forEach((id, i) => {
        if (!skipped.includes(id)) bots.byId[id].avatar = F.avatarOf(i + 20 + (b.regenRolls = (b.regenRolls || 0) + 1));
      });
      bots.moderator.avatar = F.avatarOf(60);
      changed(true);
      return resp(200, { bots: payload(false), skipped });
    }
    // ---- the moderator
    if (key === "PUT /api/moderator") {
      b.moderatorPuts = (b.moderatorPuts || []).concat([body]);
      if (!body || Object.keys(body).some((k) => k !== "avatar")) return err(400, "主持人只能更换头像，名字、性格设定和记忆都是固定的。");
      if (!body.avatar) return err(400, "请给出头像描述（avatar）。");
      const refused = gameActive("头像");
      if (refused) return refused;
      bots.moderator.avatar = body.avatar;
      changed(true);
      return resp(200, clone(bots.moderator));
    }
    if (key === "POST /api/moderator/avatar:regenerate") {
      const refused = gameActive("头像");
      if (refused) return refused;
      bots.moderator.avatar = F.avatarOf(41 + (b.moderatorRolls = (b.moderatorRolls || 0) + 1));
      changed(true);
      return resp(200, clone(bots.moderator));
    }
    // ---- one bot
    if ((m = key.match(/^GET \/api\/bots\/([^/?]+)(?:\?scenario=([^&]*))?$/))) {
      const bot = bots.byId[decode(m[1])];
      return bot ? resp(200, detail(bot, m[2] && decode(m[2]))) : notFound();
    }
    if ((m = key.match(/^PUT \/api\/bots\/([^/?]+)$/))) {
      const bot = bots.byId[decode(m[1])];
      if (!bot) return notFound();
      b.botPuts = (b.botPuts || []).concat([{ id: bot.id, ...body }]);
      const extra = Object.keys(body || {}).filter((k) => !["name", "avatar", "confirm_reset"].includes(k));
      if (extra.length) return err(422, `不认识的字段：${extra.join("、")}`);
      const refused = (body.name !== undefined || body.avatar !== undefined ? gameActive("名字和头像") : null) || busy([bot.id]);
      if (refused) return refused;
      const out = updateBot(bot, body || {});
      if (out.status) return out; // a refusal (a response)
      changed(true);
      return resp(200, detail(bot));
    }
    if ((m = key.match(/^POST \/api\/bots\/([^/?]+)\/avatar:regenerate$/))) {
      const bot = bots.byId[decode(m[1])];
      if (!bot) return notFound();
      const refused = gameActive("头像") || busy([bot.id]);
      if (refused) return refused;
      const out = updateBot(bot, { avatar: F.avatarOf(30 + bots.order.indexOf(bot.id) + (b.regenRolls = (b.regenRolls || 0) + 1)), confirm_reset: Boolean(body && body.confirm_reset) });
      if (out.status) return out;
      changed(true);
      return resp(200, detail(bot));
    }
    // ---- L0
    if ((m = key.match(/^GET \/api\/bots\/([^/?]+)\/core(\?download=1)?$/))) {
      const bot = bots.byId[decode(m[1])];
      if (!bot) return notFound();
      if (m[2]) return resp(200, F.coreMarkdown(bot.core, bot.name), "text/markdown; charset=utf-8", { "content-disposition": `attachment; filename="${bot.name}.core.md"` });
      return resp(200, detail(bot).core);
    }
    if ((m = key.match(/^PUT \/api\/bots\/([^/?]+)\/core$/))) {
      const bot = bots.byId[decode(m[1])];
      if (!bot) return notFound();
      b.corePuts = (b.corePuts || []).concat([{ id: bot.id, ...body }]);
      if (typeof (body || {}).markdown !== "string") return err(422, "markdown 必须是文字。");
      const refused = busy([bot.id]);
      if (refused) return refused;
      const parsed = parseCoreMarkdown(body.markdown);
      if (parsed.problems.length) return problemsAnswer("底层设定没有保存：", parsed.problems);
      setCoreOf(bot, parsed.core);
      changed(false);
      const out = detail(bot);
      if (parsed.hint && parsed.hint.toLowerCase() !== bot.name.toLowerCase()) out.notices = [`文件里的名字是「${parsed.hint}」，只作提示，没有改名（这个玩家仍叫「${bot.name}」）。`];
      return resp(200, out);
    }
    if ((m = key.match(/^DELETE \/api\/bots\/([^/?]+)\/core$/))) {
      const bot = bots.byId[decode(m[1])];
      if (!bot) return notFound();
      const refused = busy([bot.id]);
      if (refused) return refused;
      bot.core = { identity: "", voice: "", mind: "", extras: [] };
      bot.core_updated_at = null;
      changed(false);
      return resp(200, detail(bot));
    }
    if ((m = key.match(/^POST \/api\/bots\/([^/?]+)\/core:refine$/))) {
      const bot = bots.byId[decode(m[1])];
      if (!bot) return notFound();
      b.refineCalls = (b.refineCalls || []).concat([{ id: bot.id, ...body }]);
      if (b.refineError) return resp(b.refineError[0], { detail: b.refineError[1], ...(b.refineError[2] ? { code: b.refineError[2] } : {}) });
      const notes = String((body || {}).text || "").trim();
      if (!notes) return err(400, "请先写下一些要点，再让主持人模型完善。");
      const lines = notes.split("\n").map(oneLine).filter(Boolean);
      const core = { identity: lines[0] || bot.core.identity, voice: lines.slice(1, 3).join("\n") || bot.core.voice || "你说话简短直接。", mind: bot.core.mind || "你先看事实再听说法。", extras: bot.core.extras.map((e) => ({ ...e })) };
      const draft = typeof b.refineDraft === "string" ? b.refineDraft : F.coreMarkdown(core, bot.name); // (a test may make the model answer anything)
      const warnings = validateCore(parseCoreMarkdown(draft).core || core, true);
      return resp(200, { draft, ...(warnings.length ? { problems: warnings } : {}) });
    }
    // ---- memory
    if ((m = key.match(/^PUT \/api\/bots\/([^/?]+)\/memory\/([^/?]+)$/))) {
      const bot = bots.byId[decode(m[1])];
      if (!bot) return notFound();
      const part = decode(m[2]);
      b.memoryPuts = (b.memoryPuts || []).concat([{ id: bot.id, part, ...body }]);
      if (!PARTS.includes(part)) return notFound(`没有这个记忆部分：${part.slice(0, 24)}`);
      if (typeof (body || {}).content !== "string") return err(422, "content 必须是文字。");
      const refused = busy([bot.id]);
      if (refused) return refused;
      const normalized = normalizePart(part, body.content);
      if (normalized.problems) return problemsAnswer("没有保存：", normalized.problems);
      const sid = body.scenario_id || SCENARIO_ID;
      if (part === "arch_notes") bot.mem.arch_notes = normalized.text;
      else if (part === "people") bot.mem.people = normalized.text;
      else (bot.mem.rules[sid] = bot.mem.rules[sid] || { digest: "", playbook: "", questions: "" })[part] = normalized.text;
      changed(false);
      return resp(200, detail(bot, SCENARIO_PARTS.includes(part) ? sid : undefined));
    }
    if ((m = key.match(/^DELETE \/api\/bots\/([^/?]+)\/memory(?:\?part=([^&]*))?$/))) {
      const bot = bots.byId[decode(m[1])];
      if (!bot) return notFound();
      const refused = busy([bot.id]);
      if (refused) return refused;
      const part = m[2] ? decode(m[2]) : null;
      b.memoryDeletes = (b.memoryDeletes || []).concat([{ id: bot.id, part }]);
      if (part === null) {
        bot.mem = { arch_notes: "", people: "", record: { games: 0, wins: 0, by_role: {} }, rules: {} };
        bot.init = { status: "uninitialized", at: null, model: null, duration_s: null, error: null, scenario_id: null, hashes: null };
      } else if (!PARTS.includes(part)) return err(400, `part 必须是 ${PARTS.join("、")} 之一。`);
      else if (part === "arch_notes") bot.mem.arch_notes = "";
      else if (part === "people") bot.mem.people = "";
      else if (bot.mem.rules[SCENARIO_ID]) bot.mem.rules[SCENARIO_ID][part] = "";
      changed(false);
      return resp(200, detail(bot));
    }
    // ---- proposals
    if ((m = key.match(/^POST \/api\/bots\/([^/?]+)\/staging\/([^/?:]+):(accept|reject)$/))) {
      const bot = bots.byId[decode(m[1])];
      if (!bot) return notFound();
      const proposal = bot.staging.find((p) => p.id === m[2]);
      if (!proposal) return notFound("没有这条待确认的提案。");
      b.stagingCalls = (b.stagingCalls || []).concat([{ id: bot.id, pid: m[2], action: m[3] }]);
      if (m[3] === "accept") {
        const refused = busy([bot.id]);
        if (refused) return refused;
        const core = clone(bot.core);
        const [kind, ident] = proposal.section.startsWith("extra:") ? ["extra", proposal.section.slice(6)] : [proposal.section, ""];
        if (kind !== "extra") core[kind] = proposal.after;
        else {
          const at = core.extras.findIndex((e) => e.slug === ident || e.title.toLowerCase() === ident.toLowerCase());
          if (at >= 0) core.extras[at].text = proposal.after;
          else if (core.extras.length >= 3) return problemsAnswer("没有采纳：", [`附加分区最多 3 份，已经满了，不能再新增「${ident}」。`]);
          else core.extras.push({ title: ident, slug: "", text: proposal.after });
        }
        const problems = validateCore(core, false);
        if (problems.length) return problemsAnswer("没有采纳：", problems);
        setCoreOf(bot, core);
      }
      bot.staging = bot.staging.filter((p) => p.id !== m[2]);
      changed(false);
      return resp(200, detail(bot));
    }
    // ---- export (the page links to it) / import from the default library
    if ((m = key.match(/^GET \/api\/bots\/([^/?]+)\/export$/))) {
      const bot = bots.byId[decode(m[1])];
      return bot ? resp(200, "PK-fake-arenabot", "application/octet-stream", { "content-disposition": `attachment; filename="${bot.name}-20251119-120000.arenabot"` }) : notFound();
    }
    if ((m = key.match(/^POST \/api\/bots\/([^/?]+)\/import\?name=(.*)$/))) {
      const out = importBot(decode(m[1]), decode(m[2]), Number((body && body.size) || 0), body);
      return resp(out.status, out.body);
    }
    if ((m = key.match(/^POST \/api\/bots\/([^/?]+)\/import-default$/))) {
      const bot = bots.byId[decode(m[1])];
      if (!bot) return notFound();
      b.defaultImports = (b.defaultImports || []).concat([{ id: bot.id, ...body }]);
      const refused = gameActive("导入（会换掉名字和头像）") || busy([bot.id]);
      if (refused) return refused;
      const at = F.NAMES.findIndex((n) => n.toLowerCase() === String((body || {}).name || "").trim().toLowerCase());
      if (at < 0) return notFound(`默认库里没有「${String((body || {}).name || "").slice(0, 20)}」。`);
      if (bots.order.some((i) => i !== bot.id && bots.byId[i].name.toLowerCase() === F.NAMES[at].toLowerCase())) {
        return err(409, `名字「${F.NAMES[at]}」已被 ${seatOfBot(bots.order.find((i) => bots.byId[i].name === F.NAMES[at]))} 使用，名字不能重复。`, "name_taken");
      }
      const fresh = makeBot(at + 1);
      Object.assign(bot, { name: fresh.name, avatar: fresh.avatar, origin: "default", core: fresh.core, init: fresh.init, mem: fresh.mem, staging: [] });
      changed(true);
      return resp(200, detail(bot));
    }
    // ---- the official contract (③)
    if ((m = key.match(/^GET \/api\/contract(\?download=1)?$/))) {
      const c = state.contract;
      if (m[1]) {
        const v = contractVersion(c.markdown);
        return resp(200, c.markdown, "text/markdown; charset=utf-8", { "content-disposition": `attachment; filename="${v === "custom" ? `contract-custom-${ymd()}` : `contract-${v}`}.md"` });
      }
      return resp(200, contractPayload());
    }
    if (key === "PUT /api/contract") {
      b.contractPuts = (b.contractPuts || []).concat([body]);
      if (typeof (body || {}).markdown !== "string") return err(422, "markdown 必须是文字。");
      const problems = validateContract(body.markdown);
      if (problems.length) return problemsAnswer("契约没有保存：", problems, 400, "invalid");
      Object.assign(state.contract, { markdown: body.markdown, origin: "imported", imported_at: 1763900000, broken: null });
      changed(false);
      return resp(200, contractPayload());
    }
    if (key === "POST /api/contract:reset") {
      b.contractResets = (b.contractResets || 0) + 1;
      Object.assign(state.contract, { markdown: defaultContractMarkdown(), origin: "default", imported_at: null, broken: null });
      changed(false);
      return resp(200, contractPayload());
    }
    // ---- the rules (④)
    if (key === "GET /api/rules") return resp(200, { scenarios: Object.keys(state.rules).sort().map(rulesItem) });
    if ((m = key.match(/^GET \/api\/rules\/([^/?]+)(\?download=1)?$/))) {
      const sid = decode(m[1]);
      const sc = state.rules[sid];
      if (!sc) return notFound(`没有这个剧本：${sid.slice(0, 40)}`);
      if (m[2]) return resp(200, sc.markdown, "text/markdown; charset=utf-8", { "content-disposition": `attachment; filename="${sid}-${parseScenario(sc.markdown).fields.version}.scenario.md"` });
      return resp(200, { ...rulesItem(sid), markdown: sc.markdown });
    }
    if ((m = key.match(/^PUT \/api\/rules\/([^/?]+)$/))) {
      const sid = decode(m[1]);
      const sc = state.rules[sid];
      if (!sc) return notFound(`没有这个剧本：${sid.slice(0, 40)}`);
      b.rulesPuts = (b.rulesPuts || []).concat([{ sid, ...body }]);
      if (typeof (body || {}).markdown !== "string") return err(422, "markdown 必须是文字。");
      const parsed = parseScenario(body.markdown, `${sid}.md`);
      if (parsed.problems) return resp(400, { detail: parsed.problems[0], code: "invalid", problems: parsed.problems });
      if (parsed.fields.id !== sid) {
        const why = `剧本文件里的 id 是「${parsed.fields.id}」，与这个剧本的 id「${sid}」不同。`;
        return resp(400, { detail: why, code: "invalid", problems: [why] });
      }
      Object.assign(sc, { markdown: body.markdown, origin: "imported", imported_at: 1763900000, broken: null });
      changed(false);
      return resp(200, rulesItem(sid));
    }
    if ((m = key.match(/^POST \/api\/rules\/([^/?:]+):reset$/))) {
      const sid = decode(m[1]);
      if (!state.rules[sid]) return notFound(`没有这个剧本：${sid.slice(0, 40)}`);
      b.rulesResets = (b.rulesResets || []).concat([sid]);
      if (sid !== SCENARIO_ID) return err(400, "这个剧本没有默认版本，不能恢复默认。", "no_default");
      Object.assign(state.rules[sid], { markdown: scenarioMarkdown(), origin: "default", imported_at: null, broken: null });
      changed(false);
      return resp(200, rulesItem(sid));
    }
    if ((m = key.match(/^(GET|PUT|DELETE) \/api\/rules\/([^/?]+)\/materials\/([^/?]+)(\?download=1)?$/))) {
      const sid = decode(m[2]);
      const slug = decode(m[3]);
      const sc = state.rules[sid];
      if (!sc) return notFound(`没有这个剧本：${sid.slice(0, 40)}`);
      if (m[1] === "PUT") {
        b.materialPuts = (b.materialPuts || []).concat([{ sid, slug, ...body }]);
        if (!/^[a-z0-9][a-z0-9-]{0,31}$/.test(slug)) return err(400, "材料的标识只能用小写字母、数字和连字符（最多 32 个字符，以字母或数字开头）。");
        const text = String((body || {}).markdown || "");
        if (!text.trim()) return err(400, "材料内容不能为空。");
        if (Buffer.byteLength(text, "utf8") > 100 * 1024) return err(413, "材料太大了：单份最多 100 KB。", "too_large");
        if (!(slug in sc.materials) && Object.keys(sc.materials).length >= 20) return err(400, "每个剧本最多 20 份学习材料。", "too_many");
        const first = (text.split("\n").find((l) => l.trim()) || slug).replace(/^#+\s*/, "").trim();
        sc.materials[slug] = { title: String(body.title || first || slug).slice(0, 60), markdown: text };
        changed(false);
        return resp(200, rulesItem(sid));
      }
      const mat = sc.materials[slug];
      if (!mat) return notFound("没有这份学习材料。");
      if (m[1] === "DELETE") {
        b.materialDeletes = (b.materialDeletes || []).concat([{ sid, slug }]);
        delete sc.materials[slug];
        changed(false);
        return resp(200, rulesItem(sid));
      }
      if (m[4]) return resp(200, mat.markdown, "text/markdown; charset=utf-8", { "content-disposition": `attachment; filename="${sid}-${slug}.material.md"` });
      return resp(200, { slug, title: mat.title, markdown: mat.markdown, chars: len(mat.markdown), sha256: sha(mat.markdown) });
    }
    // ---- games (⑤)
    if ((m = key.match(/^GET \/api\/games\/([^/?]+)\/detail$/))) return gameDetail(decode(m[1]));
    if ((m = key.match(/^GET \/api\/games\/([^/?]+)\/export$/))) {
      const id = decode(m[1]);
      b.gameExports = (b.gameExports || []).concat([id]);
      const g = b.games.find((x) => x.session_id === id);
      if (!g && id !== b.session.session_id) return err(404, "对局不存在。", "not_found");
      if (id === b.session.session_id && (b.session.status === "running" || (b.session.takeover && b.session.takeover.seat))) {
        // (the file holds every identity: the same rule as the replay of the current game)
        return err(409, "对局进行中或正在接管一个座位，不能导出当前对局：请先暂停并退出接管。");
      }
      if (g && g.readable === false) return err(422, "这局的存档无法读取（格式太旧或已经损坏），不能导出。", "invalid_archive");
      return resp(200, "PK-fake-arenagame", "application/octet-stream", { "content-disposition": `attachment; filename="game-${id}-20251119.arenagame"` });
    }
    if ((m = key.match(/^POST \/api\/games\/import\?(.*)$/))) {
      const q = Object.fromEntries(m[1].split("&").map((kv) => kv.split("=").map(decode)));
      const out = importGame(q.name || "", Number((body && body.size) || 0), q.on_conflict || "ask");
      return resp(out.status, out.body);
    }
    return null;
  }

  function contractPayload() {
    const c = state.contract;
    const must = (c.markdown.split(/^##[ \t]+必读\s*$/m)[1] || "").split(/^##[ \t]+/m)[0].trim();
    return {
      markdown: c.markdown,
      source: { origin: c.origin, version: contractVersion(c.markdown), imported_at: c.imported_at, sha256: sha(c.markdown) },
      chars: len(c.markdown),
      must_read_chars: len(must),
      required_terms: [...TOOLS, ...CODES],
      problems: c.broken === "missing" ? ["契约是空的。"] : c.broken === "invalid" ? ["契约没有提到这些工具：jot、read_contract。每个工具的名字都要出现在契约里。"] : validateContract(c.markdown),
    };
  }

  function rulesItem(sid) {
    const sc = state.rules[sid];
    const parsed = parseScenario(sc.markdown, `${sid}.md`);
    const f = parsed.fields || {};
    return {
      id: sid,
      version: f.version || "",
      title: f.title || sid,
      origin: sc.origin,
      imported_at: sc.imported_at,
      sha256: sha(sc.markdown),
      chars: len(sc.markdown),
      materials: Object.entries(sc.materials)
        .sort(([a], [z]) => (a < z ? -1 : 1))
        .map(([slug, mat]) => ({ slug, title: mat.title, chars: len(mat.markdown), sha256: sha(mat.markdown) })),
      problem: (parsed.problems && parsed.problems[0]) || (sc.broken === "invalid" ? `剧本 ${sid}.md 无法加载：身份池共有 11 个身份，但对局有 12 名玩家，无法发牌。` : sc.broken === "missing" ? `读不到剧本 ${sid}：这份剧本不见了。` : null),
    };
  }

  const ROLE_OF_SEAT = { N1: "平民", N2: "平民", N3: "狼人", N4: "预言家", N5: "女巫", N6: "狼人", N7: "猎人", N8: "守卫", N9: "平民", N10: "狼人", N11: "平民", N12: "白狼王" };

  function gameDetail(id) {
    const live = b.session;
    const g = id === live.session_id ? null : b.games.find((x) => x.session_id === id);
    const bot_id_of = (i) => (g ? F.botId(i + 1) : (live.participants[i] || {}).bot_id || "");
    if (id !== live.session_id && !g) return err(404, "对局不存在。", "not_found");
    if (g && g.readable === false) return err(422, "无法读取这局的存档：存档文件已损坏，或者格式太旧（v0.12.1 及更早）。");
    const finished = (g ? g.status : live.status) === "finished";
    const supported = !g || g.schema_version !== 5;
    const reviewed = g ? Number((g.review || {}).done || 0) : (live.review.done || []).length;
    const rows = (g ? (g.participants_brief || F.NAMES) : live.participants.map((p) => p.name)).map((name, i) => {
      const seat = `N${i + 1}`;
      const role = finished ? ROLE_OF_SEAT[seat] : null;
      const done = g ? i < reviewed : (live.review.done || []).includes(seat);
      return {
        seat,
        bot_id: !supported ? "" : g ? F.botId(i + 1) : (live.participants[i] || {}).bot_id || "",
        name,
        avatar: F.avatarOf(i + 1),
        role,
        faction: role ? (["狼人", "白狼王"].includes(role) ? "狼人阵营" : "好人阵营") : null,
        // (the real server: done when it is written, na for a v5 game / a seat without a bot, else pending - also while the game is not over)
        review: done ? "done" : !supported || !bot_id_of(i) ? "na" : "pending",
      };
    });
    const humans = g ? (g.human_involved ? ["N3"] : []) : [];
    return resp(200, {
      participants: rows,
      files: { game_bytes: g ? g.size_bytes : 12000, prev_bytes: finished ? 0 : (g ? g.size_bytes : 12000) / 2, log_bytes: g ? g.log_bytes || 0 : 40000 },
      human_spans: humans.map((seat) => ({ seat, from_seq: 40, to_seq: 218 })),
    });
  }

  /** DELETE /api/games/{id}: the real server's answer (a fixed shape; the current game unless it runs; a game that is "in use" says so). */
  function deleteGame(id) {
    const NOTE = "bot 已经通过复盘形成的记忆不会被删除，也无法通过删除对局撤销。";
    b.gameDeletes = (b.gameDeletes || []).concat([id]);
    const locked = (b.gameLocks || []).includes(id);
    if (id === b.session.session_id) {
      if (b.session.status === "running") return resp(409, { detail: "进行中的对局不能删除，请先暂停。" });
      b.games = b.games.filter((g) => g.session_id !== id);
      const fresh = b.newSessionId || "s-new";
      if (locked) return resp(409, { detail: "这局的文件现在被占用，暂时删不掉；已经新建了一局空白对局。", code: "in_use", new_game: true, new_session_id: fresh });
      return resp(200, { ok: true, session_id: id, note: NOTE, new_game: true, new_session_id: fresh });
    }
    if (!b.games.some((g) => g.session_id === id)) return err(404, "对局不存在。", "not_found");
    if (locked) return resp(409, { detail: "这局的文件现在被占用，暂时删不掉：请稍后再试。", code: "in_use", new_game: false, new_session_id: null });
    b.games = b.games.filter((g) => g.session_id !== id);
    return resp(200, { ok: true, session_id: id, note: NOTE, new_game: false, new_session_id: null });
  }

  /**
   * POST /api/games/import: the NAME of the file picks what happens ("bad": 422 invalid_archive, "busy": 409 busy, "big": 413 too_large); the id of the
   * game is the one in the name (`game-<id>-....arenagame`), an id that exists (the current game's too) answers 409 `exists` for `ask`.  {status, body}.
   */
  function importGame(name, size, on) {
    b.gameImports = (b.gameImports || []).concat([{ name, size, on_conflict: on }]);
    const lowered = name.toLowerCase();
    if (!["ask", "overwrite", "copy"].includes(on)) return { status: 400, body: { detail: "on_conflict 必须是 ask / overwrite / copy 之一。", code: "bad_request" } };
    if (lowered.includes("busy")) return { status: 409, body: { detail: "另一个导入 / 导出任务正在进行：请等它结束再导入对局。", code: "busy" } };
    if (lowered.includes("big")) return { status: 413, body: { detail: ".arenagame 文件超过了 2 GiB 的上限。", code: "too_large" } };
    if (lowered.includes("bad")) return { status: 422, body: { detail: "这不是有效的 .arenagame 文件（不是 zip，或者文件已损坏）。", code: "invalid_archive" } };
    const id = (name.match(/^game-([A-Za-z0-9_-]+?)(?:-\d{8})?\.arenagame$/) || name.match(/^game-([A-Za-z0-9_-]+)/) || [])[1] || "imported-1";
    const exists = id === b.session.session_id || b.games.some((g) => g.session_id === id);
    if (exists && on === "overwrite" && id === b.session.session_id) return { status: 409, body: { detail: "这是当前对局，不能被覆盖：请作为副本导入。", code: "current" } };
    if (exists && on === "ask") return { status: 409, body: { detail: `已经有一局 id 相同的对局（${id}）：可以覆盖它，或者作为副本导入。`, code: "exists", session_id: id } };
    let sid = id;
    let replaced = false;
    let copied = false;
    if (exists && on === "copy") {
      sid = `${id}-copy1a2b`;
      copied = true;
    } else if (exists) {
      b.games = b.games.filter((g) => g.session_id !== id);
      replaced = true;
    }
    b.games.push({ session_id: sid, title: "Arena-Agents", scenario_title: "导入的对局", status: "finished", winner: "好人阵营", created_at: "2026-11-18T09:00:00Z", updated_at: "2026-11-18T10:00:00Z", duration_gt: 900, players: 12, tokens: 1000, cost_usd: 0.01, human_involved: false, schema_version: 6, size_bytes: size || 5000, log_bytes: 1000, readable: true, review: { done: 0, total: null }, participants_brief: F.NAMES.slice() });
    return { status: 200, body: { session_id: sid, status: "finished", replaced, copied } };
  }

  /**
   * A .arenabot over a bot: what the fake does is picked by the NAME of the file ("big": 413, "taken": 409 name_taken, "bad": 422 with problems);
   * otherwise the bot is replaced by `file.bot` ({name, avatar, core}) when the test gave one.  Answers {status, body}.
   */
  function importBot(botId, name, size, file) {
    b.botImports = (b.botImports || []).concat([{ id: botId, name, size }]);
    const bot = bots.byId[botId];
    const fail = (status, detailText, code, extra) => ({ status, body: { detail: detailText, ...(code ? { code } : {}), ...(extra || {}) } });
    if (!bot) return fail(404, "没有这个玩家（bot）。", "not_found");
    if (!idle()) return fail(409, "导入（会换掉名字和头像）只能在对局开始之前修改；请先新建对局。", "game_active");
    if (runningIds().includes(bot.id)) return fail(409, "正在初始化，请等它结束（或先取消）后再修改。", "init_busy");
    const lowered = name.toLowerCase();
    if (lowered.includes("big")) return fail(413, ".arenabot 文件超过了 20 MiB 的上限。", "too_large");
    if (lowered.includes("taken")) return fail(409, "名字「Bob」已被 N2 使用，名字不能重复。", "name_taken");
    if (lowered.includes("bad")) {
      const problems = ["缺少 bot.json，或者它不是有效的 JSON。", "core/voice.md：「说话方式」最多 600 字（现在 612 字）。"];
      return fail(422, `这个 bot 文件不能导入：共 ${problems.length} 处问题：${problems.join(" ")}`, undefined, { problems });
    }
    const spec = (file && file.bot) || {};
    Object.assign(bot, { name: spec.name || "Imported", origin: "import", core: spec.core || bot.core, staging: [], init: { status: "uninitialized", at: null, model: null, duration_s: null, error: null, scenario_id: null, hashes: null } });
    if (spec.avatar) bot.avatar = spec.avatar;
    changed(true);
    return { status: 200, body: detail(bot) };
  }

  // ================================================================================================ uploads (XHR)
  /** The XHR uploads of this part: a .arenabot over a bot, an .arenagame.  The NAME of the file picks what happens (like the Python mock). */
  function upload(xhr) {
    const url = String(xhr.url).replace(/^\/arena/, "");
    const size = Number((xhr.body && xhr.body.size) || 0);
    const name = decode((url.match(/[?&]name=([^&]*)/) || [])[1] || "");
    let m;
    if ((m = url.match(/^\/api\/bots\/([^/?]+)\/import(?:\?.*)?$/))) {
      xhr.progress(Math.floor(size / 2), size);
      xhr.progress(size, size);
      xhr.uploaded();
      const out = importBot(decode(m[1]), name, size, xhr.body);
      return xhr.respond(out.status, out.body) || true;
    }
    if (url.startsWith("/api/games/import")) {
      const on = (url.match(/[?&]on_conflict=([^&]*)/) || [])[1] || "ask";
      xhr.progress(size, size);
      xhr.uploaded();
      const out = importGame(name, size, on);
      return xhr.respond(out.status, out.body) || true;
    }
    return false;
  }

  api.deleteGame = deleteGame;
  return { api, handle, upload, state, notReadyText, deleteGame };
}

module.exports = { makeBotsFake, sha };
