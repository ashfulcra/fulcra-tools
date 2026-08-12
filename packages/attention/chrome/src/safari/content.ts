// chrome/src/safari/content.ts
//
// Safari content script: the ONLY production caller of startVisibilityCapture.
//
// It runs in the page because that is the only place `document.visibilityState`
// and the pageshow/pagehide lifecycle are observable. It does NOT talk to the
// native app: content scripts cannot call runtime.sendNativeMessage, so every
// completed visit is forwarded to the background worker, which owns the bridge.
//
// Kept deliberately thin — no batching, no storage, no retry. A content script
// dies with its page at arbitrary moments, so anything durable belongs in the
// background worker. What this file must not do is lose an event quietly, hence
// the failure is logged rather than swallowed.

import { startVisibilityCapture } from "../capture/visibility";
import { SAFARI_EVENT_MESSAGE } from "./protocol";

startVisibilityCapture({
  emit: (event) => {
    // Fire-and-forget by necessity: the page may be tearing down (pagehide is
    // exactly when visits complete), so awaiting a reply is not reliable.
    // Delivery is best-effort HERE and durable in the background worker.
    try {
      void chrome.runtime
        .sendMessage({ type: SAFARI_EVENT_MESSAGE, event })
        ?.catch?.((e: unknown) => {
          console.debug("[fulcra-attention-safari] event not delivered", e);
        });
    } catch (e) {
      // sendMessage throws synchronously when the extension context is gone
      // (update / disable / reload). Nothing to retry against, but say so
      // rather than failing silently.
      console.debug("[fulcra-attention-safari] extension context unavailable", e);
    }
  },
});
