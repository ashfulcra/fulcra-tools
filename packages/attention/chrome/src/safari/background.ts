// chrome/src/safari/background.ts
//
// Safari background worker: the only thing that may talk to the native app.
//
// Content scripts cannot call runtime.sendNativeMessage, so every completed
// visit arrives here, gets buffered durably, and is flushed to the containing
// app in batches. This worker deliberately imports NONE of the Chrome cloud
// path — no TokenStore, no device flow, no ensureDefinition, no direct POST.
// The native app owns all of it, which is what keeps this bundle's permission
// surface down to storage + nativeMessaging.

import { addSafariEvent, flushSafariOutbox } from "./outbox";
import { nativeAuthState } from "../relayless/nativeTransport";
import { SAFARI_AUTH_QUERY, SAFARI_EVENT_MESSAGE } from "./protocol";
import type { AttentionEvent } from "../types";

/** How often to drain the buffer. Alarms are the only timer a service worker
 * can rely on: the worker is killed aggressively between events, so a
 * setInterval would simply stop. */
const FLUSH_ALARM = "fulcra-attention-flush";
const FLUSH_PERIOD_MINUTES = 5;

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: FLUSH_PERIOD_MINUTES });
});
chrome.runtime.onStartup?.addListener(() => {
  chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: FLUSH_PERIOD_MINUTES });
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === FLUSH_ALARM) void flushSafariOutbox();
});

chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
  const msg = message as { type?: string; event?: AttentionEvent } | null;

  if (msg?.type === SAFARI_EVENT_MESSAGE && msg.event) {
    // Buffer FIRST, flush second. Storing before sending is what makes a
    // failed or interrupted flush non-destructive: the visit is already
    // durable, so the worker dying mid-send costs a retry, not an event.
    void addSafariEvent(msg.event).then(() => flushSafariOutbox());
    // No reply: the content script does not wait for one (its page may be
    // unloading, which is exactly when visits complete).
    return false;
  }

  if (msg?.type === SAFARI_AUTH_QUERY) {
    void nativeAuthState().then((signedIn) => sendResponse({ signedIn }));
    return true; // keep the channel open for the async reply
  }

  return false;
});
