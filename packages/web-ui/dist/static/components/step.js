// packages/web-ui/dist/static/components/step.js
//
// <fulcra-step> — routes to the kind-specific component by step.kind.
//
// Usage from a render site (light DOM, prop binding via x-effect because
// Alpine's `:prop` only sets attributes, not properties — and we want
// the .step / .ctx values to be the actual objects, not their toString()):
//
//   <fulcra-step x-effect="$el.step = current_step; $el.ctx = $data;
//                          void [healthChecking, dpLoading, ...];
//                          $el.requestUpdate?.()">
//   </fulcra-step>
//
// The explicit identifier list inside `void [...]` is the dep-tracker.
// Alpine's x-effect evaluator only wraps bare identifier reads in
// dep-tracking — iteration helpers like Object.values($data) /
// JSON.stringify / spread {...$data} all perform their property reads
// in native code that doesn't go through Alpine's tracker, so the
// inner property changes never re-fire the effect. See index.html
// (around line 327) for the full reactive-field list and why each
// site has to enumerate it.
//
// Reactivity choreography between Alpine and Lit:
//
//  1. Alpine x-effect reads the listed properties of $data, registering
//     each as a dep.
//  2. ANY mutation in $data — `this.healthChecking = false`, `this.
//     dpDefinitions = [...]`, etc. — re-fires x-effect.
//  3. x-effect writes `$el.step` / `$el.ctx` on the dispatcher. The
//     references haven't changed (Alpine mutates in place), so we ALSO
//     call `$el.requestUpdate()` to force a Lit cycle.
//  4. The dispatcher's render() looks up the right child by kind, caches
//     it (so we don't re-mount per ctx tick — input fields keep focus,
//     scroll positions hold), and reassigns `.step` / `.ctx`.
//  5. FulcraStepBase declares step/ctx with `hasChanged: () => true`
//     (see _base.js), so each prop reassignment triggers a child render
//     even though the references are identity-equal.
//
// Why caching the child by kind matters:
//   Without the cache, every dispatcher render created a new child
//   element, and Lit's ChildPart replaced the old one in DOM. That
//   wiped focus and scroll on every ctx tick (one tick per reactive
//   field change → many ticks per real user interaction). Caching
//   reuses the same DOM node; the hasChanged hook still drives
//   re-renders inside it.
//
// We do NOT call `child.requestUpdate()` explicitly here — the
// hasChanged hook makes that unnecessary, and calling it from inside
// the dispatcher's render() caused an infinite loop (the child read
// ctx during its render, Alpine re-registered deps mid-effect, x-effect
// re-fired, dispatcher.render ran again, etc.).
import { FulcraStepBase, nothing } from "./_base.js";

class FulcraStep extends FulcraStepBase {
  constructor() {
    super();
    this._kindChildren = {};
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
