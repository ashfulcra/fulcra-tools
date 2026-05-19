# fulcra-attention

Capture what takes your attention while browsing — every page you read, with title and time-on-page — into your own [Fulcra](https://fulcradynamics.com) account, so you can later recall *"what was that article I read on Tuesday?"*

This repo holds both halves of the system:

- **Python relay + CLI** at the repo root (`fulcra_attention/`). Runs locally, holds your Fulcra credentials, accepts loopback POSTs from the extension.
- **Chrome MV3 extension** under `chrome/`. Foreground-only capture, optional sharper-AFK content script, onboarding wizard, right-click context menu, branded UI. See [chrome/README.md](chrome/README.md) for build + load instructions.

The extension never sees your Fulcra credentials — it talks to the relay over `127.0.0.1:8771` with a per-machine bearer token.

## Per-machine install

```bash
# 1. Install fulcra-attention (Python relay + CLI)
pipx install -e .

# 2. Authenticate to Fulcra (OIDC device flow via fulcra-api)
fulcra auth login

# 3. Bootstrap the Attention annotation def + tags (idempotent)
fulcra-attention bootstrap

# 4. Generate this machine's bearer token + install launchd/systemd service
fulcra-attention setup

# (Paste the printed bearer token into the Chrome extension popup later.)
```

On macOS, after `setup`:
```bash
launchctl load ~/Library/LaunchAgents/com.fulcra.attention.relay.plist
```

On Linux:
```bash
systemctl --user daemon-reload && systemctl --user enable --now fulcra-attention-relay
```

## Architecture

- Relay binds to `127.0.0.1:8771` (loopback only — no LAN exposure in v1)
- Single endpoint: `POST /attention`, bearer-token authenticated
- Payload: `{url|category, title, og_description, favicon_url, chrome_identity, og_type, lang, start_time, end_time, client}` — exactly one of `url` or `category` non-null
- Each accepted ping becomes one `DurationAnnotation` under the `Attention` def, tagged `attention` + `web`
- Source-id idempotency: `com.fulcra.attention.v1.<sha256(url_or_category|start_time_to_second)[:16]>` — re-posts are silent no-ops

Three-tier privacy posture (Tier 1 always-on, Tiers 2 + 3 user-driven from the extension popup):

| Tier | Action | Default |
|---|---|---|
| **1 — Param strip** | Remove ~80 auth/tracking params | Always on |
| **2 — Categorize** | Replace URL/title with category slug (e.g. `banking`) | Empty by default |
| **3 — Ignore** | Drop event entirely | Empty by default |

Design docs live at `docs/superpowers/specs/2026-05-18-fulcra-attention-v1-design.md` (mirrored from the sibling `FulcraMediaHelpers` repo).

## Multi-machine + multi-identity

`fulcra-attention setup` runs per-machine — each machine gets its own bearer token and its own launchd/systemd unit. The extension's user-managed ignore list propagates across Chrome profiles via Chrome sync (`chrome.storage.sync`).

Users with multiple Chrome profiles (one per company / client / personal) have their `chrome_identity` carried through to `external_ids` on every annotation, so you can group by `external_ids.chrome_identity` at query time. The identity is captured from `chrome.identity.getProfileUserInfo()` (the Google account email signed into that Chrome profile) or a free-text user-set label.

## Manual smoke test

After `bootstrap` and `setup`, with the relay running:

```bash
TOKEN=$(jq -r .bearer_token ~/.config/fulcra-attention/relay.json)
NOW=$(date -u +%FT%TZ)
FIVE_MIN_AGO=$(date -u -v-5M +%FT%TZ 2>/dev/null || date -u -d '5 min ago' +%FT%TZ)

curl -X POST http://127.0.0.1:8771/attention \
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
- **v2:** Direct-to-cloud via Auth0 OAuth (extension drops the relay dependency); needs the dedicated Auth0 app provisioned per the spec above.

## License

Personal-use project. No license declared yet.
