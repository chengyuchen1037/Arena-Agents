# UI Guide

This guide describes the **v0.12.1** web UI. The UI itself is in Chinese; the Chinese label of a
control is given in italics where it helps you find it. Everything below can be tried without an API
key: start the server with `ARENA_DEMO=1` (scripted bots) or run the UI mock (see
[Working on the UI](#working-on-the-ui)).

![The main view in the god view](images/v0121-main-view.png)

On a phone the same game looks like this (mini cards, bottom tab bar, the moderator's own random avatar):

<img src="images/v0121-phone.png" alt="Phone portrait" width="260">

## Platforms and layouts

The page adapts itself to the device. A tiny script in `<head>` (`static/js/platform.js`) runs
before the first paint and writes these attributes on `<html>`; the CSS and the scripts only ever
look at them:

| Attribute | Values | Decided by |
|---|---|---|
| `data-platform` | `desktop` · `tablet` · `phone` | `phone`: narrower than 768 px, or a phone user agent held sideways (short side ≤ 500 px); `tablet`: a touch / mobile device (iPad, Android without `Mobile`, iPadOS reporting as a Mac with touch points, `(pointer: coarse)`, `userAgentData.mobile`) up to 1439 px wide; `desktop` otherwise |
| `data-layout` | `wide` · `compact` · `phone` | `phone` below 768 px (also a phone held sideways), `compact` up to 1023 px (up to 1024 px on a touch tablet), `wide` otherwise |
| `data-input` | `touch` · `mouse` | `(pointer: coarse)`, a mobile user agent, or an iPad posing as a Mac |
| `data-orientation` | `portrait` · `landscape` | viewport (taller than wide = portrait) |
| `data-os` | `ios` · `android` · `other` | user agent (informational: the stylesheet does not use it) |

The values are recomputed on resize and on rotation (no reload). For testing, the URL accepts
`?platform=phone|tablet|desktop` and `?input=touch|mouse`; `?nosse=1` switches the live event
stream off (no live updates then: the page shows what `/api/state` returned and only the answers to
your own clicks refresh it).

| Layout | Typical screens | What changes |
|---|---|---|
| **wide** | desktop ≥ 1024 px (touch tablets wider than 1024 px) | the 12 player cards sit in one row with a summary strip below; the channel column shows names and floor status; the detail card floats next to the clicked player |
| **compact** | touch tablets up to 1024 px, narrow windows (768 – 1023 px) | the cards are 6 × 2; the channel column shrinks to an **avatar rail** (names appear on hover / long-press; the rail is always on, so the **◀** fold button is hidden); everything else as wide |
| **phone** | < 768 px | the top bar only keeps the title (the 🎮 takeover bar takes its place while a [seat is played](#playing-a-seat)), the game status, the usage pill and the moderator chip, the one enabled **Start / Pause / Resume** button and a **⋯** menu; a bottom **tab bar** switches between *信息流* (feed, with 6 × 2 mini cards above it and — for a played seat — its input box below), *频道* (channels, full-width list with names) and *玩家* (players: the full cards, 3 per row, with the roster controls); the player details open as a **bottom drawer**; dialogs are full screen; every tap target is at least 40 px |

On touch devices (`data-input="touch"`) buttons, inputs and check boxes get a 40 px (check box rows
44 px) hit area.

## Live game

### Top bar

The first row (desktop / tablet):

1. **Title and version** — in demo mode the version badge gets a *· 演示模式* suffix.
2. **⚙ Settings** (*设置*) — opens the [settings dialog](#settings-dialog-export-and-import-an-archive):
   export and import an archive, nothing else. (v0.12.1's *工作文件夹* button is gone: the data lives in
   `data/` next to the program and there is no folder to choose.)
3. **Collapse board** (*折叠看板*, then *展开看板*) — hides the whole participant bar, including the
   buttons in its header (see [Player cards](#player-cards)), to give the feed more room.
4. **Theme** (◐) — light / dark; the choice is not remembered after a reload.
5. **Viewpoint bar** `[视角 ▾] [切换] [接管]` — pick *上帝视角* (god view) or a single player
   ("3 · Carol") in the drop-down and press *切换* to apply it (see [Spectating](#spectating)); *接管*
   plays the picked player's seat (see [Playing a seat](#playing-a-seat)). While a seat is played the
   bar is replaced by **🎮 正在扮演 3 · Carol** and *退出接管*.
6. **Usage** and **Limit** (*上限*) — see [Usage and budget](#usage-and-budget).
7. **New** (*新建*) — starts a fresh game at once. There is no *Save* button any more: every change of
   the game is written to `data/` half a second later, so the game is always saved.
8. **Start / Pause / Resume** (*开始* / *暂停* / *继续*) — game lifecycle. **Start** is only enabled
   for a fresh game (and, outside demo mode, is refused until the moderator and every player are
   bound to a model), **Pause** while the game runs, **Resume** when it is paused — and after the
   end. Timers (speaking time, request deadlines) freeze while the game is paused. A **finished**
   game can still be resumed: the moderator is told that the game was continued after the end.

The second row shows the **status** (*对局进行中* / *已暂停* / *对局已结束 …*; a pause says why: the
moderator's `hold` reason, or *因预算上限暂停* for the budget), the **phase** text written by the
moderator, small **progress chips** for open requests (*🗳 投票中 7/12*, *🗳 商议中 3/4*,
*✍ 作答中 1/1*; at most three, then *+N*) and the **moderator chip** at the end of the row. The
moderator is not a player card: it is a small chip with its own random pixel avatar, its name and
its state icon; click it to open its details.

On a **phone** the first row only holds the title and version, the one enabled lifecycle button and
the **⋯** menu; the status row below keeps the status, the usage pill and the moderator chip.
Settings, theme, the viewpoint bar, limit, new, history, bindings and Provider live in the
**⋯** menu (the very same buttons, moved there while the layout is `phone`; **Collapse board** is
not offered on a phone). The one exception is the takeover bar of a played seat: it takes the title's
place in the first row instead of moving into the menu.

### Banners

A banner appears under the top bar when something needs attention (a phone shows a one-line version):

| Banner | When |
|---|---|
| **Hold** | the moderator paused the game and gave a reason (*确认后点击「继续」*) |
| **Budget** | the game is paused because of the limit (*因预算上限暂停*), or the limit is reached while *force stop* is off (the game keeps running); after the limit was raised while paused it says that **Resume** works again |
| **Stall warning** | the watchdog saw no progress (`session.stall_notice`); the game keeps running, and the warning goes away by itself when the game moves again |
| **Game over** | winner and summary, plus the hints *continue after the end* and *review all* |
| **Autosave error / connection lost** | the save failed (*自动保存失败*); the event stream is reconnecting (*连接断开，正在重连…*, a separate bar at the top of the window) |
| **Old data found** | a bar under the top bar while `data/` is still empty and a v0.12.1 workspace exists at its default place: *检测到旧版（v0.12.1）的数据：请先运行转换脚本，再到「设置」里导入存档*; buttons *查看说明* (the command and the steps, see [Settings dialog](#settings-dialog-export-and-import-an-archive)) and *忽略* (remembered: it never comes back) |
| **Restore notice** | a bar under the banner whenever the server has something to say about the game it started with: *已恢复上次对局（已暂停）* after a restart, *已导入存档（…）* after an import or loading a stored game, or why a fresh game was created instead (an old save that was kept, a save that could not be read, a data folder that could not be used); it also lists the actors that still need a model; buttons *查看模型绑定* and *知道了* |

### Usage and budget

The **usage pill** shows the tokens of the game (all four kinds added: uncached input, cache read,
cache write, output) and the cost. Cost is accumulated from each call's price snapshot; editing a
model price later does not rewrite history. Clicking the pill opens the model bindings. The
**Limit** button next to it (*上限*; *token / 费用上限* in the **⋯** menu) opens the budget dialog:

- **Token limit** and **cost limit (USD)** (*token 上限*, *费用上限*) — empty means unlimited;
  positive numbers only (tokens must be whole numbers). The limits are saved with the game and
  remembered as the defaults of the next ones; the used amounts restart from 0 with every new game.
  Everything the game spends counts: continuing after the end, the manual review and *用主持人模型完善*
  too.
- **Force stop** (*达到上限后强制停止*) — when ticked, reaching a limit pauses the game (*因预算上限暂停*).
  **Resume** is refused until you raise the limit (or untick the box). When it is not ticked the game
  keeps running and only the colours, a banner and a toast tell you.
- Progress bars for each limit and the split of the used tokens into the four kinds, with the cache
  hit rate. The pill turns **yellow from 80 %** of a limit and **red at 100 %** (and pulses); the
  dialog updates live.

![The limit dialog at 81 % of the token limit](images/v0121-budget-dialog.png)

### Player cards

Every player is a card in the participant bar (wide layout):

```
(3)                     [警]    <- round seat number · small badges (sheriff …)
        [ pixel avatar ]💤      <- agent state, on the avatar's lower-right corner
          Carol                 <- nickname (never the internal id)
        [ 狼人 ]                <- identity tag
```

- **Seat number** in a circle, top-left (the ids of the game are `N1`…`N12`; the UI only shows the
  seat number and the nickname — default nicknames are Alice, Bob, Carol …).
- **The identity is always the main tag.** The sheriff and other highlighted tags are small badges
  (the first character of the tag, e.g. *警*) in the top-right corner and never replace it. When
  the current viewpoint may not see the identity, the tag is a grey **`?`**.
- **Pixel avatar**: a 20 × 20 sprite composed from the avatar library (`static/avatar/parts.json`,
  generated by `tools/avatar_lab.py`), drawn with `image-rendering: pixelated`. Without the library
  the page falls back to a flat circle with the seat number.
- **Players who are out** are greyed with a strike-through name and tag.
- **Agent state** — a small round icon on the avatar's lower-right corner (on the card's
  bottom-right corner in the compact and phone layouts); an agent has three states, and a seat that
  a person plays has a fourth:

  | Icon | State | Looks |
  |---|---|---|
  | 💭 | **thinking** (*思考中*) — a model call is running | pulses; the card gets a blue ring |
  | ⚡ | **acting** (*执行中*) — executing tools | flashes; the card gets an amber ring |
  | 💤 | **sleeping** (*休眠*) — nothing to do | still; the tooltip says *休眠：等待新消息* (waiting for a new message) or *休眠：定时醒来：剩余 N 秒* (timed wake-up, counted down on the game clock; *休眠：即将醒来* once it is due) |
  | 🎮 | **human** (*人类操作中*) — a person plays the seat, there is no agent | still; the card gets a green ring; it stays so while the game is paused (see [Playing a seat](#playing-a-seat)) |

  There are no floor, request or *stopped* badges any more (a paused game puts every agent to sleep).
  The moderator chip shows the same states. In a player's viewpoint only that player's own state is
  shown (the moderator chip then shows none).
- When a player uses an ability, its card flashes for three seconds and a toast says so
  (*⚡ Carol 发动了能力：…*); the declaration itself is in the feed.
- In the **god view** a **summary strip** (*对局状态*) ends the participant bar: faction counts and
  the sheriff (`狼 2/4 · 神 4/4 · 民 3/4 · 警长 Grace`). It is hidden information and is not shown
  in a player's viewpoint.

The **header of the participant bar** (*参与者*) holds the bar's own controls. On the left, above the
cards: **Regenerate all avatars** (*重新生成全部头像*, only before the game starts), the **Roster**
menu (*名册*, see [Roster, memory and review](#roster-memory-and-review)) and, after the game ended,
**Review all** (*复盘全部*). On the right: **Sessions** (*会话*: the stored games, see
[Sessions and recovery](#sessions-and-recovery)), the **model bindings** button (*模型绑定*: it shows the
moderator's Provider / model, *未绑定模型* when there is none, or *演示模式 · 脚本机器人* in demo mode)
and **Provider** (see [Provider and model setup](#provider-and-model-setup)). On a phone the
right-hand buttons live in the **⋯** menu and the left-hand ones in the *玩家* tab.

### Player details

Click a card (or focus it and press Enter or Space) to open the details: a floating card just below
the player on desktop and tablet (Esc, the ✕ or a click outside close it), a **bottom drawer** with a
backdrop on a phone (drag the handle down, tap the backdrop or press Esc to close). The moderator
chip opens a reduced panel: the moderator's name is fixed and it has no persona, memory or review,
but it shows its random avatar (with *重新生成头像* before the game starts), its state, its model and
its metrics.

- **Name** — editable while the game has not started (1 – 20 characters, no quotes, backslashes or
  angle brackets, unique among the players ignoring case; saved with *保存名字* or Enter). During a
  game the name is plain text.
- **Avatar** — *重新生成头像* rolls a new one for this player (before the game starts only).
- **View as this player** (*以 Carol 的视角观看*) — switches the spectator viewpoint (not offered
  while you are already viewing that player; disabled, with a tooltip, while a seat is
  [played](#playing-a-seat): the viewpoint is locked to it).
- **State, tags, model** (*模型*, with a *配置* button that opens the model binding) and the
  **metrics**: activations, LLM calls, thinking rounds, tool calls, rejected calls, cache hit rate,
  cost, and the four token kinds (uncached input, cache read, cache write, output). These are
  statistics only — chains of thought and tool-call details are never shown; they are written to the
  plaintext trace log in `data/logs/`.
- **Persona** (*性格设定*) and **memory** (*记忆*) — two expandable text editors, empty by
  default, saved with *保存* in `data/roster/` and loaded into the player's prompt (what you change
  while a game is running applies from the next game). Both have **Refine with the moderator
  model** (*用主持人模型完善*; it needs a few notes in the box and a model bound to the moderator):
  the model's *draft is shown first* (*尚未保存*) and nothing is saved until you press *采用并保存*;
  *放弃草稿* throws it away.
- **Review this player** (*复盘此玩家*) after the game ended; see below.

### Channels and the feed

- The **feed** shows one channel at a time: speech, moderator announcements (badge *公告*; template
  messages carry a *模板* badge and are highlighted), ability declarations (*能力*), challenges
  (*质询*) and their replies (*质询回复*), system notices (*系统*) and **request cards**. Every
  message carries the sender's avatar (a player's pixel avatar, the moderator's own random one, a
  gear for system notices) and **nickname**. Messages arrive whole — there is no streaming.
  Truncated messages (over the character limit) end with an ellipsis.
- A player id in a message — the moderator's text or a player's (`N3`, `N10` …) — keeps its
  characters but gets a dotted underline and a tooltip with the nickname (*N3 Carol*); the text
  itself is not rewritten.
- The **channel list** has **no "All" channel**: the **public channel** is selected by default (before
  the game starts only the players' private chats exist and nothing is selected). Each channel shows
  its **floor state**:

  | Icon | Floor state |
  |---|---|
  | 🔇 **禁言中** | only the moderator can write (was "closed") |
  | 🎤 **轮流发言** | turn-taking: a holder has the floor (*持麦：Kate*; *轮流（暂停）* while paused) |
  | 📋 **队列 10/12** | the moderator's speaking queue with its progress; the current speaker on a line below (*持麦：Kate*) |
  | 💬 **自由讨论** | free discussion with a character progress, e.g. *450/2000 字* |
  | ❄ **已冻结** | an ability froze the channel until the moderator rules (*等待主持人裁决*) |
  | 👁 **只听** | listen-only: the members can read but not write |

  Click a channel to read it; unread counts are shown per channel (and on the *频道* tab of a
  phone).
- **Private chats** (one per player) are folded into a **Private chats** (*私聊*) group (click it to
  unfold; it opens by itself while one of them is selected), have a different background colour and
  are titled with the other party's **nickname only** — there is no "private chat" in the title.
  A private chat that is still free shows no floor chip.
- **Challenge tickets** (*质询工单*) keep their own group: who challenged, the status (*待处理*
  pending / *成立* upheld / *驳回* rejected) and a preview; the group title counts the open ones.
  Click a ticket to jump to that private chat and to the ticket's message. Players may challenge the
  moderator at any time; the moderator must answer and may close the private chat of a player who
  abuses the feature.
- The channel column is an **avatar rail** on narrow screens (the compact layout): one icon per
  channel, the tickets folded into one numbered icon (it opens the ticket list), the private chats
  into one group icon. Hover (mouse) or long-press (touch) shows the full name. In the wide layout
  you can fold the column yourself with the **◀** button (**▶** unfolds it); its width is
  draggable there. On a phone the *频道* tab shows the full list with names.

### Request cards

When the moderator asks players something (`open_request`) the question appears **in the feed as a
card** at the place where it was opened, and the **same card** is updated as answers arrive and when
it is closed:

- the question, the kind in the title (*投票请求* vote, *商议请求* deliberation — a request with
  shared answers —, *问答请求* question), its status (*进行中* / *已关闭* / *已取消*), a countdown
  (*⏱ m:ss*) on the game clock when the request has a deadline (it freezes while the game is
  paused) and a progress bar (*4/4 已作答*);
- one row per recipient with the player's current choice;
- **shared answers** (*👥 组内可见*, e.g. the wolves' kill): every recipient sees the others' current
  choices live and may change their mind until the request closes — a changed answer is marked
  `↻1` on the same row. The closing rule (*全员一致才结束* / *到时才结束*) is shown on the card;
- **sealed** requests (*🔒 密封*, e.g. the daytime vote): other players' answers stay hidden
  (*✓ 已提交*). The god view reads them (marked 🔒);
- when the request closes, the card shows the **result**: a tally with bars (options with 0 votes
  are folded into *其余 N 项 0 票*), the leaders (*最高*), defaulted answers (*默认答案*) and
  missing players (*未作答*);
- for the seat a person [plays](#playing-a-seat) the card is also where the answer is given:
  option buttons or a text box under the rows.

**Visibility.** The god view sees every request. A player's viewpoint only sees requests addressed
to that player, in the channel where the request lives, and only the answers that player may see;
the tally of a sealed request is only drawn in the god view (a player's card says *已关闭，共 N 人作答*
and leaves the result to the moderator's announcement).

## Spectating

The viewpoint selector is a pure front-end filter (nothing is requested from the server): choose in
the drop-down, then press *切换* — the drop-down alone changes nothing (the bar also holds *接管*, see
[Playing a seat](#playing-a-seat), which locks the viewpoint to the seat you play). In a
player's viewpoint you only see what that player may see:

- messages written while the player was a member of the channel, and request cards addressed to the
  player (shared answers of their group, their own sealed answer);
- only the channels the player belongs to, and their own challenge tickets;
- other players' **public tags** only (alive / out / sheriff …) — their identity shows as `?` — and
  the player's own full tags and state;
- no summary strip.

The dark theme (**Theme** button) is supported everywhere; the pixel avatars get a pale plate so
their dark outlines stay readable.

## Playing a seat

A person can play **one player seat** instead of its agent: read what that player may read, write in
its channels, answer its requests and use its abilities. Everything the person does goes through the
same commit gate as an agent's tool call, so whose turn it is, how many characters and messages are
allowed and when time is up are the engine's rules — the page only shows the result. **Nobody is
told**: the moderator and the other players cannot tell a person from an agent (and the seat's agent
is not told afterwards either).

![Right after taking a seat over, before the game starts](images/v013-takeover-start.png)

### The viewpoint bar and taking a seat over

The viewpoint selector is a small bar, `[视角 ▾] [切换] [接管]`. The drop-down only *picks*; nothing
changes until you press a button:

| Control | What it does |
|---|---|
| **Viewpoint** drop-down (*视角*) | picks *上帝视角* or a player — a draft, nothing is applied yet |
| **Switch** (*切换*) | applies the draft as the spectator [viewpoint](#spectating); disabled while the draft is the viewpoint already shown |
| **Take over** (*接管*) | plays the picked player's seat, after a confirmation; disabled for the god view |

*接管* is available only when

- a **player** is picked (the moderator cannot be played, the god view has no seat);
- the game has **not started** or is **paused** — not while it runs and not after it ended (the
  tooltip says what to do first, e.g. *请先点「暂停」*);
- no other seat is played (one seat at a time) and no [replay](#replay) is on screen.

The server checks the same conditions and answers `409` otherwise. The confirmation (*接管 3 · Carol？*)
says what happens: the seat's agent pauses (no model calls, no tokens), the viewpoint is locked to
the seat, nobody is told, and speaking times and deadlines stay the moderator's decision; before the
start it adds that you will see your identity first and may re-deal.

### While you play

- The bar is replaced by **🎮 正在扮演 3 · Carol** and **退出接管** (on a phone it takes the title's
  place in the first row and is not moved into the **⋯** menu).
- The **viewpoint is locked** to the seat: the page shows exactly what that player may see (its own
  identity, its channels and requests, the others' public tags). The drop-down, *切换* and *View as this
  player* in the player details are disabled. *Known limit:* the lock is a filter in the page, like
  the whole [viewpoint](#spectating) — the server still sends the god view's data, so the browser's
  developer tools would show everything.
- The seat's card says **🎮 人类操作中** (for every spectator). No agent exists for it, nothing is
  called or counted for it. It still needs its **model binding**: the agent comes back when you give
  the seat back, and the seat is reviewed after the game like every other.
- **Time limits are unchanged.** Speaking time and request deadlines are the moderator's; the page
  counts down on the game clock (it stops while the game is paused). When time is up the engine does
  what it does for an agent: the floor ends, a missing answer gets its default.
- **The takeover is part of the game's save.** After a restart, when you continue the game from the
  [history](#history-of-games-and-replay) or import an archive that holds it, the game comes back
  **paused and still played**. Every open browser tab follows: the state is the server's.

### Re-dealing the identities

Taking a seat over *before the game starts* shows you your identity, which spoils the game. A green
bar under the top bar says so (*你已经知道自己的身份，建议点击「重新发牌」*) with a **Re-deal** button
(*重新发牌*): it deals every seat's identity anew (names, avatars and the roster stay) — as often as
you like.

**After the start there is no re-deal, ever.** The bar and the button are gone, and the server answers
`409` as soon as the game is not fresh any more (its status is not *idle*, or the ledger holds a
start record). *Start* and *re-deal* run under one lock, so a double click or a second tab cannot
re-deal a game that has begun.

### Giving the seat back

**退出接管** is enabled only while the game does **not run** (before the start, paused or finished);
while it runs the tooltip says to pause first. A confirmation follows. Afterwards the viewpoint is the
one you had chosen before, and when the game continues the seat's agent is built again: its
conversation starts over from the full board and the whole visible history, in which what you said
stands as its own words. It is not told that somebody else played.

### The input box

![One turn of the played seat: typing in the public channel](images/v013-takeover-turn.png)

While a seat is played the bottom of the feed holds an **input box for the selected channel**.
Selecting another channel switches to that channel's box, and **every channel keeps its own draft**
(for this game and this seat).

- **Enter** sends, **Shift+Enter** breaks the line (confirming an IME candidate never sends); the box
  grows with the text.
- The counter shows `23/300`: the characters typed and the limit of *one message* **as the server
  computes it for the seat right now** (the floor's token, the free discussion's or the private
  chat's limit). A longer text is **not refused**: the engine cuts it, and a toast says how many
  characters went (*发言超出字数上限，末尾 N 个字被截掉了*; the message ends with an ellipsis in the feed).
- The line under the box says where you stand: *轮到你发言 · 还可发 2 条 · 单条 ≤ 300 字 · 剩余 1:42*
  (a turn), *这次讨论你还可发 6 条* (a free discussion), or why you cannot write.
- You can **always type**, whatever the state. The **send button** shows the state:

  | Button | When |
  |---|---|
  | **发送** | the engine would accept a message now |
  | **禁言中** (grey) | not your turn, your turns or messages are used up, or the channel is closed, frozen or listen-only; the reason is the hover text and the line under the box |
  | **已暂停** | the game is paused (floor and request timers are frozen as well) |
  | **未开始** / **已结束** | the game has not started / is over |

- A refused message keeps your draft and shows the engine's reason under the box. If something
  important happened while you were writing (a phase change, a player's status …) the engine answers
  `STALE`: the draft is kept, the box says *局面刚刚发生了重要变化 … 草稿已保留 … 再点一次「发送」*, and the
  next press sends it.
- **结束发言** (*end my turn*) appears only while you hold the floor of this channel and gives it up
  early; it is greyed (with the reason) while the floor is paused or the channel frozen.
- **⚡ 技能** (*abilities*) is next to it in every channel; it is grey, with the reason, when no
  ability is enabled for you.

### Abilities

The button lists the abilities the moderator has enabled for the seat and that have uses left (in the
bundled scenario: the white wolf king's explosion, which the moderator enables for the daytime
speeches). Choose one and a form appears, **generated from the scenario's parameter definition**: a
player parameter is a drop-down of the living players (*7 号 Grace*, yourself marked *（我）*), several
players are check boxes, a text has its length limit, choices and numbers follow their definition,
optional ones are marked *（可选）*. *下一步* opens a **second confirmation** that repeats the values and
warns that the ability cannot be undone; only *确认使用* sends it. The engine validates everything once
more and its refusal is shown. The hunter's shot is not an ability but a request to the hunter, and
appears as a [request card](#request-cards).

### Request cards for the played seat

A request addressed to the played seat that is still open is answered **right on its card**:

- a **choice**: one button per option. Your current answer is marked (*你的选择：4号 David（请求关闭前可以修改）*)
  and a click on another option **changes** it, until the request closes (see the closing rule on
  the card);
- a **text**: a box with its limit (*写下你的回答（最多 300 字）*) and *提交* / *修改回答*; what you type
  survives the card being redrawn;
- the card's countdown follows the game clock; while the game is **paused** the buttons are disabled
  and the card says why (*对局已暂停，继续后才能作答*);
- cards of other recipients and closed requests have no buttons.

### Marks in the channel list

![A request card, the marks in the channel list and a muted channel](images/v013-takeover-request.png)

A channel where **you hold the floor or an unanswered request waits** gets the amber chip
**待你操作** (a ✋ on the avatar rail, an amber frame). The seat's own summary replaces the channel's
general one there: *🎤 轮到你发言 · 还可发 1 条*, *🔇 禁言中*, …

### Not offered

The scenario's speech templates (`speak_template`), notes and `sleep` are not offered to the person:
just type. There is no challenge button either — write to the moderator in your **private chat**.

### On a phone

![The played seat on a phone](images/v013-takeover-phone.png)

The takeover bar takes the title's place in the first row. The input box sits at the bottom of the
*信息流* page above the tab bar, one line high and growing with the text; its placeholder is short (a
touch screen has no Shift+Enter). When the soft keyboard opens, the page shrinks to what is left and
the tab bar, the status pills and the mini cards make room for the feed; they come back when the
keyboard closes.

### Interface for scripts

| Endpoint | Body | Answers |
|---|---|---|
| `POST /api/takeover` | `{"seat"}` | the session; `404` unknown seat, `422` the moderator, `409` running / finished / another seat is played |
| `POST /api/takeover/release` | – | the session; `409` while the game runs |
| `POST /api/seat/speak` | `{"channel_id", "content", "based_on_seq"?}` | a receipt |
| `POST /api/seat/pass` | `{"channel_id"}` | a receipt |
| `POST /api/seat/respond` | `{"request_id", "value", "reason"?}` | a receipt |
| `POST /api/seat/ability` | `{"ability_id", "params"}` | a receipt |

The actor is always the seat that is played — there is no field to name another one (unknown fields
are `422`). A refusal by the engine is **not** an HTTP error: the receipt `{"accepted": false, "code",
"message"}` comes back with status 200, and the receipt of an accepted message says in `truncated`
how much of it was cut. HTTP errors mean *no seat is played* / *the game does not run* (`409`) or a
malformed body (`422`). All of them are writes: they need a token (`ARENA_API_TOKEN`) or a local client. The
`takeover` field of the session and the `takeover.updated` event carry the state of the seat — what
it may write where, the open requests, the usable abilities; see the contract
([`docs/v0.12-contracts.md`](v0.12-contracts.md), section 11.7).

## Roster, memory and review

The roster is the list of the 12 seats: nickname, avatar, persona and memory. It lives in the
data folder (plain files in `data/roster/`, so persona and memory can also be edited
outside the app), so names and avatars survive a restart. A game takes the roster as it is when it
is created and the persona / memory texts as they are when it starts.

- **Names and avatars** can be changed before the game starts (player details, or *Regenerate all
  avatars* in the participant bar).
- **The moderator** is not a seat: its name, persona and memory are fixed. It does have a random
  pixel avatar of its own, shown on its chip, on its messages in the feed and in its details panel.
  The avatar is re-rolled together with the twelve players (*重新生成全部头像*; all thirteen look
  different) or alone in its own panel (*重新生成头像*) — both only before the game starts — and it
  stays the same for the whole game. The fixed top-hat icon is only a fallback for a server that
  sent no avatar or a missing sprite library.
- **Roster menu** (*名册 ▾*):
  - **Clear all memory** (*清除全部记忆…*) — asks for confirmation, with a check box to clear the
    personas too (*同时清除性格设定*); it cannot be undone, so export an archive first (the memory is
    part of it);
  - **Load memory in this game** (*本局加载记忆*) — a switch; off means the players' prompts of
    this game contain no memory section (the files are untouched). The setting is remembered in the
    data folder for the next games.

  Clear and the switch are only available before the game starts (press **New** after a
  finished game). *Regenerate all avatars* is idle-only as well. (The v0.12.1 memory pack — a zip of the
  persona and memory files — is gone: the personas and memories travel inside the archive of the
  [settings dialog](#settings-dialog-export-and-import-an-archive).)
- **Review** is manual: nothing is written to the memory after a game by itself. When the game is
  **finished**, *Review all* (*复盘全部*, participant bar) or *Review this player* (player details)
  asks the players to review the game with their own models; the win / loss record (*战绩*) is
  updated and the player's own recap is appended to its memory file (*对局记忆*). A player is
  reviewed once per game, and the review's tokens and cost count towards the game's usage. The
  button shows the progress (*复盘中 3/12…*) and ends as *✓ 已复盘*; the player details show the
  same and a *查看写入的记忆* button that opens the memory editor.

## Settings dialog: export and import an archive

**⚙ Settings** (*设置*, first row of the top bar; in the **⋯** menu on a phone) has exactly two things. Both
work on **one file**, an archive (`.arenaagents`), that holds everything: the games, the roster with the
personas and memories, the Providers (without their keys), the settings, the price table and — if you
want — the thinking logs and the API keys. Use it as a backup or to move to another computer. The format
and its checks are described in [archive.md](archive.md).

While a game runs, **import** is disabled (*对局正在运行：请先点击「暂停」，再导入存档*); export is
always possible (the running game is saved first). Only one export or import runs at a time.

### Export (*导出存档*)

1. **API Key?** — *导出 Key（需要密码）* or *不导出 Key*. Without keys nothing secret is in the file; the
   Providers come back without a key and you enter it again.
2. **Password** (only with keys) — typed twice. The three rules are shown live with ✓ / ✗ and the next
   button stays disabled until they hold: **at least 12 characters, at least three of upper case /
   lower case / digit / symbol, both entries equal**. The password encrypts the keys (AES-256-GCM, key
   derived with scrypt); it is not stored anywhere — forget it and the keys of that archive are lost
   (the rest of the archive can still be imported by skipping the keys).
3. **Thinking logs** — a check box, on by default. The logs show what each agent thought and did and can
   be large; the games themselves do not depend on them.
4. **Progress** — a bar with the phase (*正在保存当前对局…* → *正在收集文件…* → *正在加密 API Key…* →
   *正在打包…* → *正在写入文件…*). It runs on the server: closing the dialog does not stop it, and the
   result opens by itself when it is done.
5. **Result** — the path of the file (`data/exports/arena-<date>-<time>.arenaagents`; a browser on another
   machine than the server only sees `data/exports/…`), its size, what is inside, any warnings (a damaged
   game that was left out, for example) and a **Download** button (*下载*) that saves the file through
   the browser.

### Import (*导入存档*)

1. **Choose the file** — *选择文件…* opens the file picker. The browser sends the file to the server
   (the bar shows the upload; the server writes it to `data/tmp/`, it is never read into memory). The
   size limit is 2 GB (`ARENA_MAX_ARCHIVE_BYTES` changes it).
2. **Check** — the server verifies the whole file: that it is an archive of this program, that its
   format is not newer than this program understands (*请先把 Arena-Agents 升级到较新的版本再导入*),
   that nothing is missing or added, every file's SHA-256, safe paths and sizes, and that every game is
   a valid v0.13 game. A file that fails says why and nothing is changed.
3. **Summary** — file, creation time, app version, how many games, Providers (with their names) and
   roster seats, whether keys and logs are inside.
4. **Keys** — if the archive holds keys: type its password, or press **跳过 API Key并导入** (skip the
   keys). A wrong password is reported at once (*密码错误或 API Key 部分已损坏* — it also appears when the
   key part was changed) and you can try again; nothing has been replaced yet. If the password is right
   but the archive's Provider addresses were changed after the keys were encrypted, no key is imported
   (*存档在导出之后被改动过*) and the same step offers *跳过 API Key并导入*. Skipping keeps the key of a
   Provider you already have here with the same id; the other Providers show *未设置 API Key* until you enter
   one.
5. **Replace** — *整体替换并导入* replaces **all** current data. The page says so, and that the old data is
   first moved to `data/backups/<time>/` (the last 3 are kept) and put back automatically if anything fails.
   A bar shows *校验并解包 → 备份当前数据 → 替换数据 → 保存 API Key → 重新载入*; do not close the page.
6. **Done** — the result lists what came in (games, Providers, keys imported / kept), which Providers still
   need a key, where the backup is, and the page reloads itself. The game that was current in the archive
   comes back **paused**.

*取消* in any step deletes the uploaded file on the server. A file that was uploaded but not applied is
offered again (*继续导入* / *丢弃*) if you reload the page; it expires after two hours.

There is no button to restore a backup: the steps are in [archive.md](archive.md#53-手动恢复).

### Coming from v0.12.1

v0.13 has no workspace folder and never reads the old one. When `data/` is empty and the old default
folder exists, the **old data found** bar appears under the top bar. *查看说明* shows the command to run in the
program folder (as the Windows user who owns the old workspace):

```
python tools/migrate_v0121_to_archive.py                  # add --include-keys to take the API keys along (asks for a password)
```

then open **⚙ Settings → Import** and choose the `.arenaagents` file it wrote. *忽略* hides the bar for good.
The script only reads the old folder; games of v0.12.0 and earlier are skipped and listed in its report.

## Provider and model setup

![Provider dialog: price source and the four prices](images/provider-modal.png)

1. **New Provider** — enter a display name.
2. **Protocol** — OpenAI Chat Completions, OpenAI Responses, or Anthropic Messages.
3. **Base URL** — root URL when auto-append is on; complete endpoint when auto-append is off.
4. **Auto-append API path** (*自动拼接路径*) — shows the final request / model URLs live (computed
   by the server). A warning is shown for plain HTTP, for a version path other than `/v1` and — with
   auto-append off — for a URL that does not look like a complete endpoint; a complete endpoint
   (`…/chat/completions`) used in auto mode is refused with an error.
5. **API Key** — write-only. After saving, only “Key configured” is shown. Changing the protocol,
   Base URL or auto-append mode requires re-entering the key.
6. **Provider list** — read-only environment Providers (*只读*) cannot be edited or deleted. While a
   game is running the protocol, Base URL and auto-append mode are locked (only the key can be
   rotated) and nothing can be deleted; a Provider or model that a binding or template still uses
   cannot be deleted at all until those are changed.
7. **Price source** (*价格源 URL*, top of the dialog) — the URL of a LiteLLM-style price table
   (`model_prices_and_context_window.json`; the default points at the LiteLLM repository). Only
   `https://` URLs are accepted. **Save** stores the URL (a different URL drops the loaded table
   until it is refreshed), **Refresh price table** (*刷新价格表*; it saves a changed URL first)
   makes the server fetch it and the status line shows how many entries were loaded and when (or
   why the refresh failed — the previous table stays in use).
8. **Models** — click **Fetch models** (auto mode only) and select one model. If the price table
   knows it, the form is **filled in automatically**: input, cache-read, cache-write and output price
   per 1M tokens plus the context window, with a hint naming the table entry; everything stays
   editable. For a model id you typed yourself, **Match price** (*自动匹配价格*) asks the server. A
   model without a match stays empty — type the prices by hand.
9. **Four prices** — input and output are required. The two **cache prices** are optional: an
   empty field bills those tokens at the input price. The model list shows all four prices and the
   **source** of the numbers (`litellm:<entry>` or `manual`).
10. **Edit price** (*编辑价格*) / **Remove** (*移除*) — edit a model's prices independently (also
    while a game runs: every call keeps its own price snapshot) or remove it when no
    binding/template uses it (not while a game runs).

When auto-append is off, the model-list URL is unavailable and fetch is disabled; type a model ID
manually. See [providers.md](providers.md).

## Templates and bindings

![Templates and bindings](images/templates-bindings.png)

- Each of **Template 1/2/3** (*模板1/2/3*) stores a name, Provider, model and reasoning depth. Edit
  the fields and click **Save templates** (*保存模板*).
- Tick the moderator and/or players — the check boxes are in the rows of the collapsed **Advanced
  manual bindings** list (*高级手动绑定*), which names the players by seat and nickname; *全选玩家*
  ticks every row including the moderator, *全不选* clears them — then click **Apply template 1/2/3**
  (*应用模板1/2/3*). Applying is blocked while a game runs; editing templates is allowed.
- **Advanced manual bindings** remains available for per-player overrides (*保存绑定* saves the
  rows); *主持人设置应用到全部玩家* copies the moderator's Provider / model / depth to every player
  row.
- A template whose Provider/model was removed is marked invalid (*⚠ 引用失效*) and cannot be applied.
- **The moderator is an LLM agent like every player and always needs a model binding.** A game
  cannot start until the moderator and all players are bound (demo mode needs no bindings).
- The **reasoning depth** (low / medium / high / xhigh) is the model's thinking effort and is set
  per agent here. An agent can call tools repeatedly within one activation; there is no round limit
  and no "execution depth" setting any more.

## Layout controls

- Click **Collapse board** (*折叠看板*) in the top bar; click again (*展开看板*) to restore. It is not
  offered on a phone.
- In the wide layout the channel column has a **◀** button in its header that folds it to the
  avatar rail (**▶** unfolds it) and a draggable divider for its width; the compact layout always
  uses the rail. The height of the participant bar has its own draggable divider (wide and compact
  layouts).
- Double-click a divider to restore its default size.
- Collapsed/resized state and the viewpoint are stored in `localStorage` (the theme is not).

## History of games and replay

Every game is saved automatically (half a second after anything changed). **📜 History** (*历史对局*,
in the participant bar; on a phone in the **⋯** menu) lists all of them, newest first. A row shows the
scenario, when it was created, how it ended (*已结束 · 好人阵营 获胜*, *已暂停*, *未开始*), how long it
lasted in game time, how many tokens / dollars it cost and — with a 🎮 badge — whether a person played a
seat (*有人类参与*). The game on screen is marked *当前*.

| Button | What it does | When it is available |
|---|---|---|
| **▶ Replay** (*重放*) | plays the game back on its game clock (see below) | any saved game; the *current* game only while it is not running and nobody plays a seat (a replay shows everything, which would spoil it for the person at the table) |
| **Continue** (*继续*) | makes the game the live one — it comes back **paused**, like after a restart; the game on screen is saved first | unfinished games only; never while a game runs or a replay is shown |
| **Delete** (*删除*) | removes the game and its logs after a confirmation | every game except the current one |

The delete confirmation says *删除对局不会影响当前 agent 的记忆*, and that is literally true: a player's
persona and memory belong to the **roster** (`roster/N1.memory.md` …), not to a game, so deleting a game
never touches them. A game whose file cannot be read stays in the list as *无法读取* and can still be
deleted. The buttons re-check their condition when they are clicked, so a game that was started while the
list was open cannot be replaced by accident.

### Replay

Replay turns the saved ledger of a game into the same spectator events the live stream sends, and plays them
through the ordinary client store — into a session of its own. The *live* game is kept aside: while you watch,
its events still arrive and are applied to it in the background, and **Exit replay** (*退出重放*) shows it
exactly as it is now. What a replay contains is what spectators saw: messages, channels and floors, requests
and their results, identities and tags, phases, the summary strip. It does **not** contain thoughts, tool
calls or agent states (they are not in the ledger; the player details show only the public side).

The replay bar under the top bar has:

- **⏮** back to the start, **▶ / ⏸** play and pause (*play* at the end plays again from the start);
- the speed **0.5× · 1× · 2× · 4× · 8×**; the wait between two steps is the game-clock gap divided by the
  speed, but at least 0.12 s at 1× (the floor scales with the speed too; a scripted demo game has almost no
  game time, and without a floor its whole replay would flash by);
- **跳过空闲** (on by default): a gap of more than 5 game seconds takes half a second instead;
- a **seek bar** counting ledger steps and the game time (*0:41 / 2:30*). Dragging backwards rebuilds the
  session from the start in one synchronous loop (never frame by frame), dragging forwards applies the
  missing steps; playback continues from the new position.

While a replay is shown the live controls (*新建 / 开始 / 暂停 / 继续*) and **接管** are disabled (the status
line reads *重放中 · 0:41 / 2:30*), but the **viewpoint can still be switched** — replay any game from a
single player's point of view. A replay only starts from the history list; the server refuses to replay the
live game while it runs or while a seat is played (409).

## Sessions and recovery

- Loading a game (**Continue**) never starts it: a game that was in progress comes back paused (a
  finished game stays finished, a fresh one stays fresh). Review model bindings, then click
  **Resume**; the notice bar under the top bar says what was loaded and which models still need
  binding.
- On startup, the last game is restored the same way (*已恢复上次对局（已暂停）*). If it cannot
  be restored (corrupt file), the files are kept untouched, you get a blank game,
  the notice bar says why, and nothing is saved automatically until you start, create or continue a
  game.
- **Games written by v0.12.1 or earlier cannot be opened directly** (the save format is now v5). Convert
  them with `tools/migrate_v0121_to_archive.py` and import the resulting `.arenaagents` file in the
  settings. A game of an older format that is found in the data folder stays where it is, untouched.
- If the API is protected with `ARENA_API_TOKEN`, a token dialog appears only after a 401. The
  token is stored in browser localStorage.
- The event stream is resumable: after a dropped connection the page reconnects (the bar at the top
  of the window counts the attempts) and replays only the missed events — the server keeps a bounded
  history — or receives a fresh snapshot if too much was missed.

## First game checklist

1. *(No API key? Start the server with `ARENA_DEMO=1` and skip to step 5 — scripted bots play.)*
2. Add a Provider and its models (use **Refresh price table** and **Fetch
   models** to fill the prices).
3. Create or apply model bindings for the moderator **and** the players.
4. Optional: rename the players, re-roll avatars, write personas and memories (**Refine** helps),
   decide whether this game loads memory, and set a token / cost limit with **Limit**.
5. Click **Start**; the moderator opens the game by following the scenario's manual.
6. Use **Pause** at any time; do not change the model bindings while running.
7. After the verdict you can **Resume** (continue after the end) or **Review all** to write the game
   into the players' memories.
8. Rely on autosave (there is no save button); use the history to load an earlier game, and **⚙ Settings → Export** for a backup.
9. Thoughts, tool calls and receipts are in `data/logs/<session>/trace-*.jsonl`.

## Working on the UI

The front end is plain JavaScript without a build step: `static/index.html`, `static/css/style.css`
and `static/js/` (`core` · `avatar` · `platform` · `store` · `view` · `layout` · `providers` ·
`roster` · `budget` · `archive` · `takeover` · `composer` · `app`). `core`, `store` and `view` are pure and have no DOM
access; `avatar` and `platform` only reach the browser through an optional `<canvas>` (PNG
encoding) and the `window` / `<html>` they are given, so all five run under node in the tests.

To try the **real** server and UI with scripted bots without touching your own data, run
`.venv\Scripts\python.exe tools\dev_server.py --demo --port 8123`: it uses a private data directory
(`.demo-data/demo`, `--fresh` wipes it) and needs no API key or model.
`--copy-providers-from <a data folder>` runs real models on a *copy* of your Providers instead (and
**costs API credits**).

### The UI mock

`tools/mock_ui_server.py` serves the real `static/` directory and a scripted 12-player game (about
a minute at speed 1: N1…N12 with the nicknames Alice …, the wolves' shared kill with a changed
answer, a sealed vote, challenge tickets, the white wolf king's explosion, a token budget that warns
at 80 % and pauses at 100 %, the end, the review) behind the v0.12.1 API shapes. Press **开始** in
the page (or `POST /api/control/start`) to play the script; **暂停** / **继续** stop and continue it,
and after the end **继续** replays a short epilogue:

```
.venv\Scripts\python.exe tools\mock_ui_server.py --port 8731      # http://127.0.0.1:8731/ (default)
  --host 127.0.0.1   bind address                 --speed 2       replay speed multiplier
  --seed 7           another random run           --demo          show the 演示模式 badge
  --budget-tokens N  demo limit with force stop (default 150000, 0 = none)
  --selfcheck        replay offline and validate every event against the contract
```

Helper endpoints for driving it from a script or a browser tool:

| Endpoint | Effect |
|---|---|
| `POST /api/mock/hold {"reason"}` | the moderator holds the game (pause reason `hold`) |
| `POST /api/mock/error {"reason": "stall"}` | pause with an error / stall reason |
| `POST /api/mock/stall {"notice"}` | show (or, with an empty notice, clear) the stall warning banner |
| `POST /api/mock/finish` | jump to the end of the game |
| `POST /api/mock/speed {"speed"}` | change the replay speed on the fly |
| `POST /api/mock/disconnect` | close every event stream (the page reconnects and resumes) |
| `POST /api/mock/emit {"type","data"}` | publish one validated event |
| `POST /api/mock/archive {"legacy", "max_bytes", "tick"}` | show / hide the *old data found* bar (`--legacy` does it at start-up), change the upload limit, change the time between two progress events of an export / import |
| `GET /api/mock/info` | speed, latest event id, game clock, budget … |
| `GET /api/mock/frame?w=390&h=844&q=platform%3Dphone%26input%3Dtouch` | the page inside an iframe of exactly `w × h` CSS pixels (own viewport), for layout checks at any size on a desktop browser |

`POST /api/control/new` deals a fresh game; the budget limits survive it, the used amounts restart.

The mock also implements the archive API (`/api/archive/*`): exports and imports are jobs that really take
a couple of seconds and send `archive.progress` events. Nothing touches the disk. To try the import, the
**name of the file you pick** decides what happens: `…demo….arenaagents` is accepted, `…keys….arenaagents`
holds API keys (password `Correct-Horse-9`), `…mismatch…` has keys that no longer match the Provider
addresses, `…fail…` breaks half way through the replacement (and is
rolled back), `…newer…` is "a newer format", `…corrupt…` fails its checksum, `…huge…` is over the size
limit. A file you exported from the mock itself is read through its manifest.

**Playing a seat** works in the mock like on the real server — `POST /api/takeover`,
`/api/takeover/release`, `/api/seat/*` and the re-deal follow the same rules and answer with the same
shapes, `GET /api/state` carries `takeover` and every change of the seat state is pushed as
`takeover.updated` — and the script **waits for the person** instead of acting for the seat: at its
turn in a speaking queue, at the requests addressed to it (the wolves' kill, the votes, last words) and,
for N12, the white wolf king, at the explosion. A silent person is handled like the engine does it: the
time runs out and the default applies. To try it: pick *3 · Carol* in the drop-down → *接管* →
*重新发牌* → *开始*; the page then waits for you at Carol's turns.

### Tests

```
node tests/frontend/test_view.js                       # one file (the pure-logic ones: test_store / test_view / test_core / test_avatar / test_platform)
PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_frontend_js.py tests/frontend/test_mock_server.py -q -p no:cacheprovider
```

`tests/test_frontend_js.py` syntax-checks every `static/js/*.js` and runs every
`tests/frontend/test_*.js` (fake DOM harness: `tests/frontend/harness.js`, fixtures:
`tests/frontend/fixtures.js`, a minimal sprite library: `tests/frontend/fixtures/parts.min.json`).
`tests/frontend/test_mock_server.py` checks the mock against the contract and reduces its whole
event log with the **real** client store (`replay_check.js`) to the session the mock holds.
`tools/live_replay.py --base http://127.0.0.1:8135` does the same against a running
`tools/dev_server.py --demo --port 8135` instance: it plays a whole demo game (token limit, pause,
review included) and checks that the SSE events add up to exactly the session the server reports.

Playing a seat has its own tests: `tests/frontend/test_takeover_view.js` (the store and the pure view
model: viewpoint bar, takeover bar, re-deal reminder, input box states, request actions),
`tests/frontend/test_takeover_ui.js` (the page: confirmations, the lock, drafts, Enter / Shift+Enter,
the send button's states, `STALE`, abilities, request cards, the phone rules) and
`tests/frontend/test_mock_takeover.py` (the mock plays a whole game with a person at a seat, and its
event log is reduced with the real store: `node tests/frontend/replay_check.js --human`); on the server
side `tests/test_takeover_runtime.py`, `tests/test_takeover_api.py`, `tests/test_takeover_demo_game.py`
(a whole scripted 12-player game with a person at a seat) and `tests/v12/world/test_seat.py` (the seat
state against `speech_gate`, the state board and a real `speak`, channel by channel).

### Browser matrix

Before a release, check the UI in a real browser against the mock at these sizes, in the light and
the dark theme, through `/api/mock/frame` (the platform is simulated with `?platform=` / `?input=`,
e.g. `/api/mock/frame?w=1024&h=768&q=platform%3Dtablet%26input%3Dtouch`):

| Viewport | Platform | Check |
|---|---|---|
| 1920 × 1080 | desktop | full layout, summary strip, detail card, budget dialog, price source |
| 1440 × 900 | desktop | reference layout |
| 1280 × 800 | desktop | smaller cards, truncated names |
| 1024 × 768 | tablet (touch) | 6 × 2 cards, avatar rail |
| 820 × 1180 | tablet portrait | the same, re-laid out on rotation |
| 390 × 844 | phone (iPhone) | tab bar, mini cards, drawer, full-screen dialogs, **⋯** menu |
| 360 × 800 | phone (Android) | the same, no text overflow |

Walk through each size: new game → rename and re-roll avatars (the moderator's too) → write persona /
memory and refine → start → watch 💭 / ⚡ / 💤 → the wolves' request card → switch viewpoint → budget
limit with force stop pauses the game → raise the limit and continue → continue after the end →
take a seat over before the start (3 · Carol; then N12 for the explosion) → re-deal → start → write in
your turn and end it, see *禁言中* with its reason when it is not your turn → vote and change the vote
on the card → use the explosion (form, second confirmation) → pause → give the seat back → continue →
review all and read the written memory → New game, then clear the memory; ⚙ Settings → export an archive (with and without keys, with and without logs) → import it (right password, wrong password, skip the keys) → the page reloads.
