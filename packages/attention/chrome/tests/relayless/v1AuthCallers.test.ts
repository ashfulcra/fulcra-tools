// @vitest-environment node
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {IDBFactory} from 'fake-indexeddb';
vi.stubGlobal('fetch',()=>{throw Error('External network forbidden in Slice A callers');});
beforeEach(async()=>{await chrome.storage.local.clear();await chrome.storage.local.set({attentionAuthMode:'v1-held'});vi.stubGlobal('indexedDB',new IDBFactory());vi.resetModules();vi.mocked(chrome.runtime.sendMessage).mockImplementation(async message=>(await import('../../src/relayless/v1AuthWorker')).handleV1AuthCommand(message));});
afterEach(()=>{vi.restoreAllMocks();});
test('opted-in actual legacy publisher refuses force refresh before transport',async()=>{
 const {TokenStore}=await import('../../src/relayless/tokenStore');await chrome.storage.local.set({relaylessTokens:{accessToken:'synthetic-legacy',refreshToken:'synthetic-r',expiresAt:0}});
 await expect(new TokenStore().getValidAccessToken({force:true})).rejects.toThrow('WORKER_OWNER_REQUIRED');
});
test('opted-in actual resolver cannot create or retag through old cache path',async()=>{
 const {ensureAttentionDefinitionAndTags,updateIdentity}=await import('../../src/relayless/ensureDefinition');await chrome.storage.local.set({relaylessResolvedAttention:{definitionId:'synthetic-id',tagIds:['synthetic-tag']}});
 const getToken=async()=>{throw Error('Legacy token accessor forbidden');};
 await expect(ensureAttentionDefinitionAndTags({getToken})).rejects.toThrow('WORKER_OWNER_REQUIRED');
 await expect(updateIdentity({getToken},'synthetic-label')).rejects.toThrow('WORKER_OWNER_REQUIRED');
});
test('opted-in flush is held and leaves legacy queue/cache/sent evidence untouched',async()=>{
 const evidence={outbox:[{id:'synthetic-row',payload:{url:'https://synthetic.invalid/',title:null,og_description:null,favicon_url:null,category:null,chrome_identity:null,og_type:null,lang:null,start_time:'2026-01-01T00:00:00Z',end_time:'2026-01-01T00:00:01Z',client:'synthetic'},queuedAt:0,attempts:0}],relaylessSentIds:['synthetic-sent'],relaylessResolvedAttention:{definitionId:'synthetic-id',tagIds:[]},relaylessTokens:{accessToken:'synthetic-legacy',expiresAt:9999999999999}};await chrome.storage.local.set(evidence);
 const before=await chrome.storage.local.get(null);const {flushOutbox}=await import('../../src/outbox');await flushOutbox();expect(await chrome.storage.local.get(null)).toEqual(before);
});
test('actual sign-in entry routes command and never uses old provider/persistence',async()=>{
 const {startDeviceSignIn}=await import('../../src/relayless/signIn');const send=vi.spyOn(chrome.runtime,'sendMessage').mockImplementation(async(message:any)=>{if(message.command==='signin-start')return {version:1,ok:true,result:{transitionId:'synthetic-job',verificationUriComplete:'https://synthetic.invalid/approve',userCode:'SYNTHETIC'}};return {version:1,ok:true,result:{status:'done',display:{status:'committed'}}};});
 vi.useFakeTimers();try{const result=startDeviceSignIn({onPrompt:()=>{},registerOriginStrip:async()=>{}});const checked=expect(result).resolves.toEqual({ok:true});await vi.advanceTimersByTimeAsync(1001);await checked;expect(send.mock.calls.every(([m])=>(m as any).type==='attention-auth-v1')).toBe(true);expect((await chrome.storage.local.get('relaylessTokens')).relaylessTokens).toBeUndefined();}finally{vi.useRealTimers();}
});
test('actual settings route is versioned and waits for worker acknowledgment',async()=>{
 const {saveSettings}=await import('../../src/storage');const {DEFAULT_SETTINGS}=await import('../../src/types');
 vi.spyOn(chrome.runtime,'sendMessage').mockResolvedValue({version:1,ok:false,code:'SCOPE_UNKNOWN'});
 await expect(saveSettings({...DEFAULT_SETTINGS,identityLabel:'Synthetic'})).rejects.toThrow('SCOPE_UNKNOWN');expect((await chrome.storage.local.get('settings')).settings).toBeUndefined();
});
test('actual worker command listener admits manifest index popup and refuses content-script sender',async()=>{
 await import('../../src/background');const listeners=vi.mocked(chrome.runtime.onMessage.addListener).mock.calls.map(([fn])=>fn);
 const message={type:'attention-auth-v1',version:1,command:'display',payload:null},results:any[]=[];
 for(const listener of listeners)listener(message,{url:chrome.runtime.getURL('index.html')} as any,r=>results.push(r));
 await vi.waitFor(()=>expect(results.length).toBeGreaterThan(0));expect(results.some(r=>r.ok===true&&r.result.mode==='v1-held')).toBe(true);
 const rejected:any[]=[];for(const listener of listeners)listener(message,{url:'https://synthetic.invalid/'} as any,r=>rejected.push(r));expect(rejected).toContainEqual({version:1,ok:false,code:'SENDER_REFUSED'});
});
test('worker-local settings invoke owner directly, without a worker-to-self message',async()=>{
 const {saveWorkerSettings,loadWorkerSettings}=await import('../../src/relayless/v1AuthWorker');const {DEFAULT_SETTINGS}=await import('../../src/types');vi.spyOn(chrome.runtime,'sendMessage').mockImplementation(async()=>{throw Error('Self message forbidden');});
 await saveWorkerSettings({...DEFAULT_SETTINGS,enabled:false});expect((await loadWorkerSettings()).enabled).toBe(false);expect((await chrome.storage.local.get('settings')).settings).toBeUndefined();
});
test('old/unknown auth command versions and token-bearing message extensions are refused',async()=>{
 const {handleV1AuthCommand}=await import('../../src/relayless/v1AuthWorker');
 for(const msg of [{type:'auth',version:0,command:'signout',payload:null},{type:'attention-auth-v1',version:0,command:'signout',payload:null},{type:'attention-auth-v1',version:1,command:'display',payload:null,access_token:'synthetic'}])expect(await handleV1AuthCommand(msg)).toMatchObject({ok:false,code:'PROTOCOL_MISMATCH'});
 expect(await handleV1AuthCommand({type:'attention-auth-v1',version:1,command:'publish-token',payload:{access_token:'synthetic'}})).toMatchObject({ok:false,code:'PROTOCOL_MISMATCH'});
});
test('standalone readonly settings projection cannot initialize a missing broker or fall back to legacy',async()=>{
 const {readV1Settings}=await import('../../src/relayless/v1AuthStorage');await chrome.storage.local.set({settings:{enabled:true,identityLabel:'Legacy'}});
 await expect(readV1Settings()).rejects.toThrow();expect(await indexedDB.databases()).toEqual([]);
});
test('page settings projection ignores late legacy overwrites after worker initialization',async()=>{
 const {loadSettings}=await import('../../src/storage'),{saveWorkerSettings}=await import('../../src/relayless/v1AuthWorker'),{DEFAULT_SETTINGS}=await import('../../src/types');
 await saveWorkerSettings({...DEFAULT_SETTINGS,identityLabel:'Owned synthetic'});await chrome.storage.local.set({settings:{...DEFAULT_SETTINGS,identityLabel:'Legacy late'}});
 expect((await loadSettings()).identityLabel).toBe('Owned synthetic');
});
test.each(['missing','corrupt-settings','reset-initialized'])('broker state %s after initialization is held, never reconstructed from old local settings',async mode=>{
 const {loadSettings}=await import('../../src/storage'),{saveWorkerSettings}=await import('../../src/relayless/v1AuthWorker'),{DEFAULT_SETTINGS}=await import('../../src/types');await saveWorkerSettings(DEFAULT_SETTINGS);await chrome.storage.local.set({settings:{...DEFAULT_SETTINGS,identityLabel:'Forbidden fallback'}});
 const db=await new Promise<IDBDatabase>((resolve,reject)=>{const r=indexedDB.open('fulcra-attention-auth-v1');r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
 await new Promise<void>((resolve,reject)=>{const tx=db.transaction('state','readwrite'),store=tx.objectStore('state'),r=store.get('authority');r.onsuccess=()=>{if(mode==='missing')store.delete('authority');else store.put({...r.result,settings:mode==='corrupt-settings'?{enabled:true}:null},'authority');};tx.oncomplete=()=>resolve();tx.onabort=()=>reject(tx.error);});db.close();
 await expect(loadSettings()).rejects.toThrow();
});
test('worker boot heartbeat withdrawal uses concrete owner saver and authoritative settings',async()=>{
 const {saveWorkerSettings,loadWorkerSettings}=await import('../../src/relayless/v1AuthWorker'),{DEFAULT_SETTINGS}=await import('../../src/types'),{reconcileHeartbeatOnBoot}=await import('../../src/heartbeat-control');
 await saveWorkerSettings({...DEFAULT_SETTINGS,heartbeatEnabled:true});vi.spyOn(chrome.permissions,'contains').mockResolvedValue(false as never);await reconcileHeartbeatOnBoot(saveWorkerSettings,loadWorkerSettings);expect((await loadWorkerSettings()).heartbeatEnabled).toBe(false);
});

async function pendingWorkerSignin(){
 const {V1AuthBroker}=await import('../../src/relayless/v1AuthBroker'),{V1AuthStorage,makeV1Credential}=await import('../../src/relayless/v1AuthStorage'),{V1Journal}=await import('../../src/relayless/v1Journal');
 const storage=new V1AuthStorage(),journal=new V1Journal(),broker=new V1AuthBroker({storage,journal});await broker.initialize();const t=await (broker as any).claimScope('signin');await storage.retainCredential(makeV1Credential(t.receiptId,t.transitionId,null,{access_token:'synthetic-worker',expires_in:3600},1700000000000));await storage.close();await journal.close();return t;
}
test.each(['signout','settings'])('I2 actual cold worker %s bypasses online recovery and fences its publisher',async command=>{
 await pendingWorkerSignin();let entered!:()=>void,release!:(r:Response)=>void;const atProof=new Promise<void>(r=>{entered=r;}),proof=new Promise<Response>(r=>{release=r;});vi.stubGlobal('fetch',async(input:RequestInfo|URL)=>{if(new URL(String(input)).pathname.endsWith('/info')){entered();return proof;}throw Error('External network forbidden');});
 const {handleV1AuthCommand}=await import('../../src/relayless/v1AuthWorker'),{DEFAULT_SETTINGS}=await import('../../src/types');let displayDone=false,revokeDone=false;
 const display=handleV1AuthCommand({type:'attention-auth-v1',version:1,command:'display',payload:null}).then(r=>{displayDone=true;return r;});await atProof;
 const revoke=handleV1AuthCommand({type:'attention-auth-v1',version:1,command,payload:command==='settings'?{...DEFAULT_SETTINGS,enabled:false}:null}).then(r=>{revokeDone=true;return r;});
 try{await vi.waitFor(()=>expect(revokeDone).toBe(true),{timeout:150});expect(displayDone).toBe(true);expect(await revoke).toMatchObject({ok:true});}finally{release(Response.json({userid:'11111111-2222-4333-8444-555555555555'}));await Promise.all([display,revoke]);}
});
test('I4 actual worker refuses settings projection after entire broker database loss',async()=>{
 const {V1AuthBroker}=await import('../../src/relayless/v1AuthBroker'),{V1AuthStorage}=await import('../../src/relayless/v1AuthStorage'),{V1Journal}=await import('../../src/relayless/v1Journal'),{DEFAULT_SETTINGS}=await import('../../src/types');const storage=new V1AuthStorage(),journal=new V1Journal(),broker=new V1AuthBroker({storage,journal});await broker.initialize();await broker.changeSettings({...DEFAULT_SETTINGS,enabled:false});await storage.close();await journal.close();
 await chrome.storage.local.set({settings:{...DEFAULT_SETTINGS,enabled:true,identityLabel:'Forbidden legacy fallback'}});await new Promise<void>((resolve,reject)=>{const r=indexedDB.deleteDatabase('fulcra-attention-auth-v1');r.onsuccess=()=>resolve();r.onerror=()=>reject(r.error);r.onblocked=()=>reject(Error('Synthetic database still open'));});
 const {handleV1AuthCommand,loadWorkerSettings}=await import('../../src/relayless/v1AuthWorker');expect(await handleV1AuthCommand({type:'attention-auth-v1',version:1,command:'display',payload:null})).toMatchObject({ok:true,result:{status:'held'}});await expect(loadWorkerSettings()).rejects.toThrow();
});
test('I5 actual worker auto-unpause retains authenticated account and allocates fresh consent generation',async()=>{
 const {V1AuthStorage}=await import('../../src/relayless/v1AuthStorage'),{V1AuthBroker}=await import('../../src/relayless/v1AuthBroker'),{V1Journal}=await import('../../src/relayless/v1Journal'),{DEFAULT_SETTINGS}=await import('../../src/types'),account='11111111-2222-4333-8444-555555555555';
 vi.stubGlobal('fetch',async(input:RequestInfo|URL)=>{if(new URL(String(input)).pathname.endsWith('/info'))return Response.json({userid:account});throw Error('Synthetic route refused');});
 const storage=new V1AuthStorage(),journal=new V1Journal(),broker=new V1AuthBroker({storage,journal});await broker.initialize();await (broker as any).completeSignIn({access_token:'synthetic-account',refresh_token:'synthetic-refresh',expires_in:3600},await (broker as any).claimScope('signin'));await broker.changeSettings({...DEFAULT_SETTINGS,pausedUntil:1700000001000});const paused=await storage.readState();expect((await broker.display()).status).toBe('paused');await storage.close();await journal.close();
 const {sweepStaleBlurred}=await import('../../src/background'),{loadWorkerSettings,handleV1AuthCommand}=await import('../../src/relayless/v1AuthWorker');vi.spyOn(chrome.runtime,'sendMessage').mockImplementation(async()=>{throw Error('Worker self message forbidden');});await sweepStaleBlurred(1700000002000);
 const reader=new V1AuthStorage({readOnly:true});try{const resumed=await reader.readState();expect((await loadWorkerSettings()).pausedUntil).toBeNull();expect(resumed.committed?.accountId).toBe(account);expect(resumed.committed?.generation).not.toBe(paused.committed!.generation);expect(resumed.committed?.consentRevision).not.toBe(paused.committed!.consentRevision);expect(resumed.gate?.enabled).toBe(true);expect(await handleV1AuthCommand({type:'attention-auth-v1',version:1,command:'display',payload:null})).toMatchObject({ok:true,result:{status:'committed',accountId:account}});}finally{await reader.close();}
});
test('I1/I2 display cannot launch fresh old recovery while first local revocation is paused before intent',async()=>{
 await pendingWorkerSignin();const {V1AuthStorage}=await import('../../src/relayless/v1AuthStorage'),read=V1AuthStorage.prototype.readState;let entered!:()=>void,release!:()=>void,paused=false,proofs=0;const atClaim=new Promise<void>(r=>{entered=r;}),continueClaim=new Promise<void>(r=>{release=r;});
 vi.stubGlobal('fetch',async(input:RequestInfo|URL)=>{if(new URL(String(input)).pathname.endsWith('/info')){proofs++;return Response.json({userid:'11111111-2222-4333-8444-555555555555'});}throw Error('Synthetic route refused');});
 const spy=vi.spyOn(V1AuthStorage.prototype,'readState').mockImplementation(async function(this:InstanceType<typeof V1AuthStorage>){if(!paused){paused=true;entered();await continueClaim;}return read.call(this);});
 const {handleV1AuthCommand}=await import('../../src/relayless/v1AuthWorker'),revoking=handleV1AuthCommand({type:'attention-auth-v1',version:1,command:'signout',payload:null});await atClaim;
 try{expect(await handleV1AuthCommand({type:'attention-auth-v1',version:1,command:'display',payload:null})).toMatchObject({ok:true,result:{status:'held'}});await new Promise(r=>setTimeout(r,25));expect(proofs).toBe(0);const reader=new V1AuthStorage({readOnly:true});try{expect((await read.call(reader)).committed).toBeNull();}finally{await reader.close();}}finally{release();await revoking;spy.mockRestore();}
});
test.each(['signout','settings'])('R2 I1 actual worker new refresh cannot adopt old attempt during predurable %s',async command=>{
 const {V1AuthStorage}=await import('../../src/relayless/v1AuthStorage'),{V1AuthBroker}=await import('../../src/relayless/v1AuthBroker'),{V1Journal}=await import('../../src/relayless/v1Journal'),{DEFAULT_SETTINGS}=await import('../../src/types'),account='11111111-2222-4333-8444-555555555555';let grantEntered!:()=>void,grantRelease!:(r:Response)=>void,grants=0;const atGrant=new Promise<void>(r=>{grantEntered=r;}),grant=new Promise<Response>(r=>{grantRelease=r;});
 vi.stubGlobal('fetch',async(input:RequestInfo|URL)=>{const path=new URL(String(input)).pathname;if(path.endsWith('/info'))return Response.json({userid:account});if(path==='/oauth/token'){grants++;grantEntered();return grant;}throw Error('Synthetic route refused');});const storage=new V1AuthStorage(),journal=new V1Journal(),broker=new V1AuthBroker({storage,journal});await broker.initialize();await (broker as any).completeSignIn({access_token:'synthetic-before',refresh_token:'synthetic-r',expires_in:3600},await (broker as any).claimScope('signin'));const before=(await storage.readState()).committed!;await storage.close();await journal.close();
 const {handleV1AuthCommand,initializeWorkerAuth}=await import('../../src/relayless/v1AuthWorker');await initializeWorkerAuth();const envelope=(command:string,payload:unknown=null)=>({type:'attention-auth-v1',version:1,command,payload});const renewing=handleV1AuthCommand(envelope('refresh',{expectedPublicationId:before.publicationId}));await atGrant;
 const read=V1AuthStorage.prototype.readState;let entered!:()=>void,release!:()=>void,paused=false;const atClaim=new Promise<void>(r=>{entered=r;}),continueClaim=new Promise<void>(r=>{release=r;});const spy=vi.spyOn(V1AuthStorage.prototype,'readState').mockImplementation(async function(this:InstanceType<typeof V1AuthStorage>){if(!paused){paused=true;entered();await continueClaim;}return read.call(this);});const revoking=handleV1AuthCommand(envelope(command,command==='settings'?{...DEFAULT_SETTINGS,enabled:false}:null));await atClaim;const observer=new V1AuthStorage();
 try{grantRelease(Response.json({access_token:'synthetic-late',refresh_token:'synthetic-late-r',expires_in:3600}));expect(await renewing).toMatchObject({ok:true,result:{status:'held'}});expect(await handleV1AuthCommand(envelope('refresh',{expectedPublicationId:before.publicationId}))).toMatchObject({ok:true,result:{status:'held'}});expect((await read.call(observer)).committed).toEqual(before);const a=(await observer.readAttempts())[0];expect(a.state).not.toBe('COMMITTED_SUCCESSOR');expect((await observer.readCredential(a.receiptId)).response.refresh_token).toBe('synthetic-late-r');expect(grants).toBe(1);
 }finally{release();await revoking;spy.mockRestore();await observer.close();}
});
// Round 3 N1: real page settings access synchronizes only with local worker readiness.
test.each(['fresh','pending-online'])('N1 actual page settings waits for %s local initialization without waiting online recovery',async kind=>{
 if(kind==='pending-online')await pendingWorkerSignin();const {V1AuthStorage}=await import('../../src/relayless/v1AuthStorage'),initialize=V1AuthStorage.prototype.initializeAuthority;let entered!:()=>void,release!:()=>void,proofRelease!:(r:Response)=>void,proofEntered!:()=>void,first=true,proofs=0;const atLocal=new Promise<void>(r=>{entered=r;}),localReady=new Promise<void>(r=>{release=r;}),proof=new Promise<Response>(r=>{proofRelease=r;}),atProof=new Promise<void>(r=>{proofEntered=r;});
 const spy=vi.spyOn(V1AuthStorage.prototype,'initializeAuthority').mockImplementation(async function(this:InstanceType<typeof V1AuthStorage>,...args){if(first){first=false;entered();await localReady;}return initialize.apply(this,args);});vi.stubGlobal('fetch',async(input:RequestInfo|URL)=>{if(new URL(String(input)).pathname.endsWith('/info')){proofs++;proofEntered();return proof;}throw Error('External network forbidden');});
 const {handleV1AuthCommand,initializeWorkerAuth}=await import('../../src/relayless/v1AuthWorker'),{loadSettings}=await import('../../src/storage'),{DEFAULT_SETTINGS}=await import('../../src/types');vi.spyOn(chrome.runtime,'sendMessage').mockImplementation(async(message)=>handleV1AuthCommand(message));let authDone=false,pageDone=false,pageError:unknown;const startup=initializeWorkerAuth().finally(()=>{authDone=true;});await atLocal;const page=loadSettings().then(s=>{pageDone=true;return s;},e=>{pageDone=true;pageError=e;return undefined;});
 try{await new Promise(r=>setTimeout(r,25));expect(pageError).toBeUndefined();expect(pageDone).toBe(false);release();expect(await page).toEqual(DEFAULT_SETTINGS);expect(pageError).toBeUndefined();if(kind==='pending-online'){await atProof;expect(authDone).toBe(false);expect(proofs).toBe(1);}expect(vi.mocked(chrome.runtime.sendMessage).mock.calls.map(([m])=>(m as any).command)).toEqual(['settings-read']);}finally{release();proofRelease(Response.json({userid:'11111111-2222-4333-8444-555555555555'}));await startup;await page;spy.mockRestore();}
});
test('N1 settings-read wakes only local worker owner and leaves later real online recovery intact',async()=>{
 await pendingWorkerSignin();const {handleV1AuthCommand,initializeWorkerAuth}=await import('../../src/relayless/v1AuthWorker'),{loadSettings}=await import('../../src/storage'),{DEFAULT_SETTINGS}=await import('../../src/types');let release!:(r:Response)=>void,entered!:()=>void,proofs=0;const atProof=new Promise<void>(r=>{entered=r;}),proof=new Promise<Response>(r=>{release=r;});vi.stubGlobal('fetch',async(input:RequestInfo|URL)=>{if(new URL(String(input)).pathname.endsWith('/info')){proofs++;entered();return proof;}throw Error('External network forbidden');});vi.spyOn(chrome.runtime,'sendMessage').mockImplementation(async message=>handleV1AuthCommand(message));
 expect(await loadSettings()).toEqual(DEFAULT_SETTINGS);expect(proofs).toBe(0);const startup=initializeWorkerAuth();try{await atProof;expect(proofs).toBe(1);}finally{release(Response.json({userid:'11111111-2222-4333-8444-555555555555'}));await startup;}
});
test('N1 readonly settings command rejects payloads and page rejects non-Settings replies',async()=>{
 const {handleV1AuthCommand}=await import('../../src/relayless/v1AuthWorker'),{loadSettings}=await import('../../src/storage'),{DEFAULT_SETTINGS}=await import('../../src/types');for(const payload of [{},'settings',{access_token:'synthetic-forbidden'}])expect(await handleV1AuthCommand({type:'attention-auth-v1',version:1,command:'settings-read',payload})).toMatchObject({ok:false,code:'COMMAND_INVALID'});
 vi.spyOn(chrome.runtime,'sendMessage').mockResolvedValue({version:1,ok:true,result:{...DEFAULT_SETTINGS,access_token:'synthetic-forbidden'}});await expect(loadSettings()).rejects.toThrow('SETTINGS_UNKNOWN');expect(await indexedDB.databases()).toEqual([]);
});
