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

## 2. Read the compiled preferences (one GET each)

1. `GET /input/v1/file_upload?path=prefs&state=uploaded` → find your doc and its
   id. Prefer `platforms/<your-platform>.json` (global + your overrides); if it
   isn't there you simply have no platform-specific overrides — fall back to
   `compiled.json` (the global doc), don't treat its absence as "no prefs".
2. `GET /input/v1/file_upload/{id}/download` → the compiled doc. Apply it:
   keys are namespaced prefs, `weight` in [-1,1], negative = aversion,
   `stale: true` = verify with the user before relying on it.

## 3. Capture a signal (one POST)

`POST /ingest/v1/record` with JSON body:

    {"data": "{\"v\":1,\"kind\":\"preference\",\"key\":\"dining.cuisine.thai\",
      \"scope\":\"global\",\"value\":{\"liked\":true},\"strength\":0.8,
      \"confidence\":0.9,\"half_life_days\":90,
      \"source\":{\"platform\":\"chatgpt\",\"agent\":null,\"session\":null},
      \"supersedes\":null}",
     "metadata": {"content_type": "application/json",
       "data_type": "<bare type — see note below>",
       "recorded_at": "<now, ISO8601 UTC>",
       "source": ["com.fulcra-prefs.sig.<24-hex-of-sha256(key|recorded_at|platform)>",
                   "com.fulcradynamics.annotation.<definition_id>",
                   "com.fulcra-prefs.capture.<your-platform>"]},
     "specversion": 1}

**data_type**: `prefs/meta.json` stores `"data_type": "MomentAnnotation/<definition_id>"`.
Split on the first "/":
- `metadata.data_type` = the part before the slash, e.g. `"MomentAnnotation"` — this
  is the FulcraDataTypes enum value the API accepts. Sending the full compound string
  causes a 422.
- `metadata.source[1]` = `"com.fulcradynamics.annotation.<definition_id>"` where
  `<definition_id>` is the part after the slash (also available as
  `meta.json`'s `"definition_id"` field). This is how the record links to its
  definition — matching the production pattern in the attention Chrome extension.

Read `prefs/meta.json` using the same two-GET pattern as step 2.
Retry once on failure, then tell the user the capture didn't stick.

## 4. What you cannot do at this tier

Compile and solve run only where code runs (CLI-capable agents or cron).
Your single ingest POST is enough: compile reads signals straight from
get-records, so a capture you make here shows up in everyone's compiled docs
after the next compile elsewhere — you do NOT need to write any cache file.
