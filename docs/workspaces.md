# Workspaces and Encrypted Data

A workspace is a user-chosen folder that isolates all private runtime state for one group of
games.

## Where data lives

```text
<workspace>/
├─ .arena-workspace.json        workspace id, name and schema
└─ .arena/
   ├─ master.v1.dpapi          Windows DPAPI-wrapped workspace key
   ├─ providers.v1.enc         encrypted provider registry
   ├─ providers.v1.backup.enc  known-good backup
   ├─ settings.json            bindings, templates, last session, UI preferences, default spending limit
   ├─ pricing.v1.json          the downloaded price table (public data, plain JSON; v0.12.1)
   ├─ roster/                  the players' names, avatars, persona and memory (plain files; v0.12.1)
   │  ├─ roster.json           names + avatar descriptions + the "load memory" switch
   │  ├─ N1.persona.md  …      persona text of each seat (no text = no file)
   │  └─ N1.memory.md   …      memory of each seat: ## 战绩 / ## 对局记忆 / ## 经验与印象
   ├─ sessions/<id>/           current + previous encrypted session snapshots
   ├─ logs/  exports/
   └─ workspace.lock
```

The roster files are deliberately **not encrypted**: they are text you may want to read, edit, back up or
share (the *Roster* menu exports them all as one zip). They never contain Provider keys. A game takes a
copy of the roster when it is created, so editing a file never changes a game in progress.

The application-level `%LOCALAPPDATA%/Arena-Agent/app-state.json` only remembers the last/recent
workspace paths. It never stores Provider keys or session content.

Default workspace:

- Windows: `%USERPROFILE%\Arena-Agents\Default`
- macOS: `~/Arena-Agents/Default`
- Linux/Docker: `<app data dir>/workspace/default` (Docker: `/data/workspace/default`)

Override with `ARENA_WORKSPACE_DIR`.

## Switching workspaces

1. Open **工作文件夹 / Workspace** at the top-left.
2. Pick a folder using the native picker (loopback only) or enter an absolute path.
3. Confirm the switch.
4. The current game is saved first. If saving fails, or the target is invalid/locked, the switch
   is aborted and the current workspace remains active.
5. On success the page reloads, loads the target workspace's last session as **paused**, and
   rebuilds Providers, templates and bindings.

Rules:

- Allowed only while the game is `idle`, `paused` or `finished`.
- A `running` game must be paused first (the API returns 409).
- Workspaces are server-wide state: all browser tabs use the same active workspace.
- Creating a workspace in a non-empty folder requires explicit initialization confirmation; no
  existing file is moved or deleted.
- Source-code directories, drive roots, the user home itself and system directories are rejected.

## Encryption

- AES-256-GCM with a versioned, purpose-bound AAD.
- Writes are atomic and verified; a previous snapshot is kept for recovery.
- Windows uses DPAPI CurrentUser to wrap the workspace key, so the same Windows user can reopen
  it without a password.
- Docker/Linux uses `ARENA_MASTER_KEY_FILE` or `ARENA_MASTER_KEY` (32 bytes, base64/base64url).
  Missing keys fail closed; there is no plaintext fallback.
- Back up the workspace key separately. A copied workspace cannot be decrypted by another Windows
  user or machine without the key.

## Migration from earlier single-directory releases

On first workspace startup:

1. The default workspace is created if needed.
2. Existing encrypted application data (`providers.v1.enc`, `settings.json`, `sessions`,
   DPAPI key) is copied into the workspace and verified.
3. Source files are **not deleted or moved**; after confirming the workspace opens, remove them
   manually if desired.
4. Legacy `.env`/`saves` migration still runs through the guarded migration path.

Set `ARENA_DISABLE_WORKSPACE=1` to use the legacy single-directory mode (mainly for tests and
compatibility tooling). Set `ARENA_DISABLE_LEGACY_MIGRATION=1` to skip `.env`/`saves` migration.
