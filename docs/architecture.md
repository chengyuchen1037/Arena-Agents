# Architecture

Arena-Agents separates the **game-agnostic engine** from the **rules of a specific game**.

```text
config.yml + scenario.md
        │
        ▼
  Scenario parser ──► validated workflow / roles / events / dashboard
        │
        ▼
  Game orchestrator
    ├─ session runtime (bindings, autosave, restore)
    ├─ scenario director (steps, conditions, loops, events)
    ├─ participant turns (LLM + tools, concurrent batches)
    └─ moderator tools (channels, tags, announcements, dashboard)
        │
        ▼
  LLM client
    ├─ OpenAI Chat Completions adapter
    ├─ OpenAI Responses adapter
    ├─ Anthropic Messages adapter
    └─ endpoint planner + per-provider client cache
        │
        ▼
  Workspace storage
    ├─ encrypted provider registry
    ├─ settings / model templates / bindings
    ├─ encrypted session snapshots
    └─ workspace key and lock
```

## Layers

### API

`app/api/` exposes the REST/SSE surface:

- `routes.py` — game controls, state, SSE, import/export;
- `providers.py` — providers, models, templates, bindings, stored sessions;
- `workspaces.py` — workspace listing, native folder picker and safe switching;
- `security.py` — token/Origin checks, URL/SSRF policy, secret redaction;
- `session_io.py` — bounded import validation.

### Engine

`app/engine/` owns game state and execution:

- `orchestrator.py` — process singleton and game lifecycle;
- `session_runtime.py` — model bindings, autosave, restore, graceful shutdown;
- `workspace_runtime.py` — workspace startup and the reversible switch transaction;
- `director.py` — executable scenario steps;
- `scenario.py` / `expressions.py` — Markdown parsing, schema validation, bounded expression evaluation;
- `participant_loop.py` / `participant_tasks.py` — shared LLM/tool turn loop and concurrent batches;
- `context.py` — information isolation and prompt construction;
- `state.py` — sessions, participants, channels, records and usage accounting.

### Providers and protocols

`app/providers/` persists the encrypted provider registry and model metadata. `app/llm/` contains
one adapter per protocol and `endpoints.py`, the single source of truth for URL construction.

Each call resolves an actor's `ModelBinding` to a Provider + model + reasoning depth. The response
carries a price/window snapshot. Usage is accumulated by model and actor, so changing a price
later never rewrites historical cost.

### Workspaces

A workspace is a folder containing `.arena/`. The application can switch the active workspace
only when the game is idle/paused/finished. The switch saves the current game, validates and locks
the target, swaps workspace-scoped stores and LLM clients, restores the target's last session as
paused, and broadcasts `workspace_changed`.

### Storage and encryption

`app/storage/crypto.py` uses AES-256-GCM with a file-version/purpose AAD. The 32-byte workspace key
is wrapped with Windows DPAPI CurrentUser, or supplied by a Docker Secret / environment variable.
Writes are atomic with a known-good backup. The provider registry and session snapshots are
encrypted; `settings.json` contains only non-secret bindings/templates and the last-session pointer.

## Design invariants

- The engine must not contain game-specific vocabulary or rules.
- Scenario definitions are the only authoritative source of game flow.
- Provider keys never enter prompts, logs, API responses or saved sessions.
- A running game cannot change models or workspaces.
- Failed switches/saves must leave the previous state intact.
- Runtime data never goes into the repository.
