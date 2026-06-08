// chrome/src/relayless/sha256.ts
//
// SHA-256 hex digest of a UTF-8 string via the Web Crypto API. Available in
// the extension service worker, in browsers, and in Node 20+ (globalThis
// .crypto.subtle), so no dependency is needed. Used by wire.sourceId to
// derive the deterministic, Python-matching attention source-id.

export async function sha256Hex(input: string): Promise<string> {
  const bytes = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const view = new Uint8Array(digest);
  let hex = "";
  for (let i = 0; i < view.length; i++) {
    hex += view[i].toString(16).padStart(2, "0");
  }
  return hex;
}
