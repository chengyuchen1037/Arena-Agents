# Release Notes

## 0.12.1 — in development (branch `dev/v0.12.0`, not yet released)

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
  90 % the oldest complete rounds are trimmed. No round cap anywhere.
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

### Compatibility

- **Saves from 0.12.0 and earlier cannot be opened** (save format v4); they stay in the saved-game list.
- Default model bindings saved for `p01`…`p12` are read as `N1`…`N12`. `execution_depth` is gone from the
  session and the API; `config.yml` no longer has `runtime.debounce` / `max_rounds` / `llm.execution_depth`
  (concurrency limits default to *unlimited*; `compact_at` / `trim_at` are new).

## 0.12 — in development (branch `dev/v0.12.0`, not yet released)

The 0.12 release rebuilds the game loop: **every player and the moderator is now an asynchronous
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
