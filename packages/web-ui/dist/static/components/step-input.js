// packages/web-ui/dist/static/components/step-input.js
//
// kind="input" — the most complex of the field-rendering kinds.
// Iterates ctx.input_fields and renders one of four sub-branches per
// field based on field.kind:
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
