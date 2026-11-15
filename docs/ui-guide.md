# UI Guide

Screenshots below come from a local demo game. Workspace paths and Provider hostnames are
pixelated; no credentials are included.

## Live game

![Live game view](images/main-view.png)

Top bar, left to right:

1. **Workspace** — current work folder name. Opens the workspace dialog.
2. **Collapse board** — hides the participant board to give the feed more room.
3. **Theme** — light/dark theme.
4. **Status / phase** — game status and current scenario phase.
5. **Usage** — accumulated tokens and cost. Cost is accumulated from each call's price snapshot;
   editing a model price later does not rewrite history.
6. **New / Save** — start a fresh game, or save and download the current session JSON.
7. **Start / Pause / Resume / Force pause** — game lifecycle. Force pause is for interrupting a
   running action.

The board header also contains:

- **Card size** — compact, standard, large, extra-large;
- **Execution depth** — maximum tool-call rounds per participant turn (not model reasoning depth);
- **Moderator status** — live working state;
- **Import / Sessions** — import a JSON session or load a stored workspace session;
- **Model bindings** — binding and template editor;
- **Provider** — provider and model manager.

Participant cards show seat, ID, name, model binding, role/status tags. Click the model line on a
card to jump directly to that player in the binding dialog.

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

![Provider dialog](images/provider-modal.png)

1. **New Provider** — enter a display name.
2. **Protocol** — OpenAI Chat Completions, OpenAI Responses, or Anthropic Messages.
3. **Base URL** — root URL when auto-append is on; complete endpoint when auto-append is off.
4. **Auto-append API path** — shows the final request/model URLs live. A warning is shown for
   plain HTTP, unusual version paths, or a complete endpoint used in auto mode.
5. **API Key** — write-only. After saving, only “Key configured” is shown. Changing the protocol,
   Base URL or auto-append mode requires re-entering the key.
6. **Provider list** — read-only environment Providers cannot be edited or deleted.
7. **Models** — click **Fetch models** (auto mode only), select radio one model, then fill input
   and output prices. Prices are required and have no default. Repeat for each model.
8. **Edit price / Remove** — edit a model independently or remove it when no binding/template uses
   it.

When auto-append is off, the model-list URL is unavailable and fetch is disabled; type a model ID
manually. See [providers.md](providers.md).

## Templates and bindings

![Templates and bindings](images/templates-bindings.png)

- Each of **Template 1/2/3** stores Provider + model + reasoning depth. Edit the fields and click
  **Save templates**.
- Check the moderator and/or players, then click **Apply template 1/2/3**. Applying is blocked
  while a game runs; editing templates is allowed.
- **Advanced manual bindings** remains available for per-player overrides.
- A template whose Provider/model was removed is marked invalid and cannot be applied.

## Layout controls

- Click **Collapse board** in the top bar; click again to restore.
- Side panels have their own collapse buttons in their headers.
- Drag the horizontal divider under the board to change board height.
- Drag the vertical dividers next to the channel/status panels to change widths.
- Double-click a divider to restore its default size.
- Collapsed/resized state and card size are stored in `localStorage`.

See [layout-collapsed.png](images/layout-collapsed.png) for the collapsed-board state.

## Sessions and recovery

- **Sessions** lists stored sessions for the active workspace.
- Loading a session keeps it paused. Review model bindings, then click **Resume**.
- On startup, the last session is restored as paused. If it is corrupt, the files are kept and a
  recovery banner is shown; the game is not overwritten.
- If the API is protected with `ARENA_API_TOKEN`, a token dialog appears only after a 401. The
  token is stored in browser localStorage.

## First game checklist

1. Select a workspace.
2. Add a Provider and a model with prices.
3. Create or apply model bindings for the moderator and players.
4. Click **Start**.
5. Use **Pause** between turns; do not switch workspace/model while running.
6. Save or rely on autosave; use Sessions to load an earlier game.
