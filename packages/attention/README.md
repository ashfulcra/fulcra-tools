# fulcra-attention

Capture what takes your attention while browsing — every page you read, with title and time-on-page — into your own [Fulcra](https://fulcradynamics.com) account, so you can later recall *"what was that article I read on Tuesday?"*

The attention plugin owns the Fulcra "Attention" annotation definition and verifies the pipeline is wired up. Browser events are received by the fulcra-collect daemon on its stable port (default `9292`) at `POST /api/extension/attention`, then forwarded to Fulcra.

This package holds:

- **`fulcra_attention/`** — the Python plugin: bootstraps the Attention annotation definition + tags, owns this machine's hostname tag, and exposes a small CLI for catalog inspection. No relay, no launchd unit — the daemon's web port is the ingest surface now.
- **`chrome/`** — Chrome MV3 extension. Foreground-only capture, optional sharper-AFK content script, onboarding wizard, right-click context menu, branded UI. See [chrome/README.md](chrome/README.md) for build + load instructions.

## Setup

Setup is via the daemon's onboarding wizard:

1. Install / start the fulcra-collect daemon (`pipx install -e packages/collect`, then launch the menubar app).
2. Open **Preferences → Plugins → Attention**. The wizard guides you through:
   - Bootstrapping the Attention annotation definition (idempotent).
   - Installing the Chrome extension (links to the release zip or `chrome/dist/`).
   - **Pair extension** — one click hands the extension its bearer token and the daemon's port. Stored in `chrome.storage.local`.
3. (Optional) Run `fulcra-attention setup` once to tag this machine's events with its hostname.

## Architecture

- Daemon hosts the ingest endpoint on its configured `[daemon] web_port` (default `9292`) — loopback only.
- Single endpoint: `POST /api/extension/attention`, bearer-token authenticated against the per-extension token issued by the pair flow.
- Payload: `{url|category, title, og_description, favicon_url, chrome_identity, og_type, lang, start_time, end_time, client}` — exactly one of `url` or `category` non-null
- Each accepted ping becomes one `DurationAnnotation` under the `Attention` def, tagged `attention` + `web`
- Source-id idempotency: `com.fulcra.attention.v1.<sha256(url_or_category|start_time_to_second)[:16]>` — re-posts are silent no-ops

Three-tier privacy posture (Tier 1 always-on, Tiers 2 + 3 user-driven from the extension popup):

| Tier | Action | Default |
|---|---|---|
| **1 — Param strip** | Remove ~80 auth/tracking params | Always on |
| **2 — Categorize** | Replace URL/title with category slug (e.g. `banking`) | Empty by default |
| **3 — Ignore** | Drop event entirely | Empty by default |

## Multi-machine + multi-identity

Each machine runs its own daemon, with its own extension pair token. The extension's user-managed ignore list propagates across Chrome profiles via Chrome sync (`chrome.storage.sync`).

Users with multiple Chrome profiles (one per company / client / personal) have their `chrome_identity` carried through to `external_ids` on every annotation, so you can group by `external_ids.chrome_identity` at query time. The identity is captured from `chrome.identity.getProfileUserInfo()` (the Google account email signed into that Chrome profile) or a free-text user-set label.

## Manual smoke test

After bootstrap + pair, with the daemon running:

```bash
# Token is whatever the pair flow issued; you can re-read it from the
# extension popup's "Reveal token" debug control or re-pair to mint a new one.
TOKEN="<pair-token>"
NOW=$(date -u +%FT%TZ)
FIVE_MIN_AGO=$(date -u -v-5M +%FT%TZ 2>/dev/null || date -u -d '5 min ago' +%FT%TZ)

curl -X POST http://127.0.0.1:9292/api/extension/attention \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"https://example.com/article\",\"title\":\"Smoke test\",\"category\":null,\"start_time\":\"$FIVE_MIN_AGO\",\"end_time\":\"$NOW\",\"client\":\"curl/0.1\",\"chrome_identity\":\"ash@fulcradynamics.com\",\"og_type\":\"article\",\"lang\":\"en\"}"
```

Expected: `{"posted":1,"dropped":0}`. Confirm the annotation appears in Fulcra:

```bash
fulcra get-records --type DurationAnnotation --start "1 hour ago" \
  | jq '.[] | select(.data.service == "web")'
```

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q                  # 102 tests as of v0.1.0
```

Architecture references (in the sibling repo):
- Design spec: `FulcraMediaHelpers/docs/superpowers/specs/2026-05-18-fulcra-attention-v1-design.md`
- Auth0 app spec (for v2 distribution): `FulcraMediaHelpers/docs/superpowers/specs/2026-05-18-fulcra-browse-extension-auth0-app.md`
- Plan A (this repo's build): `FulcraMediaHelpers/docs/superpowers/plans/2026-05-18-fulcra-attention-plan-a-python.md`

## Status

- **Plan A (this repo):** Python backend complete. 102 tests passing.
- Chrome extension: shipped, lives under [chrome/](chrome/). Foreground-only attention, AFK detection, pause control, onboarding wizard, right-click context menu, branded UI.
- **v2:** Direct-to-cloud via Auth0 OAuth (extension posts straight to Fulcra instead of through the local daemon); needs the dedicated Auth0 app provisioned per the spec above.

## License

Personal-use project. No license declared yet.
