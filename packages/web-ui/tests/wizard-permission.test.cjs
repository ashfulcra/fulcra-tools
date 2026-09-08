const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

function setup({requestAvailable = true, failRequest = false, hook = null} = {}) {
  const calls = [];
  const context = vm.createContext({
    console, setTimeout: () => 1, clearTimeout() {}, CustomEvent: class {},
    window: {Alpine: {effect: () => {}}, addEventListener() {}},
    api: async (url) => {
      calls.push(url);
      if (hook) { const value = hook(url); if (value !== undefined) return value; }
      if (url.endsWith('/request_permission')) {
        if (failRequest) throw new Error('Access request failed');
        return {granted: true};
      }
      if (url.endsWith('/check_permission')) return {granted: false, hint: 'Allow access'};
      return {};
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../dist/static/wizard.js'), 'utf8'), context);
  const wizard = context.createWizard({id: 'synthetic', kind: 'manual', permission_check_available: true,
    permission_request_available: requestAvailable, required_permissions: [{id: 'reminders'}],
    setup_steps: [{kind: 'intro'}, {kind: 'permission_request'}, {kind: 'done'}]}, () => {});
  return {wizard, calls};
}

test('initialization, Next, Back, Skip and Verify access never request native permission', async () => {
  const {wizard, calls} = setup();
  wizard.init();
  await new Promise(resolve => setImmediate(resolve));
  await wizard.next();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(calls.some(url => url.endsWith('/check_permission')));
  await wizard.checkPermission();
  await wizard.next();
  wizard.back();
  wizard.skipStep();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.some(url => /\/(request_permission|enable|run)$/.test(url)), false);
});

test('only explicit Allow access requests permission and duplicate clicks are ignored', async () => {
  const {wizard, calls} = setup();
  await wizard.next();
  await new Promise(resolve => setImmediate(resolve));
  await Promise.all([wizard.requestPermission(), wizard.requestPermission()]);
  assert.equal(calls.filter(url => url.endsWith('/request_permission')).length, 1);
  assert.equal(wizard.permissionResult.granted, true);
  assert.equal(wizard.nextBlocked, false);
});

test('permission request failure leaves access ungranted and advancement blocked', async () => {
  const {wizard} = setup({failRequest: true});
  await wizard.next();
  await new Promise(resolve => setImmediate(resolve));
  await wizard.requestPermission();
  assert.equal(wizard.permissionResult.granted, false);
  assert.equal(wizard.nextBlocked, true);
  assert.equal(wizard.permissionRequesting, false);
});

test('plugins without a request callback keep verification only', async () => {
  const {wizard, calls} = setup({requestAvailable: false});
  await wizard.next();
  await new Promise(resolve => setImmediate(resolve));
  await wizard.requestPermission();
  assert.equal(calls.some(url => url.endsWith('/request_permission')), false);
  assert.match(wizard.permissionDeepLink('full-disk-access'), /Privacy_AllFiles/);
});

function render(wizard) {
  let Component;
  const context = vm.createContext({FulcraStepBase: class {}, nothing: '', unsafeHTML: x => x,
    html: (strings, ...values) => ({strings, values}),
    customElements: {define: (name, component) => Component = component}, window: {FulcraStepComponents: {}},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../dist/static/components/step-permission_request.js'), 'utf8').replace(/^import .*;$/mg, ''), context);
  const component = new Component();
  component.ctx = wizard;
  const callbacks = [];
  function flatten(value) {
    if (Array.isArray(value)) return value.map(flatten).join('');
    if (value && value.strings) return value.strings.reduce((text, part, i) => text + part + flatten(value.values[i] ?? ''), '');
    if (typeof value === 'function') { callbacks.push(value); return '[handler]'; }
    return String(value);
  }
  return {text: flatten(component.render()), callbacks};
}

test('permission component wires an explicit Allow access button and preserves Full Disk Access settings flow', async () => {
  const {wizard, calls} = setup();
  await wizard.next();
  await new Promise(resolve => setImmediate(resolve));
  const result = render(wizard);
  assert.match(result.text, /Allow access/);
  assert.equal(calls.some(url => url.endsWith('/request_permission')), false);
  // Both rendered controls are explicit user actions: one verifies, one prompts.
  for (const click of result.callbacks) await click();
  assert.equal(calls.filter(url => url.endsWith('/request_permission')).length, 1);
  wizard.plugin_contract.permission_request_available = false;
  wizard.plugin_contract.required_permissions = [{id: 'full-disk-access'}];
  const legacy = render(wizard).text;
  assert.doesNotMatch(legacy, /\n\s+Allow access\s*\n/);
  assert.match(legacy, /Open System Settings/);
  assert.match(legacy, /Verify access/);
});


test('late native consent cannot authorize a later visit to the same step', async () => {
  let finish;
  const {wizard, calls} = setup({hook: url => url.endsWith('/request_permission')
    ? new Promise(resolve => { finish = resolve; }) : undefined});
  await wizard.next();
  await new Promise(resolve => setImmediate(resolve));
  const pending = wizard.requestPermission();
  wizard.back();
  await wizard.next();
  await new Promise(resolve => setImmediate(resolve));
  finish({granted: true});
  await pending;
  assert.equal(calls.filter(url => url.endsWith('/check_permission')).length, 2);
  assert.equal(wizard.permissionResult.granted, false);
  assert.equal(wizard.nextBlocked, true);
});

test('late permission verification cannot unblock a different step', async () => {
  let finish;
  const {wizard} = setup({hook: url => url.endsWith('/check_permission')
    ? new Promise(resolve => { finish = resolve; }) : undefined});
  wizard.steps[2] = {kind: 'browser_extension'};
  await wizard.next();
  wizard.skipStep();
  assert.equal(wizard.nextBlocked, true);
  finish({granted: true});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(wizard.current_step.kind, 'browser_extension');
  assert.equal(wizard.nextBlocked, true);
  assert.equal(wizard.permissionResult, null);
});
