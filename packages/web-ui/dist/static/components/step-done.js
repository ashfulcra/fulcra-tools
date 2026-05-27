// packages/web-ui/dist/static/components/step-done.js
//
// kind="done" — the success page. Renders:
//   1. green check + step.title (the outer <h2> is suppressed for this
//      kind in index.html — see the `current_step.kind !== 'done'` guard
//      around the title block; this component owns its own title)
//   2. body markdown
//   3. first-run status banner — driven by ctx.firstRunStatus, which is
//      auto-triggered for non-service plugins on done-step entry (see
//      wizard.js _triggerFirstRun). Four sub-states:
//        running → spinner + "Running first import…"
//        done    → emerald banner + summary + optional timeline deep-link
//                  (ctx.firstRunTimelineUrl, only set when the run
//                  resolved a definition_id we can filter the timeline on
//                  — see #58)
//        error   → amber banner + summary + "you can retry later" hint
//        slow    → slate banner + summary ("this is taking a while")
//
// Mirrors index.html ~line 833 (onboarding) and ~line 1516 (dashboard).
// Site B at HEAD had a stripped-down done banner (no timeline link);
// the component renders the richer onboarding version everywhere now.
import { FulcraStepBase, html, nothing } from "./_base.js";
import { unsafeHTML } from "https://cdn.jsdelivr.net/gh/lit/dist@3.2.1/all/lit-all.min.js";

class FulcraStepDone extends FulcraStepBase {
  render() {
    const s = this.step || {};
    const c = this.ctx;
    const bodyHtml = c?.body_html || "";
    const status = c?.firstRunStatus;
    return html`
      <div class="space-y-4">
        <div class="flex items-center gap-3 text-green-700">
          <svg class="w-10 h-10" viewBox="0 0 40 40" fill="none">
            <circle cx="20" cy="20" r="20" fill="#D1FAE5"/>
            <path d="M12 20l6 6 10-12" stroke="#059669" stroke-width="2.5"
                  stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <div>
            <div class="font-semibold text-lg">${s.title}</div>
          </div>
        </div>
        <div class="prose prose-sm text-slate-700 max-w-none">
          ${unsafeHTML(bodyHtml)}
        </div>

        ${status === "running"
          ? html`
              <div class="rounded border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600 flex items-center gap-2">
                <svg class="w-4 h-4 animate-spin text-slate-400" viewBox="0 0 24 24" fill="none">
                  <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" stroke-dasharray="40 60" stroke-linecap="round"/>
                </svg>
                Running first import…
              </div>`
          : nothing}

        ${status === "done"
          ? html`
              <div class="rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800 space-y-2">
                <div>${`✓ ${c.firstRunSummary}`}</div>
                ${c.firstRunTimelineUrl
                  ? html`
                      <div>
                        <a href=${c.firstRunTimelineUrl}
                           target="_blank"
                           rel="noopener noreferrer"
                           class="inline-flex items-center gap-1 text-emerald-700 hover:text-emerald-900 underline text-sm font-medium">
                          View your new data on the Fulcra timeline →
                        </a>
                      </div>`
                  : nothing}
              </div>`
          : nothing}

        ${status === "error"
          ? html`
              <div class="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800 space-y-1">
                <div class="font-medium">First run reported an error.</div>
                <div>${c.firstRunSummary}</div>
                <div class="text-xs text-amber-700">
                  You can finish anyway and retry from the dashboard, or
                  go back and check your settings.
                </div>
              </div>`
          : nothing}

        ${status === "slow"
          ? html`
              <div class="rounded border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600">${c.firstRunSummary}</div>`
          : nothing}
      </div>
    `;
  }
}
customElements.define("fulcra-step-done", FulcraStepDone);
window.FulcraStepComponents.done = "fulcra-step-done";
