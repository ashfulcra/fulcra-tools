// packages/web-ui/dist/static/components/step-file_upload.js
//
// kind="file_upload" — drag-and-drop styled file picker. Calls
// ctx.onFileChange(event) when the user picks a file; the wizard then
// streams the upload to the daemon and reports back through
// ctx.uploadInFlight / ctx.uploadProgress (0-100) / ctx.uploadedFileName.
//
// Mirrors index.html ~line 402 (onboarding) and ~line 1152 (dashboard).
//
// Note: the inline templates used hard-coded ids ("fileUploadInput" at
// site A, "setupFileUploadInput" at site B) for the <label for=...>
// link. With light DOM we can have both sites mounted in the page tree
// (unlikely but possible during transitions). Use a per-instance UUID id
// so the label/input link survives concurrent mounts and so two of these
// components never share an id.
import { FulcraStepBase, html, nothing } from "./_base.js";
import { unsafeHTML } from "https://cdn.jsdelivr.net/gh/lit/dist@3.2.1/all/lit-all.min.js";

class FulcraStepFileUpload extends FulcraStepBase {
  constructor() {
    super();
    // crypto.randomUUID is available everywhere we run (Electron + modern
    // browsers). Stable per component instance — Lit doesn't tear the
    // DOM down across reactive updates, so this id stays put.
    this._inputId = "file-" + crypto.randomUUID();
  }

  render() {
    const c = this.ctx;
    const bodyHtml = c?.body_html || "";
    const uploadedFileName = c?.uploadedFileName;
    const inFlight = c?.uploadInFlight;
    const progress = c?.uploadProgress ?? 0;
    return html`
      <div class="space-y-4">
        <div class="prose prose-sm text-slate-700 max-w-none">
          ${unsafeHTML(bodyHtml)}
        </div>
        <div class="border-2 border-dashed border-slate-300 rounded-lg p-6 text-center hover:border-violet-400 transition-colors">
          <input type="file"
                 @change=${(e) => c?.onFileChange(e)}
                 class="hidden"
                 id=${this._inputId}>
          <label for=${this._inputId} class="cursor-pointer">
            <div class="text-slate-400 mb-2">
              <svg class="w-8 h-8 mx-auto" viewBox="0 0 24 24" fill="none" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
                      d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M16 8l-4-4-4 4M12 4v12"/>
              </svg>
            </div>
            <p class="text-sm text-slate-600">
              <span class="text-violet-600 font-medium">Click to choose a file</span>
              or drag and drop
            </p>
            ${uploadedFileName
              ? html`<p class="text-sm text-slate-800 mt-2 font-medium">${uploadedFileName}</p>`
              : nothing}
          </label>
        </div>
        ${inFlight
          ? html`
              <div class="space-y-1">
                <div class="h-2 bg-slate-200 rounded">
                  <div class="h-2 bg-violet-600 rounded transition-all"
                       style=${`width: ${progress}%`}></div>
                </div>
                <p class="text-xs text-slate-500">${`Uploading… ${progress}%`}</p>
              </div>`
          : nothing}
      </div>
    `;
  }
}
customElements.define("fulcra-step-file_upload", FulcraStepFileUpload);
window.FulcraStepComponents.file_upload = "fulcra-step-file_upload";
