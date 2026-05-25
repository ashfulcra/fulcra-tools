"use strict";

/**
 * app.js — root Alpine.js component
 *
 * Bootstraps by reading /api/status and /api/fulcra/auth/status then
 * decides whether to route to onboarding or the dashboard.
 *
 * Routes:
 *   loading      — initial fetch
 *   error        — daemon unreachable
 *   onboarding   — first-launch / no auth / no plugins enabled
 *   dashboard    — normal post-onboarding home
 *   add_plugin   — re-enter plugin picker from dashboard
 */

const TOKEN = document.cookie
  .split("; ")
  .find(r => r.startsWith("fulcra_token="))
  ?.split("=")[1];

async function api(path, opts = {}) {
  const headers = {
    Authorization: `Bearer ${TOKEN}`,
    ...((opts.headers) ?? {}),
  };
  // Only set Content-Type to JSON if we have a body and it is not FormData
  if (opts.body && typeof opts.body === "string") {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, { ...opts, headers });
  if (!res.ok) {
    // Try to surface the API's "detail" field (FastAPI convention) so
    // user-facing error messages like "Fulcra rejected the token" reach
    // the UI rather than the raw HTTP status line.
    let detail = "";
    try {
      const body = await res.clone().json();
      detail = body.detail || body.error || "";
    } catch (_) {
      // non-JSON body — fall through to status line
    }
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

function app() {
  return {
    route: "loading",
    errorMessage: "",

    async boot() {
      try {
        const [status, authStatus] = await Promise.all([
          api("/api/status"),
          api("/api/fulcra/auth/status").catch(() => ({ authenticated: false })),
        ]);

        const anyEnabled = (status.plugins ?? []).some(p => p.enabled);
        const signedIn = authStatus.authenticated === true;

        if (signedIn && anyEnabled) {
          this.route = "dashboard";
        } else {
          this.route = "onboarding";
        }
      } catch (e) {
        this.route = "error";
        this.errorMessage = e.message;
      }
    },
  };
}
