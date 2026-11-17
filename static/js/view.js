/* ==========================================================================
   Arena-Agents v0.12 — view model (pure, no DOM access)
   Exposes window.ArenaView and module.exports.

   Everything the renderers need to decide WHAT to show lives here, so it can
   be tested without a browser:
     - spectator viewpoint (D4): "god" or one player. Switching never asks the
       server again; the filters below run over the PublicSession v2 object.
     - player tiles: main tag choice, public-tag filtering, state / flow badges
     - channel list: floor status chips, unread counts, ticket groups
     - top-bar hints: request progress, control-button matrix, banners
   Viewpoint rules (D4):
     messages  only those whose visible_to_ids contains the player
     channels  only those the player is a member of
     tags      other players: public_tags only; the viewed player: all own tags
     badges    thinking / acting / stopped only for the viewed player
   ========================================================================== */

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.ArenaView = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const GOD = "god";
  const MODERATOR_ID = "moderator";
  const ENGINE_ID = "engine";
  const BADGE_STATES = ["thinking", "acting", "stopped"];
  const STATE_LABELS = { thinking: "思考中", acting: "执行中", stopped: "已停止", idle: "空闲" };
  const FLOW_LABELS = { floor: "持有发言权", waiting: "等待作答" };

  const num = (v, d = 0) => {
    const n = typeof v === "number" ? v : Number(v);
    return Number.isFinite(n) ? n : d;
  };
  const list = (v) => (Array.isArray(v) ? v : []);

  // -------------------------------------------------------------------------
  // names
  // -------------------------------------------------------------------------

  function participantOf(session, id) {
    const want = String(id);
    for (const p of list(session && session.participants)) if (p.id === want) return p;
    return null;
  }

  function displayName(session, id) {
    if (id === MODERATOR_ID) return (session && session.moderator && session.moderator.name) || "主持人";
    if (id === ENGINE_ID) return "系统";
    const p = participantOf(session, id);
    return p ? p.name : String(id === undefined || id === null ? "" : id);
  }

  /** "p03 · Carol 3" (moderator / engine: just the name). */
  function actorLabel(session, id) {
    if (id === MODERATOR_ID || id === ENGINE_ID) return displayName(session, id);
    const p = participantOf(session, id);
    return p ? `${p.id} · ${p.name}` : String(id);
  }

  // -------------------------------------------------------------------------
  // spectator viewpoint
  // -------------------------------------------------------------------------

  function viewpointOptions(session) {
    const opts = [{ id: GOD, label: "上帝视角" }];
    for (const p of list(session && session.participants)) {
      opts.push({ id: p.id, label: `${p.id} · ${p.name}` });
    }
    return opts;
  }

  /** Unknown / missing players fall back to the god view. */
  function normalizeViewpoint(session, viewpoint) {
    if (!viewpoint || viewpoint === GOD) return GOD;
    return participantOf(session, viewpoint) ? String(viewpoint) : GOD;
  }

  function viewpointLabel(session, viewpoint) {
    const vp = normalizeViewpoint(session, viewpoint);
    if (vp === GOD) return "上帝视角";
    return actorLabel(session, vp);
  }

  function canSee(viewpoint, msg) {
    if (viewpoint === GOD) return true;
    return Boolean(msg) && list(msg.visible_to_ids).includes(viewpoint);
  }

  /** Messages of the current viewpoint, optionally limited to one channel. */
  function visibleMessages(session, viewpoint, channelId) {
    const all = channelId === undefined || channelId === null || channelId === "all";
    return list(session && session.messages).filter(
      (m) => canSee(viewpoint, m) && (all || m.channel_id === channelId)
    );
  }

  function visibleChannels(session, viewpoint) {
    const channels = list(session && session.channels);
    if (viewpoint === GOD) return channels;
    return channels.filter((c) => list(c.members).includes(viewpoint));
  }

  /** Non-system channels and the system DMs (D23), each in creation order. */
  function channelGroups(session, viewpoint) {
    const visible = visibleChannels(session, viewpoint);
    return {
      groups: visible.filter((c) => !c.system),
      dms: visible.filter((c) => c.system),
    };
  }

  function isChannelVisible(session, viewpoint, channelId) {
    return visibleChannels(session, viewpoint).some((c) => c.id === channelId);
  }

  /** "all" or a channel the viewpoint can see; anything else falls back to "all". */
  function normalizeSelectedChannel(session, viewpoint, selected) {
    if (!selected || selected === "all") return "all";
    return isChannelVisible(session, viewpoint, selected) ? selected : "all";
  }

  // -------------------------------------------------------------------------
  // tags
  // -------------------------------------------------------------------------

  /** Other players: public tags only. The viewed player (and god) see every tag. */
  function visibleTags(session, viewpoint, participant) {
    const tags = list(participant && participant.tags);
    if (viewpoint === GOD || (participant && participant.id === viewpoint)) return tags.slice();
    const pub = new Set(list(session && session.public_tags));
    return tags.filter((t) => pub.has(t));
  }

  /**
   * The ONE tag shown on a tile: ui.highlight_tags first (earlier entries win),
   * then the first tag that is neither a status nor an out tag, then whatever is left.
   */
  function pickMainTag(tags, ui) {
    const all = list(tags);
    if (all.length === 0) return "";
    const u = ui || {};
    for (const h of list(u.highlight_tags)) if (all.includes(h)) return h;
    const skip = new Set([...list(u.status_tags), ...list(u.out_tags)]);
    const plain = all.find((t) => !skip.has(t));
    return plain !== undefined ? plain : all[0];
  }

  const VARIANTS = ["danger", "neutral", "purple", "success", "warning", "muted"];

  /** Colour variant of a tag: scenario tag_styles first, out tags muted, else neutral. */
  function tagVariant(session, tag) {
    const styles = (session && session.tag_styles) || {};
    const style = styles[tag];
    if (style && VARIANTS.includes(style.variant)) return style.variant;
    if (list(session && session.ui && session.ui.out_tags).includes(tag)) return "muted";
    return "neutral";
  }

  // -------------------------------------------------------------------------
  // player tiles
  // -------------------------------------------------------------------------

  /** Who currently holds a floor token / owes an answer, from the viewpoint's own channels. */
  function flowState(session, viewpoint) {
    const holders = new Set();
    for (const ch of visibleChannels(session, viewpoint)) {
      const f = ch.floor;
      if (!f || ch.frozen || f.paused) continue;
      if (f.kind === "turn" || f.kind === "queue") {
        for (const q of list(f.queue)) if (q.state === "active") holders.add(q.holder);
      }
    }
    const waiting = new Set();
    for (const r of list(session && session.requests)) {
      if (r.status !== "open") continue;
      for (const pid of list(r.to)) {
        if (list(r.answered_by).includes(pid)) continue;
        if (viewpoint === GOD || pid === viewpoint) waiting.add(pid);
      }
    }
    return { holders, waiting };
  }

  /** thinking / acting / stopped, only for god or the viewed player ("" otherwise). */
  function stateBadge(viewpoint, actorId, state) {
    if (!BADGE_STATES.includes(state)) return "";
    if (viewpoint !== GOD && viewpoint !== actorId) return "";
    return state;
  }

  function tileTitle(name, id, tags, state, flow) {
    const parts = [`${id} · ${name}`];
    if (tags.length) parts.push(tags.join(" "));
    if (flow && FLOW_LABELS[flow]) parts.push(FLOW_LABELS[flow]);
    if (state && STATE_LABELS[state]) parts.push(STATE_LABELS[state]);
    return parts.join("｜");
  }

  function tileModel(session, viewpoint, participant, flow) {
    const id = participant.id;
    const tags = visibleTags(session, viewpoint, participant);
    const mainTag = pickMainTag(tags, session.ui);
    const flowInfo = flow || flowState(session, viewpoint);
    const flowBadge = flowInfo.holders.has(id) ? "floor" : flowInfo.waiting.has(id) ? "waiting" : "";
    const state = stateBadge(viewpoint, id, participant.state);
    return {
      id,
      seat: participant.seat,
      name: participant.name,
      isModerator: false,
      tags,
      mainTag,
      variant: mainTag ? tagVariant(session, mainTag) : "",
      out: Boolean(participant.out),
      state,
      flow: flowBadge,
      viewing: viewpoint === id,
      title: tileTitle(participant.name, id, tags, state, flowBadge),
    };
  }

  function moderatorTile(session, viewpoint) {
    const m = (session && session.moderator) || { name: "主持人", state: "idle" };
    const state = stateBadge(viewpoint, MODERATOR_ID, m.state);
    return {
      id: MODERATOR_ID,
      seat: "",
      name: m.name || "主持人",
      isModerator: true,
      tags: ["主持人"],
      mainTag: "主持人",
      variant: "warning",
      out: false,
      state,
      flow: "",
      viewing: false,
      title: tileTitle(m.name || "主持人", "moderator", [], state, ""),
    };
  }

  /** Moderator first, then the players in seat order. */
  function boardTiles(session, viewpoint) {
    const flow = flowState(session, viewpoint);
    return [moderatorTile(session, viewpoint)].concat(
      list(session && session.participants).map((p) => tileModel(session, viewpoint, p, flow))
    );
  }

  // -------------------------------------------------------------------------
  // channels: floor status, unread, tickets
  // -------------------------------------------------------------------------

  /**
   * Short status of a channel's speaking right:
   *   ❄ frozen · 🔒 closed · 👁 listen · 💬 free · 🎤 turn (+holders) · 📋 queue (+progress)
   */
  function floorSummary(session, channel) {
    const f = (channel && channel.floor) || {};
    const queue = list(f.queue);
    if (channel && channel.frozen) {
      return { kind: "frozen", icon: "❄", text: "已冻结", detail: "等待主持人裁决" };
    }
    switch (channel && channel.mode) {
      case "closed":
        return { kind: "closed", icon: "🔒", text: "已关闭", detail: "" };
      case "listen":
        return { kind: "listen", icon: "👁", text: "只听", detail: "" };
      case "free": {
        const fr = f.free;
        const detail = fr
          ? `${Math.round(num(fr.chars_used))}/${Math.round(num(fr.max_chars_total))} 字`
          : "";
        return { kind: "free", icon: "💬", text: "自由讨论", detail };
      }
      case "turn": {
        const names = queue.filter((q) => q.state === "active").map((q) => q.holder);
        let detail = "";
        if (names.length) {
          const shown = names.slice(0, 2).map((id) => actorLabel(session, id)).join("、");
          detail = `持麦：${shown}${names.length > 2 ? ` 等${names.length}人` : ""}`;
        }
        return {
          kind: "turn",
          icon: "🎤",
          text: f.paused ? "轮流（暂停）" : "轮流发言",
          detail,
        };
      }
      case "queue": {
        const total = queue.length;
        const done = queue.filter((q) => q.state === "done").length;
        const active = queue.find((q) => q.state === "active");
        return {
          kind: "queue",
          icon: "📋",
          text: `队列 ${done}/${total}${f.paused ? "（暂停）" : ""}`,
          detail: active ? `当前：${actorLabel(session, active.holder)}` : "",
        };
      }
      default:
        return { kind: "none", icon: "", text: "", detail: "" };
    }
  }

  /**
   * Unread messages per channel for the viewpoint. `read` = {baseline, all, seqs}:
   * a message is unread when its seq is above baseline, above the "all" watermark
   * and above that channel's own watermark.
   */
  function unreadCounts(session, viewpoint, read) {
    const r = read || {};
    const baseline = num(r.baseline, 0);
    const all = num(r.all, 0);
    const seqs = r.seqs || {};
    const byChannel = {};
    let total = 0;
    const messages = list(session && session.messages);
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.seq > 0 && m.seq <= baseline) break; // ascending seq: everything older is read
      if (!canSee(viewpoint, m) || m.channel_id === null || m.channel_id === undefined) continue;
      const floor = Math.max(baseline, all, num(seqs[m.channel_id], 0));
      if (m.seq > floor) {
        byChannel[m.channel_id] = (byChannel[m.channel_id] || 0) + 1;
        total += 1;
      }
    }
    return { byChannel, total };
  }

  /** Newest message seq of a channel (any viewpoint): the watermark used when it is opened. */
  function latestSeq(session, channelId) {
    const messages = list(session && session.messages);
    for (let i = messages.length - 1; i >= 0; i--) {
      if (channelId === "all" || messages[i].channel_id === channelId) return num(messages[i].seq, 0);
    }
    return 0;
  }

  function visibleTickets(session, viewpoint) {
    const tickets = list(session && session.tickets);
    if (viewpoint === GOD) return tickets;
    return tickets.filter((t) => t.player_id === viewpoint);
  }

  /** The private DM a ticket belongs to (system channel owned by the player, else dm-<id>). */
  function ticketChannelId(session, ticket) {
    const pid = ticket && ticket.player_id;
    const own = list(session && session.channels).find((c) => c.system && c.owner === pid);
    return own ? own.id : `dm-${pid}`;
  }

  function ticketStatus(ticket) {
    if (!ticket || ticket.status !== "resolved") return { text: "待处理", cls: "open" };
    if (ticket.verdict === "upheld") return { text: "成立", cls: "upheld" };
    if (ticket.verdict === "rejected") return { text: "驳回", cls: "rejected" };
    return { text: "已处理", cls: "resolved" };
  }

  // -------------------------------------------------------------------------
  // messages
  // -------------------------------------------------------------------------

  const KIND_INFO = {
    speech: { label: "", cls: "speech" },
    announce: { label: "公告", cls: "announce" },
    template: { label: "模板", cls: "template" },
    system: { label: "系统", cls: "system" },
    reply: { label: "质询回复", cls: "reply" },
    challenge: { label: "质询", cls: "challenge" },
    ability: { label: "能力", cls: "ability" },
  };

  function messageKindInfo(kind) {
    const k = String(kind === undefined || kind === null ? "" : kind);
    if (KIND_INFO[k]) return KIND_INFO[k];
    return { label: "", cls: k.replace(/[^a-z0-9_-]/gi, "").toLowerCase() || "speech" };
  }

  // -------------------------------------------------------------------------
  // top bar: requests, controls, banners, status
  // -------------------------------------------------------------------------

  /** One light hint per open request ("投票中 7/12"), limited to what the viewpoint may see. */
  function requestHints(session, viewpoint) {
    return list(session && session.requests)
      .filter((r) => r.status === "open" && (viewpoint === GOD || list(r.to).includes(viewpoint)))
      .map((r) => {
        const total = list(r.to).length || num(r.total, 0);
        const choice = r.kind === "choice";
        return {
          id: r.id,
          kind: r.kind,
          icon: choice ? "🗳" : "✍",
          label: choice ? "投票中" : "作答中",
          answered: list(r.answered_by).length,
          total,
          sealed: Boolean(r.sealed),
          question: r.question || "",
        };
      });
  }

  /**
   * Which control buttons are enabled for a game status. A finished game can be
   * continued (D8); there is no separate force-pause any more.
   */
  function controlState(session) {
    const st = (session && session.status) || "idle";
    return { start: st === "idle", pause: st === "running", resume: st === "paused" || st === "finished" };
  }

  function statusDotClass(session) {
    const st = (session && session.status) || "idle";
    if (st === "running") return "active";
    if (st === "paused") return "paused";
    if (st === "finished") return "finished";
    return "idle";
  }

  function statusText(session) {
    const s = session || {};
    if (s.active_status_text) return s.active_status_text;
    switch (s.status) {
      case "running":
        return "对局进行中";
      case "paused":
        return "已暂停";
      case "finished":
        return "对局已结束";
      default:
        return "未开始 · 点击「开始」由主持人初始化对局";
    }
  }

  /** Lines for the banner under the top bar (hold reason, errors, game over). */
  function statusBanners(session) {
    const s = session || {};
    const out = [];
    if (s.status === "finished") {
      const winner = s.winner ? `：${s.winner} 获胜` : "";
      const summary = s.summary ? ` — ${s.summary}` : "";
      out.push({
        kind: "finished",
        text: `🏁 对局已结束${winner}${summary}　（可点击「继续」在终局后继续）`,
      });
    } else if (s.status === "paused") {
      if (s.pause_reason === "hold") {
        const reason = s.hold_reason ? `：${s.hold_reason}` : "";
        out.push({ kind: "hold", text: `⏸ 主持人暂停了对局${reason}。确认后点击「继续」。` });
      } else if (s.pause_reason === "error") {
        out.push({ kind: "error", text: "⚠ 对局因错误暂停，请检查模型绑定后点击「继续」。" });
      } else if (s.pause_reason === "stall") {
        out.push({ kind: "error", text: "⚠ 对局长时间没有进展，已自动暂停，请检查后点击「继续」。" });
      }
    }
    if (s.autosave_error) {
      out.push({ kind: "error", text: `⚠ 自动保存失败：${s.autosave_error}` });
    }
    return out;
  }

  // -------------------------------------------------------------------------
  // metrics popover
  // -------------------------------------------------------------------------

  function actorMetrics(session, actorId) {
    const by = (session && session.usage_stats && session.usage_stats.by_actor) || {};
    const m = by[actorId] || {};
    return {
      calls: num(m.calls),
      promptTokens: num(m.prompt_tokens),
      completionTokens: num(m.completion_tokens),
      toolCalls: num(m.tool_calls),
      thinkingRounds: num(m.thinking_rounds),
      rejections: num(m.rejections),
      costUsd: num(m.cost_usd),
    };
  }

  return {
    GOD,
    MODERATOR_ID,
    ENGINE_ID,
    STATE_LABELS,
    FLOW_LABELS,
    participantOf,
    displayName,
    actorLabel,
    viewpointOptions,
    normalizeViewpoint,
    viewpointLabel,
    canSee,
    visibleMessages,
    visibleChannels,
    channelGroups,
    isChannelVisible,
    normalizeSelectedChannel,
    visibleTags,
    pickMainTag,
    tagVariant,
    flowState,
    stateBadge,
    tileModel,
    moderatorTile,
    boardTiles,
    floorSummary,
    unreadCounts,
    latestSeq,
    visibleTickets,
    ticketChannelId,
    ticketStatus,
    messageKindInfo,
    requestHints,
    controlState,
    statusDotClass,
    statusText,
    statusBanners,
    actorMetrics,
  };
});
