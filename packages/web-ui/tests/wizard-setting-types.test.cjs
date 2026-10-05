const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

function loadInputComponent() {
  let Input;
  const context = vm.createContext({
    FulcraStepBase: class {},
    nothing: '',
    html: (strings, ...values) => ({strings, values}),
    customElements: {define: (_name, klass) => { Input = klass; }},
    window: {FulcraStepComponents: {}},
  });
  const source = fs.readFileSync(
    path.join(__dirname, '../dist/static/components/step-input.js'), 'utf8',
  ).replace(/^import .*;$/m, '');
  vm.runInContext(source, context);
  return new Input();
}

function eventHandler(tree) {
  const handler = tree.values.find(value => typeof value === 'function');
  assert.equal(typeof handler, 'function');
  return handler;
}

test('toggle controls write native booleans into wizard state', () => {
  const input = loadInputComponent();
  const writes = [];
  const ctx = {updateField: (key, value) => writes.push({key, value})};
  const tree = input._renderControl({key: 'dry_run', kind: 'toggle', value: true}, ctx);

  eventHandler(tree)({target: {checked: false}});

  assert.deepEqual(writes, [{key: 'dry_run', value: false}]);
});

test('port controls write native integers into wizard state', () => {
  const input = loadInputComponent();
  const writes = [];
  const ctx = {updateField: (key, value) => writes.push({key, value})};
  const tree = input._renderControl({key: 'port', kind: 'port', value: 9292}, ctx);

  eventHandler(tree)({target: {value: '8080', valueAsNumber: 8080}});

  assert.deepEqual(writes, [{key: 'port', value: 8080}]);
});

test('interval controls send numeric seconds as numbers and keep ISO durations', () => {
  const input = loadInputComponent();
  const writes = [];
  const ctx = {updateField: (key, value) => writes.push({key, value})};
  const tree = input._renderControl({key: 'poll_every', kind: 'interval', value: ''}, ctx);
  const handler = eventHandler(tree);

  handler({target: {value: '300'}});
  handler({target: {value: 'PT5M'}});

  assert.deepEqual(writes, [
    {key: 'poll_every', value: 300},
    {key: 'poll_every', value: 'PT5M'},
  ]);
});

function loadWizard(requiredSettings) {
  const context = vm.createContext({
    console,
    setTimeout: () => 1,
    clearTimeout() {},
    api: async () => ({}),
  });
  vm.runInContext(
    fs.readFileSync(path.join(__dirname, '../dist/static/wizard.js'), 'utf8'),
    context,
  );
  return context.createWizard({
    id: 'typed',
    kind: 'manual',
    required_settings: requiredSettings,
    setup_steps: [{kind: 'input', settings_keys: requiredSettings.map(s => s.key)}],
  }, () => {});
}

test('declared boolean and numeric defaults retain their JSON types', () => {
  const wizard = loadWizard([
    {key: 'dry_run', kind: 'toggle', default: false},
    {key: 'port', kind: 'port', default: 9292},
  ]);

  wizard._seedDefaults();

  assert.equal(wizard.inputValues.dry_run, false);
  assert.equal(wizard.inputValues.port, 9292);
  assert.equal(wizard.input_fields[0].value, false);
  assert.equal(wizard.input_fields[1].value, 9292);
});
