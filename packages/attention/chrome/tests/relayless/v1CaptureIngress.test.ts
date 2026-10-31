// @vitest-environment node
import {beforeEach,expect,test,vi} from 'vitest';
import {IDBFactory,IDBObjectStore} from 'fake-indexeddb';
import {V1CaptureIngress,CAPTURE_BUDGET_BYTES} from '../../src/relayless/v1CaptureIngress';
import {V1Journal,type V1Gate} from '../../src/relayless/v1Journal';
import {V1Outbox} from '../../src/relayless/v1Outbox';
import {attentionV1Contract} from '../../src/relayless/v1Wire';
import {attentionV1Fingerprint,proveAttentionV1Schema,proveAttentionV1InputSchema} from '../../src/relayless/v1AttentionSpec';
import type {AttentionEvent} from '../../src/types';
vi.stubGlobal('fetch',()=>{throw Error('External transport forbidden');});
const ACCOUNT='11111111-2222-4333-8444-555555555555',TAG='22222222-3333-4444-8555-666666666666',GEN='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',TARGET='Event/33333333-4444-4555-8666-777777777777';
const scope:V1Gate={version:1,accountId:ACCOUNT,generation:GEN,enabled:true};
const event:AttentionEvent={url:'https://synthetic.invalid/',title:'Original',og_description:null,favicon_url:null,category:null,chrome_identity:null,og_type:null,lang:null,start_time:'2026-01-01T00:00:00.123Z',end_time:'2026-01-01T00:00:01.456Z',client:'synthetic'};
const input={id:'visit:synthetic:0',origin:'visit' as const,linkage:{store:'session.visits',tabId:1,startTime:1},descriptor:null,raw:{kind:'visit',url:'https://synthetic.invalid/',startTime:1,checkpointAt:2}};
beforeEach(()=>vi.stubGlobal('indexedDB',new IDBFactory()));
async function setup(){
 const journal=new V1Journal();await journal.transition(null,scope);const c=attentionV1Contract();
 const text=JSON.stringify({type:'object',properties:{id:{type:'string',format:'uuid'},sources:{type:'array',items:{type:'string'}},tags:{type:'array',items:{type:'string',format:'uuid'}},start_time:{type:'string',format:'date-time'},end_time:{type:'string',format:'date-time'},...c.fields},required:c.required_fields});
 const specFingerprint=await attentionV1Fingerprint();await journal.reserveCreation(scope,c.key,specFingerprint);await journal.resolveCreation(scope,c.key,specFingerprint,TARGET);await journal.reserveTag(scope,'Attention','Attention');await journal.resolveTag(scope,'Attention','Attention',TAG);
 const binding={...await proveAttentionV1Schema(text),...await proveAttentionV1InputSchema(text),accountId:ACCOUNT,generation:GEN,dataType:TARGET,specFingerprint};
 const descriptor={version:1 as const,scope,binding,context:{dataType:TARGET,tagIds:[TAG],identitySlug:'synthetic'},provenance:{publicationId:'synthetic-publication',scopeFingerprint:'synthetic-scope',consentRevision:'synthetic-consent',installationId:'synthetic-installation',setupReceiptId:'synthetic-setup',dispatchReceiptId:'synthetic-dispatch'}};
 const ingress=new V1CaptureIngress();await ingress.checkpointPortion({...input,descriptor});await ingress.sealPortion(input.id,event);return {ingress,journal,descriptor};
}
async function mutate(fn:(store:IDBObjectStore)=>void){const db=await new Promise<IDBDatabase>((resolve,reject)=>{const r=indexedDB.open('fulcra-attention-ingress-v1');r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});try{await new Promise<void>((resolve,reject)=>{const t=db.transaction('checkpoints','readwrite');fn(t.objectStore('checkpoints'));t.oncomplete=()=>resolve();t.onabort=()=>reject(t.error);});}finally{db.close();}}
test('durable checkpoint survives restart and duplicate seal freezes first metadata and identity',async()=>{
 const a=new V1CaptureIngress();await a.checkpointPortion(input);await a.sealPortion(input.id,event);
 const b=new V1CaptureIngress();await b.sealPortion(input.id,{...event,title:'Changed'});await b.checkpointPortion({...input,raw:{kind:'changed'}});
 const r=await b.read(input.id);expect(r?.prepared?.title).toBe('Original');expect(r?.raw).toEqual(input.raw);expect(await b.status()).toMatchObject({retained:1,held:1,active:0,verifiedUploaded:0,drains:'disabled'});
});
test('aborted durable write never acknowledges capture and preserves original checkpoint',async()=>{
 const a=new V1CaptureIngress();await a.checkpointPortion(input);const put=IDBObjectStore.prototype.put;
 const spy=vi.spyOn(IDBObjectStore.prototype,'put').mockImplementation(function(this:IDBObjectStore,...args){const r=put.apply(this,args);if(this.name==='checkpoints')r.addEventListener('success',()=>{try{this.transaction.abort();}catch{}});return r;});
 try{await expect(a.sealPortion(input.id,event)).rejects.toThrow('UNKNOWN');}finally{spy.mockRestore();}
 expect((await a.read(input.id))?.prepared).toBeNull();expect((await a.status()).active).toBe(1);
});
test('concurrent exact retry retains one checkpoint without last-writer-wins prepared data',async()=>{
 const a=new V1CaptureIngress(),b=new V1CaptureIngress();await Promise.all([a.checkpointPortion(input),b.checkpointPortion(input)]);await Promise.all([a.sealPortion(input.id,event),b.sealPortion(input.id,{...event,title:'Late'})]);
 const rows=await a.rows();expect(rows).toHaveLength(1);expect(['Original','Late']).toContain(rows[0].prepared?.title);const title=rows[0].prepared?.title;await a.sealPortion(input.id,{...event,title:'Overwrite'});expect((await a.read(input.id))?.prepared?.title).toBe(title);
});
test('byte budget is atomic backpressure and never evicts existing evidence',async()=>{
 const a=new V1CaptureIngress();await a.checkpointPortion(input);
 // Fill through real serialized records; ASCII payload has literal byte size.
 const chunk='x'.repeat(7*1024*1024);let added=0;
 try{for(let i=0;i<10;i++){await a.checkpointPortion({...input,id:`budget:${i}`,raw:{chunk}});added++;}}catch(error){expect((error as Error).message).toContain('UNKNOWN');}
 expect(added).toBe(9);const status=await a.status();expect(status.retained).toBe(10);expect(status.serializedBytes).toBeLessThanOrEqual(CAPTURE_BUDGET_BYTES);expect((await a.read(input.id))?.raw).toEqual(input.raw);
});
test('lost enqueue acknowledgement recovers exact retained candidate without issuing enqueue again',async()=>{
 const s=await setup(),enqueue=V1Outbox.prototype.enqueue;let calls=0;
 const spy=vi.spyOn(V1Outbox.prototype,'enqueue').mockImplementation(async function(this:V1Outbox,...args){calls++;const out=await enqueue.apply(this,args);throw Error('synthetic lost ack');return out;});
 try{await expect(s.ingress.enqueueCaptured(input.id,{current:async()=>true,journal:s.journal})).rejects.toThrow('lost ack');}finally{spy.mockRestore();}
 const pending=await s.ingress.read(input.id);expect(pending?.state).toBe('prepared');const candidate=JSON.stringify(pending?.candidate);
 const forbid=vi.spyOn(V1Outbox.prototype,'enqueue').mockImplementation(async()=>{throw Error('must not repeat enqueue');});
 try{expect((await s.ingress.enqueueCaptured(input.id,{current:async()=>true,journal:s.journal})).state).toBe('enqueued');}finally{forbid.mockRestore();await s.journal.close();}
 expect(calls).toBe(1);expect(JSON.stringify((await s.ingress.read(input.id))?.candidate)).toBe(candidate);
});
test('scope change during preparation holds original candidate and never attributes it to the new generation',async()=>{
 const s=await setup();let checks=0;const r=await s.ingress.enqueueCaptured(input.id,{current:async()=>++checks===1,journal:s.journal});
 expect(r.state).toBe('held');expect(r.reason).toBe('descriptor-not-current');expect(r.candidate?.scope.generation).toBe(GEN);expect(await s.journal.readOutbox(scope,TARGET,r.candidate!.record.id)).toBeNull();await s.journal.close();
});
test('unreadable accounting is UNKNOWN instead of clean-empty status',async()=>{
 const a=new V1CaptureIngress();await a.checkpointPortion(input);await mutate(s=>s.delete('$accounting'));await expect(a.status()).rejects.toThrow('UNKNOWN');await expect(a.checkpointPortion({...input,id:'second'})).rejects.toThrow('UNKNOWN');
});
test('R1 I3 final accounting callback fences preparation if lifetime changes after the row read',async()=>{
 const a=new V1CaptureIngress();await a.checkpointPortion(input);let current=true;const get=IDBObjectStore.prototype.get;
 const spy=vi.spyOn(IDBObjectStore.prototype,'get').mockImplementation(function(this:IDBObjectStore,...args){const r=get.apply(this,args);if(this.name==='checkpoints'&&args[0]==='$accounting')r.addEventListener('success',()=>{current=false;});return r;});
 let row;try{row=await (a.sealPortion as any)(input.id,event,'unattributed',()=>current);}finally{spy.mockRestore();}expect(row.prepared).toBeNull();expect(row.reason).toBe('capture-scope-changed');expect(row.raw).toEqual(input.raw);
});
test('R1 I2 source context is detached once and never filled from later settings',async()=>{
 const a=new V1CaptureIngress(),sourceContext={version:1 as const,client:'original-client',identityLabel:'Original label'};const pending=a.checkpointPortion({...input,sourceContext});sourceContext.identityLabel='Caller mutation';await pending;
 await a.checkpointPortion({...input,sourceContext:{version:1,client:'new-client',identityLabel:'Retry label'}});expect((await a.read(input.id))?.sourceContext).toEqual({version:1,client:'original-client',identityLabel:'Original label'});
});
test('R1 I2 exact old row shape stays explicitly unknown rather than acquiring retry context',async()=>{
 const a=new V1CaptureIngress();await a.checkpointPortion(input);
 await mutate(s=>{const r=s.get(input.id);r.onsuccess=()=>{const before=JSON.stringify(r.result),old=r.result;delete old.sourceContext;s.put(old);const read=s.get('$accounting');read.onsuccess=()=>s.put({...read.result,bytes:read.result.bytes+new TextEncoder().encode(JSON.stringify(old)).length-new TextEncoder().encode(before).length});};});
 expect(Object.hasOwn((await a.read(input.id))!,'sourceContext')).toBe(false);await a.checkpointPortion({...input,sourceContext:{version:1,client:'current-client',identityLabel:'Forbidden retry label'}});expect((await a.read(input.id))?.sourceContext).toBeUndefined();
});
test.each([{version:1,client:'',identityLabel:null},{version:1,client:'client',identityLabel:[]},{version:1,client:'client',identityLabel:null,accountId:ACCOUNT}])('R1 I2 source context rejects malformed or authority-shaped additions',async sourceContext=>{
 const a=new V1CaptureIngress();await expect(a.checkpointPortion({...input,sourceContext} as any)).rejects.toThrow('UNKNOWN');expect((await a.status()).retained).toBe(0);
});
test('R1 I3 only synchronous true may prepare, while sealed history survives a revoked retry',async()=>{
 const a=new V1CaptureIngress();await a.checkpointPortion(input);const refused=await a.sealPortion(input.id,event,'unattributed',(()=>Promise.resolve(true)) as any);expect(refused.prepared).toBeNull();expect(refused.reason).toBe('capture-scope-changed');
 await a.checkpointPortion({...input,id:'confirmed'});await a.sealPortion('confirmed',event);const before=await a.read('confirmed');await a.sealPortion('confirmed',{...event,title:'Forbidden replacement'},'unattributed',()=>false);expect(await a.read('confirmed')).toEqual(before);
});
test('R1 I5 loss fact cannot be erased by a later generic hold',async()=>{
 const a=new V1CaptureIngress();await a.checkpointPortion({...input,id:'legacy',origin:'legacy',raw:{kind:'legacy-link',retention:'original-store-only'}});await a.sealPortion('legacy',null,'legacy-owner-unproved');await a.noteLegacyLoss('legacy');await a.hold('legacy','quiet');expect((await a.read('legacy'))?.reason).toBe('legacy-retention-unknown');expect((await a.status()).legacyRetentionUnknown).toBe(1);
});
