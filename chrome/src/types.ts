// chrome/src/types.ts
// Shared types. AttentionEvent matches Plan A's wire format byte-for-byte.

export const CLIENT = "fulcra-attention-chrome/0.1.0";

/**
 * The POST body sent to http://127.0.0.1:8771/attention.
 * Exactly one of {url, category} must be non-null.
 * start_time <= end_time <= now + 5min (enforced server-side).
 */
export interface AttentionEvent {
  url: string | null;
  title: string | null;
  og_description: string | null;
  favicon_url: string | null;
  category: string | null;
  chrome_identity: string | null;
  og_type: string | null;
  lang: string | null;
  start_time: string;  // ISO 8601 with trailing 'Z'
  end_time: string;
  client: string;      // CLIENT constant
}

/** Persistent settings in chrome.storage.local. */
export interface Settings {
  bearerToken: string | null;
  relayPort: number;       // default 8771
  enabled: boolean;        // master kill switch
  identityLabel: string | null;  // user override; null means use chrome.identity.getProfileUserInfo
}

export const DEFAULT_SETTINGS: Settings = {
  bearerToken: null,
  relayPort: 8771,
  enabled: true,
  identityLabel: null,
};

/** One entry in the Tier 3 ignore list (chrome.storage.sync). */
export interface IgnoreEntry {
  pattern: string;  // exact host like "example.com" or wildcard like "*.example.com"
  addedAt: string;  // ISO timestamp; informational
}

/** One Tier 2 mapping (chrome.storage.local). */
export interface CategoryMapping {
  pattern: string;     // same wildcard semantics as IgnoreEntry
  category: string;    // slug from the v1 vocabulary
}

/** An event queued for POST in chrome.storage.local. */
export interface OutboxEntry {
  id: string;          // sha1 of payload + nonce; used for dedup
  payload: AttentionEvent;
  queuedAt: number;    // Date.now()
  attempts: number;
}

/** Active visit being timed in chrome.storage.session. Keyed by tabId. */
export interface ActiveVisit {
  tabId: number;
  scrubbedUrl: string;
  startTime: number;   // Date.now()
}

/** Daily counters in chrome.storage.local for popup display. */
export interface Counts {
  date: string;        // YYYY-MM-DD
  logged: number;
  categorized: number;
  ignored: number;
}
