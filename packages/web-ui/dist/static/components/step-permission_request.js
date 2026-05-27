// packages/web-ui/dist/static/components/step-permission_request.js
//
// kind="permission_request" (task #66) — deep-link to System Settings +
// "Verify access" button when the daemon exposes a permission_check.
// The pre-#66 UX falsely claimed macOS would auto-prompt for Full Disk
// Access; this kind replaces that lie with a deep-link + verify loop.
//
// Inputs (from ctx):
//   body_html, current_permission_id, permissionResult ({granted, hint}),
//   permissionChecking (bool), plugin_contract.permission_check_available.
// Events:
//   ctx.permissionDeepLink(id) returns a deep-link URL (or "" → button
//                              hidden), ctx.checkPermission() runs the
//                              backend probe and writes permissionResult.
//
// Mirrors index.html ~line 446 (onboarding) and ~line 1182 (dashboard).
import { FulcraStepBase, html, nothing } from "./_base.js";
import { unsafeHTML } from "https://cdn.jsdelivr.net/npm/lit@3.2.1/directives/unsafe-html.js";

class FulcraStepPermissionRequest extends FulcraStepBase {
  render() {
    const c = this.ctx;
    const bodyHtml = c?.body_html || "";
    const permId = c?.current_permission_id;
    const deepLink = permId ? c?.permissionDeepLink(permId) : "";
    const checkAvailable = c?.plugin_contract?.permission_check_available;
    const result = c?.permissionResult;
    const checking = c?.permissionChecking;
    return html`
      <div class="space-y-4">
        <div class="prose prose-sm text-slate-700 max-w-none">
          ${unsafeHTML(bodyHtml)}
        </div>

        <div class="flex gap-2 items-center">
          ${deepLink
            ? html`
                <a href=${deepLink}
                   class="px-3 py-1.5 text-sm rounded border border-slate-300 hover:bg-slate-50">
                  Open System Settings →
                </a>`
            : nothing}
          ${checkAvailable
            ? html`
                <button @click=${() => c.checkPermission()}
                        ?disabled=${checking}
                        class="px-3 py-1.5 text-sm rounded border border-violet-300 text-violet-700 hover:bg-violet-50 disabled:opacity-50">
                  <span>${checking ? "Checking…" : "Verify access"}</span>
                </button>`
            : nothing}
        </div>

        ${result && result.granted
          ? html`
              <div class="rounded border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
                ✓ Access granted.
              </div>`
          : nothing}
        ${result && !result.granted
          ? html`
              <div class="rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
                <p class="font-medium">Not granted yet.</p>
                ${result.hint
                  ? html`<p class="text-amber-700 mt-1">${result.hint}</p>`
                  : nothing}
                <p class="mt-2 text-xs">Open System Settings above, grant access, then click Verify access again.</p>
              </div>`
          : nothing}

        ${!checkAvailable
          ? html`
              <div class="rounded border border-slate-200 bg-slate-50 p-3 text-sm text-slate-600">
                Follow the steps above, then click Next to continue.
              </div>`
          : nothing}
      </div>
    `;
  }
}
customElements.define("fulcra-step-permission_request", FulcraStepPermissionRequest);
window.FulcraStepComponents.permission_request = "fulcra-step-permission_request";
