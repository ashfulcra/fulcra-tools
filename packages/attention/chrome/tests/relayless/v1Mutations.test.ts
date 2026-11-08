// @vitest-environment node
import {IDBFactory,IDBObjectStore} from "fake-indexeddb";
import {describe,expect,test,vi} from "vitest";
import {V1Journal,type V1Gate} from "../../src/relayless/v1Journal";
const ACCOUNT="11111111-2222-4333-8444-555555555555",OTHER="22222222-3333-4444-8555-666666666666";
const GEN="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",NEXT="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff";
const ACTION="33333333-4444-4555-8666-777777777777",SECOND="44444444-5555-4666-8777-888888888888";
const gate:V1Gate={version:1,accountId:ACCOUNT,generation:GEN,enabled:true};
const request={target:`Event/${OTHER}`,recordIds:[ACTION],payloadHashes:["a".repeat(64)]};
const proof={version:1,outcome:"delivered",evidence:{synthetic:true}};
async function setup(){const indexedDB=new IDBFactory(),journal=new V1Journal({indexedDB}),peer=new V1Journal({indexedDB});await journal.transition(null,gate);return {indexedDB,journal,peer};}
async function finish(s:Awaited<ReturnType<typeof setup>>){await s.journal.close();await s.peer.close();}
async function raw(indexedDB:IDBFactory,store:string,action:(s:IDBObjectStore)=>void){const opened=indexedDB.open("fulcra-attention-v1");await new Promise<void>((resolve,reject)=>{opened.onerror=()=>reject(opened.error);opened.onsuccess=()=>{const db=opened.result,tx=db.transaction(store,"readwrite");action(tx.objectStore(store));tx.oncomplete=()=>{db.close();resolve();};tx.onabort=()=>{db.close();reject(tx.error);};};});}
describe("network-free durable browser mutation ownership",()=>{
 test("immutable preparation survives restart without dispatch",async()=>{const s=await setup(),input={...request,recordIds:[ACTION]};const pending=s.journal.reserveMutation(gate,ACTION,"deliver",input);input.recordIds.length=0;const r=await pending;expect(r.created).toBe(true);expect(r.intent.status).toBe("prepared");expect(JSON.parse(r.intent.requestText)).toEqual(request);expect(r.intent.requestHash).toMatch(/^[a-f0-9]{64}$/);await s.journal.close();expect(await s.peer.readMutation(gate,ACTION)).toEqual(r.intent);await s.peer.close();});
 test("same-action replay cannot replace its operation or body",async()=>{const s=await setup(),a=await s.journal.reserveMutation(gate,ACTION,"deliver",request);expect(await s.peer.reserveMutation(gate,ACTION,"deliver",request)).toEqual({created:false,intent:a.intent});await expect(s.peer.reserveMutation(gate,ACTION,"exclude",request)).rejects.toThrow();await expect(s.peer.reserveMutation(gate,ACTION,"deliver",{...request,recordIds:[SECOND]})).rejects.toThrow();await finish(s);});
 test("independent contexts cannot reserve different account mutations at once",async()=>{const s=await setup(),results=await Promise.allSettled([s.journal.reserveMutation(gate,ACTION,"deliver",request),s.peer.reserveMutation(gate,SECOND,"exclude",request)]);expect(results.filter(r=>r.status==="fulfilled")).toHaveLength(1);await finish(s);});
 test("issued CAS has one winner and restart never grants another issue",async()=>{const s=await setup(),{intent}=await s.journal.reserveMutation(gate,ACTION,"deliver",request);expect((await Promise.all([s.journal.issueMutation(gate,intent),s.peer.issueMutation(gate,intent)])).sort()).toEqual([false,true]);await s.journal.close();const issued=await s.peer.readMutation(gate,ACTION);expect(issued?.status).toBe("issued");expect(await s.peer.issueMutation(gate,issued!)).toBe(false);await expect(s.peer.reserveMutation(gate,SECOND,"deliver",request)).rejects.toThrow();await s.peer.close();});
 test("terminal replay cannot release a newer owner or change evidence",async()=>{const s=await setup(),{intent}=await s.journal.reserveMutation(gate,ACTION,"deliver",request);await s.journal.issueMutation(gate,intent);const issued=(await s.journal.readMutation(gate,ACTION))!;const settled=await s.journal.verifyMutation(gate,issued,proof);expect(settled.status).toBe("verified");expect((await s.peer.reserveMutation(gate,SECOND,"exclude",request)).created).toBe(true);expect(await s.journal.verifyMutation(gate,issued,proof)).toEqual(settled);await expect(s.journal.reserveMutation(gate,GEN,"deliver",request)).rejects.toThrow();await expect(s.journal.verifyMutation(gate,issued,{...proof,evidence:{changed:true}})).rejects.toThrow();await finish(s);});
 test("preexisting result can settle without inventing an issued effect",async()=>{const s=await setup(),{intent}=await s.journal.reserveMutation(gate,ACTION,"deliver",request);expect((await s.journal.verifyMutation(gate,intent,proof)).status).toBe("verified");expect(await s.journal.issueMutation(gate,intent)).toBe(false);await finish(s);});
 test.each(["prepared","issued"])("same-account generation change retains %s and refuses rebind",async status=>{const s=await setup(),{intent}=await s.journal.reserveMutation(gate,ACTION,"deliver",request);if(status==="issued")await s.journal.issueMutation(gate,intent);const next={...gate,generation:NEXT};await s.peer.transition(gate,next);expect((await s.peer.readMutation(next,ACTION))?.generation).toBe(GEN);await expect(s.journal.issueMutation(gate,intent)).rejects.toThrow();await expect(s.peer.issueMutation(next,intent)).rejects.toThrow();await expect(s.peer.verifyMutation(next,intent,proof)).rejects.toThrow();await expect(s.peer.reserveMutation(next,SECOND,"deliver",request)).rejects.toThrow();await finish(s);});
 test("another account is independent while prior work remains retained",async()=>{const s=await setup();await s.journal.reserveMutation(gate,ACTION,"deliver",request);const next={...gate,accountId:OTHER,generation:NEXT};await s.peer.transition(gate,next);expect((await s.peer.reserveMutation(next,SECOND,"exclude",request)).created).toBe(true);await finish(s);});
 test.each(["currentMutation","mutations"])("lost %s is UNKNOWN instead of a new reservation",async store=>{const s=await setup();await s.journal.reserveMutation(gate,ACTION,"deliver",request);await raw(s.indexedDB,store,s=>s.clear());await expect(s.peer.readMutation(gate,ACTION)).rejects.toThrow();await expect(s.peer.reserveMutation(gate,SECOND,"deliver",request)).rejects.toThrow();await finish(s);});
 test("lost older verified history cannot advance the retained count frontier",async()=>{const s=await setup(),a=await s.journal.reserveMutation(gate,ACTION,"deliver",request);await s.journal.verifyMutation(gate,a.intent,proof);await s.journal.reserveMutation(gate,SECOND,"deliver",request);await raw(s.indexedDB,"mutations",s=>s.delete([ACCOUNT,ACTION]));await expect(s.peer.readMutation(gate,SECOND)).rejects.toThrow();await finish(s);});
 test("downgraded issued status cannot issue again",async()=>{const s=await setup(),{intent}=await s.journal.reserveMutation(gate,ACTION,"deliver",request);await s.journal.issueMutation(gate,intent);await raw(s.indexedDB,"mutations",s=>{const r=s.get([ACCOUNT,ACTION]);r.onsuccess=()=>s.put({...r.result,status:"prepared"},[ACCOUNT,ACTION]);});await expect(s.peer.issueMutation(gate,intent)).rejects.toThrow();await finish(s);});
 test.each(["requestText","requestHash","requestBytes"])("changed %s never becomes authority",async field=>{const s=await setup(),{intent}=await s.journal.reserveMutation(gate,ACTION,"deliver",request);await raw(s.indexedDB,"mutations",s=>{const r=s.get([ACCOUNT,ACTION]);r.onsuccess=()=>s.put({...r.result,[field]:field==="requestBytes"?1:field==="requestHash"?"b".repeat(64):"{}"},[ACCOUNT,ACTION]);});await expect(s.peer.readMutation(gate,ACTION)).rejects.toThrow();await expect(s.peer.issueMutation(gate,intent)).rejects.toThrow();await finish(s);});
 test.each(["prepared","issued"])("stored operation retargeting of %s intent fails after restart",async status=>{
  const s=await setup(),{intent}=await s.journal.reserveMutation(gate,ACTION,"deliver",request);
  if(status==="issued")await s.journal.issueMutation(gate,intent);
  await raw(s.indexedDB,"mutations",store=>{const r=store.get([ACCOUNT,ACTION]);r.onsuccess=()=>store.put({...r.result,operation:"exclude"},[ACCOUNT,ACTION]);});
  await s.journal.close();await expect(s.peer.readMutation(gate,ACTION)).rejects.toThrow();
  await expect(s.peer.issueMutation(gate,intent)).rejects.toThrow();await s.peer.close();
 });
 test("expiry during hashing cannot commit",async()=>{const s=await setup(),original=crypto.subtle.digest.bind(crypto.subtle);let release!:()=>void,now=0;const wait=new Promise<void>(r=>{release=r;});const clock=vi.spyOn(performance,"now").mockImplementation(()=>now),hash=vi.spyOn(crypto.subtle,"digest").mockImplementation(async(...args)=>{await wait;return original(...args);});try{const pending=s.journal.reserveMutation(gate,ACTION,"deliver",request,10);now=11;release();await expect(pending).rejects.toThrow();}finally{hash.mockRestore();clock.mockRestore();}expect(await s.journal.readMutation(gate,ACTION)).toBeNull();await finish(s);});
 test("expired resumed IDB callback cannot publish ownership",async()=>{const s=await setup(),get=IDBObjectStore.prototype.get;let expired=false;const clock=vi.spyOn(performance,"now").mockImplementation(()=>expired?11:0),read=vi.spyOn(IDBObjectStore.prototype,"get").mockImplementation(function(this:IDBObjectStore,key){if(this.name==="currentMutation")expired=true;return get.call(this,key);});try{await expect(s.journal.reserveMutation(gate,ACTION,"deliver",request,10)).rejects.toThrow();}finally{read.mockRestore();clock.mockRestore();}expect(await s.journal.readMutation(gate,ACTION)).toBeNull();await finish(s);});
 test("accessors and oversized requests are refused before reservation",async()=>{const s=await setup();let calls=0;const input={};Object.defineProperty(input,"secret",{enumerable:true,get(){calls++;return "bad";}});await expect(s.journal.reserveMutation(gate,ACTION,"deliver",input)).rejects.toThrow();expect(calls).toBe(0);await expect(s.journal.reserveMutation(gate,ACTION,"deliver",{data:"x".repeat(65536)})).rejects.toThrow();expect(await s.journal.readMutation(gate,ACTION)).toBeNull();await finish(s);});
 test.each(["unknown","deleted"])("delivery cannot settle as %s",async outcome=>{const s=await setup(),{intent}=await s.journal.reserveMutation(gate,ACTION,"deliver",request);await expect(s.journal.verifyMutation(gate,intent,{...proof,outcome})).rejects.toThrow();expect((await s.journal.readMutation(gate,ACTION))?.status).toBe("prepared");await finish(s);});
});

async function oldDatabase(indexedDB:IDBFactory,version:number,broken=false){
 const open=indexedDB.open("fulcra-attention-v1",version);await new Promise<void>((resolve,reject)=>{open.onupgradeneeded=()=>{
  const db=open.result,gates=db.createObjectStore("gate");gates.put({version:1,initialized:true},"format");gates.put(gate,"current");
  db.createObjectStore("generations").put(true,GEN);
  db.createObjectStore("creations").put({version:1,accountId:ACCOUNT,specKey:"collect.attention.v1",fingerprint:"a".repeat(64),status:"resolved",createdUnderGeneration:GEN,destination:`Event/${OTHER}`},[ACCOUNT,"collect.attention.v1"]);
  if(version>=2)db.createObjectStore("tags").createIndex("accountName",["accountId","name"],{unique:!broken});
  if(version>=3)db.createObjectStore("outbox").put({original:"synthetic opaque queue bytes"},[ACCOUNT,"synthetic",ACTION]);
 };open.onerror=()=>reject(open.error);open.onsuccess=()=>{open.result.close();resolve();};});
}
async function inspectDatabase(indexedDB:IDBFactory){const open=indexedDB.open("fulcra-attention-v1");return new Promise<{version:number;stores:string[];queue:unknown}>((resolve,reject)=>{open.onerror=()=>reject(open.error);open.onsuccess=()=>{
 const db=open.result,version=db.version,stores=[...db.objectStoreNames];if(!stores.includes("outbox")){db.close();resolve({version,stores,queue:null});return;}
 const tx=db.transaction("outbox","readonly"),r=tx.objectStore("outbox").get([ACCOUNT,"synthetic",ACTION]);let queue:unknown;
 r.onsuccess=()=>{queue=r.result;};tx.oncomplete=()=>{db.close();resolve({version,stores,queue});};tx.onabort=()=>reject(tx.error);
 };});}
describe("ownership metadata, upgrade and capacity boundaries",()=>{
 test.each([1,2,3])("v%s upgrade preserves original gate, generation, type and queued bytes",async version=>{
  const indexedDB=new IDBFactory();await oldDatabase(indexedDB,version);const journal=new V1Journal({indexedDB});
  expect(await journal.readGate()).toEqual(gate);expect((await journal.readCreation(gate,"collect.attention.v1","a".repeat(64)))?.createdUnderGeneration).toBe(GEN);
  expect(await journal.readMutation(gate,ACTION)).toBeNull();await journal.close();const after=await inspectDatabase(indexedDB);
  expect(after.version).toBe(5);expect(after.stores).toContain("currentMutation");expect(after.stores).toContain("mutations");
  if(version===3)expect(after.queue).toEqual({original:"synthetic opaque queue bytes"});
 });
 test("malformed old index aborts the whole upgrade before new stores commit",async()=>{
  const indexedDB=new IDBFactory();await oldDatabase(indexedDB,3,true);const journal=new V1Journal({indexedDB});
  await expect(journal.readGate()).rejects.toThrow();const after=await inspectDatabase(indexedDB);expect(after.version).toBe(3);expect(after.stores).not.toContain("mutations");expect(after.queue).toEqual({original:"synthetic opaque queue bytes"});
 });
 test("quota failure cannot publish an owner without its immutable intent",async()=>{
  const s=await setup(),add=IDBObjectStore.prototype.add;
  const fail=vi.spyOn(IDBObjectStore.prototype,"add").mockImplementation(function(this:IDBObjectStore,...args){if(this.name==="mutations")throw new DOMException("Synthetic quota failure","QuotaExceededError");return add.apply(this,args);});
  try{await expect(s.journal.reserveMutation(gate,ACTION,"deliver",request)).rejects.toThrow();}finally{fail.mockRestore();}
  expect(await s.journal.readMutation(gate,ACTION)).toBeNull();expect((await s.journal.reserveMutation(gate,ACTION,"deliver",request)).created).toBe(true);await finish(s);
 });
 test("the transaction rolls back both rows when current-owner publication fails",async()=>{
  const s=await setup(),put=IDBObjectStore.prototype.put;
  const fail=vi.spyOn(IDBObjectStore.prototype,"put").mockImplementation(function(this:IDBObjectStore,...args){if(this.name==="currentMutation")throw new DOMException("Synthetic owner failure","QuotaExceededError");return put.apply(this,args);});
  try{await expect(s.journal.reserveMutation(gate,ACTION,"deliver",request)).rejects.toThrow();}finally{fail.mockRestore();}
  expect(await s.journal.readMutation(gate,ACTION)).toBeNull();await finish(s);
 });
 test("a moved physical key cannot masquerade as a missing action",async()=>{
  const s=await setup(),first=await s.journal.reserveMutation(gate,ACTION,"deliver",request);await s.journal.verifyMutation(gate,first.intent,proof);
  const second=await s.journal.reserveMutation(gate,SECOND,"deliver",request);await s.journal.verifyMutation(gate,second.intent,proof);
  await raw(s.indexedDB,"mutations",store=>{const r=store.get([ACCOUNT,ACTION]);r.onsuccess=()=>{store.delete([ACCOUNT,ACTION]);store.put(r.result,[ACCOUNT,GEN]);};});
  await expect(s.peer.readMutation(gate,ACTION)).rejects.toThrow();await expect(s.peer.reserveMutation(gate,ACTION,"deliver",request)).rejects.toThrow();await finish(s);
 });
 test("an object hash is rejected without invoking its coercion hooks",async()=>{
  const s=await setup(),{intent}=await s.journal.reserveMutation(gate,ACTION,"deliver",request);let calls=0;
  await expect(s.peer.issueMutation(gate,{...intent,requestHash:{toString(){calls++;return "a".repeat(64);}}} as unknown as typeof intent)).rejects.toThrow();expect(calls).toBe(0);await finish(s);
 });
 test("gate change during hashing cannot commit a stale reservation",async()=>{
  const s=await setup(),original=crypto.subtle.digest.bind(crypto.subtle);let release!:()=>void;const wait=new Promise<void>(r=>{release=r;});
  const hash=vi.spyOn(crypto.subtle,"digest").mockImplementation(async(...args)=>{await wait;return original(...args);});
  try{const pending=s.journal.reserveMutation(gate,ACTION,"deliver",request);await s.peer.transition(gate,{...gate,generation:NEXT});release();await expect(pending).rejects.toThrow();}
  finally{release();hash.mockRestore();}
  expect(await s.peer.readMutation({...gate,generation:NEXT},ACTION)).toBeNull();await finish(s);
 });
 test("byte capacity fails visibly without pruning verified history",async()=>{
  const s=await setup();let count=0,first:string|undefined;
  try{for(let i=0;i<600;i++){const id=`00000000-0000-4000-8000-${i.toString().padStart(12,"0")}`;const r=await s.journal.reserveMutation(gate,id,"deliver",{reference:"x".repeat(65000)});first??=id;await s.journal.verifyMutation(gate,r.intent,proof);count++;}}
  catch(error){expect(String(error)).toMatch(/full/);}
  expect(count).toBeGreaterThan(500);expect(count).toBeLessThan(600);expect((await s.peer.readMutation(gate,first!))?.status).toBe("verified");await finish(s);
 },15000);
});
