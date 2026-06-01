// packages/web-ui/dist/static/components/step-extension_pair.js
//
// kind="extension_pair" — one-click pairing handshake with a browser
// extension. Four-state state machine driven by ctx.pairStatus:
//   idle      → "Pair extension" button → ctx.startExtensionPair()
//   pairing   → violet pulse badge ("Pairing…")
//   success   → green check ("Paired")
//   fallback  → the extension never acked within the timeout. Leads with
//               the most likely fix (reload the page — see below) and a
//               "Try pairing again" button, then offers a manual paste
//               escape hatch: bearer token, a Copy button, and "I pasted
//               it" which calls ctx.confirmManualPair(). After confirmation
//               (ctx.pairManuallyConfirmed) the green "Click Continue
//               below" hint replaces the button.
//
// Why "reload the page" is the headline recovery action: Chrome only
// injects an extension's content script into pages loaded AFTER the
// extension was installed/enabled. If this wizard tab was already open
// when the user added the extension, pair-listener.ts isn't on the page,
// so the wizard's postMessage goes nowhere and no ack returns — the
// handshake just times out. Reloading re-injects the content script.
// ctx.pairTimedOut distinguishes this timeout case (show reload guidance)
// from a hard route error (which sets ctx.stepError instead).
//
// Mirrors index.html ~line 762 (onboarding) and ~line 1450 (dashboard).
// Note: site B's manual-confirmed message says "Click Next below" while
// site A's says "Click Continue below" — both render sites are about to
// converge through this component, so I'm keeping site A's wording
// since it appears in both the onboarding and the configure flow now
// (the Next button is labelled differently per-context anyway).
import { FulcraStepBase, html, nothing } from "./_base.js";
import { unsafeHTML } from "https://cdn.jsdelivr.net/gh/lit/dist@3.2.1/all/lit-all.min.js";

class FulcraStepExtensionPair extends FulcraStepBase {
  render() {
    const c = this.ctx;
    const bodyHtml = c?.body_html || "";
    const status = c?.pairStatus || "idle";
    return html`
      <div class="space-y-4">
        <div class="prose prose-sm text-slate-700 max-w-none">
          ${unsafeHTML(bodyHtml)}
        </div>
        ${this._renderStatus(status, c)}
      </div>
    `;
  }

  _renderStatus(status, c) {
    if (status === "idle") {
      return html`
        <button @click=${() => c.startExtensionPair()}
                class="px-4 py-2 rounded bg-violet-600 text-white text-sm font-medium hover:bg-violet-700">
          Pair extension
        </button>
      `;
    }
    if (status === "pairing") {
      return html`
        <div class="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-violet-100 text-violet-700 text-sm font-medium animate-pulse">
          <svg class="w-4 h-4 animate-spin" viewBox="0 0 16 16" fill="none">
            <circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="2" stroke-opacity="0.25"/>
            <path d="M14 8a6 6 0 0 0-6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
          </svg>
          Pairing…
        </div>
      `;
    }
    if (status === "success") {
      return html`
        <div class="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-green-100 text-green-700 text-sm font-medium">
          <svg class="w-4 h-4" viewBox="0 0 16 16" fill="none">
            <path d="M3 8l3.5 3.5L13 5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          Paired
        </div>
      `;
    }
    if (status === "fallback") {
      return html`
        <div class="rounded border border-amber-300 bg-amber-50 p-4 space-y-3">
          <div class="text-sm text-amber-900 font-medium">
            Couldn't reach the Fulcra Attention extension.
          </div>
          ${c.pairTimedOut
            ? html`
                <div class="text-sm text-amber-900 space-y-2">
                  <p>
                    Make sure the extension is installed and enabled, then
                    <strong>reload this page</strong> and click
                    <strong>Pair extension</strong> again.
                  </p>
                  <p class="text-xs text-amber-800">
                    The extension can only talk to pages that were opened
                    after it was installed, so if this tab was already open
                    when you added it, a reload is needed.
                  </p>
                  <div class="flex flex-wrap items-center gap-2 pt-1">
                    <button @click=${() => window.location.reload()}
                            class="px-3 py-1.5 rounded bg-violet-600 text-white text-xs font-medium hover:bg-violet-700">
                      Reload page
                    </button>
                    <button @click=${() => c.startExtensionPair()}
                            class="px-3 py-1.5 rounded border border-violet-300 text-violet-700 text-xs font-medium hover:bg-violet-50">
                      Try pairing again
                    </button>
                  </div>
                </div>`
            : nothing}
          <div class="text-sm text-amber-900 font-medium pt-1">
            Or finish setup manually:
          </div>
          <ol class="text-sm text-amber-900 list-decimal list-inside space-y-1">
            <li>Copy the token below.</li>
            <li>Open the extension's Options page (right-click the toolbar icon → <strong>Options</strong>).</li>
            <li>Paste it into the <strong>Bearer token</strong> field and click <strong>Save</strong>.</li>
            <li>Return here and click <strong>I pasted it</strong>.</li>
          </ol>
          <div class="flex items-center gap-2">
            <code class="flex-1 bg-white border border-amber-200 px-2 py-1 rounded text-xs font-mono text-slate-800 break-all">${c.pairFallbackToken}</code>
            <button @click=${() => c.copyPairToken()}
                    class="px-3 py-1.5 rounded bg-amber-600 text-white text-xs font-medium hover:bg-amber-700">
              Copy
            </button>
          </div>
          ${!c.pairManuallyConfirmed
            ? html`
                <button @click=${() => c.confirmManualPair()}
                        class="px-4 py-2 rounded bg-violet-600 text-white text-sm font-medium hover:bg-violet-700">
                  I pasted it
                </button>`
            : html`
                <div class="text-sm text-green-700 font-medium">
                  Click Continue below.
                </div>`}
        </div>
      `;
    }
    return nothing;
  }
}
customElements.define("fulcra-step-extension_pair", FulcraStepExtensionPair);
window.FulcraStepComponents.extension_pair = "fulcra-step-extension_pair";
