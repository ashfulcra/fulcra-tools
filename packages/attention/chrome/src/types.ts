// chrome/src/types.ts
// Shared types. AttentionEvent matches Plan A's wire format byte-for-byte.

export const CLIENT = "fulcra-attention-chrome/0.1.0";

/**
 * The POST body sent to http://127.0.0.1:9292/api/extension/attention
 * (the fulcra-collect daemon's extension-events route, formerly a
 * standalone relay on port 8771).
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

/**
 * Which transport the outbox flush uses.
 *   "relay"     — POST to the localhost fulcra-collect daemon (the v1
 *                 behavior). The daemon ensures the Attention definition +
 *                 tags and forwards to the cloud.
 *   "relayless" — POST directly to the Fulcra cloud ingest endpoint using
 *                 the relayless core (OIDC device-flow token + the
 *                 extension-side definition/tag ensuring). No daemon needed.
 */
export type TransportMode = "relay" | "relayless";

/** Persistent settings in chrome.storage.local. */
export interface Settings {
  bearerToken: string | null;
  relayPort: number;       // default 9292 (daemon's stable port)
  enabled: boolean;        // master kill switch
  identityLabel: string | null;  // user override; null means use chrome.identity.getProfileUserInfo
  onboarded: boolean;            // true once the wizard finished (any step beyond Welcome)
  pausedUntil: number | null;    // ms epoch — when null, not paused. Past = auto-resumed (lazy)
  heartbeatEnabled: boolean;     // opt-in content-script AFK watchdog (requires <all_urls>)
  transportMode: TransportMode;  // default "relayless" (no daemon — device-flow sign-in); switch to "relay" for the local Collect app
}

export const DEFAULT_SETTINGS: Settings = {
  bearerToken: null,
  relayPort: 9292,
  enabled: true,
  identityLabel: null,
  onboarded: false,
  pausedUntil: null,
  heartbeatEnabled: false,
  transportMode: "relayless",
};

/** How stale a focused visit's lastHeartbeat can be before the heartbeat
 * sweep counts it as AFK. Only applies when settings.heartbeatEnabled. */
export const HEARTBEAT_STALE_MS = 30_000;

/**
 * A completed history-backfill run. Stored in chrome.storage.sync so it
 * propagates to other machines on the same Chrome profile — a second
 * machine's wizard reads this to warn before backfilling the same
 * (synced) history again and creating duplicate events.
 */
export interface BackfillRun {
  machineId: string;  // random per-machine id (see getMachineId)
  at: string;         // ISO timestamp the backfill completed
}

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
  /** Last content-script heartbeat received for this tab (ms epoch). Null
   * means: heartbeat is disabled, or no heartbeat has been received yet
   * since this visit opened. Only used when Settings.heartbeatEnabled. */
  lastHeartbeat: number | null;
}

/** How long after blur a visit can be resumed instead of starting a new one. */
export const BLUR_GRACE_MS = 30_000;

/** Daily counters in chrome.storage.local for popup display. */
export interface Counts {
  date: string;        // YYYY-MM-DD
  logged: number;
  categorized: number;
  ignored: number;
}
