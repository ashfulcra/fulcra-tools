// chrome/src/relayless/config.ts
//
// Public, non-secret configuration for talking directly to Fulcra's cloud
// (the "relayless" path — no localhost daemon). Verified against the
// fulcra_api library: fulcra_api/core.py (audience, api base) and
// fulcra_api/oidc.py (domain, client_id, scope, device/token endpoints).
//
// The client_id is a PUBLIC Auth0 native/SPA client id — it is shipped in
// the fulcra_api PyPI package and in the CLI; it is NOT a secret. The device
// authorization grant is designed for clients that cannot keep a secret.

export const OIDC_DOMAIN = "fulcra.us.auth0.com";
export const OIDC_CLIENT_ID = "48p3VbMnr5kMuJAUe9gJ9vjmdWLdnqZt";
export const OIDC_AUDIENCE = "https://api.fulcradynamics.com/";
export const OIDC_SCOPE = "openid profile name email offline_access";

export const DEVICE_CODE_URL = `https://${OIDC_DOMAIN}/oauth/device/code`;
export const TOKEN_URL = `https://${OIDC_DOMAIN}/oauth/token`;

export const API_BASE = "https://api.fulcradynamics.com";
export const INGEST_BATCH_URL = `${API_BASE}/ingest/v1/record/batch`;

export const DEVICE_CODE_GRANT =
  "urn:ietf:params:oauth:grant-type:device_code";
