// kind="folder_picker" — explicit native macOS folder selection.
import { FulcraStepBase, html, nothing } from "./_base.js";
import { unsafeHTML } from "/static/vendor/lit-3.2.1.min.js";

class FulcraStepFolderPicker extends FulcraStepBase {
  render() {
    const c = this.ctx;
    return html`
      <div class="space-y-4">
        <div class="prose prose-sm text-slate-700 max-w-none">
          ${unsafeHTML(c?.body_html || "")}
        </div>
        <button type="button"
                @click=${() => c?.chooseFolder()}
                ?disabled=${c?.folderPicking}
                class="px-4 py-2 rounded bg-violet-600 text-white hover:bg-violet-700 disabled:opacity-50">
          ${c?.folderPicking ? "Opening folder picker…" : "Choose folder"}
        </button>
        ${c?.selectedFolderName
          ? html`<div class="rounded border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
              Selected: <strong>${c.selectedFolderName}</strong>
            </div>`
          : nothing}
      </div>
    `;
  }
}

customElements.define("fulcra-step-folder_picker", FulcraStepFolderPicker);
window.FulcraStepComponents.folder_picker = "fulcra-step-folder_picker";
