// chrome/src/scrub.ts
//
// Tier 1 URL scrubbing — TypeScript port of fulcra_attention/scrub.py.
// CROSS-LANGUAGE CONTRACT: this must produce byte-identical output to the
// Python sibling for every entry in tests/fixtures/scrub_cases.json.

export const DENYLIST: ReadonlySet<string> = new Set([
  // auth-bearing — OAuth 2 + plain
  "access_token", "id_token", "refresh_token", "code", "state", "nonce",
  "client_secret", "assertion", "session", "sid", "sessionid", "auth",
  "authorization", "token", "apikey", "api_key", "key", "signature",
  "sig", "hmac", "x-amz-signature", "x-amz-credential",
  "x-amz-security-token", "expires", "password", "pwd", "pw", "otp",
  "magic", "share_token", "invite", "confirmation_token",
  "_csrf", "csrf_token", "xsrf", "ticket", "ott",
  // OAuth 1.0a (Twitter / Trello / many self-hosted apps) — literal
  // session credentials in the query string.
  "oauth_token", "oauth_verifier", "oauth_signature", "oauth_callback",
  "oauth_consumer_key", "oauth_nonce", "oauth_timestamp",
  // tracking
  "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
  "gclid", "fbclid", "msclkid", "mc_eid", "mc_cid", "_hsenc", "_hsmi",
  "igshid", "yclid", "ref", "ref_src", "ref_url",
  // one-click action
  "unsubscribe", "unsub", "verify", "reset", "confirm", "activate",
  // magic-link identifiers (single-use login over email)
  "email", "username", "login", "magic_link",
]);

export function scrubUrl(input: string): string {
  const u = new URL(input);
  // Preserve order of surviving query params.
  const kept = new URLSearchParams();
  for (const [k, v] of u.searchParams) {
    if (!DENYLIST.has(k.toLowerCase())) {
      kept.append(k, v);
    }
  }
  const queryStr = kept.toString();
  // Drop fragment entirely.
  const base = `${u.protocol}//${u.host}${u.pathname}`;
  return queryStr ? `${base}?${queryStr}` : base;
}
