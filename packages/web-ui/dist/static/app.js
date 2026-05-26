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

// Exposed for wizard.js's _submitFileUpload, which uses XHR (not fetch) so it
// can surface upload-progress events for multi-GB takeouts. Keeping the token
// resolution in one place avoids the two helpers drifting apart.
function apiToken() {
  return TOKEN;
}

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

    // ID of the plugin currently being configured via the per-plugin setup
    // wizard. Set when the dashboard's Configure button is clicked; the
    // setup-plugin route reads this to know which contract to load.
    setupPluginId: null,
    setupContract: null,
    setupContractError: "",
    setupWizard: null,

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

    // Restart the onboarding flow from the dashboard. Used by the small
    // "Run setup wizard" link in the dashboard header — gives users a way
    // back in if they bailed out mid-flow.
    runOnboarding() {
      this.setupPluginId = null;
      this.setupContract = null;
      this.setupWizard = null;
      this.route = "onboarding";
    },

    // Triggered by the dashboard's Configure button via the @configure-plugin
    // window event. Switches to the setup-plugin route and fetches the
    // contract so the wizard can render.
    async openSetupForPlugin(pluginId) {
      this.setupPluginId = pluginId;
      this.setupContract = null;
      this.setupContractError = "";
      this.setupWizard = null;
      this.route = "setup-plugin";
      try {
        const contract = await api(`/api/plugin/${pluginId}/contract`);
        this.setupContract = contract;
        this.setupWizard = createWizard(
          contract,
          () => { this.route = "dashboard"; },   // on_complete → back to dashboard
          () => { this.route = "dashboard"; },   // on_skip_plugin → back to dashboard
        );
      } catch (e) {
        this.setupContractError = e.message || "Could not load plugin setup.";
      }
    },
  };
}
