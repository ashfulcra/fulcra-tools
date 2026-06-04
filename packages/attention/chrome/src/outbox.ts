// chrome/src/outbox.ts
//
// Write-ahead queue: every captured event lands here before POST. On 200 we
// delete the entry. On network failure / 5xx we leave it for retry. On 4xx
// we drop it (permanent failure — usually a bug or stale state). Cap at
// OUTBOX_CAP entries, dropping the oldest at overflow.

import { loadOutbox, saveOutbox } from "./storage";
import type { AttentionEvent, OutboxEntry } from "./types";
import { sendBatch } from "./relayless/relaylessSender";
import { SentSet } from "./relayless/sentSet";
import { TokenStore } from "./relayless/tokenStore";
import {
  ensureAttentionDefinitionAndTags,
  UnauthorizedError,
} from "./relayless/ensureDefinition";

export const OUTBOX_CAP = 5000;

/**
 * Surface the most recent ingest issue so the popup + toolbar icon
 * can show it. The popup reads this key; the SW's
 * refreshToolbarIcon() also reads it to flip the icon to "error".
 *
 *   { kind: "unauthorized" } — the cloud rejected the access token (401)
 *     or we have no token. User needs to sign in.
 *   { kind: "unreachable" }  — repeated network / non-401 failures
 *     reaching Fulcra Cloud.
 *   null — clear (most-recent POST succeeded).
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

// Drains the outbox by POSTing each event straight to Fulcra's
// /ingest/v1/record/batch using the relayless core: an OIDC device-flow
// access token (TokenStore), the extension-resolved Attention
// {definitionId, tagIds} (ensureAttentionDefinitionAndTags, cached), and the
// core relaylessSender.sendBatch (which builds wire records, de-dups via the
// SentSet, and POSTs the survivors).
//
// lastIngestError semantics:
//   - no token / 401            → { kind: "unauthorized" }  (needs sign-in;
//                                  events are RETAINED, never dropped)
//   - network / non-401 failure → { kind: "unreachable" }   (events retained)
//   - success (or nothing to    → clear the error
//     flush, draining a stale
//     "unreachable")
//
// Single-flight is provided by flushOutbox's module-scope guard, so the
// per-minute alarm and the backfill's fire-and-forget flush can't run two
// drains at once.
async function doFlushOutbox(): Promise<void> {
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
  // and retain events (never dropped — they ship once the user signs in).
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
    // Nothing to flush. Drain a stale "unreachable"; leave "unauthorized"
    // for an explicit sign-in / success.
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
    // event queued concurrently (during the POST) survives.
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
