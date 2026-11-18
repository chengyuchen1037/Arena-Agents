/* ==========================================================================
   Arena-Agents v0.12.1 — view model (pure, no DOM access)
   Exposes window.ArenaView and module.exports.

   Everything the renderers need to decide WHAT to show lives here, so it can
   be tested without a browser:
     - spectator viewpoint (D4): "god" or one player. Switching never asks the
       server again; the filters below run over the PublicSession v3 object.
     - player cards: nickname, identity as the MAIN tag (E17), sheriff & co. as
       small badges, the three agent states (E2)
     - channel list: no "all" channel, private chats named after the other party,
       floor status chips, unread counts, ticket groups
     - request cards of the feed (E18/E25): who may see which answer
     - top bar: request progress, control-button matrix, banners, budget colours
   Viewpoint rules (D4):
     messages  only those whose visible_to_ids contains the player
     channels  only those the player is a member of
     tags      other players: public_tags only; the viewed player: all own tags
     states    thinking / acting / sleeping only for the viewed player
     requests  only those addressed to the player; answers: own, or shared ones
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
  const STATE_ICONS = { thinking: "💭", acting: "⚡", sleeping: "💤" };
  const STATE_LABELS = { thinking: "思考中", acting: "执行中", sleeping: "休眠" };
  const BUDGET_WARN = 0.8;

  const num = (v, d = 0) => {
    const n = typeof v === "number" ? v : Number(v);
    return Number.isFinite(n) ? n : d;
  };
  const list = (v) => (Array.isArray(v) ? v : []);
  const esc = (s) =>
    String(s === undefined || s === null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  // -------------------------------------------------------------------------
  // names
  // -------------------------------------------------------------------------

  function participantOf(session, id) {
    const want = String(id);
    for (const p of list(session && session.participants)) if (p.id === want) return p;
    return null;
  }

  /** The nickname shown everywhere ("Carol"); never the internal id. */
  function displayName(session, id) {
    if (id === MODERATOR_ID) return (session && session.moderator && session.moderator.name) || "主持人";
    if (id === ENGINE_ID) return "系统";
    const p = participantOf(session, id);
    return p ? p.name : String(id === undefined || id === null ? "" : id);
  }

  /** Same as displayName (v0.12 printed "p03 · Carol 3"; now only the nickname is shown). */
  function actorLabel(session, id) {
    return displayName(session, id);
  }

  /** "N3 Carol": what the models see; used as the hover text of an id in a message. */
  function refName(session, id) {
    const p = participantOf(session, id);
    return p ? `${p.id} ${p.name}` : String(id);
  }

  /** {id, seat, avatar} of any actor, for the avatar renderer. */
  function actorAvatar(session, id) {
    if (id === MODERATOR_ID) {
      // the moderator's random avatar (null on an older backend: the renderer then draws the fixed top hat)
      const m = session && session.moderator;
      return { id, seat: undefined, desc: m && m.avatar ? m.avatar : null };
    }
    const p = participantOf(session, id);
    return { id, seat: p ? p.seat : undefined, desc: p ? p.avatar : null };
  }

  // -------------------------------------------------------------------------
  // spectator viewpoint
  // -------------------------------------------------------------------------

  function viewpointOptions(session) {
    const opts = [{ id: GOD, label: "上帝视角" }];
    for (const p of list(session && session.participants)) {
      opts.push({ id: p.id, label: `${p.seat} · ${p.name}` });
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
    return displayName(session, vp);
  }

  function canSee(viewpoint, msg) {
    if (viewpoint === GOD) return true;
    return Boolean(msg) && list(msg.visible_to_ids).includes(viewpoint);
  }

  /** Messages of the current viewpoint; `channelId` omitted / "all" means every channel (internal use). */
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

  const isDmChannel = (c) => Boolean(c && (c.system || c.type === "dm"));

  /** Group channels and private chats (system DMs), each in creation order (DMs by seat). */
  function channelGroups(session, viewpoint) {
    const visible = visibleChannels(session, viewpoint);
    const seatOfOwner = (c) => {
      const p = participantOf(session, c.owner);
      return p ? p.seat : 999;
    };
    const dms = visible.filter(isDmChannel);
    if (dms.every((c) => c.owner)) dms.sort((a, b) => seatOfOwner(a) - seatOfOwner(b));
    return { groups: visible.filter((c) => !isDmChannel(c)), dms };
  }

  function isChannelVisible(session, viewpoint, channelId) {
    return visibleChannels(session, viewpoint).some((c) => c.id === channelId);
  }

  /** A channel everyone is in (the public one): members cover every participant. */
  function coversEveryone(session, channel) {
    const players = list(session && session.participants);
    return players.length > 0 && players.every((p) => list(channel.members).includes(p.id));
  }

  /**
   * The channel selected when the user has not chosen one (the "all" channel is gone): the public
   * channel (id "public", else a non-private channel everybody is in), else the first group channel.
   * Private chats are never selected on their own: before the game starts only those exist (one per
   * player), and a feed that opens on "Alice"'s private chat would be a puzzle — null shows the hint
   * "the moderator creates the channels once the game starts" instead.
   */
  function defaultChannelId(session, viewpoint) {
    const visible = visibleChannels(session, viewpoint);
    const pick =
      visible.find((c) => c.id === "public") ||
      visible.find((c) => !isDmChannel(c) && coversEveryone(session, c)) ||
      visible.find((c) => !isDmChannel(c));
    return pick ? pick.id : null;
  }

  /** A channel the viewpoint can see, else the default channel (may be null). */
  function normalizeSelectedChannel(session, viewpoint, selected) {
    if (selected && selected !== "all" && isChannelVisible(session, viewpoint, selected)) return selected;
    return defaultChannelId(session, viewpoint);
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

  function tagSets(session) {
    const ui = (session && session.ui) || {};
    return {
      status: new Set(list(ui.status_tags)),
      out: new Set(list(ui.out_tags)),
      highlight: new Set(list(ui.highlight_tags)),
      pub: new Set(list(session && session.public_tags)),
    };
  }

  /** An identity tag is private: neither a public tag nor a status / out tag (alive, out, sheriff, …). */
  function isIdentityTag(sets, tag) {
    return !sets.pub.has(tag) && !sets.status.has(tag) && !sets.out.has(tag);
  }

  /**
   * First identity tag of a visible tag list, "" when there is none (v0.12.1 / E17: the identity is
   * always the main tag; highlight tags such as the sheriff never take its place).
   */
  function pickMainTag(tags, ui, publicTags) {
    const u = ui || {};
    const sets = {
      status: new Set(list(u.status_tags)),
      out: new Set(list(u.out_tags)),
      pub: new Set(list(publicTags)),
    };
    const found = list(tags).find((t) => isIdentityTag(sets, t));
    return found === undefined ? "" : found;
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

  /**
   * The main tag of a card for a viewpoint:
   *   known identity  -> {text: "狼人", known: true}
   *   hidden identity -> {text: "?", known: false}   (the participant has one, this viewpoint may not see it)
   * A scenario without private identity tags falls back to the first non-status tag.
   */
  function mainTagInfo(session, viewpoint, participant) {
    const sets = tagSets(session);
    const visible = visibleTags(session, viewpoint, participant);
    const identity = visible.find((t) => isIdentityTag(sets, t));
    if (identity !== undefined) return { text: identity, known: true, variant: tagVariant(session, identity) };
    const hidden = list(participant && participant.tags).some((t) => isIdentityTag(sets, t));
    if (!hidden) {
      const fallback = visible.find((t) => !sets.status.has(t) && !sets.out.has(t) && !sets.highlight.has(t));
      if (fallback !== undefined) return { text: fallback, known: true, variant: tagVariant(session, fallback) };
    }
    return { text: "?", known: false, variant: "muted" };
  }

  /** Small corner badges: the visible highlight tags (sheriff, …) that are not the identity itself. */
  function badgeTags(session, viewpoint, participant) {
    const sets = tagSets(session);
    return visibleTags(session, viewpoint, participant)
      .filter((t) => sets.highlight.has(t) && !isIdentityTag(sets, t))
      .map((t) => ({ tag: t, text: Array.from(t)[0] || t, variant: tagVariant(session, t) }));
  }

  // -------------------------------------------------------------------------
  // player cards
  // -------------------------------------------------------------------------

  /** thinking / acting / sleeping, only for god or the viewed player ("" otherwise). */
  function stateBadge(viewpoint, actorId, state) {
    if (!STATE_LABELS[state]) return "";
    if (viewpoint !== GOD && viewpoint !== actorId) return "";
    return state;
  }

  /** Hover text of a state: "思考中" / "执行中" / "休眠：等待新消息" / "休眠：定时醒来：剩余 12 秒". */
  function stateTitle(state, sleepUntil, clockNow) {
    if (!STATE_LABELS[state]) return "";
    if (state !== "sleeping") return STATE_LABELS[state];
    const has = sleepUntil !== null && sleepUntil !== undefined && Number.isFinite(Number(sleepUntil));
    if (!has) return "休眠：等待新消息";
    const left = Number(sleepUntil) - num(clockNow, 0);
    if (left <= 0) return "休眠：即将醒来";
    const s = Math.ceil(left);
    const text = s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, "0")} 秒`;
    return `休眠：定时醒来：剩余 ${text}`;
  }

  function tileTitle(seat, name, main, state, stateText) {
    const parts = [`${seat}号 ${name}`];
    if (main && main.known) parts.push(main.text);
    if (state && stateText) parts.push(`${STATE_ICONS[state]} ${stateText}`);
    return parts.join("｜");
  }

  /** One player card. ctx: {clockNow} (the client's game-clock estimate, for the sleep timer). */
  function tileModel(session, viewpoint, participant, ctx) {
    const id = participant.id;
    const main = mainTagInfo(session, viewpoint, participant);
    const state = stateBadge(viewpoint, id, participant.state);
    const clockNow = ctx && ctx.clockNow;
    const sTitle = stateTitle(state, participant.sleep_until, clockNow);
    return {
      id,
      seat: participant.seat,
      name: participant.name,
      avatar: participant.avatar || null,
      isModerator: false,
      main,
      badges: badgeTags(session, viewpoint, participant),
      out: Boolean(participant.out),
      state,
      stateIcon: state ? STATE_ICONS[state] : "",
      stateTitle: sTitle,
      sleepUntil: participant.sleep_until === undefined ? null : participant.sleep_until,
      viewing: viewpoint === id,
      title: tileTitle(participant.seat, participant.name, main, state, sTitle),
    };
  }

  /** The moderator is not a card: a small chip at the end of the status row. */
  function moderatorTile(session, viewpoint, ctx) {
    const m = (session && session.moderator) || { name: "主持人", state: "sleeping" };
    const state = stateBadge(viewpoint, MODERATOR_ID, m.state);
    const sTitle = stateTitle(state, m.sleep_until, ctx && ctx.clockNow);
    const name = m.name || "主持人";
    return {
      id: MODERATOR_ID,
      seat: "",
      name,
      avatar: null,
      isModerator: true,
      state,
      stateIcon: state ? STATE_ICONS[state] : "",
      stateTitle: sTitle,
      sleepUntil: m.sleep_until === undefined ? null : m.sleep_until,
      title: state ? `${name}｜${STATE_ICONS[state]} ${sTitle}` : name,
    };
  }

  /** The players in seat order (the moderator is a separate chip). */
  function boardTiles(session, viewpoint, ctx) {
    return list(session && session.participants)
      .slice()
      .sort((a, b) => num(a.seat) - num(b.seat))
      .map((p) => tileModel(session, viewpoint, p, ctx));
  }

  // -------------------------------------------------------------------------
  // board summary strip (god view only)
  // -------------------------------------------------------------------------

  const FACTION_SHORT = {
    狼人: "狼",
    狼: "狼",
    神职: "神",
    神民: "神",
    神: "神",
    好人: "好",
    平民: "民",
    村民: "民",
    民: "民",
    第三方: "三",
  };

  /** "狼人阵营" -> "狼", "神职" -> "神", "平民阵营" -> "民"; unknown names keep their first character. */
  function shortFactionName(name) {
    const base = String(name === undefined || name === null ? "" : name)
      .trim()
      .replace(/(阵营|阵容|阵)$/, "");
    if (FACTION_SHORT[base]) return FACTION_SHORT[base];
    const chars = Array.from(base);
    return chars.length <= 1 ? base : chars[0];
  }

  /**
   * The compact strip at the end of the participant bar: "狼 2/4 · 神 4/4 · 民 3/4 · 警长 Grace".
   * Hidden information (factions): null unless the viewpoint is god.
   */
  function boardSummary(session, viewpoint) {
    if (viewpoint !== GOD) return null;
    const items = [];
    for (const f of list(session && session.dashboard && session.dashboard.factions)) {
      const alive = num(f.alive);
      const total = num(f.total);
      items.push({
        text: `${shortFactionName(f.name)} ${alive}/${total}`,
        title: `${f.name}：存活 ${alive} / ${total}`,
        variant: VARIANTS.includes(f.variant) ? f.variant : "neutral",
      });
    }
    const highlights = list(session && session.ui && session.ui.highlight_tags);
    const sets = tagSets(session);
    for (const tag of highlights) {
      if (isIdentityTag(sets, tag)) continue; // an identity (team) is already on the cards
      const holders = list(session.participants).filter((p) => list(p.tags).includes(tag));
      if (!holders.length) continue;
      const names = holders.slice(0, 3).map((p) => p.name).join("、");
      items.push({
        text: `${tag} ${names}${holders.length > 3 ? ` 等${holders.length}人` : ""}`,
        title: `${tag}：${holders.map((p) => `${p.seat}号 ${p.name}`).join("、")}`,
        variant: tagVariant(session, tag),
      });
    }
    if (!items.length) return null;
    return { items, text: items.map((i) => i.text).join(" · "), title: items.map((i) => i.title).join("\n") };
  }

  // -------------------------------------------------------------------------
  // channels: floor status, unread, tickets
  // -------------------------------------------------------------------------

  /**
   * Short status of a channel's speaking right:
   *   ❄ frozen · 🔇 muted (was "closed") · 👁 listen · 💬 free · 🎤 turn (+holders) · 📋 queue (+progress)
   */
  function floorSummary(session, channel) {
    const f = (channel && channel.floor) || {};
    const queue = list(f.queue);
    if (channel && channel.frozen) {
      return { kind: "frozen", icon: "❄", text: "已冻结", detail: "等待主持人裁决" };
    }
    switch (channel && channel.mode) {
      case "closed":
        return { kind: "closed", icon: "🔇", text: "禁言中", detail: "" };
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
          const shown = names.slice(0, 2).map((id) => displayName(session, id)).join("、");
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
          detail: active ? `持麦：${displayName(session, active.holder)}` : "",
        };
      }
      default:
        return { kind: "none", icon: "", text: "", detail: "" };
    }
  }

  /** The other party/parties of a private chat as seen from the viewpoint. */
  function dmCounterparts(session, viewpoint, channel) {
    const members = list(channel && channel.members);
    let others;
    if (viewpoint === GOD) {
      others = channel.owner ? [channel.owner] : members.filter((m) => m !== MODERATOR_ID);
    } else {
      others = members.filter((m) => m !== viewpoint);
    }
    return others.length ? others : [MODERATOR_ID];
  }

  /**
   * Everything the channel list needs for one channel. A private chat is titled with the other
   * party's nickname only (no "私聊"), wears that party's avatar and a tinted background.
   */
  function channelModel(session, viewpoint, channel) {
    const dm = isDmChannel(channel);
    const summary = floorSummary(session, channel);
    if (dm) {
      const others = dmCounterparts(session, viewpoint, channel);
      return {
        id: channel.id,
        kind: "dm",
        title: others.map((id) => displayName(session, id)).join("、"),
        avatarId: others[0],
        glyph: "",
        summary,
        memberCount: list(channel.members).length,
        owner: channel.owner,
      };
    }
    const name = channel.name || channel.id;
    return {
      id: channel.id,
      kind: "group",
      title: name,
      avatarId: null,
      glyph: Array.from(name)[0] || "#",
      summary,
      memberCount: list(channel.members).length,
      owner: null,
    };
  }

  /** Title of a channel (the feed header): group name, or the other party of a private chat. */
  function channelTitle(session, viewpoint, channelId) {
    const ch = list(session && session.channels).find((c) => c.id === channelId);
    return ch ? channelModel(session, viewpoint, ch).title : "";
  }

  /**
   * Unread messages per channel for the viewpoint. `read` = {baseline, seqs}: a message is unread when
   * its seq is above baseline and above that channel's own watermark.
   */
  function unreadCounts(session, viewpoint, read) {
    const r = read || {};
    const baseline = num(r.baseline, 0);
    const seqs = r.seqs || {};
    const byChannel = {};
    let total = 0;
    const messages = list(session && session.messages);
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.seq > 0 && m.seq <= baseline) break; // ascending seq: everything older is read
      if (!canSee(viewpoint, m) || m.channel_id === null || m.channel_id === undefined) continue;
      const floor = Math.max(baseline, num(seqs[m.channel_id], 0));
      if (m.seq > floor) {
        byChannel[m.channel_id] = (byChannel[m.channel_id] || 0) + 1;
        total += 1;
      }
    }
    return { byChannel, total };
  }

  /** Newest message seq of a channel: the watermark used when it is opened. */
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

  /**
   * Hover texts for internal ids inside already-escaped message HTML: "N3" keeps its text and gains
   * a tooltip "N3 Carol" (E13/#8). The original text is never changed; tags are left alone.
   */
  function annotateIdRefs(html, session) {
    const players = list(session && session.participants);
    if (!players.length || !html) return html;
    const ids = players.map((p) => p.id).filter(Boolean).sort((a, b) => b.length - a.length);
    const re = new RegExp(`\\b(${ids.map((i) => i.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})\\b`, "g");
    const byId = new Map(players.map((p) => [p.id, p.name]));
    return String(html)
      .split(/(<[^>]*>)/)
      .map((part) =>
        part.startsWith("<")
          ? part
          : part.replace(re, (m) => `<span class="id-ref" title="${esc(`${m} ${byId.get(m)}`)}">${m}</span>`)
      )
      .join("");
  }

  // -------------------------------------------------------------------------
  // requests: cards in the feed (E18 / E25)
  // -------------------------------------------------------------------------

  const REQUEST_STATUS_LABELS = { open: "进行中", closed: "已关闭", cancelled: "已取消" };

  /** god sees every request; a player only the ones addressed to them. */
  function requestVisible(request, viewpoint) {
    if (viewpoint === GOD) return true;
    return list(request && request.to).includes(viewpoint);
  }

  /**
   * Answers the viewpoint may see. god: all (sealed ones included). A player: their own, plus everybody's
   * when the request shares answers and they are a recipient (a sealed request never shares).
   */
  function visibleAnswers(request, viewpoint) {
    const answers = list(request && request.answers);
    if (viewpoint === GOD) return answers;
    const isRecipient = list(request.to).includes(viewpoint);
    return answers.filter((a) => {
      if (a.player_id === viewpoint) return true;
      if (!request.share_answers || request.sealed || !isRecipient) return false;
      return Array.isArray(a.visible_to_ids) ? a.visible_to_ids.includes(viewpoint) : true;
    });
  }

  /** The vote count / leaders of a closed request are visible to god, or to recipients when answers are open. */
  function resultVisible(request, viewpoint) {
    if (!request || !request.result) return false;
    if (viewpoint === GOD) return true;
    return list(request.to).includes(viewpoint) && (!request.sealed || Boolean(request.share_answers));
  }

  /**
   * The channel whose feed shows a request card: the request's own channel_id if the backend sends one,
   * else the tightest visible channel that contains every recipient (wolves' target request -> the wolves
   * channel, a vote of everybody -> the public channel, a last-words request -> that player's private chat).
   */
  function requestChannelId(session, viewpoint, request) {
    const visible = visibleChannels(session, viewpoint);
    if (request.channel_id && visible.some((c) => c.id === request.channel_id)) return request.channel_id;
    const to = list(request.to);
    const fit = visible
      .filter((c) => to.length > 0 && to.every((id) => list(c.members).includes(id)))
      .sort((a, b) => list(a.members).length - list(b.members).length);
    return fit.length ? fit[0].id : defaultChannelId(session, viewpoint);
  }

  /** Seconds left of an open request at the client's game-clock estimate (null: no deadline). */
  function requestCountdown(request, clockNow) {
    if (!request || request.status !== "open" || request.deadline_clock === null || request.deadline_clock === undefined) {
      return null;
    }
    return Math.max(0, num(request.deadline_clock) - num(clockNow, 0));
  }

  /** View-model of one request card for a viewpoint. */
  function requestCard(session, viewpoint, request, ctx) {
    const answers = visibleAnswers(request, viewpoint);
    const byPlayer = new Map(answers.map((a) => [a.player_id, a]));
    const answered = new Set(list(request.answered_by));
    for (const a of list(request.answers)) answered.add(a.player_id);
    const recipients = list(request.to)
      .map((id) => participantOf(session, id) || { id, name: id, seat: 0 })
      .sort((a, b) => num(a.seat) - num(b.seat))
      .map((p) => {
        const a = byPlayer.get(p.id) || null;
        return {
          id: p.id,
          seat: p.seat,
          name: p.name,
          answered: answered.has(p.id),
          // an answer exists but this viewpoint may not read it (sealed vote, someone else's choice)
          hidden: answered.has(p.id) && !a,
          answer: a
            ? {
                label: a.label || String(a.value),
                value: a.value,
                reason: a.reason,
                defaulted: Boolean(a.defaulted),
                changes: num(request.answer_changes && request.answer_changes[p.id], 0),
              }
            : null,
        };
      });
    let result = null;
    if (resultVisible(request, viewpoint)) {
      const r = request.result;
      const labelOf = (id) => {
        const o = list(request.options).find((x) => x.id === id);
        return o ? o.label : displayName(session, id);
      };
      const tallyRows = r.tally
        ? Object.keys(r.tally).map((id) => ({
            id,
            label: labelOf(id),
            count: r.tally[id],
            leader: list(r.leaders).includes(id) && r.tally[id] > 0,
          }))
        : [];
      tallyRows.sort((a, b) => b.count - a.count);
      // rows without a single vote are folded into "其余 N 项 0 票" (a 12-option vote would be a wall of empty bars)
      const voted = tallyRows.filter((t) => t.count > 0);
      result = {
        reason: r.reason,
        tally: voted.length ? voted : tallyRows,
        zeroCount: voted.length ? tallyRows.length - voted.length : 0,
        leaders: list(r.leaders).map(labelOf),
        defaulted: list(r.defaulted).map((id) => displayName(session, id)),
        missing: list(r.missing).map((id) => displayName(session, id)),
      };
    }
    return {
      id: request.id,
      kind: request.kind,
      question: request.question,
      status: request.status,
      statusLabel: REQUEST_STATUS_LABELS[request.status] || request.status,
      sealed: request.sealed,
      shareAnswers: request.share_answers,
      autoClose: request.auto_close,
      options: request.options,
      recipients,
      answeredCount: answered.size,
      total: list(request.to).length || num(request.total, 0),
      countdown: requestCountdown(request, ctx && ctx.clockNow),
      deadlineClock: request.status === "open" && request.deadline_clock !== undefined ? request.deadline_clock : null,
      result,
      // sealed requests keep their answers from the other players; god always sees them
      answersHidden: request.sealed && viewpoint !== GOD,
    };
  }

  /**
   * The feed of one channel: committed messages and request cards in ledger order. A request is placed
   * at its opened_seq (a request without one comes after everything); on a tie the message goes first.
   */
  function feedEntries(session, viewpoint, channelId) {
    const entries = [];
    for (const m of visibleMessages(session, viewpoint, channelId)) {
      entries.push({ key: `m${m.seq}`, kind: "msg", seq: m.seq, order: 0, ref: m });
    }
    let n = 0;
    for (const r of list(session && session.requests)) {
      if (!requestVisible(r, viewpoint)) continue;
      if (requestChannelId(session, viewpoint, r) !== channelId) continue;
      const seq = num(r.opened_seq) > 0 ? num(r.opened_seq) : Number.MAX_SAFE_INTEGER;
      entries.push({ key: `r${r.id}`, kind: "req", seq, order: 1 + n++, ref: r });
    }
    return entries.sort((a, b) => a.seq - b.seq || a.order - b.order);
  }

  // -------------------------------------------------------------------------
  // top bar: requests, controls, banners, status, budget
  // -------------------------------------------------------------------------

  /** One light hint per open request ("投票中 7/12"), limited to what the viewpoint may see. */
  function requestHints(session, viewpoint) {
    return list(session && session.requests)
      .filter((r) => r.status === "open" && requestVisible(r, viewpoint))
      .map((r) => {
        const total = list(r.to).length || num(r.total, 0);
        const choice = r.kind === "choice";
        return {
          id: r.id,
          kind: r.kind,
          icon: choice ? "🗳" : "✍",
          label: r.share_answers ? "商议中" : choice ? "投票中" : "作答中",
          answered: new Set([...list(r.answered_by), ...list(r.answers).map((a) => a.player_id)]).size,
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
    if (s.status === "paused" && s.pause_reason === "budget") return "因预算上限暂停";
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

  /** Token / cost usage against the limits (the dialog and the top-bar colours). */
  function budgetView(session) {
    const b = (session && session.budget) || {};
    const maxTokens = b.max_tokens || null;
    const maxCost = b.max_cost_usd || null;
    const usedTokens = num(b.used_tokens);
    const usedCost = num(b.used_cost_usd);
    const tokenRatio = maxTokens ? usedTokens / maxTokens : null;
    const costRatio = maxCost ? usedCost / maxCost : null;
    const ratios = [tokenRatio, costRatio].filter((r) => r !== null);
    const ratio = ratios.length ? Math.max(...ratios) : null;
    // the server's state wins; the ratio is only a fallback for the colours
    const state =
      b.state === "warning" || b.state === "reached"
        ? b.state
        : ratio !== null && ratio >= 1
          ? "reached"
          : ratio !== null && ratio >= BUDGET_WARN
            ? "warning"
            : "ok";
    return {
      state,
      forceStop: Boolean(b.force_stop),
      hasLimit: ratio !== null,
      ratio,
      tokens: { used: usedTokens, max: maxTokens, ratio: tokenRatio },
      cost: { used: usedCost, max: maxCost, ratio: costRatio },
    };
  }

  /** Lines for the banner under the top bar, most severe first. */
  function statusBanners(session) {
    const s = session || {};
    const out = [];
    const budget = budgetView(s);
    // `short` is what a phone shows (the full sentence would take three lines of a narrow screen)
    if (s.status === "paused" && s.pause_reason === "budget") {
      const reached = budget.state === "reached";
      out.push({
        kind: "budget",
        text: reached
          ? "⛔ 因预算上限暂停：已达到设置的 token / 费用上限。请提高上限（或取消「强制停止」）后点击「继续」。"
          : "⏸ 因预算上限暂停（上限已调整，用量已低于上限）：可以点击「继续」。",
        short: reached ? "⛔ 因预算上限暂停：请提高上限后点「继续」" : "⏸ 因预算上限暂停：可以点「继续」",
      });
    } else if (budget.state === "reached") {
      out.push({
        kind: "budget",
        text: budget.forceStop
          ? "⛔ 已达到预算上限，正在强制停止。"
          : "⛔ 已达到预算上限（未勾选「强制停止」，对局仍在继续）。",
      });
    }
    if (s.stall_notice) {
      // The watchdog's own sentence already says what is wrong ("对局静止：已连续 3 次唤醒主持人，账本仍无进展
      // （约 120 秒）。"): show it as it is. A bare reason ("已 120 秒没有进展" would be a sentence too, but
      // "3 次唤醒" is not) gets the heading.
      const notice = String(s.stall_notice);
      const says = /停滞|静止|没有进展|无进展/.test(notice);
      out.push({
        kind: "stall",
        text: `⚠ ${says ? notice : `对局似乎停滞了：${notice}`}`,
        short: "⚠ 对局似乎停滞了（游戏没有暂停）",
      });
    }
    if (s.status === "finished") {
      const winner = s.winner ? `：${s.winner} 获胜` : "";
      const summary = s.summary ? ` — ${s.summary}` : "";
      out.push({
        kind: "finished",
        text: `🏁 对局已结束${winner}${summary}　（可点击「继续」在终局后继续，或「复盘全部」）`,
        short: `🏁 对局已结束${winner}`,
      });
    } else if (s.status === "paused") {
      if (s.pause_reason === "hold") {
        const reason = s.hold_reason ? `：${s.hold_reason}` : "";
        out.push({
          kind: "hold",
          text: `⏸ 主持人暂停了对局${reason}。确认后点击「继续」。`,
          short: `⏸ 主持人暂停了对局${reason}`,
        });
      } else if (s.pause_reason === "error") {
        out.push({ kind: "error", text: "⚠ 对局因错误暂停，请检查模型绑定后点击「继续」。" });
      } else if (s.pause_reason === "stall") {
        out.push({ kind: "error", text: "⚠ 对局长时间没有进展，已暂停，请检查后点击「继续」。" });
      }
    }
    if (s.autosave_error) {
      out.push({ kind: "error", text: `⚠ 自动保存失败：${s.autosave_error}` });
    }
    return out;
  }

  // -------------------------------------------------------------------------
  // metrics / detail panel
  // -------------------------------------------------------------------------

  function actorMetrics(session, actorId) {
    const by = (session && session.usage_stats && session.usage_stats.by_actor) || {};
    const m = by[actorId] || {};
    const prompt = num(m.prompt_tokens);
    const cacheRead = num(m.cache_read_tokens);
    const cacheWrite = num(m.cache_write_tokens);
    const inputSide = prompt + cacheRead + cacheWrite;
    return {
      activations: num(m.activations),
      calls: num(m.calls),
      promptTokens: prompt,
      cacheReadTokens: cacheRead,
      cacheWriteTokens: cacheWrite,
      completionTokens: num(m.completion_tokens),
      cacheHitRate: m.cache_hit_rate !== undefined ? num(m.cache_hit_rate) : inputSide > 0 ? cacheRead / inputSide : 0,
      toolCalls: num(m.tool_calls),
      thinkingRounds: num(m.thinking_rounds),
      rejections: num(m.rejections),
      costUsd: num(m.cost_usd),
    };
  }

  /**
   * Review (E21) as the UI needs it. all: the "复盘全部" button; of(id): one player.
   * The button only exists once the game is finished.
   */
  function reviewView(session) {
    const s = session || {};
    const r = s.review || { available: false, done: [], running: false, progress: {} };
    const ids = list(s.participants).map((p) => p.id);
    const done = new Set(list(r.done));
    const progress = r.progress || {};
    const running = Boolean(r.running) || Object.values(progress).some((p) => p && p.state === "running");
    const finished = s.status === "finished";
    const stateOf = (id) => {
      if (done.has(id)) return { state: "done", message: (progress[id] && progress[id].message) || "" };
      const p = progress[id];
      if (p && p.state === "running") return { state: "running", message: p.message || "" };
      if (p && p.state === "error") return { state: "error", message: p.message || "" };
      return { state: "none", message: "" };
    };
    const doneCount = ids.filter((id) => done.has(id)).length;
    return {
      visible: finished,
      available: r.available !== false,
      running,
      doneCount,
      total: ids.length,
      allDone: ids.length > 0 && doneCount === ids.length,
      of: stateOf,
    };
  }

  return {
    GOD,
    MODERATOR_ID,
    ENGINE_ID,
    STATE_ICONS,
    STATE_LABELS,
    participantOf,
    displayName,
    actorLabel,
    refName,
    actorAvatar,
    viewpointOptions,
    normalizeViewpoint,
    viewpointLabel,
    canSee,
    visibleMessages,
    visibleChannels,
    isDmChannel,
    channelGroups,
    isChannelVisible,
    defaultChannelId,
    normalizeSelectedChannel,
    visibleTags,
    pickMainTag,
    mainTagInfo,
    badgeTags,
    tagVariant,
    stateBadge,
    stateTitle,
    tileModel,
    moderatorTile,
    boardTiles,
    shortFactionName,
    boardSummary,
    floorSummary,
    dmCounterparts,
    channelModel,
    channelTitle,
    unreadCounts,
    latestSeq,
    visibleTickets,
    ticketChannelId,
    ticketStatus,
    messageKindInfo,
    annotateIdRefs,
    requestVisible,
    visibleAnswers,
    resultVisible,
    requestChannelId,
    requestCountdown,
    requestCard,
    feedEntries,
    requestHints,
    controlState,
    statusDotClass,
    statusText,
    budgetView,
    statusBanners,
    actorMetrics,
    reviewView,
  };
});
