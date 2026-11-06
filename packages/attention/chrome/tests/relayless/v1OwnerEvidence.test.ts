// @vitest-environment node
import {afterEach,expect,test,vi} from 'vitest';
import {IDBFactory,IDBIndex,IDBObjectStore} from 'fake-indexeddb';
import {V1Journal,type V1Gate} from '../../src/relayless/v1Journal';
const ACCOUNT='11111111-2222-4333-8444-555555555555',ACTION='22222222-3333-4444-8555-666666666666';
const GENERATION='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',NEXT='bbbbbbbb-cccc-4ddd-8eee-ffffffffffff';
const gate:V1Gate={version:1,accountId:ACCOUNT,generation:GENERATION,enabled:true};
const limits=()=>({deadline:performance.now()+1000,signal:new AbortController().signal,remainingRecords:100,remainingBytes:8*1024*1024});
afterEach(()=>{vi.restoreAllMocks();vi.unstubAllGlobals();});
async function fixture(){const indexedDB=new IDBFactory(),journal=new V1Journal({indexedDB});await journal.transition(null,gate);const {intent}=await journal.reserveMutation(gate,ACTION,'deliver',{original:'request bytes'});return {indexedDB,journal,intent};}
async function corrupt(indexedDB:IDBFactory,edit:(tx:IDBTransaction)=>void){
 const db=await new Promise<IDBDatabase>(resolve=>{const r=indexedDB.open('fulcra-attention-v1');r.onsuccess=()=>resolve(r.result);});
 await new Promise<void>((resolve,reject)=>{const tx=db.transaction(['mutations','currentMutation','outbox'],'readwrite');edit(tx);tx.oncomplete=()=>resolve();tx.onabort=()=>reject(tx.error);});db.close();
}

test('owner_requires_pointer_and_both_status_scans',async()=>{
 const {journal,intent}=await fixture(),scans:string[]=[],open=IDBIndex.prototype.openCursor;
 vi.spyOn(IDBIndex.prototype,'openCursor').mockImplementation(function(this:IDBIndex,...args:Parameters<IDBIndex['openCursor']>){scans.push(this.name+':'+JSON.stringify(args[0]));return open.apply(this,args);});
 const put=vi.spyOn(IDBObjectStore.prototype,'put'),fetch=vi.fn(()=>{throw Error('Network forbidden');});vi.stubGlobal('fetch',fetch);
 const evidence=await journal.readAccountOwnerEvidence(ACCOUNT,limits());
 expect(evidence).toMatchObject({status:'known',observedGate:gate,owner:{scope:{accountId:ACCOUNT,generation:GENERATION},intent},terminalPointer:null,reasons:[]});
 expect(scans).toContain('accountState:'+JSON.stringify([ACCOUNT,'prepared']));expect(scans).toContain('accountState:'+JSON.stringify([ACCOUNT,'issued']));expect(scans).toContain('accountAction:'+JSON.stringify([ACCOUNT,ACTION]));
 expect(put).not.toHaveBeenCalled();expect(fetch).not.toHaveBeenCalled();await journal.close();
});

test('missing_pointer_is_unknown',async()=>{
 const {indexedDB,journal}=await fixture();await corrupt(indexedDB,tx=>{tx.objectStore('currentMutation').delete(ACCOUNT);});
 expect((await journal.readAccountOwnerEvidence(ACCOUNT,limits())).status).toBe('unknown');await journal.close();
});

test('old_generation_is_evidence_only',async()=>{
 const {journal,intent}=await fixture(),current={...gate,generation:NEXT};await journal.transition(gate,current);
 const evidence=await journal.readAccountOwnerEvidence(ACCOUNT,limits());
 expect(evidence).toMatchObject({status:'known',observedGate:current,owner:{scope:{accountId:ACCOUNT,generation:GENERATION},intent}});
 await expect(journal.readMutation(gate,ACTION)).rejects.toThrow();expect(await journal.readGate()).toEqual(current);await journal.close();
});

test('owner_body_digest_mismatch',async()=>{
 const {indexedDB,journal}=await fixture();await corrupt(indexedDB,tx=>{const s=tx.objectStore('mutations'),r=s.get([ACCOUNT,ACTION]);r.onsuccess=()=>s.put({...r.result,requestHash:'f'.repeat(64)},[ACCOUNT,ACTION]);const p=tx.objectStore('currentMutation'),q=p.get(ACCOUNT);q.onsuccess=()=>p.put({...q.result,requestHash:'f'.repeat(64)},ACCOUNT);});
 expect((await journal.readAccountOwnerEvidence(ACCOUNT,limits())).status).toBe('unknown');await journal.close();
});

test('count_without_scan_cannot_prove_absence',async()=>{
 const {indexedDB,journal}=await fixture();await corrupt(indexedDB,tx=>{tx.objectStore('currentMutation').delete(ACCOUNT);});const count=IDBIndex.prototype.count;
 vi.spyOn(IDBIndex.prototype,'count').mockImplementation(function(this:IDBIndex,...args:Parameters<IDBIndex['count']>){return this.name==='accountId'?count.call(this,'no-such-account'):count.apply(this,args);});
 expect((await journal.readAccountOwnerEvidence(ACCOUNT,limits())).status).toBe('unknown');await journal.close();
});

test('full_referenced_body_is_original_evidence_across_current_account_switch',async()=>{
 const {actorFixture}=await import('./v1ActionActorFixture'),s=await actorFixture(),current={...gate,accountId:ACTION,generation:NEXT};
 await s.journal.transition(gate,current);const put=vi.spyOn(IDBObjectStore.prototype,'put');
 const result=await s.journal.readAccountOwnerEvidence(ACCOUNT,limits());expect(result).toMatchObject({status:'known',observedGate:current,owner:{intent:s.intent,scope:{accountId:ACCOUNT,generation:GENERATION}}});
 expect(put).not.toHaveBeenCalled();await s.journal.close();
});
test.each(['missing','hash','semantic'])('referenced_body_%s_is_unknown',async kind=>{
 const {actorFixture,TARGET}=await import('./v1ActionActorFixture'),s=await actorFixture();
 await corrupt(s.indexedDB,tx=>{const store=tx.objectStore('outbox'),key=[ACCOUNT,TARGET,s.entry.record.id];if(kind==='missing'){store.delete(key);return;}
  const r=store.get(key);r.onsuccess=()=>{const value=r.result;if(kind==='hash'){value.hash='f'.repeat(64);store.put(value,key);}else{const envelope=JSON.parse(value.text);envelope.event.title='silently rewritten';value.text=JSON.stringify(envelope);value.bytes=new TextEncoder().encode(value.text).length;store.put(value,key);}};
 });
 expect((await s.journal.readAccountOwnerEvidence(ACCOUNT,limits())).status).toBe('unknown');await s.journal.close();
});

// Regresses late nested source->record and input-schema->input-hash launches.
test.each(['source','input-schema'])('late_%s_crypto_cannot_launch_the_next_digest',async boundary=>{
 const {actorFixture}=await import('./v1ActionActorFixture'),s=await actorFixture(),original=crypto.subtle.digest.bind(crypto.subtle);
 let release!:(value:ArrayBuffer)=>void,entered!:(value:void)=>void,blocked=false;const started=new Promise<void>(resolve=>{entered=resolve;});
 const decode=(v:BufferSource)=>new TextDecoder().decode(v as ArrayBuffer);
 const digest=vi.spyOn(crypto.subtle,'digest').mockImplementation((algorithm,data)=>{
  const text=decode(data),isBoundary=boundary==='source'?text.includes('https://example.com/|'):text===s.entry.binding.schemaText;
  // schemaHash and input's stripped schema can be byte identical: block the
  // second schema digest, inside proveV1EventInputSchema's two-await path.
  if(isBoundary&&(!blocked)&&(boundary==='source'||digest.mock.calls.filter(([,bytes])=>decode(bytes)===s.entry.binding.schemaText).length===2)){
   blocked=true;entered();return new Promise(resolve=>{release=resolve;});
  }return original(algorithm,data);
 });
 const budget={...limits(),deadline:performance.now()+100},pending=s.journal.readAccountOwnerEvidence(ACCOUNT,budget);
 await started;expect(await pending).toMatchObject({status:'unknown'});const count=digest.mock.calls.length,spent={...budget};
 release(new ArrayBuffer(32));await new Promise(resolve=>setTimeout(resolve,10));expect(digest).toHaveBeenCalledTimes(count);expect(budget).toEqual(spent);await s.journal.close();
});


test('verified_owner_retains_complete_original_proof_and_terminal_pointer',async()=>{
 const {journal,intent}=await fixture(),issued=await journal.issueMutation(gate,intent);expect(issued).toBe(true);
 const terminal=await journal.verifyMutation(gate,(await journal.readMutation(gate,ACTION))!,{version:1,outcome:'delivered',evidence:{syntheticOriginal:'guarded retained proof'}});
 const result=await journal.readAccountOwnerEvidence(ACCOUNT,limits());
 expect(result).toMatchObject({status:'known',owner:{scope:{accountId:ACCOUNT,generation:GENERATION},intent:terminal},terminalPointer:{accountId:ACCOUNT,generation:GENERATION,actionId:ACTION,status:'verified',proofHash:terminal.proofHash},reasons:[]});
 expect(Object.isFrozen(result.owner?.intent)).toBe(true);await journal.close();
});

test('positive_empty_requires_both_completed_status_scans',async()=>{
 const indexedDB=new IDBFactory(),journal=new V1Journal({indexedDB});await journal.transition(null,gate);
 const scans:unknown[]=[],original=IDBIndex.prototype.openCursor;vi.spyOn(IDBIndex.prototype,'openCursor').mockImplementation(function(this:IDBIndex,...args:Parameters<IDBIndex['openCursor']>){if(this.name==='accountState')scans.push(args[0]);return original.apply(this,args);});
 expect(await journal.readAccountOwnerEvidence(ACCOUNT,limits())).toEqual({status:'known',observedGate:gate,owner:null,terminalPointer:null,reasons:[]});
 expect(scans).toEqual([[ACCOUNT,'prepared'],[ACCOUNT,'issued']]);await journal.close();
});

test('verified_referenced_owner_survives_legitimately_removed_queue_body',async()=>{
 const {actorFixture,gate:originalGate}=await import('./v1ActionActorFixture'),s=await actorFixture();
 const first=await s.journal.verifyEffect(originalGate,s.intent,'deliver',{version:1,outcome:'present',evidence:{}}),both=await s.journal.verifyEffect(originalGate,first,'repair',{version:1,outcome:'not-needed',evidence:{}});
 const terminal=await s.journal.verifyMutation(originalGate,both,{version:1,outcome:'delivered',evidence:{}});
 await corrupt(s.indexedDB,tx=>{tx.objectStore('outbox').clear();});
 expect(await s.journal.readAccountOwnerEvidence(ACCOUNT,limits())).toMatchObject({status:'known',owner:{intent:terminal},terminalPointer:{status:'verified'}});await s.journal.close();
});

// Exercises the public OwnerEvidence wrapper, not only the common job value.
test.each(['deadline','abort','close'] as const)('final_owner_wrapper_%s_crossing_cannot_publish_known',async boundary=>{
 const indexedDB=new IDBFactory(),writer=new V1Journal({indexedDB});await writer.transition(null,gate);await writer.close();
 const journal=new V1Journal({indexedDB}),controller=new AbortController();let now=0,traversals=0;const values=Object.values;
 vi.useFakeTimers({toFake:['setTimeout','clearTimeout']});vi.spyOn(performance,'now').mockImplementation(()=>now);
 vi.spyOn(Object,'values').mockImplementation(value=>{
  if(value&&typeof value==='object'&&(value as Record<string,unknown>).status==='known'&&Object.hasOwn(value,'owner')&&Object.hasOwn(value,'observedGate')){
   traversals++;
   if(boundary==='deadline')now=20;else if(boundary==='abort')controller.abort();else void journal.close();
  }
  return values(value);
 });
 try{
  const result=await journal.readAccountOwnerEvidence(ACCOUNT,{deadline:10,signal:controller.signal,remainingRecords:100,remainingBytes:100000});
  expect(traversals).toBe(1);expect(result.status).toBe('unknown');expect(result.owner).toBeNull();
 }finally{vi.useRealTimers();await journal.close();}
});
