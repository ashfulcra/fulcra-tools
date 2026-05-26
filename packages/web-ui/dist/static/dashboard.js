"use strict";

/**
 * dashboard.js — Preferences home / plugin grid + live activity feed
 *
 * Renders after onboarding completes or when the user is already signed in
 * and has plugins enabled.
 *
 * Shows:
 *   - Header: "Fulcra Collect" + "Add plugin" button
 *   - Plugin grid: name, description, richer status pill, last-run timestamp,
 *     Run-now button (for manual/scheduled)
 *   - Live activity feed: polls /api/activity?limit=30 every 5 seconds while
 *     the dashboard is the active view. Each row shows relative timestamp,
 *     plugin id, and annotation summary. Failed writes shown in red.
 *
 * Usage (in index.html, route === 'dashboard'):
 *   <section x-data="dashboard()" x-init="boot()">
 */

// ---------------------------------------------------------------------------
// Relative-time helper — "2m ago", "1h ago", "Yesterday", "3 days ago"
// ---------------------------------------------------------------------------

function timeAgo(isoString) {
  if (!isoString) return "";
  const then = new Date(isoString);
  const nowMs = Date.now();
  const diffMs = nowMs - then.getTime();
  if (isNaN(diffMs) || diffMs < 0) return "just now";

  const secs = Math.floor(diffMs / 1000);
  if (secs < 60) return "just now";
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(diffMs / 3_600_000);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(diffMs / 86_400_000);
  if (days === 1) return "Yesterday";
  return `${days} days ago`;
}

// Pick the cleanest unit for a frequency interval in seconds. Used by the
// dashboard row to show "Every 6 hours" / "Every 30 min".
function humanizeInterval(seconds) {
  if (!seconds || seconds <= 0) return null;
  if (seconds < 3600) {
    const m = Math.round(seconds / 60);
    return `Every ${m} min`;
  }
  if (seconds < 86400) {
    const h = Math.round(seconds / 3600);
    return `Every ${h} hour${h !== 1 ? "s" : ""}`;
  }
  const d = Math.round(seconds / 86400);
  return `Every ${d} day${d !== 1 ? "s" : ""}`;
}

// Same shape as timeAgo, but with a "Last run: " / "Never run" framing so
// the dashboard row can drop the result in directly.
function humanizeRelativeTime(isoString) {
  if (!isoString) return "Never run";
  return `Last run: ${timeAgo(isoString)}`;
}

// ---------------------------------------------------------------------------
// Per-plugin status pill — richer than v1's Enabled/Disabled
// ---------------------------------------------------------------------------

function pillFor(plugin) {
  if (!plugin.enabled) {
    return { label: "Disabled", cls: "bg-slate-100 text-slate-500" };
  }
  if ((plugin.consecutive_failures || 0) >= 3) {
    return { label: "Failing", cls: "bg-red-100 text-red-800" };
  }
  if (plugin.last_outcome === "running") {
    return { label: "Running", cls: "bg-violet-100 text-violet-800" };
  }
  if (plugin.last_outcome === "done") {
    return { label: "Healthy", cls: "bg-emerald-100 text-emerald-800" };
  }
  if (plugin.kind === "manual") {
    return { label: "Manual", cls: "bg-emerald-50 text-emerald-700" };
  }
  // Scheduled but hasn't run yet (or last_outcome is null/error but failures < 3)
  if (plugin.kind === "scheduled" || plugin.kind === "service") {
    return { label: "Scheduled", cls: "bg-slate-100 text-slate-700" };
  }
  return { label: "Enabled", cls: "bg-violet-100 text-violet-700" };
}

// ---------------------------------------------------------------------------
// dashboard() — Alpine component
// ---------------------------------------------------------------------------

function dashboard() {
  return {
    plugins: [],
    loading: true,
    error: "",
    runningIds: new Set(),   // plugin ids currently being triggered

    // Activity feed state
    activityEntries: [],
    activityLoading: false,
    activityError: "",
    _activityPollTimer: null,

    async boot() {
      await this.reload();
      this._startActivityPoll();
    },

    destroy() {
      this._stopActivityPoll();
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
    // Activity feed
    // ---------------------------------------------------------------------------

    _startActivityPoll() {
      this._stopActivityPoll();
      // Fetch immediately, then every 5 seconds
      this._fetchActivity();
      this._activityPollTimer = setInterval(() => this._fetchActivity(), 5000);
    },

    _stopActivityPoll() {
      if (this._activityPollTimer !== null) {
        clearInterval(this._activityPollTimer);
        this._activityPollTimer = null;
      }
    },

    async _fetchActivity() {
      this.activityError = "";
      try {
        const body = await api("/api/activity?limit=30");
        this.activityEntries = body.entries ?? [];
      } catch (e) {
        this.activityError = e.message;
      }
    },

    timeAgo(isoString) {
      return timeAgo(isoString);
    },

    // ---------------------------------------------------------------------------
    // Per-plugin helpers
    // ---------------------------------------------------------------------------

    pillClass(plugin) {
      return pillFor(plugin).cls;
    },

    pillLabel(plugin) {
      return pillFor(plugin).label;
    },

    // Frequency line. Scheduled plugins → "Every N hours/min". Service
    // plugins → "Continuous (service)". Manual plugins → null (no line).
    humanInterval(plugin) {
      if (plugin.kind === "service") return "Continuous (service)";
      if (plugin.kind === "manual") return null;
      return humanizeInterval(plugin.default_interval_s);
    },

    // Last-run line. Manual plugins use the slightly friendlier "Not run yet"
    // when they've never been triggered, vs. the generic "Never run" for
    // scheduled/service rows.
    lastRunLabel(plugin) {
      if (plugin.last_run) {
        return `Last run: ${timeAgo(plugin.last_run)}`;
      }
      return plugin.kind === "manual" ? "Not run yet" : "Never run";
    },

    // Plugins enabled by the user, sorted by kind then name. Surfaced at the
    // top of the dashboard so the "what's running" set is immediately visible.
    get enabledPlugins() {
      return this._sortedByKindName(this.plugins.filter(p => p.enabled));
    },

    // Plugins the user could turn on but hasn't. Sorted the same way and
    // rendered in a collapsed-feeling section below the enabled list.
    get disabledPlugins() {
      return this._sortedByKindName(this.plugins.filter(p => !p.enabled));
    },

    _sortedByKindName(arr) {
      return [...arr].sort((a, b) => {
        const ka = (a.kind || "").localeCompare(b.kind || "");
        if (ka !== 0) return ka;
        return (a.name || "").localeCompare(b.name || "");
      });
    },

    // Hand control back to the parent app() so it can switch the route to the
    // per-plugin setup wizard. Triggered by the Configure button on each row.
    configurePlugin(plugin) {
      this.$dispatch("configure-plugin", { id: plugin.id });
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
