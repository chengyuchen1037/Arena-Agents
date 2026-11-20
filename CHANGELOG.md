# Release Notes

## 0.14.0

0.14.0 replaces the *seat roster with a persona and a diary* by **bots**: a player is a bot with a name, an avatar and a
**layered memory** that it builds itself, the engine is separated from the bot, and the version ships a **benchmark** so
that "do the players sound different?" and "do they hold on to the core logic instead of the most fluent speech?" can be
measured instead of felt. Plan and decisions (H1–H59): [docs/v0.14-plan.md](docs/v0.14-plan.md); contract:
[docs/v0.12-contracts.md](docs/v0.12-contracts.md) section 13.

Why: in eight real games (249 public speeches) 53 % of the speeches were cut at the 300-character cap, 98 % had several
paragraphs, about half were numbered lists, 31 % began with the speaker's own seat id, and the same stock phrases
appeared in almost every seat; agents also preferred the *coherent* story over the blunt, correct one (for example
distrusting a human seer whose first-night check had "no reason"). The persona had hardly any effect because it was one
short paragraph in a prompt that taught every agent how to write a work report.

### Bots and four memory layers

- **Bots instead of seats.** `data/bots/<id>/` holds a bot; `data/bots/lineup.json` orders the twelve seats. Names and
  avatars belong to the bot, not to the seat; seats can be rearranged by dragging a card (before the game starts). The
  moderator is not a bot.
- **L0 — core (the user's):** `identity` (who I am), `voice` (how I speak, with sample lines and a length range) and
  `mind` (how I analyse), plus up to three extra sections. Only the user changes it (the editor, an `.md` import, a
  default-library import, or by accepting a proposal from a review). It is never truncated in the prompt. Twelve default
  bots (*Alice … Leo*) ship in `bots/`: distinct voices, no game vocabulary and no play advice.
- **L1 — architecture:** the *official contract* (`data/contract/contract.md`: how the platform works — a short
  must-read part that is mounted into every prompt and a reference part read on demand with `read_contract`) and the bot's
  own *operating notes*.
- **L2 — rules:** the *scenario* plus the bot's own *digest*, *playbook* (≤ 20 hypotheses, each with a confidence, a
  support and a counter-example count and a source), *open questions* and its *impressions of the other players* (keyed by
  name). Study materials can be added per scenario (empty by default; `knowledge/` stays moderator-only).
- **L3 — the game:** the status board, a hard-facts table (what the rules produced versus what a player *claimed*), the
  visible history, notes. The prompts **do not teach how to play**: the bot reads the contract and the rules and writes its
  own understanding as hypotheses to be tested.
- The system prompt of a game is byte-stable (so provider prefix caching keeps working); event hints (whose turn it is, the
  playbook entries that match the situation, "settle the one or two things that matter, then say it in full") are appended
  to the wake-up messages only. Memory is rendered as first-person prose, never as bullet lists, so it is not imitated.
  In the visible history a bot reads a speech as `N3 Carol 说：…` (its own as `N3 Carol（你）说：…`), so the log no longer shows it the shape
  `N3：…` to imitate; the status board states the limit of ONE message for a free discussion and no "total" for a floor of a single message.

### Initialization and review

- **Initialization** (⚙ *Initialize all* or per player, 120 s, all-or-nothing): the bot reads the contract, the rules and
  the materials and writes its operating notes, digest and playbook (and, if it wants, open questions). A bot that is not
  initialized (○), whose last initialization failed (✖), or whose core / contract / scenario / materials changed afterwards
  (⚠) **blocks the start** with a per-bot list of what to fix (the start check also covers the contract, the scenario and the
  model bindings). After initialization the name and avatar are frozen (🔒); changing them asks for confirmation and
  resets the bot's memory (the core is kept).
- **Review** (after the game ends, 60 s, what has been written is kept): each bot compares its judgements with the revealed
  truth and revises its own memory and its impressions of people (style, credibility and habits — never "who they are").
  A bot can only *propose* changes to its core; the user accepts or rejects them. If a person played a seat, the review
  can learn that player's style — as a proposal — and the other bots are never told who the human was. Deleting a game
  does not remove what a review already taught (the dialog says so).
- Both runs are independent loops (not the game's actors), with a wall-clock limit and a hard cap on rounds. An
  initialization is paid from a separate *learning usage* ledger (never a game's budget); a review is counted in the game
  it is about; the call log marks them `init` / `review`.
- Demo mode (`ARENA_DEMO=1`): the twelve bots start out ✔ and initialization and review are answered by the scripted demo
  bots with fixed answers, so the whole flow can be tried without a model.

### Speech: nothing is cut, nothing is rewritten

- A player's speech over the limit is **sent back with `TOO_LONG`** (nothing is posted, the floor and quota are kept, the
  receipt says how many characters it was and what the limit is) instead of being truncated; the person's input box counts
  characters and keeps what was typed. The per-message limit stays 300.
- A bot's speech that reads like a report — lines of a list, a heading or bold, a seat id as the first words — is sent back
  once per speaking turn with `FORMAT`; the bot rewrites it **in its own words** (a voice that really talks in enumerations says
  so with a line `允许口头列举` in its `voice` and the list check is switched off for it). Nobody rewrites, wraps or "assists" a bot's
  speech: speech only ever comes from the bot's own worker calling `speak` (tests assert that no code path submits for it).
- Every bot thinks without a "keep it short" instruction, and a player no longer has `list_documents` / `read_document`
  (the moderator keeps them) but has `jot` (a clue for the review) and `read_contract`.

### Identities are dealt when the game starts

- Nothing about identities exists before the game starts — not in the state, not in a card, not as a grey "?". The deal
  is a single step at the start; **re-dealing was removed**. A person who takes over a seat before the start no longer
  "already knows the role".

### Interface

- **Settings** has five collapsible sections: ① the whole archive (export / import), ② core files (clear all / one, preview,
  export, import, generate), ③ the official contract (import / export / restore default; a change marks every bot as needing
  re-initialization), ④ rules and study materials, ⑤ game management (per-game delete with the review-memory warning,
  single-game `.arenagame` export / import, jump to history).
- The seat number is a translucent bubble; ✔ ⚠ ✖ ⏳ ○ next to a name show the initialization state; a system status tag
  (*not initialized / initializing / before the game / in game / paused / finished / reviewing*) and a view tag (*global view /
  Alice's view / Alice's view (taking over)*) sit at the start of the second row.
- The start-check dialog, the initialization dialog (with per-bot progress) and the end-of-game review dialog (60 s
  countdown per bot, "learn the taker-over's style" switch, a summary of what changed and the number of proposals waiting).
- Downloads of single files (a bot, a core file, the contract, a scenario, a game) send the token as a header and are named well
  (`<name>-<time>.arenabot`, `<name>.core.md`, `contract-v0.14.0.md`, `game-<id>-<date>.arenagame`, …); only the download of the whole
  archive (`arena-agents-<time>.arenaagents`) is still a link that carries `?token=`.

### Files, archives and compatibility

- **Archive format 2** (format 1 is still read): contract, rules and materials, all bots with their memories and proposals,
  the lineup, the learning usage, all games (with their previous copy) and **all** logs, the learning logs included (the "logs
  optional" step is gone). The API-key handling is the v0.13 design, unchanged. Also new: **`.arenabot`** (one bot, exchanged
  between users; import overwrites that bot) and **`.arenagame`** (one game with its logs; id conflict → copy / overwrite /
  cancel).
- **The engine reads the contract and the scenario from the data directory** (`data/contract/`, `data/rules/<id>/`); the shipped
  `prompts/contract.md` and the scenario `config.yml` names (`scenario.path`) are only the defaults - copied on the first run and
  by *restore default*. A default you never changed is refreshed when a newer program ships another one (every bot then shows ⚠);
  one you imported or edited by hand is never touched.
- **Upgrading from 0.13.x is automatic**: the first start converts `data/roster/` into twelve bots (names, avatars, the
  persona mapped onto core files, impressions re-keyed by name, "experience" lines become low-confidence playbook entries,
  records and per-seat model bindings kept; a bot whose persona was empty and whose name is one of the defaults gets that
  default's core). `roster/` is **not touched**. Nothing is initialized yet: use *Initialize all* once.
- **A hand-written persona loses nothing.** One without headings (typed in by hand, not made by the moderator-model template) becomes the
  identity up to 300 characters, cut where a sentence ends, and goes on - word for word, white space kept - in up to three
  extra core sections *原有性格（续）*, *（续二）*, *（续三）* (300 characters each). Its voice and way of thinking stay empty
  and are **not** filled in from the defaults (a default voice does not fit a persona somebody else wrote): the start check
  names them, and *✨ Complete with the moderator's model* writes them from what is there. A persona with headings whose
  section is longer than its new limit overflows into the extra sections that are still free before anything is cut; only
  what does not fit in the four places (about 1200 characters) ends with an ellipsis.
- **Games saved by 0.13.x (save format 5) still open, replay and continue** — with the old bot behaviour
  (`LegacyBrain`, byte-identical to 0.13.1, no start check) — but cannot be reviewed. New games are save format 6.
- Removed: `/api/roster/**`, *re-deal* (`/api/control/shuffle-identities`, `takeover.reshuffle_available`), the persona /
  memory editors of the player panel (replaced by the bot panel).

### Benchmark

- `bench/` (kept in the repository) measures what v0.14 set out to change, against the v0.13.1 player:
  - **Metrics and baseline**: loaders for saved games and traces; **R** (runtime health), **S1** (form) and **S3** (homogeneity), computed on
    saved games (`python -m bench audit | baseline`) and on runs; the v0.13.1 baseline measured on the eight real games
    (`bench/reports/baseline-v0.13.1.json`); the acceptance gates (`bench/gates.yml`).
  - **Runner**: a bot (a v0.14 bot, the v0.13.1 player `legacy`, or an ablation with one memory layer taken away) is driven through the **real worker
    loop** to the decision point of a *situation script* and acts there. A fake model by default; a real one only with `--real --max-calls N`.
    A situation can be cut from a saved game (`python -m bench replay --emit`).
  - **Question bank**: 93 situations in 8 suites — R (runtime), S2 (style), L1 (expression invariance, 7 A/B pairs), L2 (trivia traps), L3 (the
    decisive fact), L4 (real decision points with every word rewritten), M2 (recall of early facts), M3 (rules quiz) — written the way the
    moderator's manual plays the game and checked on the real engine (`python -m bench situations --check`). `python -m bench suites` lists them
    with the model calls each tier (smoke / quick / full) would make; the quick tier is about 300.
  - **Judges** are DSH sub-agents, never the player model: rubric files (`bench/judges/*.md`), `judge-pack` → a DSH `workflow`
    (`judge.workflow.js`) → `judge-merge`; every verdict is checked against the rubric's schema and the judge never learns which bot, run or
    question it is looking at. A rubric counts for acceptance only when the judge agrees with **you** (Cohen's κ ≥ 0.6): `calibration-set` draws
    a blinded sample, `bench/label/index.html` is the offline page to label it, `calibrate` computes κ.
  - **Report**: `python -m bench report` computes every number with its interval and n (two-level bootstrap), checks every gate (passed / failed /
    awaiting baseline / awaiting calibration / reference only / not run) and writes `bench/reports/<version>/` together with the raw files.
  - `bench/fixtures` is out of reach of every learning tool.
- The tests never touch your data: `tests/conftest.py` points `ARENA_DATA_DIR` at a throw-away directory for the whole test
  process and warns in the summary if `data/` changed.

### Developer tools

- `tools/real_smoke.py` was rebuilt for bots: in a scratch data directory the bots are initialized first (`--init fixed` commits a
  fixed initialization without a model, `--init real` lets them really initialize) and then a small game is played; the report shows
  the speeches the engine sent back (`TOO_LONG` / `FORMAT`), the prompt-cache hit rate and how much memory was mounted.
- New `tools/init_default_bots.py` runs one real initialization of the default bot library (`--write-library` is what writes the
  result into `bots/`, `--check` verifies that it is up to date); `tests/test_bots_library_fresh.py` goes red when a shipped bot's
  initialization no longer matches the contract, the default scenario or its core. Both scripts have a hard call budget, only read
  your data directory and are never run by a test; see [docs/real-model-runs.md](docs/real-model-runs.md).

### Documentation

- New: [docs/bots.md](docs/bots.md) (writing and editing a bot: core files, the four memory layers, initialization and review),
  [bots/README.md](bots/README.md) (the default library), [docs/real-model-runs.md](docs/real-model-runs.md),
  [bench/README.md](bench/README.md), [docs/v0.14-plan.md](docs/v0.14-plan.md) and contract section 13. The READMEs, the
  architecture pages (a "Bot = Brain + memory" section) and their pictures (bots, the start check, save format 6),
  `docs/storage.md` and `docs/archive.md` follow v0.14. `tests/test_docs.py` also checks that the numbers the pages quote about
  bots (limits, the 120 s / 60 s of a learning run, the Composer's budgets) are the constants of the code.

## 0.13.1

0.13.1 makes taking over a seat practical for a whole game: **soft-pause only the agents**, **take your time on
public floors**, **mark seats privately**, **confirm choice requests before submitting**, inspect **thinking
records** and **LLM errors**, recreate avatars from a **deterministic code**, and keep bounded **impressions and
behaviour guidance** instead of an ever-growing per-game diary. Plan and decisions (G1–G45):
[docs/v0.13.1-plan.md](docs/v0.13.1-plan.md); contract: [docs/v0.12-contracts.md](docs/v0.12-contracts.md)
section 12.

### Soft pause, untimed public floors and safe answers

- The take-over bar has *⏸ Pause agents*. It is available only while playing a seat in a running game. The clock
  freezes at the click; calls already on the wire and tool batches already started finish, no new model call
  starts, and the UI moves through *finishing N answers* to *agents paused*. Calls still waiting for a concurrency
  slot return the slot and wait at the agent gate. *▶ Continue* opens the gate and merges everything that arrived
  meanwhile into the next activation. *Pause now* is still available as the existing hard pause.
- Soft pause is not a world event: it writes no ledger entry, sends no envelope to an agent and enters no save.
  Hard pause, finish, new/load/import and close clear it. While draining or held, an interrupt becomes a pending
  message rather than cancelling an answer that is already on the wire; the watchdog stays quiet.
- The person can still read, speak, pass, answer and use abilities. While the person holds a floor in a
  `type=public` channel, that floor is **untimed** (`deadline=null`); other channels and agent-held floors keep
  their timing. Requests such as votes keep their deadlines, and the moderator can still close a floor.
- Choice requests are **pick, then submit**. Clicking an option only selects it in the browser; *Submit / Change
  answer* calls the existing seat-response endpoint. A refused submission keeps the pick, a submitted answer can
  be changed while the request is open, and an unsubmitted pick is never sent automatically at the deadline. The
  last 15 game seconds show an explicit unsubmitted warning.

### Private seat marks

- While playing a seat, every other player has an eight-colour palette; every colour also has a different shape.
  The mark appears on the player card, before names in the feed and private chats, on request recipients and in
  the detail panel. No textual or game meaning is assigned to a colour.
- Marks are per-game browser state (`localStorage`, latest 50 games), synchronised between tabs and removed with
  the game. They are not in the save, archive, replay, `PublicSession` or any agent prompt. After leaving the
  take-over they remain visible read-only in the same browser; a replay shows none.

### Thinking record and LLM call log

- Player and moderator details have *🧠 Thinking record*: a large chat-like window with wake-up dividers, game
  time, round and model, newest at the bottom. It pages backwards through the current game's plaintext trace,
  polls for new rows every three seconds while open, and contains only `thought` / `content` — **no tool calls**.
  During an unfinished take-over, other players' records answer 409; the played seat, finished games and ordinary
  spectating remain available. Thoughts do not enter SSE, replay or `PublicSession`.
- The top-right *📋 Log* lists every LLM attempt and runtime error of the current game: actor, provider / model,
  purpose, attempt, elapsed time, status, token counts and a redacted error. It has actor / error-only filters,
  expandable rows, an unread-error badge and read marks. The ring holds 2000 entries and is rebuilt from the trace
  after a restart; renumbered ids are re-based by timestamp so a rebuild cannot hide a new unread error.
- Call metadata is trusted-client only in both REST and SSE (including `Last-Event-ID` replay); prompts, model
  answers and API keys never enter it. Trace readers release file handles as soon as a page is complete, including
  on Windows, and logging / error introspection can never replace the original model result or exception.

### Deterministic avatars, structured memory and persona guidance

- Every avatar has a compact code made from its eight part ids. Copy it and enter it for another seat to recreate
  exactly the same avatar; random regeneration and by-code generation are available before a game starts.
  Python and JavaScript share format vectors, validation reasons and an explicit ignored-character list.
- `N*.memory.md` now has `## 战绩 / ## 印象 / ## 经验`, plus `## 旧记忆（待整理）` only for migrated text. Each of
  the other 11 fixed seats has one updatable impression (80 characters), rather than a new note appended after
  every game. A seat's own experience has at most 3 items / 300 characters; editable memory is capped at 1500 and
  legacy text at 600. Unknown seats and placeholder answers are not stored.
- A manual post-game review updates impressions and experience and still computes the record in the engine. It
  merges against the exact memory shown to the model: an edit or clear made while the review is running is not
  overwritten. Empty / no-change answers do not discard legacy notes. Old headings and free text migrate in memory
  without an LLM; UTF-8 BOM is accepted, and the file is rewritten only on the next save or review.
- The moderator-model *complete my persona* action returns a bounded behavioural template — positioning, speech
  style, thinking, decisions, under pressure and catchphrase — rather than an unconstrained biography. Manual
  persona text is limited to 500 characters (40 per generated item). Template substitution is single-pass, so
  persona / memory text cannot inject the moderator manual into a player's prompt.
- Roster updates are atomic: a bad name, avatar, persona or memory field changes nothing. New nicknames reject
  colons (which would corrupt impression labels), invalid Unicode and duplicates with a clear Chinese error.

### Compatibility

The **save format remains v5** and the **archive format remains 1**. No conversion is needed from 0.13.0. Soft
pause, untimed-holder state, colour marks and read marks are deliberately not saved; existing saves and archives
remain readable. v0.13 still cannot open v0.12.1 data directly: use the existing conversion script described in
[docs/storage.md](docs/storage.md#upgrading-from-v0121).

Archive import also accepts ordinary Unix ZIP entries that carry permission bits without an explicit file-type field;
symbolic links, directories and other special files are still refused.

## 0.13.0

0.13.0 does four things: **a person can play a seat**, **the storage was redone** (one data directory in the
program folder, only the API keys encrypted), **everything can be exported and imported as one `.arenaagents`
file**, and **past games can be listed, continued, deleted and replayed**. Plan and decisions (F1–F38):
[docs/v0.13-plan.md](docs/v0.13-plan.md); contract: [docs/v0.12-contracts.md](docs/v0.12-contracts.md) section 11.
**v0.13 cannot open v0.12.1 data directly** — see *Upgrading from 0.12.1* below.

### Playing a seat (take-over)

- **Viewpoint bar `[viewpoint ▾] [Switch] [Take over]`.** The drop-down only chooses; *Switch* applies it
  (it used to switch on change). *Take over* plays the chosen seat: only a player seat (never the moderator), one
  seat at a time, only while the game has **not started or is paused**, after a confirmation. The server checks
  the same conditions (409).
- **While a seat is played** its agent is not built at all (no calls, no tokens), the viewpoint is locked to the
  seat (front end only, see *Known limitations*), and the bar becomes *🎮 Playing 3 · Carol [Leave]*. Leaving is
  allowed whenever the game is not running (not started, paused, finished), after a confirmation. The agent's
  conversation is rebuilt on leaving: its next activation gets a full first message whose history contains what
  the person said as the seat's own words. **Nobody is told** — not the moderator, not the other agents, not the
  agent itself; spectators see a *🎮* mark on the card. The take-over is saved with the game and comes back
  *paused and still taken over* after a restart; the model binding of the seat is still required (leaving and the
  post-game review need it).
- **Re-deal is back** (`POST /api/control/shuffle-identities` had no button since 0.12): taking over before the
  game started shows *you already know your identity — re-deal?* with a button. **Once the game has started it is
  strictly refused**: the button is hidden, the server answers 409 whenever the game is not `idle` or its ledger
  holds a start record, and *Start* and *Re-deal* run under one control lock, so a double click or two tabs cannot
  re-deal a started game.
- **One input box per channel**, like a group-chat app: switching the channel switches the box; every channel
  keeps its own draft; Enter sends, Shift+Enter breaks the line; a counter shows the length against the limit the
  server computed for *this* seat in *this* channel (turn token, free discussion or private chat). When you cannot
  speak (not your turn, your turn is over, the channel is closed / frozen / listen-only / read-only, no messages
  left) you can still type, and the send button turns grey and says *禁言中* with the reason on hover; a paused,
  not started or finished game says so instead. *End my turn* (`pass`) shows only while you hold the floor.
- **Abilities** (the white wolf's explosion is the only one of the bundled scenario) get an *Ability* button that
  lists what the moderator enabled for you; its form is generated from the scenario's parameter types (a player
  picker for `player_id`, a limited text field for `text(N)`), with a confirmation before it is released.
  **Request cards are clickable**: choice buttons, a text box for text requests, changing an answer while the
  request allows it, a countdown; the hunter's shot stays a request from the moderator. Channels where something
  waits for you carry a *your move* mark. Challenging the moderator is done in the private chat with the moderator;
  speech templates are not offered to a person.
- **The server computes what the seat may do** (`app/world/seat.py: seat_status`, locked to the engine's own
  `speech_gate` by tests over every channel mode) and pushes it as `takeover.updated`; the front end never guesses.
  Actions go through `POST /api/takeover`, `/api/takeover/release` and `/api/seat/{speak,pass,respond,ability}`;
  the actor is always the taken-over seat (a client cannot name another one) and every action passes the same
  commit gate as an agent's — turns, length, quotas, deadlines. Timers are unchanged: a slow person is resolved
  by the engine's defaults, the screen shows the countdown.

### Storage

- **One data directory, `<program dir>/data/`** (the folder that holds `app/` and `config.yml`, whatever the
  current directory is). `ARENA_DATA_DIR` overrides it; demo mode without it uses `.demo-data/demo`; Docker uses
  `/app/data` (a new volume, `arena-appdata`). `data/` is git-ignored and docker-ignored; an OS lock on
  `data/.lock` lets only one server use a directory.
- **The workspace concept is gone**: the 工作文件夹 button and dialog, switching, the recent list, `app-state.json`,
  the "data may not sit inside the project folder" rule, and `ARENA_WORKSPACE_DIR` / `ARENA_DISABLE_WORKSPACE`.
  The automatic migration of a v0.9 `.env` / `saves/` (and `ARENA_DISABLE_LEGACY_MIGRATION`) is removed too.
- **Only the API keys are encrypted** (`secrets.v1.enc`, AES-256-GCM, master key from DPAPI / a key file / the
  environment, fail-closed). `providers.json`, `settings.json`, the roster, every game (`games/<id>/game.json`,
  plus the previous good copy) and the logs are plain, readable files. `games/index.json` caches the history
  summaries and is rebuilt whenever it does not match the folders.
- **Save format v5**: `takeover`, `human_spans` (which stretch of the ledger a person played which seat) and
  `created_at` / `started_at` / `finished_at`. The old encrypted session store is gone.
- **Removed from the UI and the API**: top-bar *Save* (games autosave), *Import* of one game as JSON, the
  *Sessions* window (the history replaces it) and the roster menu's memory-pack zip export / import (clearing the
  memory and the "load memory" switch stay). Nothing can be exported or imported except through the settings.

### Archives (`.arenaagents`)

- **⚙ Settings has exactly two items: Export archive and Import archive.** The file is a zip container with a
  `manifest.json` (format and application version, archive id, the size and SHA-256 of every file, content
  counts, a hash over the file list). **Nothing in it is encrypted except the API keys.**
- **Export**: asks whether to include the API keys; if so, a strong password twice (≥ 12 characters, three of
  four character classes; the same rule in the browser and on the server) → `scrypt` (n = 2^17, r = 8, p = 1,
  random 16-byte salt) → AES-256-GCM, bound to the archive id; the password is never written anywhere. Optionally
  includes the thinking logs. Runs as a background job with progress (SSE `archive.progress`), works while a game
  runs, writes `data/exports/arena-<time>.arenaagents` (via a `.part` file, so a partial archive never exists)
  and then shows the path and a download button.
- **Import**: upload with progress (streamed to `data/tmp/`, 2 GiB limit, `ARENA_MAX_ARCHIVE_BYTES`) → verify the
  format version (newer formats are refused), the manifest, every member's path, size and SHA-256, zip-bomb
  limits, every game against the v5 model → a summary → the password, or *skip the API keys* → replace the data
  as a whole. The old data is first moved to `data/backups/<time>/` (three are kept) and any failure rolls back
  by itself. A skipped key keeps the local key of the same provider id; other providers show *未设置 API Key*.
  Refused while a game runs.
- **v0.12.1 → archive conversion**: `tools/migrate_v0121_to_archive.py [--workspace …] [--output …]
  [--include-keys] [--no-logs]` reads an old workspace (read-only; same Windows user for DPAPI, or the master key),
  converts v4 games to v5, carries the settings, roster, price table and logs along and writes one archive; games
  of v0.12.0 and earlier are listed and skipped. On the first start with an empty data directory the page offers
  an explanation once if the old default workspace exists (dismissible).

### History and replay

- **📜 History** replaces the Sessions window: every saved game with date, scenario, result, game-time length,
  tokens / cost and a *🎮 person played* badge. **Replay**, **Continue** (it becomes the live game, paused; the
  current game is saved first) and **Delete** (the game and its logs; the confirmation says *deleting a game does
  not affect the agents' memory*, which holds because persona and memory live in the roster, not in a game). The
  live game can be neither deleted nor loaded again; unreadable games stay listed and deletable.
- **Replay** projects the saved ledger into the same spectator events the live stream sends (`app/game/replay.py`)
  and plays them through the ordinary client store in a session of its own — the live game keeps following its
  stream in the background and is shown exactly as it is when you leave. A bar offers ⏮, ▶/⏸, 0.5×–8×, a seek
  bar (backwards = rebuild from the start in one loop), *skip idle* (gaps over 5 game seconds take half a second)
  and the viewpoint switch; the live controls and *Take over* are disabled meanwhile. A replay shows what
  spectators saw — messages, channels and floors, requests and results, tags, phases — not thoughts, tool calls
  or agent states (they are not in the ledger). The live game cannot be replayed while it runs or while a seat is
  played.
- **Verified against the real world**: `tests/v12/world/test_replay.py` plays a 51-step script on the real engine
  and compares the live events with the projected ones after *every* step; `tests/test_replay_client.py` replays
  a whole demo game through the real `static/js/store.js` and compares the result with the server's session.

### Upgrading from 0.12.1

1. Run `python tools/migrate_v0121_to_archive.py` (see above) with the Windows user that used 0.12.1.
2. Start 0.13 and import the file in **⚙ Settings → Import archive**.
3. Docker: the data of the old `arena-data` volume is not touched; see [docs/storage.md](docs/storage.md).

### Known limitations

- **The viewpoint lock of a played seat is front-end only**: the server still sends the browser the full spectator
  data, so a person who opens the developer tools can see every role (by design: a local single-user game).
- A checksum detects damage, not tampering: an archive is not signed. The sealed keys carry their provider's
  endpoint, so a later endpoint change makes the import refuse them; the plain provider registry itself is not
  signed — after importing an archive of unknown origin, check the providers' base URLs before entering keys.
- A replay holds only the spectator view; agents' thoughts live in the trace logs and are not replayed.
- The data lives in the program folder: deleting it, re-cloning or `git clean -xdf` takes the games with it —
  export an archive now and then.

### Verification

- `python -m pytest -q -p no:cacheprovider tests`: **2788 passed, 9 skipped, 101 subtests passed**, including the
  node-based front-end suites and the real-world / replay comparisons. Real-browser flows were exercised with the
  demo backend and the mock server at desktop, tablet and phone sizes; the plan's acceptance checklist is checked.
- Docker was not built on the development machine (no Docker installed); the compose and CI configuration was
  syntax-checked and the CI Docker job performs the image verification.

### Documentation

- New: [docs/storage.md](docs/storage.md) (replaces `docs/workspaces.md`), [docs/archive.md](docs/archive.md),
  [docs/v0.13-plan.md](docs/v0.13-plan.md), contract section 11; the READMEs, the architecture pages and pictures,
  the UI guide and the provider guide follow v0.13.

## 0.12.1

0.12.1 keeps the 0.12 architecture and changes its philosophy: **the engine no longer saves money for
you.** Every limit that used to throttle the agents (ambient messages, debounce, per-run round caps, the
"execution depth" selector, the watchdog's automatic pause) is gone. Tokens are managed by *orchestration*
— persistent conversations, prompt caching, in-flight merging and on-demand compaction — and what a game
may cost is a decision of the user (a per-game spending limit). Plan and decisions: [docs/v0.12.1-plan.md](docs/v0.12.1-plan.md);
contract: [docs/v0.12-contracts.md](docs/v0.12-contracts.md) section 10.

### Engine and agents

- **Every message wakes** every agent that can see it (the sender excepted); messages delivered in the same
  pump are one activation. Players that are out are woken only by events addressed to them. Wolves can
  agree on the knife at any time (one shared request that closes when they are unanimous), the white wolf
  can explode at any time.
- **Three states** — thinking / acting / sleeping. `sleep(seconds?)` replaces `wait` (the old name still
  works as a hidden alias): any new message wakes the agent, and the optional alarm (game clock, frozen
  while paused) wakes it when the time is up.
- **Persistent, append-only conversations** — one per agent, kept across pause / resume (not saved). `system`
  and `tools` never change after the first message, so provider prefix caches hit. The first activation gets
  the full board + memory + history; later ones get only what is new (new entries, what changed on the
  board, why you were woken). Messages arriving during a run are merged into it at the next round boundary.
- **Compaction on demand** — at 60 % of the model window the agent is asked to call `compact_context`; at
  90 % the oldest complete rounds are trimmed. No round cap anywhere. A conversation that is itself the
  product of a compaction is only reminded again after it has grown by 10 % of the window, so a window too
  small for the opening cannot make an agent compact in a loop (live check with a 12 000-token window:
  moderator compactions 16 -> 2 per ~95 calls, prompt-cache hit rate 85 % -> 92 %).
- **Requests got friends** — `share_answers` (recipients see each other's current answers and may change
  them), `auto_close = all_answered | unanimous | deadline`. `open_floor` / `update_channel` take one
  `limits` whose keys depend on the mode (a wrong key now gets a precise error). `enable_ability` /
  `disable_ability` with `holders` do what they say.
- **Werewolf manual 2.1** — the wolves' night is one free discussion plus one shared unanimous request, one
  nudge on timeout, then a majority (ties: the front-most wolf's choice) announced to the wolf channel only.
- **Watchdog** wakes only the moderator and shows a warning banner; it never pauses the game.

### Billing, limits and prices

- **Four token classes** (uncached input, cache read, cache write, output) parsed from all three protocols
  (OpenAI `cached_tokens`, DeepSeek `prompt_cache_hit_tokens`, Anthropic cache counters); Anthropic requests
  carry cache breakpoints (retried without them if an upstream rejects them). Cost uses four prices.
- **Per-game spending limit** — tokens and / or USD, optional "stop when reached"; the game is paused with
  the reason *budget*, and cannot be resumed until the limit is raised or the stop option is switched off.
- **Price source** — `/api/pricing/*`: a LiteLLM-style table downloaded safely (https, no redirects, SSRF
  guard, size cap), matched to models (exact id → provider-prefixed → without date suffix → case-insensitive),
  suggested while fetching model lists and editable before saving. Models remember where prices came from.

### Roster

- **Seats `N1`–`N12`**, default names Alice…Leo (no digits), editable and persistent. Models see
  "`N3 Carol`"; the UI shows names.
- **Pixel avatars** — random combinations from a procedurally generated parts library
  (`tools/avatar_lab.py` → `static/avatar/parts.json`); regenerate one or all. **The moderator gets a
  random avatar too** (it is not a seat: it has no name, persona or memory to edit, only an avatar that is
  rolled together with the twelve players so that all thirteen look different).
- **Persona and memory** are plain files in the workspace (`roster/N1.persona.md`, `N1.memory.md`), empty by
  default; import / export (zip) / clear; a per-game "load memory" switch; "complete it" asks the moderator's
  model for a draft you confirm. **Reviews are manual**: after a game each player writes a first-person note
  with his own model; the record (games, wins, per-role) is kept by the engine.

### Interface

- Responsive down to a phone in portrait (platform detection), participants and statistics in one bar,
  coloured private channels, request cards in the feed, a spending-limit button next to the token counter.

- **Phone / tablet / desktop layouts** (`data-platform` / `data-layout`), a bottom tab bar and drawers on a
  phone, avatar rail on narrow screens, the four token kinds everywhere, a roster menu (export / import with a
  preview / clear), review buttons, a price-source bar in the Provider dialog, and the **moderator's own
  random avatar** in the chip, in its messages and in its panel (re-rolled alone or with the players).
- The agent-metrics stream now also covers the calls outside the game loop (post-game review, "complete it"),
  so the token / cost pill always equals what the server adds up. The game starts with the persona / memory
  *files as they are at that moment* (they may have been edited outside the app while it was idle).
- The notice bar shows whatever notice the server sends — a game saved by an older version that was kept aside,
  an unreadable save, a workspace that did not open — not only after a restore (it used to be hidden unless the
  game had been restored).

### Documentation

- **Architecture pictures** — the Actor / Worker design is drawn in four pictures (overview, the Actor's
  decision tree, the Worker loop with the persistent conversation and compaction, a wake-up timeline), in
  Chinese and in English, embedded in both READMEs and in `docs/architecture.md` / `docs/architecture.en.md`
  (new). They are SVGs generated by `tools/make_diagrams.py` (standard library only; a label that would overflow
  its box is refused), so the two languages cannot drift apart.
- README (both languages), the architecture pages and `docs/ui-guide.md` were checked line by line against the
  code and corrected; `docs/v0.12-contracts.md` sections 1–9 now describe the current contract (section 10 stays as
  the revision log); the v0.12 plan and the research note are marked as history. The workspace and bindings
  screenshots were re-taken from the mock server (fictional paths).
- `tests/test_docs.py` keeps it that way: links and images resolve, repository paths named in the docs exist, the
  pictures equal what the generator writes, the numbers in them are the code defaults, the version is the same
  everywhere and no local path leaks.

### Developer tools

- `tools/dev_server.py --demo | --copy-providers-from <.arena> [--price-source]` — isolated instances (the
  user's workspace is never opened); `--price-source` prices the scratch copy's models with the LiteLLM table.
- `tools/real_smoke.py` — live-model smoke test with a hard call budget; reports the four token kinds, cache hit
  rate, activations and merges, compaction events; `--price-source`, `--window N` (pretend a small context
  window), `--resume-calls K` (pause -> resume: does the prompt cache survive?), `--oneshots N` (live check of
  "complete it" and the review).
- `tools/live_replay.py` — plays a whole demo game on a running instance and checks, with the real client store,
  that the SSE events add up to exactly the session the server reports (names, avatars, agent states, requests
  with answers and results, budget, review, token counters).
- `tools/avatar_lab.py` — generates `static/avatar/parts.json` and sample sheets (`--sheet 120`).
- `tools/mock_ui_server.py` — a scripted UI mock that speaks the 0.12.1 contract (also the browser-test backend);
  the workspace paths it reports are fictional, so screenshots taken from it cannot leak a real path.
- `tools/make_diagrams.py` — generates the architecture pictures (`--check` fails when a committed picture differs).

### Compatibility

- **Saves from 0.12.0 and earlier cannot be opened** (save format v4); they stay in the saved-game list.
- Default model bindings saved for `p01`…`p12` are read as `N1`…`N12`. `execution_depth` is gone from the
  session and the API; `config.yml` no longer has `runtime.debounce` / `max_rounds` / `llm.execution_depth`
  (concurrency limits default to *unlimited*; `compact_at` / `trim_at` are new).

## 0.12.0 — a development milestone, never released on its own (everything in it ships in 0.12.1)

The 0.12 line rebuilds the game loop: **every player and the moderator is now an asynchronous
agent**, the moderator is an LLM that drives the game by following the scenario's manual, and the
engine shrinks to a game-agnostic "physical layer". See
[docs/v0.12-async-agents-plan.md](docs/v0.12-async-agents-plan.md) for the decision log (D1–D25) and
[docs/v0.12-contracts.md](docs/v0.12-contracts.md) for the implementation contract.

### Highlights

- **Actor + Worker agents** — a front actor with a mailbox (never blocked by thinking or tools,
  priority wake-ups, debounce, injection between rounds, cancellation with anti-starvation) and a
  back worker running the "LLM → tools" loop. Conversations are rebuilt from the world on every run.
- **Commit gate + append-only ledger** — every action is validated at the moment it is committed
  (membership, floor, character limits, staleness, quotas) and answered with a receipt such as
  `NO_FLOOR`, `NOT_MEMBER`, `CHANNEL_CLOSED`, `STALE` or `QUOTA_EXCEEDED`.
- **LLM moderator** — turn taking is a moderator announcement plus floor management
  (`open_floor`, `set_floor_queue`, `close_floor`, …); requests (sealed votes, defaults, tallies),
  templates (sensitive announcements only via templates), abilities that freeze a channel, player
  state changes (including reviving), random draws and `end_game` are all tools. The moderator cannot
  rewrite history or control other agents.
- **Challenges** — players can privately challenge the moderator at any time; the moderator must
  resolve each ticket and may close a harassing player's private channel (and reopen it later).
- **Speaking limits** — time *and* character limits; over-long messages are truncated, not rejected.
- **Scenario schema v2** — identity pool, public tags, templates, abilities, check expressions,
  default limits, rules text and a moderator-only manual in one Markdown file. The bundled
  12-player Werewolf scenario was rewritten for it.
- **Minimal spectator UI** — tiny glyph tiles with a short id and tag (click for tool-call, thinking
  and token statistics), a speech-only feed rendered whole (no streaming, no chain of thought or
  tool calls), channel floor indicators, challenge tickets, god view or any player's view.
- **SSE v2** — numbered incremental events with `Last-Event-ID` resume and a snapshot fallback.
- **Pause / resume / continue after the end** — timers freeze while paused; a finished game can be
  resumed from the UI and the moderator is told.
- **Plaintext trace log** — thoughts, tool calls and receipts go to `<workspace>/.arena/logs/…`;
  secrets are scrubbed and files roll over at about 20 MB.
- **Demo mode** — `ARENA_DEMO=1` plays a full game with scripted bots (no model, no cost).
- **Hardened for real models** — generous per-call token limits (reasoning models count their thinking
  against them) with a one-shot recovery when a call is cut off, a `wait` guard that reminds a player
  who still has an unanswered request or holds the floor, moderator-only reference documents hidden from
  players, and every bound provider key registered with the trace scrubber.
- **Dev tools** — `tools/dev_server.py` (isolated demo / real-model instances) and `tools/real_smoke.py`
  (real-model smoke test with a hard call budget and a floor timeline report).

### Compatibility

- **Saves from 0.11 and earlier cannot be opened** (save format v3). They stay in the saved-game list
  and are never modified; a fresh game is created and the UI says why.
- Providers, templates, bindings and workspaces are unchanged. The moderator now always needs a model
  binding.
- The old director, the legacy LLM moderator and the workflow DSL were removed.

## 0.11

Arena-Agents is an AI Werewolf (狼人杀) and multi-agent social-deduction arena: executable Markdown
rules, LLM players, a moderator/engine, live spectators and encrypted local data.

### Highlights

- **AI Werewolf / social deduction** — a complete 12-player Werewolf scenario plus a game-agnostic
  engine for other tabletop/social-deduction rules.
- **Multi-provider model routing** — OpenAI Chat Completions, OpenAI Responses and Anthropic
  Messages; per-player model and reasoning depth.
- **Model templates** — three reusable Provider/model/reasoning presets, applied to selected
  players in one action.
- **Workspaces** — each work folder owns its providers, templates, bindings, sessions, logs and
  encryption key; switching requires a paused, successfully saved game.
- **Encrypted local data** — AES-256-GCM with a Windows DPAPI-wrapped workspace key or a
  Docker/Linux master-key Secret.
- **Endpoint preview** — exact message/model URLs before saving; auto `/v1/...` paths or a complete
  custom endpoint.
- **Live arena UI** — collapsible/resizable panels, card-size tiers and real-time SSE updates.
- **Offline test suite** — protocol, storage, API, UI and regression tests; no real LLM calls.

### Compatibility

- Existing single-directory data can be copied into a workspace and verified; originals are kept.
- Legacy `.env`/`saves` migration remains available and is non-destructive.

## Earlier iterations

Arena-Agents began as an AI Werewolf prototype. Earlier releases are intentionally summarized
rather than itemized in public release notes. The last public release is 0.11.
