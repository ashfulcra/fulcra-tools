// @vitest-environment node
import {expect,test,vi} from 'vitest';
import {IDBFactory} from 'fake-indexeddb';
import {memStorage} from './memStorage';
vi.stubGlobal('fetch',()=>{throw Error('External network forbidden in Slice A');});
const ACCOUNT='11111111-2222-4333-8444-555555555555';
const token=(revision=0,account=ACCOUNT)=>'synthetic.'+Buffer.from(JSON.stringify({'fulcradynamics.com/userid':account,exp:1700003600,revision})).toString('base64url')+'.signature';
const deferred=<T>()=>{let resolve!:(value:T)=>void;const promise=new Promise<T>(r=>{resolve=r;});return {promise,resolve};};
// Invoke the actual private worker completion seam with its durably claimed
// original transition. The public message protocol never accepts token sets.
async function publishSignIn(broker:unknown,response:unknown){const b=broker as any;return b.completeSignIn(response,await b.claimScope('signin'));}
async function fixture(){
 const {V1AuthStorage}=await import('../../src/relayless/v1AuthStorage');const {V1AuthBroker}=await import('../../src/relayless/v1AuthBroker');
 const {V1Journal}=await import('../../src/relayless/v1Journal');const indexedDB=new IDBFactory(),local=memStorage(),storage=new V1AuthStorage({indexedDB,credentials:local}),journal=new V1Journal({indexedDB});
 const state={grants:0,response:{access_token:token(1),expires_in:3600} as Record<string,unknown>,proof:ACCOUNT,grant:null as null|Promise<Record<string,unknown>>};
 const fetchFn=async(input:RequestInfo|URL)=>{const p=new URL(String(input)).pathname;if(p.endsWith('/info'))return Response.json({userid:state.proof});throw Error('Synthetic unsupported route');};
 const broker=new V1AuthBroker({storage,journal,now:()=>1700000000000,fetch:fetchFn,refresh:async()=>{state.grants++;return state.grant?await state.grant:state.response;}});
 await broker.initialize();
 return {broker,storage,journal,local,state,indexedDB};
}
test('worker broker retains exact fresh credential without foreign fallback and renews same generation',async()=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});
 const first=await s.storage.readState();const before=first.committed!;
 await s.broker.refresh(before.publicationId);const after=(await s.storage.readState()).committed!;
 expect(after.generation).toBe(before.generation);expect(after.publicationId).not.toBe(before.publicationId);expect(s.state.grants).toBe(1);
 expect((await s.storage.readCredential(after.credentialId)).tokens.refreshToken).toBe('synthetic-refresh');
 await s.journal.close();await s.storage.close();
});
test('concurrent force requests coalesce original predecessor rather than spend successor',async()=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});const first=(await s.storage.readState()).committed!;
 const wait=deferred<Record<string,unknown>>();s.state.grant=wait.promise;const a=s.broker.refresh(first.publicationId),b=s.broker.refresh(first.publicationId);
 wait.resolve(s.state.response);await Promise.all([a,b]);expect(s.state.grants).toBe(1);await s.broker.refresh(first.publicationId);expect(s.state.grants).toBe(1);
 await s.journal.close();await s.storage.close();
});
test('revocation during grant retains late candidate without reviving authority',async()=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});const first=(await s.storage.readState()).committed!;
 const wait=deferred<Record<string,unknown>>();s.state.grant=wait.promise;const refreshing=s.broker.refresh(first.publicationId);
 await vi.waitFor(()=>expect(s.state.grants).toBe(1));await s.broker.signOut();wait.resolve(s.state.response);await refreshing;
 expect((await s.storage.readState()).committed).toBeNull();expect((await s.storage.readAttempts())[0].state).toBe('REVOKED');expect(await s.local.get(null)).not.toEqual({});
 await s.journal.close();await s.storage.close();
});
test('late stale refresh failure must not hold a later committed sign-in',async()=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});const first=(await s.storage.readState()).committed!;
 const wait=deferred<Record<string,unknown>>();s.state.grant=wait.promise;const refreshing=s.broker.refresh(first.publicationId);await vi.waitFor(()=>expect(s.state.grants).toBe(1));
 await publishSignIn(s.broker,{access_token:token(3),expires_in:3600});wait.resolve(s.state.response);await refreshing;
 expect((await s.broker.display()).status).toBe('committed');expect((await s.storage.readCredential((await s.storage.readState()).committed!.credentialId)).tokens.refreshToken).toBeNull();
 await s.journal.close();await s.storage.close();
});
test.each([null,'',23,{}])('explicit malformed refresh field %j holds without fallback or replay',async refresh_token=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});const first=(await s.storage.readState()).committed!;
 s.state.response={access_token:token(1),expires_in:3600,refresh_token};await s.broker.refresh(first.publicationId);expect((await s.broker.display()).status).toBe('held');
 await s.broker.refresh(first.publicationId);expect(s.state.grants).toBe(1);expect((await s.storage.readState()).committed).toEqual(first);
 await s.journal.close();await s.storage.close();
});
test.each(['synthetic-refresh','synthetic-rotated'])('successful explicit %s earns only a later expected-publication grant',async refresh_token=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});const first=(await s.storage.readState()).committed!;
 s.state.response={access_token:token(1),expires_in:3600,refresh_token};await s.broker.refresh(first.publicationId);const next=(await s.storage.readState()).committed!;
 expect((await s.storage.readCredential(next.credentialId)).tokens.refreshToken).toBe(refresh_token);await s.broker.refresh(first.publicationId);expect(s.state.grants).toBe(1);
 await s.broker.refresh(next.publicationId);expect(s.state.grants).toBe(2);expect((await s.storage.readAttempts()).every(a=>a.state==='COMMITTED_SUCCESSOR')).toBe(true);
 await s.journal.close();await s.storage.close();
});
test.each(['GRANT_CLAIMED','CANDIDATE_RETAINED','PRINCIPAL_PROVED','SUCCESSOR_PREPARED','COMMITTED_SUCCESSOR'])('lost acknowledgment at %s reconciles locally without another grant',async phase=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});const before=(await s.storage.readState()).committed!;
 const cas=s.storage.compareAndSwap.bind(s.storage);let hit=false;
 s.storage.compareAndSwap=async(...args)=>{await cas(...args);if(!hit&&args[2]?.after.state===phase){hit=true;throw Error('Synthetic worker terminated after durable boundary');}};
 await s.broker.refresh(before.publicationId);expect(hit).toBe(true);s.storage.compareAndSwap=cas;
 const {V1AuthBroker}=await import('../../src/relayless/v1AuthBroker');const restarted=new V1AuthBroker({storage:s.storage,journal:s.journal,now:()=>1700000000000,fetch:async()=>Response.json({userid:ACCOUNT}),refresh:async()=>{s.state.grants++;throw Error('No replay');}});
 const status=await restarted.initialize();expect(s.state.grants).toBe(phase==='GRANT_CLAIMED'?0:1);
 if(phase==='GRANT_CLAIMED'){expect(status.status).toBe('held');await restarted.refresh(before.publicationId);expect(s.state.grants).toBe(0);}else{expect(status.status).toBe('committed');expect((await s.storage.readState()).committed!.generation).toBe(before.generation);}
 await s.journal.close();await s.storage.close();
});
test('lost candidate-state acknowledgment is found at the predeclared receipt key',async()=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});const before=(await s.storage.readState()).committed!;
 const cas=s.storage.compareAndSwap.bind(s.storage);s.storage.compareAndSwap=async(...args)=>{if(args[2]?.after.state==='CANDIDATE_RETAINED')throw Error('Synthetic ack not written');return cas(...args);};
 await s.broker.refresh(before.publicationId);expect((await s.storage.readAttempts())[0].state).toBe('GRANT_CLAIMED');s.storage.compareAndSwap=cas;
 const {V1AuthBroker}=await import('../../src/relayless/v1AuthBroker');const restarted=new V1AuthBroker({storage:s.storage,journal:s.journal,now:()=>1700000000000,fetch:async()=>Response.json({userid:ACCOUNT}),refresh:async()=>{throw Error('No replay');}});
 expect((await restarted.initialize()).status).toBe('committed');expect(s.state.grants).toBe(1);await s.journal.close();await s.storage.close();
});
test.each(['missing','torn','wrong-owner','wrong-predecessor','changed-retained-refresh'])('candidate %s remains UNKNOWN across restart without grant replay',async mode=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});const before=(await s.storage.readState()).committed!;
 s.state.proof='77777777-7777-4777-8777-777777777777';await s.broker.refresh(before.publicationId);const a=(await s.storage.readAttempts())[0],key='relaylessV1Credential:'+a.receiptId,r=(await s.local.get(key))[key] as any;
 if(mode==='missing')await s.local.remove(key);else await s.local.set({[key]:mode==='torn'?{version:1}:mode==='wrong-owner'?{...r,ownerId:'77777777-7777-4777-8777-777777777777'}:mode==='wrong-predecessor'?{...r,predecessorPublicationId:'77777777-7777-4777-8777-777777777777'}:{...r,tokens:{...r.tokens,refreshToken:'synthetic-other'}}});
 const {V1AuthBroker}=await import('../../src/relayless/v1AuthBroker');const restarted=new V1AuthBroker({storage:s.storage,journal:s.journal,now:()=>1700000000000,fetch:async()=>Response.json({userid:ACCOUNT}),refresh:async()=>{s.state.grants++;throw Error('No replay');}});
 expect((await restarted.initialize()).status).toBe('held');await restarted.refresh(before.publicationId);expect(s.state.grants).toBe(1);expect((await s.storage.readState()).committed).toEqual(before);
 await s.journal.close();await s.storage.close();
});
test.each(['INTENT','CANDIDATE_RETAINED','PRINCIPAL_PROVED','SUCCESSOR_PREPARED','GATE_OBSERVED','COMMITTED'])('sign-in lost acknowledgment at %s retains original generation intent',async phase=>{
 const s=await fixture(),cas=s.storage.compareAndSwap.bind(s.storage);let hit=false;
 s.storage.compareAndSwap=async(...args)=>{await cas(...args);if(!hit&&(args[1].pending?.kind==='scope'&&args[1].pending.stage===phase||phase==='COMMITTED'&&args[1].committed!==null)){hit=true;throw Error('Synthetic killed worker');}};
 try{await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});}catch{/* INTENT completion is unknown, never success. */}expect(hit).toBe(true);s.storage.compareAndSwap=cas;
 const pending=await s.storage.readState(),expectedGeneration=pending.pending?.kind==='scope'?pending.pending.nextGeneration:pending.committed!.generation;
 const {V1AuthBroker}=await import('../../src/relayless/v1AuthBroker');const restarted=new V1AuthBroker({storage:s.storage,journal:s.journal,now:()=>1700000000000,fetch:async()=>Response.json({userid:ACCOUNT}),refresh:async()=>{throw Error('No replay');}});
 const status=await restarted.initialize();expect(status.status).toBe(phase==='INTENT'?'held':'committed');if(phase!=='INTENT')expect((await s.storage.readState()).committed!.generation).toBe(expectedGeneration);
 await s.journal.close();await s.storage.close();
});
test('old relaylessTokens mutation cannot affect the private committed credential namespace',async()=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),expires_in:3600});const p=(await s.storage.readState()).committed!,before=await s.storage.readCredential(p.credentialId);
 await s.local.set({relaylessTokens:{accessToken:'synthetic-foreign',refreshToken:'synthetic-foreign'}});expect(await s.storage.readCredential(p.credentialId)).toEqual(before);expect((await s.broker.display()).status).toBe('committed');await s.journal.close();await s.storage.close();
});
test('principal proof cannot publish candidate from a different exact API account',async()=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});const p=(await s.storage.readState()).committed!;
 s.state.proof='77777777-7777-4777-8777-777777777777';await s.broker.refresh(p.publicationId);expect((await s.broker.display()).status).toBe('held');expect((await s.storage.readState()).committed).toEqual(p);expect((await s.storage.readAttempts())[0].state).toBe('QUARANTINED');await closeBroker(s);
});
async function closeBroker(s:Awaited<ReturnType<typeof fixture>>){await s.journal.close();await s.storage.close();}
test.each(['before-gate','after-gate'])('signout crash %s completes only same intended new generation',async stage=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),expires_in:3600});const before=(await s.storage.readState()).committed!,{V1Journal}=await import('../../src/relayless/v1Journal'),transition=V1Journal.prototype.transition;
 const spy=vi.spyOn(V1Journal.prototype,'transition').mockImplementation(async function(this:InstanceType<typeof V1Journal>,...args){if(stage==='after-gate')await transition.apply(this,args);throw Error('Synthetic journal boundary lost');});
 expect((await s.broker.signOut()).status).toBe('held');expect(spy).toHaveBeenCalledTimes(1);const pending=(await s.storage.readState()).pending!;expect(pending.kind).toBe('scope');const next=pending.kind==='scope'?pending.nextGeneration:'';expect(next).not.toBe(before.generation);spy.mockRestore();
 const {V1AuthBroker}=await import('../../src/relayless/v1AuthBroker');const restarted=new V1AuthBroker({storage:s.storage,journal:s.journal});expect((await restarted.initialize()).status).toBe('disconnected');expect((await s.journal.readGate())!.generation).toBe(next);await closeBroker(s);
});
test('unexpected third gate during scope recovery stays held without allocating replacement',async()=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),expires_in:3600});const {V1Journal}=await import('../../src/relayless/v1Journal'),transition=s.journal.transition.bind(s.journal),spy=vi.spyOn(V1Journal.prototype,'transition').mockRejectedValue(Error('Synthetic stop before gate'));await s.broker.signOut();spy.mockRestore();
 const pending=await s.storage.readState(),oldGate=await s.journal.readGate();await transition(oldGate,{version:1,accountId:null,generation:'77777777-7777-4777-8777-777777777777',enabled:false});
 const {V1AuthBroker}=await import('../../src/relayless/v1AuthBroker');const restarted=new V1AuthBroker({storage:s.storage,journal:s.journal});expect((await restarted.initialize()).status).toBe('held');expect(await s.storage.readState()).toEqual(pending);await closeBroker(s);
});
test.each(['write-failed','torn-write'])('private credential %s leaves durable attempt UNKNOWN without replay',async mode=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});const p=(await s.storage.readState()).committed!,set=s.local.set;
 s.local.set=async items=>{if(mode==='torn-write')await set(Object.fromEntries(Object.keys(items).map(k=>[k,{version:1}])));else throw Error('Synthetic quota');};await s.broker.refresh(p.publicationId);expect((await s.broker.display()).status).toBe('held');s.local.set=set;await s.broker.refresh(p.publicationId);expect(s.state.grants).toBe(1);expect((await s.storage.readState()).committed).toEqual(p);await closeBroker(s);
});
test('successful grant completing after original deadline retains full candidate before holding',async()=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});const p=(await s.storage.readState()).committed!,wait=deferred<Record<string,unknown>>();s.state.grant=wait.promise;
 const renewing=s.broker.refresh(p.publicationId,performance.now()+20);await vi.waitFor(()=>expect(s.state.grants).toBe(1));
 await renewing;wait.resolve({access_token:token(2),expires_in:3600,refresh_token:'synthetic-rotated',id_token:'synthetic-id',scope:'openid'});
 await vi.waitFor(async()=>{const a=(await s.storage.readAttempts())[0];expect((await s.storage.readCredential(a.receiptId)).response.id_token).toBe('synthetic-id');});
 expect((await s.storage.readState()).committed).toEqual(p);expect((await s.broker.display()).status).toBe('held');await closeBroker(s);
});
test('actual worker device-flow late completion retains original receipt after signout',async()=>{
 const {V1AuthStorage}=await import('../../src/relayless/v1AuthStorage'),{V1AuthBroker}=await import('../../src/relayless/v1AuthBroker'),{V1Journal}=await import('../../src/relayless/v1Journal');
 const indexedDB=new IDBFactory(),local=memStorage(),storage=new V1AuthStorage({indexedDB,credentials:local}),journal=new V1Journal({indexedDB}),wait=deferred<Response>(),started=deferred<void>();
 const broker=new V1AuthBroker({storage,journal,now:()=>1700000000000,registerOriginStrip:async()=>{},fetch:async(input)=>{
  const path=new URL(String(input)).pathname;if(path==='/oauth/device/code')return Response.json({device_code:'synthetic-device',user_code:'SYNTHETIC',verification_uri:'https://synthetic.invalid/activate',verification_uri_complete:'https://synthetic.invalid/activate?code=SYNTHETIC',expires_in:900,interval:1});
  if(path==='/oauth/token'){started.resolve();return wait.promise;}if(path.endsWith('/info'))return Response.json({userid:ACCOUNT});throw Error('Synthetic unsupported route');
 }});await broker.initialize();const prompt=await broker.startSignIn();const pending=(await storage.readState()).pending!;expect(pending.kind).toBe('scope');
 await started.promise;await broker.signOut();wait.resolve(Response.json({access_token:token(),refresh_token:'synthetic-late-refresh',expires_in:3600}));
 await vi.waitFor(async()=>{if(pending.kind==='scope')expect((await storage.readCredential(pending.receiptId)).ownerId).toBe(prompt.transitionId);});
 expect((await broker.display()).status).toBe('disconnected');expect((await storage.readState()).committed).toBeNull();await journal.close();await storage.close();
});
test('settings commit then lost ack recovers same authoritative scope without legacy overwrite',async()=>{
 const s=await fixture(),{DEFAULT_SETTINGS}=await import('../../src/types');await publishSignIn(s.broker,{access_token:token(),expires_in:3600});const before=(await s.storage.readState()).committed!;
 const legacy={...DEFAULT_SETTINGS,identityLabel:'Legacy evidence'};await s.local.set({settings:legacy});const desired={...DEFAULT_SETTINGS,identityLabel:'Owned synthetic'},cas=s.storage.compareAndSwap.bind(s.storage);let hit=false;
 s.storage.compareAndSwap=async(...args)=>{await cas(...args);if(!hit&&args[1].pending===null&&args[1].committed?.scopeFingerprint!==before.scopeFingerprint){hit=true;throw Error('Synthetic final settings acknowledgment lost');}};
 await s.broker.changeSettings(desired);expect(hit).toBe(true);s.storage.compareAndSwap=cas;const committed=await s.storage.readState();expect(committed.settings).toEqual(desired);expect((await s.local.get('settings')).settings).toEqual(legacy);
 const {V1AuthBroker}=await import('../../src/relayless/v1AuthBroker');const restarted=new V1AuthBroker({storage:s.storage,journal:s.journal});await restarted.initialize();expect(await restarted.readSettings()).toEqual(desired);expect((await s.storage.readState()).committed!.generation).toBe(committed.committed!.generation);await closeBroker(s);
});
test('settings intent survives pre-gate crash and resumes exact desired settings',async()=>{
 const s=await fixture(),{DEFAULT_SETTINGS}=await import('../../src/types');await publishSignIn(s.broker,{access_token:token(),expires_in:3600});const {V1Journal}=await import('../../src/relayless/v1Journal'),spy=vi.spyOn(V1Journal.prototype,'transition').mockRejectedValue(Error('Synthetic scope gate unavailable'));
 const desired={...DEFAULT_SETTINGS,identityLabel:'Pending owned synthetic'};expect((await s.broker.changeSettings(desired)).status).toBe('held');expect(spy).toHaveBeenCalledTimes(1);const original=await s.storage.readState();expect(original.settings).not.toEqual(desired);spy.mockRestore();
 const {V1AuthBroker}=await import('../../src/relayless/v1AuthBroker');const restarted=new V1AuthBroker({storage:s.storage,journal:s.journal});expect((await restarted.initialize()).status).toBe('committed');expect(await restarted.readSettings()).toEqual(desired);expect((await s.storage.readState()).committed!.generation).toBe(original.pending?.kind==='scope'?original.pending.nextGeneration:'');await closeBroker(s);
});
test('signin cannot revive capture consent disabled in authoritative settings',async()=>{
 const s=await fixture(),{DEFAULT_SETTINGS}=await import('../../src/types');await s.broker.changeSettings({...DEFAULT_SETTINGS,enabled:false});await publishSignIn(s.broker,{access_token:token(),expires_in:3600});expect((await s.storage.readState()).gate!.enabled).toBe(false);await closeBroker(s);
});
test('committed successor with missing durable attempt lineage stays held at startup',async()=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});const p=(await s.storage.readState()).committed!;await s.broker.refresh(p.publicationId);const attempt=(await s.storage.readAttempts())[0];
 const db=await new Promise<IDBDatabase>((resolve,reject)=>{const r=s.indexedDB.open('fulcra-attention-auth-v1');r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});await new Promise<void>((resolve,reject)=>{const tx=db.transaction('attempts','readwrite');tx.objectStore('attempts').delete(attempt.attemptId);tx.oncomplete=()=>resolve();tx.onabort=()=>reject(tx.error);});db.close();
 const {V1AuthBroker}=await import('../../src/relayless/v1AuthBroker');const restarted=new V1AuthBroker({storage:s.storage,journal:s.journal});expect((await restarted.initialize()).status).toBe('held');await closeBroker(s);
});
test('expired final auth CAS cannot activate a successor after caller has returned held',async()=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});const p=(await s.storage.readState()).committed!,entered=deferred<void>(),release=deferred<void>(),cas=s.storage.compareAndSwap.bind(s.storage);
 s.storage.compareAndSwap=async(...args)=>{if(args[2]?.after.state==='COMMITTED_SUCCESSOR'){entered.resolve();await release.promise;}return cas(...args);};
 const renewal=s.broker.refresh(p.publicationId,performance.now()+200);await entered.promise;await renewal;release.resolve();await new Promise(resolve=>setTimeout(resolve,30));expect((await s.storage.readState()).committed).toEqual(p);await closeBroker(s);
});
test('proved different-account candidate is durably quarantined and cannot later revive on retry',async()=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});const p=(await s.storage.readState()).committed!;
 s.state.proof='77777777-7777-4777-8777-777777777777';await s.broker.refresh(p.publicationId);s.state.proof=ACCOUNT;await s.broker.refresh(p.publicationId);
 expect((await s.storage.readAttempts())[0].state).toBe('QUARANTINED');expect((await s.storage.readState()).committed).toEqual(p);expect(s.state.grants).toBe(1);await closeBroker(s);
});
test('stale settings command cannot acknowledge a competing winner as its own success',async()=>{
 const s=await fixture(),{DEFAULT_SETTINGS}=await import('../../src/types');await publishSignIn(s.broker,{access_token:token(),expires_in:3600});const cas=s.storage.compareAndSwap.bind(s.storage),entered=deferred<void>(),release=deferred<void>();let paused=false;
 s.storage.compareAndSwap=async(...args)=>{await cas(...args);if(!paused&&args[1].pending?.kind==='scope'&&args[1].pending.stage==='INTENT'&&args[1].pending.settings.identityLabel==='First'){paused=true;entered.resolve();await release.promise;}};
 const first=s.broker.changeSettings({...DEFAULT_SETTINGS,identityLabel:'First'});await entered.promise;expect((await s.broker.changeSettings({...DEFAULT_SETTINGS,identityLabel:'Winner'})).status).toBe('committed');release.resolve();
 expect((await first).status).toBe('held');expect((await s.broker.display()).code).toBe('COMMITTED');expect((await s.broker.readSettings()).identityLabel).toBe('Winner');await closeBroker(s);
});

// Round 1: independent review I1/I3/I5 counterexamples, synthetic only.
test.each(['signout','withdrawal'])('I1 %s synchronously fences a refresh before durable scope intent',async mode=>{
 const s=await fixture(),{DEFAULT_SETTINGS}=await import('../../src/types');await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-r',expires_in:3600});const before=(await s.storage.readState()).committed!;
 const grant=deferred<Record<string,unknown>>();s.state.grant=grant.promise;const refresh=s.broker.refresh(before.publicationId);await vi.waitFor(()=>expect(s.state.grants).toBe(1));
 const read=s.storage.readState.bind(s.storage),entered=deferred<void>(),release=deferred<void>();let paused=false;s.storage.readState=async()=>{if(!paused){paused=true;entered.resolve();await release.promise;}return read();};
 const revoke=mode==='signout'?s.broker.signOut():s.broker.changeSettings({...DEFAULT_SETTINGS,enabled:false});await entered.promise;grant.resolve(s.state.response);await refresh;
 try{expect((await read()).committed).toEqual(before);expect((await s.broker.display()).status).not.toBe('committed');await expect(s.broker.runRead('types',performance.now()+1000)).rejects.toThrow();}finally{release.resolve();await revoke;s.storage.readState=read;await closeBroker(s);}
});
test('I1 final auth transaction is fenced while scope claim is paused before durable intent',async()=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-r',expires_in:3600});const before=(await s.storage.readState()).committed!,cas=s.storage.compareAndSwap.bind(s.storage),atCommit=deferred<void>(),commitRelease=deferred<void>();
 s.storage.compareAndSwap=async(...args)=>{if(args[2]?.after.state==='COMMITTED_SUCCESSOR'){atCommit.resolve();await commitRelease.promise;}return cas(...args);};
 const refresh=s.broker.refresh(before.publicationId);await atCommit.promise;const read=s.storage.readState.bind(s.storage),atClaim=deferred<void>(),claimRelease=deferred<void>();let paused=false;s.storage.readState=async()=>{if(!paused){paused=true;atClaim.resolve();await claimRelease.promise;}return read();};
 const revoke=s.broker.signOut();await atClaim.promise;commitRelease.resolve();await refresh;try{expect((await read()).committed).toEqual(before);}finally{claimRelease.resolve();await revoke;s.storage.readState=read;await closeBroker(s);}
});
test.each(['timeout','expired-lifetime','replaced-scope'])('I3 actual device grant %s retains late successful receipt without publication',async mode=>{
 const {V1AuthStorage}=await import('../../src/relayless/v1AuthStorage'),{V1AuthBroker}=await import('../../src/relayless/v1AuthBroker'),{V1Journal}=await import('../../src/relayless/v1Journal');
 const factory=new IDBFactory(),local=memStorage(),storage=new V1AuthStorage({indexedDB:factory,credentials:local}),journal=new V1Journal({indexedDB:factory}),pending=deferred<Response>(),entered=deferred<void>();let grants=0,offset=0;
 const timer=setTimeout,clock=performance.now.bind(performance);const timeSpy=vi.spyOn(globalThis,'setTimeout').mockImplementation(((fn:any,ms?:number,...args:any[])=>timer(fn,ms===1000?0:mode!=='expired-lifetime'&&ms===15000?10:ms,...args)) as typeof setTimeout);const clockSpy=vi.spyOn(performance,'now').mockImplementation(()=>clock()+offset);
 const b=new V1AuthBroker({storage,journal,now:()=>1700000000000,registerOriginStrip:async()=>{},fetch:async(input)=>{const path=new URL(String(input)).pathname;if(path==='/oauth/device/code')return Response.json({device_code:'synthetic-device',user_code:'SYNTHETIC',verification_uri:'https://synthetic.invalid/',verification_uri_complete:'https://synthetic.invalid/?code=SYNTHETIC',expires_in:900,interval:1});if(path==='/oauth/token'){grants++;entered.resolve();return pending.promise;}if(path.endsWith('/info'))return Response.json({userid:ACCOUNT});throw Error('Unexpected synthetic route');}});
 try{await b.initialize();await b.startSignIn();const original=(await storage.readState()).pending!;expect(original.kind).toBe('scope');await entered.promise;
  if(mode==='expired-lifetime')offset=600001;else await new Promise(r=>timer(r,25));if(mode==='replaced-scope')await b.signOut();
  pending.resolve(Response.json({access_token:token(),refresh_token:'synthetic-late-r',id_token:'synthetic-late-id',expires_in:3600}));
  await vi.waitFor(async()=>{if(original.kind==='scope')expect((await storage.readCredential(original.receiptId)).response.id_token).toBe('synthetic-late-id');});
  await vi.waitFor(async()=>expect((await b.signInStatus(original.kind==='scope'?original.transitionId:'')).status).toBe('held'));expect((await storage.readState()).committed).toBeNull();expect(grants).toBe(1);
 }finally{timeSpy.mockRestore();clockSpy.mockRestore();await journal.close();await storage.close();}
});
test.each(['pause','disable'])('I5 %s retains selected authentication and resumes under a fresh generation',async mode=>{
 const s=await fixture(),{DEFAULT_SETTINGS}=await import('../../src/types');await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-r',expires_in:3600});const before=(await s.storage.readState()).committed!;
 const paused=await s.broker.changeSettings({...DEFAULT_SETTINGS,...mode==='pause'?{pausedUntil:1700000010000}:{enabled:false}}),held=(await s.storage.readState()).committed;
 expect(paused.status).toBe('paused');expect(held?.accountId).toBe(ACCOUNT);expect(held?.generation).not.toBe(before.generation);await expect(s.broker.runRead('types',performance.now()+1000)).rejects.toThrow();
 expect((await s.broker.changeSettings(DEFAULT_SETTINGS)).status).toBe('committed');const resumed=(await s.storage.readState()).committed!;expect(resumed.accountId).toBe(ACCOUNT);expect(resumed.generation).not.toBe(held!.generation);expect(resumed.generation).not.toBe(before.generation);expect((await s.storage.readCredential(resumed.credentialId)).tokens.refreshToken).toBe('synthetic-r');await s.broker.signOut();await s.broker.changeSettings(DEFAULT_SETTINGS);expect((await s.storage.readState()).committed).toBeNull();await closeBroker(s);
});
test('I5 fresh sign-in while paused retains selection for later explicit unpause',async()=>{
 const s=await fixture(),{DEFAULT_SETTINGS}=await import('../../src/types');await s.broker.changeSettings({...DEFAULT_SETTINGS,pausedUntil:1700000010000});await publishSignIn(s.broker,{access_token:token(),expires_in:3600});expect((await s.broker.display()).status).toBe('paused');expect((await s.storage.readState()).committed?.accountId).toBe(ACCOUNT);expect((await s.broker.changeSettings(DEFAULT_SETTINGS)).status).toBe('committed');await closeBroker(s);
});
test.each([false,true])('I3 on-time actual device flow completes and preserves paused=%s selection',async paused=>{
 const {V1AuthStorage}=await import('../../src/relayless/v1AuthStorage'),{V1AuthBroker}=await import('../../src/relayless/v1AuthBroker'),{V1Journal}=await import('../../src/relayless/v1Journal'),{DEFAULT_SETTINGS}=await import('../../src/types');const indexedDB=new IDBFactory(),storage=new V1AuthStorage({indexedDB,credentials:memStorage()}),journal=new V1Journal({indexedDB});
 const timer=setTimeout,spy=vi.spyOn(globalThis,'setTimeout').mockImplementation(((fn:any,ms?:number,...args:any[])=>timer(fn,ms===1000?0:ms,...args)) as typeof setTimeout);
 const broker=new V1AuthBroker({storage,journal,registerOriginStrip:async()=>{},fetch:async input=>{const path=new URL(String(input)).pathname;if(path==='/oauth/device/code')return Response.json({device_code:'synthetic-device',user_code:'SYNTHETIC',verification_uri:'https://synthetic.invalid/',verification_uri_complete:'https://synthetic.invalid/?code=SYNTHETIC',expires_in:900,interval:1});if(path==='/oauth/token')return Response.json({access_token:token(),refresh_token:'synthetic-r',expires_in:3600});if(path.endsWith('/info'))return Response.json({userid:ACCOUNT});throw Error('Synthetic route refused');}});
 try{await broker.initialize();if(paused)await broker.changeSettings({...DEFAULT_SETTINGS,pausedUntil:1700000010000});const started=await broker.startSignIn();await vi.waitFor(async()=>expect((await broker.signInStatus(started.transitionId)).status).toBe('done'));expect((await broker.display()).status).toBe(paused?'paused':'committed');}finally{spy.mockRestore();await journal.close();await storage.close();}
});
test('I1 cancellation inside final auth IDB callback rolls back the exact successor',async()=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-r',expires_in:3600});const before=(await s.storage.readState()).committed!,read=s.storage.readState.bind(s.storage),atClaim=deferred<void>(),release=deferred<void>();let revocation:Promise<unknown>|undefined,paused=false,hit=false;
 const {IDBObjectStore}=await import('fake-indexeddb'),put=IDBObjectStore.prototype.put;const spy=vi.spyOn(IDBObjectStore.prototype,'put').mockImplementation(function(this:IDBObjectStore,value,key){const request=put.call(this,value,key);if(!hit&&this.name==='state'&&value?.pending===null&&value?.committed?.publicationId!==before.publicationId){hit=true;s.storage.readState=async()=>{if(!paused){paused=true;atClaim.resolve();await release.promise;}return read();};revocation=s.broker.signOut();}return request;});
 try{await s.broker.refresh(before.publicationId);await atClaim.promise;expect(hit).toBe(true);expect((await read()).committed).toEqual(before);expect((await s.storage.readAttempts())[0].state).not.toBe('COMMITTED_SUCCESSOR');}finally{spy.mockRestore();release.resolve();await revocation;s.storage.readState=read;await closeBroker(s);}
});
test('I1 auth CAS retains positively committed result when abort precedes completion callback',async()=>{
 const s=await fixture(),state=await s.storage.readState(),next={...state,revision:crypto.randomUUID()},controller=new AbortController(),{IDBDatabase}=await import('fake-indexeddb'),transaction=IDBDatabase.prototype.transaction;let hit=false;
 const spy=vi.spyOn(IDBDatabase.prototype,'transaction').mockImplementation(function(this:IDBDatabase,...args){const tx=transaction.apply(this,args);if(args[1]==='readwrite'&&this.name==='fulcra-attention-auth-v1')tx.addEventListener('complete',()=>{hit=true;controller.abort();},{once:true});return tx;});
 try{await expect(s.storage.compareAndSwap(state,next,undefined,performance.now()+1000,controller.signal)).resolves.toBeUndefined();expect(hit).toBe(true);expect(await s.storage.readState()).toEqual(next);}finally{spy.mockRestore();await closeBroker(s);}
});
test.each(['missing','torn','wrong-owner','wrong-predecessor','changed-retained-refresh'])('I1 live pending refresh candidate %s cannot publish or replay on recovery',async mode=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-refresh',expires_in:3600});const before=(await s.storage.readState()).committed!,cas=s.storage.compareAndSwap.bind(s.storage);let hit=false;
 s.storage.compareAndSwap=async(...args)=>{if(args[2]?.after.state==='CANDIDATE_RETAINED'){hit=true;throw Error('Synthetic stop with live claimed candidate');}return cas(...args);};await s.broker.refresh(before.publicationId);expect(hit).toBe(true);s.storage.compareAndSwap=cas;
 const a=(await s.storage.readAttempts())[0];expect(a.state).toBe('GRANT_CLAIMED');expect((await s.storage.readState()).pending).toEqual({kind:'refresh',attemptId:a.attemptId});const key='relaylessV1Credential:'+a.receiptId,r=(await s.local.get(key))[key] as any;
 if(mode==='missing')await s.local.remove(key);else await s.local.set({[key]:mode==='torn'?{version:1}:mode==='wrong-owner'?{...r,ownerId:'77777777-7777-4777-8777-777777777777'}:mode==='wrong-predecessor'?{...r,predecessorPublicationId:'77777777-7777-4777-8777-777777777777'}:{...r,tokens:{...r.tokens,refreshToken:'synthetic-other'}}});
 const {V1AuthBroker}=await import('../../src/relayless/v1AuthBroker');const restarted=new V1AuthBroker({storage:s.storage,journal:s.journal,fetch:async()=>Response.json({userid:ACCOUNT}),refresh:async()=>{throw Error('Grant replay forbidden');}});expect((await restarted.initialize()).status).toBe('held');expect((await s.storage.readState()).committed).toEqual(before);expect(s.state.grants).toBe(1);await closeBroker(s);
});

// Round 2: a newly arriving recovery cannot borrow a pending scope claim's lifetime.
test.each(['signout','withdrawal'])('R2 I1 new same-predecessor refresh stays held during predurable %s claim',async mode=>{
 const s=await fixture(),{DEFAULT_SETTINGS}=await import('../../src/types');await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-r',expires_in:3600});const before=(await s.storage.readState()).committed!,grant=deferred<Record<string,unknown>>();s.state.grant=grant.promise;const renewing=s.broker.refresh(before.publicationId);await vi.waitFor(()=>expect(s.state.grants).toBe(1));
 const read=s.storage.readState.bind(s.storage),entered=deferred<void>(),release=deferred<void>();let paused=false;s.storage.readState=async()=>{if(!paused){paused=true;entered.resolve();await release.promise;}return read();};const revoking=mode==='signout'?s.broker.signOut():s.broker.changeSettings({...DEFAULT_SETTINGS,enabled:false});await entered.promise;
 try{grant.resolve({access_token:token(2),refresh_token:'synthetic-late-r',expires_in:3600});expect((await renewing).status).toBe('held');const attempt=(await s.storage.readAttempts())[0];expect((await s.storage.readCredential(attempt.receiptId)).response.refresh_token).toBe('synthetic-late-r');
  const reentry=await s.broker.refresh(before.publicationId);expect(reentry.status).toBe('held');expect((await read()).committed).toEqual(before);expect((await s.storage.readAttempts())[0].state).not.toBe('COMMITTED_SUCCESSOR');expect(s.state.grants).toBe(1);await expect(s.broker.runRead('types',performance.now()+1000)).rejects.toThrow('ADMISSION_HELD');
 }finally{release.resolve();await revoking;s.storage.readState=read;await closeBroker(s);}
});
test('R2 I1 failed predurable claim stays exclusive across initialize and refresh until an explicit new scope',async()=>{
 const s=await fixture();await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-r',expires_in:3600});const before=(await s.storage.readState()).committed!,grant=deferred<Record<string,unknown>>();s.state.grant=grant.promise;const renewing=s.broker.refresh(before.publicationId);await vi.waitFor(()=>expect(s.state.grants).toBe(1));
 const read=s.storage.readState.bind(s.storage);let failed=false;s.storage.readState=async()=>{if(!failed){failed=true;throw Error('Synthetic claim read lost before intent');}return read();};expect((await s.broker.signOut()).status).toBe('held');s.storage.readState=read;grant.resolve({access_token:token(2),expires_in:3600});await renewing;
 try{expect((await s.broker.initialize()).status).toBe('held');expect((await s.broker.refresh(before.publicationId)).status).toBe('held');expect((await read()).committed).toEqual(before);expect(s.state.grants).toBe(1);
  await publishSignIn(s.broker,{access_token:token(3),refresh_token:'synthetic-new-r',expires_in:3600});const current=(await read()).committed!;s.state.grant=null;expect((await s.broker.refresh(current.publicationId)).status).toBe('committed');expect(s.state.grants).toBe(2);
 }finally{await closeBroker(s);}
});
test('R2 I1 older failed claim cannot release a newer predurable owner; its completed scope permits current refresh',async()=>{
 const s=await fixture(),{DEFAULT_SETTINGS}=await import('../../src/types');await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-r',expires_in:3600});const before=(await s.storage.readState()).committed!,read=s.storage.readState.bind(s.storage),firstEntered=deferred<void>(),secondEntered=deferred<void>(),firstRelease=deferred<void>(),secondRelease=deferred<void>();let reads=0;
 s.storage.readState=async()=>{const call=++reads;if(call===1){firstEntered.resolve();await firstRelease.promise;}if(call===2){secondEntered.resolve();await secondRelease.promise;}return read();};const first=s.broker.signOut();await firstEntered.promise;const second=s.broker.changeSettings({...DEFAULT_SETTINGS,identityLabel:'New exact scope'});await secondEntered.promise;
 try{firstRelease.resolve();expect((await first).status).toBe('held');expect((await s.broker.refresh(before.publicationId)).status).toBe('held');expect((await read()).committed).toEqual(before);expect(s.state.grants).toBe(0);secondRelease.resolve();expect((await second).status).toBe('committed');const current=(await read()).committed!;expect(current.generation).not.toBe(before.generation);expect((await s.broker.refresh(current.publicationId)).status).toBe('committed');expect(s.state.grants).toBe(1);
 }finally{firstRelease.resolve();secondRelease.resolve();await Promise.all([first,second]);s.storage.readState=read;await closeBroker(s);}
});
test.each(['pending-exact-scope','confirmed-exact-scope'])('R2 I1 explicit recovery releases only %s and current publication can refresh',async phase=>{
 const s=await fixture(),{DEFAULT_SETTINGS}=await import('../../src/types'),{V1Journal}=await import('../../src/relayless/v1Journal');await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-r',expires_in:3600});const before=(await s.storage.readState()).committed!,desired={...DEFAULT_SETTINGS,identityLabel:'Exact recoverable scope'},cas=s.storage.compareAndSwap.bind(s.storage);let hit=false;
 const spy=phase==='pending-exact-scope'?vi.spyOn(V1Journal.prototype,'transition').mockImplementation(async()=>{hit=true;throw Error('Synthetic exact pending scope held');}):undefined;
 if(phase==='confirmed-exact-scope')s.storage.compareAndSwap=async(...args)=>{await cas(...args);if(!hit&&args[1].pending===null&&args[1].settings?.identityLabel===desired.identityLabel){hit=true;throw Error('Synthetic exact final scope acknowledgment lost');}};
 try{expect((await s.broker.changeSettings(desired)).status).toBe('held');expect(hit).toBe(true);spy?.mockRestore();s.storage.compareAndSwap=cas;const held=await s.storage.readState(),expected=held.pending?.kind==='scope'?held.pending.nextGeneration:held.committed!.generation;expect((await s.broker.refresh(before.publicationId)).status).toBe('held');expect(s.state.grants).toBe(0);
  expect((await s.broker.initialize()).status).toBe('committed');const current=(await s.storage.readState()).committed!;expect(current.generation).toBe(expected);expect(current.generation).not.toBe(before.generation);expect((await s.broker.readSettings()).identityLabel).toBe(desired.identityLabel);expect((await s.broker.refresh(current.publicationId)).status).toBe('committed');expect(s.state.grants).toBe(1);
 }finally{spy?.mockRestore();s.storage.compareAndSwap=cas;await closeBroker(s);}
});
test('R2 I1 allocated but unconfirmed scope cannot release against an unrelated valid committed descriptor',async()=>{
 const s=await fixture(),{DEFAULT_SETTINGS}=await import('../../src/types');await publishSignIn(s.broker,{access_token:token(),refresh_token:'synthetic-r',expires_in:3600});const before=await s.storage.readState(),cas=s.storage.compareAndSwap.bind(s.storage);let hit=false;
 s.storage.compareAndSwap=async(...args)=>{if(args[1].pending?.kind==='scope'&&args[1].pending.settings.identityLabel==='Unconfirmed exact scope'){hit=true;throw Error('Synthetic intent never committed');}return cas(...args);};
 try{expect((await s.broker.changeSettings({...DEFAULT_SETTINGS,identityLabel:'Unconfirmed exact scope'})).status).toBe('held');expect(hit).toBe(true);s.storage.compareAndSwap=cas;expect((await s.broker.initialize()).status).toBe('held');expect((await s.broker.refresh(before.committed!.publicationId)).status).toBe('held');expect(await s.storage.readState()).toEqual(before);expect(s.state.grants).toBe(0);
  expect((await s.broker.changeSettings({...DEFAULT_SETTINGS,identityLabel:'Explicit replacement'})).status).toBe('committed');const current=(await s.storage.readState()).committed!;expect((await s.broker.refresh(current.publicationId)).status).toBe('committed');expect(s.state.grants).toBe(1);
 }finally{s.storage.compareAndSwap=cas;await closeBroker(s);}
});
