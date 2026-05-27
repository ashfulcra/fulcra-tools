// packages/web-ui/dist/static/components/step-external_action.js
//
// kind="external_action" — body copy + a single "Open <host>" link out to
// an external page (most often "go create an OAuth app on the provider's
// developer console"). Mirrors index.html ~line 301 (onboarding) and
// ~line 1052 (dashboard Configure).
import { FulcraStepBase, html, nothing } from "./_base.js";
import { unsafeHTML } from "https://cdn.jsdelivr.net/npm/lit@3.2.1/directives/unsafe-html.js";

class FulcraStepExternalAction extends FulcraStepBase {
  render() {
    const s = this.step || {};
    const bodyHtml = this.ctx?.body_html || "";
    const link = s.external_link;
    // host label = "github.com" from "https://github.com/foo" — keeps the
    // button useful when the URL is long; mirrors the inline template.
    const hostLabel = link
      ? link.replace(/^https?:\/\//, "").split("/")[0]
      : "";
    return html`
      <div class="space-y-4">
        <div class="prose prose-sm text-slate-700 max-w-none">
          ${unsafeHTML(bodyHtml)}
        </div>
        ${link
          ? html`
              <a href=${link}
                 target="_blank" rel="noopener"
                 class="inline-flex items-center gap-2 px-4 py-2 rounded bg-slate-100 text-slate-700 hover:bg-slate-200 text-sm font-medium">
                Open
                <span>${hostLabel}</span>
                <svg class="w-3 h-3" viewBox="0 0 12 12" fill="none">
                  <path d="M5 1H1v10h10V7M7 1h4v4M4.5 7.5 11 1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
                </svg>
              </a>`
          : nothing}
      </div>
    `;
  }
}
customElements.define("fulcra-step-external_action", FulcraStepExternalAction);
window.FulcraStepComponents.external_action = "fulcra-step-external_action";
