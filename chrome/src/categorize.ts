// chrome/src/categorize.ts
//
// Tier 2 — user-controlled category mapping. Replaces the URL/title with
// a category slug at ingest time. Empty by default. Stored in
// chrome.storage.local (per machine).

import { loadCategoryMap } from "./storage";
import { matchesPattern } from "./ignore";

export async function categorize(url: string): Promise<string | null> {
  let host: string;
  try {
    host = new URL(url).host;
  } catch {
    return null;
  }
  const map = await loadCategoryMap();
  for (const m of map) {
    if (matchesPattern(host, m.pattern)) return m.category;
  }
  return null;
}
