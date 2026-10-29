const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const base = path.join(__dirname, '../dist/static');
function setup(handler) {
  const calls = [];
  const ctx = vm.createContext({console, URL, setTimeout: fn => fn(), window: {}, api: async (url, opts={}) => {
    calls.push({url, opts}); return handler(url, opts);
  }});
  const shared = path.join(base, 'browser-signin.js');
  if (fs.existsSync(shared)) vm.runInContext(fs.readFileSync(shared, 'utf8'), ctx);
  for (const file of ['onboarding.js', 'settings.js']) vm.runInContext(fs.readFileSync(path.join(base, file), 'utf8'), ctx);
  return {settings: ctx.settings(), onboarding: ctx.onboarding(), calls};
}
const started = {protocol: 2, attempt_id: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee', expires_in: 900, auth_url: 'https://fulcra.us.auth0.com/activate?user_code=TEST-CODE',
  web_auth_code: 'TEST-CODE', device_code: 'synthetic-secret-device-code'};

test('Settings renders the browser link while awaiting approval, then refreshes only on explicit success', async () => {
  let finish;
  const {settings, calls} = setup((url) => {
    if (url.endsWith('cli_login_start')) return started;
    if (url.endsWith('cli_login_poll')) return new Promise(resolve => { finish = resolve; });
    throw new Error('unexpected request');
  });
  const refreshed = [];
  settings._loadAuthStatus = async () => refreshed.push('status');
  settings._loadDefs = async () => refreshed.push('definitions');
  settings._loadFavorites = async () => refreshed.push('favorites');
  const pending = settings.reconnectToFulcra();
  for (let n=0;n<8 && !finish;n++) await Promise.resolve();
  assert.equal(settings.reconnectAuthUrl, started.auth_url);
  assert.equal(settings.reconnectWebAuthCode, started.web_auth_code);
  assert.equal(settings.reconnectInFlight, true);
  assert.deepEqual(refreshed, []);
  assert.ok(!calls.some(row => row.url === '/api/fulcra/auth/cli_login'));
  finish({ok: true, protocol: 2}); await pending;
  assert.deepEqual(refreshed.sort(), ['definitions', 'favorites', 'status']);
  assert.equal(settings.reconnectAuthUrl, '');
  assert.equal(settings.reconnectInFlight, false);
});

for (const result of [{}, {ok: false}, null]) {
  test('a missing or failed completion cannot report reconnect success: ' + JSON.stringify(result), async () => {
    const {settings} = setup(url => url.endsWith('cli_login_start') ? started : result);
    let refreshed = false;
    settings._loadAuthStatus = async () => { refreshed = true; };
    await settings.reconnectToFulcra();
    assert.equal(refreshed, false);
    assert.ok(settings.reconnectError);
    assert.equal(settings.reconnectAuthUrl, '');
  });
}

test('an unsupported sign-in response never falls back to a blocking CLI', async () => {
  const {onboarding, calls} = setup(() => { throw Object.assign(new Error('Update Collect'), {status: 409}); });
  await onboarding.signinViaCli();
  assert.deepEqual(calls.map(row => row.url), ['/api/fulcra/auth/cli_login_start']);
  assert.match(onboarding.signinError, /Update Collect/);
});

for (const auth_url of ['http://fulcra.us.auth0.com/activate', 'https://example.com/activate',
  Object.assign(new URL('https://fulcra.us.auth0.com/activate?user_code=TEST-CODE'),
    {username: 'user@example.com', password: 'synthetic'}).href,
  'https://fulcra.us.auth0.com/activate?user_code=TEST-CODE&user_code=OTHER-CODE',
  'https://fulcra.us.auth0.com/activate?user_code=TEST-CODE&user_code=TEST-CODE',
  'https://fulcra.us.auth0.com/activate?user_code=TEST-CODE&%75ser_code=OTHER-CODE']) {
  test('an invalid authorization origin is not rendered or polled: ' + auth_url, async () => {
    const {settings, calls} = setup(() => ({...started, auth_url}));
    await settings.reconnectToFulcra();
    assert.ok(settings.reconnectError);
    assert.equal(settings.reconnectAuthUrl, '');
    assert.equal(calls.length, 1);
  });
}


test('pending approval uses bounded protocol 2 polling with the same captured attempt and code', async () => {
  let polls = 0;
  const {onboarding, calls} = setup(url => {
    if (url.endsWith('cli_login_start')) return started;
    return ++polls === 1 ? {protocol: 2, ok: false, pending: true, retry_after: 5} : {protocol: 2, ok: true};
  });
  let completed = false;
  onboarding._onSigninSuccess = () => { completed = true; };
  await onboarding.signinViaCli();
  assert.equal(completed, true);
  assert.equal(polls, 2);
  for (const row of calls.slice(1)) assert.deepEqual(JSON.parse(row.opts.body), {
    protocol: 2, attempt_id: started.attempt_id, device_code: started.device_code,
  });
});

for (const value of [undefined, 0, 61, NaN, '5']) {
  test('invalid retry intervals cannot continue polling: ' + value, async () => {
    const {settings, calls} = setup(url => url.endsWith('cli_login_start') ? started :
      {protocol: 2, ok: false, pending: true, retry_after: value});
    await settings.reconnectToFulcra();
    assert.equal(calls.length, 2);
    assert.ok(settings.reconnectError);
  });
}


test('switching to a token cancels the captured attempt and ignores late approval', async () => {
  let finish;
  const {onboarding, calls} = setup(url => {
    if (url.endsWith('cli_login_start')) return started;
    if (url.endsWith('cli_login_poll')) return new Promise(resolve => { finish = resolve; });
    if (url.endsWith('/cancel')) return {ok: true};
    throw new Error('unexpected request');
  });
  let completed = false;
  onboarding._onSigninSuccess = () => { completed = true; };
  const pending = onboarding.signinViaCli();
  for (let n=0;n<8 && !finish;n++) await Promise.resolve();
  await onboarding.switchSigninMode('token');
  assert.equal(onboarding.signinMode, 'token');
  assert.deepEqual(JSON.parse(calls.at(-1).opts.body), {attempt_id: started.attempt_id});
  finish({protocol: 2, ok: true});
  await pending;
  assert.equal(completed, false);
  assert.equal(onboarding.signinError, '');
});

test('cancel before start response never renders or polls the delayed attempt', async () => {
  let finishStart;
  const {settings, calls} = setup(url => {
    if (url.endsWith('cli_login_start')) return new Promise(resolve => { finishStart = resolve; });
    if (url.endsWith('/cancel')) return {ok:true};
    throw new Error('must not poll after cancel');
  });
  const pending = settings.reconnectToFulcra();
  await settings.cancelReconnect();
  finishStart(started);
  await pending;
  assert.deepEqual(calls.map(row => row.url), ['/api/fulcra/auth/cli_login_start','/api/fulcra/auth/cancel']);
  assert.equal(settings.reconnectAuthUrl, '');
  assert.equal(settings.reconnectError, '');
});


test('an old cancellation failure cannot overwrite a newer successful reconnect', async () => {
  let finishOldPoll, rejectCancel;
  let starts = 0;
  const {settings} = setup(url => {
    if (url.endsWith('cli_login_start')) { ++starts; return started; }
    if (url.endsWith('cli_login_poll')) return starts === 1 ?
      new Promise(resolve => { finishOldPoll = resolve; }) : {protocol:2, ok:true};
    if (url.endsWith('/cancel')) return new Promise((resolve,reject) => { rejectCancel = reject; });
    throw new Error('unexpected request');
  });
  settings._loadAuthStatus = settings._loadDefs = settings._loadFavorites = async () => {};
  const oldPoll = settings.reconnectToFulcra();
  for (let n=0;n<8 && !finishOldPoll;n++) await Promise.resolve();
  const oldCancel = settings.cancelReconnect();
  await settings.reconnectToFulcra();
  rejectCancel(new Error('synthetic late cancellation failure'));
  await oldCancel;
  finishOldPoll({protocol:2,ok:true});
  await oldPoll;
  assert.equal(settings.reconnectError, '');
  assert.equal(settings.reconnectInFlight, false);
});
