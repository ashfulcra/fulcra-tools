// chrome/public/heartbeat.js
//
// Tiny content script — runs on every web page IFF the user opted into
// "sharper AFK detection" in onboarding (or via the popup toggle).
//
// WHAT IT READS:    nothing. No page content, no DOM queries, no form
//                   values, no selected text, no URLs, no headers. Only
//                   input event TYPES trigger a send, and the send
//                   carries no page data.
// WHAT IT SENDS:    {kind: "heartbeat", t: Date.now()}, debounced to
//                   at most one message every HEARTBEAT_DEBOUNCE_MS.
// WHY THIS EXISTS:  chrome.idle only sees OS-level keyboard / mouse /
//                   screen-lock. A user reading-without-clicking isn't
//                   AFK, but chrome.idle can't tell. The service worker
//                   upgrades this into the AFK signal via
//                   sweepStaleBlurred + HEARTBEAT_STALE_MS.
//
// Plain JS (not TS) on purpose: Vite ships /public verbatim, so the
// path we hand to chrome.scripting.registerContentScripts resolves to
// a file that actually exists in the built extension.

(function () {
  const HEARTBEAT_DEBOUNCE_MS = 5000;
  let lastSent = 0;

  function ping() {
    const now = Date.now();
    if (now - lastSent < HEARTBEAT_DEBOUNCE_MS) return;
    lastSent = now;
    // chrome.runtime.sendMessage returns a promise in MV3. try/catch
    // only catches sync throws; the returned promise rejects when the
    // SW is being torn down (or when the message goes to nobody after
    // unregister) and we DO have to silence those — otherwise Chrome
    // logs an unhandled-rejection on every navigation.
    try {
      const p = chrome.runtime.sendMessage({ kind: "heartbeat", t: now });
      if (p && typeof p.catch === "function") p.catch(() => undefined);
    } catch {
      // Sync throw path (very old Chromes / unusual states) — ignore.
    }
  }

  const opts = { passive: true, capture: true };
  window.addEventListener("mousemove", ping, opts);
  window.addEventListener("scroll", ping, opts);
  window.addEventListener("keydown", ping, opts);
  window.addEventListener("click", ping, opts);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") ping();
  }, opts);

  // Fire one heartbeat at load so the SW knows we're alive.
  ping();
})();
