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
 *   onboarding   — first-launch / no auth / no plugins enabled; also used by
 *                  "+ Add plugin" (which sets window.__fulcraOnboardingEntryPhase
 *                  so onboarding.boot() skips to pick_plugins) and by
 *                  "Run setup wizard" (which goes through the full welcome flow)
 *   dashboard    — normal post-onboarding home
 *   setup-plugin — per-plugin reconfiguration wizard (opened by Configure button)
 *   settings     — Preferences > Annotation tracks (soft-delete UI for
 *                  Fulcra annotation definitions; task #42)
 *   docs         — in-app markdown viewer. Currently used for the
 *                  "Data sources" reference (docs/how-do-i-get-my-data.md);
 *                  any docs/<name>.md the daemon serves works the same way.
 *                  The repo is private so a github link would 404 — instead
 *                  the daemon hosts the markdown at /api/docs/<name> and
 *                  the client renders it with marked.
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

        // URL-param routing with auth-aware stash. Menubar deep-links
        // here via URLs like:
        //   /?route=docs                — opens the in-app docs view
        //   /?route=configure&plugin=X  — opens the wizard for plugin X
        //   /?route=settings            — opens the Settings page
        //
        // Why the stash: the URL-param handler used to be gated by
        // `if (signedIn)`, but the params got cleared by replaceState
        // BEFORE auth completed, so an unauthed user clicking a deep-
        // link landed on the default route post-signin instead of the
        // intended destination. Now: read params from the URL on every
        // boot, stash to sessionStorage if not yet signed in, and
        // consume the stash on a subsequent signed-in boot(). Either
        // way we clear the URL via replaceState so reloads don't replay
        // (the stash is the only persistence layer).
        //
        // Stash key prefixed `fulcra:pending-route:` to avoid collision
        // with any sessionStorage users in the future.
        //
        // Added in SP4 (drift audit 2026-05-27); stash behaviour landed
        // as a SP4 follow-up.
        const STASH_KEY = "fulcra:pending-route";

        const urlParams = new URLSearchParams(window.location.search);
        const liveRoute = urlParams.get("route");
        if (liveRoute) {
          // Stash the FULL query string so the stash replay has the
          // same shape as a live URL-param read (and can be parsed by
          // a fresh URLSearchParams).
          try {
            sessionStorage.setItem(STASH_KEY, window.location.search);
          } catch (_) {
            // sessionStorage can throw in private-browsing edge cases;
            // best-effort fallback is to consume now or lose it.
          }
          history.replaceState({}, "", window.location.pathname);
        }

        // Pick the effective params: either the live ones, or any stash
        // from a prior unauth visit. Stash is consumed on first read by
        // any signed-in caller.
        let effectiveParams = urlParams;
        if (signedIn && !liveRoute) {
          try {
            const stashed = sessionStorage.getItem(STASH_KEY);
            if (stashed) {
              effectiveParams = new URLSearchParams(stashed);
              sessionStorage.removeItem(STASH_KEY);
            }
          } catch (_) {
            // ignore; effectiveParams stays as the live (empty) urlParams.
          }
        }

        const requestedRoute = effectiveParams.get("route");
        if (signedIn && requestedRoute) {
          if (requestedRoute === "docs") {
            // Default docs page; downstream tabs can navigate further.
            const docPage = effectiveParams.get("page") || "how-do-i-get-my-data";
            await this.goToDocs(docPage, "");
            return;
          }
          if (requestedRoute === "configure") {
            const pluginId = effectiveParams.get("plugin");
            if (pluginId) {
              await this.openSetupForPlugin(pluginId);
              return;
            }
          }
          if (requestedRoute === "settings") {
            this.route = "settings";
            return;
          }
        }
      } catch (e) {
        this.route = "error";
        this.errorMessage = e.message;
      }
    },

    // Restart the onboarding flow from the dashboard. Used by the small
    // "Run setup wizard" link in the dashboard header — gives users a way
    // back in if they bailed out mid-flow.
    // Clears the window flag so onboarding.boot() runs its full logic:
    // welcome → signin → collect_modes → pick_plugins. (boot() skips
    // collect_modes when the daemon already has a stored token, since
    // that user has already seen the explainer at least once.)
    runOnboarding() {
      this.setupPluginId = null;
      this.setupContract = null;
      this.setupWizard = null;
      window.__fulcraOnboardingEntryPhase = null;
      this.route = "onboarding";
    },

    // Jump straight to the plugin picker inside the onboarding flow. Used by
    // the "+ Add plugin" button on the dashboard — the user is already signed
    // in so there's no reason to show welcome or signin again.
    // Sets the window flag before flipping the route so onboarding.boot()
    // picks it up on mount and skips straight to pick_plugins.
    addPlugin() {
      window.__fulcraOnboardingEntryPhase = "pick_plugins";
      this.route = "onboarding";
    },

    // Jump to the Settings page (currently a single screen: Annotation
    // tracks / soft-delete UI). Triggered by the small Settings link in
    // the dashboard header via the @go-to-settings.window listener on
    // <body>. Mirrors the route-flip in runOnboarding / addPlugin —
    // no setup wizard state to reset because settings is read-only of
    // the daemon and writes go straight to Fulcra.
    goToSettings() {
      this.route = "settings";
    },

    // ---------------------------------------------------------------------
    // Docs viewer — fetches docs/<name>.md from the daemon and renders it
    // in-app via marked. Triggered by @go-to-docs.window with detail.name.
    // We fetch as raw text (NOT api() which expects JSON) and let the
    // template render marked.parse() over it. Errors flip to docsError
    // instead of changing route so users can still see the dashboard's
    // chrome.
    // ---------------------------------------------------------------------

    docsName: null,        // e.g. "how-do-i-get-my-data"
    docsTitle: "",         // human label shown in the header
    docsMarkdown: "",      // raw md text
    docsError: "",
    docsLoading: false,

    async goToDocs(name, title) {
      this.docsName = name;
      this.docsTitle = title || name;
      this.docsMarkdown = "";
      this.docsError = "";
      this.docsLoading = true;
      this.route = "docs";
      try {
        const res = await fetch(`/api/docs/${encodeURIComponent(name)}`, {
          headers: { Authorization: `Bearer ${TOKEN}` },
        });
        if (!res.ok) {
          let detail = "";
          try { detail = (await res.json()).detail || ""; } catch (_) {}
          throw new Error(detail || `${res.status} ${res.statusText}`);
        }
        this.docsMarkdown = await res.text();
      } catch (e) {
        this.docsError = e.message || "Could not load doc.";
      } finally {
        this.docsLoading = false;
      }
    },

    // Computed: HTML of the loaded markdown. marked is loaded from CDN
    // (with SRI) in index.html. Same renderer config as wizard.js's
    // renderMd: links forced to http(s) and target=_blank rel=noopener.
    get docsHtml() {
      if (!this.docsMarkdown) return "";
      if (typeof marked === "undefined") return "";
      return marked.parse(this.docsMarkdown);
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
