// chrome/src/scrub.ts
//
// Tier 1 URL scrubbing — TypeScript port of fulcra_attention/scrub.py.
// CROSS-LANGUAGE CONTRACT: this must produce byte-identical output to the
// Python sibling for every entry in tests/fixtures/scrub_cases.json.

export const DENYLIST: ReadonlySet<string> = new Set([
  // auth-bearing
  "access_token", "id_token", "refresh_token", "code", "state", "nonce",
  "client_secret", "assertion", "session", "sid", "sessionid", "auth",
  "authorization", "token", "apikey", "api_key", "key", "signature",
  "sig", "hmac", "x-amz-signature", "x-amz-credential",
  "x-amz-security-token", "expires", "password", "pwd", "pw", "otp",
  "magic", "share_token", "invite", "confirmation_token",
  "_csrf", "csrf_token", "xsrf", "ticket", "ott",
  // tracking
  "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
  "gclid", "fbclid", "msclkid", "mc_eid", "mc_cid", "_hsenc", "_hsmi",
  "igshid", "yclid", "ref", "ref_src", "ref_url",
  // one-click action
  "unsubscribe", "unsub", "verify", "reset", "confirm", "activate",
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
