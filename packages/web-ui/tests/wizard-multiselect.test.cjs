const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const plain = value => JSON.parse(JSON.stringify(value));

function setup({saved = {}, options = [{value: 'a', label: 'List A'}, {value: 'b', label: 'List B', disabled: true}], required = true, defaultValue = [], kind = 'scheduled', failSave = false} = {}) {
  const calls = [];
  let discovery = options;
  const context = vm.createContext({console, setTimeout: () => 1, clearTimeout() {}, api: async (url, request = {}) => {
    calls.push({url, ...request});
    if (url.includes('/setting_options/')) {
      if (discovery instanceof Error) throw discovery;
      return {options: discovery};
    }
    if (url.endsWith('/settings')) {
      if (request.method === 'PUT') {
        if (failSave) throw new Error('Save unavailable');
        Object.assign(saved, JSON.parse(request.body));
        return {ok: true};
      }
      return saved;
    }
    return {plugins: []};
  }});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../dist/static/wizard.js'), 'utf8'), context);
  const wizard = context.createWizard({id: 'synthetic', kind, required_settings: [
    {key: 'lists', label: 'Lists', kind: 'multiselect', default: defaultValue, required},
  ], setup_steps: [{kind: 'input', settings_keys: ['lists']}, {kind: 'done'}]}, () => {});
  return {wizard, calls, saved, setOptions: x => discovery = x};
}

async function enter(wizard) {
  await wizard._loadExisting();
  wizard._onStepEnter();
  await new Promise(resolve => setImmediate(resolve));
}

test('defaults and loaded selections stay arrays with no automatic selection', async () => {
  const {wizard, saved} = setup();
  await enter(wizard);
  assert.deepEqual(plain(wizard.input_fields[0].value), []);
  assert.deepEqual(saved, {});
  await wizard.next();
  assert.equal(wizard.current_step.kind, 'input');
  assert.match(wizard.stepError, /required|select/i);
});

test('configured IDs survive discovery, save as arrays, and missing IDs remain removable', async () => {
  const {wizard, saved} = setup({saved: {lists: ['a', 'gone']}});
  await enter(wizard);
  assert.deepEqual(plain(wizard.input_fields[0].value), ['a', 'gone']);
  const missing = wizard.input_fields[0].options.find(x => x.value === 'gone');
  assert.equal(missing.unavailable, true);
  wizard.toggleSelection('lists', 'gone', false);
  await wizard.next();
  assert.deepEqual(saved.lists, ['a']);
  assert.equal(wizard.current_step.kind, 'done');
});

test('discovery failure preserves selection and retry/reentry reload choices by ID', async () => {
  const {wizard, saved, calls, setOptions} = setup({saved: {lists: ['a']}, options: new Error('Access unavailable')});
  await enter(wizard);
  assert.equal(wizard.input_fields[0].optionsStatus, 'error');
  assert.deepEqual(plain(wizard.inputValues.lists), ['a']);
  assert.deepEqual(saved.lists, ['a']);
  await wizard.next();
  assert.equal(wizard.current_step.kind, 'input');
  setOptions([{value: 'a', label: 'Renamed A'}, {value: 'c', label: 'List C'}]);
  await wizard.loadSettingOptions('lists');
  assert.equal(wizard.input_fields[0].optionsStatus, 'ready');
  assert.equal(wizard.input_fields[0].options[0].label, 'Renamed A');
  await wizard.next();
  wizard.back();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.filter(x => x.url.includes('/setting_options/')).length, 3);
  assert.equal(calls.some(x => /\/(enable|run)$/.test(x.url)), false);
});

test('disabled choices cannot be added but a previously saved disabled choice can be removed', async () => {
  const {wizard} = setup({saved: {lists: ['b']}});
  await enter(wizard);
  wizard.toggleSelection('lists', 'b', false);
  wizard.toggleSelection('lists', 'b', true);
  assert.deepEqual(plain(wizard.inputValues.lists), []);
});

test('discovery empty is ready and not error; malformed choices fail closed', async () => {
  const {wizard, setOptions} = setup({options: []});
  await enter(wizard);
  assert.equal(wizard.input_fields[0].optionsStatus, 'ready');
  assert.deepEqual(plain(wizard.input_fields[0].options), []);
  setOptions([{value: 'a', label: 'A'}, {value: 'a', label: 'Again'}]);
  await wizard.loadSettingOptions('lists');
  assert.equal(wizard.input_fields[0].optionsStatus, 'error');
});

test('loading state is visible and does not replace current selections', async () => {
  const {wizard} = setup({saved: {lists: ['a']}});
  await wizard._loadExisting();
  const loading = wizard.loadSettingOptions('lists');
  assert.equal(wizard.input_fields[0].optionsStatus, 'loading');
  assert.deepEqual(plain(wizard.inputValues.lists), ['a']);
  await loading;
});

for (const kind of ['scheduled', 'service']) {
  test(`${kind}: blank skipped required selection cannot enable`, async () => {
    const {wizard, calls} = setup({kind});
    wizard.skipStep();
    await wizard.next();
    assert.equal(calls.some(x => /\/(enable|run)$/.test(x.url)), false);
  });
  test(`${kind}: failed list save cannot advance or enable even after skip`, async () => {
    const {wizard, calls} = setup({kind, failSave: true});
    await enter(wizard);
    wizard.toggleSelection('lists', 'a', true);
    await wizard.next();
    assert.equal(wizard.current_step.kind, 'input');
    assert.match(wizard.stepError, /save/i);
    wizard.skipStep();
    await wizard.next();
    assert.equal(calls.some(x => /\/(enable|run)$/.test(x.url)), false);
  });
}

function renderControl(wizard) {
  let Input;
  const context = vm.createContext({
    FulcraStepBase: class {}, nothing: '',
    html: (strings, ...values) => ({strings, values}),
    customElements: {define: (name, klass) => { Input = klass; }},
    window: {FulcraStepComponents: {}},
  });
  const source = fs.readFileSync(path.join(__dirname, '../dist/static/components/step-input.js'), 'utf8')
    .replace(/^import .*;$/m, '');
  vm.runInContext(source, context);
  const input = new Input();
  input.ctx = wizard;
  const tree = input.render();
  function flatten(value) {
    if (Array.isArray(value)) return value.map(flatten).join('');
    if (value && value.strings) return value.strings.reduce((text, part, i) => text + part + flatten(value.values[i] ?? ''), '');
    return typeof value === 'function' ? '[handler]' : String(value);
  }
  return flatten(tree);
}

test('shared input component renders saved checkboxes, disabled choices, unavailable IDs, loading, errors and empty state', async () => {
  const {wizard, setOptions} = setup({saved: {lists: ['a', 'gone']}});
  await wizard._loadExisting();
  const pending = wizard.loadSettingOptions('lists');
  assert.match(renderControl(wizard), /Loading choices/);
  await pending;
  let rendered = renderControl(wizard);
  assert.match(rendered, /type="checkbox" \.checked=true[\s\S]*List A/);
  assert.match(rendered, /\.checked=false\s+\?disabled=true[\s\S]*List B/);
  assert.match(rendered, /gone[\s\S]*unavailable; uncheck to remove/);
  setOptions(new Error('Could not load choices'));
  await wizard.loadSettingOptions('lists');
  assert.match(renderControl(wizard), /role="alert"[\s\S]*Could not load choices/);
  assert.match(renderControl(wizard), /Retry loading choices/);
  setOptions([]);
  await wizard.loadSettingOptions('lists');
  assert.match(renderControl(wizard), /No lists available/);
});

test('an older in-flight discovery response cannot replace a retry', async () => {
  const pending = [];
  const context = vm.createContext({console, api: () => new Promise(resolve => pending.push(resolve))});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../dist/static/wizard.js'), 'utf8'), context);
  const wizard = context.createWizard({id: 'synthetic', required_settings: [], setup_steps: [{kind: 'input'}]}, () => {});
  const first = wizard.loadSettingOptions('lists');
  const second = wizard.loadSettingOptions('lists');
  pending[1]({options: [{value: 'new', label: 'New result'}]});
  await second;
  pending[0]({options: [{value: 'old', label: 'Old result'}]});
  await first;
  assert.deepEqual(plain(wizard.settingOptions.lists.options), [{value: 'new', label: 'New result'}]);
});
