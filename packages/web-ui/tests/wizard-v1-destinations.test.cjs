const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const destination = 'Event/11111111-2222-4333-8444-555555555555';

function setup({empty = false, fail = false, apiImpl, steps, kind = 'manual', onComplete = () => {}, onSkip, effect = () => {}, schedule = () => 1} = {}) {
  const calls = [];
  const context = vm.createContext({
    console, setTimeout: schedule, clearTimeout() {}, CustomEvent: class {},
    window: {Alpine: {effect}, addEventListener() {}, dispatchEvent() {}},
    api: async (url, options = {}) => {
      calls.push({url, options});
      if (apiImpl) return apiImpl(url, options);
      if (url.includes('/data-types')) {
        if (fail) throw new Error('Catalog read is unknown.');
        return {slot: 'listened', name: 'Listened', base_type: 'Event', selected: empty ? null : destination,
          destinations: empty ? [] : [{id: destination, name: 'Listened'}]};
      }
      if (url.includes('/data-type/recent')) return {entries: [{note: 'Synthetic entry', start_time: '2026-01-02T00:00:00Z'}], window_days: 30};
      return {ok: true};
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../dist/static/wizard.js'), 'utf8'), context);
  const wizard = context.createWizard({id: 'synthetic', kind, record_api_version: 'v1',
    canonical_definition_name: 'Listened', required_settings: [],
    setup_steps: steps || [{kind: 'definition_picker'}, {kind: 'done'}]}, onComplete, onSkip);
  return {wizard, calls};
}

test('v1 picker loads owned datatype choices without legacy annotation reads', async () => {
  const {wizard, calls} = setup();
  await wizard._loadDefinitions();
  assert.equal(wizard.dpIsV1, true);
  assert.equal(wizard.dpV1Slot, 'listened');
  assert.equal(wizard.dpSelectedId, destination);
  assert.equal(wizard.nextBlocked, false);
  assert.equal(calls[0].url, '/api/plugin/synthetic/data-types');
  assert.ok(calls.every(call => !call.url.includes('/definitions') && call.url !== '/api/status'));
});

test('v1 selection posts a complete destination and its slot', async () => {
  const {wizard, calls} = setup();
  await wizard._loadDefinitions();
  assert.equal(await wizard._submitDefinitionPick(), true);
  const post = calls.find(call => call.options.method === 'POST');
  assert.equal(post.url, '/api/plugin/synthetic/data-type');
  assert.deepEqual(JSON.parse(post.options.body), {slot: 'listened', data_type_id: destination});
});

test('a delayed Next save exposes pending state and coalesces repeated clicks', async () => {
  let reply;
  const pending = new Promise(resolve => { reply = resolve; });
  const {wizard, calls} = setup({apiImpl: (_url, options) => options.method === 'POST' ? pending : {}});
  wizard.dpV1Slot = 'listened';
  wizard.dpSelectedId = destination;
  const first = wizard.next();
  const repeated = wizard.next();
  assert.equal(wizard.nextPending, true);
  assert.equal(calls.filter(call => call.options.method === 'POST').length, 1);
  reply({ok: true});
  await Promise.all([first, repeated]);
  assert.equal(wizard.step_index, 1);
  assert.equal(wizard.nextPending, false);
});

test('a failed Next save stays on its step and permits a visible retry', async () => {
  const {wizard} = setup({apiImpl: () => { throw new Error('Synthetic save failed'); }});
  wizard.dpV1Slot = 'listened';
  wizard.dpSelectedId = destination;
  await wizard.next();
  assert.equal(wizard.step_index, 0);
  assert.equal(wizard.nextPending, false);
  assert.match(wizard.stepError, /Synthetic save failed/);
});

test('a late saved choice cannot advance again after the user skips its step', async () => {
  let reply;
  const pending = new Promise(resolve => { reply = resolve; });
  const {wizard} = setup({steps: [{kind: 'definition_picker'}, {kind: 'info'}, {kind: 'done'}],
    apiImpl: (_url, options) => options.method === 'POST' ? pending : {}});
  wizard.dpV1Slot = 'listened';
  wizard.dpSelectedId = destination;
  const saving = wizard.next();
  wizard.skipStep();
  assert.equal(wizard.step_index, 1);
  reply({ok: true});
  await saving;
  assert.equal(wizard.step_index, 1);
  assert.equal(wizard.nextPending, false);
});

test('leaving a pending service confirmation cannot later enable the plugin', async () => {
  let completed = 0, skipped = 0;
  const {wizard, calls} = setup({kind: 'service', steps: [{kind: 'done'}],
    onComplete: () => { completed += 1; }, onSkip: () => { skipped += 1; }});
  wizard._saveSelectionsBeforeEnable = async () => true;
  const saving = wizard.next();
  wizard.skipPlugin();
  await saving;
  assert.equal(completed, 0);
  assert.equal(skipped, 1);
  assert.ok(calls.every(call => !call.url.endsWith('/enable')));
});

test('empty successful v1 catalog offers verified use-or-create during setup', async () => {
  const {wizard, calls} = setup({empty: true});
  await wizard._loadDefinitions();
  assert.equal(wizard.dpForceNew, true);
  assert.equal(await wizard._submitDefinitionPick(), true);
  const post = calls.find(call => call.options.method === 'POST');
  assert.equal(post.url, '/api/plugin/synthetic/data-type');
  assert.deepEqual(JSON.parse(post.options.body), {slot: 'listened', use_or_create: true});
});

test('unknown v1 catalog cannot turn into an empty create offer', async () => {
  const {wizard, calls} = setup({fail: true});
  await wizard._loadDefinitions();
  assert.equal(wizard.dpForceNew, false);
  assert.equal(wizard.nextBlocked, true);
  assert.match(wizard.dpError, /unknown/);
  assert.equal(await wizard._submitDefinitionPick(), false);
  assert.ok(calls.every(call => call.options.method !== 'POST'));
});

test('v1 preview uses the owned-type endpoint and recognizes flat fields including zero', async () => {
  const {wizard, calls} = setup();
  await wizard._loadDefinitions();
  const def = wizard.dpDefinitions[0];
  await wizard.dpLoadPreview(def);
  assert.ok(calls.some(call => call.url.startsWith('/api/plugin/synthetic/data-type/recent?') && call.url.includes('Event%2F')));
  assert.match(wizard.dpEntryLabel({note: 'Synthetic entry', start_time: '2026-01-02T00:00:00Z'}), /Synthetic entry/);
  assert.match(wizard.dpEntryLabel({value: 0, unit: 'percent', start_time: '2026-01-02T00:00:00Z'}), /0 percent/);
});

test('late destination replies cannot reconfigure a different wizard step', async () => {
  let reply;
  const delayed = new Promise(resolve => { reply = resolve; });
  const {wizard} = setup({apiImpl: () => delayed});
  const loading = wizard._loadDefinitions();
  wizard.current_step = {kind: 'done'};
  wizard.step_index = 1;
  reply({slot: 'listened', name: 'Listened', base_type: 'Event', destinations: []});
  await loading;
  assert.equal(wizard.dpForceNew, false);
  assert.equal(wizard.dpDefinitions.length, 0);
});

test('v1 creation copy describes data types created during setup', async () => {
  const {wizard} = setup({empty: true});
  await wizard._loadDefinitions();
  let Component;
  const context = vm.createContext({FulcraStepBase: class {}, nothing: '', unsafeHTML: value => value,
    html: (strings, ...values) => ({strings, values}),
    customElements: {define: (_name, klass) => { Component = klass; }}, window: {FulcraStepComponents: {}}});
  const source = fs.readFileSync(path.join(__dirname, '../dist/static/components/step-definition_picker.js'), 'utf8')
    .replace(/^import .*;$/mg, '');
  vm.runInContext(source, context);
  const component = new Component();
  function text(value) {
    if (typeof value === 'string' || typeof value === 'number') return String(value);
    if (Array.isArray(value)) return value.map(text).join(' ');
    if (value?.strings) return text(value.strings) + ' ' + text(value.values);
    return '';
  }
  const copy = text(component._renderCreateNewFooter(wizard));
  assert.match(copy, /data type/i);
  assert.match(copy, /use or create/i);
  assert.doesNotMatch(copy, /first run/i);
  component.ctx = wizard;
  wizard.dpLegacyMapping = true;
  assert.match(text(component.render()), /previous annotation selection is retained for migration/);
});

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

test('Skip releases Next while the old save is pending', async () => {
  const old = deferred();
  const {wizard} = setup({steps: [{kind: 'definition_picker'}, {kind: 'info'}, {kind: 'done'}],
    apiImpl: () => old.promise});
  wizard.dpV1Slot = 'listened'; wizard.dpSelectedId = destination;
  const saving = wizard.next();
  wizard.skipStep();
  assert.equal(wizard.nextPending, false);
  await wizard.next();
  assert.equal(wizard.step_index, 2);
  old.resolve({ok: true}); await saving;
  assert.equal(wizard.step_index, 2);
});

test('an old finally cannot release a newer pending Next or erase its error', async () => {
  const old = deferred(), newer = deferred();
  const {wizard} = setup({steps: [{kind: 'definition_picker'}, {kind: 'definition_picker'}, {kind: 'done'}],
    apiImpl: () => old.promise});
  let count = 0;
  wizard._submitDefinitionPick = async () => { await (++count === 1 ? old.promise : newer.promise); return true; };
  const first = wizard.next(); wizard.skipStep();
  const second = wizard.next();
  wizard.stepError = 'New step message';
  old.resolve({}); await first;
  assert.equal(wizard.nextPending, true);
  assert.equal(wizard.stepError, 'New step message');
  newer.resolve({}); await second;
  assert.equal(wizard.nextPending, false);
});

test('an already sent service enable settles after Back without completing a departed screen', async () => {
  const enabled = deferred(), entered = deferred(); let completed = 0;
  const {wizard, calls} = setup({kind: 'service', steps: [{kind: 'info'}, {kind: 'done'}],
    onComplete: () => { completed += 1; },
    apiImpl: url => { if (url.endsWith('/enable')) { entered.resolve(); return enabled.promise; } return {}; }});
  wizard.skipStep(); const start = wizard.next(); await entered.promise;
  wizard.back(); enabled.resolve({}); await start;
  assert.equal(wizard.firstRunStatus, 'done');
  assert.equal(completed, 0);
  await wizard.next(); await wizard.next();
  assert.equal(completed, 1);
  assert.equal(calls.filter(call => call.url.endsWith('/enable')).length, 1);
});

for (const stage of ['save', 'settings', 'status', 'enable', 'run']) {
  test(`leaving manual confirmation during ${stage} stops subsequent start actions`, async () => {
    const wait = deferred(), entered = deferred();
    const {wizard, calls} = setup({steps: [{kind: 'done'}], apiImpl: url => {
      if ((stage === 'settings' && url.endsWith('/settings')) ||
          (stage === 'status' && url === '/api/status') || url.endsWith('/' + stage)) {
        entered.resolve(); return wait.promise;
      }
      return {};
    }});
    wizard._saveSelectionsBeforeEnable = async () => {
      if (stage === 'save') { entered.resolve(); await wait.promise; } return true;
    };
    if (stage === 'settings') { wizard.settingsMap.dry_run = {}; wizard.inputValues.dry_run = true; }
    const start = wizard.next(); await entered.promise;
    wizard.skipPlugin(); wait.resolve({}); await start;
    const urls = calls.map(call => call.url);
    if (['save', 'settings', 'status'].includes(stage)) assert.ok(!urls.some(url => url.endsWith('/enable')));
    if (stage !== 'run') assert.ok(!urls.some(url => url.endsWith('/run')));
    assert.notEqual(wizard.firstRunStatus, 'running');
    assert.equal(wizard.firstRunPollTimer, null);
  });
}

test('Lit bridge subscribes to the pending state used by picker controls', () => {
  let callback;
  const {wizard} = setup({effect: fn => { callback = fn; }});
  const reads = new Set();
  wizard._installLitReactivityBridge.call(new Proxy(wizard, {get(target, key, receiver) {
    reads.add(key); return Reflect.get(target, key, receiver);
  }}));
  callback();
  assert.ok(reads.has('nextPending'));
  assert.ok(reads.has('_nextPendingEpoch'));
  assert.ok(reads.has('_nextEpoch'));
});

test('leaving during an in-flight first-run poll settles the UI and never schedules another poll', async () => {
  let tick, timers = 0, statusCalls = 0;
  const wait = deferred(), entered = deferred();
  const {wizard} = setup({steps: [{kind: 'info'}, {kind: 'done'}],
    schedule: fn => { tick = fn; timers += 1; return timers; },
    apiImpl: url => {
      if (url === '/api/status' && ++statusCalls === 2) { entered.resolve(); return wait.promise; }
      return {};
    }});
  wizard.skipStep(); await wizard.next();
  const polling = tick(); await entered.promise;
  wizard.back();
  assert.equal(wizard.firstRunStatus, 'slow');
  wait.resolve({}); await polling;
  assert.equal(wizard.firstRunStatus, 'slow');
  assert.equal(wizard.firstRunPollTimer, null);
  assert.equal(timers, 1);
});

test('an old destination failure cannot overwrite the current step message', async () => {
  const wait = deferred();
  const {wizard} = setup({steps: [{kind: 'definition_picker'}, {kind: 'info'}], apiImpl: () => wait.promise});
  wizard.dpV1Slot = 'listened'; wizard.dpSelectedId = destination;
  const save = wizard.next(); wizard.skipStep(); wizard.stepError = 'Current step message';
  wait.reject(new Error('Old save failure')); await save;
  assert.equal(wizard.stepError, 'Current step message');
});

test('leaving an input step stops subsequent field saves', async () => {
  const wait = deferred(), calls = [];
  const {wizard} = setup({steps: [{kind: 'input', settings_keys: ['one', 'two']}, {kind: 'info'}],
    apiImpl: (url, options) => { calls.push({url, options}); return wait.promise; }});
  wizard.settingsMap.one = {key: 'one', label: 'One'};
  wizard.settingsMap.two = {key: 'two', label: 'Two'};
  wizard.inputValues = {one: 'first', two: 'second'};
  const save = wizard.next(); wizard.skipStep(); wait.resolve({}); await save;
  assert.equal(calls.length, 1);
  assert.equal(wizard.step_index, 1);
});
