// packages/web-ui/dist/static/components/step-test_connection.js
//
// kind="test_connection" — body markdown + run/result UI for the
// daemon's health_check. Three sub-states:
//   healthChecking          → spinner + "Testing connection…"
//   healthResult.ok         → green check + summary + optional preview list
//                             (each entry has {title|name, subtitle?, watched_at|date?})
//   healthResult && !.ok    → red error + Retry → ctx.runHealthCheck()
// When no health_check is registered, a slate hint replaces the run UI.
//
// Mirrors index.html ~line 511 (onboarding) and ~line 1245 (dashboard).
// Site B had a stripped-down variant (no preview list); the component
// uses the richer onboarding-site rendering — the user gets the better
// UX in both flows now.
import { FulcraStepBase, html, nothing } from "./_base.js";
import { unsafeHTML } from "https://cdn.jsdelivr.net/gh/lit/dist@3.2.1/all/lit-all.min.js";

class FulcraStepTestConnection extends FulcraStepBase {
  render() {
    const c = this.ctx;
    const bodyHtml = c?.body_html || "";
    const checking = c?.healthChecking;
    const result = c?.healthResult;
    const error = c?.healthError;
    return html`
      <div class="space-y-4">
        <div class="prose prose-sm text-slate-700 max-w-none">
          ${unsafeHTML(bodyHtml)}
        </div>

        ${checking
          ? html`
              <div class="flex items-center gap-3 text-slate-500">
                <svg class="animate-spin h-5 w-5 text-violet-600" viewBox="0 0 24 24" fill="none">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
                  <path class="opacity-75" fill="currentColor"
                        d="M4 12a8 8 0 018-8v8z"/>
                </svg>
                <span class="text-sm">Testing connection…</span>
              </div>`
          : nothing}

        ${!checking && result ? this._renderResult(result, c) : nothing}

        ${!checking && !result && !error
          ? html`<p class="text-sm text-slate-500">No health check available for this plugin.</p>`
          : nothing}
      </div>
    `;
  }

  _renderResult(result, c) {
    if (result.ok) {
      return html`
        <div>
          <div class="space-y-3">
            <div class="flex items-center gap-2 text-green-700">
              <svg class="w-5 h-5" viewBox="0 0 20 20" fill="currentColor">
                <path fill-rule="evenodd"
                      d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z"
                      clip-rule="evenodd"/>
              </svg>
              <span class="font-medium text-sm">${result.summary}</span>
            </div>
            ${result.preview && result.preview.length > 0
              ? html`
                  <ul class="space-y-1.5">
                    ${result.preview.map(
                      (entry) => html`
                        <li class="text-sm text-slate-600 border-l-2 border-green-300 pl-3 py-0.5">
                          <div class="font-medium">${entry.title || entry.name || JSON.stringify(entry)}</div>
                          ${entry.subtitle
                            ? html`<div class="text-xs text-slate-500">${entry.subtitle}</div>`
                            : nothing}
                          ${entry.watched_at || entry.date
                            ? html`
                                <div class="text-xs text-slate-400">
                                  ${new Date(entry.watched_at || entry.date).toLocaleString(undefined, {
                                    dateStyle: "medium",
                                    timeStyle: "short",
                                  })}
                                </div>`
                            : nothing}
                        </li>`
                    )}
                  </ul>`
              : nothing}
          </div>
        </div>
      `;
    }
    // failure
    return html`
      <div>
        <div class="space-y-3">
          <div class="flex items-start gap-2 text-red-700">
            <svg class="w-5 h-5 mt-0.5 shrink-0" viewBox="0 0 20 20" fill="currentColor">
              <path fill-rule="evenodd"
                    d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7 4a1 1 0 11-2 0 1 1 0 012 0zm-1-9a1 1 0 00-1 1v4a1 1 0 102 0V6a1 1 0 00-1-1z"
                    clip-rule="evenodd"/>
            </svg>
            <span class="text-sm">${result.summary}</span>
          </div>
          <button @click=${() => c.runHealthCheck()}
                  class="px-4 py-2 text-sm rounded border border-red-300 text-red-700 hover:bg-red-50">
            Retry
          </button>
        </div>
      </div>
    `;
  }
}
customElements.define("fulcra-step-test_connection", FulcraStepTestConnection);
window.FulcraStepComponents.test_connection = "fulcra-step-test_connection";
