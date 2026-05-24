"use strict";

/**
 * onboarding.js — top-level onboarding wizard orchestrator
 *
 * Multi-step flow:
 *   0  welcome         — static intro, Next button
 *   1  signin          — paste Fulcra token, verify via POST /api/fulcra/auth/token
 *   2  pick_plugins    — grouped plugin checkboxes from GET /api/status
 *   3  configure       — walks each picked plugin's setup_steps via createWizard()
 *   4  done            — summary, links to dashboard
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
    // Current high-level phase: welcome | signin | pick_plugins | configure | done
    phase: "welcome",

    // --- signin state ---
    tokenInput: "",
    signinError: "",
    signinLoading: false,
    signinAccount: null,

    // --- pick_plugins state ---
    allPlugins: [],         // full list from /api/status
    categories: [],         // [{name, plugins: [{id, name, description, category, enabled}]}]
    selectedIds: new Set(), // ids the user checked
    pickLoading: true,
    pickError: "",

    // --- configure state ---
    pluginsToSetup: [],     // ordered array of plugin contracts to walk through
    currentSetupIndex: 0,   // which plugin we're on
    currentWizard: null,    // createWizard(...) data object for the active plugin
    currentContract: null,  // the contract for the active plugin

    // --- done state ---
    enabledCount: 0,

    async boot() {
      // Check if already signed in — skip to pick_plugins if so
      try {
        const authStatus = await api("/api/fulcra/auth/status");
        if (authStatus.authenticated) {
          this.phase = "pick_plugins";
          await this._loadPlugins();
        }
      } catch (e) {
        // Not signed in — start at welcome
      }
    },

    // ---------------------------------------------------------------------------
    // Navigation helpers
    // ---------------------------------------------------------------------------

    async goToSignin() {
      this.phase = "signin";
    },

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
          this.phase = "pick_plugins";
          await this._loadPlugins();
        } else {
          this.signinError = result.error || "Token not accepted. Please try again.";
        }
      } catch (e) {
        this.signinError = `Failed to verify token: ${e.message}`;
      } finally {
        this.signinLoading = false;
      }
    },

    async _loadPlugins() {
      this.pickLoading = true;
      this.pickError = "";
      try {
        const status = await api("/api/status");
        this.allPlugins = status.plugins ?? [];

        // Group by category
        const catMap = {};
        for (const p of this.allPlugins) {
          const cat = p.category || "other";
          if (!catMap[cat]) catMap[cat] = [];
          catMap[cat].push(p);
        }
        // Ordered category display
        const catOrder = ["music", "video", "books", "journal", "activity", "other"];
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
      } else {
        next.add(id);
      }
      this.selectedIds = next;
    },

    isSelected(id) {
      return this.selectedIds.has(id);
    },

    get selectedCount() {
      return this.selectedIds.size;
    },

    async startConfigure() {
      if (this.selectedIds.size === 0) {
        // User picked nothing — go straight to done
        this.enabledCount = 0;
        this.phase = "done";
        return;
      }

      // Fetch contracts for all selected plugins
      const ids = [...this.selectedIds];
      const contracts = [];
      for (const id of ids) {
        try {
          const contract = await api(`/api/plugin/${id}/contract`);
          contracts.push(contract);
        } catch (e) {
          console.warn(`Could not fetch contract for ${id}:`, e);
          // Still include a minimal stub so we can enable it
          const stub = this.allPlugins.find(p => p.id === id) || { id, name: id };
          contracts.push({ id, name: stub.name, setup_steps: [], required_settings: [], required_credentials: [] });
        }
      }

      this.pluginsToSetup = contracts;
      this.currentSetupIndex = 0;
      this.enabledCount = 0;
      this._enterCurrentPlugin();
      this.phase = "configure";
    },

    _enterCurrentPlugin() {
      const contract = this.pluginsToSetup[this.currentSetupIndex];
      this.currentContract = contract;
      this.currentWizard = createWizard(contract, () => this._onPluginComplete());
    },

    async _onPluginComplete() {
      this.enabledCount += 1;
      const nextIndex = this.currentSetupIndex + 1;
      if (nextIndex < this.pluginsToSetup.length) {
        this.currentSetupIndex = nextIndex;
        this._enterCurrentPlugin();
      } else {
        this.phase = "done";
      }
    },

    completeDone() {
      // Signal parent app to switch to dashboard
      this.$dispatch("onboarding-complete");
    },

    // ---------------------------------------------------------------------------
    // Computed helpers for the template
    // ---------------------------------------------------------------------------

    get configureProgressLabel() {
      const total = this.pluginsToSetup.length;
      const current = this.currentSetupIndex + 1;
      return `Plugin ${current} of ${total}: ${this.currentContract?.name || ""}`;
    },
  };
}
