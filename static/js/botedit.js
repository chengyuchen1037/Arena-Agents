/* ==========================================================================
   Arena-Agents v0.14 — the editors of the bot panel (H30, H37–H41; docs/v0.12-contracts.md §13.7 / §13.10)
   Loaded after botlib.js and before bots.js.  Exposes window.ArenaBotEditors.create(host) (and module.exports).

   Four sections in the lower half of the player detail panel:
     底层设定   the L0: 身份 / 说话方式 / 分析方式 + up to 3 附加 sections, a counter on each, "✨ 用主持人模型完善" (the draft is read first,
                only "采用并保存" saves it: E16), save (a bot that is initialized gets the warning of H30), 导出 / 导入 .md, 清除
     记忆       操作笔记 / 规则要点 / 打法 / 待解问题 / 印象 (the user's edits take effect at once, C9) + the record; clear a part, clear all
     待确认     the L0 proposals a review made: old -> new, 采纳 / 拒绝
     导入 / 导出  .arenabot out / in, the default library

   Server API (§13.7):
     PUT api/bots/{id}/core {markdown}   DELETE api/bots/{id}/core   POST api/bots/{id}/core:refine {text} -> {draft, problems?}
     GET api/bots/{id}/core?download=1   PUT api/bots/{id}/memory/{part} {content, scenario_id?}   DELETE api/bots/{id}/memory?part=
     POST api/bots/{id}/staging/{pid}:accept | :reject     GET api/bots/{id}/export     POST api/bots/{id}/import?name= (body = the file)
     POST api/bots/{id}/import-default {name}

   host (bots.js): { api, Core, Lib, doc, win, showToast, openDialog, limits(), session(), isIdle(), isReplay(), openBotId(), summaryOf(botId),
                     detailOf(botId), setDetail(botId, detail), repaint(), refreshBots(), download(path), openFilePicker(kind), defaults() }
   ========================================================================== */

(function (root) {
  "use strict";

  const Lib = root.ArenaBotLib || (typeof require === "function" ? require("./botlib.js") : null);

  const KIND_LABELS = { core: "底层设定", memory: "记忆", staging: "待确认", transfer: "导入 / 导出" };
  const KINDS = ["core", "memory", "staging", "transfer"];
  const PART_HELP = {
    arch_notes: "它自己的操作笔记：怎么用工具、什么时候睡、怎么记事。是这个玩家自己的，不分剧本。",
    digest: "读完官方契约和规则之后，用自己的话写下的规则要点（本剧本）。",
    playbook: "打法，一行一条假设：- [把握:中|支持:0|反例:0|来源:复盘|#标签] 一句话。支持 / 反例由对局结果检验。（本剧本）",
    questions: "还没弄懂的问题（本剧本，可以留空）。",
    people: "对其他玩家的印象，一行一个：- 名字：一句话印象。是这个玩家自己的，不分剧本。",
  };
  const PART_ROWS = { arch_notes: 5, digest: 5, playbook: 7, questions: 3, people: 5 };
  /** The files a started game needs (an empty one is refused by the start check): clearing them makes the bot "not initialized" again. */
  const REQUIRED_PARTS = ["arch_notes", "digest", "playbook"];
  const MEMORY_FILE_LABELS = { arch_notes: "操作笔记", digest: "规则要点", playbook: "打法", questions: "待解问题", people: "印象", record: "战绩" };
  const CORE_PLACEHOLDERS = {
    identity: "一句话说清楚这个人是谁。例：打了二十多年线下局的老玩家，话不多，一开口就是结论。",
    voice: "怎么说话：语气、句子长短、口头禅、篇幅。例：你说话短，通常两三句就停。爱反问。",
    mind: "怎么想问题：先看什么、怎么取舍、被打脸怎么办。例：你先看发生了什么，再听谁说了什么。",
  };
  const NOTES_PLACEHOLDER = "写几个要点（自由文本），例如：爱抓票型矛盾；被怀疑时先摆时间线。主持人模型会把它整理成上面的格式。";
  const INIT_WARNING = "将变为需重新初始化，记忆保留。";

  const errText = (e) => (e && (e.detail || e.message)) || "操作失败";
  /** The first `n` characters of a text with an ellipsis (the list of the default library shows a short line of every bot). */
  const clip = (text, n) => {
    const chars = Array.from(String(text === undefined || text === null ? "" : text));
    return chars.length <= n ? chars.join("") : `${chars.slice(0, n).join("")}…`;
  };

  function create(host) {
    const { Core, doc, win } = host;
    const api = host.api;
    if (!Core || !Lib || !api) return null;
    const esc = Core.escapeHtml;
    const enc = encodeURIComponent;
    const $ = (id) => (doc && typeof doc.getElementById === "function" ? doc.getElementById(id) : null);
    const showToast = host.showToast || (() => {});
    const openDialog = host.openDialog;

    const es = {
      core: {}, // botId -> {draft: model | null (what was typed over the saved L0), notes, busy, problems: [], error}
      refine: {}, // botId -> {busy, draft, problems, error, gen}
      part: {}, // botId -> {part: typed text} (only the parts that were touched)
      partBusy: {}, // "botId:part" -> true
      partError: {}, // "botId:part" -> text
      stageBusy: {}, // "botId:pid" -> true
      stageError: {}, // "botId:pid" -> [problems]
      expanded: {}, // "botId:kind" -> true
      busy: {}, // botId -> "export" | "import" | "default" | "clear" (a transfer / clear in progress)
      pick: null, // {botId, kind: "arenabot" | "core"}: what the open file picker is for
      defaultPick: {}, // botId -> the name chosen in the default library's list
      transferNote: {}, // botId -> {kind: "ok" | "error", text, problems?}
      gen: 0,
    };

    const limits = () => host.limits();
    const botKey = (botId, kind) => `${botId}:${kind}`;

    // -----------------------------------------------------------------------------------------------------------------
    // shared pieces
    // -----------------------------------------------------------------------------------------------------------------

    const isOpen = (botId, kind) => Boolean(es.expanded[botKey(botId, kind)]);

    function expand(botId, kind, open = true) {
      es.expanded[botKey(botId, kind)] = open;
    }

    function setText(node, text, className) {
      if (!node) return;
      node.textContent = text;
      if (className !== undefined) node.className = className;
    }

    /** `bot` is being initialized right now: the server refuses every write to it (409 init_busy) - the buttons say so instead. */
    function initBusy(botId) {
      const sum = host.summaryOf(botId);
      return Boolean(sum && sum.init && sum.init.status === "initializing");
    }

    const busyNote = () => `<div class="form-hint error editor-busy">正在初始化这个玩家：现在不能修改它（可以先取消初始化）。</div>`;

    function frame(botId, kind, meta, bodyHtml) {
      const open = isOpen(botId, kind);
      return (
        `<section class="detail-editor${open ? " open" : ""}" data-kind="${kind}">` +
        `<button type="button" class="detail-editor-toggle" data-detail-action="toggle-editor" data-kind="${kind}" aria-expanded="${open}">` +
        `<span class="caret" aria-hidden="true">${open ? "▼" : "▶"}</span><span class="detail-editor-title">${KIND_LABELS[kind]}</span>` +
        `<span class="detail-editor-meta">${esc(meta)}</span></button>` +
        `<div class="detail-editor-body">${open ? bodyHtml : ""}</div></section>`
      );
    }

    const problemsHtml = (list, id, cls = "form-error") =>
      `<ul class="${cls} editor-problems" id="${id}"${list && list.length ? "" : " hidden"}>${(list || []).map((p) => `<li>${esc(p)}</li>`).join("")}</ul>`;

    // -----------------------------------------------------------------------------------------------------------------
    // 底层设定 (L0)
    // -----------------------------------------------------------------------------------------------------------------

    const coreSaved = (botId) => {
      const d = host.detailOf(botId);
      return Lib.coreModel(d && d.core ? d.core : null);
    };

    function coreState(botId) {
      return es.core[botId] || (es.core[botId] = { draft: null, notes: "", busy: false, problems: [], error: "" });
    }

    const coreCurrent = (botId) => {
      const c = es.core[botId];
      return c && c.draft ? c.draft : coreSaved(botId);
    };

    /** The model the user is typing into (a copy of the saved L0 the first time something is typed). */
    function coreDraft(botId) {
      const c = coreState(botId);
      if (!c.draft) c.draft = Lib.cloneCore(coreSaved(botId));
      return c.draft;
    }

    function coreMeta(sum) {
      if (!sum || !sum.core) return "";
      const missing = sum.core.missing || [];
      if (missing.length === 3 && !(sum.core.extras || []).length) return "还没有设定";
      if (missing.length) return `⚠ 缺少：${missing.map((k) => Lib.CORE_HEADINGS[k]).join("、")}`;
      const chars = sum.core.chars || {};
      const total = Lib.CORE_KEYS.reduce((n, k) => n + (Number(chars[k]) || 0), 0);
      const extras = (sum.core.extras || []).length;
      return `完整 · ${total} 字${extras ? ` · 附加 ${extras} 份` : ""}`;
    }

    const counterText = (n, limit) => `${n} / ${limit}`;
    const counterClass = (over) => `core-count${over ? " over" : ""}`;

    function coreFieldHtml(key, text, stat, rows) {
      return (
        `<div class="core-field" data-core="${key}">` +
        `<label class="mem-label" for="ta-core-${key}"><span>${esc(Lib.CORE_HEADINGS[key])}<span class="form-hint core-required">（必填）</span></span>` +
        `<span class="${counterClass(stat.over)}" id="core-count-${key}">${counterText(stat.n, stat.limit)}</span></label>` +
        `<textarea class="form-input detail-textarea" id="ta-core-${key}" data-core-field="${key}" rows="${rows}" placeholder="${esc(CORE_PLACEHOLDERS[key])}">${esc(text)}</textarea></div>`
      );
    }

    function coreExtraHtml(extra, index, stat) {
      return (
        `<div class="core-extra" data-extra="${index}">` +
        `<div class="core-extra-head"><span class="mem-label-text">附加：</span>` +
        `<input type="text" class="form-input core-extra-title" id="input-core-extra-title-${index}" data-core-extra-title="${index}" value="${esc(extra.title)}"` +
        ` placeholder="标题（最多 ${limits().extra_title_chars} 字）" aria-label="附加分区的标题" autocomplete="off" />` +
        `<span class="${counterClass(stat.over || stat.titleOver)}" id="core-extra-count-${index}">${counterText(stat.n, stat.limit)}</span>` +
        `<button type="button" class="btn btn-xs" data-detail-action="core-extra-del" data-index="${index}" title="删除这个附加分区">删除</button></div>` +
        `<textarea class="form-input detail-textarea" id="ta-core-extra-${index}" data-core-extra-text="${index}" rows="2" placeholder="内容（最多 ${limits().core_extra_chars} 字）">${esc(extra.text)}</textarea></div>`
      );
    }

    /** The first fault of the typed L0 that keeps it from being saved, else "" - and whether the save button is on. */
    function coreSaveState(botId) {
      const model = coreCurrent(botId);
      const stats = Lib.coreStats(model, limits());
      const dirty = Lib.coreDirty(model, coreSaved(botId));
      const c = es.core[botId] || {};
      const blocked = initBusy(botId) || Boolean(c.busy);
      return { model, stats, dirty, canSave: dirty && stats.complete && !blocked, blocked };
    }

    function coreProblemList(stats) {
      const list = stats.problems.slice();
      if (stats.missing.length) list.unshift(`还缺：${stats.missing.map((k) => Lib.CORE_HEADINGS[k]).join("、")}（三个都要写才能保存）。`);
      return list;
    }

    function refineBlockHtml(botId) {
      const rf = es.refine[botId];
      if (!rf || rf.draft === undefined || rf.draft === null || rf.draft === "") return "";
      const parsed = Lib.parseCoreMarkdown(rf.draft);
      const faults = parsed.ok ? Lib.coreProblems(parsed.core, limits(), { requireComplete: true }) : parsed.problems;
      const preview = parsed.core
        ? Lib.CORE_KEYS.map((k) => `<div class="draft-sec"><strong>${esc(Lib.CORE_HEADINGS[k])}</strong>：${esc(parsed.core[k]) || "（空）"}</div>`).join("") +
          parsed.core.extras.map((e) => `<div class="draft-sec"><strong>${esc(Lib.CORE_EXTRA_PREFIX + e.title)}</strong>：${esc(e.text) || "（空）"}</div>`).join("")
        : `<div class="detail-draft-text">${esc(rf.draft)}</div>`;
      const warn = [...(rf.problems || []), ...faults].filter((v, i, a) => a.indexOf(v) === i);
      return (
        `<div class="detail-draft" id="core-draft"><div class="detail-draft-title">主持人模型给出的草稿（尚未保存，请先看一遍）</div>` +
        `<div class="detail-draft-text draft-preview">${preview}</div>` +
        (warn.length ? `<ul class="form-error editor-problems" id="core-draft-problems">${warn.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : "") +
        `<div class="form-hint">采用后，上面的内容会换成这份草稿（没保存的修改也会被覆盖）。${warn.length ? "草稿有上面这些问题：可以先放进编辑框，改好再保存。" : ""}</div>` +
        `<div class="detail-editor-actions">` +
        `<button type="button" class="btn btn-xs btn-primary-outline" id="btn-core-draft-save" data-detail-action="core-draft-save"${parsed.ok && !faults.length && !host.isReplay() && !initBusy(botId) ? "" : " disabled"}>采用并保存</button>` +
        `<button type="button" class="btn btn-xs" id="btn-core-draft-edit" data-detail-action="core-draft-edit"${parsed.core ? "" : " disabled"}>放进编辑框修改</button>` +
        `<button type="button" class="btn btn-xs" data-detail-action="core-draft-discard">放弃草稿</button></div></div>`
      );
    }

    function coreHtml(botId) {
      const sum = host.summaryOf(botId);
      const st0 = coreSaveState(botId);
      const { model, stats } = st0;
      const c = es.core[botId] || {};
      const rf = es.refine[botId] || {};
      const L = limits();
      const reinit = sum && sum.init && sum.init.status === "ready";
      const fields = Lib.CORE_KEYS.map((k) => coreFieldHtml(k, model[k], stats.sections[k], k === "identity" ? 3 : 5)).join("");
      const extras = model.extras.map((e, i) => coreExtraHtml(e, i, stats.extras[i])).join("");
      const saved = coreSaved(botId);
      const hasSaved = Boolean(saved.identity || saved.voice || saved.mind || saved.extras.length);
      const list = coreProblemList(stats);
      return (
        `<div class="form-hint">三个分区必填，另外最多 ${L.core_extra_max} 份「附加」分区（口头禅、禁忌……）。上限：身份 ${L.core_limits.identity} 字、说话方式 ${L.core_limits.voice} 字、分析方式 ${L.core_limits.mind} 字，每份附加 ${L.core_extra_chars} 字。` +
        `${host.isIdle() ? "" : "　对局进行中改动的底层设定从下一局开始生效。"}</div>` +
        (initBusy(botId) ? busyNote() : "") +
        (reinit ? `<div class="form-hint core-warn" id="core-init-warn">⚠ 这个玩家已经初始化：保存修改后它会变为「需重新初始化」，学到的记忆保留。</div>` : "") +
        fields +
        `<div class="core-extras" id="core-extras">${extras}</div>` +
        `<div class="detail-editor-actions"><button type="button" class="btn btn-xs" id="btn-core-extra-add" data-detail-action="core-extra-add"` +
        `${model.extras.length >= L.core_extra_max ? " disabled" : ""}>＋ 添加附加分区</button>` +
        `<span class="form-hint">${model.extras.length} / ${L.core_extra_max}</span></div>` +
        `<ul class="form-error editor-problems" id="core-problems"${list.length && st0.dirty ? "" : " hidden"}>${st0.dirty ? list.map((p) => `<li>${esc(p)}</li>`).join("") : ""}</ul>` +
        problemsHtml(c.problems, "core-server-problems") +
        `<div class="form-error" id="core-error"${c.error ? "" : " hidden"}>${esc(c.error || "")}</div>` +
        `<div class="detail-editor-actions">` +
        `<button type="button" class="btn btn-xs btn-primary-outline" id="btn-core-save" data-detail-action="core-save"${st0.canSave ? "" : " disabled"}>${c.busy ? "保存中…" : "保存"}</button>` +
        `<button type="button" class="btn btn-xs" id="btn-core-revert" data-detail-action="core-revert"${st0.dirty ? "" : " disabled"}>还原</button>` +
        `<button type="button" class="btn btn-xs" id="btn-core-export" data-detail-action="core-export"${hasSaved ? "" : " disabled"} title="把底层设定存成一个 .md 文件（合并格式）">导出 .md</button>` +
        `<button type="button" class="btn btn-xs" id="btn-core-import" data-detail-action="core-import"${initBusy(botId) || host.isReplay() ? " disabled" : ""} title="用一个 .md 文件替换底层设定（合并格式）">导入 .md…</button>` +
        `<button type="button" class="btn btn-xs btn-danger-outline" id="btn-core-clear" data-detail-action="core-clear"${hasSaved && !initBusy(botId) ? "" : " disabled"}>清除</button></div>` +
        `<div class="core-refine"><label class="mem-label" for="ta-core-notes"><span>要点（交给主持人模型整理）</span></label>` +
        `<textarea class="form-input detail-textarea mem-notes" id="ta-core-notes" rows="3" placeholder="${esc(NOTES_PLACEHOLDER)}">${esc(c.notes || "")}</textarea>` +
        `<div class="detail-editor-actions"><button type="button" class="btn btn-xs" id="btn-core-refine" data-detail-action="core-refine"` +
        `${rf.busy || initBusy(botId) ? " disabled" : ""} title="把上面的要点（不写就用现在的设定）交给主持人绑定的模型，整理成合并格式的底层设定；结果先给你确认再保存">${rf.busy ? "完善中…" : "✨ 用主持人模型完善"}</button></div>` +
        `<div class="form-error" id="core-refine-error"${rf.error ? "" : " hidden"}>${esc(rf.error || "")}</div></div>` +
        refineBlockHtml(botId)
      );
    }

    /** Typing: the counters, the list of faults and the Save / Revert buttons follow the text without a repaint (the caret stays). */
    function updateCoreLive(botId) {
      const s = coreSaveState(botId);
      for (const k of Lib.CORE_KEYS) {
        const stat = s.stats.sections[k];
        setText($(`core-count-${k}`), counterText(stat.n, stat.limit), counterClass(stat.over));
      }
      s.stats.extras.forEach((stat, i) => setText($(`core-extra-count-${i}`), counterText(stat.n, stat.limit), counterClass(stat.over || stat.titleOver)));
      const list = coreProblemList(s.stats);
      const box = $("core-problems");
      if (box) {
        box.innerHTML = s.dirty ? list.map((p) => `<li>${esc(p)}</li>`).join("") : "";
        box.hidden = !(s.dirty && list.length);
      }
      const save = $("btn-core-save");
      if (save) save.disabled = !s.canSave;
      const revert = $("btn-core-revert");
      if (revert) revert.disabled = !s.dirty;
    }

    function onCoreInput(botId, t, value) {
      const field = t.getAttribute("data-core-field");
      if (field) {
        coreDraft(botId)[field] = value;
        updateCoreLive(botId);
        return true;
      }
      const text = t.getAttribute("data-core-extra-text");
      if (text !== null && text !== undefined) {
        const e = coreDraft(botId).extras[Number(text)];
        if (e) e.text = value;
        updateCoreLive(botId);
        return true;
      }
      const title = t.getAttribute("data-core-extra-title");
      if (title !== null && title !== undefined) {
        const e = coreDraft(botId).extras[Number(title)];
        if (e) e.title = value;
        updateCoreLive(botId);
        return true;
      }
      if (t.id === "ta-core-notes") {
        coreState(botId).notes = value;
        return true;
      }
      return false;
    }

    function addExtra(botId) {
      const d = coreDraft(botId);
      if (d.extras.length >= limits().core_extra_max) return;
      d.extras.push({ title: "", text: "", slug: "" });
      host.repaint();
    }

    function delExtra(botId, index) {
      const d = coreDraft(botId);
      if (index >= 0 && index < d.extras.length) d.extras.splice(index, 1);
      host.repaint();
    }

    /** Two L0 models with the same texts (the way the server compares them: stripped). */
    function sameCore(a, b) {
      return !Lib.coreDirty(a, b) && !Lib.coreDirty(b, a);
    }

    function revertCore(botId) {
      const c = coreState(botId);
      c.draft = null;
      c.problems = [];
      c.error = "";
      host.repaint();
    }

    /** The warning of H30: a bot that is initialized becomes "需重新初始化" when its L0 changes (its memory stays).  true = go on. */
    async function confirmReinit(botId, what) {
      const sum = host.summaryOf(botId);
      if (!sum || !sum.init || sum.init.status !== "ready") return true;
      const choice = await openDialog({
        title: what,
        bodyHtml: `<div>${esc(INIT_WARNING)}</div><div class="form-hint">${esc(sum.name)} 已经初始化。底层设定一变，要再初始化一次才能开局（重新初始化会修订它的记忆文件，不会清空它们）。</div>`,
        okText: "继续",
      });
      return Boolean(choice && choice.ok);
    }

    /** PUT core.  `markdown`: the text to save (default: the typed L0, rendered).  Resolves whether it was saved. */
    async function saveCore(botId, markdown, opts = {}) {
      const c = coreState(botId);
      if (c.busy) return false;
      const sum = host.summaryOf(botId);
      let text = markdown;
      if (text === undefined) {
        const s = coreSaveState(botId);
        if (!s.dirty) return false;
        if (!s.stats.complete) {
          c.error = coreProblemList(s.stats)[0] || "底层设定还不完整。";
          host.repaint();
          return false;
        }
        text = Lib.renderCoreMarkdown(s.model, sum ? sum.name : "");
      }
      if (!opts.skipConfirm && !(await confirmReinit(botId, "保存底层设定"))) return false;
      const sentDraft = c.draft ? Lib.cloneCore(c.draft) : null; // what the boxes held when Save was pressed
      c.busy = true;
      c.error = "";
      c.problems = [];
      host.repaint();
      try {
        const detail = await api.put(`api/bots/${enc(botId)}/core`, { markdown: text });
        // what was typed while the request was on its way is a LATER edit: it stays (an unsaved difference to the L0 saved now)
        if (!c.draft || !sentDraft || sameCore(c.draft, sentDraft)) c.draft = null;
        if (es.refine[botId]) es.refine[botId] = {};
        host.setDetail(botId, detail);
        showToast(`底层设定已保存${sum ? `：${sum.name}` : ""}`);
        for (const note of (detail && detail.notices) || []) showToast(note);
        return true;
      } catch (e) {
        c.problems = Array.isArray(e && e.problems) ? e.problems : [];
        c.error = c.problems.length ? "" : errText(e);
        if (c.problems.length) showToast(errText(e));
        return false;
      } finally {
        c.busy = false;
        host.repaint(true);
      }
    }

    async function clearCore(botId) {
      const sum = host.summaryOf(botId);
      const choice = await openDialog({
        title: "清除底层设定",
        bodyHtml: `<div>将清空 <strong>${esc(sum ? sum.name : "这个玩家")}</strong> 的底层设定。清除后需要重新生成，否则无法开始对局；已有的初始化会变为需重新初始化。</div>`,
        okText: "确认清除",
        danger: true,
      });
      if (!choice || !choice.ok) return false;
      try {
        const detail = await api.del(`api/bots/${enc(botId)}/core`);
        const c = coreState(botId);
        c.draft = null;
        c.problems = [];
        c.error = "";
        host.setDetail(botId, detail);
        showToast("底层设定已清除");
        return true;
      } catch (e) {
        showToast(`清除失败：${errText(e)}`);
        return false;
      } finally {
        host.repaint(true);
      }
    }

    // ---- "✨ 用主持人模型完善": the draft is shown first (E16) ---------------------------------------------------------------

    async function refineCore(botId) {
      const rf = es.refine[botId] || (es.refine[botId] = {});
      if (rf.busy) return;
      const c = coreState(botId);
      const sum = host.summaryOf(botId);
      let text = String(c.notes || "").trim();
      if (!text) {
        const cur = coreCurrent(botId);
        if (cur.identity || cur.voice || cur.mind || cur.extras.length) text = Lib.renderCoreMarkdown(cur, sum ? sum.name : "");
      }
      if (!text) {
        rf.error = "请先在上面写几个要点，或者先写一点底层设定，再让主持人模型完善。";
        host.repaint();
        return;
      }
      const gen = ++es.gen;
      rf.busy = true;
      rf.gen = gen;
      rf.error = "";
      host.repaint();
      try {
        const res = await api.post(`api/bots/${enc(botId)}/core:refine`, { text });
        if (rf.gen !== gen) return; // (a newer request, or the draft was thrown away meanwhile)
        rf.draft = res && typeof res.draft === "string" ? res.draft : "";
        rf.problems = res && Array.isArray(res.problems) ? res.problems : [];
        if (!rf.draft) rf.error = "主持人模型没有给出草稿。";
      } catch (e) {
        if (rf.gen !== gen) return;
        rf.draft = "";
        rf.error = errText(e);
      } finally {
        if (rf.gen === gen) rf.busy = false;
        host.repaint(true);
      }
    }

    async function saveDraft(botId) {
      const rf = es.refine[botId];
      if (!rf || !rf.draft) return;
      const parsed = Lib.parseCoreMarkdown(rf.draft);
      if (!parsed.ok) return;
      // (the draft text is what is saved: the server writes the canonical file)
      await saveCore(botId, rf.draft);
    }

    function draftToEditor(botId) {
      const rf = es.refine[botId];
      const parsed = rf && rf.draft ? Lib.parseCoreMarkdown(rf.draft) : null;
      if (!parsed || !parsed.core) return;
      const c = coreState(botId);
      c.draft = { identity: parsed.core.identity, voice: parsed.core.voice, mind: parsed.core.mind, extras: parsed.core.extras.map((e) => ({ title: e.title, text: e.text, slug: "" })) };
      c.problems = [];
      c.error = "";
      es.refine[botId] = {};
      expand(botId, "core", true);
      host.repaint();
    }

    function discardDraft(botId) {
      es.refine[botId] = {}; // (a refine that is still on its way finds its generation gone: its answer is dropped)
      host.repaint();
    }

    // ---- L0 as a .md file ----------------------------------------------------------------------------------------------

    function exportCore(botId) {
      host.download(`api/bots/${enc(botId)}/core?download=1`);
    }

    function chooseCoreFile(botId) {
      es.pick = { botId, kind: "core" };
      host.openFilePicker("core");
    }

    async function readText(file) {
      if (file && typeof file.text === "function") return file.text();
      return new Promise((resolve, reject) => {
        const FR = win && win.FileReader;
        if (typeof FR !== "function") {
          reject(new Error("这个浏览器不能读取文件。"));
          return;
        }
        const r = new FR();
        r.onload = () => resolve(String(r.result || ""));
        r.onerror = () => reject(new Error("读取文件失败。"));
        r.readAsText(file, "utf-8");
      });
    }

    async function importCoreFile(botId, file) {
      const sum = host.summaryOf(botId);
      const c = coreState(botId);
      c.error = "";
      c.problems = [];
      let text;
      try {
        text = await readText(file);
      } catch (e) {
        c.error = errText(e);
        host.repaint();
        return false;
      }
      const name = String((file && file.name) || "文件");
      const parsed = Lib.parseCoreMarkdown(text);
      if (!parsed.ok && !parsed.core) {
        c.problems = parsed.problems;
        c.error = `「${name}」不是合法的底层设定文件。`;
        expand(botId, "core", true);
        host.repaint();
        return false;
      }
      const choice = await openDialog({
        title: "导入底层设定",
        bodyHtml:
          `<div>将用文件 <strong>${esc(name)}</strong> 里的内容替换 <strong>${esc(sum ? sum.name : "这个玩家")}</strong> 的底层设定（${Lib.textLength(text)} 字）。名字、头像和记忆不变。</div>` +
          (sum && sum.init && sum.init.status === "ready" ? `<div class="form-hint">${esc(INIT_WARNING)}</div>` : ""),
        okText: "导入并保存",
      });
      if (!choice || !choice.ok) return false;
      expand(botId, "core", true);
      return saveCore(botId, text, { skipConfirm: true });
    }

    // -----------------------------------------------------------------------------------------------------------------
    // 记忆
    // -----------------------------------------------------------------------------------------------------------------

    const SCENARIO_NOTE = (d) => (d && d.memory && d.memory.scenario_id ? `剧本：${d.memory.scenario_id}` : "本剧本");

    /** The text of a part as the file holds it (the canonical text the server sends). */
    function partSaved(botId, part) {
      const d = host.detailOf(botId);
      const m = d && d.memory ? d.memory : {};
      if (part === "playbook") return String(m.playbook_md || "");
      if (part === "people") return String(m.people_md || "");
      return String(m[part] || "");
    }

    const partCurrent = (botId, part) => {
      const t = es.part[botId];
      return t && t[part] !== undefined ? t[part] : partSaved(botId, part);
    };

    const partDirty = (botId, part) => partCurrent(botId, part).trim() !== partSaved(botId, part).trim();

    function partStatus(botId, part) {
      const text = partCurrent(botId, part);
      const count = Lib.countPart(part, text, limits());
      const faults = Lib.partProblems(part, text, limits());
      const dirty = partDirty(botId, part);
      const blocked = initBusy(botId) || Boolean(es.partBusy[botKey(botId, part)]) || host.isReplay();
      return { count, faults, dirty, over: count.n > count.limit, canSave: dirty && !faults.length && !blocked, blocked };
    }

    const countText = (count) => `${count.n} / ${count.limit} ${count.unit}`;

    function memoryMeta(sum) {
      if (!sum || !sum.memory_stats) return "";
      const s = sum.memory_stats;
      const bits = [];
      if (s.playbook) bits.push(`打法 ${s.playbook}`);
      if (s.people) bits.push(`印象 ${s.people}`);
      if (s.arch_notes || s.digest) bits.push("有笔记");
      if (s.record) bits.push(s.record);
      return bits.length ? bits.join(" · ") : "空";
    }

    function playbookPreviewHtml(botId) {
      const d = host.detailOf(botId);
      const entries = d && d.memory && Array.isArray(d.memory.playbook) ? d.memory.playbook : [];
      if (!entries.length) return "";
      return (
        `<ul class="mem-playbook" id="mem-playbook-preview">` +
        entries
          .map(
            (e) =>
              `<li class="pb-entry"><span class="pb-conf pb-conf-${esc(e.confidence || "低")}" title="把握">${esc(e.confidence || "低")}</span>` +
              `<span class="pb-stats" title="支持 / 反例">+${Core.numText(e.support || 0)} / −${Core.numText(e.counter || 0)}</span>` +
              `${(e.tags || []).map((t) => `<span class="pb-tag">#${esc(t)}</span>`).join("")}<span class="pb-text">${esc(e.text)}</span></li>`
          )
          .join("") +
        `</ul>`
      );
    }

    function partBlockHtml(botId, part) {
      const L = limits();
      const label = L.part_labels[part] || part;
      const text = partCurrent(botId, part);
      const s = partStatus(botId, part);
      const scenarioPart = Lib.SCENARIO_PARTS.includes(part);
      const key = botKey(botId, part);
      const err = es.partError[key] || "";
      const saved = partSaved(botId, part);
      return (
        `<div class="mem-block" data-part="${part}">` +
        `<label class="mem-label" for="ta-part-${part}"><span>${esc(label)}${scenarioPart ? `<span class="form-hint mem-scope">（${esc(SCENARIO_NOTE(host.detailOf(botId)))}）</span>` : ""}</span>` +
        `<span class="${counterClass(s.over)}" id="part-count-${part}">${esc(countText(s.count))}</span></label>` +
        `<div class="form-hint">${esc(PART_HELP[part] || "")}</div>` +
        (part === "playbook" ? playbookPreviewHtml(botId) : "") +
        `<textarea class="form-input detail-textarea mem-input" id="ta-part-${part}" data-part-text="${part}" rows="${PART_ROWS[part] || 4}"${host.isReplay() ? " readonly" : ""}>${esc(text)}</textarea>` +
        `<ul class="form-error editor-problems" id="part-problems-${part}"${s.dirty && s.faults.length ? "" : " hidden"}>${s.dirty ? s.faults.map((p) => `<li>${esc(p)}</li>`).join("") : ""}</ul>` +
        `<div class="form-error" id="part-error-${part}"${err ? "" : " hidden"}>${esc(err)}</div>` +
        `<div class="detail-editor-actions">` +
        `<button type="button" class="btn btn-xs btn-primary-outline" id="btn-part-save-${part}" data-detail-action="part-save" data-part="${part}"${s.canSave ? "" : " disabled"}>${es.partBusy[key] ? "保存中…" : "保存"}</button>` +
        `<button type="button" class="btn btn-xs" id="btn-part-revert-${part}" data-detail-action="part-revert" data-part="${part}"${s.dirty ? "" : " disabled"}>还原</button>` +
        `<button type="button" class="btn btn-xs btn-danger-outline" id="btn-part-clear-${part}" data-detail-action="part-clear" data-part="${part}"${saved.trim() && !s.blocked ? "" : " disabled"}>清除</button></div></div>`
      );
    }

    function memoryHtml(botId) {
      const d = host.detailOf(botId);
      const parts = limits().memory_parts;
      const record = d && d.memory ? String(d.memory.record || "") : "";
      const sum = host.summaryOf(botId);
      return (
        `<div class="form-hint">它自己学到的东西。你的修改<strong>直接生效</strong>，不影响初始化状态；复盘写进来的内容也是直接生效，没有回滚——不满意就在这里改或清除。</div>` +
        (initBusy(botId) ? busyNote() : "") +
        parts.map((p) => partBlockHtml(botId, p)).join("") +
        `<div class="mem-block mem-record" data-part="record"><div class="mem-label"><span>战绩</span></div>` +
        `<div class="mem-record-text" id="mem-record">${record ? esc(record).replace(/\n/g, "<br>") : `<span class="form-hint">还没有战绩。</span>`}</div></div>` +
        `<div class="detail-editor-actions"><button type="button" class="btn btn-xs btn-danger-outline" id="btn-memory-clear-all" data-detail-action="memory-clear-all"` +
        `${initBusy(botId) || host.isReplay() || !sum ? " disabled" : ""}>清除 ${esc(sum ? sum.name : "这个玩家")} 的全部记忆…</button></div>`
      );
    }

    function updatePartLive(botId, part) {
      const s = partStatus(botId, part);
      setText($(`part-count-${part}`), countText(s.count), counterClass(s.over));
      const box = $(`part-problems-${part}`);
      if (box) {
        box.innerHTML = s.dirty ? s.faults.map((p) => `<li>${esc(p)}</li>`).join("") : "";
        box.hidden = !(s.dirty && s.faults.length);
      }
      const save = $(`btn-part-save-${part}`);
      if (save) save.disabled = !s.canSave;
      const revert = $(`btn-part-revert-${part}`);
      if (revert) revert.disabled = !s.dirty;
    }

    function onPartInput(botId, t, value) {
      const part = t.getAttribute("data-part-text");
      if (!part) return false;
      (es.part[botId] || (es.part[botId] = {}))[part] = value;
      delete es.partError[botKey(botId, part)];
      updatePartLive(botId, part);
      return true;
    }

    async function savePart(botId, part) {
      const key = botKey(botId, part);
      if (es.partBusy[key]) return false;
      const status = partStatus(botId, part);
      if (!status.dirty) return false;
      if (status.faults.length) {
        es.partError[key] = status.faults[0];
        host.repaint();
        return false;
      }
      const text = partCurrent(botId, part);
      const sum = host.summaryOf(botId);
      if (!text.trim() && REQUIRED_PARTS.includes(part) && sum && sum.init && sum.init.status === "ready") {
        const choice = await openDialog({
          title: `清空${limits().part_labels[part]}`,
          bodyHtml: `<div>「${esc(limits().part_labels[part])}」是开局必需的文件：清空以后，${esc(sum.name)} 要重新初始化才能开局。</div>`,
          okText: "清空并保存",
          danger: true,
        });
        if (!choice || !choice.ok) return false;
      }
      es.partBusy[key] = true;
      delete es.partError[key];
      host.repaint();
      try {
        const body = { content: text };
        if (Lib.SCENARIO_PARTS.includes(part)) {
          const d = host.detailOf(botId);
          if (d && d.memory && d.memory.scenario_id) body.scenario_id = d.memory.scenario_id;
        }
        const detail = await api.put(`api/bots/${enc(botId)}/memory/${enc(part)}`, body);
        // (typed while the request was on its way = a later edit: it stays, an unsaved difference to the text saved now)
        if (es.part[botId] && es.part[botId][part] === text) delete es.part[botId][part];
        host.setDetail(botId, detail);
        showToast(`${limits().part_labels[part]}已保存`);
        return true;
      } catch (e) {
        const problems = Array.isArray(e && e.problems) ? e.problems : [];
        es.partError[key] = problems.length ? problems.join(" ") : errText(e);
        return false;
      } finally {
        delete es.partBusy[key];
        host.repaint(true);
      }
    }

    function revertPart(botId, part) {
      if (es.part[botId]) delete es.part[botId][part];
      delete es.partError[botKey(botId, part)];
      host.repaint();
    }

    async function clearPart(botId, part) {
      const sum = host.summaryOf(botId);
      const label = limits().part_labels[part] || part;
      const choice = await openDialog({
        title: `清除${label}`,
        bodyHtml:
          `<div>将清空 <strong>${esc(sum ? sum.name : "这个玩家")}</strong> 的「${esc(label)}」。不能撤销。</div>` +
          (REQUIRED_PARTS.includes(part) && sum && sum.init && sum.init.status === "ready" ? `<div class="form-hint">这是开局必需的文件：清除后要重新初始化才能开局。</div>` : ""),
        okText: "确认清除",
        danger: true,
      });
      if (!choice || !choice.ok) return false;
      try {
        const detail = await api.del(`api/bots/${enc(botId)}/memory?part=${enc(part)}`);
        if (es.part[botId]) delete es.part[botId][part];
        host.setDetail(botId, detail);
        showToast(`${label}已清除`);
        return true;
      } catch (e) {
        showToast(`清除失败：${errText(e)}`);
        return false;
      } finally {
        host.repaint(true);
      }
    }

    async function clearAllMemory(botId) {
      const sum = host.summaryOf(botId);
      const choice = await openDialog({
        title: "清除全部记忆",
        bodyHtml:
          `<div>将清空 <strong>${esc(sum ? sum.name : "这个玩家")}</strong> 学到的全部记忆：${["arch_notes", "digest", "playbook", "questions", "people", "record"].map((k) => MEMORY_FILE_LABELS[k]).join("、")}。` +
          `初始化结果一并清空（变为未初始化），底层设定保留。此操作不能撤销，建议先导出 .arenabot。</div>`,
        okText: "确认清除",
        danger: true,
      });
      if (!choice || !choice.ok) return false;
      try {
        const detail = await api.del(`api/bots/${enc(botId)}/memory`);
        es.part[botId] = {};
        host.setDetail(botId, detail);
        showToast("已清除全部记忆");
        return true;
      } catch (e) {
        showToast(`清除失败：${errText(e)}`);
        return false;
      } finally {
        host.repaint(true);
      }
    }

    // -----------------------------------------------------------------------------------------------------------------
    // 待确认: the L0 proposals of a review
    // -----------------------------------------------------------------------------------------------------------------

    function stagingList(botId) {
      const d = host.detailOf(botId);
      return d && Array.isArray(d.staging) ? d.staging : [];
    }

    function diffHtml(before, after) {
      return Lib.lineDiff(before, after)
        .map((l) => `<div class="diff-line ${l.kind}"><span class="diff-mark">${l.kind === "add" ? "+" : l.kind === "del" ? "−" : " "}</span><span class="diff-text">${esc(l.text) || "&nbsp;"}</span></div>`)
        .join("");
    }

    function stagingHtml(botId) {
      const d = host.detailOf(botId);
      const list = stagingList(botId);
      if (!list.length) return `<div class="form-hint">没有待确认的提案。</div>`;
      const extras = d && d.core && Array.isArray(d.core.extras) ? d.core.extras : [];
      return (
        `<div class="form-hint">复盘时 bot 提出了对底层设定的修改（比如从接管者身上学到的说话方式）。<strong>采纳</strong>后才会写进它的底层设定；拒绝就丢掉。</div>` +
        list
          .map((p) => {
            const key = botKey(botId, p.id);
            const busy = Boolean(es.stageBusy[key]);
            const faults = es.stageError[key] || [];
            return (
              `<div class="stage-item" data-proposal="${esc(p.id)}">` +
              `<div class="stage-head"><strong>${esc(Lib.stagingLabel(p.section, extras))}</strong>` +
              `<span class="form-hint">${esc([p.source, Lib.formatUnix(p.created_at)].filter(Boolean).join(" · "))}</span></div>` +
              (p.reason ? `<div class="stage-reason">理由：${esc(p.reason)}</div>` : "") +
              `<div class="stage-diff" aria-label="修改前后对比">${diffHtml(p.before, p.after)}</div>` +
              (faults.length ? `<ul class="form-error editor-problems">${faults.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "") +
              `<div class="detail-editor-actions">` +
              `<button type="button" class="btn btn-xs btn-primary-outline" data-detail-action="stage-accept" data-pid="${esc(p.id)}"${busy || initBusy(botId) || host.isReplay() ? " disabled" : ""}>采纳</button>` +
              `<button type="button" class="btn btn-xs" data-detail-action="stage-reject" data-pid="${esc(p.id)}"${busy || host.isReplay() ? " disabled" : ""}>拒绝</button></div></div>`
            );
          })
          .join("")
      );
    }

    async function decideProposal(botId, pid, accept) {
      const key = botKey(botId, pid);
      if (es.stageBusy[key]) return false;
      const sum = host.summaryOf(botId);
      if (accept && !(await confirmReinit(botId, "采纳提案"))) return false;
      es.stageBusy[key] = true;
      delete es.stageError[key];
      host.repaint();
      try {
        const detail = await api.post(`api/bots/${enc(botId)}/staging/${enc(pid)}:${accept ? "accept" : "reject"}`, {});
        host.setDetail(botId, detail);
        if (accept && es.core[botId]) es.core[botId].draft = null;
        showToast(accept ? `已采纳，写进了${sum ? ` ${sum.name} 的` : ""}底层设定` : "已拒绝这条提案");
        return true;
      } catch (e) {
        const problems = Array.isArray(e && e.problems) ? e.problems : [];
        es.stageError[key] = problems.length ? problems : [errText(e)];
        return false;
      } finally {
        delete es.stageBusy[key];
        host.repaint(true);
      }
    }

    // -----------------------------------------------------------------------------------------------------------------
    // 导入 / 导出
    // -----------------------------------------------------------------------------------------------------------------

    function transferHtml(botId) {
      const sum = host.summaryOf(botId);
      const idle = host.isIdle();
      const busy = es.busy[botId] || "";
      const blocked = !idle || initBusy(botId) || Boolean(busy);
      const note = es.transferNote[botId];
      const defaults = host.defaults();
      const pick = es.defaultPick[botId] || (defaults[0] ? defaults[0].name : "");
      const why = !idle ? "对局开始之后不能导入：请先新建对局。" : initBusy(botId) ? "正在初始化，不能导入。" : "";
      return (
        `<div class="form-hint">导出：把 ${esc(sum ? sum.name : "这个玩家")} 的名字、头像、底层设定、记忆、战绩和待确认的提案打成一个 <code>.arenabot</code> 文件，可以备份，也可以给别人。</div>` +
        `<div class="detail-editor-actions"><button type="button" class="btn btn-xs btn-primary-outline" id="btn-bot-export" data-detail-action="bot-export"${busy ? " disabled" : ""}>导出 .arenabot</button>` +
        `<button type="button" class="btn btn-xs" id="btn-bot-import" data-detail-action="bot-import"${blocked ? " disabled" : ""}>${busy === "import" ? "导入中…" : "导入 .arenabot…"}</button></div>` +
        `<div class="form-hint">导入会<strong>覆盖</strong>这个玩家：名字、头像、底层设定、记忆都被文件里的替换（名字不能和其他玩家重复）。导入后按哈希重新判断初始化状态。</div>` +
        `<div class="detail-editor-actions"><label class="visually-hidden" for="sel-default-bot">默认库</label>` +
        `<select class="form-input default-select" id="sel-default-bot"${blocked || !defaults.length ? " disabled" : ""}>` +
        defaults.map((d) => `<option value="${esc(d.name)}"${d.name === pick ? " selected" : ""}>${esc(d.name)} · ${esc(clip(d.identity, 24))}</option>`).join("") +
        `</select><button type="button" class="btn btn-xs" id="btn-bot-default" data-detail-action="bot-default"${blocked || !defaults.length ? " disabled" : ""}>${busy === "default" ? "导入中…" : "从默认库导入"}</button></div>` +
        (why ? `<div class="form-hint error">${esc(why)}</div>` : "") +
        (note ? `<div class="${note.kind === "error" ? "form-error" : "form-hint"}" id="transfer-note">${esc(note.text)}</div>` : "") +
        (note && note.problems && note.problems.length ? `<ul class="form-error editor-problems">${note.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : "")
      );
    }

    function exportBot(botId) {
      host.download(`api/bots/${enc(botId)}/export`);
    }

    function chooseBotFile(botId) {
      es.pick = { botId, kind: "arenabot" };
      host.openFilePicker("arenabot");
    }

    function fileSizeText(n) {
      const b = Number(n) || 0;
      return b >= 1048576 ? `${(b / 1048576).toFixed(1)} MiB` : b >= 1024 ? `${Math.round(b / 1024)} KiB` : `${b} 字节`;
    }

    async function importBotFile(botId, file) {
      const sum = host.summaryOf(botId);
      const name = String((file && file.name) || "bot.arenabot");
      const choice = await openDialog({
        title: "导入 bot",
        bodyHtml: `<div>将用文件 <strong>${esc(name)}</strong>（${esc(fileSizeText(file && file.size))}）里的 bot 覆盖 <strong>${esc(sum ? sum.name : "这个玩家")}</strong>：名字、头像、底层设定、记忆都会被替换。此操作不能撤销。</div>`,
        okText: "覆盖导入",
        danger: true,
      });
      if (!choice || !choice.ok) return false;
      es.busy[botId] = "import";
      delete es.transferNote[botId];
      host.repaint();
      try {
        const detail = await api.upload(`api/bots/${enc(botId)}/import?name=${enc(name)}`, file, "application/octet-stream");
        resetBotDrafts(botId);
        host.adopt(botId, detail); // (the name and the avatar are the file's now: the card follows)
        es.transferNote[botId] = { kind: "ok", text: `已导入：${detail && detail.name ? detail.name : name}。` };
        showToast("已导入，这个玩家被文件里的 bot 替换");
        return true;
      } catch (e) {
        const problems = Array.isArray(e && e.problems) ? e.problems : [];
        es.transferNote[botId] = { kind: "error", text: problems.length ? "这个 bot 文件不能导入：" : errText(e), problems };
        showToast(`导入失败：${errText(e)}`);
        return false;
      } finally {
        delete es.busy[botId];
        host.repaint(true);
      }
    }

    async function importDefault(botId) {
      const sum = host.summaryOf(botId);
      const defaults = host.defaults();
      const name = es.defaultPick[botId] || (defaults[0] ? defaults[0].name : "");
      if (!name) return false;
      const choice = await openDialog({
        title: "从默认库导入",
        bodyHtml: `<div>将用默认库里的 <strong>${esc(name)}</strong> 覆盖 <strong>${esc(sum ? sum.name : "这个玩家")}</strong>：名字、头像、底层设定、记忆都会被替换。此操作不能撤销。</div>`,
        okText: "覆盖导入",
        danger: true,
      });
      if (!choice || !choice.ok) return false;
      es.busy[botId] = "default";
      delete es.transferNote[botId];
      host.repaint();
      try {
        const detail = await api.post(`api/bots/${enc(botId)}/import-default`, { name });
        resetBotDrafts(botId);
        host.adopt(botId, detail);
        es.transferNote[botId] = { kind: "ok", text: `已从默认库导入：${name}。` };
        showToast(`已导入默认库的 ${name}`);
        return true;
      } catch (e) {
        const problems = Array.isArray(e && e.problems) ? e.problems : [];
        es.transferNote[botId] = { kind: "error", text: errText(e), problems };
        showToast(`导入失败：${errText(e)}`);
        return false;
      } finally {
        delete es.busy[botId];
        host.repaint(true);
      }
    }

    /** Everything typed for a bot is moot once the bot itself was replaced (an import, a new line-up). */
    function resetBotDrafts(botId) {
      delete es.core[botId];
      delete es.refine[botId];
      delete es.part[botId];
    }

    /** A file was chosen in the picker (`kind`: "arenabot" | "core"): do what the click that opened the picker meant. */
    async function onFile(kind, file) {
      const pick = es.pick;
      es.pick = null;
      if (!file || !pick || pick.kind !== kind) return false;
      return kind === "core" ? importCoreFile(pick.botId, file) : importBotFile(pick.botId, file);
    }

    // -----------------------------------------------------------------------------------------------------------------
    // the panel's calls
    // -----------------------------------------------------------------------------------------------------------------

    /** The HTML of the four sections for the bot `botId` (the panel shows nothing of it for a replay). */
    function html(botId) {
      if (!botId) return "";
      const sum = host.summaryOf(botId);
      if (!sum) return "";
      if (!host.detailOf(botId)) {
        return `<div class="form-hint editors-loading" id="editors-loading">${host.detailError && host.detailError(botId) ? esc(host.detailError(botId)) : "正在读取底层设定和记忆…"}</div>`;
      }
      const staging = stagingList(botId).length;
      return [
        frame(botId, "core", coreMeta(sum), coreHtml(botId)),
        frame(botId, "memory", memoryMeta(sum), memoryHtml(botId)),
        frame(botId, "staging", staging ? `${staging} 条待确认` : "无", stagingHtml(botId)),
        frame(botId, "transfer", "", transferHtml(botId)),
      ].join("");
    }

    /** A click on `[data-detail-action]` of the panel; true when it was one of ours. */
    function onAction(action, node) {
      const botId = host.openBotId();
      if (!botId) return false;
      const attr = (k) => (node && typeof node.getAttribute === "function" ? node.getAttribute(k) : null);
      switch (action) {
        case "toggle-editor": {
          const kind = attr("data-kind");
          if (!KINDS.includes(kind)) return true;
          expand(botId, kind, !isOpen(botId, kind));
          host.repaint();
          return true;
        }
        case "core-extra-add":
          addExtra(botId);
          return true;
        case "core-extra-del":
          delExtra(botId, Number(attr("data-index")));
          return true;
        case "core-save":
          saveCore(botId);
          return true;
        case "core-revert":
          revertCore(botId);
          return true;
        case "core-clear":
          clearCore(botId);
          return true;
        case "core-refine":
          refineCore(botId);
          return true;
        case "core-draft-save":
          saveDraft(botId);
          return true;
        case "core-draft-edit":
          draftToEditor(botId);
          return true;
        case "core-draft-discard":
          discardDraft(botId);
          return true;
        case "core-export":
          exportCore(botId);
          return true;
        case "core-import":
          chooseCoreFile(botId);
          return true;
        case "part-save":
          savePart(botId, attr("data-part"));
          return true;
        case "part-revert":
          revertPart(botId, attr("data-part"));
          return true;
        case "part-clear":
          clearPart(botId, attr("data-part"));
          return true;
        case "memory-clear-all":
          clearAllMemory(botId);
          return true;
        case "stage-accept":
          decideProposal(botId, attr("data-pid"), true);
          return true;
        case "stage-reject":
          decideProposal(botId, attr("data-pid"), false);
          return true;
        case "bot-export":
          exportBot(botId);
          return true;
        case "bot-import":
          chooseBotFile(botId);
          return true;
        case "bot-default":
          importDefault(botId);
          return true;
        default:
          return false;
      }
    }

    /** `input` / `change` in the panel; true when the target was one of our fields. */
    function onInput(evt) {
      const t = evt && evt.target;
      if (!t || typeof t.getAttribute !== "function") return false;
      const botId = host.openBotId();
      if (!botId) return false;
      const value = String(t.value === undefined || t.value === null ? "" : t.value);
      if (t.id === "sel-default-bot") {
        es.defaultPick[botId] = value;
        return true;
      }
      return onCoreInput(botId, t, value) || onPartInput(botId, t, value);
    }

    /** What the panel does after it focused a bot for `fix` (bots.js: `Bots.focus`): open the section and put the cursor where the fix is. */
    function focusFix(botId, fix) {
      if (fix === "edit_core") {
        expand(botId, "core", true);
        host.repaint();
        const model = coreCurrent(botId);
        const first = Lib.CORE_KEYS.find((k) => !String(model[k] || "").trim()) || "identity";
        return `ta-core-${first}`;
      }
      if (fix === "import_core") {
        expand(botId, "core", true);
        host.repaint();
        return "btn-core-import";
      }
      if (fix === "import_default") {
        // (not a fix of the start check: the settings' "从默认库套用…" - the library's picker is in the section 导入 / 导出)
        expand(botId, "transfer", true);
        host.repaint();
        return "sel-default-bot";
      }
      return "";
    }

    /** The panel shows another bot / a new game: what was typed for the bots of the old one is dropped. */
    function reset() {
      es.core = {};
      es.refine = {};
      es.part = {};
      es.partBusy = {};
      es.partError = {};
      es.stageBusy = {};
      es.stageError = {};
      es.busy = {};
      es.transferNote = {};
      es.pick = null;
      es.gen++;
    }

    /** The memory of the bot was changed behind the editors' back (a review, an initialization wrote it): what was typed over the old text is moot. */
    function memoryChanged(botId) {
      delete es.part[botId];
      for (const k of Object.keys(es.partError)) if (k.startsWith(`${botId}:`)) delete es.partError[k];
    }

    return {
      html,
      onAction,
      onInput,
      onFile,
      focusFix,
      expand,
      reset,
      memoryChanged,
      resetBot: resetBotDrafts,
      isOpen,
      /** The last refusal of a save / an import of the L0 of this bot: {error, problems} (the settings dialog shows it in its own list). */
      coreIssue: (botId) => {
        const c = es.core[botId];
        return { error: c ? c.error || "" : "", problems: c && Array.isArray(c.problems) ? c.problems.slice() : [] };
      },
      // the pieces the tests (and the settings dialog) call directly
      saveCore,
      importCoreFile,
      importBotFile,
      refineCore,
      _state: es,
    };
  }

  const api = { create, KIND_LABELS, PART_HELP, INIT_WARNING, REQUIRED_PARTS };
  root.ArenaBotEditors = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
