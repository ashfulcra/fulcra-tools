const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
function wizard(api) {
  const context = vm.createContext({api, console, setTimeout: () => 1, clearTimeout() {}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../dist/static/wizard.js'), 'utf8'), context);
  return context.createWizard({id:'lastfm', required_credentials:[{key:'api-key', label:'API key'}],
    setup_steps:[{kind:'input', settings_keys:['api-key']}]}, () => {});
}
test('load preserves unknown credential status and never authorizes automatically', async () => {
  const calls = [];
  const w = wizard(async (url, opts) => {
    calls.push([url,opts?.method || 'GET']);
    return url.endsWith('/credentials') ? {ok:false, credentials:{'api-key':'access_required'}} : {};
  });
  await w._loadExisting();
  assert.equal(w._credStatus['api-key'], 'access_required');
  assert.equal(w._credPresent['api-key'], undefined);
  assert.ok(calls.every(([,method]) => method === 'GET'));
});
test('only an explicit declared credential action may authorize; refresh confirms status', async () => {
  const calls=[];
  const w=wizard(async(url,opts)=>{
    calls.push([url, opts?.method || 'GET']);
    if (url.endsWith('/authorize')) return {ok:true,status:'set'};
    return {ok:true,credentials:{'api-key':'set'}};
  });
  await w.authorizeCredential('wrong');
  assert.equal(calls.length,0);
  await w.authorizeCredential('api-key');
  assert.equal(calls.filter(([,method])=>method==='POST').length,1);
  assert.equal(calls[0][0],'/api/plugin/lastfm/credential/api-key/authorize');
  assert.equal(w._credPresent['api-key'],true);
});
test('pending clicks collapse and denial never marks the secret present', async () => {
  let finish;
  let posts=0;
  const w=wizard(async(url)=>{
    if (url.endsWith('/authorize')) {posts++; return await new Promise(r=>{finish=r;});}
    return {ok:false,credentials:{'api-key':'access_required'}};
  });
  const first=w.authorizeCredential('api-key');
  await w.authorizeCredential('api-key');
  assert.equal(posts,1);
  finish({ok:false,error:'Finish the Keychain access request.'});
  await first;
  assert.equal(w._credPresent['api-key'],undefined);
  assert.match(w._credAccessError, /Finish the Keychain/);
});

test('core keychain recovery is an explicit POST; status failure stays unknown', async () => {
  const calls=[];
  const context=vm.createContext({console,window:{},api:async(url,opts)=>{
    calls.push([url,opts?.method || 'GET']);
    if (url.endsWith('/keychain_access')) return {ok:true};
    return {authenticated:true,availability:"present",provenance:"fresh_quiet_keychain",
      checked_at:"2026-10-28T00:00:00Z",refresh_failed:false};
  }});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../dist/static/settings.js'),'utf8'),context);
  const s=context.settings();
  await s.authorizeFulcraKeychain();
  assert.equal(calls[0][0],'/api/fulcra/auth/keychain_access');
  assert.equal(calls[0][1],'POST');
  assert.equal(s.fulcraAuthStatus.authenticated,true);
});

test('blank optional credential does not block or overwrite an existing key', async () => {
  const calls=[];
  const context=vm.createContext({console,setTimeout:()=>1,clearTimeout(){},api:async(...args)=>{calls.push(args);return {};}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../dist/static/wizard.js'),'utf8'),context);
  const w=context.createWizard({id:'optional',required_credentials:[{key:'api-key',label:'API key',required:false}],
    setup_steps:[{kind:'input',settings_keys:['api-key']}]},()=>{});
  assert.equal(await w._submitInputs(w.current_step),true);
  assert.equal(calls.length,0);
});

test('authorizing a credential preserves an unsaved username draft', async () => {
  const calls=[];
  const w=wizard(async(url,options)=>{
    calls.push(url);
    if (url.endsWith('/settings')) return {username:'stored-old'};
    if (url.endsWith('/authorize')) return {ok:true,status:'set'};
    return {ok:true,credentials:{'api-key':'set'}};
  });
  w.inputValues.username='unsaved-new';
  await w.authorizeCredential('api-key');
  assert.equal(w.inputValues.username,'unsaved-new');
  assert.ok(!calls.some(url=>url.endsWith('/settings')));
});

test('leaving a step discards a late access response without rechecking credentials', async () => {
  let finish;
  const calls=[];
  const w=wizard(async(url)=>{
    calls.push(url);
    if (url.endsWith('/authorize')) return await new Promise(resolve=>{finish=resolve;});
    return {ok:true,credentials:{'api-key':'set'}};
  });
  const pending=w.authorizeCredential('api-key');
  w._invalidateStepWork();
  finish({ok:false,error:'late error'});
  await pending;
  assert.equal(calls.length,1);
  assert.equal(w._credAccessError,'');
});
