// chrome/src/flushRequest.ts
//
// Bug A3 fix — single-context flush. flushOutbox()'s single-flight guard
// lives at module scope, which is PER-JS-CONTEXT. The service worker and the
// popup/wizard pages each load a SEPARATE instance of outbox.ts, so a flush
// kicked off from a page can run CONCURRENTLY with the SW's alarm-driven
// flush: both read the same outbox snapshot and POST it (the original
// duplicate-annotation storm). The SentSet is the only backstop and its
// cross-context load→has→add is itself racy.
//
// The cure is to flush in EXACTLY ONE context: the service worker. Page
// contexts call requestFlush() instead of flushOutbox(); the SW's
// onMessage handler (see background.ts) runs the real flush in-context,
// where the module-scope guard actually serializes.

const COMPONENT = "flushRequest";
const log = {
  debug: (op: string, msg: string, ctx?: Record<string, unknown>) =>
    console.debug(`[${COMPONENT}] ${op}: ${msg}`, ctx ?? ""),
};

/**
 * Ask the service worker to flush the outbox. Fire-and-forget: we do not
 * await the SW's response or block the caller on the network flush.
 *
 * The send can reject with "Could not establish connection / Receiving end
 * does not exist" when the SW is asleep or mid-restart. That's benign — the
 * periodic FLUSH_ALARM tick will drain the outbox shortly regardless — so we
 * swallow it with a debug log rather than surfacing an error.
 */
export function requestFlush(): void {
  try {
    const p = chrome.runtime.sendMessage({ type: "flushOutbox" });
    // sendMessage returns a Promise in MV3; guard for older/stubbed shapes.
    if (p && typeof (p as Promise<unknown>).catch === "function") {
      (p as Promise<unknown>).catch((e: unknown) => {
        log.debug("send", "flush request not delivered (SW asleep?); alarm will flush", {
          error: String(e),
        });
      });
    }
  } catch (e) {
    // Synchronous throw (e.g. no runtime in a non-extension context).
    log.debug("send", "flush request threw synchronously; alarm will flush", {
      error: String(e),
    });
  }
}
