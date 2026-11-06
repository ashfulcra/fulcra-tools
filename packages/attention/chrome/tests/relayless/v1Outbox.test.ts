// @vitest-environment node
import { IDBFactory, IDBDatabase, IDBObjectStore } from "fake-indexeddb";
import { describe, expect, test, vi } from "vitest";
import { V1Journal, type V1Gate } from "../../src/relayless/v1Journal";
import { V1Outbox } from "../../src/relayless/v1Outbox";
import { attentionV1Contract } from "../../src/relayless/v1Wire";
import { attentionV1Fingerprint, proveAttentionV1Schema, proveAttentionV1InputSchema } from "../../src/relayless/v1AttentionSpec";
import type { AttentionEvent } from "../../src/types";
const ACCOUNT="11111111-2222-4333-8444-555555555555",TAG="22222222-3333-4444-8555-666666666666";
const GEN="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",NEXT="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff";
const TARGET="Event/33333333-4444-4555-8666-777777777777";
const gate:V1Gate={version:1,accountId:ACCOUNT,generation:GEN,enabled:true};
const event:AttentionEvent={url:"https://example.com/",title:"Synthetic",og_description:null,favicon_url:null,category:null,
 chrome_identity:null,og_type:null,lang:null,start_time:"2026-01-01T00:00:00.125Z",end_time:"2026-01-01T00:00:01.250Z",client:"synthetic"};
async function setup(){
 const indexedDB=new IDBFactory(),journal=new V1Journal({indexedDB});await journal.transition(null,gate);
 const c=attentionV1Contract(),text=JSON.stringify({type:"object",properties:{id:{type:"string",format:"uuid"},
 sources:{type:"array",items:{type:"string"}},tags:{type:"array",items:{type:"string",format:"uuid"}},
 start_time:{type:"string",format:"date-time"},end_time:{type:"string",format:"date-time"},...c.fields},required:c.required_fields});
 const fingerprint=await attentionV1Fingerprint();await journal.reserveCreation(gate,c.key,fingerprint);await journal.resolveCreation(gate,c.key,fingerprint,TARGET);
 await journal.reserveTag(gate,"Attention","Attention");await journal.resolveTag(gate,"Attention","Attention",TAG);
 const binding={...await proveAttentionV1Schema(text),...await proveAttentionV1InputSchema(text),accountId:ACCOUNT,generation:GEN,dataType:TARGET,specFingerprint:fingerprint};
 const context={dataType:TARGET,tagIds:[TAG],identitySlug:"synthetic-browser",identityLabel:"Synthetic browser"};
 return {indexedDB,journal,binding,context,box:new V1Outbox(journal)};
}
async function raw(indexedDB:IDBFactory,store:string,action:(store:IDBObjectStore)=>void){
 const request=indexedDB.open("fulcra-attention-v1");await new Promise<void>((resolve,reject)=>{
 request.onerror=()=>reject(request.error);request.onsuccess=()=>{const tx=request.result.transaction(store,"readwrite");
 action(tx.objectStore(store));tx.onabort=()=>{request.result.close();reject(tx.error);};tx.oncomplete=()=>{request.result.close();resolve();};};});
}
async function rows(indexedDB:IDBFactory):Promise<any[]>{
 const request=indexedDB.open("fulcra-attention-v1");return new Promise((resolve,reject)=>{request.onerror=()=>reject(request.error);request.onsuccess=()=>{
 const tx=request.result.transaction("outbox","readonly"),read=tx.objectStore("outbox").getAll();let result:any[]=[];read.onsuccess=()=>{result=read.result;};
 tx.oncomplete=()=>{request.result.close();resolve(result);};tx.onabort=()=>reject(tx.error);};});
}
describe("immutable local Attention v1 outbox",()=>{
 test("restart retains exact account/schema/installation/primary/flat record and digest",async()=>{
  const s=await setup(),entry=await s.box.enqueue({scope:gate,binding:s.binding,event,context:s.context});
  expect(entry.record.duration_seconds).toBe(1.125);expect(entry.payloadHash).toMatch(/^[a-f0-9]{64}$/);expect(Object.isFrozen(entry.record.external_ids)).toBe(true);
  await s.journal.close();const reopened=new V1Journal({indexedDB:s.indexedDB});
  expect(await new V1Outbox(reopened).read(gate,TARGET,entry.record.id)).toEqual(entry);await reopened.close();
 });
 test("two independent contexts insert one identical identity; conflicting content cannot replace it",async()=>{
  const s=await setup(),peer=new V1Journal({indexedDB:s.indexedDB}),other=new V1Outbox(peer);
  const input={scope:gate,binding:s.binding,event,context:s.context};const [a,b]=await Promise.all([s.box.enqueue(input),other.enqueue(input)]);expect(a).toEqual(b);
  await expect(other.enqueue({...input,event:{...event,title:"Conflict"}})).rejects.toThrow();
  expect(await s.box.read(gate,TARGET,a.record.id)).toEqual(a);await s.journal.close();await peer.close();
 });
 test("caller mutation after invocation cannot change the captured payload or schema",async()=>{
  const s=await setup(),ev={...event},context={...s.context,tagIds:[TAG]},scope={...gate},binding={...s.binding};
  const pending=s.box.enqueue({scope,binding,event:ev,context});ev.title="Changed";context.identitySlug="other";context.tagIds.length=0;scope.generation=NEXT;binding.schemaText="{}";
  const result=await pending;expect(result.record.title).toBe("Synthetic");expect(result.context.identitySlug).toBe("synthetic-browser");expect(result.scope.generation).toBe(GEN);await s.journal.close();
 });
 test("account/enable/generation changes hold original entries without rebind",async()=>{
  const s=await setup(),input={scope:gate,binding:s.binding,event,context:s.context},entry=await s.box.enqueue(input);
  const next={...gate,generation:NEXT};await s.journal.transition(gate,next);
  await expect(s.box.read(gate,TARGET,entry.record.id)).rejects.toThrow();await expect(s.box.read(next,TARGET,entry.record.id)).rejects.toThrow();
  await expect(s.box.enqueue({...input,scope:next,binding:{...s.binding,generation:NEXT}})).rejects.toThrow();await s.journal.close();
 });
 test("disabled capture cannot create an entry",async()=>{
  const s=await setup(),scope={...gate,generation:NEXT,enabled:false};await s.journal.transition(gate,scope);
  await expect(s.box.enqueue({scope,binding:{...s.binding,generation:NEXT},event,context:s.context})).rejects.toThrow();await s.journal.close();
 });
 test.each(["schemaHash","inputSchemaHash","specFingerprint"])("a mismatched %s cannot persist",async key=>{
  const s=await setup();await expect(s.box.enqueue({scope:gate,binding:{...s.binding,[key]:"f".repeat(64)},event,context:s.context})).rejects.toThrow();await s.journal.close();
 });
 test("accessors are refused without invocation",async()=>{
  const s=await setup();let calls=0;const ev={...event};Object.defineProperty(ev,"title",{enumerable:true,get(){calls++;return "Synthetic";}});
  await expect(s.box.enqueue({scope:gate,binding:s.binding,event:ev,context:s.context})).rejects.toThrow();expect(calls).toBe(0);await s.journal.close();
 });
});
describe("Attention v1 outbox durable failure boundaries",()=>{
 test("gate changed during a pending hash cannot commit or return a stale read",async()=>{
  const s=await setup(),input={scope:gate,binding:s.binding,event,context:s.context};
  const entry=await s.box.enqueue(input),original=crypto.subtle.digest.bind(crypto.subtle);
  let changed=false;
  const spy=vi.spyOn(crypto.subtle,"digest").mockImplementation(async(...args)=>{
   if(!changed){changed=true;await s.journal.transition(gate,{...gate,generation:NEXT});}return original(...args);
  });
  try{await expect(s.box.read(gate,TARGET,entry.record.id)).rejects.toThrow();}finally{spy.mockRestore();}
  expect((await rows(s.indexedDB))).toHaveLength(1);await s.journal.close();
  const t=await setup();changed=false;
  const spy2=vi.spyOn(crypto.subtle,"digest").mockImplementation(async(...args)=>{
   if(!changed){changed=true;await t.journal.transition(gate,{...gate,generation:NEXT});}return original(...args);
  });
  try{await expect(t.box.enqueue({scope:gate,binding:t.binding,event,context:t.context})).rejects.toThrow();}finally{spy2.mockRestore();}
  expect(await rows(t.indexedDB)).toHaveLength(0);await t.journal.close();
 });
 test.each(["hash","record","scope","unknown","bytes"])("persisted %s corruption never becomes a verified pending record",async kind=>{
  const s=await setup(),entry=await s.box.enqueue({scope:gate,binding:s.binding,event,context:s.context});
  const [row]=await rows(s.indexedDB);
  if(kind==="hash")row.hash="f".repeat(64);
  if(kind==="bytes")row.bytes++;
  if(["record","scope","unknown"].includes(kind)){
   const body=JSON.parse(row.text);if(kind==="record")body.record.title="Changed";
   if(kind==="scope")body.scope.accountId=TAG;if(kind==="unknown")body.unknown=true;
   row.text=JSON.stringify(body);row.bytes=new TextEncoder().encode(row.text).length;
   const {sha256Hex}=await import("../../src/relayless/sha256");row.hash=await sha256Hex(row.text);
  }
  await raw(s.indexedDB,"outbox",store=>{store.put(row,[ACCOUNT,TARGET,entry.record.id]);});
  await expect(s.box.read(gate,TARGET,entry.record.id)).rejects.toThrow();expect(await rows(s.indexedDB)).toHaveLength(1);await s.journal.close();
 });
 test("malformed retained JSON never exposes payload fragments through parser exceptions",async()=>{
  const s=await setup(),entry=await s.box.enqueue({scope:gate,binding:s.binding,event,context:s.context}),[row]=await rows(s.indexedDB);
  row.text="SYNTHETIC_PRIVATE_QUEUE_NOTE";row.bytes=new TextEncoder().encode(row.text).length;
  const {sha256Hex}=await import("../../src/relayless/sha256");row.hash=await sha256Hex(row.text);
  await raw(s.indexedDB,"outbox",store=>{store.put(row,[ACCOUNT,TARGET,entry.record.id]);});
  let caught:unknown;try{await s.box.read(gate,TARGET,entry.record.id);}catch(error){caught=error;}
  expect(caught).toBeInstanceOf(Error);expect((caught as Error).message).not.toContain("SYNTHETIC");expect((caught as Error).message).toMatch(/cannot be verified/);
  expect(await rows(s.indexedDB)).toHaveLength(1);await s.journal.close();
 });
 test.each(["missing","pending","different"])("unresolved destination %s cannot capture",async kind=>{
  const s=await setup();await raw(s.indexedDB,"creations",store=>{
   if(kind==="missing")store.delete([ACCOUNT,"collect.attention.v1"]);
   else store.put({version:1,accountId:ACCOUNT,specKey:"collect.attention.v1",fingerprint:s.binding.specFingerprint,
    status:kind==="pending"?"pending":"resolved",createdUnderGeneration:GEN,destination:kind==="pending"?null:`Event/${TAG}`},[ACCOUNT,"collect.attention.v1"]);
  });await expect(s.box.enqueue({scope:gate,binding:s.binding,event,context:s.context})).rejects.toThrow();expect(await rows(s.indexedDB)).toHaveLength(0);await s.journal.close();
 });
 test.each(["missing","pending","otherAccount","wrongKey"])("unresolved tag %s cannot capture",async kind=>{
  const s=await setup();await raw(s.indexedDB,"tags",store=>{
   store.clear();if(kind!=="missing")store.put({version:1,accountId:kind==="otherAccount"?TAG:ACCOUNT,label:"Attention",name:"Attention",
    status:kind==="pending"?"pending":"resolved",createdUnderGeneration:GEN,destination:kind==="pending"?null:TAG},[kind==="otherAccount"||kind==="wrongKey"?TAG:ACCOUNT,"Attention"]);
  });await expect(s.box.enqueue({scope:gate,binding:s.binding,event,context:s.context})).rejects.toThrow();expect(await rows(s.indexedDB)).toHaveLength(0);await s.journal.close();
 });
 test("strict durability refusal and quota abort retain originals and report no success",async()=>{
  const s=await setup(),input={scope:gate,binding:s.binding,event,context:s.context};await s.box.enqueue(input);
  const original=IDBDatabase.prototype.transaction;
  const ignored=vi.spyOn(IDBDatabase.prototype,"transaction").mockImplementation(function(this:IDBDatabase,names,mode){return original.call(this,names,mode);});
  try{await expect(s.box.enqueue({...input,event:{...event,start_time:"2026-01-01T00:00:02Z",end_time:"2026-01-01T00:00:03Z"}})).rejects.toThrow();}finally{ignored.mockRestore();}
  const add=IDBObjectStore.prototype.add;
  const quota=vi.spyOn(IDBObjectStore.prototype,"add").mockImplementation(function(this:IDBObjectStore,...args){
   if(this.name==="outbox")throw new DOMException("synthetic quota","QuotaExceededError");return add.apply(this,args);
  });
  try{await expect(s.box.enqueue({...input,event:{...event,start_time:"2026-01-01T00:00:02Z",end_time:"2026-01-01T00:00:03Z"}})).rejects.toThrow();}finally{quota.mockRestore();}
  expect(await rows(s.indexedDB)).toHaveLength(1);await s.journal.close();
 });
 test("a successful add request followed by transaction abort never reports queued success",async()=>{
  const s=await setup(),input={scope:gate,binding:s.binding,event,context:s.context};await s.box.enqueue(input);
  const original=IDBObjectStore.prototype.add;
  const abort=vi.spyOn(IDBObjectStore.prototype,"add").mockImplementation(function(this:IDBObjectStore,...args){
   const request=original.apply(this,args);if(this.name==="outbox")request.addEventListener("success",()=>this.transaction.abort());return request;
  });
  try{await expect(s.box.enqueue({...input,event:{...event,start_time:"2026-01-01T00:00:02Z",end_time:"2026-01-01T00:00:03Z"}})).rejects.toThrow();}finally{abort.mockRestore();}
  expect(await rows(s.indexedDB)).toHaveLength(1);await s.journal.close();
 });
 test("entry count capacity rejects instead of evicting, while exact replay remains idempotent",async()=>{
  const s=await setup(),input={scope:gate,binding:s.binding,event,context:s.context},entry=await s.box.enqueue(input),[row]=await rows(s.indexedDB);
  // Synthetic low-level wrapper fixtures exercise the bounded capacity scan;
  // these are not claimed to have passed semantic envelope reproof.
  await raw(s.indexedDB,"outbox",store=>{for(let i=0;i<4999;i++){const id=`00000000-0000-4000-8000-${i.toString().padStart(12,"0")}`;store.put({...row,recordId:id},[ACCOUNT,TARGET,id]);}});
  await expect(s.box.enqueue({...input,event:{...event,start_time:"2026-01-01T00:00:02Z",end_time:"2026-01-01T00:00:03Z"}})).rejects.toThrow(/full/);
  expect(await rows(s.indexedDB)).toHaveLength(5000);expect(await s.box.enqueue(input)).toEqual(entry);await s.journal.close();
 });
 test("aggregate byte capacity rejects instead of evicting",async()=>{
  const s=await setup(),input={scope:gate,binding:s.binding,event,context:s.context};await s.box.enqueue(input);const [row]=await rows(s.indexedDB);
  await raw(s.indexedDB,"outbox",store=>{for(let i=0;i<8;i++){const id=`00000000-0000-4000-8000-${i.toString().padStart(12,"0")}`;
   store.put({...row,recordId:id,text:"x".repeat(4*1024*1024),bytes:4*1024*1024},[ACCOUNT,TARGET,id]);}});
  await expect(s.box.enqueue({...input,event:{...event,start_time:"2026-01-01T00:00:02Z",end_time:"2026-01-01T00:00:03Z"}})).rejects.toThrow(/full/);
  expect(await rows(s.indexedDB)).toHaveLength(9);await s.journal.close();
 });
 test("v2 upgrade preserves prior gates, type/tag intents and adds only the outbox",async()=>{
  const hash=await attentionV1Fingerprint();
  const indexedDB=new IDBFactory(),request=indexedDB.open("fulcra-attention-v1",2);
  await new Promise<void>((resolve,reject)=>{request.onupgradeneeded=()=>{
   const db=request.result,store=db.createObjectStore("gate");store.put({version:1,initialized:true},"format");store.put(gate,"current");
   db.createObjectStore("generations").put(true,GEN);
   db.createObjectStore("creations").put({version:1,accountId:ACCOUNT,specKey:"collect.attention.v1",fingerprint:hash,status:"resolved",createdUnderGeneration:GEN,destination:TARGET},[ACCOUNT,"collect.attention.v1"]);
   db.createObjectStore("tags").createIndex("accountName",["accountId","name"],{unique:true});
  };request.onerror=()=>reject(request.error);request.onsuccess=()=>{request.result.close();resolve();};});
  const journal=new V1Journal({indexedDB});expect(await journal.readGate()).toEqual(gate);expect((await journal.readCreation(gate,"collect.attention.v1",hash))?.destination).toBe(TARGET);
  expect(await rows(indexedDB)).toEqual([]);await expect(journal.transition(gate,{...gate,generation:GEN})).rejects.toThrow();await journal.close();
 });
});
