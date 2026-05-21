// chrome/src/ignore.ts
//
// Tier 3 — user-managed ignore list. Drops the event entirely. Persisted
// in chrome.storage.sync so it propagates across Chrome profiles via the
// user's Google sync.

import { loadIgnoreList } from "./storage";

/**
 * Match a host against a pattern. Supports:
 *   - Exact match: "example.com" matches only "example.com".
 *   - Wildcard subdomain: "*.example.com" matches "x.example.com" but
 *     NOT "example.com" itself (user can add both).
 */
export function matchesPattern(host: string, pattern: string): boolean {
  if (!pattern.startsWith("*.")) {
    return host === pattern;
  }
  const suffix = pattern.slice(1);  // ".example.com"
  return host.endsWith(suffix) && host !== suffix.slice(1);
}

export async function isIgnored(url: string): Promise<boolean> {
  let host: string;
  try {
    host = new URL(url).host;
  } catch {
    return false;
  }
  const list = await loadIgnoreList();
  return list.some((e) => matchesPattern(host, e.pattern));
}
