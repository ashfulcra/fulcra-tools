// chrome/src/relayless/authOriginFix.ts
//
// Workaround for an Auth0 "Allowed Web Origins" rejection that breaks the
// relayless device-flow sign-in.
//
// Auth0's `/oauth/device/code` and `/oauth/token` endpoints reject any
// request whose `Origin` header is not in the application's Allowed Web
// Origins list. When the extension's auth fetches (src/relayless/oidc.ts)
// run in a browser context — popup, wizard, OR the MV3 service worker —
// Chrome forcibly attaches `Origin: chrome-extension://<id>`, which Auth0
// has no way to allow-list, so it returns HTTP 403:
//
//   {"error":"access_denied","error_description":
//    "Origin chrome-extension://… is not allowed. Behavior used for
//     check: WEB ORIGINS"}
//
// The same request with no Origin header succeeds (HTTP 200). The standard
// MV3 fix is a dynamic declarativeNetRequest rule that strips the Origin
// header from the extension's own POSTs to the Auth0 host. We scope it as
// tightly as possible (host + initiator + method) so it can never touch
// any other request. The Fulcra API host does NOT do this Origin check, so
// only the Auth0 host is targeted.
//
// Registration is idempotent: we always remove our own rule id before
// re-adding, so calling this at both SW startup and onInstalled is safe.

/** Stable id for our dynamic Origin-strip rule. Reused for removeRuleIds. */
export const AUTH0_ORIGIN_STRIP_RULE_ID = 1001;

const AUTH0_HOST = "fulcra.us.auth0.com";

// Lightweight component-scoped structured logger. The codebase has no
// shared logger module; we keep the same level/context shape the repo's
// debugging-instrumentation rule expects so failures are surfaced, never
// swallowed silently.
const COMPONENT = "relayless/authOriginFix";
const log = {
  info: (op: string, msg: string, ctx?: Record<string, unknown>) =>
    console.info(`[${COMPONENT}] ${op}: ${msg}`, ctx ?? ""),
  error: (op: string, msg: string, ctx?: Record<string, unknown>) =>
    console.error(`[${COMPONENT}] ${op}: ${msg}`, ctx ?? ""),
};

/**
 * Register (or refresh) the dynamic declarativeNetRequest rule that removes
 * the `Origin` request header from the extension's own POSTs to the Auth0
 * host. Idempotent and crash-safe: any failure is logged, never thrown, so
 * a registration error can't take down the service worker.
 *
 * `dnr` and `extensionId` are injectable so tests never touch real Chrome.
 */
export async function registerAuth0OriginStrip(
  dnr: typeof chrome.declarativeNetRequest = chrome.declarativeNetRequest,
  extensionId: string = chrome.runtime.id,
): Promise<void> {
  const rule: chrome.declarativeNetRequest.Rule = {
    id: AUTH0_ORIGIN_STRIP_RULE_ID,
    priority: 1,
    action: {
      type: "modifyHeaders" as chrome.declarativeNetRequest.RuleActionType,
      requestHeaders: [
        {
          header: "origin",
          operation:
            "remove" as chrome.declarativeNetRequest.HeaderOperation,
        },
      ],
    },
    condition: {
      requestDomains: [AUTH0_HOST],
      initiatorDomains: [extensionId],
      requestMethods: [
        "post" as chrome.declarativeNetRequest.RequestMethod,
      ],
      resourceTypes: [
        "xmlhttprequest" as chrome.declarativeNetRequest.ResourceType,
        "other" as chrome.declarativeNetRequest.ResourceType,
      ],
    },
  };

  try {
    await dnr.updateDynamicRules({
      removeRuleIds: [AUTH0_ORIGIN_STRIP_RULE_ID],
      addRules: [rule],
    });
    log.info("register", "registered Auth0 Origin-strip DNR rule", {
      ruleId: AUTH0_ORIGIN_STRIP_RULE_ID,
      host: AUTH0_HOST,
      extensionId,
    });
  } catch (err) {
    log.error("register", "failed to register Auth0 Origin-strip DNR rule", {
      ruleId: AUTH0_ORIGIN_STRIP_RULE_ID,
      error: String(err),
    });
  }
}
