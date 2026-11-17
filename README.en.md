# Arena-Agents · AI Werewolf (狼人杀) & Multi-Agent Arena

**English** | [简体中文](README.md)

**Arena-Agents is an open-source AI Werewolf (狼人杀), social-deduction and multi-agent game arena.**
It is a game-agnostic sandbox for LLM agents: a Markdown scenario defines the game rules, and the
engine runs players, voting, speech, channels, events and recovery. The bundled scenario is a
complete 12-player Werewolf game; the same engine can host other social-deduction or tabletop
rules. Keywords: **AI 狼人杀, 狼人杀, 多智能体狼人杀, AI 多智能体博弈, social deduction, AI Werewolf,
LLM Werewolf, AI 桌游, AI 剧本杀, multi-agent game**.

[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.12.0--dev-blue.svg)](CHANGELOG.md)

---

**Arena-Agents 是一个开源的 AI 狼人杀、社交推理与多智能体博弈沙盒。**
游戏规则写在可执行 Markdown 剧本中，引擎负责玩家、发言、投票、频道、事件与断点恢复。内置一套完整的 12 人狼人杀，也可用同一引擎运行其他社交推理/桌游规则。

<img src="docs/images/main-view.png" alt="Arena-Agents AI Werewolf live game view" width="100%">

## Highlights

- **Executable Markdown scenarios** — roles, phases, workflows, event hooks, win conditions and
  dashboard rules live in `scenarios/*.md`, not in Python conditionals.
- **Multi-provider model routing** — OpenAI Chat Completions, OpenAI Responses and Anthropic
  Messages; every player and the moderator can use a different model and reasoning depth.
- **Model templates** — save up to three Provider/model/reasoning presets and apply them to
  selected players in one action.
- **Workspaces** — each work folder has its own providers, templates, bindings, sessions, logs
  and encryption key. Switch folders without mixing games or API keys.
- **Safe live operations** — models are locked while a game is running; switching workspace
  requires pausing and successfully saving first.
- **Encrypted local data** — provider keys and sessions are encrypted with AES-256-GCM; on
  Windows the workspace key is protected by DPAPI.
- **Endpoint preview** — see the exact message and model-list URLs before saving a Provider;
  either auto-append `/v1/...` paths or point at a complete custom endpoint.
- **Resizable arena UI** — collapse the board or side panels, drag widths/heights, and choose
  four player-card size tiers.

## Screenshots

| Game board and live feed | Workspace selector |
|---|---|
| <img src="docs/images/main-view.png" alt="Game board and live feed" width="100%"> | <img src="docs/images/workspace-modal.png" alt="Workspace selector" width="100%"> |
| **Provider management and URL preview** | **Model templates and bindings** |
| <img src="docs/images/provider-modal.png" alt="Provider management and endpoint preview" width="100%"> | <img src="docs/images/templates-bindings.png" alt="Model templates and bindings" width="100%"> |

> Screenshots are taken from a local demo game. Workspace paths and provider hostnames are
> pixelated before publication; no credentials are displayed.

More UI walkthrough: [docs/ui-guide.md](docs/ui-guide.md).

## How it works

```text
Browser
  │  REST + SSE
  ▼
FastAPI API ────────────── Workspace selector
  │                          │
  ▼                          ▼
Game Orchestrator ◄──── Workspace (.arena/)
  │  ├─ Session runtime       ├─ providers.v1.enc
  │  ├─ Scenario Director     ├─ settings.json (templates / bindings)
  │  ├─ Participant turns     ├─ sessions/*.enc
  │  └─ Moderator tools       └─ workspace key
  │
  ▼
LLM Client ──► OpenAI Chat / OpenAI Responses / Anthropic Messages adapters
  │
  └─ encrypted usage, price snapshots, retry/backoff
```

The engine never knows game-specific phases or role names. A Scenario Parser validates the
Markdown front matter; the Director executes workflow steps, conditions, loops, events and win
conditions. See [docs/architecture.md](docs/architecture.md) for the current module map.

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

On first launch Arena-Agents creates the default workspace:

```text
Windows: %USERPROFILE%\Arena-Agents\Default
macOS:   ~/Arena-Agents/Default
Linux:   <app data dir>/workspace/default
```

Provider settings are configured from the UI; no `.env` file is required for local use.

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
all runtime data in `/data/workspace/default/.arena`. The master key must be a Docker Secret or a
separate readable file; without it the app fails closed instead of writing plaintext.

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
5. Fetch models (when auto-append is on), select **one** model, and enter its input/output prices
   per 1M tokens. Prices are required and never default to a hidden value.
6. In **Model Bindings**, create templates 1/2/3 or bind a Provider/model/reasoning depth per
   player and the moderator. Check players and click a template to apply it.

Full details: [docs/providers.md](docs/providers.md).

## Browse and load games

Use **Session** in the game header or **Load game from this workspace** in the workspace dialog.
Sessions are scoped to the current workspace. Loading a saved game keeps it paused; review model
bindings and click **Resume** when ready.

## Workspaces

A workspace is a normal folder. Everything that belongs to a set of games lives under
`<folder>/.arena/`:

```text
<workspace>/
├─ .arena-workspace.json      workspace marker
└─ .arena/
   ├─ master.v1.dpapi         Windows DPAPI-wrapped workspace key
   ├─ providers.v1.enc        encrypted provider registry
   ├─ providers.v1.backup.enc known-good backup
   ├─ settings.json           default bindings, templates, last session
   ├─ sessions/<id>/          encrypted sessions and previous copy
   ├─ logs/  exports/
   └─ workspace.lock
```

- Switching is allowed only while the game is idle, paused or finished.
- The current game is saved first; a failed save or invalid target leaves the current workspace
  unchanged.
- Running games cannot switch workspaces.
- The native folder picker is available to loopback clients only. Headless/Docker deployments use
  an absolute path instead.
- Code assets (`config.yml`, `scenarios/`, `prompts/`) are shared by all workspaces.
- An earlier single-directory data layout is copied into the default workspace and verified while
  the originals are kept untouched.

Full details: [docs/workspaces.md](docs/workspaces.md).

## Layout controls

- **Board**: collapse from the top bar.
- **Side panels**: collapse channels or status from their headers.
- **Resize**: drag the horizontal bar under the board or the vertical bars beside the side panels;
  double-click a bar to reset.
- **Card size**: compact / standard / large / extra-large from the board header.

Layout and card-size preferences are stored in the browser's `localStorage`.

## Configuration

All runtime settings, provider keys, models and sessions belong to a workspace. The application
reads only these deployment-level environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `ARENA_WORKSPACE_DIR` | `%USERPROFILE%\\Arena-Agents\\Default` (Windows/macOS) | Initial/default workspace |
| `ARENA_DISABLE_WORKSPACE` | unset | `1` disables workspaces and uses the legacy single data directory |
| `ARENA_DATA_DIR` | platform app directory | Application state / legacy data directory |
| `ARENA_MASTER_KEY_FILE` / `ARENA_MASTER_KEY` | unset (Windows uses DPAPI) | 32-byte workspace master key for Linux/Docker |
| `ARENA_API_TOKEN` | unset | Bearer token for `/api/*` writes; also accepted as `?token=` for SSE |
| `ARENA_ALLOWED_ORIGINS` | unset | Extra allowed cross-origin origins |
| `ARENA_ALLOWED_BASE_HOSTS` | unset | Optional provider host allow-list |
| `ARENA_ALLOW_PRIVATE_PROVIDERS` | `false` | Allow loopback/private provider endpoints (e.g. Ollama) |
| `ARENA_MAX_IMPORT_BYTES` | `20971520` | Maximum session import size |
| `ARENA_BIND` | `127.0.0.1` | Docker host bind address |

`config.yml` contains only game defaults, seats/personas, tag styles and the scenario path. The
cryptographic provider registry is never stored in YAML or `.env`.

## Security

- Provider keys are encrypted at rest and are never returned by the API. Changing protocol, Base
  URL or endpoint mode requires re-entering the key.
- Non-GET requests require a same-origin `Origin` (or an allow-listed origin). Without a token,
  configuration writes are limited to loopback clients.
- Provider URL validation blocks cloud metadata/link-local addresses; private/loopback endpoints
  require explicit opt-in.
- Workspace switching and native folder selection are loopback-only and require a paused game.
- Imported sessions are size-limited and validated before replacing the current game.
- The server defaults to `127.0.0.1`; expose it only behind a TLS reverse proxy with
  `ARENA_API_TOKEN` configured.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q tests
```

The suite is offline and never calls a real LLM. Node.js enables the frontend helper tests; they
are skipped when Node is unavailable. CI runs on Ubuntu and Windows.

Current local verification: **899 passed, 3 skipped, 101 subtests**.

## Project structure

```text
app/
├─ api/          REST + SSE, providers, templates, workspaces
├─ engine/       orchestration, scenario director, turns, recovery
├─ llm/          protocol adapters and endpoint planning
├─ providers/    encrypted provider registry / model metadata
├─ storage/      workspace, settings, sessions, crypto
└─ migration.py  legacy data migration
scenarios/       executable Markdown scenarios
prompts/         prompt templates
knowledge/       optional strategy documents
static/          single-page arena UI
tests/           offline regression, API, storage and UI tests
docs/            architecture, provider, workspace and UI guides
```

## Release

The current supported release is **0.11.0**, focused on workspaces, model templates and endpoint
control. Earlier releases are intentionally summarized rather than itemized; see
[CHANGELOG.md](CHANGELOG.md) for the public release notes.

## Documentation

- [Architecture](docs/architecture.md)
- [Providers and models](docs/providers.md)
- [Workspaces and encrypted data](docs/workspaces.md)
- [UI guide](docs/ui-guide.md)
- [简体中文 README](README.md)
- [Release notes](CHANGELOG.md)

## Contributing

Issues and pull requests are welcome. Before opening a PR:

1. Keep game rules in `scenarios/` and engine code game-agnostic.
2. Add or update offline tests for behavior changes.
3. Never commit `.env`, API keys, workspaces, local paths or generated runtime data.
4. Run `python -m pytest -q tests` and `node --check static/js/*.js`.

## License

MIT — see [LICENSE](LICENSE).
