// packages/web-ui/dist/static/components/step-oauth.js
//
// kind="oauth" — body markdown + "Click here to authenticate" button +
// the current oauthStatus line. The button click hands off to
// ctx.startOAuth(); the daemon then drives the rest of the OAuth dance
// out-of-band (browser opens, callback fires) and writes back into
// ctx.oauthStatus.
//
// Mirrors index.html ~line 722 (onboarding) and ~line 1401 (dashboard).
import { FulcraStepBase, html, nothing } from "./_base.js";
import { unsafeHTML } from "https://cdn.jsdelivr.net/npm/lit@3.2.1/directives/unsafe-html.js";

class FulcraStepOauth extends FulcraStepBase {
  render() {
    const c = this.ctx;
    const bodyHtml = c?.body_html || "";
    const status = c?.oauthStatus;
    return html`
      <div class="space-y-3">
        <div class="prose prose-sm text-slate-700 max-w-none">
          ${unsafeHTML(bodyHtml)}
        </div>
        <button @click=${() => c?.startOAuth()}
                class="px-4 py-2 rounded bg-violet-600 text-white text-sm font-medium hover:bg-violet-700">
          Click here to authenticate
        </button>
        ${status
          ? html`<p class="text-sm text-slate-600">${status}</p>`
          : nothing}
      </div>
    `;
  }
}
customElements.define("fulcra-step-oauth", FulcraStepOauth);
window.FulcraStepComponents.oauth = "fulcra-step-oauth";
