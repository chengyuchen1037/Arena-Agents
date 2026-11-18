# Providers and Models

Each Provider is a saved API connection. Model bindings reference Providers by ID; the API key is
encrypted at rest and never returned to the browser.

## Where Providers are stored

Since v0.13 only the **API key** is encrypted (details in [storage.md](storage.md)):

- `providers.json` in the data directory holds everything about a Provider *except* its key — name, protocol,
  address, auto-append setting, models and their prices — as plain, readable JSON, with `providers.backup.json` as
  the previous good copy. It never contains a key (one pasted in by hand is ignored).
- `secrets.v1.enc` holds `{provider id: API key}`, AES-256-GCM encrypted with the master key (`ARENA_MASTER_KEY_FILE` /
  `ARENA_MASTER_KEY`, or DPAPI on Windows).
- The two are merged when a Provider is read, so everything else behaves as if the key were part of the Provider.
- **A Provider can exist without a key** — an archive imported without keys leaves exactly that (the key of a Provider
  with the same id that is already on this machine is kept). It is shown as *未设置 API Key*; the model-binding check
  reports `Provider「…」未设置 API Key`, and a game that uses it cannot start until you edit the Provider and type the
  key. A Provider without a key can be moved to another address freely (there is no key to redirect).
- **No master key** (Linux / Docker without one): a key cannot be saved (503, nothing is written in the clear), but a
  Provider without a key can. **A key file that cannot be decrypted** leaves the Providers listed without keys and
  refuses to save a new key until you restore the master key or delete `secrets.v1.enc`.
- The key of a Provider you delete is removed with it.
- With `DEEPSEEK_API_KEY` set there is one more, read-only Provider whose key comes from the environment; it is never
  stored.

## Protocols

| Protocol | Message endpoint (auto mode) | Model discovery (auto mode) |
|---|---|---|
| OpenAI Chat Completions | `{root}/v1/chat/completions` | `{root}/v1/models` |
| OpenAI Responses | `{root}/v1/responses` | `{root}/v1/models` |
| Anthropic Messages | `{root}/v1/messages` | `{root}/v1/models` |

`root` is the Provider's Base URL. If its path already ends in `/v1`, it is not appended twice.
Custom path prefixes are preserved.

## Auto-append path

The Provider form has an **Auto-append API path** setting.

### Enabled

- Base URL is treated as a root address.
- The form shows the final message URL and model-list URL before saving.
- Model discovery is available.
- A Base URL that already contains `/chat/completions`, `/responses` or `/messages` is rejected;
  switch the toggle off and provide a complete endpoint instead.

### Disabled

- Base URL is the **complete message endpoint** and is sent verbatim.
- Automatic model discovery is disabled and the UI says so; enter model IDs manually.
- Queries are allowed on the complete endpoint when the upstream requires them (for example
  Azure-style `?api-version=` parameters).
- The adapter uses a fixed-URL transport so the SDK cannot append an unexpected path.

Changing the protocol, Base URL or auto-append setting is an endpoint change. Arena-Agents asks
for the API Key again to prevent a stored key from being redirected to a different host (a Provider
that has no key yet needs none).

## Models

Models are added **one at a time**:

- Model ID;
- **four prices** per 1M tokens: input (tokens that missed the provider's prompt cache, required),
  output (required, reasoning tokens are billed as output), **cache read** and **cache write**
  (optional: empty = billed like an input token; `0` is a real, free price);
- optional context window.

There is no hidden default price. Existing model entries can be edited independently. Model
deletion is blocked while a binding or template references the model.

A call is billed as
`(uncached input × input + cache read × cache read price + cache write × cache write price + output × output) / 10⁶`
with the prices that were in force **when the call was made** (editing a price never rewrites history).
Every protocol reports the cache counters separately (OpenAI `cached_tokens`, DeepSeek
`prompt_cache_hit_tokens`, Anthropic `cache_read_input_tokens` / `cache_creation_input_tokens`).

## Price source

Typing four prices per model is tedious, so the Provider dialog has a **price source**: a JSON
price table, by default
`https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json`.

- **Refresh** downloads the table (https only, no redirects, SSRF-guarded like model discovery, 30 s,
  20 MB cap) and keeps a reduced copy in the data directory (`pricing.json`). A failed download keeps the
  old table and says why.
- **Fetching a Provider's model list** adds a *suggested* price set and context window to every model it
  recognises; picking a model fills the four prices in. You can edit every number before saving.
- Matching: the exact id, then `<litellm provider>/<id>` (the provider is guessed from the Base URL and the
  protocol), then the id without a date suffix, then case-insensitively, finally a *unique* `…/<id>` entry.
  No match = no suggestion (nothing is guessed).
- A model remembers where its prices came from (`litellm:<key>` or `manual`); editing any price makes it
  `manual`. Unit conversion (the table is per token) is done for you.
- You can point the source at your own table (same JSON shape) — the URL is validated and, for a private
  host, subject to the usual `ARENA_ALLOW_PRIVATE_PROVIDERS` rule.

## Templates

The model-binding dialog has three fixed template slots. A template stores:

- template name;
- Provider;
- model;
- reasoning depth (`low`, `medium`, `high`, `xhigh`).

Templates do not store prices or API keys. Apply a template to checked players and the moderator.
Template references are validated when saved and applied; a missing Provider/model marks the
template as invalid.

## Runtime rules

- While a game is running, bindings, Provider protocol/address and model add/remove are locked.
- Key rotation and price editing are allowed while running.
- Applying a template is blocked while running.
- A game start validates all required bindings and reports clear errors instead of silently
  falling back to another Provider.

## Security

- URL validation rejects userinfo, control characters, fragments, blocked link-local/metadata
  addresses, and private/loopback addresses unless
  `ARENA_ALLOW_PRIVATE_PROVIDERS=true`.
- `ARENA_ALLOWED_BASE_HOSTS` can restrict Provider hosts to an explicit allow-list.
- Model discovery is server-side with a timeout, response-size limit and redirect protection.
- API keys are masked as `api_key_set` only; errors and logs are redacted. Keys exist on disk only inside
  `secrets.v1.enc`, and — if you export them — inside an archive's password-encrypted `secrets.enc`.
