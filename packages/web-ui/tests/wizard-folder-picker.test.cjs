const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

function setup({fail = false} = {}) {
  const calls = [];
  const context = vm.createContext({
    console, setTimeout: () => 1, clearTimeout() {}, CustomEvent: class {},
    window: {Alpine: {effect: () => {}}, addEventListener() {}},
    api: async (url, options = {}) => {
      calls.push({url, options});
      if (url.includes('/choose-folder')) {
        if (fail) throw new Error('Folder selection canceled.');
        return {ok: true, name: 'Synthetic Project'};
      }
      return {};
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../dist/static/wizard.js'), 'utf8'), context);
  const wizard = context.createWizard({
    id: 'synthetic', kind: 'manual',
    required_settings: [{key: 'source_path', label: 'Folder', kind: 'path'}],
    setup_steps: [
      {kind: 'intro'},
      {kind: 'folder_picker', settings_keys: ['source_path']},
      {kind: 'done'},
    ],
  }, () => {});
  return {wizard, calls};
}

test('folder step blocks until an explicit chooser succeeds', async () => {
  const {wizard, calls} = setup();
  await wizard.next();
  assert.equal(wizard.current_step.kind, 'folder_picker');
  assert.equal(wizard.nextBlocked, true);
  await wizard.chooseFolder();
  assert.equal(wizard.nextBlocked, false);
  assert.equal(wizard.selectedFolderName, 'Synthetic Project');
  assert.equal(calls.filter(call => call.url.includes('/choose-folder')).length, 1);
  assert.equal(calls.at(-1).options.method, 'POST');
});

test('canceled chooser leaves the step blocked without inventing a path', async () => {
  const {wizard} = setup({fail: true});
  await wizard.next();
  await wizard.chooseFolder();
  assert.equal(wizard.nextBlocked, true);
  assert.equal(wizard.selectedFolderName, '');
  assert.match(wizard.stepError, /canceled/i);
});

test('folder component exposes one explicit choose button', async () => {
  let Component;
  const context = vm.createContext({
    FulcraStepBase: class {}, nothing: '', unsafeHTML: value => value,
    html: (strings, ...values) => ({strings, values}),
    customElements: {define: (_name, component) => Component = component},
    window: {FulcraStepComponents: {}},
  });
  vm.runInContext(
    fs.readFileSync(path.join(__dirname, '../dist/static/components/step-folder_picker.js'), 'utf8')
      .replace(/^import .*;$/mg, ''),
    context,
  );
  const calls = [];
  const component = new Component();
  component.ctx = {body_html: 'Choose a source', chooseFolder: () => calls.push('pick')};
  const rendered = component.render();
  const callbacks = [];
  function flatten(value) {
    if (Array.isArray(value)) return value.map(flatten).join('');
    if (value && value.strings) return value.strings.reduce(
      (text, part, index) => text + part + flatten(value.values[index] ?? ''), '');
    if (typeof value === 'function') { callbacks.push(value); return '[handler]'; }
    return String(value);
  }
  assert.match(flatten(rendered), /Choose folder/);
  assert.equal(callbacks.length, 1);
  await callbacks[0]();
  assert.deepEqual(calls, ['pick']);
});
