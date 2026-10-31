// @vitest-environment node
import {expect,test,vi} from 'vitest';
import {memStorage} from './memStorage';
import {actorFixture,gate,ACCOUNT,TAG,TARGET,ACTION} from './v1ActionActorFixture';
import {V1AuthStorage,makeV1Credential} from '../../src/relayless/v1AuthStorage';
import {V1AuthBroker} from '../../src/relayless/v1AuthBroker';
import {DEFAULT_SETTINGS} from '../../src/types';
import {V1Http} from '../../src/relayless/v1Http';
import {V1Journal} from '../../src/relayless/v1Journal';
vi.stubGlobal('fetch',()=>{throw Error('External network forbidden in owned actor tests');});
const deferred=<T>()=>{let resolve!:(value:T)=>void;const promise=new Promise<T>(r=>{resolve=r;});return {promise,resolve};};
async function fixture(tagLookup=false){
 const s=await actorFixture(),local=memStorage(),storage=new V1AuthStorage({indexedDB:s.indexedDB,credentials:local});
 const token=await (s.http as any).readToken(),credential=makeV1Credential(crypto.randomUUID(),crypto.randomUUID(),null,{access_token:token,refresh_token:'synthetic-refresh',expires_in:3600},1700000000000);
 await storage.initializeAuthority(null);await storage.retainCredential(credential);const state=await storage.readState();await storage.compareAndSwap(state,{...state,revision:crypto.randomUUID(),initialized:true,settings:DEFAULT_SETTINGS,gate,committed:{publicationId:crypto.randomUUID(),credentialId:credential.credentialId,accountId:ACCOUNT,generation:gate.generation,scopeFingerprint:'0'.repeat(64),consentRevision:crypto.randomUUID()}});
 let grants=0;
 const broker=new V1AuthBroker({storage,journal:s.journal,now:()=>1700000000000,fetch:async(input,init)=>tagLookup&&new URL(String(input)).pathname==='/user/v1alpha1/tag/name/Synthetic'?Response.json({id:TAG,name:'Synthetic',userid:ACCOUNT}):(s.http as any).fetchFn(input,init),readProtection:s.protection.protection,refresh:async()=>{grants++;return {access_token:token,expires_in:3600};}});
 expect((await broker.initialize()).status).toBe('committed');return {...s,broker,storage,local,grants:()=>grants};
}
async function close(s:Awaited<ReturnType<typeof fixture>>){await s.journal.close();await s.storage.close();}
test('broker invokes original actor through final atomic settlement, terminal retry is zero HTTP',async()=>{
 const s=await fixture(),original=s.intent.requestText;const result=await s.broker.runAction(s.intent,performance.now()+30000);expect(result.status).toBe('settled');expect(s.intent.requestText).toBe(original);
 const calls=s.calls.length;expect((await s.broker.runAction(s.intent,performance.now()+30000)).status).toBe('already-settled');expect(s.calls).toHaveLength(calls);await close(s);
});
test('renewal excludes final issue preparation and new leases, original issued bytes never change',async()=>{
 const s=await fixture(),entered=deferred<void>(),release=deferred<void>(),issue=V1Journal.prototype.issueGuardedEffect;let observedSignal:AbortSignal|undefined;
 const spy=vi.spyOn(V1Journal.prototype,'issueGuardedEffect').mockImplementation(async function(this:V1Journal,...args){observedSignal=args[5];entered.resolve();await release.promise;return issue.apply(this,args);});
 const action=s.broker.runAction(s.intent,performance.now()+30000);await entered.promise;const before=(await s.storage.readState()).committed!;const refreshing=s.broker.refresh(before.publicationId);
 await expect(s.broker.runAction(s.intent,performance.now()+30000)).rejects.toThrow('ADMISSION_HELD');expect(s.grants()).toBe(0);expect((await s.storage.readState()).committed).toEqual(before);release.resolve();
 expect((await action).status).toBe('settled');await refreshing;expect(observedSignal).toBeDefined();expect((await s.storage.readState()).committed!.generation).toBe(gate.generation);expect(s.grants()).toBe(1);spy.mockRestore();await close(s);
});
test.each(['issue','verify','settle'])('scope revoke at paused final %s aborts explicit actual journal signal',async phase=>{
 const s=await fixture(),entered=deferred<void>(),release=deferred<void>();const method=phase==='issue'?'issueGuardedEffect':phase==='verify'?'verifyGuardedEffect':'settleGuardedMutation';const original=V1Journal.prototype[method] as any;let signal:AbortSignal|undefined;
 const spy=vi.spyOn(V1Journal.prototype,method).mockImplementation(async function(this:V1Journal,...args:any[]){signal=args[phase==='issue'?5:phase==='verify'?6:4];entered.resolve();await release.promise;return original.apply(this,args);} as any);
 const running=s.broker.runAction(s.intent,performance.now()+30000);await entered.promise;const signingOut=s.broker.signOut();expect(signal).toBeDefined();expect(signal!.aborted).toBe(true);release.resolve();expect((await running).status).toBe('unknown');await signingOut;
 if(phase==='issue')expect(s.calls.filter(c=>c.init.method==='POST')).toHaveLength(0);expect((await s.storage.readState()).committed).toBeNull();spy.mockRestore();await close(s);
});
test('late confirmed final settlement survives owned scope abort',async()=>{
 const s=await fixture(),settle=V1Journal.prototype.settleGuardedMutation;let revoked:Promise<unknown>|undefined;
 const spy=vi.spyOn(V1Journal.prototype,'settleGuardedMutation').mockImplementation(async function(this:V1Journal,...args){const result=await settle.apply(this,args);revoked=s.broker.signOut();return result;});
 expect((await s.broker.runAction(s.intent,performance.now()+30000)).status).toBe('settled');await revoked;spy.mockRestore();await close(s);
});
test('retained root clone/direct effect and owned tag children cannot publish after lease ends',async()=>{
 const s=await fixture();let escapedHttp:V1Http|undefined,escapedJournal:V1Journal|undefined;
 const submit=V1Http.prototype.submitEffect;
 const spy=vi.spyOn(V1Http.prototype,'submitEffect').mockImplementation(function(this:V1Http,opts){escapedHttp=this.withReadCancellation(new AbortController().signal);escapedJournal=opts.journal;return submit.call(this,opts);});
 try{expect((await s.broker.runAction(s.intent,performance.now()+30000)).status).toBe('settled');}finally{spy.mockRestore();}
 const calls=s.calls.length;
 await expect(escapedHttp!.json('/input/v1/data_type?include_deprecated=true')).rejects.toThrow();
 await expect(escapedHttp!.submitEffect({journal:escapedJournal!,intent:s.intent,name:'deliver',authority:s.authority})).rejects.toThrow();
 for(const [method,args] of [['reserveTag',[gate,'Escaped','Escaped']],['submitTag',[gate,'Attention','Attention',TAG]],['resolveTag',[gate,'Attention','Attention',TAG]]] as const)await expect((escapedJournal![method] as any)(...args)).rejects.toThrow('LEASE_ENDED');
 expect(s.calls).toHaveLength(calls);await close(s);
});
test('lease deadline is the original caller deadline, pending renewal does not restart it',async()=>{
 const s=await fixture();await expect(s.broker.runAction(s.intent,performance.now()-1)).rejects.toThrow('DEADLINE');expect(s.calls).toHaveLength(0);
 const entered=deferred<void>(),release=deferred<void>(),read=V1Journal.prototype.readSettlement;const spy=vi.spyOn(V1Journal.prototype,'readSettlement').mockImplementation(async function(this:V1Journal,...args){entered.resolve();await release.promise;return read.apply(this,args);});
 const action=s.broker.runAction(s.intent,performance.now()+30);await entered.promise;const p=(await s.storage.readState()).committed!,renewal=s.broker.refresh(p.publicationId,performance.now()+10);await renewal;expect(s.grants()).toBe(0);release.resolve();expect((await action).status).toBe('unknown');spy.mockRestore();await close(s);
});
test('issued UNKNOWN renewal retains exact owner request hash nonce flags and never reissues',async()=>{
 const s=await fixture();s.state.persist=false;expect((await s.broker.runAction(s.intent,performance.now()+30000)).status).toBe('unknown');const before=await s.journal.readMutation(gate,ACTION),queue=await s.journal.readOutbox(gate,TARGET,s.entry.record.id),p=(await s.storage.readState()).committed!;
 await s.broker.refresh(p.publicationId);expect(await s.journal.readMutation(gate,ACTION)).toEqual(before);expect(await s.journal.readOutbox(gate,TARGET,s.entry.record.id)).toEqual(queue);
 expect((await s.broker.runAction(s.intent,performance.now()+30000)).status).toBe('unknown');expect(s.calls.filter(c=>c.init.method==='POST')).toHaveLength(1);await close(s);
});
test('owned journal descendant cannot widen parent lifetime or publish direct tags after parent ends',async()=>{
 const s=await fixture(),parent=new AbortController(),owned=s.journal.withLifetime(parent.signal,performance.now()+1000),child=owned.withLifetime(new AbortController().signal,performance.now()+30000);parent.abort();
 for(const journal of [owned,child]){
  await expect(journal.reserveTag(gate,'Escaped','Escaped')).rejects.toThrow('LEASE_ENDED');
  await expect(journal.submitTag(gate,'Attention','Attention',TAG)).rejects.toThrow('LEASE_ENDED');
  await expect(journal.resolveTag(gate,'Attention','Attention',TAG)).rejects.toThrow('LEASE_ENDED');
  expect(()=>journal.withLifetime(new AbortController().signal,performance.now()+1000)).toThrow('LEASE_ENDED');
  await journal.close();
 }
 await close(s);
});
test.each(['issue','verify','settle'])('scope revocation INSIDE final %s IDB write rolls transaction back',async phase=>{
 const {IDBObjectStore}=await import('fake-indexeddb');const s=await fixture(),put=IDBObjectStore.prototype.put;let hit=false,revoked:Promise<unknown>|undefined;
 const spy=vi.spyOn(IDBObjectStore.prototype,'put').mockImplementation(function(this:IDBObjectStore,...args:any[]){const row=args[0],selected=this.name==='mutations'&&(phase==='issue'?row.effects?.[0].status==='issued':phase==='verify'?row.effects?.[0].status==='verified'&&row.status!=='verified':row.status==='verified');
  const result=(put as any).apply(this,args);if(selected&&!hit){hit=true;revoked=s.broker.signOut();}return result;
 });
 try{expect((await s.broker.runAction(s.intent,performance.now()+30000)).status).toBe('unknown');expect(hit).toBe(true);await revoked;if(phase==='issue')expect(s.calls.filter(c=>c.init.method==='POST')).toHaveLength(0);}finally{spy.mockRestore();}
 await close(s);
});
test('setup child final tag transaction is fenced by mandatory owned context',async()=>{
 const s=await fixture(true),reserve=V1Journal.prototype.reserveTag;let owned:V1Journal|undefined;
 const spy=vi.spyOn(V1Journal.prototype,'reserveTag').mockImplementation(async function(this:V1Journal,...args){owned=this;return reserve.apply(this,args);});
 try{expect(await s.broker.runSetup({kind:'tag',label:'Synthetic'},performance.now()+1000)).toBe(TAG);}finally{spy.mockRestore();}
 await expect(owned!.resolveTag(gate,'Synthetic','Synthetic',TAG)).rejects.toThrow('LEASE_ENDED');await close(s);
});
test('aborted child clone cannot replace inherited cancellation at final issue with another caller signal',async()=>{
 const s=await fixture(),child=new AbortController();let hit=false;const issue=V1Journal.prototype.issueGuardedEffect,submit=V1Http.prototype.submitEffect;
 const journalSpy=vi.spyOn(V1Journal.prototype,'issueGuardedEffect').mockImplementation(async function(this:V1Journal,...args){hit=true;child.abort();return issue.apply(this,args);});
 const httpSpy=vi.spyOn(V1Http.prototype,'submitEffect').mockImplementation(function(this:V1Http,options){return submit.call(this.withReadCancellation(child.signal),{...options,signal:new AbortController().signal});});
 try{expect((await s.broker.runAction(s.intent,performance.now()+30000)).status).toBe('unknown');expect(hit).toBe(true);expect(s.calls.filter(c=>c.init.method==='POST')).toHaveLength(0);}finally{journalSpy.mockRestore();httpSpy.mockRestore();}await close(s);
});
