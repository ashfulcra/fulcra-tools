// packages/web-ui/dist/static/components/step-permission_request.js
//
// kind="permission_request" (task #66) — deep-link to System Settings +
// "Verify access" for a read-only check and explicit "Allow access" when
// the daemon exposes a permission_request callback.
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
import { unsafeHTML } from "https://cdn.jsdelivr.net/gh/lit/dist@3.2.1/all/lit-all.min.js";

class FulcraStepPermissionRequest extends FulcraStepBase {
  render() {
    const c = this.ctx;
    const bodyHtml = c?.body_html || "";
    const permId = c?.current_permission_id;
    const deepLink = permId ? c?.permissionDeepLink(permId) : "";
    const checkAvailable = c?.plugin_contract?.permission_check_available;
    const requestAvailable = c?.plugin_contract?.permission_request_available;
    const requesting = c?.permissionRequesting;
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
          ${requestAvailable
            ? html`
                <button type="button" @click=${() => c.requestPermission()}
                        ?disabled=${checking || requesting || result?.granted}
                        class="px-3 py-1.5 text-sm rounded bg-violet-600 text-white hover:bg-violet-700 disabled:opacity-50">
                  ${requesting ? "Requesting access…" : "Allow access"}
                </button>`
            : nothing}
          ${checkAvailable
            ? html`
                <button @click=${() => c.checkPermission()}
                        ?disabled=${checking || requesting}
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
                <p class="mt-2 text-xs">${requestAvailable
                  ? "Click Allow access to request permission. If access was denied, enable it in System Settings, then verify again."
                  : "Open System Settings above, grant access, then click Verify access again."}</p>
              </div>`
          : nothing}

        ${!checkAvailable && !requestAvailable
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
