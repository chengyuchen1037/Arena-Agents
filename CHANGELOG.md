# Release Notes

## 0.11 — current

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
rather than itemized in public release notes. The current release is the supported public
baseline.
