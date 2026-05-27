// packages/web-ui/dist/static/components/step-intro.js
//
// kind="intro" — introductory text page, no user input.
//
// Replaces the inline template at index.html ~line 295 (onboarding flow)
// and ~line 1047 (dashboard Configure flow). Visual contract:
//   <div class="prose prose-sm text-slate-700 max-w-none"
//        x-html="body_html"></div>
//
// body_html is wizard.js's pre-rendered markdown — see renderMd() in
// wizard.js. We unsafeHTML it because the daemon already produced safe
// HTML (marked + sanitize-passthrough on trusted plugin metadata).
//
// SRI for the directive subpath rides Lit's main-package pin — see the
// comment block in _base.js.
import { FulcraStepBase, html } from "./_base.js";
import { unsafeHTML } from "https://cdn.jsdelivr.net/npm/lit@3.2.1/directives/unsafe-html.js";

class FulcraStepIntro extends FulcraStepBase {
  render() {
    const bodyHtml = this.ctx?.body_html || "";
    return html`
      <div class="prose prose-sm text-slate-700 max-w-none">
        ${unsafeHTML(bodyHtml)}
      </div>
    `;
  }
}
customElements.define("fulcra-step-intro", FulcraStepIntro);
window.FulcraStepComponents.intro = "fulcra-step-intro";
