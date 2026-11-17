# UI Guide

This guide describes the **v0.12.1** web UI. The UI itself is in Chinese; the Chinese label of a
control is given in quotes where it helps you find it. Everything below can be tried without an API
key: start the server with `ARENA_DEMO=1` (scripted bots) or run the UI mock (see
[Working on the UI](#working-on-the-ui)).

## Platforms and layouts

The page adapts itself to the device. A tiny script in `<head>` (`static/js/platform.js`) runs
before the first paint and writes these attributes on `<html>`; the CSS and the scripts only ever
look at them:

| Attribute | Values | Decided by |
|---|---|---|
| `data-platform` | `desktop` · `tablet` · `phone` | user agent (iPhone, Android + `Mobile`, iPad, iPadOS reporting as a Mac with touch points, `userAgentData.mobile`), `(pointer: coarse)` and the viewport |
| `data-layout` | `wide` · `compact` · `phone` | `phone` below 768 px (also a phone held sideways), `compact` up to 1023 px (up to 1024 px on a touch tablet), `wide` otherwise |
| `data-input` | `touch` · `mouse` | `(pointer: coarse)`, touch points, user agent |
| `data-orientation` | `portrait` · `landscape` | viewport |

The values are recomputed on resize and on rotation (no reload). For testing, the URL accepts
`?platform=phone|tablet|desktop` and `?input=touch|mouse`.

| Layout | Typical screens | What changes |
|---|---|---|
| **wide** | desktop ≥ 1024 px | the 12 player cards sit in one row with a summary strip below; the channel column shows names and floor status; the detail card floats next to the clicked player |
| **compact** | tablets, narrow windows (768 – 1023 px) | the cards are 6 × 2; the channel column shrinks to an **avatar rail** (names appear on hover / long-press); everything else as wide |
| **phone** | < 768 px | the top bar only keeps the title, the game status and **Start / Pause / Resume** plus a **⋯** menu; a bottom **tab bar** switches between *Feed*, *Channels* and *Players*; the player details open as a **bottom drawer**; dialogs are full screen; every tap target is at least 40 px |

On touch devices (`data-input="touch"`) buttons, inputs and check boxes get a 40 px (check box rows
44 px) hit area.

## Live game

### Top bar

The first row (desktop / tablet):

1. **Title and version** — a *演示模式* badge is added when the server runs in demo mode.
2. **Workspace** — current work folder name; opens the workspace dialog.
3. **Collapse board** — hides the participant bar to give the feed more room.
4. **Theme** — light / dark.
5. **Viewpoint** — *God view* or any single player (see [Spectating](#spectating)).
6. **Usage** and **Limit** (*上限*) — see [Usage and budget](#usage-and-budget).
7. **New / Save** — start a fresh game, or save and download the current session JSON.
8. **Start / Pause / Resume** — game lifecycle. Timers (speaking time, request deadlines) freeze
   while the game is paused. A **finished** game can still be resumed: the moderator is told that
   the game was continued after the end.

The second row shows the **status** (running / paused / finished, including a moderator `hold`
reason, a pause because of the budget — *因预算上限暂停* — or an automatic pause), the **phase** text
written by the moderator, small **progress chips** for open requests (for example *Voting 7/12*,
*Deliberating 3/4*) and the **moderator chip** at the end of the row. The moderator is not a player
card: it is a small chip with its own pixel avatar and state icon; click it to open its details.

On a **phone** the first row only holds the title, the status and the one enabled lifecycle button;
workspace, theme, viewpoint, limit, new, save, import, sessions, bindings and Provider live in the
**⋯** menu (the very same buttons, moved there while the layout is `phone`).

### Banners

A banner appears under the top bar when something needs attention (a phone shows a one-line version):

| Banner | When |
|---|---|
| **Hold** | the moderator paused the game and gave a reason (*确认后点击「继续」*) |
| **Budget** | the limit was reached (and the game was stopped, or *force stop* is off), or the limit was raised while paused |
| **Stall warning** | the watchdog saw no progress (`session.stall_notice`); the game keeps running |
| **Game over** | winner and summary, plus the hints *continue after the end* and *review all* |
| **Autosave error / connection lost** | the save failed; the event stream is reconnecting |

### Usage and budget

The **usage pill** shows the tokens of the game (all four kinds added: uncached input, cache read,
cache write, output) and the cost. Cost is accumulated from each call's price snapshot; editing a
model price later does not rewrite history. The **Limit** button next to it opens the budget
dialog:

- **Token limit** and **cost limit (USD)** — empty means unlimited; positive numbers only (tokens
  must be whole numbers). The limits belong to the game; the used amounts restart from 0 with every
  new game.
- **Force stop** — when ticked, reaching a limit pauses the game (*因预算上限暂停*). Raise the
  limit (or untick the box) and press **Resume**. When it is not ticked the game keeps running and
  only the colours / banner tell you.
- Progress bars for each limit and the split of the used tokens into the four kinds, with the cache
  hit rate. The pill turns **yellow from 80 %** of a limit and **red at 100 %**; the dialog updates
  live.

### Player cards

Every player is a card in the participant bar:

```
(3)                     [警]    <- round seat number · small badges (sheriff …)
        [ pixel avatar ]
          Carol                 <- nickname (never the internal id)
        [ 狼人 ]          💤    <- identity tag · agent state
```

- **Seat number** in a circle, top-left (the ids of the game are `N1`…`N12`; the UI only shows the
  seat number and the nickname — default nicknames are Alice, Bob, Carol …).
- **The identity is always the main tag.** The sheriff and other public tags are small badges in
  the top-right corner and never replace it. When the current viewpoint may not see the identity,
  the tag is a grey **`?`**.
- **Pixel avatar**: a 20 × 20 sprite composed from the avatar library (`static/avatar/parts.json`),
  drawn with `image-rendering: pixelated`. Without the library the page falls back to a flat
  circle with the seat number.
- **Players who are out** are greyed with a strike-through name.
- **Agent state**, bottom-right — only three exist:

  | Icon | State | Looks |
  |---|---|---|
  | 💭 | **thinking** — a model call is running | pulses |
  | ⚡ | **acting** — executing tools | flashes |
  | 💤 | **sleeping** — nothing to do | still; the tooltip says *等待新消息* (waiting for a new message) or *定时醒来：剩余 N 秒* (timed wake-up, counted down on the game clock) |

  There are no floor, request or *stopped* badges any more (a paused game puts everybody to sleep).
  The same states are shown on the moderator chip. A player's viewpoint only shows its own state.
- In the **god view** a **summary strip** ends the participant bar: faction counts and the sheriff
  (`狼 2/4 · 神 4/4 · 民 3/4 · 警长 Grace`). It is hidden information and is not shown in a
  player's viewpoint.

Next to the cards: **Regenerate all avatars** (*重新生成全部头像*, only before the game starts),
the **Roster** menu (*名册*, see [Roster, memory and review](#roster-memory-and-review)) and, after
the game ended, **Review all** (*复盘全部*).

### Player details

Click a card (or press Enter on it) to open the details: a floating card next to the player on
wide screens, a **bottom drawer** with a backdrop on a phone (drag the handle down, tap the
backdrop or press Esc to close). The same panel exists for the moderator chip.

- **Name** — editable while the game has not started (1 – 20 characters, unique among the
  players; saved with *保存名字*). During a game the name is plain text.
- **Avatar** — *重新生成头像* rolls a new one for this player (before the game starts only).
- **View as this player** — switches the spectator viewpoint.
- **State, tags, model** (with a shortcut to the model binding) and the **metrics**: activations,
  LLM calls, thinking rounds, tool calls, rejected calls, cache hit rate, cost, and the four token
  kinds (uncached input, cache read, cache write, output). These are statistics only — chains of
  thought and tool-call details are never shown; they are written to the plaintext trace log of
  the workspace.
- **Persona** (*性格设定*) and **memory** (*记忆*) — two expandable text editors, empty by
  default, saved in the workspace and loaded into the player's prompt. Both have **Refine with the
  moderator model** (*用主持人模型完善*): the model's *draft is shown first* (*尚未保存*) and
  nothing is saved until you press *采用并保存*; *放弃草稿* throws it away.
- **Review this player** (*复盘此玩家*) after the game ended; see below.

### Channels and the feed

- The **feed** shows one channel at a time: speech, moderator announcements (template messages are
  highlighted), ability declarations, challenges and their replies, and **request cards**. Every
  message carries the sender's pixel avatar and **nickname**. Messages arrive whole — there is no
  streaming. Truncated messages (over the character limit) end with an ellipsis.
- A player id in the moderator's text (`N3`, `N10` …) keeps its characters but gets a dotted
  underline and a tooltip with the nickname (*N3 Carol*) — the text itself is not rewritten.
- The **channel list** has **no "All" channel**: the **public channel** is selected by default (before
  the game starts only the players' private chats exist and nothing is selected). Each channel shows
  its **floor state**:

  | Icon | Floor state |
  |---|---|
  | 🔇 **禁言中** | only the moderator can write (was "closed") |
  | 🎤 | a holder has the floor (with the holder's name) |
  | 📋 queue | the moderator's speaking queue with progress, e.g. *队列 10/12 · 持麦：Kate* |
  | 💬 free discussion | free discussion with a character progress, e.g. *450/2000 字* |
  | ❄ frozen | an ability froze the channel until the moderator rules |
  | 👁 listen | read-only |

  Click a channel to read it; unread counts are shown per channel.
- **Private chats** (one per player) are folded into a **Private chats** group, have a different
  background colour and are titled with the other party's **nickname only** — there is no "private
  chat" in the title.
- **Challenge tickets** keep their own group: who challenged, the status (pending / upheld /
  rejected) and a preview. Click a ticket to jump to that private chat. Players may challenge the
  moderator at any time; the moderator must answer and may close the private chat of a player who
  abuses the feature.
- **Narrow screens** show the channel column as an **avatar rail**: one icon per channel, the
  tickets folded into one numbered icon (it opens the ticket list), the private chats into one
  group icon. Hover (mouse) or long-press (touch) shows the full name. You can also fold the
  column yourself with the **◀** button; its width is draggable on wide screens.

### Request cards

When the moderator asks players something (`open_request`) the question appears **in the feed as a
card** at the place where it was opened, and the **same card** is updated as answers arrive and when
it is closed:

- the question, the kind (*投票* vote, *商议* deliberation, *问答* question), a countdown on the
  game clock (it freezes while the game is paused) and a progress bar (*4/4 已作答*);
- one row per recipient with the player's current choice;
- **shared answers** (*组内可见*, e.g. the wolves' kill): every recipient sees the others' current
  choices live and may change their mind until the request closes — a changed answer is marked
  `↻1` on the same row. The closing rule (*全员一致才结束* / *到时才结束*) is shown on the card;
- **sealed** requests (*密封*, e.g. the daytime vote): other players' answers stay hidden
  (*✓ 已提交*). The god view reads them (marked 🔒);
- when the request closes, the card shows the **result**: a tally with bars (options with 0 votes
  are folded into *其余 N 项 0 票*), the leaders, defaulted answers and missing players.

**Visibility.** The god view sees every request. A player's viewpoint only sees requests addressed
to that player, in the channel where the request lives, and only the answers that player may see.

## Spectating

The viewpoint selector is a pure front-end filter (nothing is requested from the server). In a
player's viewpoint you only see what that player may see:

- messages written while the player was a member of the channel, and request cards addressed to the
  player (shared answers of their group, their own sealed answer);
- only the channels the player belongs to, and their own challenge tickets;
- other players' **public tags** only (alive / out / sheriff …) — their identity shows as `?` — and
  the player's own full tags and state;
- no summary strip.

The dark theme (**Theme** button) is supported everywhere; the pixel avatars get a pale plate so
their dark outlines stay readable.

## Roster, memory and review

The roster is the list of the 12 seats: nickname, avatar, persona and memory. It lives in the
workspace, so names and avatars survive a restart.

- **Names and avatars** can be changed before the game starts (player details, or *Regenerate all
  avatars* in the participant bar). The moderator has a random pixel avatar of its own too; it is
  re-rolled together with the players or in its own panel.
- **Roster menu** (*名册 ▾*):
  - **Export all memory** (*导出全部记忆*) — downloads a zip with every player's persona and
    memory;
  - **Import memory pack** (*导入记忆包…*) — picks a zip and first shows a **preview** of whose
    texts will be overwritten (*Carol: persona, memory*); nothing changes until you confirm;
  - **Clear all memory** (*清除全部记忆…*) — a second confirmation, with a check box to clear the
    personas too; it cannot be undone, so export first;
  - **Load memory in this game** (*本局加载记忆*) — a switch; off means the players' prompts of
    this game contain no memory section (the files are untouched).
  Import, clear and the switch are only available before the game starts (press **New** after a
  finished game); export is always available.
- **Review** is manual: nothing is written to the memory after a game by itself. When the game is
  **finished**, *Review all* (*复盘全部*, participant bar) or *Review this player* (player details)
  asks the players to review the game with their own models; the stats and the recap are appended
  to their memory files. The button shows the progress (*复盘中 3/12…*) and ends as *✓ 已复盘*;
  the player details show the same and a *查看写入的记忆* button that opens the memory editor.

## Workspace dialog

![Workspace dialog](images/workspace-modal.png)

- **Current workspace**: name, path (redacted in the screenshot), status and last session ID.
- **Recent workspaces**: click a path to fill the input.
- **Select folder**: opens the server-side native folder dialog. It is only available to loopback
  clients.
- **Manual absolute path**: fallback for headless/Docker or when the native dialog is unavailable.
- **Initialize existing non-empty folder**: required before creating a workspace marker inside a
  folder that already contains files. Existing files are not moved or deleted.
- **Create if missing**: creates the folder and workspace marker when the target does not exist.
- **Switch workspace**: only works while the game is idle/paused/finished. The current game is
  saved first; a failed save or invalid target leaves the current workspace unchanged. The page
  reloads after a successful switch.
- **Load a game from this workspace**: lists encrypted sessions in the current workspace.

While a game is running, switching is disabled and the dialog shows a pause-first hint.

## Provider and model setup

1. **New Provider** — enter a display name.
2. **Protocol** — OpenAI Chat Completions, OpenAI Responses, or Anthropic Messages.
3. **Base URL** — root URL when auto-append is on; complete endpoint when auto-append is off.
4. **Auto-append API path** — shows the final request/model URLs live. A warning is shown for
   plain HTTP, unusual version paths, or a complete endpoint used in auto mode.
5. **API Key** — write-only. After saving, only “Key configured” is shown. Changing the protocol,
   Base URL or auto-append mode requires re-entering the key.
6. **Provider list** — read-only environment Providers cannot be edited or deleted.
7. **Price source** (*价格源*, top of the dialog) — the URL of a LiteLLM-style price table
   (`model_prices_and_context_window.json`; the default points at the LiteLLM repository). Only
   `https://` URLs are accepted. **Save** stores the URL, **Refresh price table** (*刷新价格表*)
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
10. **Edit price / Remove** — edit a model independently or remove it when no binding/template uses
    it.

When auto-append is off, the model-list URL is unavailable and fetch is disabled; type a model ID
manually. See [providers.md](providers.md).

## Templates and bindings

![Templates and bindings](images/templates-bindings.png)

- Each of **Template 1/2/3** stores Provider + model + reasoning depth. Edit the fields and click
  **Save templates**.
- Check the moderator and/or players (listed by nickname), then click **Apply template 1/2/3**.
  Applying is blocked while a game runs; editing templates is allowed.
- **Advanced manual bindings** remains available for per-player overrides.
- A template whose Provider/model was removed is marked invalid and cannot be applied.
- **The moderator is an LLM agent like every player and always needs a model binding.** A game
  cannot start until the moderator and all players are bound.
- The **reasoning depth** (low / medium / high / xhigh) is the model's thinking effort and is set
  per agent here. An agent can call tools repeatedly within one activation; there is no round limit
  and no "execution depth" setting any more.

## Layout controls

- Click **Collapse board** in the top bar; click again to restore.
- The channel column has a **◀** button in its header that folds it to the avatar rail (the rail is
  also used automatically on narrow screens); on wide screens its width is draggable and the board
  height too.
- Double-click a divider to restore its default size.
- Collapsed/resized state and the viewpoint are stored in `localStorage`.

## Sessions and recovery

- **Sessions** lists stored sessions for the active workspace.
- Loading a session keeps it paused. Review model bindings, then click **Resume**.
- On startup, the last session is restored as paused. If it is corrupt, the files are kept and a
  recovery banner is shown; the game is not overwritten.
- **Saves written by v0.11 or earlier cannot be opened** (save format v3). They stay in the list
  untouched; a fresh game is created and a notice explains why.
- If the API is protected with `ARENA_API_TOKEN`, a token dialog appears only after a 401. The
  token is stored in browser localStorage.
- The event stream is resumable: after a dropped connection the page reconnects and replays only
  the missed events (or fetches a fresh snapshot if too much was missed).

## First game checklist

1. *(No API key? Start the server with `ARENA_DEMO=1` and skip to step 5 — scripted bots play.)*
2. Select a workspace, add a Provider and its models (use **Refresh price table** and **Fetch
   models** to fill the prices).
3. Create or apply model bindings for the moderator **and** the players.
4. Optional: rename the players, re-roll avatars, write personas and memories (**Refine** helps),
   decide whether this game loads memory, and set a token / cost limit with **Limit**.
5. Click **Start**; the moderator opens the game by following the scenario's manual.
6. Use **Pause** at any time; do not switch workspace/model while running.
7. After the verdict you can **Resume** (continue after the end) or **Review all** to write the game
   into the players' memories.
8. Save or rely on autosave; use Sessions to load an earlier game.
9. Thoughts, tool calls and receipts are in `<workspace>/.arena/logs/<session>/trace-*.jsonl`.

## Working on the UI

The front end is plain JavaScript without a build step: `static/index.html`, `static/css/style.css`
and `static/js/` (`core` · `avatar` · `platform` · `store` · `view` · `layout` · `providers` ·
`roster` · `budget` · `workspace` · `app`; `core`, `avatar`, `platform`, `store` and `view` are pure
and have no DOM access).

### The UI mock

`tools/mock_ui_server.py` serves the real `static/` directory and a scripted 12-player game (about
a minute at speed 1: N1…N12 with the nicknames Alice …, the wolves' shared kill with a changed
answer, a sealed vote, challenge tickets, the white wolf king's explosion, a token budget that warns
at 80 % and pauses at 100 %, the end, the review) behind the v0.12.1 API shapes:

```
.venv\Scripts\python.exe tools\mock_ui_server.py --port 8731      # http://127.0.0.1:8731/
  --speed 2        replay speed multiplier        --seed 7        another script
  --budget-tokens N  demo limit (0 = none)        --demo          show the 演示模式 badge
  --selfcheck      replay offline and validate every event against the contract
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
| `GET /api/mock/info` | speed, latest event id, game clock, budget … |
| `GET /api/mock/frame?w=390&h=844&q=platform%3Dphone%26input%3Dtouch` | the page inside an iframe of exactly `w × h` CSS pixels (own viewport), for layout checks at any size on a desktop browser |

`POST /api/control/new` deals a fresh game; the budget limits survive it, the used amounts restart.

### Tests

```
node tests/frontend/test_view.js                       # one file (pure logic: store, view, core, avatar, platform)
PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_frontend_js.py tests/frontend/test_mock_server.py -q -p no:cacheprovider
```

`tests/test_frontend_js.py` syntax-checks every `static/js/*.js` and runs every
`tests/frontend/test_*.js` (fake DOM harness: `tests/frontend/harness.js`, fixtures:
`tests/frontend/fixtures.js`, a minimal sprite library: `tests/frontend/fixtures/parts.min.json`).
`tests/frontend/test_mock_server.py` checks the mock against the contract and reduces its whole
event log with the **real** client store (`replay_check.js`) to the session the mock holds.

### Browser matrix

Before a release the UI is checked in a real browser against the mock at these sizes, in light and
dark themes, through `/api/mock/frame` (the platform simulated with `?platform=` / `?input=`):

| Viewport | Platform | Check |
|---|---|---|
| 1920 × 1080 | desktop | full layout, summary strip, detail card, budget dialog, price source |
| 1440 × 900 | desktop | reference layout |
| 1280 × 800 | desktop | smaller cards, truncated names |
| 1024 × 768 | tablet (touch) | 6 × 2 cards, avatar rail |
| 820 × 1180 | tablet portrait | the same, re-laid out on rotation |
| 390 × 844 | phone (iPhone) | tab bar, mini cards, drawer, full-screen dialogs, **⋯** menu |
| 360 × 800 | phone (Android) | the same, no text overflow |

Each size is walked through: new game → rename and re-roll avatars → write persona / memory and
refine → start → watch 💭 / ⚡ / 💤 → the wolves' request card → switch viewpoint → budget limit with
force stop pauses the game → raise the limit and continue → continue after the end → review all and
read the written memory → New game, then export / clear / re-import the memory pack.
