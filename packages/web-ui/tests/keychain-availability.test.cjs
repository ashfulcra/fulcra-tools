const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
function settings(api) {
  const context = vm.createContext({api, console, window:{}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../dist/static/settings.js'),'utf8'),context);
  return context.settings();
}
test('settings starts unknown while its first quiet status is pending', async () => {
  let finish;
  const s = settings(() => new Promise(resolve => {finish=resolve;}));
  assert.equal(s.fulcraAuthStatus.authenticated, null);
  const pending=s._loadAuthStatus();
  assert.equal(s.fulcraAuthStatus.availability,'loading');
  finish({authenticated:null,availability:'access_required',refresh_failed:false});
  await pending;
  assert.equal(s.fulcraAuthStatus.authenticated,null);
});
test('a late older availability response cannot overwrite a newer result', async () => {
  const replies=[];
  const s=settings(() => new Promise(resolve=>replies.push(resolve)));
  const first=s._loadAuthStatus();
  const second=s._loadAuthStatus();
  replies[1]({authenticated:null,availability:'access_required',refresh_failed:false});
  await second;
  replies[0]({authenticated:true,availability:'present',refresh_failed:false,
    provenance:'fresh_quiet_keychain',checked_at:'2026-10-28T00:00:00Z'});
  await first;
  assert.equal(s.fulcraAuthStatus.authenticated,null);
  assert.equal(s.fulcraAuthStatus.availability,'access_required');
});

test('scoped recovery sends only its correct navigation action, never a run or grant', () => {
  const context=vm.createContext({});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../dist/static/dashboard.js'),'utf8'),context);
  const dashboard=context.dashboard();
  const events=[];
  dashboard.$dispatch=(...args)=>events.push(args);
  dashboard.recoverCredential({id:'sample',credential_error:{recovery:'settings'}});
  dashboard.recoverCredential({id:'sample',credential_error:{recovery:'configure'}});
  dashboard.recoverCredential({id:'sample'});
  assert.equal(events[0][0],'go-to-settings');
  assert.equal(events[1][0],'configure-plugin');
  assert.equal(events.length,2);
});

test('malformed or old cached-positive status stays unknown', async () => {
  const s=settings(async()=>({authenticated:true,refresh_failed:false}));
  await s._loadAuthStatus();
  assert.equal(s.fulcraAuthStatus.authenticated,null);
  assert.equal(s.fulcraAuthStatus.availability,'unavailable');
});
