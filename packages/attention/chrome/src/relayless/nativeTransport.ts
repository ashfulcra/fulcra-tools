// chrome/src/relayless/nativeTransport.ts
//
// The JS half of the Safari native-messaging bridge (step 7 of
// docs/proposals/2026-06-04-relayless-and-mobile-safari-attention.md).
//
// On iOS there is no localhost daemon and — per the 2026-06-07 addendum — the
// Origin blocker prevents the extension posting to the Fulcra cloud directly.
// So on Safari the NATIVE app owns auth, the token and ingest, and this module
// is how the extension hands events over.
//
// THE CONTRACT IS SHARED WITH Shared (Extension)/NativeBridge.swift. Both sides
// must agree on the keys below. They are the SNAKE_CASE names `AttentionEvent`
// already uses in ../types — deliberately, because it means an outbox entry can
// be handed across as-is with no field mapping, and a mapping layer is exactly
// where a renamed field would go quietly missing.
//
// This module does NOT decide when to flush or what the Safari build looks
// like; it is the transport only. Wiring it into a Safari entry point is a
// separate change (see the note in the proposal — as of writing there is no
// Safari build variant and `startVisibilityCapture` has no production caller).

import type { AttentionEvent } from "../types";

/** Request kinds NativeBridge.swift accepts. */
export const NATIVE_INGEST = "ingest";
export const NATIVE_AUTH_STATE = "auth_state";

/** Reply keys. Mirrors `BridgeKey` in NativeBridge.swift. */
export interface NativeReply {
  ok?: boolean;
  error?: string;
  needs_sign_in?: boolean;
  sent?: number;
  skipped?: number;
  signed_in?: boolean;
}

/** What a flush learned. Mapped to the outbox's existing IngestError kinds by
 * the caller, so this module stays transport-only. */
export type NativeOutcome =
  | { kind: "ok"; sent: number; skipped: number }
  | { kind: "unauthorized"; detail?: string }
  | { kind: "unreachable"; detail?: string };

/** Injectable so tests do not need an extension host. Mirrors
 * `browser.runtime.sendNativeMessage`, which resolves with the reply. */
export type SendNativeMessage = (message: unknown) => Promise<unknown>;

/** A reported count is only usable if it is a finite, non-negative integer.
 * The bridge always emits both; anything else is a reply we do not understand,
 * and an unintelligible success is not a success. */
function isCount(v: unknown): v is number {
  return typeof v === "number" && Number.isInteger(v) && v >= 0;
}

function defaultSend(): SendNativeMessage | null {
  // Safari exposes this on `browser` and on `chrome`. Absent in a plain web
  // page and in Chrome without a registered native host.
  const runtime =
    (globalThis as { browser?: { runtime?: unknown } }).browser?.runtime ??
    (globalThis as { chrome?: { runtime?: unknown } }).chrome?.runtime;
  const fn = (runtime as { sendNativeMessage?: unknown } | undefined)
    ?.sendNativeMessage;
  if (typeof fn !== "function") return null;
  // The application id is ignored by Safari (the containing app is implied) but
  // the signature requires one.
  return (message: unknown) =>
    (fn as (app: string, msg: unknown) => Promise<unknown>)("fulcra", message);
}

/** True when this browser can reach a native containing app at all. */
export function nativeMessagingAvailable(): boolean {
  return defaultSend() !== null;
}

/**
 * Hand a batch to the native app for ingest.
 *
 * Never throws: a transport failure is reported as `unreachable` so the caller
 * RETAINS the outbox and retries, exactly as the cloud path does. Reporting
 * success here would let the caller clear events the native side never
 * accepted — the same class of bug the Swift `sendBatch` was fixed for in
 * PR 601 r2, and it is worth being explicit that both ends of this bridge have
 * now made the same mistake once.
 */
export async function sendBatchViaNative(
  events: AttentionEvent[],
  send: SendNativeMessage | null = defaultSend(),
): Promise<NativeOutcome> {
  if (!send) {
    return { kind: "unreachable", detail: "native messaging is unavailable" };
  }
  // An empty flush is success and must not cost a round trip.
  if (events.length === 0) return { kind: "ok", sent: 0, skipped: 0 };

  let reply: NativeReply;
  try {
    reply = ((await send({
      type: NATIVE_INGEST,
      // Passed through unchanged: these are already the wire shape.
      events,
    })) ?? {}) as NativeReply;
  } catch (e) {
    return { kind: "unreachable", detail: String(e) };
  }

  // needs_sign_in is checked BEFORE ok, and independently of it: it is the one
  // outcome the user can act on, and folding it into a generic failure would
  // have the extension back off silently while the fix is a single tap.
  if (reply.needs_sign_in === true) {
    return { kind: "unauthorized", detail: reply.error };
  }
  if (reply.ok === true) {
    // A success receipt must actually REPORT what happened. `kind: "ok"` is
    // what authorizes the caller to clear the snapshotted events, so a reply
    // that says ok while omitting or garbling its counts must not reach that
    // path: a version-skewed or malformed native reply would otherwise delete
    // data (codex-reviewer, PR 610 r1). Defaulting a missing count to 0 was
    // the specific hole — it turned "the native side told me nothing" into
    // "the native side accepted nothing", which reads as a clean flush.
    //
    // Deliberately NOT checking sent + skipped === events.length: duplicate
    // source ids within one batch are claimed once and join NEITHER count
    // (see RelaylessSender.sendBatch), so a correct reply can legitimately
    // total less than the batch size. A total check would reject good replies
    // and stall the outbox permanently.
    if (isCount(reply.sent) && isCount(reply.skipped)) {
      return { kind: "ok", sent: reply.sent, skipped: reply.skipped };
    }
    return {
      kind: "unreachable",
      detail: `native reply claimed success with unusable counts (sent=${String(reply.sent)}, skipped=${String(reply.skipped)})`,
    };
  }
  // Anything else — ok:false, a malformed reply, or no reply at all — is a
  // failure. A missing `ok` must NOT read as success: an extension host that
  // returns nothing looks identical to one that quietly dropped the batch.
  return { kind: "unreachable", detail: reply.error ?? "no reply from the native app" };
}

/** Ask the native side whether it can see a token, without sending events.
 * Used by the popup to show sign-in state; a failure is reported as
 * not-signed-in rather than thrown, since the UI has nothing better to do. */
export async function nativeAuthState(
  send: SendNativeMessage | null = defaultSend(),
): Promise<boolean> {
  if (!send) return false;
  try {
    const reply = ((await send({ type: NATIVE_AUTH_STATE })) ?? {}) as NativeReply;
    return reply.signed_in === true;
  } catch {
    return false;
  }
}
