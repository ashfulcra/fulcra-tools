// chrome/src/wizard/history.ts
//
// History fetch + grouping + category-bulk-exclusion helpers used by
// the onboarding wizard. Kept separate from the React UI so it can be
// unit-tested against the chrome.history stub.

import { scrubUrl } from "../scrub";

/**
 * One row in the wizard's per-domain table.
 * `urls` is kept so the ingest step can replay individual visits.
 */
export interface DomainGroup {
  host: string;
  count: number;
  /** Sorted by lastVisitTime desc — most recent first. */
  urls: ScannedUrl[];
}

export interface ScannedUrl {
  url: string;          // post-scrub
  title: string | null;
  lastVisitTime: number; // ms epoch (chrome.history.HistoryItem.lastVisitTime)
  visitCount: number;
}

/**
 * Bulk-exclusion preset. Selecting a preset adds every pattern to
 * the Tier 3 ignore list. The patterns use the same `*.example.com`
 * wildcard semantics as the existing ignore module.
 */
export interface ExclusionPreset {
  id: string;
  label: string;
  description: string;
  patterns: string[];
}

/**
 * Categories of sites likely to be sensitive enough that the user
 * wants them excluded from "what did I read?" history. These map to
 * the same vocabulary as Tier 2 categories but the wizard offers
 * exclusion presets that bulk-add common domains to the ignore list.
 *
 * Conservative defaults — well-known consumer domains only.
 */
export const EXCLUSION_PRESETS: ExclusionPreset[] = [
  {
    id: "banking",
    label: "Banking & finance",
    description: "Major US banks + brokerages. Add yours by hand if missing.",
    patterns: [
      "chase.com", "*.chase.com",
      "bankofamerica.com", "*.bankofamerica.com",
      "wellsfargo.com", "*.wellsfargo.com",
      "citi.com", "*.citi.com",
      "capitalone.com", "*.capitalone.com",
      "schwab.com", "*.schwab.com",
      "fidelity.com", "*.fidelity.com",
      "vanguard.com", "*.vanguard.com",
      "robinhood.com", "*.robinhood.com",
      "etrade.com", "*.etrade.com",
      "ally.com", "*.ally.com",
      "sofi.com", "*.sofi.com",
    ],
  },
  {
    id: "healthcare",
    label: "Healthcare & insurance",
    description: "Patient portals, common US health insurers.",
    patterns: [
      "*.mychart.com",
      "epic.com", "*.epic.com",
      "kaiserpermanente.org", "*.kaiserpermanente.org",
      "anthem.com", "*.anthem.com",
      "cigna.com", "*.cigna.com",
      "aetna.com", "*.aetna.com",
      "unitedhealthcare.com", "*.unitedhealthcare.com",
      "humana.com", "*.humana.com",
      "bcbs.com", "*.bcbs.com",
      "healthcare.gov",
    ],
  },
  {
    id: "crypto",
    label: "Crypto exchanges",
    description: "Major centralized exchanges. Tracks balances, not just navigation.",
    patterns: [
      "coinbase.com", "*.coinbase.com",
      "kraken.com", "*.kraken.com",
      "binance.com", "*.binance.com",
      "binance.us", "*.binance.us",
      "gemini.com", "*.gemini.com",
      "blockchain.com", "*.blockchain.com",
    ],
  },
  {
    id: "adult",
    label: "Adult / NSFW",
    description: "Common adult-content sites. Add yours by hand if missing.",
    patterns: [
      "pornhub.com", "*.pornhub.com",
      "xvideos.com", "*.xvideos.com",
      "xhamster.com", "*.xhamster.com",
      "redtube.com", "*.redtube.com",
      "onlyfans.com", "*.onlyfans.com",
    ],
  },
  {
    id: "dating",
    label: "Dating",
    description: "Mainstream dating apps' web interfaces.",
    patterns: [
      "tinder.com", "*.tinder.com",
      "bumble.com", "*.bumble.com",
      "hinge.co", "*.hinge.co",
      "match.com", "*.match.com",
      "okcupid.com", "*.okcupid.com",
    ],
  },
  {
    id: "auth",
    label: "Auth / SSO",
    description: "Identity providers — usually transient redirects, not pages worth logging.",
    patterns: [
      "accounts.google.com",
      "login.microsoftonline.com",
      "*.auth0.com",
      "*.okta.com",
      "appleid.apple.com",
      "github.com/login",
    ],
  },
];

/**
 * Fetch up to `maxResults` history items going back `daysBack` days,
 * group by host, scrub URLs, return sorted by visit count descending.
 *
 * `chrome.history.search` returns at most ~5000 items per query, sorted
 * by `lastVisitTime` desc. We fetch in one call and group locally.
 */
export async function fetchAndGroupHistory(opts: {
  daysBack: number;
  maxResults: number;
}): Promise<DomainGroup[]> {
  const startTime = Date.now() - opts.daysBack * 24 * 60 * 60 * 1000;
  const items = await chrome.history.search({
    text: "",
    startTime,
    maxResults: opts.maxResults,
  });
  return groupByHost(items);
}

/**
 * Pure grouping function — separated so tests can pass in a fixture
 * without going through chrome.history.search.
 */
export function groupByHost(items: chrome.history.HistoryItem[]): DomainGroup[] {
  const byHost = new Map<string, DomainGroup>();
  for (const item of items) {
    if (!item.url) continue;
    if (!item.url.startsWith("http://") && !item.url.startsWith("https://")) continue;
    let scrubbed: string;
    let host: string;
    try {
      scrubbed = scrubUrl(item.url);
      host = new URL(scrubbed).hostname;
    } catch {
      continue;
    }
    if (host === "") continue;
    const existing = byHost.get(host);
    const row: ScannedUrl = {
      url: scrubbed,
      title: item.title?.trim() || null,
      lastVisitTime: item.lastVisitTime ?? 0,
      visitCount: item.visitCount ?? 1,
    };
    if (existing) {
      existing.count += row.visitCount;
      existing.urls.push(row);
    } else {
      byHost.set(host, { host, count: row.visitCount, urls: [row] });
    }
  }
  const groups = Array.from(byHost.values());
  for (const g of groups) {
    g.urls.sort((a, b) => b.lastVisitTime - a.lastVisitTime);
  }
  groups.sort((a, b) => b.count - a.count);
  return groups;
}

/**
 * Test whether a host is covered by any existing or selected ignore
 * pattern. Same wildcard semantics as ignore.ts but kept inline so the
 * wizard doesn't need to round-trip through storage during edits.
 */
export function matchesAnyPattern(host: string, patterns: string[]): boolean {
  for (const p of patterns) {
    if (p.startsWith("*.")) {
      const tail = p.slice(2);
      if (host === tail || host.endsWith("." + tail)) return true;
    } else {
      if (host === p) return true;
    }
  }
  return false;
}

/**
 * Given a set of selected preset IDs + manually-toggled host
 * exclusions, return the merged ignore-pattern list to persist.
 */
export function buildIgnoreList(
  selectedPresetIds: string[],
  selectedHosts: string[],
  existingPatterns: string[],
): string[] {
  const out = new Set(existingPatterns);
  for (const id of selectedPresetIds) {
    const preset = EXCLUSION_PRESETS.find((p) => p.id === id);
    if (!preset) continue;
    for (const pat of preset.patterns) out.add(pat);
  }
  for (const h of selectedHosts) out.add(h);
  return Array.from(out).sort();
}
