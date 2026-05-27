"use strict";

/**
 * settings.js — Settings page (task #42)
 *
 * Currently houses a single screen: "Annotation tracks", which lists
 * every non-deleted Fulcra annotation definition on the user's account
 * and lets them soft-delete ones they no longer want surfaced in the
 * picker.  Soft-delete is the only delete primitive Fulcra exposes —
 * events written under the def remain in Fulcra but stop showing up in
 * /api/definitions.  Any plugin whose cached definition_id matched the
 * deleted def gets its cache cleared server-side so the next run
 * resolves a fresh def rather than writing to a tombstone.
 *
 * Usage (in index.html, route === 'settings'):
 *   <section x-data="settings()" x-init="boot()">
 */

// Compact "Jan 14, 2026" formatter for the per-definition row metadata.
// Copied from the definition_picker's dpHumanDate helper instead of
// importing because the rest of the file is a separate Alpine scope —
// duplicating one short formatter is cleaner than wiring a shared
// utilities module just for this.
function settingsHumanDate(isoString) {
  if (!isoString) return "unknown date";
  const d = new Date(isoString);
  if (isNaN(d.getTime())) return "unknown date";
  return d.toLocaleDateString(undefined, {
    year: "numeric", month: "short", day: "numeric",
  });
}

function settings() {
  return {
    definitions: [],
    loading: true,
    error: "",
    // Tracks which row currently has a delete in flight so the row's
    // button can show a "Deleting…" label and be disabled. Set, not
    // array, because membership-check is the only operation.
    deletingIds: new Set(),
    // Last def we successfully removed, surfaced in a small banner so the
    // user gets explicit confirmation that the destructive action landed
    // (no undo — Fulcra doesn't expose one).
    lastDeletedName: "",

    // Quick-record favorites (task #64) — per-machine. Stored as a
    // Set of def_ids. The menubar's pin toggle writes to the same
    // file via the daemon, so opening this page after pinning in the
    // menubar shows the user's actual current pin state.
    favorites: new Set(),
    favoritesError: "",
    // True while a PUT is in flight so the row checkboxes can briefly
    // disable themselves and we avoid out-of-order writes if the user
    // clicks two checkboxes faster than the network round-trip.
    favoritesSaving: false,

    // Fulcra auth status (SP5 task 3). The daemon's worker.py flips
    // `refresh_failed=True` as soon as a token refresh blows up; we read
    // that on mount and surface a "Reconnect to Fulcra" banner at the
    // top of this page so the user has a one-click path to recovery
    // without dropping to the CLI. Default to authenticated=true so we
    // don't flash a misleading banner during the initial fetch.
    fulcraAuthStatus: { authenticated: true, refresh_failed: false },
    reconnectInFlight: false,
    reconnectError: "",

    async boot() {
      // Auth status first (cheap) so the banner can render immediately
      // even if /api/definitions is slow or — more likely when
      // refresh_failed is true — fails outright. Defs + favorites run in
      // parallel after; errors in either are surfaced separately so a
      // favorites-fetch failure doesn't hide the soft-delete UI.
      await this._loadAuthStatus();
      await Promise.all([
        this._loadDefs(),
        this._loadFavorites(),
      ]);
    },

    async _loadAuthStatus() {
      try {
        const status = await api("/api/fulcra/auth/status");
        this.fulcraAuthStatus = status;
      } catch (e) {
        // Network/daemon error — leave the default (authenticated=true)
        // so we don't flash a misleading banner on a transient failure.
        // The user will see the real /api/definitions error below if
        // the daemon is genuinely down.
      }
    },

    async reconnectToFulcra() {
      // Driven by the amber banner that appears when
      // fulcraAuthStatus.refresh_failed === true. Posts to the daemon's
      // cli_login endpoint, which shells out to `fulcra login` and
      // re-reads the credentials file on success. On a successful
      // reconnect we refresh status + reload the data that the banner
      // was hiding (defs + favorites) so the page lights back up
      // without a manual page refresh.
      this.reconnectError = "";
      this.reconnectInFlight = true;
      try {
        const result = await api("/api/fulcra/auth/cli_login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        if (!result || result.ok === false) {
          this.reconnectError = (result && result.error) || "Sign-in didn't complete.";
        } else {
          await this._loadAuthStatus();
          await Promise.all([
            this._loadDefs(),
            this._loadFavorites(),
          ]);
        }
      } catch (e) {
        this.reconnectError = e.message || "Reconnect failed.";
      } finally {
        this.reconnectInFlight = false;
      }
    },

    async _loadDefs() {
      this.loading = true;
      this.error = "";
      try {
        const body = await api("/api/definitions");
        this.definitions = body.definitions ?? [];
      } catch (e) {
        this.error = e.message || "Could not load definitions.";
      } finally {
        this.loading = false;
      }
    },

    async _loadFavorites() {
      this.favoritesError = "";
      try {
        const body = await api("/api/quick-record/favorites");
        // Alpine 3 reactivity on Set requires re-assigning the field —
        // mutating .add() in place doesn't trigger a re-render. Same
        // pattern as deletingIds above.
        this.favorites = new Set(body.favorites ?? []);
      } catch (e) {
        this.favoritesError = e.message || "Could not load favorites.";
      }
    },

    isFavorite(def) {
      return this.favorites.has(def.id);
    },

    get favoritesCount() {
      return this.favorites.size;
    },

    async toggleFavorite(def) {
      // Optimistic update: flip the local Set first so the checkbox
      // feels instant, then PUT the full list. On failure, revert
      // and surface the error.
      const next = new Set(this.favorites);
      if (next.has(def.id)) {
        next.delete(def.id);
      } else {
        next.add(def.id);
      }
      const previous = this.favorites;
      this.favorites = next;
      this.favoritesSaving = true;
      try {
        await api("/api/quick-record/favorites", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ favorites: Array.from(next) }),
        });
      } catch (e) {
        // Revert and surface — most likely cause is a 401 (web token
        // rotated) or a daemon outage. Both are recoverable on retry.
        this.favorites = previous;
        this.favoritesError = e.message || "Could not save favorites.";
      } finally {
        this.favoritesSaving = false;
      }
    },

    // Same sort order as the definition picker: alphabetical by name
    // (case-insensitive), tiebreaking by created_at oldest-first so the
    // listing is stable across reloads.
    get sortedDefinitions() {
      return [...this.definitions].sort((a, b) => {
        const an = (a.name || "").toLowerCase();
        const bn = (b.name || "").toLowerCase();
        if (an < bn) return -1;
        if (an > bn) return 1;
        const at = a.created_at || "";
        const bt = b.created_at || "";
        return at.localeCompare(bt);
      });
    },

    humanDate(iso) {
      return settingsHumanDate(iso);
    },

    isDeleting(def) {
      return this.deletingIds.has(def.id);
    },

    async deleteDef(def) {
      if (this.deletingIds.has(def.id)) return;
      const ok = window.confirm(
        `Soft-delete '${def.name}'?\n\n` +
        "Events recorded under this definition stay in Fulcra, but the " +
        "definition will no longer appear in pickers and any plugin " +
        "currently using it will resolve a fresh one on its next run.\n\n" +
        "This cannot be undone from this UI."
      );
      if (!ok) return;

      // Alpine 3 reactivity on Set requires re-assigning the field —
      // mutating .add() in place doesn't trigger a re-render. Same
      // pattern as dashboard.js's runningIds.
      const nextDeleting = new Set(this.deletingIds);
      nextDeleting.add(def.id);
      this.deletingIds = nextDeleting;

      try {
        await api(`/api/definitions/${encodeURIComponent(def.id)}`, {
          method: "DELETE",
        });
        // Remove from the in-memory list so the row vanishes
        // immediately — saves a round-trip to /api/definitions.
        this.definitions = this.definitions.filter(d => d.id !== def.id);
        // The daemon's delete route also drops this def from the
        // favorites file (so the menubar doesn't keep surfacing an
        // orphan). Mirror that in our local Set so the favorites
        // counter above is correct without a separate re-fetch.
        if (this.favorites.has(def.id)) {
          const after = new Set(this.favorites);
          after.delete(def.id);
          this.favorites = after;
        }
        this.lastDeletedName = def.name;
      } catch (e) {
        // Surface failure to the user — most likely cause is a 404
        // (already deleted) or a Fulcra outage. Either way, refreshing
        // gets the UI back in sync with server state.
        this.error = e.message || "Could not delete this definition.";
        await this._loadDefs();
      } finally {
        const after = new Set(this.deletingIds);
        after.delete(def.id);
        this.deletingIds = after;
      }
    },

    // Hand control back to the parent app() so it can flip the route
    // back to the dashboard. $dispatch is the only way to reach app()
    // from inside a nested x-data scope — see dashboard.js's
    // configurePlugin / addPlugin for the same pattern.
    goBack() {
      this.$dispatch("go-to-dashboard");
    },
  };
}
