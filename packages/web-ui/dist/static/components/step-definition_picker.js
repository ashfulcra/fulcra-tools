// packages/web-ui/dist/static/components/step-definition_picker.js
//
// kind="definition_picker" — the biggest component (~150 lines) and the
// original drift offender that motivated this refactor. Site A and site
// B had explicitly diverged versions, with a "if you change one, change
// the other" comment that the next person reliably ignored. After this
// commit the component file is the single source of truth.
//
// Composition (matching the richer onboarding-site template at
// index.html ~line 593):
//   1. body markdown header
//   2. loading / error states
//   3. compatible-defs list (dpDefinitions) with click → dpSelectDef(def)
//      and an expanded "recent entries" preview when a def is selected
//   4. empty-state hint when no defs at all
//   5. "Other annotations (different type)" collapsible (dpOtherDefinitions),
//      gated by dpShowOther
//   6. "Create new instead" footer with editable name input — visible only
//      when there are any defs to choose between; dpForceNew toggles the
//      button colour + reveals the name input
//
// Inputs (from ctx):
//   body_html, dpLoading, dpError, dpDefinitions, dpOtherDefinitions,
//   dpSelectedId, dpShowOther, dpForceNew, dpNewName,
//   plugin_contract.canonical_definition_name
// Helpers / events (from ctx):
//   dpSelectDef(def), dpEntryLabel(entry), dpHumanDate(s), dpChooseForceNew()
//
// Mirrors site A (index.html ~line 593) AND replaces site B (~line 1281)
// — Phase 3 deletes both inline blocks.
import { FulcraStepBase, html, nothing } from "./_base.js";
import { unsafeHTML } from "https://cdn.jsdelivr.net/gh/lit/dist@3.2.1/all/lit-all.min.js";

class FulcraStepDefinitionPicker extends FulcraStepBase {
  render() {
    const c = this.ctx;
    const bodyHtml = c?.body_html || "";
    const loading = c?.dpLoading;
    const error = c?.dpError;
    const defs = c?.dpDefinitions || [];
    const otherDefs = c?.dpOtherDefinitions || [];

    return html`
      <div class="space-y-4">
        <div class="prose prose-sm text-slate-700 max-w-none">
          ${unsafeHTML(bodyHtml)}
        </div>

        ${loading
          ? html`<div class="text-slate-400 text-sm animate-pulse">Loading your Fulcra annotations…</div>`
          : nothing}

        ${error && !loading
          ? html`<div class="text-red-600 text-sm bg-red-50 border border-red-200 rounded p-3">${error}</div>`
          : nothing}

        ${!loading && defs.length > 0
          ? html`
              <div class="space-y-2">
                <p class="text-xs text-slate-500 font-medium">Choose an existing annotation or create a new one:</p>
                ${defs.map((def) => this._renderDefCard(def, c, /*other=*/ false))}
              </div>`
          : nothing}

        ${!loading && defs.length === 0 && otherDefs.length === 0 && !error
          ? html`
              <div class="text-sm text-slate-500 bg-slate-50 border border-slate-200 rounded p-3">
                No Fulcra annotations found. A new one will be created on first run.
              </div>`
          : nothing}

        ${!loading && otherDefs.length > 0 ? this._renderOtherDefs(otherDefs, c) : nothing}

        ${defs.length > 0 || otherDefs.length > 0 ? this._renderCreateNewFooter(c) : nothing}
      </div>
    `;
  }

  _renderDefCard(def, c, other) {
    const selected = c.dpSelectedId === def.id;
    // Outer card classes: violet when selected, neutral otherwise
    const cardClasses = selected
      ? "border rounded-lg p-3 cursor-pointer transition-colors border-violet-400 bg-violet-50"
      : "border rounded-lg p-3 cursor-pointer transition-colors border-slate-200 hover:border-slate-300";
    // Subtitle for "other" defs is muted slate-400 and parenthesises the
    // type label (because the type doesn't match what the plugin expects)
    const subtitleClasses = other
      ? "text-xs text-slate-400 mt-0.5"
      : "text-xs text-slate-500 mt-0.5";
    const subtitleText = other
      ? `(${def.annotation_type} annotation) · created ${c.dpHumanDate(def.created_at)}`
      : `${def.annotation_type} · created ${c.dpHumanDate(def.created_at)}`;

    return html`
      <div class=${cardClasses} @click=${() => c.dpSelectDef(def)}>
        <div class="flex items-start justify-between gap-2">
          <div class="flex-1 min-w-0">
            <div class="font-medium text-sm text-slate-900">${def.name}</div>
            <div class=${subtitleClasses}>${subtitleText}</div>
          </div>
          ${selected
            ? html`
                <svg class="w-5 h-5 text-violet-600 shrink-0 mt-0.5" fill="currentColor" viewBox="0 0 20 20">
                  <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/>
                </svg>`
            : nothing}
        </div>

        ${selected && !other ? this._renderPreview(def, c) : nothing}
      </div>
    `;
  }

  _renderPreview(def, c) {
    return html`
      <div class="mt-2 border-t border-violet-100 pt-2">
        ${def._previewLoading
          ? html`<div class="text-xs text-slate-400 animate-pulse">Loading recent entries…</div>`
          : nothing}
        ${def._previewError
          ? html`<div class="text-xs text-red-500">${def._previewError}</div>`
          : nothing}
        ${!def._previewLoading && def._preview && def._preview.length === 0 && def._previewLoaded
          ? html`<div class="text-xs text-slate-400">No recent entries found.</div>`
          : nothing}
        ${def._preview && def._preview.length > 0
          ? html`
              <div class="space-y-0.5">
                <div class="text-xs text-slate-500 font-medium mb-1">Recent entries:</div>
                ${def._preview.map(
                  (entry) => html`
                    <div class="text-xs text-slate-600 truncate">${c.dpEntryLabel(entry)}</div>`
                )}
              </div>`
          : nothing}
      </div>
    `;
  }

  _renderOtherDefs(otherDefs, c) {
    return html`
      <div class="space-y-2 border-t border-slate-100 pt-3">
        <button @click=${() => (c.dpShowOther = !c.dpShowOther)}
                class="flex items-center gap-1.5 text-xs font-medium text-slate-500 hover:text-slate-700 transition-colors">
          <svg class=${"w-3.5 h-3.5 transition-transform " + (c.dpShowOther ? "rotate-90" : "")} viewBox="0 0 12 12" fill="currentColor">
            <path d="M4.5 2 L9 6 L4.5 10" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          Other annotations (<span>${otherDefs.length}</span>, different type)
        </button>
        ${c.dpShowOther
          ? html`
              <div class="space-y-1.5">
                <p class="text-xs text-slate-400">These annotations exist on your account but are a different type from what this plugin expects. Picking one may cause events to not render correctly.</p>
                ${otherDefs.map((def) => this._renderDefCard(def, c, /*other=*/ true))}
              </div>`
          : nothing}
      </div>
    `;
  }

  _renderCreateNewFooter(c) {
    const canonical = c.plugin_contract?.canonical_definition_name;
    const buttonClass = c.dpForceNew
      ? "px-4 py-2 rounded border text-sm font-medium transition-colors border-emerald-500 bg-emerald-50 text-emerald-700"
      : "px-4 py-2 rounded border text-sm font-medium transition-colors border-slate-300 text-slate-700 hover:bg-slate-50 hover:border-slate-400";
    const label = c.dpForceNew
      ? canonical
        ? `✓ Will create a new '${canonical}' annotation on first run`
        : "✓ Will create a new annotation on first run"
      : canonical
        ? `+ Create a new '${canonical}' annotation`
        : "+ Create a new annotation";

    return html`
      <div class="pt-1">
        <!-- Outline button — promoted from a text link 2026-05-26 so the
             "create new" path is as visually weighty as the cards above. -->
        <button @click=${() => c.dpChooseForceNew()} class=${buttonClass}>
          <span>${label}</span>
        </button>
        ${c.dpForceNew
          ? html`
              <div class="pt-2">
                <label class="block text-xs text-slate-500 mb-1">Annotation name (you can change this):</label>
                <input type="text"
                       .value=${c.dpNewName || ""}
                       @input=${(e) => (c.dpNewName = e.target.value)}
                       class="w-full px-3 py-2 text-sm border border-slate-300 rounded focus:outline-none focus:border-violet-500"
                       placeholder=${canonical || "My annotation"}>
              </div>`
          : nothing}
      </div>
    `;
  }
}
customElements.define("fulcra-step-definition_picker", FulcraStepDefinitionPicker);
window.FulcraStepComponents.definition_picker = "fulcra-step-definition_picker";
