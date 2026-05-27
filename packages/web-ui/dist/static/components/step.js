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
import { FulcraStepBase, nothing } from "./_base.js";

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
    return child;
  }
}
customElements.define("fulcra-step", FulcraStep);
