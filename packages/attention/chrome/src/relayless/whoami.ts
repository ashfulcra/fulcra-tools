// chrome/src/relayless/whoami.ts
//
// Best-effort "who is signed in" lookup for the relayless popup. Used purely
// to render "Signed in as <email>" — it never gates ingest, so every failure
// path degrades to null and the UI falls back to a plain "Signed in."
//
// Where the email comes from:
//   1. The access-token JWT. The OIDC scope requests `email` (config.ts
//      OIDC_SCOPE = "openid profile name email offline_access"), so Auth0
//      mints the access token with an `email` claim. Decoding it locally
//      needs no network round-trip and no extra API surface.
//   2. Fallback: GET /user/v1alpha1/info (fulcra_api/core.py get_user_info →
//      "/user/v1alpha1/info"). NOTE: the documented response shape there is
//      {userid, created, preferences} — it does NOT reliably carry an email,
//      so we only read name/email if the deployment happens to include them.
//      In practice the JWT claim is the real source; /info is a soft backstop
//      that yields the userid as a last-resort label.
//
// The whole thing is wrapped so the caller can do `await whoami(...)` and get
// null rather than a throw when anything goes wrong (offline, malformed JWT,
// 401, etc.).

import { API_BASE } from "./config";
import type { FetchFn } from "./oidc";

export interface WhoAmI {
  /** A human label for the signed-in account — email if we can find one,
   * else the Fulcra userid, else null. */
  label: string | null;
}

/** Decode the payload of a JWT without verifying it (we only read display
 * claims). Returns null on any malformed input. base64url-safe. */
function decodeJwtPayload(token: string): Record<string, unknown> | null {
  const segs = token.split(".");
  if (segs.length < 2) return null;
  try {
    let b64 = segs[1].replace(/-/g, "+").replace(/_/g, "/");
    while (b64.length % 4 !== 0) b64 += "=";
    const json = atob(b64);
    const parsed = JSON.parse(json) as unknown;
    if (parsed && typeof parsed === "object") {
      return parsed as Record<string, unknown>;
    }
    return null;
  } catch {
    return null;
  }
}

/** Pull a human display label out of a decoded JWT payload, if present.
 * Prefers `name` (most readable — e.g. "Ash Kalb"), then `email`, then the
 * Fulcra-namespaced email claim. Intended to run against the OIDC id_token,
 * which (unlike the API-audience access token) carries name/email. */
function labelFromClaims(claims: Record<string, unknown>): string | null {
  for (const key of ["name", "email", "https://fulcradynamics.com/email"]) {
    const v = claims[key];
    if (typeof v === "string" && v.length > 0) return v;
  }
  return null;
}

/**
 * Best-effort identity for the signed-in user. Decodes display claims from the
 * passed JWT first (no network), preferring `name`, then `email`. Callers
 * should hand it the OIDC **id_token** (which carries those claims); the
 * API-audience access token does NOT. Falls back to GET /user/v1alpha1/info
 * for a userid label. Always resolves — never throws — returning
 * { label: null } when nothing usable is found.
 *
 * `token` is whatever JWT the caller has (id_token preferred, access token as
 * a fallback); it is also used as the Bearer for the /info request.
 */
export async function whoami(
  token: string,
  opts: { fetch?: FetchFn } = {},
): Promise<WhoAmI> {
  // 1. JWT display claim — cheapest, most reliable when the id_token is given.
  const claims = decodeJwtPayload(token);
  if (claims) {
    const fromJwt = labelFromClaims(claims);
    if (fromJwt) return { label: fromJwt };
  }

  // 2. /user/v1alpha1/info fallback. Soft — any failure yields null.
  const fetchFn = opts.fetch ?? ((...a: Parameters<FetchFn>) => fetch(...a));
  try {
    const resp = await fetchFn(`${API_BASE}/user/v1alpha1/info`, {
      method: "GET",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (resp.status >= 200 && resp.status < 300) {
      const body = (await resp.json()) as Record<string, unknown>;
      // Prefer a human field if the deployment exposes one (name first for
      // readability), else the userid so we render *something* identifying.
      for (const key of ["name", "email"]) {
        const v = body[key];
        if (typeof v === "string" && v.length > 0) return { label: v };
      }
      const uid = body.userid ?? body.fulcra_userid;
      if (typeof uid === "string" && uid.length > 0) return { label: uid };
    }
  } catch {
    // ignore — best-effort only
  }
  return { label: null };
}
