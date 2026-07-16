# fulcra-prefs over raw HTTP (no shell)

For agents that can make HTTP requests but cannot run a CLI. All endpoints on
`https://api.fulcradynamics.com`; auth domain `https://fulcra.us.auth0.com`.
Background: FULCRA-PRIMITIVES.md at the repo root.

## 1. Authenticate (device flow, three calls)

1. `POST https://fulcra.us.auth0.com/oauth/device/code`
   form: `client_id=48p3VbMnr5kMuJAUe9gJ9vjmdWLdnqZt`,
   `audience=https://api.fulcradynamics.com/`,
   `scope=openid profile email offline_access`
2. Show the user `verification_uri_complete`; they approve in a browser.
3. Poll `POST https://fulcra.us.auth0.com/oauth/token`
   form: `client_id=...`, `grant_type=urn:ietf:params:oauth:grant-type:device_code`,
   `device_code=<from step 1>` → `{access_token, refresh_token, expires_in}`.
   Send `Authorization: Bearer <access_token>` on every call below.
   NEVER show the token to the user or store it anywhere visible.

### 1a. Headless device flow without a browser on this host

Instead of opening a browser locally, request the code, have the human approve on
another device, then poll:
`POST /oauth/device/code` returns `verification_uri_complete` + `device_code`;
print the URL for the human; poll `/oauth/token` with the `device_code` grant
until it stops returning `authorization_pending`. This is the same three calls,
just split across devices.

### 1b. Writing the CLI credential file + refreshing (shell host on a proxied sandbox)

When the host *does* have the `fulcra`/`coord-engine` CLIs but the CLI's own login
can't run (e.g. its device flow uses raw `http.client` and bypasses an
intercepting `HTTPS_PROXY`), drive the flow above with a proxy-aware HTTP client
and write the result to `~/.config/fulcra/credentials.json` so the CLIs work from
then on. **Exact schema** (all four fields; timestamps are ISO-8601 **local, naive**
— the CLI compares against `datetime.now()`, so a tz-aware value raises):

```json
{
  "access_token": "<from /oauth/token>",
  "access_token_expiration": "<now + expires_in seconds, ISO local naive>",
  "refresh_token": "<from /oauth/token>",
  "refresh_token_expiration": "<now + ~30 days, ISO local naive>"
}
```

The Auth0 token response gives `expires_in` (seconds), not an absolute time —
convert it: `access_token_expiration = now + expires_in`. Auth0 does not return a
refresh-token lifetime here; use a conservative `now + 30 days`.

**Refresh (no human re-prompt):** when the access token nears expiry,
`POST https://fulcra.us.auth0.com/oauth/token` form
`grant_type=refresh_token`, `client_id=48p3VbMnr5kMuJAUe9gJ9vjmdWLdnqZt`,
`refresh_token=<stored>` → `{access_token, expires_in, refresh_token?}`. Recompute
`access_token_expiration` from the new `expires_in`; if the response includes a new
`refresh_token` (rotation), replace the stored one and its expiration; rewrite the
file. The refresh token is valid for ~30 days, so a long-running host self-renews
without prompting a human again.

## 2. Read the compiled preferences (one GET each)

1. `GET /input/v1/file_upload?path=prefs&state=uploaded` → find your doc and its
   id. Prefer `platforms/<your-platform>.json` (global + your overrides); if it
   isn't there you simply have no platform-specific overrides — fall back to
   `compiled.json` (the global doc), don't treat its absence as "no prefs".
2. `GET /input/v1/file_upload/{id}/download` → the compiled doc. Apply it:
   keys are namespaced prefs, `weight` in [-1,1], negative = aversion,
   `stale: true` = verify with the user before relying on it.

## 3. Capture a signal (one POST)

`POST /ingest/v1/record/<bare type>` (the TYPED surface — the data_type is a
path segment) with the UNWRAPPED JSON body:

    {"note": "{\"v\":1,\"kind\":\"preference\",\"key\":\"dining.cuisine.thai\",
      \"scope\":\"global\",\"value\":{\"liked\":true},\"strength\":0.8,
      \"confidence\":0.9,\"half_life_days\":90,
      \"source\":{\"platform\":\"chatgpt\",\"agent\":null,\"session\":null},
      \"supersedes\":null}",
     "recorded_at": "<now, ISO8601 UTC>",
     "sources": ["com.fulcra-prefs.sig.<24-hex-of-sha256(key|recorded_at|platform)>",
                  "com.fulcradynamics.annotation.<definition_id>",
                  "com.fulcra-prefs.capture.<your-platform>"]}

Send `Content-Type: application/json` and a `content-length` header; `201` →
`{"upload_id": "<uuid>"}`. The signal payload is a JSON **string** in `note`.

**data_type (the path segment)**: `prefs/meta.json` stores
`"data_type": "MomentAnnotation/<definition_id>"`. Split on the first "/":
- The URL uses the part BEFORE the slash, e.g.
  `POST /ingest/v1/record/MomentAnnotation` — the base FulcraDataTypes enum value.
  A custom definition is NOT a valid path segment (`.../MomentAnnotation/<uuid>`
  404s), so it rides in `sources` instead (next bullet).
- `sources[1]` = `"com.fulcradynamics.annotation.<definition_id>"` where
  `<definition_id>` is the part after the slash (also available as
  `meta.json`'s `"definition_id"` field). This is how the record links to its
  definition — matching the production pattern in the attention Chrome extension.

The legacy wrapped `POST /ingest/v1/record` with a `DataRecordV1` envelope
(`{data, metadata:{data_type, recorded_at, source}, specversion:1}`) still works
but is unpublished/retirement-eligible; prefer the typed path above. Reads
tolerate both (`data`-or-`note`).

Read `prefs/meta.json` using the same two-GET pattern as step 2.
Retry once on failure, then tell the user the capture didn't stick.

## 4. What you cannot do at this tier

Compile and solve run only where code runs (CLI-capable agents or cron).
Your single ingest POST is enough: compile reads signals straight from
get-records, so a capture you make here shows up in everyone's compiled docs
after the next compile elsewhere — you do NOT need to write any cache file.
