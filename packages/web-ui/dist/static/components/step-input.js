// packages/web-ui/dist/static/components/step-input.js
//
// kind="input" — the most complex of the field-rendering kinds.
// Iterates ctx.input_fields and renders one of four sub-branches per
// field based on field.kind:
//   multiselect → discovered checkbox choices bound to arrays of IDs
//   enum     → <select> with optional enum_labels[]
//   toggle   → <input type=checkbox> bound to "true"/"false" strings
//              (the daemon wants strings, not booleans — see updateField)
//   password / secret → masked input + "currently set — leave blank to
//              keep" placeholder when _credPresent[key] is true
//   default  → text/url/number input (type derived from field.kind)
//
// Mirrors the inline template at index.html ~line 326 (onboarding)
// and ~line 1100 (dashboard Configure). Behaviour parity is what
// matters — the visual classes are copied verbatim.
import { FulcraStepBase, html, nothing } from "./_base.js";

class FulcraStepInput extends FulcraStepBase {
  render() {
    const c = this.ctx;
    const fields = c?.input_fields || [];
    return html`
      <div class="space-y-4">
        ${fields.map((field) => this._renderField(field, c))}
      </div>
    `;
  }

  _renderField(field, c) {
    return html`
      <div>
        <label class="block text-sm font-medium text-slate-700 mb-1">${field.label}</label>
        ${this._renderControl(field, c)}
        ${field.help
          ? html`<p class="text-xs text-slate-500 mt-1">${field.help}</p>`
          : nothing}
      </div>
    `;
  }

  _renderControl(field, c) {
    if (field.kind === "multiselect") {
      const selected = Array.isArray(field.value) ? field.value : [];
      const loading = field.optionsStatus === "loading" || field.optionsStatus === "idle";
      return html`
        <fieldset aria-label=${field.label} aria-busy=${loading ? "true" : "false"}
                  class="border border-slate-300 rounded p-3 space-y-2">
          ${loading ? html`<p role="status" class="text-sm text-slate-500">Loading choices…</p>` : nothing}
          ${field.optionsStatus === "error"
            ? html`<p role="alert" class="text-sm text-red-700">${field.optionsError}</p>` : nothing}
          ${field.optionsStatus === "ready" && !(field.options || []).some(option => !option.unavailable)
            ? html`<p role="status" class="text-sm text-slate-500">No lists available. Check source access, then retry.</p>` : nothing}
          ${(field.options || []).map(option => {
            const checked = selected.includes(option.value);
            return html`
              <label class="flex items-center gap-2 text-sm text-slate-700">
                <input type="checkbox" .checked=${checked}
                       ?disabled=${!checked && (loading || field.optionsStatus !== "ready" || option.disabled || option.unavailable)}
                       @change=${e => c.toggleSelection(field.key, option.value, e.target.checked)}
                       class="h-4 w-4 rounded border-slate-300 text-violet-600">
                <span>${option.label}
                  ${option.unavailable ? html`<span class="text-amber-700"> — unavailable; uncheck to remove</span>` : nothing}
                  ${option.disabled ? html`<span class="text-slate-500"> — read-only</span>` : nothing}
                </span>
              </label>`;
          })}
          <button type="button" @click=${() => c.loadSettingOptions(field.key)}
                  ?disabled=${loading} class="text-sm text-violet-700 underline disabled:opacity-50">
            Retry loading choices
          </button>
        </fieldset>`;
    }

    // enum → select
    if (field.kind === "enum" && field.enum_values) {
      return html`
        <select .value=${field.value || ""}
                @change=${(e) => c.updateField(field.key, e.target.value)}
                class="w-full border border-slate-300 rounded px-3 py-2 text-sm focus:ring-2 focus:ring-violet-500 focus:outline-none">
          ${field.enum_values.map(
            (opt, i) => html`
              <option value=${opt} ?selected=${opt === field.value}>
                ${(field.enum_labels && field.enum_labels[i]) || opt}
              </option>`
          )}
        </select>
      `;
    }

    // toggle → checkbox
    if (field.kind === "toggle") {
      const checked = field.value === "true" || field.value === true;
      return html`
        <label class="inline-flex items-center gap-2 cursor-pointer">
          <input type="checkbox"
                 .checked=${checked}
                 @change=${(e) =>
                   c.updateField(field.key, e.target.checked ? "true" : "false")}
                 class="h-4 w-4 rounded border-slate-300 text-violet-600">
          <span class="text-sm text-slate-600">Enabled</span>
        </label>
      `;
    }

    // password / secret — show the "currently set" hint when the daemon
    // says a credential is already on file for this field key.
    if (field.kind === "password" || field.kind === "secret") {
      const credPresent = (c?._credPresent || {})[field.key];
      const placeholder = credPresent
        ? "(currently set — leave blank to keep)"
        : (field.placeholder || "");
      return html`
        <input type="password"
               .value=${field.value || ""}
               @input=${(e) => c.updateField(field.key, e.target.value)}
               placeholder=${placeholder}
               class="w-full border border-slate-300 rounded px-3 py-2 text-sm focus:ring-2 focus:ring-violet-500 focus:outline-none">
      `;
    }

    // default — text / url / number, type derived from field.kind
    const inputType =
      field.kind === "url" ? "url" : field.kind === "port" ? "number" : "text";
    return html`
      <input type=${inputType}
             .value=${field.value || ""}
             @input=${(e) => c.updateField(field.key, e.target.value)}
             placeholder=${field.placeholder || ""}
             class="w-full border border-slate-300 rounded px-3 py-2 text-sm focus:ring-2 focus:ring-violet-500 focus:outline-none">
    `;
  }
}
customElements.define("fulcra-step-input", FulcraStepInput);
window.FulcraStepComponents.input = "fulcra-step-input";
