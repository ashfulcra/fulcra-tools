// @vitest-environment node
import { IDBFactory, IDBObjectStore } from "fake-indexeddb";
import { describe, expect, test, vi } from "vitest";
import { V1Http } from "../../src/relayless/v1Http";
import { V1Journal, type V1Gate } from "../../src/relayless/v1Journal";
import { attentionV1Contract } from "../../src/relayless/v1Wire";
import { attentionV1Fingerprint } from "../../src/relayless/v1AttentionSpec";
import { resolveAttentionV1, verifyAttentionV1 } from "../../src/relayless/v1Destination";
const ACCOUNT="11111111-2222-4333-8444-555555555555", OTHER="22222222-3333-4444-8555-666666666666";
const GEN="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", NEXT="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff";
const TARGET="Event/33333333-4444-4555-8666-777777777777", ALTERNATE="Event/44444444-5555-4666-8777-888888888888";
const gate:V1Gate={version:1,accountId:ACCOUNT,generation:GEN,enabled:false};
function schema(){const c=attentionV1Contract();return {type:"object",properties:{
 id:{type:"string",format:"uuid"},sources:{type:"array",items:{type:"string"}},tags:{type:"array",items:{type:"string",format:"uuid"}},
 start_time:{type:"string",format:"date-time"},end_time:{type:"string",format:"date-time"},...c.fields},required:c.required_fields};}
function entry(id=TARGET,name="Attention"){return {id,name,api_version:"v1",recordable:true,deprecated:false,record_spec:{type:"event",schema:JSON.stringify(schema())}};}
async function setup(initial=true){
 const indexedDB=new IDBFactory(),journal=new V1Journal({indexedDB});await journal.transition(null,gate);
 const state={catalog:initial?[entry()]:[],schema:schema() as Record<string,any>,posts:0,reads:0,
  post:"redirect" as "redirect"|"lost"|"receipt"|"missing"|"wrongReceipt",publishCreate:true,beforePost:null as null|(()=>Promise<void>),duringSchema:null as null|(()=>Promise<void>),afterSchema:null as null|(()=>void)};
 const paths:string[]=[];
 const token="synthetic."+Buffer.from(JSON.stringify({"fulcradynamics.com/userid":ACCOUNT,exp:1700003600})).toString("base64url")+".signature";
 const makeHttp=(scope=gate,authority=journal)=>new V1Http({scope,journal:authority,readToken:async()=>token,now:()=>1700000000000,
  fetch:async(url,options={})=>{const path=new URL(String(url)).pathname;paths.push(path);
   if(path.endsWith("/info"))return Response.json({userid:ACCOUNT});
   if(path==="/input/v1/data_type"){state.reads++;state.afterSchema?.();return Response.json(state.catalog);}
   if(path.endsWith("/schema")){await state.duringSchema?.();return Response.json(state.schema);}
   if(options.method==="POST"&&path==="/input/v1/data_type/Event"){
    state.posts++;await state.beforePost?.();
    if(state.post!=="missing"&&state.publishCreate)state.catalog=[entry()];
    if(state.post==="lost")throw new Error("synthetic lost response");
    if(state.post==="receipt"||state.post==="missing")return Response.json({id:TARGET});
    if(state.post==="wrongReceipt")return Response.json({id:ALTERNATE});
    return new Response(null,{status:303,headers:{Location:"https://api.fulcradynamics.com/input/v1/data_type/"+TARGET}});
   }throw new Error("unexpected synthetic route");}});
 return {journal,indexedDB,state,paths,makeHttp,http:makeHttp()};
}
describe("durable Attention owned destination binding",()=>{
 test("a known verified receipt survives a gate transition before local retention",async()=>{
  const {journal,http,state,makeHttp}=await setup(false);state.post="receipt";state.publishCreate=false;
  const original=http.json.bind(http),changed={...gate,accountId:OTHER,generation:NEXT};
  vi.spyOn(http,"json").mockImplementation(async(path,options)=>{const result=await original(path,options);
   if(options?.method==="POST")await journal.transition(gate,changed);return result;});
  await expect(resolveAttentionV1({http,journal,allowCreate:true})).rejects.toThrow();
  const restored={...gate,generation:"cccccccc-dddd-4eee-8fff-000000000000"};await journal.transition(changed,restored);
  expect((await journal.readCreation(restored,"collect.attention.v1",await attentionV1Fingerprint()))?.destination).toBe(TARGET);
  state.catalog=[entry(ALTERNATE)];await expect(resolveAttentionV1({http:makeHttp(restored),journal,allowCreate:true})).rejects.toThrow();
  expect(state.posts).toBe(1);await journal.close();
 });
 test("transport and binding must share the identical authority journal",async()=>{
  const {journal,http,paths}=await setup();const other=new V1Journal({indexedDB:new IDBFactory()});await other.transition(null,gate);
  await expect(resolveAttentionV1({http,journal:other})).rejects.toThrow();expect(paths).toEqual([]);await other.close();await journal.close();
 });
 test("fresh owned/schema proof and final identity recheck precede immutable local pin",async()=>{
  const {journal,http,state}=await setup();const result=await resolveAttentionV1({http,journal});
  expect(result.dataType).toBe(TARGET);expect(result.accountId).toBe(ACCOUNT);expect(result.generation).toBe(GEN);
  expect(result.specFingerprint).toBe(await attentionV1Fingerprint());expect(result.schemaText).toBe(JSON.stringify(state.schema));
  expect(Object.isFrozen(result)).toBe(true);expect(state.reads).toBe(2);expect(state.posts).toBe(0);
  expect((await journal.readCreation(gate,"collect.attention.v1",await attentionV1Fingerprint()))?.destination).toBe(TARGET);await journal.close();
 });
 test("missing selection never creates unless explicitly allowed",async()=>{
  const {journal,http,state}=await setup(false);await expect(resolveAttentionV1({http,journal})).rejects.toThrow();expect(state.posts).toBe(0);expect(await journal.readCreation(gate,"collect.attention.v1",await attentionV1Fingerprint())).toBeNull();await journal.close();
 });
 test.each(["redirect","lost","receipt"] as const)("one-shot creation reconciles %s without following redirect or retrying POST",async post=>{
  const {journal,http,state,paths}=await setup(false);state.post=post;
  state.beforePost=async()=>{expect((await journal.readCreation(gate,"collect.attention.v1",await attentionV1Fingerprint()))?.status).toBe("pending");};
  expect((await resolveAttentionV1({http,journal,allowCreate:true})).dataType).toBe(TARGET);expect(state.posts).toBe(1);
  expect(paths.every(path=>!path.startsWith("/ingest/")&&path!=="/data/v1/catalog")).toBe(true);await journal.close();
 });
 test("unknown creation survives restart and an empty list cannot authorize another POST",async()=>{
  const {journal,indexedDB,http,state,makeHttp}=await setup(false);state.post="lost";state.publishCreate=false;
  await expect(resolveAttentionV1({http,journal,allowCreate:true})).rejects.toThrow();expect(state.posts).toBe(1);await journal.close();
  const reopened=new V1Journal({indexedDB}),current=makeHttp(gate,reopened);
  await expect(resolveAttentionV1({http:current,journal:reopened,allowCreate:true})).rejects.toThrow();expect(state.posts).toBe(1);
  state.catalog=[entry()];expect((await resolveAttentionV1({http:current,journal:reopened,allowCreate:true})).dataType).toBe(TARGET);expect(state.posts).toBe(1);await reopened.close();
 });
 test("known POST UUID cannot be substituted after missing visibility",async()=>{
  const {journal,http,state}=await setup(false);state.post="missing";await expect(resolveAttentionV1({http,journal,allowCreate:true})).rejects.toThrow();
  const prior=await journal.readCreation(gate,"collect.attention.v1",await attentionV1Fingerprint());expect(prior?.status).toBe("submitted");expect(prior?.destination).toBe(TARGET);
  state.catalog=[entry(ALTERNATE)];await expect(resolveAttentionV1({http,journal,allowCreate:true})).rejects.toThrow();expect(state.posts).toBe(1);await journal.close();
 });
 test("mismatched positive create receipt cannot bind a different name readback",async()=>{
  const {journal,http,state}=await setup(false);state.post="wrongReceipt";await expect(resolveAttentionV1({http,journal,allowCreate:true})).rejects.toThrow();expect((await journal.readCreation(gate,"collect.attention.v1",await attentionV1Fingerprint()))?.destination).toBe(ALTERNATE);await journal.close();
 });
 test("resolved pin survives rename and cannot be replaced by another explicit pin",async()=>{
  const {journal,http,state}=await setup();await resolveAttentionV1({http,journal});state.catalog=[entry(TARGET,"Renamed"),entry(ALTERNATE)];
  expect((await resolveAttentionV1({http,journal})).dataType).toBe(TARGET);await expect(resolveAttentionV1({http,journal,pinnedId:ALTERNATE,allowCreate:true})).rejects.toThrow();expect(state.posts).toBe(0);await journal.close();
 });
 test("explicit pin disambiguates only owned compatible types",async()=>{
  const {journal,http,state}=await setup();state.catalog.push(entry(ALTERNATE));
  await expect(resolveAttentionV1({http,journal})).rejects.toThrow();expect((await resolveAttentionV1({http,journal,pinnedId:ALTERNATE})).dataType).toBe(ALTERNATE);expect(state.posts).toBe(0);await journal.close();
 });
 test("caller choices are captured before the first digest await",async()=>{
  const {journal,http,state}=await setup();state.catalog.push(entry(ALTERNATE));const input={http,journal,pinnedId:TARGET,allowCreate:false};
  const pending=resolveAttentionV1(input);input.pinnedId=ALTERNATE;input.allowCreate=true;
  expect((await pending).dataType).toBe(TARGET);expect(state.posts).toBe(0);await journal.close();
 });
 test.each(["deprecated","wrongBase","schema","missingSchema"])("incompatible target %s cannot create a replacement",async kind=>{
  const {journal,http,state}=await setup();if(kind==="deprecated")state.catalog[0].deprecated=true;
  if(kind==="wrongBase"){state.catalog[0].id=TARGET.replace("Event/","Metric/");state.catalog[0].record_spec.type="metric";}
  if(kind==="schema")state.schema.properties.note.type="number";
  if(kind==="missingSchema")state.catalog[0].record_spec.schema="";
  await expect(resolveAttentionV1({http,journal,allowCreate:true})).rejects.toThrow();expect(state.posts).toBe(0);await journal.close();
 });
 test.each(["retired","schemaChanged"])("identity recheck after schema holds %s",async kind=>{
  const {journal,http,state}=await setup();state.duringSchema=async()=>{state.afterSchema=()=>{if(kind==="retired")state.catalog[0].deprecated=true;else state.catalog[0].record_spec.schema+=' ';};};
  await expect(resolveAttentionV1({http,journal})).rejects.toThrow();expect(await journal.readCreation(gate,"collect.attention.v1",await attentionV1Fingerprint())).toBeNull();await journal.close();
 });
 test("account generation change during schema cannot publish a pin",async()=>{
  const {journal,http,state}=await setup();state.duringSchema=async()=>{await journal.transition(gate,{...gate,generation:NEXT});};
  await expect(resolveAttentionV1({http,journal})).rejects.toThrow();await journal.close();
 });
 test("late POST after an account switch stays unbound and can reconcile under fresh original-account authority",async()=>{
  const {journal,http,state,makeHttp}=await setup(false);const other={...gate,accountId:OTHER,generation:NEXT};
  state.beforePost=async()=>{await journal.transition(gate,other);};
  await expect(resolveAttentionV1({http,journal,allowCreate:true})).rejects.toThrow();
  expect(await journal.readCreation(other,"collect.attention.v1",await attentionV1Fingerprint())).toBeNull();
  const current={...gate,generation:"cccccccc-dddd-4eee-8fff-000000000000"};await journal.transition(other,current);
  expect((await journal.readCreation(current,"collect.attention.v1",await attentionV1Fingerprint()))?.status).toBe("pending");
  expect((await resolveAttentionV1({http:makeHttp(current),journal,allowCreate:true})).dataType).toBe(TARGET);expect(state.posts).toBe(1);await journal.close();
 });
 test("a stale served-schema view cannot hide an incompatible current input schema",async()=>{
  const {journal,http,state}=await setup();const current=schema() as Record<string,any>;current.properties.note.maxLength=1;state.catalog[0].record_spec.schema=JSON.stringify(current);
  await expect(resolveAttentionV1({http,journal})).rejects.toThrow();expect(await journal.readCreation(gate,"collect.attention.v1",await attentionV1Fingerprint())).toBeNull();await journal.close();
 });
 test("two live contexts dispatch at most one creation",async()=>{
  const {journal,http,state}=await setup(false);const outcomes=await Promise.allSettled([resolveAttentionV1({http,journal,allowCreate:true}),resolveAttentionV1({http,journal,allowCreate:true})]);
  expect(state.posts).toBe(1);expect(outcomes.some(row=>row.status==="fulfilled")).toBe(true);await journal.close();
 });
});

async function alterCreation(indexedDB:IDBFactory,change:(row:any)=>any){const r=indexedDB.open("fulcra-attention-v1");await new Promise<void>((resolve,reject)=>{r.onerror=()=>reject(r.error);r.onsuccess=()=>{const db=r.result,tx=db.transaction("creations","readwrite"),store=tx.objectStore("creations"),q=store.get([ACCOUNT,"collect.attention.v1"]);q.onsuccess=()=>{const next=change(q.result);if(next===null)store.delete([ACCOUNT,"collect.attention.v1"]);else store.put(next,[ACCOUNT,"collect.attention.v1"]);};tx.oncomplete=()=>{db.close();resolve();};tx.onabort=()=>reject(tx.error);};});}
describe("fresh original Attention authority",()=>{
 test.each(["initial","duringSchema"])("submitted receipt cannot bypass %s first-binding ambiguity",async phase=>{const {journal,http,state}=await setup();const fp=await attentionV1Fingerprint();await journal.reserveCreation(gate,"collect.attention.v1",fp);await journal.retainCreationReceipt(gate,"collect.attention.v1",fp,TARGET);if(phase==="initial")state.catalog.push(entry(ALTERNATE));else state.duringSchema=async()=>{state.catalog.push(entry(ALTERNATE));};await expect(resolveAttentionV1({http,journal})).rejects.toThrow();expect((await journal.readCreation(gate,"collect.attention.v1",fp))?.status).toBe("submitted");expect(state.posts).toBe(0);await journal.close();});
 test("verify reproves original resolved binding without any local write",async()=>{const {journal,http,state}=await setup();await resolveAttentionV1({http,journal});const fp=await attentionV1Fingerprint(),prior=await journal.readCreation(gate,"collect.attention.v1",fp);const put=vi.spyOn(IDBObjectStore.prototype,"put"),add=vi.spyOn(IDBObjectStore.prototype,"add");try{const bound=await verifyAttentionV1({http,journal});expect(bound.dataType).toBe(TARGET);expect(bound.reservationGeneration).toBe(GEN);expect(Object.isFrozen(bound)).toBe(true);expect(await journal.readCreation(gate,"collect.attention.v1",fp)).toEqual(prior);expect(put).not.toHaveBeenCalled();expect(add).not.toHaveBeenCalled();expect(state.posts).toBe(0);}finally{put.mockRestore();add.mockRestore();await journal.close();}});
 test.each(["missing","pending","submitted"])("verify refuses %s original intent without healing it",async status=>{const {journal,http,state}=await setup();const fp=await attentionV1Fingerprint();if(status!=="missing")await journal.reserveCreation(gate,"collect.attention.v1",fp);if(status==="submitted")await journal.retainCreationReceipt(gate,"collect.attention.v1",fp,TARGET);const prior=await journal.readCreation(gate,"collect.attention.v1",fp),put=vi.spyOn(IDBObjectStore.prototype,"put"),add=vi.spyOn(IDBObjectStore.prototype,"add");try{await expect(verifyAttentionV1({http,journal,pinnedId:TARGET})).rejects.toThrow();expect(await journal.readCreation(gate,"collect.attention.v1",fp)).toEqual(prior);expect(put).not.toHaveBeenCalled();expect(add).not.toHaveBeenCalled();expect(state.reads).toBe(0);}finally{put.mockRestore();add.mockRestore();await journal.close();}});
 test.each(["missing","pending","submitted","reservation","destination"])("final original predicate holds %s mutation during proof",async change=>{const {journal,indexedDB,http,state}=await setup();await resolveAttentionV1({http,journal});state.duringSchema=async()=>{await alterCreation(indexedDB,row=>change==="missing"?null:{...row,...change==="reservation"?{createdUnderGeneration:NEXT}:change==="destination"?{destination:ALTERNATE}:{status:change,destination:change==="pending"?null:TARGET}});};await expect(verifyAttentionV1({http,journal})).rejects.toThrow();const row=await journal.readCreation(gate,"collect.attention.v1",await attentionV1Fingerprint());if(change==="missing")expect(row).toBeNull();else if(change==="reservation")expect(row?.createdUnderGeneration).toBe(NEXT);else if(change==="destination")expect(row?.destination).toBe(ALTERNATE);else expect(row?.status).toBe(change);expect(state.posts).toBe(0);await journal.close();});
 test("same-account new gate verifies the original reservation without rebinding",async()=>{const {journal,http,makeHttp}=await setup();await resolveAttentionV1({http,journal});const next={...gate,generation:NEXT};await journal.transition(gate,next);const result=await verifyAttentionV1({http:makeHttp(next),journal});expect(result.generation).toBe(NEXT);expect(result.reservationGeneration).toBe(GEN);expect((await journal.readCreation(next,"collect.attention.v1",await attentionV1Fingerprint()))?.createdUnderGeneration).toBe(GEN);await journal.close();});
 test.each(["input","served","retired","gate"])("readonly fresh proof refuses %s change",async kind=>{const {journal,http,state}=await setup();await resolveAttentionV1({http,journal});if(kind==="input")state.catalog[0].record_spec.schema+=' ';if(kind==="served")state.schema.properties.note.type="number";if(kind==="retired")state.catalog[0].deprecated=true;if(kind==="gate")state.duringSchema=async()=>{await journal.transition(gate,{...gate,generation:NEXT});};if(kind==="input")state.duringSchema=async()=>{state.catalog[0].record_spec.schema+=' ';};await expect(verifyAttentionV1({http,journal})).rejects.toThrow();expect(state.posts).toBe(0);await journal.close();});
 test("outer deadline ending with a paused monotonic clock prevents late setup publication",async()=>{const {journal,http,state}=await setup();const read=journal.readCreation.bind(journal);let release!:()=>void;const wait=new Promise<void>(r=>{release=r;}),clock=vi.spyOn(performance,"now").mockReturnValue(0),blocked=vi.spyOn(journal,"readCreation").mockImplementation(async(...args)=>{await wait;return read(...args);});const pending=resolveAttentionV1({http,journal,timeoutMs:10}),settled=pending.then(()=>"accepted",()=>"held");try{expect(await Promise.race([settled,new Promise(r=>setTimeout(()=>r("still waiting"),60))])).toBe("held");}finally{release();await settled;blocked.mockRestore();clock.mockRestore();}await new Promise(r=>setTimeout(r,5));expect(state.reads).toBe(0);expect(await journal.readCreation(gate,"collect.attention.v1",await attentionV1Fingerprint())).toBeNull();await journal.close();});
});
test("outer cancellation reaches a blocked setup transaction before any late write",async()=>{
 const {journal,indexedDB,http}=await setup(),db=await new Promise<IDBDatabase>((resolve,reject)=>{const r=indexedDB.open("fulcra-attention-v1");r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
 let hold=true,queued:Promise<unknown>|undefined;const original=journal.reserveCreation.bind(journal),clock=vi.spyOn(performance,"now").mockReturnValue(0);
 const reserve=vi.spyOn(journal,"reserveCreation").mockImplementation((...args)=>{const tx=db.transaction("creations","readwrite"),store=tx.objectStore("creations");const repeat=()=>{const r=store.get("synthetic keepalive");r.onsuccess=()=>{if(hold)repeat();};};repeat();queued=original(...args);return queued as ReturnType<typeof original>;});
 const pending=resolveAttentionV1({http,journal,timeoutMs:10}),settled=pending.then(()=>"accepted",()=>"held");
 try{expect(await Promise.race([settled,new Promise(r=>setTimeout(()=>r("still waiting"),100))])).toBe("held");}
 finally{hold=false;await queued?.catch(()=>{});reserve.mockRestore();clock.mockRestore();db.close();}
 expect(await journal.readCreation(gate,"collect.attention.v1",await attentionV1Fingerprint())).toBeNull();await journal.close();
});
test("returned verified receipt is retained after outer expiry without late binding",async()=>{
 const {journal,http,state}=await setup(false);state.post="receipt";state.publishCreate=false;
 let release!:()=>void,entered!:()=>void,retained!:()=>void;const wait=new Promise<void>(r=>{release=r;}),seen=new Promise<void>(r=>{entered=r;}),fact=new Promise<void>(r=>{retained=r;});
 const original=http.json.bind(http),save=journal.retainCreationReceipt.bind(journal);const delayed=vi.spyOn(http,"json").mockImplementation(async(path,options)=>{const result=await original(path,options);if(options?.method==="POST"){entered();await wait;}return result;});
 const saved=vi.spyOn(journal,"retainCreationReceipt").mockImplementation(async(...args)=>{await save(...args);retained();});
 const clock=vi.spyOn(performance,"now").mockReturnValue(0);vi.useFakeTimers({toFake:["setTimeout","clearTimeout"]});
 const settled=resolveAttentionV1({http,journal,allowCreate:true,timeoutMs:10000}).then(()=>"accepted",()=>"held");
 try{await seen;await vi.advanceTimersByTimeAsync(10001);expect(await settled).toBe("held");release();await fact;const prior=await journal.readCreation(gate,"collect.attention.v1",await attentionV1Fingerprint());expect(prior?.status).toBe("submitted");expect(prior?.destination).toBe(TARGET);expect(state.posts).toBe(1);expect(state.reads).toBe(1);}
 finally{release();vi.useRealTimers();clock.mockRestore();delayed.mockRestore();saved.mockRestore();await journal.close();}
});
test.each(["reserve","resolve"])("creation %s honours cancellation before queued callbacks",async mode=>{
 const {journal}=await setup(),fp=await attentionV1Fingerprint();if(mode==="resolve")await journal.reserveCreation(gate,"collect.attention.v1",fp);
 const cancelled=new AbortController(),pending=mode==="reserve"?journal.reserveCreation(gate,"collect.attention.v1",fp,undefined,cancelled.signal):journal.resolveCreation(gate,"collect.attention.v1",fp,TARGET,undefined,cancelled.signal);cancelled.abort();await expect(pending).rejects.toThrow();const prior=await journal.readCreation(gate,"collect.attention.v1",fp);if(mode==="reserve")expect(prior).toBeNull();else expect(prior?.status).toBe("pending");await journal.close();
});
test.each(["reserve","resolve"])("cancellation after %s write aborts active transaction and detaches listener",async mode=>{
 const {journal}=await setup(),fp=await attentionV1Fingerprint();if(mode==="resolve")await journal.reserveCreation(gate,"collect.attention.v1",fp);
 const cancellation=new AbortController(),method=mode==="reserve"?"add":"put",original=IDBObjectStore.prototype[method];
 const addListener=vi.spyOn(cancellation.signal,"addEventListener"),removeListener=vi.spyOn(cancellation.signal,"removeEventListener");
 const write=vi.spyOn(IDBObjectStore.prototype,method).mockImplementation(function(this:IDBObjectStore,...args){const r=original.apply(this,args);if(this.name==="creations")r.addEventListener("success",()=>cancellation.abort());return r;});
 try{const pending=mode==="reserve"?journal.reserveCreation(gate,"collect.attention.v1",fp,undefined,cancellation.signal):journal.resolveCreation(gate,"collect.attention.v1",fp,TARGET,undefined,cancellation.signal);await expect(pending).rejects.toThrow();expect(addListener).toHaveBeenCalled();expect(removeListener).toHaveBeenCalled();}
 finally{write.mockRestore();addListener.mockRestore();removeListener.mockRestore();}
 const row=await journal.readCreation(gate,"collect.attention.v1",fp);if(mode==="reserve")expect(row).toBeNull();else expect(row?.status).toBe("pending");await journal.close();
});
