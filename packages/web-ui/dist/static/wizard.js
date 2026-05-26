"use strict";

/**
 * wizard.js — SetupStep renderer
 *
 * Usage:
 *   const wizardData = createWizard(plugin_contract, on_complete);
 *   // use in Alpine: x-data="wizardData"
 *
 * The plugin_contract is the JSON from GET /api/plugin/{id}/contract.
 * on_complete() is called when the "done" step is reached and confirmed.
 *
 * Supports step kinds: intro, external_action, input, file_upload,
 * permission_request, browser_extension, test_connection,
 * definition_picker, oauth, done.
 */

// ---------------------------------------------------------------------------
// HTML entity escaper — prevents XSS from plugin-authored body_md content.
// ---------------------------------------------------------------------------

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------------------------------------------------------------------------
// Tiny markdown renderer — handles bold, links, line-breaks, code spans.
// Sufficient for the short, authored body_md strings we use in setup steps.
//
// Security: input is HTML-escaped first, then markdown transforms are applied
// on the safe escaped text. Link URLs are allowlisted to http(s) only — any
// other scheme (javascript:, data:, etc.) is rendered as plain text.
// ---------------------------------------------------------------------------

function renderMd(text) {
  if (!text) return "";
  // Escape HTML entities before any markdown processing so that raw HTML
  // in plugin-authored body_md cannot inject tags or event handlers.
  let s = escapeHtml(text);
  return s
    // Bold: **text** or __text__
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/__(.+?)__/g, "<strong>$1</strong>")
    // Inline code: `text`
    .replace(/`([^`]+)`/g, "<code class=\"bg-slate-100 text-slate-800 px-1 rounded text-sm\">$1</code>")
    // Links: [text](url) — only http(s) URLs are linked; unsafe schemes
    // (javascript:, data:, etc.) are rendered as plain text.
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_m, linkText, url) => {
      if (!/^https?:\/\//i.test(url)) return linkText;
      return `<a href="${url}" target="_blank" rel="noopener noreferrer" class="text-violet-600 underline">${linkText}</a>`;
    })
    // Bare URLs (not already inside an href) — already https? validated by regex
    .replace(/(?<!["\(=])https?:\/\/[^\s<)"]+/g, (url) =>
      `<a href="${url}" target="_blank" rel="noopener noreferrer" class="text-violet-600 underline">${url}</a>`
    )
    // List items: lines starting with "- " or "* "
    .replace(/^[\-\*] (.+)$/gm, "<li class=\"ml-4 list-disc\">$1</li>")
    // Wrap consecutive <li> runs in a <ul>
    .replace(/(<li[^>]*>.*<\/li>\n?)+/g, (block) => `<ul class="my-2 space-y-1">${block}</ul>`)
    // Line breaks (double newline = paragraph break)
    .replace(/\n\n/g, "</p><p class=\"mt-2\">")
    // Single newlines
    .replace(/\n/g, "<br>")
    // Wrap everything
    .replace(/^(.+)/, "<p class=\"mt-2\">$1")
    .replace(/(.+)$/, "$1</p>");
}

// ---------------------------------------------------------------------------
// createWizard — main export
// ---------------------------------------------------------------------------

function createWizard(plugin_contract, on_complete, on_skip_plugin, on_back_to_pick_plugins) {
  const steps = plugin_contract.setup_steps || [];
  const plugin_id = plugin_contract.id;

  // Build lookup maps from the contract for input rendering
  const settingsMap = {};
  for (const s of (plugin_contract.required_settings || [])) {
    settingsMap[s.key] = { ...s, _kind: "setting" };
  }
  const credsMap = {};
  for (const c of (plugin_contract.required_credentials || [])) {
    credsMap[c.key] = { ...c, _kind: "credential", kind: "password" };
  }

  // If no setup_steps, show a graceful placeholder
  if (steps.length === 0) {
    return {
      _noSteps: true,
      plugin_contract,
      pluginName: plugin_contract.name,

      async confirmNoSteps() {
        try {
          await api(`/api/plugin/${plugin_id}/enable`, { method: "POST" });
        } catch (e) {
          console.warn("enable failed:", e);
        }
        on_complete();
      },
    };
  }

  return {
    _noSteps: false,
    plugin_contract,
    plugin_id,
    settingsMap,
    credsMap,
    steps,
    step_index: 0,
    total_steps: steps.length,
    current_step: steps[0],

    // OAuth step state
    oauthStatus: "",
    // Input values collected from "input" steps (key → value)
    inputValues: {},
    // File upload state. uploadProgress is 0–100; uploadInFlight is true
    // while the XHR is mid-stream so the template can render a progress
    // bar (multi-GB Spotify takeouts can take minutes — the old base64-in-
    // memory implementation just OOMed the tab).
    uploadedFile: null,
    uploadedFileName: "",
    uploadProgress: 0,
    uploadInFlight: false,
    // Health check state
    healthResult: null,
    healthChecking: false,
    healthError: "",
    // Error message for current step
    stepError: "",
    // Whether next is allowed (gated on some steps)
    nextBlocked: false,
    // Browser extension confirmed?
    extensionConfirmed: false,
    // Definition picker state
    dpLoading: false,
    dpError: "",
    dpDefinitions: [],        // compatible defs — annotation_type matches current step
    dpOtherDefinitions: [],   // defs present on the account but a different type
    dpShowOther: false,       // whether the "Other annotations" section is expanded
    dpSelectedId: null,       // id of chosen definition, or null = create new
    dpForceNew: false,        // true when user chose "Create new instead"
    // Permission check state (task #66) — backend verifies the OS permission
    // is actually granted, replacing the old "click Next and macOS will
    // prompt you" lie. permissionResult is {granted: bool, hint?: string}.
    permissionResult: null,
    permissionChecking: false,
    // Extension pair state — drives the one-click pairing handshake with
    // the Fulcra Attention browser extension. See _onStepEnter +
    // startExtensionPair below.
    //   "idle"     → button shown, nothing in flight
    //   "pairing"  → POST + postMessage sent, awaiting ack
    //   "success"  → got the ack from the extension
    //   "fallback" → 3 s passed with no ack; show the manual paste-token UI
    pairStatus: "idle",
    pairFallbackToken: null,
    // Once-set callback to drop our postMessage listener on success /
    // step exit, so we don't leak a listener across navigations.
    _pairListenerCleanup: null,
    // True after the user clicks "I pasted it" in fallback mode, which
    // unblocks Next. Kept separate from pairStatus so the fallback UI
    // can still show its instructions.
    pairManuallyConfirmed: false,

    // Alpine 3 calls init() automatically when the component is initialized.
    // We register a postMessage listener here so the OAuth callback tab can
    // signal completion without polling.
    init() {
      const self = this;
      window.addEventListener("message", function (e) {
        // Only accept messages from the same origin — prevents a cross-origin
        // page from spoofing an oauth_complete signal.
        if (e.origin !== window.location.origin) return;
        if (!e.data || e.data.type !== "oauth_complete") return;
        if (e.data.plugin_id !== self.plugin_id) return;
        self.nextBlocked = false;
        self.oauthStatus = "Signed in successfully.";
      });
      // Seed defaults for step 0 if it's an input step. Subsequent steps are
      // seeded by _onStepEnter on next()/back()/skipStep(). Without this,
      // setting/credential defaults declared in the plugin contract render in
      // the UI (the input_fields getter falls back to def.default) but never
      // make it into inputValues — so _submitInputs sees an empty model and
      // throws "required" even though the user saw the default selected.
      this._seedDefaults();
    },

    // Seed inputValues with declared defaults for the current input step.
    // Idempotent: never clobbers a value the user already entered. Called on
    // first init for step 0, and on every step entry via _onStepEnter.
    _seedDefaults() {
      if (this.current_step?.kind !== "input") return;
      const keys = this.current_step.settings_keys || [];
      const next = { ...this.inputValues };
      let changed = false;
      for (const key of keys) {
        if (next[key] !== undefined && next[key] !== null && next[key] !== "") continue;
        const def = this.settingsMap[key] || this.credsMap[key];
        if (def && def.default !== null && def.default !== undefined) {
          next[key] = String(def.default);
          changed = true;
        }
      }
      if (changed) this.inputValues = next;
    },

    get progress_label() {
      return `Step ${this.step_index + 1} of ${this.total_steps}`;
    },
    get progress_pct() {
      return Math.round(((this.step_index + 1) / this.total_steps) * 100);
    },
    get has_back() {
      return this.step_index > 0;
    },
    // True when this wizard was created with an on_back_to_pick_plugins
    // callback (i.e. it's running inside the onboarding multi-plugin walk,
    // not the single-plugin Configure flow from the dashboard). The footer
    // uses this to decide whether to show "← Plugin list" on step 0.
    get has_back_to_pick_plugins() {
      return typeof on_back_to_pick_plugins === "function";
    },
    get has_next() {
      return this.step_index < this.total_steps - 1;
    },
    get is_done_step() {
      return this.current_step.kind === "done";
    },

    // Rendered HTML for body_md
    get body_html() {
      return renderMd(this.current_step.body_md || "");
    },

    // The Setting/Credential definitions for the current "input" step
    get input_fields() {
      if (this.current_step.kind !== "input") return [];
      return (this.current_step.settings_keys || []).map(key => {
        const def = this.settingsMap[key] || this.credsMap[key] || { key, label: key, kind: "text", _kind: "setting" };
        return {
          key,
          label: def.label || key,
          kind: def.kind || "text",
          help: def.help || "",
          placeholder: def.placeholder || "",
          enum_values: def.enum_values || null,
          // Optional positional labels for enum_values — see Setting.enum_labels
          // on the Python side. When absent, the renderer falls back to the
          // raw value as the label.
          enum_labels: def.enum_labels || null,
          _kind: def._kind || "setting",
          value: this.inputValues[key] ?? (def.default !== null && def.default !== undefined ? String(def.default) : ""),
        };
      });
    },

    updateField(key, val) {
      this.inputValues = { ...this.inputValues, [key]: val };
    },

    onFileChange(event) {
      const file = event.target?.files?.[0];
      if (!file) return;
      this.uploadedFile = file;
      this.uploadedFileName = file.name;
    },

    // Called when user enters the test_connection step
    async runHealthCheck() {
      this.healthChecking = true;
      this.healthResult = null;
      this.healthError = "";
      this.nextBlocked = true;
      try {
        const result = await api(`/api/plugin/${this.plugin_id}/health_check`, { method: "POST" });
        this.healthResult = result;
        this.nextBlocked = !result.ok;
        if (!result.ok) {
          this.healthError = result.summary || "Health check failed.";
        }
      } catch (e) {
        this.healthError = e.message;
        this.nextBlocked = true;
      } finally {
        this.healthChecking = false;
      }
    },

    async next() {
      this.stepError = "";
      const step = this.current_step;

      if (step.kind === "input") {
        const ok = await this._submitInputs(step);
        if (!ok) return;
      }

      if (step.kind === "file_upload") {
        const ok = await this._submitFileUpload(step);
        if (!ok) return;
      }

      if (step.kind === "definition_picker") {
        const ok = await this._submitDefinitionPick();
        if (!ok) return;
      }

      if (step.kind === "done") {
        // Enable the plugin then call the completion callback
        try {
          await api(`/api/plugin/${this.plugin_id}/enable`, { method: "POST" });
        } catch (e) {
          console.warn("enable failed:", e);
        }
        on_complete();
        return;
      }

      if (this.has_next) {
        // Advance, skipping any steps whose condition isn't satisfied.
        let nextIdx = this.step_index + 1;
        while (nextIdx < this.steps.length && !this._stepConditionMet(nextIdx)) {
          nextIdx += 1;
        }
        if (nextIdx >= this.steps.length) {
          // Skipped past the end — treat as "at the last real step"; let the
          // existing has_next / done-step logic handle completion on the next
          // next() call. In practice this shouldn't happen for a well-formed
          // wizard (there's always an unconditional "done" step).
          return;
        }
        this.step_index = nextIdx;
        this.current_step = this.steps[this.step_index];
        this.healthResult = null;
        this.healthChecking = false;
        this.healthError = "";
        this.nextBlocked = false;
        this.extensionConfirmed = false;
        this.oauthStatus = "";
        // Reset definition picker state for clean entry into the next step
        this.dpLoading = false;
        this.dpError = "";
        this.dpDefinitions = [];
        this.dpOtherDefinitions = [];
        this.dpShowOther = false;
        this.dpSelectedId = null;
        this.dpForceNew = false;
        // Reset permission check transient state
        this.permissionResult = null;
        this.permissionChecking = false;
        // Reset extension-pair transient state so a re-enter starts clean
        this._resetPairState();
        this._onStepEnter();
      }
    },

    back() {
      this.stepError = "";
      if (this.has_back) {
        // Retreat, skipping any steps whose condition isn't satisfied.
        let prevIdx = this.step_index - 1;
        while (prevIdx > 0 && !this._stepConditionMet(prevIdx)) {
          prevIdx -= 1;
        }
        this.step_index = prevIdx;
        this.current_step = this.steps[this.step_index];
        this.healthResult = null;
        this.nextBlocked = false;
        this.extensionConfirmed = false;
        // Reset definition picker so it reloads fresh if the user goes forward again
        this.dpLoading = false;
        this.dpError = "";
        this.dpDefinitions = [];
        this.dpOtherDefinitions = [];
        this.dpShowOther = false;
        this.dpSelectedId = null;
        this.dpForceNew = false;
        // Reset permission check transient state
        this.permissionResult = null;
        this.permissionChecking = false;
        // Reset extension-pair transient state
        this._resetPairState();
        // Re-run any auto-actions for the step we just landed on (e.g. a
        // permission_request step should re-verify on return).
        this._onStepEnter();
      }
    },

    // Skip the current step without submitting / validating. Use as an escape
    // hatch when something is stuck (e.g. an OAuth tab won't open, an API is
    // down). The user can return via Back if they change their mind.
    skipStep() {
      this.stepError = "";
      if (this.has_next) {
        this.step_index += 1;
        this.current_step = this.steps[this.step_index];
        this.healthResult = null;
        this.healthChecking = false;
        this.healthError = "";
        this.nextBlocked = false;
        this.extensionConfirmed = false;
        this.oauthStatus = "";
        this.dpLoading = false;
        this.dpError = "";
        this.dpDefinitions = [];
        this.dpOtherDefinitions = [];
        this.dpShowOther = false;
        this.dpSelectedId = null;
        this.dpForceNew = false;
        this.permissionResult = null;
        this.permissionChecking = false;
        this._resetPairState();
        this._onStepEnter();
      } else {
        // Last step — skipping past "Done" means skip-plugin.
        if (on_skip_plugin) on_skip_plugin();
      }
    },

    // Abandon the current plugin's setup entirely. Plugin is NOT enabled.
    // Onboarding advances to the next plugin (or to the done screen).
    skipPlugin() {
      if (on_skip_plugin) on_skip_plugin();
    },

    // Bail out of the current plugin's wizard and return to the
    // pick_plugins screen so the user can adjust which plugins they
    // selected. Only meaningful from inside the onboarding multi-plugin
    // walk; the single-plugin Configure flow leaves this callback unset.
    // (task #64)
    backToPluginList() {
      if (on_back_to_pick_plugins) on_back_to_pick_plugins();
    },

    // Returns true when the step at `index` has no condition, or when its
    // condition is satisfied by the current inputValues. A key that is absent
    // from inputValues (undefined / not yet set) counts as NOT satisfied so
    // conditional steps that depend on earlier input steps are reliably
    // skipped until the user actually fills in the gating field.
    _stepConditionMet(index) {
      const step = this.steps[index];
      if (!step || !step.condition) return true;
      for (const [key, acceptable] of Object.entries(step.condition)) {
        const val = this.inputValues[key];
        if (val === undefined || val === null || val === "") return false;
        if (!acceptable.includes(val)) return false;
      }
      return true;
    },

    // Called after step index advances — trigger auto-actions
    _onStepEnter() {
      // Seed declared defaults into inputValues so the validator agrees with
      // what the user sees in the rendered field (e.g. Day One mode dropdown
      // showed live_app but _submitInputs read inputValues[key] as undefined
      // and threw "Mode is required").
      this._seedDefaults();
      if (this.current_step.kind === "test_connection") {
        this.runHealthCheck();
      }
      if (this.current_step.kind === "browser_extension") {
        this.nextBlocked = true;
      }
      if (this.current_step.kind === "definition_picker") {
        // Block Next until the user has made a choice (or explicitly clicked
        // "Create new instead"). _loadDefinitions will auto-unblock when
        // there are no matching defs (task #67).
        this.nextBlocked = true;
        this._loadDefinitions();
      }
      if (this.current_step.kind === "oauth") {
        // Block Next until the OAuth callback page posts "oauth_complete"
        // back to this window.
        this.nextBlocked = true;
        this.oauthStatus = "";
      }
      if (this.current_step.kind === "permission_request") {
        // If the backend can verify this permission, block Next until the
        // user has clicked Verify (or until the auto-check below grants it).
        // Otherwise fall back to the old behaviour: the user reads the
        // instructions and clicks Next when they've done it manually.
        if (this.plugin_contract.permission_check_available) {
          this.nextBlocked = true;
          this.checkPermission();
        } else {
          this.nextBlocked = false;
        }
      }
      if (this.current_step.kind === "extension_pair") {
        // Block Next until the handshake succeeds OR the user clicks
        // "I pasted it" in the fallback UI. Do NOT auto-start the pair
        // attempt — the user clicks the button so the extension's
        // content script runs in response to a user gesture (some
        // future browsers may want that for postMessage permissions).
        this.nextBlocked = true;
        this._resetPairState();
      }
    },

    // ---------------------------------------------------------------------
    // Extension pairing
    // ---------------------------------------------------------------------

    _resetPairState() {
      this.pairStatus = "idle";
      this.pairFallbackToken = null;
      this.pairManuallyConfirmed = false;
      if (this._pairListenerCleanup) {
        try { this._pairListenerCleanup(); } catch (_) { /* ignore */ }
        this._pairListenerCleanup = null;
      }
    },

    async startExtensionPair() {
      this.stepError = "";
      this.pairStatus = "pairing";
      let resp;
      try {
        resp = await api(`/api/plugin/attention-relay/pair`, { method: "POST" });
      } catch (e) {
        this.pairStatus = "fallback";
        this.stepError = `Could not generate pairing token: ${e.message}`;
        return;
      }
      const token = resp.token;
      const daemonUrl = resp.daemon_url;
      if (!token || !daemonUrl) {
        this.pairStatus = "fallback";
        this.stepError = "Pairing route returned an incomplete response.";
        return;
      }
      // Keep the token around so the fallback UI can show it.
      this.pairFallbackToken = token;

      const self = this;
      const onMessage = (e) => {
        // Only accept messages from this same origin — prevents a
        // cross-origin page (or iframe) from spoofing an ack.
        if (e.origin !== window.location.origin) return;
        const d = e.data;
        if (!d || d.type !== "fulcra-attention-pair-ack" || d.ok !== true) return;
        self.pairStatus = "success";
        self.nextBlocked = false;
        self._cleanupPairListener();
      };
      window.addEventListener("message", onMessage);
      this._pairListenerCleanup = () => {
        window.removeEventListener("message", onMessage);
      };

      // Post AFTER the listener is registered so we never miss the ack.
      window.postMessage(
        { type: "fulcra-attention-pair", token, daemonUrl },
        "*",
      );

      // 3 s fallback. If we're still pairing, switch to manual paste UI.
      setTimeout(() => {
        if (self.pairStatus === "pairing") {
          self.pairStatus = "fallback";
        }
      }, 3000);
    },

    _cleanupPairListener() {
      if (this._pairListenerCleanup) {
        try { this._pairListenerCleanup(); } catch (_) { /* ignore */ }
        this._pairListenerCleanup = null;
      }
    },

    // Called by the "I pasted it" button in the fallback UI. The user
    // has manually entered the token in the extension's options page;
    // we trust them and unblock Next.
    confirmManualPair() {
      this.pairManuallyConfirmed = true;
      this.nextBlocked = false;
    },

    async copyPairToken() {
      if (!this.pairFallbackToken) return;
      try {
        await navigator.clipboard.writeText(this.pairFallbackToken);
      } catch (_) {
        // navigator.clipboard can fail in some contexts; fall back to a
        // selection range so the user can ⌘-C manually. The UI also
        // renders the token in a <code> block as a final fallback.
      }
    },

    // Initiate the OAuth flow for the current step. Calls the start route,
    // then opens the returned authorize_url in a new tab.
    //
    // NOTE: we deliberately do NOT pass "noopener" here, even though it's
    // the usual safe-default for window.open. The OAuth callback page is
    // served by THIS daemon at the same origin (127.0.0.1:9292/api/oauth/
    // {plugin}/callback), and it needs `window.opener.postMessage(...)` to
    // tell the wizard tab that sign-in completed. With noopener, opener
    // is null, the postMessage no-ops silently, and the user sits at
    // "Waiting for sign-in…" forever (bug seen live 2026-05-26). The
    // tradeoff is safe because we control the callback page; a third-
    // party page can't end up holding window.opener.
    async startOAuth() {
      this.oauthStatus = "opening…";
      try {
        const result = await api(`/api/oauth/${this.plugin_id}/start`, { method: "POST" });
        if (result.authorize_url) {
          window.open(result.authorize_url, "_blank");
          this.oauthStatus = "Waiting for sign-in… (complete it in the new tab)";
        } else {
          this.oauthStatus = "Error: server did not return an authorize_url.";
        }
      } catch (e) {
        this.oauthStatus = `Error: ${e.message}`;
      }
    },

    // ---------------------------------------------------------------------------
    // Definition picker — Phase E
    // ---------------------------------------------------------------------------

    async _loadDefinitions() {
      this.dpLoading = true;
      this.dpError = "";
      this.dpDefinitions = [];
      this.dpOtherDefinitions = [];
      this.dpShowOther = false;
      this.dpSelectedId = null;
      this.dpForceNew = false;

      // The step hints which annotation_type is compatible (e.g. "duration").
      // We fetch ALL defs and partition client-side so the user can see and
      // optionally pick from their other-type annotations too.
      const annotationType = this.current_step.annotation_type || "duration";
      try {
        const body = await api(`/api/definitions`);
        const allDefs = (body.definitions || []).map(d => ({
          ...d,
          _preview: [],
          _previewLoading: false,
          _previewLoaded: false,
          _previewError: "",
        }));
        // Stable display order: alphabetical by name (case-insensitive),
        // then oldest-first by created_at as a tiebreaker so duplicate-
        // named defs (e.g. multiple "Listened" from different machines)
        // group together with the canonical original first.
        const sortByNameThenCreated = (a, b) => {
          const na = (a.name || "").toLowerCase();
          const nb = (b.name || "").toLowerCase();
          if (na < nb) return -1;
          if (na > nb) return 1;
          const ca = a.created_at || "";
          const cb = b.created_at || "";
          if (ca < cb) return -1;
          if (ca > cb) return 1;
          return 0;
        };
        allDefs.sort(sortByNameThenCreated);
        this.dpDefinitions = allDefs.filter(d => d.annotation_type === annotationType);
        this.dpOtherDefinitions = allDefs.filter(d => d.annotation_type !== annotationType);
        // Expand "other" section by default only when there are no compatible defs.
        this.dpShowOther = this.dpDefinitions.length === 0 && this.dpOtherDefinitions.length > 0;
        // task #67 — if there are no defs at all, auto-select "create new"
        // and unblock Next so the user isn't stuck staring at a disabled button.
        if (allDefs.length === 0) {
          this.dpForceNew = true;
          this.nextBlocked = false;
        }
      } catch (e) {
        this.dpError = e.message;
      } finally {
        this.dpLoading = false;
      }
    },

    async dpLoadPreview(def) {
      if (def._previewLoaded || def._previewLoading) return;
      def._previewLoading = true;
      def._previewError = "";
      try {
        const body = await api(`/api/definitions/${encodeURIComponent(def.id)}/recent?limit=3`);
        def._preview = body.entries || [];
        def._previewLoaded = true;
      } catch (e) {
        def._previewError = e.message;
      } finally {
        def._previewLoading = false;
      }
    },

    dpSelectDef(def) {
      this.dpSelectedId = def.id;
      this.dpForceNew = false;
      this.nextBlocked = false;
      // Eagerly load preview when the user selects a definition
      this.dpLoadPreview(def);
    },

    dpChooseForceNew() {
      this.dpSelectedId = null;
      this.dpForceNew = true;
      this.nextBlocked = false;
    },

    dpHumanDate(isoString) {
      if (!isoString) return "";
      try {
        return new Date(isoString).toLocaleDateString(undefined, {
          year: "numeric", month: "short", day: "numeric",
        });
      } catch (_) {
        return isoString;
      }
    },

    dpEntryLabel(entry) {
      // Try to extract a human label from a Fulcra event record
      const rat = (entry.metadata || {}).recorded_at;
      if (!rat) return "(no timestamp)";
      if (typeof rat === "string") return rat.slice(0, 10);
      if (rat.start_time) return rat.start_time.slice(0, 10);
      return "(unknown date)";
    },

    // Submit the definition pick to the daemon
    async _submitDefinitionPick() {
      try {
        if (this.dpForceNew) {
          await api(`/api/plugin/${this.plugin_id}/definition`, {
            method: "POST",
            body: JSON.stringify({ force_new: true }),
          });
        } else if (this.dpSelectedId) {
          await api(`/api/plugin/${this.plugin_id}/definition`, {
            method: "POST",
            body: JSON.stringify({ definition_id: this.dpSelectedId }),
          });
        }
        return true;
      } catch (e) {
        this.stepError = `Failed to save definition choice: ${e.message}`;
        return false;
      }
    },

    confirmExtension() {
      this.extensionConfirmed = true;
      this.nextBlocked = false;
    },

    // ---------------------------------------------------------------------------
    // Permission check (task #66) — POSTs to the backend, which runs the
    // plugin's permission_check probe and reports whether the OS actually
    // granted access. Wired to the "Verify access" button and auto-invoked
    // on permission_request step entry.
    // ---------------------------------------------------------------------------

    async checkPermission() {
      this.permissionChecking = true;
      this.permissionResult = null;
      try {
        const result = await api(`/api/plugin/${this.plugin_id}/check_permission`, {
          method: "POST",
        });
        this.permissionResult = result;
        if (result.granted === true) {
          this.nextBlocked = false;
        } else {
          this.nextBlocked = true;
        }
      } catch (e) {
        // 404 (no permission_check on this plugin) or transport error —
        // surface as an un-granted result with the error message as the hint
        // so the user sees something rather than a silent failure.
        this.permissionResult = { granted: false, hint: e.message };
        this.nextBlocked = true;
      } finally {
        this.permissionChecking = false;
      }
    },

    // Map a permission id to the macOS Privacy pane deep-link, when one
    // exists. The template uses x-show on the returned value so unknown /
    // null ids simply hide the button.
    permissionDeepLink(permId) {
      const links = {
        "full-disk-access": "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
        "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        "automation": "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
        "network-loopback-server": null,
      };
      return links[permId] || null;
    },

    // The current setup step doesn't carry a permission_id field yet, so we
    // fall back to the plugin's first declared required_permission. Most
    // plugins only declare one permission, which makes this unambiguous.
    // If/when SetupStep grows a permission_id, prefer that.
    get current_permission_id() {
      if (this.current_step && this.current_step.permission_id) {
        return this.current_step.permission_id;
      }
      const perms = this.plugin_contract.required_permissions || [];
      return perms.length > 0 ? perms[0].id : null;
    },

    // Submit all input fields for the current input step
    async _submitInputs(step) {
      const fields = this.input_fields;
      for (const f of fields) {
        const val = this.inputValues[f.key] ?? "";
        if (!val && (this.settingsMap[f.key]?.required !== false)) {
          this.stepError = `"${f.label}" is required.`;
          return false;
        }
        try {
          if (f._kind === "credential") {
            await api(`/api/plugin/${this.plugin_id}/credential/${f.key}`, {
              method: "PUT",
              body: JSON.stringify({ secret: val }),
            });
          } else {
            // Setting — submit via plugin settings endpoint
            await api(`/api/plugin/${this.plugin_id}/settings`, {
              method: "PUT",
              body: JSON.stringify({ [f.key]: val }),
            });
          }
        } catch (e) {
          this.stepError = `Failed to save "${f.label}": ${e.message}`;
          return false;
        }
      }
      return true;
    },

    // Submit a file upload for the current file_upload step.
    //
    // Streams the file to the daemon as multipart/form-data via XHR (not
    // fetch) so we can surface upload-progress events for multi-GB
    // takeouts. The daemon route writes the bytes to disk under
    // ~/.config/fulcra-collect/uploads/<plugin>/<filename> and persists
    // the resulting absolute path into the plugin's settings — which is
    // what every plugin's run() already expects to read from
    // ctx.config[<key>]. (The previous implementation base64-encoded the
    // file in the browser and stuffed the blob into the setting value
    // directly; plugins crashed trying to resolve the blob as a path, and
    // for large takeouts the tab OOMed during encoding.)
    async _submitFileUpload(step) {
      if (!this.uploadedFile) {
        this.stepError = "Please select a file before continuing.";
        return false;
      }
      const settingKey = (step.settings_keys || [])[0];
      if (!settingKey) return true; // no key declared — just pass through

      this.uploadProgress = 0;
      this.uploadInFlight = true;
      try {
        const fd = new FormData();
        fd.append("file", this.uploadedFile);
        const url = `/api/plugin/${this.plugin_id}/upload?key=${encodeURIComponent(settingKey)}`;
        const result = await new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open("POST", url, true);
          xhr.setRequestHeader("Authorization", `Bearer ${apiToken()}`);
          xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
              this.uploadProgress = Math.round((e.loaded / e.total) * 100);
            }
          };
          xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) {
              try {
                resolve(JSON.parse(xhr.responseText));
              } catch (err) {
                reject(new Error(`bad JSON response: ${err.message}`));
              }
            } else {
              // Try to surface FastAPI's "detail" field so users see the
              // backend's user-readable message ("invalid filename", etc.)
              // instead of a bare HTTP status line.
              let detail = "";
              try {
                const body = JSON.parse(xhr.responseText);
                detail = body.detail || body.error || "";
              } catch (_) { /* ignore */ }
              reject(new Error(detail || `HTTP ${xhr.status}: ${xhr.responseText}`));
            }
          };
          xhr.onerror = () => reject(new Error("network error"));
          xhr.send(fd);
        });
        if (!result.ok) {
          this.stepError = result.error || "Upload failed.";
          return false;
        }
        return true;
      } catch (e) {
        this.stepError = `Upload failed: ${e.message}`;
        return false;
      } finally {
        this.uploadInFlight = false;
      }
    },
  };
}
