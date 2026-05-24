"use strict";

/**
 * dashboard.js — Preferences home / plugin grid
 *
 * Renders after onboarding completes or when the user is already signed in
 * and has plugins enabled.
 *
 * Shows:
 *   - Header: "Fulcra Collect" + "Add plugin" button
 *   - Plugin grid: name, description, status pill, last-run timestamp,
 *     Run-now button (for manual/scheduled), Configure link placeholder
 *   - Activity feed placeholder (Phase D fills this in)
 *
 * Usage (in index.html, route === 'dashboard'):
 *   <section x-data="dashboard()" x-init="boot()">
 */

function dashboard() {
  return {
    plugins: [],
    loading: true,
    error: "",
    runningIds: new Set(),   // plugin ids currently being triggered

    async boot() {
      await this.reload();
    },

    async reload() {
      this.loading = true;
      this.error = "";
      try {
        const status = await api("/api/status");
        this.plugins = status.plugins ?? [];
      } catch (e) {
        this.error = e.message;
      } finally {
        this.loading = false;
      }
    },

    // ---------------------------------------------------------------------------
    // Per-plugin helpers
    // ---------------------------------------------------------------------------

    pillClass(plugin) {
      // Returns a Tailwind class string for the status pill.
      // v1: simple enabled/disabled from plugin.enabled.
      // Phase D will add running/failing/auth-needed/healthy states.
      if (!plugin.enabled) return "bg-slate-100 text-slate-500";
      return "bg-violet-100 text-violet-700";
    },

    pillLabel(plugin) {
      if (!plugin.enabled) return "Disabled";
      return "Enabled";
    },

    humanInterval(plugin) {
      // Render the default_interval_s as a human string.
      const s = plugin.default_interval_s;
      if (!s) return null;
      if (s < 3600) return `Every ${Math.round(s / 60)} min`;
      if (s < 86400) {
        const h = Math.round(s / 3600);
        return `Every ${h} hour${h !== 1 ? "s" : ""}`;
      }
      const d = Math.round(s / 86400);
      return `Every ${d} day${d !== 1 ? "s" : ""}`;
    },

    canRunNow(plugin) {
      return plugin.kind === "manual" || plugin.kind === "scheduled";
    },

    isRunning(plugin) {
      return this.runningIds.has(plugin.id);
    },

    async runNow(plugin) {
      if (this.runningIds.has(plugin.id)) return;
      const next = new Set(this.runningIds);
      next.add(plugin.id);
      this.runningIds = next;
      try {
        await api(`/api/plugin/${plugin.id}/run`, { method: "POST" });
      } catch (e) {
        console.warn("run failed:", e);
      } finally {
        const after = new Set(this.runningIds);
        after.delete(plugin.id);
        this.runningIds = after;
        // Refresh status after run
        await this.reload();
      }
    },

    // Add-plugin: dispatch event to parent app to start the add-plugin flow
    addPlugin() {
      this.$dispatch("add-plugin");
    },
  };
}
