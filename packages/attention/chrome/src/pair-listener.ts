// chrome/src/pair-listener.ts
//
// Content script that runs on the local Fulcra Collect daemon URL
// (loopback only — declared in manifest.config.json content_scripts).
//
// One-click pairing flow:
//   1. The wizard page calls window.postMessage with
//      {type: "fulcra-attention-pair", token, daemonUrl}.
//   2. This script validates the message origin (must be loopback), then
//      forwards it to the background service worker via chrome.runtime
//      .sendMessage({type: "pair", ...}).
//   3. On the SW's reply, we postMessage
//      {type: "fulcra-attention-pair-ack", ok: true} back into the page
//      so the wizard can stop spinning and mark the step complete.
//
// Origin gate: only http://127.0.0.1:<port> and http://localhost:<port>
// are accepted. Any other origin is dropped silently — a third-party
// page should never be able to make this extension store a token by
// shoving a postMessage at it.

const LOOPBACK_ORIGIN_RE = /^http:\/\/(127\.0\.0\.1|localhost):\d+$/;

window.addEventListener("message", (event) => {
  // Defence in depth: messages from other windows / iframes can arrive
  // here. We require both:
  //   * The origin is a literal loopback URL with explicit port (matches
  //     the manifest's content_scripts matches).
  //   * The data is the exact pair message shape.
  if (typeof event.origin !== "string") return;
  if (!LOOPBACK_ORIGIN_RE.test(event.origin)) return;

  const data = event.data;
  if (!data || typeof data !== "object") return;
  if (data.type !== "fulcra-attention-pair") return;
  if (typeof data.token !== "string" || data.token.length === 0) return;
  if (typeof data.daemonUrl !== "string" || data.daemonUrl.length === 0) {
    return;
  }

  // Hand off to the background script. The SW does the actual
  // chrome.storage write; the content script can't access
  // chrome.storage.local for settings keys it doesn't own.
  chrome.runtime.sendMessage(
    { type: "pair", token: data.token, daemonUrl: data.daemonUrl },
    (response) => {
      // chrome.runtime.lastError fires if the SW couldn't be reached
      // (extension reloaded, etc.) — drop the ack in that case rather
      // than telling the page "ok" when nothing was stored.
      if (chrome.runtime.lastError) return;
      if (!response || response.ok !== true) return;
      window.postMessage(
        { type: "fulcra-attention-pair-ack", ok: true },
        event.origin,
      );
    },
  );
});
