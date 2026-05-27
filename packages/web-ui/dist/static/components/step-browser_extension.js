// packages/web-ui/dist/static/components/step-browser_extension.js
//
// kind="browser_extension" — body markdown + "Install Extension" link
// (when step.extension_url is set) + an "I've installed the extension"
// confirmation checkbox bound to ctx.extensionConfirmed via
// ctx.confirmExtension().
//
// Mirrors index.html ~line 487 (onboarding) and ~line 1222 (dashboard).
import { FulcraStepBase, html, nothing } from "./_base.js";
import { unsafeHTML } from "https://cdn.jsdelivr.net/npm/lit@3.2.1/directives/unsafe-html.js";

class FulcraStepBrowserExtension extends FulcraStepBase {
  render() {
    const s = this.step || {};
    const c = this.ctx;
    const bodyHtml = c?.body_html || "";
    return html`
      <div class="space-y-4">
        <div class="prose prose-sm text-slate-700 max-w-none">
          ${unsafeHTML(bodyHtml)}
        </div>
        ${s.extension_url
          ? html`
              <a href=${s.extension_url}
                 target="_blank" rel="noopener"
                 class="inline-flex items-center gap-2 px-4 py-2 rounded bg-violet-600 text-white text-sm font-medium hover:bg-violet-700">
                Install Extension
              </a>`
          : nothing}
        <div class="mt-2">
          <label class="flex items-center gap-2 cursor-pointer">
            <input type="checkbox"
                   .checked=${!!c?.extensionConfirmed}
                   @change=${() => c?.confirmExtension()}
                   class="h-4 w-4 rounded border-slate-300 text-violet-600">
            <span class="text-sm text-slate-600">I've installed the extension</span>
          </label>
        </div>
      </div>
    `;
  }
}
customElements.define("fulcra-step-browser_extension", FulcraStepBrowserExtension);
window.FulcraStepComponents.browser_extension = "fulcra-step-browser_extension";
