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
  onboarded: boolean;            // true once the wizard finished (any step beyond Welcome)
}

export const DEFAULT_SETTINGS: Settings = {
  bearerToken: null,
  relayPort: 8771,
  enabled: true,
  identityLabel: null,
  onboarded: false,
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

/**
 * A visit attached to a specific tab. At most one visit at a time is in
 * `focused` state (the active tab of the focused window with the user
 * not-idle). Background tabs that were never focused never get a visit.
 *
 * `state="focused"`  — the user is actively looking at this tab. While
 *                      focused, accumulatedFocusMs is NOT updated; the
 *                      current focus period contributes
 *                      (now - focusEpoch) at emit time.
 * `state="blurred"`  — the user has tab-hopped, blurred the window, or
 *                      gone idle. accumulatedFocusMs has been updated to
 *                      include the just-ended focus period. blurredAt
 *                      records when the blur happened; if the user
 *                      returns within BLUR_GRACE_MS, the visit resumes
 *                      (not a new visit). Past the grace window, the
 *                      visit is emitted and disappears.
 *
 * Both states live in `chrome.storage.session.visits`, a single map
 * keyed by tabId. A non-existent entry means "no visit on this tab".
 */
export interface Visit {
  tabId: number;
  windowId: number;
  url: string;             // pre-scrub (used for ignore re-check on resume)
  scrubbedUrl: string;
  category: string | null; // resolved at visit-start so re-categorisation mid-visit doesn't flip
  startTime: number;       // ms epoch — visit start (first focus). Used as `start_time` in payload.
  state: "focused" | "blurred";
  focusEpoch: number;      // ms epoch — when the CURRENT focus period started (only meaningful while focused)
  accumulatedFocusMs: number; // total focused time from PRIOR focus periods (excludes current)
  blurredAt: number | null;   // ms epoch — only set when state="blurred"
}

/** @deprecated kept as alias for tests / external code; use Visit. */
export type ActiveVisit = Visit;

/** How long after blur a visit can be resumed instead of starting a new one. */
export const BLUR_GRACE_MS = 30_000;

/** Daily counters in chrome.storage.local for popup display. */
export interface Counts {
  date: string;        // YYYY-MM-DD
  logged: number;
  categorized: number;
  ignored: number;
}
