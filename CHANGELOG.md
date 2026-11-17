# Release Notes

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
