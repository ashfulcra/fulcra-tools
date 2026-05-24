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

function createWizard(plugin_contract, on_complete) {
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
    // File upload state
    uploadedFile: null,
    uploadedFileName: "",
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
    dpDefinitions: [],        // [{id, name, annotation_type, created_at, _preview, _previewLoading}]
    dpSelectedId: null,       // id of chosen definition, or null = create new
    dpForceNew: false,        // true when user chose "Create new instead"

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
        this.step_index += 1;
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
        this.dpSelectedId = null;
        this.dpForceNew = false;
        this._onStepEnter();
      }
    },

    back() {
      this.stepError = "";
      if (this.has_back) {
        this.step_index -= 1;
        this.current_step = this.steps[this.step_index];
        this.healthResult = null;
        this.nextBlocked = false;
        this.extensionConfirmed = false;
        // Reset definition picker so it reloads fresh if the user goes forward again
        this.dpLoading = false;
        this.dpError = "";
        this.dpDefinitions = [];
        this.dpSelectedId = null;
        this.dpForceNew = false;
      }
    },

    // Called after step index advances — trigger auto-actions
    _onStepEnter() {
      if (this.current_step.kind === "test_connection") {
        this.runHealthCheck();
      }
      if (this.current_step.kind === "browser_extension") {
        this.nextBlocked = true;
      }
      if (this.current_step.kind === "definition_picker") {
        // Block Next until the user has made a choice (or explicitly clicked
        // "Create new instead").
        this.nextBlocked = true;
        this._loadDefinitions();
      }
      if (this.current_step.kind === "oauth") {
        // Block Next until the OAuth callback page posts "oauth_complete"
        // back to this window.
        this.nextBlocked = true;
        this.oauthStatus = "";
      }
    },

    // Initiate the OAuth flow for the current step. Calls the start route,
    // then opens the returned authorize_url in a new tab.
    async startOAuth() {
      this.oauthStatus = "opening…";
      try {
        const result = await api(`/api/oauth/${this.plugin_id}/start`, { method: "POST" });
        if (result.authorize_url) {
          window.open(result.authorize_url, "_blank", "noopener,noreferrer");
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
      this.dpSelectedId = null;
      this.dpForceNew = false;

      // The step may hint which annotation_type to filter by (e.g. "duration").
      const annotationType = this.current_step.annotation_type || "duration";
      try {
        const body = await api(`/api/definitions?annotation_type=${encodeURIComponent(annotationType)}`);
        this.dpDefinitions = (body.definitions || []).map(d => ({
          ...d,
          _preview: [],
          _previewLoading: false,
          _previewLoaded: false,
          _previewError: "",
        }));
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

    // Submit a file upload for the current file_upload step
    async _submitFileUpload(step) {
      if (!this.uploadedFile) {
        this.stepError = "Please select a file before continuing.";
        return false;
      }
      const settingKey = (step.settings_keys || [])[0];
      if (!settingKey) return true; // no key declared — just pass through

      try {
        const content = await this._readFileAsBase64(this.uploadedFile);
        await api(`/api/plugin/${this.plugin_id}/settings`, {
          method: "PUT",
          body: JSON.stringify({ [settingKey]: content }),
        });
        return true;
      } catch (e) {
        this.stepError = `Failed to upload file: ${e.message}`;
        return false;
      }
    },

    _readFileAsBase64(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          // reader.result is data:mime;base64,CONTENT
          const base64 = reader.result.split(",")[1] || reader.result;
          resolve(base64);
        };
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
    },
  };
}
