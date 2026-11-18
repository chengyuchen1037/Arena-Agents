# Data, Storage and Encryption

> **v0.13.** Replaces the v0.12.1 "workspaces" page. In v0.13 there is **one data directory** and nothing to
> choose; only the API keys are encrypted.

Everything Arena-Agents keeps for you — the providers, the settings, the roster, every game, the thinking logs, the
archives you export — lives in one folder, the **data directory**.

> **⚠ The data lives inside the program folder (`<program dir>/data/`).** Deleting or re-installing the program
> folder, a fresh `git clone`, `git clean -xdf` — all of it takes your games with it. 数据在程序目录下：删除程序目录或执行
> `git clean -xdf` 会丢数据，请定期导出存档（设置 → 导出存档）。Export an archive from the settings dialog now and
> then; it is a single `.arenaagents` file that you can import again anywhere (API keys optionally included, protected
> by a password you choose).

## Where the data lives

| Situation | Data directory |
|---|---|
| default | `<program dir>/data/` — the *program directory* is the folder that holds `app/` and `config.yml`, whatever the current directory is when you start the server |
| `ARENA_DATA_DIR` is set | that directory (tests, development instances, a data disk of your own) |
| demo mode (`ARENA_DEMO=1`, scripted bots) and no `ARENA_DATA_DIR` | `<program dir>/.demo-data/demo/` — a demo never mixes into your real history |
| Docker | `/app/data` (the program directory in the image is `/app`), a volume — see [Docker](#docker) |

`data/` and `.demo-data/` are in `.gitignore` and `.dockerignore`. The directory is created (mode `0700` on Linux /
macOS) when the server starts; merely looking at the location never creates it.

### One server per data directory

The server takes an exclusive lock on `data/.lock` for as long as it runs (`msvcrt` on Windows, `fcntl.flock`
elsewhere). A second server on the same directory refuses to start with
*"另一个 Arena-Agents 实例正在使用该数据目录。"* — two servers would overwrite each other's autosaves. The lock belongs to
the process: it disappears when the process ends, even after a crash, so there is nothing to clean up. (A file system
that cannot lock at all — some network shares and bind mounts — is not "another instance": the server logs a warning and
starts anyway, so make sure only one server uses such a directory.) A directory that
cannot be created or written is different: the server comes up in a *degraded* state (`/api/health` says so; endpoints
that need storage answer 503) and nothing is saved.

## Layout

```text
<data dir>/
├─ .lock                    process lock
├─ settings.json            default bindings, model templates, last game, UI preferences, default spending limit
├─ providers.json           the provider registry — everything except the API keys
├─ providers.backup.json    the previous good copy of providers.json
├─ secrets.v1.enc           {provider id: API key}, AES-256-GCM  ← the only encrypted file
├─ master.v1.dpapi          Windows only: the master key, wrapped by DPAPI for the current user
├─ pricing.json             the downloaded price table (public data)
├─ roster/                  names, avatars, persona and memory of the seats (plain files)
│  ├─ roster.json           names + avatar descriptions + the "load memory" switch
│  ├─ N1.persona.md  …      persona text of each seat (no text = no file)
│  └─ N1.memory.md   …      memory of each seat: ## 战绩 / ## 对局记忆 / ## 经验与印象
├─ games/
│  ├─ index.json            the history list (a cache: rebuilt whenever it does not match the folders)
│  └─ <id>/game.json        one game (save format v5, plain JSON)  + game.prev.json, the previous good copy
├─ logs/<id>/trace-*.jsonl  the thinking logs of a game
├─ exports/                 the .arenaagents archives you exported
├─ backups/<time>/          what an archive import replaced (the three most recent are kept)
└─ tmp/                     uploads in progress
```

Nothing but the key file is secret; all of it is meant to be readable, editable (the roster, `providers.json`) and
copyable. A game takes a copy of the roster when it is created, so editing a roster file never changes a game in
progress.

## What is encrypted — and what is not

**Only the API keys.** Everything else — providers (name, protocol, address, models, prices), settings, roster, games,
logs, archives — is plain UTF-8 JSON or text:

- `providers.json` never contains a key. If somebody pastes an `api_key` into it by hand it is ignored (and dropped at
  the next write).
- `secrets.v1.enc` is a JSON envelope (`format`, `version`, `purpose` = `secrets.v1`, `nonce`, `ciphertext`) around
  `{"schema_version": 1, "keys": {"<provider id>": "<api key>"}}`, encrypted with AES-256-GCM
  (`app/storage/crypto.py`). The purpose is part of the authenticated data, so an envelope made for something else
  cannot be passed off as the key file.
- The **master key** (32 random bytes, base64 / base64url) comes from, in this order, `ARENA_MASTER_KEY`,
  `ARENA_MASTER_KEY_FILE`, and — on Windows only — a key generated on first use and wrapped by DPAPI for the current
  user (`master.v1.dpapi`, no password needed). Docker and Linux have no DPAPI: provide the key as a Docker secret or a
  key file (preferred over the environment variable), and back it up **separately** from the data.
- Archives (`.arenaagents`) are plain zip files; keys are in them only if you chose to export them, encrypted with a
  password you typed (scrypt + AES-256-GCM).

### Failing closed

- **No master key** (Linux without `ARENA_MASTER_KEY*`): a key *cannot be saved* — the request answers 503 and nothing
  is written, not even in the clear. A provider **without** a key can still be saved (an imported archive without
  keys is exactly that): it shows as *未设置 API Key*, and a game that uses it cannot start until a key is entered.
  Reading needs no key as long as there is no key file.
- **A key file that cannot be decrypted** (another master key — for instance a copy of `data/` moved to another
  Windows user or machine — or a damaged file): the providers stay readable, listed *without* keys
  (`未设置 API Key（已保存的密钥无法解密：主密钥缺失或不匹配）`), the rest of the program keeps working, and saving a new key is
  refused (503) so the old file, which a restored master key could still open, is never overwritten. To start over,
  delete `secrets.v1.enc` yourself and type the keys again.
- **Environment provider:** with `DEEPSEEK_API_KEY` set there is one more, read-only provider whose key comes from the
  environment. It is never stored and works without any master key.

## Games, autosave and recovery

- Every change of a running game is saved within half a second (there is no "save" button), and once more when the
  server stops. Writes are atomic (temp file → fsync → replace) and verified; the previous good `game.json` is kept as
  `game.prev.json`, so a damaged file costs at most the last save.
- On start the last game comes back **paused** (`settings.json: last_session_id`), never running.
- If the last game cannot be read, the files are **kept untouched**, a blank game is shown with the reason, and
  autosave stays off until you start or create a game. If its file is simply *gone* (deleted by hand, not in an imported
  archive) there is nothing to protect: a fresh game is created and autosave continues.
- A game of an older save format (v0.12.1 and earlier) is not an error: the file stays where it is, a fresh game is
  created and you are told to convert it (below).
- Deleting a game in the history removes `games/<id>/` and `logs/<id>/`. The roster and with it the agents' memory are
  not touched.
- `games/index.json` holds the summaries for the history list (status, winner, duration, cost, whether a person played).
  It is updated by every save and rebuilt from the game files when it is missing, damaged or out of step with the
  folders (a copied-in game, an imported archive, a crash between the two writes).

## Docker

- The image's program directory is `/app`, so the default data directory is **`/app/data`** — no `ARENA_DATA_DIR`, no
  workspace variable. It is created for the unprivileged user (uid 10001, mode `0700`) and declared a `VOLUME`; everything
  else under `/app` is read-only.
- `docker-compose.yml` mounts the **new named volume `arena-appdata`** there (`ARENA_DATA_HOST` bind-mounts a host directory
  instead; on Linux `chown 10001:10001` it first). The old `arena-data` volume (v0.12.1: `/data`) is not used and not
  deleted.
- The master key stays a Docker secret: `ARENA_MASTER_KEY_FILE=/run/secrets/arena_master_key`, from the host file
  `ARENA_MASTER_KEY_HOST_FILE` (required: compose refuses to start without it).
- The image holds no data: `data/`, `.demo-data/`, `.env` and `*.arenaagents` are in `.dockerignore`.

## Environment variables

| Variable | Meaning |
|---|---|
| `ARENA_DATA_DIR` | use this directory instead of `<program dir>/data` |
| `ARENA_DEMO` | scripted bots; the data goes to `<program dir>/.demo-data/demo` unless `ARENA_DATA_DIR` says otherwise |
| `ARENA_MASTER_KEY_FILE` / `ARENA_MASTER_KEY` | the master key for `secrets.v1.enc` (not needed on Windows) |
| `DEEPSEEK_API_KEY` (+ `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, price / window variables) | the read-only environment provider |
| `ARENA_MAX_ARCHIVE_BYTES` | size limit of an uploaded archive (default 2 GiB) |

**Removed in v0.13** (nothing reads them any more): `ARENA_WORKSPACE_DIR`, `ARENA_DISABLE_WORKSPACE`,
`ARENA_DISABLE_LEGACY_MIGRATION`, `ARENA_MAX_IMPORT_BYTES`.

## What changed since v0.12.1

| v0.12.1 | v0.13 |
|---|---|
| a *workspace* folder you choose (default `%USERPROFILE%\Arena-Agents\Default`), data in its `.arena/`; the 工作文件夹 button, switching, recent list, `app-state.json` | one data directory, `<program dir>/data/`; nothing to choose or switch |
| providers **and** games encrypted (`providers.v1.enc`, `sessions/<id>/current.json.enc`) | only the API keys are encrypted (`secrets.v1.enc`); `providers.json` and `games/<id>/game.json` are plain |
| save format v4 | v5: adds `takeover`, `human_spans`, `created_at` / `started_at` / `finished_at` |
| "保存" button, "导入存档" (one game as JSON), "会话" dialog, memory-pack zip export / import | none of them: games autosave; the history lists, replays, continues and deletes games; the only export / import is the settings dialog's archive |
| automatic migration of a v0.9 `.env` and `saves/` at start | removed (an old save is converted with the script below) |
| Docker data in the volume `arena-data` at `/data` (+ `ARENA_WORKSPACE_DIR`) | the volume `arena-appdata` at `/app/data` |
| a data directory could not sit inside the project folder | it sits in the program folder by default |

## Upgrading from v0.12.1

Nothing is converted behind your back and the old data is only ever *read*. On the first start with an empty data
directory, if the old default workspace is found (`%USERPROFILE%\Arena-Agents\Default` on Windows / macOS), the page offers
to explain the conversion (you can dismiss the hint for good).

1. Close the old server (so nothing else holds the old workspace).
2. Run the conversion script with the **same Windows user** that used v0.12.1 (the keys are protected by that user's
   DPAPI), or provide the master key you used (`ARENA_MASTER_KEY_FILE`):

   ```text
   python tools/migrate_v0121_to_archive.py                              # default workspace
   python tools/migrate_v0121_to_archive.py --workspace D:/Arena-Workspaces/Mine --output mine.arenaagents --include-keys
   ```

   It reads the workspace (never changes it), turns the v4 games into v5, takes the settings, roster, price table and logs
   along, and writes one `.arenaagents` file. With `--include-keys` it asks twice for a password and puts the API keys in,
   encrypted with it. Games of v0.12.0 and earlier cannot be converted; they are listed in the report and skipped.
3. Start v0.13 and import the file in **设置 → 导入存档** (enter the password, or skip the keys).

**Docker:** the v0.12.1 data sits in the volume `arena-data`, inside `workspace/default` (`ARENA_WORKSPACE_DIR`). Copy the
workspace out of the old volume (read-only), run the script on it with the master key file you used (`tools/` is not in
the image, so run it on the host with the project's Python environment), start v0.13 with its new volume and import the
archive:

```text
docker run --rm -e UID=$(id -u) -e GID=$(id -g) -v arena-data:/old:ro -v "$PWD/old-workspace:/out" busybox \
  sh -c 'cp -a /old/workspace/default/. /out/ && chown -R $UID:$GID /out'
ARENA_MASTER_KEY_FILE=/srv/arena-secrets/master_key python tools/migrate_v0121_to_archive.py \
  --workspace ./old-workspace --output old.arenaagents --include-keys
docker compose up -d --build        # the new volume arena-appdata; then import old.arenaagents in the settings
```

Keep the old volume until the imported data looks right.

## Backup and moving to another machine

- **Export an archive** (settings dialog): one file with settings, providers, roster, games and (optionally) the logs,
  and — if you ask for it, protected by a password — the API keys. It can be exported while a game runs. Keep the file
  somewhere other than the program folder.
- **Import an archive** replaces the data as a whole. The data it replaced is moved to `backups/<time>/` first (the
  three newest are kept; the keys in there are still bound to this machine's master key), and a failed import rolls
  back by itself. There is no "restore backup" button: to go back to the data from before an import, stop the server and
  move the contents of the backup folder back into `data/` — the steps, and the archive format, are in
  [archive.md](archive.md) (Chinese).
- Copying `data/` by hand works on the same machine and user. Do not expect `secrets.v1.enc` to open anywhere else: it
  needs the same master key (another Windows user or machine cannot decrypt a DPAPI-wrapped one). Everything but the keys
  is plain and fine to copy.

## Troubleshooting

| Message / symptom | Cause and remedy |
|---|---|
| *另一个 Arena-Agents 实例正在使用该数据目录。* | another server runs on this directory; stop it, or give the second one its own `ARENA_DATA_DIR` |
| the server starts "degraded" (`/api/health`), settings and providers answer 503 | the data directory cannot be created or written — check permissions (Docker: the volume must be writable by uid 10001) |
| *无法解密已保存的 API Key* | the master key changed or is missing; restore it, or delete `secrets.v1.enc` and type the keys again |
| *不能保存 API Key：没有可用的主密钥* | Linux / Docker without `ARENA_MASTER_KEY_FILE` / `ARENA_MASTER_KEY`; provide a master key |
| *无法恢复上次对局* | the last game's file is damaged; it is kept as it is, `game.prev.json` may still hold the previous save |
| a provider shows *未设置 API Key* | it was imported without keys (or its key was never saved): edit the provider and type the key |
