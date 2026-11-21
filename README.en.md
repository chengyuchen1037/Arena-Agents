# Arena-Agents · AI Werewolf (狼人杀) & Multi-Agent Arena

**English** | [简体中文](README.md)

**Arena-Agents is an open-source AI Werewolf (狼人杀), social-deduction and multi-agent game arena.**
It is a game-agnostic sandbox for LLM agents: a Markdown scenario defines the game rules, **every
player and the moderator is an asynchronous agent** (a front *actor* that always accepts and emits
messages, plus a back *worker* that thinks and calls tools), and an LLM moderator drives the game by
following the scenario's manual. The engine only provides channels, speaking rights, requests and a
ledger. The bundled scenario is a complete 12-player Werewolf game; the same engine can host other
social-deduction or tabletop rules. Keywords: **AI 狼人杀, 狼人杀, 多智能体狼人杀, AI 多智能体博弈, social deduction, AI Werewolf,
LLM Werewolf, AI 桌游, AI 剧本杀, multi-agent game**.

[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.14.0-blue.svg)](CHANGELOG.md)

---

**Arena-Agents 是一个开源的 AI 狼人杀、社交推理与多智能体博弈沙盒。**
游戏规则写在可执行 Markdown 剧本中，引擎负责玩家、发言、投票、频道、事件与断点恢复。内置一套完整的 12 人狼人杀，也可用同一引擎运行其他社交推理/桌游规则。

<img src="docs/images/v014-main-view.png" alt="Arena-Agents AI Werewolf live game view (god view): pixel avatars with seat bubbles and identity tags, the status and viewpoint labels, a sealed-vote request card" width="100%">

## Highlights

- **A player is a bot: a name, an avatar and four layers of memory (v0.14)** — a seat is only "where this game sits":
  before the game starts you can drag a card to another seat and the bot's memory and model are unaffected. Every bot
  has four layers — **L0 core** (identity / voice / way of thinking; only you change it), **L1 architecture** (the
  official contract + its own operating notes), **L2 rules** (the official scenario + its own digest / playbook /
  open questions / impressions of the other players) and **L3 the game** (state board, hard-facts table, visible
  history, notes). The Composer mounts the layers into the prompt in a fixed order: the system prompt is byte-stable
  for a whole game (so provider prefix caches keep hitting), a bot's own memory is rendered as first-person prose
  instead of lists, and at the moment of a speech or a vote one or two playbook entries that fit the situation are
  recalled — the same model can sound clearly different from seat to seat. Twelve default bots (Alice…Leo) ship in the
  repository, and a single bot can be exported / imported (`.arenabot`). See [docs/bots.md](docs/bots.md) (Chinese).
- **Initialization and review (v0.14)** — the prompts teach no tactics. A bot first **initializes**: it reads the
  official contract and the rules and writes its operating notes, digest and playbook in its own words (120 s each,
  all or nothing). After a game a dialog asks whether to **review**: each bot has 60 s to hold its memory against the
  revealed truth (writes take effect at once; what was written when the clock runs out stays). Only you change a core:
  a review can at most propose, you accept; for a seat a person played, the bot can learn that person's way of
  speaking. ⚠ means the contract, the scenario or the core changed and the bot must be initialized again; a bot that is
  not ready is stopped by the **start check**, which lists per player what to fix.
- **Speech only ever comes from the bot itself (v0.14)** — text over the length limit is no longer truncated but **sent
  back** with `TOO_LONG` (the floor is not used up; people and bots alike); speech that reads like a report (lists,
  headings, one's own seat id in front) is sent back once with `FORMAT` and the bot rewrites it in its own words. The
  engine and the orchestrator have no way to say anything on a bot's behalf or to rewrite it, and the prompts no
  longer ask for "short thinking".
- **Identities are dealt when the game starts (v0.14)** — before the start no identity exists or is shown anywhere
  (no tag, not even a "?"); the deal happens when you press *Start* and the start check passes. *Re-deal* is gone.
- **Five settings sections and new file formats (v0.14)** — ⚙ Settings has ① whole archive ② core files ③ official
  contract ④ rules ⑤ game management. The archive (`.arenaagents` format 2, which still imports v0.13 archives) now
  holds every file of the data directory and logs are no longer optional; a single bot (`.arenabot`) and a single game
  (`.arenagame`) can be exported / imported too. API-key handling is unchanged: optional, and only the keys are
  encrypted with your password.
- **Seat bubbles, status and viewpoint tags (v0.14)** — the translucent bubble on a card's corner is the seat number,
  ✔ ⚠ ✖ ⏳ ○ next to a name show its initialization state, and the second row of the top bar starts with a **status tag**
  (not initialized / initializing / before the game / in game / paused / finished / reviewing) and a **viewpoint tag**
  (global view / Alice's view / Alice's view (taking over)).
- **A benchmark (v0.14)** — `bench/` is a reproducible suite that quantifies "do the players sound different?" and "do
  they judge by the hard facts?"; the v0.13.1 player (`LegacyBrain`) is the baseline. See
  [bench/README.md](bench/README.md) (Chinese).
- **Play a seat yourself: take-over (v0.13)** — while the game has not started or is paused, *Take over X* in the
  menu of the viewpoint tag lets you play one player: its agent is paused (and costs no tokens),
  you see that seat's viewpoint, and leaving (again only while the game is not running) gives the seat back —
  the agent wakes up next time with the full history, including what you said. You may take over before the game
  starts too (v0.14: nothing has been dealt yet, you see your identity once the game begins). Every action of yours
  goes through the same commit gate as an agent's: turns, length (too long is sent back with `TOO_LONG` as well),
  quotas and deadlines are unchanged.
- **Soft-pause the agents, not yourself (v0.13.1)** — the take-over bar can hold every agent after its current
  answer and freeze the clock at the same instant. You can still read, speak, answer and use abilities at your own
  pace; your public-channel floor is **untimed**, while requests keep their deadlines. Eight colour/shape marks on
  the other seats are private to this browser and carry no rule-defined meaning.
- **One input box per channel (v0.13)** — like a group-chat app, switching the channel switches the input box
  (each keeps its own draft, shows the character count and the limit live, and the send button is disabled when over it). When it is not your turn, your turn is
  over, or the channel is closed / frozen / listen-only you can still type, but the send button turns into a
  grey *禁言中* ("muted", hover for the reason). *End my turn* appears while you hold the floor; the *Ability*
  button releases active abilities such as the white wolf's explosion; votes and other choice requests require
  **pick, then Submit**, so a stray click cannot answer for you, and still show a countdown. Challenging the moderator is done in the private chat with the moderator.
- **One file for everything (v0.13)** — the whole archive is a single `.arenaagents` file. Its contents (bots,
  games, logs, providers …) are not encrypted; only the API keys can optionally be exported, encrypted with a strong
  password of your choice. Export and import show progress; import verifies the format version and the SHA-256 of
  every file, backs the current data up before replacing it and rolls back on failure. A conversion script brings
  v0.12.1 data over.
- **History and replay (v0.13)** — every game is listed; continue it, delete it ("deleting a game does not undo what a
  bot already learned from a review") or **replay** it with pause, 0.5×–8× speed, a draggable seek bar, skip-idle and
  viewpoint switching. Since v0.14 the settings' *game management* also shows each game's status, review progress and
  file size, and exports / imports a single game (`.arenagame`).
- **Thinking records and an error log (v0.13.1)** — the player / moderator details open a chat-like record of
  thoughts and text for each wake-up, without tool calls. The top-right *Log* lists the current game's LLM calls,
  retries, timeouts and errors, with actor and error-only filters. While a take-over is in progress and the game
  is unfinished, other players' thoughts stay closed.
- **Every agent is asynchronous (v0.12)** — each player and the moderator is an Actor + Worker. The
  actor is never blocked by thinking or tool execution; speech, votes, challenges and abilities can
  happen at any time and are validated by the engine *at commit time*.
- **The engine never saves money for you (v0.12.1)** — every message delivered to an agent wakes it at
  once (no debounce, no round cap, no "execution depth"); wolves can agree on the knife at any time and
  the white wolf can explode at any time. An agent is only *thinking*, *acting* or *sleeping*, and
  `sleep(seconds)` sets its own alarm. Tokens are managed by orchestration: one persistent append-only
  conversation per agent (so provider prompt caches hit), messages that arrive mid-run are merged into
  the running activation, and a conversation is compacted only when the model window is nearly full.
- **Per-game spending limit** — a button next to the token / cost counter sets a token and / or USD
  limit and lets you choose "stop the game when it is reached" (yellow at 80 %). Cost is computed from
  **four token classes** (uncached input / cache read / cache write / output) and four prices.
- **Price source** — the Provider dialog can fill a model's four prices and context window from a price
  table (default: LiteLLM's `model_prices_and_context_window.json`); you can still edit every number.
- **Pixel avatars** — every bot has a **pixel avatar** randomly assembled from a procedurally generated parts library
  (regenerate one or all; the moderator has a random one too; an avatar code can be copied and recreates exactly that
  avatar). The default bots' avatars are fixed.
- **Responsive UI** — from a 1920 px desktop to a phone in portrait (layout follows platform and width).
- **An LLM moderator drives the flow** — taking turns = a moderator announcement + floor management
  (`set_floor_queue` and friends). The rules and the moderator manual live in the scenario; the
  engine contains no game vocabulary.
- **Channels and speaking rights** — visibility is decided by channels only; the moderator can close
  the floor at any time (players get a "no floor" receipt), speech is limited by time *and* by
  characters (over-long text is sent back to be rewritten, never truncated), players can privately challenge the moderator, and the moderator can punish harassment
  by closing a player's private channel.
- **Append-only ledger** — everything that happened is recorded; saves, agent context and spectating
  all derive from it. The moderator may change a player's *current* state (out, revived …) but can
  never rewrite history.
- **Executable Markdown scenarios (schema v2)** — identity pool, templates, abilities, checks and
  default limits in the front matter; the body holds the rules and a moderator-only manual.
- **Multi-provider model routing** — OpenAI Chat Completions, OpenAI Responses and Anthropic
  Messages; every player and the moderator can use a different model and reasoning depth.
- **Model templates** — save up to three Provider/model/reasoning presets and apply them to
  selected players in one action.
- **Data lives in the program folder (v0.13)** — there is exactly one data directory, `<program dir>/data/`
  (no "workspace" to choose; `ARENA_DATA_DIR` overrides it, Docker uses `/app/data`; `data/` is git-ignored).
  Providers, templates, bindings, bots, the official contract, the rules, games and logs are in it.
- **Safe live operations** — model bindings, take-over and import are locked while a game is running; a process
  lock lets only one server use a data directory.
- **Only the API keys are encrypted** — keys live apart in `secrets.v1.enc` (AES-256-GCM; on Windows the master
  key is protected by DPAPI); everything else (bots, contract, rules, games, logs, settings) is plain text you can read and
  back up.
- **Endpoint preview** — see the exact message and model-list URLs before saving a Provider;
  either auto-append `/v1/...` paths or point at a complete custom endpoint.
- **Spectator UI** — players are shown as a pixel avatar + name + role tag (only once the game has started); click a
  player card for the bot panel, the thinking record and the token statistics. The feed shows spoken messages and request cards only
  (whole messages, no streaming) and supports a god view or any player's view.
- **Demo mode** — `ARENA_DEMO=1` replaces the models with scripted bots, so you can watch a whole
  game without an API key (the twelve bots start out ✔, and initialization and review are answered by the demo bots
  with fixed answers).

## Screenshots

| Start check: bots that are not ready are stopped, the dialog lists the reasons per player (v0.14) | Bot panel: name and avatar, initialization state, core editor (v0.14) |
|---|---|
| <img src="docs/images/v014-start-check.png" alt="Start-check dialog: lists the bots that are not ready per player (core changed so initialize again, the last initialization failed, not initialized yet, core not written yet), each with a button such as initialize or import a core; the game has not started and nothing has been dealt" width="100%"> | <img src="docs/images/v014-bot-panel.png" alt="Bot panel: name and avatar (frozen after initialization), the initialized state with re-initialize, the thinking record, the three required sections of the core and its extra section, character counters" width="100%"> |
| **⚙ Settings: five sections — archive / L0s / contract / rules / games** | **End-of-game review dialog: choose which bots review (60 seconds each at most)** |
| <img src="docs/images/v014-settings.png" alt="Settings dialog: the five sections archive (export / import), core files, official contract, rules and game management" width="100%"> | <img src="docs/images/v014-review.png" alt="End-of-game dialog: all 12 bots ticked by default, select all / none, Not now or Review" width="100%"> |
| **A player's viewpoint (taking Carol over): after the start the others' identities are “?”, only what that player may see is shown** | **Dark theme** |
| <img src="docs/images/v014-player-view.png" alt="A player's viewpoint: the viewpoint label reads Carol, the other players' identity tags are ?" width="100%"> | <img src="docs/images/v014-main-dark.png" alt="The game view in the dark theme: seat bubbles, the game status strip, the public channel" width="100%"> |
| **Phone portrait (mini cards + bottom tab bar; Carol is taken over here)** | **Token / cost limit (per game, optional force stop)** |
| <img src="docs/images/v014-phone.png" alt="Phone portrait: the viewpoint label and the take-over bar on the first rows, then 6×2 mini cards, the public channel and the bottom tab bar" width="60%"> | <img src="docs/images/v014-budget-dialog.png" alt="Token / cost limit dialog: usage bars, the two limits, the force-stop switch, the four token kinds" width="100%"> |
| **Provider management: price source and four prices** | **Model templates and bindings** |
| <img src="docs/images/provider-modal.png" alt="Provider management: price source and four prices" width="100%"> | <img src="docs/images/templates-bindings.png" alt="Model templates and bindings" width="100%"> |

> Screenshots are taken from the local **UI mock** (`tools/mock_ui_server.py`: scripted demo data, no model is called). The provider is a demo example (placeholder key); no real credentials are shown; every path, name and number in them is made up.

More UI walkthrough: [docs/ui-guide.md](docs/ui-guide.md).

## Architecture: Actor + Worker

The core of the engine is a single idea: **every role (12 players + the moderator) is an asynchronous agent made of two halves — a front *Actor* and a back *Worker*.**

- **The Actor is always on.** It is the agent's mailbox: synchronous, non-blocking, it never calls a model. Whenever the world commits an entry it delivers an Envelope to every agent that can see it, and a sleeping agent wakes at once — no debounce, no "only wake for important messages" money-saving rule.
- **The Worker thinks.** One activation = one asyncio task: open (or continue) **the one persistent conversation** of this agent → call the model → run tools → merge the messages that arrived meanwhile → call the model again … until the agent calls `sleep` itself. There is no round cap within an activation; the one brake is the spending limit you set.
- **The world only accepts commits.** Every agent action (speech, a vote, an ability, a challenge …) is an Intent that the commit gate re-validates **at the moment of commit** (floor, membership, length, staleness …). So "the world changed while the agent was thinking" cannot produce a wrong write; cancelling a call in flight only avoids wasting tokens, it is not what makes the game correct.

### Overview

<img src="docs/images/arch-overview.en.svg" alt="Arena-Agents overall architecture: browser, FastAPI, GameOrchestrator, Runtime (Actor + Worker), Brain and World, plus the LLM adapters, the data directory and the scenario" width="100%">

From top to bottom: the browser controls the game over REST and spectates over SSE v2 → FastAPI → the **GameOrchestrator** owns a game's lifecycle (it builds the world and 13 agents — or 12 plus a person at the seat they took over —, enforces the budget, runs the bot line-up and the start check, the initializations and reviews, autosaves the game) → in the **Runtime** every agent is an Actor + Worker and the **Supervisor** pumps the world's deliveries into them → the **Brain** translates the world into prompts and tools a model can read and tool calls back into Intents → the **World**'s commit gate and its append-only ledger. Solid arrows are calls and data; dashed arrows are notifications (an Envelope wakes an agent, a UiEvent goes to the browser).

### Actor: the always-on mailbox

<img src="docs/images/arch-actor.en.svg" alt="The Actor's decision tree: deliver puts the envelope into pending; a sleeping agent gets one activation via call_soon; a running one merges, or cancels the call in flight when an interrupt hits its focus; plus the alarm, retry and stop paths" width="100%">

- A sleeping agent receives an envelope → **one activation is scheduled** (everything delivered in the same pump is one activation; there is no timer).
- A running agent receives an envelope → it **stays in pending and is merged at the next round boundary** as one DELTA message; nothing is interrupted. Only an `interrupt` (a floor was revoked, a request cancelled …) **that also hits a channel or request the current run has read** cancels the model call in flight; after 2 cancellations in a row the next one is merged instead (starvation guard).
- `gen` (a generation counter) makes a stale run **unable to write anything into the world**; the alarm set by `sleep(seconds)` runs on the game clock (frozen while paused); a failure only affects this agent (exponential backoff, 2 retries, then give up); stopping is synchronous and idempotent.

### Worker: one activation

<img src="docs/images/arch-worker.en.svg" alt="The Worker loop: open the conversation, think, account and check gen, act, keep the conversation healthy, merge envelopes that arrived meanwhile, until sleep; on the right the persistent conversation, the compaction thresholds and what the Brain tells the model" width="100%">

- **A persistent conversation**: every agent has one append-only conversation (`system` and the tool schemas never change after creation), so the provider's prefix cache can hit; pause → resume keeps the same conversation.
- A round's tools run in one stretch **without `await`**, so a run can never be cancelled half-way; the assistant message and all tool results are appended together.
- **Three states**: 💭 thinking (an LLM call is in flight) · ⚡ acting (running tools) · 💤 sleeping.
- **Compaction on demand**: at 60 % of the model window the agent is reminded to compact, at 90 % the oldest complete rounds are trimmed if it still has not; there is no other limit.

### Timeline: how one message wakes everybody

<img src="docs/images/arch-timeline.en.svg" alt="A timeline of three commits: sleeping agents wake up, an agent that is thinking merges at its next round boundary, a player who is out is not woken by broadcasts" width="100%">

Every message wakes (the sender excepted); an agent that is thinking is not interrupted and the new message is merged at its next round boundary; a player who is out is not woken by broadcasts.

A player's Brain is one `BotBrain` per seat (v0.14): the Composer mounts that bot's four memory layers into the
system prompt in a fixed order and within budgets, and at the moment of a speech or a vote it brings the one or two
playbook entries that fit to the model's eyes; the output gate only ever sends an unfit speech back so that the bot
rewrites it itself, and never rewrites anything. Initialization and review are loops of their own (`app/learning/`), not
agents of the game. See [docs/bots.md](docs/bots.md) (Chinese) and the "Bot = Brain + memory" section of the
[architecture page](docs/architecture.en.md).

The engine never knows game-specific phases or role names (`tests/v12/test_neutrality.py` scans for
them). The scenario provides rules and a manual, the **moderator agent** drives the game with tools,
and players act with `speak` / `respond` / `use_ability` / `challenge_moderator`. Chains of thought
and tool calls go only to a plaintext trace log; the UI never shows them. A layer-by-layer description is in
[docs/architecture.en.md](docs/architecture.en.md) (中文: [architecture.md](docs/architecture.md)) and the implementation
contract is [docs/v0.12-contracts.md](docs/v0.12-contracts.md). The pictures are generated by `tools/make_diagrams.py`
(SVG, one Chinese and one English file each).

## Quick start

### Requirements

- Python 3.12
- Node.js 20 only for the optional frontend tests
- A provider API key (OpenAI-compatible, OpenAI Responses, or Anthropic)

### Run locally

```bash
git clone <repository-url> Arena-Agents
cd Arena-Agents

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>.

On first launch Arena-Agents creates its data directory `data/` in the **program folder** (the one that holds
`app/` and `config.yml`, wherever you start the server from). Providers, bindings, bots, games and logs
are all in it; there is no folder to choose. Provider settings are configured from the UI; no `.env` file is
required for local use. On the first start the twelve default bots (Alice…Leo, the repository's `bots/`) are copied into
it; they have to be **initialized** before a game can start (step 7 of "Configure a model" below).

> **⚠ The data lives inside the program folder.** Deleting or re-installing it, a fresh `git clone`, or
> `git clean -xdf` takes your games with it. Export an archive now and then (**⚙ Settings → Export archive**,
> see "Data and archives" below).

### Demo mode (no model / API key)

```bash
ARENA_DEMO=1 uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Scripted bots play the moderator and the 12 players through the real engine, channels, floors,
requests and UI, with no network traffic and no cost. The data goes to `.demo-data/demo/`, so a demo never
mixes into your real history. In the demo the twelve bots start out initialized (✔, with fixed answers and no model call), so you can start a game
at once; initialization and review are answered by the demo bots as well. You can also take over a seat, use the input
box and replay finished games.

### Isolated dev instances and real-model smoke tests

```bash
# An instance fully isolated from your real data (data in .demo-data/, git-ignored)
python tools/dev_server.py --demo --port 8123
python tools/dev_server.py --copy-providers-from "<program dir>/data" --port 8124   # copies providers, keys + bindings; calls real models and COSTS credits

# A real-model smoke test with a hard call budget (costs credits): in a scratch data directory the bots are initialized
# first and the game follows; when it ends it prints tool usage, the speeches the engine sent back (TOO_LONG / FORMAT),
# the prompt-cache hit rate and the floor timeline. --init fixed (default) commits a fixed initialization and calls no
# model for it; --init real lets the bots really initialize
python tools/real_smoke.py --from "<program dir>/data" --scenario scenarios/minimal_test.md --players 4 --max-calls 60
python tools/real_smoke.py --from "<program dir>/data" --init real --players 4 --max-calls 120

# One real initialization of the default bot library (once before a release; --write-library is what writes into bots/)
python tools/init_default_bots.py --from "<program dir>/data" --dry-run
python tools/init_default_bots.py --check
```

Both scripts only **read** your data directory (they copy the providers and keys into a private scratch directory and
work there), both have a hard budget (`--max-calls`), and no test or automatic flow ever runs them. Usage, options, the
order of magnitude of the cost and how to review the result: [docs/real-model-runs.md](docs/real-model-runs.md) (Chinese).

### Run with Docker Compose

```bash
cp .env.example .env
# Recommended: set ARENA_API_TOKEN to protect API writes.
# Generate a 32-byte master key outside the repository:
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())" \
  > /secure/arena-master-key

export ARENA_MASTER_KEY_HOST_FILE=/secure/arena-master-key
docker compose up -d --build
```

The container runs as uid 10001, serves on host `127.0.0.1:${PORT:-8000}` by default, and stores
all runtime data in `/app/data` (the named volume `arena-appdata`; the v0.12.1 `/data` volume is neither reused
nor deleted). The master key that encrypts the API keys must be a Docker Secret or a separate readable file;
without it the app refuses to save a key instead of writing it in the clear. Upgrading from v0.12.1:
[docs/storage.md](docs/storage.md#upgrading-from-v0121).

## Configure a model

1. Open **Provider → New Provider**.
2. Choose the protocol:
   - **OpenAI Chat Completions**
   - **OpenAI Responses**
   - **Anthropic Messages**
3. Enter a display name, Base URL and API Key.
4. Choose whether to auto-append API paths:
   - **On**: Base URL is a root; Arena-Agents shows and uses
     `/v1/chat/completions`, `/v1/responses` or `/v1/messages`, and `/v1/models` for discovery.
   - **Off**: Base URL is the complete message endpoint. It is sent verbatim; automatic model
     discovery is disabled, so enter model IDs manually.
5. Fetch models (when auto-append is on) and select **one** model. A model has **four** prices per
   1M tokens: input (uncached, required), output (required), cache read and cache write (optional:
   empty = billed like input). The dialog's **price source** (by default LiteLLM's price table) can
   match the model and fill in the four prices and the context window; you can still edit every number.
   No match = nothing is filled in; prices never default to a hidden value.
6. In **Model Bindings**, create templates 1/2/3 or bind a Provider/model/reasoning depth per
   player and the moderator. Check players and click a template to apply it. A binding follows the bot, so dragging a
   seat does not change it.
7. Click **⚙ Initialize all** in the participant bar header: each of the twelve bots reads the official contract and
   the rules and writes down its own understanding (in parallel, 120 s each). Only then can you press *Start*; a bot
   that is not ready is listed by the start check.

Full details: [docs/providers.md](docs/providers.md).

## Data and archives

Everything is in one directory (by default `<program dir>/data/`); **only the API keys are encrypted**, the rest is
plain and can be read directly:

```text
data/
├─ settings.json            default bindings (by bot), templates, last game, default spending limit
├─ providers.json           the provider registry (no API keys)
├─ secrets.v1.enc           the API keys (AES-256-GCM; on Windows the master key is DPAPI-wrapped in master.v1.dpapi)
├─ pricing.json             the downloaded price table (public data)
├─ contract/                the official contract (contract.md + source.json)
├─ rules/<scenario id>/     the scenario (scenario.md) and the study materials (materials/, empty by default)
├─ bots/                    the players: lineup.json (the order of the 12 seats) + <bot id>/ (bot.json, core/ = L0, arch/, rules/, people.md, record.json …)
├─ games/<id>/game.json     one game (save format v6, v5 is still read, plain JSON) + games/index.json (the history list's cache, rebuildable)
├─ logs/<id>/trace-*.jsonl  the thinking logs; logs/learning/ holds the logs of initializations and reviews
├─ exports/  backups/  tmp/ exported archives, what an import replaced (3 are kept), uploads in progress
└─ .lock                    process lock: one server per data directory
```

- **There is no "Save" button**: a game is saved half a second after anything changed and once more when the
  server stops; after a restart the last game comes back **paused**.
- **The settings have five sections**: ① whole archive ② core files ③ official contract ④ rules ⑤ game management.
  In ①, **Export archive** and **Import archive** work on a single `.arenaagents` file (a zip container, contents not
  encrypted) that holds **every file of the data directory** (settings, providers, price table, contract, rules, bots,
  games and all logs). Export asks whether to include the API keys — if so you enter a strong
  password twice (at least 12 characters, three of upper case / lower case / digits / symbols); the keys are
  encrypted with `scrypt + AES-256-GCM` and the password is never written into the archive. Importing an archive
  that holds keys needs the password; you may also choose to skip the keys and import everything else. Both show
  progress, and the result dialog tells you where the file was saved (`data/exports/`) and offers a download.
- **Import verifies** the format version, the manifest and the SHA-256 of every file, path and size limits, then
  replaces the current data as a whole: the old data is moved to `data/backups/<time>/` first and a failure rolls
  back by itself. It is refused while a game runs. Verification detects corruption but is not a signature — only
  import archives you trust. A v0.13 archive (format 1) imports too; its roster is migrated into bots.
- **One bot, one game**: the player details export / import an `.arenabot` (all the state of one bot; importing
  overwrites the target player; at most 20 MiB; no keys, contract, scenario or games). The settings' game management
  exports / imports an `.arenagame` (one game with its logs; on an id conflict you may overwrite or import a copy).
- **Upgrading from v0.12.1**: run `python tools/migrate_v0121_to_archive.py` (it only reads the old workspace;
  run it as the same Windows user, or provide the master key) and import the archive it writes in the settings (it is
  in the v0.13 format, so its roster is then migrated into bots); on the first start the page offers an explanation once
  (dismissible).
- **Upgrading from v0.13 is automatic**: on the first start `data/roster/` is converted into twelve bots (names, avatars,
  personas, impressions, lessons, records and model bindings come along; no model is called; `roster/` is left as it
  is). Then click ⚙ Initialize all once. A hand-written persona without headings loses nothing: its first 300 characters become
  the identity and the rest goes on, in order, in up to three extra sections "原有性格（续）" …; its voice and way of thinking are
  empty - complete them with ✨ *Complete with the moderator's model* or write them - before the bot can start.
- **History**: **📜 History** lists every game; replay, continue or delete it ("deleting a game does not undo what a bot
  learned from a review" — memory belongs to the bot and lives in `bots/`, not in a game).

Full details: [docs/storage.md](docs/storage.md) (data directory, what is encrypted, Docker, upgrading),
[docs/archive.md](docs/archive.md) (archive format, verification, password, conversion script; in Chinese) and
[docs/bots.md](docs/bots.md) (a bot's files and memory; in Chinese).

## Using the UI

- **Participant bar**: one card per player (bot) — pixel avatar + name + the initialization state (✔ initialized / ⚠ needs
  a new initialization / ✖ failed / ⏳ initializing / ○ not initialized) + one role tag (only once the game has started),
  with the state on the avatar's corner (💭 thinking / ⚡ acting / 💤 sleeping; the tooltip of a timed sleep shows the
  seconds left); the translucent bubble on the card's corner is the seat number. **Before the game starts you can drag a
  card to change the seat** (long-press and drag on touch screens, Alt+←/→ on the keyboard); the bot's memory and model are
  unaffected. Click a card for the **bot panel**: the thinking-record window, LLM call counts, the four token kinds, the cache
  hit rate, cost, and its core files, memory, pending proposals, initialization and review, and `.arenabot`
  export / import; tool-call details stay out of that chat-like record. Players who are out are greyed. The header of the
  bar has *⚙ Initialize all*, the *Roster ▾* menu (load memory in this game, clear everything learned, regenerate all
  avatars) and *Review all*.
- **Feed**: spoken messages, announcements and **request cards** (votes, choices, questions; with
  progress and results), rendered whole; no chain of thought, no tool calls, no streaming.
- **Status tag and viewpoint tag** (the start of the top bar's second row): the status tag reads not initialized /
  initializing / before the game / in game / paused (with the reason) / finished / reviewing; the viewpoint tag reads
  "Global view", "Alice's view" or "Alice's view (taking over)" and opens a menu: global view, watch as X (only the
  channels and messages that player may see), take over X (before the game starts or while it is paused, with a
  confirmation), leave the take-over. While a seat is played the tag carries 🎮 and is locked to it (leaving again asks
  for a confirmation and only works while the game is not running). The lock is front-end only: the server still
  sends the browser the full spectator data (a known limitation, see the plan).
- **Input box and ability**: the bottom of the feed holds the current channel's input box. On a vote or another
  request card, pick an answer and then click *Submit*; you can change the pick before submitting, and a deadline
  never auto-submits it. *End my turn* and *Ability* are described above.
- **Channel list**: shows each channel's floor state (🎤 turn / 📋 queue / 💬 free discussion /
  ❄ frozen / 👁 listen-only / 🔇 muted); challenge tickets form their own group; private channels are
  named after the other person; a channel where it is your turn gets a "your move" mark while you play a seat.
- **Spending limit**: the button next to the token / cost counter sets a per-game token and / or USD
  limit, optionally with "stop when reached".
- **Controls**: Start (it first passes the **start check**: the twelve bots' cores are complete and all ✔, the contract and
  the scenario are usable, the model bindings are complete — otherwise the problems are listed per player) / Pause / Resume;
  a finished game can still be resumed (the moderator is told); when a game ends a dialog asks whether the bots should
  **review** it (you can also do it later).
- **⚙ Settings / 📜 History**: the five sections of the settings (whole archive, core files, official contract, rules,
  game management); replay, continue and delete past games (a replay has a bar with ⏮ ▶/⏸, speed, a seek bar and
  skip-idle).
- **Responsive layout**: wide, narrow, tablet and phone portrait (mini cards + a bottom tab bar) are
  chosen from the platform and the width. The board collapses as a whole, separators can be dragged
  (double-click to reset), and the layout is stored in the browser's `localStorage`.

Every control is described in [docs/ui-guide.md](docs/ui-guide.md).

## Configuration

All runtime settings, provider keys, models and games belong to the data directory. The application
reads only these deployment-level environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `ARENA_DATA_DIR` | `<program dir>/data` (demo mode: `<program dir>/.demo-data/demo`) | The data directory (tests, dev instances, a data disk of your own) |
| `ARENA_MASTER_KEY_FILE` / `ARENA_MASTER_KEY` | unset (Windows uses DPAPI) | 32-byte master key that encrypts the API keys on Linux/Docker |
| `ARENA_MAX_ARCHIVE_BYTES` | `2147483648` | Maximum size of an uploaded `.arenaagents` / `.arenagame` file (2 GiB; an `.arenabot` is fixed at ≤ 20 MiB) |
| `ARENA_API_TOKEN` | unset | Bearer token for `/api/*` writes; also accepted as `?token=` for SSE |
| `ARENA_ALLOWED_ORIGINS` | unset | Extra allowed cross-origin origins |
| `ARENA_ALLOWED_BASE_HOSTS` | unset | Optional provider host allow-list |
| `ARENA_ALLOW_PRIVATE_PROVIDERS` | `false` | Allow loopback/private provider endpoints (e.g. Ollama) |
| `ARENA_BIND` | `127.0.0.1` | Docker host bind address |
| `ARENA_DEMO` | unset | `1` enables demo mode (scripted bots, no model calls) |

v0.13 removed `ARENA_WORKSPACE_DIR`, `ARENA_DISABLE_WORKSPACE`, `ARENA_DISABLE_LEGACY_MIGRATION` and
`ARENA_MAX_IMPORT_BYTES` (nothing reads them any more).

`config.yml` contains only the scenario path, the runtime parameters (`runtime:`, see the
[contract](docs/v0.12-contracts.md) §6, in Chinese), the knowledge-base directory, the seats' default
names, tag styles, prompt paths and the legacy LLM defaults. API keys are stored encrypted in
`data/secrets.v1.enc` and never in YAML or `.env`.

## Security

- Provider keys are encrypted at rest and are never returned by the API. Changing protocol, Base
  URL or endpoint mode requires re-entering the key.
- Non-GET requests require a same-origin `Origin` (or an allow-listed origin). Without a token,
  configuration writes (including bot edits, initialization and review, take-over, seat actions, archive export / import and
  deleting games) are limited to loopback clients.
- Provider URL validation blocks cloud metadata/link-local addresses; private/loopback endpoints
  require explicit opt-in.
- Importing an archive (`.arenaagents`, `.arenabot` and `.arenagame` go through the same checks): size limit, a path
  allow-list (absolute paths, `..` and symbolic links are refused), compression-ratio and entry-count limits, the SHA-256
  of every file and a structural check of every game; the current data is backed up before a whole archive replaces it
  and restored on failure; it is refused while a game runs.
  Exported keys are encrypted with your password, which is never written into the archive, a log or a response.
- Take-over: the actor is always chosen by the server (a client cannot act for another seat); the viewpoint is
  locked in the front end only, the browser still receives the full spectator data.
- The server defaults to `127.0.0.1`; expose it only behind a TLS reverse proxy with
  `ARENA_API_TOKEN` configured.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q tests
```

The suite is offline and never calls a real LLM (engine tests use a scripted FakeLLM / the demo
bots; the benchmark's tests in `tests/bench/` use a fake model). Node.js enables the frontend tests; they are skipped when
Node is unavailable. CI runs on Ubuntu and Windows. `tests/test_docs.py` checks the documentation: every relative link and
image exists, the version numbers agree, and the architecture pictures equal what `tools/make_diagrams.py`
generates.

**The tests never touch your real data**: `tests/conftest.py` points `ARENA_DATA_DIR` at a throw-away directory for the whole
test process (before `app` is imported) and warns in red in the terminal summary if `data/` changed during the run
(`tests/test_real_data_guard.py` guards this). A manual script of your own has to set `ARENA_DATA_DIR` first, too. The only
scripts that call a real model are the two in [docs/real-model-runs.md](docs/real-model-runs.md), and only when you run them.

## Project structure

```text
app/
├─ world/        physical layer: commit gate, ledger, channels, floors, requests, templates, abilities
├─ runtime/      async runtime: actors/workers, supervisor, LLM gate, trace log, event bus, game clock
├─ agents/       Brains: BotBrain + Composer + output gate (the v0.14 bot), LegacyBrain (the v0.13.1 player), ArenaBrain (the moderator), the learning Brain,
│                tool schemas, context building, notes and compaction, prompt-injection guard
├─ bots/         shared types and constants of a bot: the four memory layers, limits, hashes, the learning port
├─ learning/     initialization and review: loops of their own (not agents of the game), jobs and progress, learning usage and logs
├─ game/         orchestrator, session container (save format v6, v5 is still read), bot line-up and start check (bots_runtime), initialization / review jobs (learning_runtime), budget, bindings, take-over (takeover_runtime), replay projection (replay), demo bots
├─ api/          REST + SSE v2, providers, templates, bots / contract / rules, initialization and review, take-over and seat actions, history of games, archive export / import
├─ archive/      .arenaagents (format 2, 1 is still read), .arenabot, .arenagame: format and verification, password encryption of the API keys, export / import jobs, backup and rollback
├─ engine/       LLM client, expression engine
├─ llm/          protocol adapters and endpoint planning
├─ providers/    provider registry (keys encrypted apart), the four model prices, price source
├─ avatars.py    pixel avatars (parts library static/avatar/parts.json is generated by tools/avatar_lab.py)
└─ storage/      data directory and process lock, settings, bots, contract, rules, games (GameStore), API keys (SecretStore), crypto (roster.py only keeps the migration reader of a v0.13 roster)
scenarios/       Markdown scenarios (schema v2)
prompts/         moderator prompt, the default official contract (contract.md), the initialization / review prompts (learning/), the player prompt LegacyBrain uses
knowledge/       moderator guide and strategy documents (moderator only; a player's study materials live in the data directory's rules/, empty by default)
bots/            the default bot library: twelve bots (Alice…Leo), one folder each (bot.json + core/)
bench/           the benchmark: metrics, situation scripts, runner, the v0.13.1 baseline and the acceptance gates
static/          single-page arena UI (static/js front-end modules, static/avatar parts library)
tools/           dev tools: dev_server (isolated instances), real_smoke (real-model smoke test),
                 init_default_bots (one real initialization of the default bot library),
                 mock_ui_server (mock server), live_replay (event replay check), avatar_lab (avatar
                 parts library), make_diagrams (architecture pictures), migrate_v0121_to_archive
                 (v0.12.1 → .arenaagents conversion script)
tests/           offline regression, API, storage, archive, bots, learning, benchmark, take-over, replay, documentation and UI tests
docs/            architecture, contract, provider, storage, archive and UI guides; docs/images holds the
                 screenshots and the architecture pictures
```

## Release

The current version is **0.14.0**: a player changes from "a seat with a roster entry" into a **bot** — a name, an avatar and four layers
of memory (core / architecture / rules / game) that the Composer mounts into the prompt; a bot **initializes** outside the game (it reads
the contract and the rules and writes its own understanding and playbook) and **reviews** after it; over-long speech is sent back with
`TOO_LONG` instead of being truncated and speech only ever comes from the bot itself; identities are dealt when the game starts; the settings
have five sections, `.arenabot` and `.arenagame` are new and the whole archive moves to format 2; the repository ships a `bench/` benchmark
and twelve default bots. Decisions and the test plan: [docs/v0.14-plan.md](docs/v0.14-plan.md) (H1–H59, in Chinese); how to use it:
[docs/bots.md](docs/bots.md) (Chinese).
**Three things to know when upgrading**:
① **What cannot be opened directly**: data of v0.12.1 and older (convert it with `tools/migrate_v0121_to_archive.py` and import the archive in the
settings, see [docs/storage.md](docs/storage.md#upgrading-from-v0121)); v0.14 removed `/api/roster/**`, *Re-deal* and the persona / memory editors
of the player panel (the bot panel replaces them).
② **Automatic migration**: on the first start `data/roster/` is converted into twelve bots (importing a v0.13 archive does the same); no model is called,
every bot is "not initialized", so click ⚙ Initialize all once before the first game. A hand-written persona without headings is not cut
(its first 300 characters become the identity, the rest goes on in the extra sections "原有性格（续）" …), but its voice and way of
thinking have to be completed (✨ *Complete with the moderator's model*, or by hand).
③ **v0.13 games** (save format v5) still open, replay and continue — continuing uses the v0.13.1 player (`LegacyBrain`), runs no start check and
cannot be reviewed; new games are saved as v6.

The previous version, 0.13.1, added **agent-only soft pause** for take-overs, untimed public floors, eight private colour marks, thinking records
and an LLM call log; votes and other choices require **pick, then submit**. An avatar code recreates exactly the same avatar. Decisions and the test
plan: [docs/v0.13.1-plan.md](docs/v0.13.1-plan.md) (G1–G45, in Chinese). The 0.13.0 changes in that series introduced seat take-over, per-channel
composers, the data directory, single-file `.arenaagents` archives and replay.
The earlier [0.12.1](https://github.com/chengyuchen1037/Arena-Agents/releases/tag/v0.12.1) removed every
engine-side limit that saved money for the user (replaced by persistent conversations, caching, merging and
compaction) and added a per-game spending limit, four-price billing with a price source, the roster and a
responsive UI. Every version has a page under
[Releases](https://github.com/chengyuchen1037/Arena-Agents/releases), and [CHANGELOG.md](CHANGELOG.md) has the
release notes.

## Documentation

- [Architecture (Actor + Worker; Bot = Brain + memory)](docs/architecture.en.md) · [中文](docs/architecture.md)
- [Bots: core files, four memory layers, initialization and review](docs/bots.md) (Chinese) · [the default bot library](bots/README.md) (Chinese)
- [Implementation contract](docs/v0.12-contracts.md) (current, Chinese; v0.13 additions are §11, v0.13.1 is §12 and v0.14 is §13) · [v0.14 plan](docs/v0.14-plan.md) (H1–H59) · [v0.13.1 plan](docs/v0.13.1-plan.md) (G1–G45) · [acceptance record](docs/v0.13.1-acceptance.md) · [v0.13 plan](docs/v0.13-plan.md) (F1–F38) · [v0.12.1 plan](docs/v0.12.1-plan.md) (E1–E26) · [v0.12 plan](docs/v0.12-async-agents-plan.md) (history)
- [The benchmark (bench/)](bench/README.md) (Chinese) · [Running with real models: the smoke test and the default bot library's initialization](docs/real-model-runs.md) (Chinese)
- [Providers and models](docs/providers.md)
- [Data, storage and encryption](docs/storage.md)
- [Archives: export, import and conversion](docs/archive.md) (Chinese)
- [UI guide](docs/ui-guide.md)
- [简体中文 README](README.md)
- [Release notes](CHANGELOG.md)

## Contributing

Issues and pull requests are welcome. Before opening a PR:

1. Keep game rules in `scenarios/` and engine code game-agnostic.
2. Add or update offline tests for behavior changes.
3. Never commit `.env`, API keys, the data directory `data/`, exported `.arenaagents` archives, local paths or generated runtime data.
4. Run `python -m pytest -q tests` and `node --check static/js/*.js`.
5. When you change the architecture or the UI, update the documentation; the source of the architecture
   pictures is `tools/make_diagrams.py` — run `python tools/make_diagrams.py` after editing it (never
   edit an SVG by hand).

## License

MIT — see [LICENSE](LICENSE).
