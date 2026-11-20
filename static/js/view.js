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
  // v0.13 (F6): "human" is the card of a seat a person plays instead of its agent
  // v0.13.1 (G3): "held" = woken up and waiting in front of the soft-pause gate ("⏸ 暂停中")
  const STATE_ICONS = { thinking: "💭", acting: "⚡", sleeping: "💤", human: "🎮", held: "⏸" };
  const STATE_LABELS = { thinking: "思考中", acting: "执行中", sleeping: "休眠", human: "人类操作中", held: "暂停中" };
  const BUDGET_WARN = 0.8;

  const num = (v, d = 0) => {
    const n = typeof v === "number" ? v : Number(v);
    return Number.isFinite(n) ? n : d;
  };
  const list = (v) => (Array.isArray(v) ? v : []);
  /** A sentence of someone else without the stops at its end (the same rule as `Core.stripEndPunct`): the page puts its own punctuation after it. */
  const END_STOPS = /[\s。．；;，,、：:]+$/u;
  const endStopsOff = (text) => {
    let s = String(text === undefined || text === null ? "" : text).replace(END_STOPS, "");
    if (/[^.]\.$/.test(s)) s = s.slice(0, -1).replace(END_STOPS, "");
    return s;
  };
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

  /** Unknown / missing players fall back to the global view. */
  function normalizeViewpoint(session, viewpoint) {
    if (!viewpoint || viewpoint === GOD) return GOD;
    return participantOf(session, viewpoint) ? String(viewpoint) : GOD;
  }

  // -------------------------------------------------------------------------
  // v0.13 / v0.14: playing a seat: the viewpoint label and its menu (H27), the takeover bar
  // -------------------------------------------------------------------------

  /** The seat a person plays right now (null: nobody). */
  function takeoverSeat(session) {
    const t = session && session.takeover;
    return t && t.seat ? String(t.seat) : null;
  }

  /** "3 · Carol" */
  function seatLabel(session, seat) {
    const p = participantOf(session, seat);
    return p ? `${p.seat} · ${p.name}` : String(seat);
  }

  /** Why a game in `status` cannot be acted in (the send button says it, hover texts repeat it). */
  function notRunningText(status) {
    if (status === "idle") return "对局还没有开始";
    if (status === "finished") return "对局已结束";
    return "对局已暂停";
  }

  /**
   * May this seat be taken over (F2)?  Only a player seat, only while nobody is played yet, only while the game has not
   * started or is paused.  `reason` is the tooltip.
   */
  function takeoverAvailability(session, seatId) {
    if (session && session.replay) return { ok: false, reason: "重放中不能接管：请先退出重放" }; // (F36)
    if (takeoverSeat(session)) return { ok: false, reason: "你已经在扮演一个座位：请先退出接管" };
    if (!seatId || seatId === GOD) return { ok: false, reason: "全局视角没有座位，不能接管：请选一名玩家" };
    const p = participantOf(session, seatId);
    if (!p) return { ok: false, reason: "这个座位不存在" };
    const status = (session && session.status) || "idle";
    if (status === "running") return { ok: false, reason: "对局进行中不能接管：请先点「暂停」" };
    if (status === "finished") return { ok: false, reason: "对局已结束，不能接管" };
    return {
      ok: true,
      reason: `接管 ${seatLabel(session, seatId)}：以这个座位参与对局，它的 agent 暂停（${
        status === "idle" ? "对局未开始" : "对局已暂停"
      }时可用）`,
    };
  }

  /**
   * The viewpoint label of the top bar (H27, contract §13.10), a pure front-end state worked out from the chosen viewpoint and
   * `takeover.seat`: "全局视角" · "Alice视角" · "Alice视角（接管）" (with 🎮, locked while a seat is played).
   */
  function viewpointChip(session, viewpoint) {
    const seat = takeoverSeat(session);
    if (seat) {
      const name = displayName(session, seat);
      return {
        kind: "played",
        icon: "🎮",
        text: `${name}视角（接管）`,
        locked: true,
        seat,
        title: `你正在扮演 ${seatLabel(session, seat)}：视角已锁定为这个座位。点击打开菜单（退出接管…）`,
      };
    }
    const vp = normalizeViewpoint(session, viewpoint);
    if (vp === GOD) {
      return {
        kind: "god",
        icon: "👁",
        text: "全局视角",
        locked: false,
        seat: null,
        title: "全局视角：看到全部消息、频道和（开局后的）身份。点击切换视角，或接管一个座位",
      };
    }
    const name = displayName(session, vp);
    return {
      kind: "player",
      icon: "👁",
      text: `${name}视角`,
      locked: false,
      seat: null,
      title: `${name}视角：只显示这名玩家能看到的消息、频道、标签与请求。点击切换视角，或接管一个座位`,
    };
  }

  /**
   * The menu behind the label (H27): 全局视角 · 以 X 的视角观看 (one per player) · 接管 X (one per player; the rules of F2) · 退出接管.
   * Nothing here is applied until an item is chosen.  While a seat is played the viewpoint is locked: only 退出接管 works.
   */
  function viewpointMenu(session, viewpoint) {
    const chip = viewpointChip(session, viewpoint);
    const seat = takeoverSeat(session);
    const applied = seat || normalizeViewpoint(session, viewpoint);
    const lockedWhy = "接管期间视角已锁定为你扮演的座位：请先退出接管";
    const god = {
      id: GOD,
      label: "全局视角",
      current: applied === GOD,
      enabled: !seat && applied !== GOD,
      reason: seat ? lockedWhy : applied === GOD ? "现在就是全局视角" : "全局视角：看到全部消息、频道和（开局后的）身份",
    };
    const players = list(session && session.participants)
      .slice()
      .sort((a, b) => num(a.seat) - num(b.seat))
      .map((p) => {
        const take = takeoverAvailability(session, p.id);
        const current = p.id === applied;
        return {
          id: p.id,
          seat: p.seat,
          name: p.name,
          label: `${p.seat} · ${p.name}`,
          current,
          played: p.id === seat,
          view: {
            enabled: !seat && !current,
            title: seat ? lockedWhy : current ? `现在就是${p.name}的视角` : `以 ${p.name} 的视角观看：只显示这名玩家能看到的内容`,
          },
          takeover: { enabled: take.ok, title: take.ok ? `接管 ${p.seat} · ${p.name}` : take.reason },
        };
      });
    const bar = takeoverBar(session);
    const release = bar
      ? { enabled: bar.canRelease, title: bar.releaseTitle, label: `退出接管（${bar.label}）` }
      : null;
    // one sentence for the whole 接管 section when nothing can be taken over (so a phone, which has no hover, reads why)
    const note = seat || players.some((p) => p.takeover.enabled) ? "" : takeoverAvailability(session, players.length ? players[0].id : "").reason;
    return { chip, god, players, release, takeoverNote: note };
  }

  // -------------------------------------------------------------------------
  // v0.13.1: the soft pause (G1-G9, contract §12.2)
  // -------------------------------------------------------------------------

  /**
   * The soft pause of the live game as {active, phase, inFlight}.  `phase` is "draining" (the model calls that were in
   * flight are still being finished: `inFlight` of them) or "held" (every agent waits in front of the gate), null
   * without a soft pause.  It only exists while the game runs (G6): `status` itself stays "running".
   */
  function softPauseOf(session) {
    const sp = session && session.soft_pause;
    if (!sp || (sp.state !== "draining" && sp.state !== "held") || session.status !== "running") {
      return { active: false, phase: null, inFlight: 0 };
    }
    return { active: true, phase: sp.state, inFlight: sp.state === "held" ? 0 : Math.max(0, Math.round(num(sp.in_flight, 0))) };
  }

  /**
   * Does the game clock run?  The page counts request / floor deadlines down locally only while it does.  The
   * server says so (`clock_running`: `status` stays "running" during a soft pause); an older server that does not
   * is judged by its status.
   */
  function clockRunning(session) {
    if (!session) return false;
    if (typeof session.clock_running === "boolean") return session.clock_running;
    return session.status === "running" && !softPauseOf(session).active;
  }

  /** "⏳ 收尾中（还有 2 个回答）" while the calls in flight finish, "⏸ agent 已暂停" once all have stopped (G3). */
  function softPauseText(sp) {
    if (!sp.active) return "";
    if (sp.phase === "held") return "⏸ agent 已暂停";
    return sp.inFlight > 0 ? `⏳ 收尾中（还有 ${sp.inFlight} 个回答）` : "⏳ 收尾中";
  }

  /**
   * What the soft-pause part of the takeover bar shows (G1, G3, G5):
   *   action  "pause" (the button says 「⏸ 暂停 agent」) / "resume" (「▶ 继续」, also while the pause is still draining)
   *   enabled the button works: pausing only while the game runs, resuming always
   *   text    the state ("" without a soft pause)       showNow  the 「立即暂停」 link (only while draining)
   */
  function softPauseControl(session) {
    const sp = softPauseOf(session);
    const status = (session && session.status) || "idle";
    if (sp.active) {
      const draining = sp.phase === "draining";
      return {
        phase: sp.phase,
        inFlight: sp.inFlight,
        text: softPauseText(sp),
        statusTitle: draining
          ? `还有 ${sp.inFlight} 个模型回答没有收完：它们会照常完成并执行，之后所有 agent 暂停。游戏计时已经冻结，你不会超时。`
          : "所有 agent 已暂停，游戏计时已冻结：你可以继续阅读、发言和作答；agent 要等你点「继续」才会处理。",
        action: "resume",
        label: "▶ 继续",
        enabled: true,
        title: draining ? "继续：取消这次暂停，agent 接着行动，计时恢复" : "继续：agent 先合并你这段时间的发言和作答，再接着行动；计时恢复",
        showNow: draining,
        nowTitle: "立即暂停：取消仍在途的模型调用（已花的 token 作废），所有 agent 马上停下；等同顶栏的「暂停」",
      };
    }
    const running = status === "running";
    return {
      phase: null,
      inFlight: 0,
      text: "",
      statusTitle: "",
      action: "pause",
      label: "⏸ 暂停 agent",
      enabled: running,
      title: running
        ? "暂停 agent：已发出的回答会照常完成，然后所有 agent（含主持人）停下；游戏计时冻结，你可以继续阅读、发言和作答。点「继续」让它们接着行动"
        : `${notRunningText(status)}，现在不能暂停 agent（对局运行中才可以）`,
      showNow: false,
      nowTitle: "",
    };
  }

  /** The bar that replaces the viewpoint bar while a seat is played: "🎮 正在扮演 3 · Carol  [⏸ 暂停 agent]  [退出接管]". */
  function takeoverBar(session) {
    const seat = takeoverSeat(session);
    if (!seat) return null;
    const running = Boolean(session) && session.status === "running";
    const label = seatLabel(session, seat);
    return {
      seat,
      label,
      text: `🎮 正在扮演 ${label}`,
      canRelease: !running,
      releaseTitle: running
        ? "对局进行中不能退出接管：请先点「暂停」"
        : "退出接管：这个座位交还给它的 agent（你不会再看到锁定的视角）",
      softPause: softPauseControl(session), // (v0.13.1) the button, its state and the 「立即暂停」 link
    };
  }

  /** The name of the viewpoint as the feed header says it: "全局视角" or the player's nickname. */
  function viewpointLabel(session, viewpoint) {
    const vp = normalizeViewpoint(session, viewpoint);
    if (vp === GOD) return "全局视角";
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
   *   not dealt yet   -> null                        (v0.14, H52: before the game starts there is no identity tag at all - not even the "?")
   * A scenario without private identity tags falls back to the first non-status tag.
   */
  function mainTagInfo(session, viewpoint, participant) {
    if (!isDealt(session)) return null;
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

  /** Small corner badges: the visible highlight tags (sheriff, …) that are not the identity itself (none before the deal, H52). */
  function badgeTags(session, viewpoint, participant) {
    if (!isDealt(session)) return [];
    const sets = tagSets(session);
    return visibleTags(session, viewpoint, participant)
      .filter((t) => sets.highlight.has(t) && !isIdentityTag(sets, t))
      .map((t) => ({ tag: t, text: Array.from(t)[0] || t, variant: tagVariant(session, t) }));
  }

  // -------------------------------------------------------------------------
  // player cards
  // -------------------------------------------------------------------------

  /** thinking / acting / sleeping / held, only for god or the viewed player ("" otherwise). */
  function stateBadge(viewpoint, actorId, state) {
    if (!STATE_LABELS[state]) return "";
    if (viewpoint !== GOD && viewpoint !== actorId) return "";
    return state;
  }

  /**
   * The state a card (or the moderator chip) shows.  The real one wherever the viewpoint may see it.  Elsewhere - the
   * locked viewpoint of the seat a person plays hides every other agent's state (D4: who is thinking at night is a
   * secret) - a soft pause that has stopped everybody (G3) puts the same "⏸ 暂停中" on every agent's card: that is
   * the state of the whole game, so it gives nothing away about who was awake.
   */
  function cardState(session, viewpoint, actorId, state) {
    const real = stateBadge(viewpoint, actorId, state);
    if (real) return real;
    if (STATE_LABELS[state] && state !== "human" && softPauseOf(session).phase === "held") return "held";
    return "";
  }

  /** Hover text of a state: "思考中" / "执行中" / "暂停中…" / "休眠：等待新消息" / "休眠：定时醒来：剩余 12 秒". */
  function stateTitle(state, sleepUntil, clockNow) {
    if (!STATE_LABELS[state]) return "";
    if (state === "held") return "暂停中：软暂停，等你点「继续」";
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

  /**
   * Have the identities been dealt (H52)?  They are dealt when the game starts: until then (`dealt === false`) nothing about an identity
   * is shown anywhere - no identity tag, no grey "?", whatever the viewpoint (the global one included).  A server that does not say
   * (an older one) has dealt: its games always show them.
   */
  function isDealt(session) {
    return !session || session.dealt !== false;
  }

  /**
   * One player card. ctx: {clockNow} (the client's game-clock estimate, for the sleep timer).
   * `main` is the identity tag ({text, known, variant}) - `null` before the deal (H52): the card then has no identity tag at all, not even "?".
   */
  function tileModel(session, viewpoint, participant, ctx) {
    const id = participant.id;
    const main = mainTagInfo(session, viewpoint, participant); // (null before the deal)
    const state = cardState(session, viewpoint, id, participant.state);
    const clockNow = ctx && ctx.clockNow;
    const sTitle = stateTitle(state, participant.sleep_until, clockNow);
    return {
      id,
      seat: participant.seat,
      name: participant.name,
      avatar: participant.avatar || null,
      // v0.14 (H22): the bot in the seat and how far its initialization is (✔ ⚠ ✖ ⏳ next to the name); "" / null for a game of the old format
      botId: participant.bot_id || "",
      initStatus: participant.init_status || null,
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
    const state = cardState(session, viewpoint, MODERATOR_ID, m.state);
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
    if (!isDealt(session)) return null; // (H52) before the deal there is nothing to count - and nothing about the identities is shown
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
          detail = `持麦：${shown}${names.length > 2 ? ` 等${names.length}人` : ""}${f.untimed ? " · 不限时" : ""}`; // (v0.13.1, G10)
        }
        return {
          kind: "turn",
          icon: "🎤",
          text: f.paused ? "轮流（暂停）" : "轮流发言",
          detail,
          untimed: Boolean(f.untimed),
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
          detail: active ? `持麦：${displayName(session, active.holder)}${f.untimed ? " · 不限时" : ""}` : "",
          untimed: Boolean(f.untimed),
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
    // v0.13: for the seat a person plays, "muted" / "your turn" come from the server's seat state (F12, F18)
    const mine = playedChannelState(session, viewpoint, channel.id);
    const summary = personalFloorSummary(floorSummary(session, channel), mine);
    const attention = mine !== null && list(session.takeover.attention).includes(channel.id);
    if (dm) {
      const others = dmCounterparts(session, viewpoint, channel);
      return {
        id: channel.id,
        kind: "dm",
        title: others.map((id) => displayName(session, id)).join("、"),
        avatarId: others[0],
        glyph: "",
        summary,
        attention,
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
      attention,
      memberCount: list(channel.members).length,
      owner: null,
    };
  }

  /** The server's state of a channel for the seat a person plays, when `viewpoint` is that seat (else null). */
  function playedChannelState(session, viewpoint, channelId) {
    const seat = takeoverSeat(session);
    if (!seat || viewpoint !== seat) return null;
    return (session.takeover.channels && session.takeover.channels[channelId]) || null;
  }

  /**
   * The floor chip of a channel for the seat a person plays: "🔇 禁言中" whenever the seat cannot write there
   * (closed, listen-only, frozen, read-only, not its turn, quota used up: the server decides), the usual
   * description of the channel after it; "🎤 轮到你" while it holds the floor.
   */
  function personalFloorSummary(base, state) {
    if (!state) return base;
    if (!state.can_write) {
      const rest = base.kind === "closed" ? "" : [base.text, base.detail].filter(Boolean).join(" · ");
      return { kind: "muted", icon: "🔇", text: "禁言中", detail: rest };
    }
    if (state.floor && state.floor.mine) {
      const left = state.floor.messages_left;
      return {
        kind: "mine",
        icon: "🎤",
        text: "轮到你发言",
        // (v0.13.1, G10) a public floor of the person who plays the seat has no deadline
        detail: [left === null || left === undefined ? "" : `还可发 ${left} 条`, state.floor.untimed ? "不限时" : ""]
          .filter(Boolean)
          .join(" · "),
      };
    }
    return base;
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
      // v0.13 (F17): what the person who plays a recipient may do with the card (null: nothing)
      action: requestAction(session, viewpoint, request),
    };
  }

  /**
   * The buttons / input of a request card for the seat a person plays (F17): a choice shows its options (the
   * current answer marked, a click changes it - an open request can be answered again until it closes), a
   * text request an input.  Only a recipient, only while the request is open; the card is clickable while the
   * game runs and shows why not otherwise (a paused game freezes the clock of the request as well).
   */
  function requestAction(session, viewpoint, request) {
    const seat = takeoverSeat(session);
    if (!seat || viewpoint !== seat || !request || request.status !== "open") return null;
    if (!list(request.to).includes(seat)) return null;
    const row = list(session.takeover.requests).find((r) => r.id === request.id) || null;
    let mine = row && row.my_answer ? row.my_answer : null;
    if (!mine) {
      const own = list(request.answers).find((a) => a.player_id === seat && !a.defaulted);
      if (own) mine = { value: String(own.value), label: own.label || String(own.value), reason: own.reason || "" };
    }
    const status = (session && session.status) || "idle";
    const enabled = status === "running";
    return {
      kind: request.kind,
      enabled,
      disabledReason: enabled ? "" : `${notRunningText(status)}，继续后才能作答（请求的计时也已暂停）`,
      answered: mine !== null,
      myAnswer: mine,
      maxChars: row && row.max_chars ? row.max_chars : null,
      options: list(request.options).map((o) => ({
        id: o.id,
        label: o.label || o.id,
        selected: mine !== null && String(mine.value) === String(o.id),
      })),
    };
  }

  /**
   * v0.13.1 (G45): what a choice card says about the pick that is only on the page.  A click on an option merely
   * PICKS it (highlighted, nothing is sent); the card's 「提交」 button sends the pick - so a stray click never casts a
   * vote.  ``picked`` is the option id chosen but not submitted (null: none, or not an option of this request);
   * ``dirty`` = it differs from the answer that was already submitted; ``canSubmit`` = the card is clickable and there
   * is something new to send; ``kind`` = what the note under the options says: "none", "new" (只选了、还没答过) or
   * "change" (答过了，改选中).
   */
  function requestPick(action, picked) {
    const none = {
      picked: null,
      pickedLabel: "",
      currentLabel: "",
      dirty: false,
      canSubmit: false,
      submitLabel: "提交",
      kind: "none",
    };
    if (!action || action.kind === "text") return none;
    const submitLabel = action.answered ? "修改回答" : "提交";
    const option =
      picked === null || picked === undefined ? null : list(action.options).find((o) => String(o.id) === String(picked));
    if (!option) return { ...none, submitLabel };
    const dirty = !(action.answered && action.myAnswer && String(action.myAnswer.value) === String(option.id));
    return {
      picked: String(option.id),
      pickedLabel: option.label,
      currentLabel: action.answered && action.myAnswer ? action.myAnswer.label : "",
      dirty,
      canSubmit: Boolean(action.enabled) && dirty,
      submitLabel,
      kind: !dirty ? "none" : action.answered ? "change" : "new",
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
  // v0.13: the input box of a played seat (F13-F16)
  // -------------------------------------------------------------------------

  /** Characters as the server counts them (code points of the trimmed text, like the gate's limit of one message). */
  function countChars(text) {
    return Array.from(String(text === undefined || text === null ? "" : text).trim()).length;
  }

  /** "1:05" for the seconds left of a floor / request (clamped at 0). */
  function clockText(seconds) {
    const s = Math.max(0, Math.ceil(num(seconds, 0)));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  /**
   * Seconds left of the seat's token at the client's clock estimate (frozen value while the floor is paused);
   * null for a token without a deadline (`untimed`, v0.13.1: the person's turn in a public channel).
   */
  function floorSecondsLeft(floor, clockNow) {
    if (!floor || !floor.mine || floor.untimed) return null;
    if (floor.deadline_clock !== null && floor.deadline_clock !== undefined) {
      return Math.max(0, num(floor.deadline_clock) - num(clockNow, 0));
    }
    return floor.seconds_left === null || floor.seconds_left === undefined ? null : Math.max(0, num(floor.seconds_left));
  }

  /**
   * One line under the input: what the seat has left of its floor ("轮到你发言 · 还可发 2 条 · 单条 ≤ 300 字 ·
   * 剩余 1:05") or why it cannot speak.  The reasons are written out (a phone has no hover).
   */
  function composerHint(session, state, clockNow) {
    const status = (session && session.status) || "idle";
    if (status === "idle") return "对局还没有开始：开始后才能发言，现在可以先写草稿。";
    if (status === "finished") return "对局已结束。";
    if (status === "paused") return "对局已暂停：发言权和请求的计时都已冻结，继续后才能发送。";
    if (!state) return "频道状态同步中……";
    if (!state.can_write) return state.why_not || "现在不能在这个频道发言。";
    const f = state.floor;
    if (f && f.mine) {
      const parts = ["轮到你发言"];
      if (f.messages_left !== null && f.messages_left !== undefined) parts.push(`还可发 ${f.messages_left} 条`);
      if (state.max_chars) parts.push(`单条 ≤ ${state.max_chars} 字`);
      if (f.untimed) {
        parts.push("不限时"); // (v0.13.1, G10) no countdown: the turn lasts until the person ends it
      } else {
        const left = floorSecondsLeft(f, clockNow);
        // (a soft pause stops the game clock: the countdown stands still, G2)
        if (left !== null) parts.push(`剩余 ${clockText(left)}${softPauseOf(session).active ? "（计时已冻结）" : ""}`);
      }
      return parts.join(" · ");
    }
    if (f && f.kind === "free" && f.messages_left !== null && f.messages_left !== undefined) {
      return `这次讨论你还可发 ${f.messages_left} 条`;
    }
    return "";
  }

  /**
   * Everything the input box of the played seat shows for a channel (F13-F16, v0.14 H16):
   *   the send button  发送 (may write) / 禁言中 (may not; the reason on hover) / 已暂停 / 未开始 / 已结束
   *   the counter      "23/300" (the server's `max_chars`) - over the limit it turns red and the send button is off: a speech
   *                    that is too long is sent back whole (`TOO_LONG`), never cut, so the person shortens it first
   *   结束发言          only while the seat holds that channel's floor
   *   技能              greyed without a usable ability
   * Typing is always possible: only the buttons depend on the state.
   */
  function composerModel(session, channelId, text, ctx) {
    const seat = takeoverSeat(session);
    if (!seat || !channelId) return { visible: false };
    const t = session.takeover;
    const state = t.channels[channelId] || null;
    const status = session.status || "idle";
    const count = countChars(text);
    const max = state && state.max_chars ? state.max_chars : null;
    const over = max ? Math.max(0, count - max) : 0;
    const overText = over > 0 ? `这条发言 ${count} 个字，超过了单条上限 ${max} 字：请删减 ${over} 字后再发送` : "";
    let mode;
    let label;
    let reason;
    if (status === "idle") {
      mode = "idle";
      label = "未开始";
      reason = "对局还没有开始：开始后才能发言";
    } else if (status === "finished") {
      mode = "finished";
      label = "已结束";
      reason = "对局已结束";
    } else if (status === "paused") {
      mode = "paused";
      label = "已暂停";
      reason = "对局已暂停：继续后才能发送";
    } else if (!state) {
      mode = "muted";
      label = "禁言中";
      reason = "频道状态同步中……";
    } else if (!state.can_write) {
      mode = "muted";
      label = "禁言中";
      reason = state.why_not || "现在不能在这个频道发言";
    } else {
      mode = "send";
      label = "发送";
      reason = over > 0 ? overText : count > 0 ? "发送（Enter）；Shift+Enter 换行" : "先写点什么";
    }
    const running = status === "running";
    const showPass = Boolean(state && state.floor && state.floor.mine);
    const abilities = list(t.abilities);
    let abilityReason = "技能：释放前会让你确认";
    if (!running) abilityReason = `${notRunningText(status)}，现在不能使用技能`;
    else if (!abilities.length) abilityReason = "现在没有可用的技能（技能由主持人在特定环节启用）";
    return {
      visible: true,
      seat,
      channelId,
      mode,
      label,
      reason,
      sendEnabled: mode === "send" && count > 0 && over === 0,
      count,
      max,
      over,
      overText,
      showPass,
      canPass: showPass && running && Boolean(state && state.can_pass),
      passTitle: showPass
        ? running
          ? state.can_pass
            ? "结束发言：把发言权交给下一位"
            : "现在不能结束发言（发言权已暂停或频道被冻结）"
          : `${notRunningText(status)}，现在不能结束发言`
        : "",
      abilityEnabled: running && abilities.length > 0,
      abilityReason,
      // (the sentence under the box: while a text that is too long is typed it says so first - a phone has no hover)
      hint: mode === "send" && over > 0 ? overText : composerHint(session, state, ctx && ctx.clockNow),
    };
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
    // v0.13: a replayed game is not the live one - none of the live game's controls applies to it
    if (session && session.replay) return { start: false, pause: false, resume: false };
    const st = (session && session.status) || "idle";
    return { start: st === "idle", pause: st === "running", resume: st === "paused" || st === "finished" };
  }

  function statusDotClass(session) {
    const st = (session && session.status) || "idle";
    // (v0.13.1) a soft pause that has stopped every agent: the game runs, but nothing moves - the amber dot
    if (st === "running") return softPauseOf(session).phase === "held" ? "paused" : "active";
    if (st === "paused") return "paused";
    if (st === "finished") return "finished";
    return "idle";
  }

  // -------------------------------------------------------------------------
  // v0.14 (H26): the system status label - the first thing of the second row of the top bar (it replaces the status text of v0.13)
  // -------------------------------------------------------------------------

  /** `system_status.reason` of a pause, as the server words it (the same table as `PAUSE_TEXTS` of app/game/bots_runtime.py). */
  const PAUSE_REASON_TEXT = {
    user: "手动暂停",
    hold: "主持人暂停了对局",
    stall: "对局长时间没有进展，已暂停",
    error: "运行出错，已暂停",
    budget: "已达到预算上限，已暂停",
  };

  /** One sentence per code for the hover text of the label (what it means and what to do). */
  const SYSTEM_STATUS_TIPS = {
    uninitialized: "还有 bot 没有初始化好（卡片上是 ○ ⚠ ✖）：先点「⚙ 全局初始化」，之后才能开局",
    initializing: "bot 正在读官方契约和规则、写自己的笔记和打法；期间不能开局",
    before_game: "一切就绪：点「开始」发牌并开局（身份在点击「开始」时才发）",
    in_game: "对局正在进行",
    paused: "对局已暂停：点「继续」接着玩",
    finished: "对局已结束：可以复盘，也可以点「继续」在终局后继续",
    reviewing: "bot 正在复盘本局（每个最多 60 秒）",
    replay: "正在重放一局保存的对局",
  };

  /**
   * What the client can tell about the label from what it holds - the server's own rules (H26: initializing / reviewing, then in game /
   * paused / finished, then uninitialized, then before the game).  It stands in when the server sent no `system_status` (an older one) and
   * corrects one that went stale between two events.
   */
  function deriveSystemStatus(session) {
    const s = session || {};
    const bots = s.bots || {};
    if (bots.init_job) return { code: "initializing", text: "初始化中", reason: "" };
    if (s.review && s.review.running) return { code: "reviewing", text: "复盘中", reason: "" };
    switch (s.status) {
      case "running":
        return { code: "in_game", text: softPauseOf(s).active ? "对局中 · agent 已暂停" : "对局中", reason: "" };
      case "paused": {
        let reason = PAUSE_REASON_TEXT[s.pause_reason] || "已暂停";
        if (s.hold_reason) reason = `${reason}：${s.hold_reason}`;
        return { code: "paused", text: "已暂停", reason };
      }
      case "finished":
        return { code: "finished", text: "已结束", reason: "" };
      default:
        return list(bots.bots).some((b) => b && b.init && b.init.status !== "ready")
          ? { code: "uninitialized", text: "未初始化", reason: "" }
          : { code: "before_game", text: "对局前", reason: "" };
    }
  }

  /**
   * Is the label the server sent still the one that fits the status this page holds?  `game.status` (and the HTTP answer of a control) moves the status a moment
   * before the next `session.updated` brings the label: until then the old label would say something the status contradicts.  An initialization only runs for a
   * game that has not started or is over, a review only for one that is over - the server's word for those is trusted as long as the status allows them.
   */
  function systemStatusFits(code, session) {
    const s = session || {};
    switch (code) {
      case "in_game":
        return s.status === "running";
      case "paused":
        return s.status === "paused";
      case "finished":
        return s.status === "finished";
      case "uninitialized":
      case "before_game":
        return (s.status || "idle") === "idle";
      case "initializing":
        return (s.status || "idle") === "idle" || s.status === "finished";
      case "reviewing":
        return s.status === "finished";
      default:
        return false;
    }
  }

  /**
   * The label of the second row (H26, contract §13.8): `{code, text, reason, label, dot, title}` - `label` is what is written ("对局前", "对局中 · agent 已暂停",
   * "已暂停 · 手动暂停"), `dot` the class of its dot, `title` the hover sentence.  It reads `session.system_status`; the status text the server used
   * to send (`active_status_text`) is merged into it (it is the hover text of a finished / paused game).  A replay on screen says so instead.
   */
  function systemStatus(session) {
    const s = session || {};
    if (s.replay) {
      const text = s.active_status_text || "重放中";
      return { code: "replay", text, reason: "", label: text, dot: "active", title: SYSTEM_STATUS_TIPS.replay };
    }
    // the server's label is the label (H26: it decides the order of priority) - unless the status of the page has moved past it (see systemStatusFits) or the server
    // sent none (an older one): then the page works it out from what it holds (the same rules; the running initialization is `bots.init_job`, a running review `review.running`)
    const derived = deriveSystemStatus(s);
    const given = s.system_status && s.system_status.code ? s.system_status : null;
    const base = given && systemStatusFits(given.code, s) ? { code: given.code, text: given.text, reason: given.reason || "" } : { ...derived };
    if (!base.text) base.text = derived.text;
    // the soft pause (G2-G3): the game runs and nothing moves.  Draining says how many answers are still on their way.
    if (base.code === "in_game") {
      const sp = softPauseOf(s);
      if (sp.active) base.text = sp.phase === "held" ? "对局中 · agent 已暂停" : `对局中 · 收尾中${sp.inFlight > 0 ? `（还有 ${sp.inFlight} 个回答）` : ""}`;
    }
    // a pause says why: "已暂停 · 手动暂停" (the reason's own "，已暂停" at its end would only say it twice)
    const shortReason = base.reason.replace(/[，,]\s*已暂停$/, "");
    const label = base.code === "paused" && shortReason && shortReason !== base.text ? `${base.text} · ${shortReason}` : base.text;
    const dot =
      base.code === "uninitialized"
        ? "uninit"
        : base.code === "initializing" || base.code === "reviewing"
          ? "busy"
          : base.code === "before_game"
            ? "idle"
            : statusDotClass(s);
    // the hover sentence: what the label means and what to do; the full reason of a pause; and what the server's own status text adds (who won, ...)
    const lines = [SYSTEM_STATUS_TIPS[base.code] || ""];
    if (base.code === "paused" && base.reason) lines.push(base.reason);
    const extra = String(s.active_status_text || "");
    if (extra && extra !== label && extra !== base.reason && extra !== "对局进行中" && !extra.startsWith("未开始")) lines.push(extra);
    return { code: base.code, text: base.text, reason: base.reason, label, dot, title: lines.filter(Boolean).join("\n") };
  }

  // -------------------------------------------------------------------------
  // v0.14 (H29): moving the cards - the line-up (bot ids in seat order) while the game has not started
  // -------------------------------------------------------------------------

  /**
   * May the cards be dragged?  Only while the game has not started (the line-up is the seating of an idle game) and while nobody plays a seat (the seat a person
   * plays is a seat, not a bot: the bots would move under them).  `reason` is the sentence a disabled card (and the toast of a refused drag) says.
   */
  function seatDragState(session) {
    const s = session || {};
    if (s.replay) return { enabled: false, reason: "重放中不能调整座位" };
    if ((s.status || "idle") !== "idle") return { enabled: false, reason: "对局已经开始，不能再调整座位" };
    if (takeoverSeat(s)) return { enabled: false, reason: "接管期间不能调整座位：请先退出接管" };
    const lineup = list(s.bots && s.bots.lineup);
    const seats = list(s.participants).length;
    if (!seats || lineup.length !== seats) return { enabled: false, reason: "这局没有 bot 阵容（旧版存档），不能调整座位" };
    return { enabled: true, reason: "拖动卡片调整座位（触屏：长按后拖动；键盘：Alt+←/→）" };
  }

  /** The line-up after the bot at index `from` moved to index `to` (the bots in between shift by one); null when that is no move. */
  function moveInLineup(order, from, to) {
    const out = list(order).slice();
    const ok = (i) => Number.isInteger(i) && i >= 0 && i < out.length;
    if (!ok(from) || !ok(to) || from === to) return null;
    const [moved] = out.splice(from, 1);
    out.splice(to, 0, moved);
    return out;
  }

  /**
   * The card under a point: the index (in seat order) of the first of `rects` ({left, top, right, bottom}) that holds (x, y), grown by `slack` px
   * on every side (the gap between two cards belongs to the nearer one); -1 when the point is over no card.
   */
  function seatIndexAt(rects, x, y, slack = 4) {
    let best = -1;
    let bestDistance = Infinity;
    list(rects).forEach((r, i) => {
      if (!r) return;
      const dx = x < r.left ? r.left - x : x > r.right ? x - r.right : 0;
      const dy = y < r.top ? r.top - y : y > r.bottom ? y - r.bottom : 0;
      const distance = Math.hypot(dx, dy);
      if (distance <= slack && distance < bestDistance) {
        best = i;
        bestDistance = distance;
      }
    });
    return best;
  }

  /** Where the keys Alt+← / Alt+→ send the bot at index `index` (one seat to the left / right; null at the ends). */
  function seatKeyTarget(index, key, count) {
    const to = key === "ArrowLeft" ? index - 1 : key === "ArrowRight" ? index + 1 : NaN;
    return Number.isInteger(to) && to >= 0 && to < count ? to : null;
  }

  // -------------------------------------------------------------------------
  // v0.14 (H25): the start check - what a 409 NOT_READY says, grouped by player
  // -------------------------------------------------------------------------

  const FIX_LABELS = {
    edit_core: "去编辑底层设定",
    import_core: "去导入底层设定",
    init: "去初始化",
    bind_model: "去绑定模型",
    import_contract: "去导入契约",
    import_rules: "去导入规则",
  };

  /**
   * The `problems` of a refused start (`Problem[]`, §13.7) as the dialog lists them: one group per thing that is not ready, in this order -
   * the official contract, the scenario, the players (seat order; a player's own problems and the missing model of his seat are ONE group), the
   * moderator, anything else.  Every item has the sentence of the server and, when the page knows how to fix it, the button (`fix` + `fixLabel`).
   * `botId` is the bot of a player's group (null for the others); `focusId` is what the button hands to `window.Bots.focus(focusId, fix)`: the bot's id for a
   * player, "moderator" for the moderator's missing model, "" for the contract / the scenario / the game (they have no bot to go to).
   *   {groups: [{key, kind, title, seat, botId, focusId, items: [{code, message, fix, fixLabel}]}], total, initCount}
   */
  function startCheckModel(session, problems) {
    const groups = new Map();
    const seatNo = (p) => {
      const part = p.seat ? participantOf(session, p.seat) : null;
      return part ? num(part.seat, 999) : 999;
    };
    const order = { contract: 0, scenario: 1, player: 2, moderator: 3, other: 4 };
    for (const raw of list(problems)) {
      if (!raw || typeof raw !== "object") continue;
      const scope = String(raw.scope || "");
      let key;
      let kind;
      let title;
      if (scope === "contract") [key, kind, title] = ["contract", "contract", "官方契约"];
      else if (scope === "scenario") [key, kind, title] = ["scenario", "scenario", "剧本"];
      else if (raw.bot_id || raw.seat) {
        key = `player:${raw.seat || raw.bot_id}`;
        kind = "player";
        const part = raw.seat ? participantOf(session, raw.seat) : null;
        const name = raw.name || (part ? part.name : "") || String(raw.seat || raw.bot_id);
        title = part ? `${part.seat} 号 ${name}` : name;
      } else if (scope === "binding" || raw.name === "主持人") [key, kind, title] = ["moderator", "moderator", raw.name || "主持人"];
      else [key, kind, title] = [`other:${scope || raw.name || "?"}`, "other", raw.name || (scope === "game" ? "对局" : scope || "其他")];
      let group = groups.get(key);
      if (!group) {
        group = { key, kind, title, seat: raw.seat || null, botId: raw.bot_id || null, focusId: kind === "moderator" ? MODERATOR_ID : "", items: [], _no: seatNo(raw) };
        groups.set(key, group);
      }
      if (!group.botId && raw.bot_id) group.botId = raw.bot_id;
      if (group.kind === "player" && group.botId) group.focusId = group.botId;
      for (const item of list(raw.items)) {
        if (!item || typeof item !== "object") continue;
        const fix = String(item.fix || "");
        group.items.push({ code: String(item.code || ""), message: String(item.message || ""), fix, fixLabel: FIX_LABELS[fix] || "" });
      }
    }
    const out = [...groups.values()]
      .filter((g) => g.items.length)
      .sort((a, b) => order[a.kind] - order[b.kind] || a._no - b._no)
      .map(({ _no, ...g }) => g);
    const all = out.flatMap((g) => g.items);
    return { groups: out, total: all.length, initCount: all.filter((i) => i.fix === "init").length };
  }

  // -------------------------------------------------------------------------
  // v0.14 (H31-H33): the question at the end of a game - should the bots review it?
  // -------------------------------------------------------------------------

  /**
   * Does the question "这局结束了，是否让 bot 复盘？" come up for this session (contract §13.10)?  The game is over (not a replay), can be reviewed
   * (`supported`: it has bots; `available`), no review runs and nobody was reviewed yet (`done` is empty), and the user did not say 暂不 for this game
   * (`dismissed`: the object kept in `localStorage["arena_review_dismissed_v1"]`, `{session_id: true}`).
   */
  function reviewPrompt(session, dismissed) {
    const s = session || {};
    const r = s.review || {};
    if (s.replay || s.status !== "finished") return { show: false, why: "not finished" };
    if (r.supported === false) return { show: false, why: "unsupported" };
    if (r.available === false) return { show: false, why: "unavailable" };
    if (r.running) return { show: false, why: "running" };
    if (list(r.done).length) return { show: false, why: "reviewed" };
    if (dismissed && dismissed[s.session_id]) return { show: false, why: "dismissed" };
    return { show: true, why: "" };
  }

  /**
   * The rows of the review dialog: the bots at the table in seat order, `human` = a person played that seat in this game ("有人类代打"; `humanSeats`: seat ids
   * from the game's `human_spans` and what the page saw), `done` = reviewed already, `checked` = selected (all of them, except the reviewed ones), `learnStyle` =
   * the switch 学习接管者的风格 (on by default; only a human seat has one).
   */
  function reviewRows(session, humanSeats, choices) {
    const humans = new Set(list(humanSeats));
    const chosen = choices || {};
    const done = new Set(list(session && session.review && session.review.done));
    return list(session && session.participants)
      .slice()
      .sort((a, b) => num(a.seat) - num(b.seat))
      .map((p) => {
        const c = chosen[p.id] || {};
        const isDone = done.has(p.id);
        const human = humans.has(p.id);
        return {
          id: p.id,
          seat: p.seat,
          name: p.name,
          done: isDone,
          human,
          checked: !isDone && c.checked !== false,
          learnStyle: human && c.learnStyle !== false,
        };
      });
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
        // (the moderator's own sentence may end with a full stop: the banner puts one of its own after it)
        const held = endStopsOff(s.hold_reason);
        const reason = held ? `：${held}` : "";
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
    // v0.14 (§13.8): a review is queued before it runs, and one that ran into its 60 seconds is finished (it keeps what it had written)
    const underway = (p) => Boolean(p) && (p.state === "running" || p.state === "queued");
    const running = Boolean(r.running) || Object.values(progress).some(underway);
    const finished = s.status === "finished";
    const stateOf = (id) => {
      const p = progress[id];
      if (done.has(id)) return { state: "done", message: (p && p.message) || "", secondsLeft: null };
      if (underway(p)) return { state: "running", message: p.message || "", secondsLeft: p.seconds_left === undefined ? null : p.seconds_left };
      if (p && p.state === "error") return { state: "error", message: p.message || "", secondsLeft: null };
      return { state: "none", message: "", secondsLeft: null };
    };
    const doneCount = ids.filter((id) => done.has(id)).length;
    // `supported` is false for a game of the old format (v5): it has no bots, so there is nothing to review (the buttons say so)
    const supported = r.supported !== false;
    return {
      visible: finished,
      available: r.available !== false && supported,
      supported,
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
    normalizeViewpoint,
    takeoverSeat,
    seatLabel,
    takeoverAvailability,
    viewpointChip,
    viewpointMenu,
    takeoverBar,
    softPauseOf,
    softPauseControl,
    clockRunning,
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
    cardState,
    stateTitle,
    tileModel,
    moderatorTile,
    boardTiles,
    shortFactionName,
    boardSummary,
    floorSummary,
    personalFloorSummary,
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
    requestAction,
    requestPick,
    feedEntries,
    countChars,
    clockText,
    floorSecondsLeft,
    composerHint,
    composerModel,
    notRunningText,
    requestHints,
    controlState,
    statusDotClass,
    systemStatus,
    isDealt,
    seatDragState,
    moveInLineup,
    seatIndexAt,
    seatKeyTarget,
    startCheckModel,
    FIX_LABELS,
    reviewPrompt,
    reviewRows,
    budgetView,
    statusBanners,
    actorMetrics,
    reviewView,
  };
});
