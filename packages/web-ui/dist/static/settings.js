"use strict";

/**
 * settings.js — Settings page (task #42)
 *
 * Manages legacy annotation-definition metadata separately from protocol2
 * quick-record favorites. Existing moment/duration definitions supply choices;
 * new captures use owned v1 Events. Soft-deleting a definition hides that
 * choice while preserving recorded data, favorites and v1 destinations.
 * The server clears matching legacy plugin state; this is not record Undo.
 * This screen does not expose the backend's definition-restore operation.
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

const quickRecordUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
function quickRecordScope(body) {
  if (!body || body.protocol_version !== 2 || !quickRecordUuid.test(body.account_id || "") ||
      typeof body.config_epoch !== "string" || !body.config_epoch || body.config_epoch.length > 256) {
    throw new Error("Quick-record scope changed. Refresh before editing.");
  }
  return {protocol_version: 2, account_id: body.account_id, config_epoch: body.config_epoch};
}
function quickRecordFavorites(body, revision) {
  if (!body || body.ok !== true || !Number.isSafeInteger(revision) || revision < 0 ||
      !Array.isArray(body.favorites) || body.favorites.length > 2048 ||
      body.favorites.some(id => typeof id !== "string" || !quickRecordUuid.test(id)) ||
      new Set(body.favorites).size !== body.favorites.length) {
    throw new Error("Could not verify favorites. Refresh before editing.");
  }
}
function quickRecordHandshake(body) {
  const scope = quickRecordScope(body);
  quickRecordFavorites(body, body.favorites_revision);
  if (!Array.isArray(body.definitions) || body.definitions.length > 2048 ||
      new Set(body.definitions.map(d => d.id)).size !== body.definitions.length ||
      body.definitions.some(d => !quickRecordUuid.test(d.id || "") ||
        typeof d.name !== "string" || !d.name || d.name.length > 256 ||
        !["moment", "duration"].includes(d.annotation_type) ||
        typeof d.digest !== "string" || !/^[0-9a-f]{64}$/.test(d.digest) ||
        typeof d.pinned !== "boolean" || d.pinned !== body.favorites.includes(d.id))) {
    throw new Error("Could not verify quick-record choices. Refresh before editing.");
  }
  return scope;
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
    // Last metadata deletion, confirmed inline. Restoration is a separate
    // backend operation that this screen does not expose.
    lastDeletedName: "",

    // Account-scoped local favorites; one handshake owns choices/revision/scope.
    favorites: new Set(),
    quickRecordChoices: [],
    quickRecordScope: null,
    favoritesRevision: null,
    favoritesLoading: false,
    _favoriteLoadSerial: 0,
    favoritesError: "",
    // True while one revisioned pin change is in flight so the row checkboxes can briefly
    // disable themselves and we avoid out-of-order writes if the user
    // clicks two checkboxes faster than the network round-trip.
    favoritesSaving: false,

    // A fresh quiet local read is availability evidence, not server/provider health.
    fulcraAuthStatus: { authenticated: null, availability: "loading", refresh_failed: false },
    _authStatusSerial: 0,
    keychainAccessBusy: false,
    keychainAccessError: "",
    reconnectInFlight: false,
    _reconnectSerial: 0,
    _cancelReconnect: null,
    reconnectError: "",
    reconnectAuthUrl: "",
    reconnectWebAuthCode: "",

    async boot() {
      // Auth status first (cheap) so the banner can render immediately
      // even if /api/definitions is slow or — more likely when
      // refresh_failed is true — fails outright. Defs + favorites run in
      // parallel after; errors in either are surfaced separately so a
      // favorites-fetch failure doesn't hide the soft-delete UI.
      await this._loadAuthStatus();
      if (this.fulcraAuthStatus.authenticated === null) return;
      await Promise.all([
        this._loadDefs(),
        this._loadFavorites(),
      ]);
    },

    async _loadAuthStatus() {
      const serial = ++this._authStatusSerial;
      this.fulcraAuthStatus = {authenticated: null, availability: "loading", refresh_failed: false};
      try {
        const status = await api("/api/fulcra/auth/status");
        const states = ["present", "missing", "access_required", "pending", "changed", "transition", "unavailable"];
        const expected = status?.availability === "present" ? true : status?.availability === "missing" ? false : null;
        if (!states.includes(status?.availability) || status.authenticated !== expected ||
            typeof status.refresh_failed !== "boolean" ||
            ((expected === true || expected === false) &&
             (status.provenance !== "fresh_quiet_keychain" || typeof status.checked_at !== "string"))) {
          throw new Error("Sign-in availability response is unverified");
        }
        if (serial === this._authStatusSerial) this.fulcraAuthStatus = status;
      } catch (e) {
        if (serial === this._authStatusSerial) this.fulcraAuthStatus = {
          authenticated: null, availability: "unavailable", refresh_failed: false,
          code: "keychain_unavailable"};
      }
    },

    async authorizeFulcraKeychain() {
      if (this.keychainAccessBusy) return;
      this.keychainAccessBusy = true;
      this.keychainAccessError = "";
      try {
        const result = await api("/api/fulcra/auth/keychain_access", {method: "POST"});
        await this._loadAuthStatus();
        if (!result?.ok || this.fulcraAuthStatus.authenticated !== true) {
          this.keychainAccessError = result?.error || "Access is still unverified. Finish any pending request, then check again.";
        } else {
          await Promise.all([this._loadDefs(), this._loadFavorites()]);
        }
      } catch (_) {
        this.keychainAccessError = "Keychain access is unavailable. Your saved sign-in was kept.";
      } finally {
        this.keychainAccessBusy = false;
      }
    },

    async reconnectToFulcra() {
      if (this.reconnectInFlight) return;
      this.reconnectError = "";
      const serial = ++this._reconnectSerial;
      this.reconnectInFlight = true;
      this.reconnectAuthUrl = "";
      this.reconnectWebAuthCode = "";
      try {
        await browserSignin(api, ({authUrl, webCode}) => {
          this.reconnectAuthUrl = authUrl;
          this.reconnectWebAuthCode = webCode;
        }, cancel => { this._cancelReconnect = cancel; });
        if (serial !== this._reconnectSerial) return;
        await this._loadAuthStatus();
        await Promise.all([this._loadDefs(), this._loadFavorites()]);
      } catch (e) {
        if (serial === this._reconnectSerial) this.reconnectError = e.message || "Reconnect failed.";
      } finally {
        if (serial === this._reconnectSerial) {
          this.reconnectInFlight = false;
          this.reconnectAuthUrl = "";
          this.reconnectWebAuthCode = "";
          this._cancelReconnect = null;
        }
      }
    },

    async cancelReconnect() {
      const serial = ++this._reconnectSerial;
      const cancel = this._cancelReconnect;
      this._cancelReconnect = null;
      this.reconnectInFlight = false;
      this.reconnectAuthUrl = "";
      this.reconnectWebAuthCode = "";
      try { if (cancel) await cancel(); }
      catch {
        if (serial === this._reconnectSerial) this.reconnectError = "Could not cancel the previous sign-in. Try again.";
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
      const serial = ++this._favoriteLoadSerial;
      this.favoritesLoading = true;
      this.quickRecordScope = null;
      this.favoritesError = "";
      try {
        const body = await api("/api/quick-record/v2");
        const scope = quickRecordHandshake(body);
        if (serial !== this._favoriteLoadSerial) return;
        this.quickRecordChoices = body.definitions;
        this.quickRecordScope = scope;
        this.favoritesRevision = body.favorites_revision;
        this.favorites = new Set(body.favorites);
      } catch (e) {
        if (serial !== this._favoriteLoadSerial) return;
        this.quickRecordChoices = [];
        this.favoritesError = "Could not load quick-record favorites. Refresh or reconnect in Collect.";
      } finally {
        if (serial === this._favoriteLoadSerial) this.favoritesLoading = false;
      }
    },

    isFavorite(def) {
      return this.favorites.has(def.id);
    },

    get favoritesCount() {
      return this.favorites.size;
    },

    async toggleFavorite(def) {
      if (this.favoritesSaving || !this.quickRecordScope) return;
      const serial = this._favoriteLoadSerial;
      const scope = {...this.quickRecordScope};
      const previous = new Set(this.favorites);
      const pinned = !previous.has(def.id);
      const choice = this.quickRecordChoices.find(choice => choice.id === def.id);
      if (!choice) return;
      const expected = new Set(previous);
      if (pinned) expected.add(def.id); else expected.delete(def.id);
      this.favoritesSaving = true;
      this.favoritesError = "";
      try {
        const body = await api("/api/quick-record/v2", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({...scope, operation: "set_quick_record_favorites",
            choice_id: def.id, ...(pinned ? {choice_digest: choice.digest} : {}),
            pinned, expected_revision: this.favoritesRevision}),
        });
        if (serial !== this._favoriteLoadSerial) return;
        const returnedScope = quickRecordScope(body);
        quickRecordFavorites(body, body.revision);
        if (JSON.stringify(returnedScope) !== JSON.stringify(scope) ||
            body.revision !== this.favoritesRevision + 1 || body.favorites.length !== expected.size ||
            body.favorites.some(id => !expected.has(id))) {
          throw new Error("Favorite response changed the captured intent.");
        }
        this.favorites = new Set(body.favorites);
        this.favoritesRevision = body.revision;
        this.quickRecordChoices = this.quickRecordChoices.map(choice =>
          ({...choice, pinned: this.favorites.has(choice.id)}));
      } catch (e) {
        if (serial !== this._favoriteLoadSerial) return;
        this.favorites = new Set(previous);
        this.quickRecordScope = null;
        this.favoritesError = "Could not verify this pin change. Refresh favorites before retrying.";
      } finally {
        this.favoritesSaving = false;
        // Force Alpine to restore a browser-toggled checkbox after failure.
        this.favorites = new Set(this.favorites);
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
        `Soft-delete the legacy definition '${def.name}'?\n\n` +
        "This hides it from definition lists and new quick-record choices. " +
        "Recorded data, v1 destinations and saved favorites are kept. " +
        "This screen has no Restore control."
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
        // Track management and account-scoped pins have different stores.
        // A deleted choice can retain a hidden pin; reload instead of guessing.
        await this._loadFavorites();
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
