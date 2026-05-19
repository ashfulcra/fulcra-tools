# Auth0 Application Spec — Fulcra Attention Extension

**Status:** Spec only. Not yet provisioned. Architecture for v1 of the extension uses a localhost relay (no Auth0); this app is what eliminates the relay and unlocks public Chrome Web Store distribution in v2.

**Owner:** Fulcra Dynamics platform team. Ash (ash@fulcradynamics.com) is the requester and owns the [extension repo](https://github.com/ashfulcra/fulcra-attention).

**Tenant:** `fulcra.us.auth0.com` (the same Auth0 tenant the `fulcra-api` Python CLI uses).

---

## Decision: one Auth0 application, many users

This is **one** Auth0 application that represents the extension. The extension is the OAuth **client**; every end-user signs in through that one client and receives their own access + refresh token. Same model as "Sign in with Google" — one Google OAuth client, many user accounts.

We are **not** creating a per-user Auth0 application. Per-user clients would be operationally absurd (one CRUD entry in Auth0 per install) and is not how OAuth is designed.

## Application configuration

| Field | Value |
|---|---|
| **Name** | `Fulcra Attention Extension` |
| **Application type** | Native (treated as a Public Client — no client secret, PKCE required) |
| **Tenant** | `fulcra.us.auth0.com` |
| **Token endpoint auth method** | None (public client) |
| **Grant types** | Authorization Code, Refresh Token |
| **Allowed Callback URLs** | `https://<EXTENSION-ID>.chromiumapp.org/` |
| **Allowed Logout URLs** | `https://<EXTENSION-ID>.chromiumapp.org/` |
| **Allowed Web Origins** | (leave blank — extension does not use silent auth) |
| **Refresh Token Rotation** | Enabled |
| **Refresh Token Expiration** | Inactivity 30 days, absolute 90 days (Auth0 defaults) |
| **Use Refresh Tokens with Rotation** | Yes |
| **Allow Skipping User Consent** | No (this is a third-party app from Auth0's POV) |

### Notes on the redirect URI

`<EXTENSION-ID>` is the 32-character extension ID Chrome assigns. We need **two** callback URLs registered:

1. The **public extension ID** assigned by the Chrome Web Store when the extension is published. This is stable across versions.
2. A **development extension ID**, generated locally from a manifest `key` field we pin during development so it doesn't drift every reload. (See "Generating a stable dev ID" below.)

Both go into the Allowed Callback URLs list. Auth0 matches on exact string equality.

### Scopes / audience

| Field | Value |
|---|---|
| **Scope** | `openid profile email offline_access` |
| **Audience** | `https://api.fulcradynamics.com/` |

`offline_access` is what gets us a refresh token. `audience` is the Fulcra API resource server identifier — must match the existing CLI app's audience or the access token won't authorize against `/ingest/v1/...`.

### What this app is allowed to do

The extension's access token will be used to:
- `POST /ingest/v1/record/batch` (write browsing events)
- `GET /data/v1alpha1/event/DurationAnnotation` and `InstantAnnotation` (verify, dedup readback)
- `GET /user/v1alpha1/tag/...` and `POST /user/v1alpha1/tag` (ensure the `attention`, `web`, `machine:<host>`, `category:<slug>`, and `identity:<email>` tags exist)
- `POST /user/v1alpha1/annotation` (create the `Browsed` annotation definition on first run)
- `GET /user/v1alpha1/me` (display "signed in as ash@fulcradynamics.com" in the popup)

Nothing else. If Fulcra has API-level scope enforcement beyond the resource server audience, the extension's role/scope should be `media:write` or equivalent — same as the CLI today. If no such scoping exists today, no action required.

---

## Setup instructions (for the Fulcra platform engineer provisioning this)

### Step 1 — Create the Auth0 application

1. Sign in to https://manage.auth0.com (Fulcra tenant: `fulcra.us.auth0.com`).
2. **Applications → Applications → Create Application**.
3. Name: `Fulcra Attention Extension`. Type: **Native**. Click Create.
4. Under **Settings**:
   - Confirm **Token Endpoint Authentication Method** = `None` (public client).
   - Set **Allowed Callback URLs** to the development extension ID's redirect URI (we'll add the prod ID after the first CWS submission). Format: `https://<EXTENSION-ID>.chromiumapp.org/`
   - Leave **Allowed Web Origins** empty.
   - Set **Allowed Logout URLs** to the same callback URI.
   - Save.
5. Under **Settings → Advanced Settings → Grant Types**: tick **Authorization Code** and **Refresh Token**. Untick everything else (no Implicit, no Client Credentials, no Password). Save.
6. Under **Settings → Refresh Token Rotation**: enable **Rotation**, leave the default reuse interval (0s), and **Absolute Lifetime** 90 days, **Inactivity Lifetime** 30 days. Save.
7. Copy the **Client ID** — this is what the extension's source code will embed.

### Step 2 — Register the API audience (if not already)

If the Fulcra API resource server (`https://api.fulcradynamics.com/`) isn't already registered as an Auth0 API, it must be (this should already exist — the CLI uses it). Otherwise:

1. **APIs → Create API**.
2. Name: `Fulcra Life API`. Identifier (audience): `https://api.fulcradynamics.com/`. Signing algorithm: RS256.
3. Under that API's **Settings**, enable **Allow Offline Access** so `offline_access` scope can be requested.

### Step 3 — Generate a stable development extension ID

For Auth0 to accept the redirect URL during local development, the extension ID must be stable across reloads. To pin it:

1. Generate an RSA keypair:
   ```bash
   openssl genrsa 2048 | openssl pkcs8 -topk8 -nocrypt -out fulcra-attention-key.pem
   ```
2. Extract the public key as base64:
   ```bash
   openssl rsa -in fulcra-attention-key.pem -pubout -outform DER | openssl base64 -A
   ```
3. Paste that base64 string into `manifest.json` under a top-level `"key"` field.
4. Load the unpacked extension once in `chrome://extensions/`. Note the displayed **ID** (32 lowercase letters).
5. Add `https://<that-id>.chromiumapp.org/` to the Auth0 app's Allowed Callback URLs.

The `manifest.json` `key` field is only included in **development builds** — strip it before publishing to CWS (CWS assigns the production ID and rejects pinned keys).

### Step 4 — After first CWS submission

After uploading the extension to the Chrome Web Store for the first time (even as unlisted), CWS assigns the **production extension ID**. Add `https://<prod-id>.chromiumapp.org/` to the Auth0 app's Allowed Callback URLs. The production build does **not** include the `key` field; CWS computes the ID from its own signing key.

### Step 5 — Hand off to the extension repo

Provide the requester with:
- The Auth0 **Client ID** (paste into the extension's source code as `AUTH0_CLIENT_ID`)
- Confirmation that the **API audience** is `https://api.fulcradynamics.com/`
- Confirmation that **Refresh Token Rotation** is enabled
- The two callback URLs registered

---

## OAuth flow the extension will run

```
1. User clicks "Sign in to Fulcra" in extension popup.
2. Extension generates code_verifier (random 64-char) and code_challenge
   (SHA-256 of verifier, base64url).
3. Extension calls chrome.identity.launchWebAuthFlow({
     url: "https://fulcra.us.auth0.com/authorize?" + new URLSearchParams({
       response_type: "code",
       client_id: AUTH0_CLIENT_ID,
       redirect_uri: chrome.identity.getRedirectURL(),
       scope: "openid profile email offline_access",
       audience: "https://api.fulcradynamics.com/",
       code_challenge: <derived>,
       code_challenge_method: "S256",
       state: <random>,
     }),
     interactive: true,
   })
4. User signs in on Auth0 hosted page (or recognizes their existing session).
5. Auth0 redirects to https://<ext-id>.chromiumapp.org/?code=...&state=...
6. Extension extracts `code`, POSTs to https://fulcra.us.auth0.com/oauth/token:
     grant_type=authorization_code
     code=<code>
     redirect_uri=<same>
     client_id=AUTH0_CLIENT_ID
     code_verifier=<verifier>
7. Auth0 returns { access_token, refresh_token, id_token, expires_in }.
8. Extension stores all four in chrome.storage.local. Schedules a refresh
   chrome.alarms tick at expires_in - 5min.
9. On alarm: POST same /oauth/token with grant_type=refresh_token. With
   rotation enabled, Auth0 returns a new refresh_token; replace the old.
10. On sign-out: POST https://fulcra.us.auth0.com/oauth/revoke with the
    refresh token, then clear chrome.storage.local.
```

The access token is used as `Authorization: Bearer <token>` on every call to `https://api.fulcradynamics.com/`.

---

## Future considerations (not blocking provisioning)

- **Mobile Safari port:** When we ship a Safari Web Extension wrapper, the OAuth flow needs a separate redirect URI scheme (`fulcra-attention://`) because `chromiumapp.org` is Chrome-only. That'll need a **second Allowed Callback URL** added to this same Auth0 app — the app itself doesn't change, just one more URL in the list.
- **Firefox port:** Firefox WebExtensions use the same `browser.identity.launchWebAuthFlow` API, but the redirect host is `<ext-id>.extensions.allizom.org` for AMO-distributed extensions. Adding a Firefox build later means adding a third callback URL to this same Auth0 app.
- **Role/scope hardening:** Today the extension can in principle read anything its access token grants. If Fulcra adds scope-based authorization, request `media:write` (or whatever the equivalent is) explicitly via the `scope` param so the access token is least-privilege.
- **MFA / step-up:** No special handling needed — Auth0's hosted login already presents whatever MFA the user has on their Fulcra account.

---

## Open questions for Fulcra platform

1. Does Fulcra's API today enforce scope-based authorization? If so, what scopes should the extension request beyond `openid profile email offline_access`?
2. Should this extension's tokens be subject to a shorter session lifetime than the CLI's (e.g. force re-auth weekly) given the broader user base on CWS?
3. Is there a Fulcra-internal review step before adding new applications to the production Auth0 tenant, or can ash (or another insider) self-provision?
