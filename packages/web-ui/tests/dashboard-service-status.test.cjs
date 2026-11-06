const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../dist/static/dashboard.js'), 'utf8'), context);

test('a service whose failure could not be persisted visibly shows UNKNOWN', () => {
  const plugin = {kind: 'service', enabled: true, last_outcome: 'error',
    service_status: 'unknown', consecutive_failures: 0};
  assert.match(context.pillFor(plugin).label, /unknown/i);
});

test('a confirmed nonzero service exit displays the existing failure badge', () => {
  const plugin = {kind: 'service', enabled: true, last_outcome: 'error', consecutive_failures: 1};
  assert.match(context.pillFor(plugin).label, /failed/i);
});

test('UNKNOWN wins over a previously successful cached status', () => {
  const plugin = {kind: 'service', enabled: true, last_outcome: 'done',
    service_status: 'unknown', consecutive_failures: 0};
  assert.match(context.pillFor(plugin).label, /unknown/i);
});
