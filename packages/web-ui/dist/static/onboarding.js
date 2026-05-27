"use strict";

/**
 * onboarding.js — top-level onboarding wizard orchestrator
 *
 * Multi-step flow:
 *   0  welcome         — static intro, Next button
 *   1  signin          — sign in to Fulcra. Default: browser-based device-auth
 *                        flow via POST /api/fulcra/auth/cli_login. Fallback
 *                        (when fulcra CLI is unavailable, or user clicks
 *                        "Use a token instead"): paste-token via POST
 *                        /api/fulcra/auth/token.
 *   2  collect_modes   — static explanation of historical-vs-live capture
 *                        with four worked combo examples (music, TV,
 *                        podcasts, Apple movies), an Attention callout,
 *                        and a closing encouragement to write your own
 *                        plugin. No API calls, no per-plugin state.
 *   3  pick_plugins    — grouped plugin checkboxes from GET /api/status.
 *                        _loadPlugins() fires async during collect_modes
 *                        so the list is ready by the time the user
 *                        advances; the existing pickLoading flag handles
 *                        the unlikely race where the user clicks Next
 *                        before the request returns.
 *   4  configure       — walks each picked plugin's setup_steps via createWizard()
 *   5  done            — summary, links to dashboard
 *
 * Usage (in index.html):
 *   <section x-data="onboarding()" x-init="boot()">
 *     <!-- rendered via Alpine templates in index.html -->
 *   </section>
 *
 * The parent app() re-routes to 'dashboard' when onboarding emits "complete".
 */

function onboarding() {
  return {
    // Current high-level phase: welcome | signin | collect_modes | pick_plugins | configure | done
    phase: "welcome",

    // --- signin state ---
    // signinMode: "auto" while we're probing for the fulcra CLI; "cli" if it's
    // present (we offer the browser flow); "token" if not (fallback only).
    // The user can flip from "cli" to "token" with the "Use a token instead"
    // link in the template.
    signinMode: "auto",
    cliProbed: false,
    cliAvailable: false,
    cliSignedIn: false,
    tokenInput: "",
    signinError: "",
    signinLoading: false,
    signinAccount: null,

    // --- pick_plugins state ---
    allPlugins: [],         // full list from /api/status
    categories: [],         // [{name, plugins: [{id, name, description, category, enabled}]}]
    selectedIds: new Set(), // ids the user checked (mutable; seeded from wasEnabledAtStart on load)
    // Immutable snapshot of which plugins were already enabled when the
    // picker last loaded. Used to (a) decide which rows render with a
    // "Set up" pill + Reconfigure toggle and (b) skip the per-plugin
    // wizard walk for already-enabled plugins the user didn't flip
    // Reconfigure on. Refreshed only on _loadPlugins(), never on user
    // clicks — that's the whole point.
    wasEnabledAtStart: new Set(),
    // Opt-in set: plugin ids the user has flipped Reconfigure ON for.
    // Only plugins in wasEnabledAtStart can be in here; for new plugins
    // there's nothing to reconfigure. Cleared when un-checking the row
    // so toggling stays consistent.
    reconfigureIds: new Set(),
    pickLoading: true,
    pickError: "",

    // --- configure state ---
    pluginsToSetup: [],     // ordered array of plugin contracts to walk through
    currentSetupIndex: 0,   // which plugin we're on
    currentWizard: null,    // createWizard(...) data object for the active plugin
    currentContract: null,  // the contract for the active plugin

    // --- done state ---
    // The walk-completion counters drive the three-bucket summary on the
    // done screen so the user can see what actually changed this trip.
    // 'newly enabled' = walked-to-completion plugins NOT in wasEnabledAtStart.
    // 'reconfigured'  = walked-to-completion plugins IN wasEnabledAtStart
    //                   (only possible via the Reconfigure opt-in).
    // 'left alone'    = pre-checked plugins skipped at startConfigure time.
    newlyEnabledCount: 0,
    reconfiguredCount: 0,
    leftAloneCount: 0,

    async boot() {
      // Check whether app() requested a specific entry phase. app.addPlugin()
      // writes 'pick_plugins' to window.__fulcraOnboardingEntryPhase before
      // setting route = 'onboarding', so onboarding.boot() can jump straight
      // to the plugin picker without replaying welcome or signin.
      // app.runOnboarding() leaves the flag null (full flow). We consume the
      // flag here and immediately clear it so a later boot() call sees null.
      const requestedPhase = window.__fulcraOnboardingEntryPhase;
      window.__fulcraOnboardingEntryPhase = null;

      if (requestedPhase === "pick_plugins") {
        // Caller guarantees the user is already authenticated; skip straight
        // to the plugin picker without hitting the auth endpoints.
        this.phase = "pick_plugins";
        await this._loadPlugins();
        return;
      }

      // 1) Daemon already has a stored token — skip sign-in entirely.
      try {
        const authStatus = await api("/api/fulcra/auth/status");
        if (authStatus.authenticated) {
          this.phase = "pick_plugins";
          await this._loadPlugins();
          return;
        }
      } catch (e) {
        // Fall through to CLI probe.
      }

      // 2) Probe the fulcra CLI. Two-purpose call:
      //    - "available" → can we offer the browser flow?
      //    - "signed_in" → does the CLI already have credentials we can use?
      try {
        const cli = await api("/api/fulcra/auth/cli_status");
        this.cliProbed = true;
        this.cliAvailable = !!cli.available;
        this.cliSignedIn = !!cli.signed_in;
        this.signinMode = cli.available ? "cli" : "token";
      } catch (e) {
        this.cliProbed = true;
        this.cliAvailable = false;
        this.signinMode = "token";
      }
    },

    // ---------------------------------------------------------------------------
    // Navigation helpers
    // ---------------------------------------------------------------------------

    async goToSignin() {
      this.phase = "signin";
    },

    // Browser-based sign-in via the fulcra CLI. The POST blocks until the
    // user finishes the device-auth flow in their browser (up to ~2 min),
    // then the daemon validates+stores the token. We just need to wait and
    // surface progress/errors.
    async signinViaCli() {
      this.signinError = "";
      this.signinLoading = true;
      try {
        const result = await api("/api/fulcra/auth/cli_login", {
          method: "POST",
          body: JSON.stringify({}),
        });
        if (result.ok) {
          // Hand off to the static explainer screen; load plugins in the
          // background so the pick_plugins list is ready by the time the
          // user advances. pickLoading covers the race if they're faster
          // than the API.
          this.phase = "collect_modes";
          this._loadPlugins();  // intentionally not awaited
        } else {
          this.signinError = result.error || "Sign-in didn't complete. Please try again.";
        }
      } catch (e) {
        this.signinError = e.message || "Sign-in failed. Please try again.";
      } finally {
        this.signinLoading = false;
      }
    },

    // Fallback path: user pasted a token by hand.
    async submitToken() {
      this.signinError = "";
      const tok = this.tokenInput.trim();
      if (!tok) {
        this.signinError = "Please paste your Fulcra access token.";
        return;
      }
      this.signinLoading = true;
      try {
        const result = await api("/api/fulcra/auth/token", {
          method: "POST",
          body: JSON.stringify({ token: tok }),
        });
        if (result.ok) {
          // Same flow as signinViaCli — explainer then pick.
          this.phase = "collect_modes";
          this._loadPlugins();  // intentionally not awaited
        } else {
          this.signinError = result.error || "Token not accepted. Please try again.";
        }
      } catch (e) {
        this.signinError = `Failed to verify token: ${e.message}`;
      } finally {
        this.signinLoading = false;
      }
    },

    // collect_modes → pick_plugins (Next button on the static explainer screen).
    goToPickPlugins() {
      this.phase = "pick_plugins";
      // _loadPlugins() was fired during signin success; only re-load
      // if it never completed or it errored out. pickLoading flips to
      // false in _loadPlugins's finally block, so the in-flight guard
      // is safe; the (no plugins) || (had an error) check catches the
      // two cases where it's worth retrying.
      if (this.pickLoading) {
        // already in flight from signin handler; nothing to do
        return;
      }
      if (this.allPlugins.length === 0 || this.pickError) {
        this._loadPlugins();
      }
    },

    // collect_modes → signin (Back button on the static explainer screen).
    backToSignin() {
      this.phase = "signin";
    },

    async _loadPlugins() {
      this.pickLoading = true;
      this.pickError = "";
      try {
        const status = await api("/api/status");
        this.allPlugins = status.plugins ?? [];

        // Pre-check every plugin the daemon reports as enabled, and snapshot
        // which ids were enabled at this moment so the configure walk can
        // skip past them by default. wasEnabledAtStart is immutable for the
        // rest of this trip; selectedIds is what the user mutates.
        const enabledIds = this.allPlugins
          .filter(p => p.enabled)
          .map(p => p.id);
        this.selectedIds = new Set(enabledIds);
        this.wasEnabledAtStart = new Set(enabledIds);
        this.reconfigureIds = new Set();

        // Group by category
        const catMap = {};
        for (const p of this.allPlugins) {
          const cat = p.category || "other";
          if (!catMap[cat]) catMap[cat] = [];
          catMap[cat].push(p);
        }
        // Ordered category display. NOTE: "audio" replaces the old "music"
        // category slug (task #54) — plugins that produce listening data
        // (podcasts, music streams) are now grouped under Audio. Backend
        // plugin metadata is renamed in parallel; this frontend list stays in
        // sync.
        const catOrder = ["audio", "video", "books", "journal", "activity", "other"];
        this.categories = catOrder
          .filter(c => catMap[c])
          .map(c => ({
            name: c.charAt(0).toUpperCase() + c.slice(1),
            slug: c,
            plugins: catMap[c],
          }));
        // Add any unexpected categories not in the order list
        for (const [c, plugins] of Object.entries(catMap)) {
          if (!catOrder.includes(c)) {
            this.categories.push({
              name: c.charAt(0).toUpperCase() + c.slice(1),
              slug: c,
              plugins,
            });
          }
        }
      } catch (e) {
        this.pickError = `Could not load plugins: ${e.message}`;
      } finally {
        this.pickLoading = false;
      }
    },

    togglePlugin(id) {
      const next = new Set(this.selectedIds);
      if (next.has(id)) {
        next.delete(id);
        // Un-checking is canonical "not touching this trip" — drop any
        // Reconfigure opt-in too so re-checking starts from the safe
        // (skip) default.
        if (this.reconfigureIds.has(id)) {
          const nextRc = new Set(this.reconfigureIds);
          nextRc.delete(id);
          this.reconfigureIds = nextRc;
        }
      } else {
        next.add(id);
      }
      this.selectedIds = next;
    },

    isSelected(id) {
      return this.selectedIds.has(id);
    },

    // Flip the Reconfigure opt-in for a row. Only meaningful for plugins
    // in wasEnabledAtStart; for a new plugin, calling this is a no-op
    // because there's nothing to reconfigure.
    toggleReconfigure(id) {
      if (!this.wasEnabledAtStart.has(id)) return;
      const next = new Set(this.reconfigureIds);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      this.reconfigureIds = next;
    },

    isReconfiguring(id) {
      return this.reconfigureIds.has(id);
    },

    // Was this plugin already enabled when the picker first loaded?
    // Used by the template to decide whether to render the "Set up" pill
    // + Reconfigure toggle on the row.
    wasEnabledOnLoad(id) {
      return this.wasEnabledAtStart.has(id);
    },

    get selectedCount() {
      return this.selectedIds.size;
    },

    // Button copy varies by intent so the user knows what clicking it does:
    //   - nothing selected → "Skip for now" (same as before)
    //   - everything pre-checked, no Reconfigure → "Continue" (nothing to walk)
    //   - some plugins to walk → "Set up N plugin(s)" (same as before)
    get primaryActionLabel() {
      if (this.selectedIds.size === 0) return "Skip for now";
      const walkCount = [...this.selectedIds].filter(id =>
        !this.wasEnabledAtStart.has(id) || this.reconfigureIds.has(id)
      ).length;
      if (walkCount === 0) return "Continue";
      return `Set up ${walkCount} plugin${walkCount !== 1 ? "s" : ""}`;
    },

    // True when every bucket is zero — i.e. the user un-checked everything
    // and walked nothing. We swap to a friendlier "your setup is unchanged"
    // message instead of showing three "0" rows.
    get doneSummaryEmpty() {
      return this.newlyEnabledCount === 0
        && this.reconfiguredCount === 0
        && this.leftAloneCount === 0;
    },

    async startConfigure() {
      if (this.selectedIds.size === 0) {
        // User picked nothing — go straight to done with all-zero buckets.
        this.newlyEnabledCount = 0;
        this.reconfiguredCount = 0;
        this.leftAloneCount = 0;
        this.phase = "done";
        return;
      }

      // Partition selectedIds into "needs the wizard" and "leave alone."
      // A plugin only enters the walk if it's new OR the user flipped
      // Reconfigure on for it. Everything else is in wasEnabledAtStart
      // and stays enabled with its existing config untouched.
      const toWalk = [];
      let skipCount = 0;
      for (const id of this.selectedIds) {
        if (this.wasEnabledAtStart.has(id) && !this.reconfigureIds.has(id)) {
          skipCount += 1;
        } else {
          toWalk.push(id);
        }
      }
      this.leftAloneCount = skipCount;
      this.newlyEnabledCount = 0;
      this.reconfiguredCount = 0;

      if (toWalk.length === 0) {
        // Nothing to walk — pre-checks with no Reconfigure flips. Go
        // straight to the done summary so the user sees the
        // "left alone" count and understands nothing changed.
        this.phase = "done";
        return;
      }

      // Fetch contracts for the walk-needing plugins only. Skipping the
      // /contract fetch for left-alone plugins isn't just an optimization:
      // a stale contract endpoint shouldn't break a re-run that doesn't
      // touch that plugin.
      const contracts = [];
      for (const id of toWalk) {
        try {
          const contract = await api(`/api/plugin/${id}/contract`);
          contracts.push(contract);
        } catch (e) {
          console.warn(`Could not fetch contract for ${id}:`, e);
          const stub = this.allPlugins.find(p => p.id === id) || { id, name: id };
          contracts.push({ id, name: stub.name, setup_steps: [], required_settings: [], required_credentials: [] });
        }
      }

      this.pluginsToSetup = contracts;
      this.currentSetupIndex = 0;
      this._enterCurrentPlugin();
      this.phase = "configure";
    },

    _enterCurrentPlugin() {
      const contract = this.pluginsToSetup[this.currentSetupIndex];
      this.currentContract = contract;
      // Force the inner `<div x-data="currentWizard">` scope to tear down
      // and re-mount when switching plugins. Alpine's x-data binding
      // captures the object reference once at mount time; assigning a
      // new wizard object to `currentWizard` does NOT re-evaluate the
      // inner scope, because `x-if="currentWizard && ..."` stays truthy
      // through the swap. Flipping through null forces x-if to false →
      // true on the next tick, which tears the inner scope down and
      // re-initializes it with the new wizard. Without this, advancing
      // past the first plugin keeps showing the first plugin's content.
      this.currentWizard = null;
      setTimeout(() => {
        this.currentWizard = createWizard(
          contract,
          () => this._onPluginComplete(),
          () => this._onPluginSkipped(),
          () => this._backToPickPlugins(),
        );
      }, 0);
    },

    // Bail out of the current plugin's wizard back to the pick_plugins
    // screen so the user can adjust their selection (task #64). We
    // deliberately PRESERVE this.selectedIds — that's the whole point —
    // and only reset the wizard machinery so re-entering Configure builds
    // a fresh set of contracts in case picks changed.
    _backToPickPlugins() {
      this.phase = "pick_plugins";
      this.pluginsToSetup = [];
      this.currentSetupIndex = 0;
      this.currentWizard = null;
      this.currentContract = null;
    },

    async _onPluginComplete() {
      // Bucket the just-completed plugin: was it new (newly set up) or
      // already-enabled-with-Reconfigure-on (reconfigured)? The
      // wasEnabledAtStart snapshot is the source of truth.
      const id = this.currentContract?.id;
      if (id && this.wasEnabledAtStart.has(id)) {
        this.reconfiguredCount += 1;
      } else {
        this.newlyEnabledCount += 1;
      }
      this._advancePluginOrFinish();
    },

    _onPluginSkipped() {
      // Plugin abandoned without enable — don't bump any completion counter.
      this._advancePluginOrFinish();
    },

    _advancePluginOrFinish() {
      const nextIndex = this.currentSetupIndex + 1;
      if (nextIndex < this.pluginsToSetup.length) {
        this.currentSetupIndex = nextIndex;
        this._enterCurrentPlugin();
      } else {
        this.phase = "done";
      }
    },

    // Bail on the entire onboarding flow. No plugins are enabled past what
    // the user has already completed; the parent app routes to the dashboard.
    skipOnboarding() {
      this.phase = "done";
    },

    completeDone() {
      // Signal parent app to switch to dashboard
      this.$dispatch("onboarding-complete");
    },

    // ---------------------------------------------------------------------------
    // Computed helpers for the template
    // ---------------------------------------------------------------------------

    // Two-line header for the per-plugin wizard walk: small "Plugin N of M"
    // progress label on top, prominent plugin name below. The plugin name
    // is the actual context for everything happening on the current step,
    // so it deserves its own line + heading weight instead of being buried
    // in a small gray progress string.
    get configureProgressLabel() {
      const total = this.pluginsToSetup.length;
      const current = this.currentSetupIndex + 1;
      return `Plugin ${current} of ${total}`;
    },
    get configurePluginName() {
      return this.currentContract?.name || "";
    },
  };
}
