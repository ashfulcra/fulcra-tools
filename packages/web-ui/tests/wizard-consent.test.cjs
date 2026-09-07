const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

function setup({kind = 'scheduled', failEnable = false, previewSetting = false, failSettings = false} = {}) {
  const calls = [];
  let completed = 0;
  const context = vm.createContext({
    console, setTimeout: () => 1, clearTimeout() {},
    api: async (url, options) => {
      calls.push(url);
      if (failEnable && url.endsWith('/enable')) throw new Error('Could not enable');
      if (failSettings && url.endsWith('/settings')) throw new Error('Could not save');
      if (url.endsWith('/settings')) context.savedSettings = JSON.parse(options.body);
      return {plugins: []};
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../dist/static/wizard.js'), 'utf8'), context);
  const wizard = context.createWizard({
    id: 'apple-notes', kind,
    required_settings: previewSetting ? [{key: 'dry_run', kind: 'toggle', default: false}] : [],
    setup_steps: [{kind: 'intro'}, {kind: 'done'}],
  }, () => completed++);
  return {wizard, calls, completed: () => completed, savedSettings: () => context.savedSettings};
}

test('Next, Back, and skip navigation never enable or run a plugin', async () => {
  const {wizard, calls} = setup();
  await wizard.next();
  assert.equal(wizard.current_step.kind, 'done');
  await Promise.resolve();
  assert.deepEqual(calls, []);
  wizard.back();
  wizard.skipStep();
  await Promise.resolve();
  assert.deepEqual(calls, []);
  assert.equal(wizard.completion_label, 'Enable & start sync');
});

test('the explicit start action enables and starts exactly one run', async () => {
  const {wizard, calls, completed} = setup();
  await wizard.next();
  await wizard.next();
  await wizard.next(); // duplicate clicks during startup must be ignored
  assert.deepEqual(calls, ['/api/status', '/api/plugin/apple-notes/enable', '/api/plugin/apple-notes/run']);
  assert.equal(completed(), 0);
  wizard.firstRunStatus = 'done';
  assert.equal(wizard.completion_label, 'Done');
  await wizard.next();
  assert.equal(completed(), 1);
  assert.equal(calls.filter(x => x.endsWith('/run')).length, 1);
});

test('an enable failure never starts a run or reports successful setup', async () => {
  const {wizard, calls, completed} = setup({failEnable: true});
  await wizard.next();
  await wizard.next();
  assert.equal(wizard.firstRunStatus, 'error');
  assert.equal(calls.some(x => x.endsWith('/run')), false);
  assert.equal(completed(), 0);
});

test('service setup enables only after the explicit action and never runs', async () => {
  const {wizard, calls, completed} = setup({kind: 'service'});
  await wizard.next();
  assert.deepEqual(calls, []);
  assert.equal(wizard.completion_label, 'Enable plugin');
  await wizard.next();
  assert.deepEqual(calls, ['/api/plugin/apple-notes/enable']);
  assert.equal(completed(), 1);
});

test('skipping preview input still saves the mode named by the explicit start action', async () => {
  const {wizard, calls, savedSettings} = setup({previewSetting: true});
  wizard.inputValues.dry_run = 'true';
  wizard.skipStep();
  assert.equal(wizard.completion_label, 'Enable & run preview');
  await wizard.next();
  assert.deepEqual(savedSettings(), {dry_run: true});
  assert.equal(calls[0], '/api/plugin/apple-notes/settings');
  assert.equal(calls.at(-1), '/api/plugin/apple-notes/run');
});

test('failure saving preview mode never enables or starts the plugin', async () => {
  const {wizard, calls} = setup({previewSetting: true, failSettings: true});
  wizard.inputValues.dry_run = 'true';
  wizard.skipStep();
  await wizard.next();
  assert.deepEqual(calls, ['/api/plugin/apple-notes/settings']);
  assert.equal(wizard.firstRunStatus, 'error');
});

test('rapid duplicate service start actions do not enable twice', async () => {
  const {wizard, calls, completed} = setup({kind: 'service'});
  await wizard.next();
  await Promise.all([wizard.next(), wizard.next()]);
  assert.deepEqual(calls, ['/api/plugin/apple-notes/enable']);
  assert.equal(completed(), 1);
});
