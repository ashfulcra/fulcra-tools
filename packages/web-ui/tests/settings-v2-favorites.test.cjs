const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const account = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
const other = 'aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff';
const choice = '00000000-0000-0000-0000-000000000021';
const hidden = '00000000-0000-0000-0000-000000000022';
const scope = {protocol_version: 2, account_id: account, config_epoch: 'captured'};
function view(changes = {}) {
  return {...scope, ok: true, definitions: [{id: choice, name: 'Synthetic choice',
    annotation_type: 'moment', digest: 'a'.repeat(64), pinned: false}],
    favorites: [hidden], favorites_revision: 4, recent: [], timers: [], ...changes};
}
function setup(handler) {
  const calls = [];
  const context = vm.createContext({console, window: {confirm: () => true},
    api: async (url, options = {}) => {calls.push({url, options}); return handler(url, options);}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../dist/static/settings.js'), 'utf8'), context);
  return {settings: context.settings(), calls};
}
const plain = value => JSON.parse(JSON.stringify(value));

test('favorite choices and scope load from one v2 handshake, separately from legacy management', async () => {
  const {settings, calls} = setup(() => view());
  settings.definitions = [{id: 'legacy-only', name: 'Management definition'}];
  await settings._loadFavorites();
  assert.equal(calls[0].url, '/api/quick-record/v2');
  assert.equal(settings.quickRecordChoices[0].id, choice);
  assert.equal(settings.definitions[0].id, 'legacy-only');
  assert.deepEqual(plain(settings.quickRecordScope), scope);
});

test('a pin sends one scope/revision/digest diff and preserves hidden pins', async () => {
  const {settings, calls} = setup((url, options) => options.method
    ? {...scope, ok: true, favorites: [hidden, choice], revision: 5} : view());
  await settings._loadFavorites();
  await settings.toggleFavorite(settings.quickRecordChoices[0]);
  const request = calls[1];
  assert.equal(request.url, '/api/quick-record/v2');
  assert.equal(request.options.method, 'POST');
  assert.deepEqual(JSON.parse(request.options.body), {...scope, operation: 'set_quick_record_favorites',
    choice_id: choice, choice_digest: 'a'.repeat(64), pinned: true, expected_revision: 4});
  assert.equal(settings.favorites.has(hidden) && settings.favorites.has(choice), true);
});

for (const response of [{...scope, ok: false}, {...scope, ok: true, revision: 5, favorites: []},
  {...scope, account_id: other, ok: true, revision: 5, favorites: [hidden, choice]}]) {
  test('failed, mismatched or contradictory pin response cannot acknowledge the checkbox ' + JSON.stringify(response), async () => {
    const {settings} = setup((url, options) => options.method ? response : view());
    await settings._loadFavorites();
    await settings.toggleFavorite(settings.quickRecordChoices[0]);
    assert.equal(settings.favorites.has(choice), false);
    assert.equal(settings.favorites.has(hidden), true);
    assert.ok(settings.favoritesError);
    assert.equal(settings.quickRecordScope, null);
  });
}

test('a degraded handshake is visible and grants no pin authority', async () => {
  const {settings, calls} = setup(() => ({ok: false}));
  await settings._loadFavorites();
  assert.ok(settings.favoritesError);
  assert.equal(settings.quickRecordScope, null);
  await settings.toggleFavorite({id: choice});
  assert.equal(calls.length, 1);
});

test('an old pin response cannot overwrite a newer account handshake', async () => {
  let finish;
  let current = view();
  const {settings} = setup((url, options) => options.method
    ? new Promise(resolve => {finish = resolve;}) : current);
  await settings._loadFavorites();
  const pending = settings.toggleFavorite(settings.quickRecordChoices[0]);
  current = view({account_id: other, config_epoch: 'other-epoch', favorites: [], favorites_revision: 0});
  await settings._loadFavorites();
  finish({...scope, ok: true, favorites: [hidden, choice], revision: 5});
  await pending;
  assert.equal(settings.quickRecordScope.account_id, other);
  assert.equal(settings.favorites.size, 0);
});
