# Providers and Models

Each Provider is a saved API connection. Model bindings reference Providers by ID; keys are
encrypted and never returned to the browser.

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
for the API Key again to prevent a stored key from being redirected to a different host.

## Models

Models are added **one at a time**:

- Model ID;
- input price per 1M tokens (required);
- output price per 1M tokens (required);
- optional context window.

There is no hidden default price. Existing model entries can be edited independently. Model
deletion is blocked while a binding or template references the model.

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
- API keys are masked as `api_key_set` only; errors and logs are redacted.
