// chrome/src/outbox.ts
//
// Write-ahead queue: every captured event lands here before POST. On 200 we
// delete the entry. On network failure / 5xx we leave it for retry. On 4xx
// we drop it (permanent failure — usually a bug or stale state). Cap at
// OUTBOX_CAP entries, dropping the oldest at overflow.

import { loadOutbox, saveOutbox, loadSettings } from "./storage";
import type { AttentionEvent, OutboxEntry } from "./types";
import { sendBatch } from "./relayless/relaylessSender";
import { SentSet } from "./relayless/sentSet";
import { TokenStore } from "./relayless/tokenStore";
import {
  ensureAttentionDefinitionAndTags,
  UnauthorizedError,
} from "./relayless/ensureDefinition";

export const OUTBOX_CAP = 5000;
// The daemon's extension-events endpoint. Default port matches
// fulcra_collect.config.Config.web_port. Same payload + Authorization:
// Bearer scheme as the now-removed standalone relay.
export const EXTENSION_ENDPOINT_URL = "http://127.0.0.1:9292/api/extension/attention";

/**
 * Surface the most recent ingest issue so the popup + toolbar icon
 * can show it. The popup reads this key; the SW's
 * refreshToolbarIcon() also reads it to flip the icon to "error".
 *
 *   { kind: "unauthorized" } — daemon rejected the bearer token. User
 *     needs to re-pair the extension from the daemon's wizard.
 *   { kind: "unreachable" }  — repeated network failures. Most likely
 *     the daemon isn't running, or a bearer mismatch was mis-classified
 *     before we recognised 401.
 *   null — clear (most-recent POST was a 200).
 */
export interface IngestError {
  kind: "unauthorized" | "unreachable";
  at: number;
}

async function writeIngestError(err: IngestError | null): Promise<void> {
  if (err === null) await chrome.storage.local.remove("lastIngestError");
  else await chrome.storage.local.set({ lastIngestError: err });
}

function genId(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export async function addToOutbox(payload: AttentionEvent): Promise<void> {
  const cur = await loadOutbox();
  const entry: OutboxEntry = {
    id: genId(),
    payload,
    queuedAt: Date.now(),
    attempts: 0,
  };
  cur.push(entry);
  while (cur.length > OUTBOX_CAP) cur.shift();
  await saveOutbox(cur);
}

// Single-flight guard. flushOutbox loads the outbox snapshot at the top and
// only writes the remaining entries at the very end (after every POST). If two
// flushes overlap — e.g. the per-minute FLUSH_ALARM tick and the history
// backfill's fire-and-forget `void flushOutbox()` — both read the SAME snapshot
// and both POST every entry before either clears it, producing duplicate
// ingests (observed: thousands of duplicate annotations per event). We
// serialize by holding the in-flight flush's promise at module scope: a second
// concurrent call awaits and returns that same promise instead of starting an
// overlapping run. The next alarm tick drains whatever the in-flight run left.
//
// A dedicated guard (rather than reusing background.ts's withSwLock) is
// deliberate: flushOutbox touches only the `outbox` storage key, never the
// `visits` map that withSwLock protects, so coupling the two would needlessly
// serialize unrelated navigation handling against network flushes. The guard
// always resets in a finally so a throw can't wedge it permanently.
let inFlightFlush: Promise<void> | null = null;

export function flushOutbox(): Promise<void> {
  if (inFlightFlush) return inFlightFlush;
  const run = doFlushOutbox().finally(() => {
    inFlightFlush = null;
  });
  inFlightFlush = run;
  return run;
}

async function doFlushOutbox(): Promise<void> {
  const settings = await loadSettings();
  if (settings.transportMode === "relayless") {
    await flushRelayless();
    return;
  }
  await flushRelay();
}

// --- relay transport (the v1 localhost-daemon path; behavior unchanged) ----

async function flushRelay(): Promise<void> {
  const settings = await loadSettings();
  if (!settings.bearerToken) return;
  const entries = await loadOutbox();
  if (entries.length === 0) {
    // Nothing to flush. If the user just fixed a token (popup save
    // clears `lastIngestError` directly) we're already clean; if the
    // outbox naturally drained, clear any stale `unreachable` so the
    // banner doesn't keep warning about a state that no longer
    // exists. We leave `unauthorized` alone — that one only clears on
    // explicit token save or a successful POST.
    const r = await chrome.storage.local.get("lastIngestError");
    if (r.lastIngestError && (r.lastIngestError as IngestError).kind === "unreachable") {
      await chrome.storage.local.remove("lastIngestError");
    }
    return;
  }

  const remaining: OutboxEntry[] = [];
  let consecutiveFailures = 0;
  const MAX_CONSECUTIVE_FAILURES = 5;
  const REQUEST_TIMEOUT_MS = 10_000;
  let aborted = false;

  // Track 401 separately. A 401 is "unauthorized" — token mismatch.
  // Treat it as transient (keep the entry) so a re-paste fixes things
  // automatically. The popup surfaces it as a "Reconnect" banner.
  let sawUnauthorized = false;
  let sawSuccess = false;

  for (const entry of entries) {
    // Bail out early if the relay is clearly unreachable. Keep remaining
    // entries in the outbox — the next alarm tick retries.
    if (aborted) {
      remaining.push(entry);
      continue;
    }

    let ok = false;
    let permanentFail = false;
    let unauthorized = false;
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), REQUEST_TIMEOUT_MS);
    try {
      const resp = await fetch(EXTENSION_ENDPOINT_URL, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${settings.bearerToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(entry.payload),
        signal: ac.signal,
      });
      if (resp.status === 200) ok = true;
      else if (resp.status === 401) unauthorized = true;
      else if (resp.status >= 400 && resp.status < 500) permanentFail = true;
    } catch {
      // Network error / abort — keep for retry.
    } finally {
      clearTimeout(timer);
    }

    if (ok) {
      consecutiveFailures = 0;
      sawSuccess = true;
      continue;
    }
    if (unauthorized) {
      // Keep the entry — a re-paste of the token will retry. But mark
      // the run as auth-failing so the popup can surface it.
      remaining.push({ ...entry, attempts: entry.attempts + 1 });
      sawUnauthorized = true;
      consecutiveFailures += 1;
      if (consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) aborted = true;
      continue;
    }
    if (permanentFail) {
      consecutiveFailures = 0;
      continue;  // drop entry — relay said no, in a way that won't fix itself
    }
    // Transient failure — keep and bump attempts.
    remaining.push({ ...entry, attempts: entry.attempts + 1 });
    consecutiveFailures += 1;
    if (consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
      aborted = true;
    }
  }
  await saveOutbox(remaining);

  // Surface the most-recent state. Order: auth error > unreachable > clear.
  if (sawUnauthorized) {
    await writeIngestError({ kind: "unauthorized", at: Date.now() });
  } else if (sawSuccess) {
    await writeIngestError(null);
  } else if (aborted || consecutiveFailures > 0) {
    await writeIngestError({ kind: "unreachable", at: Date.now() });
  } else {
    // Empty pass and no failures — leave whatever state was there alone.
  }
}

// --- relayless transport (direct-to-cloud via the relayless core) ---------
//
// Drains the same outbox the relay path does, but POSTs each event straight
// to Fulcra's /ingest/v1/record/batch using the relayless core: an OIDC
// device-flow access token (TokenStore), the extension-resolved Attention
// {definitionId, tagIds} (ensureAttentionDefinitionAndTags, cached), and the
// core relaylessSender.sendBatch (which builds wire records, de-dups via the
// SentSet, and POSTs the survivors).
//
// lastIngestError semantics are preserved and mapped to the relayless world:
//   - no token / 401            → { kind: "unauthorized" }  (needs sign-in;
//                                  events are RETAINED, never dropped)
//   - network / non-401 failure → { kind: "unreachable" }   (events retained)
//   - success (or nothing to    → clear the error
//     flush, draining a stale
//     "unreachable")
//
// Single-flight is provided by flushOutbox's module-scope guard, shared with
// the relay path, so a relay→relayless toggle can't run two drains at once.
async function flushRelayless(): Promise<void> {
  const entries = await loadOutbox();

  // A token store + a getToken adapter the core sender/ensure use. getToken
  // returns the valid access token (refreshing transparently) or null when
  // not signed in.
  const tokenStore = new TokenStore();
  const getToken = async (opts?: { force?: boolean }): Promise<string | null> => {
    // The relayless TokenStore refreshes on staleness inside
    // getValidAccessToken. A `force` (post-401) refresh is honored straight
    // through: a 401 on a still-"fresh" (but server-revoked) token must
    // trigger a real refresh via the refresh grant, not hand back the same
    // rejected token. sendBatch retries exactly once with force:true.
    return tokenStore.getValidAccessToken({ force: opts?.force });
  };

  // Gate on having a token up front: if not signed in, surface needs-sign-in
  // and retain events. (Mirrors the relay path's "no bearer token → return".)
  let haveToken: boolean;
  try {
    haveToken = (await tokenStore.getValidAccessToken()) != null;
  } catch {
    // Refresh failed (expired refresh token, transport) — treat as
    // needs-sign-in so the user re-authenticates.
    await writeIngestError({ kind: "unauthorized", at: Date.now() });
    return;
  }
  if (!haveToken) {
    await writeIngestError({ kind: "unauthorized", at: Date.now() });
    return;
  }

  if (entries.length === 0) {
    // Nothing to flush. Drain a stale "unreachable" the same way the relay
    // path does; leave "unauthorized" for an explicit sign-in / success.
    const r = await chrome.storage.local.get("lastIngestError");
    if (
      r.lastIngestError &&
      (r.lastIngestError as IngestError).kind === "unreachable"
    ) {
      await chrome.storage.local.remove("lastIngestError");
    }
    return;
  }

  // Ensure (cached) the Attention definition + tags. A 401 here means the
  // token went bad mid-flight → needs-sign-in; any other failure (network,
  // 5xx) → unreachable. Events are retained in both cases.
  let context: { definitionId: string; tagIds: string[] };
  try {
    context = await ensureAttentionDefinitionAndTags({ getToken });
  } catch (e) {
    if (e instanceof UnauthorizedError) {
      await writeIngestError({ kind: "unauthorized", at: Date.now() });
    } else {
      await writeIngestError({ kind: "unreachable", at: Date.now() });
    }
    return;
  }

  // Drain via the core sender. It de-dups against the SentSet and only marks
  // ids sent after a 2xx, so on failure nothing is lost.
  const sentSet = new SentSet();
  const events = entries.map((e) => e.payload);
  const result = await sendBatch(events, {
    getToken,
    context,
    sentSet,
  });

  if (result.ok) {
    // ok means sendBatch processed every snapshotted event — each was either
    // POSTed successfully (now in the SentSet) or skipped because it was
    // already sent. Clear exactly the entries we snapshotted by id, so any
    // event queued concurrently (during the POST) survives. Mirrors the
    // relay path, which only writes back the entries it read.
    const settled = new Set(entries.map((e) => e.id));
    const current = await loadOutbox();
    await saveOutbox(current.filter((e) => !settled.has(e.id)));
    await writeIngestError(null);
    return;
  }

  // Failure: 401 → needs-sign-in; anything else (network=0, 5xx) →
  // unreachable. Events are retained (we did NOT clear the outbox).
  if (result.failureStatus === 401) {
    await writeIngestError({ kind: "unauthorized", at: Date.now() });
  } else {
    await writeIngestError({ kind: "unreachable", at: Date.now() });
  }
}
