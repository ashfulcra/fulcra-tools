// @vitest-environment node
import {afterEach,expect,test,vi} from 'vitest';
import {IDBFactory,IDBDatabase,IDBVersionChangeEvent,IDBObjectStore} from 'fake-indexeddb';
import {V1AuthStorage,credentialKey,initializationKey,makeV1Credential} from '../../src/relayless/v1AuthStorage';
import {V1Journal} from '../../src/relayless/v1Journal';
import {V1CaptureIngress} from '../../src/relayless/v1CaptureIngress';
import {memStorage} from './memStorage';
type Mutable<T>={-readonly[K in keyof T]:Mutable<T[K]>};
const ID='11111111-2222-4333-8444-555555555555';
const limits=(ms=500)=>({deadline:performance.now()+ms,signal:new AbortController().signal,remainingRecords:100,remainingBytes:1024*1024});
afterEach(()=>{vi.restoreAllMocks();vi.unstubAllGlobals();});

// Regresses accidentally using the creating/cached writer open path.
test('absent_read_does_not_create',async()=>{
 const indexedDB=new IDBFactory(),local=memStorage(),put=vi.spyOn(IDBObjectStore.prototype,'put');
 const auth=new V1AuthStorage({indexedDB,credentials:local}),journal=new V1Journal({indexedDB}),ingress=new V1CaptureIngress(indexedDB);
 expect((await auth.readStateBounded(limits())).status).toBe('unknown');
 expect((await journal.readGateEvidenceBounded(limits())).status).toBe('unknown');
 expect((await ingress.openExistingBounded(limits())).status).toBe('unknown');
 expect(await indexedDB.databases()).toEqual([]);expect(await local.get(null)).toEqual({});expect(put).not.toHaveBeenCalled();
});

test('old_version_never_upgrades',async()=>{
 const indexedDB=new IDBFactory();
 await new Promise<void>((resolve,reject)=>{const r=indexedDB.open('fulcra-attention-v1',4);r.onupgradeneeded=()=>r.result.createObjectStore('sentinel');r.onerror=()=>reject(r.error);r.onsuccess=()=>{r.result.close();resolve();};});
 const journal=new V1Journal({indexedDB});expect((await journal.readGateEvidenceBounded(limits())).status).toBe('unknown');
 expect(await indexedDB.databases()).toEqual([{name:'fulcra-attention-v1',version:4}]);
 const db=await new Promise<IDBDatabase>(resolve=>{const r=indexedDB.open('fulcra-attention-v1');r.onsuccess=()=>resolve(r.result);});
 expect([...db.objectStoreNames]).toEqual(['sentinel']);db.close();
});

// A platform request can remain blocked or deliver success after our deadline.
test('blocked_open_deadline',async()=>{
 const request={} as IDBOpenDBRequest;const factory={open:vi.fn(()=>request)} as unknown as IDBFactory;
 const pending=new V1Journal({indexedDB:factory}).readGateEvidenceBounded(limits(15));
 request.onblocked?.(new IDBVersionChangeEvent('blocked'));
 expect(await pending).toMatchObject({status:'unknown'});expect(factory.open).toHaveBeenCalledWith('fulcra-attention-v1');
});

test('late_open_closes_handle',async()=>{
 const request={} as IDBOpenDBRequest,factory={open:()=>request} as unknown as IDBFactory;
 const pending=new V1Journal({indexedDB:factory}).readGateEvidenceBounded(limits(10));
 expect(await pending).toMatchObject({status:'unknown'});
 const close=vi.fn(),transaction=vi.fn();Object.defineProperty(request,'result',{value:{close,transaction}});
 request.onsuccess?.(new Event('success'));expect(close).toHaveBeenCalledOnce();expect(transaction).not.toHaveBeenCalled();
});

test('late_credential_callback_has_no_effect',async()=>{
 const local=memStorage(),record=makeV1Credential(ID,ID,null,{access_token:'synthetic-only',expires_in:60},1);
 let release!:(v:Record<string,unknown>)=>void;local.get=()=>new Promise(resolve=>{release=resolve;});
 const set=vi.spyOn(local,'set'),remove=vi.spyOn(local,'remove'),indexedDB=new IDBFactory();
 const budget=limits(10),pending=new V1AuthStorage({indexedDB,credentials:local}).readCredentialBounded(ID,budget);
 expect(await pending).toMatchObject({status:'unknown'});const before={...budget};
 release({[credentialKey(ID)]:record});await Promise.resolve();await Promise.resolve();
 expect(budget).toEqual(before);expect(set).not.toHaveBeenCalled();expect(remove).not.toHaveBeenCalled();expect(await indexedDB.databases()).toEqual([]);
});

test('known_state_requires_committed_matching_witness_and_shared_budget',async()=>{
 const indexedDB=new IDBFactory(),local=memStorage(),auth=new V1AuthStorage({indexedDB,credentials:local});
 await auth.initializeAuthority(null);const expected=await auth.readState();await auth.close();
 const reader=new V1AuthStorage({indexedDB,credentials:local}),budget=limits();
 expect(await reader.readStateBounded(budget)).toEqual({status:'known',value:expected});expect(budget.remainingRecords).toBe(98);expect(budget.remainingBytes).toBeLessThan(1024*1024);
 const witness=(await local.get(initializationKey))[initializationKey] as Record<string,unknown>;await local.set({[initializationKey]:{...witness,stage:'CREATING'}});
 expect((await reader.readStateBounded(limits())).status).toBe('unknown');
 expect((await reader.readStateBounded({...limits(),remainingRecords:1})).status).toBe('unknown');
 expect((await reader.readStateBounded({...limits(),remainingBytes:10})).status).toBe('unknown');
});

test('read_abort_does_not_close_cached_writer_or_change_retained_credentials',async()=>{
 const indexedDB=new IDBFactory(),local=memStorage(),auth=new V1AuthStorage({indexedDB,credentials:local});await auth.initializeAuthority(null);
 const before=await auth.readState(),controller=new AbortController();controller.abort();
 expect((await auth.readStateBounded({...limits(),signal:controller.signal})).status).toBe('unknown');
 expect(await auth.readState()).toEqual(before);expect((await auth.readAttemptBounded(ID,limits())).status).toBe('unknown');await auth.close();
});

async function seedCapture(localValue:unknown,key='capture-source'){
 const indexedDB=new IDBFactory(),local=memStorage(),auth=new V1AuthStorage({indexedDB,credentials:local});await auth.initializeAuthority(null);await auth.close();
 const db=await new Promise<IDBDatabase>(resolve=>{const r=indexedDB.open('fulcra-attention-auth-v1');r.onsuccess=()=>resolve(r.result);});
 await new Promise<void>((resolve,reject)=>{const tx=db.transaction('state','readwrite');tx.objectStore('state').put(localValue,key);tx.oncomplete=()=>resolve();tx.onabort=()=>reject(tx.error);});db.close();
 return new V1AuthStorage({indexedDB,credentials:local});
}
test('raw_capture_point_evidence_is_detached_immutable_and_has_no_authority_schema',async()=>{
 const record={version:1,unvalidatedFutureField:{value:'synthetic'}},auth=await seedCapture(record),budget=limits();
 const result=await auth.readCaptureRecordBounded('capture-source',budget);expect(result).toEqual({status:'known',value:record});
 if(result.status==='known'){expect(Object.isFrozen(result.value)).toBe(true);expect(Object.isFrozen(result.value.unvalidatedFutureField)).toBe(true);}
 expect(budget.remainingRecords).toBe(99);
});
test.each(['authority','capture-setup:NOT-A-UUID','relaylessV1Credential:'+ID,'capture-group:AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE','capture-not-defined'])('raw_capture_key_allowlist_refuses %s',async key=>{
 const auth=await seedCapture({version:1},key);expect((await auth.readCaptureRecordBounded(key,limits())).status).toBe('unknown');
});
test.each([undefined,null,[],{version:2},{value:'missing-version'}])('raw_capture_absent_or_unsupported_envelope_stays_unknown %j',async value=>{
 const auth=await seedCapture(value===undefined?{version:1}:value);expect((await auth.readCaptureRecordBounded(value===undefined?'capture-selection':'capture-source',limits())).status).toBe('unknown');
});

test('default_codec_captures_metadata_before_its_existing_digest_await',async()=>{
 const {actorFixture}=await import('./v1ActionActorFixture'),{queuedRowFor}=await import('../../src/relayless/v1QueueCodec'),s=await actorFixture();
 const entry=structuredClone(s.entry) as Mutable<typeof s.entry>,oldTags=[...entry.record.tags],oldPrimary=entry.primary;
 let release!:(v:ArrayBuffer)=>void;vi.spyOn(crypto.subtle,'digest').mockImplementation(()=>new Promise(resolve=>{release=resolve;}));
 const pending=queuedRowFor(entry);entry.primary='later-forbidden';entry.record.tags=[];release(new ArrayBuffer(32));
 expect(await pending).toMatchObject({primary:oldPrimary,tagIds:oldTags});await s.journal.close();
});

test('readonly_projection_cannot_use_bounded_credential_seam',async()=>{
 const local=memStorage(),get=vi.spyOn(local,'get'),auth=new V1AuthStorage({indexedDB:new IDBFactory(),credentials:local,readOnly:true});
 expect((await auth.readCredentialBounded(ID,limits())).status).toBe('unknown');expect(get).not.toHaveBeenCalled();
});
test('closed_auth_and_journal_instances_do_not_reopen_independent_handles',async()=>{
 const indexedDB=new IDBFactory(),local=memStorage(),auth=new V1AuthStorage({indexedDB,credentials:local}),journal=new V1Journal({indexedDB});
 await auth.initializeAuthority(null);await journal.readGate();await auth.close();await journal.close();
 const open=vi.spyOn(indexedDB,'open'),get=vi.spyOn(local,'get');
 for(const result of [await auth.readStateBounded(limits()),await auth.readAttemptBounded(ID,limits()),await auth.readCaptureRecordBounded('capture-source',limits()),await auth.readCredentialBounded(ID,limits()),await journal.openEvidenceBounded(limits()),await journal.readGateEvidenceBounded(limits()),await journal.readAccountOwnerEvidence(ID,limits())])expect(result.status).toBe('unknown');
 expect(open).not.toHaveBeenCalled();expect(get).not.toHaveBeenCalled();
});
test('close_during_uncancellable_credential_read_discards_late_value',async()=>{
 const local=memStorage(),record=makeV1Credential(ID,ID,null,{access_token:'synthetic-only',expires_in:60},1);let release!:(v:Record<string,unknown>)=>void;
 local.get=()=>new Promise(resolve=>{release=resolve;});const auth=new V1AuthStorage({indexedDB:new IDBFactory(),credentials:local}),pending=auth.readCredentialBounded(ID,limits());
 await auth.close();release({[credentialKey(ID)]:record});expect((await pending).status).toBe('unknown');
});

test('inherited_journal_deadline_bounds_wait_without_replacing_shared_counters',async()=>{
 vi.useFakeTimers();const clock=vi.spyOn(performance,'now').mockReturnValue(100),request={} as IDBOpenDBRequest,factory={open:()=>request} as unknown as IDBFactory;
 try{
  const journal=new V1Journal({indexedDB:factory}),child=journal.withLifetime(new AbortController().signal,110),budget={deadline:1100,signal:new AbortController().signal,remainingRecords:20,remainingBytes:10000};
  let settled=false;const pending=child.readGateEvidenceBounded(budget).then(result=>{settled=true;return result;});
  await vi.advanceTimersByTimeAsync(11);expect(settled).toBe(true);expect((await pending).status).toBe('unknown');expect(budget).toEqual({deadline:1100,signal:budget.signal,remainingRecords:20,remainingBytes:10000});
  const close=vi.fn(),transaction=vi.fn();Object.defineProperty(request,'result',{value:{close,transaction}});request.onsuccess?.(new Event('success'));expect(close).toHaveBeenCalledOnce();expect(transaction).not.toHaveBeenCalled();
 }finally{clock.mockRestore();vi.useRealTimers();}
});
test('inherited_journal_abort_ends_read_and_closes_late_handle',async()=>{
 const request={} as IDBOpenDBRequest,factory={open:()=>request} as unknown as IDBFactory,controller=new AbortController();
 let settled=false;const journal=new V1Journal({indexedDB:factory}),child=journal.withLifetime(controller.signal,performance.now()+1000),pending=child.openEvidenceBounded(limits()).then(result=>{settled=true;return result;});
 controller.abort();await new Promise(resolve=>setTimeout(resolve,0));expect(settled).toBe(true);expect((await pending).status).toBe('unknown');
 const close=vi.fn(),transaction=vi.fn();Object.defineProperty(request,'result',{value:{close,transaction}});request.onsuccess?.(new Event('success'));expect(close).toHaveBeenCalledOnce();expect(transaction).not.toHaveBeenCalled();
});

test('structural_probes_close_only_their_own_handles_and_leave_writer_usable',async()=>{
 const indexedDB=new IDBFactory(),journal=new V1Journal({indexedDB}),ingress=new V1CaptureIngress(indexedDB);await journal.readGate();
 await ingress.checkpointPortion({id:'synthetic-checkpoint',origin:'visit',linkage:{},descriptor:null,raw:{kind:'synthetic'}});
 expect(await journal.openEvidenceBounded(limits())).toEqual({status:'known',value:null});expect(await ingress.openExistingBounded(limits())).toEqual({status:'known',value:null});
 expect(await journal.readGate()).toBeNull();expect((await ingress.read('synthetic-checkpoint'))?.raw).toEqual({kind:'synthetic'});await journal.close();
});

test.each(['auth-version','journal-shape','ingress-keypath'])('existing_unsupported_%s_never_migrates_or_creates_stores',async kind=>{
 const indexedDB=new IDBFactory(),name=kind==='auth-version'?'fulcra-attention-auth-v1':kind==='journal-shape'?'fulcra-attention-v1':'fulcra-attention-ingress-v1',version=kind==='auth-version'?2:kind==='journal-shape'?5:1;
 await new Promise<void>(resolve=>{const r=indexedDB.open(name,version);r.onupgradeneeded=()=>r.result.createObjectStore(kind==='ingress-keypath'?'checkpoints':'sentinel');r.onsuccess=()=>{r.result.close();resolve();};});
 const create=vi.spyOn(IDBDatabase.prototype,'createObjectStore'),put=vi.spyOn(IDBObjectStore.prototype,'put');
 const result=kind==='auth-version'?await new V1AuthStorage({indexedDB,credentials:memStorage()}).readStateBounded(limits()):kind==='journal-shape'?await new V1Journal({indexedDB}).readGateEvidenceBounded(limits()):await new V1CaptureIngress(indexedDB).openExistingBounded(limits());
 expect(result.status).toBe('unknown');expect(await indexedDB.databases()).toEqual([{name,version}]);expect(create).not.toHaveBeenCalled();expect(put).not.toHaveBeenCalled();
});

test('journal_close_before_open_callback_prevents_transaction_continuation',async()=>{
 const request={} as IDBOpenDBRequest,factory={open:()=>request} as unknown as IDBFactory,journal=new V1Journal({indexedDB:factory}),pending=journal.readGateEvidenceBounded(limits());
 await journal.close();const close=vi.fn(),transaction=vi.fn();Object.defineProperty(request,'result',{value:{close,transaction}});request.onsuccess?.(new Event('success'));
 expect((await pending).status).toBe('unknown');expect(close).toHaveBeenCalledOnce();expect(transaction).not.toHaveBeenCalled();
});

test('pending_read_transaction_is_aborted_at_deadline_and_late_result_is_discarded',async()=>{
 // A synthetic platform stall: shape validation completes; a later point read
 // never emits its result/transaction completion until after our deadline.
 const request={} as IDBOpenDBRequest,point={} as IDBRequest,abort=vi.fn(),close=vi.fn(),local=memStorage(),get=vi.spyOn(local,'get');let transactions=0;
 const db={version:1,objectStoreNames:['state','attempts'],close,transaction:()=>{
  const tx={abort,objectStore:()=>({keyPath:null,autoIncrement:false,indexNames:[],get:()=>point})} as unknown as IDBTransaction;
  if(++transactions===1)queueMicrotask(()=>tx.oncomplete?.(new Event('complete')));return tx;
 }};
 const factory={open:()=>{queueMicrotask(()=>{Object.defineProperty(request,'result',{value:db});request.onsuccess?.(new Event('success'));});return request;}} as unknown as IDBFactory;
 const pending=new V1AuthStorage({indexedDB:factory,credentials:local}).readStateBounded(limits(10));expect((await pending).status).toBe('unknown');
 expect(abort).toHaveBeenCalledOnce();expect(close).toHaveBeenCalledOnce();Object.defineProperty(point,'result',{value:{version:1}});point.onsuccess?.(new Event('success'));expect(get).not.toHaveBeenCalled();
});

// Models synchronous final traversal time; timers never preempt this work.
// Actual raw-capture API must recheck after traversing an already-frozen value.
test.each(['deadline','abort','close'] as const)('final_immutable_capture_%s_crossing_cannot_publish_known',async boundary=>{
 const auth=await seedCapture({version:1,boundaryMarker:'final-capture-traversal',nested:{retained:'synthetic'}}),controller=new AbortController();
 let now=0,traversals=0;const values=Object.values;
 vi.useFakeTimers({toFake:['setTimeout','clearTimeout']});vi.spyOn(performance,'now').mockImplementation(()=>now);
 vi.spyOn(Object,'values').mockImplementation(value=>{
  if(value&&typeof value==='object'&&Object.isFrozen(value)&&(value as Record<string,unknown>).boundaryMarker==='final-capture-traversal'){
   traversals++;
   if(boundary==='deadline')now=20;else if(boundary==='abort')controller.abort();else void auth.close();
  }
  return values(value);
 });
 try{
  const result=await auth.readCaptureRecordBounded('capture-source',{deadline:10,signal:controller.signal,remainingRecords:10,remainingBytes:10000});
  expect(traversals).toBe(1);expect(result.status).toBe('unknown');
 }finally{vi.useRealTimers();}
});
