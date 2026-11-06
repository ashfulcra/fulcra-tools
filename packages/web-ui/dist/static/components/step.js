// packages/web-ui/dist/static/components/step.js
//
// <fulcra-step> — routes to the kind-specific component by step.kind,
// and the Lit half of the reactivity bridge between Alpine's wizard
// data and Lit's render cycle.
//
// Usage from a render site (light DOM, prop binding via x-effect because
// Alpine's `:prop` only sets attributes, not properties — we need real
// object refs, not their toString()):
//
//   <fulcra-step x-effect="$el.step = current_step; $el.ctx = $data">
//   </fulcra-step>
//
// x-effect only handles binding the two props once on mount and on
// current_step changes (Next/Back navigation). Reactivity for INNER ctx
// state (healthChecking, dpLoading, firstRunStatus, etc.) is driven by
// the `fulcra-wizard-tick` CustomEvent that wizard.js dispatches from
// an Alpine.effect installed inside the wizard's init() — see
// `_installLitReactivityBridge` in wizard.js for why the bridge lives
// there instead of here.
//
// Render flow:
//   1. wizard.js's createWizard()._installLitReactivityBridge() registers
//      an Alpine.effect that reads every reactive field on `this`. Each
//      read registers a dep via Alpine's reactive proxy traps.
//   2. Any mutation of one of those fields (`this.healthChecking = false`
//      etc.) fires the effect, which dispatches `fulcra-wizard-tick` on
//      window.
//   3. This dispatcher's connectedCallback registered a window listener
//      that calls this.requestUpdate().
//   4. Lit re-runs render(), which routes to the kind-specific child
//      (cached so DOM identity holds across ticks) and reassigns its
//      .step / .ctx props.
//   5. FulcraStepBase declares step/ctx with `hasChanged: () => true`
//      so the child re-renders even when the references are identical
//      (Alpine mutates ctx in place).
import { FulcraStepBase, html, nothing } from "./_base.js";

class FulcraStep extends FulcraStepBase {
  constructor() {
    super();
    // Cache children by kind so navigating Back/Next within one wizard
    // session reuses the same DOM node — keeps input field focus, scroll
    // positions, etc. Without the cache every dispatcher render would
    // build a fresh element and Lit's ChildPart would swap it in, wiping
    // anything the user had touched.
    this._kindChildren = {};
    this._onWizardTick = () => this.requestUpdate();
  }

  connectedCallback() {
    super.connectedCallback();
    window.addEventListener("fulcra-wizard-tick", this._onWizardTick);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener("fulcra-wizard-tick", this._onWizardTick);
  }

  render() {
    if (!this.step) return nothing;
    const kind = this.step.kind;
    const tag = window.FulcraStepComponents[kind];
    // No component registered for this kind — render nothing. Forward-
    // compat safety net for a newer daemon shipping a kind the web-ui
    // hasn't grown a component for yet.
    if (!tag) return nothing;

    let child = this._kindChildren[kind];
    if (!child) {
      child = document.createElement(tag);
      this._kindChildren[kind] = child;
    }
    child.step = this.step;
    child.ctx = this.ctx;
    const c = this.ctx;
    const fields = c?.credential_access_fields || [];
    return html`
      ${fields.length ? html`
        <section aria-label="Saved credential access" class="mb-4 p-3 rounded border border-amber-300">
          <h3 class="font-semibold">Check saved access</h3>
          ${fields.map(field => html`
            <div class="my-2">
              <span class="font-medium">${field.label || field.key}</span>
              ${field.recovery === "trakt_reconnect" ? html`
                <p role="status">The old Trakt sign-in cannot be read. Reconnect once to replace it with one saved sign-in. Original items are kept.</p>
                <button type="button" @click=${() => c.startOAuth()}
                        ?disabled=${c._credAccessBusy} class="underline">Reconnect Trakt</button>
                <p role="status">${c.oauthStatus || ""}</p>
              ` : field.status === "access_required" && field.recovery !== "quiet" ? html`
                <p role="status">Saved access needs your approval for this app.</p>
                <button type="button" @click=${() => c.authorizeCredential(field.key)}
                        ?disabled=${c._credAccessBusy} class="underline">Allow Keychain access</button>
                <p class="text-xs">Choose Always Allow in macOS to remember this app.</p>
              ` : html`
                <p role="status">${field.status === "pending" ? "The saved access check is pending."
                  : field.status === "changed" ? "Saved access changed during the check."
                  : "Saved access could not be checked."} Your saved item has been kept.</p>
                <button type="button" @click=${() => c.checkCredentialAccess()}
                        ?disabled=${c._credAccessBusy} class="underline">Check again (quiet)</button>
              `}
            </div>
          `)}
          ${c._credAccessError ? html`<p role="alert">${c._credAccessError}</p>` : nothing}
        </section>
      ` : nothing}
      ${child}
    `;
  }
}
customElements.define("fulcra-step", FulcraStep);
